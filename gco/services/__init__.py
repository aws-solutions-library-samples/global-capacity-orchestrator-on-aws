"""
Services for GCO (Global Capacity Orchestrator on AWS).

This package contains the core services that run in the EKS cluster:

- health_monitor: Monitors cluster resource utilization and health status
- health_api: FastAPI service exposing health endpoints
- manifest_processor: Validates and applies Kubernetes manifests
- manifest_api: FastAPI service exposing manifest submission endpoints
- metrics_publisher: Publishes custom metrics to CloudWatch
- auth_middleware: Validates short-lived HMAC request envelopes
- webhook_dispatcher: Dispatches webhook notifications for job events
- inference_monitor: Reconciliation controller for inference endpoints
- inference_store: DynamoDB store for inference endpoint state

Imports are lazy to allow individual services to run in minimal
Docker images without pulling in all dependencies.
"""

from typing import TYPE_CHECKING

# Re-export names for static analysis (type checkers, CodeQL) while keeping
# the actual imports lazy via __getattr__ below. This keeps the package
# importable without pulling in every service's dependencies.
if TYPE_CHECKING:
    from .auth_middleware import AuthenticationMiddleware
    from .health_monitor import HealthMonitor, create_health_monitor_from_env
    from .manifest_processor import ManifestProcessor, create_manifest_processor_from_env
    from .metrics_publisher import (
        HealthMonitorMetrics,
        ManifestProcessorMetrics,
        MetricsPublisher,
        create_health_monitor_metrics,
        create_manifest_processor_metrics,
    )
    from .webhook_dispatcher import WebhookDispatcher, create_webhook_dispatcher_from_env


def __getattr__(name: str) -> object:
    """Lazy import to avoid pulling in all dependencies at package import time."""
    _imports = {
        "AuthenticationMiddleware": ".auth_middleware",
        "HealthMonitor": ".health_monitor",
        "create_health_monitor_from_env": ".health_monitor",
        "ManifestProcessor": ".manifest_processor",
        "create_manifest_processor_from_env": ".manifest_processor",
        "MetricsPublisher": ".metrics_publisher",
        "HealthMonitorMetrics": ".metrics_publisher",
        "ManifestProcessorMetrics": ".metrics_publisher",
        "create_health_monitor_metrics": ".metrics_publisher",
        "create_manifest_processor_metrics": ".metrics_publisher",
        "WebhookDispatcher": ".webhook_dispatcher",
        "create_webhook_dispatcher_from_env": ".webhook_dispatcher",
    }
    if name in _imports:
        import importlib

        mod = _imports[name]
        module = importlib.import_module(mod, __package__)  # nosemgrep
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AuthenticationMiddleware",
    "HealthMonitor",
    "HealthMonitorMetrics",
    "ManifestProcessor",
    "ManifestProcessorMetrics",
    "MetricsPublisher",
    "WebhookDispatcher",
    "create_health_monitor_from_env",
    "create_health_monitor_metrics",
    "create_manifest_processor_from_env",
    "create_manifest_processor_metrics",
    "create_webhook_dispatcher_from_env",
]
