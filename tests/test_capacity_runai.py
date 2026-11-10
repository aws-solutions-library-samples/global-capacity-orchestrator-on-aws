"""``cli/capacity/runai.py`` and ``gco capacity runai``: the read-only Run:ai capacity reader.

The transport is proven against ``tests/runai_fake_control_plane.py``, a real
HTTPS server with its own CA: certificate verification (trusted only through
``GCO_RUNAI_CA_FILE``), the form-encoded token exchange, bearer GETs, paging,
and status handling. The documents are shaped like the public API reference,
and a test asserts every field the contract file declares is present in them.
Pure parsing, matching and rendering are tested directly. The boundary tests
hold the scope the ADR records: no GCO job path imports the reader, and the
Region ranking has no Run:ai backend.
"""

from __future__ import annotations

import ast
import datetime
import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
from click.testing import CliRunner

from cli.capacity import runai
from cli.capacity.backends import BACKENDS
from cli.log_redaction import redact
from cli.main import cli
from tests import runai_fake_control_plane as fake

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "gco" / "config" / "runai_api_contract.json").read_text())
NOW = datetime.datetime(2026, 11, 9, 12, 0, 2, tzinfo=datetime.UTC)


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[fake.FakeControlPlane]:
    running = fake.RunningControlPlane(tmp_path)
    try:
        yield running.plane
    finally:
        running.stop()


def _settings(plane: fake.FakeControlPlane, **overrides: Any) -> runai.RunAISettings:
    values: dict[str, Any] = {
        "url": plane.url,
        "client_id": fake.CLIENT_ID,
        "client_secret": fake.CLIENT_SECRET,
        "ca_file": plane.ca_file,
    }
    values.update(overrides)
    return runai.RunAISettings(**values)


def _row(report: dict[str, Any], pool: str) -> dict[str, Any]:
    [row] = [r for r in report["rows"] if r["node_pool"] == pool]
    return row


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def _env(self, **overrides: str) -> dict[str, str]:
        env = {
            runai.ENV_URL: "https://runai.example.com/",
            runai.ENV_CLIENT_ID: "id",
            runai.ENV_CLIENT_SECRET: "secret",
        }
        env.update(overrides)
        return env

    def test_reads_and_normalizes_the_environment(self, tmp_path: Path) -> None:
        bundle = tmp_path / "ca.pem"
        bundle.write_text("pem")
        settings = runai.RunAISettings.from_env(self._env(**{runai.ENV_CA_FILE: str(bundle)}))
        assert settings.url == "https://runai.example.com"
        assert settings.ca_file == bundle
        assert "secret" not in repr(settings)

    def test_defaults_to_the_process_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key, value in self._env(**{runai.ENV_URL: "https://h:8443"}).items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv(runai.ENV_CA_FILE, raising=False)
        settings = runai.RunAISettings.from_env()
        assert settings.url == "https://h:8443"
        assert settings.ca_file is None

    def test_names_every_missing_variable(self) -> None:
        with pytest.raises(runai.RunAIConfigError) as exc:
            runai.RunAISettings.from_env({runai.ENV_URL: "  "})
        assert f"set {runai.ENV_URL}, {runai.ENV_CLIENT_ID}, {runai.ENV_CLIENT_SECRET}" in str(
            exc.value
        )

    @pytest.mark.parametrize("url", ["http://runai.example.com", "https://", "runai.example.com"])
    def test_requires_https(self, url: str) -> None:
        with pytest.raises(runai.RunAIConfigError, match="must be an https:// URL"):
            runai.RunAISettings.from_env(self._env(**{runai.ENV_URL: url}))

    @pytest.mark.parametrize(
        "url",
        ["https://h/api", "https://h?x=1", "https://h#frag", "https://user@h"],
    )
    def test_requires_a_bare_origin(self, url: str) -> None:
        with pytest.raises(runai.RunAIConfigError, match="must be the control plane's origin"):
            runai.RunAISettings.from_env(self._env(**{runai.ENV_URL: url}))

    def test_rejects_a_missing_ca_file(self, tmp_path: Path) -> None:
        with pytest.raises(runai.RunAIConfigError, match="not a readable file"):
            runai.RunAISettings.from_env(
                self._env(**{runai.ENV_CA_FILE: str(tmp_path / "nope.pem")})
            )

    @pytest.mark.parametrize("page_size", [0, 501])
    def test_page_size_is_bounded(self, page_size: int) -> None:
        settings = runai.RunAISettings("https://h", "id", "secret")
        with pytest.raises(runai.RunAIConfigError, match="between 1 and 500"):
            runai.RunAIClient(settings, page_size=page_size)


