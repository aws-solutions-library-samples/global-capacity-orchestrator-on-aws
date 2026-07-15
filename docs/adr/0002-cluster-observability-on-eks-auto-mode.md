# 0002. Cluster observability on EKS Auto Mode

- **Status:** Proposed
- **Date:** 2026-07-14
- **Deciders:** GCO maintainers
- **Supersedes:** none
- **Superseded by:** none

## Context

GCO ships an opt-in in-cluster observability stack (kube-prometheus-stack:
Prometheus, Grafana, Alertmanager, kube-state-metrics, and node-exporter), gated
by `cluster_observability.enabled` in `cdk.json` and installed per Region by the
regional stack. The goal is first-class GPU, scheduler/queue, and GCO-service
metrics in Grafana without standing up anything outside the cluster.

The clusters run **EKS Auto Mode**, whose managed node lifecycle and runtime
driver injection break several of the "obvious" configurations. These were found
the hard way — by validating live on a g4dn (T4) Auto Mode node — and each is
non-obvious enough that a future maintainer would otherwise re-derive it from a
production incident:

- **NVIDIA driver injection.** Auto Mode injects the GPU driver at runtime rather
  than baking it into the AMI. A stock DCGM exporter cannot find NVML (the driver
  libraries are not on the default loader path), and its profiling module fails
  to initialize on the injected driver ("the third-party Profiling module
  returned an unrecoverable error").
- **Node lifecycle via Karpenter.** Auto Mode deprovisions nodes through
  Karpenter. Pods annotated `karpenter.sh/do-not-disrupt: "true"` are held until
  a node's `terminationGracePeriod` (24h here) elapses — which interacts badly
  with DaemonSets.
- **Authenticated metrics.** Kueue exposes controller-manager metrics only over
  HTTPS behind a SubjectAccessReview, so an anonymous scrape is rejected 401/403.

## Decision

We will operate the in-cluster observability stack with the following EKS
Auto Mode–specific choices. Together they are what makes the stack actually work
on Auto Mode; the feature remains gated and off by default.

1. **DCGM GPU exporter.** Run `nvcr.io/nvidia/k8s/dcgm-exporter` as a DaemonSet on
   GPU nodes with the runtime-injected driver made reachable — `LD_LIBRARY_PATH`
   pointing at the injected driver libs and `NVIDIA_DRIVER_CAPABILITIES=all` — and
   with `CAP_SYS_ADMIN` added while every other capability is dropped
   (`allowPrivilegeEscalation: false`), which NVML requires to initialize under
   Auto Mode. Collectors are scoped to device-only `DCGM_FI_DEV_*` counters to
   sidestep the profiling module that cannot initialize. The image is pinned to a
   **plain-semver tag** (`4.8.3`, a distroless multi-arch build), deliberately not
   the compound `X.Y.Z-A.B.C-ubuntu22.04` form, so the monthly dependency scan
   tracks it for drift through its generic image check.
2. **Kueue metrics scrape.** Scrape Kueue's controller-manager metrics Service
   over HTTPS with the ServiceMonitor presenting the pod ServiceAccount bearer
   token (`insecureSkipVerify` for the in-cluster cert), and bind Prometheus's
   ServiceAccount to Kueue's `kueue-metrics-reader` ClusterRole via a dedicated
   post-Helm manifest so the authenticated endpoint authorizes the scrape.
3. **Discovery split.** Service-fronted schedulers/operators (KEDA, Volcano,
   Kueue, KubeRay, YuniKorn) and DCGM are scraped via `ServiceMonitor`s; GCO's own
   multi-replica services are scraped per-pod via `PodMonitor`s.
4. **No `do-not-disrupt` on DaemonSets.** `karpenter.sh/do-not-disrupt` is applied
   only to the singleton/stateful observability pods (Prometheus, Alertmanager,
   kube-state-metrics, the operator) that we do not want voluntarily consolidated.
   It is **never** applied to node-exporter or any other DaemonSet: a DaemonSet
   runs on every node, so the annotation pins the entire fleet against graceful
   termination until the `terminationGracePeriod`. A regression guard enforces
   this for both the chart values and the shipped manifests.
5. **Manifest organization (housekeeping).** The kubectl-applier manifest set uses
   a documented decade-block numbering convention applied in sorted filename
   order; it was renumbered to remove accidental gaps/duplicates as part of this
   work. This is cosmetic and carries no runtime behavior — see the manifests
   [README](../../lambda/kubectl-applier-simple/manifests/README.md).

## Consequences

### Positive

- GPU (DCGM), scheduler/queue, and GCO-service dashboards work on EKS Auto Mode
  out of the box when observability is enabled.
- Node consolidation, scale-down, spot reclaim, and manual NodeClaim deletion are
  no longer blocked fleet-wide by a DaemonSet annotation.
- The DCGM image is visible to the monthly dependency scan, so it ages like every
  other pinned image instead of silently drifting.

### Negative

- The DCGM DaemonSet holds `CAP_SYS_ADMIN`. We accept this narrowly: it is the
  minimum NVML needs on Auto Mode, every other capability is dropped, privilege
  escalation is disabled, and the security-scanner suppressions carry an inline
  justification pointing here.
- Device-only counters mean the profiling-module metrics are unavailable.
- The Kueue scrape uses `insecureSkipVerify` against the in-cluster serving cert;
  acceptable for intra-cluster traffic, but not certificate-validated.

### Neutral

- Observability stays opt-in and off by default; disabled clusters carry none of
  these resources (the manifests skip on the unreplaced feature-gate placeholder).
- `do-not-disrupt` on the singleton observability pods still delays *their* node's
  graceful termination up to the grace period — an intentional trade-off to keep
  the observability control plane from being consolidated out from under itself.

## Alternatives considered

### DCGM image — compound `X.Y.Z-A.B.C-ubuntu22.04` tag

- **Summary:** the vendor's ubuntu-based compound tag.
- **Why not:** invisible to the dependency scan's semver-based image check, and
  heavier than the validated distroless multi-arch `4.8.3` build.

### DCGM without `CAP_SYS_ADMIN`

- **Summary:** run the exporter unprivileged.
- **Why not:** NVML fails to initialize against the runtime-injected driver on
  Auto Mode (validated live), so no GPU metrics are produced.

### Keep `do-not-disrupt` on node-exporter / shorten `terminationGracePeriod`

- **Summary:** leave the chart default, or blunt its impact with a shorter grace.
- **Why not:** the annotation on a per-node DaemonSet blocks every node's graceful
  termination; shortening the grace would instead force-kill legitimately
  protected long-running training jobs. Removing it from the DaemonSet is the
  correct fix.

### Scrape Kueue metrics over plain HTTP

- **Summary:** point the ServiceMonitor at an unauthenticated endpoint.
- **Why not:** Kueue only exposes the metrics over authenticated HTTPS; there is
  no anonymous HTTP endpoint to scrape.

## References

- PR #152 (`spec/cluster-observability`) and commits `fa6b874` (DCGM on Auto
  Mode), `e4b5c95` (Kueue ServiceMonitor), `80b463f` (Kueue metrics RBAC
  manifest), `8361038` (plain-semver DCGM `4.8.3`), `78b4460` (manifest
  renumbering), `d5aa5f3` (drop `do-not-disrupt` from node-exporter).
- `../../lambda/kubectl-applier-simple/manifests/51-dcgm-exporter.yaml`,
  `post-helm-monitoring-servicemonitors.yaml`,
  `post-helm-monitoring-kueue-rbac.yaml`
- `../../lambda/helm-installer/charts.yaml` — kube-prometheus-stack values
- `../../tests/test_cluster_observability_charts.py` — chart wiring + the
  DaemonSet `do-not-disrupt` regression guards
- `../MONITORING.md` — operator guide for the observability stack
