"""Static validation of the curated Grafana dashboard ConfigMaps.

The dashboards ship as JSON payloads embedded in ConfigMap manifests under
``lambda/kubectl-applier-simple/manifests/`` and are imported at runtime by
the kube-prometheus-stack Grafana sidecar, which surfaces a malformed
dashboard only as a silently missing UI entry. These tests parse the payloads
through the applier's real planning path — the same substitution and
feature-gating production uses — so a stray comma, a duplicate uid, or a
templating change that eats Grafana's ``{{...}}`` legend tokens fails CI
instead of shipping. The CI workflow additionally boots the pinned chart's
Grafana image and asserts it provisions these payloads; here we hold the
extraction script and the applier path in lockstep without any container.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "validate_grafana_dashboards.py"

DASHBOARD_MANIFESTS = (
    "post-helm-grafana-dashboards.yaml",
    "post-helm-grafana-cost-dashboard.yaml",
)

# The applier gates files on unresolved {{UPPER_SNAKE}} placeholders; the
# replacement values themselves are irrelevant to dashboard structure.
REPLACEMENTS = {
    "{{CLUSTER_OBSERVABILITY_ENABLED}}": "true",
    "{{COST_MONITORING_ENABLED}}": "true",
}

# Grafana's dashboard grid is 24 columns wide and rejects uids longer than
# 40 characters on import.
GRID_COLUMNS = 24
UID_MAX_LENGTH = 40
UID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _load_script():
    """Load the extraction script by file path.

    ``.github/scripts`` is intentionally not a Python package, so import by
    path rather than adding an ``__init__.py`` — mirrors the helm-charts and
    k8s-manifest validator tests.
    """
    spec = importlib.util.spec_from_file_location("validate_grafana_dashboards", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


@pytest.fixture(scope="module")
def handler_module():
    """Import the kubectl-applier handler for its real planning path."""
    handler_path = str(PROJECT_ROOT / "lambda" / "kubectl-applier-simple")
    sys.path.insert(0, handler_path)
    try:
        sys.modules.pop("handler", None)
        import handler

        yield handler
    finally:
        sys.path.remove(handler_path)
        sys.modules.pop("handler", None)


@pytest.fixture(scope="module")
def planned_configmaps(
    handler_module, tmp_path_factory: pytest.TempPathFactory
) -> list[dict[str, Any]]:
    """The dashboard ConfigMaps as the applier's own planner sees them."""
    workdir = tmp_path_factory.mktemp("grafana-manifests")
    for name in DASHBOARD_MANIFESTS:
        shutil.copy(MANIFESTS_DIR / name, workdir / name)

    plan = handler_module.plan_manifests(str(workdir), REPLACEMENTS)

    # With both feature placeholders resolved, nothing may be gated out —
    # a skip here means a dashboard silently vanished from the deployment.
    assert plan["skipped"]["post-helm"] == []
    assert plan["featureGates"]["post-helm"] == []
    documents = [entry["document"] for entry in plan["phases"]["post-helm"]]
    assert documents, "the planner returned no dashboard ConfigMaps"
    return documents


