"""
Lambda handler for applying Kubernetes manifests to EKS clusters.

This Lambda function is triggered by CloudFormation Custom Resources during
stack deployment. It applies Kubernetes manifests (namespaces, deployments,
services, RBAC, Karpenter NodePools, etc.) to the EKS cluster.

Key Features:
- Pure Python implementation (no Docker/kubectl binary required)
- Generates EKS authentication tokens using STS presigned URLs
- Supports create/update operations with idempotent behavior
- Handles placeholder replacement for dynamic values (image URIs, etc.)
- Two-pass deployment: main pass then post-Helm pass for CRD-dependent resources

Manifest Naming Convention:
    NN-name.yaml        Applied in the main pass (before Helm)
    post-helm-*.yaml    Applied in the post-Helm pass (after Helm installs CRDs)

    Files with unreplaced {{PLACEHOLDER}} values are automatically skipped,
    enabling optional features (FSx, Valkey, queue processor).

Environment Variables:
    CLUSTER_NAME: Name of the EKS cluster
    REGION: AWS region where the cluster is deployed

CloudFormation Properties:
    ClusterName: EKS cluster name
    Region: AWS region
    ImageReplacements: Dict of placeholder -> value mappings
    SkipDeletionOnStackDelete: If "true", don't delete resources on stack deletion
    PostHelm: "true" to apply only post-helm-* manifests (after Helm installs CRDs)
"""

import base64
import copy
import json
import logging
import os
import re
import time
from datetime import UTC
from typing import Any

import boto3
import urllib3
import yaml
from kubernetes import client, dynamic
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import NotFoundError, ResourceNotFoundError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/kubectl-applier-simple/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/kubectl-applier-simple/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


# Configure logging for CloudWatch
# In Lambda, the root logger is already configured, so we need to set the level explicitly
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Lazy-initialized AWS clients
_eks_client = None

# CloudFormation response status constants
SUCCESS = "SUCCESS"
FAILED = "FAILED"

# Feature-gate placeholders follow an UPPER_SNAKE token convention
# ({{CLUSTER_OBSERVABILITY_ENABLED}}, {{FSX_FILE_SYSTEM_ID}}, {{VALKEY_ENDPOINT}},
# ...). A manifest that still contains one *after* substitution belongs to a
# feature that is turned off, so the file is skipped. The character class is
# deliberately restricted to A-Z/0-9/_ so the check never matches lower- or
# mixed-case double-brace tokens that are legitimate *content* in an applied
# manifest — e.g. Grafana dashboard legend fields ({{gpu}}, {{service}},
# {{Hostname}}) in the observability dashboard ConfigMaps, which must survive
# substitution untouched and be applied verbatim.
_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_HPA_REPLICA_OWNERSHIP_ANNOTATION = "gco.aws/hpa-controls-replicas"
_POST_HELM_PREFIX = "post-helm-"
_CLUSTER_SCOPE = "<cluster>"
_MAX_PLANNING_FAILURES = 20
_MAX_VALIDATION_FAILURES = 20
_GATEWAY_DELETE_WAIT_SECONDS = 270
_GATEWAY_DELETE_POLL_SECONDS = 5

# Gateway API resources are installed after the pinned CRD bootstrap. Keep the
# exact group/version/plural/scope mapping in one place so apply and teardown
# cannot drift onto different objects.
_GATEWAY_CUSTOM_OBJECTS: dict[str, tuple[str, str, str, bool]] = {
    "GatewayClass": ("gateway.networking.k8s.io", "v1", "gatewayclasses", True),
    "Gateway": ("gateway.networking.k8s.io", "v1", "gateways", False),
    "HTTPRoute": ("gateway.networking.k8s.io", "v1", "httproutes", False),
    "LoadBalancerConfiguration": (
        "gateway.k8s.aws",
        "v1beta1",
        "loadbalancerconfigurations",
        False,
    ),
    "TargetGroupConfiguration": (
        "gateway.k8s.aws",
        "v1beta1",
        "targetgroupconfigurations",
        False,
    ),
}

# This is the authoritative set of kinds the applier knows how to create or
# patch. Planning rejects anything else before the first Kubernetes mutation,
# which prevents a newly added raw manifest from being silently ignored.
_SUPPORTED_MANIFEST_KINDS = frozenset(
    {
        "APIService",
        "ClusterRole",
        "ClusterRoleBinding",
        "ConfigMap",
        "CronJob",
        "CustomResourceDefinition",
        "DaemonSet",
        "Deployment",
        "DeviceClass",
        "EC2NodeClass",
        "Gateway",
        "GatewayClass",
        "HTTPRoute",
        "HorizontalPodAutoscaler",
        "Ingress",
        "IngressClass",
        "IngressClassParams",
        "Job",
        "Lease",
        "LimitRange",
        "LoadBalancerConfiguration",
        "Namespace",
        "NetworkPolicy",
        "NodePool",
        "PersistentVolume",
        "PersistentVolumeClaim",
        "Pod",
        "PodDisruptionBudget",
        "PodMonitor",
        "ResourceQuota",
        "Role",
        "RoleBinding",
        "ScaledJob",
        "ScaledObject",
        "Secret",
        "Service",
        "ServiceAccount",
        "ServiceMonitor",
        "StatefulSet",
        "StorageClass",
        "TargetGroupConfiguration",
    }
)
_CLUSTER_SCOPED_KINDS = frozenset(
    {
        "APIService",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "DeviceClass",
        "EC2NodeClass",
        "GatewayClass",
        "IngressClass",
        "IngressClassParams",
        "Namespace",
        "NodePool",
        "PersistentVolume",
        "StorageClass",
    }
)
_IDENTITY_FIELDS = ("apiVersion", "kind", "namespace", "name", "sourceFile", "phase")


def _public_manifest_identity(resource: dict[str, Any]) -> dict[str, str]:
    """Return the serializable, normalized identity for a planned resource."""
    return {field: resource[field] for field in _IDENTITY_FIELDS}


def _planning_error_message(errors: list[str], failure_count: int) -> str:
    """Build a bounded planning failure with enough source context to act on."""
    hidden = failure_count - len(errors)
    suffix = f"; ... {hidden} additional error(s)" if hidden else ""
    return "Manifest planning failed: " + "; ".join(errors) + suffix


