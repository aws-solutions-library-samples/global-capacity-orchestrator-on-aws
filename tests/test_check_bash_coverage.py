"""Tests for the shell-coverage gate (``.github/scripts/check_bash_coverage.py``).

The gate turns a bashcov/SimpleCov resultset into a pass/fail verdict on the
repository's shell scripts. Two very different things can go wrong with it, so
this module covers both:

1. **The decision logic.** Every judgement the script makes is exercised
   directly against synthetic resultsets — path mapping, hit merging, the two
   lexer corrections, and each failure mode. The interesting cases are the
   ones where a wrong answer would read as success: a report scoped to the
   wrong root, a script no suite executes, or a resultset whose shape changed
   under a gem bump. All three must fail closed, and each has a test here.

2. **The floor policy.** Until every script reached 100% the climb was staged
   through ``[tool.bash-coverage] ratchet`` in ``pyproject.toml``, a
   shrink-only list of not-yet-covered scripts the checker excused. The list
   emptied and was deleted, and the checker no longer reads any exclusion
   list. The policy tests at the bottom keep it that way: the section must not
   come back, and every tracked script must be one the gate can fail.

Neither Ruby nor bats is needed: ``evaluate()`` and the parsing helpers take
plain data, so the whole decision surface is reachable from Python. The real
bashcov invocation is covered by the ``unit:bats:shell`` job.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess  # nosec B404 - fixed argv, no shell: builds a throwaway git repo
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_bash_coverage.py"

_spec = importlib.util.spec_from_file_location("check_bash_coverage", _SCRIPT)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_bash_coverage", checker)
_spec.loader.exec_module(checker)

GIT = "/usr/bin/git"


def _resultset(coverage: dict[str, object], command: str = "bats tests/BATS/") -> dict[str, object]:
    """Build a SimpleCov resultset payload around ``coverage``."""
    return {command: {"coverage": coverage, "timestamp": 1_700_000_000}}


def _lines(*hits: int | None) -> dict[str, list[int | None]]:
    """SimpleCov's per-file shape: a list indexed by line number minus one."""
    return {"lines": list(hits)}


