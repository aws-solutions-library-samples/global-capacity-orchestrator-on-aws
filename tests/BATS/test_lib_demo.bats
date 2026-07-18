#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for demo/lib_demo.sh
# ─────────────────────────────────────────────────────────────────────────────
# Covers the shared helpers used by the three record_*.sh scripts:
#   - sanitize_cast        (AWS-account/access-key redaction in .cast files)
#   - verify_recording_git_state (exact-SHA and dirty-path provenance guard)
#   - render_gif           (agg invocation with the shared font fallback)
#   - publish_recording_artifacts (paired publication with rollback)
#   - DEMO_FONT_FAMILY_*   (the default font chain for emoji coverage)
#   - ARN helpers          (is_assumed_role / extract_role_name / build_role_arn)
#
# These tests actually *source* lib_demo.sh and invoke the functions, so a
# regression in the helper implementation will be caught — grep-only tests
# would miss subtle logic bugs.
#
# Run:  bats tests/BATS/test_lib_demo.bats
# ─────────────────────────────────────────────────────────────────────────────

LIB="demo/lib_demo.sh"

setup() {
    # Fresh tmpdir per test so sanitize_cast in-place edits can't cross-pollinate.
    TEST_TMPDIR="$(mktemp -d)"
    # Source the library. `set -u` in BATS is fine — lib_demo.sh sets the
    # colour variables unconditionally inside setup_colors, but we don't
    # call setup_colors here to keep output quiet.
    # shellcheck source=demo/lib_demo.sh disable=SC1091
    source "$LIB"
}

teardown() {
    [ -n "${TEST_TMPDIR:-}" ] && [ -d "$TEST_TMPDIR" ] && rm -rf "$TEST_TMPDIR"
}

# ── File Sanity ──────────────────────────────────────────────────────────────

@test "lib_demo.sh passes bash -n syntax check" {
    bash -n "$LIB"
}

@test "lib_demo.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck "$LIB"
}

@test "verify_recording_git_state rejects a non-allowlisted rename source" {
    local repo="$TEST_TMPDIR/repo"
    mkdir -p "$repo/demo"
    git -C "$repo" init -q
    printf 'tracked source\n' > "$repo/source.txt"
    git -C "$repo" add source.txt
    git -C "$repo" -c user.name=CI -c user.email=ci@example.invalid \
        commit -q -m initial
    local sha
    sha=$(git -C "$repo" rev-parse HEAD)
    git -C "$repo" mv source.txt demo/deploy.cast

    GCO_EXPECTED_GIT_SHA="$sha"
    export GCO_EXPECTED_GIT_SHA
    run verify_recording_git_state "$repo" "demo/deploy.cast"
    unset GCO_EXPECTED_GIT_SHA

    [ "$status" -ne 0 ]
    [[ "$output" == *"source.txt"* ]]
}

# ── sanitize_cast ────────────────────────────────────────────────────────────

@test "sanitize_cast replaces a single 12-digit account ID with zeros" {
    local cast="$TEST_TMPDIR/sample.cast"
    printf '[0.0, "o", "arn:aws:eks:us-east-1:123456789012:cluster/test"]\n' > "$cast"

    sanitize_cast "$cast"

    grep -q "000000000000" "$cast"
    run grep -q "123456789012" "$cast"
    [ "$status" -ne 0 ]
}

@test "sanitize_cast replaces every occurrence across many lines" {
    local cast="$TEST_TMPDIR/sample.cast"
    {
        printf '[0.0, "o", "arn:aws:sqs:us-east-1:111122223333:q1"]\n'
        printf '[0.1, "o", "arn:aws:s3:::bucket-444455556666"]\n'
        printf '[0.2, "o", "111122223333.dkr.ecr.us-east-1.amazonaws.com/repo:tag"]\n'
        printf '[0.3, "o", "arn:aws:iam::999988887777:role/gco-role"]\n'
    } > "$cast"

    sanitize_cast "$cast"

    # Each of the three account IDs should be gone.
    run grep -q "111122223333" "$cast"
    [ "$status" -ne 0 ]
    run grep -q "444455556666" "$cast"
    [ "$status" -ne 0 ]
    run grep -q "999988887777" "$cast"
    [ "$status" -ne 0 ]
    # Four occurrences total (111122223333 appears twice) → four redactions.
    [ "$(grep -c '000000000000' "$cast")" -eq 4 ]
}

