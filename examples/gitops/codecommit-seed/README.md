# GCO GitOps repository

This repository was created by the Global Capacity Orchestrator (GCO) regional
stack for one EKS cluster. The hosted Argo CD capability attached to that
cluster reconciles the repository root into the tenant namespaces
(`gco-jobs`, `gco-inference`) through the fenced `gco-tenants` project and
the `gco-gitops-root` Application.

It starts with this file only, so the Application is Synced and Healthy with
zero resources. To deploy workloads, mirror a local directory of Kubernetes
manifests into it from a GCO checkout:

```bash
gco stacks capabilities gitops push --path ./my-manifests -r <region>
```

Every push replaces the branch content with the directory's tracked files
(one commit per push, only when something changed). Argo CD picks the commit
up on its next poll; with `sync_policy: automated` it applies it immediately.

The fence refuses cluster-scoped kinds and the tenant guardrails
(`ResourceQuota`, `LimitRange`, `NetworkPolicy`, `Role`, `RoleBinding`); see
the GCO documentation (`docs/EKS_CAPABILITIES.md`) for what the repository may
contain and how to structure it.
