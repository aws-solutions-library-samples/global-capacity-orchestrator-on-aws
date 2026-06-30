# Legacy reference material

Frozen, unmaintained files kept for people forking the project. Nothing in this
folder runs in this repository's CI.

## Contents

- [`.gitlab-ci.yml`](#gitlab-ciyml)

## `.gitlab-ci.yml`

The GitLab CI/CD pipeline this project used before standardizing on GitHub
Actions. It is preserved as a starting point for anyone forking onto GitLab.

- **Not maintained.** GitHub Actions (`.github/workflows/`) is authoritative.
  This file is not updated when the workflows change and will drift over time.
- **Not active here.** GitLab only auto-runs a `.gitlab-ci.yml` at the
  repository root, so under `.github/legacy/` this file does nothing. A GitLab
  fork would copy it back to the root to use it.
- **Scanner configs.** The linter and scanner configs it references now live in
  `.github/config/`, and the pipeline points at them there. The maintenance job
  that re-derives image tags greps `.gitlab-ci.yml` by its root-relative name,
  matching the root placement a fork would use.

See `CONTRIBUTING.md` for the project's stance on this file.
