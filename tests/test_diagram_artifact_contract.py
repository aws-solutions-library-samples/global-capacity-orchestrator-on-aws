"""Structural contracts for committed code and infrastructure diagrams."""

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

import diagrams.generate as diagrams_generate
from diagrams.code_diagrams import generate as generate_mod
from diagrams.code_diagrams._source_marker import SENTINEL
from diagrams.code_diagrams._targets import TARGETS
from diagrams.generate import (
    _marker_pointer_issues,
    _shared_source_copy_issues,
    check_diagram_contract,
)
from diagrams.infra_diagrams._catalog import INFRA_DIAGRAM_NAMES
from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS

ROOT = Path(__file__).resolve().parents[1]

_REQUIRED_HIGH_VALUE_TARGETS = {
    ("lambda/capacity-poller/handler.py", "lambda_handler"),
    ("lambda/helm-orchestrator/handler.py", "on_event"),
    ("lambda/traffic-dial-controller/handler.py", "lambda_handler"),
    ("gco/services/spot_price_gate.py", "SpotPriceGate.evaluate"),
    ("cli/commands/autopilot_cmd.py", "_plan"),
    ("gco_mcp/mission/swarm_runner.py", "SwarmRunner.run_to_completion"),
    (
        "gco/services/request_size_middleware.py",
        "RequestSizeLimitMiddleware.__call__",
    ),
    ("gco/services/webhook_dispatcher.py", "WebhookDispatcher._deliver_webhook"),
    ("gco/services/mooncake_pd_proxy.py", "_dispatch"),
    ("gco/services/health_monitor.py", "HealthMonitor.get_health_status"),
    (
        "gco/services/inference_monitor.py",
        "InferenceMonitor._reconcile_endpoint_authorized",
    ),
}


def test_committed_diagram_catalogues_are_structurally_current() -> None:
    """Targets, artifacts, indexes, per-source stamps, and markers agree.

    Every issue string names the offending file and how to fix it, so a CI
    reader never has to open the generator to know what to regenerate.
    """
    issues = check_diagram_contract(ROOT)
    assert not issues, "committed diagram catalogue is stale:\n  - " + "\n  - ".join(issues)


