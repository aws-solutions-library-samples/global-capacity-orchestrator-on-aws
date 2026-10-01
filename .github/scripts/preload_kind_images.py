#!/usr/bin/env python3
"""Preload the images a rendered Helm chart runs into a kind node.

``helm install --wait`` in the kind jobs leaves every image pull to the
kubelet, which retries a failed pull on its own exponential backoff (10 s,
20 s, ... up to 5 min between attempts), so an eight-minute wait gets about six
tries. That is not enough for Amazon ECR Public. Anonymous clients get one
image-pull request per second (the documented "Rate of unauthenticated image
pulls" quota, which is not adjustable), and ECR Public answers the next request
with 429 Too Many Requests. A pull by tag sends its manifest requests
back-to-back (the tag, the image index, then the platform manifest), so the
second is often refused even from an idle client. GitHub-hosted runners also
share their egress addresses with other jobs. Argo CD's chart runs Redis from
ECR Public, and ``integration:kind:platform-addons`` kept failing on exactly
that 429 (``argocd-redis`` stuck in ImagePullBackOff until the wait expired).

For every image the rendered manifests run, this pulls the image on the runner
with retry and then ``kind load``s it, so the kubelet (the charts pull
``IfNotPresent``) finds it on the node and never contacts a registry. Helm test
hooks are skipped because ``helm install`` never starts them. A Docker Official
Image served from ECR Public (``<ecr-public>/docker/library/<name>``) is first
resolved to its digest with paced requests to ECR Public. It is then pulled by
that digest from Docker Hub, which publishes the same index, and tagged back to
the chart's own reference. The bytes are the ones ECR Public serves, and nothing
the chart renders changes. If Docker Hub does not serve that digest, the image is
pulled from ECR Public by digest, with more attempts. If ECR Public never answers
the digest lookup (every request throttled), Docker Hub's copy of the same tag is
used, with a warning; a definitive answer, such as a tag ECR Public does not
have, still fails the step. Any other image is pulled as written. An image pinned
by digest cannot be addressed by ``kind load``, so it is left to the kubelet,
with a warning.

``--pinned IMAGE=LOCAL`` covers a pod the job writes itself that must run
digest-pinned bytes. Anonymous ECR Public pulls also run into its data limit
(429 "Data limit exceeded") from shared runner addresses, which kept failing
``integration:kind:examples-smoke`` on the MLflow TLS sidecar's pinned Python
image. The image is pulled by its digest (Docker Hub's copy first for a Docker
Official Image on ECR Public, then ECR Public), tagged ``LOCAL`` and loaded. The
pod names ``LOCAL`` with ``imagePullPolicy: Never``, so it never reaches a
registry.

``--image NAME`` loads an image a previous step already put in the runner's
Docker (a service image built with ``load: true``, or an example's image pulled
with retry) without pulling anything.

Every load goes through one path. ``kind load docker-image`` runs ``docker
save`` for the whole image and imports the archive with ``ctr images import
--all-platforms``. Docker 29 (the Ubuntu 26.04 runner image) stores images in
containerd, where a pulled or built image is recorded under its multi-platform
index while only the daemon's own platform has content, so that import fails
with ``content digest sha256:...: not found`` (kubernetes-sigs/kind#4224). This
script instead saves each image with ``docker save --platform`` for the
daemon's platform (``docker version`` reports it), so the archive carries one
manifest and the blobs behind it, and loads that archive with ``kind load
image-archive``. One archive at a time keeps the runner's disk peak at one
image. The archive lives under ``TMPDIR``, which the workflows point at the
runner's work disk.

Usage::

    python3 .github/scripts/preload_kind_images.py --cluster NAME RENDERED.yaml [...]
    python3 .github/scripts/preload_kind_images.py --cluster NAME --pinned IMAGE@DIGEST=LOCAL:TAG
    python3 .github/scripts/preload_kind_images.py --cluster NAME --image NAME:TAG [--image ...]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any

import yaml

#: Amazon ECR Public's registry hosts: the gallery name and the newer endpoint
#: charts reference (Argo CD's runs ``ecr-public.aws.com/docker/library/redis``).
ECR_PUBLIC_HOSTS = frozenset({"public.ecr.aws", "ecr-public.aws.com"})
#: ECR Public's service name in its token realm, the same for both hosts.
ECR_PUBLIC_SERVICE = "public.ecr.aws"
#: ECR Public's mirror of the Docker Official Images, published by Docker.
DOCKER_LIBRARY = "docker/library/"
DOCKER_HUB_LIBRARY = "docker.io/library/"

MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
#: ``os/arch`` as ``docker version`` prints it (``linux/amd64``, ``linux/arm64``).
PLATFORM_RE = re.compile(r"^[a-z0-9]+/[a-z0-9]+$")
#: The daemon's own platform, the one an image it built or pulled has content for.
DAEMON_PLATFORM_ARGV = ("docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}")

#: Pod-spec keys whose entries carry an ``image``.
CONTAINER_KEYS = ("initContainers", "containers", "ephemeralContainers")
#: ``helm.sh/hook`` values ``helm install`` never runs (``helm test`` does).
TEST_HOOKS = frozenset({"test", "test-success", "test-failure"})

#: ECR Public admits one anonymous request per second: space them further apart.
REQUEST_SPACING_SECONDS = 1.5
REGISTRY_ATTEMPTS = 8
REGISTRY_BACKOFF_CAP_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 20.0
PULL_ATTEMPTS = 4
PULL_DELAY_SECONDS = 15.0
#: The ECR Public fallback pull trips the same throttle, so it gets more tries.
ECR_PUBLIC_PULL_ATTEMPTS = 12
ECR_PUBLIC_PULL_DELAY_SECONDS = 5.0

Runner = Callable[[Sequence[str]], int]
#: Runs a command and returns its stdout; an empty string when it fails.
Capturer = Callable[[Sequence[str]], str]
Opener = Callable[[urllib.request.Request, float], Any]
Sleeper = Callable[[float], None]


class PreloadError(RuntimeError):
    """An image could not be resolved, pulled, tagged or loaded."""


class RegistryUnreachable(PreloadError):
    """Every attempt was throttled (429) or lost in transport: no answer about the image."""


@dataclass(frozen=True)
class ImageReference:
    """One image reference, normalized the way Docker and containerd read it."""

    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @classmethod
    def parse(cls, reference: str) -> ImageReference:
        """Split ``[registry/]repository[:tag][@digest]``; Docker Hub is the default registry."""
        name, _, digest = reference.strip().partition("@")
        first, slash, rest = name.partition("/")
        if slash and ("." in first or ":" in first or first == "localhost"):
            registry, path = first, rest
        else:
            registry, path = "docker.io", name
            if "/" not in path:
                path = f"library/{path}"
        tag: str | None = None
        if ":" in path.rsplit("/", 1)[-1]:
            path, tag = path.rsplit(":", 1)
        if not all(path.split("/")) or tag == "" or (digest and not DIGEST_RE.match(digest)):
            raise PreloadError(f"cannot parse the image reference {reference!r}")
        if not digest and tag is None:
            tag = "latest"
        return cls(registry=registry, repository=path, tag=tag, digest=digest or None)

    def docker_hub_twin(self) -> str | None:
        """Docker Hub's name for a Docker Official Image mirrored on ECR Public, else None."""
        if self.registry in ECR_PUBLIC_HOSTS and self.repository.startswith(DOCKER_LIBRARY):
            return DOCKER_HUB_LIBRARY + self.repository.removeprefix(DOCKER_LIBRARY)
        return None


