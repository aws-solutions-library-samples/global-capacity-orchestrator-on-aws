"""Structural contracts for committed code and infrastructure diagrams."""

import functools
import json
import subprocess
import sys
from pathlib import Path

import pytest
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


# ---------------------------------------------------------------------------
# One broken invariant per test: every violation names the file and the cause
# ---------------------------------------------------------------------------
#
# The checker is a CI gate, so the *exact* issue text is the contract a reader
# relies on. Each test below starts from a contract-valid fake repository under
# ``tmp_path``, breaks precisely one invariant, and pins the resulting message.

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_MARKER_SOURCE = """# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-08-30T12:00:00Z
# Generated from Git commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
# Flowchart(s) generated from this file:
#   * ``Thing.run`` -> ``diagrams/code_diagrams/thing.Thing_run.html``
#     (PNG: ``diagrams/code_diagrams/thing.Thing_run.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END
"""
_MARKER_EXPECTED = {
    (
        "Thing.run",
        "diagrams/code_diagrams/thing.Thing_run.html",
        "diagrams/code_diagrams/thing.Thing_run.png",
    )
}
_MARKER_BEGIN_LINE = "# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit\n"
_MARKER_END_LINE = "# <pyflowchart-code-diagram> END\n"
_MARKER_ENTRY = (
    "#   * ``Thing.run`` -> ``diagrams/code_diagrams/thing.Thing_run.html``\n"
    "#     (PNG: ``diagrams/code_diagrams/thing.Thing_run.png``)\n"
)


