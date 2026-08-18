"""Managed deployment-config engine: validated, atomic, audited cdk.json edits.

This module is the categorical answer to "add a CLI/MCP toggle for cdk.json
knob X" requests (issue #221). Instead of re-implementing read/validate/write
logic per knob, each externally manageable key registers a :class:`ManagedListKey`
(set-semantics list) or :class:`ManagedScalarKey` (single string value) and
every mutation flows through one engine that guarantees:

- **Resolution**: the target is the same ``cdk.json`` the CDK CLI would use
  (current directory upward), or an explicit caller-supplied path. Installed
  (``uvx`` / ``pip``) distributions resolve to read-only package data — the
  engine refuses those with an actionable message instead of half-working.
- **Validation of the result, not the starting state**: an edit is accepted
  iff the *resulting* configuration passes the same validators the CDK app
  applies at synth time (``gco/stacks/constants.py``). This deliberately
  allows repairing an already-broken config (e.g. removing a typo'd Region).
- **Idempotency**: re-adding a present value or removing an absent one is a
  reported no-op — no bytes are written, no timestamps churn.
- **Atomicity**: writes go through the same tmp-file + ``os.replace`` dance
  as the feature toggles in ``cli/stacks.py``, preserving file mode and the
  original trailing-newline state, so a crash can never leave a torn file.
- **Auditability**: every mutation attempt logs a structured line on the
  ``gco.cli.managed_config`` logger; MCP exposure adds ``@audit_logged`` on
  top of that.

Comment keys (``_comment_*``) and key order in ``cdk.json`` are preserved
because the engine round-trips the whole document with ``json.load`` /
``json.dumps(indent=2)`` exactly like the existing feature-toggle writers.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gco.stacks.constants import (
    validated_deployment_partition,
    validated_regional_deployment_regions,
)

from .stacks import (
    ConfigMutationLockError,
    _atomic_write_bytes,
    _find_cdk_json,
)
from .stacks import (
    _config_mutation_lock as _shared_config_mutation_lock,
)

logger = logging.getLogger("gco.cli.managed_config")

# Effective defaults mirror the reader contract in
# ``gco/config/config_loader.py::get_deployment_regions`` — validation of a
# candidate result must see the same effective document the CDK app will.
_DEFAULT_SCALAR_REGION = "us-east-2"
_DEFAULT_REGIONAL = ("us-east-1",)


class ManagedConfigError(RuntimeError):
    """A managed cdk.json edit was refused; the message says how to proceed."""


@dataclass(frozen=True)
class ChangeReport:
    """Uniform result of one managed mutation (including reported no-ops)."""

    key_id: str
    action: str  # "add" | "remove" | "set"
    value: str
    changed: bool
    old: tuple[str, ...] | str
    new: tuple[str, ...] | str
    config_path: Path

    @staticmethod
    def _render(side: tuple[str, ...] | str) -> str:
        return repr(list(side)) if isinstance(side, tuple) else repr(side)

    def summary(self) -> str:
        """One human line suitable for CLI output and audit trails."""
        if not self.changed:
            state = {
                "add": "already present",
                "remove": "not present",
                "set": "already the value",
            }[self.action]
            return f"{self.key_id}: no change ({self.value!r} {state})"
        return (
            f"{self.key_id}: {self.action} {self.value!r} "
            f"({self._render(self.old)} -> {self._render(self.new)}) in {self.config_path}"
        )


@dataclass(frozen=True)
class ManagedListKey:
    """Registry entry for a cdk.json context list managed with set semantics.

    ``validate_result`` receives the full parsed cdk.json document and the
    candidate value the list would hold after the edit; it must raise
    ``ValueError`` to reject. Validating in document context lets a key
    enforce cross-key invariants (the regional-Regions key checks the single
    partition constraint against the global/API/monitoring scalars).
    """

    key_id: str  # dotted id, e.g. "deployment_regions.regional"
    container: str  # context child object, e.g. "deployment_regions"
    leaf: str  # list key inside the container, e.g. "regional"
    description: str
    default: tuple[str, ...]
    validate_result: Callable[[dict[str, Any], tuple[str, ...]], None]


def _validate_regional_result(document: dict[str, Any], candidate: tuple[str, ...]) -> None:
    """Reject a regional-Regions candidate the CDK app would refuse at synth.

    Applies the exact synth-time validators: every entry must be an SDK-known
    CloudFormation Region, unique, non-empty — and together with the effective
    global/api_gateway/monitoring scalars must resolve to one AWS partition.
    """
    validated_regional_deployment_regions(list(candidate))
    container = document.get("context", {}).get("deployment_regions", {})
    if not isinstance(container, dict):
        raise ValueError("context.deployment_regions must be a JSON object")
    scalars = tuple(
        container.get(scalar_key, _DEFAULT_SCALAR_REGION)
        for scalar_key in ("global", "api_gateway", "monitoring")
    )
    validated_deployment_partition((*scalars, *candidate))


@dataclass(frozen=True)
class ManagedScalarKey:
    """Registry entry for a single-valued cdk.json context string.

    Same contract as :class:`ManagedListKey` with scalar semantics:
    ``validate_result`` receives the full parsed document and the candidate
    string the key would hold after the edit, raising ``ValueError`` to
    reject. Setting the current value is a reported no-op.
    """

    key_id: str  # dotted id, e.g. "deployment_regions.global"
    container: str  # context child object, e.g. "deployment_regions"
    leaf: str  # string key inside the container, e.g. "global"
    description: str
    default: str
    validate_result: Callable[[dict[str, Any], str], None]


def _effective_deployment_scalars(document: dict[str, Any]) -> dict[str, str]:
    """Return the effective global/api_gateway/monitoring scalar Regions."""
    container = document.get("context", {}).get("deployment_regions", {})
    if not isinstance(container, dict):
        raise ValueError("context.deployment_regions must be a JSON object")
    return {
        scalar_key: container.get(scalar_key, _DEFAULT_SCALAR_REGION)
        for scalar_key in ("global", "api_gateway", "monitoring")
    }


def _effective_regional(document: dict[str, Any]) -> tuple[str, ...]:
    """Return the effective workload-Region list (default when absent)."""
    container = document.get("context", {}).get("deployment_regions", {})
    if not isinstance(container, dict):
        raise ValueError("context.deployment_regions must be a JSON object")
    regional = container.get("regional", list(_DEFAULT_REGIONAL))
    if not isinstance(regional, list):
        raise ValueError("context.deployment_regions.regional must be a JSON array")
    return tuple(regional)


def _scalar_region_validator(role: str) -> Callable[[dict[str, Any], str], None]:
    """Build a validator for one deployment-region scalar (``role``).

    The candidate must be an SDK-known CloudFormation Region and the whole
    resulting topology (candidate + the other scalars + the workload list)
    must still resolve to one AWS partition — the same constraint synth
    enforces, applied to the result.
    """

    def _validate(document: dict[str, Any], candidate: str) -> None:
        scalars = _effective_deployment_scalars(document)
        scalars[role] = candidate
        validated_deployment_partition((*scalars.values(), *_effective_regional(document)))

    return _validate


def _validate_bedrock_model_result(document: dict[str, Any], candidate: str) -> None:
    """Mirror the reader contract in ``gco/bedrock.py``: a non-empty string.

    Model/inference-profile IDs are free-form by design (custom profiles,
    marketplace models); the runtime reader only requires a non-empty
    string, so requiring more here would reject valid configurations.
    """
    del document  # no cross-key invariants for this knob
    if not candidate.strip():
        raise ValueError("bedrock.default_model_id must be a non-empty string")
    if candidate != candidate.strip():
        raise ValueError("bedrock.default_model_id must not have leading/trailing whitespace")


#: The managed-key registry. New knobs register here instead of growing
#: bespoke read/validate/write code paths.
REGIONAL_DEPLOYMENT_REGIONS = ManagedListKey(
    key_id="deployment_regions.regional",
    container="deployment_regions",
    leaf="regional",
    description="Workload Regions that receive an EKS regional stack",
    default=_DEFAULT_REGIONAL,
    validate_result=_validate_regional_result,
)

#: The three control-plane region scalars, addressable by role name.
DEPLOYMENT_REGION_SCALARS: dict[str, ManagedScalarKey] = {
    role: ManagedScalarKey(
        key_id=f"deployment_regions.{role}",
        container="deployment_regions",
        leaf=role,
        description=description,
        default=_DEFAULT_SCALAR_REGION,
        validate_result=_scalar_region_validator(role),
    )
    for role, description in (
        ("global", "Region hosting partition-wide ECR/S3/DynamoDB and the SSM registry"),
        ("api_gateway", "Region hosting the API Gateway stack"),
        ("monitoring", "Region hosting the monitoring stack"),
    )
}

BEDROCK_DEFAULT_MODEL = ManagedScalarKey(
    key_id="bedrock.default_model_id",
    container="bedrock",
    leaf="default_model_id",
    description="Default Bedrock model/inference-profile ID for advisory features",
    default="",  # the reader has no fallback: it requires the key when consulted
    validate_result=_validate_bedrock_model_result,
)


def _resolve_config_path(config_path: Path | str | None) -> Path:
    """Return the cdk.json to edit, refusing unusable resolutions early."""
    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise ManagedConfigError(f"config path {path} does not exist or is not a file")
        return path
    found = _find_cdk_json()
    if found is None:
        raise ManagedConfigError(
            "cdk.json not found in the current directory or any parent. "
            "Run from a GCO checkout, or pass an explicit --config-path. "
            "Installed (uvx/pip) distributions do not carry a writable "
            "deployment config."
        )
    return found


def _require_writable(path: Path) -> None:
    """Refuse read-only targets (the installed-mode package-data case)."""
    parent_writable = os.access(path.parent, os.W_OK)
    file_writable = os.access(path, os.W_OK)
    if parent_writable and file_writable:
        return
    raise ManagedConfigError(
        f"{path} is not writable"
        f"{'' if parent_writable else ' (directory is read-only)'}. "
        "Installed (uvx/pip) distributions expose a read-only cdk.json; "
        "run from a writable GCO checkout or pass --config-path pointing "
        "at the deployment config you own."
    )


@contextmanager
def _config_mutation_lock(path: Path) -> Iterator[None]:
    """Translate shared-lock failures into this module's public error type."""
    try:
        with _shared_config_mutation_lock(path):
            yield
    except ConfigMutationLockError as exc:
        raise ManagedConfigError(str(exc)) from exc


