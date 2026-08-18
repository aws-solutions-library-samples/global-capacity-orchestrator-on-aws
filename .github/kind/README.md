# Kind Cluster Configuration

Configuration files for [kind](https://kind.sigs.k8s.io/) (Kubernetes-in-Docker) clusters used by the integration test workflow.

## Table of Contents

- [Files](#files)
- [Why Calico](#why-calico)
- [Usage](#usage)

## Files

| File | Description |
|------|-------------|
| `kind-calico.yaml` | Kind cluster config that disables the default kindnet CNI so Calico can be installed. Single control-plane node with `192.168.0.0/16` pod subnet. |

## Why Calico

The default kind CNI (kindnet) does not enforce `NetworkPolicy` resources. GCO deploys default-deny network policies in `lambda/kubectl-applier-simple/manifests/03-network-policies.yaml`. To actually validate that these policies work, the integration test installs Calico on top of kind, which enforces `NetworkPolicy` the same way a production CNI would.

Enforcement also decides whether a whole class of bug is visible at all. The official MLflow chart ships a pod NetworkPolicy whose ingress admits only pod sources, so it drops the kubelet's health probes — those arrive from the node's host network — and the pod restarts forever without ever reporting Available. Under kindnet that policy is accepted and inert, so the failure cannot reproduce; `integration:kind:examples-smoke` originally ran on kindnet and passed green while the bug was live in a real cluster. It now uses this config for the same reason `cluster-e2e` does.

## Usage

Used by the `integration:kind:cluster-e2e` and `integration:kind:examples-smoke` jobs in `.github/workflows/integration-tests.yml`:

```yaml
- name: Create kind cluster
  uses: helm/kind-action@v1
  with:
    config: .github/kind/kind-calico.yaml
```