@test "sanitize_cast redacts identifiers split across output events" {
    local cast="$TEST_TMPDIR/split.cast"
    {
        printf '{"version":2,"width":80,"height":24}\n'
        printf '[0.1,"o","account 123456"]\n'
        printf '[0.2,"o","789012 key AKIAABCDEFGH"]\n'
        printf '[0.3,"o","IJKLMNOP done"]\n'
    } > "$cast"

    sanitize_cast "$cast"
    verify_cast_sanitized "$cast"

    python3 - "$cast" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    documents = [json.loads(line) for line in stream]
rendered = "".join(
    document[2]
    for document in documents
    if isinstance(document, list) and len(document) >= 3 and document[1] == "o"
)
assert len(documents) == 4
assert "000000000000" in rendered
assert "REDACTED_AWS_ACCESS_KEY_ID" in rendered
assert "123456789012" not in rendered
assert "".join(("AKIA", "ABCDEFGHIJKLMNOP")) not in rendered
PYEOF
}

@test "sanitize_cast redacts complete identifiers hidden by adjacent output" {
    local cast="$TEST_TMPDIR/adjacent.cast"
    local fake_access_key_id
    fake_access_key_id="$(printf '%s%s' 'AKIA' 'ABCDEFGHIJKLMNOP')"
    {
        printf '{"version":2,"width":80,"height":24}\n'
        printf '[0.1,"o","123456789012"]\n'
        printf '[0.2,"o","999988887777"]\n'
        printf '[0.3,"o","%s"]\n' "$fake_access_key_id"
        printf '[0.4,"o","Z"]\n'
    } > "$cast"

    sanitize_cast "$cast"
    verify_cast_sanitized "$cast"

    run grep -q "123456789012" "$cast"
    [ "$status" -ne 0 ]
    run grep -q "999988887777" "$cast"
    [ "$status" -ne 0 ]
    run grep -q "$fake_access_key_id" "$cast"
    [ "$status" -ne 0 ]
}

@test "verify_cast_sanitized rejects complete identifiers hidden by adjacent output" {
    local cast="$TEST_TMPDIR/adjacent-unsanitized.cast"
    local fake_access_key_id
    fake_access_key_id="$(printf '%s%s' 'AKIA' 'ABCDEFGHIJKLMNOP')"
    {
        printf '{"version":2,"width":80,"height":24}\n'
        printf '[0.1,"o","123456789012"]\n'
        printf '[0.2,"o","999988887777"]\n'
        printf '[0.3,"o","%s"]\n' "$fake_access_key_id"
        printf '[0.4,"o","Z"]\n'
    } > "$cast"

    run verify_cast_sanitized "$cast"

    [ "$status" -ne 0 ]
    [[ "$output" == *"2 account-ID pattern(s), 1 access-key-ID pattern(s)"* ]]
}

@test "verify_cast_sanitized rejects an unsanitized split output identifier" {
    local cast="$TEST_TMPDIR/split-unsanitized.cast"
    {
        printf '{"version":2,"width":80,"height":24}\n'
        printf '[0.1,"o","account 123456"]\n'
        printf '[0.2,"o","789012"]\n'
    } > "$cast"

    run verify_cast_sanitized "$cast"

    [ "$status" -ne 0 ]
    [[ "$output" == *"1 account-ID pattern(s)"* ]]
}

@test "sanitize_cast leaves short numbers (timestamps, counts) alone" {
    # We intentionally only redact exactly-12-digit sequences. Eleven-digit
    # unix timestamps or four-digit years should pass through unchanged.
    local cast="$TEST_TMPDIR/sample.cast"
    printf '[1729800000.123, "o", "Deployed 42 stacks in 1800 seconds (year 2026)"]\n' > "$cast"

    sanitize_cast "$cast"

    grep -q "1729800000" "$cast"
    grep -q "42 stacks" "$cast"
    grep -q "1800 seconds" "$cast"
    grep -q "year 2026" "$cast"
    run grep -q "000000000000" "$cast"
    [ "$status" -ne 0 ]
}

