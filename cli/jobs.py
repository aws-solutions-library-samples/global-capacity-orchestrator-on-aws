"""
Job management for GCO CLI.

Provides functionality to submit, query, and manage jobs across GCO clusters.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml

from gco.services.manifest_processor import TRAINJOB_API_VERSION, safe_load_all_yaml

from .aws_client import get_aws_client
from .config import GCOConfig, get_config

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-02T15:43:36Z
# Generated from Git commit: 61928c2ff509188fc812466fbd21fd76868efeae
# Flowchart(s) generated from this file:
#   * ``JobManager.submit_job`` -> ``diagrams/code_diagrams/cli/jobs.JobManager_submit_job.html``
#     (PNG: ``diagrams/code_diagrams/cli/jobs.JobManager_submit_job.png``)
#   * ``JobManager.submit_job_sqs`` -> ``diagrams/code_diagrams/cli/jobs.JobManager_submit_job_sqs.html``
#     (PNG: ``diagrams/code_diagrams/cli/jobs.JobManager_submit_job_sqs.png``)
#   * ``JobManager.get_job_logs`` -> ``diagrams/code_diagrams/cli/jobs.JobManager_get_job_logs.html``
#     (PNG: ``diagrams/code_diagrams/cli/jobs.JobManager_get_job_logs.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logger = __import__("logging").getLogger(__name__)


def _format_duration(seconds: int) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m{secs:02d}s"


def _first_manifest_namespace(manifests: list[dict[str, Any]]) -> str | None:
    """Return the first explicit ``metadata.namespace`` found in a manifest list.

    Used by the SQS submission path to populate the envelope ``namespace``
    field (informational — the queue processor reads each manifest's own
    namespace for validation). Returns None if no manifest declares one.
    """
    for manifest in manifests:
        ns = manifest.get("metadata", {}).get("namespace") if isinstance(manifest, dict) else None
        if ns:
            return str(ns)
    return None


def resolve_submission_identity(
    result: Any,
    *,
    fallback_name: str | None = None,
    fallback_namespace: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the submitted Job name and namespace from supported responses.

    API submissions return resource-status dictionaries, while direct kubectl
    submissions return a top-level ``job_name`` and a ``resources`` list of
    human-readable strings. Only mapping-shaped resources are inspected, so
    direct response lines can never be mistaken for response envelopes.
    """
    if not isinstance(result, Mapping):
        return fallback_name, fallback_namespace

    raw_resources = result.get("resources") or []
    if isinstance(raw_resources, Mapping):
        raw_resources = [raw_resources]
    resources = [resource for resource in raw_resources if isinstance(resource, Mapping)]
    job_resources = [
        resource for resource in resources if str(resource.get("kind", "")).lower() == "job"
    ]

    explicit_job_name = result.get("job_name")
    resource_with_name = next(
        (resource for resource in job_resources if resource.get("name")), None
    )
    job_name = (
        str(explicit_job_name)
        if explicit_job_name
        else str(resource_with_name.get("name"))
        if resource_with_name is not None
        else fallback_name
    )

    matching_resource = next(
        (resource for resource in job_resources if resource.get("name") == job_name),
        resource_with_name,
    )
    resource_namespace = matching_resource.get("namespace") if matching_resource else None
    envelope_namespace = result.get("namespace")
    namespace = (
        str(resource_namespace)
        if resource_namespace
        else str(envelope_namespace)
        if envelope_namespace
        else fallback_namespace
    )
    return job_name, namespace


def _extract_image_refs(spec: dict[str, Any]) -> list[str]:
    """Extract container image refs from a parsed Job spec.

    The API surface for a Job carries ``spec.template.spec.containers`` and
    ``spec.template.spec.initContainers`` lists, each entry of which has
    a ``name`` and an ``image`` URI. Returns an alphabetically-sorted,
    deduplicated list so the output is stable across calls — orphan-image
    cross-references rely on set equality.
    """
    refs: set[str] = set()
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    if not isinstance(pod_spec, dict):
        return []
    for key in ("containers", "initContainers"):
        items = pod_spec.get(key, [])
        if not isinstance(items, list):
            continue
        for entry in items:
            if not isinstance(entry, dict):
                continue
            image = entry.get("image")
            if isinstance(image, str) and image:
                refs.add(image)
    return sorted(refs)


