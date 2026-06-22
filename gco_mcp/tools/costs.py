"""Cost tracking MCP tools."""

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_summary(days: int = 30) -> str:
    """Get total GCO spend broken down by AWS service.

    Args:
        days: Number of days to look back.
    """
    return cli_runner._run_cli("costs", "summary", "--days", str(days))


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_by_region(days: int = 30) -> str:
    """Get cost breakdown by AWS region.

    Args:
        days: Number of days to look back.
    """
    return cli_runner._run_cli("costs", "regions", "--days", str(days))


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_trend(days: int = 14) -> str:
    """Get daily cost trend.

    Args:
        days: Number of days to show.
    """
    return cli_runner._run_cli("costs", "trend", "--days", str(days))


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_forecast(days_ahead: int = 30) -> str:
    """Forecast GCO costs for the next N days.

    Args:
        days_ahead: Days to forecast ahead.
    """
    return cli_runner._run_cli("costs", "forecast", "--days", str(days_ahead))
