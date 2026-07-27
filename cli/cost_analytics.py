"""Athena-backed cost analytics for the GCO CLI.

Queries the cost allocation data the per-region cost-monitor services write
to the central cost report bucket (Hive-partitioned Parquet), through the
Glue table + Athena workgroup the monitoring stack provisions. Query results
land in the workgroup-enforced ``athena-results/`` prefix, KMS-encrypted and
lifecycle-expired.

All SQL is assembled from validated identifiers and integer bounds only —
user-supplied strings (namespace filters) are passed through Athena
``ExecutionParameters``, never interpolated.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import boto3

from .config import GCOConfig, get_config

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SECONDS = 1.0
_DEFAULT_TIMEOUT_SECONDS = 120.0
_MAX_RESULT_ROWS = 1_000

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_GRANULARITY_EXPRESSIONS = {
    "daily": "date",
    "hourly": "date_format(window_start, '%Y-%m-%dT%H:00Z')",
}

_TOP_GROUPINGS = {
    "namespace": "namespace",
    "region": "region",
    "cluster": "cluster",
}


class AthenaQueryError(RuntimeError):
    """Raised when an Athena query fails, is cancelled, or times out."""


def _database_name(project_name: str) -> str:
    """Glue database name — must mirror gco.stacks.constants.cost_glue_database_name."""
    return f"{project_name.replace('-', '_')}_cost"


def _workgroup_name(project_name: str) -> str:
    """Athena workgroup — must mirror gco.stacks.constants.cost_athena_workgroup_name."""
    return f"{project_name}-cost"


@dataclass
class QueryResult:
    """Column names plus row dictionaries for one executed query."""

    columns: list[str]
    rows: list[dict[str, Any]]
    query_execution_id: str


class CostAnalytics:
    """Runs canned cost aggregation queries through Athena."""

    def __init__(
        self,
        config: GCOConfig | None = None,
        *,
        athena_client: Any | None = None,
        region: str | None = None,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config or get_config()
        self.project_name = self._config.project_name
        # Athena + Glue live in the monitoring region.
        self.region = region or self._monitoring_region()
        self.database = _database_name(self.project_name)
        self.workgroup = _workgroup_name(self.project_name)
        self.table = "allocation_reports"
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self._athena = athena_client or boto3.client("athena", region_name=self.region)

    def _monitoring_region(self) -> str:
        from .config import _load_cdk_json

        regions = _load_cdk_json() or {}
        monitoring = regions.get("monitoring")
        if isinstance(monitoring, str) and monitoring:
            return monitoring
        return str(self._config.default_region)

    # ------------------------------------------------------------------
    # Query execution plumbing
    # ------------------------------------------------------------------

    def run_query(self, sql: str, parameters: list[str] | None = None) -> QueryResult:
        """Execute one query in the cost workgroup and return decoded rows."""
        start_kwargs: dict[str, Any] = {
            "QueryString": sql,
            "QueryExecutionContext": {"Database": self.database},
            "WorkGroup": self.workgroup,
        }
        if parameters:
            start_kwargs["ExecutionParameters"] = parameters
        try:
            start = self._athena.start_query_execution(**start_kwargs)
        except Exception as exc:  # noqa: BLE001 - boto error shapes vary
            raise AthenaQueryError(
                f"Failed to start Athena query (workgroup {self.workgroup}, "
                f"database {self.database}): {exc}"
            ) from exc

        execution_id = str(start["QueryExecutionId"])
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            execution = self._athena.get_query_execution(QueryExecutionId=execution_id)
            state = execution["QueryExecution"]["Status"]["State"]
            if state == "SUCCEEDED":
                break
            if state in {"FAILED", "CANCELLED"}:
                reason = execution["QueryExecution"]["Status"].get(
                    "StateChangeReason", "no reason provided"
                )
                raise AthenaQueryError(f"Athena query {state.lower()}: {reason}")
            if time.monotonic() >= deadline:
                self._athena.stop_query_execution(QueryExecutionId=execution_id)
                raise AthenaQueryError(f"Athena query timed out after {self.timeout_seconds:.0f}s")
            time.sleep(self.poll_seconds)

        return self._collect_results(execution_id)

    def _collect_results(self, execution_id: str) -> QueryResult:
        paginator = self._athena.get_paginator("get_query_results")
        columns: list[str] = []
        rows: list[dict[str, Any]] = []
        header_skipped = False
        for page in paginator.paginate(QueryExecutionId=execution_id):
            metadata = page.get("ResultSet", {}).get("ResultSetMetadata", {})
            if not columns:
                columns = [str(column["Name"]) for column in metadata.get("ColumnInfo", [])]
            for record in page.get("ResultSet", {}).get("Rows", []):
                values = [entry.get("VarCharValue") for entry in record.get("Data", [])]
                if not header_skipped:
                    # The first row of the first page repeats the header.
                    header_skipped = True
                    if values == columns:
                        continue
                rows.append(dict(zip(columns, values, strict=False)))
                if len(rows) >= _MAX_RESULT_ROWS:
                    return QueryResult(columns, rows, execution_id)
        return QueryResult(columns, rows, execution_id)

    def _qualified_table(self) -> str:
        for identifier in (self.database, self.table):
            if not _IDENTIFIER_PATTERN.fullmatch(identifier):
                raise AthenaQueryError(f"Invalid Athena identifier: {identifier!r}")
        return f'"{self.database}"."{self.table}"'

    @staticmethod
    def _days_clause(days: int) -> str:
        bounded = min(max(int(days), 1), 3_650)
        return f"date >= date_format(date_add('day', -{bounded}, current_date), '%Y-%m-%d')"

    # ------------------------------------------------------------------
    # Canned aggregations
    # ------------------------------------------------------------------

    def cost_by_namespace(self, days: int = 7, region: str | None = None) -> QueryResult:
        """Total cost per namespace across all regions (optionally one region)."""
        table = self._qualified_table()
        clauses = [self._days_clause(days)]
        parameters: list[str] = []
        if region:
            clauses.append("region = ?")
            parameters.append(region)
        where = " AND ".join(clauses)
        sql = (
            "SELECT namespace, "
            "round(sum(total_cost), 4) AS total_cost, "
            "round(sum(cpu_cost), 4) AS cpu_cost, "
            "round(sum(ram_cost), 4) AS ram_cost, "
            "round(sum(gpu_cost), 4) AS gpu_cost, "
            "round(sum(pv_cost), 4) AS pv_cost "
            f"FROM {table} WHERE {where} "  # nosec B608 - identifiers regex-validated, values via ExecutionParameters
            "GROUP BY namespace ORDER BY total_cost DESC"
        )
        return self.run_query(sql, parameters or None)

    def cost_by_region(self, days: int = 7) -> QueryResult:
        """Total Kubernetes allocation cost per deployment region."""
        table = self._qualified_table()
        sql = (
            "SELECT region, "
            "round(sum(total_cost), 4) AS total_cost, "
            "round(sum(cpu_cost), 4) AS cpu_cost, "
            "round(sum(ram_cost), 4) AS ram_cost, "
            "round(sum(gpu_cost), 4) AS gpu_cost "
            f"FROM {table} WHERE {self._days_clause(days)} "  # nosec B608 - identifiers regex-validated, days bounded int
            "GROUP BY region ORDER BY total_cost DESC"
        )
        return self.run_query(sql)

    def cost_over_time(
        self,
        days: int = 14,
        granularity: str = "daily",
        namespace: str | None = None,
    ) -> QueryResult:
        """Cost trend bucketed by day or hour, optionally for one namespace."""
        bucket = _GRANULARITY_EXPRESSIONS.get(granularity)
        if bucket is None:
            raise AthenaQueryError(f"granularity must be one of {sorted(_GRANULARITY_EXPRESSIONS)}")
        table = self._qualified_table()
        clauses = [self._days_clause(days)]
        parameters: list[str] = []
        if namespace:
            clauses.append("namespace = ?")
            parameters.append(namespace)
        where = " AND ".join(clauses)
        sql = (
            f"SELECT {bucket} AS period, "
            "round(sum(total_cost), 4) AS total_cost "
            f"FROM {table} WHERE {where} "  # nosec B608 - bucket from a fixed mapping, identifiers regex-validated, values via ExecutionParameters
            "GROUP BY 1 ORDER BY 1"
        )
        return self.run_query(sql, parameters or None)

    def top_spenders(self, n: int = 10, by: str = "namespace", days: int = 7) -> QueryResult:
        """Top-N spenders grouped by namespace, region, or cluster."""
        grouping = _TOP_GROUPINGS.get(by)
        if grouping is None:
            raise AthenaQueryError(f"by must be one of {sorted(_TOP_GROUPINGS)}")
        bounded_n = min(max(int(n), 1), 100)
        table = self._qualified_table()
        sql = (
            f"SELECT {grouping}, "
            "round(sum(total_cost), 4) AS total_cost "
            f"FROM {table} WHERE {self._days_clause(days)} "  # nosec B608 - grouping from a fixed mapping, identifiers regex-validated, n/days bounded ints
            f"GROUP BY {grouping} ORDER BY total_cost DESC LIMIT {bounded_n}"
        )
        return self.run_query(sql)


def get_cost_analytics(config: GCOConfig | None = None) -> CostAnalytics:
    """Factory function for CostAnalytics."""
    return CostAnalytics(config=config)
