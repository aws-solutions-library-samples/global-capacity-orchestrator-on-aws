"""Tests for ``.github/scripts/check_runai_api_contract.py``.

The checker is the maintenance loop for ``gco capacity runai``: the monthly
dependency scan runs it against NVIDIA's published API reference, and every
row it prints is drift on a route the reader depends on. These tests build a
documentation site under ``tmp_path`` (served through ``file://`` URLs, the
same reader path the live site goes through) from the real contract file, then
break it one way at a time.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "check_runai_api_contract.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_runai_api_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load()
CONTRACT: dict[str, Any] = json.loads(checker.DEFAULT_CONTRACT.read_text())


def _schema_for(paths: list[str]) -> dict[str, Any]:
    """The smallest JSON schema in which every contract field path resolves."""
    tree: dict[str, Any] = {}
    for path in paths:
        node = tree
        for part in path.replace("[]", ".[]").strip(".").split("."):
            node = node.setdefault(part, {})

    def build(node: dict[str, Any]) -> dict[str, Any]:
        if not node:
            return {"type": "string"}
        if set(node) == {"[]"}:
            return {"type": "array", "items": build(node["[]"])}
        return {"type": "object", "properties": {k: build(v) for k, v in node.items()}}

    return build(tree)


def _operation(route: dict[str, Any]) -> dict[str, Any]:
    parameters = []
    for name in route.get("parameters", []):
        values = (route.get("parameter_values") or {}).get(name)
        schema: dict[str, Any] = {"type": "string"}
        if values and name == "groupBy":
            schema = {"type": "array", "items": {"type": "string", "enum": [*values, "Node"]}}
        elif values:
            schema = {"type": "string", "enum": [*values, "READY_GPUS"]}
        parameters.append({"name": name, "in": "query", "schema": schema})
    operation: dict[str, Any] = {
        "parameters": parameters,
        "responses": {
            "200": {
                "content": {"application/json": {"schema": _schema_for(route["response_fields"])}}
            }
        },
    }
    if route.get("request_fields"):
        operation["requestBody"] = {
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": _schema_for(route["request_fields"])
                }
            }
        }
    return operation


def _page(routes: list[dict[str, Any]], mutate: Any = None) -> str:
    blocks = []
    for route in routes:
        document = {
            "openapi": "3.0.3",
            "paths": {route["path"]: {route["method"].lower(): _operation(route)}},
            "components": {"schemas": {}},
        }
        if mutate is not None:
            mutate(route, document)
        blocks.append("```json\n" + json.dumps(document) + "\n```")
    return "# Page\n\n" + "\n\n".join(blocks) + "\n"


def _site(
    root: Path,
    versions: tuple[str, ...] = ("2.23", "2.24", "2.25"),
    mutate: dict[str, Any] | None = None,
) -> str:
    """Write a documentation site; ``mutate`` maps a version (or 'latest') to a hook."""
    locs = [f"https://example/api/{v}/sitemap-pages.xml" for v in versions]
    locs += [
        "https://example/api/sitemap-pages.xml",
        "https://example/self-hosted/2.30/sitemap-pages.xml",
    ]
    (root / "sitemap.xml").write_text(
        "<sitemapindex>"
        + "".join(f"<sitemap><loc> {loc} </loc></sitemap>" for loc in locs)
        + "</sitemapindex>"
    )
    pages: dict[str, list[dict[str, Any]]] = {}
    for route in CONTRACT["routes"]:
        pages.setdefault(route["page"], []).append(route)
    for version in (*versions, "latest"):
        base = root / "api" if version == "latest" else root / "api" / version
        for page, routes in pages.items():
            target = base / f"{page}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_page(routes, (mutate or {}).get(version)))
    return root.as_uri()


def _contract(**overrides: Any) -> dict[str, Any]:
    contract = copy.deepcopy(CONTRACT)
    contract.update(overrides)
    return contract


def _rows(findings: list[Any]) -> list[str]:
    return [finding.row() for finding in findings]


class TestRun:
    def test_a_site_that_matches_the_contract_reports_only_unreviewed_versions(
        self, tmp_path: Path
    ) -> None:
        docs = _site(tmp_path, versions=("2.23", "2.24", "2.25", "2.30"))
        findings = checker.run(_contract(reviewed_versions=["2.24", "2.25"]), docs)
        assert _rows(findings) == [
            "2.30|-|published; the contract holds|add it to reviewed_versions in gco/config/runai_api_contract.json"
        ]

    def test_the_live_contract_against_its_own_fixture_is_clean(self, tmp_path: Path) -> None:
        assert checker.run(CONTRACT, _site(tmp_path, versions=("2.24", "2.25", "2.26"))) == []

    def test_every_kind_of_drift_is_a_row(self, tmp_path: Path) -> None:
        def break_things(route: dict[str, Any], document: dict[str, Any]) -> None:
            operation = document["paths"][route["path"]][route["method"].lower()]
            if route["path"] == "/api/v1/clusters":
                document["paths"] = {}
            elif route["path"] == "/api/v1/nodes":
                operation["deprecated"] = True
                operation["parameters"] = [
                    p for p in operation["parameters"] if p["name"] != "offset"
                ]
                schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
                del schema["properties"]["nodes"]["items"]["properties"]["gpuInfo"]
            elif route["path"] == "/api/v1/nodes/telemetry":
                for parameter in operation["parameters"]:
                    if parameter["name"] == "telemetryType":
                        parameter["schema"]["enum"] = ["TOTAL_GPUS", "ALLOCATED_GPUS"]
                    if parameter["name"] == "groupBy":
                        parameter["schema"]["items"]["enum"] = ["ClusterId"]
            elif route["path"] == "/api/v2/token":
                del operation["requestBody"]["content"]["application/x-www-form-urlencoded"][
                    "schema"
                ]["properties"]["client_secret"]

        findings = checker.run(
            CONTRACT, _site(tmp_path, versions=("2.24",), mutate={"latest": break_things})
        )
        assert _rows(findings) == [
            "latest|POST /api/v2/token|request field client_secret missing|update cli/capacity/runai.py and gco/config/runai_api_contract.json",
            "latest|GET /api/v1/clusters|route not in the API reference|update cli/capacity/runai.py and gco/config/runai_api_contract.json",
            "latest|GET /api/v1/nodes|route marked deprecated|move the reader to the replacement route before NVIDIA removes it",
            "latest|GET /api/v1/nodes|parameter offset missing|update cli/capacity/runai.py and gco/config/runai_api_contract.json",
            "latest|GET /api/v1/nodes|response field nodes[].gpuInfo.gpuType missing|update cli/capacity/runai.py and gco/config/runai_api_contract.json",
            "latest|GET /api/v1/nodes|response field nodes[].gpuInfo.gpuCount missing|update cli/capacity/runai.py and gco/config/runai_api_contract.json",
            "latest|GET /api/v1/nodes/telemetry|telemetryType=FREE_GPUS not accepted|update cli/capacity/runai.py and gco/config/runai_api_contract.json",
            "latest|GET /api/v1/nodes/telemetry|groupBy=Nodepool not accepted|update cli/capacity/runai.py and gco/config/runai_api_contract.json",
        ]

    def test_unconstrained_parameters_accept_any_value(self, tmp_path: Path) -> None:
        def drop_enums(route: dict[str, Any], document: dict[str, Any]) -> None:
            for parameter in document["paths"][route["path"]][route["method"].lower()][
                "parameters"
            ]:
                parameter["schema"] = {"type": "string"}

        assert (
            checker.run(CONTRACT, _site(tmp_path, versions=("2.24",), mutate={"2.24": drop_enums}))
            == []
        )

    def test_a_version_without_routes_is_unreadable(self, tmp_path: Path) -> None:
        docs = _site(tmp_path, versions=("2.24",))
        for page in (tmp_path / "api" / "2.24").rglob("*.md"):
            page.write_text("# moved\n")
        with pytest.raises(
            checker.ContractCheckError, match=re.escape("no OpenAPI document found in the 2.24")
        ):
            checker.run(CONTRACT, docs)

    def test_a_sitemap_without_supported_versions_is_unreadable(self, tmp_path: Path) -> None:
        docs = _site(tmp_path, versions=("2.20", "2.23"))
        with pytest.raises(
            checker.ContractCheckError, match=re.escape("no API reference version from 2.24 on")
        ):
            checker.run(CONTRACT, docs)

    def test_malformed_embedded_openapi(self, tmp_path: Path) -> None:
        docs = _site(tmp_path, versions=("2.24",))
        (tmp_path / "api" / "2.24" / "organizations" / "nodes.md").write_text(
            '```json\n{"openapi": nope}\n```'
        )
        with pytest.raises(checker.ContractCheckError, match="not JSON"):
            checker.run(CONTRACT, docs)


class TestSchemaWalk:
    COMPONENTS = {
        "schemas": {
            "Node": {
                "allOf": [
                    {"$ref": "#/components/schemas/Base"},
                    {"properties": {"gpuInfo": {"$ref": "#/components/schemas/Gpu"}}},
                ]
            },
            "Base": {"type": "object", "properties": {"name": {"type": "string"}}},
            "Gpu": {"oneOf": [{"properties": {"gpuType": {"type": "string"}}}]},
            "Nodes": {"allOf": [{"items": {"$ref": "#/components/schemas/Node"}}]},
            "Loop": {"$ref": "#/components/schemas/Loop"},
        },
        "responses": {
            "Ok": {
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Nodes"}}}
            }
        },
    }

    @pytest.mark.parametrize(
        ("schema", "path", "expected"),
        [
            ({"$ref": "#/components/schemas/Nodes"}, "[].gpuInfo.gpuType", True),
            ({"$ref": "#/components/schemas/Nodes"}, "[].name", True),
            ({"$ref": "#/components/schemas/Nodes"}, "[].gpuInfo.gpuCount", False),
            (
                {"type": "object", "properties": {"nodes": {"$ref": "#/components/schemas/Nodes"}}},
                "nodes[].name",
                True,
            ),
            ({"type": "object"}, "[].name", False),
            ({"$ref": "#/components/schemas/Loop"}, "name", False),
            ("not-a-schema", "name", False),
        ],
    )
    def test_has_field(self, schema: Any, path: str, expected: bool) -> None:
        assert checker.has_field(schema, path, self.COMPONENTS) is expected

    def test_response_and_request_lookups(self) -> None:
        operation = {
            "responses": {"201": {"$ref": "#/components/responses/Ok"}},
            "requestBody": {"content": {"application/json": {"schema": {"properties": {"x": {}}}}}},
        }
        assert checker.has_field(
            checker._response_schema(operation, self.COMPONENTS), "[].name", self.COMPONENTS
        )
        assert checker.has_field(
            checker._request_schema(operation, self.COMPONENTS), "x", self.COMPONENTS
        )
        assert checker._response_schema({"responses": {"204": {}}}, self.COMPONENTS) == {}
        assert checker._request_schema({}, self.COMPONENTS) == {}

    def test_parameters_skip_unnamed_entries(self) -> None:
        operation = {
            "parameters": [{"in": "query"}, {"name": "limit", "schema": {"type": "integer"}}]
        }
        assert checker._parameters(operation, {}) == {"limit": {"type": "integer"}}


class TestFetch:
    def test_reads_file_urls_and_does_not_retry_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = tmp_path / "p.md"
        page.write_text("hello")
        assert checker.fetch_url(page.as_uri()) == "hello"
        slept: list[float] = []
        monkeypatch.setattr(checker.time, "sleep", slept.append)
        with pytest.raises(checker.ContractCheckError, match="cannot read"):
            checker.fetch_url((tmp_path / "missing.md").as_uri())
        assert slept == []

    def test_retries_transient_https_failures_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        class Response:
            def __enter__(self) -> Response:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return b"ok"

        def urlopen(request: Any, timeout: float) -> Response:
            calls.append(request.get_header("User-agent"))
            if len(calls) < 3:
                raise urllib.error.URLError("flaky")
            return Response()

        slept: list[float] = []
        monkeypatch.setattr(checker.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(checker.time, "sleep", slept.append)
        assert checker.fetch_url("https://docs.example/x.md") == "ok"
        assert calls == [checker.USER_AGENT] * 3
        assert slept == [1, 2]

    def test_gives_up_after_the_last_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def urlopen(request: Any, timeout: float) -> Any:
            raise TimeoutError("slow")

        monkeypatch.setattr(checker.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(checker.time, "sleep", lambda _: None)
        with pytest.raises(checker.ContractCheckError, match="slow"):
            checker.fetch_url("https://docs.example/x.md", attempts=2)


class TestMain:
    def test_rows_and_json_formats(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        docs = _site(tmp_path, versions=("2.24", "2.40"))
        assert checker.main(["--docs-root", docs]) == 0
        assert capsys.readouterr().out.startswith("2.40|-|published; the contract holds|")
        assert checker.main(["--docs-root", docs, "--format", "json"]) == 0
        [finding] = json.loads(capsys.readouterr().out)
        assert finding["version"] == "2.40" and finding["route"] == "-"

    def test_the_default_docs_root_comes_from_the_contract(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: list[str] = []

        def fetch(url: str) -> str:
            seen.append(url)
            raise checker.ContractCheckError("offline")

        assert checker.main([], fetch=fetch) == 2
        assert seen == [f"{CONTRACT['docs_root']}/sitemap.xml"]
        assert "skipped: offline" in capsys.readouterr().err

    @pytest.mark.parametrize("body", ["{not json", json.dumps({"routes": "nope"})])
    def test_an_unreadable_contract_is_a_skip(self, tmp_path: Path, body: str) -> None:
        contract = tmp_path / "contract.json"
        contract.write_text(body)
        assert checker.main(["--contract", str(contract), "--docs-root", tmp_path.as_uri()]) == 2

    def test_rows_never_carry_the_separator(self) -> None:
        finding = checker.Finding("2.24", "GET /a|b", "x  y\nz", "do it")
        assert finding.row() == "2.24|GET /a/b|x y z|do it"
