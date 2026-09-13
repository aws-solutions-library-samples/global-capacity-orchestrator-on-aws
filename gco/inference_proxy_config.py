"""Dependency-light inference proxy autoscaling defaults and rendering."""

from __future__ import annotations

from collections.abc import Mapping

INFERENCE_PROXY_TLS_CPU_REQUEST_MILLICORES_DEFAULT = 100
INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION_DEFAULT = 70
#: HPA floor and ceiling for the shared proxy Deployment. The floor is also the
#: Deployment's create-time ``replicas`` (the HPA owns the count afterwards).
INFERENCE_PROXY_MIN_REPLICAS_DEFAULT = 3
INFERENCE_PROXY_MAX_REPLICAS_DEFAULT = 10


def compute_inference_proxy_tls_replacements(
    config: Mapping[str, object],
) -> dict[str, str]:
    """Render the typed inference-proxy placeholders without importing AWS CDK.

    Two shapes matter to the applier and to kubeconform: the TLS CPU request
    is a quoted Kubernetes quantity (``"100m"``), while the HPA target and the
    replica bounds are bare integers in the manifest.
    """
    request = config["tls_proxy_cpu_request_millicores"]
    target = config["tls_proxy_cpu_target_utilization_percentage"]
    min_replicas = config.get("min_replicas", INFERENCE_PROXY_MIN_REPLICAS_DEFAULT)
    max_replicas = config.get("max_replicas", INFERENCE_PROXY_MAX_REPLICAS_DEFAULT)
    if (
        type(request) is not int
        or type(target) is not int
        or type(min_replicas) is not int
        or type(max_replicas) is not int
    ):
        raise ValueError("validated inference proxy TLS settings must be integers")
    return {
        "{{INFERENCE_PROXY_TLS_CPU_REQUEST}}": f"{request}m",
        "{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}": str(target),
        "{{INFERENCE_PROXY_MIN_REPLICAS}}": str(min_replicas),
        "{{INFERENCE_PROXY_MAX_REPLICAS}}": str(max_replicas),
    }
