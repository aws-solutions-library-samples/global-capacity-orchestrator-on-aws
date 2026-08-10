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