def plan_manifests(
    manifests_dir: str,
    replacements: dict[str, str],
) -> dict[str, Any]:
    """Plan the complete raw-manifest inventory without mutating the cluster.

    Files are scanned in lexical order. Replacements are literal string
    replacements, then an unresolved UPPER_SNAKE placeholder gates the entire
    file out. Every remaining nonempty YAML document must have an exact,
    supported identity and be unique across both apply phases.
    """
    phases: dict[str, list[dict[str, Any]]] = {"base": [], "post-helm": []}
    skipped: dict[str, list[str]] = {"base": [], "post-helm": []}
    feature_gates: dict[str, set[str]] = {"base": set(), "post-helm": set()}
    errors: list[str] = []
    failure_count = 0
    seen: dict[tuple[str, str, str, str], str] = {}

    def add_error(message: str) -> None:
        nonlocal failure_count
        failure_count += 1
        if len(errors) < _MAX_PLANNING_FAILURES:
            errors.append(message[:500])

    replacement_items: list[tuple[str, str]] = []
    if not isinstance(replacements, dict):
        add_error("ImageReplacements must be a string-to-string mapping")
    else:
        for key, value in replacements.items():
            if not isinstance(key, str) or not key:
                add_error("ImageReplacements contains an empty or non-string key")
                continue
            if not isinstance(value, str):
                add_error(f"ImageReplacements value for {key!r} is not a string")
                continue
            replacement_items.append((key, value))

    if failure_count:
        raise ValueError(_planning_error_message(errors, failure_count))

    for filename in sorted(os.listdir(manifests_dir)):
        if not filename.endswith((".yaml", ".yml")):
            continue

        phase = "post-helm" if filename.startswith(_POST_HELM_PREFIX) else "base"
        if phase == "post-helm":
            skipped["base"].append(f"{filename}:deferred-to-post-helm")

        filepath = os.path.join(manifests_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as manifest_file:
                content = manifest_file.read()
        except OSError as exc:
            add_error(f"{filename}: unable to read manifest: {exc}")
            continue

        for key, value in replacement_items:
            content = content.replace(key, value)

        unresolved = sorted(set(_UNRESOLVED_PLACEHOLDER_RE.findall(content)))
        if unresolved:
            skipped[phase].append(f"{filename}:unreplaced-placeholders")
            feature_gates[phase].update(unresolved)
            logger.info(
                "Planning excludes %s - unresolved feature placeholder(s): %s",
                filename,
                ", ".join(unresolved),
            )
            continue

        try:
            documents = list(yaml.safe_load_all(content))
        except yaml.YAMLError as exc:
            add_error(f"{filename}: invalid YAML: {exc}")
            continue

        for document_index, document in enumerate(documents, start=1):
            if document is None:
                continue
            location = f"{filename} document {document_index}"
            if not isinstance(document, dict):
                add_error(f"{location}: document must be a mapping")
                continue

            api_version = document.get("apiVersion")
            kind = document.get("kind")
            metadata = document.get("metadata")
            if not isinstance(api_version, str) or not api_version.strip():
                add_error(f"{location}: apiVersion must be a nonempty string")
                continue
            if not isinstance(kind, str) or not kind.strip():
                add_error(f"{location}: kind must be a nonempty string")
                continue
            api_version = api_version.strip()
            kind = kind.strip()
            if kind not in _SUPPORTED_MANIFEST_KINDS:
                add_error(f"{location}: unsupported kind {kind!r}")
                continue
            if not isinstance(metadata, dict):
                add_error(f"{location}: metadata must be a mapping")
                continue

            name = metadata.get("name")
            if not isinstance(name, str) or not name.strip():
                add_error(f"{location}: metadata.name must be a nonempty string")
                continue
            name = name.strip()

            if kind in _CLUSTER_SCOPED_KINDS:
                namespace = _CLUSTER_SCOPE
            else:
                namespace_value = metadata.get("namespace", "default")
                if not isinstance(namespace_value, str) or not namespace_value.strip():
                    add_error(
                        f"{location}: metadata.namespace must be a nonempty string when present"
                    )
                    continue
                namespace = namespace_value.strip()

            duplicate_key = (api_version, kind, namespace, name)
            previous = seen.get(duplicate_key)
            if previous is not None:
                add_error(
                    f"{location}: duplicate {api_version}/{kind}/{namespace}/{name}; "
                    f"first declared in {previous}"
                )
                continue
            seen[duplicate_key] = location

            phases[phase].append(
                {
                    "apiVersion": api_version,
                    "kind": kind,
                    "namespace": namespace,
                    "name": name,
                    "sourceFile": filename,
                    "phase": phase,
                    "document": document,
                }
            )

    if failure_count:
        raise ValueError(_planning_error_message(errors, failure_count))

    return {
        "phases": phases,
        "skipped": skipped,
        "featureGates": {
            phase: sorted(placeholders) for phase, placeholders in feature_gates.items()
        },
    }


def _deployment_patch_body(document: dict[str, Any]) -> dict[str, Any]:
    """Copy a Deployment update without replicas when its HPA owns scale."""
    patch_body = copy.deepcopy(document)
    metadata = patch_body.get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    if not isinstance(annotations, dict) or (
        annotations.get(_HPA_REPLICA_OWNERSHIP_ANNOTATION) != "true"
    ):
        return patch_body

    spec = patch_body.get("spec")
    if isinstance(spec, dict):
        spec.pop("replicas", None)
    return patch_body


# Exact resources owned by optional features. Keys include the apply phase so
# disabling one feature cannot delete similarly named or unrelated resources.
# Tuples are (apiVersion, kind, namespace-or-None, name); order is deliberate
# for dependent resources such as FSx claims before volumes before the class.
_FEATURE_RESOURCE_INVENTORY: dict[
    tuple[str, bool], tuple[tuple[str, str, str | None, str], ...]
] = {
    ("{{FSX_FILE_SYSTEM_ID}}", False): (
        ("v1", "PersistentVolumeClaim", "default", "gco-fsx-storage"),
        ("v1", "PersistentVolumeClaim", "gco-jobs", "gco-fsx-storage"),
        ("v1", "PersistentVolumeClaim", "gco-system", "gco-fsx-storage"),
        ("v1", "PersistentVolume", None, "gco-fsx-pv-default"),
        ("v1", "PersistentVolume", None, "gco-fsx-pv-jobs"),
        ("v1", "PersistentVolume", None, "gco-fsx-pv-system"),
        ("storage.k8s.io/v1", "StorageClass", None, "fsx-sc"),
    ),
    ("{{VALKEY_ENDPOINT}}", False): tuple(
        ("v1", "ConfigMap", namespace, "gco-valkey")
        for namespace in ("gco-system", "gco-jobs", "gco-inference")
    ),
    ("{{AURORA_PGVECTOR_ENDPOINT}}", False): tuple(
        ("v1", "ConfigMap", namespace, "gco-aurora-pgvector")
        for namespace in ("gco-system", "gco-jobs", "gco-inference")
    ),
    ("{{CLUSTER_OBSERVABILITY_ENABLED}}", False): (
        ("apps/v1", "DaemonSet", "kube-system", "dcgm-exporter"),
        ("v1", "Service", "kube-system", "dcgm-exporter"),
        ("v1", "ConfigMap", "kube-system", "dcgm-device-counters"),
        ("storage.k8s.io/v1", "StorageClass", None, "gco-observability-gp3"),
    ),
    ("{{CLUSTER_OBSERVABILITY_ENABLED}}", True): (
        ("batch/v1", "CronJob", "monitoring", "gco-grafana-admin-password-rotation"),
        ("v1", "ConfigMap", "monitoring", "gco-dashboard-gpu"),
        ("v1", "ConfigMap", "monitoring", "gco-dashboard-schedulers"),
        ("v1", "ConfigMap", "monitoring", "gco-dashboard-keda"),
        ("v1", "ConfigMap", "monitoring", "gco-dashboard-services"),
        (
            "rbac.authorization.k8s.io/v1",
            "ClusterRoleBinding",
            None,
            "gco-prometheus-kueue-metrics",
        ),
        *tuple(
            ("monitoring.coreos.com/v1", "ServiceMonitor", "monitoring", name)
            for name in (
                "gco-keda",
                "gco-volcano",
                "gco-kueue",
                "gco-kuberay",
                "gco-yunikorn",
                "gco-dcgm-exporter",
            )
        ),
        *tuple(
            ("monitoring.coreos.com/v1", "PodMonitor", "monitoring", name)
            for name in (
                "gco-health-monitor",
                "gco-manifest-processor",
                "gco-inference-proxy",
                "gco-inference-monitor",
            )
        ),
        ("rbac.authorization.k8s.io/v1", "RoleBinding", "monitoring", "gco-grafana-rotator"),
        ("rbac.authorization.k8s.io/v1", "Role", "monitoring", "gco-grafana-rotator"),
        ("v1", "ServiceAccount", "monitoring", "gco-grafana-rotator"),
    ),
    ("{{QUEUE_PROCESSOR_IMAGE}}", True): (
        ("keda.sh/v1alpha1", "ScaledJob", "gco-system", "sqs-queue-processor"),
    ),
}


def _prune_disabled_feature(placeholder: str, post_helm: bool) -> dict[str, list[str]]:
    """Delete only the exact resources managed by a disabled optional feature.

    Missing resources and missing CRDs/API resource types are successful no-ops.
    Every other error is returned so convergence fails instead of silently
    leaving stale resources running after the feature was disabled.
    """
    targets = _FEATURE_RESOURCE_INVENTORY.get((placeholder, post_helm), ())
    result: dict[str, list[str]] = {"pruned": [], "failed": []}
    if not targets:
        return result

    dynamic_client = dynamic.DynamicClient(client.ApiClient())
    delete_options = client.V1DeleteOptions(propagation_policy="Background")
    for api_version, kind, namespace, name in targets:
        identifier = f"{api_version}/{kind}/{namespace or '<cluster>'}/{name}"
        try:
            resource = dynamic_client.resources.get(api_version=api_version, kind=kind)
            kwargs: dict[str, Any] = {"name": name, "body": delete_options}
            if namespace is not None:
                kwargs["namespace"] = namespace
            resource.delete(**kwargs)
            result["pruned"].append(identifier)
            logger.info("Pruned disabled-feature resource %s", identifier)
        except ResourceNotFoundError, NotFoundError:
            logger.info("Disabled-feature resource already absent: %s", identifier)
        except ApiException as exc:
            if exc.status == 404:
                logger.info("Disabled-feature resource already absent: %s", identifier)
            else:
                failure = f"{identifier}:{exc.status}:{exc.reason}"
                result["failed"].append(failure)
                logger.error("Failed pruning disabled-feature resource %s", failure)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                logger.info("Disabled-feature resource already absent: %s", identifier)
            else:
                failure = f"{identifier}:{exc}"
                result["failed"].append(failure)
                logger.error("Failed pruning disabled-feature resource %s", failure)

    return result


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Maximum time to wait for a PersistentVolume or PersistentVolumeClaim to
# disappear after we issue a delete. Needed when we're recreating a PV
# whose ``volumeHandle`` changed (FSx/EFS ID rotated) or reconciling a
# Lost PVC whose backing PV was just recreated. If you see this wait
# consistently timing out, check for stuck finalizers (the handler
# already clears the standard ``pv-protection`` / ``pvc-protection``
# finalizers) or for an AWS control-plane issue on the underlying
# volume. Raising the value is safe — it only blocks the re-create path,
# not steady-state applies.
PV_PVC_DELETE_WAIT_SECONDS = 30

# Interval between PV/PVC existence polls during the delete wait.
PV_PVC_DELETE_POLL_INTERVAL_SECONDS = 1


def get_eks_client() -> Any:
    """Get EKS client with lazy initialization."""
    global _eks_client
    if _eks_client is None:
        _eks_client = boto3.client("eks")
    return _eks_client


def send_response(
    event: dict[str, Any],
    context: Any,
    response_status: str,
    response_data: dict[str, Any],
    physical_resource_id: str,
    reason: str | None = None,
) -> None:
    """Send response to CloudFormation."""
    response_body = {
        "Status": response_status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_resource_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": response_data,
    }

    logger.info(f"Sending response: {json.dumps(response_data)}")

    # Timeout is for the CFN response callback (HTTP PUT to S3 presigned URL),
    # not for manifest application. K8s API calls have their own timeouts.
    http = urllib3.PoolManager()
    try:
        http.request(
            "PUT",
            event["ResponseURL"],
            body=json.dumps(response_body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
    except Exception as e:
        logger.error(f"Failed to send response: {e}")


def get_eks_token(cluster_name: str, region: str) -> str:
    """Generate EKS authentication token using STS presigned URL."""
    from botocore.signers import RequestSigner

    # Create STS client
    session = boto3.Session()
    sts_client = session.client("sts", region_name=region)
    service_id = sts_client.meta.service_model.service_id

    # Create request signer
    signer = RequestSigner(
        service_id, region, "sts", "v4", session.get_credentials(), session.events
    )

    # Build the presigned URL for GetCallerIdentity
    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }

    # Generate presigned URL (valid for 60 seconds)
    url = signer.generate_presigned_url(
        params, region_name=region, expires_in=60, operation_name=""
    )

    # Encode as base64 and create the k8s-aws-v1 token
    token_b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"k8s-aws-v1.{token_b64}"


def configure_k8s_client(cluster_name: str, region: str) -> None:
    """Configure Kubernetes client for EKS cluster."""
    eks = get_eks_client()

    # Get cluster info
    cluster_info = eks.describe_cluster(name=cluster_name)
    cluster = cluster_info["cluster"]

    # Configure Kubernetes client
    configuration = client.Configuration()
    configuration.host = cluster["endpoint"]
    configuration.verify_ssl = True

    # Set connection timeouts (important for Lambda!)
    configuration.connection_pool_maxsize = 1
    configuration.retries = 3
    # Set socket timeout to 30 seconds
    import socket

    socket.setdefaulttimeout(30)

    # Decode and write CA certificate to temp file using secure method
    ca_cert = base64.b64decode(cluster["certificateAuthority"]["data"])
    import tempfile

    fd, ca_cert_path = tempfile.mkstemp(suffix=".crt")
    try:
        with os.fdopen(fd, "wb") as ca_file:
            ca_file.write(ca_cert)
            ca_file.flush()
        configuration.ssl_ca_cert = ca_cert_path
    except Exception:
        os.close(fd)
        raise

    # Generate EKS authentication token
    eks_token = get_eks_token(cluster_name, region)

    # Set the bearer token
    configuration.api_key = {"authorization": f"Bearer {eks_token}"}

    logger.info(
        f"✓ Configured Kubernetes client for cluster {cluster_name} at {cluster['endpoint']}"
    )

    client.Configuration.set_default(configuration)


def restart_deployments(namespace: str, deployment_names: list[str]) -> dict[str, Any]:
    """
    Restart deployments by patching their spec with a restart annotation.
    This forces Kubernetes to roll out new pods with the latest image.
    """
    from datetime import datetime

    apps_v1 = client.AppsV1Api()
    restarted = []
    failed = []

    restart_time = datetime.now(UTC).isoformat()

    for name in deployment_names:
        try:
            # Patch the deployment with a restart annotation
            # This is equivalent to `kubectl rollout restart deployment`
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": restart_time}
                        }
                    }
                }
            }
            apps_v1.patch_namespaced_deployment(name, namespace, body=patch)
            restarted.append(name)
            logger.info(f"✓ Restarted deployment {name} in namespace {namespace}")
        except ApiException as e:
            # 404 means the deployment isn't installed on this cluster
            # (e.g. fsx-csi when FSx is disabled) — that's not a failure.
            if e.status == 404:
                logger.info(f"Deployment {namespace}/{name} not found — skipping restart")
            else:
                logger.error(f"Failed to restart deployment {name}: {e.status} - {e.reason}")
                failed.append(name)

    return {"restarted": restarted, "failed": failed}


