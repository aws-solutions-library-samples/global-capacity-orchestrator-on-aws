"""
CDK stacks for GCO (Global Capacity Orchestrator on AWS).

The public stack classes are imported lazily.  Besides reducing import cost,
this keeps neutral configuration helpers in ``gco.stacks.constants`` usable
while ``gco.config`` is still initializing.
"""

from __future__ import annotations

from importlib import import_module
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

_LAZY_EXPORTS = {
    "GCOApiGatewayGlobalStack": (".api_gateway_global_stack", "GCOApiGatewayGlobalStack"),
    "GCOGlobalStack": (".global_stack", "GCOGlobalStack"),
    "GCOMonitoringStack": (".monitoring_stack", "GCOMonitoringStack"),
    "GCORegionalStack": (".regional_stack", "GCORegionalStack"),
}


def __getattr__(name: str) -> Any:
    """Load one public stack class on first access and cache the result."""
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy public exports in interactive discovery."""
    return sorted({*globals(), *__all__})