@test "sanitize_cast leaves longer numeric identifiers alone" {
    # AWS account IDs are exactly 12 standalone digits. A longer numeric run is
    # not account-ID-shaped and must not be partially rewritten into a new ID.
    local cast="$TEST_TMPDIR/sample.cast"
    printf '[0.0, "o", "1234567890123 is a 13-digit id"]\n' > "$cast"

    sanitize_cast "$cast"

    run grep -q "1234567890123 is" "$cast"
    [ "$status" -eq 0 ]
    run grep -q "000000000000" "$cast"
    [ "$status" -ne 0 ]
}

@test "sanitize_cast edits in place (not to stdout)" {
    local cast="$TEST_TMPDIR/sample.cast"
    printf '[0.0, "o", "arn:aws:eks:us-east-1:123456789012:cluster/test"]\n' > "$cast"

    # Get mtime portably. BSD stat (macOS) uses `stat -f %m`; GNU stat
    # (Linux) uses `stat -c %Y`. `stat -f` on GNU means "display filesystem
    # status" — a completely different command — so we must detect which
    # implementation we have rather than relying on `||` fallthrough, which
    # on Linux causes before_mtime to end up as a multi-line filesystem
    # report and the later `-gt` test to fail with "integer expression".
    _mtime() {
        if stat --version >/dev/null 2>&1; then
            stat -c %Y "$1"   # GNU stat (Linux)
        else
            stat -f %m "$1"   # BSD stat (macOS)
        fi
    }

    local before_mtime after_mtime
    before_mtime=$(_mtime "$cast")

    # Sleep 1s so mtime resolution catches the write.
    sleep 1
    run sanitize_cast "$cast"
    [ "$status" -eq 0 ]
    # Helper should not print anything on success.
    [ -z "$output" ]

    after_mtime=$(_mtime "$cast")
    [ "$after_mtime" -gt "$before_mtime" ]
}

@test "sanitize_cast skips quietly when the file doesn't exist" {
    # Record scripts call sanitize_cast unconditionally after asciinema. If
    # recording was aborted and no cast was produced, we shouldn't hard-fail.
    run sanitize_cast "$TEST_TMPDIR/does-not-exist.cast"
    [ "$status" -eq 0 ]
}

@test "SKIP_SANITIZE=1 bypasses redaction" {
    local cast="$TEST_TMPDIR/sample.cast"
    printf '[0.0, "o", "arn:aws:eks:us-east-1:123456789012:cluster/test"]\n' > "$cast"

    SKIP_SANITIZE=1 sanitize_cast "$cast"

    # Original ID remains intact.
    grep -q "123456789012" "$cast"
    run grep -q "000000000000" "$cast"
    [ "$status" -ne 0 ]
}

# ── render_gif ───────────────────────────────────────────────────────────────
# We can't verify that agg actually produces a valid GIF in CI (the tool isn't
# necessarily installed, and we don't want to ship test cast files large
# enough for a real render). Instead we stub `agg` with a bash function that
# records its argv, then assert the helper passed every expected flag.

@test "render_gif invokes agg with --speed, --theme, --font-family, --font-size, --cols, --rows" {
    local argv_file="$TEST_TMPDIR/agg.argv"
    agg() { printf '%s\n' "$@" > "$argv_file"; }
    export -f agg

    render_gif "cast.cast" "out.gif" "2" "monokai" "120" "37"

    [ -f "$argv_file" ]
    grep -qx -- "--speed" "$argv_file"
    grep -qx -- "--theme" "$argv_file"
    grep -qx -- "--font-family" "$argv_file"
    grep -qx -- "--font-size" "$argv_file"
    grep -qx -- "--cols" "$argv_file"
    grep -qx -- "--rows" "$argv_file"
    grep -qx -- "cast.cast" "$argv_file"
    grep -qx -- "out.gif" "$argv_file"
}

@test "render_gif propagates the positional arguments to agg" {
    local argv_file="$TEST_TMPDIR/agg.argv"
    agg() { printf '%s\n' "$@" > "$argv_file"; }
    export -f agg

    render_gif "mycast.cast" "mygif.gif" "5" "dracula" "160" "42"

    grep -qx "5" "$argv_file"
    grep -qx "dracula" "$argv_file"
    grep -qx "160" "$argv_file"
    grep -qx "42" "$argv_file"
}

