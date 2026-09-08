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
| `test_live_demo.bats` | `demo/live_demo.sh` + `demo/lib_demo.sh` | 45 | Sources `lib_demo.sh` and calls real display, feature-detection, pause, command-status, bounded global inference-generation, fallback cleanup, lifecycle reporting, and ARN helpers. Validates manifest YAML/namespace targeting, fleet-overview `gco status --with-costs --with-policy`, the MCP/CLI policy boundary, the current vLLM image, fail-closed inference lifecycle, cleanup, and section completeness |
| `test_lib_demo.bats` | `demo/lib_demo.sh` | 59 | Sources the shared helpers and exercises exact-SHA/account authorization, the six-artifact dirty allowlist, authorized EKS endpoint matching, atomic owner-file recording locks including handled acquisition signals, account/access-key sanitization and independent verification, Unicode normalization, agg argv/font overrides and real default-canvas headroom, wait behavior, ARN helpers, and transactional pair publication/rollback. Also covers the run-scoped enablement overrides: `demo_feature_forced` exact-name matching (no substring false-positives), `detect_features` leaving committed defaults alone, applying `GCO_DEMO_ENABLE` one-way so a configured-on feature is never disabled, and `verify_enablement_overrides` rejecting a typo with a single-line error rather than a traceback. The lower-level sanitizer bypass remains covered for isolated helper debugging; publishable recorders reject it. |
| `test_setup_cluster_access.bats` | `scripts/setup-cluster-access.sh` | 20 | Argument defaults and overrides, sources `lib_demo.sh` for ARN helpers (`is_assumed_role`, `extract_role_name`, `build_role_arn`), error handling patterns, AWS CLI call structure |
| `test_setup_dev_alias.bats` | `scripts/setup-dev-alias.sh` | 53 | Functional tests with PATH-shimmed container runtimes: detection precedence (docker > finch > podman via `<rt> info`), `GCO_CONTAINER_RUNTIME` / `CDK_DOCKER` / `--runtime` override precedence, per-runtime socket selection (docker & podman mount a socket, finch omits it), `--print` block emission (markers, TTY `-it` vs non-TTY `-i` branches, custom `--image`, no file writes), idempotent rc-file install (exactly one marked block, preserves existing content, in-place block replacement), and an end-to-end check that the emitted `gco` function forwards args and mounts the workspace, plus podman qualifying a bare image as `localhost/<name>` while leaving registry-qualified names untouched. Credential coverage: every AWS variable is forwarded by *name only* (`-e NAME`, never `-e NAME=`, so an unset host variable can't blank the container's), profile/region/static-key/session/role/web-identity variables appear on both TTY branches of every runtime, an exported `AWS_PROFILE` reaches the runtime argv, `~/.aws` is read-only by default and read-write under `--aws-writable`, and the function pre-creates `~/.aws` so env-only/OIDC/IMDS auth still has a mount source. Plus `--uninstall` (removes only the managed block, no-op success when absent, works with no container runtime installed) and fish-shell handling (fails loudly rather than writing a block fish can't parse into a file it never reads; `--rc` still forces an explicit target) |
| `test_podman_ci_config.bats` | `.github/scripts/podman_ci_config.sh` | 17 | Pins both OCI runtime configurations the `integration:dev-alias:podman` job alternates between, because each fails in a way the other survives and the *pairing* is what matters: the crun config must set `cgroups = "disabled"` (rootless CI has no systemd user session) and pin no runtime, while the runc config must set `runtime = "runc"` and must **not** disable cgroups — podman rejects that combination outright with "requested OCI runtime runc is not compatible with NoCgroups". Also covers argument handling (missing argument, unknown runtime exiting 2 without writing a file), that the config is written under `$HOME` with the directory created (the job relies on rewriting it between attempts), that a second call fully replaces the previous configuration rather than leaving stale keys, the version echo used for debugging, the clear failure when `runc` is absent (PATH carrying only the utilities the script needs, so the lookup genuinely misses), and that the emitted file contains nothing but section headers and `key = "value"` lines. `runc` is faked with a PATH shim so no real runtime is required |
| `test_dev_alias_live.bats` | `.github/scripts/dev_alias_live.sh` | 7 | Static checks (`bash -n`, shellcheck) plus the container-less paths: `--help` header output, the unknown-option / `--image`-without-value / no-argument usage errors, and the `--no-runtime` refusal (masks docker/finch/podman with PATH stubs that fail `<rt> info`, then asserts the setup script refuses and writes no rc block). The live docker/finch/podman proofs that build gco-dev and run the generated function execute in `integration-tests.yml`. |
| `test_dependency_scan.bats` | `.github/scripts/dependency-scan.sh` + `lib_dependency_scan.sh` | 294 | Functional tests that source `lib_dependency_scan.sh` and call real helpers: `parse_image_registry` (nvcr.io / gcr.io / quay / ghcr / registry.k8s.io / public.ecr.aws / docker.io defaulting), `is_semver_tag`, `is_project_image`, `compare_semver` (two- and three-part versions, v-prefix handling), `extract_aurora_versions` / `extract_emr_versions` / `extract_eks_addons` (constants-module happy path + regex fallback), `extract_k8s_version`, `extract_dockerfile_pins` (allowlist filter, inline-comment stripping, commented-out ARG skipping, empty file / missing file), `extract_npm_direct_pins` (dependencies + devDependencies, range/tag specifiers skipped, `packageManager`/`engines`/`overrides` ignored, missing/malformed files empty, every `list_npm_package_dirs` graph yields pins), `extract_python_extras` (every `[project.optional-dependencies]` group listed, bare-name output format, count agrees with tomllib, missing/malformed files empty — feeds the all-extras install so extras-only pins like `aws-cdk-lib` reach the drift report), `extract_security_epochs` (APT/DNF epoch date parsing, commented-epoch skipping), `extract_precommit_hooks` (skips `local`/`meta` repos, skips entries with no `rev:`, malformed YAML), `get_latest_precommit_hook_release` (rejects non-GitHub hosts and malformed paths, tolerates `.git` and trailing-slash forms, with `curl`-shimmed fixtures for the tag-parsing branches), `check_image_digest_consistency` (the committed tree pins every digest once; two digests under one tag are reported naming both files; a reference split across adjacent string literals — the shape the real stale copy had, invisible to a line-at-a-time matcher — is still matched; agreeing copies, distinct tags, and bare tags stay silent; sibling worktrees and generated trees are excluded; URLs and `host:port` pairs are not mistaken for images), `check_lambda_requirements_pins` (the committed Lambda copies are in lockstep; a stale pin is reported against `pyproject.toml`, an optional group, or a lock-only transitive; agreeing / comment-only / Lambda-only-dependency files stay silent; the generated `*-build` staging bundles are skipped so findings are neither doubled locally nor lost in CI; an unreadable `pyproject.toml` is a finding rather than a pass; a missing lockfile narrows coverage instead of abandoning the check; PEP 503 name normalisation and inline-comment stripping) |
| `test_runbooks.bats` | `docs/RUNBOOKS.md` Root-State Recovery | 4 | Pins the ALB-to-listener command order with realistic `loadbalancer/app/...` and `listener/app/...` ARN fixtures; proves one matching listener succeeds and no matching listener fails closed |
| `test_run_semgrep.bats` | `.github/scripts/run-semgrep.sh` | 10 | Stubs `semgrep` on `PATH` to record the argv the wrapper builds: one `--exclude-rule` per non-comment, non-blank line of the suppression file, comment/blank lines skipped, an inline comment stripped to the bare rule id, a missing suppression file degrading to no excludes (while still scanning), the committed default file no longer suppressing `github-actions-mutable-action-tag` (actions are SHA-pinned, so re-suppressing it would mask a regression) while its all-comment form still assembles a clean scan, caller-supplied extra args forwarded, and the assembled command scanning `.` with `--json -o semgrep-report.json`. Plus `sh -n` syntax and shellcheck static checks. |
| `test_aws_cli_examples.bats` | `docs/client-examples/aws_cli_examples.sh` | 17 | API region detection with fallback chain, URL trailing-slash stripping, JSON payload validation, trusted registry enforcement, [SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html) signing patterns |
| `test_curl_sigv4_proxy.bats` | `docs/client-examples/curl_sigv4_proxy_example.sh` | 27 | URL host/path parsing pipeline, stack name construction, proxy lifecycle (port check, trap, kill), HTTP method coverage, Host header inclusion, auth failure testing, temp file cleanup |
| `test_record_demo.bats` | `demo/record_demo.sh` | 18 | Guarded live authorization, exact output/default contracts, repository-bound CLI wrapper, asciinema `--return`, private kubeconfig propagation/cleanup, sanitize→verify→render→transactional publish ordering, cloud-offline render-existing success, missing-agg and sanitizer-bypass preservation, wrong-kube-context refusal before recording, region-override binding, isolated post-preflight context-drift refusal before namespace cleanup, and that `GCO_DEMO_ENABLE` is validated in preflight and exported before recording so the child's `detect_features` sees it |
| `test_record_deploy.bats` | `demo/record_deploy.sh` | 29 | Syntax/ShellCheck, guarded live consent/SHA/account delegation, 140×37/15× defaults, repository-bound deploy argv and exit propagation, staging/rollback, sanitize→verify→render→publish ordering, offline rendering, missing-agg preservation, and recorder-level sanitizer-bypass rejection. Also pins the run-scoped override plumbing against a fake `python3` that records the recorded command's argv: `GCO_DEMO_ENABLE` arrives as a single `--enable <value>` pair (not word-split), omitting it records a bare `deploy-all`, and an invalid value aborts in preflight before any AWS or asciinema call |
| `test_record_destroy.bats` | `demo/record_destroy.sh` | 28 | Syntax/ShellCheck, guarded live consent/SHA/account delegation, 116×36/10× defaults, repository-bound destroy argv and exit propagation, staging/rollback, sanitize→verify→render→publish ordering, offline rendering, missing-agg preservation, recorder-level sanitizer-bypass rejection, correct destroy embed text, and that `GCO_DEMO_ENABLE` reaches the recorded teardown as a single `--enable` pair so it evaluates the same app the recorded deploy did |
| `test_gif_to_mp4.bats` | `demo/gif_to_mp4.sh` | 13 | Static checks (`bash -n`, shellcheck) plus argument handling (usage on no args / `--help`, missing-ffmpeg guidance via a stripped `PATH`, missing/non-`.gif` input, identical input/output rejection, non-numeric `MP4_FPS`) and the assembled ffmpeg argv via a `PATH`-shimmed encoder that logs its arguments: default `.gif`→`.mp4` output naming, explicit output path, the H.264 compatibility flags (`yuv420p`, `+faststart`, even-dimension `trunc(iw/2)*2` scaling), and `MP4_FPS` frame-rate override |

Total: **641 tests** across 15 files.

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

BATS tests run on every push and PR as the `unit:bats:shell` job in `.github/workflows/unit-tests.yml`. Their focused ShellCheck assertions are optional local checks and skip when `shellcheck` is absent. The authoritative pinned gate is `lint:shellcheck:shell` in `.github/workflows/lint.yml`; it runs ShellCheck 0.11.0 at `style` severity with external sources enabled over every tracked `*.sh` path using NUL-safe Git-index discovery.

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