# ---------------------------------------------------------------------------
# End to end over TLS
# ---------------------------------------------------------------------------


class TestReadCapacity:
    def test_reports_every_pool_with_one_token_and_seven_gets(
        self, plane: fake.FakeControlPlane
    ) -> None:
        report = runai.read_capacity(_settings(plane), now=NOW)
        assert [(r.method, r.path) for r in plane.recorded] == [
            ("POST", "/api/v2/token"),
            ("GET", "/api/v1/clusters"),
            ("GET", "/api/v1/node-pools"),
            ("GET", "/api/v1/nodes"),
            ("GET", "/api/v1/org-unit/projects/node-pool-resources"),
            ("GET", "/api/v1/nodes/telemetry"),
            ("GET", "/api/v1/nodes/telemetry"),
            ("GET", "/api/v1/nodes/telemetry"),
        ]
        token = plane.recorded[0]
        assert token.headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert all(r.headers["Authorization"] == f"Bearer {fake.TOKEN}" for r in plane.recorded[1:])
        assert [r.query["telemetryType"] for r in plane.recorded[5:]] == [
            ["TOTAL_GPUS"],
            ["ALLOCATED_GPUS"],
            ["FREE_GPUS"],
        ]
        assert all(r.query["groupBy"] == ["ClusterId", "Nodepool"] for r in plane.recorded[5:])
        assert plane.recorded[2].query == {"offset": ["0"], "limit": ["500"]}

        assert report["source"] == "runai" and report["submit_compatible"] is False
        assert report["control_plane"] == plane.url
        assert report["observed_at"] == "2026-11-09T12:00:00Z"
        assert report["valid_until"] == "2026-11-09T12:01:30Z"
        assert report["clusters"] == [
            {"id": fake.OTHER_CLUSTER_ID, "name": "lab", "version": "2.24.10"},
            {"id": fake.CLUSTER_ID, "name": "prod", "version": "2.26.3"},
        ]
        assert [(r["cluster"], r["node_pool"]) for r in report["rows"]] == [
            ("lab", "h100"),
            ("prod", "default"),
            ("prod", "t4"),
        ]
        t4 = _row(report, "t4")
        assert (t4["total_gpus"], t4["allocated_gpus"], t4["free_gpus"]) == (4.0, 2.5, 2.0)
        # 2.5 allocated leaves 1 whole GPU even though FREE_GPUS says 2.
        assert t4["whole_gpus_upper_bound"] == 1
        assert t4["gpu_types"] == [
            {
                "gpu_type": "Tesla-T4",
                "nodes": 1,
                "ready_nodes": 1,
                "gpus": 4,
                "ec2_gpu_model": "T4",
                "ec2_families": ["g4dn"],
            }
        ]
        assert t4["projects"] == [
            {
                "project": "team-a",
                "project_id": "p-1",
                "deserved_gpus": 2.0,
                "limit_gpus": 4.0,
                "allocated_gpus": 2.0,
                "headroom_gpus": 0.0,
            }
        ]
        assert all(
            r["source"] == "runai" and r["submit_compatible"] is False for r in report["rows"]
        )
        h100 = _row(report, "h100")
        assert h100["whole_gpus_upper_bound"] == 8
        assert h100["gpu_types"][0]["nodes"] == 2 and h100["gpu_types"][0]["ready_nodes"] == 1
        assert h100["gpu_types"][0]["ec2_families"] == ["p5"]
        assert h100["projects"][0]["allocated_gpus"] == 0.0  # {} read as 0, with a note
        assert (
            "project team-b: quotaStatus.allocated has no gpu value; read as 0" in report["notes"]
        )
        default = _row(report, "default")
        assert (default["total_gpus"], default["free_gpus"], default["whole_gpus_upper_bound"]) == (
            None,
            None,
            None,
        )
        assert "no FREE_GPUS value for this pool; reported as unknown, not 0" in default["notes"]
        assert "fits_gpu_count" not in default and "project_headroom_gpus" not in default

    def test_every_contract_field_is_in_the_documents_the_tests_serve(
        self, plane: fake.FakeControlPlane
    ) -> None:
        documents = {
            "/api/v2/token": {"access_token": "x"},
            "/api/v1/clusters": plane.state["clusters"],
            "/api/v1/node-pools": {"nodepools": plane.state["node_pools"]},
            "/api/v1/nodes": {"nodes": plane.state["nodes"]},
            "/api/v1/nodes/telemetry": plane.state["telemetry"]["FREE_GPUS"],
            "/api/v1/org-unit/projects/node-pool-resources": {
                "nodePoolResources": plane.state["quotas"]
            },
        }

        def present(node: Any, path: str) -> bool:
            parts = path.split(".")
            if parts[0].startswith("[]"):
                parts[0] = parts[0][2:]
                return (
                    all(present(item, ".".join(p for p in parts if p)) for item in node)
                    if node
                    else False
                )
            head, rest = parts[0], ".".join(parts[1:])
            key = head[:-2] if head.endswith("[]") else head
            if not isinstance(node, dict) or key not in node:
                return False
            value = node[key]
            if head.endswith("[]"):
                items = [v for v in value if v is not None]
                return bool(items) and (not rest or any(present(v, rest) for v in items))
            return not rest or value is None or present(value, rest)

        for route in CONTRACT["routes"]:
            for path in route["response_fields"]:
                assert present(documents[route["path"]], path), (route["path"], path)

    def test_filters_and_fit_columns(self, plane: fake.FakeControlPlane) -> None:
        request = runai.Request(cluster="prod", node_pool="t4", gpu_count=1, project="team-a")
        report = runai.read_capacity(_settings(plane), request, now=NOW)
        [row] = report["rows"]
        assert row["fits_gpu_count"] is True
        assert row["project_headroom_gpus"] == 0.0
        assert row["within_project_quota"] is False
        assert report["request"] == {
            "cluster": "prod",
            "node_pool": "t4",
            "gpu_count": 1,
            "project": "team-a",
        }
        by_id = runai.read_capacity(
            _settings(plane),
            runai.Request(cluster=fake.OTHER_CLUSTER_ID, gpu_count=9, project="nobody"),
            now=NOW,
        )
        [row] = by_id["rows"]
        assert row["fits_gpu_count"] is False
        assert row["projects"] == [] and row["project_headroom_gpus"] is None
        assert row["within_project_quota"] is None

    def test_untrusted_certificate_names_the_ca_setting(self, plane: fake.FakeControlPlane) -> None:
        for ca_file in (None, plane.other_ca_file):
            with pytest.raises(
                runai.RunAIError, match=f"set {runai.ENV_CA_FILE} to that CA's PEM bundle"
            ):
                runai.read_capacity(_settings(plane, ca_file=ca_file), now=NOW)

    def test_unreachable_control_plane(self, plane: fake.FakeControlPlane) -> None:
        settings = _settings(plane, url="https://127.0.0.1:9")  # discard port: nothing listens
        with pytest.raises(runai.RunAIError, match=re.escape("cannot reach https://127.0.0.1:9")):
            runai.read_capacity(settings, now=NOW)

    @pytest.mark.parametrize(
        ("label", "status", "error", "fragment"),
        [
            ("POST /api/v2/token", 401, runai.RunAIAuthError, "rejected the client credentials"),
            ("GET /api/v1/clusters", 401, runai.RunAIAuthError, "rejected the token"),
            (
                "GET /api/v1/nodes/telemetry",
                403,
                runai.RunAIForbidden,
                "403 Forbidden, the access key may not read this",
            ),
            (
                "POST /api/v2/token",
                404,
                runai.RunAIUnsupported,
                "needs NVIDIA Run:ai 2.24 or later",
            ),
            (
                "GET /api/v1/org-unit/projects/node-pool-resources",
                404,
                runai.RunAIUnsupported,
                "does not serve the route",
            ),
            ("GET /api/v1/nodes", 503, runai.RunAIError, "GET /api/v1/nodes: HTTP 503"),
        ],
    )
    def test_status_codes_are_named_errors_never_zero_gpus(
        self,
        plane: fake.FakeControlPlane,
        label: str,
        status: int,
        error: type[Exception],
        fragment: str,
    ) -> None:
        plane.status[label] = status
        with pytest.raises(error, match=fragment):
            runai.read_capacity(_settings(plane), now=NOW)

    def test_redirects_are_not_followed(self, plane: fake.FakeControlPlane) -> None:
        plane.raw["POST /api/v2/token"] = (302, {"Location": "https://elsewhere.example.com/"}, b"")
        with pytest.raises(runai.RunAIError, match=re.escape("redirected (HTTP 302)")):
            runai.read_capacity(_settings(plane), now=NOW)
        assert len(plane.recorded) == 1

    @pytest.mark.parametrize(
        ("label", "body", "fragment"),
        [
            ("POST /api/v2/token", b"not json", "the response is not JSON"),
            ("POST /api/v2/token", b'{"token_type": "Bearer"}', "no access_token"),
            ("GET /api/v1/clusters", b'{"clusters": []}', "is not a list"),
            ("GET /api/v1/nodes", b'{"items": []}', "has no nodes list"),
            ("GET /api/v1/nodes/telemetry", b'{"type": "TOTAL_GPUS"}', "no values list"),
        ],
    )
    def test_unexpected_shapes_are_response_errors(
        self, plane: fake.FakeControlPlane, label: str, body: bytes, fragment: str
    ) -> None:
        plane.raw[label] = (200, {"Content-Type": "application/json"}, body)
        with pytest.raises(runai.RunAIResponseError, match=fragment):
            runai.read_capacity(_settings(plane), now=NOW)