@test "render_gif uses DEMO_FONT_FAMILY_DEFAULT when DEMO_FONT_FAMILY is unset" {
    local argv_file="$TEST_TMPDIR/agg.argv"
    agg() { printf '%s\n' "$@" > "$argv_file"; }
    export -f agg
    unset DEMO_FONT_FAMILY

    render_gif "cast.cast" "out.gif" "2" "monokai" "120" "37"

    grep -qF "$DEMO_FONT_FAMILY_DEFAULT" "$argv_file"
}

@test "DEMO_FONT_FAMILY env var overrides the default font chain" {
    local argv_file="$TEST_TMPDIR/agg.argv"
    agg() { printf '%s\n' "$@" > "$argv_file"; }
    export -f agg

    DEMO_FONT_FAMILY="Fira Code,Noto Color Emoji" \
        render_gif "cast.cast" "out.gif" "2" "monokai" "120" "37"

    grep -qF "Fira Code,Noto Color Emoji" "$argv_file"
}

# ── Font family default (emoji + geometric shape coverage) ───────────────────

@test "DEMO_FONT_FAMILY_DEFAULT includes a primary monospace font" {
    [[ "$DEMO_FONT_FAMILY_DEFAULT" == *"Menlo"* ]]
}

@test "DEMO_FONT_FAMILY_DEFAULT includes a fallback monospace font" {
    # Monaco is the macOS fallback if Menlo is not available; Courier New is
    # universally available on every OS.
    [[ "$DEMO_FONT_FAMILY_DEFAULT" == *"Monaco"* ]]
}

@test "DEMO_FONT_FAMILY_DEFAULT ends with a universally-available fallback" {
    # Courier New ships with every OS — the final-resort font.
    [[ "$DEMO_FONT_FAMILY_DEFAULT" == *"Courier New"* ]]
}

@test "DEMO_FONT_FAMILY_DEFAULT deliberately omits colour-emoji fonts" {
    # agg/resvg cannot render bitmap colour-emoji fonts like Apple Color
    # Emoji or Noto Color Emoji — it can only use vector (TrueType/OpenType
    # outline) fonts. Listing them would not help and could confuse the
    # renderer. We handle missing-glyph cases by rewriting the source cast
    # in strip_emoji_from_cast, not with more font fallbacks.
    [[ "$DEMO_FONT_FAMILY_DEFAULT" != *"Apple Color Emoji"* ]]
    [[ "$DEMO_FONT_FAMILY_DEFAULT" != *"Noto Color Emoji"* ]]
}

# ── strip_emoji_from_cast ────────────────────────────────────────────────────
# Rewrites the five codepoints agg's Menlo-rendered output can't handle into
# safe monochrome substitutes. Runs after sanitize_cast and before render_gif
# so the cast file committed to the repo and the derived GIF both carry the
# substitutions.

# ── strip_emoji_from_cast helper ────────────────────────────────────────────
# BATS runs individual test bodies under /bin/sh-style printf, which does not
# interpret \uNNNN escape sequences — the literal string "\u2139" lands on
# disk instead of the actual UTF-8 byte sequence for U+2139. We side-step
# that by writing and verifying the fixture file via Python 3 in all these
# tests, so the substitution logic is tested against real Unicode input.

# write_cast <path> <python-string-literal-without-outer-quotes>
# Example: write_cast "$cast" '\u2139 start \u2705'
write_cast() {
    local path="$1"
    local content="$2"
    python3 -c "open('$path', 'w').write('[0.0, \"o\", \"$content\"]\n')"
}

@test "strip_emoji_from_cast rewrites U+2139 INFORMATION SOURCE to lowercase i" {
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\u2139 Submitting job to SQS'

    strip_emoji_from_cast "$cast"

    python3 -c "
import sys
text = open('$cast').read()
sys.exit(0 if '\u2139' not in text and 'i Submitting' in text else 1)
"
}

@test "strip_emoji_from_cast rewrites U+2705 WHITE HEAVY CHECK MARK to U+2713" {
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\u2705 Deploy complete'

    strip_emoji_from_cast "$cast"

    python3 -c "
import sys
text = open('$cast').read()
sys.exit(0 if '\u2705' not in text and '\u2713' in text else 1)
"
}

@test "strip_emoji_from_cast rewrites U+2728 SPARKLES to asterisk" {
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\u2728 Ready'

    strip_emoji_from_cast "$cast"

    python3 -c "
import sys
text = open('$cast').read()
sys.exit(0 if '\u2728' not in text and '* Ready' in text else 1)
"
}

