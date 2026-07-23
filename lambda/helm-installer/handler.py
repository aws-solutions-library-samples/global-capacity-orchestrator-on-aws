"""
Helm Installer Lambda Handler

Installs and manages Helm charts on EKS clusters via CloudFormation Custom Resources.
Supports KEDA and other Helm-based installations.

Features:
- Automatic Helm repo management
- Idempotent install/upgrade operations
- Configurable chart values via CloudFormation properties
- EKS authentication via IAM

Environment Variables:
    CLUSTER_NAME: Name of the EKS cluster
    REGION: AWS region

CloudFormation Properties:
    ClusterName: EKS cluster name
    Region: AWS region
    Charts: Dict of chart configurations to override defaults
    EnabledCharts: List of chart names to enable (overrides charts.yaml)
"""

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import urllib3
import yaml

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/helm-installer/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/helm-installer/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUCCESS = "SUCCESS"
FAILED = "FAILED"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Maximum number of install/upgrade attempts for each chart. Some charts
# (e.g. cert-manager, NVIDIA operators) depend on admission webhooks or
# CRDs that briefly flap during cluster bring-up, and a single retry often
# clears those transient failures. Raise this if you see persistent retry
# exhaustion in the logs; lower it for faster feedback in local testing.
HELM_INSTALL_MAX_RETRIES = 3

# Seconds to wait between failed chart attempts. Sized to give the EKS
# control plane time to stabilise (webhook endpoints coming up, API
# server throttling clearing) without dragging CloudFormation custom
# resource completion beyond its 15-minute timeout.
HELM_INSTALL_RETRY_DELAY_SECONDS = 30

# Delete is a synchronous CloudFormation custom-resource operation with a
# one-hour ceiling. Ordinary releases get a 60-second Helm deadline and a
# 75-second process cap so each state-machine task fits its two-minute slot.
# LBC receives a dedicated four-minute Helm deadline because it must remove
# controller webhooks and finalizers after every Gateway-owned ALB is gone.
HELM_UNINSTALL_TIMEOUT = "60s"
HELM_UNINSTALL_COMMAND_TIMEOUT_SECONDS = 75
LBC_CHART_NAME = "aws-load-balancer-controller"
LBC_UNINSTALL_TIMEOUT = "4m"
LBC_UNINSTALL_COMMAND_TIMEOUT_SECONDS = 270

# KEDA's Helm release owns CRDs whose instances carry operator-managed
# finalizers. Four bounded discovery calls plus two ordered deletion calls and
# the final Helm uninstall fit inside the dedicated four-minute KEDA task.
KEDA_API_GROUPS = ("keda.sh", "eventing.keda.sh")
KEDA_CUSTOM_RESOURCE_DELETE_TIMEOUT = "45s"
KEDA_CUSTOM_RESOURCE_COMMAND_TIMEOUT_SECONDS = 55
KEDA_CUSTOM_RESOURCE_DISCOVERY_TIMEOUT_SECONDS = 10

# Validation intentionally has tighter command caps than chart installation:
# these are read-only convergence checks and should never consume an entire
# Lambda invocation when the API server or a Helm storage backend is wedged.
HELM_VALIDATION_COMMAND_TIMEOUT_SECONDS = 120
HELM_VALIDATION_TOTAL_TIMEOUT_SECONDS = 780
KUBECTL_VALIDATION_COMMAND_TIMEOUT_SECONDS = 120
# Service endpoint readiness is polled (it is a convergence condition); every
# other validation dimension stays single-shot within the shared deadline.
ENDPOINT_READINESS_POLL_SECONDS = 10.0
KUBECTL_VALIDATION_REQUEST_TIMEOUT = "30s"
MAX_VALIDATION_DIAGNOSTIC_CHARS = 2048
_HELM_RELEASE_NOT_FOUND = "Error: release: not found"


@dataclass(frozen=True)
class _PinnedManifestBundle:
    """One remotely hosted manifest whose bytes and inventory are immutable."""

    name: str
    url: str
    size: int
    sha256: str
    object_count: int
    crd_count: int


PINNED_GATEWAY_CRD_BUNDLES = (
    _PinnedManifestBundle(
        name="gateway-api-standard-v1.5.0",
        url=(
            "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
            "v1.5.0/standard-install.yaml"
        ),
        size=1_023_753,
        sha256="510338cf6709f84410efcce5269268f4c7c5067efdc5d04c75aa2fd2f8380c96",
        object_count=10,
        crd_count=8,
    ),
    _PinnedManifestBundle(
        name="aws-lbc-gateway-v3.4.2",
        url=(
            "https://raw.githubusercontent.com/kubernetes-sigs/"
            "aws-load-balancer-controller/v3.4.2/config/crd/gateway/gateway-crds.yaml"
        ),
        size=65_111,
        sha256="89983f8b43b1b85c3d065d6f0007ee1fa2bffe8790282b9a57ccc9a355f65bd7",
        object_count=3,
        crd_count=3,
    ),
)
GATEWAY_CRD_HTTP_CONNECT_TIMEOUT_SECONDS = 5
GATEWAY_CRD_HTTP_READ_TIMEOUT_SECONDS = 45
GATEWAY_CRD_HTTP_MAX_REDIRECTS = 3
GATEWAY_CRD_APPLY_COMMAND_TIMEOUT_SECONDS = 180


class _ValidationTimeout(RuntimeError):
    """A systemic command/budget timeout that should stop further release checks."""


def _validation_command_timeout(deadline: float, cap: int) -> int:
    """Cap one command to both its normal limit and the invocation-wide budget."""
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise _ValidationTimeout("Helm validation exhausted its invocation-wide time budget")
    return min(cap, max(1, int(remaining)))


def _bounded_diagnostic(value: Any, limit: int = MAX_VALIDATION_DIAGNOSTIC_CHARS) -> str:
    """Return useful subprocess/error text without emitting unbounded payloads."""
    text = str(value).strip()
    if not text:
        return "<empty>"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def _record_addon_status(chart_name: str, status: str, message: str) -> None:
    """Record a single chart's install outcome to SSM (best-effort).

    Writes ``/<project>/addons/<region>/<chart>`` as a small JSON blob so the
    add-on layer's health is observable out-of-band — decoupled from the
    CloudFormation rollback path. Read back via ``gco stacks addons-status``.
    Failures here are swallowed: status reporting must never turn a successful
    install into a failure (or vice versa).
    """
    project = os.environ.get("PROJECT_NAME")
    region = os.environ.get("REGION")
    if not project or not region:
        return
    import contextlib
    import time as _time

    with contextlib.suppress(Exception):
        boto3.client("ssm").put_parameter(
            Name=f"/{project}/addons/{region}/{chart_name}",
            Value=json.dumps(
                {
                    "chart": chart_name,
                    "status": status,
                    "message": message[:1024],
                    "updated_at": int(_time.time()),
                }
            ),
            Type="String",
            Overwrite=True,
        )


# Load default chart configurations
CHARTS_CONFIG_PATH = Path(__file__).parent / "charts.yaml"


def load_charts_config() -> dict[str, Any]:
    """Load chart configurations from charts.yaml."""
    if CHARTS_CONFIG_PATH.exists():
        with open(CHARTS_CONFIG_PATH, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
            return loaded if isinstance(loaded, dict) else {"charts": {}}
    return {"charts": {}}


def send_response(
    event: dict[str, Any],
    context: Any,
    status: str,
    data: dict[str, Any],
    physical_id: str,
    reason: str | None = None,
) -> None:
    """Send response to CloudFormation."""
    body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data,
    }

    logger.info(f"Sending response: {json.dumps(data)}")

    # Timeout is for the CFN response callback (HTTP PUT to S3 presigned URL),
    # not for Helm chart installation. Helm installs use subprocess with --timeout 10m.
    http = urllib3.PoolManager()
    try:
        http.request(
            "PUT",
            event["ResponseURL"],
            body=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
    except Exception as e:
        logger.error(f"Failed to send response: {e}")


def get_eks_token(cluster_name: str, region: str) -> str:
    """Generate EKS authentication token."""
    from botocore.signers import RequestSigner

    session = boto3.Session()
    sts = session.client("sts", region_name=region)
    service_id = sts.meta.service_model.service_id

    signer = RequestSigner(
        service_id, region, "sts", "v4", session.get_credentials(), session.events
    )

    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }

    url = signer.generate_presigned_url(
        params, region_name=region, expires_in=60, operation_name=""
    )
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"k8s-aws-v1.{token}"


def configure_kubeconfig(cluster_name: str, region: str) -> str:
    """Configure kubeconfig for EKS cluster and return path."""
    eks = boto3.client("eks", region_name=region)
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]

    # Create kubeconfig
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": cluster_name,
                "cluster": {
                    "server": cluster["endpoint"],
                    "certificate-authority-data": cluster["certificateAuthority"]["data"],
                },
            }
        ],
        "contexts": [
            {
                "name": cluster_name,
                "context": {
                    "cluster": cluster_name,
                    "user": cluster_name,
                },
            }
        ],
        "current-context": cluster_name,
        "users": [
            {
                "name": cluster_name,
                "user": {
                    "token": get_eks_token(cluster_name, region),
                },
            }
        ],
    }

    # Write kubeconfig to temp file using secure method
    fd, kubeconfig_path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(kubeconfig, f)
    except Exception:
        # ``fdopen`` owns and normally closes the descriptor. Suppress a
        # possible EBADF here, but always remove a partially-written credential
        # file before propagating the original error.
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(kubeconfig_path)
        raise

    return kubeconfig_path


