"""Shared fail-closed parsing for manifest admission security policy."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

MANIFEST_SECURITY_POLICY_DEFAULTS: Final[Mapping[str, bool]] = {
    "block_privileged": True,
    "block_privilege_escalation": True,
    "block_host_network": True,
    "block_host_pid": True,
    "block_host_ipc": True,
    "block_host_path": True,
    "block_added_capabilities": True,
    "block_run_as_root": False,
}

_TRUE_BOOLEAN_VALUES: Final = frozenset({"true", "1", "yes", "on"})
_FALSE_BOOLEAN_VALUES: Final = frozenset({"false", "0", "no", "off"})


def parse_boolean_environment(name: str, default: bool) -> bool:
    """Parse one boolean environment variable without treating typos as false.

    Unset or blank values retain the documented default. Non-empty values must
    use an explicit true or false spelling; malformed deployment substitutions
    raise during service startup instead of disabling an admission control.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default

    normalized = raw.strip().lower()
    if normalized in _TRUE_BOOLEAN_VALUES:
        return True
    if normalized in _FALSE_BOOLEAN_VALUES:
        return False
    raise ValueError(
        f"{name} must be an explicit boolean value "
        f"(true/false, 1/0, yes/no, or on/off); got {raw!r}"
    )


def validate_manifest_security_policy(policy: object) -> dict[str, bool]:
    """Return a complete policy after rejecting malformed or unknown fields."""
    if not isinstance(policy, Mapping):
        raise ValueError("manifest_security_policy must be an object")

    unsupported = sorted(
        repr(key) for key in policy if key not in MANIFEST_SECURITY_POLICY_DEFAULTS
    )
    if unsupported:
        raise ValueError(
            "manifest_security_policy contains unsupported fields: " + ", ".join(unsupported)
        )

    validated = dict(MANIFEST_SECURITY_POLICY_DEFAULTS)
    for key in MANIFEST_SECURITY_POLICY_DEFAULTS:
        if key not in policy:
            continue
        value = policy[key]
        if type(value) is not bool:
            raise ValueError(f"manifest_security_policy.{key} must be a boolean")
        validated[key] = value
    return validated