@test "strip_emoji_from_cast rewrites U+1F4E6 PACKAGE to bracketed pkg" {
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\U0001F4E6 Package built'

    strip_emoji_from_cast "$cast"

    python3 -c "
import sys
text = open('$cast').read()
sys.exit(0 if '\U0001F4E6' not in text and '[pkg] Package' in text else 1)
"
}

@test "strip_emoji_from_cast rewrites U+1F680 ROCKET to double greater-than" {
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\U0001F680 Launching'

    strip_emoji_from_cast "$cast"

    python3 -c "
import sys
text = open('$cast').read()
sys.exit(0 if '\U0001F680' not in text and '>> Launching' in text else 1)
"
}

@test "strip_emoji_from_cast applies all five substitutions in a single pass" {
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\u2139 start \u2705 \u2728 \U0001F4E6 \U0001F680 end'

    strip_emoji_from_cast "$cast"

    python3 -c "
import sys
text = open('$cast').read()
bad = ['\u2139', '\u2705', '\u2728', '\U0001F4E6', '\U0001F680']
good = ['i start', '\u2713', '*', '[pkg]', '>>']
ok = all(ch not in text for ch in bad) and all(s in text for s in good)
sys.exit(0 if ok else 1)
"
}

@test "strip_emoji_from_cast preserves characters Menlo already renders" {
    # Menlo covers these codepoints, so strip must pass them through untouched.
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\u2713 check \u2717 cross \u26A0 warn \u25B8 step \u2192 arrow \u2501 \u2501 \u2550'
    local before
    before=$(cat "$cast")

    strip_emoji_from_cast "$cast"

    [ "$(cat "$cast")" = "$before" ]
}

@test "strip_emoji_from_cast edits in place (no stdout output)" {
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\u2139 info'

    run strip_emoji_from_cast "$cast"
    [ "$status" -eq 0 ]
    # Helper should not print anything on success.
    [ -z "$output" ]
    # The file is rewritten.
    grep -q "i info" "$cast"
}

@test "strip_emoji_from_cast skips quietly when the file doesn't exist" {
    # Record scripts call it unconditionally after asciinema — a missing
    # cast file shouldn't hard-fail the pipeline.
    run strip_emoji_from_cast "$TEST_TMPDIR/does-not-exist.cast"
    [ "$status" -eq 0 ]
}

@test "SKIP_EMOJI_STRIP=1 bypasses the substitution pass" {
    local cast="$TEST_TMPDIR/emoji.cast"
    write_cast "$cast" '\u2139 \u2705 \u2728 \U0001F4E6 \U0001F680'

    SKIP_EMOJI_STRIP=1 strip_emoji_from_cast "$cast"

    # Original codepoints still present — bypass worked.
    python3 -c "
import sys
text = open('$cast').read()
codepoints = ['\u2139', '\u2705', '\u2728', '\U0001F4E6', '\U0001F680']
sys.exit(0 if all(ch in text for ch in codepoints) else 1)
"
}

# ── wait_for_job ─────────────────────────────────────────────────────────────
# Regression guards for the contract live_demo.sh relies on: a recording that
# lives under ``set -euo pipefail`` must not die if a job times out mid-demo.

@test "wait_for_job always returns 0 even when kubectl wait fails" {
    # Stub kubectl: ``get`` succeeds (job exists), ``wait`` always fails.
    # This simulates a slow job whose completion exceeds the budget.
    kubectl() {
        case "${1:-}" in
            get)  return 0 ;;
            wait) return 1 ;;
            *)    return 0 ;;
        esac
    }
    export -f kubectl

    # Use a very short budget so the test runs fast.
    run wait_for_job "fake-job" "fake-ns" 1
    # The recording is under ``set -e``; any non-zero here would kill the demo.
    [ "$status" -eq 0 ]
}

@test "wait_for_job returns 0 on successful kubectl wait" {
    kubectl() { return 0; }
    export -f kubectl

    run wait_for_job "fake-job" "fake-ns" 1
    [ "$status" -eq 0 ]
}

# ── ARN helpers (regression guard — used by setup-cluster-access.sh too) ─────

