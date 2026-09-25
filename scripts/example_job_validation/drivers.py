"""Submission, success-criteria, setup, and cleanup drivers for one example.

Every driver takes the parsed example plus the run plumbing and returns
evidence dictionaries for the report. Submission always travels the
DOCUMENTED path (the real ``gco`` CLI or ``kubectl apply``); any deliberate
manifest mutation (spec.mutations) is applied to a disclosed temp copy.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .kube import KubectlRunner, through_tunnel
from .specs import (
    ACK_RESOURCE_SYNCED,
    ARGOCD_APP_HEALTHY,
    COMPOSED_JOB_COMPLETES,
    DAG_RUN,
    DEPLOYMENT_AVAILABLE,
    JOB_COMPLETES,
    KUBECTL_APPLY,
    RAYCLUSTER_READY,
    SCALEDJOB_SCALES,
    SUBMIT_API,
    SUBMIT_DIRECT,
    SUBMIT_SQS,
    TRAINJOB_COMPLETES,
    VCJOB_COMPLETES,
)
from .static_checks import ParsedExample, application_source_documents

#: boto3 Session.client() is not thread-safe (client creation mutates shared
#: loader state); every client creation against the run's shared session must
#: hold this lock when examples run in parallel. The created clients ARE safe
#: to use concurrently.
BOTO_CLIENT_LOCK = threading.Lock()

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
    result = subprocess.run(
        args, cwd=repo_root, capture_output=True, text=True, timeout=timeout, check=False
    )
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

    def submit() -> tuple[int, str, str]:
        return _run_cli(args, repo_root, timeout=timeout)

    if spec.submission == SUBMIT_DIRECT:
        # submit-direct is the one submission that shells out to kubectl
        # through this session's tunnel (a live run lost five examples to a
        # stalled tunnel at exactly this step). It is repeated once the tunnel
        # is back, but only while none of the example's Jobs exists: the CLI
        # renames a second submission of a Job that is still running, which
        # would start a duplicate the harness never cleans up.
        code, out, err = through_tunnel(
            kubectl, submit, safe_to_repeat=lambda: _no_job_landed(parsed, kubectl)
        )
    else:
        code, out, err = submit()
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


def _no_job_landed(parsed: ParsedExample, kubectl: KubectlRunner) -> bool:
    """True when the API server answers NotFound for every Job the example defines."""
    return all(
        _job_status(
            kubectl,
            (doc.get("metadata") or {}).get("namespace", "gco-jobs"),
            doc["metadata"]["name"],
        )[0]
        == "missing"
        for doc in _workload_documents(parsed, {"Job"})
    )


def _job_status(kubectl: KubectlRunner, namespace: str, name: str) -> tuple[str, str]:
    """``(state, detail)``: complete, failed, running, missing, or unreachable.

    ``missing`` is only ever the API server answering NotFound. Any other
    failed read — a stalled tunnel, a timeout — is ``unreachable`` with
    kubectl's error, so a watcher keeps polling a Job it cannot see and a
    cleanup check never takes a read it could not make for a Job that is gone
    (a live run's watchers reported submitted Jobs as missing for 40 minutes
    while every read failed with ``TLS handshake timeout``).
    """
    code, out, err = kubectl("get", "job", name, "-n", namespace, "-o", "json")
    if code != 0:
        if "(NotFound)" in err:
            return "missing", ""
        return "unreachable", (err or out).strip()[:300]
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


def _job_admission_rejection(kubectl: KubectlRunner, namespace: str, name: str) -> str | None:
    """Return the rejection message when the Job's pods are forbidden.

    A LimitRange or ResourceQuota rejection never becomes a Job condition:
    the controller retries pod creation forever, the Job stays podless, and
    the only signal is ``FailedCreate ... forbidden`` namespace events.
    Waiting the full example timeout on such a job is pure burn (observed
    live: example-job validation run ex241-df723811, 40 minutes against the
    old per-container GPU ceiling) — surface the event message immediately.
    ResourceQuota rejections (``exceeded quota``) are the one retriable
    shape: under parallel example submission the namespace quota is
    transiently full, the Job controller retries pod creation, and the pods
    land once peers finish. Never fail fast on those; each message is
    evaluated separately so a transient quota event cannot mask a permanent
    LimitRange rejection emitted for the same Job.
    """
    code, out, _ = kubectl(
        "get",
        "events",
        "-n",
        namespace,
        "--field-selector",
        f"involvedObject.kind=Job,involvedObject.name={name},reason=FailedCreate",
        "-o",
        'jsonpath={range .items[*]}{.message}{"\\n"}{end}',
        timeout=60,
    )
    if code != 0:
        return None
    for message in out.splitlines():
        if "forbidden" in message and "exceeded quota" not in message:
            return message[-600:]
    return None


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
            pending[(namespace, name)] = f"{state} ({message})" if state == "unreachable" else state
            if state == "failed":
                _, logs, _ = kubectl(
                    "logs", f"job/{name}", "-n", namespace, "--tail", "40", timeout=60
                )
                raise ExampleValidationError(
                    f"Job {namespace}/{name} failed: {message} :: last logs: {logs[-800:]}"
                )
            rejection = _job_admission_rejection(kubectl, namespace, name)
            if rejection is not None:
                raise ExampleValidationError(
                    f"Job {namespace}/{name} pods are rejected at admission and can never "
                    f"run: {rejection}"
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


def wait_trainjob_completes(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    """Kubeflow TrainJob must reach condition Complete (Failed is terminal).

    Evidence carries the terminal condition plus the per-child-Job counts
    from status.jobsStatus so the report shows the gang actually ran
    (numNodes pods succeeded), not merely that a condition flipped.
    """
    trainjobs = _workload_documents(parsed, {"TrainJob"})
    namespace = trainjobs[0]["metadata"].get("namespace", "gco-jobs")
    name = trainjobs[0]["metadata"]["name"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, out, _ = kubectl("get", "trainjob", name, "-n", namespace, "-o", "json")
        if code == 0:
            status = json.loads(out).get("status", {}) or {}
            jobs_status = status.get("jobsStatus", []) or []
            for condition in status.get("conditions", []) or []:
                if condition.get("status") != "True":
                    continue
                if condition.get("type") == "Complete":
                    return {
                        "trainjob": f"{namespace}/{name}",
                        "condition": "Complete",
                        "jobsStatus": jobs_status,
                    }
                if condition.get("type") == "Failed":
                    raise ExampleValidationError(
                        f"TrainJob {namespace}/{name} reached condition Failed: "
                        f"{condition.get('message', '')}"
                    )
        time.sleep(_POLL_SECONDS)
    _, describe, _ = kubectl("describe", "trainjob", name, "-n", namespace, timeout=60)
    raise ExampleValidationError(
        f"TrainJob {namespace}/{name} did not complete within {timeout}s; tail: {describe[-600:]}"
    )


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


_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _conditions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    conditions = (payload.get("status") or {}).get("conditions") or []
    return [condition for condition in conditions if isinstance(condition, dict)]


def _condition_true(payload: dict[str, Any], condition_type: str) -> bool:
    return any(
        condition.get("type") == condition_type and condition.get("status") == "True"
        for condition in _conditions(payload)
    )


def _condition_summary(payload: dict[str, Any]) -> str:
    return "; ".join(
        " ".join(
            str(part)
            for part in (
                f"{condition.get('type')}={condition.get('status')}",
                condition.get("reason", ""),
                condition.get("message", ""),
            )
            if part
        )
        for condition in _conditions(payload)
    )[:600]


def _resource_ref(doc: dict[str, Any]) -> str:
    """``kind.group`` for kubectl (``batchjob.kro.run``, ``queue.sqs.services.k8s.aws``)."""
    group = str(doc.get("apiVersion", "")).rpartition("/")[0]
    kind = str(doc.get("kind", "")).lower()
    return f"{kind}.{group}" if group else kind


def _get_json(kubectl: KubectlRunner, *args: str) -> dict[str, Any] | None:
    code, out, _ = kubectl("get", *args, "-o", "json")
    return json.loads(out) if code == 0 else None


def wait_argocd_application_healthy(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    """Every Application must be Synced + Healthy, and every Job it syncs Complete.

    A 40-hex ``targetRevision`` (the harness pins the commit under validation)
    must equal the revision Argo CD reports it synced. The Jobs are read from
    the Application's Git path in this checkout and must appear among the
    resources Argo CD manages, so the evidence proves Argo CD applied them.
    """
    applications = _workload_documents(parsed, {"Application"})
    if not applications:
        raise ExampleValidationError(
            "spec says argocd-app-healthy but the file defines no Application"
        )
    repo_root = parsed.path.parent.parent
    deadline = time.monotonic() + timeout
    synced: dict[str, dict[str, Any]] = {}
    managed: set[tuple[str, str, str]] = set()
    for doc in applications:
        namespace = doc["metadata"].get("namespace", "argocd")
        name = doc["metadata"]["name"]
        last = "Application not found"
        while True:
            app = _get_json(kubectl, "application", name, "-n", namespace)
            if app is not None:
                status = app.get("status") or {}
                sync = status.get("sync") or {}
                health = status.get("health") or {}
                operation = status.get("operationState") or {}
                if operation.get("phase") in {"Failed", "Error"}:
                    raise ExampleValidationError(
                        f"Application {namespace}/{name} sync {operation['phase']}: "
                        f"{str(operation.get('message', ''))[:400]}; {_condition_summary(app)}"
                    )
                if health.get("status") == "Degraded":
                    raise ExampleValidationError(
                        f"Application {namespace}/{name} is Degraded: "
                        f"{str(health.get('message', ''))[:300]}; {_condition_summary(app)}"
                    )
                if sync.get("status") == "Synced" and health.get("status") == "Healthy":
                    target = str(
                        ((app.get("spec") or {}).get("source") or {}).get("targetRevision")
                    )
                    revision = str(sync.get("revision", ""))
                    if _SHA_RE.fullmatch(target) and revision != target:
                        raise ExampleValidationError(
                            f"Application {namespace}/{name} synced revision {revision!r}, "
                            f"not the pinned {target!r}"
                        )
                    synced[f"{namespace}/{name}"] = {"revision": revision, "resources": 0}
                    for resource in status.get("resources") or []:
                        managed.add(
                            (
                                str(resource.get("kind", "")),
                                str(resource.get("namespace", "")),
                                str(resource.get("name", "")),
                            )
                        )
                        synced[f"{namespace}/{name}"]["resources"] += 1
                    break
                last = (
                    f"sync={sync.get('status', '')} health={health.get('status', '')} "
                    f"operation={operation.get('phase', '')}; {_condition_summary(app)}"
                )
            if time.monotonic() >= deadline:
                raise ExampleValidationError(
                    f"Application {namespace}/{name} not Synced/Healthy within {timeout}s: {last}"
                )
            time.sleep(_POLL_SECONDS)
    jobs: dict[str, str] = {}
    for doc in applications:
        for item in application_source_documents(repo_root, doc):
            if item.get("kind") != "Job":
                continue
            namespace = item["metadata"]["namespace"]
            name = item["metadata"]["name"]
            if ("Job", namespace, name) not in managed:
                raise ExampleValidationError(
                    f"Job {namespace}/{name} from the Git path is not among the resources "
                    f"Argo CD manages for {doc['metadata']['name']}"
                )
            state, message = _job_status(kubectl, namespace, name)
            if state != "complete":
                raise ExampleValidationError(
                    f"Application is Healthy but Job {namespace}/{name} is {state} {message}".strip()
                )
            jobs[f"{namespace}/{name}"] = "complete"
    if not jobs:
        raise ExampleValidationError("the Application syncs no Job from this repository")
    return {"applications": synced, "jobs": jobs}


def _instance_summary(kubectl: KubectlRunner, doc: dict[str, Any], namespace: str) -> str:
    instance = _get_json(kubectl, _resource_ref(doc), doc["metadata"]["name"], "-n", namespace)
    if instance is None:
        return "instance not found"
    state = (instance.get("status") or {}).get("state")
    conditions = _condition_summary(instance)
    return " ".join(part for part in (f"state={state}" if state else "", conditions) if part) or (
        "no status yet"
    )


def wait_composed_jobs_complete(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    """Each instance's composed Job — named after the instance — must reach Complete.

    The example file holds only the instance (a kro or Crossplane custom
    resource), so the Job's existence already proves the composition ran; the
    instance's own state and conditions are reported with it (and on timeout).
    """
    instances = [doc for doc in parsed.documents if (doc.get("metadata") or {}).get("name")]
    if not instances:
        raise ExampleValidationError(
            "spec says composed-job-completes but the file defines nothing"
        )
    deadline = time.monotonic() + timeout
    results: dict[str, dict[str, str]] = {}
    for doc in instances:
        namespace = doc["metadata"].get("namespace", "gco-jobs")
        name = doc["metadata"]["name"]
        while True:
            state, message = _job_status(kubectl, namespace, name)
            if state == "complete":
                results[f"{namespace}/{name}"] = {
                    "job": "complete",
                    "instance": _instance_summary(kubectl, doc, namespace),
                }
                break
            if state == "failed":
                _, logs, _ = kubectl(
                    "logs", f"job/{name}", "-n", namespace, "--tail", "40", timeout=60
                )
                raise ExampleValidationError(
                    f"composed Job {namespace}/{name} failed: {message} :: last logs: {logs[-800:]}"
                )
            rejection = _job_admission_rejection(kubectl, namespace, name)
            if rejection is not None:
                raise ExampleValidationError(
                    f"composed Job {namespace}/{name} pods are rejected at admission: {rejection}"
                )
            if time.monotonic() >= deadline:
                raise ExampleValidationError(
                    f"composed Job {namespace}/{name} is {state} after {timeout}s; "
                    f"{_resource_ref(doc)}: {_instance_summary(kubectl, doc, namespace)}"
                )
            time.sleep(_POLL_SECONDS)
    return {"composed_jobs": results}


def wait_ack_resources_synced(
    parsed: ParsedExample, kubectl: KubectlRunner, *, timeout: int
) -> dict[str, Any]:
    """Every ACK resource must report ``ACK.ResourceSynced=True`` (``ACK.Terminal`` fails fast).

    ACK sets ResourceSynced only after reading the resource back from AWS, so
    the evidence (ARN, URL fields) is AWS's own answer. ``ACK.Recoverable``
    (an access denial, say) keeps the wait going and is reported on timeout.
    """
    resources = [
        doc for doc in parsed.documents if ".services.k8s.aws/" in str(doc.get("apiVersion", ""))
    ]
    if not resources:
        raise ExampleValidationError("spec says ack-resource-synced but the file has no ACK kind")
    deadline = time.monotonic() + timeout
    results: dict[str, dict[str, Any]] = {}
    for doc in resources:
        namespace = doc["metadata"].get("namespace", "gco-jobs")
        name = doc["metadata"]["name"]
        ref = _resource_ref(doc)
        last = "not found"
        while True:
            payload = _get_json(kubectl, ref, name, "-n", namespace)
            if payload is not None:
                if _condition_true(payload, "ACK.Terminal"):
                    raise ExampleValidationError(
                        f"{ref} {namespace}/{name} is terminal: {_condition_summary(payload)}"
                    )
                if _condition_true(payload, "ACK.ResourceSynced"):
                    status = payload.get("status") or {}
                    results[f"{namespace}/{name}"] = {
                        "arn": str((status.get("ackResourceMetadata") or {}).get("arn", "")),
                        **{
                            key: value
                            for key, value in status.items()
                            if key.lower().endswith("url") and isinstance(value, str)
                        },
                    }
                    break
                last = _condition_summary(payload) or "no conditions yet"
            if time.monotonic() >= deadline:
                raise ExampleValidationError(
                    f"{ref} {namespace}/{name} not synced within {timeout}s: {last}"
                )
            time.sleep(_POLL_SECONDS)
    return {"ack_resources": results}


CRITERIA_WAITERS = {
    JOB_COMPLETES: wait_jobs_complete,
    DEPLOYMENT_AVAILABLE: wait_deployment_available,
    RAYCLUSTER_READY: wait_raycluster_ready,
    VCJOB_COMPLETES: wait_vcjob_completes,
    SCALEDJOB_SCALES: wait_scaledjob_scales,
    TRAINJOB_COMPLETES: wait_trainjob_completes,
    ARGOCD_APP_HEALTHY: wait_argocd_application_healthy,
    COMPOSED_JOB_COMPLETES: wait_composed_jobs_complete,
    ACK_RESOURCE_SYNCED: wait_ack_resources_synced,
}


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


def cleanup_example(
    parsed: ParsedExample, manifest_path: Path, kubectl: KubectlRunner
) -> dict[str, Any]:
    """Delete everything the example created and verify it is gone."""
    if parsed.spec.submission == DAG_RUN:
        # A DAG example's file is a pipeline SPEC, not a Kubernetes manifest
        # (kubectl cannot decode it — observed live in run ex241-4bf01801);
        # what actually ran on the cluster are the step manifests it names.
        repo_root = parsed.path.parent.parent
        deleted: list[str] = []
        for document in parsed.documents:
            for step in document.get("steps", []):
                step_manifest = repo_root / str(step.get("manifest", ""))
                code, out, err = kubectl(
                    "delete",
                    "-f",
                    str(step_manifest),
                    "--ignore-not-found",
                    "--wait=true",
                    timeout=300,
                )
                if code != 0:
                    raise ExampleValidationError(
                        f"cleanup failed for {parsed.name} step "
                        f"{step.get('name', '?')}: {err.strip()[:500]}"
                    )
                deleted.extend(line for line in out.strip().splitlines() if line)
        return {"deleted": deleted[:20]}
    code, out, err = kubectl(
        "delete", "-f", str(manifest_path), "--ignore-not-found", "--wait=true", timeout=300
    )
    if code != 0:
        raise ExampleValidationError(f"cleanup failed for {parsed.name}: {err.strip()[:500]}")
    result: dict[str, Any] = {"deleted": [line for line in out.strip().splitlines() if line][:20]}
    derived = _derived_jobs(parsed)
    if derived:
        result["derived_jobs_removed"] = _wait_jobs_gone(kubectl, derived, timeout=180)
    return result


def _derived_jobs(parsed: ParsedExample) -> list[tuple[str, str]]:
    """Jobs the example creates indirectly: synced by Argo CD or composed by kro/Crossplane.

    Deleting the example's own objects deletes these asynchronously (the
    Application's resources finalizer, the instance's composition), so cleanup
    waits for them too: a lingering Job would hold namespace quota.
    """
    if parsed.spec.criteria == ARGOCD_APP_HEALTHY:
        repo_root = parsed.path.parent.parent
        return [
            (item["metadata"]["namespace"], item["metadata"]["name"])
            for doc in _workload_documents(parsed, {"Application"})
            for item in application_source_documents(repo_root, doc)
            if item.get("kind") == "Job"
        ]
    if parsed.spec.criteria == COMPOSED_JOB_COMPLETES:
        return [
            ((doc.get("metadata") or {}).get("namespace", "gco-jobs"), doc["metadata"]["name"])
            for doc in parsed.documents
            if (doc.get("metadata") or {}).get("name")
        ]
    return []


def _wait_jobs_gone(
    kubectl: KubectlRunner, jobs: list[tuple[str, str]], *, timeout: int
) -> list[str]:
    deadline = time.monotonic() + timeout
    remaining = list(jobs)
    while True:
        states = {job: _job_status(kubectl, *job)[0] for job in remaining}
        remaining = [job for job in remaining if states[job] != "missing"]
        if not remaining:
            return [f"{namespace}/{name}" for namespace, name in jobs]
        if time.monotonic() >= deadline:
            # "unreachable" is named as such: a Job the reads could not see
            # is not proven gone, and it is not proven present either.
            raise ExampleValidationError(
                "derived Job(s) still present after cleanup: "
                + ", ".join(
                    f"{namespace}/{name} ({states[(namespace, name)]})"
                    for namespace, name in remaining
                )
            )
        time.sleep(_POLL_SECONDS)


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

    def _sqs(self) -> Any:
        # boto3 Session.client() is not thread-safe; examples may run in
        # parallel threads. The returned client is safe to use concurrently.
        with BOTO_CLIENT_LOCK:
            return self.session.client("sqs", region_name=self.region)

    def create(self, operator_role_arn: str) -> dict[str, Any]:
        sqs = self._sqs()
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
            self._sqs().delete_queue(QueueUrl=self.queue_url)


@dataclass
class VectorDemoCorpus:
    """Demo-corpus precondition for the vector-search example, fully reverted.

    ``create`` runs the example's documented prerequisite verbatim —
    ``gco vector ingest --demo --wait`` — and records exactly which corpus
    objects the CLI uploaded. ``destroy`` reverts precisely those: the S3
    objects (whose upload is what triggered ingestion) and every DynamoDB
    chunk item whose ``source`` is one of the recorded keys, resolved with
    the same filtered-Scan shape the CLI's ingest wait uses (the table has
    no by-source key schema; corpora are document-scale). Scoping deletion
    to the recorded keys means a pre-existing user corpus in the same table
    is never touched.
    """

    repo_root: Path
    session: Any
    region: str
    uploaded: list[str] = field(default_factory=list)
    bucket: str = ""

    def create(self) -> dict[str, Any]:
        code, out, err = _run_cli(
            ["gco", "vector", "ingest", "--demo", "--wait", "--output", "json"],
            self.repo_root,
            timeout=900,
        )
        if code != 0:
            raise ExampleValidationError(
                f"gco vector ingest --demo --wait failed (exit {code}): "
                f"{(err or out).strip()[:800]}"
            )
        try:
            summary = json.loads(out)
        except ValueError as exc:
            raise ExampleValidationError(
                f"gco vector ingest emitted non-JSON output: {out[:400]}"
            ) from exc
        self.bucket = str(summary.get("bucket", ""))
        self.uploaded = [str(key) for key in summary.get("uploaded", [])]
        if not self.bucket or not self.uploaded:
            raise ExampleValidationError(
                f"ingest summary carried no bucket/keys to revert later: {out[:400]}"
            )
        return {
            "command": "gco vector ingest --demo --wait",
            "bucket": self.bucket,
            "uploaded": self.uploaded,
            "chunks_by_source": summary.get("chunks_by_source", {}),
        }

    def destroy(self) -> None:
        if not self.uploaded:
            return
        from cli.vector_store import VectorStoreClient

        client = VectorStoreClient(query_region=self.region)
        table_name = client._resolve_table_name()
        bucket_name, bucket_region = client._resolve_bucket()
        if bucket_name != self.bucket:
            # Fail loudly rather than delete from a bucket other than the
            # one create() actually uploaded to.
            raise ExampleValidationError(
                f"corpus bucket changed between ingest ({self.bucket}) and "
                f"revert ({bucket_name}); refusing to delete"
            )
        with BOTO_CLIENT_LOCK:
            dynamodb = self.session.client("dynamodb", region_name=self.region)
            s3 = self.session.client("s3", region_name=bucket_region)

        # Chunk items first (their source keys reference the S3 objects),
        # then the objects themselves. Deleting the objects does NOT
        # un-ingest — the notification only fires on creates — hence the
        # explicit item sweep.
        for key in self.uploaded:
            doc_ids: list[str] = []
            scan_kwargs: dict[str, Any] = {
                "TableName": table_name,
                "FilterExpression": "#source = :source",
                "ExpressionAttributeNames": {"#source": "source"},
                "ExpressionAttributeValues": {":source": {"S": key}},
                "ProjectionExpression": "doc_id",
            }
            while True:
                page = dynamodb.scan(**scan_kwargs)
                doc_ids.extend(
                    item["doc_id"]["S"] for item in page.get("Items", []) if "doc_id" in item
                )
                last_key = page.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
            for start in range(0, len(doc_ids), 25):
                batch = doc_ids[start : start + 25]
                dynamodb.batch_write_item(
                    RequestItems={
                        table_name: [
                            {"DeleteRequest": {"Key": {"doc_id": {"S": doc_id}}}}
                            for doc_id in batch
                        ]
                    }
                )
            s3.delete_object(Bucket=bucket_name, Key=key)


def wait_trainer_runtime_ready(kubectl: KubectlRunner, *, timeout: int = 300) -> dict[str, Any]:
    """Wait until the TrainJob CRD is served and the shipped runtime exists.

    The trainer chart installs the CRDs and controller; the post-Helm
    kubectl pass applies the torch-distributed ClusterTrainingRuntime the
    example's ``runtimeRef`` names. Both are deploy-time artifacts, so this
    is a readiness wait, not created state — nothing to revert.
    """
    deadline = time.monotonic() + timeout
    last_error = ""
    while True:
        code, _, err = kubectl("get", "crd", "trainjobs.trainer.kubeflow.org")
        if code != 0:
            last_error = f"TrainJob CRD not present: {err.strip()[:300]}"
        else:
            code, out, err = kubectl(
                "get", "clustertrainingruntime", "torch-distributed", "-o", "json"
            )
            if code == 0:
                runtime = json.loads(out)
                return {
                    "crd": "trainjobs.trainer.kubeflow.org",
                    "runtime": runtime["metadata"]["name"],
                    "runtime_created": runtime["metadata"].get("creationTimestamp", ""),
                }
            last_error = f"torch-distributed runtime not present: {err.strip()[:300]}"
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SECONDS)
    raise ExampleValidationError(
        f"Kubeflow Trainer runtime not ready within {timeout}s — is "
        f"helm.kubeflow_trainer enabled? Last error: {last_error}"
    )


def wait_mlflow_ready(kubectl: KubectlRunner, *, timeout: int = 600) -> dict[str, Any]:
    """Wait until the MLflow tracking server Deployment is Available.

    The example's client job fails its read-back (or hangs on connect) if
    it races the server's first rollout — the backend PVC arrives one
    applier pass after the chart on a fresh install. Readiness wait only;
    nothing to revert.
    """
    deadline = time.monotonic() + timeout
    last_state = ""
    while True:
        code, out, err = kubectl("get", "deployment", "mlflow", "-n", "monitoring", "-o", "json")
        if code != 0:
            last_state = f"mlflow Deployment not found: {err.strip()[:300]}"
        else:
            payload = json.loads(out)
            conditions = payload.get("status", {}).get("conditions", []) or []
            available = any(
                condition.get("type") == "Available" and condition.get("status") == "True"
                for condition in conditions
            )
            if available:
                return {
                    "deployment": "monitoring/mlflow",
                    "ready_replicas": payload.get("status", {}).get("readyReplicas", 0),
                }
            last_state = f"conditions: {json.dumps(conditions)[:400]}"
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SECONDS)
    raise ExampleValidationError(
        f"MLflow tracking server not Available within {timeout}s — is "
        f"cluster_observability.mlflow enabled? Last state: {last_state}"
    )


def _wait_until(probe: Any, *, deadline: float, what: str, hint: str = "") -> Any:
    """Poll ``probe()`` (returns a truthy result or a falsy one plus state) until the deadline."""
    last = ""
    while True:
        result, state = probe()
        if result:
            return result
        last = state or last
        if time.monotonic() >= deadline:
            suffix = f" ({hint})" if hint else ""
            raise ExampleValidationError(
                f"timed out waiting for {what}. Last state: {last}{suffix}"
            )
        time.sleep(_POLL_SECONDS)


def _crd_probe(kubectl: KubectlRunner, crd: str) -> Any:
    def probe() -> tuple[bool, str]:
        code, _, err = kubectl("get", "crd", crd)
        return code == 0, f"CRD {crd} not present: {err.strip()[:200]}"

    return probe


def _condition_probe(kubectl: KubectlRunner, resource: str, name: str, condition: str) -> Any:
    def probe() -> tuple[bool, str]:
        payload = _get_json(kubectl, resource, name)
        if payload is None:
            return False, f"{resource}/{name} not found"
        return _condition_true(payload, condition), _condition_summary(payload) or "no conditions"

    return probe


def _served_probe(kubectl: KubectlRunner, resource: str) -> Any:
    def probe() -> tuple[bool, str]:
        code, _, err = kubectl("get", resource, "-A", "-o", "name")
        return code == 0, f"{resource} not served yet: {err.strip()[:200]}"

    return probe


def wait_argocd_ready(kubectl: KubectlRunner, *, timeout: int = 600) -> dict[str, Any]:
    """Wait until Argo CD can sync: the fenced project exists, controller and repo server run.

    All three are deploy-time artifacts (the argo-cd chart and the post-Helm
    ``post-helm-argocd-access.yaml``), so this is a readiness wait with
    nothing to revert.
    """
    deadline = time.monotonic() + timeout

    def probe() -> tuple[dict[str, Any] | None, str]:
        if _get_json(kubectl, "appproject", "gco-tenants", "-n", "argocd") is None:
            return None, "AppProject argocd/gco-tenants not found"
        controller = _get_json(
            kubectl, "statefulset", "argocd-application-controller", "-n", "argocd"
        )
        ready = int(((controller or {}).get("status") or {}).get("readyReplicas") or 0)
        if ready < 1:
            return None, "argocd-application-controller has no ready replica"
        repo_server = _get_json(kubectl, "deployment", "argocd-repo-server", "-n", "argocd")
        if repo_server is None or not _condition_true(repo_server, "Available"):
            return None, "argocd-repo-server is not Available"
        return {
            "project": "argocd/gco-tenants",
            "application_controller_ready": ready,
            "repo_server": "Available",
        }, ""

    ready: dict[str, Any] = _wait_until(
        probe, deadline=deadline, what="Argo CD", hint="is helm.argocd enabled for this deploy?"
    )
    return ready


@dataclass
class CompanionApi:
    """A companion API definition applied before an instance example and deleted after it.

    ``flavor`` ``kro`` applies a ResourceGraphDefinition (after the kro
    capability's CRD exists) and waits for it to be Active; ``crossplane``
    waits for every composition function the Compositions reference to be
    Healthy, applies the XRD + Composition and waits for the XRD to be
    Established. Either way the new API must be served before the instance
    is applied, so ``kubectl apply`` never races discovery.
    """

    flavor: str
    path: Path
    kubectl: KubectlRunner
    applied: bool = False

    def _documents(self) -> list[dict[str, Any]]:
        return [
            doc
            for doc in yaml.safe_load_all(self.path.read_text(encoding="utf-8"))
            if isinstance(doc, dict)
        ]

    def create(self, *, timeout: int = 600) -> dict[str, Any]:
        if self.flavor not in {"kro", "crossplane"}:
            raise ExampleValidationError(f"unknown companion flavor {self.flavor!r}")
        deadline = time.monotonic() + timeout
        documents = self._documents()
        prerequisites: list[str] = []
        if self.flavor == "kro":
            crd = "resourcegraphdefinitions.kro.run"
            _wait_until(
                _crd_probe(self.kubectl, crd),
                deadline=deadline,
                what="the kro capability",
                hint="is eks_capabilities.kro enabled for this deploy?",
            )
            prerequisites.append(crd)
        else:
            functions = sorted(
                {
                    str((step.get("functionRef") or {}).get("name", ""))
                    for doc in documents
                    if doc.get("kind") == "Composition"
                    for step in (doc.get("spec") or {}).get("pipeline") or []
                }
            )
            for function in functions:
                _wait_until(
                    _condition_probe(
                        self.kubectl, "function.pkg.crossplane.io", function, "Healthy"
                    ),
                    deadline=deadline,
                    what=f"Crossplane function {function}",
                    hint="is helm.crossplane enabled for this deploy?",
                )
                prerequisites.append(f"function/{function}=Healthy")
        code, out, err = self.kubectl("apply", "-f", str(self.path))
        if code != 0:
            raise ExampleValidationError(
                f"kubectl apply -f examples/{self.path.name} failed: {err.strip()[:600]}"
            )
        self.applied = True
        served: list[str] = []
        for doc in documents:
            name = str((doc.get("metadata") or {}).get("name", ""))
            spec = doc.get("spec") or {}
            if self.flavor == "kro" and doc.get("kind") == "ResourceGraphDefinition":

                def rgd_probe(name: str = name) -> tuple[bool, str]:
                    payload = _get_json(self.kubectl, "resourcegraphdefinition", name)
                    state = str(((payload or {}).get("status") or {}).get("state", ""))
                    summary = _condition_summary(payload or {})
                    return state == "Active", f"state={state or 'unknown'} {summary}".strip()

                _wait_until(rgd_probe, deadline=deadline, what=f"ResourceGraphDefinition {name}")
                schema = spec.get("schema") or {}
                resource = f"{str(schema.get('kind', '')).lower()}.{schema.get('group', 'kro.run')}"
            elif self.flavor == "crossplane" and doc.get("kind") == "CompositeResourceDefinition":
                _wait_until(
                    _condition_probe(
                        self.kubectl,
                        "compositeresourcedefinition.apiextensions.crossplane.io",
                        name,
                        "Established",
                    ),
                    deadline=deadline,
                    what=f"CompositeResourceDefinition {name}",
                )
                names = spec.get("names") or {}
                resource = f"{str(names.get('kind', '')).lower()}.{spec.get('group', '')}"
            else:
                continue
            _wait_until(
                _served_probe(self.kubectl, resource), deadline=deadline, what=f"the {resource} API"
            )
            served.append(resource)
        return {
            "prerequisites": prerequisites,
            "applied": [line for line in out.strip().splitlines() if line][:20],
            "served": served,
        }

    def destroy(self) -> dict[str, Any]:
        if not self.applied:
            return {"deleted": []}
        code, out, err = self.kubectl(
            "delete", "-f", str(self.path), "--ignore-not-found", "--wait=true", timeout=300
        )
        if code != 0:
            raise ExampleValidationError(
                f"deleting companion examples/{self.path.name} failed: {err.strip()[:500]}"
            )
        self.applied = False
        return {"deleted": [line for line in out.strip().splitlines() if line][:20]}


@dataclass
class AckSqsQueues:
    """AWS-side proof for the ACK SQS example.

    ``wait_ready`` waits for the ACK capability's Queue CRD; after the
    example syncs, ``verify_created`` resolves each queue in SQS directly;
    after cleanup, ``verify_deleted`` requires SQS to stop resolving it — ACK
    deletes the AWS queue when the object goes, so a surviving queue is a leak.
    """

    session: Any
    region: str

    def _sqs(self) -> Any:
        with BOTO_CLIENT_LOCK:
            return self.session.client("sqs", region_name=self.region)

    @staticmethod
    def queue_names(parsed: ParsedExample) -> list[str]:
        return [
            str((doc.get("spec") or {}).get("queueName", ""))
            for doc in parsed.documents
            if doc.get("kind") == "Queue"
        ]

    def wait_ready(self, kubectl: KubectlRunner, *, timeout: int = 600) -> dict[str, Any]:
        crd = "queues.sqs.services.k8s.aws"
        _wait_until(
            _crd_probe(kubectl, crd),
            deadline=time.monotonic() + timeout,
            what="the ACK SQS controller",
            hint="is eks_capabilities.ack enabled for this deploy?",
        )
        return {"crd": crd}

    def verify_created(self, parsed: ParsedExample) -> dict[str, Any]:
        sqs = self._sqs()
        urls: dict[str, str] = {}
        for name in self.queue_names(parsed):
            try:
                urls[name] = str(sqs.get_queue_url(QueueName=name)["QueueUrl"])
            except sqs.exceptions.QueueDoesNotExist as exc:
                raise ExampleValidationError(
                    f"ACK reports queue {name} synced but SQS does not resolve it"
                ) from exc
        return {"sqs_queue_urls": urls}

    def verify_deleted(self, parsed: ParsedExample, *, timeout: int = 180) -> dict[str, Any]:
        sqs = self._sqs()
        deadline = time.monotonic() + timeout
        for name in self.queue_names(parsed):

            def gone(name: str = name) -> tuple[bool, str]:
                try:
                    sqs.get_queue_url(QueueName=name)
                except sqs.exceptions.QueueDoesNotExist:
                    return True, ""
                return False, f"SQS still resolves {name}"

            _wait_until(gone, deadline=deadline, what=f"deletion of SQS queue {name}")
        return {"sqs_queues_deleted": self.queue_names(parsed)}


#: Setup drivers _run_one_example knows how to dispatch, by spec.setup_driver
#: name. Kept as an explicit registry so a spec naming a driver that does not
#: exist fails the registry pin test, not a live run.
KNOWN_SETUP_DRIVERS = frozenset(
    {
        "keda-demo-queue",
        "vector-demo-corpus",
        "trainer-runtime-ready",
        "mlflow-ready",
        "argocd-revision-pin",
        "kro-api",
        "crossplane-api",
        "ack-sqs",
    }
)
