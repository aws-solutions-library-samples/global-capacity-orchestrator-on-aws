# CI Helper Scripts

Helper scripts invoked by GitHub Actions workflows. Separated from the workflows themselves so they can be tested independently and reused across jobs.

## Table of Contents

- [Files](#files)
- [Testing](#testing)
- [Adding a New Script](#adding-a-new-script)

## Files

| File | Invoked By | Description |
|------|------------|-------------|
| `dependency-scan.sh` | `deps-scan.yml` (monthly) | Checks pinned dependency surfaces, always runs deterministic accelerator catalog/NodePool/watch-list validation, and—with AWS credentials—compares the checked-in accelerator catalog with the live enabled-Region [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html) union. Writes one Markdown report and sets `has_drift=true` for version drift, policy findings, catalog drift, or operational check failures. |
| `lib_dependency_scan.sh` | `dependency-scan.sh` | Sourceable helper functions — image registry parsing (`parse_image_registry`), semver comparison (`compare_semver`), tag filtering (`is_semver_tag`, `is_project_image`), [Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) model-family/version helpers, and strict accelerator JSON summary parsing (`parse_accelerator_drift_count`). Extracted so BATS tests can exercise logic without running the full scan. |
| `check_pip_audit_ignore.py` | `security.yml` (`security:pip-audit:deps`) | Validates the project-local `.github/config/.pip-audit-ignore` suppression file. Fails the workflow when any entry's `exp:YYYY-MM-DD` marker is on-or-before today (inclusive) or when an entry is missing the marker entirely. Importable as a module (`check_file()`, `main()`) so it can be exercised by pytest fixtures rather than only ever run end-to-end through CI. |
| `validate_helm_charts.py` | `integration-tests.yml` (`integration:helm:charts-valid`) | Validates every `(chart, version)` pinned in `lambda/helm-installer/charts.yaml`. Structural checks run always (required fields, SemVer version, `oci://`/`use_oci` consistency); `--mode online` additionally resolves each chart at its exact pinned version (`helm show chart`) and renders it (`helm template`) so a typo'd name or a version that never shipped fails in CI rather than mid-deploy. Every entry is checked, including `enabled: false` charts. Importable (`validate_structure()`, `build_refs()`, `validate_online()`, `main()`) for pytest. |
| `run-semgrep.sh` | `security.yml` (`security:semgrep:sast`) | Runs `semgrep scan --config auto --error` with repo-wide rule suppressions loaded from `.github/config/semgrep-excluded-rules.txt` — each non-comment, non-blank line becomes a `--exclude-rule` flag, so the suppression list lives in a reviewable data file instead of being hardwired into the workflow. POSIX `sh` (the semgrep container image is not guaranteed to ship bash). Tested by `tests/BATS/test_run_semgrep.bats`. |
| `dev_alias_live.sh` | `integration-tests.yml` (`integration:dev-alias:{docker,finch,podman,none}`) | Live proof that `scripts/setup-dev-alias.sh` builds the image and generates a working `gco` shell function. Drives `setup-dev-alias.sh` to build the real `gco-dev` image from `Dockerfile.dev` and install the generated function into a throwaway rc, sources it in a fresh shell, and proves through it: `gco --version` (the real CLI runs) and `gco dag validate ci-dag.yaml` (an offline command that reads a relative-path file from the mounted workspace, proving arg-forwarding, the `$PWD` -> `/workspace` bind mount, and `cwd=/workspace`). `--skip-build` reuses an existing image (and tells `setup-dev-alias.sh` to skip its build); `--no-runtime` proves the script refuses cleanly (non-zero exit, no rc block) when no runtime answers. |

## Testing

Shell scripts are tested by BATS:

```bash
# From the repository root
bats tests/BATS/test_dependency_scan.bats
bats tests/BATS/test_run_semgrep.bats

# The same deterministic accelerator guard run by normal CI and the monthly scan
python scripts/accelerator_catalog.py validate
pytest tests/test_accelerator_catalog.py -q
```

Python helpers ship with pytest tests under `tests/`:

```bash
# Validator coverage
pytest tests/test_pip_audit_ignore_validator.py -v
pytest tests/test_helm_charts_validation.py -v
```

`validate_helm_charts.py` also has an opt-in online tier that pulls and renders
every chart with the real `helm` binary. It is skipped by default (and in the
normal unit job); enable it locally with a `helm` on `PATH`:

```bash
# Structural checks only (no helm/network needed):
python3 .github/scripts/validate_helm_charts.py --mode offline

# Full resolve + render of every pinned chart (needs helm + network):
python3 .github/scripts/validate_helm_charts.py --mode online --verbose

# Same, via the opt-in pytest tier:
GCO_HELM_CHART_VALIDATION=1 pytest tests/test_helm_charts_validation.py -v
```

`dev_alias_live.sh` is itself a live test — it exercises the onboarding alias
end to end against a real runtime. It runs `setup-dev-alias.sh`, which builds
the real `gco-dev` image, so each run takes a few minutes; pass `--skip-build`
to reuse an image you already built. Run it locally against whichever runtime
you have installed:

```bash
# From the repository root.
.github/scripts/dev_alias_live.sh docker
.github/scripts/dev_alias_live.sh finch
.github/scripts/dev_alias_live.sh podman
# Reuse an already-built gco-dev image (skip the Dockerfile.dev build):
.github/scripts/dev_alias_live.sh finch --skip-build
# Prove graceful refusal when no runtime is available:
.github/scripts/dev_alias_live.sh --no-runtime
```

## Adding a New Script

1. Create the script in this directory (shell or Python).
2. For shell: make it executable (`chmod +x .github/scripts/my-script.sh`); for Python, leave it non-executable and invoke as `python3 .github/scripts/my-script.py`.
3. Extract reusable shell helpers into a `lib_*.sh` file; keep Python helpers as importable modules.
4. Add tests under `tests/BATS/` (shell) or `tests/test_*.py` (Python).
5. Reference it from the workflow with `run: bash .github/scripts/my-script.sh` or `run: python3 .github/scripts/my-script.py`.