def _write_report(tmp_path: Path, payload: object, name: str = ".resultset.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fake_repo(tmp_path: Path, scripts: dict[str, str]) -> Path:
    """Create a throwaway git repo so ``git ls-files`` has something to list."""
    root = tmp_path / "repo"
    root.mkdir()
    for relative, body in scripts.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    for argv in (
        [GIT, "init", "-q", "."],
        [GIT, "config", "user.email", "t@example.com"],
        [GIT, "config", "user.name", "t"],
        [GIT, "add", "-A"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)  # nosec B603
    return root


# --------------------------------------------------------------------------
# ScriptCoverage
# --------------------------------------------------------------------------


def test_script_coverage_reports_missed_lines_and_percent() -> None:
    record = checker.ScriptCoverage(path="a.sh", hits={1: 3, 2: 0, 3: 1, 4: 0})
    assert record.total_lines == 4
    assert record.missed_lines == [2, 4]
    assert record.percent == pytest.approx(50.0)


def test_script_coverage_without_hits_is_unmeasured_not_covered() -> None:
    """The distinction the report depends on: no data is not the same as 100%."""
    record = checker.ScriptCoverage(path="a.sh")
    assert record.measured is False
    assert record.percent == pytest.approx(100.0)  # vacuous: no relevant lines
    record.sources.add("/w/a.sh")
    assert record.measured is True


# --------------------------------------------------------------------------
# find_report
# --------------------------------------------------------------------------


def test_find_report_accepts_the_resultset_directly(tmp_path: Path) -> None:
    report = _write_report(tmp_path, _resultset({}))
    assert checker.find_report(report) == report


def test_find_report_accepts_the_coverage_directory(tmp_path: Path) -> None:
    report = _write_report(tmp_path, _resultset({}))
    assert checker.find_report(tmp_path) == report


def test_find_report_searches_one_level_down(tmp_path: Path) -> None:
    """bashcov can nest its output when several commands are merged."""
    nested = tmp_path / "coverage"
    nested.mkdir()
    report = _write_report(nested, _resultset({}))
    assert checker.find_report(tmp_path) == report


def test_find_report_rejects_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(checker.ReportError, match="does not exist"):
        checker.find_report(tmp_path / "absent")


def test_find_report_rejects_a_directory_with_no_resultset(tmp_path: Path) -> None:
    with pytest.raises(checker.ReportError, match="no .resultset.json"):
        checker.find_report(tmp_path)


# --------------------------------------------------------------------------
# parse_report
# --------------------------------------------------------------------------


def test_parse_report_reads_hits_and_skips_non_executable_lines(tmp_path: Path) -> None:
    report = _write_report(tmp_path, _resultset({"/w/a.sh": _lines(None, 2, 0, None, 5)}))
    assert checker.parse_report(report) == {"/w/a.sh": {2: 2, 3: 0, 5: 5}}


def test_parse_report_accepts_a_bare_line_array(tmp_path: Path) -> None:
    """Older SimpleCov payloads store the array without a "lines" wrapper."""
    report = _write_report(tmp_path, _resultset({"/w/a.sh": [1, None, 0]}))
    assert checker.parse_report(report) == {"/w/a.sh": {1: 1, 3: 0}}


def test_parse_report_merges_commands_by_highest_hit_count(tmp_path: Path) -> None:
    """A script run by several suites is credited with all of them."""
    payload = {
        "bats one": {"coverage": {"/w/a.sh": _lines(0, 1)}},
        "bats two": {"coverage": {"/w/a.sh": _lines(4, 0)}},
    }
    report = _write_report(tmp_path, payload)
    assert checker.parse_report(report) == {"/w/a.sh": {1: 4, 2: 1}}


def test_parse_report_ignores_non_shell_files(tmp_path: Path) -> None:
    """bashcov also traces bats-core's own Bash; only *.sh is in scope."""
    coverage = {"/usr/lib/bats-core/tracing.bash": _lines(1), "/w/a.sh": _lines(1)}
    report = _write_report(tmp_path, _resultset(coverage))
    assert list(checker.parse_report(report)) == ["/w/a.sh"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"cmd": "not-an-object"}, {}, id="command-not-an-object"),
        pytest.param({"cmd": {"coverage": []}}, {}, id="coverage-not-an-object"),
        pytest.param({"cmd": {}}, {}, id="coverage-key-absent"),
        pytest.param({"cmd": {"coverage": {"/w/a.sh": {}}}}, {}, id="lines-absent"),
        pytest.param({"cmd": {"coverage": {"/w/a.sh": "nope"}}}, {}, id="lines-not-a-list"),
    ],
)
def test_parse_report_tolerates_unexpected_shapes(
    tmp_path: Path, payload: dict[str, object], expected: dict[str, dict[int, int]]
) -> None:
    """A gem bump must not crash the gate, and must not silently pass it either.

    Anything unrecognised yields no hits, which leaves the affected script
    looking unmeasured — and an unmeasured enforced script is a failure.
    """
    assert checker.parse_report(_write_report(tmp_path, payload)) == expected


def test_parse_report_rejects_unparseable_json(tmp_path: Path) -> None:
    report = tmp_path / ".resultset.json"
    report.write_text("{not json", encoding="utf-8")
    with pytest.raises(checker.ReportError, match="could not parse"):
        checker.parse_report(report)


def test_parse_report_rejects_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(checker.ReportError, match="could not parse"):
        checker.parse_report(tmp_path / "absent.json")


def test_parse_report_rejects_a_json_document_that_is_not_a_resultset(tmp_path: Path) -> None:
    with pytest.raises(checker.ReportError, match="not a SimpleCov resultset"):
        checker.parse_report(_write_report(tmp_path, ["a", "list"]))


# --------------------------------------------------------------------------
# map_to_tracked
# --------------------------------------------------------------------------


def test_map_to_tracked_matches_exact_and_suffix_paths() -> None:
    inventory = ["demo/lib_demo.sh"]
    assert checker.map_to_tracked("demo/lib_demo.sh", inventory) == "demo/lib_demo.sh"
    assert checker.map_to_tracked("/w/demo/lib_demo.sh", inventory) == "demo/lib_demo.sh"