def _load_document(path: Path) -> tuple[dict[str, Any], bytes]:
    """Parse the target document, keeping raw bytes for faithful re-encoding."""
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("context"), dict):
        raise ManagedConfigError(
            f"{path} does not look like a GCO cdk.json (missing a 'context' object); "
            "refusing to edit it"
        )
    return document, raw


def _write_document(path: Path, document: dict[str, Any], original_raw: bytes) -> None:
    """Serialize like the existing feature-toggle writers, atomically.

    ``json.dumps(indent=2)`` with insertion order preserves ``_comment_*``
    keys and their placement. ``ensure_ascii=False`` keeps the em dashes and
    other non-ASCII characters inside those comments as UTF-8 rather than
    rewriting them to ``\\uXXXX`` escapes — without it, adding one Region
    rewrites every documented block in the file and buries the real change in
    hundreds of lines of encoding churn. The original trailing-newline state is
    kept so diffs stay minimal regardless of how the file was last formatted.
    """
    serialized = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
    if original_raw.endswith(b"\n"):
        serialized += b"\n"
    _atomic_write_bytes(path, serialized, mode=stat.S_IMODE(path.stat().st_mode))


def _current_values(document: dict[str, Any], key: ManagedListKey) -> tuple[str, ...]:
    """Return the configured list, or the effective default when absent."""
    container = document["context"].get(key.container)
    if container is None:
        return key.default
    if not isinstance(container, dict):
        raise ManagedConfigError(
            f"context.{key.container} must be a JSON object, found {type(container).__name__}"
        )
    values = container.get(key.leaf)
    if values is None:
        return key.default
    if not isinstance(values, list):
        raise ManagedConfigError(
            f"context.{key.container}.{key.leaf} must be a JSON array, "
            f"found {type(values).__name__}"
        )
    return tuple(values)


