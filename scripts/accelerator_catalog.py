#!/usr/bin/env python3
"""Validate and maintain GCO's EC2 accelerator catalog.

Normal CI is deliberately offline: ``validate`` compares the checked-in catalog
with Karpenter NodePools and ``cdk.json``. The monthly dependency workflow runs
``check-online`` to compare that catalog with the union of NVIDIA GPU and AWS
Neuron instance types returned by EC2 in every enabled commercial Region.

Online reads are sequential and paginated. Botocore adaptive retries protect the
monthly scan from transient EC2 throttling without making the deterministic test
suite depend on credentials or a mutable cloud catalog.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = ROOT / "gco" / "config" / "accelerator_catalog.json"
DEFAULT_CDK_PATH = ROOT / "cdk.json"
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
        manifest_allowed_value = raw.get(
            "manifest_allowed", lifecycle_value in {"active", "announced"}
        )
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
        default_allowed = self.lifecycle in {"active", "announced"}
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
            return "Accelerator catalog validation passed: NodePools and watch list are current.\n"
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
                    "The checked-in EC2 catalog, Karpenter families, and "
                    "`historical.watch_instance_types` are synchronized.",
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


def validate_watch_instance_types(
    catalog: Catalog, cdk_path: Path = DEFAULT_CDK_PATH
) -> tuple[Finding, ...]:
    """Require cdk.json's capacity-history watch list to exactly match the catalog."""
    try:
        parsed: object = json.loads(cdk_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read {cdk_path}: {exc}") from exc
    root = _mapping(parsed, str(cdk_path))
    context = _mapping(root.get("context"), f"{cdk_path}: context")
    historical = _mapping(context.get("historical"), f"{cdk_path}: context.historical")
    watched = _string_list(
        historical.get("watch_instance_types"),
        f"{cdk_path}: context.historical.watch_instance_types",
    )
    findings: list[Finding] = []
    if len(watched) != len(set(watched)):
        duplicates = sorted(item for item in set(watched) if watched.count(item) > 1)
        findings.append(
            Finding(
                code="watch-list-duplicates",
                title="capacity-history watch list contains duplicate instance types",
                locations=(str(cdk_path),),
                detail=f"Duplicate values: {', '.join(duplicates)}.",
                recommendation="Remove duplicate entries and use the catalog's normalized order.",
            )
        )
    expected = set(catalog.instance_types)
    actual = set(watched)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        findings.append(
            Finding(
                code="watch-list-missing",
                title="capacity-history watch list omits accelerator instance types",
                locations=(str(cdk_path),),
                detail=f"Missing {len(missing)} catalog type(s): {', '.join(missing)}.",
                recommendation=(
                    "Add every listed type to context.historical.watch_instance_types and mirror "
                    "the same default in ConfigLoader.get_capacity_history_config()."
                ),
            )
        )
    if unexpected:
        findings.append(
            Finding(
                code="watch-list-unexpected",
                title="capacity-history watch list contains types outside the catalog",
                locations=(str(cdk_path),),
                detail=f"Unexpected {len(unexpected)} type(s): {', '.join(unexpected)}.",
                recommendation=(
                    "Refresh and review the catalog before retaining these entries, or remove them "
                    "from context.historical.watch_instance_types."
                ),
            )
        )
    if not missing and not unexpected and watched != catalog.instance_types:
        findings.append(
            Finding(
                code="watch-list-order",
                title="capacity-history watch list is not in normalized catalog order",
                locations=(str(cdk_path),),
                detail="The values are complete but their order differs from the checked-in catalog.",
                recommendation=(
                    "Replace the list with gco/config/accelerator_catalog.json instance_types so "
                    "future catalog refreshes produce reviewable diffs."
                ),
            )
        )
    return tuple(findings)


def validate_repository(
    *,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
    manifests_path: Path = DEFAULT_MANIFESTS_PATH,
    cdk_path: Path = DEFAULT_CDK_PATH,
) -> ValidationReport:
    """Run every deterministic repository validation without AWS access."""
    catalog = Catalog.load(catalog_path)
    nodepools = load_nodepools(manifests_path)
    findings = [
        *validate_nodepools(catalog, nodepools),
        *validate_watch_instance_types(catalog, cdk_path),
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
    """Write discovered types only after every family has reviewed metadata."""
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
