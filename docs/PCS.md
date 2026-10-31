# AWS Parallel Computing Service on GCO

GCO can run Slurm on [AWS Parallel Computing Service](https://docs.aws.amazon.com/pcs/latest/userguide/what-is-service.html) (PCS), AWS's managed Slurm, as a second backend behind the same `gco slurm` CLI, `/api/v1/slurm/*` API and `slurm_*` MCP tools the in-cluster [Slinky cluster](SLURM_OPERATOR.md) answers. In **managed** mode each regional stack creates the PCS cluster, its compute node groups, queues, login node and shared storage from one `cdk.json` block; in **existing** mode it fronts a PCS cluster you already run. PCS is opt-in: `pcs.enabled` in `cdk.json`.

## Table of Contents

- [Overview](#overview)
- [Enable PCS](#enable-pcs)
- [Modes](#modes)
- [What Gets Deployed](#what-gets-deployed)
- [Using the Cluster](#using-the-cluster)
- [Node Groups and Queues](#node-groups-and-queues)
- [Storage](#storage)
- [Monitoring](#monitoring)
- [Live Validation](#live-validation)
- [Security](#security)
- [Preflight](#preflight)
- [Compared with Slurm on EKS (Slinky)](#compared-with-slurm-on-eks-slinky)
- [Configuration Reference](#configuration-reference)
- [Operations](#operations)
- [Further Reading](#further-reading)

## Overview

PCS runs Slurm's controller (`slurmctld`, `slurmdbd` and, when enabled, `slurmrestd`) as a managed service and launches EC2 compute nodes in your account from launch templates it is given. GCO's regional API reaches the controller's `slurmrestd` inside the regional VPC on tcp/6820 and authenticates with a JWT it mints from the signing key PCS keeps in Secrets Manager, so the same routes, CLI and tools that drive the Slinky cluster drive PCS. Nothing about a job changes for the caller: `sbatch`-style submission, queue, accounting and cancellation go through the GCO API with the caller's identity stamped into every job's comment.

The two backends are independent and may be on together in one Region: `pcs` is this block, `slinky` is `helm.slurm`. With one on, `gco slurm` commands and the API address it; with both, every call names one (`--backend pcs`, `?backend=pcs`). `gco slurm backends -r REGION` shows which are on.

**When to use PCS on GCO:**

- You want Slurm's control plane run by AWS, with EC2 node groups scaled by Slurm itself, rather than Slurm workers as pods on the EKS cluster.
- Your jobs need whole EC2 instances with their native networking: EFA on every network card of a `p5`/`p6` node, a cluster placement group, a Capacity Block or a reserved instance pool.
- You already run a PCS cluster and want GCO's API, attribution, CLI and MCP tools in front of it without moving it.

## Enable PCS

Edit `cdk.json` and redeploy, use the CLI, or deploy once with a run-scoped override:

```json
{ "context": { "pcs": { "enabled": true } } }
```

```bash
gco stacks pcs enable -y                 # writes cdk.json (managed mode)
gco stacks pcs preflight                 # check the account first
gco stacks deploy-all -y                 # from cdk.json
gco stacks deploy-all -y --enable pcs    # one run, cdk.json untouched
```

`gco stacks deploy` and `deploy-all` run the [preflight](#preflight) themselves for every regional stack with PCS on and stop before CloudFormation on a failure (`--skip-pcs-preflight` overrides). Per-Region settings go in `pcs_regions`, which deep-merges over the global block:

```json
{
  "context": {
    "pcs": { "enabled": false },
    "pcs_regions": { "us-east-1": { "enabled": true } }
  }
}
```

Verify:

```bash
gco stacks pcs status -r us-east-1
gco slurm backends -r us-east-1
gco slurm status -r us-east-1 --backend pcs
```

## Modes

| | `mode: managed` (default) | `mode: existing` |
|--|--|--|
| Who creates the cluster | The regional stack, from `pcs.managed` | You; `pcs.cluster_id` names it (`aws pcs list-clusters`) |
| Compute | Node groups, queues and a login node from the block | Whatever the cluster has |
| Storage | The regional EFS at `/home`, FSx for Lustre at `/scratch` where the Region has it | Whatever the cluster mounts |
| Network | The cluster sits in the regional VPC's private subnets | The cluster must sit in the regional VPC, or `pcs.endpoint_cidr` must name the peered range its REST endpoint lives in; its security group must admit tcp/6820 from the EKS cluster security group |
| Requirements | A Region where PCS is offered | Slurm 25.11 or 26.05, `slurmRest` `STANDARD` |
| `gco slurm shell` / `output --backend pcs` | The managed login node, over SSM | Not available (GCO manages no login node); use your own login path |
| On destroy | `pcs.managed.removal_policy`: `destroy` deletes the cluster, node groups and queues; `retain` keeps them | Nothing of yours is touched |

Both modes give the API pod exactly two reads: `pcs:GetCluster` on that one cluster and the value of its JWT signing key secret (`secretsmanager:GetSecretValue` / `DescribeSecret` on that one secret). In existing mode a deploy-time custom resource (`PcsClusterFacts`) reads the cluster so the stack can wire the key's ARN and refuse a cluster whose REST API is off.

## What Gets Deployed

Managed mode, per Region (`gco/stacks/pcs_cluster.py`):

| Resource | Notes |
|----------|-------|
| `AWS::PCS::Cluster` | `cluster_name` (`<project>-pcs` by default), `slurm_version`, `size`, accounting with `purge_days`, idle `scale_down_idle_seconds`, the REST API on, cluster-level `slurm.conf` / `slurmdbd.conf` / `cgroup.conf` custom settings |
| Security group | Self-referencing all-traffic rule (the shape PCS documents for controller-to-node and node-to-node traffic, which includes EFA's SRD protocol) plus tcp/6820 from the EKS cluster security group for the API pod |
| Node IAM role and instance profile | Path `/aws-pcs/`, `AWSPCSComputeNodePolicy` and `AmazonSSMManagedInstanceCore`, read access to the lifecycle scripts and, when on, write access to the lifecycle log group |
| Launch templates | One per node group and one for the login node: the security group, the instance profile, the AMI, EFA network interfaces, the placement group, the capacity reservation or Capacity Block, and the node lifecycle actions |
| `AWS::PCS::ComputeNodeGroup` | One per `node_groups` entry plus the `login` group; the node group's name is its `name` |
| `AWS::PCS::Queue` | One per `queues` entry; exactly one is Slurm's default partition |
| EFS access point | `/pcs/home` on the regional file system, owned by the identity, mounted at `/home` (`storage.home: efs`) |
| Lookups | A custom-resource Lambda (`lambda/pcs-lookup`) that resolves `sample` and `dlami` to the newest AMI for the cluster's Slurm version and the node group's architecture, narrows a node group without `availability_zones` to the private subnets whose zone offers every one of its instance types (PCS refuses a group whose types are missing from one of its zones, and the regional VPC spans every zone, `us-east-1e` included) and, with `metrics.enabled`, the controller's address for the metrics scrape |
| Metrics (`metrics.enabled`) | The cluster is created with `MetricsType=metrics/openmetrics` and `CommunicationParameters=enable_http`, the security group admits tcp/6817 from the EKS cluster security group, and a Prometheus `ScrapeConfig` targets the controller; see [Monitoring](#monitoring) |
| Lifecycle actions | PCS's maintained scripts (`configure-cloudwatch-logs`, `mount-efs`, `mount-fsx-lustre`, pinned v1.0.0) and GCO's `ensure-identity-home.sh`, run on every boot |
| Lifecycle log group (`lifecycle_logs.enabled`) | `/aws/pcs/<cluster id>/lifecycle`, created by the stack with the project tags and a 30-day retention so it is listed by the inventory and deleted with the stack (the nodes only write to it; an agent-created group would be untagged and outlive `gco stacks destroy`) |
| Stack outputs | `PcsClusterId`, `PcsClusterName`, `PcsMode`, `PcsSlurmVersion`, `PcsRestApiMode`, `PcsRestApiPort`, `PcsSecurityGroupId`, `PcsNodeRoleArn`, `PcsLoginNodeGroupId` (the keys `gco stacks pcs status` and `gco slurm shell` read) |

In both modes the regional stack also sets the API pod's PCS environment (`GCO_PCS_CLUSTER_ID`, `GCO_PCS_IDENTITY`, `GCO_PCS_DEFAULT_PARTITION`, `GCO_SLURM_BACKENDS`) through the manifest replacements and applies `post-helm-pcs-network.yaml`: the `allow-manifest-processor-to-pcs-restapi` NetworkPolicy that lets the API pod reach tcp/6820 in `pcs.endpoint_cidr` (the regional VPC CIDR by default). Both are skipped and pruned when PCS is off ([manifest inventory](../lambda/kubectl-applier-simple/manifests/README.md)).

## Using the Cluster

**The GCO API, CLI and MCP tools.** Every route under `/api/v1/slurm/*` ([API reference](API.md#slurm)) takes `backend=pcs`; the CLI's [`gco slurm`](CLI.md#slurm-commands) commands take `--backend`/`-b pcs`. Both may be left out in a Region where PCS is the only Slurm backend on.

```bash
gco slurm backends -r us-east-1
gco slurm submit job.sbatch -r us-east-1 --backend pcs
gco slurm queue -r us-east-1 -b pcs
gco slurm job 42 -r us-east-1 -b pcs
gco slurm accounting -r us-east-1 -b pcs --since 7d
```

**The identity.** PCS's `slurmrestd` does not support user impersonation (`X-SLURM-USER-NAME`), so the API acts as one fixed POSIX identity, `pcs.identity` (`ec2-user`, uid/gid 1000 by default), carried in the JWT it mints. Jobs run as that uid; the caller's IAM identity travels in each job's comment (`gco:<caller>`) for attribution, exactly as with Slinky. The identity's uid and gid must resolve to a user on the compute nodes: 1000:1000 is the first user on both the sample AMI (`ec2-user`) and the DLAMI (`ubuntu`); a different identity has to exist on your AMI, or `identity.create_user: true` has the `ensure-identity-home` lifecycle action add it to every node on boot (`groupadd` / `useradd` with the configured uid, gid, shell and GECOS; a uid or name that already belongs to someone else fails the boot rather than running jobs as the wrong user). The same action creates the identity's home directory on the shared EFS on every boot.

**The login node.** In managed mode `gco slurm shell --backend pcs` opens an SSM session (the Session Manager plugin, no SSH key, no public IP, no `--via-ssm`) on the login EC2 instance as the identity, and `gco slurm output JOB -b pcs` reads a job's output file through `ssm:SendCommand` (`--follow` streams it over a session). Your IAM identity needs `ssm:StartSession` and `ssm:SendCommand` on the login instance, `ssm:GetCommandInvocation`, `ec2:DescribeInstances` and `cloudformation:DescribeStacks`. PCS replaces the login instance when it is unhealthy; the CLI finds the current one through the node group's `aws:pcs:compute-node-group-id` tag.

## Node Groups and Queues

A node group is the PCS equivalent of a Slinky partition: EC2 instance types, scaling bounds, a purchase option, an AMI and optional Slurm overrides. A queue maps onto one or more node groups; jobs name the queue as their partition.

- **Instance types** share one architecture per group (one AMI per group), read from the family name (`c7g`, `hpc7g` are Graviton). A Capacity Block requires exactly the reserved type.
- **Purchase option** is `ONDEMAND` unless the group buys `SPOT` (`spot_allocation_strategy` optional) or names a `capacity_reservation`: `{"type": "odcr" | "capacity_block" | "interruptible", "id": "cr-..."}` implies On-Demand into a targeted reservation, `CAPACITY_BLOCK` (with `MarketType=capacity-block` in the launch template) or `INTERRUPTIBLE_CAPACITY_RESERVATION`. An explicit `purchase_option` must agree with the reservation.
- **AMI**: `sample` (the PCS sample AMI, Amazon Linux 2023, the default and the login node's), `dlami` (the PCS-ready Deep Learning AMI, Ubuntu 24.04 with NVIDIA drivers, CUDA, EFA and the Lustre client, the production choice for accelerators) or a literal `ami-` id. The keywords resolve at deploy time to an AMI built for the cluster's Slurm version.
- **EFA**: `efa: true` puts one EFA interface on each of `efa_interfaces` network cards (32 on `p5.48xlarge`, one on most EFA types; the preflight checks the type's maximum). EFA traffic stays in one subnet, so the group names exactly one `availability_zones` entry, as does a group with a `placement_group` or a `capacity_reservation`.
- **Slurm overrides**: `slurm_custom_settings` (node-group `slurm.conf` keys such as `Weight`, `Features`, `RealMemory`) and `gres_custom_settings` (`gres.conf` records; PCS generates the GPU record for GPU instance types itself). Queues take `slurm_custom_settings` too (`MaxTime`, `DefaultTime`, `Exclusive`, `PriorityTier`, `AllowAccounts`, `QOS`...).

```json
{
  "context": {
    "pcs": {
      "enabled": true,
      "managed": {
        "node_groups": [
          { "name": "cpu", "instance_types": ["c6i.large", "c6i.xlarge"], "max_nodes": 8 },
          {
            "name": "gpu",
            "instance_types": ["p5.48xlarge"],
            "ami": "dlami",
            "efa": true,
            "efa_interfaces": 32,
            "placement_group": true,
            "availability_zones": ["us-east-1a"],
            "capacity_reservation": { "type": "capacity_block", "id": "cr-0123456789abcdef0" },
            "max_nodes": 2
          }
        ],
        "queues": [
          { "name": "cpu", "node_groups": ["cpu"], "default": true },
          { "name": "gpu", "node_groups": ["gpu"], "slurm_custom_settings": { "MaxTime": "2-00:00:00" } }
        ]
      }
    }
  }
}
```

Every key is validated at synth by `gco/pcs_config.py` with the offending path in the message: an unknown key, a mixed-architecture group, two default queues, a queue naming an undeclared group, a group named `login`, `efa` without one zone, `fsx` scratch without `fsx_lustre` in the Region.

## Storage

- **`/home`** (`storage.home: efs`, the default): the regional EFS file system GCO already runs, through a PCS-only access point rooted at `/pcs/home`, owned by the identity's uid:gid. PCS homes never mix with the EFS root job pods see or with Slinky's `slurm/home`. PCS's `mount-efs` lifecycle script mounts it with `tls,iam,accesspoint=<id>` on every boot; `ensure-identity-home.sh` then creates `/home/<identity>` with the right owner. `identity.dir` must be `/home/<name>` in this mode; with `storage.home: none` set `identity.dir` to a directory the AMI provides.
- **`/scratch`** (`storage.scratch: auto`, the default): FSx for Lustre when the Region has `fsx_lustre` on, mounted by PCS's `mount-fsx-lustre` script; `fsx` requires it (synth fails without it), `none` never mounts it.
- **Lifecycle logs** (`lifecycle_logs.enabled`, on by default): PCS's `configure-cloudwatch-logs` script forwards each node's lifecycle-action output to CloudWatch Logs under `/aws/pcs/<cluster id>/lifecycle` (a log group the stack owns, 30-day retention), the first place to look when a node fails to join.

## Monitoring

`pcs.managed.metrics.enabled` (off by default) scrapes the managed cluster's Slurm metrics into the in-cluster Prometheus, next to the Slinky cluster's, when `cluster_observability` is on. PCS serves Slurm's OpenMetrics endpoints (`/metrics/jobs`, `/metrics/nodes`, `/metrics/partitions`, `/metrics/scheduler`) from slurmctld's own port, 6817, on Slurm 25.11 and later once three things hold ([Slurm metrics in AWS PCS](https://docs.aws.amazon.com/pcs/latest/userguide/slurm-metrics.html)): the cluster's Slurm settings carry `MetricsType=metrics/openmetrics` and `CommunicationParameters=enable_http`, `PrivateData` is unset, and the security group admits HTTP on 6817 from the scraper. The regional stack does all three: it creates the cluster with the two settings (merging `enable_http` into any `CommunicationParameters` you set, and refusing `PrivateData` or another `MetricsType` at synth), opens tcp/6817 from the EKS cluster security group, and resolves the controller's address after the cluster is `ACTIVE` with the `Custom::PcsControllerLookup` custom resource (`GetCluster` lists the endpoints in no fixed order, so the lookup picks the `SLURMCTLD` one by type). The kubectl-applier then applies `post-helm-pcs-observability.yaml`: a Prometheus Operator `ScrapeConfig` (`gco-pcs-slurm-controller`) with the four endpoints as static targets under the `pcs-slurm-controller` job, scraped every 60 seconds as Slurm's metrics guide recommends.

Slurm's series carry no cluster label, so a Region that runs both backends lands two sets of identically named `slurm_*` series in one Prometheus. The `job` label keeps them apart: every PCS dashboard panel and alert selects `job="pcs-slurm-controller"`, and the Slinky dashboard, alerts and KEDA autoscaling queries select `job="slinky-slurm-controller"`. Use the same selector in your own queries.

The *GCO Slurm (AWS PCS)* Grafana dashboard shows pending and running jobs and the largest pending job per partition, node states, allocated CPUs and GPUs, scheduler cycle times and the controller's scrape status; `gco monitoring open` opens Grafana. The `gco-pcs-alerts` PrometheusRule carries `PcsSlurmControllerDown` (no successful scrape for five minutes: the cluster is not `ACTIVE`, the settings changed, or 6817 is closed), `PcsSlurmNodesDown` (DOWN nodes that were not drained for fifteen minutes; PCS scales nodes down through Slurm's power save, which leaves no DOWN record, so a DOWN node failed its lifecycle actions or lost its slurmd) and `PcsSlurmQueueStalled` (pending work and nothing running for thirty minutes: a node group at `max_nodes`, no capacity in its zones, a reservation that is not active, or instances that never register).

An existing cluster (`mode: existing`) is scraped by whoever runs it; the key sits under `managed` on purpose. Metrics are unauthenticated HTTP on the controller ENI, which is why the stack admits only the EKS cluster security group.

## Live Validation

Every [live release validation](LIVE_RELEASE_VALIDATION.md) run deploys PCS in managed mode with its metrics on, whatever the checkout's `cdk.json` says (the run-scoped `pcs_overrides` CDK context, never an edit), and proves it before the release: the `preflight` action runs the same checks as `gco stacks pcs preflight` for the block the run deploys; the `schedulers` action reads `/api/v1/slurm/backends`, submits a batch job with `backend=pcs` that must reach `COMPLETED` on a node PCS launches from zero, requires its accounting record with the caller attribution, and reads the job's output file from the login node with `gco slurm output --backend pcs`; and the teardown waits for the instances PCS launched to finish terminating before the final inventory proves the account clean. A Region where PCS is not offered fails the preflight with the reason. The final inventory's PCS scanner runs in every enabled Region PCS is offered in, read from AWS's public `/aws/service/global-infrastructure/services/pcs/regions` parameters (botocore names no Region for PCS).

## Security

- **Credentials.** The API never holds a long-lived Slurm credential: it reads the cluster's JWT signing key from Secrets Manager (the secret PCS creates with the cluster) and mints a short-lived HS256 token for the identity before each call, re-minted at 80% of its 15-minute life. The grant is `secretsmanager:GetSecretValue` on that one secret; the cluster facts come from `pcs:GetCluster` on that one cluster ARN.
- **Network.** `slurmrestd` is plain HTTP on tcp/6820, reachable only inside the cluster's VPC; the managed security group admits it from the EKS cluster security group alone, and the API pod's NetworkPolicy allows egress only to `pcs.endpoint_cidr` on that port. The managed cluster's self-referencing all-traffic rule is the one PCS documents (EFA's SRD protocol is neither TCP nor UDP, so it cannot be narrowed by port); the deploy acknowledges the cdk-nag restricted-ports finding on exactly that rule.
- **Nodes.** Compute and login instances carry the `/aws-pcs/` node role with `AWSPCSComputeNodePolicy` and `AmazonSSMManagedInstanceCore`; they have no public IP and no SSH key. The login node is reached only through SSM sessions, authorized by IAM.
- **Attribution.** Jobs run as the fixed identity; the caller's IAM identity is in each job's comment, as with Slinky, and the API's request logging applies unchanged.

## Preflight

`gco stacks pcs preflight` checks the account with the AWS APIs alone before a deploy ([CLI reference](CLI.md#gco-stacks-pcs)). Existing mode: the cluster exists and is `ACTIVE` on Slurm 25.11 or 26.05 with the REST API on and a `SLURMRESTD` endpoint, its JWT key secret exists, its security group admits tcp/6820 from the EKS cluster security group (by group or by a CIDR covering the regional VPC), and it sits in the regional VPC or `pcs.endpoint_cidr` covers the endpoint. Managed mode: PCS answers in the Region, the cluster name is free, every instance type exists with the architecture its name implies, supports EFA on enough network cards when asked, and is offered in the group's zones (a group without `availability_zones` must have at least one zone in the Region that offers all of its types; the check names the zones the stack will skip); every capacity reservation exists, is usable (`active`, or `scheduled` for a Capacity Block) and matches its group's type, zone and kind; the EC2 Spot service-linked role exists when a group buys Spot. An AWS error while checking is a warning (the preflight never blocks a deploy over its own permissions); only a definite fact that would break the deploy or the API fails it. `gco stacks deploy` / `deploy-all` run the same checks for every regional stack with PCS on.

## Compared with Slurm on EKS (Slinky)

Both backends give you Slurm 25/26 with `sbatch`/`srun`/`sacct`, accounting, autoscaling from zero, GPU and EFA capacity, a login path, shared `/home` and Lustre scratch, through the same GCO API, CLI and MCP tools. Slinky runs Slurm as pods on the EKS cluster GCO already manages, sharing its node pools, quota, images and observability; PCS runs the controller as a managed service with whole EC2 instances as nodes, which is where native EFA fan-out, placement groups and Capacity Blocks live. [Slurm on GCO (Slinky)](SLURM_OPERATOR.md#compared-with-aws-parallel-computing-service) has the side-by-side table. Because both can be on in one Region, a team can keep interactive and CPU work on Slinky and send large training jobs to PCS without changing tools.

## Configuration Reference

`pcs` in `cdk.json` (every key optional; unknown keys fail synth by name). `pcs_regions.<region>` deep-merges any of these over the global block.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Deploy (managed) or connect to (existing) a PCS cluster in the Region |
| `mode` | `managed` | `managed` or `existing` |
| `cluster_id` | `null` | Existing mode: the cluster's id (`pcs_...`) |
| `endpoint_cidr` | `null` | IPv4 range the API pod may reach `slurmrestd` in; `null` = the regional VPC CIDR |
| `identity.name` / `uid` / `gid` | `ec2-user` / `1000` / `1000` | The POSIX user the API submits as; must exist on the nodes (or see `create_user`) |
| `identity.gids` | `[]` | Supplementary group ids (the primary gid is always included) |
| `identity.dir` | `null` | Home and default working directory; `null` = `/home/<name>`; required with `storage.home: none` |
| `identity.shell` / `gecos` | `/bin/bash` / `null` | Login shell and GECOS field in the token |
| `identity.create_user` | `false` | Have the `ensure-identity-home` lifecycle action create the identity (group and user) on every managed compute node when its uid is free; needs `storage.home: efs` |

`pcs.managed`:

| Key | Default | Description |
|-----|---------|-------------|
| `cluster_name` | `null` | PCS cluster name; `null` = `<project_name>-pcs`. Changing it replaces the cluster |
| `slurm_version` | `25.11` | `25.11` or `26.05` (both serve the OpenAPI version the API speaks) |
| `size` | `SMALL` | `SMALL` (32 nodes / 256 jobs), `MEDIUM` (512 / 8192) or `LARGE` (2048 / 16384) |
| `accounting.enabled` / `purge_days` | `true` / `30` | slurmdbd (`sacct`, `gco slurm accounting`); `-1` never purges. The cluster gets `AccountingStoreFlags=job_comment` (merged into any flags you set) so slurmdbd keeps the API's `gco:<caller>` attribution on every record |
| `scale_down_idle_seconds` | `600` | Idle time before PCS scales a node group's instances down |
| `storage.home` | `efs` | `/home` on the regional EFS through a PCS-only access point, or `none` |
| `storage.scratch` | `auto` | `/scratch` on FSx for Lustre when the Region has it (`auto`), always (`fsx`) or never (`none`) |
| `login.enabled` | `true` | The always-on login node behind `gco slurm shell` / `output` |
| `login.instance_type` / `ami` / `availability_zones` | `t3.medium` / `sample` / `[]` | The login node's instance, image and zones (`[]` = every private subnet whose zone offers the instance type) |
| `node_groups` | one `gco-compute` group of `c6i.large`, 0-4 nodes | See below |
| `queues` | one default `gco` queue on `gco-compute` | See below |
| `metrics.enabled` | `false` | Scrape the controller's Slurm metrics into the in-cluster Prometheus (sets `MetricsType` and `enable_http` on the cluster, opens tcp/6817 from the EKS cluster security group; `PrivateData` must stay unset); see [Monitoring](#monitoring) |
| `lifecycle_logs.enabled` | `true` | Forward node lifecycle-action logs to CloudWatch Logs |
| `slurm_custom_settings` / `slurmdbd_custom_settings` / `cgroup_custom_settings` | `{}` | Cluster-level `slurm.conf` / `slurmdbd.conf` / `cgroup.conf` overrides (`slurmdbd` ones need accounting) |
| `removal_policy` | `destroy` | `destroy` or `retain` the cluster, node groups and queues on destroy |

Per node group (`pcs.managed.node_groups[]`):

| Key | Default | Description |
|-----|---------|-------------|
| `name` | required | 3-25 characters, a letter then letters, digits and hyphens; `login` is reserved |
| `instance_types` | required | EC2 instance types of one architecture |
| `min_nodes` / `max_nodes` | `0` / `4` | Scaling bounds (`max_nodes` up to 2048, within the cluster `size`) |
| `purchase_option` | `null` | `ONDEMAND`, `SPOT`, `CAPACITY_BLOCK` or `INTERRUPTIBLE_CAPACITY_RESERVATION`; `null` = derived |
| `spot_allocation_strategy` | `null` | `lowest-price`, `capacity-optimized` or `price-capacity-optimized` (Spot only) |
| `capacity_reservation` | `null` | `{"type": "odcr" \| "capacity_block" \| "interruptible", "id": "cr-..."}` |
| `availability_zones` | `[]` | Zones to launch in (`[]` = every private subnet whose zone offers all of the group's `instance_types`, resolved at deploy time); exactly one with EFA, a placement group or a reservation |
| `ami` | `sample` | `sample`, `dlami` or an `ami-` id |
| `efa` / `efa_interfaces` | `false` / `1` | EFA on `efa_interfaces` network cards |
| `placement_group` | `false` | A cluster placement group for the group's instances |
| `slurm_custom_settings` / `gres_custom_settings` | `{}` / `[]` | Node-group `slurm.conf` keys and `gres.conf` records |
| `scale_down_idle_seconds` | `null` | Override the cluster's idle scale-down; `null` = inherit |

Per queue (`pcs.managed.queues[]`):

| Key | Default | Description |
|-----|---------|-------------|
| `name` | required | 3-25 characters, as for node groups |
| `node_groups` | required | Declared node group names (at least one, each once) |
| `default` | `false` | Slurm's default partition (exactly one queue) |
| `slurm_custom_settings` | `{}` | Queue-level `slurm.conf` keys |

## Operations

- **Status:** `gco stacks pcs status -r REGION` (configured block and the stack's `Pcs*` outputs); `gco slurm status -r REGION -b pcs` for the live controller; `aws pcs get-cluster --cluster-identifier <id>` and `aws pcs list-compute-node-groups` for PCS's own view.
- **Nodes that never join:** read the node's lifecycle log in CloudWatch Logs (`/aws/pcs/<cluster id>/lifecycle`); a failed lifecycle action terminates the instance (`TERMINATE` on failure), so the stream is the record.
- **Pending jobs:** `gco slurm queue -b pcs` shows the reason. `Resources` while PCS launches instances is normal; a group at `max_nodes`, a zone without capacity, or a reservation that is not yet `active` holds jobs until it changes. The preflight catches the configuration causes before a deploy.
- **Changing a node group** (instance types, AMI, EFA, zones) publishes a new launch template version and updates the node group; PCS replaces the group's running instances as part of the update, so change a compute group while it is idle. `cluster_name` and `size` replace the cluster (a new cluster id, an empty accounting database); `slurm_version` updates it in place, and nodes keep the old version until PCS replaces them.
- **Disable:** `gco stacks pcs disable -y` then deploy; with `removal_policy: destroy` the cluster, node groups and queues are deleted and the EFS home directory stays. `retain` leaves the PCS resources for you to delete.
- **Login node replaced:** PCS replaces an unhealthy login instance; `gco slurm shell -b pcs` picks the newest running one, so no configuration changes.
- **Pins the monthly dependency scan watches** (`deps-scan`, see [MAINTENANCE.md](MAINTENANCE.md)): the **PCS Slurm Versions** row flags a Slurm version the [PCS User Guide](https://docs.aws.amazon.com/pcs/latest/userguide/slurm-versions.html) supports that `PCS_SUPPORTED_SLURM_VERSIONS` does not list, a listed version PCS has retired, and the managed default within 180 days of its EOL (after EOL, PCS creates no clusters with that version). The **PCS Lifecycle Scripts and AMIs** row flags a newer immutable version of the AWS node lifecycle scripts the node groups run (`mount-efs-v1.0.0.sh` and friends, pinned in `gco/stacks/pcs_cluster.py`; AWS publishes new versions beside the old ones and only moves the `-v1-latest` aliases) and a newer release of the DLAMI or sample-AMI OS line the lookup Lambda resolves. Each is a deployed change: bump the constant and re-run the live release validation.

## Further Reading

- [AWS PCS User Guide](https://docs.aws.amazon.com/pcs/latest/userguide/what-is-service.html), [node lifecycle scripts](https://docs.aws.amazon.com/pcs/latest/userguide/working-with_lifecycle-scripts.html), [Slurm REST API in PCS](https://docs.aws.amazon.com/pcs/latest/userguide/working-with_slurm_rest-api.html)
- [Slurm REST API](https://slurm.schedmd.com/rest_api.html) and [auth/jwt](https://slurm.schedmd.com/jwt.html)
- [Slurm on GCO (Slinky)](SLURM_OPERATOR.md), [Schedulers Overview](SCHEDULERS.md)
- [`gco stacks pcs`](CLI.md#gco-stacks-pcs) and [`gco slurm`](CLI.md#slurm-commands) in the CLI reference