@test "is_assumed_role returns true for assumed-role ARNs" {
    run is_assumed_role "arn:aws:sts::123456789012:assumed-role/MyRole/session-123"
    [ "$status" -eq 0 ]
}

@test "is_assumed_role returns false for IAM role ARNs" {
    run is_assumed_role "arn:aws:iam::123456789012:role/MyRole"
    [ "$status" -ne 0 ]
}

@test "extract_role_name pulls the role name out of an assumed-role ARN" {
    run extract_role_name "arn:aws:sts::123456789012:assumed-role/GcoDeployer/abc-session"
    [ "$status" -eq 0 ]
    [ "$output" = "GcoDeployer" ]
}

@test "build_role_arn reconstructs an IAM role ARN" {
    run build_role_arn "GcoDeployer" "123456789012"
    [ "$status" -eq 0 ]
    [ "$output" = "arn:aws:iam::123456789012:role/GcoDeployer" ]
}

# ── Paired recording publication ────────────────────────────────────────────

@test "publish_recording_artifacts replaces a prepared cast and GIF together" {
    local stage="$TEST_TMPDIR/stage"
    local final_dir="$TEST_TMPDIR/final"
    mkdir -p "$stage" "$final_dir"
    printf 'old cast\n' > "$final_dir/demo.cast"
    printf 'old gif\n' > "$final_dir/demo.gif"
    printf 'new cast\n' > "$stage/demo.cast"
    printf 'new gif\n' > "$stage/demo.gif"

    publish_recording_artifacts \
        "$stage/demo.cast" "$stage/demo.gif" \
        "$final_dir/demo.cast" "$final_dir/demo.gif"

    [ "$(cat "$final_dir/demo.cast")" = "new cast" ]
    [ "$(cat "$final_dir/demo.gif")" = "new gif" ]
    [ "$RECORDING_PUBLICATION_COMPLETE" -eq 1 ]
    [ "$RECORDING_PUBLICATION_IN_PROGRESS" -eq 0 ]
}

@test "publish_recording_artifacts restores both originals when GIF publication fails" {
    local stage="$TEST_TMPDIR/stage"
    local final_dir="$TEST_TMPDIR/final"
    mkdir -p "$stage" "$final_dir"
    printf 'old cast\n' > "$final_dir/demo.cast"
    printf 'old gif\n' > "$final_dir/demo.gif"
    printf 'new cast\n' > "$stage/demo.cast"
    printf 'new gif\n' > "$stage/demo.gif"

    FAILING_MV_SOURCE="$stage/demo.gif"
    FAILING_MV_DESTINATION="$final_dir/demo.gif"
    export FAILING_MV_SOURCE FAILING_MV_DESTINATION
    mv() {
        local source destination
        if [ "${1:-}" = "-f" ]; then
            source="$2"
            destination="$3"
        else
            source="$1"
            destination="$2"
        fi
        if [ "$source" = "$FAILING_MV_SOURCE" ] && \
                [ "$destination" = "$FAILING_MV_DESTINATION" ]; then
            return 73
        fi
        command mv "$@"
    }
    export -f mv

    run publish_recording_artifacts \
        "$stage/demo.cast" "$stage/demo.gif" \
        "$final_dir/demo.cast" "$final_dir/demo.gif"

    [ "$status" -eq 73 ]
    [ "$(cat "$final_dir/demo.cast")" = "old cast" ]
    [ "$(cat "$final_dir/demo.gif")" = "old gif" ]
}

@test "publish_recording_artifacts removes an old GIF transactionally in SKIP_GIF mode" {
    local stage="$TEST_TMPDIR/stage"
    local final_dir="$TEST_TMPDIR/final"
    mkdir -p "$stage" "$final_dir"
    printf 'old cast\n' > "$final_dir/demo.cast"
    printf 'old gif\n' > "$final_dir/demo.gif"
    printf 'new cast\n' > "$stage/demo.cast"

    publish_recording_artifacts \
        "$stage/demo.cast" "" \
        "$final_dir/demo.cast" "$final_dir/demo.gif"

    [ "$(cat "$final_dir/demo.cast")" = "new cast" ]
    [ ! -e "$final_dir/demo.gif" ]
    [ "$RECORDING_PUBLICATION_COMPLETE" -eq 1 ]
    [ "$RECORDING_PUBLICATION_IN_PROGRESS" -eq 0 ]
}