def _is_test_hook(document: Mapping[str, Any]) -> bool:
    metadata = document.get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, Mapping) else None
    hooks = annotations.get("helm.sh/hook", "") if isinstance(annotations, Mapping) else ""
    return any(hook.strip() in TEST_HOOKS for hook in str(hooks).split(","))


def _container_images(node: Any) -> Iterator[str]:
    """Every container image under ``node``, wherever a pod template sits."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in CONTAINER_KEYS and isinstance(value, list):
                for container in value:
                    image = container.get("image") if isinstance(container, Mapping) else None
                    if isinstance(image, str) and image.strip():
                        yield image.strip()
            else:
                yield from _container_images(value)
    elif isinstance(node, list):
        for item in node:
            yield from _container_images(item)


def collect_images(texts: Iterable[str]) -> list[str]:
    """The images the rendered manifests run, in first-seen order, test hooks excluded."""
    images: dict[str, None] = {}
    for text in texts:
        for document in yaml.safe_load_all(text):
            if isinstance(document, Mapping) and not _is_test_hook(document):
                for image in _container_images(document):
                    images.setdefault(image, None)
    return list(images)


def _open(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(  # nosec B310  # nosemgrep: dynamic-urllib-use-detected - every URL is https:// on a host from the fixed ECR_PUBLIC_HOSTS set; a reference on any other registry never reaches this function
        request, timeout=timeout
    )


def _run(argv: Sequence[str]) -> int:
    return subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed docker/kind argv, no shell=True
        list(argv), check=False
    ).returncode


def _capture(argv: Sequence[str]) -> str:
    completed = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed docker argv, no shell=True
        list(argv), check=False, capture_output=True, text=True
    )
    return completed.stdout if completed.returncode == 0 else ""


def _registry_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    opener: Opener = _open,
    sleep: Sleeper = time.sleep,
    attempts: int = REGISTRY_ATTEMPTS,
    spacing: float = REQUEST_SPACING_SECONDS,
) -> tuple[Message, bytes]:
    """One ECR Public request, retried on 429 and transport errors with growing waits.

    Any other HTTP status is an answer (a missing tag is a 404) and raises
    :class:`PreloadError` at once; running out of attempts raises
    :class:`RegistryUnreachable`.
    """
    last_error = ""
    for attempt in range(1, attempts + 1):
        retry_after = ""
        request = urllib.request.Request(url, method=method, headers=dict(headers or {}))
        try:
            with opener(request, REQUEST_TIMEOUT_SECONDS) as response:
                return response.headers, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise PreloadError(f"{method} {url} answered HTTP {exc.code}") from exc
            last_error = "HTTP 429 Too Many Requests"
            retry_after = str((exc.headers.get("Retry-After") if exc.headers else None) or "")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            wait = min(spacing * 2 ** (attempt - 1), REGISTRY_BACKOFF_CAP_SECONDS)
            if retry_after.isdigit():
                wait = max(wait, float(retry_after))
            print(f"{method} {url}: {last_error}; retrying in {wait:.1f}s", flush=True)
            sleep(wait)
    raise RegistryUnreachable(f"{method} {url} failed after {attempts} attempts ({last_error})")


def ecr_public_digest(
    reference: ImageReference,
    *,
    opener: Opener = _open,
    sleep: Sleeper = time.sleep,
) -> str:
    """Resolve an ECR Public tag to the digest it serves: one token and one HEAD request."""
    if reference.digest is not None:
        return reference.digest
    scope = f"repository:{reference.repository}:pull"
    token_url = f"https://{reference.registry}/token/?service={ECR_PUBLIC_SERVICE}&scope={scope}"
    _headers, body = _registry_request(token_url, opener=opener, sleep=sleep)
    try:
        token = json.loads(body)["token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PreloadError(f"{token_url} returned no anonymous token") from exc
    sleep(REQUEST_SPACING_SECONDS)
    manifest_url = (
        f"https://{reference.registry}/v2/{reference.repository}/manifests/{reference.tag}"
    )
    headers, _body = _registry_request(
        manifest_url,
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT},
        opener=opener,
        sleep=sleep,
    )
    digest = str(headers.get("Docker-Content-Digest") or "").strip()
    if not DIGEST_RE.match(digest):
        raise PreloadError(f"{manifest_url} returned no usable Docker-Content-Digest ({digest!r})")
    return digest


def pull_with_retry(
    image: str,
    *,
    run: Runner = _run,
    sleep: Sleeper = time.sleep,
    attempts: int = PULL_ATTEMPTS,
    delay: float = PULL_DELAY_SECONDS,
) -> bool:
    """``docker pull`` with a fixed wait between attempts; False once they run out."""
    for attempt in range(1, attempts + 1):
        print(f"::group::docker pull {image} (attempt {attempt}/{attempts})", flush=True)
        code = run(["docker", "pull", image])
        print("::endgroup::", flush=True)
        if code == 0:
            return True
        if attempt < attempts:
            print(
                f"::warning::docker pull {image} failed (attempt {attempt}/{attempts}); "
                f"retrying in {delay:.0f}s",
                flush=True,
            )
            sleep(delay)
    return False


def _tag(source: str, target: str, run: Runner) -> None:
    if run(["docker", "tag", source, target]) != 0:
        raise PreloadError(f"docker tag {source} {target} failed")


def stage_image(
    image: str,
    *,
    run: Runner = _run,
    sleep: Sleeper = time.sleep,
    resolve: Callable[[ImageReference], str] = ecr_public_digest,
) -> bool:
    """Put ``image`` in the runner's Docker under the chart's own name; False when skipped."""
    reference = ImageReference.parse(image)
    if reference.digest is not None:
        print(
            f"::warning::{image} is pinned by digest, which kind load cannot address; "
            "the kubelet pulls it",
            flush=True,
        )
        return False
    twin = reference.docker_hub_twin()
    if twin is None:
        if not pull_with_retry(image, run=run, sleep=sleep):
            raise PreloadError(f"could not pull {image} after {PULL_ATTEMPTS} attempts")
        return True

    try:
        digest = resolve(reference)
    except RegistryUnreachable as exc:
        # ECR Public never answered, so its digest cannot be compared. The
        # tag is the Docker Official Image either way. A definitive answer
        # (a tag ECR Public does not have) is a PreloadError and is not caught.
        source = f"{twin}:{reference.tag}"
        print(f"::warning::{exc}; pulling {source} without the digest check", flush=True)
        if not pull_with_retry(source, run=run, sleep=sleep):
            raise PreloadError(f"could not pull {source} after {PULL_ATTEMPTS} attempts") from exc
        _tag(source, image, run)
        return True
    source = f"{twin}@{digest}"
    if pull_with_retry(source, run=run, sleep=sleep):
        _tag(source, image, run)
        print(f"{image}: pulled {source}, Docker Hub's copy of the same index", flush=True)
        return True
    print(f"::warning::Docker Hub does not serve {source}; pulling it from ECR Public", flush=True)
    fallback = f"{reference.registry}/{reference.repository}@{digest}"
    if not pull_with_retry(
        fallback,
        run=run,
        sleep=sleep,
        attempts=ECR_PUBLIC_PULL_ATTEMPTS,
        delay=ECR_PUBLIC_PULL_DELAY_SECONDS,
    ):
        raise PreloadError(
            f"could not pull {image} ({digest}) from Docker Hub or, after "
            f"{ECR_PUBLIC_PULL_ATTEMPTS} attempts, from ECR Public"
        )
    _tag(fallback, image, run)
    return True


def stage_pinned_image(
    image: str,
    local: str,
    *,
    run: Runner = _run,
    sleep: Sleeper = time.sleep,
) -> None:
    """Put the digest-pinned ``image`` in the runner's Docker under the local tag ``local``.

    ``kind load`` cannot address a digest reference, so a pod that must run
    pinned bytes without a registry names a local tag and pulls ``Never``.
    Docker checks a pull by digest against that digest, so the bytes are the
    pinned ones whichever registry serves them. A Docker Official Image on ECR
    Public comes from Docker Hub first, which publishes the same index, then
    from ECR Public with more attempts. Any other image is pulled by digest
    from its own registry.
    """
    reference = ImageReference.parse(image)
    if reference.digest is None:
        raise PreloadError(f"{image} is not pinned by digest")
    if ImageReference.parse(local).digest is not None:
        raise PreloadError(f"the local name {local} must be a tag, not a digest")
    own = f"{reference.registry}/{reference.repository}@{reference.digest}"
    twin = reference.docker_hub_twin()
    sources = (
        [
            (f"{twin}@{reference.digest}", PULL_ATTEMPTS, PULL_DELAY_SECONDS),
            (own, ECR_PUBLIC_PULL_ATTEMPTS, ECR_PUBLIC_PULL_DELAY_SECONDS),
        ]
        if twin is not None
        else [(own, PULL_ATTEMPTS, PULL_DELAY_SECONDS)]
    )
    for source, attempts, delay in sources:
        if pull_with_retry(source, run=run, sleep=sleep, attempts=attempts, delay=delay):
            _tag(source, local, run)
            print(f"{image}: pulled {source} and tagged it {local}", flush=True)
            return
        print(f"::warning::could not pull {source}", flush=True)
    raise PreloadError(
        f"could not pull {image} from " + " or ".join(source for source, _, _ in sources)
    )


def _pinned_argument(value: str) -> tuple[str, str]:
    """Parse ``--pinned IMAGE=LOCAL``."""
    image, separator, local = value.partition("=")
    if not separator or not image.strip() or not local.strip():
        raise argparse.ArgumentTypeError(f"expected IMAGE=LOCAL, got {value!r}")
    return image.strip(), local.strip()


def daemon_platform(*, capture: Capturer = _capture) -> str:
    """The Docker daemon's ``os/arch``: the one platform its images have content for."""
    platform = capture(DAEMON_PLATFORM_ARGV).strip()
    if not PLATFORM_RE.match(platform):
        raise PreloadError(f"docker version reported no usable daemon platform ({platform!r})")
    return platform


