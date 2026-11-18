"""Dependency-light settings for the platform ALB's target-group health check.

The AWS Load Balancer Controller builds the internal platform ALB from the
Gateway API resources in
``lambda/kubectl-applier-simple/manifests/post-helm-gateway.yaml``. The
default ``TargetGroupConfiguration`` there sets the health check every API
target group inherits. Its four numbers come from the cdk.json
``gateway_health_check`` block, rendered into the manifest through the
kubectl-applier's ``{{TOKEN}}`` replacements.

This module holds the defaults, the validation and the renderer without
importing AWS CDK, so the regional stack, ``ConfigLoader``, the cluster
gateway OpenAPI generator and the tests share one source.
"""

from __future__ import annotations

from collections.abc import Mapping

#: cdk.json context key of the block.
GATEWAY_HEALTH_CHECK_CONTEXT_KEY = "gateway_health_check"

#: What the manifest shipped before the block existed: probe every 15 seconds,
#: time out after 5, and flip state after 2 consecutive results either way.
GATEWAY_HEALTH_CHECK_DEFAULTS: dict[str, int] = {
    "interval_seconds": 15,
    "timeout_seconds": 5,
    "healthy_threshold": 2,
    "unhealthy_threshold": 2,
}

#: Inclusive ranges Elastic Load Balancing accepts for an Application Load
#: Balancer target group (HealthCheckIntervalSeconds, HealthCheckTimeoutSeconds,
#: HealthyThresholdCount, UnhealthyThresholdCount in the CreateTargetGroup API).
GATEWAY_HEALTH_CHECK_RANGES: dict[str, tuple[int, int]] = {
    "interval_seconds": (5, 300),
    "timeout_seconds": (2, 120),
    "healthy_threshold": (2, 10),
    "unhealthy_threshold": (2, 10),
}

#: The manifest placeholder each setting renders into. They sit in bare
#: integer positions, so the values render as plain decimal integers.
GATEWAY_HEALTH_CHECK_TOKENS: dict[str, str] = {
    "interval_seconds": "{{GATEWAY_HEALTH_CHECK_INTERVAL_SECONDS}}",
    "timeout_seconds": "{{GATEWAY_HEALTH_CHECK_TIMEOUT_SECONDS}}",
    "healthy_threshold": "{{GATEWAY_HEALTHY_THRESHOLD_COUNT}}",
    "unhealthy_threshold": "{{GATEWAY_UNHEALTHY_THRESHOLD_COUNT}}",
}


class GatewayHealthCheckConfigError(ValueError):
    """Raised when the ``gateway_health_check`` block is invalid."""


def validate_gateway_health_check_config(raw: object) -> dict[str, int]:
    """Return the block merged over :data:`GATEWAY_HEALTH_CHECK_DEFAULTS`.

    ``None`` (the block is absent) means the defaults. Keys starting with
    ``_comment`` are documentation and are skipped. Every value must be an
    integer (not a boolean) inside its :data:`GATEWAY_HEALTH_CHECK_RANGES`
    range, and the timeout must be shorter than the interval, which Elastic
    Load Balancing requires.

    Raises:
        GatewayHealthCheckConfigError: The block is not an object, names an
            unknown key, or holds a value Elastic Load Balancing rejects.
    """
    key = GATEWAY_HEALTH_CHECK_CONTEXT_KEY
    if raw is None:
        return dict(GATEWAY_HEALTH_CHECK_DEFAULTS)
    if not isinstance(raw, Mapping):
        raise GatewayHealthCheckConfigError(
            f"{key} must be an object, got {type(raw).__name__}: {raw!r}"
        )
    settings = {
        str(name): value for name, value in raw.items() if not str(name).startswith("_comment")
    }
    unknown = sorted(name for name in settings if name not in GATEWAY_HEALTH_CHECK_DEFAULTS)
    if unknown:
        raise GatewayHealthCheckConfigError(
            f"{key} contains unknown key(s): "
            + ", ".join(f"{key}.{name}" for name in unknown)
            + "; allowed keys: "
            + ", ".join(f"{key}.{name}" for name in GATEWAY_HEALTH_CHECK_DEFAULTS)
        )
    merged: dict[str, int] = dict(GATEWAY_HEALTH_CHECK_DEFAULTS)
    for name, value in settings.items():
        minimum, maximum = GATEWAY_HEALTH_CHECK_RANGES[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise GatewayHealthCheckConfigError(
                f"{key}.{name} must be an integer between {minimum} and {maximum}, got {value!r}"
            )
        merged[name] = value
    if merged["timeout_seconds"] >= merged["interval_seconds"]:
        raise GatewayHealthCheckConfigError(
            f"{key}.timeout_seconds ({merged['timeout_seconds']}) must be less than "
            f"{key}.interval_seconds ({merged['interval_seconds']})"
        )
    return merged


def compute_gateway_health_check_replacements(config: Mapping[str, int]) -> dict[str, str]:
    """Render validated settings as the manifest's ``{{TOKEN}}`` replacements."""
    return {token: str(config[name]) for name, token in GATEWAY_HEALTH_CHECK_TOKENS.items()}


def render_gateway_health_check_defaults(text: str) -> str:
    """Substitute the default health check into gateway manifest text.

    For readers of the template that are not the kubectl-applier (the cluster
    gateway OpenAPI generator and the tests), so they see the manifest a stock
    deployment applies.
    """
    for token, value in compute_gateway_health_check_replacements(
        GATEWAY_HEALTH_CHECK_DEFAULTS
    ).items():
        text = text.replace(token, value)
    return text