class TestPaging:
    def _nodes(self, count: int) -> list[dict[str, Any]]:
        return [
            {
                "name": f"n{i}",
                "clusterUuid": fake.CLUSTER_ID,
                "nodePool": "t4",
                "status": "Ready",
                "gpuInfo": {"gpuType": "Tesla-T4", "gpuCount": 1},
            }
            for i in range(count)
        ]

    def test_pages_past_the_api_maximum(self, plane: fake.FakeControlPlane) -> None:
        plane.state["nodes"] = self._nodes(1201)
        report = runai.read_capacity(_settings(plane), now=NOW)
        offsets = [r.query["offset"][0] for r in plane.recorded if r.path == "/api/v1/nodes"]
        assert offsets == ["0", "500", "1000"]
        assert _row(report, "t4")["gpu_types"][0]["nodes"] == 1201

    def test_page_size_one_walks_every_route_item_by_item(
        self, plane: fake.FakeControlPlane
    ) -> None:
        runai.read_capacity(_settings(plane), page_size=1, now=NOW)
        calls = [(r.path, r.query.get("offset", [""])[0]) for r in plane.recorded]
        assert [o for p, o in calls if p == "/api/v1/node-pools"] == ["0", "1", "2", "3"]
        assert [o for p, o in calls if p == "/api/v1/nodes"] == ["0", "1", "2", "3", "4"]
        # The quota route returns `next`; its null on the last page ends the walk.
        assert [o for p, o in calls if p == "/api/v1/org-unit/projects/node-pool-resources"] == [
            "0",
            "1",
        ]

    def test_a_full_last_page_ends_on_the_following_empty_page(
        self, plane: fake.FakeControlPlane
    ) -> None:
        plane.state["node_pools"] = plane.state["node_pools"][:2]
        runai.read_capacity(_settings(plane), page_size=2, now=NOW)
        assert [r.query["offset"][0] for r in plane.recorded if r.path == "/api/v1/node-pools"] == [
            "0",
            "2",
        ]

    def test_a_server_that_ignores_offset_is_an_error(self, plane: fake.FakeControlPlane) -> None:
        plane.ignore_offset = True
        with pytest.raises(runai.RunAIResponseError, match="returned the previous page again"):
            runai.read_capacity(_settings(plane), page_size=1, now=NOW)

    def test_the_item_ceiling_stops_a_runaway_route(
        self, plane: fake.FakeControlPlane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runai, "MAX_ITEMS_PER_ROUTE", 2)
        with pytest.raises(runai.RunAIResponseError, match="more than 2 items"):
            runai.read_capacity(_settings(plane), now=NOW)


