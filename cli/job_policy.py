"""Read deployed job-validation policy and judge manifests against it.

``GET /api/v1/policy`` reports what a region enforces. This module turns that
into answers to questions the submission path cannot answer on its own, because
each needs more than one region or needs an answer before anything is submitted:

  * *which regions would admit this job* -- fan the policy read out and evaluate
    the same manifest against each. A 32-GPU job is admissible in one region and
    over-cap in another, and today you find that out by submitting.
  * *do the regions still agree* -- there are no per-region policy overrides, so
    any field that differs across regions is a region deployed from a different
    checkout. That is invisible until a job that worked yesterday is rejected.
  * *will this be admitted here* -- an advisory pre-submit check, so a rejection
    costs a local round trip instead of a queue round trip.

Everything is advisory. The authoritative gate is the cluster, and this reads a
snapshot over a network; a check that blocks on its own opinion would refuse
valid jobs whenever it is wrong or merely stale. So the callers render findings
and exit 0 unless the user opts into a failing exit code.

The checks themselves are not reimplemented here -- they come from
:mod:`gco.job_admission`, which is the same code the manifest processor runs.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from gco.job_admission import (
    JobValidationPolicy,
    check_resource_caps,
    check_security_context,
    check_tolerations,
    validate_image_sources,
    validate_resource_kind,
)

logger = logging.getLogger(__name__)

#: Per-region fetch outcomes.
FETCH_OK = "ok"
FETCH_UNREACHABLE = "unreachable"
FETCH_ERROR = "error"

#: Admissibility verdicts. ``unknown`` is distinct from ``reject`` on purpose:
#: a region whose policy could not be read has not refused anything, and
#: collapsing the two would report a network failure as a policy violation.
VERDICT_ADMIT = "admit"
VERDICT_REJECT = "reject"
VERDICT_UNKNOWN = "unknown"

#: Checks are named so a caller can tell which layer objected. These match the
#: order the manifest processor applies them in.
CHECK_KIND = "kind"
CHECK_NAMESPACE = "namespace"
CHECK_IMAGES = "images"
CHECK_CAPS = "resource_caps"
CHECK_SECURITY = "security_context"
CHECK_TOLERATIONS = "tolerations"

#: An AWS ECR registry hostname, e.g. 123456789012.dkr.ecr.us-east-2.amazonaws.com
#: (also matching the .cn and ISO partition suffixes).
_ECR_HOSTNAME = re.compile(r"^\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$")

# How long to wait on one region's policy read before giving up on it. A fan-out
# has to bound the slowest region or one unreachable region stalls the whole
# answer; the per-region timeout inside the HTTP client is 30s, and this leaves
# room for its retries without letting a hung region hold everything.
FETCH_TIMEOUT_SECONDS = 45


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionPolicy:
    """One region's policy read, successful or not."""

    region: str
    status: str
    policy: JobValidationPolicy | None = None
    document: dict[str, Any] = field(default_factory=dict)
    cluster_enforcement: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == FETCH_OK and self.policy is not None

    @property
    def enforcement_gaps(self) -> list[str]:
        """Namespaces whose live ResourceQuota / LimitRange could not be read.

        A gap here means the answer covers only the front-door caps: the
        manifest can clear those and still be rejected at pod creation. Callers
        surface this rather than letting an ``admit`` verdict imply more
        confidence than it has.
        """
        return sorted(
            namespace
            for namespace, layer in (self.cluster_enforcement or {}).items()
            if isinstance(layer, dict) and layer.get("status") != "ok"
        )


def fetch_region_policy(aws_client: Any, region: str) -> RegionPolicy:
    """Read one region's policy, converting any failure into a status."""
    try:
        document = aws_client.get_job_validation_policy(region=region)
    except Exception as e:
        # A region with no regional API bridge raises RuntimeError naming that;
        # anything else is genuinely unexpected. Both are non-fatal here.
        message = str(e)
        status = FETCH_UNREACHABLE if "not deployed" in message else FETCH_ERROR
        logger.debug("Policy read failed for %s: %s", region, e)
        return RegionPolicy(region=region, status=status, reason=f"{type(e).__name__}: {e}")

    policy_document = document.get("policy", {}) or {}
    if not policy_document:
        return RegionPolicy(
            region=region,
            status=FETCH_ERROR,
            document=document,
            reason="the response carried no policy object",
        )

    return RegionPolicy(
        region=region,
        status=FETCH_OK,
        policy=JobValidationPolicy.from_policy_document(policy_document),
        document=policy_document,
        cluster_enforcement=document.get("cluster_enforcement", {}) or {},
    )


