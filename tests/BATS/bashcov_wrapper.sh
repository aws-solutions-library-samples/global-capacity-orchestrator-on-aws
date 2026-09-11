#!/usr/bin/env bash
# =============================================================================
# bashcov_wrapper.sh — launch the BATS suite under bashcov without leaking the
# harness's shell options into the scripts being measured
# =============================================================================
#
# Usage (as the command bashcov runs):
#   bundle exec bashcov --root . -- tests/BATS/bashcov_wrapper.sh tests/BATS/
#
# bashcov makes every Bash it spawns trace itself by exporting SHELLOPTS with
# xtrace in it: a Bash that finds SHELLOPTS in its environment enables every
# option the value lists, and keeps the variable exported so its own children
# do the same. That second half is the problem. SHELLOPTS names *all* of a
# shell's options, so each process re-exports whatever it has switched on by
# the time it forks — and the bats entry point on Debian and Ubuntu (the
# /usr/bin/bats wrapper) runs `set -euo pipefail`. Every script a test then
# executes starts life with errexit, nounset and pipefail already on, whether
# or not it asked for them, and behaves differently from the uninstrumented
# run (measured: `lib_demo.sh: line 274: DIM: unbound variable` from a helper
# called without setup_colors, and `grep -oP` no-match exits turning fatal
# under an inherited pipefail — nine assertions that pass without bashcov).
#
# This wrapper carries the tracing through BASH_ENV instead. Bash reads and
# executes the file BASH_ENV names at the start of every non-interactive
# shell, so a two-line file — bashcov's PS4, then `set -o xtrace` — reaches
# every child exactly as SHELLOPTS did, but switches on tracing alone. With
# SHELLOPTS and PS4 removed from the environment, a shell's other options are
# once again its own business, and the instrumented run tests the same
# scripts the plain `bats tests/BATS/` run does.
#
# Taking PS4 out of the environment also closes a second hole: a POSIX sh
# (dash on Debian) inherits PS4 from the environment too, so a `set -x` in a
# script run under `sh` printed bashcov's field markers to stderr, a test
# captured them into $output, and the traced `output=...` assignment fed a
# forged record with an empty LINENO back into bashcov's parser — which
# aborts at the first malformed record and drops every hit after it (this
# is what cut the CI report off at test_run_semgrep.bats). dash never sees
# the markers now.
#
# The second repair is to the pipe itself. Ruby creates bashcov's pipe
# non-blocking, and the flag lives on the open file description every traced
# Bash inherits. bashcov parses the pipe live, in Ruby, far slower than
# hundreds of Bash processes fill it, so the pipe is full most of the time —
# and a write into a full non-blocking pipe fails with EAGAIN instead of
# waiting. Bash does not retry the failed flush of its xtrace buffer, so the
# record is silently gone: one probe measured 1, 2 and 5 hits on the same
# line across three identical runs, and the same stream captured to a file
# was complete every time. Clearing O_NONBLOCK on the shared description
# (below) makes every writer block like an ordinary pipe writer whenever Ruby
# falls behind, and the counts became identical run to run. The trace still
# goes straight into bashcov's pipe rather than through a spool file: bashcov
# resolves each record's path as it arrives, so a fixture the suite created
# under $BATS_TEST_TMPDIR is looked up while it still exists and cannot be
# mistaken for a same-named file in the checkout after bats has removed it.
#
# BASH_XTRACEFD is a plain exported variable that Bash honours on import and
# dash ignores; the children keep seeing bashcov's descriptor in it.
# =============================================================================
set -euo pipefail
set +o xtrace # the wrapper's own lines are harness, not measurement

if [ -z "${PS4:-}" ] || [ -z "${BASH_XTRACEFD:-}" ]; then
    echo "bashcov_wrapper.sh: expected bashcov's PS4 and BASH_XTRACEFD in the environment" >&2
    exit 2
fi
bashcov_fd="$BASH_XTRACEFD"

# python3 is already a dependency of the job (the suite's own YAML checks use
# it), and fcntl is the only way to reach the flag from a shell script.
python3 - "$bashcov_fd" <<'PY'
import fcntl
import os
import sys

fd = int(sys.argv[1])
flags = fcntl.fcntl(fd, fcntl.F_GETFL)
fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
PY

scratch="$(mktemp -d "${TMPDIR:-/tmp}/bashcov-wrapper.XXXXXX")"
trap 'rm -rf -- "$scratch"' EXIT
{
    printf 'PS4=%q\n' "$PS4"
    printf 'set -o xtrace\n'
} > "$scratch/bash_env"

status=0
env -u SHELLOPTS -u PS4 BASH_ENV="$scratch/bash_env" BASH_XTRACEFD="$bashcov_fd" \
    bats "$@" || status=$?
exit "$status"
