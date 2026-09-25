"""The ``examples`` action: run every selected example through its documented path."""

from __future__ import annotations

import contextlib
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from scripts.live_release_validation.models import RunContext

from . import drivers, kube
from .drivers import ExampleRunResult, ExampleValidationError
from .specs import COMPANION, EXAMPLE_SPECS, KUBECTL_APPLY, SCALEDJOB_SCALES
from .static_checks import examples_dir, parse_example, run_static_checks


def action_static(ctx: RunContext) -> dict[str, Any]:
    """Offline checks for the selected examples (also run standalone in CI)."""
    names = list(getattr(ctx.settings, "selected_examples", ()) or [])
    findings = run_static_checks(ctx.settings.repo_root, names or None)
    failed = [finding for finding in findings if not finding.passed]
    details = {
        "checked": len(findings),
        "failed": [
            {"example": item.example, "check": item.check, "detail": item.detail} for item in failed
        ],
    }
    if failed:
        raise RuntimeError(f"{len(failed)} static example check(s) failed: {details['failed']}")
    return details


def _capacity_skip_reason(ctx: RunContext, region: str, quota_code: str) -> str | None:
    """Return a skip reason when the account has zero quota for the family."""
    if not quota_code:
        return None
    with drivers.BOTO_CLIENT_LOCK:
        client = ctx.session.client("service-quotas", region_name=region)
    try:
        quota = client.get_service_quota(ServiceCode="ec2", QuotaCode=quota_code)
        value = float(quota["Quota"]["Value"])
    except Exception as exc:  # quota lookup failing must not fail the run
        return f"quota {quota_code} lookup failed ({type(exc).__name__}); treating as unavailable"
    if value <= 0:
        name = quota["Quota"].get("QuotaName", quota_code)
        return f"account quota '{name}' is {value:g} vCPUs — no capacity for this example"
    return None


def _keda_operator_role_arn(kubectl: kube.KubectlRunner) -> str:
    """Resolve the KEDA operator's IAM role from its service-account annotation."""
    for namespace in ("keda", "gco-system", "kube-system"):
        code, out, _ = kubectl(
            "get",
            "serviceaccount",
            "keda-operator",
            "-n",
            namespace,
            "-o",
            "jsonpath={.metadata.annotations.eks\\.amazonaws\\.com/role-arn}",
        )
        if code == 0 and out.strip():
            return out.strip()
    raise ExampleValidationError(
        "KEDA operator service-account role annotation not found in keda/gco-system/kube-system"
    )


def _prepare_keda_manifest(parsed: Any, queue_url: str, region: str, manifest_path: Path) -> Path:
    """Substitute the documented placeholder queue URL with the demo queue."""
    documents = []
    for doc in yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "ScaledJob":
            for trigger in doc["spec"]["triggers"]:
                metadata = trigger.get("metadata", {})
                if "queueURL" in metadata:
                    metadata["queueURL"] = queue_url
                    metadata["awsRegion"] = region
        if doc:
            documents.append(doc)
    return drivers.write_temp_manifest(documents, f"-{parsed.name}.yaml")


def _pin_argocd_revision(parsed: Any, revision: str, manifest_path: Path) -> Path:
    """Point every Application at the commit under validation (a disclosed mutation).

    The shipped example tracks ``main``; the run must sync the Git path at
    exactly the SHA it validates, so a change to the fixture is tested before
    it merges.
    """
    documents = []
    for doc in yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "Application":
            doc["spec"]["source"]["targetRevision"] = revision
        if doc:
            documents.append(doc)
    return drivers.write_temp_manifest(documents, f"-{parsed.name}.yaml")


