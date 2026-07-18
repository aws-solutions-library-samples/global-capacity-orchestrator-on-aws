"""
CDK stacks for GCO (Global Capacity Orchestrator on AWS).

The public stack classes are imported lazily.  Besides reducing import cost,
this keeps neutral configuration helpers in ``gco.stacks.constants`` usable
while ``gco.config`` is still initializing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .api_gateway_global_stack import GCOApiGatewayGlobalStack
    from .global_stack import GCOGlobalStack
    from .monitoring_stack import GCOMonitoringStack
    from .regional_stack import GCORegionalStack

__all__ = [
    "GCOApiGatewayGlobalStack",
    "GCOGlobalStack",
    "GCOMonitoringStack",
    "GCORegionalStack",
]


def _load_api_gateway_global_stack() -> Any:
    from .api_gateway_global_stack import GCOApiGatewayGlobalStack  # noqa: PLC0415

    return GCOApiGatewayGlobalStack


def _load_global_stack() -> Any:
    from .global_stack import GCOGlobalStack  # noqa: PLC0415

    return GCOGlobalStack


def _load_monitoring_stack() -> Any:
    from .monitoring_stack import GCOMonitoringStack  # noqa: PLC0415

    return GCOMonitoringStack


def _load_regional_stack() -> Any:
    from .regional_stack import GCORegionalStack  # noqa: PLC0415

    return GCORegionalStack


_LAZY_EXPORTS: dict[str, Callable[[], Any]] = {
    "GCOApiGatewayGlobalStack": _load_api_gateway_global_stack,
    "GCOGlobalStack": _load_global_stack,
    "GCOMonitoringStack": _load_monitoring_stack,
    "GCORegionalStack": _load_regional_stack,
}


def __getattr__(name: str) -> Any:
    """Load one public stack class on first access and cache the result."""
    try:
        loader = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = loader()
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy public exports in interactive discovery."""
    return sorted({*globals(), *__all__})
