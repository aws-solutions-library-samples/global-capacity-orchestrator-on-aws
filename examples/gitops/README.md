# GitOps Fixtures

Repository paths the self-managed [Argo CD](../../docs/GITOPS.md) can sync.
Unlike the manifests in the parent directory, nothing here goes through the
GCO API: Argo CD pulls a path from Git and applies it into a tenant namespace
through the fenced `gco-tenants` project (tenant namespaces only, no
cluster-scoped kinds, and never a ResourceQuota, LimitRange, NetworkPolicy,
Role or RoleBinding). Manifests without a namespace land in the Application's
destination namespace.

| Path | Contents |
|------|----------|
| [`hello-job/`](hello-job/) | One Job, `gco-gitops-hello`, with no namespace of its own and no `ttlSecondsAfterFinished` (automated sync with self-heal would re-create a Job the TTL controller removed). [`../argocd-gitops-job.yaml`](../argocd-gitops-job.yaml) syncs it into `gco-jobs`; the [example harness](../../docs/EXAMPLE_VALIDATION.md) pins that Application to the commit under validation and requires the Job to complete, and the kind CI job syncs it from the pull request's own commit. |

## Trying the fixture on your own deployment

Enable Argo CD, then either apply the example Application:

```bash
kubectl apply -f examples/argocd-gitops-job.yaml
kubectl get application gco-gitops-hello -n argocd
```

or let GCO create the root Application for the path, one per cluster:

```json
"helm": {
  "argocd": {
    "enabled": true,
    "gitops": {
      "repo_url": "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git",
      "revision": "main",
      "path": "examples/gitops/hello-job",
      "sync_policy": "automated"
    }
  }
}
```

`gco gitops status` shows the configured hand-off and the path each Region
syncs; `gco gitops open` opens the Argo CD UI.
