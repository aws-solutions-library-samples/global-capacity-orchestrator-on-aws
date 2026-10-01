"""Tests for ``.github/scripts/disambiguate_lcov_functions.py``.

``unit:node:inference-streaming-proxy`` renders its lcov tracefile with
``genhtml``. lcov 2.4 (the Ubuntu 26.04 package) refuses a tracefile in which
two functions of one source file share a name, and ``index.mjs`` has two
``onAbort`` closures. The script renames only the repeated names, in both the
``FN`` and ``FNDA`` records, pairing the k-th ``FNDA`` of a name with the k-th
``FN`` of that name, and fails closed on anything it cannot pair. These tests
cover every branch: the rename itself, untouched tracefiles, the three
refusals, lcov 2's optional end line, several ``SF:`` blocks, and the command.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "disambiguate_lcov_functions.py"
_spec = importlib.util.spec_from_file_location("disambiguate_lcov_functions", _SCRIPT)
assert _spec is not None and _spec.loader is not None
lcov = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("disambiguate_lcov_functions", lcov)
_spec.loader.exec_module(lcov)

TRACEFILE = """\
TN:
SF:index.mjs
FN:67,boundedEnvFloat
FN:897,onAbort
FN:900,openUpstream
FN:974,onAbort
FNF:4
FNH:4
FNDA:3,boundedEnvFloat
FNDA:1,onAbort
FNDA:2,openUpstream
FNDA:1,onAbort
DA:67,3
DA:897,1
LF:2
LH:2
BRDA:67,0,0,1
BRF:1
BRH:1
end_of_record
"""

EXPECTED = TRACEFILE.replace("FN:897,onAbort", "FN:897,onAbort@L897").replace(
    "FN:974,onAbort", "FN:974,onAbort@L974"
)
EXPECTED = EXPECTED.replace(
    "FNDA:1,onAbort\nFNDA:2,openUpstream\nFNDA:1,onAbort",
    "FNDA:1,onAbort@L897\nFNDA:2,openUpstream\nFNDA:1,onAbort@L974",
)


def test_repeated_names_get_their_line_in_both_records() -> None:
    text, renames = lcov.disambiguate(TRACEFILE)
    assert text == EXPECTED
    assert renames == [
        "index.mjs: onAbort at line 897 -> onAbort@L897",
        "index.mjs: onAbort at line 974 -> onAbort@L974",
    ]


def test_the_kth_fnda_follows_the_kth_fn_even_when_counts_differ() -> None:
    """The hit counts are what tell the two apart; each must stay with its own line."""
    tracefile = TRACEFILE.replace("FNDA:1,onAbort\nFNDA:2", "FNDA:7,onAbort\nFNDA:2")
    text, _renames = lcov.disambiguate(tracefile)
    assert "FNDA:7,onAbort@L897" in text
    assert "FNDA:1,onAbort@L974" in text


def test_a_tracefile_without_repeats_is_returned_unchanged() -> None:
    tracefile = TRACEFILE.replace("FN:974,onAbort", "FN:974,onTimeout").replace(
        "FNDA:2,openUpstream\nFNDA:1,onAbort", "FNDA:2,openUpstream\nFNDA:1,onTimeout"
    )
    assert lcov.disambiguate(tracefile) == (tracefile, [])
    assert lcov.disambiguate("") == ("", [])
    assert lcov.disambiguate("TN:\n\n") == ("TN:\n\n", [])


def test_a_missing_trailing_newline_is_preserved() -> None:
    text, _renames = lcov.disambiguate(TRACEFILE.rstrip("\n"))
    assert text == EXPECTED.rstrip("\n")


def test_each_source_block_is_handled_on_its_own() -> None:
    second = TRACEFILE.replace("SF:index.mjs", "SF:other.mjs").replace("FN:974,onAbort", "FN:5,x")
    second = second.replace("FNDA:2,openUpstream\nFNDA:1,onAbort", "FNDA:2,openUpstream\nFNDA:0,x")
    text, renames = lcov.disambiguate(TRACEFILE + second)
    assert text == EXPECTED + second
    assert [rename.split(":")[0] for rename in renames] == ["index.mjs", "index.mjs"]


def test_lcov_2_end_lines_and_names_with_commas_are_read() -> None:
    tracefile = (
        "SF:a.js\nFN:1,3,f\nFN:5,9,f\nFNDA:1,f\nFNDA:2,f\nFN:11,g,h\nFNDA:0,g,h\nend_of_record\n"
    )
    text, renames = lcov.disambiguate(tracefile)
    assert text == (
        "SF:a.js\nFN:1,3,f@L1\nFN:5,9,f@L5\nFNDA:1,f@L1\nFNDA:2,f@L5\n"
        "FN:11,g,h\nFNDA:0,g,h\nend_of_record\n"
    )
    assert len(renames) == 2


@pytest.mark.parametrize(
    ("tracefile", "message"),
    [
        ("FN:1,f\n", "line 1: 'FN:1,f' appears before any SF: record"),
        ("SF:a.js\nhello\n", "line 2: 'hello' is not an lcov record"),
        ("SF:a.js\nFN:1,f\nFN:2,f\nFNDA:1,f\nend_of_record\n", "'f' FN x2 FNDA x1"),
        ("SF:a.js\nFN:1,f\nFNDA:1,f\nFNDA:1,g\nend_of_record\n", "'g' FN x0 FNDA x1"),
        ("SF:a.js\nFN:1,f\nFNDA:1,f\nFNF:2\nend_of_record\n", "a.js: FNF:2 but 1 FN records"),
    ],
    ids=["no SF", "not a record", "FN without FNDA", "FNDA without FN", "FNF off"],
)
def test_anything_the_script_cannot_pair_is_refused(tracefile: str, message: str) -> None:
    with pytest.raises(lcov.TracefileError, match=message.replace("(", r"\(")):
        lcov.disambiguate(tracefile)


def test_a_record_after_end_of_record_needs_a_new_sf() -> None:
    with pytest.raises(lcov.TracefileError, match="line 3: 'FN:1,f' appears before any SF"):
        lcov.disambiguate("SF:a.js\nend_of_record\nFN:1,f\n")


def test_main_rewrites_in_place_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracefile = tmp_path / "lcov.info"
    tracefile.write_text(TRACEFILE, encoding="utf-8")
    assert lcov.main([str(tracefile)]) == 0
    assert tracefile.read_text(encoding="utf-8") == EXPECTED
    out = capsys.readouterr().out
    assert "index.mjs: onAbort at line 897 -> onAbort@L897" in out
    assert f"{tracefile}: 2 function name(s) made distinct" in out


def test_main_writes_elsewhere_when_asked(tmp_path: Path) -> None:
    tracefile = tmp_path / "lcov.info"
    output = tmp_path / "out" / "lcov.info"
    output.parent.mkdir()
    tracefile.write_text(TRACEFILE, encoding="utf-8")
    assert lcov.main([str(tracefile), "--output", str(output)]) == 0
    assert tracefile.read_text(encoding="utf-8") == TRACEFILE
    assert output.read_text(encoding="utf-8") == EXPECTED


@pytest.mark.parametrize(
    ("content", "message"),
    [(None, "No such file"), ("SF:a.js\nFN:1,f\nend_of_record\n", "FN and FNDA records disagree")],
    ids=["missing file", "unpairable"],
)
def test_main_reports_a_refusal_as_an_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None, message: str
) -> None:
    tracefile = tmp_path / "lcov.info"
    if content is not None:
        tracefile.write_text(content, encoding="utf-8")
    assert lcov.main([str(tracefile)]) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"::error::{tracefile}: ")
    assert message in out
    assert content is None or tracefile.read_text(encoding="utf-8") == content
