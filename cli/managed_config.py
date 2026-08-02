"""Managed deployment-config engine: validated, atomic, audited cdk.json edits.

This module is the categorical answer to "add a CLI/MCP toggle for cdk.json
knob X" requests (issue #221). Instead of re-implementing read/validate/write
logic per knob, each externally manageable key registers a :class:`ManagedListKey`
(list-with-set-semantics today; scalar kinds can join the registry later) and
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gco.stacks.constants import (
    validated_deployment_partition,
    validated_regional_deployment_regions,
)

from .stacks import _atomic_write_bytes, _find_cdk_json

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
    action: str  # "add" | "remove"
    value: str
    changed: bool
    old: tuple[str, ...]
    new: tuple[str, ...]
    config_path: Path

    def summary(self) -> str:
        """One human line suitable for CLI output and audit trails."""
        if not self.changed:
            state = "already present" if self.action == "add" else "not present"
            return f"{self.key_id}: no change ({self.value!r} {state})"
        return (
            f"{self.key_id}: {self.action} {self.value!r} "
            f"({list(self.old)} -> {list(self.new)}) in {self.config_path}"
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


#: The one key managed today. New knobs register here instead of growing
#: bespoke read/validate/write code paths.
REGIONAL_DEPLOYMENT_REGIONS = ManagedListKey(
    key_id="deployment_regions.regional",
    container="deployment_regions",
    leaf="regional",
    description="Workload Regions that receive an EKS regional stack",
    default=_DEFAULT_REGIONAL,
    validate_result=_validate_regional_result,
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
    keys and their placement. The original trailing-newline state is kept so
    diffs stay minimal regardless of how the file was last formatted.
    """
    serialized = json.dumps(document, indent=2).encode("utf-8")
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
    """Shared add/remove core: resolve, load, no-op check, validate, write."""
    path = _resolve_config_path(config_path)
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
