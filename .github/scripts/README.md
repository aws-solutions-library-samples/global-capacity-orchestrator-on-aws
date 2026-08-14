# CI Helper Scripts

Helper scripts invoked by GitHub Actions workflows. Separated from the workflows themselves so they can be tested independently and reused across jobs.

## Table of Contents

- [Files](#files)
- [Podman Runtime Fallback](#podman-runtime-fallback)
- [Testing](#testing)
- [Adding a New Script](#adding-a-new-script)

## Files

| File | Invoked By | Description |
|------|------------|-------------|
| `validate_demo_gifs.py` | `security.yml` (`security:bandit:sast`) | Validates the exact tracked demo GIF allowlist before Bandit runs in the read-only security job. Rejects symlinks, disguised files, malformed block boundaries, missing trailers, appended payloads, and assets over reviewed byte/canvas/frame/pixel ceilings, then uses pinned Pillow to verify and fully decode every frame under a workflow timeout. |
| `dependency-scan.sh` | `deps-scan.yml` (monthly) | Checks pinned dependency surfaces, always runs deterministic accelerator catalog/NodePool/watch-list validation, and—with AWS credentials—compares the checked-in accelerator catalog with the live enabled-Region [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html) union. Writes one Markdown report and sets `has_drift=true` for version drift, policy findings, catalog drift, or operational check failures. |
| `lib_dependency_scan.sh` | `dependency-scan.sh` | Sourceable helper functions — image registry parsing (`parse_image_registry`), semver comparison (`compare_semver`), tag filtering (`is_semver_tag`, `is_project_image`), [Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) model-family/version helpers, and strict accelerator JSON summary parsing (`parse_accelerator_drift_count`). Extracted so BATS tests can exercise logic without running the full scan. |
| `check_pip_audit_ignore.py` | `security.yml` (`security:pip-audit:deps`) | Validates the project-local `.github/config/.pip-audit-ignore` suppression file. Fails the workflow when any entry's `exp:YYYY-MM-DD` marker is on-or-before today (inclusive) or when an entry is missing the marker entirely. Importable as a module (`check_file()`, `main()`) so it can be exercised by pytest fixtures rather than only ever run end-to-end through CI. |
| `check_npm_audit.py` | `security.yml` (`security:npm-audit:all-packages`) | Gates `npm audit --json` output for each package directory against `.github/config/.npm-audit-ignore`. A high-or-critical finding is suppressed only when a `package-dir\|package\|advisory\|node-path\|exp:YYYY-MM-DD` entry matches on all four identity fields, and compound records fail closed — every advisory/node pair in the finding must be covered, so no entry can act as a broad package-level mute. Expired entries (inclusive), duplicates, malformed lines, and suppressions that no longer match anything all fail, which forces stale entries out once upstream ships a fix. Importable (`check_report()`, `main()`) for pytest. |
| `use-pinned-npm.sh` | `security.yml`, `lint.yml`, `unit-tests.yml`, `integration-tests.yml`, `inference-streaming-proxy.yml`, `mooncake-image.yml`, `deps-scan.yml` | Installs and then verifies the exact npm release declared by `packageManager` in a `package.json`, so every job that shells out to npm uses one pinned version instead of whatever the runner image or `setup-node` happened to ship. Rejects a `packageManager` field that isn't an exact `npm@X.Y.Z` (no ranges), skips the global install when the running version already matches, and re-checks afterwards so a failed install fails the job rather than silently proceeding on the wrong npm. Takes the manifest path as `$1` (default `package.json`) — `inference-streaming-proxy.yml` runs it against that package's own manifest. |
| `validate_helm_charts.py` | `integration-tests.yml` (`integration:helm:charts-valid`) | Validates every `(chart, version)` pinned in `lambda/helm-installer/charts.yaml`. Structural checks run always (required fields, SemVer version, `oci://`/`use_oci` consistency), plus the Gateway API lockstep contract: the `aws-lbc-gateway` CRD bundle pinned in `lambda/helm-installer/handler.py` must match the `aws-load-balancer-controller` chart version. `--mode online` additionally resolves each chart at its exact pinned version (`helm show chart`), renders it (`helm template`), and verifies the pinned `gateway-api-standard` CRD bundle satisfies the `sigs.k8s.io/gateway-api` release named by the controller tag's own `go.mod` — upgrading the controller past its CRDs silently stops gateway reconciliation (caught live, 2026-08). The Kubeflow Trainer runtime lockstep rides the same split: offline, the shipped `torch-distributed` extraction (`post-helm-kubeflow-trainer-runtimes.yaml`), the `examples/kubeflow-trainjob.yaml` trainer image, and every `pytorch/pytorch` image in `docs/DISTRIBUTED_TRAINING.md` must agree; online, the extraction must reproduce what the pinned `kubeflow-trainer` chart actually ships (image called out explicitly, full spec compared modulo the two documented pod-template deviations — `automountServiceAccountToken: false` and NoNewPrivs on the `node` container — `trainer.kubeflow.org/*` labels preserved) — so a chart bump that skips re-extraction fails CI instead of silently running a stale runtime. Every entry is checked, including `enabled: false` charts. Importable (`validate_structure()`, `build_refs()`, `validate_online()`, `validate_gateway_lockstep()`, `validate_trainer_runtime_lockstep()`, `main()`) for pytest. |
| `validate_grafana_dashboards.py` | `grafana-dashboards.yml` (`grafana:dashboards:provision`) | Proves the Grafana version the pinned kube-prometheus-stack chart ships accepts the curated dashboard ConfigMaps, closing the sidecar pipeline's silent failure mode (a rejected dashboard just never appears in the UI). `extract` pulls every `grafana_dashboard="1"` JSON payload out of the applier manifests — resolving `{{UPPER_SNAKE}}` feature placeholders with the same UPPER_SNAKE-only pattern the applier uses, so Grafana's lowercase legend tokens survive — and emits the dashboards plus a sidecar-shaped file-provisioning provider; `chart-version` reads the `charts.yaml` pin so the workflow resolves the Grafana image from the deployment's own pin (no second pin to drift); `verify` waits for `/api/health` and requires every uid to answer 200 with a matching title and `meta.provisioned=true`. Importable (`extract_dashboards()`, `read_chart_pin()`, `verify()`) — `tests/test_grafana_dashboards.py` holds the extraction in lockstep with the applier's real planning path. |
| `validate_k8s_manifests.py` | `integration-tests.yml` (`integration:k8s:manifest-schema`) | Schema-validates (not just YAML-parses) the kubectl-applier manifests and the `examples/` gallery with [kubeconform](https://github.com/yannh/kubeconform). Exists to bridge two gaps kubeconform can't close alone: it renders the `{{PLACEHOLDER}}` tokens the applier Lambda substitutes at deploy time into schema-shaped stubs (raw, several aren't parseable YAML), and it excludes the non-Kubernetes files under `examples/` (the GCO-format `pipeline-dag.yaml`, JSON fixtures) by construction. CRDs the repo depends on (Karpenter, ALB Gateway API, Kueue, KEDA) resolve through the datreeio/CRDs-catalog as a second `-schema-location`; RayCluster, Volcano `Job`, and the Kubeflow Trainer v2 kinds (`TrainJob`, `ClusterTrainingRuntime`) aren't in that catalog yet and are explicitly `-skip`ped rather than reported as "no schema found" — the shipped trainer runtime gets stronger coverage from `validate_helm_charts.py`'s trainer runtime lockstep, which re-renders the pinned chart and requires spec-for-spec agreement. `--path` is repeatable and accepts a file, directory, or quoted glob. Exit `2` is reserved for I/O and argument errors so a missing binary is distinguishable from a real violation. Importable (`render_placeholders()`, `collect_target_files()`, `iter_target_files()`). |
| `verify_lambda_imports.py` | `integration-tests.yml` (`integration:lambda:imports`) | Discovers every tracked Python Lambda handler instead of relying on a hand-maintained directory list, then imports each handler in an isolated subprocess with a 30-second bound. Handles the two nonstandard entrypoints explicitly and fails when discovery or any import is incomplete. |
| `verify_container_tool_versions.py` | `integration-tests.yml` (`integration:docker:helm-installer`, `integration:docker:dev-container (amd64/arm64)`) | Reads the reviewed version pins from the helm-installer and development Dockerfiles, runs bounded checks inside the built images, and requires every installed Helm, kubectl, AWS CLI, and development tool version to match exactly. Valid-but-unreviewed versions fail rather than merely proving that a binary starts. |
| `functional_container_test.sh` | `integration-tests.yml` (`integration:docker:{cost-monitor,health-monitor,manifest-processor,inference-monitor,inference-proxy}`) | Boots a distroless service image under the deployment manifests' pod-equivalent constraints (read-only root, tmpfs `/tmp`, uid:gid 1000:1000, all capabilities dropped, no-new-privileges) and asserts its serving contract: waits for the readiness path, checks each `--probe "path=code[=body-substring]"` (liveness/readiness endpoints, auth fail-closed 503s, degraded-dependency 503s), runs each `--exec-python` payload via `docker exec` (the same mechanism kubelet uses for exec probes and `preStop` hooks — the images ship no shell, so this proves the manifests' `["python", "-c", ...]` command shapes work against the live container), optionally holds a `--min-uptime` stability window with dependencies unreachable, and requires `docker stop` to exit with `--expect-stop-exit`. The queue-processor job tests the consume/reject/exit-code contract inline against a moto SQS server instead — a one-shot KEDA ScaledJob has no serving state for this script to probe. |
| `verify_inference_streaming_bundle_freshness.py` | `unit-tests.yml` (`unit:cdk:synth`) | CI-only regression proving the *real* git-ignored `lambda/inference-streaming-proxy-build` bundle is rebuilt when it drifts from source. Deliberately damages the bundle two ways — rewriting `index.mjs` and deleting a transitive `node_modules` marker — then enters the production `StackManager.synth` and `StackManager.diff` paths and asserts each one restored it. Only `_run_cdk` is mocked, so no CDK process runs and no AWS calls are made, but the real pinned npm builder does execute. Kept out of the pytest suite because it needs the actual bundle and npm toolchain rather than a fixture. |
| `run-semgrep.sh` | `security.yml` (`security:semgrep:sast`) | Runs `semgrep scan --config auto --error` with repo-wide rule suppressions loaded from `.github/config/semgrep-excluded-rules.txt` — each non-comment, non-blank line becomes a `--exclude-rule` flag, so the suppression list lives in a reviewable data file instead of being hardwired into the workflow. POSIX `sh` (the semgrep container image is not guaranteed to ship bash). Tested by `tests/BATS/test_run_semgrep.bats`. |
| `dev_alias_live.sh` | `integration-tests.yml` (`integration:dev-alias:{docker,finch,podman,none}`) | Live proof that `scripts/setup-dev-alias.sh` builds the image and generates a working `gco` shell function. Drives `setup-dev-alias.sh` to build the real `gco-dev` image from `Dockerfile.dev` and install the generated function into a throwaway rc, sources it in a fresh shell, and proves through it: `gco --version` (the real CLI runs) and `gco dag validate ci-dag.yaml` (an offline command that reads a relative-path file from the mounted workspace, proving arg-forwarding, the `$PWD` -> `/workspace` bind mount, and `cwd=/workspace`). `--skip-build` reuses an existing image (and tells `setup-dev-alias.sh` to skip its build); `--no-runtime` proves the script refuses cleanly (non-zero exit, no rc block) when no runtime answers. |
| `podman_ci_config.sh` | `integration-tests.yml` (`integration:dev-alias:podman`) | Writes `~/.config/containers/containers.conf` for one of the two OCI runtime configurations that work for rootless podman on a GitHub runner, which has no systemd user session. Takes `crun` or `runc` as `$1`; the job writes the `crun` configuration during setup and then loops over both, re-invoking this script between attempts, so a runner whose podman/crun pairing is broken still gets a green build. Each configuration is a *pairing* that has to hold together: `crun` needs `cgroups = "disabled"` and pins no runtime, while `runc` pins `runtime = "runc"` and must leave cgroups alone, because podman rejects the combination with "requested OCI runtime runc is not compatible with NoCgroups". Prints the resolved config and runtime version for debugging, and exits non-zero when `runc` is requested but absent. See [Podman runtime fallback](#podman-runtime-fallback) for why this is two configurations rather than a retry. |

## Podman Runtime Fallback

`integration:dev-alias:podman` is the one job that can't just pick a container
runtime and trust it. Rootless podman on a GitHub runner has no systemd user
session, so the OCI runtime has to be configured to avoid sd-bus calls, and the
two configurations that achieve this each fail in a way the other survives:

| Configuration | Why it can fail | Why the other one covers it |
|---------------|-----------------|-----------------------------|
| `crun` — `cgroup_manager = "cgroupfs"` + `cgroups = "disabled"` | When the runner image's apt podman is newer than its crun, podman emits an OCI spec version crun doesn't recognize and every container dies at the first `RUN` with `unknown version specified`. | `runc` isn't subject to that version skew. |
| `runc` — `cgroup_manager = "cgroupfs"`, cgroups left at the default, `runtime = "runc"` | Depends on `runc` being installed, and can't be combined with `cgroups = "disabled"`. | `crun` is podman's default runtime and needs no extra package. |

Two things follow from this, and both are easy to undo by accident:

- **It is a fallback, not a retry.** The crun failure is deterministic inside a
  given runner — the same commit passes or fails purely on which image version
  the job landed on — so re-running the identical setup could never clear it.
  Wrapping the step in a generic retry would burn three attempts on a
  guaranteed failure.
- **Neither configuration can be reduced to a flag on the other.** Pinning
  `runtime = "runc"` while leaving `cgroups = "disabled"` in place is rejected
  by podman outright, which is why the script writes a whole file per
  configuration instead of patching one key.

`tests/BATS/test_podman_ci_config.bats` pins both pairings, including the
combination podman rejects, so a future simplification fails locally instead of
in CI.

## Testing

Shell scripts are tested by BATS:

```bash
# From the repository root
bats tests/BATS/test_dependency_scan.bats
bats tests/BATS/test_run_semgrep.bats
bats tests/BATS/test_dev_alias_live.bats
bats tests/BATS/test_podman_ci_config.bats

# The same deterministic accelerator guard run by normal CI and the monthly scan
python scripts/accelerator_catalog.py validate
pytest tests/test_accelerator_catalog.py -q
```

Python helpers ship with pytest tests under `tests/`:

```bash
# Validator coverage
pytest tests/test_pip_audit_ignore_validator.py -v
pytest tests/test_npm_audit_checker.py -v
pytest tests/test_helm_charts_validation.py -v
pytest tests/test_k8s_manifest_validation.py -v

# Guard that keeps the Files table above in sync with this directory
pytest tests/test_ci_scripts_readme_coverage.py -v
```

Two scripts are not covered by either suite, by design:

- `use-pinned-npm.sh` installs a global npm, so exercising it means mutating the
  toolchain of whatever machine runs it. Its behavior is proven by the nine jobs
  that call it: any drift between `packageManager` and the npm they run fails
  those jobs immediately.
- `verify_inference_streaming_bundle_freshness.py` *is* a test — it's the
  CI-only tier for bundle freshness, kept out of pytest because it needs the
  real git-ignored bundle and the pinned npm toolchain instead of a fixture.

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
6. Add a row to the [Files](#files) table above naming the invoking job.
   `tests/test_ci_scripts_readme_coverage.py` fails the build if you skip this,
   and fails the same way if you delete a script and leave its row behind.
