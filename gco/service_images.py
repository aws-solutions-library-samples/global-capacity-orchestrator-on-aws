"""Discovery of the container images this repository builds.

There is deliberately no hand-kept list of service images anywhere in the
tree. Every in-cluster platform service is built from
``dockerfiles/Dockerfile.<service>`` and that filename *is* the catalog: the
regional stack (image assets and asset-hash excludes), the CLI's maintained
image inventory, the CI build/scan matrix and the inventory guard test all
read this module, so adding a service is a matter of adding its Dockerfile.

Three image families are discovered, each from the tree rather than listed:

* **platform services** — ``dockerfiles/Dockerfile.<service>``, built with
  the repository root as context (they ``COPY`` shared ``gco/`` code);
* **container Lambdas** — ``lambda/<name>/Dockerfile``, built with the
  Lambda's own directory as context (``*-build`` staging dirs are skipped);
* **root images** — ``Dockerfile.<name>`` at the repository root (today the
  contributor dev container), built from the root.

Run as a module to print the GitHub Actions matrix the container-scan job
consumes::

    python3 -m gco.service_images --github-matrix

The module uses only the standard library so CI can run it with the
runner's stock interpreter, before any dependency is installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

#: Repository root (``gco/`` lives one level below it).
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directory holding one Dockerfile per in-cluster platform service.
SERVICE_DOCKERFILE_DIR = "dockerfiles"

_DOCKERFILE_PREFIX = "Dockerfile."

#: Image names double as ``gco/<name>`` ECR repository suffixes and CI cache
#: scopes, so they must be short DNS-style labels (same shape ``cli.images``
#: accepts for user images).
_IMAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class ImageBuild:
    """One buildable image: its short name, Dockerfile and build context."""

    image: str
    dockerfile: str
    context: str


def service_dockerfile(service: str) -> str:
    """Return the repo-relative Dockerfile path for ``service``.

    The naming convention lives here and nowhere else:
    ``dockerfiles/Dockerfile.<service>``.
    """
    return f"{SERVICE_DOCKERFILE_DIR}/{_DOCKERFILE_PREFIX}{service}"


def _validated_name(name: str, source: Path) -> str:
    """Return ``name`` if it is a usable image name, else raise ``ValueError``.

    Discovery turns filenames into ECR repository suffixes, image tags and CI
    cache scopes, so a name that is not a short DNS-style label has to fail
    here rather than downstream in a registry or a workflow expression.
    """
    if not _IMAGE_NAME_RE.match(name):
        raise ValueError(
            f"{source}: '{name}' is not a valid image name. Service Dockerfiles must be "
            f"named {_DOCKERFILE_PREFIX}<name> and container Lambdas must live in "
            f"lambda/<name>/, where <name> matches {_IMAGE_NAME_RE.pattern} (it becomes the "
            "ECR repository suffix, the image tag and the CI cache scope)."
        )
    return name


def _image_name(dockerfile: Path) -> str:
    """Derive the image name from a ``Dockerfile.<name>`` filename, validating it."""
    return _validated_name(dockerfile.name[len(_DOCKERFILE_PREFIX) :], dockerfile)


def discover_service_dockerfiles(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Return ``{service: repo-relative Dockerfile path}`` for every shipped service image.

    Sorted by service name so every consumer sees the same order. Raises
    ``ValueError`` for a ``Dockerfile.*`` whose suffix is not a valid image
    name — a stray editor backup in ``dockerfiles/`` should fail loudly, not
    silently become (or hide) a service.
    """
    directory = repo_root / SERVICE_DOCKERFILE_DIR
    found = sorted(directory.glob(f"{_DOCKERFILE_PREFIX}*"))
    return {
        _image_name(path): f"{SERVICE_DOCKERFILE_DIR}/{path.name}"
        for path in found
        if path.is_file()
    }


def discover_image_builds(repo_root: Path = REPO_ROOT) -> list[ImageBuild]:
    """Return every image the repository builds, services first.

    Platform services come from :func:`discover_service_dockerfiles`;
    container Lambdas are every ``lambda/<name>/Dockerfile`` (skipping the
    generated ``*-build`` staging directories); root images are every
    ``Dockerfile.<name>`` at the repository root.
    """
    builds = [
        ImageBuild(image=name, dockerfile=dockerfile, context=".")
        for name, dockerfile in discover_service_dockerfiles(repo_root).items()
    ]
    for dockerfile in sorted((repo_root / "lambda").glob("*/Dockerfile")):
        lambda_dir = dockerfile.parent
        if lambda_dir.name.endswith("-build"):
            continue
        builds.append(
            ImageBuild(
                image=_validated_name(lambda_dir.name, lambda_dir),
                dockerfile=f"lambda/{lambda_dir.name}/Dockerfile",
                context=f"lambda/{lambda_dir.name}",
            )
        )
    for dockerfile in sorted(repo_root.glob(f"{_DOCKERFILE_PREFIX}*")):
        if dockerfile.is_file():
            builds.append(
                ImageBuild(image=_image_name(dockerfile), dockerfile=dockerfile.name, context=".")
            )
    return builds


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: print discovered images as JSON.

    ``--github-matrix`` emits the ``include`` list for a GitHub Actions job
    matrix (one object per image with ``image``/``dockerfile``/``context``
    keys); without it the same list is pretty-printed for humans.
    """
    parser = argparse.ArgumentParser(description="List the container images this repo builds.")
    parser.add_argument(
        "--github-matrix",
        action="store_true",
        help="print a compact JSON list suitable for `fromJSON()` in a workflow matrix",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root to scan (default: the checkout this module lives in)",
    )
    args = parser.parse_args(argv)
    rows = [asdict(build) for build in discover_image_builds(args.repo_root)]
    if not rows:
        print(f"no Dockerfiles discovered under {args.repo_root}", file=sys.stderr)
        return 1
    indent = None if args.github_matrix else 2
    print(json.dumps(rows, indent=indent))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