def _apply(
    key: ManagedListKey,
    action: str,
    value: str,
    config_path: Path | str | None,
) -> ChangeReport:
    """Shared add/remove core: resolve, lock, load, validate, and write."""
    path = _resolve_config_path(config_path)
    with _config_mutation_lock(path):
        document, raw = _load_document(path)
        old = _current_values(document, key)

        if action == "add":
            changed = value not in old
            candidate = (*old, value) if changed else old
        else:
            changed = value in old
            candidate = tuple(entry for entry in old if entry != value) if changed else old

        if not changed:
            report = ChangeReport(key.key_id, action, value, False, old, old, path)
            logger.info(
                "managed-config no-op: key=%s action=%s value=%s path=%s",
                key.key_id,
                action,
                value,
                path,
            )
            return report

        try:
            key.validate_result(document, candidate)
        except ValueError as exc:
            logger.warning(
                "managed-config refused: key=%s action=%s value=%s path=%s reason=%s",
                key.key_id,
                action,
                value,
                path,
                exc,
            )
            raise ManagedConfigError(f"refusing to update {key.key_id}: {exc}") from exc

        _require_writable(path)
        # Materialize only what this key manages; absent sibling scalars keep
        # falling through to the reader defaults instead of being frozen into
        # the file by an unrelated edit.
        container = document["context"].setdefault(key.container, {})
        container[key.leaf] = list(candidate)
        _write_document(path, document, raw)

        report = ChangeReport(key.key_id, action, value, True, old, candidate, path)
        logger.info(
            "managed-config write: key=%s action=%s value=%s old=%s new=%s path=%s",
            key.key_id,
            action,
            value,
            list(old),
            list(candidate),
            path,
        )
        return report


