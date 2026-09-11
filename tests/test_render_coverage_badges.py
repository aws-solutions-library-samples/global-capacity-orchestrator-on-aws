"""Tests for ``.github/scripts/render_coverage_badges.py``.

The renderer turns three differently shaped coverage documents into three
shields.io endpoint files for the README. What matters is that each reader
extracts the number its gate enforces (and refuses a document that is not
that), that the colour rule is the floor and nothing else, and that a broken
input leaves no partial set behind for ``pages.yml`` to publish.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "render_coverage_badges.py"

_spec = importlib.util.spec_from_file_location("render_coverage_badges", _SCRIPT)
assert _spec is not None and _spec.loader is not None
renderer = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("render_coverage_badges", renderer)
_spec.loader.exec_module(renderer)

LCOV = (
    "TN:\n"
    "SF:index.mjs\n"
    "FN:1,handler\n"
    "FNDA:3,handler\n"
    "FNF:1\n"
    "FNH:1\n"
    "DA:1,3\n"
    "DA:2,0\n"
    "DA:3,3\n"
    "DA:4,3\n"
    "BRDA:2,0,0,3\n"
    "BRDA:2,0,1,0\n"
    "BRF:2\n"
    "BRH:1\n"
    "LF:4\n"
    "LH:3\n"
    "end_of_record\n"
)


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# python_percent
# --------------------------------------------------------------------------


def test_python_percent_reads_coverage_py_totals(tmp_path: Path) -> None:
    path = _write(tmp_path, "coverage.json", {"totals": {"percent_covered": 99.25}})
    assert renderer.python_percent(path) == 99.25


def test_python_percent_rejects_a_document_without_totals(tmp_path: Path) -> None:
    path = _write(tmp_path, "coverage.json", {"files": {}})
    with pytest.raises(renderer.BadgeError, match="no totals.percent_covered"):
        renderer.python_percent(path)


def test_python_percent_rejects_a_non_numeric_total(tmp_path: Path) -> None:
    path = _write(tmp_path, "coverage.json", {"totals": {"percent_covered": "100"}})
    with pytest.raises(renderer.BadgeError, match="is not a number"):
        renderer.python_percent(path)
    path = _write(tmp_path, "bool.json", {"totals": {"percent_covered": True}})
    with pytest.raises(renderer.BadgeError, match="is not a number"):
        renderer.python_percent(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not json", "could not read"),
        ("[1, 2]", "expected a JSON object"),
    ],
)
def test_python_percent_rejects_unreadable_documents(
    tmp_path: Path, payload: str, message: str
) -> None:
    path = _write(tmp_path, "coverage.json", payload)
    with pytest.raises(renderer.BadgeError, match=message):
        renderer.python_percent(path)


def test_python_percent_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(renderer.BadgeError, match="could not read"):
        renderer.python_percent(tmp_path / "absent.json")


# --------------------------------------------------------------------------
# bash_percent
# --------------------------------------------------------------------------


def test_bash_percent_reads_the_checker_summary(tmp_path: Path) -> None:
    path = _write(tmp_path, "summary.json", {"ok": True, "percent": 100.0})
    assert renderer.bash_percent(path) == 100.0


def test_bash_percent_refuses_a_run_that_failed_the_floor(tmp_path: Path) -> None:
    """A red bash badge would misreport: the gate failing means Unit Tests failed."""
    path = _write(tmp_path, "summary.json", {"ok": False, "percent": 100.0})
    with pytest.raises(renderer.BadgeError, match="did not pass the shell coverage floor"):
        renderer.bash_percent(path)
    path = _write(tmp_path, "no-ok.json", {"percent": 100.0})
    with pytest.raises(renderer.BadgeError, match=r"ok=None"):
        renderer.bash_percent(path)


def test_bash_percent_rejects_a_summary_without_a_percent(tmp_path: Path) -> None:
    path = _write(tmp_path, "summary.json", {"ok": True})
    with pytest.raises(renderer.BadgeError, match="has no percent"):
        renderer.bash_percent(path)


# --------------------------------------------------------------------------
# node_percent
# --------------------------------------------------------------------------


def test_node_percent_combines_lines_and_branches(tmp_path: Path) -> None:
    """3 of 4 lines and 1 of 2 branches: 4/6, the way coverage.py would count it."""
    path = _write(tmp_path, "lcov.info", LCOV)
    assert renderer.node_percent(path) == pytest.approx(100.0 * 4 / 6)


def test_node_percent_sums_every_source_record(tmp_path: Path) -> None:
    second = LCOV.replace("SF:index.mjs", "SF:other.mjs")
    path = _write(tmp_path, "lcov.info", LCOV + second)
    assert renderer.node_percent(path) == pytest.approx(100.0 * 8 / 12)


def test_node_percent_is_exact_at_the_floor(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "lcov.info", "SF:index.mjs\nLF:10\nLH:10\nBRF:4\nBRH:4\nend_of_record\n"
    )
    assert renderer.node_percent(path) == 100.0


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("TN:\nLF:3\nLH:3\n", "no SF: records"),
        ("SF:index.mjs\nLF:0\nLH:0\nend_of_record\n", "no lines or branches"),
        ("SF:index.mjs\nLF:three\nLH:3\nend_of_record\n", "malformed lcov line"),
    ],
)
def test_node_percent_rejects_tracefiles_with_nothing_to_measure(
    tmp_path: Path, payload: str, message: str
) -> None:
    path = _write(tmp_path, "lcov.info", payload)
    with pytest.raises(renderer.BadgeError, match=message):
        renderer.node_percent(path)


def test_node_percent_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(renderer.BadgeError, match="could not read the lcov tracefile"):
        renderer.node_percent(tmp_path / "absent.info")


# --------------------------------------------------------------------------
# badge
# --------------------------------------------------------------------------


def test_badge_is_green_only_at_the_floor() -> None:
    assert renderer.badge("bash coverage", 100.0) == {
        "schemaVersion": 1,
        "label": "bash coverage",
        "message": "100.0%",
        "color": "brightgreen",
    }
    assert renderer.badge("x", 99.96)["message"] == "100.0%", "rounds for display only"
    assert renderer.badge("x", 99.96)["color"] == "red", "but colours on the exact value"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def _inputs(tmp_path: Path, *, bash_ok: bool = True) -> list[str]:
    python = _write(tmp_path, "coverage.json", {"totals": {"percent_covered": 100.0}})
    bash = _write(tmp_path, "summary.json", {"ok": bash_ok, "percent": 100.0})
    node = _write(tmp_path, "lcov.info", LCOV)
    return ["--python", str(python), "--bash", str(bash), "--node", str(node)]


def test_main_writes_the_three_badges(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "site"
    assert renderer.main([*_inputs(tmp_path), "--out", str(out)]) == 0
    written = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in out.iterdir()}
    assert set(written) == {
        "python-coverage-badge.json",
        "bash-coverage-badge.json",
        "nodejs-coverage-badge.json",
    }
    assert written["python-coverage-badge.json"]["label"] == "python coverage"
    assert written["python-coverage-badge.json"]["color"] == "brightgreen"
    assert written["bash-coverage-badge.json"]["message"] == "100.0%"
    assert written["nodejs-coverage-badge.json"] == {
        "schemaVersion": 1,
        "label": "node.js coverage",
        "message": "66.7%",
        "color": "red",
    }
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == [
        "python-coverage-badge.json: python coverage 100.0% (brightgreen)",
        "bash-coverage-badge.json: bash coverage 100.0% (brightgreen)",
        "nodejs-coverage-badge.json: node.js coverage 66.7% (red)",
    ]


def test_main_writes_nothing_when_an_input_is_broken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No half-rendered set: the deploy must not publish two badges and a 404."""
    out = tmp_path / "site"
    assert renderer.main([*_inputs(tmp_path, bash_ok=False), "--out", str(out)]) == 2
    assert not out.exists()
    assert "did not pass the shell coverage floor" in capsys.readouterr().err


def test_main_requires_every_input() -> None:
    with pytest.raises(SystemExit) as excinfo:
        renderer.main(["--python", "a", "--bash", "b"])
    assert excinfo.value.code == 2


def test_badge_table_names_the_files_pages_yml_publishes() -> None:
    """The workflow and the README both spell these names; keep them in one place."""
    pages = (REPO_ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for _option, _label, name in renderer.BADGES:
        assert name in pages, f"pages.yml does not mention {name}"
        assert name in readme, f"the README badge row does not read {name}"
