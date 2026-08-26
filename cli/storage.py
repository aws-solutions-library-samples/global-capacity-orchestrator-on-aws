"""Discover GCO S3 buckets and safely sync data with local storage.

The physical names of GCO buckets contain deployment-generated account and
region components.  This module keeps those names out of the user interface by
resolving stable aliases from the SSM and CloudFormation contracts published by
the stacks.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
import sys
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import boto3
from botocore.exceptions import ClientError

from .config import GCOConfig, get_config

type _FileSignature = tuple[int, int, int, int, int]


class StorageBucketNotFoundError(RuntimeError):
    """Raised when a friendly alias has no deployed backing bucket."""


@dataclass(frozen=True)
class _ConfinementContract:
    """Identity-bound MCP local-root contract propagated to the CLI."""

    root: Path
    device: int
    inode: int


class _PinnedRoot:
    """Descriptor-pinned root for race-resistant confined filesystem access."""

    def __init__(self, contract: _ConfinementContract):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise RuntimeError(
                "Confined storage sync requires descriptor-relative no-follow filesystem support"
            )
        if not contract.root.is_absolute():
            raise ValueError("The internal storage confinement root must be absolute")

        self.root = contract.root
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            self._fd = os.open(self.root, flags)
        except OSError as exc:
            raise ValueError(
                f"Cannot open the configured storage confinement root securely: {self.root}"
            ) from exc

        root_stat = os.fstat(self._fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            os.close(self._fd)
            self._fd = -1
            raise NotADirectoryError(
                f"The configured storage confinement root is not a directory: {self.root}"
            )
        if (root_stat.st_dev, root_stat.st_ino) != (contract.device, contract.inode):
            os.close(self._fd)
            self._fd = -1
            raise RuntimeError(
                "GCO_STORAGE_LOCAL_ROOT changed after the MCP request was validated; retry the call"
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    @staticmethod
    def _file_flags() -> int:
        # Avoid blocking if a raced source replacement is a FIFO; fstat below
        # still rejects every opened object that is not a regular file.
        return (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )

    def relative_parts(self, local_path: str) -> tuple[str, ...]:
        """Return a lexical root-relative path; traversal itself stays descriptor-relative."""
        supplied = Path(local_path).expanduser()
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        lexical = Path(os.path.abspath(candidate))
        try:
            relative = lexical.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"Local sync path must stay within GCO_STORAGE_LOCAL_ROOT: {local_path}"
            ) from exc

        parts = tuple(relative.parts)
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(
                f"Local sync path must stay within GCO_STORAGE_LOCAL_ROOT: {local_path}"
            )
        return parts

    def display_path(self, parts: tuple[str, ...]) -> Path:
        """Return a human-readable path without using it for confined access."""
        return self.root.joinpath(*parts)

    def open_directory(
        self,
        parts: tuple[str, ...],
        *,
        create: bool = False,
        allow_missing: bool = False,
    ) -> int | None:
        """Open a directory by walking from the pinned root without following links."""
        current_fd = os.dup(self._fd)
        try:
            walked: tuple[str, ...] = ()
            for part in parts:
                walked += (part,)
                try:
                    child_fd = os.open(part, self._directory_flags(), dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        if allow_missing:
                            os.close(current_fd)
                            return None
                        raise FileNotFoundError(
                            f"Confined local directory does not exist: {self.display_path(walked)}"
                        ) from None
                    with suppress(FileExistsError):
                        os.mkdir(part, dir_fd=current_fd)
                    try:
                        child_fd = os.open(part, self._directory_flags(), dir_fd=current_fd)
                    except OSError as exc:
                        raise ValueError(
                            "Confined local path changed while creating a directory: "
                            f"{self.display_path(walked)}"
                        ) from exc
                except OSError as exc:
                    raise ValueError(
                        "Confined local path contains a symbolic link or non-directory: "
                        f"{self.display_path(walked)}"
                    ) from exc
                os.close(current_fd)
                current_fd = child_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    def inspect_directory(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> bool:
        """Securely inspect or create a confined directory."""
        directory_fd = self.open_directory(parts, create=create, allow_missing=not create)
        if directory_fd is None:
            return False
        os.close(directory_fd)
        return True

    def lstat(self, parts: tuple[str, ...]) -> os.stat_result | None:
        """Stat a confined path without following its final component."""
        if not parts:
            return os.fstat(self._fd)
        parent_fd = self.open_directory(parts[:-1], allow_missing=True)
        if parent_fd is None:
            return None
        try:
            try:
                return os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
        finally:
            os.close(parent_fd)

    def open_regular_file(self, parts: tuple[str, ...]) -> int:
        """Open a confined regular file without following any path component."""
        if not parts:
            raise IsADirectoryError(f"Upload source is a directory: {self.root}")
        parent_fd = self.open_directory(parts[:-1])
        if parent_fd is None:  # pragma: no cover - allow_missing is false
            raise FileNotFoundError(self.display_path(parts))
        try:
            try:
                file_fd = os.open(parts[-1], self._file_flags(), dir_fd=parent_fd)
            except OSError as exc:
                raise ValueError(
                    "Confined upload source is missing, linked, or not a regular file: "
                    f"{self.display_path(parts)}"
                ) from exc
        finally:
            os.close(parent_fd)

        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            raise ValueError(f"Upload source is not a regular file: {self.display_path(parts)}")
        return file_fd

    def open_child_directory(self, parent_fd: int, name: str, display: Path) -> int:
        """Open an enumerated child directory without following a raced replacement."""
        try:
            child_fd = os.open(name, self._directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"Upload source directory changed or is linked: {display}") from exc
        if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
            os.close(child_fd)
            raise ValueError(f"Upload source is not a directory: {display}")
        return child_fd

    def open_child_regular_file(self, parent_fd: int, name: str, display: Path) -> int:
        """Open an enumerated child file without following a raced replacement."""
        try:
            file_fd = os.open(name, self._file_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"Upload source file changed or is linked: {display}") from exc
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            raise ValueError(f"Upload source is not a regular file: {display}")
        return file_fd

    def download_target_is_current(
        self,
        parts: tuple[str, ...],
        size: int,
        modified: datetime | None,
        *,
        evaluate_current: bool,
    ) -> bool:
        """Securely inspect a prospective destination and optionally test freshness."""
        parent_fd = self.open_directory(parts[:-1], allow_missing=True)
        if parent_fd is None:
            return False
        try:
            try:
                target_stat = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
        finally:
            os.close(parent_fd)

        display = self.display_path(parts)
        if stat.S_ISLNK(target_stat.st_mode):
            raise ValueError(f"Download destination must not be a symbolic link: {display}")
        if stat.S_ISDIR(target_stat.st_mode):
            raise IsADirectoryError(f"S3 object maps to an existing directory: {display}")
        if not stat.S_ISREG(target_stat.st_mode):
            raise ValueError(f"Download destination is not a regular file: {display}")
        return bool(
            evaluate_current
            and modified is not None
            and target_stat.st_size == size
            and int(target_stat.st_mtime) >= int(modified.timestamp())
        )

    def download_object(self, s3: Any, bucket: str, obj: _SyncObject) -> None:
        """Download to a secure sibling temporary file and atomically install it."""
        if obj.destination_parts is None:  # pragma: no cover - internal invariant
            raise RuntimeError("Missing confined destination components")
        parent_fd = self.open_directory(obj.destination_parts[:-1], create=True)
        if parent_fd is None:  # pragma: no cover - create is true
            raise RuntimeError("Failed to create confined destination directory")

        temporary_name = ""
        temporary_created = False
        try:
            temporary_flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            for _ in range(128):
                temporary_name = f".gco-sync-{os.getpid()}-{secrets.token_hex(12)}.tmp"
                try:
                    temporary_fd = os.open(
                        temporary_name,
                        temporary_flags,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    temporary_created = True
                    break
                except FileExistsError:
                    continue
            else:  # pragma: no cover - cryptographically improbable
                raise RuntimeError("Could not allocate a unique download temporary file")

            with os.fdopen(temporary_fd, "w+b") as temporary_file:
                s3.download_fileobj(bucket, obj.key, temporary_file)
                temporary_file.flush()
                downloaded_stat = os.fstat(temporary_file.fileno())
                if downloaded_stat.st_size != obj.size:
                    raise RuntimeError(
                        f"Downloaded size changed for s3://{bucket}/{obj.key}: "
                        f"expected {obj.size}, received {downloaded_stat.st_size}"
                    )
                if obj.last_modified is not None:
                    timestamp = obj.last_modified.timestamp()
                    os.utime(temporary_file.fileno(), (timestamp, timestamp))

            os.replace(
                temporary_name,
                obj.destination_parts[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_created = False
        finally:
            if temporary_created:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent_fd)
            os.close(parent_fd)


@dataclass(frozen=True)
class _SyncObject:
    """One validated object in a bucket-to-local sync plan."""

    key: str
    destination: Path
    destination_parts: tuple[str, ...] | None
    size: int
    last_modified: datetime | None
    current: bool


@dataclass(frozen=True)
class _PreparedUpload:
    """One securely opened and hashed local upload source."""

    source: Path
    relative: str
    source_parts: tuple[str, ...] | None
    size: int
    sha256: str
    signature: _FileSignature


@dataclass(frozen=True)
class _UploadObject:
    """One validated local file in a local-to-bucket sync plan."""

    source: Path
    source_parts: tuple[str, ...] | None
    key: str
    size: int
    sha256: str
    signature: _FileSignature
    current: bool


class StorageManager:
    """Resolve friendly GCO bucket aliases and transfer their contents."""

    _UPLOAD_DIGEST_METADATA = "gco-sync-sha256"

    _PURPOSES = {
        "cluster-shared": "Cross-region cluster job artifacts and shared data",
        "model-weights": "Central model weights used by inference endpoints",
        "regional-shared": "General-purpose data for workloads in one region",
        "analytics-studio": "SageMaker Studio private scratch data and outputs",
    }

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        # One CloudFormation sweep per (stack, region) serves every access-logs
        # entry in that stack, so a full inventory stays at one call per stack.
        self._stack_resource_cache: dict[tuple[str, str], dict[str, str]] = {}

    def list_buckets(self, region: str | None = None) -> list[dict[str, str]]:
        """Return deployed user-facing buckets under their stable aliases.

        Global and analytics buckets are always considered. ``region`` limits
        regional-bucket discovery to one region; otherwise every regional
        deployment configured in ``cdk.json`` is considered. A bucket whose
        stack is not deployed is omitted, while permission and transport
        errors still surface to the caller.
        """
        buckets: list[dict[str, str]] = []

        for alias in ("cluster-shared", "model-weights"):
            with suppress(StorageBucketNotFoundError):
                buckets.append(self.resolve_bucket(alias))

        regional_regions = [region] if region else self._configured_regional_regions()
        for regional_region in regional_regions:
            with suppress(StorageBucketNotFoundError):
                buckets.append(self.resolve_bucket("regional-shared", region=regional_region))

        with suppress(StorageBucketNotFoundError):
            buckets.append(self.resolve_bucket("analytics-studio"))

        return buckets

    def s3_inventory(self, region: str | None = None) -> dict[str, Any]:
        """Describe every S3 bucket this deployment creates, deployed or not.

        Complements :meth:`list_buckets`, which deliberately reports only the
        four user-facing buckets addressable by ``storage sync``. This reports
        the full set — including the server-access-log sinks and the cost-report
        bucket — with the deployment-contract facts a caller actually needs:
        which stack owns each bucket, what it is for, whether job pods can reach
        it and how they discover it, what happens to it on teardown, and which
        object-key prefixes the platform has already reserved.

        A bucket whose stack is not deployed is reported with
        ``status="not-deployed"`` rather than omitted, so the answer to "what
        buckets does this deployment have?" is complete even before a region is
        rolled out. Static facts come from :data:`BUCKET_DESCRIPTORS` and need no
        AWS call; only the physical name, ARN, and status are resolved live.

        ``region`` limits the regional entries to one region. Global,
        monitoring, and analytics entries are always included because they exist
        once per deployment, not once per region.

        Note this inventories *buckets and their deployment contract*. It is
        unrelated to the AWS "S3 Inventory" feature, which produces scheduled
        reports of the objects inside a single bucket.
        """
        project = self.config.project_name
        regional_regions = [region] if region else self._configured_regional_regions()
        account = self._account_id()

        # One CloudFormation sweep per stack, shared by every access-logs entry
        # in that stack — those buckets are CDK-auto-named, so their physical
        # names exist only as stack resources.
        log_buckets: dict[tuple[str, str], str | None] = {}
        stacks_to_sweep: list[tuple[str, str, str]] = [
            ("global", self.config.global_stack_name, self.config.global_region),
            ("monitoring", f"{project}-monitoring", self.config.monitoring_region),
            ("analytics", f"{project}-analytics", self.config.api_gateway_region),
        ]
        stacks_to_sweep.extend(
            (f"regional:{item}", f"{self.config.regional_stack_prefix}-{item}", item)
            for item in regional_regions
        )
        for scope_key, stack_name, stack_region in stacks_to_sweep:
            for logical_id, physical in self._stack_bucket_resources(
                stack_name, stack_region
            ).items():
                log_buckets[(scope_key, logical_id)] = physical

        records: list[dict[str, Any]] = []
        for descriptor in BUCKET_DESCRIPTORS:
            if descriptor.scope == "regional":
                for regional_region in regional_regions:
                    records.append(
                        self._s3_inventory_record(
                            descriptor,
                            region=regional_region,
                            stack_name=f"{self.config.regional_stack_prefix}-{regional_region}",
                            account=account,
                            log_buckets=log_buckets,
                            scope_key=f"regional:{regional_region}",
                        )
                    )
                continue

            scope_region, stack_name = {
                "global": (self.config.global_region, self.config.global_stack_name),
                "monitoring": (self.config.monitoring_region, f"{project}-monitoring"),
                "analytics": (self.config.api_gateway_region, f"{project}-analytics"),
            }[descriptor.scope]
            records.append(
                self._s3_inventory_record(
                    descriptor,
                    region=scope_region,
                    stack_name=stack_name,
                    account=account,
                    log_buckets=log_buckets,
                    scope_key=descriptor.scope,
                )
            )

        deployed = sum(1 for item in records if item["status"] == "deployed")
        return {
            "project_name": project,
            "account": account,
            "regions": {
                "global": self.config.global_region,
                "monitoring": self.config.monitoring_region,
                "analytics": self.config.api_gateway_region,
                "regional": regional_regions,
            },
            "buckets": records,
            "summary": {
                "total": len(records),
                "deployed": deployed,
                "not_deployed": len(records) - deployed,
                "pod_writable": sorted(
                    item["bucket"]
                    for item in records
                    if item["pod_access"] == "read-write" and item["bucket"]
                ),
            },
        }

    def _s3_inventory_record(
        self,
        descriptor: BucketDescriptor,
        *,
        region: str,
        stack_name: str,
        account: str | None,
        log_buckets: dict[tuple[str, str], str | None],
        scope_key: str,
    ) -> dict[str, Any]:
        """Merge one descriptor's static facts with its live name and status."""
        name: str | None = None
        arn: str | None = None
        detail = ""

        try:
            if descriptor.role == "access-logs":
                name = log_buckets.get((scope_key, descriptor.logical_id_prefix))
            else:
                name, arn = self._resolve_primary_bucket(descriptor, region, account)
        except Exception as exc:  # noqa: BLE001 - an unresolvable entry is reported, not fatal
            detail = f"could not resolve: {exc}"

        if name and not arn:
            arn = f"arn:{self._partition_for(region)}:s3:::{name}"
        if not name and not detail:
            detail = (
                f"{stack_name} is not deployed"
                if descriptor.opt_in is None
                else f"{stack_name} is not deployed (opt-in: {descriptor.opt_in})"
            )

        return {
            "id": descriptor.id if descriptor.scope != "regional" else f"{descriptor.id}:{region}",
            "role": descriptor.role,
            "scope": descriptor.scope,
            "region": region,
            "owning_stack": stack_name,
            "bucket": name,
            "arn": arn,
            "s3_uri": f"s3://{name}/" if name else None,
            "status": "deployed" if name else "not-deployed",
            "detail": detail,
            "purpose": descriptor.purpose,
            "pod_access": descriptor.pod_access,
            "discovery": descriptor.discovery,
            "removal_policy": descriptor.removal_policy,
            "reserved_prefixes": list(descriptor.reserved_prefixes),
            "sync_alias": (
                f"{descriptor.sync_alias}:{region}"
                if descriptor.sync_alias and descriptor.scope == "regional"
                else descriptor.sync_alias
            ),
            "opt_in": descriptor.opt_in,
        }

    def _resolve_primary_bucket(
        self, descriptor: BucketDescriptor, region: str, account: str | None
    ) -> tuple[str | None, str | None]:
        """Resolve a primary bucket's physical name and ARN, or ``(None, None)``.

        Each family publishes its identity differently, so this routes to the
        contract that family actually uses rather than reconstructing names:
        the two shared buckets publish name+ARN to SSM, the model bucket
        publishes its name, the cost bucket's name is deterministic by design
        (so regional stacks can grant on it before it exists), and the Studio
        bucket is CDK-auto-named and only knowable from its stack.
        """
        from gco.services.aws_ssm import get_ssm_parameter_optional
        from gco.stacks.constants import (
            cluster_shared_ssm_parameter_prefix,
            cost_report_bucket_name,
            regional_shared_ssm_parameter_prefix,
        )

        project = self.config.project_name

        if descriptor.id == "cluster-shared":
            prefix = cluster_shared_ssm_parameter_prefix(project)
            return (
                get_ssm_parameter_optional(f"{prefix}/name", region=region),
                get_ssm_parameter_optional(f"{prefix}/arn", region=region),
            )
        if descriptor.id == "regional-shared":
            prefix = regional_shared_ssm_parameter_prefix(project)
            return (
                get_ssm_parameter_optional(f"{prefix}/name", region=region),
                get_ssm_parameter_optional(f"{prefix}/arn", region=region),
            )
        if descriptor.id == "model-weights":
            return get_ssm_parameter_optional(f"/{project}/model-bucket-name", region=region), None
        if descriptor.id == "cost-reports":
            if not account:
                return None, None
            # Deterministic by design so regional stacks can grant on it before
            # the monitoring stack exists. Confirm it is really there rather
            # than reporting a name that may never have been created.
            expected = cost_report_bucket_name(project, account, region)
            resources = self._stack_bucket_resources(f"{project}-monitoring", region)
            return (expected, None) if expected in resources.values() else (None, None)
        if descriptor.id == "analytics-studio":
            resources = self._stack_bucket_resources(f"{project}-analytics", region)
            return resources.get(descriptor.logical_id_prefix), None
        return None, None

    def _stack_bucket_resources(self, stack_name: str, region: str) -> dict[str, str]:
        """Map ``logical-id-prefix -> physical bucket name`` for one stack.

        CDK appends a hash to logical IDs, so entries are keyed by the stable
        construct-id prefix the descriptors declare. Returns ``{}`` when the
        stack is absent — an undeployed stack is an expected state here, not an
        error — while permission and transport failures propagate so they are
        never silently reported as "not deployed".
        """
        cache_key = (stack_name, region)
        if cache_key in self._stack_resource_cache:
            return self._stack_resource_cache[cache_key]

        prefixes = {item.logical_id_prefix for item in BUCKET_DESCRIPTORS}
        found: dict[str, str] = {}
        cfn = boto3.client("cloudformation", region_name=region)
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, str] = {"StackName": stack_name}
                if token:
                    kwargs["NextToken"] = token
                response = cfn.list_stack_resources(**kwargs)
                for resource in response.get("StackResourceSummaries", []):
                    if resource.get("ResourceType") != "AWS::S3::Bucket":
                        continue
                    logical_id = str(resource.get("LogicalResourceId", ""))
                    physical = resource.get("PhysicalResourceId")
                    if not isinstance(physical, str) or not physical:
                        continue
                    for prefix in prefixes:
                        if logical_id.startswith(prefix):
                            found[prefix] = physical
                            break
                token_value = response.get("NextToken")
                token = token_value if isinstance(token_value, str) else None
                if not token:
                    break
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") == "ValidationError" and "does not exist" in str(
                error.get("Message", "")
            ):
                found = {}
            else:
                raise

        self._stack_resource_cache[cache_key] = found
        return found

    def _account_id(self) -> str | None:
        """The caller's account id, or ``None`` when it cannot be determined.

        ``sts:GetCallerIdentity`` needs no IAM permission, so this normally
        succeeds wherever credentials exist at all.
        """
        try:
            identity = boto3.client("sts").get_caller_identity()
            value = identity.get("Account")
            return str(value) if value else None
        except Exception:  # noqa: BLE001 - the inventory degrades without it
            return None

    def _partition_for(self, region: str) -> str:
        """ARN partition for a region (aws, aws-cn, aws-us-gov)."""
        try:
            return str(boto3.Session().get_partition_for_region(region))
        except Exception:  # noqa: BLE001 - commercial is the right default
            return "aws"

    def resolve_bucket(self, alias: str, region: str | None = None) -> dict[str, str]:
        """Resolve a stable alias to a physical bucket and home region.

        Supported aliases are ``cluster-shared``, ``model-weights``,
        ``analytics-studio``, and either ``regional-shared:<region>`` or
        ``regional-shared`` with ``region`` supplied. The unqualified regional
        alias is inferred only when exactly one deployment region is configured.
        """
        normalized = alias.strip().lower()
        embedded_region: str | None = None

        if normalized.startswith("regional-shared:"):
            normalized, embedded_region = normalized.split(":", 1)
            embedded_region = embedded_region.strip()
            if not embedded_region:
                raise ValueError("Regional bucket alias must include a region after ':'")
            if region and region != embedded_region:
                raise ValueError(
                    f"Alias region '{embedded_region}' conflicts with --region '{region}'"
                )
            region = embedded_region

        if normalized == "regional-shared":
            target_region = region or self._infer_single_regional_region()
            return self._resolve_regional_shared(target_region)

        if region:
            raise ValueError("--region is only valid with the 'regional-shared' alias")

        if normalized == "cluster-shared":
            return self._resolve_cluster_shared()
        if normalized == "model-weights":
            return self._resolve_model_weights()
        if normalized == "analytics-studio":
            return self._resolve_analytics_studio()

        raise ValueError(
            f"Unknown bucket alias '{alias}'. Use one of: cluster-shared, "
            "model-weights, regional-shared:<region>, analytics-studio"
        )

    def sync(
        self,
        alias: str,
        local_dir: str,
        *,
        region: str | None = None,
        prefix: str = "",
        direction: str = "download",
        dry_run: bool = False,
        force: bool = False,
        confinement_root: str | None = None,
        confinement_device: int | None = None,
        confinement_inode: int | None = None,
    ) -> dict[str, Any]:
        """Incrementally transfer files in one explicit direction.

        ``download`` preserves the original S3-to-local behavior. ``upload``
        transfers a local file or directory into S3. Neither direction deletes
        destination-only files or objects. The confinement values form an
        internal MCP-to-CLI contract and are not public CLI options.
        """
        normalized_direction = direction.strip().lower()
        if normalized_direction not in {"download", "upload"}:
            raise ValueError("Sync direction must be either 'download' or 'upload'")

        contract = self._make_confinement_contract(
            confinement_root,
            confinement_device,
            confinement_inode,
        )
        bucket = self.resolve_bucket(alias, region=region)
        normalized_prefix = self._normalize_prefix(prefix)
        s3 = boto3.client("s3", region_name=bucket["region"])

        if contract is not None:
            with _PinnedRoot(contract) as confinement:
                local_parts = confinement.relative_parts(local_dir)
                local_path = confinement.display_path(local_parts)
                if normalized_direction == "upload":
                    return self._sync_upload(
                        bucket,
                        s3,
                        local_path,
                        normalized_prefix,
                        dry_run=dry_run,
                        force=force,
                        confinement=confinement,
                        source_parts=local_parts,
                    )
                return self._sync_download(
                    bucket,
                    s3,
                    local_path,
                    normalized_prefix,
                    dry_run=dry_run,
                    force=force,
                    confinement=confinement,
                    destination_parts=local_parts,
                )

        local_path = Path(local_dir).expanduser()
        if normalized_direction == "upload":
            return self._sync_upload(
                bucket,
                s3,
                local_path,
                normalized_prefix,
                dry_run=dry_run,
                force=force,
            )
        return self._sync_download(
            bucket,
            s3,
            local_path,
            normalized_prefix,
            dry_run=dry_run,
            force=force,
        )

    @staticmethod
    def _make_confinement_contract(
        root: str | None,
        device: int | None,
        inode: int | None,
    ) -> _ConfinementContract | None:
        supplied = (root is not None, device is not None, inode is not None)
        if not any(supplied):
            return None
        if not all(supplied) or not root:
            raise ValueError("The internal storage confinement contract is incomplete")
        if device is None or inode is None:  # pragma: no cover - narrowed by all()
            raise ValueError("The internal storage confinement identity is incomplete")
        if device < 0 or inode < 0:
            raise ValueError("The internal storage confinement identity is invalid")
        root_path = Path(root).expanduser()
        if not root_path.is_absolute() or Path(os.path.abspath(root_path)) != root_path:
            raise ValueError(
                "The internal storage confinement root must be normalized and absolute"
            )
        return _ConfinementContract(root=root_path, device=device, inode=inode)

    def _sync_download(
        self,
        bucket: dict[str, str],
        s3: Any,
        destination: Path,
        prefix: str,
        *,
        dry_run: bool,
        force: bool,
        confinement: _PinnedRoot | None = None,
        destination_parts: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Download the selected bucket prefix into a local directory."""
        if confinement is None:
            if destination.exists() and not destination.is_dir():
                raise NotADirectoryError(f"Sync destination is not a directory: {destination}")
            if not dry_run:
                destination.mkdir(parents=True, exist_ok=True)
            destination = destination.resolve()
        else:
            confinement.inspect_directory(destination_parts, create=not dry_run)

        objects, directory_markers = self._build_sync_plan(
            s3,
            bucket["bucket"],
            prefix,
            destination,
            force=force,
            confinement=confinement,
            destination_parts=destination_parts,
        )
        pending = [obj for obj in objects if not obj.current]
        current = [obj for obj in objects if obj.current]
        skipped = len(current)
        bytes_planned = sum(obj.size for obj in pending)
        downloaded = 0
        bytes_downloaded = 0

        if not dry_run:
            for obj in pending:
                try:
                    if confinement is not None:
                        confinement.download_object(s3, bucket["bucket"], obj)
                    else:
                        obj.destination.parent.mkdir(parents=True, exist_ok=True)
                        s3.download_file(bucket["bucket"], obj.key, str(obj.destination))
                        if obj.last_modified is not None:
                            timestamp = obj.last_modified.timestamp()
                            os.utime(obj.destination, (timestamp, timestamp))
                except Exception as exc:
                    raise RuntimeError(
                        "Sync did not complete: failed to download "
                        f"'s3://{bucket['bucket']}/{obj.key}' to "
                        f"'{obj.destination}': {exc}"
                    ) from exc
                downloaded += 1
                bytes_downloaded += obj.size

            for obj in current:
                if confinement is not None:
                    if obj.destination_parts is None:  # pragma: no cover - internal invariant
                        raise RuntimeError("Missing confined destination components")
                    still_current = confinement.download_target_is_current(
                        obj.destination_parts,
                        obj.size,
                        obj.last_modified,
                        evaluate_current=True,
                    )
                else:
                    still_current = self._is_current(
                        obj.destination,
                        obj.size,
                        obj.last_modified,
                    )
                if not still_current:
                    raise RuntimeError(
                        "Sync did not complete: a skipped local file changed after planning: "
                        f"{obj.destination}"
                    )

        source = f"s3://{bucket['bucket']}/{prefix}"
        return {
            "alias": bucket["alias"],
            "bucket": bucket["bucket"],
            "region": bucket["region"],
            "direction": "download",
            "source": source,
            "destination": str(destination),
            "prefix": prefix,
            "dry_run": dry_run,
            "force": force,
            "objects_scanned": len(objects) + directory_markers,
            "directory_markers": directory_markers,
            "files_planned": len(pending),
            "files_downloaded": downloaded,
            "files_skipped": skipped,
            "bytes_planned": bytes_planned,
            "bytes_downloaded": bytes_downloaded,
        }

    def _sync_upload(
        self,
        bucket: dict[str, str],
        s3: Any,
        source: Path,
        prefix: str,
        *,
        dry_run: bool,
        force: bool,
        confinement: _PinnedRoot | None = None,
        source_parts: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Upload a local file or directory into the selected bucket prefix."""
        if confinement is None:
            if source.is_symlink():
                raise ValueError(f"Upload source must not be a symbolic link: {source}")
            if not source.exists():
                raise FileNotFoundError(f"Upload source not found: {source}")
            if not source.is_file() and not source.is_dir():
                raise ValueError(f"Upload source must be a regular file or directory: {source}")
            source = source.resolve()

        objects, remote_objects_probed = self._build_upload_plan(
            s3,
            bucket["bucket"],
            prefix,
            source,
            force=force,
            confinement=confinement,
            source_parts=source_parts,
        )
        pending = [obj for obj in objects if not obj.current]
        current = [obj for obj in objects if obj.current]
        skipped = len(current)
        bytes_planned = sum(obj.size for obj in pending)
        uploaded = 0
        bytes_uploaded = 0

        if not dry_run:
            for obj in pending:
                try:
                    source_fd = self._open_upload_source(obj, confinement)
                    try:
                        if self._stat_signature(os.fstat(source_fd)) != obj.signature:
                            raise RuntimeError(f"Local file changed after planning: {obj.source}")
                        # Managed single-part uploads close their input stream. Keep the
                        # securely opened descriptor alive for the post-transfer signature
                        # check while allowing s3transfer to close its stream normally.
                        with open(source_fd, "rb", closefd=False) as source_file:
                            s3.upload_fileobj(
                                source_file,
                                bucket["bucket"],
                                obj.key,
                                ExtraArgs={
                                    "Metadata": {self._UPLOAD_DIGEST_METADATA: obj.sha256},
                                    "ChecksumSHA256": base64.b64encode(
                                        bytes.fromhex(obj.sha256)
                                    ).decode("ascii"),
                                },
                            )
                        if self._stat_signature(os.fstat(source_fd)) != obj.signature:
                            raise RuntimeError(f"Local file changed during upload: {obj.source}")
                    finally:
                        os.close(source_fd)
                    # A descriptor remains valid if its pathname is renamed away. Reopen
                    # after upload so a raced path replacement cannot be reported current.
                    self._verify_upload_source_signature(
                        obj,
                        confinement,
                        skipped=False,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "Sync did not complete: failed to upload "
                        f"'{obj.source}' to 's3://{bucket['bucket']}/{obj.key}': {exc}"
                    ) from exc
                uploaded += 1
                bytes_uploaded += obj.size

            for obj in current:
                source_fd = self._open_upload_source(obj, confinement)
                try:
                    if self._stat_signature(os.fstat(source_fd)) != obj.signature:
                        raise RuntimeError(
                            f"A skipped local file changed after planning: {obj.source}"
                        )
                    remote_objects_probed += 1
                    remote_current = self._remote_digest_matches(
                        s3,
                        bucket["bucket"],
                        obj.key,
                        obj.size,
                        obj.sha256,
                    )
                    if self._stat_signature(os.fstat(source_fd)) != obj.signature:
                        raise RuntimeError(
                            f"A skipped local file changed during revalidation: {obj.source}"
                        )
                finally:
                    os.close(source_fd)

                # Reopen after the remote probe so a renamed source path cannot
                # be reported current merely because its old descriptor is stable.
                self._verify_upload_source_signature(
                    obj,
                    confinement,
                    skipped=True,
                )
                if not remote_current:
                    raise RuntimeError(
                        "Sync did not complete: a skipped S3 object changed after planning: "
                        f"s3://{bucket['bucket']}/{obj.key}"
                    )

        destination = f"s3://{bucket['bucket']}/{prefix}"
        return {
            "alias": bucket["alias"],
            "bucket": bucket["bucket"],
            "region": bucket["region"],
            "direction": "upload",
            "source": str(source),
            "destination": destination,
            "prefix": prefix,
            "dry_run": dry_run,
            "force": force,
            "files_scanned": len(objects),
            "objects_scanned": len(objects),
            "objects_probed": remote_objects_probed,
            "files_planned": len(pending),
            "files_uploaded": uploaded,
            "files_skipped": skipped,
            "bytes_planned": bytes_planned,
            "bytes_uploaded": bytes_uploaded,
        }

    def _resolve_cluster_shared(self) -> dict[str, str]:
        from gco.services.aws_ssm import get_ssm_parameter_optional
        from gco.stacks.constants import cluster_shared_ssm_parameter_prefix

        prefix = cluster_shared_ssm_parameter_prefix(self.config.project_name)
        name = get_ssm_parameter_optional(
            f"{prefix}/name",
            region=self.config.global_region,
        )
        if not name:
            raise StorageBucketNotFoundError(
                "Cluster shared bucket not found. Deploy the global stack first."
            )
        bucket_region = get_ssm_parameter_optional(
            f"{prefix}/region",
            region=self.config.global_region,
        )
        return self._bucket_record(
            alias="cluster-shared",
            name=name,
            region=bucket_region or self.config.global_region,
            scope="global",
        )

    def _resolve_model_weights(self) -> dict[str, str]:
        from gco.services.aws_ssm import get_ssm_parameter_optional

        name = get_ssm_parameter_optional(
            f"/{self.config.project_name}/model-bucket-name",
            region=self.config.global_region,
        )
        if not name:
            raise StorageBucketNotFoundError(
                "Model weights bucket not found. Deploy the global stack first."
            )
        return self._bucket_record(
            alias="model-weights",
            name=name,
            region=self.config.global_region,
            scope="global",
        )

    def _resolve_regional_shared(self, region: str) -> dict[str, str]:
        from gco.services.aws_ssm import get_ssm_parameter_optional
        from gco.stacks.constants import regional_shared_ssm_parameter_prefix

        prefix = regional_shared_ssm_parameter_prefix(self.config.project_name)
        name = get_ssm_parameter_optional(f"{prefix}/name", region=region)
        if not name:
            raise StorageBucketNotFoundError(
                f"Regional shared bucket not found in region '{region}'. "
                "Deploy that region's stack first."
            )
        bucket_region = get_ssm_parameter_optional(f"{prefix}/region", region=region)
        return self._bucket_record(
            alias=f"regional-shared:{region}",
            name=name,
            region=bucket_region or region,
            scope="regional",
        )

    def _resolve_analytics_studio(self) -> dict[str, str]:
        region = self.config.api_gateway_region
        stack_name = f"{self.config.project_name}-analytics"
        cfn = boto3.client("cloudformation", region_name=region)
        token: str | None = None

        try:
            while True:
                kwargs: dict[str, str] = {"StackName": stack_name}
                if token:
                    kwargs["NextToken"] = token
                response = cfn.list_stack_resources(**kwargs)
                for resource in response.get("StackResourceSummaries", []):
                    logical_id = str(resource.get("LogicalResourceId", ""))
                    if resource.get("ResourceType") == "AWS::S3::Bucket" and logical_id.startswith(
                        "StudioOnlyBucket"
                    ):
                        name = resource.get("PhysicalResourceId")
                        if isinstance(name, str) and name:
                            return self._bucket_record(
                                alias="analytics-studio",
                                name=name,
                                region=region,
                                scope="analytics",
                            )
                token_value = response.get("NextToken")
                token = token_value if isinstance(token_value, str) else None
                if not token:
                    break
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") == "ValidationError" and "does not exist" in str(
                error.get("Message", "")
            ):
                raise StorageBucketNotFoundError(
                    "Analytics Studio bucket not found. Deploy the analytics stack first."
                ) from exc
            raise

        raise StorageBucketNotFoundError(
            "Analytics Studio bucket not found in the deployed analytics stack."
        )

    def _configured_regional_regions(self) -> list[str]:
        from .config import _load_cdk_json

        configured = _load_cdk_json().get("regional", [])
        candidates = configured if isinstance(configured, list) else []
        regions: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            if isinstance(value, str) and value and value not in seen:
                regions.append(value)
                seen.add(value)
        return regions or [self.config.default_region]

    def _infer_single_regional_region(self) -> str:
        regions = self._configured_regional_regions()
        if len(regions) == 1:
            return regions[0]
        choices = ", ".join(f"regional-shared:{item}" for item in regions)
        raise ValueError(
            "The 'regional-shared' alias is ambiguous across configured regions. "
            f"Use --region or one of: {choices}"
        )

    def _bucket_record(self, *, alias: str, name: str, region: str, scope: str) -> dict[str, str]:
        purpose_key = "regional-shared" if alias.startswith("regional-shared:") else alias
        return {
            "alias": alias,
            "bucket": name,
            "region": region,
            "scope": scope,
            "purpose": self._PURPOSES[purpose_key],
            "s3_uri": f"s3://{name}/",
        }

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        normalized = prefix.lstrip("/")
        if normalized and not normalized.endswith("/"):
            normalized += "/"
        return normalized

    @staticmethod
    def _stat_signature(value: os.stat_result) -> _FileSignature:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    @classmethod
    def _hash_upload_fd(cls, file_fd: int, path: Path) -> tuple[str, _FileSignature]:
        with os.fdopen(file_fd, "rb") as source_file:
            before = os.fstat(source_file.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"Upload source is not a regular file: {path}")
            before_signature = cls._stat_signature(before)
            digest = hashlib.sha256()
            while chunk := source_file.read(8 * 1024 * 1024):
                digest.update(chunk)
            after_signature = cls._stat_signature(os.fstat(source_file.fileno()))
        if before_signature != after_signature:
            raise RuntimeError(f"Local file changed while planning upload: {path}")
        return digest.hexdigest(), before_signature

    @classmethod
    def _hash_upload_file(cls, path: Path) -> tuple[str, _FileSignature]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        return cls._hash_upload_fd(os.open(path, flags), path)

    @staticmethod
    def _validate_upload_relative_path(relative: str, source: Path) -> None:
        parts = relative.split("/")
        if (
            not relative
            or "\x00" in relative
            or "\\" in relative
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError(f"Local path cannot be represented safely as an S3 key: {source}")

    @classmethod
    def _collect_upload_files(cls, source: Path) -> list[_PreparedUpload]:
        paths: list[tuple[Path, str]] = []
        if source.is_file():
            relative = source.name
            cls._validate_upload_relative_path(relative, source)
            paths.append((source, relative))
        else:

            def raise_walk_error(error: OSError) -> None:
                raise error

            for root, directories, names in os.walk(
                source,
                topdown=True,
                onerror=raise_walk_error,
                followlinks=False,
            ):
                root_path = Path(root)
                for directory_name in directories:
                    directory = root_path / directory_name
                    if directory.is_symlink():
                        raise ValueError(f"Upload source contains a symbolic link: {directory}")
                for name in names:
                    path = root_path / name
                    if path.is_symlink():
                        raise ValueError(f"Upload source contains a symbolic link: {path}")
                    if not path.is_file():
                        raise ValueError(f"Upload source contains a non-regular file: {path}")
                    relative = path.relative_to(source).as_posix()
                    cls._validate_upload_relative_path(relative, path)
                    paths.append((path, relative))

        prepared: list[_PreparedUpload] = []
        for path, relative in sorted(paths, key=lambda item: item[1]):
            digest, signature = cls._hash_upload_file(path)
            prepared.append(
                _PreparedUpload(
                    source=path,
                    relative=relative,
                    source_parts=None,
                    size=signature[2],
                    sha256=digest,
                    signature=signature,
                )
            )
        return prepared

    @classmethod
    def _collect_confined_upload_files(
        cls,
        confinement: _PinnedRoot,
        source_parts: tuple[str, ...],
    ) -> list[_PreparedUpload]:
        source = confinement.display_path(source_parts)
        source_stat = confinement.lstat(source_parts)
        if source_stat is None:
            raise FileNotFoundError(f"Upload source not found: {source}")
        if stat.S_ISLNK(source_stat.st_mode):
            raise ValueError(f"Upload source must not be a symbolic link: {source}")
        if stat.S_ISREG(source_stat.st_mode):
            relative = source.name
            cls._validate_upload_relative_path(relative, source)
            digest, signature = cls._hash_upload_fd(
                confinement.open_regular_file(source_parts),
                source,
            )
            return [
                _PreparedUpload(
                    source=source,
                    relative=relative,
                    source_parts=source_parts,
                    size=signature[2],
                    sha256=digest,
                    signature=signature,
                )
            ]
        if not stat.S_ISDIR(source_stat.st_mode):
            raise ValueError(f"Upload source must be a regular file or directory: {source}")

        source_fd = confinement.open_directory(source_parts)
        if source_fd is None:  # pragma: no cover - allow_missing is false
            raise FileNotFoundError(source)
        prepared: list[_PreparedUpload] = []
        try:
            cls._walk_confined_upload_directory(
                confinement,
                source_fd,
                source_parts,
                (),
                prepared,
            )
        finally:
            os.close(source_fd)
        return sorted(prepared, key=lambda item: item.relative)

    @classmethod
    def _walk_confined_upload_directory(
        cls,
        confinement: _PinnedRoot,
        directory_fd: int,
        directory_parts: tuple[str, ...],
        relative_parts: tuple[str, ...],
        prepared: list[_PreparedUpload],
    ) -> None:
        """Enumerate and open one source directory through already-pinned descriptors."""
        for name in sorted(os.listdir(directory_fd)):
            child_parts = directory_parts + (name,)
            child_relative_parts = relative_parts + (name,)
            child = confinement.display_path(child_parts)
            relative = "/".join(child_relative_parts)
            cls._validate_upload_relative_path(relative, child)
            try:
                child_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise RuntimeError(f"Upload source changed during enumeration: {child}") from exc

            if stat.S_ISLNK(child_stat.st_mode):
                raise ValueError(f"Upload source contains a symbolic link: {child}")
            if stat.S_ISDIR(child_stat.st_mode):
                child_fd = confinement.open_child_directory(directory_fd, name, child)
                try:
                    cls._walk_confined_upload_directory(
                        confinement,
                        child_fd,
                        child_parts,
                        child_relative_parts,
                        prepared,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise ValueError(f"Upload source contains a non-regular file: {child}")

            digest, signature = cls._hash_upload_fd(
                confinement.open_child_regular_file(directory_fd, name, child),
                child,
            )
            prepared.append(
                _PreparedUpload(
                    source=child,
                    relative=relative,
                    source_parts=child_parts,
                    size=signature[2],
                    sha256=digest,
                    signature=signature,
                )
            )

    @staticmethod
    def _open_upload_source(obj: _UploadObject, confinement: _PinnedRoot | None) -> int:
        if confinement is not None:
            if obj.source_parts is None:  # pragma: no cover - internal invariant
                raise RuntimeError("Missing confined upload source components")
            return confinement.open_regular_file(obj.source_parts)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        file_fd = os.open(obj.source, flags)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            raise ValueError(f"Upload source is not a regular file: {obj.source}")
        return file_fd

    @classmethod
    def _verify_upload_source_signature(
        cls,
        obj: _UploadObject,
        confinement: _PinnedRoot | None,
        *,
        skipped: bool,
    ) -> None:
        source_fd = cls._open_upload_source(obj, confinement)
        try:
            if cls._stat_signature(os.fstat(source_fd)) != obj.signature:
                description = "A skipped local file" if skipped else "Local file"
                raise RuntimeError(f"{description} changed after planning: {obj.source}")
        finally:
            os.close(source_fd)

    def _remote_digest_matches(
        self,
        s3: Any,
        bucket: str,
        key: str,
        size: int,
        digest: str,
    ) -> bool:
        try:
            response = s3.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error.get("Code") in {
                "403",
                "404",
                "AccessDenied",
                "NoSuchKey",
                "NotFound",
            } or status in {403, 404}:
                # S3 returns 403 rather than 404 for a missing key when the
                # caller intentionally lacks ListBucket. Treat it as not
                # current and let PutObject enforce write authorization.
                return False
            raise

        if int(response.get("ContentLength", -1)) != size:
            return False
        metadata = response.get("Metadata", {})
        if not isinstance(metadata, dict):
            return False
        remote_digest = next(
            (
                str(value)
                for name, value in metadata.items()
                if str(name).lower() == self._UPLOAD_DIGEST_METADATA
            ),
            "",
        )
        return remote_digest.strip().lower() == digest

    def _build_upload_plan(
        self,
        s3: Any,
        bucket: str,
        prefix: str,
        source: Path,
        *,
        force: bool,
        confinement: _PinnedRoot | None,
        source_parts: tuple[str, ...],
    ) -> tuple[list[_UploadObject], int]:
        if confinement is None:
            files = self._collect_upload_files(source)
        else:
            files = self._collect_confined_upload_files(confinement, source_parts)

        objects: list[_UploadObject] = []
        remote_objects_probed = 0
        for prepared in files:
            key = f"{prefix}{prepared.relative}"
            current = False
            if not force:
                remote_objects_probed += 1
                current = self._remote_digest_matches(
                    s3,
                    bucket,
                    key,
                    prepared.size,
                    prepared.sha256,
                )
            objects.append(
                _UploadObject(
                    source=prepared.source,
                    source_parts=prepared.source_parts,
                    key=key,
                    size=prepared.size,
                    sha256=prepared.sha256,
                    signature=prepared.signature,
                    current=current,
                )
            )

        return objects, remote_objects_probed

    def _build_sync_plan(
        self,
        s3: Any,
        bucket: str,
        prefix: str,
        destination: Path,
        *,
        force: bool,
        confinement: _PinnedRoot | None,
        destination_parts: tuple[str, ...],
    ) -> tuple[list[_SyncObject], int]:
        objects: list[_SyncObject] = []
        directory_markers = 0
        paginator = s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = str(item.get("Key", ""))
                if not key:
                    raise ValueError("S3 returned an object with an empty key")
                size = int(item.get("Size", 0))
                if key.endswith("/"):
                    if size != 0:
                        raise ValueError(
                            "S3 object keys ending in '/' cannot be represented as local files: "
                            f"{key!r}"
                        )
                    directory_markers += 1
                    continue

                relative_key = key[len(prefix) :] if prefix else key
                key_parts = self._download_relative_parts(relative_key, key)
                modified_value = item.get("LastModified")
                modified = modified_value if isinstance(modified_value, datetime) else None
                if confinement is None:
                    local_path = self._safe_local_path(destination, relative_key, key)
                    local_parts: tuple[str, ...] | None = None
                    current = False if force else self._is_current(local_path, size, modified)
                else:
                    local_parts = destination_parts + key_parts
                    local_path = confinement.display_path(local_parts)
                    current = confinement.download_target_is_current(
                        local_parts,
                        size,
                        modified,
                        evaluate_current=not force,
                    )
                objects.append(
                    _SyncObject(
                        key=key,
                        destination=local_path,
                        destination_parts=local_parts,
                        size=size,
                        last_modified=modified,
                        current=current,
                    )
                )

        self._validate_sync_plan(objects)
        return objects, directory_markers

    @staticmethod
    def _validate_windows_download_part(part: str, source_key: str) -> None:
        if not sys.platform.startswith("win"):
            return
        if part.endswith((".", " ")):
            raise ValueError(f"Unsafe Windows S3 object key cannot be synced: {source_key!r}")
        if any(
            unicodedata.category(character) == "Cc" or character in '<>:"|?*' for character in part
        ):
            raise ValueError(f"Unsafe Windows S3 object key cannot be synced: {source_key!r}")

        stem = part.split(".", 1)[0].rstrip(" .").casefold()
        reserved = {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
        numbered_suffixes = "123456789¹²³"
        if stem in reserved or (
            len(stem) == 4 and stem[:3] in {"com", "lpt"} and stem[3] in numbered_suffixes
        ):
            raise ValueError(f"Reserved Windows path cannot be synced: {source_key!r}")

    @classmethod
    def _download_relative_parts(cls, relative_key: str, source_key: str) -> tuple[str, ...]:
        if "\x00" in relative_key or "\\" in relative_key:
            raise ValueError(f"Unsafe S3 object key cannot be synced: {source_key!r}")
        parts = tuple(relative_key.split("/"))
        if not relative_key or any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"Unsafe S3 object key cannot be synced: {source_key!r}")
        for part in parts:
            cls._validate_windows_download_part(part, source_key)
        return parts

    @staticmethod
    def _local_collision_parts(path: Path) -> tuple[str, ...]:
        """Return path components normalized conservatively for the host filesystem."""
        if sys.platform.startswith("win"):
            return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)
        parts = tuple(os.path.normcase(part) for part in path.parts)
        if sys.platform == "darwin":
            # Default macOS volumes compare names case-insensitively and apply
            # Unicode normalization even though os.path.normcase is a no-op.
            return tuple(unicodedata.normalize("NFD", part).casefold() for part in parts)
        return parts

    @classmethod
    def _validate_sync_plan(cls, objects: list[_SyncObject]) -> None:
        """Reject object sets that cannot be represented without collisions."""
        seen: dict[tuple[str, ...], _SyncObject] = {}
        ordered = sorted(objects, key=lambda obj: len(obj.destination.parts))
        for obj in ordered:
            collision_key = cls._local_collision_parts(obj.destination)
            conflicting = seen.get(collision_key)
            if conflicting is not None:
                raise ValueError(
                    "S3 object keys map to the same local path: "
                    f"{conflicting.key!r} and {obj.key!r}"
                )
            for length in range(1, len(collision_key)):
                ancestor = seen.get(collision_key[:length])
                if ancestor is not None:
                    raise ValueError(
                        "S3 object keys have a local file/directory collision: "
                        f"{ancestor.key!r} and {obj.key!r}"
                    )
            seen[collision_key] = obj

    @classmethod
    def _safe_local_path(cls, destination: Path, relative_key: str, source_key: str) -> Path:
        parts = cls._download_relative_parts(relative_key, source_key)
        candidate = destination.joinpath(*parts).resolve()
        if candidate == destination or not candidate.is_relative_to(destination):
            raise ValueError(f"S3 object key escapes the sync destination: {source_key!r}")
        for parent in candidate.parents:
            if parent == destination:
                break
            if parent.exists() and not parent.is_dir():
                raise NotADirectoryError(
                    f"S3 object '{source_key}' has a local parent that is not a directory: {parent}"
                )
        if candidate.exists() and candidate.is_dir():
            raise IsADirectoryError(
                f"S3 object '{source_key}' maps to an existing directory: {candidate}"
            )
        return candidate

    @staticmethod
    def _is_current(path: Path, size: int, modified: datetime | None) -> bool:
        if not path.is_file() or modified is None:
            return False
        path_stat = path.stat()
        return path_stat.st_size == size and int(path_stat.st_mtime) >= int(modified.timestamp())


@dataclass(frozen=True)
class BucketDescriptor:
    """The deployment-contract facts about one bucket the stacks create.

    Everything here is a property of the *design* — which stack owns the
    bucket, what it is for, whether job pods can reach it, and what happens to
    it on teardown — so it is knowable without an AWS call. The physical name,
    ARN, and deployed/absent status are resolved separately at inventory time.

    Keeping the two apart is deliberate: an operator asking "what buckets does
    this deployment have and which can my pods write to?" gets a complete
    answer even for a region that has not been deployed yet, with each entry
    marked ``not-deployed`` rather than silently missing.
    """

    id: str
    role: str
    scope: str
    purpose: str
    pod_access: str
    discovery: str
    removal_policy: str
    logical_id_prefix: str
    sync_alias: str | None = None
    reserved_prefixes: tuple[str, ...] = ()
    opt_in: str | None = None


#: Every bucket the GCO stacks create, in reporting order. ``scope`` decides
#: which stack and region an entry resolves against; ``role`` separates the
#: buckets workloads use from the server-access-log sinks that exist only to
#: satisfy the "every bucket must log" control.
BUCKET_DESCRIPTORS: tuple[BucketDescriptor, ...] = (
    BucketDescriptor(
        id="cluster-shared",
        role="primary",
        scope="global",
        purpose="Always-on central bucket every regional cluster can read and write",
        pod_access="read-write",
        discovery="gco-cluster-shared-bucket ConfigMap (sharedBucketName) in gco-jobs/gco-system/gco-inference",
        removal_policy="destroy",
        logical_id_prefix="ClusterSharedBucket",
        sync_alias="cluster-shared",
        reserved_prefixes=("mlflow-artifacts/", "analytics-data/", "vector-corpus/"),
    ),
    BucketDescriptor(
        id="cluster-shared-access-logs",
        role="access-logs",
        scope="global",
        purpose="Server access logs for the cluster-shared bucket",
        pod_access="none",
        discovery="CloudFormation resource of the global stack (CDK-generated name)",
        removal_policy="destroy",
        logical_id_prefix="ClusterSharedAccessLogsBucket",
    ),
    BucketDescriptor(
        id="model-weights",
        role="primary",
        scope="global",
        purpose="Central model weights pulled by inference init containers",
        pod_access="read-only",
        discovery="SSM /<project>/model-bucket-name in the global region",
        removal_policy="destroy",
        logical_id_prefix="ModelWeightsBucket",
        sync_alias="model-weights",
    ),
    BucketDescriptor(
        id="model-weights-access-logs",
        role="access-logs",
        scope="global",
        purpose="Server access logs for the model weights bucket",
        pod_access="none",
        discovery="CloudFormation resource of the global stack (CDK-generated name)",
        removal_policy="destroy",
        logical_id_prefix="ModelWeightsAccessLogsBucket",
    ),
    BucketDescriptor(
        id="regional-shared",
        role="primary",
        scope="regional",
        purpose="Always-on general-purpose in-region bucket; no cross-region egress",
        pod_access="read-write",
        discovery="gco-regional-shared-bucket ConfigMap (regionalBucketName) in gco-jobs/gco-system/gco-inference",
        removal_policy="destroy",
        logical_id_prefix="RegionalSharedBucket",
        sync_alias="regional-shared",
        reserved_prefixes=("mooncake-kv/",),
    ),
    BucketDescriptor(
        id="regional-shared-access-logs",
        role="access-logs",
        scope="regional",
        purpose="Server access logs for that region's regional-shared bucket",
        pod_access="none",
        discovery="CloudFormation resource of the regional stack (CDK-generated name)",
        removal_policy="destroy",
        logical_id_prefix="RegionalSharedAccessLogsBucket",
    ),
    BucketDescriptor(
        id="cost-reports",
        role="primary",
        scope="monitoring",
        purpose="Hive-partitioned Parquet cost reports queried through Athena",
        pod_access="none",
        discovery="Deterministic name <project>-cost-reports-<account>-<monitoring-region>",
        removal_policy="destroy",
        logical_id_prefix="CostReportBucket",
        reserved_prefixes=("reports/", "adhoc/", "athena-results/"),
    ),
    BucketDescriptor(
        id="cost-reports-access-logs",
        role="access-logs",
        scope="monitoring",
        purpose="Server access logs for the cost report bucket",
        pod_access="none",
        discovery="CloudFormation resource of the monitoring stack (CDK-generated name)",
        removal_policy="destroy",
        logical_id_prefix="CostReportAccessLogsBucket",
    ),
    BucketDescriptor(
        id="analytics-studio",
        role="primary",
        scope="analytics",
        purpose="SageMaker Studio private scratch data and notebook outputs",
        pod_access="none",
        discovery="CloudFormation resource of the analytics stack (CDK-generated name)",
        removal_policy="destroy",
        logical_id_prefix="StudioOnlyBucket",
        sync_alias="analytics-studio",
        opt_in="analytics_environment.enabled",
    ),
    BucketDescriptor(
        id="analytics-studio-access-logs",
        role="access-logs",
        scope="analytics",
        purpose="Server access logs for the analytics Studio bucket",
        pod_access="none",
        discovery="CloudFormation resource of the analytics stack (CDK-generated name)",
        removal_policy="destroy",
        logical_id_prefix="AnalyticsAccessLogsBucket",
        opt_in="analytics_environment.enabled",
    ),
)


def get_storage_manager(config: GCOConfig | None = None) -> StorageManager:
    """Return a storage manager using the merged CLI configuration."""
    return StorageManager(config)
