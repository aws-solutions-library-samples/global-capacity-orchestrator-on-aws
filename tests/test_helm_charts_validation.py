"""Tests for ``.github/scripts/validate_helm_charts.py``.

The validator gates a CI job (``integration:helm:charts-valid`` in
``integration-tests.yml``) that proves every ``(chart, version)`` pinned in
``lambda/helm-installer/charts.yaml`` is a real, installable Helm chart. These
tests pin the offline behavior so a refactor can't quietly relax the rules, and
add an opt-in online test that exercises the real ``helm`` resolve/render path.

The script is loaded by file path because ``.github/scripts/`` isn't on
``sys.path`` and shouldn't be turned into a package just to support tests —
same posture as ``tests/test_pip_audit_ignore_validator.py``.

Two tiers:

* **Offline** (always run): structural checks, reference construction, and
  ``main()`` exit codes against in-memory dicts and temp files. No network,
  no ``helm`` binary — these run in the normal unit job.
* **Online** (opt-in): gated behind ``GCO_HELM_CHART_VALIDATION=1`` *and* a
  ``helm`` binary on ``PATH``, so the ~30s network pass never runs in the
  normal unit job. The dedicated CI job sets the env var.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "validate_helm_charts.py"
LIVE_CHARTS = PROJECT_ROOT / "lambda" / "helm-installer" / "charts.yaml"


def _load_validator():
    """Load the validator module by file path.

    ``.github/scripts`` is intentionally not a Python package, so import by
    path rather than adding an ``__init__.py`` — mirrors the pip-audit / trivy
    validator tests.
    """
    spec = importlib.util.spec_from_file_location("validate_helm_charts", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _classic(**overrides) -> dict:
    """A minimal well-formed classic (HTTP) chart entry, with overrides."""
    base = {
        "enabled": True,
        "repo_name": "kedacore",
        "repo_url": "https://kedacore.github.io/charts",
        "chart": "keda",
        "version": "2.20.1",
        "namespace": "keda",
    }
    base.update(overrides)
    return base


def _oci(**overrides) -> dict:
    """A minimal well-formed OCI chart entry, with overrides."""
    base = {
        "enabled": True,
        "repo_url": "oci://registry.k8s.io/kueue/charts",
        "chart": "kueue",
        "version": "0.18.2",
        "namespace": "kueue-system",
        "use_oci": True,
    }
    base.update(overrides)
    return base


# ── validate_structure: happy paths ──────────────────────────────────────────


class TestValidateStructureHappyPath:
    def test_well_formed_classic_and_oci_pass(self) -> None:
        charts = {"keda": _classic(), "kueue": _oci()}
        assert validator.validate_structure(charts) == []

    def test_leading_v_version_is_accepted(self) -> None:
        # cert-manager / aws-efa tag their charts as v1.20.3 / v0.5.29.
        charts = {"cert-manager": _classic(version="v1.20.3")}
        assert validator.validate_structure(charts) == []

    def test_two_component_version_is_accepted(self) -> None:
        charts = {"x": _classic(version="1.8")}
        assert validator.validate_structure(charts) == []

    def test_disabled_charts_are_still_validated_by_default(self) -> None:
        # Disabled charts can be toggled on via cdk.json, so a broken pin must
        # still fail by default.
        charts = {"slurm": _oci(enabled=False, version="not-a-version")}
        errors = validator.validate_structure(charts)
        assert any("slurm" in e for e in errors)

    def test_enabled_only_skips_disabled_charts(self) -> None:
        charts = {"slurm": _oci(enabled=False, version="not-a-version")}
        assert validator.validate_structure(charts, enabled_only=True) == []


# ── validate_structure: failure cases ────────────────────────────────────────


class TestValidateStructureFailures:
    def test_missing_version(self) -> None:
        charts = {"x": _classic()}
        del charts["x"]["version"]
        errors = validator.validate_structure(charts)
        assert errors == ["x: missing or empty 'version'"]

    def test_empty_chart_name(self) -> None:
        errors = validator.validate_structure({"x": _classic(chart="  ")})
        assert any("missing or empty 'chart'" in e for e in errors)

    def test_missing_repo_url(self) -> None:
        charts = {"x": _classic()}
        del charts["x"]["repo_url"]
        errors = validator.validate_structure(charts)
        assert any("missing or empty 'repo_url'" in e for e in errors)

    def test_non_semver_version_flagged(self) -> None:
        errors = validator.validate_structure({"x": _classic(version="latest")})
        assert any("not a valid SemVer" in e for e in errors)

    def test_oci_url_without_use_oci_flag(self) -> None:
        charts = {"x": _classic(repo_url="oci://ghcr.io/x/charts")}
        errors = validator.validate_structure(charts)
        assert any("use_oci is not set to true" in e for e in errors)

    def test_use_oci_true_but_http_url(self) -> None:
        errors = validator.validate_structure({"x": _oci(repo_url="https://example.com/charts")})
        assert any("not an oci:// URL" in e for e in errors)

    def test_classic_url_bad_scheme(self) -> None:
        errors = validator.validate_structure({"x": _classic(repo_url="ftp://example.com")})
        assert any("must be http(s):// or oci://" in e for e in errors)

    def test_classic_missing_repo_name(self) -> None:
        charts = {"x": _classic()}
        del charts["x"]["repo_name"]
        errors = validator.validate_structure(charts)
        assert any("needs a 'repo_name'" in e for e in errors)

    def test_entry_not_a_mapping(self) -> None:
        errors = validator.validate_structure({"x": ["not", "a", "dict"]})
        assert errors == ["x: entry is not a mapping"]

    def test_empty_charts_mapping(self) -> None:
        errors = validator.validate_structure({})
        assert errors == ["charts.yaml contains no chart entries under 'charts:'"]

    def test_all_problems_reported_in_one_pass(self) -> None:
        # Operators shouldn't have to fix-and-rerun to find every problem.
        charts = {
            "a": _classic(version="nope"),
            "b": _classic(repo_url="oci://ghcr.io/b/charts"),
        }
        errors = validator.validate_structure(charts)
        assert len(errors) == 2


# ── build_refs + ChartRef.reference ──────────────────────────────────────────


class TestBuildRefs:
    def test_classic_reference_shape(self) -> None:
        (ref,) = validator.build_refs({"keda": _classic()})
        assert ref.use_oci is False
        assert ref.reference() == "kedacore/keda"

    def test_oci_reference_shape(self) -> None:
        (ref,) = validator.build_refs({"kueue": _oci()})
        assert ref.use_oci is True
        # OCI ref is repo_url + "/" + chart — exactly what handler.install_chart builds.
        assert ref.reference() == "oci://registry.k8s.io/kueue/charts/kueue"

    def test_malformed_entries_are_skipped(self) -> None:
        # build_refs only yields entries resolvable by helm; validate_structure
        # is what reports the malformed ones.
        charts = {
            "good": _classic(),
            "no-version": {"repo_name": "r", "repo_url": "https://x", "chart": "c"},
        }
        refs = validator.build_refs(charts)
        assert [r.name for r in refs] == ["good"]

    def test_classic_without_repo_name_is_skipped(self) -> None:
        charts = {"x": _classic()}
        del charts["x"]["repo_name"]
        assert validator.build_refs(charts) == []

    def test_enabled_only_filter(self) -> None:
        charts = {"on": _classic(enabled=True), "off": _classic(enabled=False)}
        refs = validator.build_refs(charts, enabled_only=True)
        assert [r.name for r in refs] == ["on"]

    def test_values_default_to_empty_dict(self) -> None:
        (ref,) = validator.build_refs({"keda": _classic()})
        assert ref.values == {}


# ── load_charts ───────────────────────────────────────────────────────────────


class TestLoadCharts:
    def test_loads_charts_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "charts.yaml"
        path.write_text("charts:\n  keda:\n    chart: keda\n    version: '2.20.1'\n")
        loaded = validator.load_charts(path)
        assert "keda" in loaded

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            validator.load_charts(tmp_path / "nope.yaml")

    def test_missing_charts_key_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "charts.yaml"
        path.write_text("something_else: true\n")
        with pytest.raises(ValueError, match="charts:"):
            validator.load_charts(path)


# ── helper functions ──────────────────────────────────────────────────────────


class TestHelpers:
    def test_chart_version_from_show(self) -> None:
        out = "apiVersion: v2\nname: keda\nversion: 2.20.1\n"
        assert validator._chart_version_from_show(out) == "2.20.1"

    def test_chart_version_from_show_handles_garbage(self) -> None:
        assert validator._chart_version_from_show("::: not yaml :::") in (None, "")

    def test_versions_match_ignores_leading_v(self) -> None:
        assert validator._versions_match("v1.20.3", "1.20.3")
        assert validator._versions_match("1.20.3", "v1.20.3")
        assert not validator._versions_match("1.20.3", "1.20.4")

    def test_tail_truncates_long_text(self) -> None:
        assert validator._tail("x" * 1000, limit=100).startswith("...")
        assert validator._tail("short") == "short"


# ── main(): exit codes ────────────────────────────────────────────────────────


class TestMainExitCodes:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "charts.yaml"
        path.write_text(body)
        return path

    def test_offline_clean_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._write(
            tmp_path,
            "charts:\n"
            "  keda:\n"
            "    repo_name: kedacore\n"
            "    repo_url: https://kedacore.github.io/charts\n"
            "    chart: keda\n"
            "    version: '2.20.1'\n"
            "    namespace: keda\n",
        )
        rc = validator.main(["--charts", str(path), "--mode", "offline"])
        assert rc == 0
        assert "structurally valid" in capsys.readouterr().out

    def test_offline_bad_returns_one_and_names_chart(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._write(
            tmp_path,
            "charts:\n"
            "  broken:\n"
            "    repo_name: r\n"
            "    repo_url: https://x/charts\n"
            "    chart: c\n"
            "    version: not-a-version\n",
        )
        rc = validator.main(["--charts", str(path), "--mode", "offline"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "broken" in out

    def test_missing_file_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = validator.main(["--charts", str(tmp_path / "nope.yaml"), "--mode", "offline"])
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_online_without_helm_binary_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # --mode online must fail loudly when the requested helm binary is
        # absent, rather than silently degrading to a structural-only pass.
        path = self._write(
            tmp_path,
            "charts:\n"
            "  keda:\n"
            "    repo_name: kedacore\n"
            "    repo_url: https://kedacore.github.io/charts\n"
            "    chart: keda\n"
            "    version: '2.20.1'\n",
        )
        rc = validator.main(
            [
                "--charts",
                str(path),
                "--mode",
                "online",
                "--helm-binary",
                "helm-does-not-exist-xyz",
            ]
        )
        assert rc == 2
        assert "requires the" in capsys.readouterr().err

    def test_auto_without_helm_runs_structural_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._write(
            tmp_path,
            "charts:\n"
            "  keda:\n"
            "    repo_name: kedacore\n"
            "    repo_url: https://kedacore.github.io/charts\n"
            "    chart: keda\n"
            "    version: '2.20.1'\n",
        )
        rc = validator.main(
            ["--charts", str(path), "--mode", "auto", "--helm-binary", "helm-does-not-exist-xyz"]
        )
        assert rc == 0
        assert "structural checks only" in capsys.readouterr().out


# ── emit query modes (--emit-ref / --emit-values) ────────────────────────────


class TestEmitQueryModes:
    """--emit-ref / --emit-values feed the integration:kind:examples-smoke job.

    That CI job helm-installs the pinned trainer and mlflow charts into a
    kind cluster; these flags are how it gets the exact reference, version,
    namespace, and values from charts.yaml at run time instead of carrying
    copies that could drift.
    """

    CHARTS = {
        "mlflow": _oci(
            repo_url="oci://ghcr.io/mlflow/charts",
            chart="mlflow",
            version="0.1.0",
            namespace="monitoring",
            values={"fullnameOverride": "mlflow", "image": {"tag": "v9-full"}},
        ),
        "keda": _classic(values={"watchNamespace": ""}),
    }

    def test_emit_ref_builds_the_installer_reference(self) -> None:
        text, error = validator.emit_chart_ref(self.CHARTS, "mlflow")
        assert error == ""
        assert text == (
            "oci://ghcr.io/mlflow/charts/mlflow 0.1.0 monitoring oci://ghcr.io/mlflow/charts"
        )

    def test_emit_ref_carries_the_repo_url_for_classic_charts(self) -> None:
        """Classic refs are repo_name/chart — the URL is what helm pull needs."""
        text, error = validator.emit_chart_ref(self.CHARTS, "keda")
        assert error == ""
        ref, version, namespace, repo_url = text.split()
        assert ref == "kedacore/keda"
        assert repo_url == "https://kedacore.github.io/charts"
        assert version and namespace

    def test_emit_ref_unknown_chart_names_known_entries(self) -> None:
        text, error = validator.emit_chart_ref(self.CHARTS, "nope")
        assert text == ""
        assert "'nope' not found" in error
        assert "keda" in error and "mlflow" in error

    def test_emit_values_round_trips_the_values_block(self) -> None:
        text, error = validator.emit_chart_values(self.CHARTS, "mlflow")
        assert error == ""
        assert yaml.safe_load(text) == self.CHARTS["mlflow"]["values"]

    def test_emit_values_rejects_deployment_tokens(self) -> None:
        charts = {
            "tokened": _classic(
                values={"serviceAccount": {"roleArn": "{{SERVICE_ACCOUNT_ROLE_ARN}}"}}
            )
        }
        text, error = validator.emit_chart_values(charts, "tokened")
        assert text == ""
        assert "{{SERVICE_ACCOUNT_ROLE_ARN}}" in error
        assert "standalone-installable" in error

    def test_main_emit_ref_prints_and_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "charts.yaml"
        path.write_text(yaml.safe_dump({"charts": self.CHARTS}))
        rc = validator.main(["--charts", str(path), "--emit-ref", "mlflow"])
        assert rc == 0
        assert capsys.readouterr().out == (
            "oci://ghcr.io/mlflow/charts/mlflow 0.1.0 monitoring oci://ghcr.io/mlflow/charts\n"
        )

    def test_main_emit_values_prints_yaml_and_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "charts.yaml"
        path.write_text(yaml.safe_dump({"charts": self.CHARTS}))
        rc = validator.main(["--charts", str(path), "--emit-values", "keda"])
        assert rc == 0
        assert yaml.safe_load(capsys.readouterr().out) == {"watchNamespace": ""}

    def test_main_emit_unknown_chart_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "charts.yaml"
        path.write_text(yaml.safe_dump({"charts": self.CHARTS}))
        rc = validator.main(["--charts", str(path), "--emit-values", "nope"])
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_emit_flags_are_mutually_exclusive(self, tmp_path: Path) -> None:
        path = tmp_path / "charts.yaml"
        path.write_text(yaml.safe_dump({"charts": self.CHARTS}))
        with pytest.raises(SystemExit) as excinfo:
            validator.main(["--charts", str(path), "--emit-ref", "keda", "--emit-values", "keda"])
        assert excinfo.value.code == 2

    def test_live_charts_emit_smoke_charts_cleanly(self) -> None:
        """The two charts the smoke job installs must emit token-free values.

        Guards the job's runtime contract against future charts.yaml edits:
        if someone adds a {{TOKEN}} to mlflow or kubeflow-trainer values, the
        kind job's helm install would receive a literal brace string — fail
        here first, with a message pointing at the contract.
        """
        charts = validator.load_charts(validator._DEFAULT_CHARTS)
        for name in ("mlflow", "kubeflow-trainer", "kube-prometheus-stack"):
            ref_text, ref_error = validator.emit_chart_ref(charts, name)
            assert ref_error == "", ref_error
            assert len(ref_text.split()) == 4
            _, values_error = validator.emit_chart_values(charts, name)
            assert values_error == "", values_error


# ── live charts.yaml (offline) ────────────────────────────────────────────────


class TestLiveChartsOffline:
    """The committed charts.yaml must be structurally valid and fully resolvable-in-principle."""

    def test_committed_charts_yaml_is_structurally_valid(self) -> None:
        charts = validator.load_charts(LIVE_CHARTS)
        errors = validator.validate_structure(charts)
        assert errors == [], "charts.yaml structural validation failed:\n" + "\n".join(errors)

    def test_every_live_chart_builds_a_reference(self) -> None:
        # If a real entry silently fails to build a ref, the online pass would
        # skip it — guard against that.
        charts = validator.load_charts(LIVE_CHARTS)
        assert len(validator.build_refs(charts)) == len(charts)

    def test_main_offline_on_live_charts_returns_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = validator.main(["--charts", str(LIVE_CHARTS), "--mode", "offline"])
        assert rc == 0


# ── live charts.yaml (online, opt-in) ─────────────────────────────────────────

_HELM = shutil.which("helm")
_ONLINE_ENABLED = os.environ.get("GCO_HELM_CHART_VALIDATION") == "1" and _HELM is not None


@pytest.mark.helm_online
@pytest.mark.skipif(
    not _ONLINE_ENABLED,
    reason="opt-in: set GCO_HELM_CHART_VALIDATION=1 and install helm to run the online checks",
)
class TestLiveChartsOnline:
    """Real ``helm`` resolve/render of every pinned chart. Opt-in, network-bound."""

    def test_all_live_charts_resolve_and_render(self) -> None:
        charts = validator.load_charts(LIVE_CHARTS)
        refs = validator.build_refs(charts)
        errors = validator.validate_online(refs)
        assert errors == [], "Helm resolve/render failed:\n" + "\n".join(errors)

    def test_shipped_trainer_runtime_matches_the_pinned_chart(self) -> None:
        # The committed torch-distributed extraction must reproduce what the
        # pinned kubeflow-trainer chart actually ships (image + full spec,
        # modulo the documented automount deviation).
        charts = validator.load_charts(LIVE_CHARTS)
        errors = validator.validate_trainer_runtime_lockstep(charts, online=True)
        assert errors == [], "Trainer runtime lockstep failed:\n" + "\n".join(errors)

    def test_online_detects_a_bogus_version(self) -> None:
        # Give the check a version that cannot exist and confirm it fails —
        # proves the online gate has teeth against a mistyped pin.
        bogus = validator.ChartRef(
            name="keda",
            chart="keda",
            version="99.99.99",
            repo_name="kedacore",
            repo_url="https://kedacore.github.io/charts",
            use_oci=False,
            namespace="keda",
            enabled=True,
            values={},
        )
        errors = validator.validate_online([bogus])
        assert errors
        assert any("99.99.99" in e for e in errors)


# ── fixed-count network retry guard (offline) ────────────────────────────────
#
# These pin the retry behavior added to ride out intermittent registry blips
# (the kind that used to force a manual rerun of integration:helm:charts-valid):
# a network-touching helm call is retried a fixed number of times and the first
# success wins. All offline: _run and time.sleep are monkeypatched so nothing
# sleeps or touches the network.


class TestRunWithRetry:
    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Retry tests must never actually sleep.
        monkeypatch.setattr(validator.time, "sleep", lambda *_a, **_k: None)

    def _script(self, monkeypatch: pytest.MonkeyPatch, results: list[tuple]) -> dict:
        """Make validator._run return successive (rc, out, err) tuples, counting calls.

        The last tuple is repeated once the sequence is exhausted, so a
        single-element list models a persistent failure.
        """
        calls = {"n": 0}
        seq = list(results)

        def fake_run(cmd, env, *, timeout=120):  # noqa: ANN001
            calls["n"] += 1
            return seq[min(calls["n"] - 1, len(seq) - 1)]

        monkeypatch.setattr(validator, "_run", fake_run)
        return calls

    def test_success_first_try_runs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._script(monkeypatch, [(0, "ok", "")])
        rc, out, _err = validator._run_with_retry(["helm", "x"], {})
        assert rc == 0 and out == "ok"
        assert calls["n"] == 1

    def test_first_success_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Any single successful attempt counts as success, no matter how many
        # attempts failed before it.
        calls = self._script(
            monkeypatch,
            [(1, "", "boom"), (1, "", "still boom"), (0, "ok", "")],
        )
        rc, _out, _err = validator._run_with_retry(["helm", "x"], {}, attempts=4)
        assert rc == 0
        assert calls["n"] == 3  # stopped as soon as it succeeded

    def test_retries_exactly_attempts_times_then_gives_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A persistent failure is attempted exactly `attempts` times (regardless
        # of the failure text) and then the last result is returned.
        calls = self._script(monkeypatch, [(1, "", "whatever")])
        rc, _out, _err = validator._run_with_retry(["helm", "x"], {}, attempts=4)
        assert rc == 1
        assert calls["n"] == 4

    def test_attempts_one_means_no_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._script(monkeypatch, [(1, "", "boom")])
        validator._run_with_retry(["helm", "x"], {}, attempts=1)
        assert calls["n"] == 1

    def test_backoff_is_exponential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._script(monkeypatch, [(1, "", "boom")])
        sleeps: list[float] = []
        monkeypatch.setattr(validator.time, "sleep", lambda s: sleeps.append(s))
        validator._run_with_retry(["helm", "x"], {}, attempts=4, base_delay=2.0, max_delay=100.0)
        # 3 sleeps between 4 attempts: 2, 4, 8.
        assert sleeps == [2.0, 4.0, 8.0]

    def test_backoff_capped_at_max_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._script(monkeypatch, [(1, "", "boom")])
        sleeps: list[float] = []
        monkeypatch.setattr(validator.time, "sleep", lambda s: sleeps.append(s))
        validator._run_with_retry(["helm", "x"], {}, attempts=5, base_delay=10.0, max_delay=15.0)
        assert sleeps == [10.0, 15.0, 15.0, 15.0]


class TestRenderChartUsesRetry:
    def test_render_routes_through_run_with_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return (0, "rendered", "")

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)
        (ref,) = validator.build_refs({"keda": _classic()})
        err = validator._render_chart(ref, ref.reference(), "helm", {}, verbose=True)
        assert err is None
        assert captured["cmd"][:2] == ["helm", "template"]
        assert "--version" in captured["cmd"]
        # verbose is threaded so a retry is visible in the CI log.
        assert captured["kwargs"].get("verbose") is True


class TestValidateOnlineRetriesEveryNetworkCall:
    def test_repo_update_and_show_chart_go_through_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `helm repo update` used to bypass the retry helper via a bare _run;
        # prove every network call (repo add, repo update, show chart) now
        # rides the retry path. _render_chart is stubbed so we observe only the
        # resolve/repo commands here.
        retried: list[list[str]] = []

        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001
            retried.append(cmd)
            return (0, "apiVersion: v2\nname: keda\nversion: 2.20.1\n", "")

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)
        monkeypatch.setattr(validator, "_render_chart", lambda *a, **k: None)
        # If any call slipped past the retry helper to a bare _run, fail loudly.
        monkeypatch.setattr(
            validator,
            "_run",
            lambda *a, **k: pytest.fail("network call bypassed _run_with_retry"),
        )

        (ref,) = validator.build_refs({"keda": _classic()})
        errors = validator.validate_online([ref])
        assert errors == []

        joined = [" ".join(c) for c in retried]
        assert any(c.startswith("helm repo add kedacore") for c in joined)
        assert any(c == "helm repo update" for c in joined)
        assert any("show chart kedacore/keda" in c for c in joined)


# ---------------------------------------------------------------------------
# Gateway API / aws-load-balancer-controller lockstep
# ---------------------------------------------------------------------------

_HANDLER_FIXTURE = """
PINNED_GATEWAY_CRD_BUNDLES = (
    _PinnedManifestBundle(
        name="gateway-api-standard-v1.6.0",
        url="https://example.invalid/standard-install.yaml",
    ),
    _PinnedManifestBundle(
        name="aws-lbc-gateway-v3.5.0",
        url="https://example.invalid/gateway-crds.yaml",
    ),
)
"""

_GO_MOD_FIXTURE = """
module sigs.k8s.io/aws-load-balancer-controller/v3