def _edit(path: Path, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in ``path``; fail loudly if ``old`` is absent."""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {path}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def _refresh_manifest_digest(root: Path, source: str) -> None:
    """Re-record ``source``'s digest so a source edit breaks only the invariant under test."""
    manifest_path = root / "diagrams" / "code_diagrams" / "provenance.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][source]["digest"] = generate_mod.source_content_digest(
        (root / source).read_bytes()
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _add_sibling_target(root: Path, source: str, function: str) -> object:
    """Chart a second ``function`` of an already-catalogued ``source``.

    Writes the artifacts, index links, and marker pointer the contract expects,
    reusing the stamp recorded for ``source``. The marker is excluded from the
    provenance digest, so adding a pointer keeps the manifest valid.
    """
    from diagrams.code_diagrams._targets import Target

    output_dir = root / "diagrams" / "code_diagrams"
    manifest = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    generated_at = manifest["sources"][source]["generated_at"]
    commit = manifest["sources"][source]["source_commit"]
    target = Target(source=source, function=function)
    stem = output_dir / Path(source).parent / f"{Path(source).stem}.{target.slug()}"
    html = stem.with_name(f"{stem.name}.html")
    png = stem.with_name(f"{stem.name}.png")
    flow_digest = "1" * 16
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
    png.write_bytes(_PNG_SIGNATURE)
    pointer = (
        f"#   * ``{function}`` -> ``{html.relative_to(root).as_posix()}``\n"
        f"#     (PNG: ``{png.relative_to(root).as_posix()}``)\n"
    )
    _edit(root / source, "# Regenerate with", pointer + "# Regenerate with")
    _append(
        output_dir / "README.md",
        f"- [chart](./{html.relative_to(output_dir).as_posix()})\n"
        f"- [chart](./{png.relative_to(output_dir).as_posix()})\n",
    )
    return target


def _write_infra_catalogue(root: Path) -> Path:
    """Lay out one PNG per catalogued infrastructure diagram under ``root``."""
    output_dir = root / "diagrams" / "infra_diagrams"
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in INFRA_DIAGRAM_NAMES:
        (output_dir / f"{name}.png").write_bytes(_PNG_SIGNATURE)
    return output_dir


class TestSharedSourceCopies:
    """Charted shared Lambda sources need byte-identical checked-in copies."""

    def test_missing_canonical_source_is_reported_once(self, tmp_path: Path) -> None:
        """Without the canonical file the copies are not inspected at all."""
        source = "lambda/proxy-shared/proxy_utils.py"
        assert _shared_source_copy_issues(tmp_path, {source}) == [
            f"missing canonical shared source: {source}"
        ]

    def test_every_missing_copy_is_named(self, tmp_path: Path) -> None:
        """Each absent copy of a present canonical source is its own issue."""
        source = "lambda/proxy-shared/proxy_utils.py"
        canonical = tmp_path / source
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(b"canonical\n")
        assert _shared_source_copy_issues(tmp_path, {source}) == [
            f"missing shared source copy: {copy}" for copy in LAMBDA_SHARED_SOURCE_TARGETS[source]
        ]

    def test_uncharted_shared_sources_are_ignored(self, tmp_path: Path) -> None:
        """Only shared sources that are charted targets take part in the contract."""
        assert _shared_source_copy_issues(tmp_path, set()) == []
        assert _shared_source_copy_issues(tmp_path, {"gco/unrelated.py"}) == []


class TestMarkerPointerIssues:
    """Exact grammar and pointer diagnostics for one source marker block."""

    def test_valid_marker_has_no_issues(self) -> None:
        assert _marker_pointer_issues(_MARKER_SOURCE, _MARKER_EXPECTED, "thing.py") == []

    def test_third_sentinel_mention_is_a_delimiter_count_error(self) -> None:
        """Any extra mention of the sentinel short-circuits to one delimiter issue."""
        source = _MARKER_SOURCE + "# see pyflowchart-code-diagram above\n"
        assert _marker_pointer_issues(source, _MARKER_EXPECTED, "thing.py") == [
            "source marker delimiter count invalid: thing.py"
        ]

    def test_end_before_begin_is_a_block_count_error(self) -> None:
        """Balanced delimiters in the wrong order never form a block."""
        body = _MARKER_SOURCE.replace(_MARKER_BEGIN_LINE, "").replace(_MARKER_END_LINE, "")
        source = _MARKER_END_LINE + body + _MARKER_BEGIN_LINE
        assert _marker_pointer_issues(source, _MARKER_EXPECTED, "thing.py") == [
            "source marker block count invalid: thing.py"
        ]

    def test_duplicate_pointer_lines_are_reported(self) -> None:
        """A pointer listed twice is flagged even though the pointer set is right."""
        source = _MARKER_SOURCE.replace(_MARKER_ENTRY, _MARKER_ENTRY * 2)
        assert _marker_pointer_issues(source, _MARKER_EXPECTED, "thing.py") == [
            "source marker contains duplicate pointers: thing.py"
        ]

    def test_drift_lists_missing_and_stale_pointers(self) -> None:
        """The drift message spells out both the expected and the stale pointer."""
        source = _MARKER_SOURCE.replace("thing.Thing_run.html", "thing.stale.html")
        assert _marker_pointer_issues(source, _MARKER_EXPECTED, "thing.py") == [
            "source marker pointers drifted: thing.py: "
            "missing=[('Thing.run', 'diagrams/code_diagrams/thing.Thing_run.html', "
            "'diagrams/code_diagrams/thing.Thing_run.png')], "
            "stale=[('Thing.run', 'diagrams/code_diagrams/thing.stale.html', "
            "'diagrams/code_diagrams/thing.Thing_run.png')]"
        ]

    def test_grammar_violation_is_reported_alongside_pointer_drift(self) -> None:
        """Dropping the PNG line breaks the grammar and turns the pointer PNG-less."""
        source = _MARKER_SOURCE.replace(
            "#     (PNG: ``diagrams/code_diagrams/thing.Thing_run.png``)\n", ""
        )
        issues = _marker_pointer_issues(source, _MARKER_EXPECTED, "thing.py")
        assert issues[0] == "source marker grammar invalid: thing.py"
        assert issues[1].startswith("source marker pointers drifted: thing.py: ")
        assert "('Thing.run', 'diagrams/code_diagrams/thing.Thing_run.html', None)" in issues[1]
        assert len(issues) == 2


class TestCodeCatalogueViolations:
    """Each broken code-catalogue invariant yields exactly one precise issue."""

    @pytest.fixture
    def root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        targets = _write_catalogue(tmp_path, {"alpha.py": _STAMP_A})
        monkeypatch.setattr(diagrams_generate, "TARGETS", targets)
        assert check_diagram_contract(tmp_path, infra=False) == []
        return tmp_path

    @staticmethod
    def _issues(root: Path) -> list[str]:
        return check_diagram_contract(root, infra=False)

    def test_missing_and_orphan_artifacts_are_named_by_repository_path(self, root: Path) -> None:
        """A deleted PNG and a stray HTML are reported relative to the project root."""
        (root / "diagrams" / "code_diagrams" / "alpha.f.png").unlink()
        (root / "diagrams" / "code_diagrams" / "stray.html").write_text("<html></html>")
        assert self._issues(root) == [
            "missing code artifact: diagrams/code_diagrams/alpha.f.png",
            "orphan code artifact: diagrams/code_diagrams/stray.html",
        ]

    def test_index_with_two_timestamps_is_invalid(self, root: Path) -> None:
        """The index header must carry exactly one generation timestamp."""
        readme = root / "diagrams" / "code_diagrams" / "README.md"
        _append(readme, "Generated at (UTC): `2026-09-05T00:00:00Z`.\n")
        assert self._issues(root) == [
            "code index timestamp invalid: ['2026-09-01T12:00:00Z', '2026-09-05T00:00:00Z']"
        ]

    def test_index_without_source_commit_is_invalid(self, root: Path) -> None:
        """The index header must carry exactly one source commit."""
        readme = root / "diagrams" / "code_diagrams" / "README.md"
        _edit(readme, f"Generated from Git commit: `{_STAMP_A[1]}`.\n", "")
        assert self._issues(root) == ["code index source commit invalid: []"]

    def test_index_must_link_every_artifact(self, root: Path) -> None:
        """An artifact absent from the index is reported by its index-relative path."""
        readme = root / "diagrams" / "code_diagrams" / "README.md"
        _edit(readme, "- [chart](./alpha.f.png)\n", "")
        assert self._issues(root) == ["code index omitted: alpha.f.png"]

    def test_index_must_not_link_unknown_artifacts(self, root: Path) -> None:
        """A link to an artifact no target produces is an orphan index entry."""
        readme = root / "diagrams" / "code_diagrams" / "README.md"
        _append(readme, "- [chart](./zeta.g.html)\n")
        assert self._issues(root) == ["orphan code index entry: zeta.g.html"]

    def test_source_may_mention_one_generation_timestamp_only(self, root: Path) -> None:
        """A second timestamp anywhere in the source makes its marker stamp ambiguous."""
        _append(root / "alpha.py", "# Generated at (UTC): 2026-09-05T00:00:00Z\n")
        _refresh_manifest_digest(root, "alpha.py")
        assert self._issues(root) == [
            "source marker timestamp invalid: alpha.py: "
            "['2026-09-01T12:00:00Z', '2026-09-05T00:00:00Z']"
        ]

    def test_source_may_mention_one_source_commit_only(self, root: Path) -> None:
        """A second commit anywhere in the source makes its marker stamp ambiguous."""
        _append(root / "alpha.py", f"# Generated from Git commit: {'c' * 40}\n")
        _refresh_manifest_digest(root, "alpha.py")
        assert self._issues(root) == [
            f"source marker commit invalid: alpha.py: ['{'a' * 40}', '{'c' * 40}']"
        ]

    def test_every_function_of_a_source_needs_its_own_pointer(self, root: Path) -> None:
        """A second target of one source is checked only for its own pointer."""
        diagrams_generate.TARGETS.append(_add_sibling_target(root, "alpha.py", "g"))
        assert self._issues(root) == []
        _edit(root / "alpha.py", "``g``", "``h``")
        assert self._issues(root) == [
            "source marker pointers drifted: alpha.py: "
            "missing=[('g', 'diagrams/code_diagrams/alpha.g.html', "
            "'diagrams/code_diagrams/alpha.g.png')], "
            "stale=[('h', 'diagrams/code_diagrams/alpha.g.html', "
            "'diagrams/code_diagrams/alpha.g.png')]",
            "source marker omitted: alpha.py:g",
        ]

    def test_artifact_without_timestamp_is_invalid(self, root: Path) -> None:
        html = root / "diagrams" / "code_diagrams" / "alpha.f.html"
        _edit(html, f"<p>Generated at (UTC): {_STAMP_A[0]}</p>", "")
        assert self._issues(root) == [
            "code artifact timestamp invalid: diagrams/code_diagrams/alpha.f.html: []"
        ]

    def test_artifact_with_two_source_commit_metas_is_invalid(self, root: Path) -> None:
        html = root / "diagrams" / "code_diagrams" / "alpha.f.html"
        _edit(html, "</head>", f'<meta name="gco-source-commit" content="{"c" * 40}"></head>')
        assert self._issues(root) == [
            "code artifact source commit invalid: diagrams/code_diagrams/alpha.f.html: "
            f"['{'a' * 40}', '{'c' * 40}']"
        ]

    def test_artifact_must_show_its_source_commit_visibly(self, root: Path) -> None:
        """The meta commit alone is not enough; the page must render it as text."""
        html = root / "diagrams" / "code_diagrams" / "alpha.f.html"
        _edit(html, f"<p><code>{_STAMP_A[1]}</code></p>", "")
        assert self._issues(root) == [
            "code artifact visible source commit omitted: diagrams/code_diagrams/alpha.f.html"
        ]

    def test_artifact_without_flow_digest_is_invalid(self, root: Path) -> None:
        html = root / "diagrams" / "code_diagrams" / "alpha.f.html"
        _edit(html, f'<meta name="gco-flow-digest" content="{"0" * 16}">', "")
        assert self._issues(root) == [
            "code artifact flow digest invalid: diagrams/code_diagrams/alpha.f.html: []"
        ]

    def test_artifact_must_show_its_flow_digest_visibly(self, root: Path) -> None:
        html = root / "diagrams" / "code_diagrams" / "alpha.f.html"
        _edit(html, f"<p><code>{'0' * 16}</code></p>", "")
        assert self._issues(root) == [
            "code artifact visible flow digest omitted: diagrams/code_diagrams/alpha.f.html"
        ]

    def test_retired_markers_are_reported_only_for_eligible_files(self, root: Path) -> None:
        """Markers in uncharted sources are retired; build output and dirs are skipped."""
        marker = f"# <{SENTINEL}> BEGIN\n# <{SENTINEL}> END\n"
        (root / "app.py").write_text(marker, encoding="utf-8")
        (root / "cli").mkdir()
        (root / "cli" / "retired.py").write_text(marker, encoding="utf-8")
        (root / "cli" / "clean.py").write_text("x = 1\n", encoding="utf-8")
        (root / "gco" / "odd.py").mkdir(parents=True)
        (root / "lambda" / "x-build").mkdir(parents=True)
        (root / "lambda" / "x-build" / "handler.py").write_text(marker, encoding="utf-8")
        assert self._issues(root) == [
            "retired source marker: app.py",
            "retired source marker: cli/retired.py",
        ]

    def test_shared_source_copies_carry_the_canonical_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Byte-identical copies of a charted shared source are neither retired nor drifted."""
        source = "lambda/proxy-shared/proxy_utils.py"
        targets = _write_catalogue(tmp_path, {source: _STAMP_A})
        monkeypatch.setattr(diagrams_generate, "TARGETS", targets)
        canonical = (tmp_path / source).read_bytes()
        for copy in LAMBDA_SHARED_SOURCE_TARGETS[source]:
            copy_path = tmp_path / copy
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            copy_path.write_bytes(canonical)
        assert self._issues(tmp_path) == []

        drifted = LAMBDA_SHARED_SOURCE_TARGETS[source][0]
        (tmp_path / drifted).write_bytes(canonical + b"# local tweak\n")
        assert self._issues(tmp_path) == [f"shared source copy drifted: {drifted} != {source}"]


class TestInfrastructureCatalogue:
    """``code=False`` checks only the infrastructure PNG catalogue."""

    def test_complete_catalogue_is_clean(self, tmp_path: Path) -> None:
        _write_infra_catalogue(tmp_path)
        assert check_diagram_contract(tmp_path, code=False) == []

    def test_absent_catalogue_reports_every_expected_png(self, tmp_path: Path) -> None:
        assert check_diagram_contract(tmp_path, code=False) == [
            f"missing infrastructure artifact: {name}"
            for name in sorted(f"{name}.png" for name in INFRA_DIAGRAM_NAMES)
        ]

    def test_orphans_and_graphviz_sidecars_are_reported(self, tmp_path: Path) -> None:
        """Stray PNGs and leftover ``.dot`` files are both structural drift."""
        output_dir = _write_infra_catalogue(tmp_path)
        (output_dir / "stray.png").write_bytes(_PNG_SIGNATURE)
        (output_dir / "global-stack.dot").write_text("digraph {}\n", encoding="utf-8")
        assert check_diagram_contract(tmp_path, code=False) == [
            "orphan infrastructure artifact: stray.png",
            "transient Graphviz sidecar: global-stack.dot",
        ]


# ---------------------------------------------------------------------------
# ``python diagrams/generate.py`` CLI
# ---------------------------------------------------------------------------

_CODE_GENERATOR_ARGV = [sys.executable, "diagrams/code_diagrams/generate.py", "--require-png"]
_INFRA_GENERATOR_ARGV = [sys.executable, "diagrams/infra_diagrams/generate.py", "--stack", "all"]


class _CliHarness:
    """Run ``main()`` against a fake repository with child processes recorded.

    ``ROOT`` is redirected so generator children would run in ``root``;
    ``check_diagram_contract`` is bound to ``root`` because its default
    project root is captured at definition time; ``subprocess.run`` is
    replaced by a recorder that honours ``check=True`` semantics.
    """

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.child_returncode = 0
        self._monkeypatch = monkeypatch
        monkeypatch.setattr(diagrams_generate, "ROOT", root)
        monkeypatch.setattr(
            diagrams_generate,
            "check_diagram_contract",
            functools.partial(check_diagram_contract, root),
        )
        monkeypatch.setattr(diagrams_generate.subprocess, "run", self._run)

    def _run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), kwargs))
        if kwargs.get("check") and self.child_returncode:
            raise subprocess.CalledProcessError(self.child_returncode, list(argv))
        return subprocess.CompletedProcess(list(argv), self.child_returncode)

    def main(self, *argv: str) -> None:
        self._monkeypatch.setattr(sys, "argv", ["generate.py", *argv])
        diagrams_generate.main()

    def with_code_catalogue(self) -> None:
        targets = _write_catalogue(self.root, {"alpha.py": _STAMP_A})
        self._monkeypatch.setattr(diagrams_generate, "TARGETS", targets)

    def with_infra_catalogue(self) -> Path:
        return _write_infra_catalogue(self.root)

    def with_generation_env(self) -> None:
        self._monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")
        self._monkeypatch.setenv("GCO_DIAGRAM_SOURCE_COMMIT", "a" * 40)


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _CliHarness:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    monkeypatch.delenv("GCO_DIAGRAM_SOURCE_COMMIT", raising=False)
    return _CliHarness(tmp_path, monkeypatch)


class TestCliCheck:
    """``--check`` verifies the checkout without launching any generator."""

    def test_current_catalogues_print_confirmation_and_exit_zero(
        self, cli: _CliHarness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.with_code_catalogue()
        cli.with_infra_catalogue()
        cli.main("--check")
        out, err = capsys.readouterr()
        assert out == "Diagram artifact contract is current\n"
        assert err == ""
        assert cli.calls == []

    def test_each_issue_is_printed_to_stderr_before_exit_one(
        self, cli: _CliHarness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.with_code_catalogue()
        infra = cli.with_infra_catalogue()
        (infra / "global-stack.png").unlink()
        (infra / "regional-stack.dot").write_text("digraph {}\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            cli.main("--check")
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert err == (
            "ERROR: missing infrastructure artifact: global-stack.png\n"
            "ERROR: transient Graphviz sidecar: regional-stack.dot\n"
        )
        assert cli.calls == []

    def test_code_only_ignores_the_infrastructure_catalogue(
        self, cli: _CliHarness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No infrastructure PNGs exist, yet ``--code-only`` passes."""
        cli.with_code_catalogue()
        cli.main("--check", "--code-only")
        assert capsys.readouterr().out == "Diagram artifact contract is current\n"

    def test_infra_only_ignores_the_code_catalogue(
        self, cli: _CliHarness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No code catalogue exists at all, yet ``--infra-only`` passes."""
        cli.with_infra_catalogue()
        cli.main("--check", "--infra-only")
        assert capsys.readouterr().out == "Diagram artifact contract is current\n"

    def test_check_needs_no_provenance_environment(
        self, cli: _CliHarness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Verification must work in any clone without generation-time variables."""
        cli.with_code_catalogue()
        cli.with_infra_catalogue()
        cli.main("--check")
        assert capsys.readouterr().out == "Diagram artifact contract is current\n"

    def test_selecting_both_families_is_a_usage_error(
        self, cli: _CliHarness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main("--check", "--code-only", "--infra-only")
        assert exc.value.code == 2
        assert "not allowed with argument" in capsys.readouterr().err
        assert cli.calls == []


class TestCliGeneration:
    """Without ``--check`` the CLI delegates to both generators, then re-verifies."""

    def test_code_generation_requires_source_date_epoch(
        self, cli: _CliHarness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.with_code_catalogue()
        with pytest.raises(SystemExit) as exc:
            cli.main("--code-only")
        assert exc.value.code == 2
        assert "canonical code generation requires integer SOURCE_DATE_EPOCH" in (
            capsys.readouterr().err
        )
        assert cli.calls == []

    def test_code_generation_requires_source_commit(
        self,
        cli: _CliHarness,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cli.with_code_catalogue()
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert "canonical code generation requires 40-character GCO_DIAGRAM_SOURCE_COMMIT" in (
            capsys.readouterr().err
        )
        assert cli.calls == []

    def test_default_runs_code_then_infra_generator_from_root(
        self, cli: _CliHarness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Both children run from the project root with ``check=True``; a clean tree returns."""
        cli.with_code_catalogue()
        cli.with_infra_catalogue()
        cli.with_generation_env()
        assert cli.main() is None
        assert cli.calls == [
            (_CODE_GENERATOR_ARGV, {"cwd": cli.root, "check": True}),
            (_INFRA_GENERATOR_ARGV, {"cwd": cli.root, "check": True}),
        ]
        assert capsys.readouterr() == ("", "")

    def test_code_only_runs_just_the_code_generator(self, cli: _CliHarness) -> None:
        cli.with_code_catalogue()
        cli.with_generation_env()
        cli.main("--code-only")
        assert cli.calls == [(_CODE_GENERATOR_ARGV, {"cwd": cli.root, "check": True})]

    def test_infra_only_needs_no_provenance_environment(self, cli: _CliHarness) -> None:
        """Infrastructure rendering is not stamped, so the env guards do not apply."""
        cli.with_infra_catalogue()
        cli.main("--infra-only")
        assert cli.calls == [(_INFRA_GENERATOR_ARGV, {"cwd": cli.root, "check": True})]

    def test_failing_generator_surfaces_as_called_process_error(self, cli: _CliHarness) -> None:
        """A non-zero child aborts before the infra generator or the re-check run."""
        cli.with_code_catalogue()
        cli.with_infra_catalogue()
        cli.with_generation_env()
        cli.child_returncode = 3
        with pytest.raises(subprocess.CalledProcessError) as exc:
            cli.main()
        assert exc.value.returncode == 3
        assert exc.value.cmd == _CODE_GENERATOR_ARGV
        assert [argv for argv, _ in cli.calls] == [_CODE_GENERATOR_ARGV]

    def test_drift_left_behind_by_generators_is_a_runtime_error(self, cli: _CliHarness) -> None:
        """Successful children followed by a stale tree fail with every issue joined."""
        cli.with_code_catalogue()
        infra = cli.with_infra_catalogue()
        (infra / "global-stack.png").unlink()
        (infra / "global-stack.dot").write_text("digraph {}\n", encoding="utf-8")
        cli.with_generation_env()
        with pytest.raises(RuntimeError) as exc:
            cli.main()
        assert str(exc.value) == (
            "diagram generation completed with structural drift: "
            "missing infrastructure artifact: global-stack.png; "
            "transient Graphviz sidecar: global-stack.dot"
        )
        assert [argv for argv, _ in cli.calls] == [_CODE_GENERATOR_ARGV, _INFRA_GENERATOR_ARGV]