def test_map_to_tracked_prefers_the_longest_suffix() -> None:
    """With nested candidates the most specific must win, not the first seen."""
    inventory = ["b/c.sh", "a/b/c.sh"]
    assert checker.map_to_tracked("/w/a/b/c.sh", inventory) == "a/b/c.sh"


def test_map_to_tracked_normalises_windows_separators() -> None:
    assert checker.map_to_tracked(r"C:\w\demo\a.sh", ["demo/a.sh"]) == "demo/a.sh"


def test_map_to_tracked_falls_back_to_a_unique_basename() -> None:
    """Covers a fixture copy whose directory layout does not match the repo."""
    assert checker.map_to_tracked("/tmp/xyz/a.sh", ["demo/a.sh"]) == "demo/a.sh"


def test_map_to_tracked_refuses_an_ambiguous_basename() -> None:
    """Guessing between two same-named scripts would credit the wrong one."""
    assert checker.map_to_tracked("/tmp/xyz/a.sh", ["one/a.sh", "two/a.sh"]) is None


def test_map_to_tracked_returns_none_for_an_unrelated_path() -> None:
    assert checker.map_to_tracked("/usr/share/other.sh", ["demo/a.sh"]) is None


# --------------------------------------------------------------------------
# untraceable_lines
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('  done < "$exclude_file"', id="done-file"),
        pytest.param('  done <<< "$BUILD_SYSTEM_PINS"', id="done-here-string"),
        pytest.param('    done < "$NPM_RESULTS" > "$npm_disp"', id="done-in-and-out"),
        pytest.param('  } > "$report_path"', id="group-out"),
        pytest.param('    } >> "$GITHUB_OUTPUT"', id="group-append"),
        pytest.param("  } 2>/dev/null", id="group-stderr-bare-word"),
        pytest.param("  fi < input.txt  # trailing comment", id="fi-with-comment"),
        pytest.param("    */*) ;;", id="empty-arm-glob"),
        pytest.param("    https://github.com/*) ;;", id="empty-arm-url"),
        pytest.param("        /*) ;;", id="empty-arm-slash"),
    ],
)
def test_untraceable_lines_recognises_terminators_and_empty_arms(line: str) -> None:
    """The shapes bashcov's lexer marks executable but `set -x` never prints.

    Each is copied from a tracked script; bashcov reports the redirection-only
    terminators and empty case arms as 0 hits no matter what runs.
    """
    assert checker.untraceable_lines(f"echo before\n{line}\necho after\n") == {2}


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("  } | sed '/^$/d' | sort -u", id="group-piped-into-a-command"),
        pytest.param(
            '  done < <(extract_companion_mcp_packages "$AUTOPILOT_SOURCE")',
            id="process-substitution",
        ),
        pytest.param("    *.gif) : ;;", id="arm-with-null-command"),
        pytest.param('    docker|finch|podman) RUNTIME="$1"; shift ;;', id="arm-with-body"),
        pytest.param("  done", id="bare-done"),
        pytest.param("  }", id="bare-brace"),
        pytest.param("    ;;", id="bare-terminator"),
        pytest.param("  echo done < file", id="command-named-like-a-keyword"),
        pytest.param("  done_flag=1 > out", id="identifier-starting-with-done"),
    ],
)
def test_untraceable_lines_leaves_measurable_lines_alone(line: str) -> None:
    """A command on the line — piped, substituted or a bare `:` — is traced."""
    assert checker.untraceable_lines(f"echo before\n{line}\necho after\n") == set()


def test_untraceable_lines_matches_the_committed_scripts() -> None:
    """The real inventory: every match is one of the two shapes and nothing else.

    A regression here would show up in the gate as a permanently uncovered
    line (too narrow) or a silently excused statement (too wide), so the
    classification is checked against the actual tree, not only fixtures.
    """
    inventory = checker.tracked_shell_scripts(REPO_ROOT)
    found, _spans = checker.classify_scripts(REPO_ROOT, inventory)
    assert found, "the repository is expected to carry redirected loop terminators"
    for path, numbers in found.items():
        source = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
        for number in numbers:
            text = source[number - 1].strip()
            assert text.startswith(("done", "fi", "esac", "}")) or text.endswith(";;"), (
                f"{path}:{number} classified as untraceable but looks like a statement: {text!r}"
            )


