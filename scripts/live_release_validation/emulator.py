"""Explicit, verified emulator opt-in for the live validation harness.

The harness is local-only by design: ``require_local_execution`` refuses to
run inside GitHub Actions so ordinary CI can never touch a real AWS account.
One narrowly scoped exception exists: running the ENTIRE harness against a
local AWS emulator (Floci) — the CI rehearsal layer documented in
docs/FLOCI_TESTING.md. The exception is opt-in and fails closed:

1. ``GCO_LIVE_VALIDATION_EMULATOR`` must name the emulator's base URL;
2. ``AWS_ENDPOINT_URL`` must point at exactly that URL, so every SDK client
   in the run — harness, CLI, CDK — resolves to the emulator;
3. the URL must be plain ``http://`` on an allow-listed local hostname
   (every real AWS endpoint is HTTPS on ``*.amazonaws.com``); and
4. STS must echo the fabricated 12-digit access-key id back as the caller
   account — emulator multi-account behavior that real AWS cannot imitate,
   because a fabricated key id never passes real signature validation.

Any violation raises before a checkpoint or AWS client exists. Nothing else
about the harness changes in emulator mode: preflight still pins account,
SHA, branch, and worktree cleanliness, and cleanup still runs. This is a
deliberate testability seam, not an emulator compatibility layer — no other
harness code consults it.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

EMULATOR_ENDPOINT_ENV = "GCO_LIVE_VALIDATION_EMULATOR"

_ALLOWED_EMULATOR_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "floci"})


def emulator_endpoint_requested() -> str | None:
    """The declared emulator endpoint, or None for a real-AWS run."""
    value = os.environ.get(EMULATOR_ENDPOINT_ENV, "").strip()
    return value or None


def verify_emulator_endpoint(endpoint: str) -> None:
    """Prove ``endpoint`` is an emulator or raise ``RuntimeError``.

    Performs the static URL checks and the STS identity-echo probe described
    in the module docstring. Callers must invoke this BEFORE building any
    other AWS client so a misconfigured run dies without side effects.
    """
    normalized = endpoint.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "http":
        raise RuntimeError(
            f"Emulator endpoint {endpoint!r} must be plain http; https implies a real service"
        )
    if (parsed.hostname or "") not in _ALLOWED_EMULATOR_HOSTNAMES:
        raise RuntimeError(
            f"Emulator endpoint host {parsed.hostname!r} is not an allowed emulator host "
            f"({', '.join(sorted(_ALLOWED_EMULATOR_HOSTNAMES))})"
        )
    configured = os.environ.get("AWS_ENDPOINT_URL", "").rstrip("/")
    if configured != normalized:
        raise RuntimeError(
            "AWS_ENDPOINT_URL must point at the declared emulator endpoint "
            f"({normalized!r}), got {configured!r}; refusing a split-endpoint run"
        )
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    if len(access_key) != 12 or not access_key.isdigit():
        raise RuntimeError(
            "Emulator runs require a fabricated 12-digit AWS_ACCESS_KEY_ID (the emulator "
            "account id); refusing credentials that could belong to a real principal"
        )

    import boto3

    identity = boto3.client("sts").get_caller_identity()
    account = str(identity.get("Account") or "")
    if account != access_key:
        raise RuntimeError(
            f"STS at {normalized} answered account {account!r} instead of echoing the "
            f"fabricated key id {access_key!r}; this endpoint does not behave like an "
            "emulator, so the run is refused"
        )
