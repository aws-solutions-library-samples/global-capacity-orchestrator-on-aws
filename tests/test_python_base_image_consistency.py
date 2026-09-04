"""Python base-image pins stay consistent across service containers.

The platform's service containers and the dev image all build from a single
pinned ``python:<major>.<minor>.<patch>-slim`` tag. This guard fails if any of
them drifts to a different patch — or to a rolling tag like ``3.14`` or
``latest`` — so a version bump cannot silently leave one Dockerfile behind
(the exact footgun a multi-file bump invites).

The AWS Lambda base image (``public.ecr.aws/lambda/python:<major>.<minor>``)
is intentionally excluded: it has no patch-pinned tag and tracks the latest
3.x patch on its own. The example job manifests under ``examples/`` are
illustrative and are likewise not coupled here.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_FROM_PYTHON = re.compile(r"^\s*FROM\s+python:(\S+)", re.MULTILINE)
_PINNED_SLIM = re.compile(r"\d+\.\d+\.\d+-slim")


def _service_dockerfiles() -> list[Path]:
    """The service container Dockerfiles plus the dev image.

    Discovered by glob so a future service Dockerfile is covered automatically.
    """
    files = sorted((REPO_ROOT / "dockerfiles").glob("*-dockerfile"))
    files.append(REPO_ROOT / "Dockerfile.dev")
    return [f for f in files if f.is_file()]


def test_service_dockerfiles_are_discovered():
    """Sanity: discovery finds the dev image and the known service Dockerfiles."""
    names = {f.name for f in _service_dockerfiles()}
    assert "Dockerfile.dev" in names
    service = {n for n in names if n.endswith("-dockerfile")}
    # health-monitor, inference-monitor, inference-proxy, manifest-processor,
    # and queue-processor.
    assert len(service) >= 5, f"expected >=5 service Dockerfiles, found {service}"


def test_all_service_dockerfiles_share_one_pinned_python_tag():
    """Every service/dev Dockerfile builds from the same pinned python:*-slim tag."""
    tags: dict[str, str] = {}
    for path in _service_dockerfiles():
        match = _FROM_PYTHON.search(path.read_text())
        assert match, f"{path.name}: no 'FROM python:...' line found"
        tags[path.name] = match.group(1)

    distinct = set(tags.values())
    assert len(distinct) == 1, f"Python base-image tags drifted across Dockerfiles: {tags}"

    (tag,) = distinct
    assert _PINNED_SLIM.fullmatch(tag), (
        f"Python base image must be a fully pinned 'X.Y.Z-slim' tag (no rolling tag), got {tag!r}"
    )
