# Upgrading GCO

`gco upgrade` moves a whole GCO deployment to the latest tagged release in one
command: the checked-out source, the locally installed CLI and toolchain, the
`gco-dev` container image, and every deployed CloudFormation stack. This guide
is the procedure around that command — what it does, what it destroys, what to
back up before you run it, and how to put the data back afterwards.

**Read the "What is destroyed" section before the first run.** The upgrade
recreates every regional stack, and the data inside those stacks goes with them.

## Table of Contents

- [How an upgrade works](#how-an-upgrade-works)
- [What is destroyed, what survives](#what-is-destroyed-what-survives)
- [The procedure](#the-procedure)
  - [1. Check](#1-check)
  - [2. Back up regional data to the cluster-shared bucket](#2-back-up-regional-data-to-the-cluster-shared-bucket)
  - [3. Run the upgrade](#3-run-the-upgrade)
  - [4. Restore the data you staged](#4-restore-the-data-you-staged)
- [If it stops halfway](#if-it-stops-halfway)
- [Variations](#variations)
- [Rolling back](#rolling-back)
- [What the command does not do](#what-the-command-does-not-do)
- [Release notes for running deployments](#release-notes-for-running-deployments)
  - [In-cluster TLS and tracing](#in-cluster-tls-and-tracing)
  - [Tenant write fence and MLflow over HTTPS](#tenant-write-fence-and-mlflow-over-https)

## How an upgrade works

GCO has two tiers of stacks. The **control plane** — `<project>-global`,
`<project>-api-gateway` and `<project>-monitoring` — holds the shared state:
DynamoDB tables, the model and cluster-shared S3 buckets, the image registry, the
backup vault, the cost-report bucket, the auth secret and the private TLS root.
The **workload tier** — one `<project>-<region>` stack and one
`<project>-regional-api-<region>` bridge per entry in
`deployment_regions.regional` — holds the EKS clusters and everything that runs
in them.

A release can change the workload tier in ways CloudFormation cannot apply in
place (cluster configuration, add-on wiring, storage layout), so the upgrade
does not try. It scales the workload tier to zero and builds it again on the new
release, while the control plane is updated in place and keeps its state:

1. **Resolve the release.** Fetch the tags of the git remote and pick the highest
   `vMAJOR.MINOR.PATCH` (or the tag named with `--ref`).
2. **Move the checkout.** `git checkout --detach <tag>`. `cdk.json` is the
   deployment's configuration, not the release's, so its exact bytes are
   snapshotted first and written back afterwards — your project name, Regions and
   feature toggles are what deploys, not the release's defaults.
3. **Refresh the local install.** `pip install -e .` when the running `gco` is
   the editable install of this checkout, `npm ci` when the checkout has its own
   CDK toolchain, and a rebuild of the `gco-dev` image when a container runtime
   and the image are present.
4. **Scale the workload tier to zero.** The monitoring stack is updated in place
   with `--context gco:control-plane-only=true` so it stops referencing the
   regional stacks, then every regional API bridge and regional stack is
   destroyed — the same teardown as `gco stacks destroy-all --keep-control-plane`,
   with its retry loop and its sweeps of orphaned bastions, implicit log groups
   and dynamically provisioned EBS volumes.
5. **Deploy-all on the new release.** `<project>-global` and
   `<project>-api-gateway` are updated in place, the regional stacks and their
   bridges are recreated from `cdk.json`, and the monitoring stack is updated back
   to the full topology.

Expect 45–90 minutes for a single Region (EKS cluster deletion and creation
dominate); `--parallel` overlaps the regional work when there are several.

## What is destroyed, what survives

| Resource | Where it lives | During `gco upgrade` |
|----------|----------------|----------------------|
| EFS file system (`gco-shared-storage` and its access points) | regional stack | **Destroyed.** Recovery points in the backup vault survive (the vault is in the global stack and is not purged), but the file system itself is recreated empty |
| FSx for Lustre | regional stack | **Destroyed.** `SCRATCH_2` deployments are ephemeral by design; anything not exported to S3 is gone |
| Valkey Serverless, Aurora Serverless v2 (pgvector) | regional stack | **Destroyed** (Aurora automated backups follow the cluster's retention setting; a manual snapshot survives) |
| In-cluster PersistentVolumes — Prometheus, Grafana, Alertmanager, MLflow | EBS volumes the cluster's CSI driver provisioned | **Deleted** once the cluster is confirmed gone, exactly as `destroy-all` does. Pass nothing to keep them — take EBS snapshots first if you want the history |
| Regional-shared bucket (`regional-shared:<region>`) | regional stack | **Destroyed** unless `regional_shared_bucket.removal_policy` is `retain` ([REGIONAL_SHARED_BUCKET.md](REGIONAL_SHARED_BUCKET.md#removal-policy)) |
| Running jobs, inference endpoints, queued work on a cluster | regional stack | **Gone.** Drain the clusters first: let jobs finish or resubmit them afterwards; inference endpoints have to be recreated |
| Cluster-shared bucket, model bucket, DynamoDB tables (jobs, templates, webhooks, vector store), ECR image registry | global stack | Survive — updated in place |
| Auth secret, private TLS root CA, API Gateway, WAF, aggregator | API Gateway stack | Survive — updated in place |
| Cost-report bucket, Glue/Athena, alarms, dashboard | monitoring stack | Survive — updated in place (the dashboard briefly shows only its control-plane sections while the workload tier is down) |
| Global Accelerator, endpoint groups, traffic-dial state | global stack | Survive; the regional ALBs re-register as the regional stacks come back |
| CDK bootstrap stacks, IAM roles outside GCO | account | Untouched |

Everything in the first six rows is what step 2 of the procedure is for.

## The procedure

### 1. Check

```bash
cd global-capacity-orchestrator-on-aws   # your checkout
gco upgrade --check
```

`--check` fetches the tags, synthesizes the stack list, and prints the plan —
the release you are on and the one you would move to, what happens to the
checkout and the local install, and exactly which stacks will be destroyed —
without changing anything. `gco -o json upgrade --check` gives the same as one
document (`up_to_date`, `plan.target`, `plan.workload_stacks`, …).

If the checkout has local modifications other than `cdk.json`, commit or stash
them now; the upgrade refuses to move a dirty checkout.

### 2. Back up regional data to the cluster-shared bucket

The cluster-shared bucket lives in the global stack, is reachable from every
Region, and is exactly the kind of storage that survives an upgrade — which makes
it the natural staging area. Stage everything from the first six rows of the
table above that you want to keep. Typical steps, adapt to what you actually use:

```bash
# Let running work finish (or record what to resubmit).
gco jobs list --all-regions
gco inference list

# EFS / FSx contents: pull them through a helper pod, then push to the shared bucket.
gco files download outputs/ ./staged/efs-outputs -r us-east-1 -t efs
gco files download checkpoints/ ./staged/fsx-checkpoints -r us-east-1 -t fsx
gco storage sync cluster-shared ./staged --direction upload --prefix upgrade/$(date +%F)/

# Regional-shared bucket: copy it into the cluster-shared bucket (or flip it to
# retain and redeploy before upgrading — see REGIONAL_SHARED_BUCKET.md).
gco storage sync regional-shared:us-east-1 ./staged/regional-shared
gco storage sync cluster-shared ./staged/regional-shared --direction upload --prefix upgrade/$(date +%F)/regional-shared/

# Aurora pgvector: take a manual snapshot (survives the cluster) or dump with pg_dump.
# Valkey: export a snapshot if the data matters; it is a cache for most deployments.
# Prometheus / Grafana / MLflow history: EBS snapshots of the PVs if you want it back.
```

`gco storage sync` never deletes destination-side objects and `--dry-run` shows
the transfer plan first. Keep the local `./staged` copy until step 4 has finished.

### 3. Run the upgrade

```bash
gco upgrade
```

The command prints the plan again, names every stack it will destroy, and asks
you to type the project name. `-y` skips the prompt for unattended runs;
`--parallel` overlaps the regional destroys and deploys. Progress is printed per
stack; the redeploy half is the same output `gco stacks deploy-all` produces.

When it finishes, open a new shell: the process that ran the upgrade was still
executing the previous release's CLI code, and the dev-container shell function
starts a fresh container from the rebuilt image. `gco --version` should now
report the new release, and `gco stacks status <project>-<region> -r <region>`
should show `CREATE_COMPLETE`.

### 4. Restore the data you staged

The new regional stacks start empty. Put back what the workloads need:

```bash
gco stacks access -r us-east-1                                           # kubectl access to the new cluster
gco storage sync cluster-shared ./staged --prefix upgrade/$(date +%F)/   # or straight from S3 in a job
gco storage sync regional-shared:us-east-1 ./staged/regional-shared --direction upload
gco models list                                                          # model weights were never touched
gco inference deploy ...                                                 # recreate endpoints
gco jobs submit ...                                                      # resubmit work
```

Jobs that read their inputs from the cluster-shared or model buckets need
nothing restored; only data that lived inside a regional stack does.

## If it stops halfway

Every step reports what it did, and the command tells you where it stopped.

- **Before the checkout** (dirty tree, no release tags, no container runtime):
  nothing has changed. Fix the cause and rerun.
- **After the checkout, during the install refresh**: the checkout is on the new
  release; the message names the `pip`/`uv` command to run by hand, then
  `gco upgrade --skip-checkout` finishes the stack cycle.
- **During the stack cycle**: the checkout and local install are on the new
  release. The failing stack is named; `gco stacks status <stack> -r <region>`
  and the CloudFormation console show why. Once fixed, rerun
  `gco upgrade --skip-checkout` — the teardown half is idempotent (already
  deleted stacks are skipped) and the deploy half is the ordinary deploy-all.
- **Container image rebuild or `npm ci` failures** are warnings, not stops: the
  stack cycle runs regardless, and `./scripts/setup-dev-alias.sh` or `npm ci`
  can be rerun afterwards.

## Variations

- **A specific release**: `gco upgrade --ref v8.1.0`. Only tagged releases are
  accepted; the command does not move between arbitrary commits.
- **A fork**: if your fork tracks upstream as a remote, `--remote upstream`
  resolves the release from there. If you merge upstream releases into your own
  branch instead, merge first and run `gco upgrade --skip-checkout` to cycle the
  stacks on what you merged.
- **Already on the latest release** (for example after a manual `git checkout`):
  `gco upgrade` says so and exits; `--force` runs the stack cycle anyway.
- **Run-scoped feature overrides**: if you deployed with
  `gco stacks deploy-all --enable fsx_lustre,valkey`, pass the same `--enable`
  to `gco upgrade` so the teardown evaluates the same app and the redeploy keeps
  the features on. Committing the toggles to `cdk.json` avoids the need.
- **Scaling to zero without upgrading**: `gco stacks destroy-all
  --keep-control-plane` is the teardown half on its own; see
  [Scaling to zero workload Regions](CUSTOMIZATION.md#scaling-to-zero-workload-regions).

## Rolling back

There is no one-step rollback for an upgrade that completed, for the same reason
there is no in-place upgrade: the regional stacks were recreated. To go back,
run the same procedure toward the previous release — `gco upgrade --ref
v<previous>` — after staging data again. `gco upgrade` treats any tagged release
as a valid target, so a downgrade is the same command with an older tag. Release
notes on the GitHub Releases page say when a release changes shared-state
schemas in a way that makes going back unsafe.

## What the command does not do

- It does not run outside a git checkout of the repository: the CDK app and the
  release history both live there. The CLI you run it with may be installed from
  anywhere; if it is not the editable install of the checkout, the command says
  so and leaves reinstalling it to you.
- It does not edit `cdk.json`, and it does not add or remove Regions; that is
  `gco stacks regions`.
- It does not back up or restore data. Steps 2 and 4 are yours.
- It is not exposed as an MCP tool: it replaces the checkout the MCP server
  itself runs from.

## Release notes for running deployments

`gco upgrade` rebuilds the regional stacks, so a cluster it creates starts in
the new release's state. The notes below describe what an operator sees when a
running deployment is instead updated in place with `gco stacks deploy-all` on
a newer checkout, plus any account-level effect that applies either way.

### In-cluster TLS and tracing

The release that moves every in-cluster hop to verified HTTPS
([ARCHITECTURE.md → In-cluster TLS](ARCHITECTURE.md#in-cluster-tls)) and adds
OpenTelemetry tracing ([MONITORING.md → Distributed tracing](MONITORING.md#distributed-tracing))
changes running clusters in these ways during an in-place redeploy:

- **The cost API is unavailable until its certificate exists.** The single
  cost-monitor pod (`Recreate`) now mounts the `cost-monitor-tls` Secret, which
  cert-manager issues in the post-Helm pass, and the updated manifest processor
  dials the cost monitor on 8443. Until the new pod is running, the
  `/api/v1/cost/*` routes answer 503.
- **Model endpoints re-roll once.** The inference monitor adds the
  `endpoint-tls-proxy` sidecar to every existing endpoint and moves its Service
  to port 8443 in place. The updated monitor itself starts only once the
  post-Helm pass has issued its `inference-monitor-tls` certificate, while the
  updated inference proxy dials 8443 from the base pass on, so each endpoint is
  unavailable through the inference proxy until its first TLS-capable pod is
  Ready, which also needs a free GPU and a model load; see
  [INFERENCE.md → Model Endpoint TLS](INFERENCE.md#model-endpoint-tls).
  Workloads of your own that call model Services directly must switch to
  `https://<service>.gco-inference.svc.cluster.local:8443` and trust the
  internal CA.
- **Grafana restarts once.** Its Deployment switches to the `Recreate` strategy
  and its pod gains the `grafana-tls-proxy` sidecar and an amd64 node selector.
  The OpenCost pod rolls once for the same sidecar and node selector.
- **The API certificates move to the internal CA.** The base pass deletes the
  old `gco-api-selfsigned` Issuer; the post-Helm pass re-points the
  health-monitor, manifest-processor, and inference-proxy Certificates at the
  `gco-internal-ca` ClusterIssuer, and their sidecars load the re-issued leaves
  in place. Prometheus's verified scrapes of those pods fail until then. The
  inference proxy builds its model-endpoint client per request from the CA
  bundle on disk, so once the kubelet has refreshed its re-issued `ca.crt` it
  trusts the internal CA from the next request on, without a restart.
- **CloudWatch Transaction Search is switched on and left on.** Each regional
  stack turns Transaction Search on in its Region unless it already is. The
  setting is account-wide in that Region, moves every X-Ray trace there to
  CloudWatch Logs pricing, and is not turned off by any later destroy or
  upgrade. Set `tracing.enable_transaction_search` to `false` before deploying
  if your organization manages it, or `tracing.enabled` to `false` for no
  tracing at all.

### Tenant write fence and MLflow over HTTPS

The release that fences the monitor's objects in the tenant namespaces
([ARCHITECTURE.md → Tenant write fence](ARCHITECTURE.md#tenant-write-fence))
and moves MLflow's clients to verified HTTPS changes running clusters in these
ways during an in-place redeploy:

- **Only the inference monitor changes what it manages in `gco-inference`.**
  The new base-pass policy `gco-tenant-write-fence` applies to every other
  identity, cluster administrators and the kubectl applier included. A
  `kubectl edit`, `kubectl set image` or `kubectl rollout restart` of a
  monitor-managed Deployment, or an edit of a ConfigMap or Secret it manages,
  is refused; change an endpoint through `gco inference` (`update-image`,
  `scale`, `canary`, `stop`/`start`) instead. Annotations, scaling and deletes
  stay allowed. Only cert-manager may write
  `gco-inference/gco-inference-tls`, and only trust-manager
  `gco-jobs/gco-internal-ca`.
- **Tenant objects must not carry the monitor's label on their own metadata.**
  Manifests that Argo CD, Crossplane or kro apply to `gco-inference` may keep
  `gco.io/type: inference` on pod templates, not on the Deployment, Service or
  other object itself (the examples no longer do). An object created with it
  before this release can no longer be changed by its tool, not even to drop
  the label: delete it, and the tool recreates it without the label.
- **Platform pods roll once.** The five platform Deployments, new
  queue-processor Jobs and the Grafana credential rotator gain a
  `kubernetes.io/arch: amd64` node selector, because the service images are
  built for amd64 only and the `cpu-general` pool also provisions Graviton
  nodes.
- **MLflow restarts once, and its clients move to HTTPS.** The tracking server
  pod (`Recreate`) gains the `mlflow-tls-proxy` sidecar and the amd64 node
  selector, and trust-manager is installed with it (namespace
  `trust-manager`) to publish the internal CA as the ConfigMap
  `gco-jobs/gco-internal-ca`. The `gco.io/mlflow-client` label now admits the
  HTTPS port (5443) only. Point jobs at
  `https://mlflow-tls.monitoring.svc.cluster.local:5443`, mount that ConfigMap
  and set `MLFLOW_TRACKING_SERVER_CERT_PATH` to its `ca.crt`, as
  [`examples/mlflow-tracking-job.yaml`](../examples/mlflow-tracking-job.yaml)
  does. A job that still dials `http://mlflow.monitoring:5000` reaches the
  server only through `allow-vpc-egress`, where `vpc_endpoint_cidrs` covers
  the pod subnets (the default). `gco monitoring open --service mlflow` is
  unchanged. Disabling MLflow removes trust-manager too; its `Bundle` CRD
  stays.
- **CA renewals need no restarts.** The cost monitor's OpenCost client and the
  Mooncake PD proxy now build their TLS clients per call from the CA bundle on
  disk, like the inference proxy.
- **Header bytes outside Latin-1 no longer turn into a 500.** The inference
  proxy and the PD proxy relay request and response header bytes as they were
  sent, and an HMAC signature or content hash that is not 64 lowercase hex
  characters is refused with 403.
- **`gco stacks deploy` checks that the deployable sources are readable.** A
  checkout made under a restrictive umask (such as 077) left owner-only files
  that the packaged Lambdas and service images could not read at runtime. The
  deploy, the orchestrated deploy and the release harness now refuse to start
  and name the files (fix them with `chmod -R a+rX` or re-clone under umask
  022), and the CLI's own Lambda builds set their file modes explicitly. Code
  an older CLI already uploaded owner-only stays in the bootstrap bucket until
  its object is deleted once; see
  [TROUBLESHOOTING.md → Deploy Refuses Owner-Only Sources](TROUBLESHOOTING.md#deploy-refuses-owner-only-sources-restrictive-umask).
