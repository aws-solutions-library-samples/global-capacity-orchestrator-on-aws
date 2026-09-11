# frozen_string_literal: true

# =============================================================================
# .simplecov — SimpleCov settings for the bashcov run (unit:bats:shell)
# =============================================================================
#
# bashcov hands its line hits to SimpleCov, which decides which files make it
# into .resultset.json, coverage.json and the HTML report. SimpleCov loads this
# file from the project root before the run starts. Two things need saying
# that its defaults get wrong for this repository:
#
#   * SimpleCov's default "hidden_filter" profile drops every file whose
#     project-relative path starts with a dot. That is every script under
#     .github/scripts/ — ten of the twenty-two tracked shell scripts — and it
#     is why none of them ever appeared in a bashcov report, whatever their
#     BATS suite executed. The gate (.github/scripts/check_bash_coverage.py)
#     read that absence as "not executed by any suite". Removing the filter
#     is what lets a hit on a .github/ script count.
#
#   * bashcov lists every shell script under --root, executed or not, so the
#     report shows 0% for a script no suite touched rather than omitting it.
#     Trees that are not this project's source must be skipped or a local
#     run drowns the report in .venv activate scripts, sibling worktrees and
#     vendored gems. The same trees are omitted on the Python side in
#     pyproject.toml [tool.coverage.run].
#
#   * The test-suite measures; it is not measured. tests/BATS/ holds the .bats
#     files (which bashcov does not classify as shell scripts anyway) and the
#     wrapper bashcov launches the suite through, whose lines are traced like
#     any other bash but are harness, not product.
# =============================================================================

SimpleCov.configure do
  # The built-in hidden_filter is registered as exactly this Regexp. If a
  # SimpleCov bump changes it, removing it silently fails and .github/ vanishes
  # from the report again — so refuse to run rather than measure the wrong set.
  remove_filter(/\A\..*/) or raise "SimpleCov's hidden_filter was not found; .simplecov needs updating"
  skip(%r{\A(?:\.git|\.venv|\.worktrees|\.bundle|node_modules|vendor|cdk\.out|coverage|htmlcov|site|build|dist)/})
  skip(%r{\Alambda/[^/]+-build/})
  skip(%r{\Atests/})
end