def restart_daemonsets(namespace: str, daemonset_names: list[str]) -> dict[str, Any]:
    """
    Restart daemonsets by patching their pod template with a restart annotation.
    This forces Kubernetes to roll out new pods with the latest image or latest
    service-account annotation set.

    Used for IRSA-annotated addons whose pods still need to be re-mutated by
    the EKS Pod Identity webhook after the addon's service account had its
    role ARN annotation patched post-install. Without this, DaemonSet pods
    like efs-csi-node, fsx-csi-node, and cloudwatch-agent keep their
    original (credential-less) pod spec and silently fail with IMDS 401s.
    """
    from datetime import datetime

    apps_v1 = client.AppsV1Api()
    restarted = []
    failed = []

    restart_time = datetime.now(UTC).isoformat()

    for name in daemonset_names:
        try:
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": restart_time}
                        }
                    }
                }
            }
            apps_v1.patch_namespaced_daemon_set(name, namespace, body=patch)
            restarted.append(name)
            logger.info(f"✓ Restarted daemonset {name} in namespace {namespace}")
        except ApiException as e:
            # 404 means the daemonset isn't installed on this cluster
            # (e.g. fsx-csi-node when FSx is disabled) — that's expected,
            # not a failure.
            if e.status == 404:
                logger.info(f"DaemonSet {namespace}/{name} not found — skipping restart")
            else:
                logger.error(f"Failed to restart daemonset {name}: {e.status} - {e.reason}")
                failed.append(name)

    return {"restarted": restarted, "failed": failed}


def _verify_workload_credentials(apps_v1: Any) -> list[str]:
    """Verify that key GCO deployments have working IAM credential configuration.

    Checks that:
    1. Deployments use their dedicated service account (with IRSA annotation)
    2. The projected service-account token volume is mounted
    3. AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE env vars are set

    Returns a list of warning strings (empty = all good).
    """
    warnings: list[str] = []
    # Each deployment maps to its dedicated service account
    expected_deployments = [
        ("gco-system", "health-monitor", "gco-health-monitor-sa"),
        ("gco-system", "manifest-processor", "gco-manifest-processor-sa"),
        ("gco-system", "inference-proxy", "gco-inference-proxy-sa"),
        ("gco-system", "inference-monitor", "gco-inference-monitor-sa"),
    ]

    for namespace, name, expected_sa in expected_deployments:
        try:
            dep = apps_v1.read_namespaced_deployment(name, namespace)
            spec = dep.spec.template.spec

            # Check service account
            if spec.service_account_name != expected_sa:
                warnings.append(
                    f"{namespace}/{name}: uses SA '{spec.service_account_name}' instead of {expected_sa}"
                )

            # Check for projected token volume
            has_token_volume = False
            if spec.volumes:
                for vol in spec.volumes:
                    if vol.projected and vol.projected.sources:
                        for src in vol.projected.sources:
                            if (
                                src.service_account_token
                                and src.service_account_token.audience == "sts.amazonaws.com"
                            ):
                                has_token_volume = True
                                break

            if not has_token_volume:
                warnings.append(
                    f"{namespace}/{name}: missing projected service-account token volume for IRSA"
                )

            # Check env vars on first container
            container = spec.containers[0] if spec.containers else None
            if container and container.env:
                env_names = {e.name for e in container.env}
                if "AWS_ROLE_ARN" not in env_names:
                    warnings.append(f"{namespace}/{name}: missing AWS_ROLE_ARN env var")
                if "AWS_WEB_IDENTITY_TOKEN_FILE" not in env_names:
                    warnings.append(
                        f"{namespace}/{name}: missing AWS_WEB_IDENTITY_TOKEN_FILE env var"
                    )

        except ApiException as e:
            if e.status == 404:
                warnings.append(f"{namespace}/{name}: deployment not found")
            else:
                warnings.append(f"{namespace}/{name}: failed to read ({e.status})")
        except Exception as e:
            warnings.append(f"{namespace}/{name}: verification error ({e})")

    # Check that service accounts exist in all required namespaces
    v1 = client.CoreV1Api()
    # Platform service SAs in gco-system
    platform_sas = [
        ("gco-system", "gco-health-monitor-sa"),
        ("gco-system", "gco-manifest-processor-sa"),
        ("gco-system", "gco-inference-monitor-sa"),
    ]
    # User workload SAs in their respective namespaces
    workload_sas = [
        ("gco-jobs", "gco-service-account"),
        ("gco-inference", "gco-service-account"),
    ]
    for namespace, sa_name in platform_sas + workload_sas:
        try:
            sa = v1.read_namespaced_service_account(sa_name, namespace)
            annotations = sa.metadata.annotations or {}
            if "eks.amazonaws.com/role-arn" not in annotations:
                warnings.append(
                    f"{namespace}/{sa_name}: missing eks.amazonaws.com/role-arn annotation"
                )
        except ApiException as e:
            if e.status == 404:
                warnings.append(f"{namespace}/{sa_name}: ServiceAccount not found")
            else:
                warnings.append(f"{namespace}/{sa_name}: failed to read ({e.status})")

    if warnings:
        for w in warnings:
            logger.warning(f"⚠ Credential check: {w}")
    else:
        logger.info("✓ All workload IAM credential configurations verified")

    return warnings


