"""Submission, success-criteria, setup, and cleanup drivers for one example.

Every driver takes the parsed example plus the run plumbing and returns
evidence dictionaries for the report. Submission always travels the
DOCUMENTED path (the real ``gco`` CLI or ``kubectl apply``); any deliberate
manifest mutation (spec.mutations) is applied to a disclosed temp copy.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .kube import KubectlRunner
from .specs import (
    DAG_RUN,
    DEPLOYMENT_AVAILABLE,
    JOB_COMPLETES,
    KUBECTL_APPLY,
    RAYCLUSTER_READY,
    SCALEDJOB_SCALES,
    SUBMIT_API,
    SUBMIT_DIRECT,
    SUBMIT_SQS,
    VCJOB_COMPLETES,
)
from .static_checks import ParsedExample

_POLL_SECONDS = 15


class ExampleValidationError(RuntimeError):
    """One example failed its criteria; the message carries the evidence."""


@dataclass
class ExampleRunResult:
    """Evidence for one example's live validation."""

    name: str
    status: str  # passed | failed | skipped
    submission: str
    duration_seconds: float = 0.0
    detail: str = ""
    mutations: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "submission": self.submission,
            "duration_seconds": round(self.duration_seconds, 3),
            "detail": self.detail,
            "mutations": self.mutations,
            "evidence": self.evidence,
        }


