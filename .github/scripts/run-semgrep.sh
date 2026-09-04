#!/bin/sh
# =============================================================================
# run-semgrep.sh — run semgrep with repo-wide rule suppressions from a file
# =============================================================================
#
# The list of suppressed rule IDs (each with a justification) lives in
#   .github/config/semgrep-excluded-rules.txt
# This script expands every non-comment, non-blank line into a repeated
# `--exclude-rule <id>` argument and then runs `semgrep scan`. Keeping the IDs
# in a version-controlled data file (the same posture as .trivyignore and
# .pip-audit-ignore) means a suppression is reviewed as a data change rather
# than hardwired into pipeline YAML, and every caller invokes this one script
# instead of duplicating the flag list.
#
# Any arguments passed to this script are forwarded to `semgrep scan` ahead of
# the scan target (the repository root).
#
# Usage:
#   sh .github/scripts/run-semgrep.sh
#   SEMGREP_EXCLUDE_RULES_FILE=/path/to/list sh .github/scripts/run-semgrep.sh
#
# POSIX sh on purpose: the semgrep container image is not guaranteed to ship
# bash.
# =============================================================================
set -eu

# Make path resolution independent of any inherited CDPATH (which would make
# `cd` echo the resolved directory into the command substitution).
unset CDPATH

# Resolve the repository root from this script's own location (.github/scripts).
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
exclude_file="${SEMGREP_EXCLUDE_RULES_FILE:-$repo_root/.github/config/semgrep-excluded-rules.txt}"

# Positional parameters ($@) accumulate any caller-supplied args first, then the
# --exclude-rule flags read from the static config file. Each rule sits alone on
# its line; `read` strips surrounding whitespace and the throwaway second field
# absorbs any trailing inline comment, while the case skips blank/comment lines.
if [ -f "$exclude_file" ]; then
  while read -r rule _ || [ -n "$rule" ]; do
    case "$rule" in
      '' | '#'*) continue ;;
    esac
    set -- "$@" --exclude-rule "$rule"
  done < "$exclude_file"
  echo "run-semgrep: applied rule suppressions from $exclude_file"
else
  echo "run-semgrep: no suppression file at $exclude_file (no rule excludes)"
fi

set -x
exec semgrep scan --config auto --error "$@" --json -o semgrep-report.json .
