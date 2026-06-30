"""Tests for ``.github/scripts/check_pip_audit_ignore.py``.

The validator gates the pip-audit job in ``.github/workflows/security.yml``.
These tests pin its behavior so refactors don't quietly relax the rules.
The script is loaded by file path because ``.github/scripts/`` isn't on
``sys.path`` and shouldn't be turned into a package just to support tests.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "check_pip_audit_ignore.py"


def _load_validator():
    """Load the validator module by file path.

    The script lives under ``.github/scripts`` (not on ``sys.path``); we
    don't want to add an ``__init__.py`` just to import it because the
    ``.github`` tree is intentionally not a Python package. ``importlib``
    handles this without polluting the package namespace.
    """
    spec = importlib.util.spec_from_file_location("check_pip_audit_ignore", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


# Reference date used by every test that pins time. Picked arbitrarily;
# the only requirement is that fixtures with future-dated `exp:` markers
# stay future-relative to it.
_TODAY = datetime.date(2026, 5, 20)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".pip-audit-ignore"
    path.write_text(body)
    return path


# ── check_file: happy paths ──────────────────────────────────────────────────


class TestCheckFileHappyPath:
    """Entries with a valid future ``exp:`` produce no findings."""

    def test_single_valid_entry(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "PYSEC-2025-001 exp:2026-12-31\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == []

    def test_multiple_valid_entries(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "\n".join(
                [
                    "PYSEC-2025-001 exp:2026-12-31",
                    "GHSA-aaaa-bbbb-cccc exp:2027-01-15",
                    "CVE-2025-99999 exp:2026-09-01",
                ]
            )
            + "\n",
        )
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == []

    def test_blank_lines_and_comments_skipped(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "\n".join(
                [
                    "# This is a comment.",
                    "",
                    "  # Indented comment with leading whitespace.",
                    "PYSEC-2025-001 exp:2026-12-31",
                    "",
                    "# Trailing comment.",
                ]
            )
            + "\n",
        )
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == []

    def test_missing_file_is_clean(self, tmp_path: Path) -> None:
        # A missing file is not an error — the project may not yet have any
        # suppressions. Same posture as .trivyignore being absent.
        missing, expired = validator.check_file(tmp_path / "nonexistent", today=_TODAY)
        assert missing == []
        assert expired == []


# ── check_file: expired entries ──────────────────────────────────────────────


class TestCheckFileExpired:
    """``exp:`` dates on-or-before today must surface as expired."""

    def test_date_in_past(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "PYSEC-1999-001 exp:1999-01-01\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == [(1, "PYSEC-1999-001", datetime.date(1999, 1, 1))]

    def test_date_equal_to_today_is_expired(self, tmp_path: Path) -> None:
        # Inclusive expiration: once the listed date arrives, the entry
        # is considered expired. Pinning this prevents a future "off by
        # one" refactor from silently granting a bonus day.
        path = _write(tmp_path, f"PYSEC-2026-EXACT exp:{_TODAY.isoformat()}\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == [(1, "PYSEC-2026-EXACT", _TODAY)]

    def test_date_one_day_before_today_passes(self, tmp_path: Path) -> None:
        # The day-before mirror of the equal-to-today test. Bracketing
        # both sides nails down the boundary.
        day_before = _TODAY - datetime.timedelta(days=1)
        path = _write(tmp_path, f"PYSEC-2026-CLOSE exp:{day_before.isoformat()}\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == [(1, "PYSEC-2026-CLOSE", day_before)]

    def test_date_one_day_after_today_passes(self, tmp_path: Path) -> None:
        # And the +1 day side, which must NOT trigger.
        day_after = _TODAY + datetime.timedelta(days=1)
        path = _write(tmp_path, f"PYSEC-2026-FUTURE exp:{day_after.isoformat()}\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == []

    def test_multiple_expired_entries_all_reported(self, tmp_path: Path) -> None:
        # All offending entries must surface in one pass — operators
        # shouldn't have to fix-and-rerun to find every problem.
        path = _write(
            tmp_path,
            "\n".join(
                [
                    "PYSEC-A exp:1999-01-01",
                    "PYSEC-B exp:1999-12-31",
                    "PYSEC-C exp:2026-12-31",  # future, OK
                ]
            )
            + "\n",
        )
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert [vid for _, vid, _ in expired] == ["PYSEC-A", "PYSEC-B"]


# ── check_file: missing or malformed exp: markers ────────────────────────────


class TestCheckFileMissingMarker:
    """Anything that's not a valid future ``exp:YYYY-MM-DD`` is rejected."""

    def test_no_exp_marker(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "PYSEC-2025-001 some rationale\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == [(1, "PYSEC-2025-001")]
        assert expired == []

    def test_id_only(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "PYSEC-2025-001\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == [(1, "PYSEC-2025-001")]
        assert expired == []

    def test_malformed_date_components(self, tmp_path: Path) -> None:
        # Regex syntax is satisfied (\d{4}-\d{2}-\d{2}) but the date is
        # not a real calendar day. The validator treats this as missing
        # because the suppression has no actionable expiration.
        path = _write(tmp_path, "PYSEC-2025-001 exp:2026-13-40\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        # exp:2026-13-40 doesn't satisfy the regex (month must be \d{2}
        # but real validation happens via datetime.date()), so it's
        # treated as missing — the regex permits it, datetime rejects it.
        assert missing == [(1, "PYSEC-2025-001")]
        assert expired == []

    def test_exp_with_wrong_format(self, tmp_path: Path) -> None:
        # Two-digit year doesn't satisfy the YYYY-MM-DD regex anchor,
        # so it's treated as no marker at all.
        path = _write(tmp_path, "PYSEC-2025-001 exp:26-12-31\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == [(1, "PYSEC-2025-001")]
        assert expired == []


# ── main(): exit codes and stdout ────────────────────────────────────────────


class TestMainExitCodes:
    """``main()`` returns the same exit codes the workflow inspects."""

    def test_clean_file_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, "PYSEC-2025-001 exp:2099-01-01\n")
        rc = validator.main([str(path)])
        captured = capsys.readouterr()
        assert rc == 0
        # Clean runs are silent — operators only see output when there's
        # something to act on.
        assert captured.out == ""

    def test_expired_file_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, "PYSEC-EXP exp:1999-01-01\n")
        rc = validator.main([str(path), "--today", _TODAY.isoformat()])
        captured = capsys.readouterr()
        assert rc == 1
        # The report must name the offending ID and date so the failure is
        # actionable from the workflow log alone.
        assert "PYSEC-EXP" in captured.out
        assert "1999-01-01" in captured.out

    def test_missing_marker_file_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, "PYSEC-MISSING\n")
        rc = validator.main([str(path), "--today", _TODAY.isoformat()])
        captured = capsys.readouterr()
        assert rc == 1
        assert "PYSEC-MISSING" in captured.out
        assert "missing a valid exp" in captured.out

    def test_invalid_today_format_errors(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "PYSEC-2025-001 exp:2099-01-01\n")
        # argparse turns ArgumentTypeError into SystemExit(2).
        with pytest.raises(SystemExit) as exc_info:
            validator.main([str(path), "--today", "not-a-date"])
        assert exc_info.value.code == 2

    def test_missing_file_argument_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Missing ignore file is not an error — same posture as a clean run.
        rc = validator.main([str(tmp_path / "nonexistent")])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == ""


# ── live file ────────────────────────────────────────────────────────────────


class TestLiveFile:
    """The committed ``.pip-audit-ignore`` must validate clean against today.

    A failure here means someone added or extended a suppression with an
    expiration date that has already passed. The fix is to re-evaluate
    the entry, not to silence the test.
    """

    def test_committed_file_passes_today(self, capsys: pytest.CaptureFixture[str]) -> None:
        live_path = PROJECT_ROOT / ".github" / "config" / ".pip-audit-ignore"
        if not live_path.exists():
            pytest.skip("No .pip-audit-ignore committed under .github/config/")
        rc = validator.main([str(live_path)])
        captured = capsys.readouterr()
        assert rc == 0, (
            f".pip-audit-ignore failed validation today.\nValidator output:\n{captured.out}"
        )
