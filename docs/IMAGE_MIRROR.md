# Image Mirror

GCO can mirror third-party container images into the project's own Amazon ECR so the cluster pulls them from same-account ECR instead of a rate-limited public registry. Today the one consumer is **Volcano** (`docker.io/volcanosh/vc-*`), whose images live only on Docker Hub and gate its Helm install — but the mirror is a **general** tool, and this guide explains how to point another chart's image at it down the road.

This is the reference for the feature. For the quick "enable it in `cdk.json` and deploy" recipe, see [Customization Guide → Get Volcano's docker.io images off the rate-limited path](CUSTOMIZATION.md#get-volcanos-dockerio-images-off-the-rate-limited-path-ecr-mirror). For Volcano itself, see [Volcano Integration](VOLCANO.md).

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Why Mirror Instead of a Pull-Through Cache](#why-mirror-instead-of-a-pull-through-cache)
- [How It Works](#how-it-works)
- [Multi-Architecture Copy](#multi-architecture-copy)
- [Enable and Deploy](#enable-and-deploy)
- [Mirror Manually](#mirror-manually)
- [MCP Tools](#mcp-tools)
- [How to Add an Image to the Mirror](#how-to-add-an-image-to-the-mirror)
- [Operational Notes](#operational-notes)
- [Troubleshooting](#troubleshooting)
- [Reference](#reference)

## Why This Exists

A few add-on Helm charts pull their images straight from Docker Hub (`docker.io`). On GCO the most visible one is Volcano — its `vc-controller-manager`, `vc-scheduler`, and `vc-webhook-manager` images are published only to Docker Hub.

Two things make that a problem on a cold, private EKS Auto Mode cluster:

- **Docker Hub rate-limits anonymous pulls.** When a fresh cluster scales up and several nodes pull the same Volcano images at once, the anonymous pulls are slow and can be throttled. Volcano's Helm install blocks waiting for its pods to become ready, and a slow pull can push that past the installer's timeout, so the install retries — sometimes in a loop.
- **EKS Auto Mode nodes pull with a service-managed, pull-only role.** They already have permission to pull from the account's own `gco/*` ECR repositories, but they are not a place to wire in Docker Hub credentials.

Mirroring the images into `gco/*` ECR turns a slow, throttled, cross-internet pull into a fast, same-account ECR pull over the node role the cluster already has. It needs **no Docker Hub credential**.

## Why Mirror Instead of a Pull-Through Cache

ECR offers a pull-through cache (PTC) that lazily imports images from an upstream registry on first pull. It is the natural first instinct, but it does not fit here:

- **ECR PTC for Docker Hub requires a stored Docker Hub credential.** Anonymous Docker Hub pull-through is not supported, so a credential-free cache — the thing we want — is not an option.
- **EKS Auto Mode's pull-only, service-managed node role complicates cache-miss imports**, which is exactly when a PTC does its work.

A mirror sidesteps both. There is no credential to store, and the mirrored images are ordinary `gco/*` ECR repositories that inherit the project's existing node-pull access, cross-region replication, and trusted-registry allow-list. The trade-off is that the mirror is **static**: it is refreshed when the chart version changes rather than lazily on demand. In practice the refresh is automatic on deploy (see below), so the static nature is invisible day to day.

## How It Works

The mirror has three moving parts: the **source set**, the **copy**, and the **consumer override**.

1. **Source set.** [`cli/_image_mirror.py`](../cli/_image_mirror.py) builds the list of upstream images to mirror in `collect_source_refs()`. Volcano's entries are derived from [`lambda/helm-installer/charts.yaml`](../lambda/helm-installer/charts.yaml) — the per-component `*_image_name` fields under `volcano.values.basic` plus the pinned `image_tag_version` — so the mirrored tag is always exactly what Helm requests and never drifts from the deployed chart.
2. **Copy.** For each source ref `<registry>/<repo>:<tag>`, the mirror ensures an ECR repository `<ecr_namespace>/<repo>` exists and copies the image into `<account>.dkr.ecr.<region>.<url-suffix>/<ecr_namespace>/<repo>:<tag>`, preserving the full multi-arch manifest list. Copies are idempotent — a tag already present in ECR is skipped.
3. **Consumer override.** When the mirror is enabled, the regional stack ([`gco/stacks/regional_stack.py`](../gco/stacks/regional_stack.py)) injects a single Volcano Helm value override — `basic.image_registry` → `<account>.dkr.ecr.<region>.<url-suffix>/<ecr_namespace>` — into the `HelmInstallCharts` custom resource. Every Volcano image (controller, scheduler, admission webhook, and the pre-install admission-init hook) renders from `basic.image_registry`, so all of them resolve from the mirror.

The override creates **no** CloudFormation resources of its own — it is just a value passed to the existing Helm install path. The ECR repositories are created by the copy step, not by CDK.

```text
charts.yaml (volcanosh/vc-*:v1.15.0)
        │  collect_source_refs()
        ▼
docker.io/volcanosh/vc-scheduler:v1.15.0   ── copy (all arches) ──▶  <acct>.dkr.ecr.<region>.<url-suffix>/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0
        ▲                                                                         ▲
        │                                                  Volcano basic.image_registry override points here
        └── upstream (one-time, anonymous)                 so the cluster pulls from ECR, not docker.io
```

## Multi-Architecture Copy

The copy must preserve **every** architecture in the source image's manifest list. GCO clusters can run a mix of amd64 and arm64 (Graviton) nodes, and each node needs to find a matching image under the same tag. A naive `docker pull` / `tag` / `push` keeps only the build host's architecture and silently drops the rest, so it is **never** used.

Instead, the mirror picks a manifest-list-preserving strategy at runtime based on what the machine has, in this order:

1. **Docker Buildx** — `docker buildx imagetools create` (a registry-to-registry copy, no local pull).
2. **Finch / nerdctl** — `pull --all-platforms` then `push --all-platforms` (containerd preserves the manifest list).
3. **skopeo** — `skopeo copy --all` (daemon-less).

If none is available the mirror fails fast with guidance rather than producing a single-arch image. No new binary dependency is introduced — the mirror reuses whichever of these the environment already provides. The strategy selection lives in `resolve_copy_strategy()` in [`cli/_image_mirror.py`](../cli/_image_mirror.py).

## Enable and Deploy

The mirror is **on by default**, so a fresh `gco stacks deploy` auto-mirrors the images into ECR with no extra step — the `cdk.json` block below is the shipped default. To turn it off, set `enabled` to `false`.

**1. Default `cdk.json` config** (already on; the default `ecr_namespace` of `gco/dockerhub` is fine for most setups):

```json
{
  "context": {
    "volcano_image_mirror": {
      "enabled": true,
      "ecr_namespace": "gco/dockerhub"
    }
  }
}
```

**2. Deploy.** `gco stacks deploy <stack>` (and `deploy-all`) **auto-mirrors** the images into ECR for each target region right before the regional stack's Helm install — so a fresh install just works, with no separate manual step. The copy is idempotent and skips images already present, so a repeat deploy costs only a couple of ECR lookups:

```bash
gco stacks deploy gco-us-east-1
```

**3. Converge and check.** Helm charts converge asynchronously after the stack reports complete. If Volcano had previously failed, re-converge without touching the rest of the cluster:

```bash
gco stacks addons install -r us-east-1
gco stacks addons status -r us-east-1
```

`ecr_namespace` must start with `gco/` so the mirrored repos inherit the project's `gco/*` node-pull access, ECR replication, and trusted-registry allow-list. The toggle is validated at synth time — an `ecr_namespace` outside `gco/`, or an invalid ECR path, fails the synth fast.

## Mirror Manually

The auto-mirror on deploy covers the common case. Run the CLI directly when you want to pre-seed a region before enabling the toggle, or re-mirror after a version bump, out of band from a deploy:

```bash
gco images mirror --region us-east-1
gco images mirror --region us-east-1 --dry-run          # preview the plan only
gco images mirror --region us-east-1 --ecr-namespace gco/dockerhub
gco images mirror --region us-east-1 --no-skip-existing  # re-copy even if present
```

The command is a thin wrapper over the same [`cli/_image_mirror.py`](../cli/_image_mirror.py) core the deploy uses, so it reads the image set and pinned tag from `charts.yaml`, creates the `gco/<...>` repositories if needed, and copies with the same multi-arch strategy. Run it from a machine with a container runtime (Docker Buildx, Finch, or skopeo) and AWS credentials; the upstream pull from Docker Hub is anonymous and one-time.

## MCP Tools

The mirror is exposed through three MCP tools in the `images` group so an agent can inspect and drive it. They wrap the same `cli/_image_mirror.py` core as the CLI and deploy paths.

| Tool | Risk tier | Feature flag | What it does |
|------|-----------|--------------|--------------|
| `images_mirror_plan` | safe | (default-on) | Resolves the destination registry and repository for every managed image without creating repos or copying anything. Reports the `cdk.json` `enabled` toggle alongside the plan. |
| `images_mirror_status` | safe | (default-on) | Reports, per managed image, whether its tag already exists in ECR, plus top-level `all_mirrored` / `missing`. Confirms a deploy's auto-mirror populated everything before the Helm install runs. |
| `images_mirror` | image-upload | `GCO_ENABLE_IMAGE_PUBLISH` | Creates the destination repos and copies each image (multi-arch preserved, idempotent). The same operation the deploy runs automatically; invoke it to pre-seed or repair the mirror out of band. |

`images_mirror` shares the `GCO_ENABLE_IMAGE_PUBLISH` flag with `images_build` / `images_push` because, like them, it uploads image data to ECR. The two read-only tools are always registered. See [MCP Feature Flags](../gco_mcp/README.md#feature-flags).

Typical agent flow: call `images_mirror_status` to see whether the mirror is populated; if `all_mirrored` is false and the publish flag is set, call `images_mirror` to fill it in.

## How to Add an Image to the Mirror

The mirror is general — the set of images is a single list, and adding one is a two-step change. The authoritative how-to lives in the module docstring of [`cli/_image_mirror.py`](../cli/_image_mirror.py) ("HOW TO ADD AN IMAGE TO THE MIRROR"); the short version:

1. **Add the source ref.** Extend `collect_source_refs()` in `cli/_image_mirror.py` with either a static literal or a chart-derived ref:

   ```python
   # Static literal:
   refs.append("docker.io/bitnami/redis:7.4.1")

   # Or chart-derived, so the mirrored tag tracks the deployed chart version
   # (use _volcano_source_refs as the template):
   refs.extend(_my_chart_source_refs(charts_config))
   ```

2. **Point the consumer at the mirror.** Wire whatever pulls the image to `<registry_host>/<ecr_namespace>/<repo>` instead of the upstream — typically a Helm `image_registry` / `image` value override in [`gco/stacks/regional_stack.py`](../gco/stacks/regional_stack.py) (see `_configure_volcano_image_mirror` / `_helm_chart_value_overrides` for the Volcano example) or a manifest image reference.

The copy step needs no change — it mirrors whatever `collect_source_refs()` returns. Add a test case alongside the others in [`tests/test_image_mirror.py`](../tests/test_image_mirror.py) so the new ref's plan layout is asserted.

## Operational Notes

- **Fail-fast on deploy.** If an enabled mirror can't complete during `gco stacks deploy` (no container runtime, no network, missing credentials), the deploy aborts **before** CloudFormation runs — rather than bringing up a cluster whose Volcano images aren't in ECR yet.
- **Static, version-pinned mirror.** When you bump Volcano's chart `version` / `image_tag_version` in `charts.yaml`, the next `gco stacks deploy` mirrors the new tag automatically (or run `gco images mirror` to do it out of band). Old tags are left in place.
- **Pull-path only.** The mirror changes only *where* images are pulled from. Volcano's behavior, versions, and configuration are unchanged.
- **Per-region.** ECR is regional, so the mirror runs once per target region. `deploy-all` mirrors each region in `cdk.json`'s `regional` block; a single-region deploy mirrors just that region.
- **Idempotent and cheap to repeat.** Already-mirrored tags are skipped (`skip_existing`), so a steady-state deploy adds only a couple of ECR lookups.

## Troubleshooting

**Volcano is stuck `failed` or `pending` in `gco stacks addons status`.** Confirm the mirror is populated for the region, then re-converge:

```bash
gco images mirror --region us-east-1 --dry-run   # what should be mirrored
gco stacks addons install -r us-east-1                          # re-fire the Helm install
gco stacks addons status -r us-east-1
```

Via MCP, `images_mirror_status` returns the same presence check (`all_mirrored` / `missing`).

**`addons status` reads the per-chart state from SSM.** Each chart writes its outcome to `/gco/addons/<region>/<chart>` (e.g. `/gco/addons/us-east-1/volcano`), which the install Step Functions state machine updates as it converges. A value of `installed` means the chart is ready.

**Deploy aborted with a mirror error before CloudFormation.** That is the fail-fast guard. Check that the machine running the deploy has a container runtime (Docker Buildx, Finch, or skopeo) and valid AWS credentials, then re-run the deploy.

**A node can't pull a mirrored image.** Confirm `ecr_namespace` starts with `gco/` (only `gco/*` repos inherit the node-pull access and trusted-registry allow-list) and that the image is present with `images_mirror_status` or `aws ecr describe-images`.

## Reference

**Configuration** (`cdk.json` → `context.volcano_image_mirror`):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Turns the mirror (and the Volcano `image_registry` override + deploy auto-mirror) on. **On by default**; set to `false` to disable. |
| `ecr_namespace` | `gco/dockerhub` | Destination namespace under the account's ECR registry. Must start with `gco/`. |

**Source map:**

| File | Role |
|------|------|
| [`cli/_image_mirror.py`](../cli/_image_mirror.py) | General mirror core — source set, plan, multi-arch copy, status. The extension point. |
| [`cli/commands/images_cmd.py`](../cli/commands/images_cmd.py) | The `gco images mirror` operator CLI over the core. |
| [`cli/stacks.py`](../cli/stacks.py) | `gco stacks deploy` auto-mirror hook (runs before CDK). |
| [`gco/stacks/regional_stack.py`](../gco/stacks/regional_stack.py) | Reads the toggle and injects Volcano's `image_registry` override. |
| [`lambda/helm-installer/charts.yaml`](../lambda/helm-installer/charts.yaml) | Volcano image names and the pinned tag the mirror derives from. |
| [`gco_mcp/tools/images.py`](../gco_mcp/tools/images.py) | `images_mirror_plan` / `images_mirror_status` / `images_mirror` MCP tools. |

**Related docs:**

- [Customization Guide](CUSTOMIZATION.md#get-volcanos-dockerio-images-off-the-rate-limited-path-ecr-mirror) — the enable-and-deploy recipe in context.
- [Schedulers & Orchestrators](SCHEDULERS.md) — how add-on charts converge after deploy.
- [MCP Server](../gco_mcp/README.md#feature-flags) — the feature flag that gates `images_mirror`.
