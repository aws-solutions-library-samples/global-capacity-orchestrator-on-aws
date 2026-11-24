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
  - [2. Stage regional data where it survives](#2-stage-regional-data-where-it-survives)
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
  - [Clusters first deployed with v4.1.0 or earlier](#clusters-first-deployed-with-v410-or-earlier)
  - [vLLM endpoints deployed with MODEL](#vllm-endpoints-deployed-with-model)
  - [Stricter cdk.json validation](#stricter-cdkjson-validation)
  - [API calls need an IAM permission](#api-calls-need-an-iam-permission)
  - [The APIs answer over IPv6 too](#the-apis-answer-over-ipv6-too)
  - [A persistent bucket in each Region](#a-persistent-bucket-in-each-region)
  - [Workload pods reach fewer AWS resources](#workload-pods-reach-fewer-aws-resources)
  - [In-VPC hosts need the cluster security group](#in-vpc-hosts-need-the-cluster-security-group)
  - [MCP server role and its access entry](#mcp-server-role-and-its-access-entry)
  - [Workload alarms move to their Regions](#workload-alarms-move-to-their-regions)
  - [ECR replication rules of your own are kept](#ecr-replication-rules-of-your-own-are-kept)
  - [Default-deny ingress in `gco-inference`](#default-deny-ingress-in-gco-inference)
  - [Valkey and Aurora ConfigMaps only in `gco-jobs`](#valkey-and-aurora-configmaps-only-in-gco-jobs)
  - [Valkey 9 for the optional cache](#valkey-9-for-the-optional-cache)
  - [Narrower platform service-account RBAC](#narrower-platform-service-account-rbac)
  - [Endpoint and project labels on the monitor's objects](#endpoint-and-project-labels-on-the-monitors-objects)
  - [Training job admission](#training-job-admission)
  - [Stopped inference endpoints stay stopped](#stopped-inference-endpoints-stay-stopped)
  - [Health readings and the `active_jobs` metric](#health-readings-and-the-active_jobs-metric)
  - [Job status filter and request paths](#job-status-filter-and-request-paths)
  - [Access-logs buckets use SSE-S3](#access-logs-buckets-use-sse-s3)
  - [Analytics roles act on their own domain](#analytics-roles-act-on-their-own-domain)
  - [Studio login takes an access token](#studio-login-takes-an-access-token)
  - [Studio users: first sign-in and removal](#studio-users-first-sign-in-and-removal)
  - [Studio spaces can attach Studio_EFS](#studio-spaces-can-attach-studio_efs)
  - [`Project` tag on PCS instances](#project-tag-on-pcs-instances)
  - [Add-on replay input in parts](#add-on-replay-input-in-parts)
  - [Volcano Jobs get their pods](#volcano-jobs-get-their-pods)
  - [Kueue's controller is not preempted](#kueues-controller-is-not-preempted)
  - [The keep-warm GPU pool](#the-keep-warm-gpu-pool)
  - [Log groups earlier upgrades left behind](#log-groups-earlier-upgrades-left-behind)
  - [CLI exit codes, output and prompts](#cli-exit-codes-output-and-prompts)
  - [MCP server tools](#mcp-server-tools)
  - [Pre-commit hook for contributors](#pre-commit-hook-for-contributors)

## How an upgrade works

GCO has two tiers of stacks. The **control plane** — `<project>-global`,
`<project>-api-gateway`, `<project>-monitoring`, each Region's
`<project>-<region>-persistent` when `regional_persistent_bucket.enabled` is
true and, when the analytics environment is enabled, `<project>-analytics` —
holds the shared state: DynamoDB tables, the model and cluster-shared S3
buckets, the persistent buckets, the image registry, the backup vault, the
cost-report bucket, the auth secret, the private TLS root, and the SageMaker
Studio domain with its home directories and Cognito users.
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
   (see [If it stops halfway](#if-it-stops-halfway)). Then the new release's
   configuration loader reads the preserved `cdk.json`, with any `--enable`
   overrides: each warning it has (a deprecated key, a key that no longer takes
   effect) is printed with what to do instead, and a `cdk.json` it rejects stops
   the command here too, with the loader's message. Upgrades from v8.23.0 or
   earlier skip this check; their teardown's first synth stops on the same
   error before deleting anything. The monitoring stack is
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
| Regional persistent bucket (`regional-persistent:<region>`) | `<project>-<region>-persistent`, when `regional_persistent_bucket.enabled` is true | Survives — the upgrade keeps this stack with the control plane ([REGIONAL_PERSISTENT_BUCKET.md](REGIONAL_PERSISTENT_BUCKET.md)) |
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

### 2. Stage regional data where it survives

Two kinds of storage survive the upgrade: each Region's
[persistent bucket](REGIONAL_PERSISTENT_BUCKET.md) and the cluster-shared
bucket. Stage everything from the first six rows of the table above that you
want to keep in one of them:

- **The persistent bucket**, `regional-persistent:<region>`, is in the same
  Region as the data, so copies stay in-Region, and a Job in `gco-jobs` can
  write to it directly. It exists when `regional_persistent_bucket.enabled` is
  true. A deployment first created with v9.1.0 or earlier has no persistent
  bucket until you turn it on after an upgrade
  ([A persistent bucket in each Region](#a-persistent-bucket-in-each-region)).
- **The cluster-shared bucket**, `cluster-shared`, lives in the global stack and
  is reachable from every Region. Use it when the persistent bucket is not
  deployed, or when another Region or SageMaker Studio must read the data.

Typical steps, adapt to what you actually use:

```bash
# Let running work finish (or record what to resubmit).
gco jobs list --all-regions
gco inference list

# EFS / FSx contents: pull them through a helper pod, then push them up.
# (A Job can also copy them straight into the persistent bucket; see
# REGIONAL_PERSISTENT_BUCKET.md → Staging data before an upgrade.)
gco files download outputs/ ./staged/efs-outputs -r us-east-1 -t efs
gco files download checkpoints/ ./staged/fsx-checkpoints -r us-east-1 -t fsx
gco storage sync regional-persistent:us-east-1 ./staged --direction upload --prefix upgrade/$(date +%F)/
# Without a persistent bucket:
#   gco storage sync cluster-shared ./staged --direction upload --prefix upgrade/$(date +%F)/

# Regional-shared bucket: copy it into the persistent bucket (or flip it to
# retain and redeploy before upgrading — see REGIONAL_SHARED_BUCKET.md).
gco storage sync regional-shared:us-east-1 ./staged/regional-shared
gco storage sync regional-persistent:us-east-1 ./staged/regional-shared --direction upload --prefix upgrade/$(date +%F)/regional-shared/

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
gco stacks access -r us-east-1                                                         # kubectl access to the new cluster
gco storage sync regional-persistent:us-east-1 ./staged --prefix upgrade/$(date +%F)/  # or straight from S3 in a job
gco storage sync regional-shared:us-east-1 ./staged/regional-shared --direction upload
gco models list                                                                        # model weights were never touched
gco inference deploy ...                                                               # recreate endpoints
gco jobs submit ...                                                                    # resubmit work
```

Jobs that read their inputs from the persistent, cluster-shared or model
buckets need nothing restored; only data that lived inside a regional stack
does. Delete the staged objects from the persistent bucket once you no longer
need them, or let `regional_persistent_bucket.expiration_days` expire them.

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
- **At the configuration check, before the teardown** (`Stopped before the
  teardown: the checked-out release rejects cdk.json: ...`): the checkout and
  local install are on the new release and no stack has changed. The message
  is the new release's validation error and names the key to change; the
  warnings printed above it name every deprecated or ignored key. Edit
  `cdk.json`, then run `gco upgrade --skip-checkout`. The loader stops at the
  first error, so a
  second problem shows on the next run. The same check also stops the command
  when it cannot run at all, for example when the CDK library is missing
  (`... configuration check could not run`).
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

**A rejected `cdk.json` stops the upgrade before any stack changes.**
`gco upgrade` runs the CLI it was started with. An upgrade started from
v8.23.0 or earlier has no configuration check of its own, so the new
release's loader first runs in the teardown's opening `cdk list`, which fails
before any stack is touched. The message reads `Upgrade failed during the
stack cycle: Failed to list stacks: ...`, followed by the CDK output that
holds the validation error; its "Stacks may be partly torn down" does not
apply. Upgrades started from this release on check the file before the
teardown and print each warning ([step 4](#how-an-upgrade-works)). Either
way, fix `cdk.json` and run `gco upgrade --skip-checkout`
(see [If it stops halfway](#if-it-stops-halfway)).

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
([step 2](#2-stage-regional-data-where-it-survives)).

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
| `api_gateway.regional_api_enabled` | The API resource policies no longer admit the account as a whole, so callers reach the global API and the regional bridges with their own `execute-api:Invoke` permission whatever it says (see [API calls need an IAM permission](#api-calls-need-an-iam-permission)) |
| `analytics_environment.studio.user_profile_name_prefix` (a value other than `null`) | It was never applied: each SageMaker Studio user profile is named after its Cognito username. Remove the `studio` block |
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
  calls reach the bridges with the permission above whatever its value. The
  key is deprecated: the synth warns about it and ignores it, and the shipped
  `cdk.json` no longer has it, so remove it from yours.
- **The analytics `/studio/*` pages keep working.** Their requests carry no IAM
  identity, so the resource policy still admits `GET /studio/*`; the Cognito
  authorizer decides who signs in.

### The APIs answer over IPv6 too

The global API and every regional API bridge are now dual-stack by default
(`api_gateway.ip_address_type`, see
[CUSTOMIZATION.md → API Gateway IP Address Type](CUSTOMIZATION.md#api-gateway-ip-address-type)).
The redeploy updates both APIs in place: their endpoint URLs stay the same,
and their host names start answering with IPv6 (AAAA) records next to the
IPv4 ones, so callers with IPv6 connectivity start connecting over IPv6.

- **Callers limited to IPv4 source ranges are refused (403) over IPv6.** If an
  identity-based policy or a service control policy limits `execute-api:Invoke`
  with IPv4-only `aws:SourceIp` ranges, add the caller's IPv6 ranges before
  redeploying, or set `"ip_address_type": "ipv4"` in the `api_gateway` block of
  your `cdk.json` to keep the APIs IPv4-only.
- **IPv6 clients get a separate WAF budget per address.** The per-IP rate rule
  counts each IPv6 address on its own.
- **Older CLIs on networks that drop IPv6 can stall.** This release's CLI and
  MCP server alternate between IPv6 and IPv4 addresses, so such a network costs
  them about two seconds per new connection. Upgrade every machine that runs
  `gco` against the deployment.

### A persistent bucket in each Region

This release adds an optional stack per workload Region,
`<project>-<region>-persistent`, holding an S3 bucket that `gco upgrade` and
`gco stacks destroy-all --keep-control-plane` leave standing
([REGIONAL_PERSISTENT_BUCKET.md](REGIONAL_PERSISTENT_BUCKET.md)). The shipped
`cdk.json` turns it on; a `cdk.json` without the `regional_persistent_bucket`
block, such as yours from an earlier release, leaves it off.

- **Upgrading into this release with `gco upgrade`:** keep the block out of
  `cdk.json` until the upgrade has finished. The previous release's `gco` runs
  that upgrade's stack cycle and cannot deploy the new stack type, so stage
  data in the cluster-shared bucket as before.
- **Turning it on afterwards:** add
  `"regional_persistent_bucket": {"enabled": true, "expiration_days": null}` to
  `cdk.json` and run `gco stacks deploy-all -y`. The persistent stacks deploy
  with the global stacks, and the regional stacks update in place to grant the
  job-pod role access and create the `gco-regional-persistent-bucket`
  ConfigMap. Later upgrades can stage data there.
- **A full `gco stacks destroy-all` deletes the persistent buckets and their
  objects**, like every other GCO bucket.

### Workload pods reach fewer AWS resources

Every job and model pod runs as `gco-service-account` in `gco-jobs` or
`gco-inference`, which assumes the regional stack's `ServiceAccountRole`.
Earlier releases ran the inference monitor as that role too, so the role held
read and write on the inference endpoints table and the Parameter Store read
of `/<project>/regional-shared-bucket/name` that only the monitor uses. It
also read every bucket whose name starts with the project name and could
decrypt through S3 with any key in the account.

- **The inference monitor runs as its own role.** The redeploy creates
  `InferenceMonitorRole`, trusted only by
  `gco-system/gco-inference-monitor-sa`, and the monitor pods restart onto it
  during the same deploy. Nothing to do.
- **BREAKING: job and model pods lose the inference endpoints table and the
  bucket-name parameter.** A pod that read or wrote the table, or read
  `/<project>/regional-shared-bucket/name`, gets `AccessDenied`. Change
  endpoints through `gco inference`, and read the bucket name from the
  `gco-regional-shared-bucket` ConfigMap
  ([REGIONAL_SHARED_BUCKET.md → ConfigMap schema](REGIONAL_SHARED_BUCKET.md#configmap-schema)).
- **BREAKING: pods read three buckets.** The role keeps read and write on the
  cluster-shared and regional-shared buckets and read on the model bucket,
  each named exactly; the `<project>-*` statement and its account-wide key
  grant are gone. GCO's access-log, cost-report and analytics Studio buckets,
  and any bucket of your own named `<project>-...`, are out of the pods' reach
  ([REGIONAL_SHARED_BUCKET.md → The job-pod role's other buckets](REGIONAL_SHARED_BUCKET.md#the-job-pod-roles-other-buckets)).
  A model endpoint whose `--model-source` points at another bucket fails in
  its `model-sync` init container with `AccessDenied` the next time one of its
  pods starts. Upload the weights with `gco models upload` and deploy the
  endpoint again with `--model-source $(gco models uri <name>)`, or attach a
  policy to `ServiceAccountRole` in each `<project>-<region>` stack that
  allows `s3:GetObject` and `s3:ListBucket` on the other bucket and, when that
  bucket uses SSE-KMS, `kms:Decrypt` on its key. Do the same for jobs that
  read such a bucket.

### In-VPC hosts need the cluster security group

Earlier releases let the EKS cluster security group admit HTTPS (443) from the
whole VPC CIDR, a rule meant for the GA registration Lambda. Through it, any
host in the regional VPC could reach the cluster's private Kubernetes API
endpoint.

- **BREAKING: a host of your own in the VPC needs a rule of its own.** The
  redeploy replaces the CIDR rule with one from the Lambda's own security
  group. GCO's own clients are covered: Auto Mode nodes and the
  `--via-ssm auto` bastion carry the cluster security group, the
  kubectl-applier, Helm installer and GA registration Lambdas have 443 rules
  from their security groups, and HyperPod nodes have a rule from theirs. An
  instance you name with `--via-ssm <instance-id>`, a bastion or Cloud9
  instance, a Client VPN endpoint or a VPC-attached CI runner that reached
  the endpoint only through the CIDR rule now times out. Before redeploying,
  attach the cluster security group (the regional stack's
  `ClusterSecurityGroupId` output) to it, or admit its own security group on
  443:

  ```bash
  aws ec2 authorize-security-group-ingress --region <region> \
    --group-id <ClusterSecurityGroupId> \
    --protocol tcp --port 443 --source-group <your-security-group-id>
  ```

  `gco upgrade` creates a new cluster and with it a new cluster security
  group, so after an upgrade do this with the new `ClusterSecurityGroupId`.
  [ARCHITECTURE.md → Network Security](ARCHITECTURE.md#network-security)
  lists every client GCO runs and the rule that admits it.

### MCP server role and its access entry

Each regional stack creates `McpServerRole`, exported as `McpServerRoleArn`,
for the MCP server's `GCO_MCP_ROLE_ARN` setting. In earlier releases the role
applied only to the server's own AWS calls: the `gco` and `kubectl` processes
behind most tools ran with the credentials of whoever started the server, and
the role had no EKS access entry.

- **BREAKING (servers with `GCO_MCP_ROLE_ARN` set): the tools' `gco` and
  `kubectl` processes run as the role.** The role now grants what the tools
  registered without a `GCO_ENABLE_*` flag call: `execute-api:Invoke` on the
  GCO APIs, CloudFormation, SSM, EKS, ECR, Logs and DynamoDB reads scoped to
  the project (the inference endpoint reads included), job submission to the
  regional queues, and read-only Kubernetes access to its own Region's
  cluster through an access entry. It grants nothing broader, so twenty-nine
  default tools (among them the thirteen inference tools that create or
  change an endpoint's record, such as `deploy_inference`, `scale_inference`
  and `canary_deploy`), and every gated tool that deploys, destroys, builds,
  pushes, uploads, deletes or buys capacity, fail under it with
  `AccessDenied` or `Forbidden`;
  [gco_mcp/README.md → IAM role](../gco_mcp/README.md#iam-role-gco_mcp_role_arn)
  lists them. Unset `GCO_MCP_ROLE_ARN` for those tools, or extend the role.
- **BREAKING: delete an access entry you created for the role.** The stack
  now creates the role's access entry itself (`McpServerAccessEntry`). If you
  created one for `McpServerRole` by hand, the in-place redeploy fails on it
  with `ResourceInUseException` and rolls back. Before running
  `gco stacks deploy-all`, look for one in each workload Region and delete
  it:

  ```bash
  aws eks describe-access-entry --region <region> \
    --cluster-name <project>-<region> --principal-arn <McpServerRoleArn>
  aws eks delete-access-entry --region <region> \
    --cluster-name <project>-<region> --principal-arn <McpServerRoleArn>
  ```

  `describe-access-entry` answers `ResourceNotFoundException` when there is
  none. `gco upgrade` needs no such step: it recreates each cluster, and with
  it the role, which gets a new ARN, so set `GCO_MCP_ROLE_ARN` to the new
  `McpServerRoleArn` afterwards.

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
  Region, a redeploy creates that group of alarms, with new names, in
  `<project>-global` or `<project>-api-gateway`, with an alert topic exported as
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

### Valkey 9 for the optional cache

With `valkey.enabled`, the regional stacks now create their ElastiCache
Serverless caches with Valkey 9 instead of Valkey 8.

- **A running cache is upgraded in place on the next deploy.** ElastiCache
  Serverless upgrades the major version without downtime, and the endpoint,
  the `gco-valkey` ConfigMap and the cached data stay. A cache cannot go back
  to Valkey 8 afterwards.
- **Check your own clients first.** AWS lists the behavior that changes from
  Valkey 8 in
  [Major version behavior and compatibility differences for Valkey](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/VersionManagementConsiderations-valkey.html):
  unauthenticated clients get an authentication error before an unknown-command
  error, a few error messages are worded differently, and in 9.1
  `CLUSTER SHARDS` and `CLUSTER SLOTS` answer with an availability-zone field
  that strict parsers, such as older go-redis releases, may reject.

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
- **The inference monitor** lost its grant on HTTPRoutes in `gco-inference`;
  no release created per-endpoint HTTPRoutes.
- **BREAKING: a kind you added to `job_validation_policy.allowed_kinds` needs
  its own grant.** `gco-manifest-processor-sa` can create only the default
  kinds; for PersistentVolumeClaim (or NetworkPolicy, as before), bind it to a
  Role of your own in each allowed namespace, or the create is refused with
  403. Tooling that borrowed either service account's token for other reads
  needs a role of its own too.

### Endpoint and project labels on the monitor's objects

The tenant write fence
([Tenant write fence and MLflow over HTTPS](#tenant-write-fence-and-mlflow-over-https))
kept the inference monitor's `gco.io/type` label and provenance annotations
to the monitor. The monitor now also labels every object it creates with
`gco.io/endpoint: <name>` and leaves alone an object labelled for another
endpoint, and the fence covers that label and the `project` label, which the
monitor checks before it deletes a stale object
([ARCHITECTURE.md → Tenant write fence](ARCHITECTURE.md#tenant-write-fence)).

- **BREAKING (Argo CD, Crossplane and kro tenant tools): only the inference
  monitor sets, changes or removes `gco.io/endpoint` or `project` on the
  objects it manages in `gco-inference`**: those labelled
  `gco.io/type: inference`, the shared `mooncake-master`, and the ConfigMaps
  its pods mount by name. Every other identity, cluster administrators and
  the kubectl applier included, is refused. Remove both labels from the
  manifests your tools apply to those objects. Objects of your own are not
  affected.

### Training job admission

- **BREAKING: TrainJob runtime patches without containers are validated.**
  A Kubeflow TrainJob runtime patch may set pod-level fields alone, and the
  REST and SQS paths now check such a patch as a pod spec. With
  `job_validation_policy.manifest_security_policy.block_host_path` on (as
  shipped), a patch that adds a hostPath volume is refused even when another
  patch's container mounts it; with `block_run_as_root` on, a patch that sets
  the pod's `securityContext.runAsUser: 0` is refused. Earlier releases
  admitted both. Move the volume to a supported volume type, or set a
  non-root user.
- **BREAKING: a TrainJob whose runtime patches select HyperPod nodes needs
  the HyperPod toleration.** The job API now merges a TrainJob's runtime
  patches per pod template, as Kubeflow Trainer does, and a TrainJob whose
  every patched pod template selects `gco.aws/capacity-provider: hyperpod`
  is held to the HyperPod rule: it must tolerate `gco.aws/hyperpod`, in the
  same patch or another one (the next item says which list counts), or carry
  the `kueue.x-k8s.io/queue-name: gco-hyperpod` label. Earlier releases judged
  each patch on its own, so they asked such a TrainJob for an
  `nvidia.com/gpu` toleration when it requested GPUs, although HyperPod
  nodes have no such taint, and admitted one without the HyperPod
  toleration, whose pods then stayed Pending. Add the toleration to a
  runtime patch
  ([HYPERPOD.md → How work reaches HyperPod nodes](HYPERPOD.md#how-work-reaches-hyperpod-nodes)).
  `gco jobs submit-sqs --auto-region` now also ranks only the Regions with
  HyperPod for such a TrainJob.
- **BREAKING: a TrainJob is judged on the tolerations Trainer applies.**
  Trainer applies `spec.runtimePatches` in order, and a patch that sets
  `tolerations` for a replicated job replaces the list an earlier patch set
  for it; an empty list sets nothing. The REST and SQS paths and
  `gco jobs check-policy` now match the GPU and HyperPod taints against the
  list each replicated job ends up with, so a toleration in a list that a
  later patch replaces, or anywhere outside the runtime patches, no longer
  counts. Earlier releases matched the tolerations of every patch together
  and admitted such a TrainJob, whose pods then stayed Pending. Put every
  toleration a replicated job needs in the last patch that sets tolerations
  for it.
- **BREAKING: the `torch-distributed` runtime schedules on amd64 nodes only.**
  Its image is published for amd64 only, so the shipped ClusterTrainingRuntime
  now selects `kubernetes.io/arch: amd64`. A TrainJob that runs an image of
  its own with an arm64 build on Graviton nodes must set the node selector in
  a runtime patch.
- **BREAKING: queued manifests are held to
  `manifest_processor.yaml_max_depth`.** The SQS path (`gco jobs submit-sqs`)
  refuses a manifest nested deeper than the limit (default 50), as the REST
  path already did.

### Stopped inference endpoints stay stopped

- **BREAKING: changing a stopped endpoint no longer starts it.**
  `gco inference update-image`, `set-topology`, `configure-store`, `canary`,
  `promote` and `rollback` moved a stopped endpoint back to `deploying`, and
  the monitors scaled it up again. They now store the change and the endpoint
  stays stopped; run `gco inference start <name>` to bring it up on the new
  spec. Scripts that relied on the restart need that extra command.
- **BREAKING (only if you set it): `K8S_API_TIMEOUT` must be a positive whole
  number of seconds.** No manifest sets it; the default is 30. If you added it
  to the inference monitor's or the manifest processor's environment, `0`
  (which the Kubernetes client reads as no timeout) or a negative value now
  stops the service at start-up with
  `K8S_API_TIMEOUT must be a positive number of seconds`. Remove it or set a
  positive whole number.

### Health readings and the `active_jobs` metric

- **BREAKING (dashboards and alarms): `active_jobs` counts Jobs.** The
  `active_jobs` field of `GET /api/v1/metrics` and the health monitor's
  CloudWatch metric `ActiveJobs` (namespace `GCO/HealthMonitor`) now count the
  Jobs in the job namespaces that have a pending or running pod. They counted
  every running pod outside `kube-system`, `kube-public` and
  `kube-node-lease`, platform and model pods included, so the values drop.
  Re-baseline dashboards and alarms built on them.
- **A failed cluster read is no longer healthy.** When the metrics-server,
  node or pod read fails, the health monitor keeps its last complete reading
  for up to 60 seconds, then reports the cluster unhealthy and names the read
  that failed; earlier releases reported a healthy, idle cluster.
  `GET /api/v1/health` answers 503 while it is unhealthy. Utilization above
  100% is reported as it is instead of failing the reading.
- **BREAKING (API contract): `GET /api/v1/metrics` answers 503 until the
  health monitor has started.** It answered 500 with the 503's text, and other
  failures now answer 500 with `Failed to get metrics` and no exception text.
  Messages from `GET /api/v1/health` name the read that failed instead of
  carrying the exception text. Clients that matched on either text need the
  new one.

### Job status filter and request paths

- **BREAKING (scripts that filter on `succeeded`): `succeeded` matches
  finished jobs.** The job responses call a finished Job `succeeded`, but the
  `status` filter of `GET /api/v1/jobs` and `GET /api/v1/global/jobs`, and of
  the bulk deletes `DELETE /api/v1/jobs` and `DELETE /api/v1/global/jobs`,
  compared it with the manifest processor's name, `completed`, so `succeeded`
  matched nothing. The API now reads `succeeded` as `completed`, and
  `gco jobs list --status succeeded` and `gco jobs bulk-delete --status
  succeeded` send `completed`. A scheduled bulk delete filtered on
  `succeeded` that deleted nothing now deletes the finished jobs it matches:
  run it once with `"dry_run": true` (or `gco jobs bulk-delete` without
  `--execute`) to see what it would delete.
- **Request paths with a dot segment answer 400.** Both API Gateway proxies,
  behind the global API and the regional bridges, refuse a path with a `.` or
  `..` segment, literal or percent-encoded (`%2E%2E`), before forwarding it:
  the HTTP client they forward with removes such segments, so the request
  would reach a route other than the one API Gateway authorized. No GCO route
  has one, and the CLI already refused such names; a client that builds
  paths from unchecked names gets 400 for them.

### Access-logs buckets use SSE-S3

Earlier releases encrypted the server access logs of the cluster-shared,
regional-shared and cost report buckets with each bucket's KMS key. S3 server
access logging requires the destination bucket to use SSE-S3; with SSE-KMS,
log objects might be delivered encrypted with a key the account cannot use.

- **The three access-logs buckets switch to SSE-S3 in place.** The redeploy
  changes only their default encryption and drops the key-policy statement
  for `logging.s3.amazonaws.com`; no bucket is replaced and no object is
  touched. Log objects delivered earlier keep their KMS encryption and stay
  readable to principals with `kms:Decrypt` on the key, which is unchanged.
- **BREAKING (only for reports you added): the cluster-shared and cost
  report keys no longer name the `s3.amazonaws.com` service principal, and
  the cluster-shared key no longer names CloudWatch Logs.** Nothing GCO
  deploys used those statements: S3 encrypts objects with the permissions of
  whoever reads or writes them, and no log group uses either key. If you
  configured S3 Inventory reports or storage class analysis exports that S3
  writes into the cluster-shared or cost report bucket, add a key-policy
  statement for `s3.amazonaws.com` (`kms:GenerateDataKey`) to that key
  before redeploying, or point them at another bucket.

### Analytics roles act on their own domain

With `analytics_environment.enabled`, the analytics stack's roles were scoped
to what they use.

- **BREAKING (narrow): the SageMaker execution role's Studio actions name
  this stack's domain.** Creating, updating or deleting spaces and apps,
  tagging, and describing profiles through the role's own statement now
  covers `<project>-studio-<region>` only, not every domain in the Region; a
  notebook that managed another domain's spaces or apps with the execution
  role needs a role of its own for that. `AmazonSageMakerFullAccess` stays
  attached, unchanged.
- **The role reads exactly the three cluster-shared parameters**
  (`/<project>/cluster-shared-bucket/{name,arn,region}`) and uses the
  cluster-shared key only through S3 for that bucket
  (`kms:EncryptionContext:aws:s3:arn`), as the regional job role does.
- **BREAKING (narrow): notebooks can no longer send to the job dead-letter
  queues.** The role's `sqs:SendMessage` names each workload Region's job
  queue (`<project>-jobs-<region>`); its `<project>-jobs-*` pattern also
  matched the dead-letter queues (`<project>-jobs-dlq-<region>`). Submit jobs
  to the job queues; a notebook that wrote to a dead-letter queue needs a
  role of its own for that.
- **The stack-deletion cleanup Lambda** holds exactly the API calls it makes,
  scoped to the domain, `Studio_EFS`, the Region's access points and file
  systems, and security groups in the analytics VPC.

### Studio login takes an access token

With `analytics_environment.enabled`, `GET /studio/login` on
`<project>-api-gateway` requires the Cognito scope
`aws.cognito.signin.user.admin`. A Cognito authorizer reads the token as an
access token when the method requires a scope, and an ID token carries no
scope, so the route accepts only access tokens. API Gateway recommends
requiring scopes because nothing else tells the two token types apart.
`gco analytics studio login` sends the access token from this release on.

- **BREAKING (narrow): an older CLI cannot sign in to an upgraded API.** It
  sends the ID token and gets `HTTP 401`. Upgrade the CLI with the stacks;
  `gco upgrade` does both.
- **This release's CLI cannot sign in to an API that was not redeployed.**
  An API deployed by an earlier release accepts only an ID token, so it
  refuses the access token with `HTTP 401`, and the CLI says to run
  `gco stacks deploy <project>-api-gateway`.
- **Your own clients** that call `/studio/login` must send the
  `AccessToken` from `AdminInitiateAuth` or `InitiateAuth` instead of the
  `IdToken`. The Lambda reads the username from the access token's
  `username` claim (an ID token names it `cognito:username`) and the
  `sub` claim, which both tokens carry.
- **The route documents its `403`.** `UserProfileOwnedByAnotherUser` (a
  profile of that name belongs to another Cognito user) is now one of the
  method's declared responses, so it is in
  `docs/openapi/api-gateway-global.json`.

### Studio users: first sign-in and removal

- **A new user can sign in with the temporary password.**
  `gco analytics users add` now generates the temporary password, prints
  it once and, with `--email`, has Cognito email it in its invitation (the
  invitation used to go by SMS, which these users have no number for, and
  Cognito never returns a password it generates, so nobody received one).
  `gco analytics studio login` answers the `NEW_PASSWORD_REQUIRED`
  challenge with `--new-password` (or `$GCO_STUDIO_NEW_PASSWORD`, or a
  prompt on a terminal), which needs
  `cognito-idp:AdminRespondToAuthChallenge`; it used to fail with "no
  AccessToken returned". With `--password` or `--generate-password`,
  `users add` sends no invitation.
- **`users add` JSON for a temporary password changed.** It now has
  `password_state: "temporary"`, `password_generated: true`,
  `password_source: "generated"`, `password_permanent: false`, the
  `password` and `invitation_emailed`; the `not_returned` / `unavailable`
  values are gone.
- **BREAKING (narrow): `gco analytics users remove` also deletes the
  Studio profile.** It deletes the user's SageMaker Studio profile, the
  spaces it owns and their apps, then the Cognito user, and needs the
  SageMaker permissions listed in
  [CLI.md](CLI.md#gco-analytics-users-remove). A profile tagged for another
  Cognito user is left in place. When the profile cannot be deleted the
  command exits `1` and keeps the Cognito user; pass
  `--keep-studio-profile` for the old behavior. The `<project>-analytics`
  stack publishes the domain id as `StudioDomainId` (an older stack is
  found by its domain name) and the EMR Serverless application id as
  `EmrServerlessApplicationId`.
- **`gco jobs logs` reads a finished job from Studio too.** The Studio
  execution role may run the CloudWatch Logs Insights query the command
  falls back to once a job's pods are gone (`logs:StartQuery` on each
  workload Region's Container Insights application log group, and
  `logs:GetQueryResults`).
- **No more per-user EFS access points.** The presigned-URL Lambda no
  longer creates an access point at `/home/<username>` at every login, and
  its role loses `elasticfilesystem:DescribeAccessPoints`,
  `CreateAccessPoint` and `TagResource`. Nothing mounted through them (a
  profile's `EFSFileSystemConfig` takes no access point), so Studio behaves
  the same. The ones already created stay until the stack is destroyed,
  whose cleanup deletes them, or until you delete them with
  `aws efs delete-access-point`.

### Studio spaces can attach Studio_EFS

In earlier releases the analytics environment's `Studio_EFS` could not be
used, and new users could not sign in:

- A user's first `gco analytics studio login` created a Studio profile that
  went to `Failed` ("You don't have permission to describe the mount
  targets for the following file system"): `CreateUserProfile` checks the
  profile's custom file system with the caller's identity, and the
  presigned-URL Lambda lacked `elasticfilesystem:DescribeMountTargets`. The
  Lambda replaced the profile at every poll, and the command gave up with
  `User profile did not become ready within 120s`.
- A JupyterLab or Code Editor space with `Studio_EFS` attached failed to
  start with "SageMaker is unable to mount the file system. Check your file
  system policy": the file system policy granted the mount actions to the
  execution role, and Studio mounts without IAM authorization.
- With that fixed, the space would still fail ("Check your file system path
  input to ensure that the path provided exists"): nothing created
  `/home/<username>`.

The redeploy of `<project>-analytics` fixes all three
([ANALYTICS.md → Studio_EFS](ANALYTICS.md#k-studio_efs-and-the-studio-home-directory)):

- **The presigned-URL Lambda moves into the analytics VPC** and mounts
  `Studio_EFS`'s `/home` through a new access point, `StudioHomeAccessPoint`.
  Its role gains `elasticfilesystem:DescribeMountTargets`,
  `elasticfilesystem:ClientRootAccess` through that access point, and the
  `AWSLambdaVPCAccessExecutionRole` managed policy. Its SageMaker calls go
  through the VPC's SageMaker API endpoint.
- **The file system policy lets any client mount `Studio_EFS`**, write and
  act as root, only through a mount target in the analytics VPC.
- **Each user's directory is created at the next `studio login`**, with
  `/home/sagemaker-user` for profiles created outside the Lambda. A profile
  an earlier release left `Failed` is replaced at that login too.
- **Destroying `<project>-analytics` takes about 20 minutes longer**:
  CloudFormation deletes a VPC-attached Lambda function only once Lambda
  has released its network interfaces.

### `Project` tag on PCS instances

The cost reports filter Cost Explorer on the `Project` tag that `cdk.json`
`context.tags` puts on the whole app (`GCO` as shipped). In `pcs` managed
mode, the EC2 instances and EBS volumes AWS PCS launches take their tags from
the node groups' launch templates, which set `Project` to `project_name`
(`gco` as shipped), as did the PCS lifecycle log group, so PCS compute spend
fell outside the reports.

- **BREAKING (narrow, PCS managed mode): PCS resources carry
  `Project=<context.tags.Project>`.** `project_name` stands in when
  `context.tags` sets no `Project`. The redeploy updates the launch templates
  and the log group in place; the node groups move to the new launch template
  version, and running nodes keep the old tag until PCS replaces them. A
  tag-based IAM condition, cost filter or script that finds PCS instances by
  `Project=<project_name>` should match the `gco:project` tag, which keeps the
  project name, or the new value.

### Add-on replay input in parts

A regional deployment whose Helm convergence input encoded to more than 8 KiB
failed and rolled back with
`Encoded convergence replay input is <n> bytes; SSM Parameter Store supports at most 8192 bytes`.
The default charts plus Slurm and YuniKorn, with PCS managed mode, HyperPod
and EKS capabilities enabled, are one configuration that reaches that size.

- **Such a deployment now succeeds.** The input's limit is now Step
  Functions' 256 KiB execution-input limit. Its replay copy for `gco stacks addons install`,
  `/<project>/addons/<region>/_input`, stays one value while its encoding fits
  in 8 KiB, as in earlier releases. A larger encoding is written to
  `_input.<digest>.1` to `_input.<digest>.<n>`, and `_input` holds a header
  that names the parts. `gco stacks addons status` does not list the parts,
  `destroy-all` deletes them with the other add-on parameters, and reading
  them takes the same `ssm:GetParameter` permission as `_input`.
- **Replay a split input with this release's CLI.** An older
  `gco stacks addons install` cannot read the header and exits 1 with
  `Could not read /<project>/addons/<region>/_input`. Only an input too large
  for the earlier releases to deploy is split.

### Volcano Jobs get their pods

Releases from v8.18.1 to v8.23.0 install the Volcano 1.16.0 chart with
v1.15.2 images. The chart registers the PodGroup validating webhook for
updates as well as creations, and the v1.15.2 webhook manager rejects every
update; the `volcano-scheduler` log repeats
`admission webhook "validatepodgroup.volcano.sh" denied the request: unsupported operation UPDATE`.
The scheduler cannot move a PodGroup to `Inqueue`, so a Volcano Job
(`batch.volcano.sh/v1alpha1`) stays `Pending` with no pods. Kubernetes Jobs
whose pods set `schedulerName: volcano` are still scheduled.

- **Volcano runs its 1.16.0 images.** The redeploy rolls the Volcano
  controller, scheduler and admission deployments once. With
  `volcano_image_mirror` enabled, the deploy mirrors the `v1.16.0` images
  into ECR first, as for any tag change.
- **Pending Volcano Jobs can start.** A Volcano Job left pending by the old
  images can get its pods once the new scheduler runs. Delete any you no
  longer want before you redeploy.

### Kueue's controller is not preempted

Earlier releases ran the Kueue controller at the default priority (0). Its
Job and Pod webhooks reject creates in every namespace but `kube-system` and
`kueue-system` while it is down (`failurePolicy: Fail`), and on full nodes
GCO's own `gco-platform-critical` pods, such as the SQS queue processor,
preempted it. Until its replacement started, Job submissions failed with
`failed calling webhook "mjob.kb.io": ... no endpoints available for service "kueue-webhook-service"`.

- **The controller runs at `system-cluster-critical`**, like the AWS Load
  Balancer Controller and Volcano's admission webhook. The redeploy rolls it
  once.

### The keep-warm GPU pool

The release adds `gpu-keep-warm-pool`, which keeps an empty GPU node for a
window so that short GPU Jobs run back to back reuse it
([GPU_KEEP_WARM.md](GPU_KEEP_WARM.md)).

- **The pool launches nothing by itself.** Its nodes take only pods that
  select `gco.aws/keep-warm` and tolerate its taint, so running Jobs stay
  where they are. Without a `gpu_keep_warm` block in `cdk.json` the window is
  10 minutes, so an earlier release's `cdk.json` deploys unchanged.
- **The health monitor rolls once.** It gets a third pre-created election
  Lease, `gco-health-monitor-keep-warm`, and the Role
  `gco-health-monitor-keep-warm-records`, which lets it read and patch Jobs in
  `gco-jobs` to write their idle records.
- **Node agents tolerate one more taint.** The CloudWatch Observability, EFS
  CSI, FSx CSI and Pod Identity Agent add-ons, node-exporter, the DCGM
  exporter and the EFA device plugin tolerate `gco.aws/keep-warm`. The
  redeploy updates those add-ons' configuration and rolls their DaemonSets
  once.
- **Admission judges manifests that select the pool.** A manifest that
  selects `gco.aws/keep-warm` but could never run on the pool is refused on
  every submission path. Manifests that don't select it are judged as before.

### Log groups earlier upgrades left behind

Each `gco upgrade` and `gco stacks destroy-all --keep-control-plane` run with
v9.2.0 or earlier left one CloudWatch log group in the monitoring Region when a
workload Region was not the monitoring Region:
`/aws/lambda/<project>-monitoring-CustomCrossRegionExportReaderCustom-<suffix>`.
The monitoring stack reads the regional stacks' values through that Lambda
function. The update that stops it referencing them deletes the function, but
CloudFormation never owned the log group Lambda created for it, and the group
never expires.

- **This release's teardown deletes the group.** It lists the monitoring
  stack's functions before the update and deletes the log groups of the ones
  the update removed ([CLI.md → Keeping the control plane](CLI.md#keeping-the-control-plane---keep-control-plane)).
  `gco upgrade` runs the teardown of the release it upgrades from, so an
  upgrade from v9.2.0 or earlier still leaves one.
- **Delete the groups already there.** Each run left a group with its own
  suffix. List the monitoring stack's functions and the groups, then delete
  every group that is not `/aws/lambda/<function>` for a function in the first
  list (all of them, once the monitoring stack is deleted):

  ```bash
  aws cloudformation describe-stack-resources --region <monitoring-region> \
    --stack-name <project>-monitoring \
    --query "StackResources[?ResourceType=='AWS::Lambda::Function'].PhysicalResourceId"
  aws logs describe-log-groups --region <monitoring-region> \
    --log-group-name-prefix /aws/lambda/<project>-monitoring-CustomCrossRegionExportReaderCustom- \
    --query 'logGroups[].logGroupName'
  aws logs delete-log-group --region <monitoring-region> --log-group-name <group>
  ```

### CLI exit codes, output and prompts

Scripts that check `gco`'s exit status, parse its `-o json` or `-o yaml`
output, or run it without a terminal see the changes below. Exit status 1
still means the command failed, and 2 that Click refused its options.

Exit status and options:

| Command | Change | What to do |
|---|---|---|
| `gco jobs submit --wait`, `gco jobs submit-direct --wait` | **BREAKING:** exit 3 when the job fails and 4 when `--timeout` expires; a failed job exited 0 and a timeout 1, which now means the job was not submitted or its status could not be read. `--wait` also follows a Kubeflow TrainJob, with the same codes ([CLI.md → `gco jobs submit`](CLI.md#gco-jobs-submit)) | Treat 3 as "ran and failed" and 4 as "still running"; do not resubmit on either |
| `gco jobs list --region` | Exits 1 when the Region's API read fails (it printed no jobs); `--limit` is honoured across API pages and must be at least 1 | Handle the failure |
| `gco jobs list --all-regions` | **BREAKING:** exits 1 when the global API reports a Region it could not read; it exited 0. The other Regions' jobs are still printed, and each unread Region is listed under `errors` as `{"region", "error"}` (named on stderr in table mode) | Check the exit status; `errors` names the Regions that are missing |
| `gco jobs delete` | **BREAKING:** exits 1 with the API's 404 for a name that is neither a Job nor a TrainJob (with the TrainJob CRD installed it printed "deleted"); a TrainJob delete the cluster refuses is reported instead of "not found" | Check the name |
| `gco inference promote`, `gco inference rollback`, `gco capacity traffic-dial set` | **BREAKING:** a declined confirmation exits 1 (it exited 0 with "Cancelled") | Pass `-y` where the change is intended |
| `gco inference configure-store` | **BREAKING:** with no option, exits 1 and changes nothing; with a store block it used to restart the rollout | Pass at least one setting |
| `gco inference deploy` | **BREAKING:** exits 2 for `-e`, `-l` or `--node-selector` without `KEY=VALUE`, an `--autoscale-metric` other than `TYPE[:TARGET]` (`cpu`, `memory`, `gpu` or `gpu_memory`, target a whole number of at least 1), `--min-replicas` or `--max-replicas` below 1 or a maximum below the minimum, and replica bounds without `--autoscale-metric`, which were ignored | Fix the value; use `--replicas` for a fixed count |
| `gco capacity reserve`, `gco capacity create-reservation` | **BREAKING:** ask for confirmation before spending money; without a terminal on stdin, exit 2 unless `--yes` or `--dry-run` is given | Add `--yes` to unattended runs |
| `gco capacity cancel-reservation` | **BREAKING:** without a terminal on stdin, exits 2 unless `--yes` or `--dry-run` is given; a piped answer no longer works | Add `--yes` |
| `gco capacity ai-recommend` | **BREAKING (narrow):** exits 2 for an `-i` that is not an EC2 instance type once aliases are expanded (an `ml.` HyperPod type, `gb200`), an `-r` or `--bedrock-region` that is not a Region name, a `--max-cost` of 0 or less, and a negative `--min-gpus` or `--min-memory-gb`. `--min-memory-gb` is the workload's total GPU memory and `--min-gpus` and `--max-cost` are totals across its instances; the model used to get a bare "Minimum Memory" | Fix the value; give totals |
| `gco capacity reserve`, `create-reservation`, `cancel-reservation` with `-o json` or `-o yaml` | Exit 1 when the request fails (they exited 0); the result document is still printed | Check the exit status |
| `gco costs forecast` | **BREAKING:** exits 1 when Cost Explorer cannot forecast (it exited 0 with an `error` field) | Handle the failure |
| `gco costs workloads` | **BREAKING:** exits 1 when a Region's cluster cannot be read; the workloads it read are still printed | Run it where each cluster's API endpoint is reachable |
| `gco costs` commands with `--days` | **BREAKING:** `--days` must be at least 1 (exit 2) | |
| `gco queue list` | `--limit` must be 1-1000, the API's range (exit 2); `--cursor` reads the next page | |
| `gco stacks access` | Exits 1 when the access entry, the policy association or kubectl is refused (it said the entry "may already exist" and exited 0) | Fix the permission the message names |
| `gco stacks addons status` | Exits 1 when a Region cannot be read or an add-on is not healthy | |
| `gco images orphans` | **BREAKING:** exits 1, naming the source, when the inference endpoints or a Region's jobs cannot be read; it treated an unreadable source as using no image, so it listed tags in use. It also counts more images as in use: every image an endpoint record names and the images of every unfinished job whatever its age, by tag or by digest, through any Region's replica | Fix the read the message names before deleting anything |
| `gco images describe` | **BREAKING:** exits 1, with `Tag '<tag>' not found in <name>` on stderr, for a tag the repository does not have; it exited 0 | Treat exit 1 as "no such image" |
| `gco files access-points` | **BREAKING:** exits 1, with the error on stderr, when the access points cannot be read: access denied, throttling, a malformed ID or a service error. It printed "No access points found" (`[]`) and exited 0. A file system that does not exist still gives `[]` | Fix the permission or ID the message names |
| `gco vector ingest` | **BREAKING:** exits 1 (`vector_ingest_failed`) before uploading anything when a file is over 4 MiB or makes more than 500 chunks, the ingest Lambda's limits, or when two files have the same file name, which upload to the same key | Split large files; rename one of two files with the same name |
| `gco slurm submit` | **BREAKING:** refuses `--mem=0`, also as an `#SBATCH` directive, which it sent as a 1 MiB limit | Give a size |
| Commands that look up the regional stacks (`gco jobs submit-direct`, `submit-sqs`, `queue-status` and the fleet-wide commands) | **BREAKING:** a throttled, denied or failed `DescribeStacks` fails the command; it read as "No GCO stack found" or dropped the Region | Fix the credentials or permission the message names |
| Commands that put a name or ID into an API path | **BREAKING:** an empty value, `.`, `..`, or a value with such a path segment is refused before any request (exit 1); `gco inference invoke --path` must start with `/` | |

JSON and YAML output. **BREAKING** for scripts that parsed the old output:
these commands printed `{"status": "ok"}` or their text wrapped in
`{"status": "ok", "output": "..."}`, and now print one document with their
data.

| Command | Prints |
|---|---|
| `gco inference scale`, `stop`, `start`, `delete`, `update-image`, `canary`, `promote`, `rollback` | The updated endpoint record |
| `gco jobs queue-status --all-regions` | A list of the per-Region documents `queue-status -r` prints |
| `gco tasks list` | `{"tasks": [...]}` |
| `gco dag run` | The DAG with each step's `status`, `job_name`, times and `error`, and a top-level `status` of `succeeded` or `failed` |
| `gco dag validate` | `name`, `valid`, `errors`, `region`, `namespace`, `steps` and, when valid, `execution_order` |
| `gco stacks addons status` | A list of `{region, name, status, message}` rows |
| `gco stacks access` | `cluster`, `region`, `principal_arn`, `api_server_reachable`, `node_count`, `kubectl_error`, `warning` |
| `gco analytics doctor` | `{"passed": ..., "checks": [{"name", "ok", "remediation"}]}`, also when it exits 1 |
| `gco capacity ai-recommend` | The recommendation's fields, now with `instance_count`, `reservation_advice`, `validation_warnings` (GCO's checks of the answer, a failed one lowering `confidence` to `low`), `model_id`, `bedrock_region`, `data_collected_at`, `regions_analyzed`, `instance_types_analyzed`, `data_gaps`, `usage`, `latency_ms` and `disclaimer`. `recommended_capacity_type` is `unknown` when the model names no type it recognizes; it read `spot` |
| `gco capacity check --enrich-historical` | `{"estimates": [...], "historical": {...}}` |
| `gco capacity spot-prices`, `gco files list`, `gco files access-points`, `gco images replication status` and `gco nodepools list` with nothing to list | `[]` |
| `gco images replication get` with no replication rules | `{"registryId": ..., "policy": {"rules": []}}`, the document it prints when there are rules |
| `gco models delete` | `{"model_name": ..., "files_deleted": N}`, `N` being `0` for a model without files |
| `gco -o yaml deps scan` | The same envelope as `-o json`; `--report` now writes its file in every mode |
| `gco jobs submit --check-policy` | The submission document; the policy findings go to stderr |
| Every command | Datetimes in ISO 8601 at any depth (`2026-01-15T10:30:00+00:00`); those inside job, stack and file records printed as `2026-01-15 10:30:00+00:00` |

Prompts, namespaces, Regions and permissions:

- **BREAKING: `gco jobs submit-queue` is `gco queue submit` under a second
  name.** It goes through the global API (a Region's bridge only with
  `--regional-api`), checks `--priority` in 0-100 and has the spot price
  gate, and `gco queue submit` gains `--sandbox`. Both send the manifest's
  own namespace, else `--namespace`, else `GCO_DEFAULT_NAMESPACE` (default
  `gco-jobs`); they used to send `gco-jobs` and get a 422 for a manifest in
  another namespace. A `--namespace` that contradicts the manifest fails
  before any AWS call: drop one of them.
- **BREAKING: `gco jobs submit-sqs` applies `GCO_DEFAULT_NAMESPACE`** to a
  manifest without a namespace, as `gco jobs submit` does; the queue
  processor used to put it in `gco-jobs`. Set the namespace in the manifest
  to pin it.
- **`--status succeeded` matches succeeded jobs**; see
  [Job status filter and request paths](#job-status-filter-and-request-paths).
- **BREAKING: `gco webhooks create` asks for the secret on a terminal** when
  neither `--secret` nor `GCO_WEBHOOK_SECRET` is set. Pass `--secret ''` to
  register without one; without a terminal nothing changes.
- **BREAKING: `gco config-cmd init` no longer writes `project_name` or the
  Regions.** A `~/.gco/config.yaml` an earlier `init` wrote still pins them,
  and a key in that file overrides every checkout's `cdk.json`, so a second
  deployment's checkout can drive the first one's stacks. Rerun
  `gco config-cmd init --force`.
- **BREAKING: cluster-access commands follow the CLI's Region.**
  `gco cluster tunnel`, `cluster doctor`, `monitoring open`,
  `costs dashboard`, `gitops`, `crossplane` and `slurm` take `--region`, else
  the default Region (`gco -r`, `GCO_DEFAULT_REGION` or the config file),
  else the first `deployment_regions.regional` entry. They used to skip
  straight from `--region` to that entry. Pass `--region` where a script
  depends on the Region.
- **BREAKING: `gco monitoring users add`, `list` and `remove` read the
  Grafana Secret on the CLI's Region.** Without `--admin-password` (or
  `GCO_GRAFANA_ADMIN_PASSWORD`) they read the `kube-prometheus-stack-grafana`
  Secret on the `--region` cluster, else the default Region's, through that
  cluster's kubeconfig context, and give kubectl 60 seconds. They used to read
  it through the current kubeconfig context, whichever cluster that named,
  and outside the VPC waited for a private endpoint (the default) until TCP
  gave up. Pass `--region` where a script depends on the cluster, and outside
  the VPC `--via-ssm INSTANCE_ID|auto` (`-y` skips the `auto` bastion
  prompt): the read opens an SSM tunnel of its own, next to the one
  `gco monitoring open` holds. The `monitoring_users_list`,
  `monitoring_user_add` and `monitoring_user_remove` MCP tools take optional
  `region` and `via_ssm` arguments and pass them on; `via_ssm` names an
  SSM-managed instance, and `auto` is refused
  (`code: bastion_not_provisioned`) because the tools never launch a
  bastion. Without `via_ssm`, run the server where the Region's API endpoint
  is reachable, or set `GCO_GRAFANA_ADMIN_PASSWORD` in its environment.
- **BREAKING: `gco vector status`, `search` and `ingest` read the CLI's
  deployment.** They take the project and the global Region from the CLI
  configuration (`project_name` and `deployment_regions.global` in
  `cdk.json`, the config file, `GCO_PROJECT_NAME`, `GCO_GLOBAL_REGION`) and
  read the store's SSM parameters in that Region, where the global stack
  publishes them. They used to take `GCO_PROJECT_NAME` (default `gco`) and
  `AWS_REGION` or the profile Region, so they called a deployed store
  unavailable from another Region and read the `gco` project's store from
  another project's checkout. `DYNAMODB_REGION` and `GLOBAL_REGION` still
  come first. Where `AWS_REGION` chose the Region, set `GCO_GLOBAL_REGION`;
  outside a checkout the CLI's global Region defaults to `us-east-2`.
- **BREAKING (projects not named `gco`): `gco inference` reads the CLI's
  deployment.** Every `gco inference` command, the endpoint scan of
  `gco images orphans`, the MCP inference tools and the
  `gco://inference/{endpoint_name}` resource use the
  `<project_name>-inference-endpoints` table in the CLI's global Region
  (`project_name` and `deployment_regions.global` in `cdk.json`, the config
  file, `GCO_PROJECT_NAME`, `GCO_GLOBAL_REGION`). They used to read
  `gco-inference-endpoints` whatever the project, so in another project they
  failed with `ResourceNotFoundException`, or read and changed the endpoints
  of a `gco` deployment in the same account. `INFERENCE_ENDPOINTS_TABLE_NAME`
  still names the table when it is set; unset it unless you mean to read
  another table.
- **BREAKING (automation that selects image repositories by tag):
  `gco images init` tags a new repository with the stacks' `Project` value**,
  `context.tags.Project` in `cdk.json` (`GCO` in the shipped file and when
  none is set), the value `gco costs` filters on. It used to tag
  `Project=<project_name>` (`gco`), so the repositories' storage was missing
  from the cost reports. A repository created earlier keeps its tag; re-tag
  it to have it counted:

  ```bash
  aws ecr tag-resource --resource-arn <repository-arn> --tags Key=Project,Value=<value>
  ```

  In tag-based IAM conditions or scripts, select on the
  `context.tags.Project` value (or set that value to the project name).
- **BREAKING (skopeo older than 0.1.41 only): the image mirror's skopeo
  copies pass the ECR login in a file.** When neither Docker Buildx nor
  Finch/nerdctl can copy multi-arch images, the deploy's Volcano mirror,
  `gco images mirror` and the `images_mirror` MCP tool run `skopeo copy`,
  which now reads the login from an auth file only you can read
  (`--dest-authfile`) instead of `--dest-creds` on its command line, where
  every local user could see the token. skopeo 0.1.41 and later have the
  option; upgrade an older one.
- **BREAKING: kubectl calls name the cluster's context.** A kubeconfig
  cluster entry pinned to a local tunnel that no context uses now fails
  instead of falling back to the current context, which could be another
  cluster's. Add a context for it
  (`kubectl config set-context <name> --cluster <entry> --user <user>`), or
  remove the entry and rerun the command to refresh it. An SSM tunnel refuses
  a local port that already accepts connections: stop the other listener or
  pick another port.
- **BREAKING: some commands call AWS somewhere else.**
  `gco stacks addons install` reads its state machine from the
  `<project>-<region>` stack, so it needs `cloudformation:ListStackResources`
  there. `gco costs k8s` runs its Athena queries in the CLI's monitoring
  Region: `GCO_MONITORING_REGION`, else `monitoring_region` in the config
  file, else `deployment_regions.monitoring`, else `us-east-2`. Outside a
  checkout it used the default workload Region, and the first two were
  ignored whenever a `cdk.json` was present. `gco capacity ai-recommend` and
  `gco capacity predict` call Bedrock in `AWS_REGION`, else
  `AWS_DEFAULT_REGION`, else the default Region, instead of `us-east-1`; set
  `AWS_REGION` when that Region does not offer the model.
- **BREAKING: the PCS preflight fails a `capacity_reservation.id` that does
  not exist**, so `gco stacks deploy` stops on it instead of warning. Fix the
  ID in `cdk.json`.
- **BREAKING: `gco release validate` refuses an account that still has a
  parameter under `/<project>/`.** Teardowns from earlier releases left the
  add-on and traffic-dial parameters behind; confirm nothing deployed reads
  them and delete them with `aws ssm delete-parameters`
  ([LIVE_RELEASE_VALIDATION.md](LIVE_RELEASE_VALIDATION.md)).
- **`gco upgrade` checks the preserved `cdk.json` before its teardown** from
  the next upgrade on, and stops when the check cannot run
  ([step 4](#how-an-upgrade-works)).

### MCP server tools

These change what a client of the MCP server (`gco-mcp`) sees;
[gco_mcp/README.md → Breaking Changes in This Version](../gco_mcp/README.md#breaking-changes-in-this-version)
has the details.

- **BREAKING: `populate_kv_cache` is gated and confined.** It registers only
  with `GCO_ENABLE_MODEL_UPLOAD=true` and uploads only a source under
  `GCO_STORAGE_LOCAL_ROOT`, like `upload_to_regional_bucket`. Set both for a
  client that uses it.
- **BREAKING: local path arguments must be inside the project root.**
  `manifest_path`, `slurm_submit`'s `script_path`, `images_build`'s
  `context`, the `deploy_*` tools' `outputs_file`, and every step manifest a
  DAG file names must resolve under `GCO_PROJECT_ROOT` (else the checkout,
  else the server's working directory), or the tool returns
  `code: path_outside_project_root`. Move the files under the project, or
  point `GCO_PROJECT_ROOT` at a directory that holds them. Free text that
  contains `..` is no longer refused.
- **BREAKING: `GCO_MCP_ROLE_ARN` applies to the tools' processes**; see
  [MCP server role and its access entry](#mcp-server-role-and-its-access-entry).
- **BREAKING (Mission): scripts and allowlists are held to their limits.**
  `GCO_MCP_CODE_MODE_MAX_DURATION_SECS` now counts the time a scripted
  strategy spends in tool calls, so a script that loops on tool calls stops
  at the cap, and a stopped script ends the iteration with
  `script_time_limit`, `script_memory_limit` or `script_error` instead of
  `max_wall_clock`. An explicit allowlist (`tool_allowlist`, or
  `gco mission start --tool-allowlist`) that names a `mission_*` control
  tool or a `swarm_*` tool is rejected with `control_tool_not_allowed`;
  `mission_memory_search` stays allowed. `predicate` criteria are
  size-bounded ([MISSION.md → Size bounds](MISSION.md#size-bounds)):
  `mission_start` and `swarm_start` reject a larger one with
  `validation_error`, and a session saved before the upgrade reports it as
  `inconclusive` (`predicate_rejected_post_load`). Rewrite such a predicate
  with subscripts (`obs["events"]`) and `int()` or `float()` around the
  operands (`int(obs["step"]) % 10 == 0`).
- **`dag_run` runs as a long task.** Its result is the task status with a
  `task_id`, not the CLI's step document; read the steps with `task_tail`.
- **Failed status tools return their document.** `addons_status`,
  `eks_capabilities_status`, `hyperpod_status`, `hyperpod_preflight` and
  `analytics_doctor` return the CLI's JSON document under `output`, next to
  `error` and `exit_code`.
- **Child processes get `/dev/null` as stdin.** `destroy_stack` and
  `destroy_all` with `yes=False` stop before destroying anything, and
  `analytics_login_url` without a password (argument or
  `GCO_STUDIO_PASSWORD`) returns `code: password_required`.
- **`images_orphans` fails instead of guessing, and `images_describe`
  answers `{}` for a missing tag.** `images_orphans` returns an error when
  the inference endpoints or a Region's jobs cannot be read, as
  `gco images orphans` exits 1, instead of listing tags that may be in use.
  `images_describe` returns `{}` for a tag the repository does not have,
  where ECR's `ImageNotFoundException` used to come back as an error.
- **`deps_scan` is tagged `low-risk`** instead of `safe`: its full scan
  pip-installs into the server's environment.
- **The analytics user tools follow the CLI.** `analytics_user_remove`
  (gated) deletes the user's Studio profile too, waiting up to 20 minutes,
  unless `keep_studio_profile=True`. `analytics_login_url` takes a
  `new_password` for a user's first sign-in, handed to the CLI as
  `$GCO_STUDIO_NEW_PASSWORD`. `analytics_user_add` returns the temporary
  password once under `password`.
- **`ai_recommend` runs asynchronously, for up to 10 minutes.** Cancelling
  the call stops its CLI process; the two-minute limit used to cut the gather
  and the model off. It takes a list (or a comma-separated string) of
  instance types and Regions, `bedrock_region` and `data_only`, and
  `workload` is optional. `fault_tolerance` now defaults to `medium` and is
  always passed on; the old default `low` was never sent, so such calls ran at
  `medium` anyway, and an explicit `low` now reaches the CLI.
- **Audit records are written.** The server writes one JSON line per record
  to stderr, or appends them to the file `GCO_MCP_AUDIT_LOG` names;
  `GCO_MCP_AUDIT_LOG_LEVEL` filters them. Earlier releases wrote none.

### Pre-commit hook for contributors

- **The linter hook's id is `ruff-check`.** `.pre-commit-config.yaml` uses
  ruff-pre-commit's current id instead of its legacy alias `ruff`, so
  `SKIP=ruff` no longer skips the linter: skip it for one commit with
  `SKIP=ruff-check git commit ...`, and run it alone with
  `pre-commit run ruff-check`. CI runs ruff directly and is unaffected.