def _extract_scheduling(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the node-placement block out of a job or pods API response.

    Absent on responses that predate the field (an older regional bridge) and
    on the list endpoint, which deliberately does not pay for a Node read per
    job. Both cases yield empty placement rather than an error: an absent
    instance type is honest, a guessed one is not.
    """
    scheduling = payload.get("scheduling")
    if not isinstance(scheduling, dict):
        return {}

    nodes = scheduling.get("nodes")
    labels = scheduling.get("node_labels")
    return {
        "node_name": scheduling.get("node_name"),
        "node_instance_type": scheduling.get("node_instance_type"),
        "node_capacity_type": scheduling.get("node_capacity_type"),
        "node_labels": dict(labels) if isinstance(labels, dict) else {},
        "nodes": list(nodes) if isinstance(nodes, list) else [],
    }


@dataclass
class JobInfo:
    """Information about a Kubernetes job."""

    name: str
    namespace: str
    region: str
    status: str  # "pending", "running", "succeeded", "failed"
    created_time: datetime | None = None
    start_time: datetime | None = None
    completion_time: datetime | None = None
    active_pods: int = 0
    succeeded_pods: int = 0
    failed_pods: int = 0
    parallelism: int = 1
    completions: int = 1
    labels: dict[str, str] = field(default_factory=dict)
    image_refs: list[str] = field(default_factory=list)

    # Where the job's pods actually landed. A job constrained to a *set* of
    # interchangeable instance types is placed by Karpenter within that set,
    # so the manifest records only what the run was authorized to use. These
    # record what it used, which is what cost reconciliation and
    # "did this fail on the smaller box?" both need.
    #
    # ``node_*`` describe the earliest-created scheduled pod; ``nodes`` lists
    # every node involved (a retried job can move between instance types) with
    # the pods on each. All are unset when nothing is scheduled yet, when the
    # pods have been garbage-collected, or on the list endpoint, which does not
    # pay for a Node read per job.
    node_name: str | None = None
    node_instance_type: str | None = None
    node_capacity_type: str | None = None
    node_labels: dict[str, str] = field(default_factory=dict)
    nodes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.status in ("succeeded", "failed")

    @property
    def duration_seconds(self) -> int | None:
        if self.start_time and self.completion_time:
            return int((self.completion_time - self.start_time).total_seconds())
        if self.start_time:
            return int((datetime.now(UTC) - self.start_time).total_seconds())
        return None


class JobManager:
    """
    Manages jobs across GCO clusters.

    Provides:
    - Job submission with region targeting
    - Job status queries across regions
    - Job logs retrieval
    - Job deletion
    """

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._aws_client = get_aws_client(self.config)

    def load_manifests(self, path: str) -> list[dict[str, Any]]:
        """
        Load Kubernetes manifests from a file or directory.

        Args:
            path: Path to YAML file or directory containing YAML files

        Returns:
            List of manifest dictionaries
        """
        manifests = []
        path_obj = Path(path)

        if path_obj.is_file():
            manifests.extend(self._load_yaml_file(path_obj))
        elif path_obj.is_dir():
            for yaml_file in sorted(path_obj.glob("*.yaml")):
                manifests.extend(self._load_yaml_file(yaml_file))
            for yaml_file in sorted(path_obj.glob("*.yml")):
                manifests.extend(self._load_yaml_file(yaml_file))
        else:
            raise FileNotFoundError(f"Path not found: {path}")

        return manifests

    def _load_yaml_file(self, path: Path) -> list[dict[str, Any]]:
        """Load manifests from a single YAML file."""
        with open(path, encoding="utf-8") as f:
            return safe_load_all_yaml(f, allow_aliases=False)

    def submit_job(
        self,
        manifests: str | list[dict[str, Any]],
        namespace: str | None = None,
        target_region: str | None = None,
        dry_run: bool = False,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Submit a job to GCO.

        Args:
            manifests: Path to manifest file/directory or list of manifest dicts
            namespace: Fallback namespace for manifests that don't declare
                their own. When set, each manifest's ``metadata.namespace`` is
                filled in only if missing — existing values are preserved so
                users who've declared a target namespace in the manifest can
                rely on it reaching the server untouched. Server-side
                validation enforces the allowlist.
            target_region: Force job to specific region
            dry_run: Validate without applying
            labels: Additional labels to add to manifests

        Returns:
            Submission result dictionary
        """
        # Load manifests if path provided
        manifest_list = self.load_manifests(manifests) if isinstance(manifests, str) else manifests

        # Apply the explicit or configured namespace as a fallback only —
        # preserve any namespace declared by an individual manifest.
        effective_namespace = namespace or self.config.default_namespace
        for manifest in manifest_list:
            if "metadata" not in manifest:
                manifest["metadata"] = {}
            manifest["metadata"].setdefault("namespace", effective_namespace)

        # Apply additional labels
        if labels:
            for manifest in manifest_list:
                if "metadata" not in manifest:
                    manifest["metadata"] = {}
                if "labels" not in manifest["metadata"]:
                    manifest["metadata"]["labels"] = {}
                manifest["metadata"]["labels"].update(labels)

        # Submit via API
        return self._aws_client.submit_manifests(
            manifests=manifest_list,
            namespace=effective_namespace,
            target_region=target_region,
            dry_run=dry_run,
        )

    def submit_job_direct(
        self,
        manifests: str | list[dict[str, Any]],
        region: str,
        namespace: str | None = None,
        dry_run: bool = False,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Submit a job directly to a regional cluster using kubectl.

        This bypasses the API Gateway and submits directly to the EKS cluster
        using kubectl. Requires:
        - kubectl installed and in PATH
        - EKS access entry configured for your IAM principal
        - AWS credentials with eks:DescribeCluster permission

        Args:
            manifests: Path to manifest file/directory or list of manifest dicts
            region: Target region for direct submission (required)
            namespace: Fallback namespace for manifests that don't declare
                their own. When set, each manifest's ``metadata.namespace`` is
                filled in only if missing — existing values are preserved so
                users who've declared a target namespace in the manifest can
                rely on it reaching ``kubectl apply`` untouched.
            dry_run: Validate without applying
            labels: Additional labels to add to manifests

        Returns:
            Submission result dictionary
        """
        import subprocess
        import tempfile
        import uuid

        # Load manifests if path provided
        manifest_list = self.load_manifests(manifests) if isinstance(manifests, str) else manifests

        # Apply the explicit or configured namespace as a fallback only —
        # preserve any namespace declared by an individual manifest.
        effective_namespace = namespace or self.config.default_namespace
        for manifest in manifest_list:
            if "metadata" not in manifest:
                manifest["metadata"] = {}
            manifest["metadata"].setdefault("namespace", effective_namespace)

        # Apply additional labels
        if labels:
            for manifest in manifest_list:
                if "metadata" not in manifest:
                    manifest["metadata"] = {}
                if "labels" not in manifest["metadata"]:
                    manifest["metadata"]["labels"] = {}
                manifest["metadata"]["labels"].update(labels)

        # Get cluster name from stack
        stack = self._aws_client.get_regional_stack(region)
        if not stack:
            raise ValueError(f"No GCO stack found in region {region}")

        cluster_name = stack.cluster_name

        # Update kubeconfig for the cluster
        from .kubectl_helpers import update_kubeconfig

        update_kubeconfig(cluster_name, region)

        # Handle existing Job resources before applying
        warnings: list[str] = []
        if not dry_run:
            for manifest in manifest_list:
                if manifest.get("kind") != "Job":
                    continue
                job_name = manifest.get("metadata", {}).get("name")
                job_ns = manifest.get("metadata", {}).get("namespace", effective_namespace)
                if not job_name:
                    continue

                existing_status = self._get_kubectl_job_status(job_name, job_ns)
                if existing_status is None:
                    # No existing job — nothing to do
                    continue

                if existing_status in ("complete", "failed"):
                    # Finished job — safe to delete and replace
                    subprocess.run(
                        ["kubectl", "delete", "job", job_name, "-n", job_ns],
                        capture_output=True,
                        text=True,
                    )
                else:
                    # Job is still active — auto-rename to avoid collision
                    suffix = uuid.uuid4().hex[:5]
                    new_name = f"{job_name}-{suffix}"
                    original_name = job_name
                    manifest["metadata"]["name"] = new_name
                    warnings.append(
                        f"Job '{original_name}' is still running in namespace "
                        f"'{job_ns}'. Renamed new submission to '{new_name}'."
                    )
                    logger.warning(
                        "Job %s is active in %s, renamed to %s",
                        original_name,
                        job_ns,
                        new_name,
                    )

        # Write manifests to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump_all(manifest_list, f)
            f.flush()  # Ensure content is written before using f.name
            temp_path = f.name  # nosemgrep: tempfile-without-flush

        try:
            # Build kubectl command
            kubectl_cmd = ["kubectl", "apply", "-f", temp_path]

            if dry_run:
                kubectl_cmd.extend(["--dry-run=client"])

            # Run kubectl apply
            result = subprocess.run(
                kubectl_cmd, capture_output=True, text=True
            )  # nosemgrep: dangerous-subprocess-use-audit - kubectl_cmd is a list ["kubectl","apply","-f",temp_path]; temp_path is a secure tempfile, not user input

            if result.returncode != 0:
                raise RuntimeError(f"kubectl apply failed: {result.stderr}")

            # Parse output to get job name
            output_lines = result.stdout.strip().split("\n")
            created_resources = []
            for line in output_lines:
                if line:
                    created_resources.append(line)

            # Get the actual Job identity from the submitted manifest. The
            # name may have been changed above to avoid an active-job collision,
            # and a manifest-declared namespace takes precedence over the CLI
            # fallback.
            job_name = None
            job_namespace = effective_namespace
            for manifest in manifest_list:
                if manifest.get("kind") == "Job":
                    metadata = manifest.get("metadata", {})
                    job_name = metadata.get("name")
                    job_namespace = metadata.get("namespace") or job_namespace
                    break

            response: dict[str, Any] = {
                "status": "success",
                "method": "kubectl",
                "cluster": cluster_name,
                "region": region,
                "namespace": job_namespace,
                "job_name": job_name,
                "dry_run": dry_run,
                "resources": created_resources,
                "output": result.stdout,
            }
            if warnings:
                response["warnings"] = warnings
            return response

        finally:
            # Clean up temp file
            import os

            os.unlink(temp_path)

    def _get_kubectl_job_status(self, job_name: str, namespace: str) -> str | None:
        """Check the status of an existing Job via kubectl.

        Returns:
            "complete", "failed", "active", or None if the job doesn't exist.
        """
        import json
        import subprocess

        result = subprocess.run(
            [
                "kubectl",
                "get",
                "job",
                job_name,
                "-n",
                namespace,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None  # Job doesn't exist

        try:
            job_data = json.loads(result.stdout)
        except json.JSONDecodeError, KeyError:
            return None

        conditions = job_data.get("status", {}).get("conditions") or []
        for condition in conditions:
            cond_type = condition.get("type", "")
            cond_status = condition.get("status", "")
            if cond_type == "Complete" and cond_status == "True":
                return "complete"
            if cond_type == "Failed" and cond_status == "True":
                return "failed"
        return "active"

    def list_jobs(
        self,
        region: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        all_regions: bool = False,
    ) -> list[JobInfo]:
        """
        List jobs across GCO clusters.

        Args:
            region: Specific region to query
            namespace: Filter by namespace
            status: Filter by status
            all_regions: Query all discovered regions

        Returns:
            List of JobInfo objects
        """
        jobs = []

        if all_regions:
            # Query all discovered regional stacks
            stacks = self._aws_client.discover_regional_stacks()
            for stack_region in stacks:
                try:
                    region_jobs = self._query_jobs_in_region(stack_region, namespace, status)
                    jobs.extend(region_jobs)
                except Exception as e:
                    logger.warning("Failed to query jobs in %s: %s", stack_region, e)
                    continue
        elif region:
            jobs = self._query_jobs_in_region(region, namespace, status)
        else:
            # Use default region
            jobs = self._query_jobs_in_region(self.config.default_region, namespace, status)

        return jobs

    def _query_jobs_in_region(
        self, region: str, namespace: str | None, status: str | None
    ) -> list[JobInfo]:
        """Query jobs in a specific region."""
        try:
            response = self._aws_client.get_jobs(region=region, namespace=namespace, status=status)

            jobs = []
            # response is a list, but we expect a dict with "jobs" key from the API
            job_list = response.get("jobs", []) if isinstance(response, dict) else response
            for job_data in job_list:
                jobs.append(self._parse_job_info(job_data, region))

            return jobs
        except Exception as exc:
            logger.warning("Failed to query jobs in %s: %s", region, exc)
            return []

    def _parse_job_info(self, job_data: dict[str, Any], region: str) -> JobInfo:
        """Parse job data into JobInfo object."""
        metadata = job_data.get("metadata", {})
        status_data = job_data.get("status", {})
        spec = job_data.get("spec", {})

        # Determine job status
        conditions = status_data.get("conditions", [])
        job_status = "pending"
        for condition in conditions:
            if condition.get("type") == "Complete" and condition.get("status") == "True":
                job_status = "succeeded"
                break
            if condition.get("type") == "Failed" and condition.get("status") == "True":
                job_status = "failed"
                break

        if job_status == "pending" and status_data.get("active", 0) > 0:
            job_status = "running"

        # Parse timestamps
        created_time = None
        if metadata.get("creationTimestamp"):
            created_time = datetime.fromisoformat(
                metadata["creationTimestamp"].replace("Z", "+00:00")
            )

        start_time = None
        if status_data.get("startTime"):
            start_time = datetime.fromisoformat(status_data["startTime"].replace("Z", "+00:00"))

        completion_time = None
        if status_data.get("completionTime"):
            completion_time = datetime.fromisoformat(
                status_data["completionTime"].replace("Z", "+00:00")
            )

        return JobInfo(
            name=metadata.get("name", ""),
            namespace=metadata.get("namespace", "default"),
            region=region,
            status=job_status,
            created_time=created_time,
            start_time=start_time,
            completion_time=completion_time,
            active_pods=status_data.get("active", 0),
            succeeded_pods=status_data.get("succeeded", 0),
            failed_pods=status_data.get("failed", 0),
            parallelism=spec.get("parallelism", 1),
            completions=spec.get("completions", 1),
            labels=metadata.get("labels", {}),
            image_refs=_extract_image_refs(spec),
            **_extract_scheduling(job_data),
        )

    def get_job(self, job_name: str, namespace: str, region: str | None = None) -> JobInfo | None:
        """
        Get detailed information about a specific job.

        A name the batch Job endpoint does not know (404) is retried as a
        Kubeflow TrainJob through the generic manifests endpoint, so
        ``gco jobs get`` / ``gco jobs submit --wait`` work unchanged for
        TrainJobs.

        ``None`` means the API confirmed the job does not exist (HTTP 404 on
        both lookups). Any other failure — an unreachable regional API bridge,
        an auth error, a 5xx — propagates to the caller so it is never
        misreported as "not found" (see issue #258).

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            JobInfo, or None if the API confirmed the job does not exist

        Raises:
            RuntimeError: If the regional API bridge is unreachable or the
                request fails for reasons other than the job being absent
            requests.exceptions.HTTPError: For non-404 HTTP responses
        """
        target_region = region or self.config.default_region
        try:
            response = self._aws_client.get_job_details(
                job_name=job_name, namespace=namespace, region=target_region
            )
            return self._parse_job_info(response, target_region)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # The batch Job endpoint confirmed the name is unknown; the
                # job may still exist as a Kubeflow TrainJob.
                trainjob = self._get_trainjob_info(job_name, namespace, target_region)
                if trainjob is not None:
                    return trainjob
                logger.debug("Job %s not found in %s/%s", job_name, target_region, namespace)
                return None
            # Non-404 responses (401/403/5xx) do not mean the job is absent.
            # Surface the real failure instead of collapsing it into None,
            # which the CLI would render as "not found".
            raise

    def _get_trainjob_info(self, job_name: str, namespace: str, region: str) -> JobInfo | None:
        """Fetch a TrainJob through the generic manifests endpoint, or None."""
        try:
            response = self._aws_client.call_api(
                "GET",
                f"/api/v1/manifests/{quote(namespace, safe='')}/{quote(job_name, safe='')}",
                region=region,
                params={"api_version": TRAINJOB_API_VERSION, "kind": "TrainJob"},
            )
        except RuntimeError as e:
            # 404 (no such TrainJob either) and transport errors both mean
            # "not resolvable as a TrainJob"; the caller reports not-found.
            logger.debug("TrainJob fallback lookup failed for %s: %s", job_name, e)
            return None
        resource = response.get("resource") or {}
        if not resource.get("exists", False):
            return None
        info = self._parse_trainjob_info(resource, region)
        self._attach_trainjob_scheduling(info, job_name, namespace, region)
        return info

    def _attach_trainjob_scheduling(
        self, info: JobInfo, job_name: str, namespace: str, region: str
    ) -> None:
        """Fill in node placement for a TrainJob from its child Job's pods.

        The manifests endpoint that resolves a TrainJob knows nothing about
        pods, so placement comes from the child Job ``<name>-node-0`` whose
        indexed pods are the node ranks (same convention the log fetch uses).
        Best effort: a TrainJob with no pods yet, or a bridge too old to report
        placement, simply leaves the fields unset.
        """
        try:
            response = self._aws_client.get_job_pods(f"{job_name}-node-0", namespace, region)
        except Exception as exc:
            logger.debug("No node placement available for TrainJob %s: %s", job_name, exc)
            return

        scheduling = _extract_scheduling(response if isinstance(response, dict) else {})
        for attr, value in scheduling.items():
            setattr(info, attr, value)

    def _parse_trainjob_info(self, resource: dict[str, Any], region: str) -> JobInfo:
        """Map a TrainJob resource payload onto JobInfo.

        TrainJob status carries ``conditions`` (terminal types ``Complete`` /
        ``Failed``, same strings as batch Jobs) plus per-child-Job counts in
        ``jobsStatus``; there is no ``startTime``/``completionTime``, so the
        terminal condition's ``lastTransitionTime`` stands in for completion.
        """
        metadata = resource.get("metadata", {}) or {}
        status_data = resource.get("status", {}) or {}
        spec = resource.get("spec", {}) or {}

        job_status = "pending"
        completion_time = None
        for condition in status_data.get("conditions", []) or []:
            if condition.get("status") != "True":
                continue
            if condition.get("type") == "Complete":
                job_status = "succeeded"
            elif condition.get("type") == "Failed":
                job_status = "failed"
            else:
                continue
            if condition.get("lastTransitionTime"):
                completion_time = datetime.fromisoformat(
                    condition["lastTransitionTime"].replace("Z", "+00:00")
                )
            break

        active = succeeded = failed = 0
        for child in status_data.get("jobsStatus", []) or []:
            active += child.get("active", 0) or 0
            succeeded += child.get("succeeded", 0) or 0
            failed += child.get("failed", 0) or 0
        if job_status == "pending" and active > 0:
            job_status = "running"

        created_time = None
        if metadata.get("creationTimestamp"):
            created_time = datetime.fromisoformat(
                metadata["creationTimestamp"].replace("Z", "+00:00")
            )

        trainer = spec.get("trainer", {}) or {}
        try:
            num_nodes = max(1, int(trainer.get("numNodes") or 1))
        except TypeError, ValueError:
            num_nodes = 1

        return JobInfo(
            name=metadata.get("name", ""),
            namespace=metadata.get("namespace", "default"),
            region=region,
            status=job_status,
            created_time=created_time,
            # TrainJob status reports no startTime; leave it unset rather
            # than fabricating one from creation time.
            completion_time=completion_time,
            active_pods=active,
            succeeded_pods=succeeded,
            failed_pods=failed,
            parallelism=num_nodes,
            completions=num_nodes,
            labels=metadata.get("labels", {}) or {},
            image_refs=sorted({trainer["image"]} if trainer.get("image") else set()),
        )

    def get_job_logs(
        self,
        job_name: str,
        namespace: str,
        region: str | None = None,
        tail_lines: int = 100,
        follow: bool = False,
        since_hours: int = 24,
        node: int = 0,
    ) -> str:
        """
        Get logs from a job.

        Tries the Kubernetes API first (via the GCO API). A name the batch
        Job endpoint does not know is retried as a Kubeflow TrainJob: the
        torch runtime runs the whole job as one child Job named
        ``<name>-node-0`` whose indexed pods are the node ranks, so the
        rank-``node`` pod's logs are fetched (rank 0 by default). If the pod
        is no longer available (completed/deleted), falls back to CloudWatch
        Logs where Container Insights stores application logs.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running
            tail_lines: Number of lines to return
            follow: Stream logs (not implemented yet)
            since_hours: Hours to look back in CloudWatch (default 24)
            node: Node rank to fetch for a distributed TrainJob (default 0)

        Returns:
            Log content as string
        """
        if follow:
            raise NotImplementedError("Log streaming not yet implemented")

        target_region = region or self.config.default_region

        try:
            return self._aws_client.get_job_logs(
                job_name=job_name,
                namespace=namespace,
                region=target_region,
                tail_lines=tail_lines,
            )
        except RuntimeError as e:
            error_msg = str(e)
            lowered = error_msg.lower()
            # An unknown batch Job name may be a TrainJob; its pods run
            # under the JobSet child Job instead.
            if "not found" in lowered:
                try:
                    return self._get_trainjob_node_logs(
                        job_name=job_name,
                        namespace=namespace,
                        region=target_region,
                        node=node,
                        tail_lines=tail_lines,
                    )
                except Exception as tj_err:
                    logger.debug("TrainJob logs fallback failed: %s", tj_err)
            # If the pod is gone or pending, try CloudWatch
            if any(
                hint in lowered
                for hint in ["not found", "pending", "no pods", "terminated", "completed"]
            ):
                logger.info("Pod not available, falling back to CloudWatch Logs")
                try:
                    return self._get_cloudwatch_logs(
                        job_name=job_name,
                        region=target_region,
                        tail_lines=tail_lines,
                        since_hours=since_hours,
                    )
                except Exception as cw_err:
                    logger.debug("CloudWatch fallback failed: %s", cw_err)
                    raise RuntimeError(
                        f"{error_msg}\n\n"
                        f"CloudWatch Logs fallback also failed: {cw_err}\n"
                        f"Tip: Container logs appear in CloudWatch within a few minutes. "
                        f"If the job just finished, try again shortly."
                    ) from e
            raise

    def _get_trainjob_node_logs(
        self,
        job_name: str,
        namespace: str,
        region: str,
        node: int,
        tail_lines: int,
    ) -> str:
        """Fetch logs from the rank-``node`` pod of a TrainJob.

        The shipped torch-distributed runtime materializes a TrainJob as a
        JobSet with a single replicated Job ``<name>-node-0`` running
        ``numNodes`` indexed pods; the completion index is the node rank
        (``PET_NODE_RANK``). Raises if no rank-``node`` pod exists.
        """
        child_job = f"{job_name}-node-0"
        pods_response = self._aws_client.get_job_pods(child_job, namespace, region)
        pods = pods_response.get("pods", []) or []
        if not pods:
            raise RuntimeError(f"no pods found for TrainJob child job '{child_job}'")

        target_pod = None
        for pod in pods:
            metadata = pod.get("metadata", {}) or {}
            labels = metadata.get("labels", {}) or {}
            if labels.get("batch.kubernetes.io/job-completion-index") == str(node):
                target_pod = metadata.get("name")
                break
        if target_pod is None:
            # Older control planes may omit the index label; fall back to the
            # deterministic indexed-pod name prefix '<child>-<index>-'.
            prefix = f"{child_job}-{node}-"
            for pod in pods:
                pod_name = (pod.get("metadata", {}) or {}).get("name", "")
                if pod_name.startswith(prefix):
                    target_pod = pod_name
                    break
        if target_pod is None:
            raise RuntimeError(
                f"no rank-{node} pod found for TrainJob '{job_name}' "
                f"({len(pods)} pod(s) under child job '{child_job}'); "
                f"list them with: gco jobs pods {child_job} -n {namespace} -r {region}"
            )

        logger.info(
            "Job '%s' resolved as TrainJob; fetching logs from rank-%d pod '%s'",
            job_name,
            node,
            target_pod,
        )
        response = self._aws_client.get_pod_logs(
            job_name=child_job,
            pod_name=target_pod,
            namespace=namespace,
            region=region,
            tail_lines=tail_lines,
        )
        return str(response.get("logs", ""))

    def _get_cloudwatch_logs(
        self,
        job_name: str,
        region: str,
        tail_lines: int = 100,
        since_hours: int = 24,
    ) -> str:
        """
        Fetch job logs from CloudWatch Logs (Container Insights).

        The CloudWatch Observability addon ships container stdout/stderr to:
          /aws/containerinsights/{cluster_name}/application

        Args:
            job_name: Name of the job (used to filter log streams)
            region: AWS region
            tail_lines: Number of log lines to return
            since_hours: Hours to look back (default 24)

        Returns:
            Log content as string
        """
        cluster_name = f"{self.config.project_name}-{region}"
        log_group = f"/aws/containerinsights/{cluster_name}/application"

        logs_client = self._aws_client._session.client("logs", region_name=region)

        import time

        now = int(time.time())
        start_time = now - (since_hours * 3600)

        query = (
            f"fields @timestamp, @message "
            f'| filter @logStream like "{job_name}" '
            f"| sort @timestamp asc "
            f"| limit {tail_lines}"
        )

        start_query = logs_client.start_query(
            logGroupName=log_group,
            startTime=start_time,
            endTime=now,
            queryString=query,
        )
        query_id = start_query["queryId"]

        # Poll for results (CloudWatch Insights is async)
        result = None
        for _ in range(30):  # up to 30 seconds
            time.sleep(1)
            result = logs_client.get_query_results(queryId=query_id)
            if result["status"] in ("Complete", "Failed", "Cancelled"):
                break

        if result is None or result["status"] != "Complete":
            status = result["status"] if result else "unknown"
            raise RuntimeError(
                f"CloudWatch Logs query did not complete (status: {status}). Try again in a moment."
            )

        if not result["results"]:
            raise RuntimeError(
                f"No logs found in CloudWatch for job '{job_name}' "
                f"in the last {since_hours} hours (log group: {log_group}). "
                f"Logs may take 1-2 minutes to appear after a pod runs. "
                f"Use --since to search further back, or check the job name "
                f"with: gco jobs list -r {region}"
            )

        # Extract log messages from results.
        # CloudWatch Container Insights wraps logs in a JSON envelope:
        #   {"time":"...","stream":"stdout","log":"actual message","kubernetes":{...}}
        # We parse out the "log" field for clean output, falling back to the
        # raw message if it's not JSON.
        import json as _json

        lines = []
        for row in result["results"]:
            for entry in row:
                if entry["field"] == "@message":
                    raw = entry["value"].rstrip()
                    try:
                        parsed = _json.loads(raw)
                        lines.append(parsed.get("log", raw).rstrip())
                    except ValueError, TypeError:
                        lines.append(raw)
                    break

        header = f"[CloudWatch Logs — {log_group}]\n"
        return header + "\n".join(lines)

    def delete_job(
        self,
        job_name: str,
        namespace: str,
        region: str | None = None,
        expected_uid: str | None = None,
    ) -> dict[str, Any]:
        """
        Delete a job.

        A name the batch Job endpoint does not know (404) is retried as a
        Kubeflow TrainJob through the generic manifests endpoint. Deleting
        the TrainJob cascades to its JobSet, child Jobs, and pods via owner
        references. ``expected_uid`` (a batch-Job concurrency guard) is not
        supported on the TrainJob path.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Deletion result
        """
        target_region = region or self.config.default_region
        try:
            return self._aws_client.delete_job(
                job_name=job_name,
                namespace=namespace,
                region=target_region,
                expected_uid=expected_uid,
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                trainjob_result = self._delete_trainjob(job_name, namespace, target_region)
                if trainjob_result is not None:
                    return trainjob_result
            raise

    def _delete_trainjob(self, job_name: str, namespace: str, region: str) -> dict[str, Any] | None:
        """Delete a TrainJob via the manifests endpoint, or None if that fails.

        Returning None (rather than raising) lets the caller surface the
        original batch-Job 404, which is the right error for a typo'd name.
        """
        try:
            return self._aws_client.call_api(
                "DELETE",
                f"/api/v1/manifests/{quote(namespace, safe='')}/{quote(job_name, safe='')}",
                region=region,
                params={"api_version": TRAINJOB_API_VERSION, "kind": "TrainJob"},
            )
        except RuntimeError as e:
            logger.debug("TrainJob delete fallback failed for %s: %s", job_name, e)
            return None

    def wait_for_job(
        self,
        job_name: str,
        namespace: str,
        region: str | None = None,
        timeout_seconds: int = 3600,
        poll_interval: int = 10,
        progress_callback: Callable[[JobInfo, int], None] | None = None,
    ) -> JobInfo:
        """
        Wait for a job to complete with progress reporting.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running
            timeout_seconds: Maximum time to wait
            poll_interval: Seconds between status checks
            progress_callback: Optional callable(JobInfo, elapsed_seconds) for progress updates.
                If None, a default stderr progress line is printed.

        Returns:
            Final JobInfo

        Raises:
            TimeoutError: If job doesn't complete within timeout
        """
        import sys
        import time

        start_time = time.time()

        while True:
            job = self.get_job(job_name, namespace, region)

            if job is None:
                raise ValueError(f"Job {job_name} not found in namespace {namespace}")

            elapsed = time.time() - start_time
            elapsed_str = _format_duration(int(elapsed))

            if job.is_complete:
                # Clear the progress line and return
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()
                return job

            # Build progress message
            pods_info = (
                f"{job.active_pods} active, {job.succeeded_pods}/{job.completions} succeeded"
            )
            if job.failed_pods:
                pods_info += f", {job.failed_pods} failed"

            status_line = f"  ⏳ {job.status.capitalize()} — {pods_info} — {elapsed_str} elapsed"

            if progress_callback:
                progress_callback(job, int(elapsed))
            else:
                # Overwrite the same line on stderr
                sys.stderr.write(f"\r\033[K{status_line}")
                sys.stderr.flush()

            if elapsed >= timeout_seconds:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()
                raise TimeoutError(
                    f"Job {job_name} did not complete within {timeout_seconds} seconds "
                    f"(last status: {job.status}, pods: {pods_info})"
                )

            time.sleep(poll_interval)  # nosemgrep: arbitrary-sleep - intentional polling delay

    def submit_job_sqs(
        self,
        manifests: str | list[dict[str, Any]],
        region: str,
        namespace: str | None = None,
        labels: dict[str, str] | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        """
        Submit a job to a regional SQS queue for processing.

        This is the recommended way to submit jobs as it:
        - Decouples submission from processing
        - Enables KEDA-based autoscaling
        - Provides better fault tolerance

        Args:
            manifests: Path to manifest file/directory or list of manifest dicts
            region: Target region for job submission (required)
            namespace: Fallback namespace for manifests that don't declare
                their own. When set, each manifest's ``metadata.namespace`` is
                filled in only if missing — existing values are preserved so
                users who've declared a target namespace in the manifest can
                rely on it reaching the queue processor untouched. Server-side
                validation enforces the allowlist.
            labels: Additional labels to add to manifests
            priority: Job priority (higher = more important)

        Returns:
            Submission result dictionary with message_id and queue info
        """
        import json
        import uuid

        import boto3

        # Load manifests if path provided
        manifest_list = self.load_manifests(manifests) if isinstance(manifests, str) else manifests

        # Apply namespace as a fallback only — preserve any namespace the
        # manifest declared itself.
        if namespace:
            for manifest in manifest_list:
                if "metadata" not in manifest:
                    manifest["metadata"] = {}
                manifest["metadata"].setdefault("namespace", namespace)

        # Apply additional labels
        if labels:
            for manifest in manifest_list:
                if "metadata" not in manifest:
                    manifest["metadata"] = {}
                if "labels" not in manifest["metadata"]:
                    manifest["metadata"]["labels"] = {}
                manifest["metadata"]["labels"].update(labels)

        # Get queue URL from stack
        stack = self._aws_client.get_regional_stack(region)
        if not stack:
            raise ValueError(f"No GCO stack found in region {region}")

        # Get queue URL from CloudFormation outputs
        cfn = boto3.client("cloudformation", region_name=region)
        response = cfn.describe_stacks(StackName=stack.stack_name)
        outputs = {
            o["OutputKey"]: o["OutputValue"] for o in response["Stacks"][0].get("Outputs", [])
        }
        queue_url = outputs.get("JobQueueUrl")

        if not queue_url:
            raise ValueError(f"Job queue not found in stack {stack.stack_name}")

        # Create SQS message. The ``namespace`` field in the envelope is
        # informational only — the queue processor reads each manifest's
        # own ``metadata.namespace`` for validation and application. Report
        # the first manifest's namespace here so the submission response
        # matches reality when the user doesn't pass ``--namespace``.
        job_id = str(uuid.uuid4())[:8]
        envelope_namespace = namespace or _first_manifest_namespace(manifest_list) or "gco-jobs"
        message_body = {
            "job_id": job_id,
            "manifests": manifest_list,
            "namespace": envelope_namespace,
            "priority": priority,
            "submitted_at": datetime.now(UTC).isoformat(),
        }

        # Send to SQS
        sqs = boto3.client("sqs", region_name=region)
        response = sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message_body),
            MessageAttributes={
                "Priority": {"DataType": "Number", "StringValue": str(priority)},
                "JobId": {"DataType": "String", "StringValue": job_id},
            },
        )

        # Get job name from first manifest
        job_name = None
        for manifest in manifest_list:
            if manifest.get("kind") == "Job":
                job_name = manifest.get("metadata", {}).get("name")
                break

        return {
            "status": "queued",
            "method": "sqs",
            "message_id": response["MessageId"],
            "job_id": job_id,
            "job_name": job_name,
            "queue_url": queue_url,
            "region": region,
            "namespace": envelope_namespace,
            "priority": priority,
        }

    def get_queue_status(self, region: str) -> dict[str, Any]:
        """
        Get the status of the job queue in a region.

        Args:
            region: AWS region

        Returns:
            Queue status including message counts
        """
        import boto3

        stack = self._aws_client.get_regional_stack(region)
        if not stack:
            raise ValueError(f"No GCO stack found in region {region}")

        # Get queue URLs from CloudFormation outputs
        cfn = boto3.client("cloudformation", region_name=region)
        response = cfn.describe_stacks(StackName=stack.stack_name)
        outputs = {
            o["OutputKey"]: o["OutputValue"] for o in response["Stacks"][0].get("Outputs", [])
        }

        queue_url = outputs.get("JobQueueUrl")
        dlq_url = outputs.get("JobDlqUrl")

        if not queue_url:
            raise ValueError(f"Job queue not found in stack {stack.stack_name}")

        sqs = boto3.client("sqs", region_name=region)

        # Get main queue attributes
        queue_attrs = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ],
        )["Attributes"]

        result = {
            "region": region,
            "queue_url": queue_url,
            "messages_available": int(queue_attrs.get("ApproximateNumberOfMessages", 0)),
            "messages_in_flight": int(queue_attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            "messages_delayed": int(queue_attrs.get("ApproximateNumberOfMessagesDelayed", 0)),
        }

        # Get DLQ attributes if available
        if dlq_url:
            dlq_attrs = sqs.get_queue_attributes(
                QueueUrl=dlq_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )["Attributes"]
            result["dlq_url"] = dlq_url
            result["dlq_messages"] = int(dlq_attrs.get("ApproximateNumberOfMessages", 0))

        return result

    def list_jobs_global(
        self,
        namespace: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        List jobs across all regions via the global API endpoint.

        This uses the cross-region aggregator Lambda to query all regional
        clusters in parallel and return a unified view.

        Args:
            namespace: Filter by namespace
            status: Filter by status
            limit: Maximum jobs to return

        Returns:
            Aggregated job list with region information
        """
        return self._aws_client.get_global_jobs(
            namespace=namespace,
            status=status,
            limit=limit,
        )

    def get_global_health(self) -> dict[str, Any]:
        """
        Get health status across all regions.

        Returns:
            Aggregated health status from all regional clusters
        """
        return self._aws_client.get_global_health()

    def get_global_status(self) -> dict[str, Any]:
        """
        Get cluster status across all regions.

        Returns:
            Aggregated status from all regional clusters
        """
        return self._aws_client.get_global_status()

    def bulk_delete_global(
        self,
        namespace: str | None = None,
        status: str | None = None,
        older_than_days: int | None = None,
        label_selector: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Bulk delete jobs across all regions.

        Args:
            namespace: Filter by namespace
            status: Filter by status
            older_than_days: Delete jobs older than N days
            label_selector: Kubernetes label selector
            dry_run: If True, only return what would be deleted

        Returns:
            Deletion results from all regions
        """
        return self._aws_client.bulk_delete_global(
            namespace=namespace,
            status=status,
            older_than_days=older_than_days,
            label_selector=label_selector,
            dry_run=dry_run,
        )

    def get_job_events(
        self,
        job_name: str,
        namespace: str,
        region: str | None = None,
    ) -> dict[str, Any]:
        """
        Get Kubernetes events for a job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Events related to the job
        """
        return self._aws_client.get_job_events(
            job_name=job_name,
            namespace=namespace,
            region=region or self.config.default_region,
        )

    def get_job_pods(
        self,
        job_name: str,
        namespace: str,
        region: str | None = None,
    ) -> dict[str, Any]:
        """
        Get pods for a job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Pod details for the job
        """
        return self._aws_client.get_job_pods(
            job_name=job_name,
            namespace=namespace,
            region=region or self.config.default_region,
        )

    def get_pod_logs(
        self,
        job_name: str,
        pod_name: str,
        namespace: str,
        region: str | None = None,
        tail_lines: int = 100,
        container: str | None = None,
    ) -> dict[str, Any]:
        """
        Get logs from a specific pod of a job.

        Args:
            job_name: Name of the job
            pod_name: Name of the pod
            namespace: Namespace of the job
            region: Region where the job is running
            tail_lines: Number of lines to return
            container: Container name (for multi-container pods)

        Returns:
            Pod logs response
        """
        return self._aws_client.get_pod_logs(
            job_name=job_name,
            pod_name=pod_name,
            namespace=namespace,
            region=region or self.config.default_region,
            tail_lines=tail_lines,
            container=container,
        )

    def get_job_metrics(
        self,
        job_name: str,
        namespace: str,
        region: str | None = None,
    ) -> dict[str, Any]:
        """
        Get resource metrics for a job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Resource usage metrics for the job's pods
        """
        return self._aws_client.get_job_metrics(
            job_name=job_name,
            namespace=namespace,
            region=region or self.config.default_region,
        )

    def retry_job(
        self,
        job_name: str,
        namespace: str,
        region: str | None = None,
    ) -> dict[str, Any]:
        """
        Retry a failed job.

        Creates a new job from the failed job's spec with a new name.

        Args:
            job_name: Name of the failed job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Result with new job name
        """
        return self._aws_client.retry_job(
            job_name=job_name,
            namespace=namespace,
            region=region or self.config.default_region,
        )

    def bulk_delete_jobs(
        self,
        namespace: str | None = None,
        status: str | None = None,
        older_than_days: int | None = None,
        label_selector: str | None = None,
        region: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Bulk delete jobs in a region.

        Args:
            namespace: Filter by namespace
            status: Filter by status
            older_than_days: Delete jobs older than N days
            label_selector: Kubernetes label selector
            region: Target region
            dry_run: If True, only return what would be deleted

        Returns:
            Deletion results
        """
        return self._aws_client.bulk_delete_jobs(
            namespace=namespace,
            status=status,
            older_than_days=older_than_days,
            label_selector=label_selector,
            region=region or self.config.default_region,
            dry_run=dry_run,
        )


def get_job_manager(config: GCOConfig | None = None) -> JobManager:
    """Get a configured job manager instance."""
    return JobManager(config)