def run_helm(
    args: list[str],
    kubeconfig: str,
    env: dict[str, str] | None = None,
    command_timeout_seconds: int | None = None,
    log_output: bool = True,
) -> tuple[int, str, str]:
    """Run helm command with kubeconfig.

    Returns ``(returncode, stdout, stderr)``. A subprocess timeout is mapped
    to ``(-1, "", "timeout: ...")`` so callers get a uniform failure contract
    and can branch on the return code instead of wrapping every invocation in
    ``try: ... except subprocess.TimeoutExpired``. This matters because
    ``helm ... --wait`` can block on operator reconciliation; without this
    mapping a single stuck release would crash the Lambda past the outer
    ``except Exception`` and fail the whole retry loop.

    ``command_timeout_seconds`` lets synchronous stack deletion use a tighter,
    provable bound than create/update without changing the latter's 13-minute
    allowance.
    """
    cmd = ["helm"] + args

    helm_env = os.environ.copy()
    helm_env["KUBECONFIG"] = kubeconfig
    # Lambda has read-only filesystem except /tmp
    helm_env["HELM_CACHE_HOME"] = "/tmp/.helm/cache"  # nosec B108 - Lambda runtime requires /tmp for writable storage
    helm_env["HELM_CONFIG_HOME"] = "/tmp/.helm/config"  # nosec B108 - Lambda runtime requires /tmp for writable storage
    helm_env["HELM_DATA_HOME"] = "/tmp/.helm/data"  # nosec B108 - Lambda runtime requires /tmp for writable storage
    if env:
        helm_env.update(env)

    logger.info(f"Running: {' '.join(cmd)}")

    # Subprocess wall-clock cap. This MUST be >= helm's own ``--timeout`` (10m)
    # below, otherwise a legitimately-slow install (e.g. a cold NVIDIA operator
    # image pull) gets SIGKILLed by Python before helm's own deadline and a
    # would-succeed install is reported as a failure. Each chart now runs in its
    # own Step Functions task / Lambda invocation, so this can safely approach
    # the per-invocation Lambda limit; retries are handled at the state-machine
    # level. Override with HELM_CMD_TIMEOUT_SECONDS.
    cmd_timeout = (
        command_timeout_seconds
        if command_timeout_seconds is not None
        else int(os.environ.get("HELM_CMD_TIMEOUT_SECONDS", "780"))
    )

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - cmd is ["helm"] + static args list; helm_env is a controlled copy of os.environ, no shell=True
            cmd,
            capture_output=True,
            text=True,
            env=helm_env,
            timeout=cmd_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(f"helm subprocess timed out after {exc.timeout}s: {' '.join(cmd)}")
        return -1, "", f"timeout: helm command exceeded {exc.timeout}s"

    if log_output and result.stdout:
        logger.info(f"stdout: {_bounded_diagnostic(result.stdout, 4096)}")
    if log_output and result.stderr:
        logger.warning(f"stderr: {_bounded_diagnostic(result.stderr, 4096)}")

    return result.returncode, result.stdout, result.stderr


def _clear_stuck_release(chart_name: str, namespace: str, kubeconfig: str) -> bool:
    """Delete release secrets for revisions stuck in ``pending-*`` state.

    When a previous ``helm upgrade --wait`` is interrupted (timeout, Lambda
    crash, network blip, operator reconciliation stall), Helm leaves the
    revision's release secret in ``pending-upgrade``, ``pending-install``,
    or ``pending-rollback`` status. That status acts as an exclusive lock:
    every subsequent ``helm upgrade`` / ``helm rollback`` against the same
    release fails with ``another operation (install/upgrade/rollback) is in
    progress`` until the lock is cleared.

    ``helm rollback --wait`` would normally clear it, but it can hang
    indefinitely when the target chart's own operator (e.g. a CRD
    controller) is stuck reconciling the half-applied state — which is
    exactly the failure mode that got us here. Deleting the stuck secret
    is the reliable recovery: Helm's view
    of the release reverts to the previous ``deployed`` revision, and the
    next upgrade proceeds normally.

    Returns ``True`` if any stuck secrets were deleted.
    """
    status_code, status_out, _ = run_helm(
        ["status", chart_name, "-n", namespace, "-o", "json"], kubeconfig
    )
    if status_code != 0:
        # Release not installed yet (first install) — nothing to clear.
        return False

    try:
        status = json.loads(status_out).get("info", {}).get("status", "")
    except json.JSONDecodeError, AttributeError:
        return False

    if status not in ("pending-install", "pending-upgrade", "pending-rollback"):
        return False

    logger.warning(
        f"Release {chart_name} in namespace {namespace} is stuck in {status!r}; "
        f"clearing the stuck release secret so the next upgrade can proceed."
    )

    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig

    # Only delete secrets matching the exact stuck status. ``deployed`` /
    # ``superseded`` / ``failed`` history is preserved so ``helm history``
    # still shows the prior revisions for debugging.
    try:
        list_result = (
            subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed argv, no shell=True
                [
                    "kubectl",
                    "get",
                    "secrets",
                    "-n",
                    namespace,
                    "-l",
                    f"owner=helm,name={chart_name},status={status}",
                    "-o",
                    "jsonpath={.items[*].metadata.name}",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"kubectl get secrets timed out while clearing {chart_name}")
        return False

    if list_result.returncode != 0 or not list_result.stdout.strip():
        return False

    cleared = False
    for secret in list_result.stdout.split():
        try:
            del_result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed argv, no shell=True
                ["kubectl", "delete", "secret", "-n", namespace, secret, "--ignore-not-found"],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"kubectl delete timed out for {secret}")
            continue
        if del_result.returncode == 0:
            cleared = True
            logger.info(f"Deleted stuck release secret {secret}")

    return cleared


def add_helm_repo(repo_name: str, repo_url: str, kubeconfig: str) -> bool:
    """Add Helm repository."""
    code, _, _ = run_helm(["repo", "add", repo_name, repo_url, "--force-update"], kubeconfig)
    if code != 0:
        return False

    code, _, _ = run_helm(["repo", "update", repo_name], kubeconfig)
    return code == 0