def fetch_region_policies(aws_client: Any, regions: list[str]) -> list[RegionPolicy]:
    """Read every region's policy concurrently, preserving *regions* order.

    Concurrent because each read costs a CloudFormation describe plus an API
    call and they are independent; serial fan-out over a handful of regions is
    slow enough that people stop running the check.
    """
    if not regions:
        return []
    if len(regions) == 1:
        return [fetch_region_policy(aws_client, regions[0])]

    results: dict[str, RegionPolicy] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(regions), 8)) as pool:
        futures = {
            pool.submit(fetch_region_policy, aws_client, region): region for region in regions
        }
        for future in concurrent.futures.as_completed(futures, timeout=None):
            region = futures[future]
            try:
                results[region] = future.result(timeout=FETCH_TIMEOUT_SECONDS)
            except Exception as e:  # pragma: no cover - defensive
                results[region] = RegionPolicy(
                    region=region, status=FETCH_ERROR, reason=f"{type(e).__name__}: {e}"
                )
    return [
        results.get(region, RegionPolicy(region=region, status=FETCH_ERROR, reason="no result"))
        for region in regions
    ]


# ---------------------------------------------------------------------------
# Judging a manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionIssue:
    """One reason a manifest would be rejected."""

    check: str
    message: str
    manifest: str | None = None


def manifest_label(manifest: dict[str, Any]) -> str:
    """A short ``Kind/name`` label for messages."""
    kind = manifest.get("kind") or "?"
    name = (manifest.get("metadata") or {}).get("name") or "<unnamed>"
    return f"{kind}/{name}"


def evaluate_manifest(
    manifest: dict[str, Any], policy: JobValidationPolicy
) -> list[AdmissionIssue]:
    """Return every reason *policy* would reject *manifest*.

    Every check runs -- the cluster short-circuits on the first failure, but a
    caller fixing a manifest wants the whole list rather than one round trip per
    problem. The trade-off is that a manifest failing the kind check also
    reports whatever else is wrong with it, which is more information than the
    server would give.
    """
    label = manifest_label(manifest)
    issues: list[AdmissionIssue] = []

    if not policy.validation_enabled:
        return issues

    ok, message = validate_resource_kind(manifest, policy.allowed_kinds)
    if not ok:
        issues.append(AdmissionIssue(CHECK_KIND, message or "kind is not allowed", label))

    namespace = (manifest.get("metadata") or {}).get("namespace")
    if namespace and namespace not in policy.allowed_namespaces:
        issues.append(
            AdmissionIssue(
                CHECK_NAMESPACE,
                f"namespace '{namespace}' is not in the allowlist "
                f"({', '.join(sorted(policy.allowed_namespaces))})",
                label,
            )
        )

    ok, message = validate_image_sources(
        manifest,
        trusted_registries=list(policy.trusted_registries),
        trusted_dockerhub_orgs=list(policy.trusted_dockerhub_orgs),
    )
    if not ok:
        issues.append(AdmissionIssue(CHECK_IMAGES, message or "untrusted image source", label))

    ok, caps_message = check_resource_caps(manifest, policy)
    if not ok:
        issues.append(AdmissionIssue(CHECK_CAPS, caps_message, label))

    ok, message = check_security_context(manifest, policy)
    if not ok:
        issues.append(AdmissionIssue(CHECK_SECURITY, message or "security policy violation", label))

    if policy.require_accelerator_toleration:
        ok, message = check_tolerations(manifest)
        if not ok:
            issues.append(AdmissionIssue(CHECK_TOLERATIONS, message or "missing toleration", label))

    return issues


@contextlib.contextmanager
def _quiet_admission_logging() -> Iterator[None]:
    """Silence gco.job_admission's warnings for the duration of a check.

    Those warnings are an audit trail in the service -- a rejected submission
    should leave a record of why. In a CLI pre-check they are noise: the caller
    is about to be shown the same information as formatted findings, so the log
    line duplicates it and interleaves with the report.

    Single-threaded by construction: evaluation runs after the concurrent policy
    fetch has joined, so this never races another region's evaluation.
    """
    admission_logger = logging.getLogger("gco.job_admission")
    previous = admission_logger.level
    admission_logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        admission_logger.setLevel(previous)


def evaluate_manifests(
    manifests: list[dict[str, Any]], policy: JobValidationPolicy
) -> list[AdmissionIssue]:
    """Evaluate every manifest against one policy."""
    issues: list[AdmissionIssue] = []
    with _quiet_admission_logging():
        for manifest in manifests:
            if not isinstance(manifest, dict):
                continue
            issues.extend(evaluate_manifest(manifest, policy))
    return issues


@dataclass(frozen=True)
class RegionVerdict:
    """Whether one region would admit the manifests, and why not."""

    region: str
    verdict: str
    issues: list[AdmissionIssue] = field(default_factory=list)
    reason: str | None = None
    enforcement_gaps: list[str] = field(default_factory=list)


