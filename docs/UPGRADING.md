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
- [How the upgrade path is tested](#how-the-upgrade-path-is-tested)
- [Release notes for running deployments](#release-notes-for-running-deployments)
  - [In-cluster TLS and tracing](#in-cluster-tls-and-tracing)
  - [Tenant write fence and MLflow over HTTPS](#tenant-write-fence-and-mlflow-over-https)
  - [vLLM endpoints deployed with MODEL](#vllm-endpoints-deployed-with-model)
  - [Stricter cdk.json validation](#stricter-cdkjson-validation)
  - [API calls need an IAM permission](#api-calls-need-an-iam-permission)
  - [Workload alarms move to their Regions](#workload-alarms-move-to-their-regions)
  - [ECR replication rules of your own are kept](#ecr-replication-rules-of-your-own-are-kept)
  - [Default-deny ingress in `gco-inference`](#default-deny-ingress-in-gco-inference)
  - [Valkey and Aurora ConfigMaps only in `gco-jobs`](#valkey-and-aurora-configmaps-only-in-gco-jobs)
  - [Narrower platform service-account RBAC](#narrower-platform-service-account-rbac)

## How an upgrade works

GCO has two tiers of stacks. The **control plane** — `<project>-global`,
`<project>-api-gateway`, `<project>-monitoring` and, when the analytics
environment is enabled, `<project>-analytics` — holds the shared state:
DynamoDB tables, the model and cluster-shared S3 buckets, the image registry, the
backup vault, the cost-report bucket, the auth secret, the private TLS root, and
the SageMaker Studio domain with its home directories and Cognito users.
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
   snapshotted first, copied to `cdk.json.pre-upgrade-<UTC time>`, and written
   back afterwards — your project name, Regions and feature toggles are what
   deploys, not the release's defaults. The bytes are written back even when
   the checkout fails; the copy is deleted once they are back and kept when
   anything fails.
3. **Refresh the local install.** `pip install -e '.[cdk,...]'` when the running
   `gco` is the editable install of this checkout: the `cdk` extra plus every
   extra that was already installed, so the CDK library the redeploy synthesizes
   with follows the release's pins. Then `npm ci` when the checkout has its own
   CDK toolchain, and a rebuild of the `gco-dev` image when a container runtime
   and the image are present.
4. **Scale the workload tier to zero.** First the checkout's deployable sources
   are checked: the redeploy refuses files other users cannot read, so the
   command stops here, with no stack touched, rather than after the teardown
   (see [If it stops halfway](#if-it-stops-halfway)). The monitoring stack is
   then updated in place with `--context gco:control-plane-only=true` so it
   stops referencing the regional stacks, and every regional API bridge and
   regional stack is destroyed — the same teardown as
   `gco stacks destroy-all --keep-control-plane`, with its retry loop and its
   sweeps of orphaned bastions, implicit log groups and dynamically provisioned
   EBS volumes. Any of the regional stack's roles that something outside the
   stack put into an instance profile are removed from it first, so the role
   delete cannot leave the stack in `DELETE_FAILED`. The teardown is the one in
   the release you upgrade from (see [Run the upgrade](#3-run-the-upgrade)), so upgrades from v8.14.0 or
   earlier do not get this sweep; see [If it stops halfway](#if-it-stops-halfway).
5. **Deploy-all on the new release.** `<project>-global`,
   `<project>-api-gateway` and (when enabled) `<project>-analytics` are updated
   in place, the regional stacks and their bridges are recreated from
   `cdk.json`, and the monitoring stack is updated back to the full topology.

Expect 45–90 minutes for a single Region (EKS cluster deletion and creation
dominate); `--parallel` overlaps the regional work when there are several.

## What is destroyed, what survives

| Resource | Where it lives | During `gco upgrade` |
|----------|----------------|----------------------|
| EFS file system (`gco-shared-storage` and its access points) | regional stack | **Destroyed.** Recovery points in the backup vault survive (the vault is in the global stack and is not purged), but the file system itself is recreated empty |
| FSx for Lustre | regional stack | **Destroyed.** `SCRATCH_2` deployments are ephemeral by design; anything not exported to S3 is gone |
| Valkey Serverless, Aurora Serverless v2 (pgvector) | regional stack | **Destroyed** (Aurora automated backups follow the cluster's retention setting; a manual snapshot survives) |
| In-cluster PersistentVolumes — Prometheus, Grafana, Alertmanager, MLflow, the Slurm controller state and accounting database | EBS volumes the cluster's CSI driver provisioned | **Deleted** once the cluster is confirmed gone, exactly as `destroy-all` does. Pass nothing to keep them — take EBS snapshots first if you want the history. Slurm's nightly accounting dumps are in the regional-shared bucket (`slurm/accounting-backups/`), so they follow that bucket's row |
| Regional-shared bucket (`regional-shared:<region>`) | regional stack | **Destroyed** unless `regional_shared_bucket.removal_policy` is `retain` ([REGIONAL_SHARED_BUCKET.md](REGIONAL_SHARED_BUCKET.md#removal-policy)) |
| Running jobs, inference endpoints, queued work on a cluster | regional stack | **Gone.** Drain the clusters first: let jobs finish or resubmit them afterwards; inference endpoints have to be recreated |
| Cluster-shared bucket, model bucket, DynamoDB tables (jobs, templates, webhooks, vector store), ECR image registry | global stack | Survive — updated in place |
| Auth secret, private TLS root CA, API Gateway, WAF, aggregator | API Gateway stack | Survive — updated in place |
| Cost-report bucket, Glue/Athena, alarms, dashboard | monitoring stack | Survive — updated in place (the dashboard briefly shows only its control-plane sections while the workload tier is down) |
| SageMaker Studio domain, Studio home directories (EFS), Cognito users, analytics buckets | analytics stack, when enabled | Survive — updated in place |
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
- **At the checkout** (`git checkout --quiet --detach <tag> failed`, for
  example because an untracked file is in the way of one the release adds):
  `cdk.json` is written back as it was, and the message names the
  `cdk.json.pre-upgrade-<UTC time>` copy it kept. Move the named file aside,
  delete the copy, and rerun. If the message says `cdk.json` itself could not
  be restored, copy that file over `cdk.json` before running anything else.
- **After the checkout, during the install refresh**: the checkout is on the new
  release; the message names the `pip`/`uv` command to run by hand, then
  `gco upgrade --skip-checkout` finishes the stack cycle.
- **At the source check, before the teardown** (`N deployable path(s) in the
  checkout are not readable by every user`): the checkout and local install are
  on the new release and no stack has changed. Make the named sources
  readable — `chmod -R a+rX lambda gco dockerfiles pyproject.toml
  requirements-lock.txt` from the repository root; see
  [TROUBLESHOOTING.md → Deploy Refuses Owner-Only Sources](TROUBLESHOOTING.md#deploy-refuses-owner-only-sources-restrictive-umask)
  — then `gco upgrade --skip-checkout`.
- **At the first stack step, with `npm X is required to package the inference
  streaming Lambda; found Y`**: the new release pins a different npm (the
  `packageManager` field of its `package.json`), and the CDK app packages the
  streaming Lambda only with that exact version. Packaging runs before the
  first CloudFormation call, so no stack has changed, even though the message
  arrives as `Upgrade failed during the stack cycle`; the checkout and local
  install are on the new release. Run the `npm install --global npm@X` the
  message names, then `gco upgrade --skip-checkout`. If you run `gco` through
  the dev container, the message names the image rebuild instead: the npm it
  checks comes from the `gco-dev` image the failing run started in, and
  rebuilding the image during the upgrade does not change a container that is
  already running. Run `./scripts/setup-dev-alias.sh` on the host, then
  `gco upgrade --skip-checkout`.
- **During the stack cycle**: the checkout and local install are on the new
  release. The failing stack is named; `gco stacks status <stack> -r <region>`
  and the CloudFormation console show why. An error that interrupts the cycle
  itself (credentials that expired, a dropped network) is reported as
  `Upgrade failed during the stack cycle`. Either way, once fixed, rerun
  `gco upgrade --skip-checkout` — the teardown half is idempotent (already
  deleted stacks are skipped) and the deploy half is the ordinary deploy-all.
- **A regional stack in `DELETE_FAILED` on an `AWS::IAM::Role` with "must
  remove roles from instance profile first"**: something outside the stack put
  that role into an instance profile. The process running the upgrade still
  executes the previous release's CLI, so an upgrade from v8.14.0 or earlier
  does not detach it. Find the profile and remove the role from it (the
  profile itself is not GCO's to delete), then rerun
  `gco upgrade --skip-checkout`, which now runs the new release's teardown:

  ```bash
  aws iam list-instance-profiles-for-role --role-name <role-physical-id>
  aws iam remove-role-from-instance-profile --instance-profile-name <profile> --role-name <role-physical-id>
  ```

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

## How the upgrade path is tested

Releases whose changes can affect this path are validated live with
`gco release validate-upgrade` ([UPGRADE_VALIDATION.md](UPGRADE_VALIDATION.md)).
It deploys the previous release with that release's own `gco`, runs its
`gco upgrade` to the new commit, and requires what this guide promises: the
control-plane stacks updated in place, the regional stacks recreated, a job
template written before the upgrade still there afterwards, cdk.json
unchanged, and a healthy deployment. It then tears everything down and checks
that nothing was left behind.

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
  022), and the CLI's own Lambda builds set their file modes explicitly.
  `gco upgrade` runs the same check after its checkout and before its teardown,
  so an upgrade the redeploy would refuse stops with every stack intact; this
  takes effect from the upgrade after this release, because an upgrade runs the
  CLI it started with. Code
  an older CLI already uploaded owner-only stays in the bootstrap bucket until
  its object is deleted once; see
  [TROUBLESHOOTING.md → Deploy Refuses Owner-Only Sources](TROUBLESHOOTING.md#deploy-refuses-owner-only-sources-restrictive-umask).

### Clusters first deployed with v4.1.0 or earlier

Releases up to v4.1.0 published the API through an internet-facing ALB that
EKS Auto Mode built from the Ingress `gco-system/gco-ingress`, the IngressClass
`alb` and the IngressClassParams `alb`. v5.0.0 moved the API to the internal
Gateway ALB but left those objects, and with them the old load balancer, on
clusters updated in place; it routes to a Service port that no longer exists.

- **The old load balancer is deleted.** The base pass now deletes the Ingress,
  then the IngressClass and the IngressClassParams, and EKS Auto Mode removes
  the load balancer it built for them.
- **Ingresses of your own on the `alb` class lose their load balancer too.**
  The `alb` class was the cluster's default IngressClass. Before redeploying,
  create an IngressClass and IngressClassParams of your own under another name
  and point those Ingresses at it with `spec.ingressClassName`.

### vLLM endpoints deployed with MODEL

From v8.12.0 the inference monitor starts vLLM with `--model $(MODEL)` when an
endpoint names its model only through `-e MODEL=...`. Earlier releases did not,
so those endpoints serve vLLM's built-in default model (`Qwen/Qwen3-0.6B` for
v0.31.0 and v0.32.0) and `gco inference invoke` gets a 404 for the model it asks for.

- **Running endpoints keep their arguments.** The monitor renders a
  Deployment's arguments only when it creates it, so an in-place
  `gco stacks deploy-all` leaves an affected endpoint serving the default
  model. Recreate it once its Regions run the new release; `gco upgrade`
  recreates every endpoint anyway. See
  [TROUBLESHOOTING.md → Invoke Says the Model Does Not Exist](TROUBLESHOOTING.md#invoke-says-the-model-does-not-exist).
- **`gco inference models <endpoint>` shows what an endpoint serves.** From
  the release after v8.13.0, `invoke` also names the served model when vLLM
  rejects the requested one.

### Stricter cdk.json validation

This release validates every block of `cdk.json` when the app synthesizes,
before any stack changes. `gco upgrade` keeps your `cdk.json` byte for byte,
and the `cdk.json` files earlier releases shipped (v8.23.0 included) still
pass, with the deprecation warnings below. A file you edited can fail: each
error names the full key path (for example `cost_monitoring.reports.interval`)
and what the key accepts, and `gco stacks synth` shows it without deploying.

**Fix errors before you run `gco upgrade`.** The upgrade validates your file
when it first synthesizes with the new release. With a deployed monitoring
stack that happens before the teardown; without one it can happen after it.
If the upgrade stops on a validation error, fix `cdk.json` and run
`gco stacks deploy-all` (see [If it stops halfway](#if-it-stops-halfway)).

**String flags are never read differently on upgrade.** Earlier releases read
the `enabled` flags of `fsx_lustre` (and `fsx_lustre_regions`), `valkey`,
`aurora_pgvector`, `cluster_observability.mlflow`, `queue_processor`,
`drift_detection`, `mcp_server`, `volcano_image_mirror`, `images.replication`
and the `helm` charts by truthiness, so a quoted `"false"`, like every
non-empty string, turned the feature on. Reading it as off now would delete a
running file system, cache, database cluster or MLflow volume, or uninstall a
chart, on the next deploy. So `"true"` (any case) still reads as on, with a
warning, and any other string fails with a message saying how the setting has
been running. Write `true` to keep the feature, or `false` to remove it, which
for the storage features deletes the resource and its data: back up first
([step 2](#2-back-up-regional-data-to-the-cluster-shared-bucket)).

Errors (the synth fails):

| What fails | Fix |
|---|---|
| A block written as a scalar, such as `"cluster_observability": false`; earlier releases read it as an absent block and applied its defaults | Write the object: `{"enabled": false}` |
| A key a block does not define (a typo such as `cost_monitoring.reports.interval`) | Use a key from the allowed list in the message; `_comment*` keys stay allowed everywhere |
| `true` or `false` where an integer belongs (`manifest_processor.replicas`, the thresholds and limits) | Write the number |
| An `enabled` flag written as a string other than `"true"` (the flags listed above) | Write the JSON boolean `true` to keep the feature or `false` to remove it |
| `project_name` that is not 2-31 characters of lowercase letters and digits in runs joined by single hyphens, starting with a letter (`gco-` and `a--b` were accepted and then failed at deploy) | Pick a valid name; renaming means a new deployment |
| `global_accelerator.name` outside the CreateAccelerator rule (1-64 letters, digits, periods and hyphens, not starting or ending with a period or hyphen) | Fix the name, or leave it out for `<project_name>-accelerator` |
| `global_accelerator.health_check_path` other than `/api/v1/health`, `/healthz`, `/readyz` or `/metrics` (a query string is allowed); every other path needs authentication, so every probe failed | Use `/api/v1/health`, the default |
| `kubernetes_version` as a JSON number (`1.30` read as `1.3`) | Quote it: `"1.30"` |
| `eks_cluster.public_access_cidrs` as a string (it was split into characters) or with an entry that is not an IPv4 CIDR | Write a list: `["203.0.113.7/32"]` |
| A `fsx_lustre_regions`, `pcs_regions` or `hyperpod_regions` key that is not an AWS Region | Fix the Region name |
| An `fsx_lustre` value FSx rejects: deployment type, storage capacity (1200, 2400, then multiples of 2400, or 3600 for `SCRATCH_1`), Lustre version, `per_unit_storage_throughput` outside the type's values (`PERSISTENT_1` 50/100/200, `PERSISTENT_2` 125/250/500/1000), a non-`s3://` path, an `export_path` without an `import_path` in the same bucket | Use a value the message lists. `PERSISTENT_2` without a throughput now gets 250, its own default, instead of 200, which FSx rejects |
| `valkey` limits below their minimums (`max_data_storage_gb` 1, `max_ecpu_per_second` 1000, `snapshot_retention_limit` 0) or `aurora_pgvector` capacity outside 0-256 (`max_acu` from 1) in steps of 0.5, `max_acu` below `min_acu`, or `backup_retention_days` outside 1-35 | Use a value in range |
| A `cluster_observability` `persistence_size` (Grafana, Prometheus, Alertmanager or MLflow) that is not a positive Kubernetes quantity, such as `"10GB"` | Write a quantity: `"10Gi"`, `"500Mi"` |
| A `helm.slurm` partition larger than `resource_quota` after its defaults are merged in | Shrink the partition or raise `resource_quota` |
| `vector_store.embedding_model_id` that is not a Bedrock foundation-model id (`provider.model[:version]`) | Use the model id, for example `amazon.titan-embed-text-v2:0`; surrounding spaces are removed |
| `helm.argocd.gitops.repo_url` that does not start with `https://`, `ssh://` or `git@` (such as `http://host/repo.git`) | Use an `https://` or `ssh://` URL |
| A `gateway_health_check` value the load balancer rejects: interval 5-300 seconds, timeout 2-120 and below the interval, thresholds 2-10 | Use a value in range, or leave the block out for 15 / 5 / 2 / 2 |
| A `tags` value that is not a string | Quote it |

A `cdk.json` without `project_name` is now validated too (it used to skip
every check); the name defaults to `gco`.

Warnings (the synth continues; the key is ignored, so remove it):

| Key | Why |
|---|---|
| `alb_config` | It never configured the platform ALB; `gateway_health_check` does, with the 15-second interval, 5-second timeout and thresholds of 2 that have always been deployed |
| `manifest_processor.image` | The regional stack builds that image itself |
| `global_accelerator.health_check_grace_period`, `global_accelerator.health_check_timeout` | Global Accelerator endpoint groups have no such settings |
| `fsx_lustre.node_group` (also in `fsx_lustre_regions`) | FSx workloads run on the EKS Auto Mode node pools; `gco stacks fsx enable` removes it |
| `images.removal_policy`, `images.empty_on_delete`, `images.lifecycle` | No stack creates or deletes the `<project_name>/*` ECR repositories (the gco CLI creates them), so they survive a destroy whatever these say, and `gco images init` applies its own lifecycle policy. Delete a repository with `gco images delete-repo <name> --force -y`; change a policy with `gco images lifecycle set` |
| An `enabled` flag written as `"true"` | Write the JSON boolean `true` |
| A top-level key in GCO's `snake_case` style that GCO does not read | Most likely a misspelt block, whose settings would not apply |
| A `*_regions` overlay for a Region that is not in `deployment_regions.regional` | Nothing reads it until the Region is added |

Command changes that go with it:

- `gco stacks destroy` and `destroy-all` no longer refuse to destroy the
  global stack, or ask to delete the image repositories, because of
  `images.removal_policy` / `images.empty_on_delete`: nothing ever deleted
  the repositories.
- `gco stacks fsx enable` writes the configuration its summary shows and
  removes the options you leave out, so an import path, export path or
  throughput from an earlier run no longer carries over.
- The committed `cdk.json` is stored exactly as the CLI writes it, so the
  first `gco stacks ... enable` changes only the keys it touches.

### API calls need an IAM permission

Earlier releases put a statement in the global API's resource policy that
admitted every principal of the deployment account, and the regional bridges
had the same statement when `api_gateway.regional_api_enabled` was `true` (and
always outside the `aws` partition). For a caller in the same account, API
Gateway allows a request that either the caller's identity policy or the
resource policy allows, so any role or user in the account could call every
IAM-authorized route without an `execute-api:Invoke` permission of its own.

- **BREAKING: callers without `execute-api:Invoke` are refused (403).** After
  the redeploy, a caller in the account needs an identity-based policy that
  allows `execute-api:Invoke` on the route. Administrator credentials already
  have it. Before redeploying, grant it to every other role or user that calls
  the global API or a regional bridge (CI pipelines, notebooks outside the
  analytics stack, scripts); see
  [CLI.md → Regional API Mode](CLI.md#regional-api-mode) for a policy that
  covers the CLI and [docs/iam-policies](iam-policies/README.md) for narrower
  ones.
- **`api_gateway.regional_api_enabled` no longer has an effect.** Region-pinned
  calls reach the bridges with the permission above whatever its value.
- **The analytics `/studio/*` pages keep working.** Their requests carry no IAM
  identity, so the resource policy still admits `GET /studio/*`; the Cognito
  authorizer decides who signs in.

### Workload alarms move to their Regions

Earlier releases created every workload Region's SQS, EKS node and
manifest-processor alarms in the monitoring stack. CloudWatch evaluates an
alarm only against metrics in its own Region, so in any Region other than the
monitoring Region those alarms never left `OK`.

- **The alarms are recreated in each regional stack.** A redeploy adds them,
  with the same metrics and thresholds, to every `<project>-<region>` stack and
  then deletes the old copies from `<project>-monitoring`. Their names change
  (CloudFormation generates them from the new stack), so update anything that
  looks an alarm up by name.
- **Each Region gets its own alert topic.** The new alarms notify an SNS topic
  in the same Region, exported as the regional stack's `RegionalAlertTopicArn`
  output. **Subscribe your on-call channel to it in every Region**; the
  monitoring stack's `AlertTopicArn` keeps the API Gateway, Lambda and
  DynamoDB alarms whose metrics are in its Region.
- **A global or API Gateway stack outside the monitoring Region gets its own
  alarms.** The jobs table's alarms read metrics of `deployment_regions.global`
  and the API Gateway and Lambda alarms metrics of
  `deployment_regions.api_gateway`. When either Region is not the monitoring
  Region, a redeploy creates that group of alarms in `<project>-global` or
  `<project>-api-gateway`, with an alert topic exported as
  `GlobalAlertTopicArn` or `ApiGatewayAlertTopicArn`, and deletes the copies
  from `<project>-monitoring`, which never saw data (and a global Region other
  than the monitoring Region used to fail `cdk synth`). **Subscribe your
  on-call channel to the new topic.** With the default `deployment_regions`,
  where the three are one Region, these alarms stay where they are.

### ECR replication rules of your own are kept

The global stack's `AWS::ECR::ReplicationConfiguration` is the registry's whole
replication configuration in the global Region. Earlier releases did not retain
it, so changing GCO's rule rewrote every rule in the registry and deleting the
stack, or turning `images.replication` off, deleted all of them.

- **The configuration is retained from this release on.** The first redeploy
  only marks it retained (`DeletionPolicy` and `UpdateReplacePolicy`); it does
  not touch the registry.
- **BREAKING: `gco stacks deploy` and `destroy` can refuse.** When the registry
  has replication rules this deployment did not create, a deploy that would
  create GCO's configuration, change GCO's rule, or remove a configuration an
  earlier release deployed is refused with instructions, and so is destroying
  a global stack an earlier release deployed. Redeploy with this release first;
  to keep rules of your own, set `images.replication.enabled` to `false` and
  manage the registry's replication yourself. A plain `cdk deploy` skips the
  check.
- **Destroy removes only GCO's rule.** After the global stack is deleted, the
  CLI removes this project's `<project>/` rule and keeps every other rule.

### Default-deny ingress in `gco-inference`

Earlier releases isolated only the pods the inference monitor labels
(`gco.io/type: inference`) and the shared Mooncake master. Any other pod in
`gco-inference`, such as a Deployment of your own whose pods lack that label,
accepted connections from every namespace on every port.

- **BREAKING: unlabelled pods in `gco-inference` accept no connections.** The
  base pass adds `gco-inference/default-deny-ingress`. Managed endpoints and the
  inference example manifests are unaffected: their pods carry
  `gco.io/type: inference`. Before redeploying, give such a workload's pod
  template that label (its pods then get the model pods' rules: the inference
  proxy on 8443 and the other model pods), or add a NetworkPolicy that admits
  its clients. The kubelet's probes and `kubectl port-forward` keep working.

### Valkey and Aurora ConfigMaps only in `gco-jobs`

Earlier releases published the `gco-valkey` and `gco-aurora-pgvector`
ConfigMaps in `gco-system`, `gco-jobs` and `gco-inference`, but only
`gco-jobs` has an egress rule that reaches the cache and the database.

- **BREAKING: the `gco-system` and `gco-inference` copies are deleted.** The
  base pass sweeps them. A pod there that references either ConfigMap without
  `optional: true` no longer starts. Run such a workload in `gco-jobs`, or give
  it a NetworkPolicy that admits egress to the endpoint's port and a
  ConfigMap of your own.

### Narrower platform service-account RBAC

The health monitor's and the manifest processor's Kubernetes roles now grant
exactly the calls those services make.

- **The health monitor** lists nodes, pods and node metrics and lists and
  watches Jobs; it lost get and watch on pods and nodes and every grant on
  services, events, deployments and pod metrics.
- **The manifest processor** lost its read grants on every Kueue, KubeRay and
  Volcano object (nothing in GCO read them), the unused second verb on
  namespaces, nodes, events, NodeClaims and pod metrics, and every grant on
  PersistentVolumeClaims in `gco-jobs`.
- **BREAKING: a kind you added to `job_validation_policy.allowed_kinds` needs
  its own grant.** `gco-manifest-processor-sa` can create only the default
  kinds; for PersistentVolumeClaim (or NetworkPolicy, as before), bind it to a
  Role of your own in each allowed namespace, or the create is refused with
  403. Tooling that borrowed either service account's token for other reads
  needs a role of its own too.