go 1.24

require (
\tgithub.com/aws/aws-sdk-go-v2 v1.32.0
\tsigs.k8s.io/gateway-api v1.6.0
)
"""


def _lbc_charts(version: str = "3.5.0") -> dict:
    # Shaped like ``load_charts`` output: the unwrapped ``charts:`` mapping.
    return {
        "aws-load-balancer-controller": {
            "enabled": True,
            "repo_name": "eks",
            "repo_url": "https://aws.github.io/eks-charts",
            "chart": "aws-load-balancer-controller",
            "version": version,
            "namespace": "kube-system",
        }
    }


class TestGatewayLockstepParsers:
    def test_parses_both_bundle_versions(self) -> None:
        assert validator.parse_pinned_gateway_bundles(_HANDLER_FIXTURE) == (
            "1.6.0",
            "3.5.0",
        )

    def test_missing_bundles_parse_as_none(self) -> None:
        assert validator.parse_pinned_gateway_bundles("nothing here") == (None, None)

    def test_go_mod_requirement_extraction(self) -> None:
        assert validator.gateway_api_requirement_from_go_mod(_GO_MOD_FIXTURE) == "1.6.0"
        assert validator.gateway_api_requirement_from_go_mod("module x") is None

    def test_real_handler_and_charts_are_in_lockstep_offline(self) -> None:
        # The committed repository state must satisfy its own contract.
        charts = validator.load_charts(validator._DEFAULT_CHARTS)
        assert validator.validate_gateway_lockstep(charts, online=False) == []


class TestGatewayLockstepValidation:
    def test_matching_versions_pass_online(self) -> None:
        errors = validator.validate_gateway_lockstep(
            _lbc_charts(),
            handler_source=_HANDLER_FIXTURE,
            go_mod_fetcher=lambda version: _GO_MOD_FIXTURE,
            online=True,
        )
        assert errors == []

    def test_lbc_bundle_chart_skew_fails_offline(self) -> None:
        errors = validator.validate_gateway_lockstep(
            _lbc_charts(version="3.6.0"),
            handler_source=_HANDLER_FIXTURE,
            online=False,
        )
        assert len(errors) == 1
        assert "aws-lbc-gateway CRD bundle v3.5.0" in errors[0]
        assert "3.6.0" in errors[0]

    def test_stale_gateway_api_bundle_fails_online(self) -> None:
        # The 2026-08 incident shape: controller requires a newer gateway-api
        # than the pinned standard bundle provides.
        newer_go_mod = _GO_MOD_FIXTURE.replace("gateway-api v1.6.0", "gateway-api v1.7.0")
        errors = validator.validate_gateway_lockstep(
            _lbc_charts(),
            handler_source=_HANDLER_FIXTURE,
            go_mod_fetcher=lambda version: newer_go_mod,
            online=True,
        )
        assert len(errors) == 1
        assert "built against gateway-api v1.7.0" in errors[0]
        assert "v1.6.0" in errors[0]

    def test_newer_pinned_bundle_than_required_passes(self) -> None:
        older_go_mod = _GO_MOD_FIXTURE.replace("gateway-api v1.6.0", "gateway-api v1.5.1")
        errors = validator.validate_gateway_lockstep(
            _lbc_charts(),
            handler_source=_HANDLER_FIXTURE,
            go_mod_fetcher=lambda version: older_go_mod,
            online=True,
        )
        assert errors == []

    def test_offline_mode_never_calls_the_fetcher(self) -> None:
        errors = validator.validate_gateway_lockstep(
            _lbc_charts(),
            handler_source=_HANDLER_FIXTURE,
            go_mod_fetcher=lambda version: pytest.fail("offline must not fetch"),
            online=False,
        )
        assert errors == []

    def test_fetch_failure_is_reported_not_swallowed(self) -> None:
        def _boom(version: str) -> str:
            raise RuntimeError("could not fetch https://example.invalid/go.mod: timed out")

        errors = validator.validate_gateway_lockstep(
            _lbc_charts(),
            handler_source=_HANDLER_FIXTURE,
            go_mod_fetcher=_boom,
            online=True,
        )
        assert len(errors) == 1
        assert "could not fetch" in errors[0]

    def test_missing_chart_entry_is_an_error_for_the_real_charts_file(self) -> None:
        errors = validator.validate_gateway_lockstep({}, online=False, require_entry=True)
        assert len(errors) == 1
        assert "no 'aws-load-balancer-controller' entry" in errors[0]

    def test_missing_chart_entry_is_skipped_for_fixture_files(self) -> None:
        assert validator.validate_gateway_lockstep({}, online=False, require_entry=False) == []

    def test_renamed_handler_bundles_fail_loudly(self) -> None:
        errors = validator.validate_gateway_lockstep(
            _lbc_charts(),
            handler_source="PINNED_GATEWAY_CRD_BUNDLES = ()",
            online=False,
        )
        assert len(errors) == 1
        assert "no longer names" in errors[0]

    def test_go_mod_without_gateway_api_fails_loudly(self) -> None:
        errors = validator.validate_gateway_lockstep(
            _lbc_charts(),
            handler_source=_HANDLER_FIXTURE,
            go_mod_fetcher=lambda version: "module x\n",
            online=True,
        )
        assert len(errors) == 1
        assert "does not declare sigs.k8s.io/gateway-api" in errors[0]

    def test_go_mod_fetch_refuses_non_semver_versions(self) -> None:
        # The fetch URL interpolates the chart version; anything that is not
        # strict semver is refused before any network use.
        with pytest.raises(RuntimeError, match="non-semver controller version"):
            validator.fetch_lbc_go_mod("3.5.0/../../evil")


class TestSlinkySlurmValuesShape:
    """Pin the slinky-slurm values in the LIVE charts.yaml to the chart's schema.

    Helm merges unknown value keys silently, so a misspelled key deploys a
    half-configured cluster with no error anywhere. Both shapes below were
    caught live by the release-validation ``schedulers`` action (run
    sched241-6b8520b2-r2): a camelCase ``nodeSets`` list was ignored — the
    cluster came up with **zero slurmd workers** — and with no enabled
    ``partitions`` entry slurmctld had no default partition, so every
    partition-less submission (``sbatch --wrap``, the REST probe,
    ``examples/slurm-cluster-job.yaml``) failed with rc 2001.

    Chart schema reference: ``helm show values oci://ghcr.io/slinkyproject/charts/slurm``
    — ``nodesets`` is a lowercase MAP keyed by NodeSet name; ``partitions`` is
    a map whose entries carry ``enabled`` + ``configMap``.
    """

    @pytest.fixture(scope="class")
    def slinky_values(self) -> dict:
        import yaml

        charts = yaml.safe_load(LIVE_CHARTS.read_text(encoding="utf-8"))["charts"]
        return charts["slinky-slurm"]["values"]

    def test_no_camelcase_nodesets_key(self, slinky_values: dict) -> None:
        assert "nodeSets" not in slinky_values, (
            "slinky-slurm values use camelCase 'nodeSets' — the chart's key is "
            "lowercase 'nodesets'; Helm ignores the unknown key and deploys a "
            "Slurm cluster with zero workers"
        )

    def test_nodesets_is_nonempty_map_of_maps(self, slinky_values: dict) -> None:
        nodesets = slinky_values.get("nodesets")
        assert isinstance(nodesets, dict) and nodesets, (
            "slinky-slurm values must define 'nodesets' as a non-empty map "
            "keyed by NodeSet name (list shapes are silently ignored)"
        )
        for name, spec in nodesets.items():
            assert isinstance(spec, dict), f"nodesets.{name} must be a mapping"
            assert "name" not in spec, (
                f"nodesets.{name} carries a 'name' key — that's the list shape; "
                "the map key IS the NodeSet name"
            )

    def test_default_partition_exists_and_spans_nodesets(self, slinky_values: dict) -> None:
        partitions = slinky_values.get("partitions")
        assert isinstance(partitions, dict) and partitions, (
            "slinky-slurm values must define 'partitions' — without one, "
            "slurmctld has no default partition and every partition-less "
            "submission fails with rc 2001"
        )
        enabled = {
            name: spec
            for name, spec in partitions.items()
            if isinstance(spec, dict) and spec.get("enabled")
        }
        assert enabled, "at least one partitions entry must set enabled: true"
        defaults = [
            name
            for name, spec in enabled.items()
            if str((spec.get("configMap") or {}).get("Default", "")).upper() == "YES"
        ]
        assert defaults, (
            "exactly one enabled partition must carry configMap.Default: 'YES' so "
            "sbatch/REST submissions without an explicit partition are accepted"
        )
        assert len(defaults) == 1, f"multiple default partitions defined: {defaults}"


# ── bounded re-pass over failed charts (offline) ──────────────────────────────
#
# The inner per-command retry rides out blips shorter than one command's
# attempt window; these pin the outer layer: charts that still fail the first
# sweep are re-validated once more behind a fresh repo index, so a registry
# outage lasting minutes no longer forces a manual rerun of the job while a
# genuinely bad pin still fails every pass. All offline: _run and time.sleep
# are monkeypatched.


class TestValidateOnlineRepass:
    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator.time, "sleep", lambda *_a, **_k: None)

    @staticmethod
    def _ref(name: str = "keda") -> validator.ChartRef:
        return validator.ChartRef(
            name=name,
            chart=name,
            version="1.2.3",
            repo_name="repo",
            repo_url="https://example.invalid/charts",
            use_oci=False,
            namespace="default",
            enabled=True,
            values={},
        )

    def _fake_run(self, monkeypatch: pytest.MonkeyPatch, outcomes: dict) -> list[list[str]]:
        """Route validator._run by helm subcommand.

        ``outcomes["show"]`` is a list of (rc, out, err) consumed one per
        ``helm show chart`` call (last repeated when exhausted); repo
        add/update and template always succeed. Returns the recorded argv
        list for assertions.
        """
        calls: list[list[str]] = []
        show_seq = list(outcomes.get("show", [(0, "version: 1.2.3\n", "")]))
        shows = {"n": 0}

        def fake_run(cmd, env, *, timeout=120):  # noqa: ANN001
            calls.append(list(cmd))
            if cmd[1] == "show":
                result = show_seq[min(shows["n"], len(show_seq) - 1)]
                shows["n"] += 1
                return result
            return (0, "", "")

        monkeypatch.setattr(validator, "_run", fake_run)
        return calls

    def test_transient_failure_clears_on_the_second_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # First sweep: every inner attempt fails (4 attempts). Second sweep:
        # first attempt succeeds. The job must end green.
        failures = [(1, "", "registry blip")] * 4
        self._fake_run(monkeypatch, {"show": [*failures, (0, "version: 1.2.3\n", "")]})
        errors = validator.validate_online([self._ref()], skip_template=True, repass_delay=0.0)
        assert errors == []

    def test_persistent_failure_survives_every_pass_and_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._fake_run(monkeypatch, {"show": [(1, "", "no such version")]})
        errors = validator.validate_online([self._ref()], skip_template=True, repass_delay=0.0)
        assert len(errors) == 1
        assert "cannot resolve" in errors[0]
        # Two sweeps x four inner attempts.
        assert sum(1 for c in calls if c[1] == "show") == 8

    def test_repass_refreshes_the_repo_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failures = [(1, "", "blip")] * 4
        calls = self._fake_run(monkeypatch, {"show": [*failures, (0, "version: 1.2.3\n", "")]})
        validator.validate_online([self._ref()], skip_template=True, repass_delay=0.0)
        repo_adds = [c for c in calls if c[1] == "repo" and c[2] == "add"]
        repo_updates = [c for c in calls if c[1] == "repo" and c[2] == "update"]
        assert len(repo_adds) == 2, "re-pass must re-add repos behind a fresh index"
        assert len(repo_updates) == 2

    def test_only_failed_charts_are_revalidated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        good = self._ref("good-chart")
        bad = self._ref("bad-chart")
        calls: list[list[str]] = []

        def fake_run(cmd, env, *, timeout=120):  # noqa: ANN001
            calls.append(list(cmd))
            if cmd[1] == "show":
                if "bad-chart" in cmd[3]:
                    return (1, "", "still broken")
                return (0, "version: 1.2.3\n", "")
            return (0, "", "")

        monkeypatch.setattr(validator, "_run", fake_run)
        errors = validator.validate_online([good, bad], skip_template=True, repass_delay=0.0)
        assert len(errors) == 1 and "bad-chart" in errors[0]
        good_shows = [c for c in calls if c[1] == "show" and "good-chart" in c[3]]
        assert len(good_shows) == 1, "a chart that passed must not be re-fetched"

    def test_single_pass_configuration_disables_the_repass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._fake_run(monkeypatch, {"show": [(1, "", "boom")]})
        errors = validator.validate_online(
            [self._ref()], skip_template=True, passes=1, repass_delay=0.0
        )
        assert errors
        assert sum(1 for c in calls if c[1] == "show") == 4


# ---------------------------------------------------------------------------
# Kubeflow Trainer runtime / example / docs lockstep
# ---------------------------------------------------------------------------

_TRAINER_IMAGE = "pytorch/pytorch:9.9.9-cuda99.9-cudnn9-runtime"

_RUNTIME_MANIFEST_FIXTURE = f"""
apiVersion: trainer.kubeflow.org/v1alpha1
kind: ClusterTrainingRuntime
metadata:
  name: torch-distributed
  labels:
    trainer.kubeflow.org/framework: torch
    trainer.kubeflow.org/webhook-validation: disabled
    project: gco
