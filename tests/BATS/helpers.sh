#!/usr/bin/env bash
# =============================================================================
# helpers.sh — shared primitives for the BATS suites
# =============================================================================
#
# Loaded at the top of a suite with `load 'helpers.sh'`. Everything here is
# test harness: .simplecov keeps tests/ out of the coverage report and the
# shell coverage gate never lists it, but lint:shellcheck:shell does lint it.
#
# Subjects are referenced by ABSOLUTE path (see REPO_ROOT). bashcov records
# BASH_SOURCE as the script was invoked and resolves a relative one through a
# single working-directory stack shared by every traced shell; with fixture
# repositories that carry same-named copies (demo/lib_demo.sh under a temp
# checkout, say), a relative `source demo/lib_demo.sh` can be credited to the
# wrong file in either direction. An absolute path needs no resolution.
# =============================================================================

# Absolute path of the checkout the suite is testing.
REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
export REPO_ROOT

# write_stub <dir> <name>
#
# Writes stdin to <dir>/<name> and makes it executable, creating <dir>. The
# standard way the suites fake an external command: a bash script that records
# its argv and answers what the test scripted.
write_stub() {
    local dir="$1" name="$2"
    mkdir -p "$dir"
    cat > "$dir/$name"
    chmod +x "$dir/$name"
}

# stub_noop <dir> <name>...
#
# Fakes each named command as an executable that succeeds silently — for
# `sleep`, `clear` and friends, whose only effect on a test is to slow it down.
stub_noop() {
    local dir="$1"
    shift
    local name
    for name in "$@"; do
        write_stub "$dir" "$name" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
    done
}

# stub_forbidden <dir> <name>...
#
# Fakes each named command as one that must never run: it fails with a
# distinctive status and message, so a script that reaches for the live
# tool by mistake fails the test loudly instead of silently succeeding.
stub_forbidden() {
    local dir="$1"
    shift
    local name
    for name in "$@"; do
        write_stub "$dir" "$name" <<'STUB'
#!/usr/bin/env bash
echo "unexpected live command: $(basename "$0") $*" >&2
exit 97
STUB
    done
}

# link_tools <dir> <tool>...
#
# Symlinks the real executables for the named tools into <dir>, so a test can
# run a script with PATH restricted to <dir> (plus its stubs) and prove what
# the script does when some *other* tool is absent. Skips a tool that is not
# installed rather than failing, so the caller decides whether that matters.
link_tools() {
    local dir="$1"
    shift
    mkdir -p "$dir"
    local tool real
    for tool in "$@"; do
        real="$(command -v "$tool" 2>/dev/null || true)"
        if [ -n "$real" ] && [ ! -e "$dir/$tool" ]; then
            ln -s "$real" "$dir/$tool"
        fi
    done
}

# init_fixture_repo <dir>
#
# Turns <dir> into a git repository with everything in it committed, and
# prints the resulting HEAD SHA. The recorders bind a live recording to one
# reviewed commit through verify_recording_git_state, so a fixture checkout
# needs a real history for that guard to pass.
init_fixture_repo() {
    local dir="$1"
    git -C "$dir" init -q
    git -C "$dir" add -A .
    git -C "$dir" -c user.name=CI -c user.email=ci@example.invalid \
        commit -q -m fixture
    git -C "$dir" rev-parse HEAD
}