# --------------------------------------------------------------------------
# statement_spans
# --------------------------------------------------------------------------
#
# Each expected span was checked against what `bash -x` prints for the shape,
# with PS4 exposing LINENO: Bash reports the whole statement on ONE of its
# lines (the first, the second or the last depending on the shape), so the
# checker folds the span rather than guessing which line that is.

BACKSLASH_CHAIN = """\
aws eks create-access-entry \\
  --cluster-name "$CLUSTER_NAME" \\
  --region "$REGION" \\
  --principal-arn "$PRINCIPAL_ARN" 2>&1 || echo "   Access entry may already exist"
echo done
"""

SUBSTITUTION_ASSIGNMENT = """\
API_ENDPOINT=$(aws cloudformation describe-stacks \\
  --stack-name "$STACK_NAME" \\
  --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' \\
  --output text)
API_ENDPOINT=${API_ENDPOINT%/}
"""

HEREDOC_IN_SUBSTITUTION = """\
MANIFEST_PAYLOAD=$(cat <<'EOF'
{
  "manifests": [
    {"kind": "Job", "spec": {"parallelism": (1)}}
  ]
}
EOF
)
echo "$MANIFEST_PAYLOAD" | jq '.'
"""

MULTILINE_STRING_COMMAND = """\
python3 -c "
import re, sys
m = re.search(r'^VERSION\\s*=\\s*\\"([^\\"]+)\\"', open(sys.argv[1]).read())
print(m.group(1) if m else '')
" "$file" 2>/dev/null
echo after
"""

BACKGROUND_CHAIN = """\
aws-sigv4-proxy \\
  --name execute-api \\
  --region "$API_REGION" \\
  --log-level info &
PROXY_PID=$!
"""

CONTINUED_LIST = """\
[ -n "$count" ] \\
    && [ -n "$PID" ] \\
    && echo chained
"""

SUBSHELL_BLOCK = """\
(
    cd "$repo_root" || exit 1
    python3 -c 'import gco'
)
"""

ARRAY_ASSIGNMENT = """\
FORWARDED_ENV_VARS=(
    AWS_PROFILE
    AWS_REGION
)
echo "${FORWARDED_ENV_VARS[0]}"
"""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(BACKSLASH_CHAIN, [(1, 3)], id="backslash-chain-split-at-the-fallback"),
        pytest.param(SUBSTITUTION_ASSIGNMENT, [(1, 4)], id="substitution-assignment"),
        pytest.param(HEREDOC_IN_SUBSTITUTION, [(1, 8)], id="heredoc-inside-substitution"),
        pytest.param(
            MULTILINE_STRING_COMMAND, [(1, 5)], id="multi-line-string-with-escaped-quotes"
        ),
        pytest.param(BACKGROUND_CHAIN, [(1, 4)], id="backgrounded-chain"),
        pytest.param(CONTINUED_LIST, [], id="continued-list-each-element-reported-alone"),
        pytest.param(SUBSHELL_BLOCK, [], id="subshell-inner-lines-reported-alone"),
        pytest.param(ARRAY_ASSIGNMENT, [(1, 4)], id="array-assignment"),
        pytest.param("echo one\necho two\n", [], id="single-line-statements"),
        pytest.param(
            "case $x in\n  a) echo a ;;\n  *) ;;\nesac\n", [], id="case-arms-are-not-openers"
        ),
        pytest.param(
            "echo \"it's\" # don't\necho next\n", [], id="apostrophes-in-strings-and-comments"
        ),
        pytest.param(
            "cat <<-EOT\n\tbody\n\tEOT\necho after\n", [(1, 3)], id="dash-heredoc-strips-tabs"
        ),
    ],
)
def test_statement_spans_follow_bash_statement_boundaries(
    source: str, expected: list[tuple[int, int]]
) -> None:
    assert checker.statement_spans(source) == expected