spec:
  mlPolicy:
    numNodes: 1
    torch: {{}}
  template:
    spec:
      replicatedJobs:
        - name: node
          template:
            spec:
              template:
                spec:
                  automountServiceAccountToken: false
                  containers:
                    - name: node
                      image: {_TRAINER_IMAGE}
                      securityContext:
                        allowPrivilegeEscalation: false
"""

_EXAMPLE_FIXTURE = f"""
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainJob
metadata:
  name: kubeflow-trainjob-example
spec:
  runtimeRef:
    name: torch-distributed
  trainer:
    numNodes: 2
    image: {_TRAINER_IMAGE}
"""

_DOC_FIXTURE = f"""
# Distributed Training

```yaml
  trainer:
    image: {_TRAINER_IMAGE}
```
"""


def _upstream_runtime(image: str = _TRAINER_IMAGE) -> dict:
    """The runtime as the chart ships it: helm labels, no automount override."""
    return {
        "apiVersion": "trainer.kubeflow.org/v1alpha1",
        "kind": "ClusterTrainingRuntime",
        "metadata": {
            "name": "torch-distributed",
            "labels": {
                "trainer.kubeflow.org/framework": "torch",
                "trainer.kubeflow.org/webhook-validation": "disabled",
                "helm.sh/chart": "kubeflow-trainer-9.9.9",
                "app.kubernetes.io/managed-by": "Helm",
            },
        },
        "spec": {
            "mlPolicy": {"numNodes": 1, "torch": {}},
            "template": {
                "spec": {
                    "replicatedJobs": [
                        {
                            "name": "node",
                            "template": {
                                "spec": {
                                    "template": {
                                        "spec": {"containers": [{"name": "node", "image": image}]}
                                    }
                                }
                            },
                        }
                    ]
                }
            },
        },
    }


def _trainer_charts(version: str = "9.9.9") -> dict:
    return {
        "kubeflow-trainer": {
            "enabled": True,
            "repo_url": "oci://ghcr.io/kubeflow/charts",
            "chart": "kubeflow-trainer",
            "version": version,
            "namespace": "kubeflow-trainer",
            "use_oci": True,
        }
    }


def _lockstep(charts: dict | None = None, **overrides) -> list[str]:
    kwargs = {
        "manifest_text": _RUNTIME_MANIFEST_FIXTURE,
        "example_text": _EXAMPLE_FIXTURE,
        "doc_text": _DOC_FIXTURE,
        "online": False,
    }
    kwargs.update(overrides)
    return validator.validate_trainer_runtime_lockstep(
        _trainer_charts() if charts is None else charts, **kwargs
    )


class TestTrainerLockstepParsers:
    def test_parses_the_shipped_runtime(self) -> None:
        runtime = validator.parse_shipped_torch_runtime(_RUNTIME_MANIFEST_FIXTURE)
        assert runtime is not None
        assert validator.trainer_node_image(runtime) == _TRAINER_IMAGE

    def test_zero_or_many_runtimes_parse_as_none(self) -> None:
        assert validator.parse_shipped_torch_runtime("kind: ConfigMap") is None
        doubled = _RUNTIME_MANIFEST_FIXTURE + "\n---\n" + _RUNTIME_MANIFEST_FIXTURE
        assert validator.parse_shipped_torch_runtime(doubled) is None

    def test_upstream_runtime_extracted_from_installer_configmap(self) -> None:
        payload = yaml.safe_dump(_upstream_runtime())
        render = yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "kubeflow-trainer-runtimes-installer"},
                "data": {"runtimes.yaml": payload},
            }
        )
        runtime = validator.upstream_torch_runtime_from_render(render)
        assert runtime is not None
        assert validator.trainer_node_image(runtime) == _TRAINER_IMAGE

    def test_render_without_installer_configmap_is_none(self) -> None:
        assert validator.upstream_torch_runtime_from_render("kind: Deployment") is None

    def test_trainer_node_image_requires_the_node_container(self) -> None:
        runtime = _upstream_runtime()
        (job,) = runtime["spec"]["template"]["spec"]["replicatedJobs"]
        job["template"]["spec"]["template"]["spec"]["containers"][0]["name"] = "sidecar"
        assert validator.trainer_node_image(runtime) is None

    def test_example_trainer_image(self) -> None:
        assert validator.example_trainer_image(_EXAMPLE_FIXTURE) == _TRAINER_IMAGE
        assert validator.example_trainer_image("kind: Job") is None

    def test_doc_regex_binds_only_pytorch_org_images(self) -> None:
        text = (
            "image: pytorch/pytorch:1.0.0-runtime\n"
            "image: nvcr.io/nvidia/pytorch:24.01\n"
            "    image: pytorch/pytorch:2.0.0-runtime\n"
        )
        assert validator.doc_pytorch_images(text) == [
            "pytorch/pytorch:1.0.0-runtime",
            "pytorch/pytorch:2.0.0-runtime",
        ]

    def test_real_repo_is_in_lockstep_offline(self) -> None:
        # The committed manifest, example and doc must satisfy their own
        # contract — this is the always-on (network-free) half of the gate.
        charts = validator.load_charts(validator._DEFAULT_CHARTS)
        assert validator.validate_trainer_runtime_lockstep(charts, online=False) == []


class TestTrainerLockstepValidation:
    def test_everything_in_sync_passes_offline(self) -> None:
        assert _lockstep() == []

    def test_example_drift_fails_offline(self) -> None:
        stale = _EXAMPLE_FIXTURE.replace(_TRAINER_IMAGE, "pytorch/pytorch:8.8.8-runtime")
        errors = _lockstep(example_text=stale)
        assert len(errors) == 1
        assert "examples/kubeflow-trainjob.yaml" in errors[0]
        assert "pytorch/pytorch:8.8.8-runtime" in errors[0]
        assert _TRAINER_IMAGE in errors[0]

    def test_doc_drift_fails_offline(self) -> None:
        stale = _DOC_FIXTURE.replace(_TRAINER_IMAGE, "pytorch/pytorch:8.8.8-runtime")
        errors = _lockstep(doc_text=stale)
        assert len(errors) == 1
        assert "DISTRIBUTED_TRAINING.md" in errors[0]

    def test_doc_without_pytorch_mentions_is_unconstrained(self) -> None:
        assert _lockstep(doc_text="# rewritten doc, prose only") == []

    def test_manifest_without_the_runtime_fails_loudly(self) -> None:
        errors = _lockstep(manifest_text="kind: ConfigMap")
        assert len(errors) == 1
        assert "update this check" in errors[0]

    def test_missing_chart_entry_is_an_error_for_the_real_charts_file(self) -> None:
        errors = _lockstep(charts={}, require_entry=True)
        assert len(errors) == 1
        assert "no 'kubeflow-trainer' entry" in errors[0]

    def test_missing_chart_entry_is_skipped_for_fixture_files(self) -> None:
        assert _lockstep(charts={}, require_entry=False) == []

    def test_offline_mode_never_calls_the_fetcher(self) -> None:
        errors = _lockstep(
            runtime_fetcher=lambda entry, helm: pytest.fail("offline must not render"),
        )
        assert errors == []

    def test_matching_upstream_passes_online(self) -> None:
        # The upstream fixture has neither the automount override nor the
        # NoNewPrivs securityContext — proving both documented deviations
        # are tolerated by the spec comparison.
        errors = _lockstep(
            online=True,
            runtime_fetcher=lambda entry, helm: _upstream_runtime(),
        )
        assert errors == []

    def test_upstream_image_drift_fails_online(self) -> None:
        errors = _lockstep(
            online=True,
            runtime_fetcher=lambda entry, helm: _upstream_runtime(
                image="pytorch/pytorch:8.8.8-runtime"
            ),
        )
        image_errors = [e for e in errors if "re-extract the runtime" in e]
        assert len(image_errors) == 1
        assert "pytorch/pytorch:8.8.8-runtime" in image_errors[0]
        assert _TRAINER_IMAGE in image_errors[0]

    def test_non_image_spec_drift_fails_online(self) -> None:
        upstream = _upstream_runtime()
        upstream["spec"]["mlPolicy"]["torch"] = {"numProcPerNode": "2"}
        errors = _lockstep(online=True, runtime_fetcher=lambda entry, helm: upstream)
        assert len(errors) == 1
        assert "differs from what chart" in errors[0]
        assert "_apply_documented_runtime_deviations" in errors[0]

    def test_semantic_label_drift_fails_online(self) -> None:
        shipped = _RUNTIME_MANIFEST_FIXTURE.replace(
            "trainer.kubeflow.org/webhook-validation: disabled",
            "trainer.kubeflow.org/webhook-validation: enabled",
        )
        errors = _lockstep(
            manifest_text=shipped,
            online=True,
            runtime_fetcher=lambda entry, helm: _upstream_runtime(),
        )
        assert len(errors) == 1
        assert "webhook-validation" in errors[0]

    def test_render_failure_is_reported_not_swallowed(self) -> None:
        def _boom(entry: dict, helm: str) -> dict:
            raise RuntimeError("helm template oci://ghcr.io/... failed: registry down")

        errors = _lockstep(online=True, runtime_fetcher=_boom)
        assert len(errors) == 1
        assert "registry down" in errors[0]


# ── Remaining branches ────────────────────────────────────────────────────────
#
# Everything below closes a specific branch the suites above route around.
# Grouped by the function they land in; ``validator._run`` is faked the same
# way TestValidateOnlineRepass does, so helm is never actually invoked.


def _fake_ref(name: str = "keda", *, use_oci: bool = False, values: dict | None = None):
    return validator.ChartRef(
        name=name,
        chart=name,
        version="1.2.3",
        repo_name="repo",
        repo_url="oci://example.invalid/charts" if use_oci else "https://example.invalid/charts",
        use_oci=use_oci,
        namespace="default",
        enabled=True,
        values=values or {},
    )


class TestBuildRefsSkips:
    def test_a_non_mapping_entry_is_skipped(self) -> None:
        """``validate_structure`` already reports it; no point asking Helm."""
        assert validator.build_refs({"broken": "not-a-mapping", "keda": _classic()}) != []
        assert [ref.name for ref in validator.build_refs({"broken": "not-a-mapping"})] == []

    @pytest.mark.parametrize("field", ["chart", "repo_url"])
    def test_an_entry_missing_a_reference_field_is_skipped(self, field: str) -> None:
        entry = _classic()
        entry[field] = "   "
        assert validator.build_refs({"keda": entry}) == []


class TestRunSubprocessBoundary:
    def test_run_returns_the_process_streams(self) -> None:
        rc, out, err = validator._run(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            dict(os.environ),
        )
        assert (rc, out.strip(), err.strip()) == (0, "out", "err")

    def test_run_maps_a_timeout_to_the_uniform_failure_contract(self) -> None:
        """Callers get ``(-1, "", "timeout: ...")``, never an exception."""
        rc, out, err = validator._run(
            [sys.executable, "-c", "import time; time.sleep(5)"], dict(os.environ), timeout=1
        )
        assert rc == -1
        assert out == ""
        assert err.startswith("timeout: command exceeded")


class TestRunWithRetryVerbose:
    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator.time, "sleep", lambda *_a, **_k: None)

    def test_verbose_narrates_each_failed_attempt_with_the_description(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(validator, "_run", lambda cmd, env, *, timeout=120: (1, "", "boom"))

        validator._run_with_retry(
            ["helm", "x"], {}, attempts=3, verbose=True, description="helm show chart keda"
        )

        out = capsys.readouterr().out
        assert out.count("failed, retrying in") == 2, "the last attempt is not retried"
        assert "helm show chart keda: boom" in out

    def test_verbose_falls_back_to_the_argv_when_undescribed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(validator, "_run", lambda cmd, env, *, timeout=120: (1, "", "boom"))

        validator._run_with_retry(["helm", "repo", "update"], {}, attempts=2, verbose=True)

        assert "helm repo update: boom" in capsys.readouterr().out


class TestRenderChartValuesFile:
    def test_values_are_written_to_a_temp_file_and_removed_afterwards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The values block reaches helm as ``--values <file>`` and never lingers."""
        seen: dict[str, object] = {}

        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001, ANN202
            index = cmd.index("--values")
            values_path = Path(cmd[index + 1])
            seen["path"] = values_path
            seen["content"] = yaml.safe_load(values_path.read_text(encoding="utf-8"))
            return (0, "", "")

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)

        error = validator._render_chart(
            _fake_ref(values={"replicaCount": 3}), "repo/keda", "helm", {}
        )

        assert error is None
        assert seen["content"] == {"replicaCount": 3}
        assert not Path(seen["path"]).exists(), "the temporary values file leaked"

    def test_a_values_block_that_cannot_be_serialised_surfaces_the_real_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If ``yaml.safe_dump`` raises, *that* error propagates and no file leaks.

        Regression: the handler used to ``os.close(fd)`` after ``fdopen`` had
        already closed it, which raised ``EBADF`` in place of the real exception
        and left the half-written values file behind.
        """

        def explode(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
            raise TypeError("unserialisable")

        monkeypatch.setattr(validator.yaml, "safe_dump", explode)
        monkeypatch.setattr(validator.tempfile, "tempdir", str(tmp_path))

        with pytest.raises(TypeError, match="unserialisable"):
            validator._render_chart(_fake_ref(values={"k": object()}), "repo/keda", "helm", {})

        assert list(tmp_path.iterdir()) == [], "the temporary values file leaked"

    def test_a_render_failure_is_returned_with_the_error_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            validator, "_run_with_retry", lambda cmd, env, **k: (1, "", "template: bad value")
        )

        error = validator._render_chart(_fake_ref(), "repo/keda", "helm", {})

        assert error == "chart failed to render (helm template): template: bad value"


class TestSyncClassicReposFailures:
    def test_a_failing_repo_add_is_reported_and_the_index_still_refreshed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001, ANN202
            calls.append(list(cmd))
            if cmd[1:3] == ["repo", "add"]:
                return (
                    1,
                    "",
                    "Error: looks like https://example.invalid is not a valid chart repository",
                )
            return (0, "", "")

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)

        errors = validator._sync_classic_repos({"repo": "https://example.invalid"}, "helm", {})

        assert errors == [
            "helm repo add repo (https://example.invalid) failed: Error: looks like "
            "https://example.invalid is not a valid chart repository"
        ]
        assert ["helm", "repo", "update"] in calls

    def test_no_classic_repos_means_no_index_refresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An all-OCI charts file must not run ``helm repo update`` against nothing."""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            validator,
            "_run_with_retry",
            lambda cmd, env, **k: (calls.append(list(cmd)), (0, "", ""))[1],
        )

        assert validator._sync_classic_repos({}, "helm", {}) == []
        assert calls == []


