# CI Helper Scripts

Helper scripts invoked by GitHub Actions workflows. Separated from the workflows themselves so they can be tested independently and reused across jobs.

## Table of Contents

- [Files](#files)
- [Testing](#testing)
- [Adding a New Script](#adding-a-new-script)

## Files

| File | Invoked By | Description |
|------|------------|-------------|
| `dependency-scan.sh` | `deps-scan.yml` (monthly) | Checks for outdated Python packages, Docker images, Helm charts, and EKS add-on versions. Writes a Markdown report and sets `has_drift=true` on `$GITHUB_OUTPUT` if any are outdated. |
| `lib_dependency_scan.sh` | `dependency-scan.sh` | Sourceable helper functions — image registry parsing (`parse_image_registry`), semver comparison (`compare_semver`), tag filtering (`is_semver_tag`, `is_project_image`). Extracted so BATS tests can exercise the logic without running the full scan. |
| `check_pip_audit_ignore.py` | `security.yml` (`security:pip-audit:deps`) | Validates the project-local `.pip-audit-ignore` suppression file. Fails the workflow when any entry's `exp:YYYY-MM-DD` marker is on-or-before today (inclusive) or when an entry is missing the marker entirely. Importable as a module (`check_file()`, `main()`) so it can be exercised by pytest fixtures rather than only ever run end-to-end through CI. |

## Testing

Shell helpers in `lib_dependency_scan.sh` are tested by BATS:

```bash
# From the repository root
bats tests/BATS/test_dependency_scan.bats
```

Python helpers ship with pytest tests under `tests/`:

```bash
# Validator coverage
pytest tests/test_pip_audit_ignore_validator.py -v
```

## Adding a New Script

1. Create the script in this directory (shell or Python).
2. For shell: make it executable (`chmod +x .github/scripts/my-script.sh`); for Python, leave it non-executable and invoke as `python3 .github/scripts/my-script.py`.
3. Extract reusable shell helpers into a `lib_*.sh` file; keep Python helpers as importable modules.
4. Add tests under `tests/BATS/` (shell) or `tests/test_*.py` (Python).
5. Reference it from the workflow with `run: bash .github/scripts/my-script.sh` or `run: python3 .github/scripts/my-script.py`.
