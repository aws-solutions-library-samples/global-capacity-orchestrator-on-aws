"""
Container image registry management for GCO CLI.

Provides ``ImageManager`` for building, pushing, and managing user
container images stored in per-project ECR repositories under the
``gco/`` prefix. Builds run through the same container runtime
(Docker, Finch, or Podman) used by CDK asset bundling, detected via
``cli._container_runtime``.

The ECR repository layout mirrors the project naming convention:
``<account>.dkr.ecr.<region>.<url-suffix>/gco/<name>:<tag>``.

Read-only methods (``list_repos``, ``list_tags``, ``describe``,
``get_uri``, ``replication_get``, ``replication_status``) hit ECR
directly via boto3 and do not invoke any container runtime.

Administrative methods (``init``, ``lifecycle_get``, ``lifecycle_set``,
``replication_sync``) configure the repository surface and are
idempotent — re-running them is safe.

Destructive methods (``delete_tag``, ``delete_repo``, ``cleanup``,
``prune``, ``orphans``) require explicit caller intent and never run
implicitly.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from gco.service_images import IMAGE_NAME_RE, discover_service_dockerfiles

from ._container_runtime import detect_container_runtime
from ._image_uri import (
    aws_partition,
    ecr_registry_host,
)
from ._image_uri import (
    rewrite_image_uri_for_region as _rewrite_image_uri_for_region,
)
from .config import GCOConfig, _load_cdk_json, get_config

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-19T12:23:38Z
# Generated from Git commit: e192bb01bb7807a52e11e262c0cb44173133f894
# Flowchart(s) generated from this file:
#   * ``ImageManager.build`` -> ``diagrams/code_diagrams/cli/images.ImageManager_build.html``
#     (PNG: ``diagrams/code_diagrams/cli/images.ImageManager_build.png``)
#   * ``ImageManager.push`` -> ``diagrams/code_diagrams/cli/images.ImageManager_push.html``
#     (PNG: ``diagrams/code_diagrams/cli/images.ImageManager_push.png``)
#   * ``ImageManager.cleanup`` -> ``diagrams/code_diagrams/cli/images.ImageManager_cleanup.html``
#     (PNG: ``diagrams/code_diagrams/cli/images.ImageManager_cleanup.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger(__name__)

# Image name and tag validation regexes.
#
# Names become ``<project>/<name>`` ECR repository suffixes, so they follow the
# rule gco/service_images.py applies to the service images: a lowercase letter,
# then lowercase letters and digits joined by single dashes, at most 63
# characters. ECR rejects a path segment that ends in a dash or holds two in a
# row.
_NAME_RE = IMAGE_NAME_RE

# Tags: docker reference grammar. First character must be alnum or
# underscore; subsequent characters allow dot, dash, underscore.
# 128 chars max.
_TAG_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.\-]{0,127}$")

# Project repository prefix. Every repo this manager creates lives under
# ``<project_name>/`` (default ``gco/``) so a single replication / lifecycle /
# removal-policy rule can target the whole deployment, and two deployments in
# one account+region get isolated ECR namespaces (#139). Resolved per-instance
# from ``config.project_name`` into ``self._repo_prefix`` in ``__init__``.

# First-party images that GCO builds and ships itself, as opposed to the user
# images pushed through ``build``/``push``. Each entry pairs the logical image
# name (which becomes the ``gco/<name>`` ECR repository suffix) with the
# Dockerfile that produces it, and the mapping is *discovered* from
# ``dockerfiles/Dockerfile.<name>`` rather than typed out: the previous
# hand-kept list had already drifted (it was missing cost-monitor), so the
# filenames are the single source of truth shared with the regional stack and
# the CI build/scan matrix. See ``gco.service_images``.
_MAINTAINED_IMAGES: dict[str, str] = discover_service_dockerfiles()

# Default image served by disaggregated prefill/decode deployments when the
# operator does not supply one. As of this tag the upstream vLLM OpenAI server
# image bundles the Mooncake transfer engine as a first-class KV-connector
# dependency, so GCO no longer builds or maintains its own image — deploys pull
# this upstream image directly from Docker Hub. Pinned to an explicit version
# for reproducibility; bump intentionally when validating a new vLLM release
# and never use a mutable/rolling tag such as ``latest``.
_DISAGGREGATED_DEFAULT_IMAGE = "vllm/vllm-openai:v0.32.0"

# Default lifecycle policy parameters.
_DEFAULT_KEEP_TAGGED = 20
_DEFAULT_EXPIRE_UNTAGGED_DAYS = 7

# Digest extraction from ``docker push`` stdout/stderr. The runtime
# emits a line of the form ``... digest: sha256:... size: ...``.
_DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")


class ImageManager:
    """Manages user container images in ECR.

    Construction is cheap — no AWS calls happen until a method is
    invoked. The account ID and target region are resolved lazily.
    """

    def __init__(self, config: GCOConfig | None = None, region: str | None = None):
        self.config = config or get_config()
        # ECR repo namespace for this deployment (#139): repos live under
        # ``<project_name>/`` so two deployments in one account+region don't
        # share an ECR namespace. Defaults to ``gco`` — byte-identical to the
        # pre-#139 hardcoded prefix for the stock deployment.
        self._repo_prefix = self.config.project_name
        self.region = self._resolve_region(region)
        self._account_id_cache: str | None = None

    # ------------------------------------------------------------------
    # Region / account helpers
    # ------------------------------------------------------------------
    def _resolve_region(self, region: str | None) -> str:
        """Pick a region for ECR API calls.

        Priority: explicit argument, ``AWS_DEFAULT_REGION``, then the global
        region where the shared ECR registry is deployed. ``GCOConfig`` has no
        ``regions`` attribute; deployment-region discovery is handled
        separately by :meth:`_replication_regions`.
        """
        if region:
            return region
        env_region = os.environ.get("AWS_DEFAULT_REGION")
        if env_region:
            return env_region
        return str(self.config.global_region)

    def _account_id(self) -> str:
        """Return the AWS account ID via STS GetCallerIdentity (cached)."""
        if self._account_id_cache is None:
            sts = boto3.client("sts")
            self._account_id_cache = sts.get_caller_identity()["Account"]
        return self._account_id_cache

    def _registry_host(self) -> str:
        """Return the partition-correct ECR registry host for this region."""
        return ecr_registry_host(self._account_id(), self.region)

    def _repo_arn(self, name: str) -> str:
        """Return the full ARN of the repository under the project prefix."""
        return (
            f"arn:{aws_partition(self.region)}:ecr:{self.region}:{self._account_id()}:"
            f"repository/{self._repo_prefix}/{name}"
        )

    def _ecr_client(self) -> Any:
        """Return a boto3 ECR client targeting the manager's region."""
        return boto3.client("ecr", region_name=self.region)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def _validate_context(self, context: str) -> Path:
        """Validate the build context path.

        The path must exist on disk and resolve to a directory. Raw
        ``..`` segments in the supplied string are rejected outright
        so the caller can't trick the manager into reaching outside an
        intended workspace; the resolved path is then returned for use
        as ``cwd`` of the build.
        """
        # Reject string-level traversal segments BEFORE resolving the
        # path so callers receive a clear error rather than a silent
        # rewrite up the tree.
        parts = Path(context).parts
        if ".." in parts:
            raise ValueError(f"Invalid build context: path traversal not allowed: {context}")
        resolved = Path(context).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Build context not found: {context}")
        if not resolved.is_dir():
            raise ValueError(f"Build context is not a directory: {context}")
        return resolved

    def _validate_name(self, name: str) -> str:
        """Validate an image name against ``_NAME_RE``."""
        if not _NAME_RE.fullmatch(name):
            raise ValueError(
                f"Invalid image name: {name!r}. Expected lowercase letters and digits "
                "joined by single dashes; must start with a letter and end with a "
                "letter or digit; max 63 chars."
            )
        return name

    def _validate_tag(self, tag: str) -> str:
        """Validate an image tag against ``_TAG_RE``."""
        if not _TAG_RE.match(tag):
            raise ValueError(
                f"Invalid image tag: {tag!r}. Expected alphanumerics, dots, "
                "dashes, and underscores; max 128 chars."
            )
        return tag

    # ------------------------------------------------------------------
    # Default-value helpers
    # ------------------------------------------------------------------
    def _git_short_sha(self) -> str | None:
        """Return the current short git SHA, or ``None`` when unavailable."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            logger.debug("git rev-parse failed: %s", e)
        return None

    def _default_tag(self) -> str:
        """Return ``_git_short_sha()`` when available, else ``"latest"``."""
        sha = self._git_short_sha()
        return sha if sha else "latest"

    def _default_lifecycle_policy(self) -> dict[str, Any]:
        """Return the default ECR lifecycle policy as a dict.

        The policy keeps the most recent ``_DEFAULT_KEEP_TAGGED`` tagged
        images and expires untagged images after
        ``_DEFAULT_EXPIRE_UNTAGGED_DAYS`` days. The structure matches
        the JSON shape that ``ecr.put_lifecycle_policy`` accepts after
        being JSON-stringified at the call site.
        """
        return {
            "rules": [
                {
                    "rulePriority": 1,
                    "description": (f"Keep last {_DEFAULT_KEEP_TAGGED} tagged images"),
                    "selection": {
                        "tagStatus": "tagged",
                        "countType": "imageCountMoreThan",
                        "countNumber": _DEFAULT_KEEP_TAGGED,
                        "tagPatternList": ["*"],
                    },
                    "action": {"type": "expire"},
                },
                {
                    "rulePriority": 2,
                    "description": (f"Expire untagged after {_DEFAULT_EXPIRE_UNTAGGED_DAYS} days"),
                    "selection": {
                        "tagStatus": "untagged",
                        "countType": "sinceImagePushed",
                        "countUnit": "days",
                        "countNumber": _DEFAULT_EXPIRE_UNTAGGED_DAYS,
                    },
                    "action": {"type": "expire"},
                },
            ],
        }

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------
    def _extract_digest(self, push_output: str) -> str | None:
        """Pull the first ``sha256:...`` digest out of push stdout/stderr."""
        match = _DIGEST_RE.search(push_output)
        return match.group(0) if match else None

    # ------------------------------------------------------------------
    # ECR repository helpers (used by build/push)
    # ------------------------------------------------------------------
    def _runtime_or_error(self) -> str:
        """Return the detected container runtime, or raise a friendly error."""
        runtime = detect_container_runtime()
        if not runtime:
            from ._container_runtime import container_runtime_error_message

            raise RuntimeError(container_runtime_error_message(allow_cdk_docker=True))
        return runtime

    def _ecr_login(self, runtime: str) -> None:
        """Authenticate the runtime against the ECR registry."""
        ecr = self._ecr_client()
        token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
        username, password = base64.b64decode(token).decode().split(":", 1)
        registry = self._registry_host()
        result = subprocess.run(
            [runtime, "login", "-u", username, "--password-stdin", registry],
            input=password.encode(),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{runtime} login to {registry} failed: "
                f"{result.stderr.decode(errors='replace').strip()}"
            )

    def _check_tag_immutable_collision(self, name: str, tag: str) -> None:
        """Block re-pushing a tag when the repo is immutable.

        ECR repos can be configured with ``imageTagMutability=IMMUTABLE``,
        in which case attempting to overwrite an existing tag silently
        succeeds at build time but fails at push time with a confusing
        error. Catch this earlier and surface a helpful message.
        """
        ecr = self._ecr_client()
        repo_name = f"{self._repo_prefix}/{name}"
        try:
            repo_resp = ecr.describe_repositories(repositoryNames=[repo_name])
        except ecr.exceptions.RepositoryNotFoundException:
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "RepositoryNotFoundException":
                return
            raise

        repos = repo_resp.get("repositories", [])
        if not repos:
            return
        mutability = repos[0].get("imageTagMutability", "MUTABLE")
        if mutability != "IMMUTABLE":
            return

        try:
            existing = ecr.describe_images(
                repositoryName=repo_name,
                imageIds=[{"imageTag": tag}],
            )
        except ecr.exceptions.ImageNotFoundException:
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ImageNotFoundException":
                return
            raise

        if existing.get("imageDetails"):
            raise RuntimeError(
                f"Tag {tag!r} already exists on immutable repo "
                f"{repo_name!r}. Re-run with a different tag, e.g. "
                f"--tag <new_tag>."
            )

    def _apply_retain_tag(self, name: str) -> None:
        """Apply the ``gco:retain=true`` resource tag to the repository.

        A marker only. No GCO stack creates or deletes ``<project>/*``
        repositories, so every one survives ``gco stacks destroy`` with or
        without it, and :meth:`delete_repo` deletes a tagged one too. The one
        reader is the image-lookup custom resource handler
        (lambda/image-lookup), which keeps a tagged repository on delete; the
        global stack exports it, but no GCO stack uses it.
        """
        ecr = self._ecr_client()
        ecr.tag_resource(
            resourceArn=self._repo_arn(name),
            tags=[{"Key": "gco:retain", "Value": "true"}],
        )

    # ------------------------------------------------------------------
    # build / push
    # ------------------------------------------------------------------
    def build(
        self,
        context: str,
        name: str,
        tag: str | None = None,
        dockerfile: str = "Dockerfile",
        build_args: dict[str, str] | None = None,
        platform: str = "linux/amd64",
        retain: bool = False,
        quiet: bool = False,
    ) -> dict[str, Any]:
        """Build a container image and push it to the project's ECR repo.

        Args:
            context: Build context directory.
            name: Image name (validated; lowercase letters, digits, dashes).
            tag: Image tag (defaults to git short SHA, else ``latest``).
            dockerfile: Path to the Dockerfile, relative to ``context``.
            build_args: Optional ``KEY=value`` build args.
            platform: ``--platform`` argument for the build (default
                ``linux/amd64``).
            retain: When True, tag the repository ``gco:retain=true`` (a
                marker; see :meth:`_apply_retain_tag`).
            quiet: Capture container build output instead of writing it to the
                command's output stream. Used for machine-readable CLI output.

        Returns:
            ``{"image_uri", "digest", "size_bytes", ...}``.
        """
        ctx = self._validate_context(context)
        validated_name = self._validate_name(name)
        validated_tag = self._validate_tag(tag if tag is not None else self._default_tag())

        df_path = (ctx / dockerfile).resolve()
        if not df_path.exists() or not df_path.is_file():
            raise FileNotFoundError(f"Dockerfile not found: {df_path} (relative to {ctx})")
        # Use path components: /tmp/app-evil has the string prefix /tmp/app.
        if not df_path.is_relative_to(ctx):
            raise ValueError(f"Dockerfile must live inside the build context: {df_path}")

        runtime = self._runtime_or_error()
        self.init(name, retain=retain)
        self._check_tag_immutable_collision(validated_name, validated_tag)
        self._ecr_login(runtime)

        full_uri = f"{self._registry_host()}/{self._repo_prefix}/{validated_name}:{validated_tag}"

        build_cmd: list[str] = [
            runtime,
            "build",
            "-t",
            full_uri,
            "--platform",
            platform,
            "-f",
            str(df_path),
        ]
        for key, value in (build_args or {}).items():
            build_cmd.extend(["--build-arg", f"{key}={value}"])
        build_cmd.append(str(ctx))

        logger.info("Building image: %s", " ".join(build_cmd))
        build_run_kwargs: dict[str, Any] = {"cwd": str(ctx), "stdin": subprocess.DEVNULL}
        if quiet:
            build_run_kwargs.update(capture_output=True, text=True)
        # check=True: a failed build must not fall through to the push below.
        subprocess.run(build_cmd, check=True, **build_run_kwargs)

        push_result = subprocess.run(
            [runtime, "push", full_uri],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            cwd=str(ctx),
        )
        digest = self._extract_digest((push_result.stdout or "") + (push_result.stderr or ""))

        if retain:
            self._apply_retain_tag(validated_name)

        size_bytes = self._image_size_bytes(validated_name, validated_tag)

        return {
            "image_uri": full_uri,
            "digest": digest,
            "size_bytes": size_bytes,
            "runtime": runtime,
            "repository": f"{self._repo_prefix}/{validated_name}",
            "tag": validated_tag,
            "region": self.region,
            "retain": retain,
        }

    def push(
        self,
        name: str,
        tag: str,
        local_image: str,
        retain: bool = False,
        quiet: bool = False,
    ) -> dict[str, Any]:
        """Push an already-built local image to the project's ECR repo.

        Tags ``local_image`` as the project URI before invoking
        ``<runtime> push``. Skips the build step but otherwise mirrors
        ``build`` (init repo, login, push, optional retain tag). When ``quiet``
        is true, the local tag command is captured for machine-readable output.
        """
        validated_name = self._validate_name(name)
        validated_tag = self._validate_tag(tag)
        if not local_image:
            raise ValueError("local_image must be a non-empty image reference")

        runtime = self._runtime_or_error()
        self.init(name, retain=retain)
        self._check_tag_immutable_collision(validated_name, validated_tag)
        self._ecr_login(runtime)

        full_uri = f"{self._registry_host()}/{self._repo_prefix}/{validated_name}:{validated_tag}"

        tag_run_kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL}
        if quiet:
            tag_run_kwargs.update(capture_output=True, text=True)
        # check=True: a failed tag must not fall through to the push below.
        subprocess.run([runtime, "tag", local_image, full_uri], check=True, **tag_run_kwargs)
        push_result = subprocess.run(
            [runtime, "push", full_uri],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
        )
        digest = self._extract_digest((push_result.stdout or "") + (push_result.stderr or ""))

        if retain:
            self._apply_retain_tag(validated_name)

        size_bytes = self._image_size_bytes(validated_name, validated_tag)

        return {
            "image_uri": full_uri,
            "digest": digest,
            "size_bytes": size_bytes,
            "runtime": runtime,
            "repository": f"{self._repo_prefix}/{validated_name}",
            "tag": validated_tag,
            "region": self.region,
            "retain": retain,
        }

    def _image_size_bytes(self, name: str, tag: str) -> int | None:
        """Best-effort ECR lookup for the pushed image size."""
        ecr = self._ecr_client()
        try:
            resp = ecr.describe_images(
                repositoryName=f"{self._repo_prefix}/{name}",
                imageIds=[{"imageTag": tag}],
            )
            details = resp.get("imageDetails", [])
            if details:
                size = details[0].get("imageSizeInBytes")
                if isinstance(size, int):
                    return size
        except Exception as e:
            logger.debug("describe_images for size lookup failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Read-only methods
    # ------------------------------------------------------------------
    def list_repos(self) -> list[dict[str, Any]]:
        """List every repository under the project's ``gco/`` prefix."""
        ecr = self._ecr_client()
        repos: list[dict[str, Any]] = []
        paginator = ecr.get_paginator("describe_repositories")
        for page in paginator.paginate():
            for repo in page.get("repositories", []):
                repo_name = repo.get("repositoryName", "")
                if not repo_name.startswith(f"{self._repo_prefix}/"):
                    continue
                image_count = self._image_count(repo_name)
                repos.append(
                    {
                        "name": repo_name,
                        "arn": repo.get("repositoryArn"),
                        "uri": repo.get("repositoryUri"),
                        "created_at": _isoformat(repo.get("createdAt")),
                        "image_count": image_count,
                        "tag_mutability": repo.get("imageTagMutability"),
                    }
                )
        return repos

    def _image_count(self, repository_name: str) -> int:
        """Best-effort count of images in a repository."""
        ecr = self._ecr_client()
        try:
            count = 0
            paginator = ecr.get_paginator("describe_images")
            for page in paginator.paginate(repositoryName=repository_name):
                count += len(page.get("imageDetails", []))
            return count
        except Exception as e:
            logger.debug("describe_images count for %s failed: %s", repository_name, e)
            return 0

    def list_tags(self, name: str) -> list[dict[str, Any]]:
        """List every tag (with digest, pushed date, size) on a repository."""
        validated = self._validate_name(name)
        ecr = self._ecr_client()
        rows: list[dict[str, Any]] = []
        paginator = ecr.get_paginator("describe_images")
        for page in paginator.paginate(
            repositoryName=f"{self._repo_prefix}/{validated}",
        ):
            for detail in page.get("imageDetails", []):
                for tag in detail.get("imageTags", []) or [None]:
                    rows.append(
                        {
                            "tag": tag,
                            "digest": detail.get("imageDigest"),
                            "pushed_at": _isoformat(detail.get("imagePushedAt")),
                            "size_bytes": detail.get("imageSizeInBytes"),
                        }
                    )
        return rows

    def describe(self, name: str, tag: str) -> dict[str, Any]:
        """Return the full ECR image details for a single tag, or ``{}`` without one.

        DescribeImages answers a tag the repository does not have with
        ImageNotFoundException rather than an empty list; both mean ``{}``.
        A missing repository still raises.
        """
        validated_name = self._validate_name(name)
        validated_tag = self._validate_tag(tag)
        ecr = self._ecr_client()
        try:
            resp = ecr.describe_images(
                repositoryName=f"{self._repo_prefix}/{validated_name}",
                imageIds=[{"imageTag": validated_tag}],
            )
        except ecr.exceptions.ImageNotFoundException:
            return {}
        details = resp.get("imageDetails", [])
        if not details:
            return {}
        detail = details[0]
        return {
            "name": f"{self._repo_prefix}/{validated_name}",
            "tag": validated_tag,
            "digest": detail.get("imageDigest"),
            "pushed_at": _isoformat(detail.get("imagePushedAt")),
            "size_bytes": detail.get("imageSizeInBytes"),
            "tags": detail.get("imageTags", []),
            "scan_findings_summary": detail.get("imageScanFindingsSummary"),
        }

    def get_uri(self, name: str, tag: str = "latest") -> str:
        """Return the full registry URI for ``name:tag``. No API call."""
        validated_name = self._validate_name(name)
        validated_tag = self._validate_tag(tag)
        return f"{self._registry_host()}/{self._repo_prefix}/{validated_name}:{validated_tag}"

    def list_maintained_images(self, tag: str = "latest") -> list[dict[str, Any]]:
        """List the first-party images GCO builds and ships.

        Returns one row per shipped image with its logical name, the
        ``gco/<name>`` repository, the Dockerfile that produces it, and
        the registry URI for ``tag``. No API call — the catalog is
        resolved locally from the shipped Dockerfile set.
        """
        rows: list[dict[str, Any]] = []
        for name, dockerfile in _MAINTAINED_IMAGES.items():
            rows.append(
                {
                    "name": name,
                    "repository": f"{self._repo_prefix}/{name}",
                    "dockerfile": dockerfile,
                    "uri": self.get_uri(name, tag),
                }
            )
        return rows

    def get_maintained_image(self, name: str, tag: str = "latest") -> dict[str, Any]:
        """Resolve a single shipped image by name.

        Raises ``ValueError`` for a name that is not one of the shipped
        images, listing the known names so the caller can correct the
        lookup.
        """
        dockerfile = _MAINTAINED_IMAGES.get(name)
        if dockerfile is None:
            known = ", ".join(sorted(_MAINTAINED_IMAGES))
            raise ValueError(f"Unknown maintained image: {name!r}. Known images: {known}.")
        return {
            "name": name,
            "repository": f"{self._repo_prefix}/{name}",
            "dockerfile": dockerfile,
            "uri": self.get_uri(name, tag),
        }

    def default_disaggregated_image_uri(self, tag: str | None = None) -> str:
        """Return the image reference disaggregated prefill/decode deploys serve from.

        As of the pinned tag, the upstream ``vllm/vllm-openai`` image bundles
        the Mooncake transfer engine, so GCO no longer builds its own image —
        deploys pull this upstream image from Docker Hub directly. Returns the
        pinned reference; ``tag``, when given, overrides only the version.
        """
        if tag:
            repo = _DISAGGREGATED_DEFAULT_IMAGE.rsplit(":", 1)[0]
            return f"{repo}:{tag}"
        return _DISAGGREGATED_DEFAULT_IMAGE

    def _current_replication_configuration(self, ecr: Any) -> tuple[str, dict[str, Any]]:
        """Return the registry ID and the registry's ECR replication configuration.

        ECR has no GetReplicationConfiguration operation: DescribeRegistry
        returns the rules (:func:`cli.ecr_replication.read_registry`), an empty
        list when none are configured.
        """
        from .ecr_replication import read_registry

        registry_id, rules = read_registry(ecr)
        return registry_id, {"rules": rules}

    def replication_get(self) -> dict[str, Any]:
        """Return the current ECR replication configuration, or ``{}``.

        The historical ``policy`` response key is retained for CLI/API
        compatibility, but its value now comes from the replication API rather
        than the unrelated registry-permissions policy API.
        """
        registry_id, configuration = self._current_replication_configuration(self._ecr_client())
        if not configuration.get("rules"):
            return {}
        return {
            "registryId": registry_id,
            "policy": configuration,
        }

    def _replication_regions(self) -> list[str]:
        """Resolve deployed regional destinations from supported config data."""
        deployment_regions = _load_cdk_json().get("regional", [])
        candidates = deployment_regions if isinstance(deployment_regions, list) else []
        if not candidates:
            default_region = getattr(self.config, "default_region", None)
            if isinstance(default_region, str) and default_region:
                candidates = [default_region]

        # Preserve declaration order while removing invalid values/duplicates.
        return list(
            dict.fromkeys(region for region in candidates if isinstance(region, str) and region)
        )

    def replication_status(self) -> list[dict[str, Any]]:
        """Per-repo replication status across the project repos."""
        ecr = self._ecr_client()
        rows: list[dict[str, Any]] = []
        for repo in self.list_repos():
            repo_name = repo["name"]
            paginator = ecr.get_paginator("describe_images")
            try:
                for page in paginator.paginate(repositoryName=repo_name):
                    for detail in page.get("imageDetails", []):
                        digest = detail.get("imageDigest")
                        try:
                            status = ecr.describe_image_replication_status(
                                repositoryName=repo_name,
                                imageId={"imageDigest": digest},
                            )
                            for entry in status.get("replicationStatuses", []):
                                rows.append(
                                    {
                                        "repository": repo_name,
                                        "digest": digest,
                                        "region": entry.get("region"),
                                        "status": entry.get("status"),
                                        "registry_id": entry.get("registryId"),
                                    }
                                )
                        except (ClientError, AttributeError) as e:
                            logger.debug(
                                "describe_image_replication_status failed for %s %s: %s",
                                repo_name,
                                digest,
                                e,
                            )
            except ClientError as e:
                logger.debug("describe_images failed for %s: %s", repo_name, e)
        return rows

    # ------------------------------------------------------------------
    # Administrative methods
    # ------------------------------------------------------------------
    def init(self, name: str, retain: bool = False) -> dict[str, Any]:
        """Create the project repository idempotently with default lifecycle.

        ``CreateRepository`` is invoked with ``imageTagMutability=MUTABLE``
        and ``scanOnPush=True``. If the repository already exists, the
        method becomes a no-op for repository creation but still applies
        the default lifecycle policy and the optional ``gco:retain`` tag.
        """
        validated = self._validate_name(name)
        repo_name = f"{self._repo_prefix}/{validated}"
        ecr = self._ecr_client()

        created = False
        try:
            ecr.create_repository(
                repositoryName=repo_name,
                imageTagMutability="MUTABLE",
                imageScanningConfiguration={"scanOnPush": True},
                tags=[
                    {"Key": "Project", "Value": self.config.project_name},
                ],
            )
            created = True
        except ecr.exceptions.RepositoryAlreadyExistsException:
            # Idempotent init — re-running ``gco images init`` against an
            # already-provisioned repo is a no-op for create_repository.
            # We still flow through the lifecycle/retain blocks below so
            # any drift in policy is healed on every call.
            logger.debug("repository %s already exists; skipping create", repo_name)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "RepositoryAlreadyExistsException":
                raise

        try:
            ecr.put_lifecycle_policy(
                repositoryName=repo_name,
                lifecyclePolicyText=json.dumps(self._default_lifecycle_policy()),
            )
        except ClientError as e:
            logger.debug("put_lifecycle_policy on %s failed: %s", repo_name, e)

        if retain:
            try:
                self._apply_retain_tag(validated)
            except ClientError as e:
                logger.debug("apply retain tag on %s failed: %s", repo_name, e)

        return {
            "name": repo_name,
            "created": created,
            "retain": retain,
        }

    def lifecycle_get(self, name: str) -> dict[str, Any]:
        """Return the lifecycle policy on a repository, or ``{}``."""
        validated = self._validate_name(name)
        ecr = self._ecr_client()
        try:
            resp = ecr.get_lifecycle_policy(
                repositoryName=f"{self._repo_prefix}/{validated}",
            )
            policy_text = resp.get("lifecyclePolicyText")
            if policy_text:
                return {
                    "name": f"{self._repo_prefix}/{validated}",
                    "policy": json.loads(policy_text),
                }
        except ecr.exceptions.LifecyclePolicyNotFoundException:
            return {}
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "LifecyclePolicyNotFoundException":
                return {}
            raise
        return {}

    def lifecycle_set(self, name: str, policy: dict[str, Any]) -> dict[str, Any]:
        """Replace the lifecycle policy on a repository."""
        validated = self._validate_name(name)
        ecr = self._ecr_client()
        resp = ecr.put_lifecycle_policy(
            repositoryName=f"{self._repo_prefix}/{validated}",
            lifecyclePolicyText=json.dumps(policy),
        )
        return {
            "name": f"{self._repo_prefix}/{validated}",
            "registry_id": resp.get("registryId"),
            "policy": policy,
        }

    def replication_sync(self) -> dict[str, Any]:
        """Apply this project's replication rule without clobbering others.

        Existing rules for unrelated repository prefixes are retained. If no
        non-source destination can be resolved, no write is made; this avoids
        replacing a valid registry configuration with an empty rule set.
        """
        ecr = self._ecr_client()
        registry_id, current = self._current_replication_configuration(ecr)
        destinations = [region for region in self._replication_regions() if region != self.region]

        if not destinations:
            return {
                "configuration": current,
                "destinations": [],
                "registry_id": registry_id,
                "updated": False,
            }

        account = self._account_id()
        managed_filter = {
            "filter": f"{self._repo_prefix}/",
            "filterType": "PREFIX_MATCH",
        }
        managed_rule = {
            "destinations": [{"region": region, "registryId": account} for region in destinations],
            "repositoryFilters": [managed_filter],
        }

        preserved_rules: list[dict[str, Any]] = []
        for existing_rule in current.get("rules", []):
            if not isinstance(existing_rule, dict):
                continue
            filters = existing_rule.get("repositoryFilters") or []
            managed_filters = [item for item in filters if item == managed_filter]
            if not managed_filters:
                preserved_rules.append(existing_rule)
                continue

            # A rule can contain filters for multiple prefixes. Retain the
            # unrelated filters with their original destinations while replacing
            # only this project's managed filter.
            unrelated_filters = [item for item in filters if item != managed_filter]
            if unrelated_filters:
                preserved_rules.append({**existing_rule, "repositoryFilters": unrelated_filters})

        configuration = {
            **current,
            "rules": [*preserved_rules, managed_rule],
        }
        # The response carries only the configuration written; the registry ID
        # is the one DescribeRegistry reported.
        ecr.put_replication_configuration(replicationConfiguration=configuration)
        return {
            "configuration": configuration,
            "destinations": destinations,
            "registry_id": registry_id,
            "updated": True,
        }

    # ------------------------------------------------------------------
    # Destructive methods
    # ------------------------------------------------------------------
    def delete_tag(self, name: str, tag: str) -> dict[str, Any]:
        """Delete a single tag from a repository."""
        validated_name = self._validate_name(name)
        validated_tag = self._validate_tag(tag)
        ecr = self._ecr_client()
        resp = ecr.batch_delete_image(
            repositoryName=f"{self._repo_prefix}/{validated_name}",
            imageIds=[{"imageTag": validated_tag}],
        )
        return {
            "name": f"{self._repo_prefix}/{validated_name}",
            "tag": validated_tag,
            "deleted": [
                {"digest": d.get("imageDigest"), "tag": d.get("imageTag")}
                for d in resp.get("imageIds", [])
            ],
            "failures": resp.get("failures", []),
        }

    def delete_repo(self, name: str, force: bool = False) -> dict[str, Any]:
        """Delete a repository (optionally including its images)."""
        validated = self._validate_name(name)
        ecr = self._ecr_client()
        resp = ecr.delete_repository(
            repositoryName=f"{self._repo_prefix}/{validated}",
            force=force,
        )
        return {
            "name": f"{self._repo_prefix}/{validated}",
            "deleted": True,
            "registry_id": resp.get("repository", {}).get("registryId"),
        }

    def cleanup(
        self,
        name: str | None = None,
        all: bool = False,
    ) -> dict[str, Any]:
        """Delete every untagged image across one or all project repos."""
        if not name and not all:
            raise ValueError("cleanup() requires either a name or all=True")

        repos: list[str]
        if name:
            validated = self._validate_name(name)
            repos = [f"{self._repo_prefix}/{validated}"]
        else:
            repos = [r["name"] for r in self.list_repos()]

        ecr = self._ecr_client()
        repos_touched = 0
        tags_deleted = 0
        bytes_freed = 0

        for repo_name in repos:
            untagged_ids: list[dict[str, str]] = []
            untagged_size = 0
            try:
                paginator = ecr.get_paginator("describe_images")
                for page in paginator.paginate(
                    repositoryName=repo_name,
                    filter={"tagStatus": "UNTAGGED"},
                ):
                    for detail in page.get("imageDetails", []):
                        digest = detail.get("imageDigest")
                        if not digest:
                            continue
                        untagged_ids.append({"imageDigest": digest})
                        size = detail.get("imageSizeInBytes") or 0
                        if isinstance(size, int):
                            untagged_size += size
            except ClientError as e:
                logger.debug("describe_images for cleanup of %s failed: %s", repo_name, e)
                continue

            if not untagged_ids:
                continue
            repos_touched += 1
            # batch_delete_image accepts up to 100 ids per call.
            for chunk_start in range(0, len(untagged_ids), 100):
                chunk = untagged_ids[chunk_start : chunk_start + 100]
                resp = ecr.batch_delete_image(
                    repositoryName=repo_name,
                    imageIds=chunk,
                )
                tags_deleted += len(resp.get("imageIds", []))
            bytes_freed += untagged_size

        return {
            "repos_touched": repos_touched,
            "tags_deleted": tags_deleted,
            "bytes_freed": bytes_freed,
        }

    def prune(self, dry_run: bool = True) -> dict[str, Any]:
        """Remove untagged images older than 30 days.

        Returns the same shape as ``cleanup``; when ``dry_run`` is True
        (the default), no images are deleted.
        """
        cutoff = datetime.now(UTC) - timedelta(days=30)
        ecr = self._ecr_client()
        repos_touched = 0
        tags_deleted = 0
        bytes_freed = 0

        for repo in self.list_repos():
            repo_name = repo["name"]
            stale_ids: list[dict[str, str]] = []
            stale_size = 0
            try:
                paginator = ecr.get_paginator("describe_images")
                for page in paginator.paginate(
                    repositoryName=repo_name,
                    filter={"tagStatus": "UNTAGGED"},
                ):
                    for detail in page.get("imageDetails", []):
                        pushed = detail.get("imagePushedAt")
                        if pushed and pushed >= cutoff:
                            continue
                        digest = detail.get("imageDigest")
                        if not digest:
                            continue
                        stale_ids.append({"imageDigest": digest})
                        size = detail.get("imageSizeInBytes") or 0
                        if isinstance(size, int):
                            stale_size += size
            except ClientError as e:
                logger.debug("describe_images for prune of %s failed: %s", repo_name, e)
                continue

            if not stale_ids:
                continue
            repos_touched += 1
            tags_deleted += len(stale_ids)
            bytes_freed += stale_size
            if dry_run:
                continue
            for chunk_start in range(0, len(stale_ids), 100):
                chunk = stale_ids[chunk_start : chunk_start + 100]
                ecr.batch_delete_image(
                    repositoryName=repo_name,
                    imageIds=chunk,
                )

        return {
            "dry_run": dry_run,
            "repos_touched": repos_touched,
            "tags_deleted": tags_deleted,
            "bytes_freed": bytes_freed,
        }

    def orphans(self, threshold_days: int = 30) -> list[dict[str, Any]]:
        """List ``<project>/*`` tags older than ``threshold_days`` that nothing uses.

        A tag is in use when an inference endpoint's record or a job names it,
        by tag or by digest, through any Region's replica of the registry
        (:meth:`_references_here`). Jobs count while they are unfinished, and
        finished ones while they are newer than ``threshold_days``. Every
        source must be read: a failure raises rather than report the tags of
        an unread endpoint or Region as orphans.
        """
        cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
        tags_in_use, digests_in_use = self._references_here(
            self._collect_inference_image_refs()
            | self._collect_recent_job_image_refs(threshold_days)
        )

        rows: list[dict[str, Any]] = []
        for repo in self.list_repos():
            repo_name = repo["name"]
            for tag_row in self.list_tags(repo_name.removeprefix(f"{self._repo_prefix}/")):
                tag = tag_row.get("tag")
                if not tag:
                    continue
                pushed = self._parse_iso(tag_row.get("pushed_at"))
                if pushed and pushed >= cutoff:
                    continue
                repository = f"{self._registry_host()}/{repo_name}"
                uri = f"{repository}:{tag}"
                digest = tag_row.get("digest")
                if uri in tags_in_use or f"{repository}@{digest}" in digests_in_use:
                    continue
                rows.append(
                    {
                        "repository": repo_name,
                        "tag": tag,
                        "digest": digest,
                        "pushed_at": tag_row.get("pushed_at"),
                        "uri": uri,
                    }
                )
        return rows

    def _references_here(self, refs: set[str]) -> tuple[set[str], set[str]]:
        """Return ``(tag_uris, digest_uris)`` for ``refs`` as this Region names them.

        Endpoints and jobs name the replica in their own Region
        (:func:`cli._image_uri.rewrite_image_uri_for_region`), so an ECR
        reference is first rewritten to this manager's Region. A reference
        with a digest (``repo@sha256:...``, also ``repo:tag@sha256:...``)
        names that image whatever its tags; one without a tag or digest
        names ``:latest``.
        """
        tag_uris: set[str] = set()
        digest_uris: set[str] = set()
        for ref in refs:
            name, _, digest = _rewrite_image_uri_for_region(ref, self.region).partition("@")
            slash, colon = name.rfind("/"), name.rfind(":")
            repository, tag = (name[:colon], name[colon + 1 :]) if colon > slash else (name, "")
            if digest:
                digest_uris.add(f"{repository}@{digest}")
                if tag:
                    tag_uris.add(name)
            else:
                tag_uris.add(f"{repository}:{tag or 'latest'}")
        return tag_uris, digest_uris

    def _collect_inference_image_refs(self) -> set[str]:
        """Return every image an inference endpoint's record names.

        That is each ``image`` value anywhere in the record (the endpoint's,
        the canary's, the Mooncake proxy's) and each per-Region replica URI in
        ``region_image_uris``. Raises when the endpoints cannot be read.
        """
        from .inference import InferenceManager

        try:
            endpoints = InferenceManager(self.config).list_endpoints()
        except Exception as e:
            raise RuntimeError(f"could not read the inference endpoints: {e}") from e

        refs: set[str] = set()

        def collect(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "image" and isinstance(value, str) and value:
                        refs.add(value)
                    elif key == "region_image_uris" and isinstance(value, dict):
                        refs.update(uri for uri in value.values() if isinstance(uri, str) and uri)
                    else:
                        collect(value)
            elif isinstance(node, list):
                for item in node:
                    collect(item)

        collect(endpoints)
        return refs

    def _collect_recent_job_image_refs(self, threshold_days: int = 30) -> set[str]:
        """Return the images of every unfinished job and of jobs newer than ``threshold_days``.

        Lists each regional stack's jobs with :class:`cli.jobs.JobManager`, one
        Region at a time, so a Region that cannot be read raises instead of
        being skipped: a job there may use any tag. A job without a
        ``created_time`` counts as new. The deferred imports break an
        otherwise-circular ``cli.images`` ↔ ``cli.jobs`` dependency.
        """
        from .aws_client import get_aws_client
        from .jobs import JobManager

        manager = JobManager(self.config)
        regions = sorted(get_aws_client(self.config).discover_regional_stacks())
        cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
        refs: set[str] = set()
        for region in regions:
            try:
                jobs = manager.list_jobs(region=region)
            except Exception as e:
                raise RuntimeError(f"could not list the jobs in {region}: {e}") from e
            for job in jobs:
                created = self._parse_iso(getattr(job, "created_time", None))
                finished = getattr(job, "status", None) in ("succeeded", "failed")
                if finished and created is not None and created < cutoff:
                    continue
                refs.update(
                    ref for ref in getattr(job, "image_refs", None) or [] if isinstance(ref, str)
                )
        refs.discard("")
        return refs

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        """Parse an ISO-8601 string into a tz-aware datetime, else None."""
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _isoformat(value: Any) -> str | None:
    """Return ISO-8601 form of a datetime, or pass-through for strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def get_image_manager(config: GCOConfig | None = None, region: str | None = None) -> ImageManager:
    """Factory function for ``ImageManager``."""
    return ImageManager(config=config, region=region)


def default_disaggregated_image(
    config: GCOConfig | None = None,
    region: str | None = None,
    tag: str | None = None,
) -> str:
    """Resolve the default image reference for disaggregated prefill/decode deploys.

    Convenience wrapper around
    :meth:`ImageManager.default_disaggregated_image_uri` for callers
    that only need the reference and do not otherwise hold a manager.
    """
    return get_image_manager(config=config, region=region).default_disaggregated_image_uri(tag)