def managed_list_add(
    key: ManagedListKey, value: str, *, config_path: Path | str | None = None
) -> ChangeReport:
    """Add ``value`` to a managed list if absent; validated and atomic."""
    return _apply(key, "add", value, config_path)


def managed_list_remove(
    key: ManagedListKey, value: str, *, config_path: Path | str | None = None
) -> ChangeReport:
    """Remove ``value`` from a managed list if present; validated and atomic."""
    return _apply(key, "remove", value, config_path)


def _current_scalar(document: dict[str, Any], key: ManagedScalarKey) -> str:
    """Return the configured scalar, or the effective default when absent."""
    container = document["context"].get(key.container)
    if container is None:
        return key.default
    if not isinstance(container, dict):
        raise ManagedConfigError(
            f"context.{key.container} must be a JSON object, found {type(container).__name__}"
        )
    value = container.get(key.leaf)
    if value is None:
        return key.default
    if not isinstance(value, str):
        raise ManagedConfigError(
            f"context.{key.container}.{key.leaf} must be a JSON string, "
            f"found {type(value).__name__}"
        )
    return value


def managed_scalar_set(
    key: ManagedScalarKey, value: str, *, config_path: Path | str | None = None
) -> ChangeReport:
    """Set a managed scalar; validated, atomic, and a no-op when unchanged."""
    path = _resolve_config_path(config_path)
    with _config_mutation_lock(path):
        document, raw = _load_document(path)
        old = _current_scalar(document, key)

        if value == old:
            report = ChangeReport(key.key_id, "set", value, False, old, old, path)
            logger.info(
                "managed-config no-op: key=%s action=set value=%s path=%s",
                key.key_id,
                value,
                path,
            )
            return report

        try:
            key.validate_result(document, value)
        except ValueError as exc:
            logger.warning(
                "managed-config refused: key=%s action=set value=%s path=%s reason=%s",
                key.key_id,
                value,
                path,
                exc,
            )
            raise ManagedConfigError(f"refusing to update {key.key_id}: {exc}") from exc

        _require_writable(path)
        # Materialize only the managed leaf; sibling keys (e.g. bedrock.thinking)
        # and absent sibling scalars keep their current state / reader defaults.
        container = document["context"].setdefault(key.container, {})
        container[key.leaf] = value
        _write_document(path, document, raw)

        report = ChangeReport(key.key_id, "set", value, True, old, value, path)
        logger.info(
            "managed-config write: key=%s action=set value=%s old=%s new=%s path=%s",
            key.key_id,
            value,
            old,
            value,
            path,
        )
        return report


