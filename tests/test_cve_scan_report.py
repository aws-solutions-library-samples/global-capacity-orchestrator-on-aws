"""Tests for ``.github/scripts/cve_scan_report.py``.

The script decides whether the weekly cve-scan opens, refreshes or closes its
rolling issue. The verdicts it must never get wrong: a run with any finding is
``findings``; a run that did not produce every report, or whose jobs did not
all succeed, is ``incomplete`` even with no findings, because a scan that did
not run is not a clean scan; only a complete, empty run is ``clean``.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "cve_scan_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("cve_scan_report", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report = _load()

TODAY = datetime.date(2026, 11, 9)
RUN_URL = "https://github.com/example/repo/actions/runs/1"


def _vuln(cve: str = "CVE-2026-0001", pkg: str = "zlib1g", **extra: Any) -> dict[str, Any]:
    return {
        "VulnerabilityID": cve,
        "PkgName": pkg,
        "InstalledVersion": "1.0",
        "FixedVersion": "1.1",
        "Severity": "HIGH",
        **extra,
    }


def _write(directory: Path, name: str, results: list[dict[str, Any]]) -> None:
    (directory / name).write_text(json.dumps({"ArtifactName": name, "Results": results}))


def _needs(images: list[str] | None = None, **results: str) -> dict[str, Any]:
    images = ["dev"] if images is None else images
    needs: dict[str, Any] = {
        "cve-scan": {"result": "success", "outputs": {}},
        "discover-images": {"result": "success", "outputs": {"images": json.dumps(images)}},
        "container-scan": {"result": "success", "outputs": {}},
    }
    for job, result in results.items():
        needs[job.replace("_", "-")]["result"] = result
    return needs


def _clean_tree(tmp_path: Path, images: tuple[str, ...] = ("dev",)) -> Path:
    _write(
        tmp_path, report.FS_REPORT, [{"Target": "requirements-lock.txt", "Vulnerabilities": None}]
    )
    for image in images:
        _write(tmp_path, f"trivy-{image}-report.json", [{"Target": f"{image}:scan"}])
    return tmp_path


class TestEvaluate:
    def test_complete_empty_run_is_clean(self, tmp_path: Path) -> None:
        verdict = report.evaluate(_clean_tree(tmp_path), _needs())
        assert verdict.status == "clean"
        assert verdict.findings == ()
        assert verdict.problems == ()

    def test_any_vulnerability_is_a_finding(self, tmp_path: Path) -> None:
        _clean_tree(tmp_path)
        _write(
            tmp_path,
            "trivy-dev-report.json",
            [{"Target": "dev:scan (debian 13.7)", "Vulnerabilities": [_vuln()]}],
        )
        verdict = report.evaluate(tmp_path, _needs(container_scan="failure"))
        assert verdict.status == "findings"
        assert [f.vulnerability for f in verdict.findings] == ["CVE-2026-0001"]
        # The failed job is still reported alongside the findings.
        assert verdict.problems == ("job `container-scan` finished `failure`",)

    def test_failed_job_without_findings_is_incomplete(self, tmp_path: Path) -> None:
        verdict = report.evaluate(_clean_tree(tmp_path), _needs(container_scan="failure"))
        assert verdict.status == "incomplete"

    def test_missing_report_is_incomplete(self, tmp_path: Path) -> None:
        _clean_tree(tmp_path)
        verdict = report.evaluate(tmp_path, _needs(images=["dev", "queue-processor"]))
        assert verdict.status == "incomplete"
        assert verdict.problems == ("report `trivy-queue-processor-report.json` is missing",)

    def test_unreadable_report_is_incomplete(self, tmp_path: Path) -> None:
        _clean_tree(tmp_path)
        (tmp_path / "trivy-dev-report.json").write_text("{not json")
        verdict = report.evaluate(tmp_path, _needs())
        assert verdict.status == "incomplete"
        assert "trivy-dev-report.json: cannot be read as JSON" in verdict.problems[0]

    def test_non_report_json_is_incomplete(self, tmp_path: Path) -> None:
        _clean_tree(tmp_path)
        (tmp_path / "trivy-dev-report.json").write_text(json.dumps({"Results": "nope"}))
        verdict = report.evaluate(tmp_path, _needs())
        assert verdict.problems == ("trivy-dev-report.json: not a Trivy report (no Results list)",)

    def test_skipped_or_unknown_job_results_are_problems(self, tmp_path: Path) -> None:
        needs = _needs(cve_scan="skipped")
        needs["mystery"] = "not-a-mapping"
        verdict = report.evaluate(_clean_tree(tmp_path), needs)
        assert verdict.status == "incomplete"
        assert "job `cve-scan` finished `skipped`" in verdict.problems
        assert "job `mystery` finished `unknown`" in verdict.problems

    def test_malformed_entries_inside_a_report_are_ignored(self, tmp_path: Path) -> None:
        _clean_tree(tmp_path)
        _write(
            tmp_path,
            report.FS_REPORT,
            [
                "not-a-result",
                {"Target": "x", "Vulnerabilities": ["not-a-vuln", _vuln("CVE-2026-0002")]},
            ],
        )
        verdict = report.evaluate(tmp_path, _needs())
        assert [f.vulnerability for f in verdict.findings] == ["CVE-2026-0002"]


class TestExpectedReports:
    @pytest.mark.parametrize(
        "outputs",
        [
            {},
            {"images": ""},
            {"images": "{broken"},
            {"images": json.dumps({"not": "a list"})},
        ],
    )
    def test_without_a_usable_image_list_only_the_fs_report_is_expected(
        self, outputs: dict[str, Any]
    ) -> None:
        needs = {"discover-images": {"result": "failure", "outputs": outputs}}
        assert report.expected_reports(needs) == [report.FS_REPORT]

    def test_non_mapping_discovery_entry_expects_only_the_fs_report(self) -> None:
        assert report.expected_reports({"discover-images": "failure"}) == [report.FS_REPORT]

    def test_one_report_per_published_image(self) -> None:
        needs = {"discover-images": {"outputs": {"images": json.dumps(["a", "", 7, "b"])}}}
        assert report.expected_reports(needs) == [
            report.FS_REPORT,
            "trivy-a-report.json",
            "trivy-b-report.json",
        ]


class TestRender:
    def test_findings_table_is_deduplicated_sorted_and_escaped(self, tmp_path: Path) -> None:
        finding = report.Finding("r.json", "t|arget", "pkg", "CVE-2", "HIGH", "1", "")
        other = report.Finding("r.json", "a", "pkg", "CVE-1", "HIGH", "1", "2")
        verdict = report.Verdict("findings", (finding, other, finding), ())
        body = report.render(verdict, run_url=RUN_URL, today=TODAY)
        assert body.count("| r.json |") == 2
        assert body.index("CVE-1") < body.index("CVE-2")
        assert "t\\|arget" in body
        assert "| 1 | - |" in body  # an empty fixed version renders as "-"
        assert "2026-11-09" in body and RUN_URL in body
        assert "## How to clear it" in body
        assert "Incomplete evidence" not in body

    def test_incomplete_run_explains_what_is_missing(self) -> None:
        verdict = report.Verdict("incomplete", (), ("report `x` is missing",))
        body = report.render(verdict, run_url=RUN_URL, today=TODAY)
        assert "## Incomplete evidence" in body
        assert "- report `x` is missing" in body
        assert "## Findings" not in body


class TestMain:
    def _run(self, tmp_path: Path, needs: str, *extra: str) -> tuple[int, Path, Path]:
        body = tmp_path / "body.md"
        output = tmp_path / "github_output"
        code = report.main(
            [
                "--reports-dir",
                str(tmp_path / "reports"),
                "--needs-json",
                needs,
                "--run-url",
                RUN_URL,
                "--output",
                str(body),
                *extra,
            ]
        )
        return code, body, output

    def test_writes_the_body_and_the_status_output(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        reports.mkdir()
        _clean_tree(reports)
        output = tmp_path / "github_output"
        code, body, _ = self._run(
            tmp_path,
            json.dumps(_needs()),
            "--github-output",
            str(output),
            "--today",
            "2026-11-09",
        )
        assert code == 0
        assert output.read_text() == "status=clean\n"
        assert "2026-11-09" in body.read_text()

    def test_defaults_today_and_skips_the_output_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, body, output = self._run(tmp_path, json.dumps(_needs()))
        assert code == 0
        assert not output.exists()
        assert body.read_text().startswith("The scheduled CVE scan on ")
        assert "cve-scan verdict: incomplete" in capsys.readouterr().out

    @pytest.mark.parametrize("needs", ["{not json", "[1, 2]"])
    def test_malformed_needs_is_a_usage_error(
        self, tmp_path: Path, needs: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, body, _ = self._run(tmp_path, needs)
        assert code == 2
        assert not body.exists()
        assert "--needs-json" in capsys.readouterr().err
