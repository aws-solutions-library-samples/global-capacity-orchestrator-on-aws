#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for .github/scripts/run-semgrep.sh
# ─────────────────────────────────────────────────────────────────────────────
# The wrapper runs `semgrep scan` with repo-wide rule suppressions loaded from
# .github/config/semgrep-excluded-rules.txt — each non-comment, non-blank line
# becomes a `--exclude-rule` flag. These tests stub `semgrep` on PATH so the
# real engine never runs: the stub records the exact argv it was handed (one
# token per line) and the tests assert on the flags the wrapper built.
#
# Run:  bats tests/BATS/test_run_semgrep.bats
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT=".github/scripts/run-semgrep.sh"

setup() {
    # Stub `semgrep` that records its argv and exits 0, so the wrapper's
    # `exec semgrep ...` is intercepted without invoking the real engine.
    STUB_BIN="$BATS_TEST_TMPDIR/bin"
    mkdir -p "$STUB_BIN"
    export SEMGREP_ARGS_OUT="$BATS_TEST_TMPDIR/semgrep_args"
    cat > "$STUB_BIN/semgrep" <<'STUB'
#!/bin/sh
printf '%s\n' "$@" > "$SEMGREP_ARGS_OUT"
STUB
    chmod +x "$STUB_BIN/semgrep"
    export PATH="$STUB_BIN:$PATH"
}

# ── Static checks ────────────────────────────────────────────────────────────

@test "run-semgrep.sh passes sh -n syntax check" {
    sh -n "$SCRIPT"
}

@test "run-semgrep.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

# ── Exclude-rule expansion ───────────────────────────────────────────────────

@test "one --exclude-rule per non-comment, non-blank line" {
    printf '# header comment\n\nrule.one\nrule.two\n' > "$BATS_TEST_TMPDIR/excl.txt"
    SEMGREP_EXCLUDE_RULES_FILE="$BATS_TEST_TMPDIR/excl.txt" run sh "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(grep -ce '--exclude-rule' "$SEMGREP_ARGS_OUT")" -eq 2 ]
    grep -qx 'rule.one' "$SEMGREP_ARGS_OUT"
    grep -qx 'rule.two' "$SEMGREP_ARGS_OUT"
}

@test "comment and blank lines produce no --exclude-rule flags" {
    printf '# only comments\n\n   \n# another\n' > "$BATS_TEST_TMPDIR/excl.txt"
    SEMGREP_EXCLUDE_RULES_FILE="$BATS_TEST_TMPDIR/excl.txt" run sh "$SCRIPT"
    [ "$status" -eq 0 ]
    ! grep -qe '--exclude-rule' "$SEMGREP_ARGS_OUT"
}

@test "an inline comment after a rule id is stripped to the id" {
    printf 'rule.alpha  # keep only the id\n' > "$BATS_TEST_TMPDIR/excl.txt"
    SEMGREP_EXCLUDE_RULES_FILE="$BATS_TEST_TMPDIR/excl.txt" run sh "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(grep -ce '--exclude-rule' "$SEMGREP_ARGS_OUT")" -eq 1 ]
    grep -qx 'rule.alpha' "$SEMGREP_ARGS_OUT"
    ! grep -q 'keep' "$SEMGREP_ARGS_OUT"
}

@test "a missing suppression file yields no excludes but still runs the scan" {
    SEMGREP_EXCLUDE_RULES_FILE="$BATS_TEST_TMPDIR/does-not-exist.txt" run sh "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"no suppression file"* ]]
    ! grep -qe '--exclude-rule' "$SEMGREP_ARGS_OUT"
    grep -qx 'scan' "$SEMGREP_ARGS_OUT"
}

@test "the committed default suppression file excludes mutable-action-tag" {
    run sh "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q 'github-actions-mutable-action-tag' "$SEMGREP_ARGS_OUT"
}

# ── Argument assembly ────────────────────────────────────────────────────────

@test "caller-supplied extra args are forwarded to semgrep" {
    printf 'rule.one\n' > "$BATS_TEST_TMPDIR/excl.txt"
    SEMGREP_EXCLUDE_RULES_FILE="$BATS_TEST_TMPDIR/excl.txt" run sh "$SCRIPT" --verbose
    [ "$status" -eq 0 ]
    grep -qxe '--verbose' "$SEMGREP_ARGS_OUT"
}

@test "the assembled command scans the repo with JSON report output" {
    printf 'rule.one\n' > "$BATS_TEST_TMPDIR/excl.txt"
    SEMGREP_EXCLUDE_RULES_FILE="$BATS_TEST_TMPDIR/excl.txt" run sh "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(head -1 "$SEMGREP_ARGS_OUT")" = "scan" ]
    grep -qxe '--config' "$SEMGREP_ARGS_OUT"
    grep -qx 'auto' "$SEMGREP_ARGS_OUT"
    grep -qxe '--error' "$SEMGREP_ARGS_OUT"
    grep -qxe '-o' "$SEMGREP_ARGS_OUT"
    grep -qx 'semgrep-report.json' "$SEMGREP_ARGS_OUT"
    [ "$(tail -1 "$SEMGREP_ARGS_OUT")" = "." ]
}
