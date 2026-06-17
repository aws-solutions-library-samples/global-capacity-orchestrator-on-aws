"""
Mirror third-party container images into the project's own ECR — shared core.

Some upstream registries (chiefly Docker Hub, ``docker.io``) rate-limit
anonymous pulls and have **no credential-free** ECR pull-through cache. On a
cold cluster that can stall image pulls — most visibly Volcano, whose images
(``volcanosh/vc-*``) live only on Docker Hub and gate its Helm install. The
credential-free fix is to **mirror** those images into a ``gco/*`` ECR namespace
and point the consumer (a Helm values override, a manifest, …) at the mirror, so
the cluster pulls from same-account ECR with the pull-only node role it already
has. See ``docs/CUSTOMIZATION.md``.

This module is the reusable, **general** mirror core — it copies an arbitrary
list of images, not just Volcano's. It is shared by two callers:

- ``gco images mirror`` (in ``cli/commands/images_cmd.py``) — the operator CLI.
- ``cli/stacks.py`` (``StackManager.deploy``) — the auto-mirror that runs before
  a regional stack's Helm install, so a fresh ``gco stacks deploy`` with
  ``volcano_image_mirror.enabled`` just works (no separate manual step).

═══════════════════════════════════════════════════════════════════════════
HOW TO ADD AN IMAGE TO THE MIRROR
═══════════════════════════════════════════════════════════════════════════
The set of images to mirror is produced by :func:`collect_source_refs`, which
returns a flat list of fully-qualified upstream refs
(``"<registry>/<repo>:<tag>"``). To add one, extend that function — two flavors:

1. **Static ref** — append a literal, e.g.::

       refs.append("docker.io/bitnami/redis:7.4.1")

2. **Chart-derived ref** — derive the name/tag from
   ``lambda/helm-installer/charts.yaml`` so the mirror never drifts from the
   deployed Helm chart version. Use :func:`_volcano_source_refs` as the template
   (read the chart's ``values`` block, build ``docker.io/<image>:<tag>``).

Then wire up the **consumer** so the cluster actually pulls the mirrored copy
instead of the upstream — typically a Helm ``image_registry``/``image`` override
in ``gco/stacks/regional_stack.py`` (see ``_helm_chart_value_overrides`` /
``_configure_volcano_image_mirror`` for the Volcano example) or a manifest image
reference. The mirror copies ``<registry>/<repo>:<tag>`` to
``<account>.dkr.ecr.<region>.amazonaws.com/<ecr_namespace>/<repo>:<tag>``, so the
consumer must point at ``<…>/<ecr_namespace>/<repo>``.

WHY mirror rather than pull-through cache: ECR pull-through cache for Docker Hub
*requires* stored credentials (anonymous is unsupported), and on EKS Auto Mode
the pull-only, service-managed node role complicates cache-miss imports.
Mirroring needs no credential — the images become plain ``gco/*`` ECR repos.
═══════════════════════════════════════════════════════════════════════════

The copy preserves the **full manifest list** (every architecture) so an arm64
(Graviton) node and an amd64 node both find a matching image; a naive
``pull``/``tag``/``push`` (which drops all but the host architecture) is never
used. The concrete mechanism is chosen at runtime — Docker Buildx
(``buildx imagetools create``), Finch/nerdctl (``--all-platforms``), or skopeo
(``copy --all``) — see :func:`resolve_copy_strategy`.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess  # nosec B404 - invokes container CLI / skopeo with fixed, non-shell argv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import yaml

# Repo root is the parent of cli/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHARTS_YAML = _REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"
_CDK_JSON = _REPO_ROOT / "cdk.json"

_DEFAULT_NAMESPACE = "gco/dockerhub"

# The Volcano components enabled by default (controller, scheduler, admission),
# keyed by the ``basic.<key>`` image-name field in charts.yaml. The
# agent/agent-scheduler images are not pulled in the default configuration and
# are intentionally excluded.
_VOLCANO_IMAGE_NAME_KEYS = (
    "controller_image_name",
    "scheduler_image_name",
    "admission_image_name",
)
# Upstream registry Volcano's chart pulls from by default (chart value
# ``basic.image_registry``).
_VOLCANO_UPSTREAM_REGISTRY = "docker.io"

# Default logger — callers may pass their own ``log`` callable (e.g. to route
# through a deploy progress stream).
LogFn = Callable[[str], None]


@dataclass(frozen=True)
class MirrorItem:
    """One image to copy: ``source_ref`` -> ``dest_ref`` (repo ``dest_repo``)."""

    source_ref: str
    dest_repo: str
    dest_ref: str

    @property
    def tag(self) -> str:
        """The image tag (the segment after the final ``:`` of ``dest_ref``)."""
        return self.dest_ref.rsplit(":", 1)[1]


def parse_source_ref(ref: str) -> tuple[str, str]:
    """Split ``"<registry>/<repo>:<tag>"`` into ``(repo_path, tag)``.

    The registry host (first slash-delimited segment) is dropped so the image
    can be re-homed under the ECR namespace while preserving its repo path, e.g.
    ``"docker.io/volcanosh/vc-scheduler:v1.15.0"`` -> ``("volcanosh/vc-scheduler",
    "v1.15.0")``.
    """
    if "/" not in ref:
        raise ValueError(f"source ref must include a registry host: {ref!r}")
    _registry, rest = ref.split("/", 1)
    if ":" not in rest:
        raise ValueError(f"source ref must include a tag: {ref!r}")
    repo_path, tag = rest.rsplit(":", 1)
    if not repo_path or not tag:
        raise ValueError(f"could not parse source ref {ref!r}")
    return repo_path, tag


def load_charts_config(charts_path: Path = _CHARTS_YAML) -> dict[str, Any]:
    """Load and return the parsed ``charts.yaml`` mapping."""
    with open(charts_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{charts_path} did not parse to a mapping")
    return data


def _volcano_source_refs(charts_config: dict[str, Any]) -> list[str]:
    """Return Volcano's upstream image refs derived from ``charts.yaml``.

    Reads ``charts.charts.volcano.values.basic`` — the per-component
    ``*_image_name`` fields and the shared ``image_tag_version`` (each
    component's own ``*_image_tag_version`` takes precedence when set, matching
    the chart's templating) — and returns ``["docker.io/<image>:<tag>", ...]``.
    Deriving from the chart keeps the mirror tag identical to what Helm requests.
    """
    volcano = (charts_config.get("charts", {}) or {}).get("volcano", {}) or {}
    basic = (volcano.get("values", {}) or {}).get("basic", {}) or {}
    shared_tag = str(basic.get("image_tag_version") or "").strip()
    if not shared_tag:
        raise ValueError(
            "volcano.values.basic.image_tag_version is missing from charts.yaml; "
            "cannot determine which Volcano image tag to mirror."
        )

    refs: list[str] = []
    for name_key in _VOLCANO_IMAGE_NAME_KEYS:
        image_name = str(basic.get(name_key) or "").strip()
        if not image_name:
            continue
        component = name_key.removesuffix("_image_name")
        per_component_tag = str(basic.get(f"{component}_image_tag_version") or "").strip()
        tag = per_component_tag or shared_tag
        refs.append(f"{_VOLCANO_UPSTREAM_REGISTRY}/{image_name}:{tag}")
    if not refs:
        raise ValueError(
            "No Volcano component image names found under volcano.values.basic in charts.yaml."
        )
    return refs


def collect_source_refs(charts_config: dict[str, Any] | None = None) -> list[str]:
    """Return every upstream image ref to mirror (``"<registry>/<repo>:<tag>"``).

    This is the single extension point for the mirror. To add an image, append
    to ``refs`` below — either a static literal or a chart-derived ref (see the
    module docstring, "HOW TO ADD AN IMAGE TO THE MIRROR"). Remember to also
    point the image's *consumer* at the mirrored copy.
    """
    charts_config = charts_config if charts_config is not None else load_charts_config()
    refs: list[str] = []

    # Volcano (docker.io/volcanosh/vc-*) — the reason this mirror exists.
    refs.extend(_volcano_source_refs(charts_config))

    # ── ADD MORE IMAGES HERE ──────────────────────────────────────────────
    # e.g. refs.append("docker.io/bitnami/redis:7.4.1")          # static, or
    #      refs.extend(_my_chart_source_refs(charts_config))     # chart-derived
    # Then wire the consumer to <ecr_namespace>/<repo> (see module docstring).

    return refs


def plan_from_sources(
    source_refs: list[str], registry_host: str, ecr_namespace: str
) -> list[MirrorItem]:
    """Compute the copy plan: one :class:`MirrorItem` per source ref.

    ``registry_host`` is ``<account>.dkr.ecr.<region>.amazonaws.com`` and
    ``ecr_namespace`` is the destination prefix (e.g. ``gco/dockerhub``). The
    destination preserves the upstream repo path so it lines up with whatever
    ``image_registry``/``image`` override the consumer points at
    (``<registry_host>/<ecr_namespace>`` + ``/<repo_path>``).
    """
    ecr_namespace = ecr_namespace.strip("/")
    items: list[MirrorItem] = []
    for ref in source_refs:
        repo_path, tag = parse_source_ref(ref)
        dest_repo = f"{ecr_namespace}/{repo_path}"
        dest_ref = f"{registry_host}/{dest_repo}:{tag}"
        items.append(MirrorItem(source_ref=ref, dest_repo=dest_repo, dest_ref=dest_ref))
    return items


def read_mirror_config(cdk_json_path: Path = _CDK_JSON) -> dict[str, Any]:
    """Return ``{enabled, ecr_namespace}`` from cdk.json ``volcano_image_mirror``.

    Defaults to disabled / ``gco/dockerhub`` when the block or file is absent.
    Used by the deploy path to decide whether to auto-mirror. (The cdk.json key
    is still named ``volcano_image_mirror`` — Volcano is the only consumer today
    — but the mirror itself is general; see :func:`collect_source_refs`.)
    """
    try:
        with open(cdk_json_path, encoding="utf-8") as f:
            ctx = json.load(f).get("context", {}) or {}
    except OSError, json.JSONDecodeError:
        return {"enabled": False, "ecr_namespace": _DEFAULT_NAMESPACE}
    block = ctx.get("volcano_image_mirror") or {}
    namespace = str(block.get("ecr_namespace", _DEFAULT_NAMESPACE)).strip("/") or _DEFAULT_NAMESPACE
    return {"enabled": bool(block.get("enabled", False)), "ecr_namespace": namespace}


def cdk_default_namespace(cdk_json_path: Path = _CDK_JSON) -> str:
    """Return ``volcano_image_mirror.ecr_namespace`` from cdk.json (default gco/dockerhub)."""
    return str(read_mirror_config(cdk_json_path)["ecr_namespace"])


def _account_id() -> str:
    return str(boto3.client("sts").get_caller_identity()["Account"])


def _registry_host(account_id: str, region: str) -> str:
    return f"{account_id}.dkr.ecr.{region}.amazonaws.com"


def detect_runtime() -> str:
    """Return the container CLI to drive (``docker``, ``finch``, or ``podman``).

    Mirrors the project's runtime preference (docker > finch > podman). Note
    that on a Finch-based setup the ``docker`` shim may itself be Finch — the
    copy strategy is resolved separately by probing for capabilities rather
    than trusting the command name.
    """
    for cmd in ("docker", "finch", "podman"):
        if shutil.which(cmd):
            return cmd
    raise RuntimeError(
        "No container CLI found (looked for docker, finch, podman). Install one, "
        "or install skopeo for a daemon-less copy."
    )


def _runtime_has_buildx(runtime: str) -> bool:
    """True if ``<runtime> buildx version`` succeeds (Docker Buildx present)."""
    try:
        return (
            subprocess.run(  # nosec B603 - fixed argv, no shell
                [runtime, "buildx", "version"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except OSError, subprocess.SubprocessError:
        return False


def _runtime_supports_all_platforms(runtime: str) -> bool:
    """True if ``<runtime> pull`` advertises ``--all-platforms`` (Finch/nerdctl)."""
    try:
        out = subprocess.run(  # nosec B603 - fixed argv, no shell
            [runtime, "pull", "--help"], capture_output=True, text=True, timeout=15
        )
    except OSError, subprocess.SubprocessError:
        return False
    return "--all-platforms" in (out.stdout + out.stderr)


def resolve_copy_strategy(runtime: str) -> str:
    """Pick a multi-arch-preserving copy strategy from what's available.

    Priority:
      1. ``buildx`` — ``<runtime> buildx imagetools create`` (registry-to-registry,
         no local pull). Best when Docker Buildx is present.
      2. ``all-platforms`` — ``<runtime> pull/tag/push --all-platforms``
         (Finch / nerdctl), preserves the manifest list via containerd.
      3. ``skopeo`` — ``skopeo copy --all`` (daemon-less), if skopeo is on PATH.

    Every strategy preserves all architectures; a plain ``pull``/``tag``/``push``
    (which would drop every arch except the host's) is never used. Raises with
    guidance if none is available.
    """
    if _runtime_has_buildx(runtime):
        return "buildx"
    if _runtime_supports_all_platforms(runtime):
        return "all-platforms"
    if shutil.which("skopeo"):
        return "skopeo"
    raise RuntimeError(
        f"No multi-arch image-copy method available. Need one of: "
        f"'{runtime} buildx' (Docker Buildx), '{runtime} pull --all-platforms' "
        f"(Finch/nerdctl), or skopeo on PATH."
    )


def ensure_repository(ecr_client: Any, repo_name: str, log: LogFn = print) -> None:
    """Create the ECR repository if it does not already exist (idempotent)."""
    try:
        ecr_client.create_repository(repositoryName=repo_name)
        log(f"  created ECR repository {repo_name}")
    except ecr_client.exceptions.RepositoryAlreadyExistsException:
        log(f"  ECR repository {repo_name} already exists")


def tag_exists(ecr_client: Any, repo_name: str, tag: str) -> bool:
    """True if ``tag`` already exists in the ECR repo (drives skip-if-mirrored).

    Returns False when the repository or tag does not exist, so the caller
    mirrors it. Any other error propagates.
    """
    try:
        resp = ecr_client.describe_images(repositoryName=repo_name, imageIds=[{"imageTag": tag}])
        return bool(resp.get("imageDetails"))
    except ecr_client.exceptions.ImageNotFoundException:
        return False
    except ecr_client.exceptions.RepositoryNotFoundException:
        return False


def ecr_auth(region: str) -> tuple[str, str]:
    """Return ``(username, password)`` for the region's ECR registry."""
    ecr = boto3.client("ecr", region_name=region)
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    username, password = base64.b64decode(token).decode().split(":", 1)
    return username, password


def runtime_login(
    runtime: str, registry_host: str, username: str, password: str, log: LogFn = print
) -> None:
    """Authenticate the container runtime against the ECR registry."""
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [runtime, "login", "--username", username, "--password-stdin", registry_host],
        input=password.encode(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{runtime} login to {registry_host} failed: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    log(f"  authenticated {runtime} to {registry_host}")


def _copy_commands(item: MirrorItem, runtime: str, strategy: str, password: str) -> list[list[str]]:
    """Build the argv list(s) for one image copy under the chosen strategy.

    Factored out (pure) so the command shape is unit-testable without invoking
    any runtime. All strategies preserve the full multi-arch manifest list.
    """
    if strategy == "buildx":
        return [
            [runtime, "buildx", "imagetools", "create", "--tag", item.dest_ref, item.source_ref]
        ]
    if strategy == "all-platforms":
        return [
            [runtime, "pull", "--all-platforms", item.source_ref],
            [runtime, "tag", item.source_ref, item.dest_ref],
            [runtime, "push", "--all-platforms", item.dest_ref],
        ]
    if strategy == "skopeo":
        return [
            [
                "skopeo",
                "copy",
                "--all",
                "--dest-creds",
                f"AWS:{password}",
                f"docker://{item.source_ref}",
                f"docker://{item.dest_ref}",
            ]
        ]
    raise ValueError(f"unknown copy strategy: {strategy!r}")


def copy_image(
    item: MirrorItem,
    runtime: str = "docker",
    strategy: str = "buildx",
    password: str = "",
    log: LogFn = print,
) -> None:
    """Copy one image registry-to-registry, preserving the full manifest list.

    Dispatches to the resolved ``strategy`` (``buildx`` / ``all-platforms`` /
    ``skopeo``) — see :func:`resolve_copy_strategy`. Every strategy carries all
    architectures so both amd64 and arm64 (Graviton) nodes find a match.
    """
    log(f"  copying {item.source_ref} -> {item.dest_ref}  [{strategy}]")
    for cmd in _copy_commands(item, runtime, strategy, password):
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"image copy failed for {item.source_ref} "
                f"(strategy {strategy}, step {cmd[:3]}): "
                f"{(result.stderr or result.stdout).strip()}"
            )


def plan_mirror(
    region: str,
    ecr_namespace: str | None = None,
    source_refs: list[str] | None = None,
    charts_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve the mirror plan as plain data — no ECR writes, no image copies.

    Read-only counterpart to :func:`mirror_images`: it resolves the account and
    destination registry (a single STS ``GetCallerIdentity`` call) and computes
    where each upstream image *would* be mirrored, but creates no repositories
    and copies nothing. Backs the ``images_mirror_plan`` MCP tool and is reused
    by :func:`mirror_status`.

    ``source_refs`` defaults to :func:`collect_source_refs`; ``ecr_namespace``
    defaults to :func:`cdk_default_namespace`. Returns ``{region, registry,
    ecr_namespace, images: [{source_ref, dest_repo, dest_ref, tag}, ...]}`` where
    ``registry`` is ``<account>.dkr.ecr.<region>.amazonaws.com/<ecr_namespace>``.
    """
    ecr_namespace = (ecr_namespace or cdk_default_namespace()).strip("/")
    if source_refs is None:
        source_refs = collect_source_refs(load_charts_config(charts_path) if charts_path else None)
    registry_host = _registry_host(_account_id(), region)
    plan = plan_from_sources(source_refs, registry_host, ecr_namespace)
    return {
        "region": region,
        "registry": f"{registry_host}/{ecr_namespace}",
        "ecr_namespace": ecr_namespace,
        "images": [
            {
                "source_ref": item.source_ref,
                "dest_repo": item.dest_repo,
                "dest_ref": item.dest_ref,
                "tag": item.tag,
            }
            for item in plan
        ],
    }


def mirror_status(
    region: str,
    ecr_namespace: str | None = None,
    source_refs: list[str] | None = None,
    charts_path: Path | None = None,
) -> dict[str, Any]:
    """Report, per planned image, whether it is already mirrored in ECR.

    Read-only: builds the plan via :func:`plan_mirror`, then probes each
    destination tag with :func:`tag_exists` (ECR ``DescribeImages``; no writes).
    Returns the plan augmented with a ``mirrored`` bool per image plus top-level
    ``all_mirrored`` and ``missing`` (the destination refs not yet present), so
    an operator can tell at a glance whether a deploy's auto-mirror still has
    anything to copy before the consuming Helm install runs.
    """
    plan = plan_mirror(region, ecr_namespace, source_refs, charts_path)
    ecr_client = boto3.client("ecr", region_name=region)
    images: list[dict[str, Any]] = []
    missing: list[str] = []
    for img in plan["images"]:
        present = tag_exists(ecr_client, img["dest_repo"], img["tag"])
        images.append({**img, "mirrored": present})
        if not present:
            missing.append(img["dest_ref"])
    return {
        "region": plan["region"],
        "registry": plan["registry"],
        "ecr_namespace": plan["ecr_namespace"],
        "images": images,
        "all_mirrored": not missing,
        "missing": missing,
    }


def mirror_images(
    region: str,
    ecr_namespace: str | None = None,
    source_refs: list[str] | None = None,
    charts_path: Path | None = None,
    skip_existing: bool = True,
    log: LogFn = print,
) -> dict[str, Any]:
    """Mirror the configured upstream images into ECR for ``region`` (full flow).

    ``source_refs`` defaults to :func:`collect_source_refs` (every registered
    image). Resolves the account/registry, builds the plan, picks a multi-arch
    copy strategy, authenticates, then for each image ensures the repo exists and
    copies it — skipping any tag already present when ``skip_existing`` is True
    (so repeat deploys are a fast no-op). Returns a summary dict with the
    destination ``registry``, the ``strategy`` used, and the ``mirrored`` /
    ``skipped`` destination refs.
    """
    ecr_namespace = (ecr_namespace or cdk_default_namespace()).strip("/")
    if source_refs is None:
        source_refs = collect_source_refs(load_charts_config(charts_path) if charts_path else None)

    account_id = _account_id()
    registry_host = _registry_host(account_id, region)
    plan = plan_from_sources(source_refs, registry_host, ecr_namespace)

    runtime = detect_runtime()
    strategy = resolve_copy_strategy(runtime)

    log(
        f"Mirroring {len(plan)} image(s) into "
        f"{registry_host}/{ecr_namespace} (region {region}) "
        f"via {strategy} (runtime: {runtime}):"
    )

    # Auth: every strategy needs ECR push credentials. buildx / all-platforms
    # use the runtime's credential store (so we `<runtime> login`); skopeo takes
    # the password inline via --dest-creds.
    username, password = ecr_auth(region)
    if strategy != "skopeo":
        runtime_login(runtime, registry_host, username, password, log=log)

    ecr_client = boto3.client("ecr", region_name=region)
    mirrored: list[str] = []
    skipped: list[str] = []
    for item in plan:
        ensure_repository(ecr_client, item.dest_repo, log=log)
        if skip_existing and tag_exists(ecr_client, item.dest_repo, item.tag):
            log(f"  skip (already mirrored): {item.dest_ref}")
            skipped.append(item.dest_ref)
            continue
        copy_image(item, runtime=runtime, strategy=strategy, password=password, log=log)
        mirrored.append(item.dest_ref)

    return {
        "registry": f"{registry_host}/{ecr_namespace}",
        "strategy": strategy,
        "mirrored": mirrored,
        "skipped": skipped,
    }
