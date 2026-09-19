"""Unit tests for ``.github/scripts/verify_gate_needs.py``.

The gate jobs are the only required status checks on ``main``, so this script
is the merge rule: it must refuse every result but ``success`` unless a skip
allowance explains it, and it must fail *louder* (exit 2) when its own inputs
are malformed, because a misconfigured gate that exits 0 would merge anything.
Every path is exercised on synthetic ``needs`` documents; nothing here touches
GitHub or a workflow file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "verify_gate_needs.py"


def _load_gate() -> ModuleType:
    """Load the non-package script by path without changing ``sys.path``."""
    spec = importlib.util.spec_from_file_location("verify_gate_needs", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _needs(**jobs: str | tuple[str, dict[str, str]]) -> dict[str, dict[str, object]]:
    """Build a ``needs`` context: ``job="result"`` or ``job=("result", {outputs})``."""
    document: dict[str, dict[str, object]] = {}
    for job_id, spec in jobs.items():
        if isinstance(spec, tuple):
            result, outputs = spec
            document[job_id.replace("_", "-")] = {"result": result, "outputs": outputs}
        else:
            document[job_id.replace("_", "-")] = {"result": spec, "outputs": {}}
    return document


def _run(needs: dict[str, dict[str, object]], *allow: str) -> int:
    argv = ["--needs", json.dumps(needs)]
    for spec in allow:
        argv += ["--allow-skipped", spec]
    return gate.main(argv)


# ─── parse_allowance ─────────────────────────────────────────────────────────


def test_unconditional_allowance_parses_to_a_bare_job() -> None:
    allowance = gate.parse_allowance("changes")
    assert allowance == gate.SkipAllowance(job="changes")
    assert allowance.describe() == "skip allowed unconditionally"


def test_conditional_allowance_parses_dependency_output_and_value() -> None:
    allowance = gate.parse_allowance("unit-lockfile-freshness=changes.deps=false")
    assert allowance.job == "unit-lockfile-freshness"
    assert allowance.condition == gate.OutputCondition("changes", "deps", "false")
    assert allowance.describe() == "skip allowed while changes.outputs.deps == 'false'"


@pytest.mark.parametrize(
    "spec",
    [
        "",  # nothing at all
        "=changes.deps=false",  # empty job id
        "job=changes",  # no output
        "job=changes.deps",  # no value
        "job=.deps=false",  # empty dependency
        "job=changes.=false",  # empty output
        "job=changes.deps=",  # empty value
        "job=changesdeps=false",  # no dot
    ],
)
def test_malformed_allowances_are_policy_errors(spec: str) -> None:
    with pytest.raises(gate.PolicyError):
        gate.parse_allowance(spec)


# ─── parse_needs ─────────────────────────────────────────────────────────────


def test_parse_needs_accepts_the_toJSON_shape() -> None:
    needs = gate.parse_needs('{"a": {"result": "success", "outputs": {}}}')
    assert needs == {"a": {"result": "success", "outputs": {}}}


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        "{}",  # a gate that needs nothing must not pass vacuously
        '"success"',
        '{"a": "success"}',  # entry is not an object
        '{"a": {"outputs": {}}}',  # no result
        '{"a": {"result": 1}}',  # result is not a string
    ],
)
def test_parse_needs_rejects_every_malformed_document(document: str) -> None:
    with pytest.raises(gate.PolicyError):
        gate.parse_needs(document)


# ─── evaluate ────────────────────────────────────────────────────────────────


def test_all_success_yields_no_violations() -> None:
    rows, violations = gate.evaluate(_needs(a="success", b="success"), [])
    assert violations == []
    assert rows == [("a", "success", ""), ("b", "success", "")]


@pytest.mark.parametrize("result", ["failure", "cancelled", "timed_out", "neutral", "banana"])
def test_any_result_but_success_is_a_violation(result: str) -> None:
    rows, violations = gate.evaluate(_needs(a="success", b=result), [])
    assert violations == [f"b: result was {result!r}"]
    assert rows[1] == ("b", result, f"result was {result!r}")


def test_a_skip_without_an_allowance_is_a_violation() -> None:
    _rows, violations = gate.evaluate(_needs(a="skipped"), [])
    assert violations == ["a: skipped without an allowance"]


def test_an_unconditional_allowance_excuses_a_skip() -> None:
    rows, violations = gate.evaluate(_needs(changes="skipped"), [gate.parse_allowance("changes")])
    assert violations == []
    assert rows == [("changes", "skipped", "skip allowed unconditionally")]


def test_an_allowance_never_excuses_a_failure() -> None:
    """Allowances are about skips only; a failed job stays a failure."""
    _rows, violations = gate.evaluate(_needs(changes="failure"), [gate.parse_allowance("changes")])
    assert violations == ["changes: result was 'failure'"]


def test_a_conditional_allowance_excuses_a_skip_when_the_output_matches() -> None:
    needs = _needs(changes=("success", {"deps": "false"}), unit_lockfile_freshness="skipped")
    rows, violations = gate.evaluate(
        needs, [gate.parse_allowance("unit-lockfile-freshness=changes.deps=false")]
    )
    assert violations == []
    assert rows[1] == (
        "unit-lockfile-freshness",
        "skipped",
        "skip allowed while changes.outputs.deps == 'false'",
    )


def test_a_conditional_allowance_refuses_a_skip_when_the_output_differs() -> None:
    """The filter said the job should run; a skip then is a broken ``if:``."""
    needs = _needs(changes=("success", {"deps": "true"}), unit_lockfile_freshness="skipped")
    _rows, violations = gate.evaluate(
        needs, [gate.parse_allowance("unit-lockfile-freshness=changes.deps=false")]
    )
    assert violations == [
        "unit-lockfile-freshness: skipped, but changes.outputs.deps is 'true', not 'false'"
    ]


@pytest.mark.parametrize(
    "dependency_entry",
    [
        {"result": "success", "outputs": {}},  # output absent
        {"result": "success"},  # no outputs object at all
        {"result": "success", "outputs": "deps=false"},  # outputs not an object
    ],
)
def test_a_conditional_allowance_refuses_a_skip_when_the_output_is_missing(
    dependency_entry: dict[str, object],
) -> None:
    needs: dict[str, dict[str, object]] = {
        "changes": dependency_entry,
        "unit-lockfile-freshness": {"result": "skipped", "outputs": {}},
    }
    _rows, violations = gate.evaluate(
        needs, [gate.parse_allowance("unit-lockfile-freshness=changes.deps=false")]
    )
    assert violations == [
        "unit-lockfile-freshness: skipped, but changes.outputs.deps is None, not 'false'"
    ]


def test_an_allowance_for_an_unneeded_job_is_a_policy_error() -> None:
    with pytest.raises(gate.PolicyError, match="does not need"):
        gate.evaluate(_needs(a="success"), [gate.parse_allowance("ghost")])


def test_an_allowance_reading_an_unneeded_dependency_is_a_policy_error() -> None:
    with pytest.raises(gate.PolicyError, match="reads 'ghost'"):
        gate.evaluate(_needs(a="skipped"), [gate.parse_allowance("a=ghost.deps=false")])


def test_a_duplicate_allowance_is_a_policy_error() -> None:
    with pytest.raises(gate.PolicyError, match="twice"):
        gate.evaluate(_needs(a="skipped"), [gate.parse_allowance("a"), gate.parse_allowance("a")])


# ─── render_table / write_step_summary ───────────────────────────────────────


def test_render_table_marks_passes_and_excused_skips_green_and_the_rest_red() -> None:
    table = gate.render_table(
        [
            ("a", "success", ""),
            ("b", "skipped", "skip allowed unconditionally"),
            ("c", "skipped", "skipped without an allowance"),
            ("d", "failure", "result was 'failure'"),
        ]
    )
    lines = table.splitlines()
    assert lines[:2] == ["| Job | Result | Note |", "|-----|--------|------|"]
    assert lines[2] == "| `a` | ✅ `success` |  |"
    assert lines[3] == "| `b` | ✅ `skipped` | skip allowed unconditionally |"
    assert lines[4] == "| `c` | ❌ `skipped` | skipped without an allowance |"
    assert lines[5] == "| `d` | ❌ `failure` | result was 'failure' |"


def test_step_summary_is_appended_when_github_provides_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("existing\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    gate.write_step_summary("| table |", "every needed job succeeded")
    assert summary.read_text(encoding="utf-8") == (
        "existing\n### Gate: every needed job succeeded\n\n| table |\n\n"
    )


def test_step_summary_is_skipped_outside_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.chdir(tmp_path)
    gate.write_step_summary("| table |", "verdict")
    assert list(tmp_path.iterdir()) == []


# ─── main ────────────────────────────────────────────────────────────────────


def test_main_exits_zero_when_every_job_passed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert _run(_needs(a="success", b="success")) == 0
    out, err = capsys.readouterr()
    assert "| `a` | ✅ `success` |" in out
    assert out.rstrip().endswith("Gate satisfied: 2 job(s) succeeded or were skipped by policy.")
    assert err == ""


def test_main_exits_one_and_annotates_each_failed_job(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    needs = _needs(a="success", b="failure", c="skipped", d="cancelled")
    assert _run(needs) == 1
    out, err = capsys.readouterr()
    assert "| `b` | ❌ `failure` |" in out
    assert "::error::b: result was 'failure'" in err
    assert "::error::c: skipped without an allowance" in err
    assert "::error::d: result was 'cancelled'" in err
    assert "::error::refusing a successful gate: 3 job(s) did not succeed" in err
    assert "### Gate: 3 job(s) did not succeed" in summary.read_text(encoding="utf-8")


def test_main_honours_allowances_end_to_end(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    needs = _needs(
        changes=("success", {"deps": "false"}),
        unit_lockfile_freshness="skipped",
        unit_fresh_install="skipped",
        unit_pytest_core="success",
    )
    code = _run(
        needs,
        "changes",
        "unit-lockfile-freshness=changes.deps=false",
        "unit-fresh-install=changes.deps=false",
    )
    assert code == 0
    out, _err = capsys.readouterr()
    assert (
        "| `unit-fresh-install` | ✅ `skipped` | skip allowed while changes.outputs.deps == 'false' |"
        in out
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--needs", "{}"], "needs nothing"),
        (["--needs", "nope"], "not valid JSON"),
        (["--needs", '{"a": {"result": "success"}}', "--allow-skipped", "a=b"], "expected JOB"),
        (["--needs", '{"a": {"result": "success"}}', "--allow-skipped", "ghost"], "does not need"),
    ],
)
def test_main_exits_two_for_a_misconfigured_gate(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken gate must be distinguishable from a failed job, and never a pass."""
    assert gate.main(argv) == 2
    _out, err = capsys.readouterr()
    assert err.startswith("::error::gate misconfigured: ")
    assert message in err


def test_needs_is_required() -> None:
    with pytest.raises(SystemExit) as excinfo:
        gate.main([])
    assert excinfo.value.code == 2


def test_script_runs_as_a_file(tmp_path: Path) -> None:
    """The workflow invokes it with the system ``python3``: stdlib only, no package."""
    import subprocess

    needs = json.dumps(_needs(a="success"))
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--needs", needs],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    assert "Gate satisfied: 1 job(s)" in completed.stdout