def region_verdicts(
    manifests: list[dict[str, Any]], policies: list[RegionPolicy]
) -> list[RegionVerdict]:
    """Judge *manifests* against each region's policy."""
    verdicts: list[RegionVerdict] = []
    for entry in policies:
        if not entry.ok:
            verdicts.append(
                RegionVerdict(region=entry.region, verdict=VERDICT_UNKNOWN, reason=entry.reason)
            )
            continue
        assert entry.policy is not None
        issues = evaluate_manifests(manifests, entry.policy)
        verdicts.append(
            RegionVerdict(
                region=entry.region,
                verdict=VERDICT_REJECT if issues else VERDICT_ADMIT,
                issues=issues,
                enforcement_gaps=entry.enforcement_gaps,
            )
        )
    return verdicts


# ---------------------------------------------------------------------------
# Cross-region drift
# ---------------------------------------------------------------------------

#: Policy fields compared across regions. ``trusted_registries`` is handled
#: separately because CDK legitimately varies it per deployment.
_DRIFT_FIELDS: tuple[str, ...] = (
    "max_cpu_millicores",
    "max_memory_bytes",
    "max_gpu_count",
    "allowed_namespaces",
    "allowed_kinds",
    "trusted_dockerhub_orgs",
    "require_accelerator_toleration",
    "validation_enabled",
    "yaml_max_depth",
    "security",
)


@dataclass(frozen=True)
class PolicyDrift:
    """One policy field that is not identical across regions."""

    field: str
    values: dict[str, Any]


def _comparable(value: Any) -> Any:
    """Normalize a policy value into something hashable and printable."""
    if isinstance(value, frozenset | set):
        return tuple(sorted(value))
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


def _renderable(value: Any) -> Any:
    """Turn a normalized value back into something JSON-serializable."""
    if isinstance(value, tuple):
        if value and all(isinstance(item, tuple) and len(item) == 2 for item in value):
            return dict(value)
        return list(value)
    return value


def detect_policy_drift(policies: list[RegionPolicy]) -> list[PolicyDrift]:
    """Return the policy fields that differ across successfully-read regions.

    There are no per-region policy overrides -- every region is deployed from
    the same ``cdk.json`` -- so a field that differs means at least one region
    was deployed from a different checkout of it. That is worth surfacing
    because it is otherwise invisible until a manifest that was admitted in one
    region is rejected in another.

    ``trusted_registries`` is deliberately excluded from the field list and
    handled by :func:`registry_drift`, since CDK appends the project's own ECR
    hostnames at synth time and those legitimately differ.
    """
    readable = [entry for entry in policies if entry.ok]
    if len(readable) < 2:
        return []

    drifts: list[PolicyDrift] = []
    for name in _DRIFT_FIELDS:
        observed = {entry.region: _comparable(getattr(entry.policy, name)) for entry in readable}
        if len(set(observed.values())) > 1:
            drifts.append(
                PolicyDrift(
                    field=name,
                    values={region: _renderable(value) for region, value in observed.items()},
                )
            )
    return drifts


def registry_drift(policies: list[RegionPolicy]) -> PolicyDrift | None:
    """Compare ``trusted_registries`` with the project's ECR hostnames removed.

    CDK augments the configured allowlist with the project's own ECR registry
    hostnames, which encode a region, so a raw comparison reports drift on every
    multi-region deployment. Stripping anything that looks like an ECR hostname
    leaves the part that came from ``cdk.json``, where a difference is real
    drift. The stripped entries are not lost -- they are what
    :func:`ecr_augmentation` reports.
    """
    readable = [entry for entry in policies if entry.ok]
    if len(readable) < 2:
        return None

    observed = {
        entry.region: tuple(
            sorted(
                host
                for host in entry.policy.trusted_registries  # type: ignore[union-attr]
                if not _ECR_HOSTNAME.match(host)
            )
        )
        for entry in readable
    }
    if len(set(observed.values())) <= 1:
        return None
    return PolicyDrift(
        field="trusted_registries",
        values={region: list(value) for region, value in observed.items()},
    )


def ecr_augmentation(policies: list[RegionPolicy]) -> dict[str, list[str]]:
    """Report the ECR hostnames CDK added to each region's allowlist.

    Informational, not drift: these appear in no ``cdk.json``, which is the
    concrete reason a locally-computed policy is not authoritative.
    """
    return {
        entry.region: sorted(
            host
            for host in entry.policy.trusted_registries  # type: ignore[union-attr]
            if _ECR_HOSTNAME.match(host)
        )
        for entry in policies
        if entry.ok
    }
