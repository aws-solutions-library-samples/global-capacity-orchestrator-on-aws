# Maintenance Guide

Routine upkeep for GCO: adding new instance types, upgrading the EKS
Kubernetes version, refreshing base-image security patches, renewing CVE
suppressions, and acting on the monthly dependency scan.

This guide is the *how*. The monthly [`deps-scan` workflow](../.github/CI.md#dependency-scan-script)
is the *when* — it opens an issue when a pinned version falls behind upstream.
Some lists here (instance families, for example) are deliberately not
auto-bumped: adding hardware is a human decision, so the scan leaves them to
this runbook.

## Table of contents

- [Maintenance at a glance](#maintenance-at-a-glance)
- [Adding a new instance type or family](#adding-a-new-instance-type-or-family)
- [Upgrading the EKS Kubernetes version](#upgrading-the-eks-kubernetes-version)
- [Refreshing base-image security patches](#refreshing-base-image-security-patches)
- [Renewing CVE suppressions](#renewing-cve-suppressions)
- [Routine dependency bumps](#routine-dependency-bumps)
- [Maintaining the MCP server](#maintaining-the-mcp-server)

## Maintenance at a glance

| Cadence | Task | Trigger |
|---------|------|---------|
| Monthly | Review the dependency-scan issue and bump flagged pins | Automated `deps-scan` issue |
| When AWS ships new hardware | Add instance types / families to the lists below | AWS launch announcement |
| When the scan flags EKS standard-support ending (or ~yearly) | Upgrade the EKS Kubernetes minor | `deps-scan` **EKS Kubernetes Version** row |
| When the scan flags an epoch older than 45 days (or Trivy finds an OS CVE) | Bump the base-image security epoch | `deps-scan` **Base-image Security Epochs** row |
| ~30 days before a suppression `exp:` date | Renew or drop the CVE suppression | `deps-scan` **Suppression Expiries** row |

## Adding a new instance type or family

There is no single list of instance types. Which files you touch depends on
*why* you are adding the hardware — CLI capacity analysis, a training pool, a
serving pool, and so on. Use the table, then follow the matching recipe.

### Where instance types live

| Purpose | File(s) to edit | Format |
|---------|-----------------|--------|
| CLI capacity/spot analysis, `gco capacity` | `cli/capacity/models.py` → `GPU_INSTANCE_SPECS` | Python dict: exact type → `InstanceTypeInfo(vcpus, mem, gpu_count, gpu_type, gpu_mem, arch)` |
| Default set the capacity advisor probes | `cli/capacity/advisor.py` → default `instance_types` list | Python list of exact types |
| x86 general GPU pool | `lambda/kubectl-applier-simple/manifests/40-nodepool-gpu-x86.yaml` | Karpenter `instance-family` values |
| ARM64 GPU pool | `.../41-nodepool-gpu-arm.yaml` | Karpenter `instance-family` values |
| Inference-optimized GPU pool | `.../42-nodepool-inference.yaml` | Karpenter `instance-family` values |
| EFA distributed-training pool | `.../43-nodepool-efa.yaml` | Karpenter `instance-family` values |
| Neuron / Trainium / Inferentia pool | `.../44-nodepool-neuron.yaml` | Karpenter `instance-family` values |
| Curated ≥80 GB FP8 serving pool (Mooncake) | `.../46-nodepool-mooncake-efa.yaml` | Karpenter `instance-family` values |
| Example jobs that pin a family | `examples/*.yaml` (e.g. `trainium-job.yaml`, `inferentia-job.yaml`, `megatrain-sft-job.yaml`, `inference-sglang.yaml`) | `nodeSelector` / affinity |
| Human-facing lists (keep accurate, not load-bearing) | `gco/stacks/regional_stack.py` (nodepool comment block), `README.md`, `docs/CUSTOMIZATION.md` | Prose / comments |

`45-nodepool-cpu-general.yaml` selects by category and generation rather than a
family list, so it does **not** need per-hardware edits.

> **Instance-family gotcha:** EKS Auto Mode labels a node with the exact family
> segment AWS uses in its catalog, and a bare entry only matches that one
> family. `p5`, `p5e`, and `p5en` are three separate families; `p6-b200`,
> `p6-b300`, and `p6e-gb200` are three more. Enumerate every generation you
> want — see the header note in `43-nodepool-efa.yaml`.

### Recipe: a new GPU instance type visible to the CLI

1. Add an entry to `GPU_INSTANCE_SPECS` in `cli/capacity/models.py`. The
   `gpu_type` field (for example `H100`, `B200`) is what ties the accelerator
   to its family, so fill it in accurately.
2. If the type belongs in the advisor's default probe set, add it to the
   `instance_types` list in `cli/capacity/advisor.py`.
3. Update the tests that assert on the specs (see [below](#tests-that-must-change-together)).

### Recipe: a new accelerator family for a node pool

1. Add the family string to the `values` list under the
   `eks.amazonaws.com/instance-family` requirement in the matching NodePool
   manifest from the table above.
2. If the family is a new GPU generation, confirm the pool's other
   requirements still apply (GPU manufacturer, architecture, EFA taint).
3. For a serving-tier GPU, add it to `46-nodepool-mooncake-efa.yaml` **only** if
   it is a ≥80 GB FP8-capable part, and keep `p4d` out of that curated pool.
4. Refresh the human-facing lists (the nodepool comment block in
   `regional_stack.py`, the `README.md` nodepool summary, and
   `docs/CUSTOMIZATION.md`) so they stay accurate.

### Tests that must change together

These tests assert on the instance lists and will fail if you edit a list
without updating them:

- `tests/test_cli.py` — expects `GPU_INSTANCE_SPECS` to contain the baseline
  types (`g4dn.xlarge`, `g5.xlarge`, `p3.2xlarge`, `p4d.24xlarge`).
- `tests/test_capacity_history_config.py` — derives the family set from the
  spec keys and asserts a baseline set is present.
- `tests/test_mooncake_nodepool_manifest.py` — pins the pool 46 family set
  **exactly** (`_EXPECTED_FAMILIES`) and requires `p4d` to stay in pool 43.
  Edit this whenever you change either pool's families.
- `tests/test_inference.py` — asserts an example's `nodeSelector` family
  (for example `inf2`); update if you change the referenced example.
- `tests/test_integration.py` — validates the shape of every manifest under
  `lambda/kubectl-applier-simple/`, so a new pool must keep the NodePool schema.

Run the focused set after editing:

```bash
pytest tests/test_cli.py tests/test_capacity_history_config.py \
  tests/test_mooncake_nodepool_manifest.py tests/test_inference.py \
  tests/test_integration.py
```

## Upgrading the EKS Kubernetes version

The version lives in one place — `cdk.json` `context.kubernetes_version` (for
example `1.36`) — and flows through `gco/config/config_loader.py`
(`get_kubernetes_version()`) into `GCORegionalStack`, which resolves it to
`eks.KubernetesVersion.V1_<minor>` (falling back to `.of()` if the installed
`aws-cdk-lib` does not yet expose that enum). Several pinned tools track the
same minor and are guarded by tests, so a partial bump fails CI rather than
shipping a skew.

### Files to change, in order

1. `cdk.json` — set `context.kubernetes_version` to the new minor.
2. `pyproject.toml` — bump `kubernetes==<minor>.x` so the Python client's major
   equals the cluster minor (for example `kubernetes==37.*` for EKS `1.37`),
   then regenerate the lock:

   ```bash
   pip-compile --all-extras --strip-extras -o requirements-lock.txt pyproject.toml
   ```

3. `gco/stacks/constants.py` — update the five `EKS_ADDON_*` constants to builds
   published for the new minor (see [validating add-ons](#validating-add-on-versions)).
4. Confirm the pinned `aws-cdk-lib` exposes `eks.KubernetesVersion.V1_<minor>`.
   If it does not, bump `aws-cdk-lib` in `pyproject.toml` and re-lock; otherwise
   the stack silently uses the `.of()` fallback.
5. kubectl pins — bump to a patch of the new minor in all three spots, staying
   within one minor of the cluster:
   - `Dockerfile.dev` (`ARG KUBECTL_VERSION`)
   - `lambda/helm-installer/Dockerfile` (the `dl.k8s.io/release/...` URL)
   - `.github/workflows/deps-scan.yml` (`KUBECTL_VERSION` env)
6. Helm pins — bump `HELM_VERSION` / the `get.helm.sh` URL in the same
   `deps-scan.yml` and `lambda/helm-installer/Dockerfile` if a new Helm is
   needed for the minor.
7. `.github/workflows/integration-tests.yml` — bump the kind `node_image`
   (`kindest/node:v<minor>.<patch>`) so CI exercises the new control plane.
8. `.github/config/.trivyignore` — revisit any suppressions tied to the old
   kubectl/helm binaries; several entries clear once the pins move.
9. `tests/test_config_loader.py` and `tests/test_config_loader_validation.py` —
   update the hardcoded default minor.

### Validating add-on versions

The `EKS_ADDON_*` builds are `-eksbuild.N` releases tied to a specific minor.
Query the ones supported for the new minor (recipe also in
[CONTRIBUTING.md](../CONTRIBUTING.md)):

```bash
K8S_VERSION="1.37"  # the minor you are moving to
for addon in eks-pod-identity-agent metrics-server aws-efs-csi-driver \
             amazon-cloudwatch-observability aws-fsx-csi-driver; do
  echo "=== $addon ==="
  aws eks describe-addon-versions \
    --addon-name "$addon" \
    --kubernetes-version "$K8S_VERSION" \
    --query 'addons[0].addonVersions[0].addonVersion' \
    --output text
done
```

### Version-skew rules

- **kubectl** must stay within ±1 minor of the cluster (the standard Kubernetes
  skew policy — see the comment on `ARG KUBECTL_VERSION` in `Dockerfile.dev`).
- **kubernetes Python client**: its major must equal the cluster minor
  (`kubernetes==36.x` ↔ EKS `1.36`). Enforced by
  `tests/test_integration.py::test_kubernetes_python_client_matches_eks_version`.
- **kubectl in the helm-installer image** must match `cdk.json`. Enforced by
  `tests/test_integration.py::test_kubectl_version_matches_eks_version`.

### Deploy and verify

1. Run the guard tests first — they catch a partial bump immediately:

   ```bash
   pytest tests/test_integration.py tests/test_config_loader.py \
     tests/test_config_loader_validation.py
   ```

2. Deploy to a non-production account/region and confirm the cluster reaches
   the new version and every add-on reconciles.
3. Watch the first NodePool scale-up so a new node image / add-on combination
   does not regress GPU or EFA scheduling.
4. Roll out to remaining regions once non-prod is healthy.

## Refreshing base-image security patches

The container images pull OS security patches at build time behind a
hand-bumped epoch ARG that busts the CI layer cache. Bump the date to force a
rebuild that pulls the latest fixes. The dependency scan flags an epoch older
than 45 days; Trivy's container scan is the backstop.

- `APT_SECURITY_EPOCH` (Debian images): `Dockerfile.dev` and the four
  `dockerfiles/*-dockerfile` service images.
- `DNF_SECURITY_EPOCH` (Amazon Linux 2023): `lambda/helm-installer/Dockerfile`.

Set the ARG default to today's date (`YYYY-MM-DD`), rebuild, and re-run the
container scan to confirm the previously flagged packages are gone.

## Renewing CVE suppressions

`.github/config/.trivyignore` and `.github/config/.pip-audit-ignore` entries
each carry an `exp:YYYY-MM-DD` marker and a justification. The rules:

- The dependency scan surfaces entries expiring within 30 days so you can act
  early; the CI validator hard-fails a build on the expiry date itself.
- When an entry is due, re-check upstream. Drop it if the fix has shipped
  (and bump whatever pin carries the fix); otherwise extend the date with a
  fresh justification — never extend blindly.
- Keep both files short. A growing ignore file is a smell.

## Routine dependency bumps

The monthly [`deps-scan`](../.github/CI.md#dependency-scan-script) issue lists
every surface that has drifted (Python packages, Docker images, Helm charts,
EKS add-ons, CI tooling, and more), grouped with an urgency hint and per-row
links to the upstream changelog. To act on it:

1. Follow the report's **Ref** links to review changelogs for breaking changes.
2. Update the version in `pyproject.toml`, the manifest, `charts.yaml`, or the
   pinned `*_VERSION` env/ARG value.
3. Regenerate `requirements-lock.txt` if Python dependencies changed.
4. Reconcile any **Version Consistency** rows so every copy of a pin agrees.
5. Run the test suite locally, then open a PR.

Python dependencies are intentionally not tracked by Dependabot — they are
pinned through `requirements-lock.txt` with `pip-compile` and reviewed
deliberately. GitHub Actions and Docker base images *are* tracked by Dependabot;
see [Dependabot](../.github/CI.md#dependabot) for the split.

## Maintaining the MCP server

The in-tree MCP server (`gco_mcp/`) exposes the docs, example manifests, and
`gco` operations to agents. A few catalogs must stay in sync with the rest of
the repo or CI fails — most of this is "when you add X, register it in Y":

| When you… | Update | Guard |
|-----------|--------|-------|
| Add a `docs/*.md` guide | `DOC_METADATA` in `gco_mcp/resources/docs.py` (keep `topics` from the existing small vocabulary; every `related` entry must reference a real key) | `tests/test_mcp_docs_index.py` |
| Add an `examples/*.yaml` manifest | `EXAMPLE_METADATA` in `gco_mcp/resources/docs.py` | `find_examples` discovery |
| Add a package README meant for agents | `PACKAGE_DOC_METADATA` in `gco_mcp/resources/docs.py` | `tests/test_mcp_docs_index.py` |
| Add or rename an MCP tool | the Tool Reference table (and the per-module count) in `gco_mcp/tools/README.md` | `tests/test_docs_coverage.py` |
| Gate a tool behind a feature flag | `gco_mcp/feature_flags.py`, and document the flag in `gco_mcp/README.md` | — |

Notes:

- **Version** — the MCP server version tracks the project `VERSION` through
  `gco_mcp/version.py`; there is no separate MCP version to bump.
- **Dependency** — `fastmcp` is pinned in `pyproject.toml` and covered by the
  monthly dependency scan (Python packages) and the weekly CVE scan. After
  bumping it, re-run the MCP install smoke (`unit:mcp:install`) to confirm the
  server still launches and registers its tools.
- This guide is itself registered in `DOC_METADATA`, so `find_docs` surfaces it
  to agents — the same step every new guide needs.
