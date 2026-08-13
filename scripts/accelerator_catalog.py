#!/usr/bin/env python3
"""Validate and maintain GCO's EC2 accelerator catalog.

Normal CI is deliberately offline: ``validate`` compares the checked-in catalog
with Karpenter NodePools, the capacity-history watch lists in ``cdk.json`` and
``ConfigLoader``, and the Spot Placement Score instance pools declared below. The monthly dependency workflow runs ``check-online`` to
compare that catalog with the union of NVIDIA GPU and AWS Neuron instance types
returned by EC2 in every enabled commercial Region.

Online reads are sequential and paginated. Botocore adaptive retries protect the
monthly scan from transient EC2 throttling without making the deterministic test
suite depend on credentials or a mutable cloud catalog.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = ROOT / "gco" / "config" / "accelerator_catalog.json"
DEFAULT_CDK_PATH = ROOT / "cdk.json"
DEFAULT_CONFIG_LOADER_PATH = ROOT / "gco" / "config" / "config_loader.py"
DEFAULT_MANIFESTS_PATH = ROOT / "lambda" / "kubectl-applier-simple" / "manifests"

Accelerator = Literal["nvidia", "neuron"]
Lifecycle = Literal["active", "announced", "deprecated", "end-of-life"]

_ALLOWED_ACCELERATORS = {"nvidia", "neuron"}
_ALLOWED_LIFECYCLES = {"active", "announced", "deprecated", "end-of-life"}
_DEPRECATED_LIFECYCLES = {"deprecated", "end-of-life"}
_ENABLED_REGION_STATUSES = {"opt-in-not-required", "opted-in"}
_EC2_TO_KUBERNETES_ARCH = {"x86_64": "amd64", "arm64": "arm64"}


class CatalogError(ValueError):
    """Raised when checked-in catalog or repository input is malformed."""


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CatalogError(f"{label} must be a JSON/YAML object with string keys")
    return cast(dict[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{label} must be a non-empty string")
    return value


def _utc_timestamp(value: object, label: str) -> str:
    timestamp = _string(value, label)
    if "T" not in timestamp or not timestamp.endswith("Z"):
        raise CatalogError(f"{label} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(f"{timestamp[:-1]}+00:00")
    except ValueError as exc:
        raise CatalogError(f"{label} must be an ISO 8601 UTC timestamp ending in Z") from exc
    return timestamp


def _current_utc_timestamp() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _string_list(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise CatalogError(f"{label} must be a list of non-empty strings")
    result = tuple(cast(list[str], value))
    if not allow_empty and not result:
        raise CatalogError(f"{label} must not be empty")
    return result


def _family_for_instance_type(instance_type: str) -> str:
    family, separator, size = instance_type.partition(".")
    if not separator or not family or not size:
        raise CatalogError(f"invalid EC2 instance type in catalog: {instance_type!r}")
    return family


@dataclass(frozen=True)
class FamilyPolicy:
    """Reviewed lifecycle and generation metadata for one EC2 family."""

    name: str
    accelerator: Accelerator
    architectures: tuple[str, ...]
    track: str
    generation: int
    lifecycle: Lifecycle
    manifest_allowed: bool
    reason: str | None
    replacements: tuple[str, ...]

    @classmethod
    def from_mapping(cls, name: str, value: object) -> FamilyPolicy:
        raw = _mapping(value, f"families.{name}")
        accelerator_value = _string(raw.get("accelerator"), f"families.{name}.accelerator")
        if accelerator_value not in _ALLOWED_ACCELERATORS:
            raise CatalogError(
                f"families.{name}.accelerator must be one of {sorted(_ALLOWED_ACCELERATORS)}"
            )
        lifecycle_value = _string(raw.get("lifecycle"), f"families.{name}.lifecycle")
        if lifecycle_value not in _ALLOWED_LIFECYCLES:
            raise CatalogError(
                f"families.{name}.lifecycle must be one of {sorted(_ALLOWED_LIFECYCLES)}"
            )
        generation = raw.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise CatalogError(f"families.{name}.generation must be a non-negative integer")
        if lifecycle_value == "announced" and "manifest_allowed" not in raw:
            raise CatalogError(
                f"families.{name}.manifest_allowed is required for announced families"
            )
        manifest_allowed_value = raw.get("manifest_allowed", lifecycle_value == "active")
        if not isinstance(manifest_allowed_value, bool):
            raise CatalogError(f"families.{name}.manifest_allowed must be a boolean")
        reason_value = raw.get("reason")
        if reason_value is not None and not isinstance(reason_value, str):
            raise CatalogError(f"families.{name}.reason must be a string when present")
        replacements_value = raw.get("replacements", [])
        replacements = _string_list(
            replacements_value,
            f"families.{name}.replacements",
            allow_empty=True,
        )
        return cls(
            name=name,
            accelerator=cast(Accelerator, accelerator_value),
            architectures=_string_list(raw.get("architectures"), f"families.{name}.architectures"),
            track=_string(raw.get("track"), f"families.{name}.track"),
            generation=generation,
            lifecycle=cast(Lifecycle, lifecycle_value),
            manifest_allowed=manifest_allowed_value,
            reason=reason_value,
            replacements=replacements,
        )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "accelerator": self.accelerator,
            "architectures": list(self.architectures),
            "track": self.track,
            "generation": self.generation,
            "lifecycle": self.lifecycle,
        }
        default_allowed = self.lifecycle == "active"
        if self.manifest_allowed != default_allowed or self.lifecycle == "announced":
            result["manifest_allowed"] = self.manifest_allowed
        if self.reason is not None:
            result["reason"] = self.reason
        if self.replacements:
            result["replacements"] = list(self.replacements)
        return result


@dataclass(frozen=True)
class Catalog:
    """Normalized checked-in accelerator catalog."""

    schema_version: int
    last_refreshed_at: str
    source: dict[str, object]
    families: dict[str, FamilyPolicy]
    instance_types: tuple[str, ...]

    @classmethod
    def load(cls, path: Path = DEFAULT_CATALOG_PATH) -> Catalog:
        try:
            parsed: object = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogError(f"cannot read accelerator catalog {path}: {exc}") from exc
        raw = _mapping(parsed, str(path))
        schema_version = raw.get("schema_version")
        if schema_version != 1:
            raise CatalogError(f"{path}: schema_version must be 1")
        last_refreshed_at = _utc_timestamp(
            raw.get("last_refreshed_at"), f"{path}: last_refreshed_at"
        )
        source = _mapping(raw.get("source"), f"{path}: source")
        family_values = _mapping(raw.get("families"), f"{path}: families")
        families = {
            name: FamilyPolicy.from_mapping(name, value)
            for name, value in sorted(family_values.items())
        }
        instance_types = _string_list(raw.get("instance_types"), f"{path}: instance_types")
        if instance_types != tuple(sorted(instance_types)):
            raise CatalogError(f"{path}: instance_types must be sorted lexicographically")
        if len(instance_types) != len(set(instance_types)):
            raise CatalogError(f"{path}: instance_types contains duplicates")
        for instance_type in instance_types:
            family = _family_for_instance_type(instance_type)
            if family not in families:
                raise CatalogError(
                    f"{path}: {instance_type} has no reviewed families.{family} policy"
                )
        return cls(
            schema_version=1,
            last_refreshed_at=last_refreshed_at,
            source=source,
            families=families,
            instance_types=instance_types,
        )

    @property
    def live_families(self) -> frozenset[str]:
        return frozenset(_family_for_instance_type(item) for item in self.instance_types)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "last_refreshed_at": self.last_refreshed_at,
            "source": self.source,
            "families": {
                name: policy.to_mapping() for name, policy in sorted(self.families.items())
            },
            "instance_types": list(self.instance_types),
        }


@dataclass(frozen=True)
class NodePoolReference:
    """Accelerator families and architectures selected by one NodePool."""

    path: Path
    name: str
    families: tuple[str, ...]
    architectures: tuple[str, ...]

    @property
    def location(self) -> str:
        try:
            display_path = self.path.relative_to(ROOT)
        except ValueError:
            display_path = self.path
        return f"{display_path} (NodePool {self.name})"


@dataclass(frozen=True)
class Finding:
    """One deterministic, actionable offline validation failure."""

    code: str
    title: str
    detail: str
    recommendation: str
    locations: tuple[str, ...] = ()

    def sort_key(self) -> tuple[str, str, tuple[str, ...]]:
        return (self.code, self.title, self.locations)


@dataclass(frozen=True)
class ValidationReport:
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_text(self) -> str:
        if self.ok:
            return (
                "Accelerator catalog validation passed: NodePools, both watch lists, "
                "and the instance pools are current.\n"
            )
        lines = [f"Accelerator catalog validation failed with {len(self.findings)} finding(s):"]
        for finding in self.findings:
            lines.append(f"\nERROR [{finding.code}] {finding.title}")
            if finding.locations:
                lines.append(f"  Location: {', '.join(finding.locations)}")
            lines.append(f"  Why: {finding.detail}")
            lines.append(f"  Recommended change: {finding.recommendation}")
        return "\n".join(lines) + "\n"

    def to_markdown(self) -> str:
        status = "PASS" if self.ok else "ACTION REQUIRED"
        lines = [
            "## Accelerator catalog and NodePool policy",
            "",
            f"**Status: {status}.**",
        ]
        if self.ok:
            lines.extend(
                [
                    "",
                    "The checked-in EC2 catalog, Karpenter families, capacity-history "
                    "watch lists in `cdk.json` and `ConfigLoader`, and the Spot "
                    "Placement Score instance pools are synchronized.",
                ]
            )
            return "\n".join(lines) + "\n"
        lines.extend(["", f"{len(self.findings)} actionable finding(s):"])
        for finding in self.findings:
            lines.extend(["", f"### {finding.title}", ""])
            if finding.locations:
                lines.append(f"- **Location:** {', '.join(finding.locations)}")
            lines.append(f"- **Why:** {finding.detail}")
            lines.append(f"- **Recommended change:** {finding.recommendation}")
        return "\n".join(lines) + "\n"


def load_nodepools(manifests_path: Path = DEFAULT_MANIFESTS_PATH) -> tuple[NodePoolReference, ...]:
    """Load every NodePool manifest that declares explicit instance families."""
    pools: list[NodePoolReference] = []
    for path in sorted(manifests_path.glob("*nodepool*.yaml")):
        try:
            parsed_documents: list[object] = list(yaml.safe_load_all(path.read_text()))
        except (OSError, yaml.YAMLError) as exc:
            raise CatalogError(f"cannot read NodePool manifest {path}: {exc}") from exc
        root: dict[str, object] | None = None
        for document_index, parsed in enumerate(parsed_documents):
            if parsed is None:
                continue
            candidate = _mapping(parsed, f"{path}: document {document_index + 1}")
            if candidate.get("kind") == "NodePool":
                root = candidate
                break
        if root is None:
            continue
        metadata = _mapping(root.get("metadata"), f"{path}: metadata")
        spec = _mapping(root.get("spec"), f"{path}: spec")
        template = _mapping(spec.get("template"), f"{path}: spec.template")
        template_spec = _mapping(template.get("spec"), f"{path}: spec.template.spec")
        requirements_value = template_spec.get("requirements", [])
        if not isinstance(requirements_value, list):
            raise CatalogError(f"{path}: spec.template.spec.requirements must be a list")
        families: tuple[str, ...] = ()
        architectures: tuple[str, ...] = ()
        for index, requirement_value in enumerate(requirements_value):
            requirement = _mapping(requirement_value, f"{path}: requirements[{index}]")
            key = requirement.get("key")
            if key == "eks.amazonaws.com/instance-family":
                families = _string_list(
                    requirement.get("values"), f"{path}: instance-family values"
                )
            elif key == "kubernetes.io/arch":
                architectures = _string_list(
                    requirement.get("values"), f"{path}: architecture values"
                )
        if families:
            pools.append(
                NodePoolReference(
                    path=path,
                    name=_string(metadata.get("name"), f"{path}: metadata.name"),
                    families=families,
                    architectures=architectures,
                )
            )
    return tuple(pools)


def validate_nodepools(
    catalog: Catalog, nodepools: tuple[NodePoolReference, ...]
) -> tuple[Finding, ...]:
    """Validate lifecycle, architecture, and newest-generation NodePool policy."""
    findings: list[Finding] = []
    referenced_families = {family for pool in nodepools for family in pool.families}

    for pool in nodepools:
        for family in pool.families:
            policy = catalog.families.get(family)
            if policy is None:
                findings.append(
                    Finding(
                        code="unknown-family",
                        title=f"{pool.name} references unreviewed family {family}",
                        locations=(pool.location,),
                        detail=(
                            "The family has no lifecycle, architecture, or generation policy in "
                            "gco/config/accelerator_catalog.json."
                        ),
                        recommendation=(
                            f"Review {family} against EC2, add explicit family metadata to the "
                            "catalog, then rerun this validator; do not silently allow unknown "
                            "families."
                        ),
                    )
                )
                continue
            if not policy.manifest_allowed or policy.lifecycle in _DEPRECATED_LIFECYCLES:
                replacements = ", ".join(policy.replacements) or "a reviewed active family"
                findings.append(
                    Finding(
                        code="deprecated-family",
                        title=(f"{pool.name} references {policy.lifecycle} family {policy.name}"),
                        locations=(pool.location,),
                        detail=policy.reason
                        or f"Family {policy.name} is marked {policy.lifecycle} by project policy.",
                        recommendation=(
                            f"Remove {policy.name} from this NodePool and use {replacements} "
                            "instead. Keep capacity-history observation separate from scheduling "
                            "eligibility."
                        ),
                    )
                )
            if pool.architectures and not set(pool.architectures).intersection(
                policy.architectures
            ):
                findings.append(
                    Finding(
                        code="architecture-mismatch",
                        title=f"{pool.name} cannot launch {policy.name}",
                        locations=(pool.location,),
                        detail=(
                            f"The NodePool requires {list(pool.architectures)}, but {policy.name} "
                            f"is cataloged for {list(policy.architectures)}."
                        ),
                        recommendation=(
                            f"Move {policy.name} to an architecture-compatible NodePool or correct "
                            "the NodePool's kubernetes.io/arch requirement."
                        ),
                    )
                )

    active_by_track: dict[str, list[FamilyPolicy]] = {}
    for family in catalog.live_families:
        policy = catalog.families[family]
        if policy.lifecycle == "active":
            active_by_track.setdefault(policy.track, []).append(policy)

    for track, policies in sorted(active_by_track.items()):
        latest_generation = max(policy.generation for policy in policies)
        latest = sorted(
            policy.name for policy in policies if policy.generation == latest_generation
        )
        if referenced_families.intersection(latest):
            continue
        latest_architectures = {
            architecture
            for policy in policies
            if policy.generation == latest_generation
            for architecture in policy.architectures
        }
        candidates: list[NodePoolReference] = []
        for pool in nodepools:
            pool_tracks = {
                catalog.families[family].track
                for family in pool.families
                if family in catalog.families
            }
            architecture_matches = not pool.architectures or bool(
                latest_architectures.intersection(pool.architectures)
            )
            if track in pool_tracks and architecture_matches:
                candidates.append(pool)
        locations = tuple(pool.location for pool in candidates)
        latest_display = ", ".join(latest)
        if locations:
            target = ", ".join(locations)
            recommendation = (
                f"Update {target}: add a reviewed family from [{latest_display}] after confirming "
                "EKS Auto Mode labels and workload compatibility."
            )
        else:
            recommendation = (
                f"Create an architecture-compatible NodePool for [{latest_display}], or document "
                f"why the {track} track is intentionally unsupported."
            )
        findings.append(
            Finding(
                code="newer-generation-unreferenced",
                title=f"New {track} generation is absent from all NodePools",
                locations=locations,
                detail=(
                    f"The EC2 catalog contains generation {latest_generation} family/families "
                    f"[{latest_display}], but no NodePool references any of them."
                ),
                recommendation=recommendation,
            )
        )

    return tuple(findings)


def _watch_list_findings(
    catalog: Catalog,
    watched: tuple[str, ...],
    *,
    location: Path,
    code_prefix: str,
    subject: str,
    target: str,
    peer: str,
) -> tuple[Finding, ...]:
    """Compare one capacity-history watch list with the normalized catalog."""
    findings: list[Finding] = []
    if len(watched) != len(set(watched)):
        duplicates = sorted(item for item in set(watched) if watched.count(item) > 1)
        findings.append(
            Finding(
                code=f"{code_prefix}-duplicates",
                title=f"{subject} contains duplicate instance types",
                locations=(str(location),),
                detail=f"Duplicate values: {', '.join(duplicates)}.",
                recommendation=f"Remove duplicate entries from {target}.",
            )
        )

    expected = set(catalog.instance_types)
    actual = set(watched)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        findings.append(
            Finding(
                code=f"{code_prefix}-missing",
                title=f"{subject} omits accelerator instance types",
                locations=(str(location),),
                detail=f"Missing {len(missing)} catalog type(s): {', '.join(missing)}.",
                recommendation=(
                    f"Add every listed type to {target} and mirror the same default in {peer}."
                ),
            )
        )
    if unexpected:
        findings.append(
            Finding(
                code=f"{code_prefix}-unexpected",
                title=f"{subject} contains types outside the catalog",
                locations=(str(location),),
                detail=f"Unexpected {len(unexpected)} type(s): {', '.join(unexpected)}.",
                recommendation=(
                    "Refresh and review the catalog before retaining these entries, or remove "
                    f"them from {target}."
                ),
            )
        )
    if not missing and not unexpected and watched != catalog.instance_types:
        findings.append(
            Finding(
                code=f"{code_prefix}-order",
                title=f"{subject} is not in normalized catalog order",
                locations=(str(location),),
                detail="The values are complete but their order differs from the checked-in catalog.",
                recommendation=(
                    f"Replace {target} with gco/config/accelerator_catalog.json instance_types so "
                    "future catalog refreshes produce reviewable diffs."
                ),
            )
        )
    return tuple(findings)


def validate_watch_instance_types(
    catalog: Catalog, cdk_path: Path = DEFAULT_CDK_PATH
) -> tuple[Finding, ...]:
    """Require cdk.json's capacity-history watch list to exactly match the catalog."""
    try:
        parsed: object = json.loads(cdk_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read {cdk_path}: {exc}") from exc
    root = _mapping(parsed, str(cdk_path))
    context = _mapping(root.get("context"), f"{cdk_path}: context")
    historical = _mapping(context.get("historical"), f"{cdk_path}: context.historical")
    watched = _string_list(
        historical.get("watch_instance_types"),
        f"{cdk_path}: context.historical.watch_instance_types",
    )
    return _watch_list_findings(
        catalog,
        watched,
        location=cdk_path,
        code_prefix="watch-list",
        subject="capacity-history watch list",
        target="context.historical.watch_instance_types",
        peer="ConfigLoader.get_capacity_history_config()",
    )


def _load_config_loader_watch_instance_types(
    config_loader_path: Path = DEFAULT_CONFIG_LOADER_PATH,
) -> tuple[str, ...]:
    """Read ConfigLoader's literal fallback without importing CDK or boto3."""
    try:
        source = config_loader_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(config_loader_path))
    except (OSError, SyntaxError) as exc:
        raise CatalogError(f"cannot parse {config_loader_path}: {exc}") from exc

    classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ConfigLoader"
    ]
    if len(classes) != 1:
        raise CatalogError(f"{config_loader_path}: expected exactly one ConfigLoader class")
    methods = [
        node
        for node in classes[0].body
        if isinstance(node, ast.FunctionDef) and node.name == "get_capacity_history_config"
    ]
    if len(methods) != 1:
        raise CatalogError(
            f"{config_loader_path}: expected exactly one get_capacity_history_config method"
        )

    default_configs: list[ast.Dict] = []
    for statement in methods[0].body:
        if not isinstance(statement, (ast.AnnAssign, ast.Assign)):
            continue
        targets = (
            (statement.target,)
            if isinstance(statement, ast.AnnAssign)
            else tuple(statement.targets)
        )
        if (
            len(targets) == 1
            and isinstance(targets[0], ast.Name)
            and targets[0].id == "default_config"
            and isinstance(statement.value, ast.Dict)
        ):
            default_configs.append(statement.value)
    if len(default_configs) != 1:
        raise CatalogError(
            f"{config_loader_path}: expected one literal default_config in "
            "ConfigLoader.get_capacity_history_config()"
        )

    watch_values: list[ast.expr] = []
    for key, value in zip(default_configs[0].keys, default_configs[0].values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "watch_instance_types":
            watch_values.append(value)
    if len(watch_values) != 1:
        raise CatalogError(
            f"{config_loader_path}: expected one default_config watch_instance_types value"
        )
    try:
        parsed_watch_values: object = ast.literal_eval(watch_values[0])
    except (SyntaxError, TypeError, ValueError) as exc:
        raise CatalogError(
            f"{config_loader_path}: default_config watch_instance_types must be a literal list"
        ) from exc
    return _string_list(
        parsed_watch_values,
        f"{config_loader_path}: ConfigLoader.get_capacity_history_config() "
        "default_config.watch_instance_types",
    )


def validate_config_loader_watch_instance_types(
    catalog: Catalog,
    config_loader_path: Path = DEFAULT_CONFIG_LOADER_PATH,
) -> tuple[Finding, ...]:
    """Require ConfigLoader's fallback watch list to exactly match the catalog."""
    watched = _load_config_loader_watch_instance_types(config_loader_path)
    return _watch_list_findings(
        catalog,
        watched,
        location=config_loader_path,
        code_prefix="config-loader-watch-list",
        subject="ConfigLoader capacity-history default watch list",
        target="ConfigLoader.get_capacity_history_config() default watch_instance_types",
        peer="cdk.json context.historical.watch_instance_types",
    )


@dataclass(frozen=True)
class InstancePool:
    """Named set of instance types scored together for Spot Placement Scores.

    AWS documents that ``GetSpotPlacementScores`` needs at least three instance
    types (or ``InstanceRequirements``) to return meaningful scores; querying a
    single type yields artificially depressed values. Members are grouped by
    accelerator class and per-instance accelerator memory so a workload can
    plausibly run on any member without change. Pools may overlap; snapshot
    attribution uses the first pool in ``INSTANCE_POOLS`` order that contains
    the instance type (see ``pool_for_instance_type``).
    """

    name: str
    members: tuple[str, ...]
    description: str = ""

    def __post_init__(self) -> None:
        _string(self.name, "instance pool name")
        if not self.members:
            raise CatalogError(f"instance pool {self.name} has no members")
        for member in self.members:
            _family_for_instance_type(_string(member, f"instance pool {self.name} member"))


#: Spot Placement Score pools over ``historical.watch_instance_types``.
#:
#: Definition order is meaningful: where pools overlap, the first pool that
#: contains a type wins snapshot attribution. Membership follows real per-size
#: accelerator layouts (for example ``g5.16xlarge`` carries one A10G while
#: ``g5.12xlarge`` carries four, and ``g5g.16xlarge`` carries two T4Gs, unlike
#: the single-GPU smaller g5g sizes). Graviton (arm64) types never share a pool
#: with x86_64 types because images are not interchangeable across
#: architectures.
INSTANCE_POOLS: tuple[InstancePool, ...] = (
    InstancePool(
        name="single-gpu-t4-16gb",
        members=(
            "g4dn.xlarge",
            "g4dn.2xlarge",
            "g4dn.4xlarge",
            "g4dn.8xlarge",
            "g4dn.16xlarge",
        ),
        description="One NVIDIA T4 (16 GB) per instance on the single-GPU x86_64 g4dn sizes.",
    ),
    InstancePool(
        name="single-gpu-arm-16gb",
        members=(
            "g5g.xlarge",
            "g5g.2xlarge",
            "g5g.4xlarge",
            "g5g.8xlarge",
        ),
        description=(
            "One NVIDIA T4G (16 GB) per Graviton g5g instance; arm64 images keep "
            "this pool separate from every x86_64 pool."
        ),
    ),
    InstancePool(
        name="single-gpu-24gb",
        members=(
            "g5.xlarge",
            "g5.2xlarge",
            "g5.4xlarge",
            "g5.8xlarge",
            "g5.16xlarge",
            "g6.xlarge",
            "g6.2xlarge",
            "g6.4xlarge",
            "g6.8xlarge",
            "g6.16xlarge",
            "gr6.4xlarge",
            "gr6.8xlarge",
        ),
        description=(
            "One 24 GB mid-range NVIDIA GPU per x86_64 instance: A10G on g5, L4 on "
            "g6 and the RAM-heavy gr6 sizes."
        ),
    ),
    InstancePool(
        name="single-gpu-fractional-l4",
        members=(
            "g6f.large",
            "g6f.xlarge",
            "g6f.2xlarge",
            "g6f.4xlarge",
            "gr6f.4xlarge",
        ),
        description=(
            "Fractional shares of one NVIDIA L4 (24 GB) on g6f and gr6f; sized by "
            "GPU fraction rather than GPU count."
        ),
    ),
    InstancePool(
        name="single-gpu-48gb",
        members=(
            "g6e.xlarge",
            "g6e.2xlarge",
            "g6e.4xlarge",
            "g6e.8xlarge",
            "g6e.16xlarge",
        ),
        description="One NVIDIA L40S (48 GB) per instance on the single-GPU g6e sizes.",
    ),
    InstancePool(
        name="single-gpu-gen7",
        members=(
            "g7.2xlarge",
            "g7.4xlarge",
            "g7.8xlarge",
        ),
        description="One current-generation NVIDIA GPU per instance on the small g7 sizes.",
    ),
    InstancePool(
        name="single-gpu-gen7-48gb",
        members=(
            "g7e.2xlarge",
            "g7e.4xlarge",
            "g7e.8xlarge",
        ),
        description=(
            "One current-generation 48 GB-class NVIDIA GPU per instance on the small g7e sizes."
        ),
    ),
    InstancePool(
        name="multi-gpu-4x",
        members=(
            "g5.12xlarge",
            "g5.24xlarge",
            "g6.12xlarge",
            "g6.24xlarge",
            "g6e.12xlarge",
            "g6e.24xlarge",
            "g7.12xlarge",
            "g7.24xlarge",
            "g7e.12xlarge",
            "g7e.24xlarge",
        ),
        description=(
            "Four datacenter NVIDIA GPUs per instance: the 12xlarge and 24xlarge "
            "sizes across g5, g6, g6e, g7, and g7e."
        ),
    ),
    InstancePool(
        name="multi-gpu-8x",
        members=(
            "g5.48xlarge",
            "g6.48xlarge",
            "g6e.48xlarge",
            "g7.48xlarge",
            "g7e.48xlarge",
        ),
        description=(
            "Eight datacenter NVIDIA GPUs per instance: the 48xlarge sizes across "
            "g5, g6, g6e, g7, and g7e."
        ),
    ),
    InstancePool(
        name="hpc-8x-training",
        members=(
            "p4d.24xlarge",
            "p4de.24xlarge",
            "p5.48xlarge",
            "p5e.48xlarge",
            "p5en.48xlarge",
            "p6-b200.48xlarge",
            "p6-b300.48xlarge",
        ),
        description=(
            "Eight EFA-attached NVIDIA training GPUs per instance (A100, H100, "
            "H200, B200, B300). Per-GPU memory spans 40 GB upward across members, "
            "so confirm model fit before substituting within the pool."
        ),
    ),
    InstancePool(
        name="inferentia1",
        members=(
            "inf1.xlarge",
            "inf1.2xlarge",
            "inf1.6xlarge",
            "inf1.24xlarge",
        ),
        description=(
            "AWS Inferentia (first generation) instances from one to sixteen "
            "accelerators; interchangeable for Neuron inference that fits one "
            "accelerator."
        ),
    ),
    InstancePool(
        name="inferentia2",
        members=(
            "inf2.xlarge",
            "inf2.8xlarge",
            "inf2.24xlarge",
            "inf2.48xlarge",
        ),
        description=(
            "AWS Inferentia2 instances from one to twelve accelerators; "
            "interchangeable for Neuron inference that fits one accelerator."
        ),
    ),
    InstancePool(
        name="trainium",
        members=(
            "trn1.32xlarge",
            "trn1n.32xlarge",
            "trn2.48xlarge",
        ),
        description="Sixteen-accelerator Trainium (trn1, trn1n) and Trainium2 training instances.",
    ),
)

#: Watch-list types deliberately outside every pool. These still get spot
#: pricing and Capacity Block observation from the capacity poller, but no
#: Spot Placement Score: each lacks two interchangeable peers in the watch
#: list, and padding a pool with unrelated types just to reach the AWS
#: three-type minimum is exactly the practice requirement 1.4 forbids.
#:
#: - g4dn.12xlarge (4x T4) and g4dn.metal (8x T4): no other multi-GPU 16 GB types.
#: - g5g.16xlarge and g5g.metal (2x T4G): a two-member arm64 pool is invalid.
#: - p3dn.24xlarge: deprecated V100 family; not interchangeable with active pools.
#: - p5.4xlarge: the only single-GPU H100 size in the list.
#: - trn1.2xlarge and trn2.3xlarge: single-accelerator Trainium sizes from
#:   different chip generations do not make an interchangeable trio.
UNPOOLED_INSTANCE_TYPES: tuple[str, ...] = (
    "g4dn.12xlarge",
    "g4dn.metal",
    "g5g.16xlarge",
    "g5g.metal",
    "p3dn.24xlarge",
    "p5.4xlarge",
    "trn1.2xlarge",
    "trn2.3xlarge",
)

_POOLS_LOCATION = "scripts/accelerator_catalog.py (INSTANCE_POOLS)"


def pool_for_instance_type(
    instance_type: str,
    pools: tuple[InstancePool, ...] = INSTANCE_POOLS,
) -> InstancePool | None:
    """Return the first pool in definition order containing ``instance_type``.

    Pools may overlap, but a capacity snapshot records exactly one pool per
    instance type, so attribution must be deterministic: definition order in
    ``INSTANCE_POOLS`` decides. Returns ``None`` for unpooled types.
    """
    for pool in pools:
        if instance_type in pool.members:
            return pool
    return None


def validate_instance_pools(
    catalog: Catalog,
    pools: tuple[InstancePool, ...] = INSTANCE_POOLS,
    unpooled_instance_types: tuple[str, ...] = UNPOOLED_INSTANCE_TYPES,
) -> tuple[Finding, ...]:
    """Enforce the Spot Placement Score pool policy against the catalog.

    Every pool needs at least three distinct members, every member must be a
    watched catalog type, and every watched type must be either pooled or
    explicitly declared unpooled, so new catalog entries force a reviewed
    pooling decision instead of silently going unscored.
    """
    findings: list[Finding] = []
    watched = set(catalog.instance_types)

    name_counts: dict[str, int] = {}
    for pool in pools:
        name_counts[pool.name] = name_counts.get(pool.name, 0) + 1
    duplicate_names = sorted(name for name, count in name_counts.items() if count > 1)
    if duplicate_names:
        findings.append(
            Finding(
                code="instance-pool-duplicate-name",
                title="Instance pools declare duplicate pool names",
                locations=(_POOLS_LOCATION,),
                detail=f"Duplicated pool name(s): {', '.join(duplicate_names)}.",
                recommendation=(
                    "Rename or merge the duplicated pools; snapshot attribution and "
                    "configuration errors must name exactly one pool."
                ),
            )
        )

    for pool in pools:
        duplicate_members = sorted(
            member for member in set(pool.members) if pool.members.count(member) > 1
        )
        if duplicate_members:
            findings.append(
                Finding(
                    code="instance-pool-duplicate-member",
                    title=f"Pool {pool.name} lists duplicate member types",
                    locations=(_POOLS_LOCATION,),
                    detail=f"Duplicate member(s): {', '.join(duplicate_members)}.",
                    recommendation=(
                        f"Remove the duplicate entries from {pool.name}; duplicates "
                        "must not count toward the three-distinct-type minimum."
                    ),
                )
            )
        distinct_members = set(pool.members)
        if len(distinct_members) < 3:
            findings.append(
                Finding(
                    code="instance-pool-too-small",
                    title=f"Pool {pool.name} has fewer than three distinct member types",
                    locations=(_POOLS_LOCATION,),
                    detail=(
                        f"Pool {pool.name} declares {len(distinct_members)} distinct "
                        "member type(s), but GetSpotPlacementScores needs at least "
                        "three instance types to return meaningful scores."
                    ),
                    recommendation=(
                        f"Add interchangeable types to {pool.name} (comparable "
                        "accelerator class and per-instance accelerator memory) or "
                        "move its members to UNPOOLED_INSTANCE_TYPES with a rationale."
                    ),
                )
            )
        unknown_members = sorted(distinct_members - watched)
        if unknown_members:
            findings.append(
                Finding(
                    code="instance-pool-unknown-member",
                    title=f"Pool {pool.name} contains types outside the watch list",
                    locations=(_POOLS_LOCATION,),
                    detail=(
                        f"Member(s) not in the catalog watch list: {', '.join(unknown_members)}."
                    ),
                    recommendation=(
                        "Pools score only observed capacity: add the type to the "
                        f"reviewed catalog and watch lists first, or remove it from {pool.name}."
                    ),
                )
            )

    pooled = {member for pool in pools for member in pool.members}
    declared_unpooled = set(unpooled_instance_types)
    uncovered = sorted(watched - pooled - declared_unpooled)
    if uncovered:
        findings.append(
            Finding(
                code="instance-pool-uncovered-type",
                title="Watched instance types have no reviewed pooling decision",
                locations=(_POOLS_LOCATION,),
                detail=(
                    f"{len(uncovered)} watched type(s) are neither pooled nor declared "
                    f"unpooled: {', '.join(uncovered)}."
                ),
                recommendation=(
                    "Add each type to an interchangeable pool, or add it to "
                    "UNPOOLED_INSTANCE_TYPES with a rationale so it visibly skips "
                    "Spot Placement Scores."
                ),
            )
        )
    stale_unpooled = sorted((declared_unpooled & pooled) | (declared_unpooled - watched))
    if stale_unpooled:
        findings.append(
            Finding(
                code="instance-pool-stale-unpooled",
                title="UNPOOLED_INSTANCE_TYPES is out of date",
                locations=(_POOLS_LOCATION,),
                detail=(f"Entries are pooled or no longer watched: {', '.join(stale_unpooled)}."),
                recommendation=(
                    "Keep UNPOOLED_INSTANCE_TYPES limited to watched types that no "
                    "pool contains; remove entries that are pooled or retired."
                ),
            )
        )
    return tuple(findings)


def validate_repository(
    *,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
    manifests_path: Path = DEFAULT_MANIFESTS_PATH,
    cdk_path: Path = DEFAULT_CDK_PATH,
    config_loader_path: Path = DEFAULT_CONFIG_LOADER_PATH,
    pools: tuple[InstancePool, ...] = INSTANCE_POOLS,
    unpooled_instance_types: tuple[str, ...] = UNPOOLED_INSTANCE_TYPES,
) -> ValidationReport:
    """Run every deterministic repository validation without AWS access."""
    catalog = Catalog.load(catalog_path)
    nodepools = load_nodepools(manifests_path)
    findings = [
        *validate_nodepools(catalog, nodepools),
        *validate_watch_instance_types(catalog, cdk_path),
        *validate_config_loader_watch_instance_types(catalog, config_loader_path),
        *validate_instance_pools(catalog, pools, unpooled_instance_types),
    ]
    return ValidationReport(tuple(sorted(findings, key=Finding.sort_key)))


@dataclass(frozen=True)
class DiscoveredFamily:
    accelerator: Accelerator
    architectures: tuple[str, ...]


@dataclass(frozen=True)
class Discovery:
    regions: tuple[str, ...]
    instance_types: tuple[str, ...]
    families: dict[str, DiscoveredFamily]

    def to_mapping(self) -> dict[str, object]:
        return {
            "regions_checked": list(self.regions),
            "instance_types": list(self.instance_types),
            "families": {
                name: {
                    "accelerator": family.accelerator,
                    "architectures": list(family.architectures),
                }
                for name, family in sorted(self.families.items())
            },
        }


def _detect_accelerator(instance: dict[str, object]) -> Accelerator | None:
    gpu_info_value = instance.get("GpuInfo")
    if isinstance(gpu_info_value, dict):
        gpu_info = _mapping(gpu_info_value, "DescribeInstanceTypes.GpuInfo")
        gpus_value = gpu_info.get("Gpus", [])
        if isinstance(gpus_value, list):
            for gpu_value in gpus_value:
                if isinstance(gpu_value, dict):
                    gpu = _mapping(gpu_value, "DescribeInstanceTypes.GpuInfo.Gpus[]")
                    manufacturer = gpu.get("Manufacturer")
                    if isinstance(manufacturer, str) and manufacturer.casefold() == "nvidia":
                        return "nvidia"
    neuron_info_value = instance.get("NeuronInfo")
    if isinstance(neuron_info_value, dict):
        neuron_info = _mapping(neuron_info_value, "DescribeInstanceTypes.NeuronInfo")
        devices = neuron_info.get("NeuronDevices")
        if isinstance(devices, list) and devices:
            return "neuron"
    return None


def _instance_architectures(instance: dict[str, object]) -> tuple[str, ...]:
    processor = _mapping(instance.get("ProcessorInfo"), "DescribeInstanceTypes.ProcessorInfo")
    ec2_architectures = _string_list(
        processor.get("SupportedArchitectures"),
        "DescribeInstanceTypes.ProcessorInfo.SupportedArchitectures",
    )
    return tuple(sorted(_EC2_TO_KUBERNETES_ARCH.get(item, item) for item in ec2_architectures))


def discover_accelerator_catalog(
    *,
    profile: str | None = None,
    home_region: str = "us-east-1",
) -> Discovery:
    """Query accelerator types across enabled commercial Regions, sequentially.

    EC2 Describe calls use the standard non-mutating request bucket (100-token
    burst, 20 requests/second refill at the time this was implemented). Explicit
    pagination, no parallel fan-out, and adaptive retries keep this monthly scan
    well below that envelope and resilient to account-level concurrent traffic.
    """
    # Keep AWS SDK imports out of the offline validation path. The minimal
    # shell-test job intentionally installs only Python + PyYAML, while the
    # monthly online workflow installs boto3/botocore through the project.
    import boto3
    from botocore.config import Config

    session: Any = boto3.Session() if profile is None else boto3.Session(profile_name=profile)
    client_config = Config(
        connect_timeout=10,
        read_timeout=60,
        retries={"mode": "adaptive", "total_max_attempts": 10},
        user_agent_extra="gco-accelerator-catalog/1",
    )
    home_client: Any = session.client("ec2", region_name=home_region, config=client_config)
    region_response: object = home_client.describe_regions(AllRegions=True)
    region_mapping = _mapping(region_response, "DescribeRegions response")
    region_values = region_mapping.get("Regions", [])
    if not isinstance(region_values, list):
        raise CatalogError("DescribeRegions response Regions must be a list")
    regions: list[str] = []
    for region_value in region_values:
        region_record = _mapping(region_value, "DescribeRegions.Regions[]")
        status = region_record.get("OptInStatus")
        region_name_value = region_record.get("RegionName")
        if status in _ENABLED_REGION_STATUSES and isinstance(region_name_value, str):
            regions.append(region_name_value)
    regions = sorted(set(regions))
    if not regions:
        raise CatalogError("DescribeRegions returned no enabled commercial Regions")

    discovered_types: set[str] = set()
    family_accelerators: dict[str, Accelerator] = {}
    family_architectures: dict[str, set[str]] = {}
    for region_name in regions:
        client: Any = session.client("ec2", region_name=region_name, config=client_config)
        paginator: Any = client.get_paginator("describe_instance_types")
        pages: Any = paginator.paginate(PaginationConfig={"PageSize": 100})
        for page_value in pages:
            page = _mapping(page_value, f"DescribeInstanceTypes response in {region_name}")
            instance_values = page.get("InstanceTypes", [])
            if not isinstance(instance_values, list):
                raise CatalogError(
                    f"DescribeInstanceTypes response InstanceTypes must be a list in {region_name}"
                )
            for instance_value in instance_values:
                instance = _mapping(
                    instance_value,
                    f"DescribeInstanceTypes.InstanceTypes[] in {region_name}",
                )
                accelerator = _detect_accelerator(instance)
                if accelerator is None:
                    continue
                instance_type = _string(
                    instance.get("InstanceType"), "DescribeInstanceTypes.InstanceType"
                )
                family = _family_for_instance_type(instance_type)
                existing_accelerator = family_accelerators.setdefault(family, accelerator)
                if existing_accelerator != accelerator:
                    raise CatalogError(
                        f"EC2 returned conflicting accelerator classes for family {family}"
                    )
                family_architectures.setdefault(family, set()).update(
                    _instance_architectures(instance)
                )
                discovered_types.add(instance_type)

    families = {
        name: DiscoveredFamily(
            accelerator=family_accelerators[name],
            architectures=tuple(sorted(family_architectures[name])),
        )
        for name in sorted(family_accelerators)
    }
    return Discovery(
        regions=tuple(regions),
        instance_types=tuple(sorted(discovered_types)),
        families=families,
    )


@dataclass(frozen=True)
class CatalogDrift:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    metadata_changes: tuple[str, ...]
    regions_checked: int

    @property
    def count(self) -> int:
        return len(self.added) + len(self.removed) + len(self.metadata_changes)

    @property
    def has_drift(self) -> bool:
        return self.count > 0

    def to_markdown(self) -> str:
        status = "ACTION REQUIRED" if self.has_drift else "CURRENT"
        lines = [
            "## Online EC2 accelerator catalog drift",
            "",
            f"**Status: {status}.** Checked {self.regions_checked} enabled commercial Regions "
            "sequentially with adaptive retries.",
            "",
            "| Change | Count |",
            "|--------|------:|",
            f"| New instance types | {len(self.added)} |",
            f"| No-longer-returned instance types | {len(self.removed)} |",
            f"| Family metadata changes | {len(self.metadata_changes)} |",
        ]
        if not self.has_drift:
            lines.extend(
                [
                    "",
                    "The checked-in catalog matches the EC2 union for NVIDIA GPU and AWS Neuron "
                    "instance types.",
                ]
            )
            return "\n".join(lines) + "\n"
        if self.added:
            lines.extend(["", "### New EC2 instance types", ""])
            lines.extend(f"- `{item}`" for item in self.added)
            lines.extend(
                [
                    "",
                    "Review each new family/size, update family lifecycle and generation metadata, "
                    "then run `python scripts/accelerator_catalog.py refresh`.",
                ]
            )
        if self.removed:
            lines.extend(["", "### Instance types no longer returned", ""])
            lines.extend(f"- `{item}`" for item in self.removed)
            lines.extend(
                [
                    "",
                    "Confirm this is durable across Regions before removing a type. If its family "
                    "is retired, mark the family deprecated or end-of-life with replacements.",
                ]
            )
        if self.metadata_changes:
            lines.extend(["", "### Family metadata changes", ""])
            lines.extend(f"- {item}" for item in self.metadata_changes)
        return "\n".join(lines) + "\n"

    def summary_mapping(self) -> dict[str, object]:
        return {
            "status": "drift" if self.has_drift else "current",
            "drift_count": self.count,
            "added_count": len(self.added),
            "removed_count": len(self.removed),
            "metadata_change_count": len(self.metadata_changes),
            "regions_checked": self.regions_checked,
        }


def compare_catalog(catalog: Catalog, discovery: Discovery) -> CatalogDrift:
    expected = set(catalog.instance_types)
    actual = set(discovery.instance_types)
    metadata_changes: list[str] = []
    for name, discovered in sorted(discovery.families.items()):
        policy = catalog.families.get(name)
        if policy is None:
            metadata_changes.append(
                f"New family `{name}` requires reviewed track, generation, and lifecycle policy "
                f"(accelerator={discovered.accelerator}, "
                f"architectures={list(discovered.architectures)})."
            )
            continue
        if policy.accelerator != discovered.accelerator:
            metadata_changes.append(
                f"`{name}` accelerator changed: catalog={policy.accelerator}, "
                f"EC2={discovered.accelerator}."
            )
        if policy.architectures != discovered.architectures:
            metadata_changes.append(
                f"`{name}` architectures changed: catalog={list(policy.architectures)}, "
                f"EC2={list(discovered.architectures)}."
            )
    return CatalogDrift(
        added=tuple(sorted(actual - expected)),
        removed=tuple(sorted(expected - actual)),
        metadata_changes=tuple(metadata_changes),
        regions_checked=len(discovery.regions),
    )


def refresh_catalog(catalog: Catalog, discovery: Discovery, output_path: Path) -> None:
    """Write discovered types and a UTC timestamp after policy validation."""
    unknown_families = sorted(set(discovery.families) - set(catalog.families))
    if unknown_families:
        details = ", ".join(unknown_families)
        raise CatalogError(
            "refusing to refresh with unreviewed families: "
            f"{details}. Add explicit track/generation/lifecycle policy first."
        )
    metadata_drift = compare_catalog(catalog, discovery).metadata_changes
    if metadata_drift:
        raise CatalogError(
            "refusing to refresh while family metadata differs from EC2: "
            + " ".join(metadata_drift)
        )
    refreshed = Catalog(
        schema_version=catalog.schema_version,
        last_refreshed_at=_current_utc_timestamp(),
        source=catalog.source,
        families=catalog.families,
        instance_types=discovery.instance_types,
    )
    output_path.write_text(json.dumps(refreshed.to_mapping(), indent=2) + "\n")


def _write_or_print(content: str, output: Path | None) -> None:
    if output is None:
        print(content, end="")
    else:
        output.write_text(content)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="run deterministic offline validation")
    validate.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
    validate.add_argument("--manifests", type=Path, default=DEFAULT_MANIFESTS_PATH)
    validate.add_argument("--cdk-config", type=Path, default=DEFAULT_CDK_PATH)
    validate.add_argument("--config-loader", type=Path, default=DEFAULT_CONFIG_LOADER_PATH)
    validate.add_argument("--format", choices=("text", "markdown"), default="text")
    validate.add_argument("--output", type=Path)

    for name, help_text in (
        ("capture", "print the live enabled-Region accelerator union"),
        ("check-online", "compare the checked-in catalog with live EC2"),
        ("refresh", "replace catalog instance types with the reviewed live union"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
        command.add_argument("--profile")
        command.add_argument("--home-region", default="us-east-1")
        if name == "check-online":
            command.add_argument("--report", type=Path)
            command.add_argument("--json-summary", action="store_true")
        if name == "refresh":
            command.add_argument("--output", type=Path, default=DEFAULT_CATALOG_PATH)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            report = validate_repository(
                catalog_path=args.catalog,
                manifests_path=args.manifests,
                cdk_path=args.cdk_config,
                config_loader_path=args.config_loader,
            )
            content = report.to_markdown() if args.format == "markdown" else report.to_text()
            _write_or_print(content, args.output)
            return 0 if report.ok else 1

        discovery = discover_accelerator_catalog(
            profile=args.profile,
            home_region=args.home_region,
        )
        if args.command == "capture":
            print(json.dumps(discovery.to_mapping(), indent=2))
            return 0

        catalog = Catalog.load(args.catalog)
        if args.command == "check-online":
            drift = compare_catalog(catalog, discovery)
            if args.report is not None:
                args.report.write_text(drift.to_markdown())
            if args.json_summary:
                print(json.dumps(drift.summary_mapping(), sort_keys=True))
            else:
                print(
                    f"accelerator catalog: status="
                    f"{'drift' if drift.has_drift else 'current'} "
                    f"drift_count={drift.count} regions_checked={drift.regions_checked}"
                )
            return 1 if drift.has_drift else 0

        if args.command == "refresh":
            refresh_catalog(catalog, discovery, args.output)
            print(
                f"Refreshed {args.output} with {len(discovery.instance_types)} instance types "
                f"from {len(discovery.regions)} enabled Regions."
            )
            return 0

        raise CatalogError(f"unsupported command: {args.command}")
    except Exception as exc:
        print(f"accelerator catalog error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
