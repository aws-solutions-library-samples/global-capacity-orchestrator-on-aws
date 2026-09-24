# GitOps Fixtures

Repository paths the [Argo CD EKS Capability](../../docs/EKS_CAPABILITIES.md)'s
GitOps hand-off can be pointed at. Unlike the Job manifests in the parent
directory, nothing here is submitted through the GCO API: the hosted Argo CD
pulls a path from Git and reconciles it into a tenant namespace through the
fenced `gco-tenants` project the regional stack applies
(`lambda/kubectl-applier-simple/manifests/08-argocd-gitops.yaml`).

| Path | Contents |
|------|----------|
| [`codecommit-seed/`](codecommit-seed/) | The initial content of every GCO-managed CodeCommit GitOps repository (`gitops.source: codecommit`, the default): one README explaining what the repository is and how to fill it. The regional stack seeds the repository's `main` branch from this directory at creation, so the root `Application` is `Synced`/`Healthy` with zero resources before the first `gco stacks capabilities gitops push`. CloudFormation applies the seed only when the repository is created; later edits here never touch an existing repository. |
| [`tenant-smoke/`](tenant-smoke/) | One ConfigMap, `gco-gitops-tenant-smoke`, with no namespace of its own so the Application's destination (`gco-jobs`) supplies it. The [live-validation harness](../../docs/LIVE_RELEASE_VALIDATION.md) pushes this directory into each cluster's CodeCommit repository (or, with `--argocd-gitops-repo-url`, points the root `Application` at this path in that repository at the commit under validation) and requires the ConfigMap to appear in `gco-jobs` carrying Argo CD's tracking label — proof the hosted control plane pulled the repository, the project admitted the kind, and the capability role's RBAC allowed the write. The kind CI job renders the same path into the `Application` spec. |

## Trying the fixture on your own deployment

With the default CodeCommit source, enable the hand-off and push the
directory — nothing else to configure:

```json
"eks_capabilities": {
  "argocd": {
    "enabled": true,
    "idc_instance_arn": "arn:aws:sso:::instance/ssoins-EXAMPLE",
    "rbac_role_mappings": [
      {"role": "ADMIN", "identities": [{"id": "<identity-center-user-id>", "type": "SSO_USER"}]}
    ],
    "gitops": {
      "enabled": true,
      "destination_namespaces": ["gco-jobs"],
      "sync_policy": "automated"
    }
  }
}
```

```bash
gco stacks deploy-all
gco stacks capabilities gitops push --path examples/gitops/tenant-smoke -A
```

To read the fixture straight from this repository on GitHub instead, use the
`git` source:

```json
"gitops": {
  "enabled": true,
  "source": "git",
  "repo_url": "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git",
  "revision": "main",
  "path": "examples/gitops/tenant-smoke",
  "destination_namespaces": ["gco-jobs"],
  "sync_policy": "automated"
}
```

Either way, `gco stacks capabilities status` shows the capability and the
rendered hand-off, and the Application's sync state is visible in the hosted
Argo CD UI (`gco stacks capabilities argocd open`).
