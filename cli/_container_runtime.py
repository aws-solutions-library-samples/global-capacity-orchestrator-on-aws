"""
Container runtime detection (Docker, Finch, Podman) — shared helper.

Originally part of cli/stacks.py for CDK asset bundling; extracted so
the new cli/images.py ImageManager can reuse the cached detection
without duplicating the probe logic.

CDK requires a container runtime to build Lambda function assets, and
the image registry uses the same runtime for ``docker build`` /
``docker push`` calls. This module checks for available runtimes in
order of preference and verifies they are actually running (not just
installed).

Priority order: docker > finch > podman.

If the ``CDK_DOCKER`` environment variable is set, that value is
returned without checking if the runtime is available.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-08-14T03:46:22Z
# Flowchart(s) generated from this file:
#   * ``detect_container_runtime`` -> ``diagrams/code_diagrams/cli/_container_runtime.detect_container_runtime.html``
#     (PNG: ``diagrams/code_diagrams/cli/_container_runtime.detect_container_runtime.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger(__name__)

# Cached result for container runtime detection.
#
# Sentinel pattern: ``_UNCHECKED`` means the probe has not run yet;
# any other value (including ``None``, which means "no runtime found")
# is the cached result of the last probe. Using a single sentinel
# instead of two separate ``_cache`` / ``_checked`` globals keeps the
# cache state idempotent under concurrent first-callers and avoids
# the static-analysis false positive on a stand-alone bool flag.
_UNCHECKED: object = object()
_container_runtime_cache: str | None | object = _UNCHECKED


def detect_container_runtime() -> str | None:
    """
    Detect available container runtime (cached).

    Returns:
        Runtime name (``"docker"``, ``"finch"``, or ``"podman"``) if a
        runtime is found and running, ``None`` if nothing is available.

    Note:
        If the ``CDK_DOCKER`` environment variable is set, that value
        is returned without checking if the runtime is available.
    """
    global _container_runtime_cache
    if _container_runtime_cache is not _UNCHECKED:
        # ``_container_runtime_cache`` is narrowed to ``str | None`` once
        # past the sentinel check, but mypy can't infer that across the
        # ``object`` union. The runtime cast is explicit.
        return _container_runtime_cache  # type: ignore[return-value]

    result = _detect_container_runtime_uncached()
    _container_runtime_cache = result
    return result


def _detect_container_runtime_uncached() -> str | None:
    """Uncached implementation of container runtime detection."""
    # Check if CDK_DOCKER is already set
    if os.environ.get("CDK_DOCKER"):
        return os.environ["CDK_DOCKER"]

    # Try docker first
    if shutil.which("docker"):
        # Verify docker is actually running
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return "docker"
        except Exception as e:
            logger.debug("docker info check failed: %s", e)

    # Try finch as fallback
    if shutil.which("finch"):
        try:
            result = subprocess.run(
                ["finch", "info"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return "finch"
        except Exception as e:
            logger.debug("finch info check failed: %s", e)

    # Try podman as last resort
    if shutil.which("podman"):
        try:
            result = subprocess.run(
                ["podman", "info"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return "podman"
        except Exception as e:
            logger.debug("podman info check failed: %s", e)

    return None


def container_runtime_error_message(*, allow_cdk_docker: bool = False) -> str:
    """Build an actionable "no runtime" message for the situation on this host.

    Detection requires a runtime that is *running*, not merely installed, so the
    two failure modes need different advice and conflating them wastes real time:
    a stopped Finch VM was reported as "please install Finch" during a live
    release-validation run on 2026-08-26, on a machine that already had it. The
    install hint sent the reader to a page they had already followed.

    So: if a runtime binary is on PATH but did not answer, say so and give the
    command that starts it. Only suggest installing when nothing is present.
    """
    import shutil

    installed = [name for name in ("docker", "finch", "podman") if shutil.which(name)]

    if installed:
        start_hints = {
            "docker": "start Docker Desktop (or `open -a Docker` on macOS)",
            "finch": "finch vm start   (first time: finch vm init)",
            "podman": "podman machine start   (first time: podman machine init)",
        }
        lines = [
            "No container runtime is running. These are installed but did not "
            f"respond: {', '.join(installed)}.",
            "",
            "Start one and retry:",
        ]
        lines += [f"  - {name}: {start_hints[name]}" for name in installed]
        lines += [
            "",
            "Detection runs `<runtime> info` with a 5s timeout, so a runtime whose "
            "VM is still booting also reports as unavailable — give it a moment "
            "and retry.",
        ]
        if allow_cdk_docker:
            lines.append("Alternatively set CDK_DOCKER=<path> to a runtime binary.")
        return "\n".join(lines)

    lines = [
        "No container runtime found. Install Docker, Finch, or Podman.",
        "  - Docker: https://docs.docker.com/get-docker/",
        "  - Finch: brew install finch && finch vm init && finch vm start",
        "  - Podman: https://podman.io/getting-started/installation",
    ]
    if allow_cdk_docker:
        lines.append("Alternatively set CDK_DOCKER=<path> to a runtime binary.")
    return "\n".join(lines)