def _run_one_example(
    ctx: RunContext,
    name: str,
    region: str,
    kubectl: kube.KubectlRunner,
) -> ExampleRunResult:
    spec = EXAMPLE_SPECS[name]
    parsed = parse_example(ctx.settings.repo_root, name)
    started = time.monotonic()

    if spec.submission == COMPANION:
        return ExampleRunResult(
            name=name,
            status="passed",
            submission=spec.submission,
            detail=f"companion artifact: {spec.notes}",
        )

    skip_reason = _capacity_skip_reason(ctx, region, spec.capacity_quota_code)
    if skip_reason:
        return ExampleRunResult(
            name=name, status="skipped", submission=spec.submission, detail=skip_reason
        )

    try:
        # A tunnel that stalled between examples is reopened before anything
        # is submitted; one that cannot be reopened fails the example at once
        # rather than letting its watchers wait out the whole timeout.
        kube.ensure_tunnel(kubectl)
    except kube.TunnelUnavailableError as exc:
        return ExampleRunResult(
            name=name,
            status="failed",
            submission=spec.submission,
            duration_seconds=time.monotonic() - started,
            detail=f"the cluster API was unreachable before the example started: {exc}"[:1500],
        )

    manifest_path, mutations = drivers.apply_mutations(parsed)
    evidence: dict[str, Any] = {}
    keda_queue: drivers.KedaDemoQueue | None = None
    vector_corpus: drivers.VectorDemoCorpus | None = None
    companion: drivers.CompanionApi | None = None
    ack_queues: drivers.AckSqsQueues | None = None
    try:
        if spec.setup_driver and spec.setup_driver not in drivers.KNOWN_SETUP_DRIVERS:
            # Fail closed: a spec naming a driver this dispatcher does not
            # implement must fail loudly, not run without its precondition
            # and report an unearned pass.
            raise ExampleValidationError(
                f"setup driver {spec.setup_driver!r} is not implemented in actions._run_one_example"
            )
        if spec.setup_driver == "keda-demo-queue":
            role_arn = _keda_operator_role_arn(kubectl)
            keda_queue = drivers.KedaDemoQueue(
                session=ctx.session, region=region, run_id=ctx.settings.run_id
            )
            evidence["setup"] = keda_queue.create(role_arn)
            manifest_path = _prepare_keda_manifest(
                parsed, keda_queue.queue_url, region, manifest_path
            )
            mutations["ScaledJob.triggers.queueURL"] = "disposable demo queue for this run"
        elif spec.setup_driver == "vector-demo-corpus":
            # The example's documented prerequisite, run verbatim and fully
            # reverted in the finally below (S3 objects + chunk items).
            vector_corpus = drivers.VectorDemoCorpus(
                repo_root=ctx.settings.repo_root, session=ctx.session, region=region
            )
            evidence["setup"] = vector_corpus.create()
        elif spec.setup_driver == "trainer-runtime-ready":
            # Readiness wait on deploy-time artifacts; nothing to revert.
            evidence["setup"] = drivers.wait_trainer_runtime_ready(kubectl)
        elif spec.setup_driver == "mlflow-ready":
            # Readiness wait; the tracking server may still be rolling out
            # right after a fresh install (its PVC lands one applier pass
            # after the chart). Nothing to revert.
            evidence["setup"] = drivers.wait_mlflow_ready(kubectl)
        elif spec.setup_driver == "argocd-revision-pin":
            # Readiness wait on deploy-time artifacts, then the disclosed
            # revision pin; the Application itself is the example's object.
            evidence["setup"] = drivers.wait_argocd_ready(kubectl)
            manifest_path = _pin_argocd_revision(parsed, ctx.settings.expected_sha, manifest_path)
            mutations["Application.spec.source.targetRevision"] = (
                f"{ctx.settings.expected_sha} (the commit under validation)"
            )
        elif spec.setup_driver in {"kro-api", "crossplane-api"}:
            # The companion API definition goes first and is deleted after the
            # instance's cleanup (below, and in the finally on failure).
            companion = drivers.CompanionApi(
                flavor=spec.setup_driver.removesuffix("-api"),
                path=examples_dir(ctx.settings.repo_root) / f"{spec.companion}.yaml",
                kubectl=kubectl,
            )
            evidence["setup"] = companion.create()
        elif spec.setup_driver == "ack-sqs":
            ack_queues = drivers.AckSqsQueues(session=ctx.session, region=region)
            evidence["setup"] = ack_queues.wait_ready(kubectl)

        evidence["submission"] = drivers.submit_example(
            parsed, manifest_path, repo_root=ctx.settings.repo_root, region=region, kubectl=kubectl
        )
        if spec.criteria in drivers.CRITERIA_WAITERS:
            evidence["criteria"] = drivers.CRITERIA_WAITERS[spec.criteria](
                parsed, kubectl, timeout=spec.timeout_seconds
            )
        if ack_queues is not None:
            evidence["aws"] = ack_queues.verify_created(parsed)
        if spec.criteria == SCALEDJOB_SCALES or spec.submission == KUBECTL_APPLY:
            evidence["cleanup"] = drivers.cleanup_example(parsed, manifest_path, kubectl)
        else:
            # CLI-submitted resources: delete through kubectl as well so quota
            # headroom is restored for the next example.
            evidence["cleanup"] = drivers.cleanup_example(parsed, manifest_path, kubectl)
        if ack_queues is not None:
            evidence["aws_cleanup"] = ack_queues.verify_deleted(parsed)
        if companion is not None:
            evidence["companion_cleanup"] = companion.destroy()
        return ExampleRunResult(
            name=name,
            status="passed",
            submission=spec.submission,
            duration_seconds=time.monotonic() - started,
            mutations=mutations,
            evidence=evidence,
        )
    except ExampleValidationError as exc:
        with contextlib.suppress(ExampleValidationError):
            drivers.cleanup_example(parsed, manifest_path, kubectl)
        return ExampleRunResult(
            name=name,
            status="failed",
            submission=spec.submission,
            duration_seconds=time.monotonic() - started,
            detail=str(exc)[:1500],
            mutations=mutations,
            evidence=evidence,
        )
    finally:
        if keda_queue is not None:
            keda_queue.destroy()
        if vector_corpus is not None:
            vector_corpus.destroy()
        if companion is not None and companion.applied:
            # Failure path: the instance cleanup above ran first.
            with contextlib.suppress(ExampleValidationError):
                companion.destroy()