def test_code_only_check_has_no_cdk_or_site_package_dependency() -> None:
    result = subprocess.run(
        [sys.executable, "-S", "diagrams/generate.py", "--check", "--code-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Diagram artifact contract is current"


def test_source_marker_contract_rejects_wrong_or_missing_artifact_pointers() -> None:
    source = """# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-08-30T12:00:00Z
# Generated from Git commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
# Flowchart(s) generated from this file:
#   * ``Thing.run`` -> ``diagrams/code_diagrams/thing.Thing_run.html``
#     (PNG: ``diagrams/code_diagrams/thing.Thing_run.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END
"""
    expected = {
        (
            "Thing.run",
            "diagrams/code_diagrams/thing.Thing_run.html",
            "diagrams/code_diagrams/thing.Thing_run.png",
        )
    }
    assert _marker_pointer_issues(source, expected, "thing.py") == []
    assert _marker_pointer_issues(
        source.replace("thing.Thing_run.html", "thing.stale.html"),
        expected,
        "thing.py",
    )
    assert _marker_pointer_issues(
        source.replace(
            "#     (PNG: ``diagrams/code_diagrams/thing.Thing_run.png``)\n",
            "",
        ),
        expected,
        "thing.py",
    )
    begin = "# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit\n"
    end = "# <pyflowchart-code-diagram> END\n"
    assert _marker_pointer_issues(source.replace(begin, begin + begin, 1), expected, "thing.py")
    assert _marker_pointer_issues(source + end, expected, "thing.py")
    assert _marker_pointer_issues(
        source.replace(
            "# Flowchart(s) generated from this file:\n",
            "# Flowchart(s) generated from this file:\n# unexpected marker content\n",
        ),
        expected,
        "thing.py",
    )


def test_shared_copy_provenance_is_part_of_standalone_contract(tmp_path: Path) -> None:
    source = "lambda/proxy-shared/proxy_utils.py"
    copies = LAMBDA_SHARED_SOURCE_TARGETS[source]
    canonical = tmp_path / source
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"canonical marker and source\n")
    for copy in copies:
        copy_path = tmp_path / copy
        copy_path.parent.mkdir(parents=True, exist_ok=True)
        copy_path.write_bytes(canonical.read_bytes())

    assert _shared_source_copy_issues(tmp_path, {source}) == []
    (tmp_path / copies[0]).write_bytes(b"drifted provenance\n")
    assert _shared_source_copy_issues(tmp_path, {source}) == [
        f"shared source copy drifted: {copies[0]} != {source}"
    ]


def test_required_high_value_code_flows_remain_catalogued() -> None:
    catalog = {(target.source, target.function) for target in TARGETS}
    assert catalog >= _REQUIRED_HIGH_VALUE_TARGETS


def test_committed_pngs_are_valid_with_nonzero_dimensions() -> None:
    """Pillow must parse and verify every committed generated PNG."""
    paths = sorted((ROOT / "diagrams" / "code_diagrams").rglob("*.png"))
    paths += sorted((ROOT / "diagrams" / "infra_diagrams").glob("*.png"))
    assert len(paths) == len(TARGETS) + len(INFRA_DIAGRAM_NAMES)

    # Whole-architecture views are intentionally large. Disable Pillow's
    # generic web-upload bomb heuristic for these trusted local artifacts;
    # verify() streams and CRC-checks PNG chunks without allocating a full
    # multi-hundred-megapixel pixel buffer.
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        for path in paths:
            assert path.stat().st_size > 0, path
            with Image.open(path) as image:
                assert image.format == "PNG", path
                assert image.width > 0 and image.height > 0, path
                image.verify()
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


def test_infrastructure_catalog_has_six_stack_and_two_aggregate_views() -> None:
    assert INFRA_DIAGRAM_NAMES == (
        "global-stack",
        "api-gateway-stack",
        "regional-stack",
        "regional-api-stack",
        "monitoring-stack",
        "analytics-stack",
        "full-architecture",
        "full-architecture-detailed",
    )


# ---------------------------------------------------------------------------
# Per-source provenance: a mixed-vintage catalogue is valid
# ---------------------------------------------------------------------------
#
# Incremental regeneration restamps only the sources that changed, so the
# committed catalogue legitimately carries several vintages at once. These
# tests build a minimal two-source catalogue against a temp project root
# (``TARGETS`` monkeypatched) and pin the invariant that replaced the old
# "one timestamp and one commit for everything" rule: each source's marker
# and artifacts must match *that source's* recorded stamp, and the index
# header must match the newest one.

_STAMP_A = ("2026-09-01T12:00:00Z", "a" * 40)
_STAMP_B = ("2026-09-02T12:00:00Z", "b" * 40)


def _write_catalogue(
    root: Path,
    entries: dict[str, tuple[str, str]],
    *,
    index_stamp: tuple[str, str] | None = None,
) -> list[object]:
    """Build a contract-valid code catalogue for ``{source: (at, commit)}``.

    Returns the target list the caller should monkeypatch over ``TARGETS``.
    """
    from diagrams.code_diagrams._targets import Target

    output_dir = root / "diagrams" / "code_diagrams"
    output_dir.mkdir(parents=True, exist_ok=True)
    targets: list[object] = []
    manifest_sources: dict[str, dict[str, str]] = {}
    index_links: list[str] = []

    for source, (generated_at, commit) in entries.items():
        function = "f"
        target = Target(source=source, function=function)
        targets.append(target)
        stem = output_dir / Path(source).parent / f"{Path(source).stem}.{target.slug()}"
        stem.parent.mkdir(parents=True, exist_ok=True)
        html = stem.with_name(f"{stem.name}.html")
        png = stem.with_name(f"{stem.name}.png")
        html_rel = html.relative_to(root).as_posix()
        png_rel = png.relative_to(root).as_posix()
        flow_digest = "0" * 16

        html.write_text(
            "<html><head>"
            f'<meta name="gco-source-commit" content="{commit}">'
            f'<meta name="gco-flow-digest" content="{flow_digest}">'
            "</head><body>"
            f"<p>Generated at (UTC): {generated_at}</p>"
            f"<p><code>{commit}</code></p><p><code>{flow_digest}</code></p>"
            "</body></html>",
            encoding="utf-8",
        )
        png.write_bytes(b"\x89PNG\r\n\x1a\n")

        # Mirror real placement: the marker sits after the module docstring
        # and is followed by a blank line, which is what lets the generator's
        # strip regex remove it cleanly when computing the content digest.
        body = (
            '"""Example."""\n\n'
            f"# <{SENTINEL}> BEGIN - auto-inserted, do not edit\n"
            f"# Generated at (UTC): {generated_at}\n"
            f"# Generated from Git commit: {commit}\n"
            "# Flowchart(s) generated from this file:\n"
            f"#   * ``{function}`` -> ``{html_rel}``\n"
            f"#     (PNG: ``{png_rel}``)\n"
            "# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> "
            "GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> "
            "python diagrams/generate.py --code-only``.\n"
            f"# <{SENTINEL}> END\n"
            "\n"
            f"def {function}():\n    return True\n"
        )
        source_path = root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(body, encoding="utf-8")

        manifest_sources[source] = {
            "digest": generate_mod.source_content_digest(source_path.read_bytes()),
            "generated_at": generated_at,
            "source_commit": commit,
        }
        index_links += [
            f"./{html.relative_to(output_dir).as_posix()}",
            f"./{png.relative_to(output_dir).as_posix()}",
        ]

    (output_dir / "provenance.json").write_text(
        json.dumps({"schema_version": 2, "sources": manifest_sources}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    newest_at, newest_commit = index_stamp or max(entries.values(), key=lambda pair: pair[0])
    readme_lines = [
        "# GCO Code Flowcharts",
        f"Generated at (UTC): `{newest_at}`.",
        f"Generated from Git commit: `{newest_commit}`.",
    ]
    readme_lines += [f"- [chart]({link})" for link in index_links]
    (output_dir / "README.md").write_text("\n".join(readme_lines) + "\n", encoding="utf-8")
    return targets


class TestMixedVintageCatalogue:
    """Per-source stamps replace the old catalogue-wide uniformity rule."""

    def test_two_vintages_are_accepted(self, tmp_path, monkeypatch) -> None:
        targets = _write_catalogue(tmp_path, {"alpha.py": _STAMP_A, "beta.py": _STAMP_B})
        monkeypatch.setattr(diagrams_generate, "TARGETS", targets)
        assert diagrams_generate.check_diagram_contract(tmp_path, infra=False) == []

    def test_marker_stamp_drift_is_reported_with_the_file_and_remedy(
        self, tmp_path, monkeypatch
    ) -> None:
        targets = _write_catalogue(tmp_path, {"alpha.py": _STAMP_A, "beta.py": _STAMP_B})
        monkeypatch.setattr(diagrams_generate, "TARGETS", targets)
        # Restamp only beta's marker, leaving its manifest entry behind.
        source = tmp_path / "beta.py"
        source.write_text(
            source.read_text(encoding="utf-8").replace(_STAMP_B[0], "2026-09-03T12:00:00Z"),
            encoding="utf-8",
        )
        issues = diagrams_generate.check_diagram_contract(tmp_path, infra=False)
        assert any("beta.py" in issue and "marker block's stamp" in issue for issue in issues)
        assert any("python diagrams/generate.py --code-only" in issue for issue in issues)

    def test_artifact_stamp_drift_names_the_owning_source(self, tmp_path, monkeypatch) -> None:
        targets = _write_catalogue(tmp_path, {"alpha.py": _STAMP_A, "beta.py": _STAMP_B})
        monkeypatch.setattr(diagrams_generate, "TARGETS", targets)
        html = tmp_path / "diagrams" / "code_diagrams" / "alpha.f.html"
        html.write_text(
            html.read_text(encoding="utf-8").replace(_STAMP_A[0], "2026-09-04T12:00:00Z"),
            encoding="utf-8",
        )
        issues = diagrams_generate.check_diagram_contract(tmp_path, infra=False)
        assert any(
            "alpha.f.html" in issue and "alpha.py" in issue and "stamp disagrees" in issue
            for issue in issues
        )

    def test_index_header_must_track_the_newest_entry(self, tmp_path, monkeypatch) -> None:
        targets = _write_catalogue(
            tmp_path,
            {"alpha.py": _STAMP_A, "beta.py": _STAMP_B},
            index_stamp=_STAMP_A,  # stale: alpha is older than beta
        )
        monkeypatch.setattr(diagrams_generate, "TARGETS", targets)
        issues = diagrams_generate.check_diagram_contract(tmp_path, infra=False)
        assert any("README.md" in issue and "newest provenance entry" in issue for issue in issues)

    def test_substantive_source_change_is_reported_with_remedy(self, tmp_path, monkeypatch) -> None:
        targets = _write_catalogue(tmp_path, {"alpha.py": _STAMP_A, "beta.py": _STAMP_B})
        monkeypatch.setattr(diagrams_generate, "TARGETS", targets)
        source = tmp_path / "alpha.py"
        source.write_text(
            source.read_text(encoding="utf-8").replace("return True", "return False"),
            encoding="utf-8",
        )
        issues = diagrams_generate.check_diagram_contract(tmp_path, infra=False)
        assert any("alpha.py" in issue and "no longer describe them" in issue for issue in issues)
        assert any("--target alpha.py:<function>" in issue for issue in issues)

    def test_missing_manifest_is_reported_once_with_remedy(self, tmp_path, monkeypatch) -> None:
        targets = _write_catalogue(tmp_path, {"alpha.py": _STAMP_A})
        monkeypatch.setattr(diagrams_generate, "TARGETS", targets)
        (tmp_path / "diagrams" / "code_diagrams" / "provenance.json").unlink()
        issues = diagrams_generate.check_diagram_contract(tmp_path, infra=False)
        assert any("provenance.json" in issue and "To fix:" in issue for issue in issues)
