"""Unit tests for the exact, expiring npm-audit suppression checker."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "check_npm_audit.py"

PACKAGE_DIR = "."
PACKAGE = "brace-expansion"
ADVISORY = "GHSA-3jxr-9vmj-r5cp"
NODE = "node_modules/aws-cdk-lib/node_modules/brace-expansion"
TODAY = dt.date(2026, 7, 19)
EXPIRY = dt.date(2026, 8, 20)


def _load_checker() -> ModuleType:
    """Load the non-package script by path without changing ``sys.path``."""
    spec = importlib.util.spec_from_file_location("check_npm_audit", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _suppression(**overrides: object):
    values = {
        "package_dir": PACKAGE_DIR,
        "package": PACKAGE,
        "advisory": ADVISORY,
        "node_path": NODE,
        "expires": EXPIRY,
        "line": 2,
    }
    values.update(overrides)
    return checker.Suppression(**values)


def _finding(
    *,
    severity: str = "high",
    advisories: tuple[str, ...] = (ADVISORY,),
    nodes: tuple[str, ...] = (NODE,),
) -> dict[str, object]:
    return {
        "severity": severity,
        "via": [{"url": f"https://github.com/advisories/{advisory}"} for advisory in advisories],
        "nodes": list(nodes),
    }


def _report(
    vulnerabilities: dict[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    return {"vulnerabilities": vulnerabilities or {}}


class TestLoadSuppressions:
    def test_loads_exact_identity_and_skips_comments(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            ".npm-audit-ignore",
            "# Temporary exception\n\n"
            f"{PACKAGE_DIR}|{PACKAGE}|{ADVISORY}|{NODE}|exp:{EXPIRY.isoformat()}\n",
        )

        assert checker._load_suppressions(path, TODAY) == [
            checker.Suppression(
                package_dir=PACKAGE_DIR,
                package=PACKAGE,
                advisory=ADVISORY,
                node_path=NODE,
                expires=EXPIRY,
                line=3,
            )
        ]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ("too|few|fields|here\n", r"expected package-dir\|package\|advisory"),
            (
                f"{PACKAGE_DIR}||{ADVISORY}|{NODE}|exp:{EXPIRY.isoformat()}\n",
                "suppression fields cannot be empty",
            ),
            (
                f"{PACKAGE_DIR}|{PACKAGE}|{ADVISORY}|{NODE}|until:{EXPIRY.isoformat()}\n",
                r"expected package-dir\|package\|advisory",
            ),
            (
                f"{PACKAGE_DIR}|{PACKAGE}|{ADVISORY}|{NODE}|exp:2026-02-30\n",
                "invalid expiration date",
            ),
        ],
    )
    def test_rejects_malformed_entries(self, tmp_path: Path, body: str, message: str) -> None:
        path = _write(tmp_path, ".npm-audit-ignore", body)

        with pytest.raises(ValueError, match=message):
            checker._load_suppressions(path, TODAY)

    def test_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="suppression file not found"):
            checker._load_suppressions(tmp_path / "missing", TODAY)

    def test_expiration_is_inclusive(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            ".npm-audit-ignore",
            f"{PACKAGE_DIR}|{PACKAGE}|{ADVISORY}|{NODE}|exp:{TODAY.isoformat()}\n",
        )

        with pytest.raises(ValueError, match=f"expired on {TODAY.isoformat()}"):
            checker._load_suppressions(path, TODAY)

    def test_rejects_duplicate_identity(self, tmp_path: Path) -> None:
        entry = f"{PACKAGE_DIR}|{PACKAGE}|{ADVISORY}|{NODE}"
        path = _write(
            tmp_path,
            ".npm-audit-ignore",
            f"{entry}|exp:2026-08-20\n{entry}|exp:2026-09-20\n",
        )

        with pytest.raises(ValueError, match=r":2: duplicate suppression"):
            checker._load_suppressions(path, TODAY)


class TestNpmAuditReportParsing:
    def test_extracts_only_github_advisory_ids(self) -> None:
        via = [
            {"url": f"https://github.com/advisories/{ADVISORY}"},
            {"url": f"https://github.com/advisories/{ADVISORY}"},
            {"url": "https://example.invalid/GHSA-ignored"},
            "brace-expansion",
            None,
        ]

        assert checker._advisories(via) == {ADVISORY}
        assert checker._advisories("not-a-list") == set()

    def test_loads_valid_json_report(self, tmp_path: Path) -> None:
        expected = _report({PACKAGE: _finding()})
        path = _write(tmp_path, "audit.json", json.dumps(expected))

        assert checker._load_report(path) == expected

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ("not json", "invalid npm-audit JSON"),
            ("[]", "npm-audit report must be a JSON object"),
            (
                json.dumps({"error": {"code": "EAUDIT", "summary": "audit failed"}}),
                "npm audit returned an operational error",
            ),
            (json.dumps({"metadata": {}}), "missing the vulnerabilities object"),
        ],
    )
    def test_rejects_malformed_or_operational_error_reports(
        self, tmp_path: Path, body: str, message: str
    ) -> None:
        path = _write(tmp_path, "audit.json", body)

        with pytest.raises(ValueError, match=message):
            checker._load_report(path)


class TestCheckReport:
    def test_clean_report_succeeds(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert checker.check_report(_report(), PACKAGE_DIR, []) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_exact_suppression_succeeds_with_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = checker.check_report(_report({PACKAGE: _finding()}), PACKAGE_DIR, [_suppression()])

        captured = capsys.readouterr()
        assert rc == 0
        assert ADVISORY in captured.out
        assert NODE in captured.out
        assert EXPIRY.isoformat() in captured.out
        assert captured.err == ""

    @pytest.mark.parametrize(
        ("field", "wrong_value"),
        [
            ("package_dir", "lambda/inference-streaming-proxy"),
            ("package", "other-package"),
            ("advisory", "GHSA-aaaa-bbbb-cccc"),
            ("node_path", "node_modules/brace-expansion"),
        ],
    )
    def test_every_identity_field_must_match(
        self,
        capsys: pytest.CaptureFixture[str],
        field: str,
        wrong_value: str,
    ) -> None:
        rc = checker.check_report(
            _report({PACKAGE: _finding()}),
            PACKAGE_DIR,
            [_suppression(**{field: wrong_value})],
        )

        captured = capsys.readouterr()
        assert rc == 1
        assert captured.out == ""
        assert f"{PACKAGE}: unsuppressed high finding" in captured.err

    def test_additional_high_finding_still_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        other_advisory = "GHSA-aaaa-bbbb-cccc"
        other_node = "node_modules/other-package"
        report = _report(
            {
                PACKAGE: _finding(),
                "other-package": _finding(advisories=(other_advisory,), nodes=(other_node,)),
            }
        )

        rc = checker.check_report(report, PACKAGE_DIR, [_suppression()])

        captured = capsys.readouterr()
        assert rc == 1
        assert f"Temporarily suppressing {ADVISORY}" in captured.out
        assert "other-package: unsuppressed high finding" in captured.err

    def test_compound_record_fails_closed_when_any_pair_is_missing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        second_advisory = "GHSA-aaaa-bbbb-cccc"
        second_node = "node_modules/brace-expansion"
        report = _report(
            {
                PACKAGE: _finding(
                    advisories=(ADVISORY, second_advisory),
                    nodes=(NODE, second_node),
                )
            }
        )

        rc = checker.check_report(report, PACKAGE_DIR, [_suppression()])

        captured = capsys.readouterr()
        assert rc == 1
        assert captured.out == ""
        assert f"{PACKAGE}: unsuppressed high finding" in captured.err
        assert "stale suppression at line 2" in captured.err

    def test_compound_record_succeeds_when_every_pair_is_suppressed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        advisories = (ADVISORY, "GHSA-aaaa-bbbb-cccc")
        nodes = (NODE, "node_modules/brace-expansion")
        suppressions = [
            _suppression(advisory=advisory, node_path=node, line=index)
            for index, (advisory, node) in enumerate(
                ((advisory, node) for advisory in advisories for node in nodes),
                start=1,
            )
        ]

        rc = checker.check_report(
            _report({PACKAGE: _finding(advisories=advisories, nodes=nodes)}),
            PACKAGE_DIR,
            suppressions,
        )

        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.count("::warning::Temporarily suppressing") == 4
        assert captured.err == ""

    def test_moderate_finding_is_below_gate(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = checker.check_report(
            _report({PACKAGE: _finding(severity="moderate")}), PACKAGE_DIR, []
        )

        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == ""
        assert captured.err == ""

    def test_stale_scoped_suppression_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = checker.check_report(_report(), PACKAGE_DIR, [_suppression(line=7)])

        captured = capsys.readouterr()
        assert rc == 1
        assert "stale suppression at line 7" in captured.err
        assert ADVISORY in captured.err

    def test_suppression_for_another_package_directory_is_not_stale(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = checker.check_report(
            _report(),
            "lambda/inference-streaming-proxy",
            [_suppression(package_dir=PACKAGE_DIR)],
        )

        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == ""
        assert captured.err == ""

    def test_malformed_vulnerability_record_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = checker.check_report(_report({PACKAGE: "invalid"}), PACKAGE_DIR, [])

        captured = capsys.readouterr()
        assert rc == 1
        assert f"{PACKAGE}: malformed vulnerability record" in captured.err


class TestMain:
    def test_exact_suppression_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = _write(
            tmp_path,
            "audit.json",
            json.dumps(_report({PACKAGE: _finding()})),
        )
        ignore = _write(
            tmp_path,
            ".npm-audit-ignore",
            f"{PACKAGE_DIR}|{PACKAGE}|{ADVISORY}|{NODE}|exp:2099-01-01\n",
        )

        rc = checker.main(
            [
                "--report",
                str(report),
                "--package-dir",
                PACKAGE_DIR,
                "--ignore-file",
                str(ignore),
            ]
        )

        captured = capsys.readouterr()
        assert rc == 0
        assert f"Temporarily suppressing {ADVISORY}" in captured.out
        assert captured.err == ""

    def test_unsuppressed_finding_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = _write(
            tmp_path,
            "audit.json",
            json.dumps(_report({PACKAGE: _finding()})),
        )
        ignore = _write(tmp_path, ".npm-audit-ignore", "# No suppressions\n")

        rc = checker.main(
            [
                "--report",
                str(report),
                "--package-dir",
                PACKAGE_DIR,
                "--ignore-file",
                str(ignore),
            ]
        )

        captured = capsys.readouterr()
        assert rc == 1
        assert f"{PACKAGE}: unsuppressed high finding" in captured.err

    def test_malformed_report_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = _write(tmp_path, "audit.json", "not json")
        ignore = _write(tmp_path, ".npm-audit-ignore", "# No suppressions\n")

        rc = checker.main(
            [
                "--report",
                str(report),
                "--package-dir",
                PACKAGE_DIR,
                "--ignore-file",
                str(ignore),
            ]
        )

        captured = capsys.readouterr()
        assert rc == 1
        assert "invalid npm-audit JSON" in captured.err