def install_chart(
    chart_name: str,
    config: dict[str, Any],
    kubeconfig: str,
    value_overrides: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Install or upgrade a Helm chart."""
    repo_name = config["repo_name"]
    repo_url = config["repo_url"]
    chart = config["chart"]
    version = config.get("version")
    namespace = config.get("namespace", "default")
    create_ns = config.get("create_namespace", True)
    values = config.get("values", {})
    use_oci = config.get("use_oci", False)

    # Per-chart readiness gate.
    #   ``wait`` (default True)        -> ``helm --wait`` (block until the
    #                                     release's resources report Ready).
    #   ``wait_timeout`` (default 10m) -> ``helm --timeout``.
    # A chart whose components converge asynchronously — e.g. one that pulls
    # large images from a slow/rate-limited registry — can set ``wait: false``
    # so the install returns as soon as manifests are applied instead of
    # blocking the whole invocation on readiness. That keeps a single slow
    # chart from burning the Lambda wall-clock guard (HELM_CMD_TIMEOUT_SECONDS)
    # and lets the Step Functions state machine move on to the next chart; the
    # release still converges in the background and its status is recorded to
    # SSM either way. ``wait_timeout`` must stay below HELM_CMD_TIMEOUT_SECONDS
    # (default 780s) or the subprocess guard SIGKILLs helm before its own
    # deadline and a would-succeed install is reported as a failure.
    wait = config.get("wait", True)
    wait_timeout = config.get("wait_timeout", "10m")

    # Merge value overrides
    if value_overrides:
        values = deep_merge(values, value_overrides)

    # For OCI registries, we don't need to add a repo
    if not use_oci:
        # Add repo
        if not add_helm_repo(repo_name, repo_url, kubeconfig):
            return False, f"Failed to add repo {repo_name}"
        chart_ref = f"{repo_name}/{chart}"
    else:
        # For OCI, use the full OCI URL
        chart_ref = f"{repo_url}/{chart}"

    # Build helm upgrade --install command
    args = [
        "upgrade",
        "--install",
        chart_name,
        chart_ref,
        "--namespace",
        namespace,
        "--timeout",
        wait_timeout,
    ]

    # ``--wait`` blocks until the release's resources are Ready. Opt-out per
    # chart via ``wait: false`` for asynchronously-converging charts.
    if wait:
        args.append("--wait")

    if version:
        args.extend(["--version", version])

    if create_ns:
        args.append("--create-namespace")

    # Write values to temp file using secure method
    if values:
        fd, values_file = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(values, f)
        except Exception:
            os.close(fd)
            raise
        args.extend(["--values", values_file])

    # Preflight: if a previous upgrade was interrupted, the release is
    # wedged in ``pending-*`` and blocks all subsequent operations. Clear
    # the stuck secret before attempting the upgrade so we don't have to
    # rely on rollback-after-failure (which itself hangs when the chart's
    # operator is stuck reconciling the half-applied state).
    _clear_stuck_release(chart_name, namespace, kubeconfig)

    code, stdout, stderr = run_helm(args, kubeconfig)

    if code == 0:
        return True, f"Successfully installed {chart_name}"
    else:
        # If we still hit "another operation in progress" despite the
        # preflight (e.g. a concurrent operation started between the check
        # and the upgrade), clear the stuck state and retry once. Unlike
        # the previous ``rollback --wait`` approach, this never blocks on
        # operator reconciliation.
        if "another operation" in stderr.lower() and "in progress" in stderr.lower():
            logger.warning(
                f"Release {chart_name} reports 'another operation in progress' "
                f"after preflight; clearing stuck state and retrying once."
            )
            _clear_stuck_release(chart_name, namespace, kubeconfig)
            code2, _, stderr2 = run_helm(args, kubeconfig)
            if code2 == 0:
                return True, f"Successfully installed {chart_name} (after clearing stuck state)"
            return False, f"Failed to install {chart_name}: {stderr2}"
        return False, f"Failed to install {chart_name}: {stderr}"


def _delete_keda_custom_resources(kubeconfig: str) -> tuple[bool, str]:
    """Delete all KEDA custom resources before uninstalling its controller.

    Resource discovery keeps this compatible with the exact KEDA chart version
    in use instead of maintaining a second CRD list here. Namespaced resources
    (including ``ScaledJob``) are deleted first across every namespace, then
    cluster-scoped authentication resources. ``kubectl delete --wait`` does not
    return until operator-owned finalizers are gone, so Helm can safely remove
    the controller and CRDs afterwards.
    """
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    common = ["kubectl", "--kubeconfig", kubeconfig, "--request-timeout=30s"]
    resources_by_scope: dict[bool, list[str]] = {True: [], False: []}

    for api_group in KEDA_API_GROUPS:
        for namespaced in (True, False):
            try:
                discovery = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed argv, no shell=True
                    [
                        *common,
                        "api-resources",
                        f"--api-group={api_group}",
                        "--verbs=list,delete",
                        f"--namespaced={'true' if namespaced else 'false'}",
                        "-o",
                        "name",
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=KEDA_CUSTOM_RESOURCE_DISCOVERY_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return False, f"Timed out discovering {api_group} custom resources"

            if discovery.returncode != 0:
                error = (discovery.stderr or discovery.stdout).strip()
                return False, f"Failed to discover {api_group} custom resources: {error}"
            resources_by_scope[namespaced].extend(discovery.stdout.split())

    deleted_types = 0
    for namespaced in (True, False):
        resources = list(dict.fromkeys(resources_by_scope[namespaced]))
        if not resources:
            continue

        command = [
            *common,
            "delete",
            ",".join(resources),
            "--all",
            "--ignore-not-found=true",
            "--wait=true",
            f"--timeout={KEDA_CUSTOM_RESOURCE_DELETE_TIMEOUT}",
        ]
        if namespaced:
            command.append("--all-namespaces")

        try:
            deletion = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - discovered KEDA resource names, no shell=True
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=KEDA_CUSTOM_RESOURCE_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            scope = "namespaced" if namespaced else "cluster-scoped"
            return False, f"Timed out deleting {scope} KEDA custom resources"

        if deletion.returncode != 0:
            error = (deletion.stderr or deletion.stdout).strip()
            scope = "namespaced" if namespaced else "cluster-scoped"
            return False, f"Failed to delete {scope} KEDA custom resources: {error}"
        deleted_types += len(resources)

    return True, f"Deleted and waited for {deleted_types} KEDA custom resource type(s)"


def uninstall_chart(chart_name: str, namespace: str, kubeconfig: str) -> tuple[bool, str]:
    """Uninstall a Helm chart within the synchronous teardown budget."""
    if chart_name == "keda":
        cleaned, cleanup_message = _delete_keda_custom_resources(kubeconfig)
        if not cleaned:
            return False, f"KEDA pre-uninstall cleanup failed: {cleanup_message}"
        logger.info(cleanup_message)

    helm_timeout = LBC_UNINSTALL_TIMEOUT if chart_name == LBC_CHART_NAME else HELM_UNINSTALL_TIMEOUT
    command_timeout = (
        LBC_UNINSTALL_COMMAND_TIMEOUT_SECONDS
        if chart_name == LBC_CHART_NAME
        else HELM_UNINSTALL_COMMAND_TIMEOUT_SECONDS
    )
    args = [
        "uninstall",
        chart_name,
        "--namespace",
        namespace,
        "--wait",
        "--timeout",
        helm_timeout,
    ]
    code, _, stderr = run_helm(
        args,
        kubeconfig,
        command_timeout_seconds=command_timeout,
    )

    if code == 0:
        return True, f"Successfully uninstalled {chart_name}"

    # Helm's explicit release-absence signature is idempotent success. Do not
    # accept a generic "not found": Kubernetes API/resource failures can carry
    # that text while the release is still live and must block teardown.
    if "release: not found" in stderr.lower():
        return True, f"Chart {chart_name} not found (already uninstalled)"
    return False, f"Failed to uninstall {chart_name}: {stderr}"


def quiesce_health_monitor(kubeconfig: str, namespace: str = "gco-system") -> tuple[bool, str]:
    """Scale health-monitor to zero and wait until every replica is gone.

    This is the first synchronous stack-delete task. It prevents the monitor
    from recreating the ALB-hostname SSM parameter after GA deregistration.
    Kubernetes' exact Deployment ``NotFound`` response is idempotent; every
    other scale/wait failure is surfaced to the teardown state machine.
    """
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    common = ["kubectl", "--kubeconfig", kubeconfig, "--request-timeout=30s"]

    try:
        scale = subprocess.run(
            [
                *common,
                "scale",
                "deployment/health-monitor",
                "--namespace",
                namespace,
                "--replicas=0",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out scaling health-monitor deployment to zero"

    if scale.returncode != 0:
        scale_error = (scale.stderr or scale.stdout).strip()
        lowered = scale_error.lower()
        exact_absence = (
            'deployments.apps "health-monitor" not found',
            'deployment.apps "health-monitor" not found',
            'deployment "health-monitor" not found',
        )
        if not any(signature in lowered for signature in exact_absence):
            return False, f"Failed to scale health-monitor to zero: {scale_error}"

    try:
        wait = subprocess.run(
            [
                *common,
                "wait",
                "--for=delete",
                "pod",
                "--selector=app=health-monitor",
                "--namespace",
                namespace,
                "--timeout=120s",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=135,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for health-monitor pods to terminate"

    if wait.returncode != 0:
        wait_error = (wait.stderr or wait.stdout).strip()
        if "no matching resources found" not in wait_error.lower():
            return False, f"Failed waiting for health-monitor pods: {wait_error}"

    return True, "Health monitor quiesced"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dictionaries."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def run_kubectl(
    args: list[str],
    kubeconfig: str,
    command_timeout_seconds: int = KUBECTL_VALIDATION_COMMAND_TIMEOUT_SECONDS,
    log_output: bool = True,
) -> tuple[int, str, str]:
    """Run a bounded, argument-vector-only kubectl command for validation."""
    cmd = [
        "kubectl",
        "--kubeconfig",
        kubeconfig,
        f"--request-timeout={KUBECTL_VALIDATION_REQUEST_TIMEOUT}",
        *args,
    ]
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    logger.info(f"Running: {' '.join(cmd)}")

    try:
        result = (
            subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - argv only, no shell=True
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=command_timeout_seconds,
            )
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(f"kubectl subprocess timed out after {exc.timeout}s")
        return -1, "", f"timeout: kubectl command exceeded {exc.timeout}s"

    if log_output and result.stdout:
        logger.info(f"stdout: {_bounded_diagnostic(result.stdout, 4096)}")
    if log_output and result.stderr:
        logger.warning(f"stderr: {_bounded_diagnostic(result.stderr, 4096)}")
    return result.returncode, result.stdout, result.stderr


def _release_configurations(
    event: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], set[str]]:
    """Return ordered, deeply-merged release configs and the enabled set."""
    defaults = load_charts_config().get("charts", {})
    overrides = event.get("Charts", {})
    enabled_charts = event.get("EnabledCharts", [])
    if overrides is None:
        overrides = {}
    if enabled_charts is None:
        enabled_charts = []

    if not isinstance(defaults, dict):
        raise RuntimeError("charts.yaml field 'charts' must be a mapping")
    if not isinstance(overrides, dict):
        raise RuntimeError("Charts must be a mapping")
    if not isinstance(enabled_charts, list) or not all(
        isinstance(name, str) and name for name in enabled_charts
    ):
        raise RuntimeError("EnabledCharts must be a list of non-empty release names")

    merged: dict[str, dict[str, Any]] = {}
    for release, config in defaults.items():
        if not isinstance(release, str) or not release or not isinstance(config, dict):
            raise RuntimeError("charts.yaml contains an invalid release configuration")
        merged[release] = deep_merge({}, config)

    # Existing releases retain charts.yaml order. Runtime-only releases append
    # in the JSON mapping's insertion order, while known releases receive the
    # exact same recursive override semantics used by installation.
    for release, override in overrides.items():
        if not isinstance(release, str) or not release or not isinstance(override, dict):
            raise RuntimeError("Charts contains an invalid release override")
        merged[release] = deep_merge(merged.get(release, {}), override)

    unknown_enabled = [name for name in enabled_charts if name not in merged]
    if unknown_enabled:
        names = ", ".join(unknown_enabled[:5])
        raise RuntimeError(f"EnabledCharts has no chart configuration for: {names}")

    return list(merged.items()), set(enabled_charts)


def _release_metadata(release: str, config: dict[str, Any]) -> tuple[str, str, str]:
    """Extract the chart, version, and namespace needed for exact validation."""
    chart = config.get("chart")
    version = config.get("version")
    namespace = config.get("namespace", "default")
    if not isinstance(chart, str) or not chart:
        raise RuntimeError(f"release {release!r} has no valid chart name")
    if version is None or not str(version):
        raise RuntimeError(f"release {release!r} has no configured chart version")
    if not isinstance(namespace, str) or not namespace:
        raise RuntimeError(f"release {release!r} has no valid namespace")
    return chart, str(version), namespace


def _parse_json_object(output: str, description: str) -> dict[str, Any]:
    """Parse command output as a JSON object with a bounded failure message."""
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{description} returned invalid JSON: {exc.msg}") from exc
    except TypeError as exc:
        raise RuntimeError(f"{description} returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{description} returned {type(parsed).__name__}, expected object")
    return parsed


def _flatten_resources(value: Any, description: str) -> list[dict[str, Any]]:
    """Flatten Kubernetes ``kind: List`` documents into individual objects."""
    documents = value if isinstance(value, list) else [value]

    resources: list[dict[str, Any]] = []
    for document in documents:
        if document is None:
            continue
        if not isinstance(document, dict):
            raise RuntimeError(f"{description} contains a non-object document")
        if document.get("kind") == "List":
            items = document.get("items")
            if not isinstance(items, list):
                raise RuntimeError(f"{description} contains kind List without an items list")
            resources.extend(_flatten_resources(items, description))
        else:
            resources.append(document)
    return resources


def _resource_core_identity(resource: dict[str, Any], description: str) -> tuple[str, str, str]:
    """Return the immutable API-version/kind/name portion of an identity."""
    api_version = resource.get("apiVersion")
    kind = resource.get("kind")
    metadata = resource.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(api_version, str) or not api_version:
        raise RuntimeError(f"{description} contains an object without apiVersion/kind/name")
    if not isinstance(kind, str) or not kind:
        raise RuntimeError(f"{description} contains an object without apiVersion/kind/name")
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"{description} contains an object without apiVersion/kind/name")
    return api_version, kind, name


def _resource_namespace(resource: dict[str, Any]) -> str | None:
    metadata = resource.get("metadata")
    namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
    return namespace if isinstance(namespace, str) and namespace else None


def _display_identity(identity: tuple[str, str, str], namespace: str | None = None) -> str:
    api_version, kind, name = identity
    object_name = f"{namespace}/{name}" if namespace else name
    return f"{api_version}/{kind} {object_name}"


def _compare_resource_identities(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    release: str,
    release_namespace: str,
) -> None:
    """Require exact counts and API/kind/name/namespace identities."""
    expected_core = Counter(
        _resource_core_identity(resource, f"manifest for {release}") for resource in expected
    )
    actual_core = Counter(
        _resource_core_identity(resource, f"kubectl output for {release}") for resource in actual
    )
    if len(expected) != len(actual) or expected_core != actual_core:
        missing = list((expected_core - actual_core).elements())[:5]
        unexpected = list((actual_core - expected_core).elements())[:5]
        details = []
        if missing:
            details.append("missing=" + ", ".join(_display_identity(item) for item in missing))
        if unexpected:
            details.append(
                "unexpected=" + ", ".join(_display_identity(item) for item in unexpected)
            )
        detail = "; ".join(details) or "duplicate resource identities differ"
        raise RuntimeError(
            f"release {release!r} rendered {len(expected)} resources but kubectl returned "
            f"{len(actual)} ({detail})"
        )

    # Explicit manifest namespaces must match exactly for namespaced kinds. A
    # namespace omitted by a namespaced Helm object is defaulted by
    # ``kubectl -n`` and must return from the release namespace; a
    # cluster-scoped object legitimately returns no namespace even when the
    # chart templates ``metadata.namespace`` onto it (for example kueue's
    # MutatingWebhookConfiguration) because the API server discards the field
    # on cluster-scoped kinds. Namespaced kinds always return with their
    # namespace, so accepting a cluster-scoped return never weakens the check
    # for them. Consume counters so duplicate identities are also exact.
    actual_namespaced = Counter(
        (_resource_core_identity(resource, "kubectl output"), _resource_namespace(resource))
        for resource in actual
    )
    for resource in expected:
        namespace = _resource_namespace(resource)
        if namespace is None:
            continue
        identity = _resource_core_identity(resource, "manifest")
        key = (identity, namespace)
        cluster_scoped_key = (identity, None)
        if actual_namespaced[key] > 0:
            actual_namespaced[key] -= 1
        elif actual_namespaced[cluster_scoped_key] > 0:
            actual_namespaced[cluster_scoped_key] -= 1
        else:
            wrong_namespaces = sorted(
                actual_namespace or "<cluster-scoped>"
                for (actual_identity, actual_namespace), count in actual_namespaced.items()
                if actual_identity == identity and count > 0
            )
            raise RuntimeError(
                f"release {release!r} returned the wrong namespace for "
                f"{_display_identity(identity, namespace)}: expected {namespace!r} or "
                f"cluster scope, got {wrong_namespaces}"
            )

    for resource in expected:
        if _resource_namespace(resource) is not None:
            continue
        identity = _resource_core_identity(resource, "manifest")
        namespaced_key = (identity, release_namespace)
        cluster_scoped_key = (identity, None)
        if actual_namespaced[namespaced_key] > 0:
            actual_namespaced[namespaced_key] -= 1
        elif actual_namespaced[cluster_scoped_key] > 0:
            actual_namespaced[cluster_scoped_key] -= 1
        else:
            wrong_namespaces = sorted(
                namespace or "<cluster-scoped>"
                for (actual_identity, namespace), count in actual_namespaced.items()
                if actual_identity == identity and count > 0
            )
            raise RuntimeError(
                f"release {release!r} returned the wrong namespace for "
                f"{_display_identity(identity)}: expected {release_namespace!r} or "
                f"cluster scope, got {wrong_namespaces}"
            )


def _condition_status(resource: dict[str, Any], condition_type: str) -> Any:
    status = resource.get("status")
    conditions = status.get("conditions", []) if isinstance(status, dict) else []
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if isinstance(condition, dict) and condition.get("type") == condition_type:
            return condition.get("status")
    return None


def _is_true(value: Any) -> bool:
    return value is True or value == "True"


def _is_false(value: Any) -> bool:
    return value is False or value == "False" or value == "false"


def _replica_value(status: dict[str, Any], field: str) -> int | None:
    # Kubernetes omits optional integer counters when their value is zero.
    value = status.get(field, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _require_observed_generation(resource: dict[str, Any], identity: str) -> None:
    metadata = resource.get("metadata", {})
    status = resource.get("status", {})
    generation = metadata.get("generation") if isinstance(metadata, dict) else None
    observed = status.get("observedGeneration") if isinstance(status, dict) else None
    if not isinstance(generation, int) or observed != generation:
        raise RuntimeError(
            f"{identity} has stale generation: observed={observed!r}, expected={generation!r}"
        )


def _require_replica_convergence(
    resource: dict[str, Any], identity: str, fields: tuple[str, ...]
) -> None:
    spec = resource.get("spec", {})
    status = resource.get("status", {})
    desired = spec.get("replicas", 1) if isinstance(spec, dict) else None
    if not isinstance(desired, int) or isinstance(desired, bool) or not isinstance(status, dict):
        raise RuntimeError(f"{identity} has invalid desired/status replica data")
    mismatches = {
        field: _replica_value(status, field)
        for field in fields
        if _replica_value(status, field) != desired
    }
    if mismatches:
        raise RuntimeError(f"{identity} is not converged: desired={desired}, replicas={mismatches}")


def _validate_resource_readiness(resource: dict[str, Any]) -> None:
    """Apply kind-specific readiness gates plus generic custom conditions."""
    core = _resource_core_identity(resource, "kubectl output")
    namespace = _resource_namespace(resource)
    identity = _display_identity(core, namespace)
    _, kind, _ = core
    metadata = resource.get("metadata", {})
    if isinstance(metadata, dict) and metadata.get("deletionTimestamp") is not None:
        raise RuntimeError(f"{identity} is terminating")
    status = resource.get("status", {})
    if not isinstance(status, dict):
        status = {}

    if kind == "Deployment":
        _require_observed_generation(resource, identity)
        _require_replica_convergence(
            resource,
            identity,
            ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"),
        )
        if not _is_true(_condition_status(resource, "Available")):
            raise RuntimeError(f"{identity} does not report Available=True")
    elif kind == "StatefulSet":
        _require_observed_generation(resource, identity)
        _require_replica_convergence(
            resource, identity, ("currentReplicas", "updatedReplicas", "readyReplicas")
        )
    elif kind == "DaemonSet":
        _require_observed_generation(resource, identity)
        if "desiredNumberScheduled" not in status:
            raise RuntimeError(f"{identity} has no desiredNumberScheduled")
        desired = _replica_value(status, "desiredNumberScheduled")
        if desired is None:
            raise RuntimeError(f"{identity} has invalid desiredNumberScheduled")
        mismatches = {
            field: _replica_value(status, field)
            for field in (
                "currentNumberScheduled",
                "updatedNumberScheduled",
                "numberReady",
                "numberAvailable",
            )
            if _replica_value(status, field) != desired
        }
        misscheduled = _replica_value(status, "numberMisscheduled")
        if misscheduled != 0:
            mismatches["numberMisscheduled"] = misscheduled
        if mismatches:
            raise RuntimeError(
                f"{identity} is not converged: desired={desired}, replicas={mismatches}"
            )
    elif kind == "Job":
        if not _is_true(_condition_status(resource, "Complete")):
            raise RuntimeError(f"{identity} does not report Complete=True")
    elif kind == "Pod":
        if not _is_true(_condition_status(resource, "Ready")):
            raise RuntimeError(f"{identity} does not report Ready=True")
    elif kind == "PersistentVolumeClaim":
        if status.get("phase") != "Bound":
            raise RuntimeError(f"{identity} is not Bound (phase={status.get('phase')!r})")
    elif kind == "PersistentVolume":
        if status.get("phase") not in ("Bound", "Available"):
            raise RuntimeError(
                f"{identity} is neither Bound nor Available (phase={status.get('phase')!r})"
            )
    elif kind == "Ingress":
        load_balancer = status.get("loadBalancer", {})
        ingress = load_balancer.get("ingress", []) if isinstance(load_balancer, dict) else []
        has_address = isinstance(ingress, list) and any(
            isinstance(item, dict) and (item.get("ip") or item.get("hostname")) for item in ingress
        )
        if not has_address:
            raise RuntimeError(f"{identity} has no load-balancer address")
    elif kind == "CustomResourceDefinition":
        if not _is_true(_condition_status(resource, "Established")):
            raise RuntimeError(f"{identity} does not report Established=True")
    elif kind == "APIService":
        if not _is_true(_condition_status(resource, "Available")):
            raise RuntimeError(f"{identity} does not report Available=True")
    elif kind == "HorizontalPodAutoscaler":
        _require_observed_generation(resource, identity)
        if not _is_true(_condition_status(resource, "AbleToScale")):
            raise RuntimeError(f"{identity} does not report AbleToScale=True")
        if not _is_true(_condition_status(resource, "ScalingActive")):
            raise RuntimeError(f"{identity} does not report ScalingActive=True")
    elif kind == "PodDisruptionBudget":
        _require_observed_generation(resource, identity)
        current = status.get("currentHealthy")
        desired = status.get("desiredHealthy")
        if (
            not isinstance(current, int)
            or isinstance(current, bool)
            or not isinstance(desired, int)
            or isinstance(desired, bool)
            or current < desired
        ):
            raise RuntimeError(
                f"{identity} is unhealthy: currentHealthy={current!r}, desiredHealthy={desired!r}"
            )

    conditions = status.get("conditions", [])
    if isinstance(conditions, list):
        for condition in conditions:
            if (
                isinstance(condition, dict)
                and condition.get("type") in ("Ready", "Available")
                and _is_false(condition.get("status"))
            ):
                raise RuntimeError(
                    f"{identity} reports {condition.get('type')}=False: "
                    f"{_bounded_diagnostic(condition.get('message', 'no message'), 300)}"
                )


def _service_has_ready_endpoint(
    name: str,
    namespace: str,
    display: str,
    kubeconfig: str,
    deadline: float,
) -> bool:
    """Return whether one ready, non-terminating endpoint backs the Service."""
    code, stdout, stderr = run_kubectl(
        [
            "get",
            "endpointslices.discovery.k8s.io",
            "-n",
            namespace,
            "-l",
            f"kubernetes.io/service-name={name}",
            "-o",
            "json",
        ],
        kubeconfig,
        command_timeout_seconds=_validation_command_timeout(
            deadline, KUBECTL_VALIDATION_COMMAND_TIMEOUT_SECONDS
        ),
        log_output=False,
    )
    if code == -1:
        raise _ValidationTimeout(f"{display} EndpointSlice query timed out")
    if code != 0:
        raise RuntimeError(
            f"{display} EndpointSlice query failed: {_bounded_diagnostic(stderr or stdout)}"
        )

    payload = _parse_json_object(stdout, f"EndpointSlice query for {display}")
    items = payload.get("items")
    slices = items if isinstance(items, list) else [payload]
    if not isinstance(slices, list):
        raise RuntimeError(f"EndpointSlice query for {display} returned invalid items")
    for endpoint_slice in slices:
        if not isinstance(endpoint_slice, dict):
            continue
        metadata = endpoint_slice.get("metadata", {})
        if isinstance(metadata, dict) and metadata.get("deletionTimestamp") is not None:
            continue
        endpoints = endpoint_slice.get("endpoints", [])
        if not isinstance(endpoints, list):
            continue
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            conditions = endpoint.get("conditions", {})
            if not isinstance(conditions, dict):
                continue
            if _is_true(conditions.get("ready")) and not _is_true(conditions.get("terminating")):
                return True
    return False


def _validate_service_endpoints(
    resource: dict[str, Any],
    kubeconfig: str,
    release_namespace: str,
    deadline: float,
) -> None:
    """Wait, within the validation deadline, for one ready Service endpoint.

    Endpoint readiness is a convergence condition, not an instant contract:
    slow-starting workloads (for example Grafana running its first-boot
    database migrations on a fresh PersistentVolume) legitimately publish
    their ready endpoint minutes after installation. A single-shot check
    failed a healthy live deployment for exactly that reason, so poll until
    the shared validation deadline; a Service that never converges still
    fails with the exact object named. Query and parse failures are not
    convergence conditions and surface immediately.
    """
    spec = resource.get("spec", {})
    selector = spec.get("selector") if isinstance(spec, dict) else None
    if resource.get("kind") != "Service" or not isinstance(selector, dict) or not selector:
        return

    identity = _resource_core_identity(resource, "kubectl output")
    name = identity[2]
    namespace = _resource_namespace(resource) or release_namespace
    display = _display_identity(identity, namespace)

    while not _service_has_ready_endpoint(name, namespace, display, kubeconfig, deadline):
        if deadline - time.monotonic() <= ENDPOINT_READINESS_POLL_SECONDS:
            raise RuntimeError(f"{display} has no ready, non-terminating EndpointSlice endpoint")
        # nosemgrep: arbitrary-sleep - bounded convergence polling within the validation deadline
        time.sleep(ENDPOINT_READINESS_POLL_SECONDS)


def _remove_validation_file(path: str) -> None:
    """Remove validation material, ignoring only an already-absent path."""
    try:
        os.remove(path)
    except FileNotFoundError:
        return


@contextlib.contextmanager
def _secure_manifest_file(manifest: str) -> Iterator[str]:
    """Write a mode-0600 manifest in the system temporary directory and always remove it."""
    fd, path = tempfile.mkstemp(
        prefix="helm-validation-",
        suffix=".yaml",
        dir=tempfile.gettempdir(),
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as manifest_file:
            manifest_file.write(manifest)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        _remove_validation_file(path)


@contextlib.contextmanager
def _verified_gateway_crd_bundle(
    bundle: _PinnedManifestBundle,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Download one pinned bundle, verify exact bytes, and expose a mode-0600 file."""
    response: Any | None = None
    body = b""
    try:
        response = urllib3.PoolManager().request(
            "GET",
            bundle.url,
            headers={"User-Agent": "gco-helm-installer/1"},
            timeout=urllib3.Timeout(
                connect=GATEWAY_CRD_HTTP_CONNECT_TIMEOUT_SECONDS,
                read=GATEWAY_CRD_HTTP_READ_TIMEOUT_SECONDS,
            ),
            retries=urllib3.Retry(
                total=GATEWAY_CRD_HTTP_MAX_REDIRECTS,
                connect=0,
                read=0,
                redirect=GATEWAY_CRD_HTTP_MAX_REDIRECTS,
                status=0,
                other=0,
                raise_on_redirect=True,
                raise_on_status=True,
            ),
            redirect=True,
        )
        if response.status != 200:
            raise RuntimeError(
                f"{bundle.name} download returned HTTP {response.status}, expected 200"
            )
        body = response.data
        if not isinstance(body, bytes):
            raise RuntimeError(f"{bundle.name} download returned a non-byte body")
    finally:
        if response is not None:
            response.release_conn()

    if len(body) != bundle.size:
        raise RuntimeError(f"{bundle.name} size mismatch: got {len(body)}, expected {bundle.size}")
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if actual_sha256 != bundle.sha256:
        raise RuntimeError(
            f"{bundle.name} SHA-256 mismatch: got {actual_sha256}, expected {bundle.sha256}"
        )

    try:
        documents = list(yaml.safe_load_all(body.decode("utf-8")))
        resources = _flatten_resources(documents, bundle.name)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeError(f"{bundle.name} is not valid UTF-8 YAML: {exc}") from exc

    identities = [_resource_core_identity(resource, bundle.name) for resource in resources]
    crd_count = sum(kind == "CustomResourceDefinition" for _, kind, _ in identities)
    if len(resources) != bundle.object_count or crd_count != bundle.crd_count:
        raise RuntimeError(
            f"{bundle.name} inventory mismatch: objects={len(resources)}/"
            f"{bundle.object_count}, CRDs={crd_count}/{bundle.crd_count}"
        )
    if len(identities) != len(set(identities)):
        raise RuntimeError(f"{bundle.name} contains duplicate object identities")

    fd, path = tempfile.mkstemp(
        prefix=f"{bundle.name}-",
        suffix=".yaml",
        dir=tempfile.gettempdir(),
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as manifest_file:
            manifest_file.write(body)
        yield path, resources
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        _remove_validation_file(path)


def _apply_gateway_crds(kubeconfig: str) -> list[dict[str, Any]]:
    """Server-side apply both verified Gateway API bundles before LBC install."""
    evidence: list[dict[str, Any]] = []
    for bundle in PINNED_GATEWAY_CRD_BUNDLES:
        with _verified_gateway_crd_bundle(bundle) as (manifest_path, resources):
            code, stdout, stderr = run_kubectl(
                [
                    "apply",
                    "--server-side=true",
                    "--force-conflicts",
                    "--field-manager=gco-helm-installer",
                    "-f",
                    manifest_path,
                ],
                kubeconfig,
                command_timeout_seconds=GATEWAY_CRD_APPLY_COMMAND_TIMEOUT_SECONDS,
            )
            if code != 0:
                raise RuntimeError(
                    f"failed to apply {bundle.name}: {_bounded_diagnostic(stderr or stdout)}"
                )
            evidence.append(
                {
                    "bundle": bundle.name,
                    "object_count": len(resources),
                    "crd_count": bundle.crd_count,
                    "sha256": bundle.sha256,
                }
            )
    return evidence


def _validate_gateway_crds(kubeconfig: str, deadline: float) -> list[dict[str, Any]]:
    """Redownload and prove exact live identities plus Established=True CRDs."""
    evidence: list[dict[str, Any]] = []
    for bundle in PINNED_GATEWAY_CRD_BUNDLES:
        with _verified_gateway_crd_bundle(bundle) as (manifest_path, expected):
            code, stdout, stderr = run_kubectl(
                ["get", "-f", manifest_path, "-o", "json"],
                kubeconfig,
                command_timeout_seconds=_validation_command_timeout(
                    deadline, KUBECTL_VALIDATION_COMMAND_TIMEOUT_SECONDS
                ),
                log_output=False,
            )
            if code == -1:
                raise _ValidationTimeout(f"kubectl get timed out for {bundle.name}")
            if code != 0:
                raise RuntimeError(
                    f"kubectl could not retrieve {bundle.name}: "
                    f"{_bounded_diagnostic(stderr or stdout)}"
                )
            live_payload = _parse_json_object(stdout, f"kubectl get for {bundle.name}")
            live_resources = _flatten_resources(live_payload, f"kubectl output for {bundle.name}")
            _compare_resource_identities(
                expected,
                live_resources,
                bundle.name,
                "default",
            )
            for resource in live_resources:
                _validate_resource_readiness(resource)
            evidence.append(
                {
                    "bundle": bundle.name,
                    "object_count": len(live_resources),
                    "crd_count": bundle.crd_count,
                    "sha256": bundle.sha256,
                }
            )
    return evidence


def _validate_enabled_release(
    release: str,
    chart: str,
    version: str,
    namespace: str,
    kubeconfig: str,
    deadline: float,
) -> int:
    code, stdout, stderr = run_helm(
        ["status", release, "-n", namespace, "-o", "json"],
        kubeconfig,
        command_timeout_seconds=_validation_command_timeout(
            deadline, HELM_VALIDATION_COMMAND_TIMEOUT_SECONDS
        ),
        log_output=False,
    )
    if code == -1:
        raise _ValidationTimeout(f"helm status timed out for release {release!r}")
    if code != 0:
        raise RuntimeError(f"helm status failed: {_bounded_diagnostic(stderr or stdout)}")
    status_payload = _parse_json_object(stdout, f"helm status for {release}")
    release_status = status_payload.get("info", {}).get("status")
    if release_status != "deployed":
        raise RuntimeError(f"helm status is {release_status!r}, expected exactly 'deployed'")

    # Release names are DNS labels, so '-' is literal outside a character
    # class. ``re.escape`` handles all regex metacharacters; undoing its
    # unnecessary hyphen escape keeps the expression valid for Helm's Go regex.
    escaped_release = re.escape(release).replace(r"\-", "-")
    code, stdout, stderr = run_helm(
        ["list", "-n", namespace, "--filter", f"^{escaped_release}$", "-o", "json"],
        kubeconfig,
        command_timeout_seconds=_validation_command_timeout(
            deadline, HELM_VALIDATION_COMMAND_TIMEOUT_SECONDS
        ),
        log_output=False,
    )
    if code == -1:
        raise _ValidationTimeout(f"helm list timed out for release {release!r}")
    if code != 0:
        raise RuntimeError(f"helm list failed: {_bounded_diagnostic(stderr or stdout)}")
    try:
        listed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"helm list for {release} returned invalid JSON: {exc.msg}") from exc
    except TypeError as exc:
        raise RuntimeError(f"helm list for {release} returned invalid JSON: {exc}") from exc
    if not isinstance(listed, list) or len(listed) != 1 or not isinstance(listed[0], dict):
        count = len(listed) if isinstance(listed, list) else "non-list"
        raise RuntimeError(f"helm list returned {count} entries, expected exactly one")
    entry = listed[0]
    expected_chart = f"{chart}-{version}"
    mismatches = {
        field: (entry.get(field), expected)
        for field, expected in (
            ("name", release),
            ("namespace", namespace),
            ("status", "deployed"),
            ("chart", expected_chart),
        )
        if entry.get(field) != expected
    }
    if mismatches:
        raise RuntimeError(f"helm list metadata mismatch: {mismatches}")

    code, manifest, stderr = run_helm(
        ["get", "manifest", release, "-n", namespace],
        kubeconfig,
        command_timeout_seconds=_validation_command_timeout(
            deadline, HELM_VALIDATION_COMMAND_TIMEOUT_SECONDS
        ),
        log_output=False,
    )
    if code == -1:
        raise _ValidationTimeout(f"helm get manifest timed out for release {release!r}")
    if code != 0:
        raise RuntimeError(f"helm get manifest failed: {_bounded_diagnostic(stderr or manifest)}")
    if not manifest.strip():
        raise RuntimeError("helm get manifest returned empty output")
    try:
        rendered = _flatten_resources(list(yaml.safe_load_all(manifest)), f"manifest for {release}")
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"helm manifest is invalid YAML: {_bounded_diagnostic(exc, 400)}"
        ) from exc
    if not rendered:
        raise RuntimeError("helm get manifest yielded no Kubernetes objects")

    # Charts legitimately render objects into other namespaces (kube-system
    # auth-reader RoleBindings from KEDA/cert-manager/kueue, control-plane
    # metric Services from kube-prometheus-stack). kubectl refuses a single
    # ``-n`` covering mixed namespaces, so retrieval is grouped by each
    # object's effective namespace; objects without an explicit namespace
    # resolve to the release namespace, and kubectl ignores ``-n`` for
    # cluster-scoped kinds.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for resource in rendered:
        grouped.setdefault(_resource_namespace(resource) or namespace, []).append(resource)
    live_resources: list[dict[str, Any]] = []
    for group_namespace in sorted(grouped):
        group_manifest = yaml.safe_dump_all(grouped[group_namespace], sort_keys=False)
        with _secure_manifest_file(group_manifest) as manifest_path:
            code, live_output, stderr = run_kubectl(
                ["get", "-f", manifest_path, "-n", group_namespace, "-o", "json"],
                kubeconfig,
                command_timeout_seconds=_validation_command_timeout(
                    deadline, KUBECTL_VALIDATION_COMMAND_TIMEOUT_SECONDS
                ),
                log_output=False,
            )
            if code == -1:
                raise _ValidationTimeout(f"kubectl get timed out for release {release!r}")
            if code != 0:
                raise RuntimeError(
                    "kubectl could not retrieve every rendered object: "
                    f"{_bounded_diagnostic(stderr or live_output)}"
                )
            live_payload = _parse_json_object(live_output, f"kubectl get for {release}")
            live_resources.extend(_flatten_resources(live_payload, f"kubectl output for {release}"))
    _compare_resource_identities(rendered, live_resources, release, namespace)

    for resource in live_resources:
        _validate_resource_readiness(resource)
        _validate_service_endpoints(resource, kubeconfig, namespace, deadline)
    return len(live_resources)


