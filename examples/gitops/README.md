# GitOps Fixtures

Repository paths the [Argo CD EKS Capability](../../docs/EKS_CAPABILITIES.md)'s
GitOps hand-off can be pointed at. Unlike the Job manifests in the parent
directory, nothing here is submitted through the GCO API: the hosted Argo CD
pulls a path from Git and reconciles it into a tenant namespace through the
fenced `gco-tenants` project the regional stack applies
(`lambda/kubectl-applier-simple/manifests/08-argocd-gitops.yaml`).

| Path | Contents |
|------|----------|
| [`tenant-smoke/`](tenant-smoke/) | One ConfigMap, `gco-gitops-tenant-smoke`, with no namespace of its own so the Application's destination (`gco-jobs`) supplies it. The [live-validation harness](../../docs/LIVE_RELEASE_VALIDATION.md) points the run's root `Application` at this path at the commit under validation and requires the ConfigMap to appear in `gco-jobs` carrying Argo CD's tracking label — proof the hosted control plane pulled this repository, the project admitted the kind, and the capability role's RBAC allowed the write. The kind CI job renders the same path into the `Application` spec. |

To point your own deployment at this fixture, set in `cdk.json`:

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
      "repo_url": "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git",
      "revision": "main",
      "path": "examples/gitops/tenant-smoke",
      "destination_namespaces": ["gco-jobs"],
      "sync_policy": "automated"
    }
  }
}
```

and deploy. `gco stacks capabilities status` shows the capability; the
Application's sync state is visible in the hosted Argo CD UI
(`gco stacks capabilities argocd open`).