class TestTokenReuse:
    def test_one_token_per_client_and_none_written_to_disk(
        self, plane: fake.FakeControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        before = sorted(p.name for p in tmp_path.iterdir())
        client = runai.RunAIClient(_settings(plane))
        client.authenticate()
        client.authenticate()
        client.get(runai.CLUSTERS_PATH)
        assert client.requests_made == ["POST /api/v2/token", "GET /api/v1/clusters"]
        assert sorted(p.name for p in tmp_path.iterdir()) == before


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


class TestParseTelemetry:
    def test_values_notes_and_timestamp(self) -> None:
        document = {
            "type": "FREE_GPUS",
            "timestamp": "2026-11-09T12:00:00",  # no zone: read as UTC
            "values": [
                "junk",
                {
                    "value": "3",
                    "groups": [
                        {"key": "ClusterId", "value": "c"},
                        {"key": "Nodepool", "value": "a"},
                        "junk",
                    ],
                },
                {
                    "value": "9",
                    "groups": [
                        {"key": "clusterid", "value": "c"},
                        {"key": "NODEPOOL", "value": "a"},
                    ],
                },
                {
                    "value": "n/a",
                    "groups": [
                        {"key": "ClusterId", "value": "c"},
                        {"key": "Nodepool", "value": "b"},
                    ],
                },
                {"value": "1", "groups": [{"key": "Nodepool", "value": "orphan"}]},
                {"value": "1"},
            ],
        }
        telemetry = runai.parse_telemetry(document, "FREE_GPUS")
        assert telemetry.values == {("c", "a"): 3.0, ("c", "b"): None}
        assert telemetry.observed_at == datetime.datetime(
            2026, 11, 9, 12, 0, 0, tzinfo=datetime.UTC
        )
        assert telemetry.notes == [
            "FREE_GPUS: a reported twice; the first value is kept",
            "FREE_GPUS: b reported 'n/a', not a number",
            "FREE_GPUS: a value without a ClusterId and Nodepool group was ignored",
            "FREE_GPUS: a value without a ClusterId and Nodepool group was ignored",
        ]

    @pytest.mark.parametrize("timestamp", [None, "", "yesterday", 7])
    def test_unreadable_timestamps(self, timestamp: Any) -> None:
        assert (
            runai.parse_telemetry({"values": [], "timestamp": timestamp}, "X").observed_at is None
        )

    @pytest.mark.parametrize("value", [True, None, "inf", "nan", [], "1e400"])
    def test_non_numbers(self, value: Any) -> None:
        assert runai._number(value) is None


def _report(**overrides: Any) -> dict[str, Any]:
    empty = runai.Telemetry(values={}, observed_at=None, notes=[])
    values: dict[str, Any] = {
        "url": "https://h",
        "clusters": [],
        "node_pools": [],
        "nodes": [],
        "quotas": [],
        "telemetry": dict.fromkeys(runai.TELEMETRY_TYPES, empty),
        "queried_at": NOW,
        "request": runai.Request(),
        "gpu_models": {},
    }
    values.update(overrides)
    return runai.build_report(**values)


class TestBuildReport:
    def test_without_timestamps_the_query_time_is_observed_at(self) -> None:
        report = _report()
        assert report["observed_at"] == "2026-11-09T12:00:02Z"
        assert "telemetry carried no timestamp; observed_at is when GCO asked" in report["notes"]
        assert report["rows"] == []

    def test_quota_shapes(self) -> None:
        pool = {"id": "np", "name": "p", "clusterId": "c", "clusterName": "c", "phase": "Ready"}
        quotas = [
            {
                "orgUnitName": "none",
                "clusterId": "c",
                "nodePoolName": "p",
                "resources": {"gpu": {"deserved": None}},
            },
            {
                "orgUnitId": "only-id",
                "clusterId": "c",
                "nodePoolName": "p",
                "resources": "junk",
                "quotaStatus": {"allocated": "junk"},
            },
            {
                "orgUnitName": "neg",
                "clusterId": "c",
                "nodePoolName": "p",
                "resources": {"gpu": {"deserved": -1}},
                "quotaStatus": {"allocated": {"gpu": 1}},
            },
            {
                "orgUnitName": "bad",
                "clusterId": "c",
                "nodePoolName": "p",
                "resources": {"gpu": "junk"},
                "quotaStatus": {"allocated": {"gpu": "x"}},
            },
        ]
        report = _report(node_pools=[pool], quotas=quotas)
        rows = {p["project"]: p for p in report["rows"][0]["projects"]}
        assert rows["none"]["allocated_gpus"] is None and rows["none"]["headroom_gpus"] is None
        assert (
            rows["only-id"]["project_id"] == "only-id" and rows["only-id"]["allocated_gpus"] is None
        )
        assert rows["neg"]["deserved_gpus"] == -1.0 and rows["neg"]["headroom_gpus"] is None
        assert rows["bad"]["deserved_gpus"] is None and rows["bad"]["allocated_gpus"] is None
        assert "project none: no quotaStatus.allocated; allocation unknown" in report["notes"]

    def test_unknown_cluster_and_odd_nodes(self) -> None:
        pool = {"id": "np", "name": "p", "clusterId": "ghost", "clusterName": "ghost-name"}
        nodes = [
            {
                "clusterUuid": "ghost",
                "nodePool": "p",
                "status": "Ready",
                "gpuInfo": {"gpuType": "Mystery-GPU", "gpuCount": "?"},
            },
            {"clusterUuid": "ghost", "nodePool": "p", "gpuInfo": {"gpuType": ""}},
            {"clusterUuid": "ghost", "nodePool": "p", "gpuInfo": "junk"},
        ]
        clusters = ["junk", {"name": "no-uuid"}, {"uuid": "u-only"}]
        report = _report(node_pools=[pool], nodes=nodes, clusters=clusters)
        row = report["rows"][0]
        assert row["cluster"] == "ghost-name" and row["cluster_id"] == "ghost"
        assert row["gpu_types"] == [
            {
                "gpu_type": "Mystery-GPU",
                "nodes": 1,
                "ready_nodes": 1,
                "gpus": 0,
                "ec2_gpu_model": None,
                "ec2_families": [],
            }
        ]
        assert report["clusters"] == [{"id": "u-only", "name": "u-only", "version": None}]

    def test_bound_without_free_and_fit_unknown(self) -> None:
        pool = {"id": "np", "name": "p", "clusterId": "c"}
        series = {
            "TOTAL_GPUS": runai.Telemetry({("c", "p"): 4.0}, None, []),
            "ALLOCATED_GPUS": runai.Telemetry({("c", "p"): 5.0}, None, []),
            "FREE_GPUS": runai.Telemetry({}, None, []),
        }
        report = _report(node_pools=[pool], telemetry=series, request=runai.Request(gpu_count=1))
        assert report["rows"][0]["whole_gpus_upper_bound"] == 0  # never negative
        assert report["rows"][0]["fits_gpu_count"] is False
        unknown = _report(node_pools=[pool], request=runai.Request(gpu_count=1))
        assert unknown["rows"][0]["fits_gpu_count"] is None

    def test_project_without_a_gpu_count_reports_headroom_only(self) -> None:
        pool = {"id": "np", "name": "p", "clusterId": "c"}
        quota = {
            "orgUnitName": "team",
            "clusterId": "c",
            "nodePoolName": "p",
            "resources": {"gpu": {"deserved": 3}},
            "quotaStatus": {"allocated": {"gpu": 2}},
        }
        report = _report(node_pools=[pool], quotas=[quota], request=runai.Request(project="team"))
        row = report["rows"][0]
        assert row["project_headroom_gpus"] == 1.0
        assert "within_project_quota" not in row and "fits_gpu_count" not in row

    def test_filters_drop_other_clusters_and_pools(self) -> None:
        pools = [
            {"id": "a", "name": "a", "clusterId": "c"},
            {"id": "b", "name": "b", "clusterId": "c"},
        ]
        assert _report(node_pools=pools, request=runai.Request(cluster="other"))["rows"] == []
        assert [
            r["node_pool"]
            for r in _report(node_pools=pools, request=runai.Request(node_pool="b"))["rows"]
        ] == ["b"]


class TestGpuModels:
    MODELS = runai.load_gpu_models()

    @pytest.mark.parametrize(
        ("gpu_type", "model"),
        [
            ("Tesla-T4", "T4"),
            ("NVIDIA-A10G", "A10G"),
            ("NVIDIA-H100-80GB-HBM3", "H100"),
            ("NVIDIA-H200", "H200"),
            ("NVIDIA-B200", "B200"),
            ("NVIDIA-L40S", "L40S"),
            ("NVIDIA-L4", "L4"),
            ("NVIDIA-A100-SXM4-40GB", "A100"),
            ("nvidia_rtx_pro_server_6000", "RTX PRO Server 6000"),
            ("NVIDIA-GeForce-RTX-4090", None),
            ("", None),
        ],
    )
    def test_matches_the_catalog_model(self, gpu_type: str, model: str | None) -> None:
        assert runai.match_gpu_model(gpu_type, self.MODELS) == model

    def test_catalog_index(self) -> None:
        assert self.MODELS["T4"] == ["g4dn"]
        assert self.MODELS["L4"] == ["g6", "g6f", "gr6", "gr6f"]
        assert self.MODELS["H200"] == ["p5e", "p5en"]

    def test_an_unreadable_catalog_matches_nothing(self, tmp_path: Path) -> None:
        assert runai.load_gpu_models(tmp_path / "missing.json") == {}
        broken = tmp_path / "broken.json"
        broken.write_text("[]")
        assert runai.load_gpu_models(broken) == {}
        odd = tmp_path / "odd.json"
        odd.write_text(
            json.dumps(
                {"families": {"x": "junk", "y": {"gpu_model": ""}, "z": {"gpu_model": "Z1"}}}
            )
        )
        assert runai.load_gpu_models(odd) == {"Z1": ["z"]}


class TestRenderTable:
    def test_columns_follow_the_request(self, plane: fake.FakeControlPlane) -> None:
        report = runai.read_capacity(
            _settings(plane), runai.Request(gpu_count=2, project="team-a"), now=NOW
        )
        table = runai.render_table(report)
        assert "FITS 2" in table and "HEADROOM" in table
        assert "Tesla-T4(T4)" in table
        assert "not ranked with AWS Regions" in table
        lines = {
            line.split()[1]: line
            for line in table.splitlines()
            if line.startswith(("  prod ", "  lab "))
        }
        assert lines["t4"].split()[-2:] == ["no", "0"]
        assert lines["h100"].split()[-2:] == ["yes", "?"]
        assert lines["default"].split()[2:] == ["-", "?", "?", "?", "?", "?", "?"]
        assert "note: prod/default: no TOTAL_GPUS value" in table

    def test_no_rows_and_no_notes(self) -> None:
        report = _report(
            telemetry=dict.fromkeys(runai.TELEMETRY_TYPES, runai.Telemetry({}, NOW, []))
        )
        table = runai.render_table(report)
        assert "(no node pools match)" in table
        assert "note:" not in table


# ---------------------------------------------------------------------------
# The command, the MCP tool, redaction
# ---------------------------------------------------------------------------


class TestCommand:
    def _env(self, plane: fake.FakeControlPlane) -> dict[str, str]:
        return {
            runai.ENV_URL: plane.url,
            runai.ENV_CLIENT_ID: fake.CLIENT_ID,
            runai.ENV_CLIENT_SECRET: fake.CLIENT_SECRET,
            runai.ENV_CA_FILE: str(plane.ca_file),
        }

    def test_json_output(self, plane: fake.FakeControlPlane) -> None:
        result = CliRunner().invoke(
            cli,
            ["--output", "json", "capacity", "runai", "--gpu-count", "2", "--page-size", "1"],
            env=self._env(plane),
        )
        assert result.exit_code == 0, result.output
        document = json.loads(result.output)
        assert document["request"]["gpu_count"] == 2
        assert {r["node_pool"] for r in document["rows"]} == {"t4", "default", "h100"}
        assert fake.CLIENT_SECRET not in result.output and fake.TOKEN not in result.output

    def test_table_output(self, plane: fake.FakeControlPlane) -> None:
        result = CliRunner().invoke(
            cli, ["capacity", "runai", "--node-pool", "h100"], env=self._env(plane)
        )
        assert result.exit_code == 0, result.output
        assert "NVIDIA Run:ai capacity" in result.output and "h100" in result.output

    def test_errors_exit_one_with_the_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (runai.ENV_URL, runai.ENV_CLIENT_ID, runai.ENV_CLIENT_SECRET):
            monkeypatch.delenv(name, raising=False)
        result = CliRunner().invoke(cli, ["capacity", "runai"])
        assert result.exit_code == 1
        assert f"Run:ai capacity: set {runai.ENV_URL}" in result.output

    def test_page_size_is_validated_by_click(self) -> None:
        result = CliRunner().invoke(cli, ["capacity", "runai", "--page-size", "0"])
        assert result.exit_code == 2


def test_the_mcp_tool_takes_no_url_or_credential_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control plane and secret come from the MCP host's environment only."""
    import inspect
    import subprocess
    import sys

    snippet = (
        "import inspect, json, sys; sys.path.insert(0, 'gco_mcp'); import run_mcp;"
        "print(json.dumps(list(inspect.signature(run_mcp.runai_capacity).parameters)))"
    )
    env = {**__import__("os").environ, "GCO_ENABLE_RUNAI_CAPACITY": "true"}
    out = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=True,
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [
        "cluster",
        "node_pool",
        "gpu_count",
        "project",
    ]
    del inspect


def test_token_exchange_secrets_are_redacted_in_logs() -> None:
    form = f"grant_type=client_credentials&client_id=id&client_secret={fake.CLIENT_SECRET}"
    assert fake.CLIENT_SECRET not in redact(form)
    body = json.dumps(
        {
            "access_token": fake.TOKEN,
            "refresh_token": "r1",
            "id_token": "i1",
            "token_type": "Bearer",
        }
    )
    redacted = redact(body)
    assert fake.TOKEN not in redacted and "r1" not in redacted and "i1" not in redacted
    assert '"token_type": "Bearer"' in redacted
    assert fake.TOKEN not in redact(f"Authorization: Bearer {fake.TOKEN}")


def test_debug_logging_of_a_read_never_shows_the_secret_or_token(
    plane: fake.FakeControlPlane, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        runai.read_capacity(_settings(plane), now=NOW)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert fake.CLIENT_SECRET not in text and fake.TOKEN not in text


# ---------------------------------------------------------------------------
# Scope boundaries (docs/adr/0006-runai-read-only-capacity-source.md)
# ---------------------------------------------------------------------------


def _imports_runai(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {alias.name for alias in node.names}
            if (
                module.endswith("capacity.runai")
                or (module.endswith("capacity") and "runai" in names)
                or (module == "" and "runai" in names)
            ):
                return True
        if isinstance(node, ast.Import) and any(
            a.name.endswith("capacity.runai") for a in node.names
        ):
            return True
    return False


def test_only_the_capacity_command_imports_the_reader() -> None:
    importers = sorted(
        str(path.relative_to(ROOT))
        for top in ("cli", "gco", "gco_mcp", "lambda", "scripts")
        for path in (ROOT / top).rglob("*.py")
        if "-build" not in str(path) and _imports_runai(path)
    )
    assert importers == ["cli/commands/capacity_cmd.py"]


def test_the_region_ranking_has_no_runai_backend() -> None:
    assert "runai" not in BACKENDS
    assert not hasattr(runai, "score")


def test_the_contract_declares_exactly_the_routes_the_reader_calls() -> None:
    declared = {(route["method"], route["path"]) for route in CONTRACT["routes"]}
    assert declared == set(runai.ROUTES)
    assert CONTRACT["min_version"] == runai.MIN_SUPPORTED_VERSION
    telemetry = next(r for r in CONTRACT["routes"] if r["path"] == runai.TELEMETRY_PATH)
    assert telemetry["parameter_values"]["telemetryType"] == list(runai.TELEMETRY_TYPES)


def test_a_session_error_other_than_tls_is_wrapped() -> None:
    class Broken:
        def request(self, *args: Any, **kwargs: Any) -> Any:
            raise requests.exceptions.ReadTimeout("slow")

    client = runai.RunAIClient(runai.RunAISettings("https://h", "id", "secret"), session=Broken())  # type: ignore[arg-type]
    with pytest.raises(runai.RunAIError, match=re.escape("cannot reach https://h (ReadTimeout)")):
        client.authenticate()