def _validate_disabled_release(
    release: str, namespace: str, kubeconfig: str, deadline: float
) -> None:
    code, stdout, stderr = run_helm(
        ["status", release, "-n", namespace, "-o", "json"],
        kubeconfig,
        command_timeout_seconds=_validation_command_timeout(
            deadline, HELM_VALIDATION_COMMAND_TIMEOUT_SECONDS
        ),
        log_output=False,
    )
    if code == -1:
        raise _ValidationTimeout(f"helm status timed out for disabled release {release!r}")
    if code == 0:
        raise RuntimeError("disabled release is still present")
    if stdout.strip() or stderr.strip() != _HELM_RELEASE_NOT_FOUND:
        raise RuntimeError(
            "disabled release absence is ambiguous; expected exact "
            f"{_HELM_RELEASE_NOT_FOUND!r}, got {_bounded_diagnostic(stderr or stdout)!r}"
        )


def validate_releases(event: dict[str, Any], kubeconfig: str) -> dict[str, Any]:
    """Validate exact Helm state and Kubernetes convergence for every release."""
    configurations, enabled_releases = _release_configurations(event)
    release_evidence: list[dict[str, Any]] = []
    failures: list[str] = []
    expected_resources = 0
    validated_resources = 0
    validated_releases = 0
    deadline = time.monotonic() + HELM_VALIDATION_TOTAL_TIMEOUT_SECONDS
    gateway_crd_evidence: list[dict[str, Any]] = []
    if LBC_CHART_NAME in enabled_releases:
        try:
            gateway_crd_evidence = _validate_gateway_crds(kubeconfig, deadline)
        except _ValidationTimeout:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"pinned Gateway CRD validation failed: {_bounded_diagnostic(exc, 800)}"
            ) from exc
        gateway_resource_count = sum(item["object_count"] for item in gateway_crd_evidence)
        expected_resources += gateway_resource_count
        validated_resources += gateway_resource_count

    for release, config in configurations:
        enabled = release in enabled_releases
        try:
            chart, version, namespace = _release_metadata(release, config)
            if enabled:
                resource_count = _validate_enabled_release(
                    release, chart, version, namespace, kubeconfig, deadline
                )
                state = "deployed"
                expected_resources += resource_count
                validated_resources += resource_count
            else:
                _validate_disabled_release(release, namespace, kubeconfig, deadline)
                resource_count = 0
                state = "absent"
            release_evidence.append(
                {
                    "release": release,
                    "namespace": namespace,
                    "chart": chart,
                    "version": version,
                    "enabled": enabled,
                    "status": state,
                    "resource_count": resource_count,
                }
            )
            validated_releases += 1
        except _ValidationTimeout as exc:
            failures.append(f"{release}: {_bounded_diagnostic(exc, 600)}")
            # A timed-out control plane/storage backend is systemic. Continuing
            # would only consume the remaining Lambda budget and risk skipping
            # status recording and secure-file cleanup at the hard deadline.
            break
        except Exception as exc:
            failures.append(f"{release}: {_bounded_diagnostic(exc, 600)}")

    if failures:
        shown = failures[:8]
        if len(failures) > len(shown):
            shown.append(f"... and {len(failures) - len(shown)} more failure(s)")
        summary = f"validated {validated_releases}/{len(configurations)} releases; " + "; ".join(
            shown
        )
        raise RuntimeError(_bounded_diagnostic(summary))

    return {
        "status": "validated",
        "DeploymentToken": event.get("DeploymentToken"),
        "expected_release_count": len(configurations),
        "validated_release_count": validated_releases,
        "expected_resource_count": expected_resources,
        "validated_resource_count": validated_resources,
        "enabled_release_count": len(enabled_releases),
        "disabled_release_count": len(configurations) - len(enabled_releases),
        "gateway_crd_bundles": gateway_crd_evidence,
        "releases": release_evidence,
    }