# ---------------------------------------------------------------------------
# Deployment-region veneers (domain-named entry points used by the CLI; the
# MCP tools shell to the CLI commands, matching every other gated tool).
# ---------------------------------------------------------------------------


def get_deployment_regions_status(*, config_path: Path | str | None = None) -> dict[str, Any]:
    """Return the effective deployment-region topology plus its partition.

    Works on broken configurations too (this is the diagnosis entry point):
    when the topology fails validation, ``partition`` is ``None`` and
    ``partition_error`` carries the validator message.
    """
    path = _resolve_config_path(config_path)
    document, _ = _load_document(path)
    container_key = REGIONAL_DEPLOYMENT_REGIONS.container
    container = document["context"].get(container_key)
    if container is None:
        container = {}
    if not isinstance(container, dict):
        raise ManagedConfigError(f"context.{container_key} must be a JSON object")
    regional = _current_values(document, REGIONAL_DEPLOYMENT_REGIONS)
    status: dict[str, Any] = {
        "config_path": str(path),
        "global": container.get("global", _DEFAULT_SCALAR_REGION),
        "api_gateway": container.get("api_gateway", _DEFAULT_SCALAR_REGION),
        "monitoring": container.get("monitoring", _DEFAULT_SCALAR_REGION),
        "regional": list(regional),
    }
    try:
        _validate_regional_result(document, regional)
        status["partition"] = validated_deployment_partition(
            (status["global"], status["api_gateway"], status["monitoring"], *regional)
        )
    except ValueError as exc:
        status["partition"] = None
        status["partition_error"] = str(exc)
    return status


def add_deployment_region(region: str, *, config_path: Path | str | None = None) -> ChangeReport:
    """Add a workload Region to ``deployment_regions.regional``."""
    return managed_list_add(REGIONAL_DEPLOYMENT_REGIONS, region, config_path=config_path)


def remove_deployment_region(region: str, *, config_path: Path | str | None = None) -> ChangeReport:
    """Remove a workload Region from ``deployment_regions.regional``."""
    return managed_list_remove(REGIONAL_DEPLOYMENT_REGIONS, region, config_path=config_path)


def set_deployment_region_role(
    role: str, region: str, *, config_path: Path | str | None = None
) -> ChangeReport:
    """Set one control-plane region scalar (``global``/``api_gateway``/``monitoring``)."""
    key = DEPLOYMENT_REGION_SCALARS.get(role)
    if key is None:
        raise ManagedConfigError(
            f"unknown deployment-region role {role!r}; "
            f"expected one of {sorted(DEPLOYMENT_REGION_SCALARS)}"
        )
    return managed_scalar_set(key, region, config_path=config_path)


def get_bedrock_model_status(*, config_path: Path | str | None = None) -> dict[str, Any]:
    """Return the configured Bedrock default model ID and its backing path."""
    path = _resolve_config_path(config_path)
    document, _ = _load_document(path)
    return {
        "config_path": str(path),
        "default_model_id": _current_scalar(document, BEDROCK_DEFAULT_MODEL),
    }


def set_default_bedrock_model(
    model_id: str, *, config_path: Path | str | None = None
) -> ChangeReport:
    """Set ``bedrock.default_model_id`` (advisory-feature model default)."""
    return managed_scalar_set(BEDROCK_DEFAULT_MODEL, model_id, config_path=config_path)