def test_statement_spans_report_an_unterminated_statement_to_the_end() -> None:
    """A truncated file still yields a well-formed span rather than an error."""
    assert checker.statement_spans("VAR=$(cat <<EOF\nnever closed\n") == [(1, 2)]


def test_fold_spans_credits_the_statement_wherever_bash_reported_it() -> None:
    hits = {1: 0, 2: 0, 3: 0, 4: 1, 5: 2}  # the assignment reported on its last line
    assert checker.fold_spans(hits, [(1, 4)]) == {1: 1, 5: 2}


def test_fold_spans_leaves_an_unreported_span_absent() -> None:
    """Lines bashcov itself judged non-executable must not reappear as covered."""
    assert checker.fold_spans({7: 1}, [(1, 4)]) == {7: 1}


def test_statement_spans_over_the_committed_scripts_are_sane() -> None:
    """Spans never overlap, never run backwards, and cover the known shapes."""
    inventory = checker.tracked_shell_scripts(REPO_ROOT)
    _untraceable, spans = checker.classify_scripts(REPO_ROOT, inventory)
    assert "docs/client-examples/aws_cli_examples.sh" in spans
    for path, found in spans.items():
        previous_end = 0
        for first, last in found:
            assert first > previous_end, f"{path}: overlapping spans around line {first}"
            assert last > first, f"{path}: degenerate span {first}-{last}"
            previous_end = last


# --------------------------------------------------------------------------
# evaluate
# --------------------------------------------------------------------------


def test_evaluate_drops_untraceable_lines_from_the_count() -> None:
    """A redirected `done` at 0 hits must not fail the script it structures."""
    reported = {"/w/a.sh": {1: 1, 2: 0, 3: 1}}
    failing = checker.evaluate(reported, ["a.sh"])
    assert failing.ok is False
    passing = checker.evaluate(reported, ["a.sh"], {"a.sh": {2}})
    assert passing.ok is True
    assert passing.scripts[0].total_lines == 2


def test_evaluate_folds_multi_line_statements_onto_their_first_line() -> None:
    """A `VAR=$(...)` reported on its last line counts once, as covered."""
    reported = {"/w/a.sh": {1: 0, 2: 0, 3: 1, 4: 1}}
    failing = checker.evaluate(reported, ["a.sh"])
    assert failing.ok is False
    passing = checker.evaluate(reported, ["a.sh"], spans={"a.sh": [(1, 3)]})
    assert passing.ok is True
    assert passing.scripts[0].hits == {1: 1, 4: 1}


def test_evaluate_passes_a_fully_covered_script() -> None:
    result = checker.evaluate({"/w/a.sh": {1: 1, 2: 3}}, ["a.sh"])
    assert result.ok is True
    assert result.failures == []
    assert [record.path for record in result.scripts] == ["a.sh"]


def test_evaluate_fails_a_script_with_uncovered_lines() -> None:
    result = checker.evaluate({"/w/a.sh": {1: 1, 2: 0}}, ["a.sh"])
    assert result.ok is False
    assert "1/2 lines uncovered" in result.failures[0]


def test_evaluate_fails_a_script_absent_from_the_report() -> None:
    """The fail-closed case: a mis-scoped bashcov run must not read as success."""
    result = checker.evaluate({}, ["a.sh"])
    assert result.ok is False
    assert "absent from the bashcov report" in result.failures[0]


def test_evaluate_holds_every_script_in_the_inventory_to_the_floor() -> None:
    """There is no exclusion list: each uncovered or absent script is its own failure."""
    reported = {"/w/a.sh": {1: 1}, "/w/b.sh": {1: 0}}
    result = checker.evaluate(reported, ["a.sh", "b.sh", "c.sh"])
    assert [record.path for record in result.scripts] == ["a.sh", "b.sh", "c.sh"]
    assert [failure.split(":")[0] for failure in result.failures] == ["b.sh", "c.sh"]