#: What one example may spend outside its criteria wait: a setup driver's
#: readiness wait (up to 600 s), the submission (``gco`` allows 600 s, a DAG
#: run 1800 s), and cleanup (a 300 s delete plus 180 s for derived Jobs).
_EXAMPLE_OVERHEAD_SECONDS = 1800


def _bastion_ttl_minutes(names: list[str], workers: int) -> int:
    """The tunnel bastion's self-termination backstop, sized to the pending examples.

    The bastion lives exactly as long as the session in the normal path; the
    TTL only stops an orphan (a harness killed outright). The bastion's
    two-hour default is shorter than a sequential pass over the catalog, and
    a bastion that terminates mid-run takes the tunnel with it, so the TTL is
    the worst case a thread pool can take — the total over the workers plus
    the longest single example — within the one day a bastion accepts.
    """
    from cli import ephemeral_bastion

    budgets = [EXAMPLE_SPECS[name].timeout_seconds + _EXAMPLE_OVERHEAD_SECONDS for name in names]
    minutes = math.ceil((sum(budgets) / workers + max(budgets)) / 60)
    return max(
        ephemeral_bastion.DEFAULT_TTL_MINUTES, min(ephemeral_bastion.MAX_TTL_MINUTES, minutes)
    )


def action_examples(ctx: RunContext) -> dict[str, Any]:
    """Run the selected examples in parallel inside one cluster session.

    Every example is self-contained (own workload names, own temp manifest,
    own cleanup), so all selected examples are submitted at once and each
    thread drives its example's full documented flow: submit, wait on the
    success criteria, clean up. GPU node provisioning and image pulls — the
    dominant wall-clock costs — overlap instead of serializing. Transient
    ``exceeded quota`` admission rejections while peers hold the namespace
    quota are expected and retried by the Job controller (the fail-fast in
    ``drivers`` deliberately exempts them). ``max_parallel_examples``
    throttles the pool; 0 means all selected examples at once.
    """
    selected = list(getattr(ctx.settings, "selected_examples", ()) or EXAMPLE_SPECS)
    region = ctx.deployment_regions[0]
    cluster_name = f"{ctx.config.project_name}-{region}"
    state: dict[str, Any] = ctx.checkpoint.state.setdefault("examples", {})

    results: dict[str, ExampleRunResult] = {}
    pending: list[str] = []
    for name in selected:
        previous = state.get(name)
        if isinstance(previous, dict) and previous.get("status") == "passed":
            results[name] = ExampleRunResult(
                name=name,
                status="passed",
                submission=str(previous.get("submission", "")),
                detail="checkpoint: already passed in this run",
            )
        else:
            pending.append(name)

    def run_example(name: str, kubectl: kube.KubectlRunner) -> None:
        print(f"[example] {name} ({EXAMPLE_SPECS[name].submission}) started")
        result = _run_one_example(ctx, name, region, kubectl)
        with ctx.state_lock:
            results[name] = result
            state[name] = result.to_dict()
            ctx.persist()
        print(f"[example] {name}: {result.status} ({result.duration_seconds:.1f}s)")

    limit = int(getattr(ctx.settings, "max_parallel_examples", 0) or 0)
    workers = min(len(pending), limit) if limit > 0 else len(pending)
    tunnel: dict[str, Any] | None = None
    if pending:
        tunnel = {"bastion_ttl_minutes": _bastion_ttl_minutes(pending, workers), "reopens": []}
        with kube.cluster_session(
            ctx.settings.repo_root,
            cluster_name,
            region,
            bastion_ttl_minutes=tunnel["bastion_ttl_minutes"],
            tunnel_events=tunnel["reopens"],
        ) as kubectl:
            if workers == 1:
                for name in pending:
                    run_example(name, kubectl)
            else:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="example") as pool:
                    futures = {pool.submit(run_example, name, kubectl): name for name in pending}
                    for future, name in futures.items():
                        # Surface unexpected (non-validation) errors with the
                        # example's name; ExampleValidationError is already
                        # converted to a failed result inside the thread.
                        try:
                            future.result()
                        except Exception as exc:
                            raise RuntimeError(f"example {name} crashed: {exc}") from exc

    ordered = [results[name] for name in selected if name in results]
    summary = {
        "region": region,
        "results": [result.to_dict() for result in ordered],
        "max_parallel": workers,
        "passed": sum(1 for item in ordered if item.status == "passed"),
        "skipped": sum(1 for item in ordered if item.status == "skipped"),
        "failed": sum(1 for item in ordered if item.status == "failed"),
    }
    if tunnel is not None:
        # Every reopen the tunnel keeper attempted, so a pass that needed one
        # says so, and the bastion lifetime the run asked for.
        summary["tunnel"] = tunnel
    ctx.checkpoint.state["examples_summary"] = summary
    ctx.persist()
    if summary["failed"]:
        failed_names = [item.name for item in ordered if item.status == "failed"]
        raise RuntimeError(
            f"{summary['failed']} example(s) failed: {', '.join(failed_names)} "
            "(per-example evidence is in the report details)"
        )
    return summary
