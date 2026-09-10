# =============================================================================
# Gemfile — RubyGems dependencies
# =============================================================================
#
# One tool lives here: bashcov, which measures line coverage of the shell
# scripts exercised by the BATS suite (see the unit:bats:shell job in
# .github/workflows/unit-tests.yml). Bash coverage needs a tool that knows
# which lines of a script are executable — here-documents, `case` arms, line
# continuations and function headers all have to be classified correctly — and
# bashcov gets that from SimpleCov rather than from a hand-rolled parser.
#
# Why a Gemfile at all, for a Python/Node project: every dependency the
# project uses is pinned and auditable, so a Ruby tool gets the same treatment
# as a Python or npm one:
#
#   * the version is pinned exactly here and the resolved graph is committed
#     in Gemfile.lock (the RubyGems equivalent of requirements-lock.txt and
#     package-lock.json), including the CHECKSUMS block Bundler 4 emits — a
#     sha256 per gem, so a republished gem cannot change under us;
#   * the interpreter is pinned in .ruby-version, mirroring .python-version.
#     Ruby 4.0 is the current stable series (supported to 2029-03-31); the
#     dependency scan reports when a newer stable series ships;
#   * Dependabot watches the "bundler" ecosystem (.github/dependabot.yml);
#   * security:bundler-audit:deps checks the graph against the Ruby advisory
#     database, mirroring security:pip-audit:deps and security:npm-audit:deps;
#   * tests/test_supply_chain_integrity.py asserts the pins stay exact.
#
# Nothing in the shipped product depends on Ruby — it is a CI-only tool, and
# contributors who do not touch shell scripts never need it installed.
# =============================================================================
source "https://rubygems.org"

# Bash line coverage for the BATS suite. Pinned exactly; Dependabot proposes
# bumps. Its own dependency (simplecov) is resolved in Gemfile.lock.
gem "bashcov", "4.0.0"

# Audits Gemfile.lock against the Ruby advisory database. Pinned exactly for
# the same reason pip-audit and npm's audit tooling are.
gem "bundler-audit", "0.9.3"
