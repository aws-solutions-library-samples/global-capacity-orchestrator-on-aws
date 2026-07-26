# BATS Shell Script Tests

[BATS](https://github.com/bats-core/bats-core) (Bash Automated Testing System) tests for GCO's shell scripts. These are primarily functional tests that execute real bash logic (parameter expansion, sed transforms, jq queries against cdk.json, JSON validation) rather than just checking for string presence.

## Table of Contents

- [What's Tested](#whats-tested)
- [Running Locally](#running-locally)
- [CI Integration](#ci-integration)
- [Adding New Tests](#adding-new-tests)
- [Test Design Philosophy](#test-design-philosophy)

## What's Tested

| Test File | Script Under Test | Tests | What It Covers |
|---|---|---|---|
| `test_live_demo.bats` | `demo/live_demo.sh` + `demo/lib_demo.sh` | 38 | Sources `lib_demo.sh` and calls real functions: `feature_status`, `detect_features`, `setup_pauses`, `setup_colors`, `is_assumed_role`, `extract_role_name`, `build_role_arn`. Also validates manifest YAML, namespace targeting, inference lifecycle, section completeness |
| `test_lib_demo.bats` | `demo/lib_demo.sh` | 33 | Sources and calls the real helper functions used by the three `record_*.sh` scripts: `sanitize_cast` (AWS-account-ID redaction, in-place edit, graceful missing-file, `SKIP_SANITIZE` bypass), `strip_emoji_from_cast` (substitutes the five codepoints agg can't render with Menlo — `ℹ→i`, `✅→✓`, `✨→*`, `📦→[pkg]`, `🚀→>>` — plus in-place semantics, graceful missing-file, `SKIP_EMOJI_STRIP` bypass), `render_gif` (agg argv via function stub, positional-arg propagation, `DEMO_FONT_FAMILY` override), `DEMO_FONT_FAMILY_DEFAULT` (Menlo-first monospace chain that deliberately omits colour-emoji fonts agg can't use), `wait_for_job` (always returns 0 so `set -e` callers don't die on slow jobs), plus regression guards for the ARN helpers shared with `setup-cluster-access.sh` |
| `test_setup_cluster_access.bats` | `scripts/setup-cluster-access.sh` | 20 | Argument defaults and overrides, sources `lib_demo.sh` for ARN helpers (`is_assumed_role`, `extract_role_name`, `build_role_arn`), error handling patterns, AWS CLI call structure |
| `test_setup_dev_alias.bats` | `scripts/setup-dev-alias.sh` | 24 | Functional tests with PATH-shimmed container runtimes: detection precedence (docker > finch > podman via `<rt> info`), `GCO_CONTAINER_RUNTIME` / `CDK_DOCKER` / `--runtime` override precedence, per-runtime socket selection (docker & podman mount a socket, finch omits it), `--print` block emission (markers, TTY `-it` vs non-TTY `-i` branches, custom `--image`, no file writes), idempotent rc-file install (exactly one marked block, preserves existing content, in-place block replacement), and an end-to-end check that the emitted `gco` function forwards args and mounts the workspace, plus podman qualifying a bare image as `localhost/<name>` while leaving registry-qualified names untouched |
| `test_dev_alias_live.bats` | `.github/scripts/dev_alias_live.sh` | 7 | Static checks (`bash -n`, shellcheck) plus the container-less paths: `--help` header output, the unknown-option / `--image`-without-value / no-argument usage errors, and the `--no-runtime` refusal (masks docker/finch/podman with PATH stubs that fail `<rt> info`, then asserts the setup script refuses and writes no rc block). The live docker/finch/podman proofs that build gco-dev and run the generated function execute in `integration-tests.yml`. |
| `test_dependency_scan.bats` | `.github/scripts/dependency-scan.sh` + `lib_dependency_scan.sh` | 88 | Functional tests that source `lib_dependency_scan.sh` and call real helpers: `parse_image_registry` (nvcr.io / gcr.io / quay / ghcr / registry.k8s.io / public.ecr.aws / docker.io defaulting), `is_semver_tag`, `is_project_image`, `compare_semver` (two- and three-part versions, v-prefix handling), `extract_aurora_versions` / `extract_emr_versions` / `extract_eks_addons` (constants-module happy path + regex fallback), `extract_k8s_version`, `extract_dockerfile_pins` (allowlist filter, inline-comment stripping, commented-out ARG skipping, empty file / missing file), `extract_precommit_hooks` (skips `local`/`meta` repos, skips entries with no `rev:`, malformed YAML), `get_latest_precommit_hook_release` (rejects non-GitHub hosts and malformed paths, tolerates `.git` and trailing-slash forms, with `curl`-shimmed fixtures for the tag-parsing branches) |
| `test_runbooks.bats` | `docs/RUNBOOKS.md` Root-State Recovery | 3 | Pins the ALB-to-listener command order with realistic `loadbalancer/app/...` and `listener/app/...` ARN fixtures; proves one matching listener succeeds and no matching listener fails closed |
| `test_run_semgrep.bats` | `.github/scripts/run-semgrep.sh` | 9 | Stubs `semgrep` on `PATH` to record the argv the wrapper builds: one `--exclude-rule` per non-comment, non-blank line of the suppression file, comment/blank lines skipped, an inline comment stripped to the bare rule id, a missing suppression file degrading to no excludes (while still scanning), the committed default file excluding `github-actions-mutable-action-tag`, caller-supplied extra args forwarded, and the assembled command scanning `.` with `--json -o semgrep-report.json`. Plus `sh -n` syntax and shellcheck static checks. |
| `test_aws_cli_examples.bats` | `docs/client-examples/aws_cli_examples.sh` | 17 | API region detection with fallback chain, URL trailing-slash stripping, JSON payload validation, trusted registry enforcement, [SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html) signing patterns |
| `test_curl_sigv4_proxy.bats` | `docs/client-examples/curl_sigv4_proxy_example.sh` | 27 | URL host/path parsing pipeline, stack name construction, proxy lifecycle (port check, trap, kill), HTTP method coverage, Host header inclusion, auth failure testing, temp file cleanup |
| `test_record_demo.bats` | `demo/record_demo.sh` | 40 | Configuration defaults and overrides, preflight checks (asciinema, agg, jq, gco, kubectl, cluster connectivity, disk space), wrapper generation, asciinema invocation, `sanitize_cast` → `strip_emoji_from_cast` → `render_gif` ordering, `SKIP_GIF` / `SKIP_SANITIZE` / `SKIP_EMOJI_STRIP` support, path resolution |
| `test_record_deploy.bats` | `demo/record_deploy.sh` | 18 | Syntax validation, configuration defaults, preflight checks, asciinema invocation pattern, full `sanitize_cast` → `strip_emoji_from_cast` → `render_gif` pipeline ordering, `SKIP_SANITIZE` / `SKIP_EMOJI_STRIP` support, deploy command structure, output file paths |
| `test_record_destroy.bats` | `demo/record_destroy.sh` | 18 | Syntax validation, configuration defaults, preflight checks, asciinema invocation pattern, full `sanitize_cast` → `strip_emoji_from_cast` → `render_gif` pipeline ordering, `SKIP_SANITIZE` / `SKIP_EMOJI_STRIP` support, destroy command structure, output file paths |

Total: **342 tests** across 13 files.

## Running Locally

```bash
# Install BATS (pick one)
brew install bats-core       # via Homebrew (macOS)
apt install bats             # via apt (Debian/Ubuntu)

# Run all BATS tests
bats tests/BATS/

# Run a single test file
bats tests/BATS/test_live_demo.bats

# TAP output (verbose, shows each test name)
bats tests/BATS/ --tap
```

## CI Integration

BATS tests run on every push and PR as the `unit:bats:shell` job in `.github/workflows/unit-tests.yml`. They run in parallel with pytest, linting, and security scans — see the workflow file for the exact image pin and installed dependencies.

## Adding New Tests

1. Create a new `.bats` file in this directory (e.g., `test_my_script.bats`)
2. Prefer functional tests — run bash logic, parse real files, validate output
3. Use `command -v tool &>/dev/null || skip "tool not installed"` for optional dependencies
4. Use `run bash -c '...'` for portable inline evaluation (avoids subshell variable issues)
5. The CI job auto-discovers all `.bats` files in this directory

## Test Design Philosophy

These tests prioritize functional correctness over string matching:

- **Parameter expansion**: Tests actually evaluate `${VAR:-default}` and `${VAR:+override}` patterns to verify defaults and overrides work
- **sed/awk transforms**: Tests run the real sed commands from the scripts against sample input (e.g., assumed-role ARN parsing)
- **jq queries**: Tests execute the actual jq expressions against the real `cdk.json` to verify feature detection
- **JSON validation**: Tests pipe payloads through `jq -e` to verify structure, not just syntax
- **YAML validation**: Tests use `python3 -c "import yaml; ..."` to parse manifests the same way Kubernetes would
- **Portability**: All tests use `bash -c` for inline evaluation, avoid GNU-only flags (like `head -n -1`), and skip gracefully when optional tools are missing