def test_evaluate_merges_every_path_that_maps_to_one_script() -> None:
    """Two absolute prefixes for the same file must not read as half-covered."""
    reported = {"/w/a.sh": {1: 1, 2: 0}, "/tmp/fixture/a.sh": {1: 0, 2: 7}}
    result = checker.evaluate(reported, ["a.sh"])
    assert result.ok is True
    assert result.scripts[0].sources == {"/w/a.sh", "/tmp/fixture/a.sh"}


def test_evaluate_collects_paths_that_map_to_nothing() -> None:
    result = checker.evaluate({"/opt/vendor/x.sh": {1: 1}}, ["a.sh"])
    assert result.unmapped == ["/opt/vendor/x.sh"]


# --------------------------------------------------------------------------
# tracked_shell_scripts
# --------------------------------------------------------------------------


def test_tracked_shell_scripts_lists_the_repository_and_excludes_the_suite() -> None:
    scripts = checker.tracked_shell_scripts(REPO_ROOT)
    assert scripts == sorted(scripts)
    assert "demo/gif_to_mp4.sh" in scripts
    assert not [path for path in scripts if path.startswith("tests/")], (
        "the BATS suite's own helpers are not subjects of the gate"
    )


def test_tracked_shell_scripts_rejects_a_non_repository(tmp_path: Path) -> None:
    with pytest.raises(checker.ReportError, match="could not list tracked shell scripts"):
        checker.tracked_shell_scripts(tmp_path)


def test_classify_scripts_rejects_a_tracked_script_it_cannot_read(tmp_path: Path) -> None:
    """A tracked script missing from the working tree is a broken measurement (exit 2)."""
    root = _fake_repo(tmp_path, {"a.sh": "echo hi\n"})
    inventory = checker.tracked_shell_scripts(root)
    assert inventory == ["a.sh"]
    (root / "a.sh").unlink()
    with pytest.raises(checker.ReportError, match="could not read tracked script a.sh"):
        checker.classify_scripts(root, inventory)


# --------------------------------------------------------------------------
# format_report
# --------------------------------------------------------------------------


def test_format_report_summarises_a_passing_run() -> None:
    result = checker.evaluate({"/w/a.sh": {1: 1}}, ["a.sh"])
    assert "1/1 tracked scripts at 100%" in checker.format_report(result)


def test_format_report_never_renders_an_unmeasured_script_as_a_percentage() -> None:
    """A script no suite executed is a named failure, not a number."""
    reported = {"/w/a.sh": {1: 1}, "/w/b.sh": {1: 1, 2: 0}}
    result = checker.evaluate(reported, ["a.sh", "b.sh", "c.sh"])
    rendered = checker.format_report(result)
    assert "1/3 tracked scripts at 100%" in rendered
    assert "b.sh: 1/2 lines uncovered (50.00%)" in rendered
    assert "c.sh: absent from the bashcov report" in rendered
    assert "%" not in rendered.split("c.sh: absent", 1)[1].split("\n", 1)[0]


def test_format_report_truncates_a_long_miss_list() -> None:
    hits = dict.fromkeys(range(1, 31), 0)
    result = checker.evaluate({"/w/a.sh": hits}, ["a.sh"])
    rendered = checker.format_report(result)
    assert "(+10 more)" in rendered
    assert "ERROR: shell scripts are not fully covered" in rendered
    assert "ratchet" not in rendered, "the remedy must not point at a list that no longer exists"


def test_format_report_notes_unmapped_paths() -> None:
    reported = {f"/opt/vendor/x{index}.sh": {1: 1} for index in range(12)}
    result = checker.evaluate(reported, ["a.sh"])
    rendered = checker.format_report(result)
    assert "12 reported path(s) matched no tracked script" in rendered


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def test_main_returns_zero_when_the_floor_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path, {"a.sh": "echo hi\n"})
    report = _write_report(tmp_path, _resultset({str(root / "a.sh"): _lines(1)}))
    assert checker.main([str(report), "--root", str(root)]) == 0
    assert "1/1 tracked scripts at 100%" in capsys.readouterr().out