def apply_manifests(
    cluster_name: str,
    region: str,
    manifests_dir: str,
    replacements: dict[str, str],
    post_helm: bool = False,
) -> dict[str, Any]:
    """Apply Kubernetes manifests.

    Args:
        cluster_name: EKS cluster name
        region: AWS region
        manifests_dir: Directory containing manifest YAML files
        replacements: Template variable substitutions
        post_helm: If True, apply only post-helm-* files (run after Helm installs CRDs).
                   If False (default), apply all other files and skip post-helm-* ones.
    """
    plan = plan_manifests(manifests_dir, replacements)
    phase_name = "post-helm" if post_helm else "base"
    planned_resources = plan["phases"][phase_name]
    expected_resources = [_public_manifest_identity(item) for item in planned_resources]
    expected_count = len(planned_resources)

    configure_k8s_client(cluster_name, region)
    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    autoscaling_v2 = client.AutoscalingV2Api()
    rbac_v1 = client.RbacAuthorizationV1Api()
    networking_v1 = client.NetworkingV1Api()
    custom_api = client.CustomObjectsApi()

    applied_count = 0
    failed: list[str] = []
    skipped: list[str] = list(plan["skipped"][phase_name])
    pruned: list[str] = []
    prune_failures: list[str] = []

    # A gated-out file represents an optional feature that is disabled. Preserve
    # the existing exact-resource pruning behavior, once per gate and phase.
    for placeholder in plan["featureGates"][phase_name]:
        if (placeholder, post_helm) not in _FEATURE_RESOURCE_INVENTORY:
            continue
        prune_result = _prune_disabled_feature(placeholder, post_helm)
        pruned.extend(prune_result["pruned"])
        prune_failures.extend(prune_result["failed"])
        failed.extend(f"prune:{failure}" for failure in prune_result["failed"])

    for planned_resource in planned_resources:
        filename = planned_resource["sourceFile"]
        try:
            for doc in (planned_resource["document"],):
                kind = planned_resource["kind"]
                api_version = planned_resource["apiVersion"]
                namespace = doc.get("metadata", {}).get("namespace", "default")
                name = planned_resource["name"]

                logger.info(f"Applying {kind}/{name} in namespace {namespace}")
                try:
                    # Apply based on kind
                    if kind == "Namespace":
                        try:
                            v1.create_namespace(body=doc)
                        except ApiException as e:
                            if e.status == 409:  # Already exists
                                v1.patch_namespace(name, body=doc)
                            else:
                                raise

                    elif kind == "ServiceAccount":
                        try:
                            v1.create_namespaced_service_account(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                v1.patch_namespaced_service_account(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "ClusterRole":
                        try:
                            rbac_v1.create_cluster_role(body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                rbac_v1.patch_cluster_role(name, body=doc)
                            else:
                                raise

                    elif kind == "ClusterRoleBinding":
                        try:
                            rbac_v1.create_cluster_role_binding(body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                rbac_v1.patch_cluster_role_binding(name, body=doc)
                            else:
                                raise

                    elif kind == "Role":
                        try:
                            rbac_v1.create_namespaced_role(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                rbac_v1.patch_namespaced_role(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "RoleBinding":
                        try:
                            rbac_v1.create_namespaced_role_binding(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                rbac_v1.patch_namespaced_role_binding(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "Lease":
                        coordination_v1 = client.CoordinationV1Api()
                        try:
                            coordination_v1.create_namespaced_lease(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                coordination_v1.patch_namespaced_lease(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "Deployment":
                        try:
                            apps_v1.create_namespaced_deployment(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                apps_v1.patch_namespaced_deployment(
                                    name,
                                    namespace,
                                    body=_deployment_patch_body(doc),
                                )
                            else:
                                raise

                    elif kind == "StatefulSet":
                        try:
                            apps_v1.create_namespaced_stateful_set(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                apps_v1.patch_namespaced_stateful_set(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "DaemonSet":
                        try:
                            apps_v1.create_namespaced_daemon_set(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                apps_v1.patch_namespaced_daemon_set(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "Job":
                        batch_v1 = client.BatchV1Api()
                        try:
                            batch_v1.create_namespaced_job(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                batch_v1.patch_namespaced_job(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "CronJob":
                        # batch/v1 CronJob (e.g. the Grafana admin-password
                        # rotation job in the observability post-Helm pass).
                        batch_v1 = client.BatchV1Api()
                        try:
                            batch_v1.create_namespaced_cron_job(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                batch_v1.patch_namespaced_cron_job(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "HorizontalPodAutoscaler":
                        try:
                            autoscaling_v2.create_namespaced_horizontal_pod_autoscaler(
                                namespace, body=doc
                            )
                        except ApiException as e:
                            if e.status == 409:
                                autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(
                                    name, namespace, body=doc
                                )
                            else:
                                raise

                    elif kind == "PodDisruptionBudget":
                        policy_v1 = client.PolicyV1Api()
                        try:
                            policy_v1.create_namespaced_pod_disruption_budget(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                policy_v1.patch_namespaced_pod_disruption_budget(
                                    name, namespace, body=doc
                                )
                            else:
                                raise

                    elif kind == "Service":
                        try:
                            v1.create_namespaced_service(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                v1.patch_namespaced_service(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "Pod":
                        try:
                            v1.create_namespaced_pod(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                v1.patch_namespaced_pod(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "ConfigMap":
                        try:
                            v1.create_namespaced_config_map(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                v1.patch_namespaced_config_map(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "Secret":
                        try:
                            v1.create_namespaced_secret(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                v1.patch_namespaced_secret(name, namespace, body=doc)
                            else:
                                raise

                    elif kind in _GATEWAY_CUSTOM_OBJECTS:
                        group, version, plural, cluster_scoped = _GATEWAY_CUSTOM_OBJECTS[kind]
                        try:
                            if cluster_scoped:
                                custom_api.create_cluster_custom_object(
                                    group, version, plural, body=doc
                                )
                            else:
                                custom_api.create_namespaced_custom_object(
                                    group, version, namespace, plural, body=doc
                                )
                        except ApiException as e:
                            if e.status != 409:
                                raise
                            if cluster_scoped:
                                custom_api.patch_cluster_custom_object(
                                    group, version, plural, name, body=doc
                                )
                            else:
                                custom_api.patch_namespaced_custom_object(
                                    group, version, namespace, plural, name, body=doc
                                )

                    elif kind == "IngressClassParams":
                        # EKS Auto Mode IngressClassParams CRD
                        group = "eks.amazonaws.com"
                        version = api_version.split("/")[-1] if "/" in api_version else "v1"
                        plural = "ingressclassparams"
                        try:
                            custom_api.create_cluster_custom_object(
                                group, version, plural, body=doc
                            )
                        except ApiException as e:
                            if e.status == 409:
                                custom_api.patch_cluster_custom_object(
                                    group, version, plural, name, body=doc
                                )
                            else:
                                raise

                    elif kind == "IngressClass":
                        try:
                            networking_v1.create_ingress_class(body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                networking_v1.patch_ingress_class(name, body=doc)
                            else:
                                raise

                    elif kind == "StorageClass":
                        storage_v1 = client.StorageV1Api()
                        try:
                            storage_v1.create_storage_class(body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                # StorageClass already exists - skip patching as most fields are immutable
                                logger.info(f"StorageClass {name} already exists, skipping update")
                            else:
                                raise

                    elif kind == "PersistentVolume":
                        try:
                            v1.create_persistent_volume(body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                # PVs have immutable spec fields. Check if the existing PV
                                # matches — if so, skip. If the volumeHandle changed (new
                                # FSx file system), force-remove the old PV and recreate.
                                existing = v1.read_persistent_volume(name)
                                existing_handle = (
                                    existing.spec.csi.volume_handle if existing.spec.csi else None
                                )
                                new_handle = doc.get("spec", {}).get("csi", {}).get("volumeHandle")

                                if existing_handle == new_handle:
                                    logger.info(f"PersistentVolume {name} unchanged, skipping")
                                else:
                                    logger.info(
                                        f"PersistentVolume {name} volumeHandle changed "
                                        f"({existing_handle} → {new_handle}), recreating"
                                    )
                                    # Remove the protection finalizer so the PV can be deleted
                                    # even while bound to a PVC
                                    v1.patch_persistent_volume(
                                        name,
                                        body={"metadata": {"finalizers": None}},
                                    )
                                    v1.delete_persistent_volume(name)
                                    # Wait for the PV to actually disappear
                                    import time as _time

                                    _pv_iterations = max(
                                        1,
                                        PV_PVC_DELETE_WAIT_SECONDS
                                        // PV_PVC_DELETE_POLL_INTERVAL_SECONDS,
                                    )
                                    for _wait in range(_pv_iterations):
                                        try:
                                            v1.read_persistent_volume(name)
                                            _time.sleep(PV_PVC_DELETE_POLL_INTERVAL_SECONDS)
                                        except ApiException as read_e:
                                            if read_e.status == 404:
                                                break
                                            raise
                                    v1.create_persistent_volume(body=doc)
                            else:
                                raise

                    elif kind == "PersistentVolumeClaim":
                        try:
                            v1.create_namespaced_persistent_volume_claim(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                # Check if the PVC is in Lost state (bound PV was recreated).
                                # A Lost PVC can't be patched back to health — it must be
                                # deleted and recreated so it binds to the new PV.
                                existing_pvc = v1.read_namespaced_persistent_volume_claim(
                                    name, namespace
                                )
                                if existing_pvc.status.phase == "Lost":
                                    logger.info(
                                        f"PVC {namespace}/{name} is Lost (bound PV was "
                                        f"recreated), deleting and recreating"
                                    )
                                    v1.delete_namespaced_persistent_volume_claim(name, namespace)
                                    import time as _time

                                    _pvc_iterations = max(
                                        1,
                                        PV_PVC_DELETE_WAIT_SECONDS
                                        // PV_PVC_DELETE_POLL_INTERVAL_SECONDS,
                                    )
                                    for _wait in range(_pvc_iterations):
                                        try:
                                            v1.read_namespaced_persistent_volume_claim(
                                                name, namespace
                                            )
                                            _time.sleep(PV_PVC_DELETE_POLL_INTERVAL_SECONDS)
                                        except ApiException as read_e:
                                            if read_e.status == 404:
                                                break
                                            raise
                                    v1.create_namespaced_persistent_volume_claim(
                                        namespace, body=doc
                                    )
                                else:
                                    v1.patch_namespaced_persistent_volume_claim(
                                        name, namespace, body=doc
                                    )
                            else:
                                raise

                    elif kind == "Ingress":
                        try:
                            networking_v1.create_namespaced_ingress(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                networking_v1.patch_namespaced_ingress(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "NodePool":
                        # Karpenter NodePool CRD
                        group = "karpenter.sh"
                        version = api_version.split("/")[-1] if "/" in api_version else "v1"
                        plural = "nodepools"
                        try:
                            custom_api.create_cluster_custom_object(
                                group, version, plural, body=doc
                            )
                        except ApiException as e:
                            if e.status == 409:
                                custom_api.patch_cluster_custom_object(
                                    group, version, plural, name, body=doc
                                )
                            else:
                                raise

                    elif kind == "EC2NodeClass":
                        # Karpenter EC2NodeClass CRD
                        group = "karpenter.k8s.aws"
                        version = api_version.split("/")[-1] if "/" in api_version else "v1"
                        plural = "ec2nodeclasses"
                        try:
                            custom_api.create_cluster_custom_object(
                                group, version, plural, body=doc
                            )
                        except ApiException as e:
                            if e.status == 409:
                                custom_api.patch_cluster_custom_object(
                                    group, version, plural, name, body=doc
                                )
                            else:
                                raise

                    elif kind == "APIService":
                        # Kubernetes API aggregation layer
                        api_reg_v1 = client.ApiregistrationV1Api()
                        try:
                            api_reg_v1.create_api_service(body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                api_reg_v1.patch_api_service(name, body=doc)
                            else:
                                raise

                    elif kind == "CustomResourceDefinition":
                        api_extensions_v1 = client.ApiextensionsV1Api()
                        try:
                            api_extensions_v1.create_custom_resource_definition(body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                api_extensions_v1.patch_custom_resource_definition(name, body=doc)
                            else:
                                raise

                    elif kind == "DeviceClass":
                        # Kubernetes DRA DeviceClass (resource.k8s.io)
                        group = "resource.k8s.io"
                        version = api_version.split("/")[-1] if "/" in api_version else "v1"
                        plural = "deviceclasses"
                        try:
                            custom_api.create_cluster_custom_object(
                                group, version, plural, body=doc
                            )
                        except ApiException as e:
                            if e.status == 409:
                                custom_api.patch_cluster_custom_object(
                                    group, version, plural, name, body=doc
                                )
                            else:
                                raise

                    elif kind == "NetworkPolicy":
                        try:
                            networking_v1.create_namespaced_network_policy(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                networking_v1.patch_namespaced_network_policy(
                                    name, namespace, body=doc
                                )
                            else:
                                raise

                    elif kind == "ResourceQuota":
                        try:
                            v1.create_namespaced_resource_quota(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                v1.patch_namespaced_resource_quota(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "LimitRange":
                        try:
                            v1.create_namespaced_limit_range(namespace, body=doc)
                        except ApiException as e:
                            if e.status == 409:
                                v1.patch_namespaced_limit_range(name, namespace, body=doc)
                            else:
                                raise

                    elif kind == "ScaledJob":
                        # KEDA ScaledJob CRD
                        group = "keda.sh"
                        version = api_version.split("/")[-1] if "/" in api_version else "v1alpha1"
                        plural = "scaledjobs"
                        try:
                            custom_api.create_namespaced_custom_object(
                                group, version, namespace, plural, body=doc
                            )
                        except ApiException as e:
                            if e.status == 409:
                                custom_api.patch_namespaced_custom_object(
                                    group, version, namespace, plural, name, body=doc
                                )
                            else:
                                raise

                    elif kind == "ScaledObject":
                        # KEDA ScaledObject CRD
                        group = "keda.sh"
                        version = api_version.split("/")[-1] if "/" in api_version else "v1alpha1"
                        plural = "scaledobjects"
                        try:
                            custom_api.create_namespaced_custom_object(
                                group, version, namespace, plural, body=doc
                            )
                        except ApiException as e:
                            if e.status == 409:
                                custom_api.patch_namespaced_custom_object(
                                    group, version, namespace, plural, name, body=doc
                                )
                            else:
                                raise

                    elif kind in ("ServiceMonitor", "PodMonitor"):
                        # Prometheus Operator CRDs registered by the
                        # kube-prometheus-stack chart, so these land in the
                        # post-Helm pass. GCO uses ServiceMonitors for
                        # components fronted by a Service (schedulers, DCGM)
                        # and PodMonitors for its own multi-replica services.
                        group = "monitoring.coreos.com"
                        version = api_version.split("/")[-1] if "/" in api_version else "v1"
                        plural = "servicemonitors" if kind == "ServiceMonitor" else "podmonitors"
                        try:
                            custom_api.create_namespaced_custom_object(
                                group, version, namespace, plural, body=doc
                            )
                        except ApiException as e:
                            if e.status == 409:
                                custom_api.patch_namespaced_custom_object(
                                    group, version, namespace, plural, name, body=doc
                                )
                            else:
                                raise

                    else:
                        # Defensive only: plan_manifests rejects unsupported kinds.
                        raise ValueError(f"Planner admitted unsupported kind: {kind}")

                    applied_count += 1
                    logger.info(f"✓ Applied {kind}/{name}")

                except ApiException as e:
                    logger.error(f"API error applying {kind}/{name}: {e.status} - {e.reason}")
                    failed.append(f"{filename}:{kind}/{name}")

        except Exception as e:
            logger.error(f"Failed to apply {filename}: {e}")
            failed.append(filename)

    if not failed and applied_count != expected_count:
        raise RuntimeError(
            f"Manifest apply count mismatch: expected={expected_count} applied={applied_count}"
        )

    # Restart deployments and verify credentials only on the main (full) pass,
    # not on the post-Helm pass
    if post_helm:
        return {
            "AppliedCount": applied_count,
            "ExpectedCount": expected_count,
            "ExpectedResources": expected_resources,
            "FailedCount": len(failed),
            "SkippedCount": len(skipped),
            "Failed": ",".join(failed) if failed else "None",
            "Skipped": ",".join(skipped) if skipped else "None",
            "PrunedCount": len(pruned),
            "Pruned": ",".join(pruned) if pruned else "None",
            "PruneFailures": ",".join(prune_failures) if prune_failures else "None",
        }

    # Restart deployments in gco-system namespace to pick up new images
    # This ensures that any updated container images are actually deployed
    gco_deployments = [
        "health-monitor",
        "manifest-processor",
        "inference-monitor",
        "inference-proxy",
    ]
    logger.info(f"Restarting deployments in gco-system: {gco_deployments}")
    restart_result = restart_deployments("gco-system", gco_deployments)

    # Restart the EFS CSI controller so it picks up the IRSA role-ARN
    # annotation that the EKS addon update patched onto its service account.
    #
    # Background: the managed aws-efs-csi-driver addon creates the
    # efs-csi-controller-sa ServiceAccount and the controller Deployment in
    # parallel. Our stack later calls UpdateAddon with a
    # serviceAccountRoleArn, which patches the SA's eks.amazonaws.com/role-arn
    # annotation — but EKS does NOT restart the controller pods. The existing
    # pods keep their original (un-mutated) pod spec: no AWS_ROLE_ARN,
    # no AWS_WEB_IDENTITY_TOKEN_FILE. They then fall back to IMDS for
    # credentials, which EKS Auto Mode blocks at the pod network level
    # (hop-limit / security policy), causing every EFS CreateAccessPoint
    # call to fail with HTTP 401. The visible symptom is a PVC that stays
    # Pending forever with "no EC2 IMDS role found".
    #
    # Restarting the deployment forces a new pod template to go through the
    # EKS Pod Identity / IRSA mutating webhook, which sees the annotation
    # this time and injects the projected-token volume and env vars.
    #
    # The same pattern applies to:
    #   - aws-fsx-csi-driver: fsx-csi-controller Deployment +
    #     fsx-csi-node DaemonSet
    #   - amazon-cloudwatch-observability: cloudwatch-agent DaemonSet
    #   - aws-efs-csi-driver: efs-csi-node DaemonSet (the controller already
    #     above)
    #
    # Deployments/DaemonSets that don't exist on this cluster (e.g. FSx when
    # fsx_lustre.enabled=false) return 404 and are skipped gracefully — see
    # the 404 branch in restart_deployments/restart_daemonsets.
    kube_system_deployments = ["efs-csi-controller", "fsx-csi-controller"]
    logger.info(f"Restarting deployments in kube-system: {kube_system_deployments}")
    ks_deploy_restart = restart_deployments("kube-system", kube_system_deployments)

    kube_system_daemonsets = ["efs-csi-node", "fsx-csi-node"]
    logger.info(f"Restarting daemonsets in kube-system: {kube_system_daemonsets}")
    ks_ds_restart = restart_daemonsets("kube-system", kube_system_daemonsets)

    # CloudWatch agent daemonset runs in its own namespace when the
    # amazon-cloudwatch-observability addon is installed.
    cw_daemonsets = ["cloudwatch-agent"]
    logger.info(f"Restarting daemonsets in amazon-cloudwatch: {cw_daemonsets}")
    cw_ds_restart = restart_daemonsets("amazon-cloudwatch", cw_daemonsets)

    # Verify IAM credentials are available for workloads
    # Check that the projected service-account token volume is configured
    # on key deployments — if missing, IRSA won't work
    credential_warnings = _verify_workload_credentials(apps_v1)

    # Combine the restart results for the return payload.
    all_restarted = (
        restart_result["restarted"]
        + ks_deploy_restart["restarted"]
        + ks_ds_restart["restarted"]
        + cw_ds_restart["restarted"]
    )

    return {
        "AppliedCount": applied_count,
        "ExpectedCount": expected_count,
        "ExpectedResources": expected_resources,
        "FailedCount": len(failed),
        "SkippedCount": len(skipped),
        "Failed": ",".join(failed) if failed else "None",
        "Skipped": ",".join(skipped) if skipped else "None",
        "RestartedDeployments": (",".join(all_restarted) if all_restarted else "None"),
        "CredentialWarnings": ",".join(credential_warnings) if credential_warnings else "None",
        "PrunedCount": len(pruned),
        "Pruned": ",".join(pruned) if pruned else "None",
        "PruneFailures": ",".join(prune_failures) if prune_failures else "None",
    }


def _delete_gateway_resources(cluster_name: str, region: str) -> dict[str, Any]:
    """Delete Gateway objects while the LBC controller is still running.

    The Gateway is deleted only after its routes, and every object is polled to
    exact absence. Waiting for the Gateway's controller finalizer is what proves
    the ALB has been removed before the LBC Helm release is uninstalled.
    """
    configure_k8s_client(cluster_name, region)
    custom_api = client.CustomObjectsApi()
    namespace = "gco-system"
    delete_options = client.V1DeleteOptions(propagation_policy="Foreground")
    resources = (
        ("HTTPRoute", "gco-routes"),
        ("Gateway", "gco-gateway"),
        ("LoadBalancerConfiguration", "gco-gateway-load-balancer"),
        ("TargetGroupConfiguration", "gco-default-target-group"),
        ("GatewayClass", "gco-aws-alb"),
    )
    deleted: list[str] = []
    deadline = time.monotonic() + _GATEWAY_DELETE_WAIT_SECONDS

    for kind, name in resources:
        group, version, plural, cluster_scoped = _GATEWAY_CUSTOM_OBJECTS[kind]
        label = f"{kind}/{name}" if cluster_scoped else f"{kind}/{namespace}/{name}"
        try:
            if cluster_scoped:
                custom_api.delete_cluster_custom_object(
                    group, version, plural, name, body=delete_options
                )
            else:
                custom_api.delete_namespaced_custom_object(
                    group, version, namespace, plural, name, body=delete_options
                )
        except ApiException as exc:
            if exc.status != 404:
                raise RuntimeError(
                    f"failed to delete {label}: Kubernetes API {exc.status} ({exc.reason})"
                ) from exc
            deleted.append(f"{label}:already-absent")
            continue

        while True:
            try:
                if cluster_scoped:
                    custom_api.get_cluster_custom_object(group, version, plural, name)
                else:
                    custom_api.get_namespaced_custom_object(group, version, namespace, plural, name)
            except ApiException as exc:
                if exc.status == 404:
                    deleted.append(label)
                    break
                raise RuntimeError(
                    f"failed while waiting for {label} deletion: "
                    f"Kubernetes API {exc.status} ({exc.reason})"
                ) from exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "timed out waiting for the complete Gateway resource set to delete "
                    f"within {_GATEWAY_DELETE_WAIT_SECONDS}s; last resource was {label}"
                )
            time.sleep(_GATEWAY_DELETE_POLL_SECONDS)

    return {"status": "deleted", "DeletedCount": len(deleted), "Deleted": deleted}


def _as_plain_dict(value: Any) -> dict[str, Any]:
    """Convert a DynamicClient response to a plain mapping."""
    if isinstance(value, dict):
        return value
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, dict):
            return converted
    try:
        converted = dict(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Kubernetes response is not a mapping: {type(value).__name__}") from exc
    return converted


def _condition_matches(condition: dict[str, Any], expected_status: str) -> bool:
    value = condition.get("status")
    if isinstance(value, bool):
        value = "True" if value else "False"
    return str(value).lower() == expected_status.lower()


def _conditions(status: dict[str, Any]) -> list[dict[str, Any]]:
    value = status.get("conditions", [])
    if not isinstance(value, list):
        return []
    return [condition for condition in value if isinstance(condition, dict)]


def _required_condition_failure(
    status: dict[str, Any],
    condition_type: str,
) -> str | None:
    matching = [item for item in _conditions(status) if item.get("type") == condition_type]
    if any(_condition_matches(item, "True") for item in matching):
        return None
    if matching:
        details = matching[-1].get("message") or matching[-1].get("reason") or "status is not True"
        return f"condition {condition_type} is not True ({details})"
    return f"condition {condition_type} is missing"


def _generation_failure(resource: dict[str, Any]) -> str | None:
    metadata_value = resource.get("metadata")
    status_value = resource.get("status")
    metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
    status: dict[str, Any] = status_value if isinstance(status_value, dict) else {}
    generation = metadata.get("generation")
    observed = status.get("observedGeneration")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or not isinstance(observed, int)
        or isinstance(observed, bool)
        or observed != generation
    ):
        return f"generation not observed (generation={generation}, observedGeneration={observed})"
    return None


def _replica_failure(
    status: dict[str, Any],
    desired: int,
    fields: tuple[str, ...],
) -> str | None:
    for field in fields:
        # Kubernetes omits optional zero-valued counters. Treat omission as
        # zero while still rejecting malformed non-integer values.
        value = status.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value != desired:
            return f"replicas not converged ({field}={value}, desired={desired})"
    unavailable = status.get("unavailableReplicas", status.get("numberUnavailable", 0))
    if not isinstance(unavailable, int) or isinstance(unavailable, bool) or unavailable != 0:
        return f"replicas unavailable ({unavailable})"
    return None


def _current_condition_failure(
    conditions: list[dict[str, Any]],
    condition_type: str,
    generation: Any,
    context: str,
) -> str | None:
    """Require a True condition that observed the object's current generation."""
    current = [
        condition
        for condition in conditions
        if condition.get("type") == condition_type
        and condition.get("observedGeneration") == generation
    ]
    if any(_condition_matches(condition, "True") for condition in current):
        return None
    matching = [condition for condition in conditions if condition.get("type") == condition_type]
    if current:
        detail = current[-1].get("message") or current[-1].get("reason") or "status is not True"
        return f"{context} condition {condition_type} is not True ({detail})"
    if matching:
        observed = matching[-1].get("observedGeneration")
        return (
            f"{context} condition {condition_type} is stale "
            f"(generation={generation}, observedGeneration={observed})"
        )
    return f"{context} condition {condition_type} is missing"


def _gateway_api_readiness_failure(kind: str, resource: dict[str, Any]) -> str | None:
    """Return strict, generation-aware Gateway API readiness evidence."""
    metadata_value = resource.get("metadata")
    spec_value = resource.get("spec")
    status_value = resource.get("status")
    metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
    spec: dict[str, Any] = spec_value if isinstance(spec_value, dict) else {}
    status: dict[str, Any] = status_value if isinstance(status_value, dict) else {}
    generation = metadata.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        return f"invalid metadata.generation ({generation})"

    if kind == "GatewayClass":
        return _current_condition_failure(
            _conditions(status), "Accepted", generation, "GatewayClass"
        )

    if kind == "Gateway":
        for condition_type in ("Accepted", "Programmed"):
            failure = _current_condition_failure(
                _conditions(status), condition_type, generation, "Gateway"
            )
            if failure:
                return failure
        addresses = status.get("addresses", [])
        if not isinstance(addresses, list) or not any(
            isinstance(address, dict) and str(address.get("value", "")).strip()
            for address in addresses
        ):
            return "Gateway has no address"

        intended_listeners: set[str] = {
            listener["name"]
            for listener in spec.get("listeners", [])
            if isinstance(listener, dict) and isinstance(listener.get("name"), str)
        }
        listener_statuses = status.get("listeners", [])
        if not isinstance(listener_statuses, list):
            return "Gateway listener status is missing"
        by_name = {
            listener.get("name"): listener
            for listener in listener_statuses
            if isinstance(listener, dict) and isinstance(listener.get("name"), str)
        }
        for listener_name in sorted(intended_listeners):
            listener = by_name.get(listener_name)
            if not isinstance(listener, dict):
                return f"Gateway listener {listener_name!r} status is missing"
            listener_conditions = listener.get("conditions", [])
            if not isinstance(listener_conditions, list):
                return f"Gateway listener {listener_name!r} conditions are missing"
            plain_conditions = [item for item in listener_conditions if isinstance(item, dict)]
            for condition_type in ("Accepted", "ResolvedRefs", "Programmed"):
                failure = _current_condition_failure(
                    plain_conditions,
                    condition_type,
                    generation,
                    f"Gateway listener {listener_name!r}",
                )
                if failure:
                    return failure
        return None

    if kind == "HTTPRoute":
        namespace = str(metadata.get("namespace", "default"))

        def parent_key(reference: dict[str, Any]) -> tuple[str, str, str, str, str]:
            return (
                str(reference.get("group", "gateway.networking.k8s.io")),
                str(reference.get("kind", "Gateway")),
                str(reference.get("namespace", namespace)),
                str(reference.get("name", "")),
                str(reference.get("sectionName", "")),
            )

        intended: set[tuple[str, str, str, str, str]] = {
            parent_key(parent) for parent in spec.get("parentRefs", []) if isinstance(parent, dict)
        }
        parents = status.get("parents", [])
        if not isinstance(parents, list):
            return "HTTPRoute parent status is missing"
        live_by_ref = {
            parent_key(parent["parentRef"]): parent
            for parent in parents
            if isinstance(parent, dict) and isinstance(parent.get("parentRef"), dict)
        }
        for reference in sorted(intended):
            parent = live_by_ref.get(reference)
            if not isinstance(parent, dict):
                return f"HTTPRoute intended parent {reference} status is missing"
            parent_conditions = parent.get("conditions", [])
            if not isinstance(parent_conditions, list):
                return f"HTTPRoute intended parent {reference} conditions are missing"
            plain_conditions = [item for item in parent_conditions if isinstance(item, dict)]
            for condition_type in ("Accepted", "ResolvedRefs"):
                failure = _current_condition_failure(
                    plain_conditions,
                    condition_type,
                    generation,
                    f"HTTPRoute parent {reference}",
                )
                if failure:
                    return failure
        return None

    return None


def _resource_readiness_failure(kind: str, resource: dict[str, Any]) -> str | None:
    """Return an actionable readiness reason, or None when the object is ready."""
    metadata_value = resource.get("metadata")
    spec_value = resource.get("spec")
    status_value = resource.get("status")
    metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
    spec: dict[str, Any] = spec_value if isinstance(spec_value, dict) else {}
    status: dict[str, Any] = status_value if isinstance(status_value, dict) else {}

    if metadata.get("deletionTimestamp"):
        return f"object is terminating since {metadata['deletionTimestamp']}"

    for condition in _conditions(status):
        if condition.get("type") in {"Ready", "Available"} and _condition_matches(
            condition, "False"
        ):
            detail = condition.get("message") or condition.get("reason") or "no detail"
            return f"condition {condition.get('type')} is False ({detail})"

    if kind in {"GatewayClass", "Gateway", "HTTPRoute"}:
        return _gateway_api_readiness_failure(kind, resource)

    if kind in {"Deployment", "StatefulSet"}:
        generation_failure = _generation_failure(resource)
        if generation_failure:
            return generation_failure
        desired_value = spec.get("replicas", 1)
        if not isinstance(desired_value, int) or isinstance(desired_value, bool):
            return f"invalid desired replica count ({desired_value})"
        desired = desired_value
        fields = (
            ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
            if kind == "Deployment"
            else ("currentReplicas", "updatedReplicas", "readyReplicas")
        )
        return _replica_failure(status, desired, fields)

    if kind == "DaemonSet":
        generation_failure = _generation_failure(resource)
        if generation_failure:
            return generation_failure
        desired_value = status.get("desiredNumberScheduled")
        if not isinstance(desired_value, int) or isinstance(desired_value, bool):
            return f"invalid desiredNumberScheduled ({desired_value})"
        desired = desired_value
        replica_failure = _replica_failure(
            status,
            desired,
            (
                "currentNumberScheduled",
                "updatedNumberScheduled",
                "numberReady",
                "numberAvailable",
            ),
        )
        if replica_failure:
            return replica_failure
        misscheduled = status.get("numberMisscheduled", 0)
        if not isinstance(misscheduled, int) or isinstance(misscheduled, bool) or misscheduled != 0:
            return f"pods are misscheduled (numberMisscheduled={misscheduled})"
        return None

    if kind == "Job":
        failed = _required_condition_failure(status, "Failed")
        if failed is None:
            return "condition Failed is True"
        return _required_condition_failure(status, "Complete")

    if kind == "Pod":
        return _required_condition_failure(status, "Ready")

    if kind == "PersistentVolumeClaim":
        phase = status.get("phase")
        return None if phase == "Bound" else f"PVC phase is {phase!r}, expected 'Bound'"

    if kind == "PersistentVolume":
        phase = status.get("phase")
        return (
            None
            if phase in {"Bound", "Available"}
            else f"PV phase is {phase!r}, expected 'Bound' or 'Available'"
        )

    if kind == "Ingress":
        load_balancer = status.get("loadBalancer")
        addresses = load_balancer.get("ingress", []) if isinstance(load_balancer, dict) else []
        if not isinstance(addresses, list) or not any(
            isinstance(address, dict) and (address.get("hostname") or address.get("ip"))
            for address in addresses
        ):
            return "Ingress has no load-balancer hostname or IP address"
        return None

    if kind == "CustomResourceDefinition":
        return _required_condition_failure(status, "Established")

    if kind == "APIService":
        return _required_condition_failure(status, "Available")

    if kind == "HorizontalPodAutoscaler":
        for condition_type in ("AbleToScale", "ScalingActive"):
            failure = _required_condition_failure(status, condition_type)
            if failure:
                return failure
        return None

    if kind == "PodDisruptionBudget":
        generation_failure = _generation_failure(resource)
        if generation_failure:
            return generation_failure
        current_healthy = status.get("currentHealthy")
        desired_healthy = status.get("desiredHealthy")
        if (
            not isinstance(current_healthy, int)
            or isinstance(current_healthy, bool)
            or not isinstance(desired_healthy, int)
            or isinstance(desired_healthy, bool)
        ):
            return (
                "invalid PDB health "
                f"(currentHealthy={current_healthy}, desiredHealthy={desired_healthy})"
            )
        if current_healthy < desired_healthy:
            return (
                "PDB health below target "
                f"(currentHealthy={current_healthy}, desiredHealthy={desired_healthy})"
            )
        return None

    if kind in {"NodePool", "EC2NodeClass", "ScaledJob", "ScaledObject"}:
        return _required_condition_failure(status, "Ready")

    # Static configuration and RBAC resources have no rollout contract. Their
    # exact-object existence is sufficient unless a generic Ready/Available
    # condition explicitly reported False above.
    return None


def _dynamic_resource(
    dynamic_client: Any,
    cache: dict[tuple[str, str], Any],
    api_version: str,
    kind: str,
) -> Any:
    key = (api_version, kind)
    if key not in cache:
        cache[key] = dynamic_client.resources.get(api_version=api_version, kind=kind)
    return cache[key]


def _service_endpoint_failure(
    dynamic_client: Any,
    cache: dict[tuple[str, str], Any],
    planned_resource: dict[str, Any],
) -> str | None:
    document_value = planned_resource["document"]
    document: dict[str, Any] = document_value if isinstance(document_value, dict) else {}
    spec_value = document.get("spec")
    spec: dict[str, Any] = spec_value if isinstance(spec_value, dict) else {}
    selector = spec.get("selector")
    if not isinstance(selector, dict) or not selector:
        return None

    endpoint_slices = _dynamic_resource(
        dynamic_client,
        cache,
        "discovery.k8s.io/v1",
        "EndpointSlice",
    )
    response = endpoint_slices.get(
        namespace=planned_resource["namespace"],
        label_selector=f"kubernetes.io/service-name={planned_resource['name']}",
    )
    items = _as_plain_dict(response).get("items", [])
    if not isinstance(items, list):
        return "EndpointSlice response does not contain an items list"
    for item in items:
        item_mapping = _as_plain_dict(item)
        metadata_value = item_mapping.get("metadata")
        metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
        if metadata.get("deletionTimestamp"):
            continue
        endpoints = item_mapping.get("endpoints", [])
        if not isinstance(endpoints, list):
            continue
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            conditions = endpoint.get("conditions", {})
            if not isinstance(conditions, dict):
                continue
            if conditions.get("ready") is True and conditions.get("terminating") is not True:
                return None
    return "selector-backed Service has no ready, nonterminating EndpointSlice endpoint"


def _manifest_identity_label(resource: dict[str, Any]) -> str:
    return (
        f"{resource['apiVersion']}/{resource['kind']}/"
        f"{resource['namespace']}/{resource['name']}"
        f" [{resource['phase']}:{resource['sourceFile']}]"
    )


def validate_manifests(
    cluster_name: str,
    region: str,
    manifests_dir: str,
    replacements: dict[str, str],
    deployment_token: Any = None,
) -> dict[str, Any]:
    """Validate exact existence and readiness for the complete planned inventory."""
    plan = plan_manifests(manifests_dir, replacements)
    expected = plan["phases"]["base"] + plan["phases"]["post-helm"]
    expected_resources = [_public_manifest_identity(item) for item in expected]
    phase_counts = {
        phase: {"ExpectedCount": len(plan["phases"][phase]), "ValidatedCount": 0}
        for phase in ("base", "post-helm")
    }

    configure_k8s_client(cluster_name, region)
    dynamic_client = dynamic.DynamicClient(client.ApiClient())
    resource_cache: dict[tuple[str, str], Any] = {}
    validated_resources: list[dict[str, str]] = []
    failures: list[str] = []
    failure_count = 0

    def add_failure(message: str) -> None:
        nonlocal failure_count
        failure_count += 1
        if len(failures) < _MAX_VALIDATION_FAILURES:
            failures.append(message[:500])

    for planned_resource in expected:
        label = _manifest_identity_label(planned_resource)
        try:
            resource_api = _dynamic_resource(
                dynamic_client,
                resource_cache,
                planned_resource["apiVersion"],
                planned_resource["kind"],
            )
            get_kwargs: dict[str, Any] = {"name": planned_resource["name"]}
            if planned_resource["namespace"] != _CLUSTER_SCOPE:
                get_kwargs["namespace"] = planned_resource["namespace"]
            live_resource = _as_plain_dict(resource_api.get(**get_kwargs))
            failure = _resource_readiness_failure(planned_resource["kind"], live_resource)
            if failure is None and planned_resource["kind"] == "Service":
                failure = _service_endpoint_failure(
                    dynamic_client,
                    resource_cache,
                    planned_resource,
                )
            if failure:
                add_failure(f"{label}: {failure}")
                continue
        except (NotFoundError, ResourceNotFoundError) as exc:
            add_failure(f"{label}: object or API resource not found ({exc})")
            continue
        except ApiException as exc:
            detail = exc.reason or str(exc)
            add_failure(f"{label}: Kubernetes API error {exc.status} ({detail})")
            continue
        except Exception as exc:
            add_failure(f"{label}: validation error ({exc})")
            continue

        identity = _public_manifest_identity(planned_resource)
        validated_resources.append(identity)
        phase_counts[planned_resource["phase"]]["ValidatedCount"] += 1

    if failure_count:
        hidden = failure_count - len(failures)
        suffix = f"; ... {hidden} additional failure(s)" if hidden else ""
        raise RuntimeError(
            f"Manifest validation failed: validated={len(validated_resources)} "
            f"expected={len(expected)}; " + "; ".join(failures) + suffix
        )

    return {
        "status": "validated",
        "DeploymentToken": deployment_token,
        "ExpectedCount": len(expected),
        "ValidatedCount": len(validated_resources),
        "BaseExpectedCount": phase_counts["base"]["ExpectedCount"],
        "BaseValidatedCount": phase_counts["base"]["ValidatedCount"],
        "PostHelmExpectedCount": phase_counts["post-helm"]["ExpectedCount"],
        "PostHelmValidatedCount": phase_counts["post-helm"]["ValidatedCount"],
        "PhaseCounts": phase_counts,
        "ExpectedResources": expected_resources,
        "ValidatedResources": validated_resources,
    }


def _record_phase_status(phase: str, status: str, message: str) -> None:
    """Record a convergence phase's outcome to SSM (best-effort).

    Mirrors the helm worker's per-chart status (``_record_addon_status``) so
    ``gco stacks addons status`` surfaces the base and post-Helm apply passes
    alongside the charts. Writes ``/<project>/addons/<region>/<phase>`` as a
    small JSON blob. Failures are swallowed — status reporting must never turn a
    successful apply into a failure (or vice versa). Reads PROJECT_NAME / REGION
    from the Lambda environment; a no-op if either is unset.
    """
    project = os.environ.get("PROJECT_NAME")
    region = os.environ.get("REGION")
    if not project or not region:
        return
    import contextlib
    import time as _time

    with contextlib.suppress(Exception):
        boto3.client("ssm").put_parameter(
            Name=f"/{project}/addons/{region}/{phase}",
            Value=json.dumps(
                {
                    "phase": phase,
                    "status": status,
                    "message": message[:1024],
                    "updated_at": int(_time.time()),
                }
            ),
            Type="String",
            Overwrite=True,
        )


def handle_task(event: dict[str, Any]) -> dict[str, Any]:
    """Run an apply or exhaustive manifest-validation Step Functions task."""
    cluster_name = event["ClusterName"]
    region = event["Region"]
    replacements = event.get("ImageReplacements", {})
    action = event.get("Action", "apply_manifests")
    manifests_dir = os.path.join(os.path.dirname(__file__), "manifests")

    if action == "delete_gateway_resources":
        phase = "gateway-teardown"
        try:
            result = _delete_gateway_resources(cluster_name, region)
        except Exception as exc:
            _record_phase_status(phase, "failed", str(exc))
            raise
        _record_phase_status(
            phase,
            "deleted",
            f"deleted={result['DeletedCount']}",
        )
        return result

    if action == "validate_manifests":
        phase = "manifest-validation"
        deployment_token = event.get("DeploymentToken")
        try:
            result = validate_manifests(
                cluster_name,
                region,
                manifests_dir,
                replacements,
                deployment_token,
            )
        except Exception as exc:
            _record_phase_status(phase, "failed", f"token={deployment_token} {exc}")
            raise
        _record_phase_status(
            phase,
            "validated",
            f"token={deployment_token} validated={result['ValidatedCount']} "
            f"expected={result['ExpectedCount']}",
        )
        return result

    if action != "apply_manifests":
        raise ValueError(f"Unsupported task action: {action}")

    post_helm = str(event.get("PostHelm", "false")).lower() == "true"
    phase = "post-helm-manifests" if post_helm else "base-manifests"
    try:
        result = apply_manifests(cluster_name, region, manifests_dir, replacements, post_helm)
    except Exception as exc:
        _record_phase_status(phase, "failed", str(exc))
        raise
    if result.get("FailedCount", 0):
        _record_phase_status(phase, "failed", str(result.get("Failed")))
        raise RuntimeError(
            f"kubectl apply failed (post_helm={post_helm}, "
            f"failed={result.get('FailedCount')}): {result.get('Failed')}"
        )
    if result.get("ExpectedCount") is not None and result.get("AppliedCount") != result.get(
        "ExpectedCount"
    ):
        message = (
            f"apply count mismatch: applied={result.get('AppliedCount')} "
            f"expected={result.get('ExpectedCount')}"
        )
        _record_phase_status(phase, "failed", message)
        raise RuntimeError(message)
    _record_phase_status(
        phase,
        "applied",
        f"applied={result.get('AppliedCount')} expected={result.get('ExpectedCount')} "
        f"skipped={result.get('SkippedCount')}",
    )
    return result


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    """Main Lambda handler.

    Two entrypoints share this function:

    - **Step Functions task** (the convergence pipeline): the event carries an
      ``Action`` key and is dispatched to :func:`handle_task`, which applies the
      manifests and raises on any failure.
    - **CloudFormation custom resource** (legacy/fallback): the event carries a
      ``RequestType`` and the result is POSTed back to CloudFormation.
    """
    if event.get("Action"):
        logger.info(f"Task event: {json.dumps(event)}")
        return handle_task(event)

    print(f"[HANDLER] Received event type: {event.get('RequestType')}")
    logger.info(f"Received event: {json.dumps(event)}")

    request_type = event["RequestType"]
    physical_resource_id = event.get("PhysicalResourceId", f"kubectl-{event['LogicalResourceId']}")

    try:
        properties = event["ResourceProperties"]
        cluster_name = properties["ClusterName"]
        region = properties["Region"]

        if request_type == "Create" or request_type == "Update":
            manifests_dir = os.path.join(os.path.dirname(__file__), "manifests")
            replacements = properties.get("ImageReplacements", {})
            # PostHelm: "true" means this is the post-Helm pass — apply only post-helm-* files
            post_helm = properties.get("PostHelm", "false").lower() == "true"
            response_data = apply_manifests(
                cluster_name, region, manifests_dir, replacements, post_helm
            )
            send_response(event, context, SUCCESS, response_data, physical_resource_id)

        elif request_type == "Delete":
            # Always succeed on delete to prevent stack from getting stuck
            skip_deletion = properties.get("SkipDeletionOnStackDelete", "false").lower() == "true"
            if skip_deletion:
                logger.info("Skipping deletion (SkipDeletionOnStackDelete=true)")
            response_data = {"Status": "Deleted"}
            send_response(event, context, SUCCESS, response_data, physical_resource_id)

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        # On delete, always return success to prevent stack from getting stuck
        if request_type == "Delete":
            send_response(
                event,
                context,
                SUCCESS,
                {"Status": "Forced success on delete"},
                physical_resource_id,
            )
        else:
            send_response(event, context, FAILED, {}, physical_resource_id, str(e))