def load_into_kind(
    images: Sequence[str],
    cluster: str,
    *,
    run: Runner = _run,
    capture: Capturer = _capture,
) -> None:
    """Load every staged image into the kind node, one single-platform archive at a time.

    ``docker save --platform`` writes only the daemon's platform, so the
    archive is complete and ``kind load image-archive`` (``ctr images import
    --all-platforms``) accepts it from Docker's containerd image store, where a
    plain ``kind load docker-image`` fails on the other platforms' missing
    content. Each archive is removed before the next is written.
    """
    if not images:
        return
    platform = daemon_platform(capture=capture)
    for image in images:
        with tempfile.TemporaryDirectory(prefix="kind-image-") as directory:
            archive = str(Path(directory) / "image.tar")
            if run(["docker", "save", "--platform", platform, "--output", archive, image]) != 0:
                raise PreloadError(f"docker save --platform {platform} {image} failed")
            if run(["kind", "load", "image-archive", "--name", cluster, archive]) != 0:
                raise PreloadError(f"kind load image-archive of {image} into {cluster} failed")
        print(f"loaded {image} ({platform}) into kind cluster {cluster}", flush=True)


def _image_argument(value: str) -> str:
    """Parse ``--image NAME``: a tag reference already in the runner's Docker."""
    image = value.strip()
    try:
        reference = ImageReference.parse(image)
    except PreloadError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if reference.digest is not None:
        raise argparse.ArgumentTypeError(
            f"{image} is pinned by digest, which kind cannot address; give the tag it carries"
        )
    return image


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner = _run,
    capture: Capturer = _capture,
    sleep: Sleeper = time.sleep,
    resolve: Callable[[ImageReference], str] = ecr_public_digest,
) -> int:
    parser = argparse.ArgumentParser(
        description="Pull the images rendered Helm manifests run and load them into kind."
    )
    parser.add_argument("--cluster", required=True, help="kind cluster name")
    parser.add_argument(
        "--pinned",
        action="append",
        default=[],
        type=_pinned_argument,
        metavar="IMAGE=LOCAL",
        help=(
            "also stage the digest-pinned IMAGE under the local tag LOCAL, for a pod "
            "that runs it with imagePullPolicy: Never (repeatable)"
        ),
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        type=_image_argument,
        metavar="NAME",
        help=(
            "also load NAME, an image a previous step built or pulled into the runner's "
            "Docker, without pulling it (repeatable)"
        ),
    )
    parser.add_argument("manifests", nargs="*", type=Path, help="rendered manifest files")
    args = parser.parse_args(argv)
    if not args.manifests and not args.pinned and not args.image:
        parser.error("give rendered manifests, --pinned IMAGE=LOCAL, --image NAME, or a mix")

    images = collect_images(path.read_text(encoding="utf-8") for path in args.manifests)
    if args.manifests and not images:
        print("::error::the rendered manifests run no container images", flush=True)
        return 1
    try:
        staged = [
            image for image in images if stage_image(image, run=run, sleep=sleep, resolve=resolve)
        ]
        for image, local in args.pinned:
            stage_pinned_image(image, local, run=run, sleep=sleep)
        load_into_kind(
            [*staged, *(local for _, local in args.pinned), *args.image],
            args.cluster,
            run=run,
            capture=capture,
        )
    except PreloadError as exc:
        print(f"::error::{exc}", flush=True)
        return 1
    if args.manifests:
        print(f"preloaded {len(staged)} of {len(images)} image(s) into kind cluster {args.cluster}")
    if args.pinned:
        print(
            f"loaded {len(args.pinned)} pinned image(s) under local tags into kind cluster "
            f"{args.cluster}"
        )
    if args.image:
        print(f"loaded {len(args.image)} local image(s) into kind cluster {args.cluster}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