def test_main_returns_one_when_a_script_is_uncovered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path, {"a.sh": "echo hi\n"})
    report = _write_report(tmp_path, _resultset({str(root / "a.sh"): _lines(0)}))
    assert checker.main([str(report), "--root", str(root)]) == 1
    assert "1/1 lines uncovered" in capsys.readouterr().out


def test_main_returns_one_when_a_tracked_script_is_not_in_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second script the report never mentions fails the run on its own."""
    root = _fake_repo(tmp_path, {"a.sh": "echo hi\n", "lib/b.sh": "echo there\n"})
    report = _write_report(tmp_path, _resultset({str(root / "a.sh"): _lines(1)}))
    assert checker.main([str(report), "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "1/2 tracked scripts at 100%" in out
    assert "lib/b.sh: absent from the bashcov report" in out


def test_main_returns_two_when_the_report_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A distinct exit code so CI can tell "not covered" from "never ran"."""
    root = _fake_repo(tmp_path, {"a.sh": "echo hi\n"})
    assert checker.main([str(tmp_path / "absent"), "--root", str(root)]) == 2
    assert "ERROR:" in capsys.readouterr().err


def test_main_defaults_the_root_to_the_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --root the gate measures this repository, and all of it.

    A report covering exactly the tracked inventory is enough to pass, and
    nothing less is: dropping any one script from the report fails the run,
    which is the property the retired ratchet used to weaken.
    """
    inventory = checker.tracked_shell_scripts(REPO_ROOT)
    assert inventory, "the gate is meaningless without tracked scripts"
    full = {f"/w/{path}": _lines(1) for path in inventory}
    report = _write_report(tmp_path, _resultset(full))
    assert checker.main([str(report)]) == 0
    out = capsys.readouterr().out
    assert f"{len(inventory)}/{len(inventory)} tracked scripts at 100%" in out
    assert "ERROR" not in out

    dropped = inventory[-1]
    partial = {path: lines for path, lines in full.items() if path != f"/w/{dropped}"}
    report = _write_report(tmp_path, _resultset(partial), name="partial.json")
    assert checker.main([str(report)]) == 1
    assert f"{dropped}: absent from the bashcov report" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The floor policy: no exclusion list, in the checker or in pyproject.toml
# --------------------------------------------------------------------------

RETIRED_SECTION = "bash-coverage"


def test_ratchet_section_has_not_been_reintroduced() -> None:
    """The not-yet-covered list emptied and was deleted; it must not come back.

    The checker ignores pyproject.toml entirely now, so a revived section would
    excuse nothing — but it would *look* as if it did, and the next person
    would list their new script there instead of covering it.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert RETIRED_SECTION not in data.get("tool", {}), (
        f"pyproject.toml carries a [tool.{RETIRED_SECTION}] section again: every tracked "
        "shell script is at 100%, so cover the new script instead of listing it"
    )


def test_checker_takes_no_exclusion_list() -> None:
    """``evaluate`` has no parameter that could excuse a script from the floor."""
    parameters = set(inspect.signature(checker.evaluate).parameters)
    assert parameters == {"reported", "inventory", "untraceable", "spans"}, (
        f"evaluate() grew a parameter: {sorted(parameters)}. A per-script exclusion "
        "belongs in a test that covers the script, not in the gate"
    )
    assert not hasattr(checker, "load_ratchet")


def test_every_tracked_script_can_fail_the_gate() -> None:
    """With an empty report, each tracked script is its own named failure."""
    inventory = checker.tracked_shell_scripts(REPO_ROOT)
    result = checker.evaluate({}, inventory)
    assert [failure.split(":", 1)[0] for failure in result.failures] == inventory


def test_script_basenames_are_unique_so_the_fallback_is_safe() -> None:
    """``map_to_tracked`` may match on basename alone; that needs uniqueness."""
    inventory = checker.tracked_shell_scripts(REPO_ROOT)
    basenames = [path.rsplit("/", 1)[-1] for path in inventory]
    duplicates = sorted({name for name in basenames if basenames.count(name) > 1})
    assert not duplicates, (
        "two tracked scripts share a basename, so a fixture copy could be "
        f"credited to the wrong one: {duplicates}"
    )