def _cleanup_stale_webhooks(kubeconfig: str) -> None:
    """Remove MutatingWebhookConfigurations whose service endpoints are unavailable.

    When a webhook's backing pod is down (evicted, pending, crashed), the webhook
    blocks all API mutations for the resources it intercepts. This function detects
    and temporarily removes such webhooks so other Helm charts can upgrade.
    The webhook will be recreated when its chart is successfully reinstalled.
    """
    try:
        # Use kubectl to check for stale webhooks (simpler than kubernetes Python client)
        code, stdout, _ = run_helm(
            ["--kubeconfig", kubeconfig],  # dummy — we just need the env
            kubeconfig,
        )

        # Get all mutating webhook configs
        import subprocess

        env = os.environ.copy()
        env["KUBECONFIG"] = kubeconfig

        result = subprocess.run(
            [
                "kubectl",
                "get",
                "mutatingwebhookconfigurations",
                "-o",
                "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to list webhooks: {result.stderr}")
            return

        for webhook_name in result.stdout.strip().split("\n"):
            if not webhook_name:
                continue

            # Check if the webhook's service has ready endpoints
            svc_result = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "mutatingwebhookconfiguration",
                    webhook_name,
                    "-o",
                    "jsonpath={.webhooks[0].clientConfig.service.namespace}/{.webhooks[0].clientConfig.service.name}",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            if svc_result.returncode != 0 or "/" not in svc_result.stdout:
                continue

            ns, svc = svc_result.stdout.strip().split("/", 1)

            # Check if the service has ready endpoints
            ep_result = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "endpoints",
                    svc,
                    "-n",
                    ns,
                    "-o",
                    "jsonpath={.subsets[*].addresses[*].ip}",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )

            if not ep_result.stdout.strip():
                logger.warning(
                    f"Webhook {webhook_name} has no ready endpoints "
                    f"(service {ns}/{svc}), temporarily removing..."
                )
                subprocess.run(
                    ["kubectl", "delete", "mutatingwebhookconfiguration", webhook_name],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=15,
                )

    except Exception as e:
        logger.warning(f"Webhook cleanup failed (non-fatal): {e}")


