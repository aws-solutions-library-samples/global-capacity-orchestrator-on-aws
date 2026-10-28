# Slurm on GCO (Slinky)

GCO deploys a production Slurm cluster inside each regional EKS cluster with the [Slinky slurm-operator](https://github.com/SlinkyProject/slurm-operator) by SchedMD: autoscaled CPU and GPU partitions that start at zero workers, accounting with nightly backups, a login pod on the shared filesystem, metrics, alerts and a dashboard, and `sbatch`/`squeue`/`sacct` through the GCO API, CLI and MCP server. Slurm is opt-in: `helm.slurm.enabled` in `cdk.json`.

## Table of Contents

- [Overview](#overview)
- [Enable Slurm](#enable-slurm)
- [What Gets Deployed](#what-gets-deployed)
- [Using the Cluster](#using-the-cluster)
- [Partitions and Autoscaling](#partitions-and-autoscaling)
- [GPU and EFA Workloads](#gpu-and-efa-workloads)
- [Storage](#storage)
- [Accounting](#accounting)
- [Monitoring](#monitoring)
- [Security](#security)
- [Coexistence with Kueue, Volcano and YuniKorn](#coexistence-with-kueue-volcano-and-yunikorn)
- [Compared with AWS Parallel Computing Service](#compared-with-aws-parallel-computing-service)
- [Configuration Reference](#configuration-reference)
- [Operations](#operations)
- [Further Reading](#further-reading)

## Overview

Slinky runs Slurm's daemons as Kubernetes workloads: `slurmctld` (the controller) as a StatefulSet, `slurmd` workers as a `NodeSet` per partition, `slurmrestd` (the REST API) as a Deployment, `slurmdbd` (accounting) as a StatefulSet and a login pod as a `LoginSet`. GCO configures all of that from one `cdk.json` block (`helm.slurm`, validated at synth by `gco/slurm_config.py`), places the control plane on on-demand, non-accelerator nodes, and ships the pieces the chart does not: the accounting database, the autoscaler, the network policies, the metrics scrape, the alerts, the credentials the API and in-cluster clients use, and the `gco slurm` command group.

**When to use Slurm on GCO:**

- Teams that already work in `sbatch`/`srun`/`sacct` terms and want the same workflow on EKS capacity.
- HPC and training workloads that want Slurm's deterministic scheduling, whole-node GPU allocation and job accounting next to the Kubernetes-native schedulers GCO also runs.
- Organizations moving a Slurm cluster to the cloud without adopting a second control plane: the cluster lives inside GCO's regional EKS cluster and shares its node pools, storage, observability and API.

## Enable Slurm

Edit `cdk.json` and redeploy, or deploy once with a run-scoped override:

```json
{ "context": { "helm": { "slurm": { "enabled": true } } } }
```

```bash
gco stacks deploy-all -y                 # from cdk.json
gco stacks deploy-all -y --enable slurm  # one run, cdk.json untouched
```

Autoscaling reads Slurm's metrics through Prometheus, so `cluster_observability` (on by default) must stay on; disabling it with autoscaled partitions fails at synth with the reason. The default partitions fit the default `resource_quota`; see [Partitions and Autoscaling](#partitions-and-autoscaling) for what to raise when you enlarge them.

One toggle installs two Helm charts (versions pinned in [`lambda/helm-installer/charts.yaml`](../lambda/helm-installer/charts.yaml): slurm-operator 1.2.3, Slurm 26.05) and applies the gated manifests listed below. Both charts need [cert-manager](https://cert-manager.io/docs/) for their webhook certificates; GCO installs it by default.

Verify:

```bash
gco slurm status -r us-east-1
```

## What Gets Deployed

| Component | Kind | Where | Notes |
|-----------|------|-------|-------|
| `slinky-slurm-controller` | StatefulSet (slurmctld) | `gco-jobs` | State on a dedicated gp3 claim (`helm.slurm.controller.state_storage_size`, 20Gi); OpenMetrics on port 6817 |
| `slinky-slurm-restapi` | Deployment (slurmrestd) | `gco-jobs` | 2 replicas with a PodDisruptionBudget; the API's and the operator's path into Slurm |
| `slinky-slurm-accounting` | StatefulSet (slurmdbd) | `gco-jobs` | On by default (`helm.slurm.accounting`) |
| `slinky-slurm-mariadb` | StatefulSet | `gco-jobs` | The accounting database, applied before the chart so slurmctld's first boot finds its dbd; password generated in-cluster |
| `slinky-slurm-accounting-backup` | CronJob | `gco-jobs` | Nightly `mariadb-dump` to the regional shared bucket, 30-day lifecycle |
| `slinky-slurm-login-gco` | Deployment (LoginSet) | `gco-jobs` | ClusterIP only; `gco slurm shell` target; mounts `/home` and `/scratch` |
| `slinky-slurm-worker-<partition>` | NodeSet (slurmd) | `gco-jobs` | One per enabled partition, scaled by KEDA from `min_nodes` (0 by default) |
| `slinky-slurm-autoscale-<partition>` | KEDA ScaledObject | `gco-jobs` | Prometheus triggers on pending jobs and the largest pending job |
| `slinky-slurm-token-gco-api`, `slinky-slurm-token-client` | Token (slurm-operator) | `gco-jobs` | Operator-minted, hourly-rotated `auth/jwt` credentials for the GCO API and for in-cluster client pods |
| `gco-slurm-controller` | ServiceMonitor | `monitoring` | Scrapes slurmctld's `/metrics/{jobs,nodes,partitions,scheduler}` |
| `gco-slurm-alerts`, *GCO Slurm* dashboard | PrometheusRule, ConfigMap | `monitoring` | See [Monitoring](#monitoring) |
| `allow-slurm-*`, `allow-prometheus-to-slurm-controller`, `allow-manifest-processor-to-slurm-restapi` | NetworkPolicy | `gco-jobs`, `gco-system` | See [Security](#security) |

Every non-worker pod is pinned to on-demand nodes that carry no `workload-type` label, so the control plane never lands on (and never pins, through `do-not-disrupt`) an accelerator node. Workers go to the pool their partition names.

The gated manifests are `28-storage-slurm-gp3.yaml`, `29-slurm-accounting-db.yaml`, `post-helm-slurm-network.yaml`, `post-helm-slurm-api-access.yaml`, `post-helm-slurm-observability.yaml`, `post-helm-slurm-accounting-backup.yaml` and the Slurm rows of `post-helm-monitoring-servicemonitors.yaml`; all are skipped and pruned when Slurm is disabled ([manifest inventory](../lambda/kubectl-applier-simple/manifests/README.md)).

## Using the Cluster

There are three ways in, all authenticated by GCO rather than by a Slurm credential you hold.

**The GCO API, CLI and MCP tools.** Each region's manifest API exposes `/api/v1/slurm/*` ([API reference](API.md#slurm)) and proxies to slurmrestd with a token the slurm-operator mints for it; your caller identity is stamped into every job's comment (`gco:<caller>`) for accounting. The CLI wraps the routes ([`gco slurm`](CLI.md#slurm-commands)), the MCP server exposes them as `slurm_*` tools ([tool reference](../gco_mcp/tools/README.md#slurmpy)):

```bash
cat > hello.sbatch <<'EOF'
#!/bin/bash
#SBATCH --job-name=hello
#SBATCH --time=00:05:00
hostname; nproc; echo "Hello from Slurm on GCO"
EOF
gco slurm submit hello.sbatch -r us-east-1          # sbatch; #SBATCH directives are honoured, flags override
gco slurm queue -r us-east-1                        # squeue
gco slurm job 42 -r us-east-1                       # scontrol show job (from accounting once finished)
gco slurm output 42 -r us-east-1 --via-ssm auto -y  # the job's stdout, read from the login pod
gco slurm accounting -r us-east-1 --since 7d        # sacct
gco slurm cancel 42 -r us-east-1                    # scancel
```

Jobs submitted this way run as Slurm's `slurm` user in `/home/slurm` on the shared filesystem by default (`--chdir` moves them), so their `slurm-<jobid>.out` is readable from the login pod and from every worker.

**A shell on the login pod.** `gco slurm shell -r REGION --via-ssm auto -y` opens `kubectl exec -it` into the login pod over the private EKS API endpoint (through an SSM-managed instance, like `gco monitoring open`). Every Slurm client is there (`sinfo`, `sbatch`, `srun`, `squeue`, `sacct`, `scontrol`), with `/home` and `/scratch` mounted exactly as the workers see them. There is no SSH listener and no load balancer: the Kubernetes API is the access control.

**In-cluster clients.** A pod in `gco-jobs` labeled `gco.aws/slurm-client: "true"` may reach slurmrestd on port 6820 and authenticates with the operator-minted client token in the Secret `slinky-slurm-client-token`; [`examples/slurm-cluster-job.yaml`](../examples/slurm-cluster-job.yaml) shows the pattern (submit, wait, check the accounting record). Any pod in `gco-jobs` can mount that Secret, so this is the namespace's trust level, not per-pod authorization; multi-tenant submission belongs behind the GCO API.

## Partitions and Autoscaling

A partition is one `NodeSet` of identical worker pods on one GCO node pool. The shipped partitions:

| Partition | Node pool | Worker size | Nodes | Default | Notes |
|-----------|-----------|-------------|-------|---------|-------|
| `cpu` | `cpu` (`workload-type: cpu`) | 8 CPU, 32Gi | 0–8 | yes | `MaxTime=UNLIMITED` |
| `gpu` | `gpu` (`workload-type: gpu`, NVIDIA taint) | 8 CPU, 32Gi, 1 GPU | 0–4 | no | whole-node (`OverSubscribe=EXCLUSIVE`), `MaxTime=2-00:00:00` |
| `gpu-efa` | `gpu-efa` (NVIDIA + EFA taints) | 180 CPU, 1800Gi, 8 GPU, 32 EFA | 0–2 | no | **disabled** template for p5-class nodes; see the quota note |

Each worker advertises exactly its pod's resources to Slurm (`CPUs`, `RealMemory` = the pod limit minus headroom for slurmd, `Gres=gpu:N`), so the node record is identical whichever EC2 instance Karpenter chose and re-registration after a scale-to-zero cycle always matches it. `DefMemPerCPU` is set per partition so a job that names no `--mem` gets its share of the node rather than all of it. Partitions merge over the shipped ones in `cdk.json`:

```json
{
  "context": {
    "helm": {
      "slurm": {
        "enabled": true,
        "partitions": {
          "cpu": { "max_nodes": 16, "capacity_type": "spot" },
          "gpu": { "cpu": "32", "memory": "128Gi", "gpus": 4, "max_nodes": 8, "features": ["a10g"] },
          "gpu-efa": { "enabled": true }
        }
      }
    }
  }
}
```

**Autoscaling** is KEDA driving each NodeSet's scale subresource from slurmctld's own metrics (Prometheus): the pending-job count of the partition plus its busy nodes (ALLOCATED or MIXED, so a worker running one small job counts), and the node count of the largest pending job plus busy nodes (so a 4-node job scales four workers up at once). Scale-up is immediate; scale-down waits ten quiet minutes and then removes one worker every two minutes, because the operator drains a worker before deleting its pod. With `min_nodes: 0` an idle partition costs nothing; the first job then waits for a worker pod and, usually, a Karpenter node (two to four minutes observed). A scaled-in worker keeps its record in slurmctld as `DOWN+DRAIN` with the operator's reason until the next scale-up re-registers it; `gco slurm nodes` shows those as `SCALED-IN` and the `SlurmNodesDown` alert ignores them. Tune `helm.slurm.autoscaling` (`pending_jobs_threshold`, `cooldown_seconds`, `polling_interval_seconds`) and `min_nodes` for a warm pool.

**Quota.** Workers live in `gco-jobs` and count against its `ResourceQuota` and `LimitRange` (`cdk.json` `resource_quota`). Synth fails with the exact figure when a worker is larger than a per-container ceiling or when every enabled partition at `max_nodes` cannot fit the namespace ceiling: enabling the shipped `gpu-efa` template under the default `max_cpu: "400"` needs 456 CPUs (2×180 for EFA, 64 for cpu, 32 for gpu), so raise `resource_quota.max_cpu` (and `max_memory`, `max_gpu`) first. Fitting the quota is necessary, not sufficient: the quota is shared with every other job pod in the namespace.

## GPU and EFA Workloads

GPU partitions run on GCO's NVIDIA pools with `gres.conf AutoDetect=nvidia` (the NVIDIA runtime provides the devices; no NVML build is needed), `GresTypes=gpu`, `cgroup.conf` constraining cores, memory and devices to the allocation, and `AccountingStorageTRES=gres/gpu` so `sacct` reports GPU usage. Request GPUs the Slurm way:

```bash
gco slurm submit train.sbatch -r us-east-1 -p gpu --gpus-per-node 1
# or, in the script:  #SBATCH --partition=gpu  /  #SBATCH --gpus-per-node=1
```

Inside the job `CUDA_VISIBLE_DEVICES` and `SLURM_GPUS_ON_NODE` are set and `nvidia-smi` lists the allocated devices (verified live on g5). The `gpu-efa` template (off by default) adds `vpc.amazonaws.com/efa` interfaces and the `efa` feature for p5-class multi-node training; enable it only after raising the quota as above.

## Storage

- **`/home`** is the shared EFS claim (`gco-shared-storage`) under the `slurm/home` prefix, mounted on every worker and on the login pod; `/home/slurm` is created by the login pod and is the default working directory of API submissions. `storage.home: "none"` leaves it out.
- **`/scratch`** is the FSx for Lustre claim when `fsx_lustre` is enabled (`storage.scratch: "auto"`, the default); `"fsx"` requires it and fails synth without it, `"none"` leaves it out.
- **Controller state** (`StateSaveLocation`) is a dedicated gp3 RWO claim from the `gco-slurm-gp3` StorageClass, expandable online through `helm.slurm.controller.state_storage_size`; the `SlurmControllerStateVolumeFilling` alert fires at 90%.

## Accounting

Accounting is on by default: slurmdbd in front of an in-cluster MariaDB (`slinky-slurm-mariadb`, gp3 claim sized by `accounting.storage_size`, on-demand node, `do-not-disrupt`). The password is generated inside the cluster by the kubectl-applier when it first creates the Secret and never leaves it. `AccountingStoreFlags=job_comment` keeps each job's comment, which is where the API records the submitting caller. A nightly CronJob (`accounting.backup.schedule`, 03:00 UTC) dumps `slurm_acct_db` to the regional shared bucket under `slurm/accounting-backups/<cluster>/` and an S3 lifecycle rule expires dumps after `accounting.backup.retention_days`. Disabling accounting removes the database and its volume; the dumps stay.

To use a database you already run (RDS for MariaDB, Aurora MySQL), set `accounting.external.host` (and `port`, `database`, `username`, `password_secret` naming a Secret in `gco-jobs` with the password under `password_key`); the in-cluster database and backup CronJob are then not deployed. `helm.slurm.slurmdbd_conf` adds `slurmdbd.conf` keys such as `PurgeJobAfter`.

Query it with `gco slurm accounting -r REGION --since 7d --state COMPLETED,FAILED` or `sacct` on the login pod.

## Monitoring

Slurm 26.05 serves OpenMetrics from slurmctld itself (`MetricsType=metrics/openmetrics`, port 6817, paths `/metrics/jobs`, `/metrics/nodes`, `/metrics/partitions`, `/metrics/scheduler`); the `gco-slurm-controller` ServiceMonitor scrapes all four when `cluster_observability` is on. Metric names are `slurm_*` (for example `slurm_partition_jobs_pending`, `slurm_partition_nodes_alloc`, `slurm_nodes_down`, `slurm_jobs_gpus_alloc`, `slurm_schedule_cycle_last`); the autoscaler and the alerts below use exactly these names.

The *GCO Slurm* Grafana dashboard shows pending and running jobs and the largest pending job per partition, node states, worker pods per NodeSet, allocated CPUs and GPUs, scheduler cycle times and control-plane status. The `gco-slurm-alerts` PrometheusRule carries `SlurmControllerDown`, `SlurmRestApiUnavailable`, `SlurmAccountingDown`, `SlurmNodesDown` (DOWN nodes that were not drained for scale-in), `SlurmQueueStalled` (pending work and nothing running for 30 minutes) and `SlurmControllerStateVolumeFilling`. Open Grafana with `gco monitoring open`.

## Security

- **No Slurm credential leaves the cluster.** The chart's JWT signing key (`slinky-slurm-auth-jwt`) is read only by slurmctld, slurmdbd and the slurm-operator. The GCO API authenticates to slurmrestd with a token the operator mints from a `Token` custom resource into `slinky-slurm-token-gco-api` (lifetime 1h, refreshed at 80%), which a Role grants the manifest-processor `get` on, and nothing else in `gco-jobs`. In-cluster clients get the same kind of token in `slinky-slurm-client-token`.
- **Caller attribution.** The API Gateway proxies forward the SigV4 principal's ARN as `x-gco-caller` (a header callers cannot set: the proxy's allowlist drops it) and the API records it in each job's comment.
- **Network.** `gco-jobs` default-denies pod-to-pod traffic; the Slurm policies admit exactly: Slinky's own pods to each other (`app.kubernetes.io/part-of: slurm`), labeled client pods and the slurm-operator's namespace to slurmrestd (6820), Prometheus to slurmctld's metrics port (6817), and the manifest-processor to slurmrestd on both the `gco-system` egress and `gco-jobs` ingress side. slurmrestd has no listener outside the cluster.
- **Placement.** Control-plane pods carry `karpenter.sh/do-not-disrupt` and node affinity away from accelerator pools; slurmd workers run privileged (a Slinky requirement for cgroup device constraints) on the pool their partition names.
- **Shell access** is `kubectl exec` through the private EKS endpoint, so Kubernetes RBAC on `pods/exec` in `gco-jobs` is the access control for `gco slurm shell`.

## Coexistence with Kueue, Volcano and YuniKorn

Slurm manages its own worker pods and its own queue; Slurm jobs run inside slurmd pods, not as Kubernetes Jobs, so Kueue's admission, Volcano's and YuniKorn's `schedulerName`-based scheduling never see them. Workers are ordinary pods to Kubernetes: they take their partition's pool, count against the `gco-jobs` quota, and a GPU worker holds its GPUs for as long as it exists (with `min_nodes: 0` that is only while Slurm has work). [Scheduler Coexistence](SCHEDULERS.md#scheduler-coexistence) covers how every scheduler GCO runs interacts.

## Compared with AWS Parallel Computing Service

[AWS PCS](https://docs.aws.amazon.com/pcs/) is a managed Slurm control plane with EC2 compute node groups. GCO's Slurm runs the same scheduler on the EKS capacity GCO already manages. What you get in both: Slurm 25/26 with `sbatch`/`srun`/`sacct`, accounting, autoscaling from zero, GPU and EFA partitions, a login path, shared `/home` and Lustre scratch. Where GCO differs:

| | GCO Slurm (Slinky) | AWS PCS |
|--|--|--|
| Control plane | Kubernetes workloads in your EKS cluster; GCO owns the config, placement, backups and alerts | Managed by AWS |
| Compute | Slurm workers are pods on GCO's Karpenter pools, scaled by KEDA from Slurm's queue metrics, sharing quota, storage, images and observability with the Kubernetes-native schedulers | EC2 node groups scaled by Slurm's power-save plugin |
| Access | The GCO API (SigV4, caller attribution), `gco slurm`, MCP tools, `kubectl exec` shell; no public endpoint | SSH to login nodes you provision; AWS API for cluster lifecycle |
| Accounting | slurmdbd + in-cluster MariaDB with nightly S3 dumps, or your own database | slurmdbd backed by an Aurora database you provision or the managed accounting option |
| Multi-region | One cluster per GCO region, each behind its regional API; cross-region job routing is outside this release | One cluster per region |
| Cost | Pays only for workers while jobs run plus the small control plane; no per-cluster service fee | Per-cluster control-plane charge plus EC2 |

Not provided here: an SSH login load balancer (shell access is through the Kubernetes API), cross-region Slurm job routing, and Slurm federation.

## Configuration Reference

`helm.slurm` in `cdk.json` (every key optional; unknown keys fail synth by name):

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Deploy the two Slinky charts and the gated manifests |
| `cluster_name` | `gco-slurm` | Slurm `ClusterName` (also the backup prefix) |
| `controller.state_storage_size` | `20Gi` | gp3 claim for `StateSaveLocation` (expandable online) |
| `restapi.replicas` | `2` | slurmrestd replicas (a PDB keeps one available above 1) |
| `accounting.enabled` | `true` | Run slurmdbd |
| `accounting.storage_size` | `20Gi` | In-cluster MariaDB data claim |
| `accounting.backup.enabled` / `schedule` / `retention_days` | `true` / `0 3 * * *` / `30` | Nightly dump to the regional shared bucket and its S3 lifecycle |
| `accounting.external.host` / `port` / `database` / `username` / `password_secret` / `password_key` | `""` / `3306` / `slurm_acct_db` / `slurm` / `""` / `password` | Use an external MySQL-family database instead (host set = external) |
| `metrics.enabled` | `true` | slurmctld OpenMetrics; required by autoscaling |
| `login.enabled` / `login.replicas` | `true` / `1` | The ClusterIP login pod |
| `autoscaling.pending_jobs_threshold` | `1` | Pending jobs per additional worker |
| `autoscaling.cooldown_seconds` | `300` | Idle time before scaling toward `min_nodes` |
| `autoscaling.polling_interval_seconds` | `30` | KEDA metric polling interval |
| `storage.home` | `efs` | `/home` on the shared EFS claim, or `none` |
| `storage.scratch` | `auto` | `/scratch` on FSx for Lustre when enabled (`auto`), always (`fsx`), or never (`none`) |
| `slurm_conf` / `slurmdbd_conf` | `{}` | Extra `slurm.conf` / `slurmdbd.conf` keys (operator-owned keys such as `SlurmctldHost`, `NodeName`, `PartitionName` are refused) |
| `partitions.<name>` | `cpu`, `gpu`, `gpu-efa` (disabled) | Merged over the shipped partitions; see below |

Per partition:

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Deploy the NodeSet and its ScaledObject |
| `node_pool` | `cpu` | `cpu`, `gpu` or `gpu-efa` (GCO's Karpenter pools) |
| `cpu` / `memory` | `4` / `16Gi` | Worker pod size, requests = limits; whole cores, at least 1Gi |
| `gpus` / `efa_interfaces` | `0` / `0` | Devices per worker; GPU pools need `gpus`, the EFA pool needs `efa_interfaces` |
| `capacity_type` | `any` | `any`, `spot` or `on-demand` |
| `min_nodes` / `max_nodes` | `0` / `4` | Autoscaling bounds (up to 1024) |
| `default` | `false` | Slurm's default partition (exactly one enabled partition) |
| `max_time` | `UNLIMITED` | Partition `MaxTime` |
| `exclusive` | `false` | `OverSubscribe=EXCLUSIVE` (whole-node jobs) |
| `features` | `[]` | Extra Slurm node features (the partition name is always one) |

## Operations

- **Status:** `gco slurm status -r REGION`; `gco stacks addons status` for the chart releases; `kubectl get nodesets,scaledobjects -n gco-jobs` through `gco cluster tunnel`.
- **Stuck pending jobs:** `gco slurm queue` shows the reason. `Resources` while a NodeSet shows `0` ready replicas means Karpenter is still provisioning; `ReqNodeNotAvail` or `PartitionConfig` with no worker means the partition is at `max_nodes`, the quota is exhausted, or the pool has no capacity (check `kubectl get nodeclaims` and the `SlurmQueueStalled` alert).
- **Changing a partition's size** rolls its NodeSet; running jobs on the old workers finish first (the operator drains before deleting).
- **Deploying while jobs run** is safe: KEDA owns each autoscaled NodeSet's replica count after install, and the helm installer applies the live count on every upgrade (`autoscaled_replicas` in `charts.yaml`), so a deploy neither scales workers in nor conflicts with the autoscaler. A partition with autoscaling off keeps `min_nodes` as its size and a changed `min_nodes` applies on the next deploy.
- **Backups:** list `s3://<regional-shared-bucket>/slurm/accounting-backups/<cluster>/`; restore by piping a dump into `mariadb` on the `slinky-slurm-mariadb` pod with the password from its Secret.
- **Disable:** set `helm.slurm.enabled: false` (or redeploy without `--enable slurm`); the installer deletes the Slinky custom resources before uninstalling, and the applier prunes every gated object, including the accounting database and its volume.
- **Upgrades:** the chart and image pins live in `charts.yaml`; `SLURM_API_VERSION` in `gco/services/slurm_client.py` names the slurmrestd OpenAPI plugin version and moves with the Slurm release.

## Further Reading

- [Slinky Project](https://github.com/SlinkyProject) and the [slurm-operator](https://github.com/SlinkyProject/slurm-operator)
- [Slurm Documentation](https://slurm.schedmd.com/documentation.html), [REST API](https://slurm.schedmd.com/rest_api.html), [OpenMetrics plugin](https://slurm.schedmd.com/metrics.html)
- [Running Slurm on Amazon EKS with Slinky](https://aws.amazon.com/blogs/containers/running-slurm-on-amazon-eks-with-slinky/) (AWS blog)
- [AWS Parallel Computing Service](https://docs.aws.amazon.com/pcs/)