def _run_cli(args: list[str], repo_root: Path, timeout: int = 600) -> tuple[int, str, str]:
    result = subprocess.run(args, cwd=repo_root, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def write_temp_manifest(documents: list[dict[str, Any]], suffix: str) -> Path:
    """Write documents to a private temp file and return its path."""
    fd, name = tempfile.mkstemp(suffix=suffix, text=True)
    with open(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump_all(documents, fh)
    return Path(name)


def apply_mutations(parsed: ParsedExample) -> tuple[Path, dict[str, str]]:
    """Materialize the manifest to submit: verbatim, or a disclosed mutated copy.

    Mutation keys use the shape ``Deployment.env.NAME`` (replace that env
    var's value) or ``Deployment.args.--flag`` (replace the argv element
    following the flag). Anything else is a spec bug and raises.
    """
    if not parsed.spec.mutations:
        return parsed.path, {}
    from .specs import REMOVE_VALUE

    documents = [dict(doc) for doc in parsed.documents]
    for key, replacement in parsed.spec.mutations.items():
        kind, channel, target = key.split(".", 2)
        for doc in documents:
            if doc.get("kind") != kind:
                continue
            containers = doc["spec"]["template"]["spec"]["containers"]
            for container in containers:
                if channel == "env":
                    env_entries = container.get("env", [])
                    if replacement == REMOVE_VALUE:
                        container["env"] = [
                            entry for entry in env_entries if entry.get("name") != target
                        ]
                        continue
                    for env_entry in env_entries:
                        if env_entry.get("name") == target:
                            env_entry["value"] = replacement
                elif channel == "args":
                    args = container.get("args", [])
                    for index, value in enumerate(args):
                        if value == target and index + 1 < len(args):
                            args[index + 1] = replacement
                else:
                    raise ValueError(f"Unsupported mutation channel in {key!r}")
    return (
        write_temp_manifest(documents, f"-{parsed.name}.yaml"),
        dict(parsed.spec.mutations),
    )


def submit_example(
    parsed: ParsedExample,
    manifest_path: Path,
    *,
    repo_root: Path,
    region: str,
    kubectl: KubectlRunner,
) -> dict[str, Any]:
    """Submit via the documented path; returns submission evidence."""
    spec = parsed.spec
    if spec.submission == SUBMIT_DIRECT:
        args = ["gco", "jobs", "submit-direct", str(manifest_path), "-r", region]
    elif spec.submission == SUBMIT_SQS:
        args = ["gco", "jobs", "submit-sqs", str(manifest_path), "--region", region]
    elif spec.submission == SUBMIT_API:
        args = ["gco", "jobs", "submit", str(manifest_path), "--region", region]
    elif spec.submission == DAG_RUN:
        args = ["gco", "dag", "run", str(manifest_path), "-r", region]
    elif spec.submission == KUBECTL_APPLY:
        code, out, err = kubectl("apply", "-f", str(manifest_path))
        if code != 0:
            raise ExampleValidationError(f"kubectl apply failed: {err.strip()[:800]}")
        return {"command": f"kubectl apply -f examples/{parsed.name}.yaml", "output": out.strip()}
    else:
        raise ExampleValidationError(f"No live submission for {spec.submission}")

    timeout = 1800 if spec.submission == DAG_RUN else 600
    code, out, err = _run_cli(args, repo_root, timeout=timeout)
    if code != 0:
        raise ExampleValidationError(
            f"{' '.join(args[:3])} failed (exit {code}): {(err or out).strip()[:800]}"
        )
    return {"command": " ".join(args[:3]) + f" examples/{parsed.name}.yaml", "output": out[-1500:]}


# --------------------------------------------------------------------------
# success criteria
# --------------------------------------------------------------------------


def _workload_documents(parsed: ParsedExample, kinds: set[str]) -> list[dict[str, Any]]:
    return [doc for doc in parsed.documents if doc.get("kind") in kinds]


def _job_status(kubectl: KubectlRunner, namespace: str, name: str) -> tuple[str, str]:
    code, out, _ = kubectl("get", "job", name, "-n", namespace, "-o", "json")
    if code != 0:
        return "missing", ""
    payload = json.loads(out)
    for condition in payload.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == "Complete" and condition.get("status") == "True":
            return "complete", ""
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return "failed", str(condition.get("message", ""))
    return "running", ""


def _pod_diagnostics(kubectl: KubectlRunner, namespace: str, selector: str) -> str:
    _, out, _ = kubectl(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        selector,
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}={.status.phase} {end}",
    )
    return out.strip()


def wait_jobs_complete(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    """Every batch/v1 Job in the example must reach Complete."""
    jobs = [
        ((doc.get("metadata") or {}).get("namespace", "gco-jobs"), doc["metadata"]["name"])
        for doc in _workload_documents(parsed, {"Job"})
    ]
    if not jobs:
        raise ExampleValidationError("spec says job-completes but the file defines no Jobs")
    deadline = time.monotonic() + timeout
    pending = dict.fromkeys(jobs, "unknown")
    while time.monotonic() < deadline:
        for namespace, name in jobs:
            state, message = _job_status(kubectl, namespace, name)
            pending[(namespace, name)] = state
            if state == "failed":
                _, logs, _ = kubectl(
                    "logs", f"job/{name}", "-n", namespace, "--tail", "40", timeout=60
                )
                raise ExampleValidationError(
                    f"Job {namespace}/{name} failed: {message} :: last logs: {logs[-800:]}"
                )
        if all(state == "complete" for state in pending.values()):
            return {"jobs": {f"{ns}/{name}": "complete" for (ns, name) in jobs}}
        time.sleep(_POLL_SECONDS)
    detail = ", ".join(f"{ns}/{name}={state}" for (ns, name), state in pending.items())
    pods = _pod_diagnostics(kubectl, jobs[0][0], f"job-name={jobs[0][1]}")
    raise ExampleValidationError(f"timeout after {timeout}s: {detail}; pods: {pods}")


def wait_deployment_available(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    """The Deployment must report Available and its Service must have endpoints."""
    deployments = _workload_documents(parsed, {"Deployment"})
    services = _workload_documents(parsed, {"Service"})
    if not deployments:
        raise ExampleValidationError("spec says deployment-available but no Deployment found")
    namespace = deployments[0]["metadata"].get("namespace", "gco-inference")
    name = deployments[0]["metadata"]["name"]
    code, _, err = kubectl(
        "wait",
        f"deployment/{name}",
        "-n",
        namespace,
        "--for",
        "condition=Available",
        f"--timeout={timeout}s",
        timeout=timeout + 60,
    )
    if code != 0:
        pods = _pod_diagnostics(kubectl, namespace, f"app={name}")
        _, describe, _ = kubectl("describe", f"deployment/{name}", "-n", namespace, timeout=60)
        raise ExampleValidationError(
            f"Deployment {namespace}/{name} never became Available: {err.strip()[:300]}; "
            f"pods: {pods}; describe tail: {describe[-600:]}"
        )
    evidence: dict[str, Any] = {"deployment": f"{namespace}/{name}=Available"}
    if services:
        service_name = services[0]["metadata"]["name"]
        _, endpoints, _ = kubectl(
            "get",
            "endpoints",
            service_name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.subsets[*].addresses[*].ip}",
        )
        if not endpoints.strip():
            raise ExampleValidationError(f"Service {namespace}/{service_name} has no endpoints")
        evidence["service_endpoints"] = endpoints.strip()
    return evidence


def wait_raycluster_ready(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    clusters = _workload_documents(parsed, {"RayCluster"})
    namespace = clusters[0]["metadata"].get("namespace", "gco-jobs")
    name = clusters[0]["metadata"]["name"]
    min_workers = int(clusters[0]["spec"]["workerGroupSpecs"][0].get("minReplicas", 1))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, out, _ = kubectl("get", "raycluster", name, "-n", namespace, "-o", "json")
        if code == 0:
            status = json.loads(out).get("status", {})
            state = str(status.get("state", ""))
            ready_workers = int(status.get("readyWorkerReplicas", 0) or 0)
            if state.lower() == "ready" and ready_workers >= min_workers:
                return {
                    "raycluster": f"{namespace}/{name}",
                    "state": state,
                    "ready_workers": ready_workers,
                }
        time.sleep(_POLL_SECONDS)
    _, describe, _ = kubectl("describe", "raycluster", name, "-n", namespace, timeout=60)
    raise ExampleValidationError(
        f"RayCluster {namespace}/{name} not ready after {timeout}s; tail: {describe[-600:]}"
    )


def wait_vcjob_completes(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    jobs = _workload_documents(parsed, {"Job"})
    volcano_jobs = [
        doc for doc in jobs if str(doc.get("apiVersion", "")).startswith("batch.volcano")
    ]
    namespace = volcano_jobs[0]["metadata"].get("namespace", "gco-jobs")
    name = volcano_jobs[0]["metadata"]["name"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, out, _ = kubectl("get", "vcjob", name, "-n", namespace, "-o", "json")
        if code == 0:
            phase = str(json.loads(out).get("status", {}).get("state", {}).get("phase", ""))
            if phase == "Completed":
                return {"vcjob": f"{namespace}/{name}", "phase": phase}
            if phase in {"Failed", "Aborted", "Terminated"}:
                raise ExampleValidationError(f"vcjob {namespace}/{name} reached phase {phase}")
        time.sleep(_POLL_SECONDS)
    raise ExampleValidationError(f"vcjob {namespace}/{name} did not complete within {timeout}s")


def wait_scaledjob_scales(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    """KEDA must spawn at least one Job for the ScaledJob from queue depth."""
    scaled = _workload_documents(parsed, {"ScaledJob"})
    namespace = scaled[0]["metadata"].get("namespace", "gco-jobs")
    name = scaled[0]["metadata"]["name"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, out, _ = kubectl(
            "get",
            "jobs",
            "-n",
            namespace,
            "-l",
            f"scaledjob.keda.sh/name={name}",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        )
        spawned = [item for item in out.split() if item]
        if code == 0 and spawned:
            return {"scaledjob": f"{namespace}/{name}", "spawned_jobs": spawned[:5]}
        time.sleep(_POLL_SECONDS)
    _, describe, _ = kubectl("describe", "scaledjob", name, "-n", namespace, timeout=60)
    raise ExampleValidationError(
        f"ScaledJob {namespace}/{name} spawned no Jobs within {timeout}s; tail: {describe[-600:]}"
    )


CRITERIA_WAITERS = {
    JOB_COMPLETES: wait_jobs_complete,
    DEPLOYMENT_AVAILABLE: wait_deployment_available,
    RAYCLUSTER_READY: wait_raycluster_ready,
    VCJOB_COMPLETES: wait_vcjob_completes,
    SCALEDJOB_SCALES: wait_scaledjob_scales,
}


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


def cleanup_example(
    parsed: ParsedExample, manifest_path: Path, kubectl: KubectlRunner
) -> dict[str, Any]:
    """Delete everything the example created and verify it is gone."""
    code, out, err = kubectl(
        "delete", "-f", str(manifest_path), "--ignore-not-found", "--wait=true", timeout=300
    )
    if code != 0:
        raise ExampleValidationError(f"cleanup failed for {parsed.name}: {err.strip()[:500]}")
    return {"deleted": [line for line in out.strip().splitlines() if line][:20]}


# --------------------------------------------------------------------------
# setup drivers (spec.setup_driver)
# --------------------------------------------------------------------------


@dataclass
class KedaDemoQueue:
    """Disposable SQS queue backing the KEDA scaling demonstration.

    Implements the example's documented prerequisites: a demo queue with
    synthetic messages and read-only queue-metric access for the KEDA
    operator (granted with a queue policy, never by touching IAM roles).
    """

    session: Any
    region: str
    run_id: str
    queue_url: str = ""
    queue_arn: str = ""

    def create(self, operator_role_arn: str) -> dict[str, Any]:
        sqs = self.session.client("sqs", region_name=self.region)
        name = f"gco-keda-demo-{self.run_id}"[:80]
        self.queue_url = sqs.create_queue(QueueName=name)["QueueUrl"]
        attrs = sqs.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=["QueueArn"])
        self.queue_arn = attrs["Attributes"]["QueueArn"]
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "KedaOperatorQueueMetrics",
                    "Effect": "Allow",
                    "Principal": {"AWS": operator_role_arn},
                    "Action": ["sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
                    "Resource": self.queue_arn,
                }
            ],
        }
        sqs.set_queue_attributes(QueueUrl=self.queue_url, Attributes={"Policy": json.dumps(policy)})
        for index in range(10):
            sqs.send_message(QueueUrl=self.queue_url, MessageBody=f"demo-{index}")
        return {"queue_arn": self.queue_arn, "seeded_messages": 10}

    def destroy(self) -> None:
        if self.queue_url:
            self.session.client("sqs", region_name=self.region).delete_queue(
                QueueUrl=self.queue_url
            )


TIMESLICING_CONFIGMAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "nvidia-device-plugin-config", "namespace": "kube-system"},
    "data": {
        "config.yaml": (
            "version: v1\n"
            "sharing:\n"
            "  timeSlicing:\n"
            "    renameByDefault: false\n"
            "    resources:\n"
            "      - name: nvidia.com/gpu\n"
            "        replicas: 4\n"
        )
    },
}