class TestValidateRefsVerbose:
    """The verbose narration for each resolve/render outcome."""

    def _route(self, monkeypatch: pytest.MonkeyPatch, *, show: tuple, template: tuple) -> None:
        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001, ANN202
            return show if cmd[1] == "show" else template

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)

    def test_resolve_failure_is_narrated(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._route(monkeypatch, show=(1, "", "not found"), template=(0, "", ""))

        failures = validator._validate_refs([_fake_ref()], "helm", {}, verbose=True)

        assert "keda" in failures
        assert "FAIL  resolve  keda 1.2.3" in capsys.readouterr().out

    def test_version_disagreement_is_a_failure_even_when_resolve_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Helm silently serving a different version is the classic pin-drift."""
        self._route(monkeypatch, show=(0, "version: 9.9.9\n", ""), template=(0, "", ""))

        failures = validator._validate_refs([_fake_ref()], "helm", {}, skip_template=True)

        assert failures == {"keda": ["keda: requested version '1.2.3' but helm resolved '9.9.9'"]}

    def test_render_success_and_failure_are_both_narrated(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._route(monkeypatch, show=(0, "version: 1.2.3\n", ""), template=(0, "", ""))
        assert validator._validate_refs([_fake_ref()], "helm", {}, verbose=True) == {}
        assert "ok    render   keda 1.2.3" in capsys.readouterr().out

        self._route(monkeypatch, show=(0, "version: 1.2.3\n", ""), template=(1, "", "nope"))
        failures = validator._validate_refs([_fake_ref()], "helm", {}, verbose=True)
        assert "keda" in failures
        assert "FAIL  render   keda 1.2.3" in capsys.readouterr().out


class TestValidateOnlineEdges:
    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator.time, "sleep", lambda *_a, **_k: None)

    def test_no_refs_is_an_immediate_empty_result(self) -> None:
        """Nothing to validate must not spin up a Helm home or touch the network."""
        assert validator.validate_online([]) == []

    def test_a_verbose_repass_announces_itself(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shows = {"n": 0}

        # Faked at the *retry* boundary: faking `_run` would let the inner
        # per-command retry absorb the blip, and the outer re-pass -- the thing
        # under test -- would never trigger.
        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001, ANN202
            if cmd[1] == "show":
                shows["n"] += 1
                return (1, "", "blip") if shows["n"] == 1 else (0, "version: 1.2.3\n", "")
            return (0, "", "")

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)

        errors = validator.validate_online(
            [_fake_ref()], skip_template=True, verbose=True, passes=2, repass_delay=0
        )

        assert errors == []
        assert "re-pass 2/2: retrying 1 failed chart(s)" in capsys.readouterr().out


class TestGoModFetch:
    """``fetch_lbc_go_mod`` against a stubbed ``urlopen`` -- never the network."""

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator.time, "sleep", lambda *_a, **_k: None)

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def test_returns_the_decoded_go_mod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        seen: dict[str, object] = {}

        def fake_urlopen(url, timeout=None):  # noqa: ANN001, ANN202
            seen["url"] = url
            return self._Response(b"module x\n\nrequire sigs.k8s.io/gateway-api v1.5.0\n")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        text = validator.fetch_lbc_go_mod("3.5.0")

        assert "gateway-api v1.5.0" in text
        assert seen["url"] == (
            "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v3.5.0/go.mod"
        )

    def test_retries_transient_failures_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.error
        import urllib.request

        attempts = {"n": 0}

        def flaky(url, timeout=None):  # noqa: ANN001, ANN202
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib.error.URLError("reset")
            return self._Response(b"module x\n")

        monkeypatch.setattr(urllib.request, "urlopen", flaky)

        assert validator.fetch_lbc_go_mod("3.5.0") == "module x\n"
        assert attempts["n"] == 3

    def test_gives_up_after_the_attempt_budget_with_the_last_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.request

        def down(url, timeout=None):  # noqa: ANN001, ANN202
            raise TimeoutError("timed out")

        monkeypatch.setattr(urllib.request, "urlopen", down)

        with pytest.raises(RuntimeError, match=r"could not fetch .*go\.mod: timed out"):
            validator.fetch_lbc_go_mod("3.5.0")


class TestGatewayLockstepFileFallbacks:
    def test_an_unreadable_handler_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(validator, "_HANDLER_PATH", tmp_path / "missing" / "handler.py")

        errors = validator.validate_gateway_lockstep(
            {"aws-load-balancer-controller": _classic(version="3.5.0")}
        )

        assert len(errors) == 1
        assert errors[0].startswith("gateway lockstep: cannot read")


class TestTrainerLockstepParserEdges:
    def test_shipped_runtime_parser_returns_none_for_invalid_yaml(self) -> None:
        assert validator.parse_shipped_torch_runtime("kind: [unterminated") is None

    def test_upstream_render_parser_returns_none_for_invalid_yaml(self) -> None:
        assert validator.upstream_torch_runtime_from_render("kind: [unterminated") is None

    def test_upstream_render_parser_skips_configmaps_that_are_not_the_installer(self) -> None:
        render = (
            "kind: ConfigMap\nmetadata:\n  name: other-config\ndata:\n  runtimes.yaml: 'x'\n"
            "---\nkind: ConfigMap\nmetadata:\n  name: trainer-runtimes-installer\ndata:\n"
            "  notes.txt: 'no runtimes key'\n"
        )
        assert validator.upstream_torch_runtime_from_render(render) is None

    def test_upstream_render_parser_returns_none_for_an_unparseable_payload(self) -> None:
        render = (
            "kind: ConfigMap\nmetadata:\n  name: trainer-runtimes-installer\ndata:\n"
            "  runtimes.yaml: 'kind: [unterminated'\n"
        )
        assert validator.upstream_torch_runtime_from_render(render) is None

    def test_upstream_render_parser_skips_a_payload_without_the_runtime(self) -> None:
        render = (
            "kind: ConfigMap\nmetadata:\n  name: trainer-runtimes-installer\ndata:\n"
            "  runtimes.yaml: |\n    kind: ClusterTrainingRuntime\n    metadata:\n"
            "      name: some-other-runtime\n"
        )
        assert validator.upstream_torch_runtime_from_render(render) is None

    @pytest.mark.parametrize(
        "runtime",
        [
            pytest.param(
                {"spec": {"template": {"spec": {"replicatedJobs": "not-a-list"}}}},
                id="jobs-not-a-list",
            ),
            pytest.param(
                {
                    "spec": {
                        "template": {
                            "spec": {"replicatedJobs": ["not-a-mapping", {"name": "other"}]}
                        }
                    }
                },
                id="no-node-job",
            ),
            pytest.param(
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "replicatedJobs": [
                                    {
                                        "name": "node",
                                        "template": {
                                            "spec": {"template": {"spec": {"containers": "nope"}}}
                                        },
                                    }
                                ]
                            }
                        }
                    }
                },
                id="containers-not-a-list",
            ),
            pytest.param(
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "replicatedJobs": [
                                    {
                                        "name": "node",
                                        "template": {
                                            "spec": {
                                                "template": {
                                                    "spec": {
                                                        "containers": [
                                                            "not-a-mapping",
                                                            {"name": "sidecar", "image": "x"},
                                                        ]
                                                    }
                                                }
                                            }
                                        },
                                    }
                                ]
                            }
                        }
                    }
                },
                id="no-node-container",
            ),
            pytest.param(
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "replicatedJobs": [
                                    {
                                        "name": "node",
                                        "template": {
                                            "spec": {
                                                "template": {
                                                    "spec": {
                                                        "containers": [
                                                            {"name": "node", "image": 42}
                                                        ]
                                                    }
                                                }
                                            }
                                        },
                                    }
                                ]
                            }
                        }
                    }
                },
                id="image-not-a-string",
            ),
        ],
    )
    def test_trainer_node_image_returns_none_for_every_malformed_shape(self, runtime: dict) -> None:
        assert validator.trainer_node_image(runtime) is None

    def test_example_trainer_image_edges(self) -> None:
        assert validator.example_trainer_image("kind: [unterminated") is None
        assert (
            validator.example_trainer_image("kind: TrainJob\nspec:\n  trainer:\n    image: 7\n")
            is None
        )
        assert validator.example_trainer_image("kind: Job\n") is None

    def test_documented_deviations_skip_malformed_node_jobs(self) -> None:
        """A shape the deviation pass cannot navigate is left untouched, not crashed on."""
        spec = {
            "template": {
                "spec": {
                    "replicatedJobs": [
                        "not-a-mapping",
                        {"name": "other"},
                        {
                            "name": "node",
                            "template": {"spec": {"template": {"spec": "not-a-mapping"}}},
                        },
                        {
                            "name": "node",
                            "template": {
                                "spec": {
                                    "template": {
                                        "spec": {
                                            "containers": [
                                                "not-a-mapping",
                                                {"name": "sidecar"},
                                                {
                                                    "name": "node",
                                                    "securityContext": "not-a-mapping",
                                                },
                                            ]
                                        }
                                    }
                                }
                            },
                        },
                    ]
                }
            }
        }

        adjusted = validator._apply_documented_runtime_deviations(spec)

        node_pod = adjusted["template"]["spec"]["replicatedJobs"][3]["template"]["spec"][
            "template"
        ]["spec"]
        assert node_pod["automountServiceAccountToken"] is False
        # A non-mapping securityContext is left alone rather than overwritten.
        assert node_pod["containers"][2]["securityContext"] == "not-a-mapping"
        assert adjusted["template"]["spec"]["replicatedJobs"][0] == "not-a-mapping"

    def test_documented_deviations_tolerate_a_non_list_jobs_field(self) -> None:
        spec = {"template": {"spec": {"replicatedJobs": "nope"}}}
        assert validator._apply_documented_runtime_deviations(spec) == spec


class TestFetchUpstreamTorchRuntime:
    """The render step of the online trainer lockstep, with helm faked."""

    _RENDER = (
        "kind: ConfigMap\nmetadata:\n  name: trainer-runtimes-installer\ndata:\n"
        "  runtimes.yaml: |\n    kind: ClusterTrainingRuntime\n    metadata:\n"
        "      name: torch-distributed\n    spec: {}\n"
    )

    def test_an_unbuildable_entry_is_refused(self) -> None:
        with pytest.raises(RuntimeError, match="cannot build a Helm reference"):
            validator.fetch_upstream_torch_runtime({"chart": ""})

    def test_a_classic_entry_syncs_its_repo_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001, ANN202
            calls.append(list(cmd))
            return (0, self._RENDER, "")

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)

        runtime = validator.fetch_upstream_torch_runtime(_classic(chart="kubeflow-trainer"))

        assert runtime["metadata"]["name"] == "torch-distributed"
        assert calls[0][1:3] == ["repo", "add"], "classic repos are added before templating"
        assert any("runtimes.torchDistributed.enabled=true" in call for call in calls)

    def test_an_oci_entry_skips_the_repo_sync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            validator,
            "_run_with_retry",
            lambda cmd, env, **k: (calls.append(list(cmd)), (0, self._RENDER, ""))[1],
        )

        validator.fetch_upstream_torch_runtime(_oci(chart="kubeflow-trainer"))

        assert all(call[1] == "template" for call in calls)

    def test_a_render_failure_is_a_runtime_error_with_the_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            validator, "_run_with_retry", lambda cmd, env, **k: (1, "", "registry 503")
        )

        with pytest.raises(RuntimeError, match=r"helm template .* failed: registry 503"):
            validator.fetch_upstream_torch_runtime(_oci(chart="kubeflow-trainer"))

    def test_a_render_without_the_runtime_is_an_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            validator, "_run_with_retry", lambda cmd, env, **k: (0, "kind: Service\n", "")
        )

        with pytest.raises(RuntimeError, match="no longer ships a 'runtimes-installer' ConfigMap"):
            validator.fetch_upstream_torch_runtime(_oci(chart="kubeflow-trainer"))


class TestTrainerLockstepFileFallbacks:
    @pytest.mark.parametrize(
        "attribute", ["_TRAINER_RUNTIME_MANIFEST", "_TRAINJOB_EXAMPLE", "_DISTRIBUTED_TRAINING_DOC"]
    )
    def test_an_unreadable_input_file_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, attribute: str
    ) -> None:
        monkeypatch.setattr(validator, attribute, tmp_path / "missing")

        errors = validator.validate_trainer_runtime_lockstep(
            {"kubeflow-trainer": _oci(chart="kubeflow-trainer")}
        )

        assert len(errors) == 1
        assert errors[0].startswith("trainer runtime lockstep: cannot read")

    def test_a_shipped_runtime_without_a_node_image_fails_loudly(self) -> None:
        manifest = (
            "kind: ClusterTrainingRuntime\nmetadata:\n  name: torch-distributed\n"
            "spec:\n  template:\n    spec:\n      replicatedJobs: []\n"
        )

        errors = validator.validate_trainer_runtime_lockstep(
            {"kubeflow-trainer": _oci(chart="kubeflow-trainer")},
            manifest_text=manifest,
            example_text="kind: TrainJob\n",
            doc_text="",
        )

        assert errors == [
            "trainer runtime lockstep: the shipped torch-distributed runtime has no "
            "containers[name=node] image; update this check alongside any restructure"
        ]


class TestMainRemainingPaths:
    def test_an_unparseable_charts_file_is_exit_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text("charts: [unterminated", encoding="utf-8")

        assert validator.main(["--charts", str(charts)]) == 2
        assert "could not parse" in capsys.readouterr().err

    def test_online_mode_runs_the_resolve_pass_and_reports_the_rendered_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The full online path, with helm faked at the retry boundary."""
        charts = tmp_path / "charts.yaml"
        charts.write_text(yaml.safe_dump({"charts": {"kueue": _oci()}}), encoding="utf-8")
        monkeypatch.setattr(validator.shutil, "which", lambda name: "/usr/bin/helm")
        monkeypatch.setattr(validator.time, "sleep", lambda *_a, **_k: None)

        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001, ANN202
            if cmd[1] == "show":
                return (0, "version: 0.18.2\n", "")
            return (0, "", "")

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)

        rc = validator.main(["--charts", str(charts), "--mode", "online"])

        assert rc == 0
        assert "resolvable + rendered at their pinned versions" in capsys.readouterr().out

    def test_online_skip_template_reports_resolve_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text(yaml.safe_dump({"charts": {"kueue": _oci()}}), encoding="utf-8")
        monkeypatch.setattr(validator.shutil, "which", lambda name: "/usr/bin/helm")
        monkeypatch.setattr(
            validator, "_run_with_retry", lambda cmd, env, **k: (0, "version: 0.18.2\n", "")
        )

        rc = validator.main(["--charts", str(charts), "--mode", "online", "--skip-template"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "resolvable at their pinned versions" in out
        assert "rendered" not in out

    def test_enabled_only_is_reflected_in_the_summary_scope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        charts = tmp_path / "charts.yaml"
        charts.write_text(
            yaml.safe_dump({"charts": {"kueue": _oci(), "off": _oci(enabled=False)}}),
            encoding="utf-8",
        )

        rc = validator.main(["--charts", str(charts), "--mode", "offline", "--enabled-only"])

        assert rc == 0
        assert "OK: 1 Helm chart(s) (enabled) are structurally valid." in capsys.readouterr().out


class TestLastBranches:
    def test_show_output_with_a_non_string_version_is_none(self) -> None:
        """``version: 1.2`` parses as a float; only a string is a version."""
        assert validator._chart_version_from_show("version: 1.2\n") is None
        assert validator._chart_version_from_show("name: x\n") is None
        assert validator._chart_version_from_show("- not\n- a\n- mapping\n") is None

    def test_a_quiet_render_failure_is_still_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --verbose nothing is printed, but the failure must not be lost."""

        def fake_retry(cmd, env, **kwargs):  # noqa: ANN001, ANN202
            return (0, "version: 1.2.3\n", "") if cmd[1] == "show" else (1, "", "nope")

        monkeypatch.setattr(validator, "_run_with_retry", fake_retry)

        failures = validator._validate_refs([_fake_ref()], "helm", {}, verbose=False)

        assert failures == {"keda": ["keda: chart failed to render (helm template): nope"]}

    def test_auto_mode_goes_online_when_helm_is_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``auto`` is the developer default: use helm if it is there, quietly."""
        charts = tmp_path / "charts.yaml"
        charts.write_text(yaml.safe_dump({"charts": {"kueue": _oci()}}), encoding="utf-8")
        monkeypatch.setattr(validator.shutil, "which", lambda name: "/usr/bin/helm")
        monkeypatch.setattr(
            validator, "_run_with_retry", lambda cmd, env, **k: (0, "version: 0.18.2\n", "")
        )

        rc = validator.main(["--charts", str(charts), "--mode", "auto"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "resolvable" in out
        assert "not found on PATH" not in out
