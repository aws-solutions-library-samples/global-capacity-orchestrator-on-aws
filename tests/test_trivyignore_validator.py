"""Expiration/format guard for ``.github/config/.trivyignore``.

``.trivyignore`` uses the same ``<ID> exp:YYYY-MM-DD`` line format as
``.pip-audit-ignore`` (both files' headers say each entry MUST carry a dated
``exp:`` marker and be re-evaluated when it expires). Nothing enforced that for
``.trivyignore``, so an expired suppression could silently outlive its fix and
keep hiding a HIGH/CRITICAL CVE from the Trivy scan.

This reuses the already-format-agnostic validator behind ``.pip-audit-ignore``
(``.github/scripts/check_pip_audit_ignore.py`` — its own docstring notes it
mirrors how ``.trivyignore`` is treated) and points it at the committed
``.trivyignore``. A failure here means an entry is malformed or past its
``exp:`` date: re-evaluate it — drop it if the CVE is fixed (or now patched at
build time, as the AL2023 base-image entries were) or extend the date with
fresh rationale. Do not silence the test.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "check_pip_audit_ignore.py"
TRIVYIGNORE = PROJECT_ROOT / ".github" / "config" / ".trivyignore"

_TODAY = datetime.date(2026, 5, 20)


def _load_validator():
    """Load the shared suppression-file validator by file path.

    Same loader the pip-audit validator test uses — ``.github/scripts`` is
    intentionally not a Python package, so import by path rather than adding an
    ``__init__.py``.
    """
    spec = importlib.util.spec_from_file_location("check_suppression_ignore", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".trivyignore"
    path.write_text(body)
    return path


class TestTrivyignoreFormat:
    """The validator enforces the dated-suppression contract on trivy entries."""

    def test_valid_future_entry_passes(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "CVE-2026-12345 exp:2026-12-31\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == []

    def test_expired_entry_is_flagged(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "CVE-2026-00001 exp:2026-01-01\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert [vid for _, vid, _ in expired] == ["CVE-2026-00001"]

    def test_missing_marker_is_flagged(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "CVE-2026-00002 no expiration here\n")
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == [(1, "CVE-2026-00002")]
        assert expired == []

    def test_comments_and_blanks_skipped(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "\n".join(
                [
                    "# a comment",
                    "",
                    "CVE-2026-00003 exp:2027-01-01  # rationale",
                ]
            )
            + "\n",
        )
        missing, expired = validator.check_file(path, today=_TODAY)
        assert missing == []
        assert expired == []


class TestLiveTrivyignore:
    """The committed ``.trivyignore`` must validate clean against today.

    A failure means someone added/extended a suppression whose ``exp:`` date has
    already passed, or omitted the marker. Re-evaluate the entry rather than
    silencing this test.
    """

    def test_committed_file_passes_today(self, capsys: pytest.CaptureFixture[str]) -> None:
        if not TRIVYIGNORE.exists():
            pytest.skip("No .trivyignore committed under .github/config/")
        rc = validator.main([str(TRIVYIGNORE)])
        captured = capsys.readouterr()
        assert rc == 0, f".trivyignore failed validation today.\nValidator output:\n{captured.out}"

    def test_committed_file_entries_are_well_formed(self) -> None:
        """Every non-comment entry starts with a CVE/GHSA/PYSEC-style id token."""
        if not TRIVYIGNORE.exists():
            pytest.skip("No .trivyignore committed under .github/config/")
        bad: list[tuple[int, str]] = []
        for lineno, raw in enumerate(TRIVYIGNORE.read_text().splitlines(), start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            token = stripped.split(None, 1)[0]
            if not token.startswith(("CVE-", "GHSA-", "PYSEC-")):
                bad.append((lineno, token))
        assert not bad, f"Unexpected non-advisory tokens in .trivyignore: {bad}"