def handle_task(event: dict[str, Any]) -> dict[str, Any]:
    """Step Functions task entrypoint: install or uninstall a single chart.

    Each chart is its own state-machine task, so this performs exactly one
    helm operation per invocation and raises on failure — retries and ordering
    are owned by the state machine, not this function. That keeps every
    invocation comfortably under the Lambda timeout and gives per-chart retry
    and observability in the Step Functions console.

    Event shape (from the state machine task payload)::

        {
          "Action": "install_chart" | "uninstall_chart" |
                    "quiesce_health_monitor" | "validate_releases",
          "Chart": "<chart name as keyed in charts.yaml>",  # omitted for quiesce/validate
          "ClusterName": "...", "Region": "...",
          "EnabledCharts": ["keda", ...],
          "KedaOperatorRoleArn": "arn:...",   # optional
          "Charts": { "<name>": { ...overrides... } }  # optional
        }

    Returns a small status dict on success; raises on failure so the state
    machine's Retry/Catch handles it.
    """
    action = event["Action"]
    cluster_name = event.get("ClusterName") or os.environ["CLUSTER_NAME"]
    region = event.get("Region") or os.environ["REGION"]

    if action == "quiesce_health_monitor":
        kubeconfig = configure_kubeconfig(cluster_name, region)
        try:
            success, message = quiesce_health_monitor(kubeconfig)
            if not success:
                raise RuntimeError(f"health-monitor quiesce failed: {message}")
            return {"status": "quiesced", "message": message}
        finally:
            with contextlib.suppress(Exception):
                os.remove(kubeconfig)

    if action == "validate_releases":
        try:
            kubeconfig = configure_kubeconfig(cluster_name, region)
            try:
                evidence = validate_releases(event, kubeconfig)
            finally:
                # Validation must not report success while credentials remain
                # in a reusable warm Lambda filesystem. An unlink failure is a
                # validation failure and is recorded by the outer handler.
                _remove_validation_file(kubeconfig)
        except Exception as exc:
            diagnostic = _bounded_diagnostic(exc)
            _record_addon_status("helm-validation", "failed", diagnostic)
            raise RuntimeError(f"helm release validation failed: {diagnostic}") from exc

        message = (
            f"validated {evidence['validated_release_count']}/"
            f"{evidence['expected_release_count']} releases and "
            f"{evidence['validated_resource_count']}/"
            f"{evidence['expected_resource_count']} resources"
        )
        _record_addon_status("helm-validation", "validated", message)
        return evidence

    chart_name = event["Chart"]
    enabled_charts = event.get("EnabledCharts") or []
    chart_overrides = event.get("Charts") or {}
    keda_operator_role_arn = event.get("KedaOperatorRoleArn")

    default_config = load_charts_config().get("charts", {})
    config = dict(default_config.get(chart_name, {}))
    if chart_name in chart_overrides:
        config = deep_merge(config, chart_overrides[chart_name])

    is_enabled = chart_name in enabled_charts

    # Inject the KEDA operator IAM role ARN for IRSA, mirroring the legacy
    # custom-resource path.
    if chart_name == "keda" and keda_operator_role_arn:
        keda_values = config.setdefault("values", {})
        service_account = keda_values.setdefault("serviceAccount", {})
        operator = service_account.setdefault("operator", {})
        annotations = operator.setdefault("annotations", {})
        annotations["eks.amazonaws.com/role-arn"] = keda_operator_role_arn

    namespace = config.get("namespace", "default")
    kubeconfig = configure_kubeconfig(cluster_name, region)
    try:
        disabled_install = action == "install_chart" and not is_enabled
        if action == "install_chart" and is_enabled and chart_name == LBC_CHART_NAME:
            _apply_gateway_crds(kubeconfig)
        if action == "uninstall_chart" or disabled_install:
            # Disabled chart on an install pass: ensure it's gone (idempotent).
            # Helm's explicit "release: not found" is already reported as
            # success by uninstall_chart; every other error must escape so
            # CloudFormation teardown cannot continue against a live release.
            success, message = uninstall_chart(chart_name, namespace, kubeconfig)
            if not success:
                _record_addon_status(chart_name, "failed", message)
                raise RuntimeError(f"helm uninstall {chart_name} failed: {message}")
            if disabled_install:
                message = f"uninstalled (disabled): {message}"
            _record_addon_status(chart_name, "uninstalled", message)
            return {
                "chart": chart_name,
                "status": "uninstalled",
                "message": message,
            }

        if action == "install_chart":
            value_overrides = chart_overrides.get(chart_name, {}).get("values", {})
            success, message = install_chart(chart_name, config, kubeconfig, value_overrides)
            if not success:
                _record_addon_status(chart_name, "failed", message)
                # Raise so the state machine retries this single chart with
                # backoff rather than failing the whole deploy.
                raise RuntimeError(f"helm install {chart_name} failed: {message}")
            _record_addon_status(chart_name, "installed", message)
            return {"chart": chart_name, "status": "installed", "message": message}

        raise ValueError(f"Unknown Action: {action!r}")
    finally:
        with contextlib.suppress(Exception):
            os.remove(kubeconfig)


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    """Main Lambda handler.

    Two entrypoints share this function:

    - **Step Functions task** (the current install path): the event carries an
      ``Action`` key and is dispatched to :func:`handle_task`, which operates on
      a single chart and raises on failure.
    - **CloudFormation custom resource** (legacy/fallback): the event carries a
      ``RequestType`` and the whole-chart-set loop below runs.
    """
    if event.get("Action"):
        logger.info(
            "Task event: action=%s chart=%s cluster=%s region=%s",
            event.get("Action"),
            event.get("Chart"),
            event.get("ClusterName"),
            event.get("Region"),
        )
        return handle_task(event)

    logger.info(f"Received event: {json.dumps(event)}")

    request_type = event["RequestType"]
    physical_id = event.get("PhysicalResourceId", f"helm-{event['LogicalResourceId']}")

    try:
        props = event["ResourceProperties"]
        cluster_name = props["ClusterName"]
        region = props["Region"]

        # Load default config and merge with overrides
        default_config = load_charts_config()
        charts_config = default_config.get("charts", {})

        # Apply chart overrides from CloudFormation
        chart_overrides = props.get("Charts", {})
        for chart_name, overrides in chart_overrides.items():
            if chart_name in charts_config:
                charts_config[chart_name] = deep_merge(charts_config[chart_name], overrides)
            else:
                charts_config[chart_name] = overrides

        # Apply enabled charts list
        enabled_charts = props.get("EnabledCharts", [])
        if enabled_charts:
            for chart_name in charts_config:
                charts_config[chart_name]["enabled"] = chart_name in enabled_charts

        # Inject KEDA operator IAM role ARN for IRSA if provided
        keda_operator_role_arn = props.get("KedaOperatorRoleArn")
        if keda_operator_role_arn and "keda" in charts_config:
            logger.info(f"Injecting KEDA operator role ARN: {keda_operator_role_arn}")
            keda_values = charts_config["keda"].setdefault("values", {})
            service_account = keda_values.setdefault("serviceAccount", {})
            operator = service_account.setdefault("operator", {})
            annotations = operator.setdefault("annotations", {})
            annotations["eks.amazonaws.com/role-arn"] = keda_operator_role_arn

        # Configure kubeconfig
        kubeconfig = configure_kubeconfig(cluster_name, region)

        results = {}
        failed = []
        uninstall_failed = []

        if request_type in ("Create", "Update"):
            # Install/upgrade enabled charts with retry for transient failures
            # (e.g., webhook not ready yet, API server temporarily unavailable).
            # Tunables at the top of this module.
            max_retries = HELM_INSTALL_MAX_RETRIES
            retry_delay = HELM_INSTALL_RETRY_DELAY_SECONDS

            # First pass: uninstall disabled charts that were previously installed.
            # A genuine uninstall error is a failed convergence operation; only
            # Helm's explicit "not found" result is idempotent success.
            for chart_name, config in charts_config.items():
                if not config.get("enabled", False):
                    namespace = config.get("namespace", "default")
                    logger.info(f"Chart {chart_name} is disabled, checking if installed...")
                    success, message = uninstall_chart(chart_name, namespace, kubeconfig)
                    if success:
                        message = f"uninstalled (disabled): {message}"
                    results[chart_name] = message
                    if not success:
                        uninstall_failed.append(chart_name)

            # Second pass: install/upgrade enabled charts
            for chart_name, config in charts_config.items():
                if not config.get("enabled", False):
                    continue

                if chart_name == LBC_CHART_NAME:
                    _apply_gateway_crds(kubeconfig)
                value_overrides = chart_overrides.get(chart_name, {}).get("values", {})
                success, message = install_chart(chart_name, config, kubeconfig, value_overrides)
                results[chart_name] = message

                if not success:
                    failed.append(chart_name)

            # Retry failed charts — transient issues (webhook races, API timeouts)
            # often resolve after other charts finish installing
            for attempt in range(1, max_retries + 1):
                if not failed:
                    break

                # If failures look like webhook issues, temporarily remove stale
                # MutatingWebhookConfigurations whose endpoints are unavailable.
                # This breaks the deadlock where a down webhook blocks all upgrades.
                if any(
                    "webhook" in results.get(c, "").lower()
                    or "no endpoints" in results.get(c, "").lower()
                    for c in failed
                ):
                    logger.info("Detected webhook-related failures, cleaning stale webhooks...")
                    _cleanup_stale_webhooks(kubeconfig)

                logger.info(
                    f"Retrying {len(failed)} failed chart(s) "
                    f"(attempt {attempt}/{max_retries}, waiting {retry_delay}s)..."
                )
                import time

                time.sleep(retry_delay)

                retry_list = failed.copy()
                failed = []
                for chart_name in retry_list:
                    config = charts_config[chart_name]
                    value_overrides = chart_overrides.get(chart_name, {}).get("values", {})
                    success, message = install_chart(
                        chart_name, config, kubeconfig, value_overrides
                    )
                    results[chart_name] = message
                    if not success:
                        failed.append(chart_name)
                    else:
                        logger.info(f"Retry succeeded for {chart_name}")

            if failed:
                logger.warning(f"Charts still failing after {max_retries} retries: {failed}")

            # Keep uninstall failures out of the install retry loop: retrying
            # one as an install would recreate the disabled release. They still
            # participate in the final FAILED response.
            failed.extend(uninstall_failed)

        elif request_type == "Delete":
            # Uninstall charts (in reverse order)
            for chart_name, config in reversed(list(charts_config.items())):
                if not config.get("enabled", False):
                    continue

                namespace = config.get("namespace", "default")
                success, message = uninstall_chart(chart_name, namespace, kubeconfig)
                results[chart_name] = message

                if not success:
                    failed.append(chart_name)

        # Clean up kubeconfig
        import contextlib

        with contextlib.suppress(Exception):
            os.remove(kubeconfig)

        # Prepare response
        response_data = {
            "Results": json.dumps(results),
            "InstalledCharts": ",".join(
                [k for k, v in results.items() if "Successfully" in str(v)]
            ),
            "FailedCharts": ",".join(failed),
        }

        if failed:
            send_response(
                event,
                context,
                FAILED,
                response_data,
                physical_id,
                f"Failed charts: {', '.join(failed)}",
            )
        else:
            send_response(event, context, SUCCESS, response_data, physical_id)

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        # Delete errors are intentionally failures. Reporting success here lets
        # CloudFormation remove the EKS/access resources while Helm releases
        # (and their external load balancers/webhooks) are still live.
        send_response(event, context, FAILED, {}, physical_id, str(e))
