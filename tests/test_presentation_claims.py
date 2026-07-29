"""Keep the headline numbers in ``demo/GCO_PRESENTATION.pdf`` true.

The "Built on a serious engineering foundation" slide quotes three figures that
are really assertions about this repository: the unit-test total, the number of
CDK synth matrix configurations, and the enforced coverage floor. Nothing checked
them, so the deck could drift and the drift would surface in front of an
audience rather than in review.

These read the committed PDF with ``pypdf`` and compare each figure to the live
repository.

The test total is deliberately compared at 500-test granularity. The slide says
"9,000+", which stays honest as tests are added and only needs a new number when
the total crosses the next 500 boundary — so the deck is not invalidated by a
single new test, and CI asks for an edit only when the rounded claim actually
changes.

When one of these fails, the fix is to edit ``demo/GCO_PRESENTATION.pptx`` and
re-export the PDF; the failure message states the exact value to use.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRESENTATION_PDF = PROJECT_ROOT / "demo" / "GCO_PRESENTATION.pdf"
PRESENTATION_SOURCE = PROJECT_ROOT / "demo" / "GCO_PRESENTATION.pptx"
BATS_DIR = PROJECT_ROOT / "tests" / "BATS"

#: The slide is found by this label rather than by page number, so reordering
#: slides does not break the check.
SLIDE_ANCHOR = "unit tests (PyTest + BATS)"

#: Granularity for the quoted test total. The slide claims "N+", so any total in
#: ``[N, N + 500)`` is truthful and requires no edit.
TEST_COUNT_GRANULARITY = 500

#: How the deck should be corrected, appended to every failure here.
_REMEDIATION = (
    f"Update {PRESENTATION_SOURCE.relative_to(PROJECT_ROOT)} and re-export "
    f"{PRESENTATION_PDF.relative_to(PROJECT_ROOT)} (PowerPoint or LibreOffice: "
    "File > Export as PDF), then commit both."
)


def _slide_text() -> str:
    """Return the text of the engineering-foundation slide.

    ``pypdf`` logs "Ignoring wrong pointing object" for this deck's cross
    reference table; the text extracts correctly, so the noise is silenced
    rather than allowed to bury a real failure message.
    """
    pytest.importorskip("pypdf", reason="pypdf is required to read the presentation")
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)

    assert PRESENTATION_PDF.is_file(), f"missing {PRESENTATION_PDF}"
    reader = PdfReader(str(PRESENTATION_PDF))
    for page in reader.pages:
        text = page.extract_text() or ""
        if SLIDE_ANCHOR in text:
            return text

    raise AssertionError(
        f"no slide in {PRESENTATION_PDF.name} contains {SLIDE_ANCHOR!r}. If the "
        "slide was reworded, update SLIDE_ANCHOR in this test to match."
    )


def _count_pytest_tests() -> int:
    """Collect every pytest test, including modules run by dedicated CI jobs.

    ``-o addopts=`` clears the project's ``-v``, without which ``--collect-only``
    prints an indented tree instead of one node id per line.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-o",
            "addopts=",
            "-q",
            "--collect-only",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"pytest collection failed ({result.returncode}); cannot count tests:\n"
        f"{result.stdout[-2000:]}{result.stderr[-2000:]}"
    )

    count = sum(1 for line in result.stdout.splitlines() if "::" in line)
    assert count > 1000, f"sanity floor: only counted {count} pytest tests"
    return count


def _count_bats_tests() -> int:
    """Count ``@test`` cases across the BATS suites."""
    assert BATS_DIR.is_dir(), f"missing {BATS_DIR}"
    total = 0
    for path in sorted(BATS_DIR.glob("*.bats")):
        total += len(
            re.findall(r"^\s*@test\b", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
        )
    assert total > 50, f"sanity floor: only counted {total} BATS tests"
    return total


def _floor_to_granularity(value: int) -> int:
    return (value // TEST_COUNT_GRANULARITY) * TEST_COUNT_GRANULARITY


@pytest.fixture(scope="module")
def slide_text() -> str:
    return _slide_text()


def test_quoted_unit_test_total_is_current(slide_text: str) -> None:
    """The "N+ unit tests" figure matches the real total, floored to 500."""
    match = re.search(r"([\d,]+)\s*\+\s*unit tests \(PyTest \+ BATS\)", slide_text)
    assert match, (
        "could not find the unit-test total on the slide; expected text like "
        f"'9,000+unit tests (PyTest + BATS)'. Slide text was:\n{slide_text[:400]}"
    )
    quoted = int(match.group(1).replace(",", ""))

    pytest_tests = _count_pytest_tests()
    bats_tests = _count_bats_tests()
    total = pytest_tests + bats_tests
    expected = _floor_to_granularity(total)

    assert quoted == expected, (
        f"The presentation claims {quoted:,}+ unit tests, but the repository has "
        f"{total:,} ({pytest_tests:,} pytest + {bats_tests} BATS), which rounds "
        f"down to {expected:,}. Change the slide to read '{expected:,}+'. "
        f"{_REMEDIATION}"
    )


def test_quoted_cdk_matrix_config_count_is_current(slide_text: str) -> None:
    """The "N CDK synth matrix configs" figure matches the real matrix."""
    match = re.search(r"(\d+)\s*CDK synth matrix configs", slide_text)
    assert match, (
        "could not find the CDK matrix figure on the slide; expected text like "
        f"'34CDK synth matrix configs'. Slide text was:\n{slide_text[:400]}"
    )
    quoted = int(match.group(1))

    sys.path.insert(0, str(PROJECT_ROOT))
    from tests._cdk_config_matrix import CONFIGS

    actual = len(CONFIGS)
    assert quoted == actual, (
        f"The presentation claims {quoted} CDK synth matrix configs, but "
        f"tests/_cdk_config_matrix.py defines {actual}. Change the slide to read "
        f"'{actual}'. {_REMEDIATION}"
    )


def test_quoted_coverage_floor_matches_the_enforced_floor(slide_text: str) -> None:
    """The ">N% unit-test coverage" figure matches the floor CI enforces."""
    match = re.search(r"(\d+)\s*%\s*>?\s*unit-test coverage", slide_text)
    assert match, (
        "could not find the coverage claim on the slide; expected text like "
        f"'90%>unit-test coverage'. Slide text was:\n{slide_text[:400]}"
    )
    quoted = int(match.group(1))

    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    floor_match = re.search(r"^fail_under\s*=\s*(\d+)", pyproject, flags=re.MULTILINE)
    assert floor_match, "could not read fail_under from [tool.coverage.report]"
    enforced = int(floor_match.group(1))

    assert quoted == enforced, (
        f"The presentation claims >{quoted}% coverage, but the enforced floor in "
        f"pyproject's [tool.coverage.report] is {enforced}%. Align them: change the "
        f"slide to '{enforced}%' or raise fail_under. {_REMEDIATION}"
    )