@pytest.fixture(scope="module")
def dashboards(planned_configmaps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{data key: parsed dashboard}`` across every planned ConfigMap."""
    import json

    parsed: dict[str, dict[str, Any]] = {}
    for configmap in planned_configmaps:
        for key, payload in (configmap.get("data") or {}).items():
            parsed[key] = json.loads(payload)
    return parsed


# ---------------------------------------------------------------------------
# ConfigMap envelope
# ---------------------------------------------------------------------------


def test_every_configmap_carries_the_sidecar_import_contract(
    planned_configmaps: list[dict[str, Any]],
) -> None:
    for configmap in planned_configmaps:
        metadata = configmap["metadata"]
        labels = metadata.get("labels") or {}
        assert labels.get("grafana_dashboard") == "1", metadata["name"]
        assert metadata.get("namespace") == "monitoring", metadata["name"]
        data = configmap.get("data") or {}
        assert data, f"{metadata['name']} carries no dashboard payloads"
        for key in data:
            assert key.endswith(".json"), f"{metadata['name']} data key {key}"


def test_legend_template_tokens_survive_the_applier_substitution(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    """Grafana's lowercase legend tokens must pass through planning intact.

    The applier resolves only ``{{UPPER_SNAKE}}`` feature placeholders; a
    regression that widens that substitution would blank every legend in the
    GPU dashboard.
    """
    gpu = dashboards["gco-gpu-dcgm.json"]
    legends = [
        target.get("legendFormat", "")
        for panel in gpu["panels"]
        for target in panel.get("targets", [])
    ]
    assert any("{{Hostname}}" in legend for legend in legends)
    assert any("{{gpu}}" in legend for legend in legends)

    cost = dashboards["gco-cost.json"]
    cost_legends = [
        target.get("legendFormat", "")
        for panel in cost["panels"]
        for target in panel.get("targets", [])
    ]
    assert any("{{namespace}}" in legend for legend in cost_legends)


# ---------------------------------------------------------------------------
# Dashboard payloads
# ---------------------------------------------------------------------------


def test_every_payload_parses_and_carries_identity(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    assert len(dashboards) >= 5
    for key, dashboard in dashboards.items():
        assert isinstance(dashboard.get("title"), str) and dashboard["title"], key
        assert isinstance(dashboard.get("uid"), str) and dashboard["uid"], key
        assert isinstance(dashboard.get("schemaVersion"), int), key


def test_uids_are_unique_and_grafana_acceptable(dashboards: dict[str, dict[str, Any]]) -> None:
    uids = [dashboard["uid"] for dashboard in dashboards.values()]
    assert len(uids) == len(set(uids)), f"duplicate dashboard uids: {uids}"
    for uid in uids:
        assert len(uid) <= UID_MAX_LENGTH, uid
        assert UID_PATTERN.match(uid), uid


def test_titles_are_unique(dashboards: dict[str, dict[str, Any]]) -> None:
    titles = [dashboard["title"] for dashboard in dashboards.values()]
    assert len(titles) == len(set(titles)), f"duplicate dashboard titles: {titles}"


def test_panels_have_types_unique_ids_and_grid_positions(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    for key, dashboard in dashboards.items():
        panels = dashboard.get("panels")
        assert isinstance(panels, list) and panels, f"{key} has no panels"
        seen_ids: set[int] = set()
        for panel in panels:
            where = f"{key} panel {panel.get('id')!r}"
            assert isinstance(panel.get("id"), int), where
            assert panel["id"] not in seen_ids, f"{where}: duplicate panel id"
            seen_ids.add(panel["id"])
            assert isinstance(panel.get("type"), str) and panel["type"], where
            assert isinstance(panel.get("title"), str) and panel["title"], where
            grid = panel.get("gridPos")
            assert isinstance(grid, dict), f"{where}: missing gridPos"
            for field in ("h", "w", "x", "y"):
                assert isinstance(grid.get(field), int), f"{where}: gridPos.{field}"
            assert grid["h"] >= 1 and grid["w"] >= 1, where
            assert grid["x"] >= 0 and grid["y"] >= 0, where
            assert grid["x"] + grid["w"] <= GRID_COLUMNS, (
                f"{where}: panel overflows the {GRID_COLUMNS}-column grid"
            )


def test_every_target_carries_a_promql_expression(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    for key, dashboard in dashboards.items():
        for panel in dashboard["panels"]:
            targets = panel.get("targets")
            assert isinstance(targets, list) and targets, f"{key} panel {panel['id']}"
            for target in targets:
                expr = target.get("expr")
                assert isinstance(expr, str) and expr.strip(), (
                    f"{key} panel {panel['id']}: target without a PromQL expr"
                )


# ---------------------------------------------------------------------------
# Extraction-script lockstep
# ---------------------------------------------------------------------------


def test_extraction_script_agrees_with_the_applier_path(
    dashboards: dict[str, dict[str, Any]],
) -> None:
    """The CI extraction must see exactly the dashboards the applier plans."""
    extracted = script.extract_dashboards([MANIFESTS_DIR / name for name in DASHBOARD_MANIFESTS])
    assert set(extracted) == {dashboard["uid"] for dashboard in dashboards.values()}
    for uid, dashboard in extracted.items():
        assert dashboard["title"], uid


def test_chart_pin_is_readable_for_the_ci_image_resolution() -> None:
    pin = script.read_chart_pin(PROJECT_ROOT / "lambda" / "helm-installer" / "charts.yaml")
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin["version"]), pin
    assert pin["repo_url"].startswith("https://"), pin


def test_fetch_treats_startup_transport_failures_as_retriable() -> None:
    """A booting Grafana resets or refuses connections; the poll must retry.

    docker-proxy accepts the published port before Grafana listens, so the
    first health probes can die with a raw ConnectionResetError rather than
    a URLError — caught live on the CI runner.
    """
    from unittest.mock import patch

    for boot_noise in (
        ConnectionResetError(104, "Connection reset by peer"),
        ConnectionRefusedError(111, "Connection refused"),
        TimeoutError("timed out"),
    ):
        with patch.object(script.urllib.request, "urlopen", side_effect=boot_noise):
            assert script._get("http://127.0.0.1:3000/api/health") == (0, None)


def test_extraction_rejects_a_malformed_payload(tmp_path: Path) -> None:
    """The whole point: a stray comma must fail loudly, not ship silently."""
    manifest = tmp_path / "post-helm-grafana-broken.yaml"
    manifest.write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: gco-dashboard-broken\n"
        "  namespace: monitoring\n"
        "  labels:\n"
        '    grafana_dashboard: "1"\n'
        "data:\n"
        "  broken.json: |\n"
        '    {"title": "Broken", "uid": "broken",}\n',
        encoding="utf-8",
    )

    with pytest.raises(script.ValidationError, match="invalid JSON"):
        script.extract_dashboards([manifest])


# ── Extraction rejections ────────────────────────────────────────────────────
#
# Each of these is something the Grafana sidecar would swallow silently: the
# dashboard just never appears in the UI, and nobody notices until they go
# looking for it. The extractor's job is to make them loud.


def _configmap(
    *, name: str = "gco-dashboard", labelled: bool = True, data: dict[str, str] | None = None
) -> str:
    label = '    grafana_dashboard: "1"\n' if labelled else "    other: label\n"
    body = "".join(
        f"  {key}: |\n    {value}\n"
        for key, value in (data or {"d.json": '{"uid": "d", "title": "D"}'}).items()
    )
    return (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
        f"  name: {name}\n  namespace: monitoring\n  labels:\n{label}"
        f"data:\n{body}"
    )


def _write(tmp_path: Path, *documents: str, name: str = "m.yaml") -> Path:
    path = tmp_path / name
    path.write_text("---\n".join(documents), encoding="utf-8")
    return path


class TestExtractionRejections:
    def test_documents_that_are_not_labelled_configmaps_are_ignored(self, tmp_path: Path) -> None:
        """Only ConfigMaps carrying the sidecar label are dashboards."""
        manifest = _write(
            tmp_path,
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\n",
            _configmap(name="unlabelled", labelled=False),
            _configmap(name="real"),
            "not-a-mapping\n",
        )

        assert set(script.extract_dashboards([manifest])) == {"d"}

    def test_non_json_data_keys_are_ignored(self, tmp_path: Path) -> None:
        """A ConfigMap can carry a README next to its dashboards."""
        manifest = _write(
            tmp_path,
            _configmap(data={"README.md": "prose", "d.json": '{"uid": "d", "title": "D"}'}),
        )

        assert set(script.extract_dashboards([manifest])) == {"d"}

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param('{"title": "No uid"}', "dashboard has no uid", id="uid-missing"),
            pytest.param('{"uid": "", "title": "T"}', "dashboard has no uid", id="uid-empty"),
            pytest.param('{"uid": 7, "title": "T"}', "dashboard has no uid", id="uid-not-string"),
            pytest.param('{"uid": "d"}', "dashboard has no title", id="title-missing"),
        ],
    )
    def test_a_dashboard_without_identity_is_rejected(
        self, tmp_path: Path, payload: str, expected: str
    ) -> None:
        manifest = _write(tmp_path, _configmap(data={"d.json": payload}))

        with pytest.raises(script.ValidationError, match=expected):
            script.extract_dashboards([manifest])

    def test_a_duplicate_uid_across_configmaps_is_rejected(self, tmp_path: Path) -> None:
        """Grafana keeps one of the two and drops the other without a word."""
        manifest = _write(tmp_path, _configmap(name="one"), _configmap(name="two"))

        with pytest.raises(script.ValidationError, match="duplicate dashboard uid 'd'"):
            script.extract_dashboards([manifest])

    def test_finding_no_dashboards_at_all_is_an_error(self, tmp_path: Path) -> None:
        """An empty result would let the verify step pass vacuously."""
        manifest = _write(tmp_path, _configmap(labelled=False))

        with pytest.raises(script.ValidationError, match="no sidecar-labeled dashboard"):
            script.extract_dashboards([manifest])


class TestChartPinRejections:
    def test_a_missing_entry_is_reported(self, tmp_path: Path) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text("charts:\n  other:\n    chart: something-else\n", encoding="utf-8")

        with pytest.raises(script.ValidationError, match="no kube-prometheus-stack entry"):
            script.read_chart_pin(charts)

    def test_an_entry_missing_its_version_or_repo_is_reported(self, tmp_path: Path) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text(
            "charts:\n  kps:\n    chart: kube-prometheus-stack\n    version: ''\n"
            "    repo_url: https://x\n",
            encoding="utf-8",
        )

        with pytest.raises(script.ValidationError, match="missing version or repo_url"):
            script.read_chart_pin(charts)

    def test_a_flat_document_without_a_charts_key_is_accepted(self, tmp_path: Path) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text(
            "kps:\n  chart: kube-prometheus-stack\n  version: 1.2.3\n  repo_url: https://x\n",
            encoding="utf-8",
        )

        assert script.read_chart_pin(charts) == {"version": "1.2.3", "repo_url": "https://x"}


# ── _get ─────────────────────────────────────────────────────────────────────


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        import json

        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_get_refuses_a_non_http_url() -> None:
    """The scheme check is what earns the urlopen suppression."""
    with pytest.raises(script.ValidationError, match="refusing non-HTTP URL"):
        script._get("file:///etc/passwd")


def test_get_returns_status_and_body_and_sends_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        captured["auth"] = request.get_header("Authorization")
        return _Response(200, {"database": "ok"})

    monkeypatch.setattr(script.urllib.request, "urlopen", fake_urlopen)

    assert script._get("http://g/api/health", auth=("admin", "pw")) == (200, {"database": "ok"})
    import base64

    assert captured["auth"] == "Basic " + base64.b64encode(b"admin:pw").decode()


def test_get_returns_the_http_status_of_an_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 for an unprovisioned uid is an answer, not a transport failure."""
    import urllib.error

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(script.urllib.request, "urlopen", fake_urlopen)

    assert script._get("http://g/api/dashboards/uid/x") == (404, None)


def test_get_treats_a_non_json_body_as_retriable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A booting Grafana can answer 200 with an HTML placeholder page."""

    class _Html:
        status = 200

        def read(self) -> bytes:
            return b"<html>starting</html>"

        def __enter__(self) -> _Html:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(script.urllib.request, "urlopen", lambda *a, **k: _Html())

    assert script._get("http://g/api/health") == (0, None)


# ── verify, against a scripted Grafana ───────────────────────────────────────
#
# ``_get`` is replaced by a scripted responder keyed on the URL path, and
# ``time`` is frozen-then-advanced so the deadlines are exercised without
# waiting on a clock. Each scenario is a small table of what Grafana "says".


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _grafana(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[tuple[int, object]]]):
    """Install a fake Grafana. ``answers[path]`` is consumed one call at a time; the
    last entry repeats forever."""
    clock = _Clock()
    monkeypatch.setattr(script.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(script.time, "sleep", clock.sleep)
    calls: list[str] = []

    def fake_get(url: str, auth=None, timeout=10.0):  # noqa: ANN001, ANN202
        path = url.split("/", 3)[3] if url.count("/") >= 3 else url
        calls.append(path)
        queue = answers[path]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(script, "_get", fake_get)
    return clock, calls


def _dashboards_dir(tmp_path: Path, *uids: str) -> Path:
    import json

    directory = tmp_path / "dashboards"
    directory.mkdir()
    for uid in uids:
        (directory / f"{uid}.json").write_text(
            json.dumps({"uid": uid, "title": f"Title {uid}"}), encoding="utf-8"
        )
    return directory


def _loaded(uid: str, *, title: str | None = None, provisioned: object = True) -> tuple[int, dict]:
    return 200, {
        "dashboard": {"title": title if title is not None else f"Title {uid}"},
        "meta": {"provisioned": provisioned},
    }


class TestVerify:
    def test_every_dashboard_provisioned_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _grafana(
            monkeypatch,
            {
                "api/health": [(200, {"database": "ok", "version": "12.0.0"})],
                "api/dashboards/uid/a": [_loaded("a")],
                "api/dashboards/uid/b": [_loaded("b")],
            },
        )

        failures = script.verify("http://g", _dashboards_dir(tmp_path, "a", "b"), "u", "p")

        assert failures == []
        out = capsys.readouterr().out
        assert "Grafana healthy: version 12.0.0" in out
        assert out.count("PASS ") == 2

    def test_health_polling_retries_until_the_database_is_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Boot noise first, then a real answer; the poll must ride it out."""
        _, calls = _grafana(
            monkeypatch,
            {
                "api/health": [
                    (0, None),
                    (503, None),
                    (200, {"database": "starting"}),
                    (200, {"database": "ok"}),
                ],
                "api/dashboards/uid/a": [_loaded("a")],
            },
        )

        assert script.verify("http://g", _dashboards_dir(tmp_path, "a"), "u", "p") == []
        assert calls.count("api/health") == 4

    def test_a_grafana_that_never_becomes_healthy_is_one_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _grafana(monkeypatch, {"api/health": [(0, None)]})

        failures = script.verify(
            "http://g", _dashboards_dir(tmp_path, "a"), "u", "p", timeout_seconds=10
        )

        assert failures == ["Grafana at http://g did not become healthy within 10s"]

    def test_an_empty_dashboards_dir_is_a_failure_not_a_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to check must not read as everything passed."""
        _grafana(monkeypatch, {"api/health": [(200, {"database": "ok"})]})
        empty = tmp_path / "dashboards"
        empty.mkdir()

        failures = script.verify("http://g", empty, "u", "p")

        assert failures == [f"no extracted dashboards found under {empty}"]

    def test_provisioning_is_polled_per_dashboard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """File provisioning is asynchronous; a 404 then a 200 is normal."""
        _, calls = _grafana(
            monkeypatch,
            {
                "api/health": [(200, {"database": "ok"})],
                "api/dashboards/uid/a": [(404, None), (404, None), _loaded("a")],
            },
        )

        assert script.verify("http://g", _dashboards_dir(tmp_path, "a"), "u", "p") == []
        assert calls.count("api/dashboards/uid/a") == 3

    def test_a_dashboard_that_never_appears_is_reported_with_its_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _grafana(
            monkeypatch,
            {"api/health": [(200, {"database": "ok"})], "api/dashboards/uid/a": [(404, None)]},
        )

        failures = script.verify("http://g", _dashboards_dir(tmp_path, "a"), "u", "p")

        assert failures == ["a: Grafana answered 404, expected 200"]

    def test_a_title_mismatch_and_an_unprovisioned_flag_are_distinct_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both mean the file provisioner did not load *this* file."""
        _grafana(
            monkeypatch,
            {
                "api/health": [(200, {"database": "ok"})],
                "api/dashboards/uid/a": [_loaded("a", title="Something Else")],
                "api/dashboards/uid/b": [_loaded("b", provisioned=False)],
            },
        )

        failures = script.verify("http://g", _dashboards_dir(tmp_path, "a", "b"), "u", "p")

        assert failures == [
            "a: loaded title 'Something Else' != source 'Title a'",
            "b: dashboard loaded but meta.provisioned is False",
        ]

    def test_a_200_with_a_non_object_body_is_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _grafana(
            monkeypatch,
            {"api/health": [(200, {"database": "ok"})], "api/dashboards/uid/a": [(200, "text")]},
        )

        failures = script.verify("http://g", _dashboards_dir(tmp_path, "a"), "u", "p")

        assert failures == ["a: Grafana answered 200, expected 200"]


# ── The CLI ──────────────────────────────────────────────────────────────────


class TestCli:
    def test_extract_writes_dashboards_and_the_provider_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = _write(
            tmp_path,
            _configmap(name="one", data={"a.json": '{"uid": "a", "title": "A"}'}),
            _configmap(name="two", data={"b.json": '{"uid": "b", "title": "B"}'}),
        )
        out_dir = tmp_path / "out"

        rc = script.main(["extract", "--manifest", str(manifest), "--out-dir", str(out_dir)])

        assert rc == 0
        assert sorted(p.name for p in (out_dir / "dashboards").iterdir()) == ["a.json", "b.json"]
        provider = (out_dir / "provisioning" / "gco-dashboards.yaml").read_text(encoding="utf-8")
        assert provider == script._PROVIDER_YAML
        assert "extracted 2 dashboard(s)" in capsys.readouterr().out

    def test_extract_reports_a_validation_error_as_exit_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = _write(tmp_path, _configmap(data={"d.json": '{"title": "no uid"}'}))

        rc = script.main(["extract", "--manifest", str(manifest), "--out-dir", str(tmp_path / "o")])

        assert rc == 1
        assert "FAIL" in capsys.readouterr().err

    def test_chart_version_prints_and_appends_to_github_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``GITHUB_OUTPUT`` is how the workflow resolves the Grafana image tag."""
        charts = tmp_path / "charts.yaml"
        charts.write_text(
            "charts:\n  kps:\n    chart: kube-prometheus-stack\n    version: 9.9.9\n"
            "    repo_url: https://charts.example\n",
            encoding="utf-8",
        )
        github_output = tmp_path / "gh_output"
        github_output.write_text("previous=1\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

        rc = script.main(["chart-version", "--charts-yaml", str(charts)])

        assert rc == 0
        assert capsys.readouterr().out == "version=9.9.9\nrepo_url=https://charts.example\n"
        assert github_output.read_text(encoding="utf-8") == (
            "previous=1\nversion=9.9.9\nrepo_url=https://charts.example\n"
        ), "existing outputs must be preserved, not overwritten"

    def test_chart_version_without_github_output_only_prints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text(
            "charts:\n  kps:\n    chart: kube-prometheus-stack\n    version: 1.0.0\n"
            "    repo_url: https://x\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        assert script.main(["chart-version", "--charts-yaml", str(charts)]) == 0
        assert "version=1.0.0" in capsys.readouterr().out

    def test_verify_subcommand_passes_credentials_and_reports_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: dict[str, object] = {}

        def fake_verify(url, dashboards_dir, user, password, timeout_seconds):  # noqa: ANN001, ANN202
            seen.update(url=url, user=user, password=password, timeout=timeout_seconds)
            return ["a: Grafana answered 404, expected 200"]

        monkeypatch.setattr(script, "verify", fake_verify)
        monkeypatch.setenv("GRAFANA_USER", "ci-user")
        monkeypatch.setenv("GRAFANA_PASSWORD", "ci-pass")

        rc = script.main(
            [
                "verify",
                "--url",
                "http://g:3000/",
                "--dashboards-dir",
                str(tmp_path),
                "--timeout",
                "5",
            ]
        )

        assert rc == 1
        assert seen == {
            "url": "http://g:3000",
            "user": "ci-user",
            "password": "ci-pass",
            "timeout": 5.0,
        }
        assert "FAIL a: Grafana answered 404" in capsys.readouterr().err

    def test_verify_subcommand_defaults_credentials_and_exits_zero_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def fake_verify(url, dashboards_dir, user, password, timeout_seconds):  # noqa: ANN001, ANN202
            seen.update(user=user, password=password)
            return []

        monkeypatch.setattr(script, "verify", fake_verify)
        monkeypatch.delenv("GRAFANA_USER", raising=False)
        monkeypatch.delenv("GRAFANA_PASSWORD", raising=False)

        assert script.main(["verify", "--dashboards-dir", str(tmp_path)]) == 0
        assert seen == {"user": "admin", "password": "admin"}
