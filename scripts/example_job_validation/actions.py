"""The ``examples`` action: run every selected example through its documented path."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

import yaml

from scripts.live_release_validation.models import RunContext

from . import drivers, kube
from .drivers import ExampleRunResult, ExampleValidationError
from .specs import COMPANION, EXAMPLE_SPECS, KUBECTL_APPLY, SCALEDJOB_SCALES
from .static_checks import parse_example, run_static_checks


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
    client = ctx.session.client("service-quotas", region_name=region)
    try:
        quota = client.get_service_quota(ServiceCode="ec2", QuotaCode=quota_code)
        value = float(quota["Quota"]["Value"])
    except Exception as exc:  # noqa: BLE001 — quota lookup failing must not fail the run
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

    manifest_path, mutations = drivers.apply_mutations(parsed)
    evidence: dict[str, Any] = {}
    keda_queue: drivers.KedaDemoQueue | None = None
    try:
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

        evidence["submission"] = drivers.submit_example(
            parsed, manifest_path, repo_root=ctx.settings.repo_root, region=region, kubectl=kubectl
        )
        if spec.criteria in drivers.CRITERIA_WAITERS:
            evidence["criteria"] = drivers.CRITERIA_WAITERS[spec.criteria](
                parsed, kubectl, timeout=spec.timeout_seconds
            )
        if spec.criteria == SCALEDJOB_SCALES or spec.submission == KUBECTL_APPLY:
            evidence["cleanup"] = drivers.cleanup_example(parsed, manifest_path, kubectl)
        else:
            # CLI-submitted resources: delete through kubectl as well so quota
            # headroom is restored for the next example.
            evidence["cleanup"] = drivers.cleanup_example(parsed, manifest_path, kubectl)
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


def action_examples(ctx: RunContext) -> dict[str, Any]:
    """Run the selected examples in registry order inside one cluster session."""
    selected = list(getattr(ctx.settings, "selected_examples", ()) or EXAMPLE_SPECS)
    region = ctx.deployment_regions[0]
    cluster_name = f"{ctx.config.project_name}-{region}"
    state: dict[str, Any] = ctx.checkpoint.state.setdefault("examples", {})

    results: list[ExampleRunResult] = []
    with kube.cluster_session(ctx.settings.repo_root, cluster_name, region) as kubectl:
        for name in selected:
            previous = state.get(name)
            if isinstance(previous, dict) and previous.get("status") == "passed":
                results.append(
                    ExampleRunResult(
                        name=name,
                        status="passed",
                        submission=str(previous.get("submission", "")),
                        detail="checkpoint: already passed in this run",
                    )
                )
                continue
            print(f"[example] {name} ({EXAMPLE_SPECS[name].submission})")
            result = _run_one_example(ctx, name, region, kubectl)
            results.append(result)
            state[name] = result.to_dict()
            ctx.persist()
            print(f"[example] {name}: {result.status} ({result.duration_seconds:.1f}s)")

    summary = {
        "region": region,
        "results": [result.to_dict() for result in results],
        "passed": sum(1 for item in results if item.status == "passed"),
        "skipped": sum(1 for item in results if item.status == "skipped"),
        "failed": sum(1 for item in results if item.status == "failed"),
    }
    ctx.checkpoint.state["examples_summary"] = summary
    ctx.persist()
    if summary["failed"]:
        failed_names = [item.name for item in results if item.status == "failed"]
        raise RuntimeError(
            f"{summary['failed']} example(s) failed: {', '.join(failed_names)} "
            "(per-example evidence is in the report details)"
        )
    return summary
