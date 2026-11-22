# Learning Path

A hands-on route from a fresh clone to GPU jobs, shared storage and inference with **Global Capacity Orchestrator (GCO)**: *One API. Every Accelerator. Any Region.* Each level adds one idea, says what it costs, and ends with a check that tells you it worked. Level 0 is free. Billing starts at Level 1, and [Clean up](#clean-up) stops it.

You run every step with the `gco` CLI from the repository root. No step needs kubectl, and Level 3 shows how GCO reaches the private Kubernetes API for you when a command does use it.

## Table of Contents

- [Pick your starting point](#pick-your-starting-point)
- [What it costs](#what-it-costs)
- [Level 0: Explore for free](#level-0-explore-for-free)
- [Level 1: Deploy and run a first job](#level-1-deploy-and-run-a-first-job)
- [Level 2: Run a GPU job](#level-2-run-a-gpu-job)
- [Level 3: Keep job outputs](#level-3-keep-job-outputs)
- [Level 4: Pipelines and repeatable jobs](#level-4-pipelines-and-repeatable-jobs)
- [Tracks](#tracks)
- [Learn by asking](#learn-by-asking)
- [Clean up](#clean-up)
- [At a glance](#at-a-glance)

## Pick your starting point

| You are | Start with | Then |
|---------|-----------|------|
| New to Kubernetes | [Level 0](#level-0-explore-for-free), including [The Kubernetes you need](#the-kubernetes-you-need) | Levels 1 to 3, then a [track](#tracks) |
| Comfortable with Kubernetes, new to GCO | [Level 0](#level-0-explore-for-free); skim the primer | Levels 1 to 3, then a [track](#tracks) |
| Evaluating GCO for a team | [What it costs](#what-it-costs) and [Level 0](#level-0-explore-for-free) | [Level 1](#level-1-deploy-and-run-a-first-job) when you are ready to pay for a deployment |
| Planning to build your own scheduler on GCO's API | Levels 0 and 1 | The [Builder track](#builder-put-your-own-scheduler-on-top) |
| Changing GCO itself | [Level 0](#level-0-explore-for-free) | The [Contributor track](#contributor-change-gco-itself) |

[Core Concepts](CONCEPTS.md) explains the ideas behind every level. Read it before or alongside this path.

## What it costs

| Level | What runs | Cost (US East, N. Virginia) |
|-------|-----------|------------------------------|
| 0 | Only your machine, plus free read-only AWS API calls | $0 |
| 1 to 4 | The GCO platform in one workload Region, idle or busy | About $0.77 an hour (about $560 a month); the README's [sample cost table](../README.md#sample-cost-table) itemizes it |
| 2, and the GPU steps of the tracks | A GPU node, only while a pod needs it | A `g4dn.xlarge` is $0.526 an hour on demand plus $0.041 for EKS Auto Mode, billed per second; Spot is cheaper |

The platform cost does not stop between levels. If you pause for a day or more, run [Clean up](#clean-up) and redeploy when you come back. `gco costs summary` shows what you have spent so far.

## Level 0: Explore for free

**You will learn:** how to run the CLI, the handful of Kubernetes ideas GCO uses, and how GCO checks a job before it accepts it.
**Cost:** nothing. The last two steps call read-only AWS APIs, which are free.

### Set up the CLI

You need Git and a container runtime ([Docker](https://docs.docker.com/reference/), [Finch](https://runfinch.com/) or [Podman](https://podman.io/docs)). The setup script builds the dev container, which has every tool GCO needs at pinned versions, and adds a `gco` shell function that runs each command inside it:

```bash
git clone https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git
cd global-capacity-orchestrator-on-aws
./scripts/setup-dev-alias.sh
source ~/.zshrc   # or ~/.bashrc; the script prints which file it updated
gco --version
```

The function mounts the current directory into the container, so run `gco` from the repository root, and keep the files you pass to it under that directory. The [Quick Start](../QUICKSTART.md) has the details and a host install for advanced users.

### The Kubernetes you need

GCO accepts Kubernetes manifests, so its vocabulary is Kubernetes. You do not run a control plane or manage nodes: EKS Auto Mode does both. These are the parts you will meet:

| Kubernetes idea | What it means in GCO |
|-----------------|----------------------|
| Pod | One running copy of your container. It runs on a node that EKS Auto Mode starts for it. |
| Job (`batch/v1`) | What you submit for batch and training work. GCO runs your jobs in the `gco-jobs` namespace. |
| Namespace | `gco-jobs` holds your jobs, `gco-inference` the inference endpoints, and `gco-system` GCO's own services. |
| Resource requests and limits | How much CPU, memory and accelerator a pod needs. A limit of `nvidia.com/gpu: 1` (or `aws.amazon.com/neuron` for Trainium and Inferentia) is what makes EKS Auto Mode start an accelerator node. |
| Taints and tolerations | GPU nodes carry the taint `nvidia.com/gpu=true:NoSchedule`, so only pods that tolerate it land there. GCO refuses an accelerator pod without the toleration at submission, because it could never be scheduled. |
| Node selector | Narrows the nodes a pod may use, for example `eks.amazonaws.com/instance-gpu-count: "1"` for single-GPU instances. |
| NodePool | A [Karpenter](https://karpenter.sh/docs/concepts/nodepools/) rule for which instances EKS Auto Mode may launch. GCO ships pools for NVIDIA GPUs on x86 and Arm, inference, EFA, Neuron and CPU. Nodes start when a pod needs one and stop when they are empty. |
| PersistentVolumeClaim | Storage that outlives a pod. `gco-shared-storage` is the EFS file system in every Region. |
| Deployment and Service | Long-running servers. The inference monitor creates them for each endpoint you deploy with `gco inference deploy`. |

To go further, the official [Kubernetes Basics tutorial](https://kubernetes.io/docs/tutorials/kubernetes-basics/) and the [Job concept page](https://kubernetes.io/docs/concepts/workloads/controllers/job/) cover these ideas in depth.

### Read a GPU job

[`examples/gpu-job.yaml`](../examples/gpu-job.yaml) runs `nvidia-smi` on one GPU. These are the lines that matter (the security context and CPU and memory settings are left out here):

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: gpu-test-job
  namespace: gco-jobs                 # where GCO runs jobs
spec:
  template:
    spec:
      containers:
      - name: gpu-test
        image: nvidia/cuda:13.4.3-base-ubuntu24.04
        command: ['sh', '-c', 'nvidia-smi && echo "GPU job completed!"']
        resources:
          limits:
            nvidia.com/gpu: 1         # one NVIDIA GPU, so a GPU node starts
      restartPolicy: Never
      tolerations:                    # lets the pod onto the tainted GPU node
      - key: nvidia.com/gpu
        operator: Equal
        value: "true"
        effect: NoSchedule
      nodeSelector:
        eks.amazonaws.com/instance-gpu-count: "1"   # a single-GPU instance
  backoffLimit: 3
```

### Check manifests offline

GCO checks every manifest before it runs it: image registries, namespaces, resource caps, security settings and accelerator tolerations. You can run the same checks on your machine.

```bash
gco examples validate --static-only                       # every example, in a few seconds
gco jobs check-policy examples/gpu-job.yaml --offline     # one manifest against cdk.json
```

The first command ends with a line such as `432 checks, 0 failed`, and the second reports `no violations of the configured policy`. Now remove the toleration and check again:

```bash
mkdir -p tmp   # tmp/ is ignored by Git
sed '/tolerations:/,/effect: NoSchedule/d' examples/gpu-job.yaml > tmp/no-toleration.yaml
gco jobs check-policy tmp/no-toleration.yaml --offline
```

This time the check reports `[tolerations] Job requests accelerator 'nvidia.com/gpu' but no matching toleration ...`. A deployed GCO refuses that manifest the same way. Pipelines get the same treatment: `gco dag run examples/pipeline-dag.yaml --dry-run` validates the two-step pipeline you will run in Level 4 and prints its execution order.

### Look at live capacity

With AWS credentials configured (`~/.aws` or environment variables), ask where GPU capacity is right now. This needs no deployment:

```bash
gco capacity check -i g4dn.xlarge -r us-east-1
```

You get one row per Availability Zone for Spot and one for On-Demand, each with an availability rating and the current hourly price. `gco capacity --help` lists the other tools: Spot price history, Capacity Blocks for ML, capacity reservations and SageMaker Training Plans.

### Check your GPU quota

New AWS accounts start with a quota of **0** for GPU instances, so do this now, because an increase can take a while. The quotas count vCPUs: a `g4dn.xlarge` needs 4.

- **Running On-Demand G and VT instances** (quota code `L-DB2E81BA`)
- **All G and VT Spot Instance Requests** (quota code `L-3819A6DF`)

Look them up, and request an increase, in the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/ec2/quotas) for `us-east-1`. With the AWS CLI:

```bash
aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --region us-east-1
```

**Done when:** `gco --version` prints `gco, version …`, the offline checks pass for `examples/gpu-job.yaml` and fail for your copy without the toleration, and you know your GPU quota.

## Level 1: Deploy and run a first job

**You will learn:** what a deployment creates, and the submit, watch and read-logs loop.
**Cost:** billing starts here, at about $0.77 an hour for one Region (see [What it costs](#what-it-costs)).

### Deploy

```bash
gco stacks deploy-all -y
```

With the `cdk.json` in the repository, this deploys GCO's global control plane in `us-east-2` (the global API, the job queue and template tables, monitoring) and one workload Region, `us-east-1`, with a VPC, an EKS Auto Mode cluster, an internal load balancer and the GCO services. It bootstraps CDK where needed and takes about an hour. To use other Regions, edit `deployment_regions` in `cdk.json` first or run `gco stacks regions add <region>`.

When the command finishes, the cluster add-ons (KEDA, Volcano, Kueue, KubeRay, Kubeflow Trainer, cert-manager and the observability stack) are still installing in the background. That takes another 10 to 30 minutes. Follow it with:

```bash
gco stacks addons status -r us-east-1
```

### See the fleet

```bash
gco status
```

`gco status` gathers the stacks, queue depth, jobs, capacity and inference endpoints of every Region into one report. Add `--watch 10` to keep it refreshing.

### Submit a job through SQS

```bash
gco jobs submit-sqs examples/simple-job.yaml -r us-east-1
gco jobs list --all-regions
gco jobs get hello-gco -r us-east-1
gco jobs logs hello-gco -r us-east-1
```

The queue processor checks the queue every 10 seconds, so the Job appears in `gco jobs list` within about a minute, and its logs end with `Hello from GCO!` and `Job completed!`. Here is what happened: the CLI sent the manifest to the Region's SQS queue; the queue processor, which KEDA starts when messages arrive, ran the same checks you ran offline and created the Job in `gco-jobs`; and EKS Auto Mode placed its pod on a general-purpose node. `gco jobs get` shows the Job's status and the node it ran on.

`examples/simple-job.yaml` sets `ttlSecondsAfterFinished: 120`, so Kubernetes deletes the Job two minutes after it finishes. `gco jobs logs` still works after that, because pod logs also go to CloudWatch Logs.

### Try the other ways in

The same manifest goes through GCO's other front doors:

```bash
gco jobs submit examples/simple-job.yaml -r us-east-1 --wait   # the SigV4-authenticated REST API
gco queue submit examples/simple-job.yaml -r us-east-1         # the global job queue
gco queue list
```

The REST API applies the manifest and, with `--wait`, follows the Job until it finishes. The global queue stores the job in DynamoDB, where the target Region claims it, and keeps its status history, which `gco queue list` and `gco queue get` read. Submitting a name again replaces a Job that has finished. [Core Concepts](CONCEPTS.md#manifest-submission) compares the four submission methods.

**Done when:** `gco jobs logs hello-gco -r us-east-1` prints `Hello from GCO!`, and you can say which component turned your SQS message into a Job.

## Level 2: Run a GPU job

**You will learn:** how a GPU request becomes a GPU node, and how to see where a job ran.
**Cost:** a GPU node for a few minutes, about $0.57 an hour for an On-Demand `g4dn.xlarge` (less on Spot). You need the GPU quota from [Level 0](#check-your-gpu-quota).

```bash
gco capacity check -i g4dn.xlarge -r us-east-1
gco jobs submit-sqs examples/gpu-job.yaml -r us-east-1
gco jobs get gpu-test-job -r us-east-1
```

Once the queue processor has created the Job, `gco jobs get` shows its pod waiting, with the scheduler's reason: no node can take it yet. EKS Auto Mode is starting one. GCO's GPU NodePools allow On-Demand and Spot capacity across several NVIDIA instance families, and the node selector limits the choice to single-GPU instances. After a few minutes the pod runs; run `gco jobs get` again to see the node's instance type and whether it is Spot or On-Demand. Then:

```bash
gco jobs events gpu-test-job -r us-east-1   # scheduling, the node's launch, the image pull
gco jobs logs gpu-test-job -r us-east-1     # the nvidia-smi table
gco jobs delete gpu-test-job -r us-east-1 -y
```

This example sets no TTL, so delete the Job yourself. The GPU node does not wait for that: EKS Auto Mode removes a GPU node once its last pod has finished (after 30 seconds in the default GPU pools, 5 minutes in the inference pool), and its cost stops with it. For many short GPU jobs in a row, `--keep-warm` keeps the node for a window instead ([Keep-Warm GPU Nodes](GPU_KEEP_WARM.md)).

**Done when:** the logs show the `nvidia-smi` table, and `gco jobs get` told you the instance type the job ran on.

## Level 3: Keep job outputs

**You will learn:** where jobs write results that must outlive their pods, and how commands that need the Kubernetes API reach a private cluster.
**Cost:** CPU-only pods on the platform's nodes, plus pennies of storage. A temporary `t3.micro` bastion runs for the minutes the kubectl steps take.

A pod's own disk disappears with it. Every Region has these places for outputs:

| Storage | How a job uses it | How you read it back |
|---------|-------------------|----------------------|
| [Cluster-shared S3 bucket](CLUSTER_SHARED_BUCKET.md), in the global Region | `envFrom` the `gco-cluster-shared-bucket` ConfigMap, which holds the bucket's name and Region | `gco storage sync cluster-shared <local-dir>` |
| Regional shared S3 bucket, one per Region | `envFrom` the `gco-regional-shared-bucket` ConfigMap | `gco storage sync regional-shared:<region> <local-dir>` |
| EFS file system | Mount the `gco-shared-storage` PersistentVolumeClaim | `gco files ls` and `gco files download` |
| FSx for Lustre (optional) | Mount the `gco-fsx-storage` PersistentVolumeClaim | `gco files ... -t fsx` |

### Write to S3 and read it back

[`examples/cluster-shared-bucket-upload-job.yaml`](../examples/cluster-shared-bucket-upload-job.yaml) writes a small JSON file to `uploads/` in the cluster-shared bucket. Submit it, check its logs, then copy the file to your machine:

```bash
gco jobs submit-sqs examples/cluster-shared-bucket-upload-job.yaml -r us-east-1
gco jobs logs cluster-shared-bucket-upload -r us-east-1
gco storage sync cluster-shared ./cluster-data --prefix uploads/
gco jobs delete cluster-shared-bucket-upload -r us-east-1 -y
```

The job got the bucket's name from the ConfigMap and its write permission from EKS Pod Identity, so the manifest contains no bucket name and no credentials. `gco storage list` shows every bucket and the alias `storage sync` takes for it.

### Write to EFS and reach the cluster

EFS outputs are read through the Kubernetes API: `gco files` starts a small helper pod that mounts the file system. The API endpoint of a GCO cluster is private by default, so two things have to be in place, and GCO handles both:

- **Permission.** Your IAM principal needs an EKS access entry. Create it once per Region with `gco stacks access -r us-east-1`.
- **A network path.** `--via-ssm auto` starts a temporary `t3.micro` bastion in the cluster's VPC, with no inbound ports, opens an SSM tunnel through it for the one command, and terminates it afterwards. That adds a minute or two to each command.

```bash
gco stacks access -r us-east-1
gco jobs submit-direct examples/efs-output-job.yaml -r us-east-1 --via-ssm auto -y
gco files download efs-output-example ./efs-results -r us-east-1 --via-ssm auto -y
cat efs-results/results.json
```

`submit-direct` applies the manifest with kubectl instead of going through GCO's API, which some workloads in [`examples/`](../examples/README.md) use. `gco cluster doctor --region us-east-1` checks each layer of cluster access when something does not connect, and `gco cluster tunnel` holds a tunnel open for your own kubectl session.

**Done when:** you have the uploaded JSON file under `./cluster-data` and `results.json` under `./efs-results`, both written by pods that no longer exist.

## Level 4: Pipelines and repeatable jobs

**You will learn:** how to chain jobs, reuse a manifest, and let GCO pick the Region.
**Cost:** CPU-only pods, as in Level 3.

```bash
gco dag run examples/pipeline-dag.yaml -r us-east-1                        # two steps that share EFS
gco templates create examples/simple-job.yaml -n hello -d "The hello job"  # save a manifest
gco templates run hello -n hello-from-template -r us-east-1                # a job from it
gco jobs submit-sqs examples/simple-job.yaml --auto-region                 # GCO picks the Region
```

- **Pipelines.** [`examples/pipeline-dag.yaml`](../examples/pipeline-dag.yaml) runs a preprocess step that writes to EFS and a train step that reads it. A step runs only after its dependencies succeed, and a failed step skips the steps that depend on it.
- **Templates.** A stored manifest can contain `{{parameter}}` placeholders with defaults (`gco templates create ... -p epochs=3`) that each run overrides (`gco templates run ... -p epochs=10`).
- **Region choice.** `--auto-region` places the job from capacity and queue signals. With more than one workload Region, add one with `gco stacks regions add us-west-2` and `gco stacks deploy-all -y`, and the same command starts choosing between them.
- **Notifications.** `gco webhooks create -u <your-https-endpoint> -e job.failed` posts job events to an endpoint you run, signed with HMAC when you pass `-s`.

**Done when:** `gco dag run` reports both steps completed, and `gco jobs list --all-regions` shows `hello-from-template`.

## Tracks

Each track picks up after Level 3. Follow the ones that match your work.

### ML engineer: train and serve models

- **Serve a model.** The [Inference Guide](INFERENCE.md) covers vLLM, SGLang and Triton, model weights from S3, autoscaling and canaries. An endpoint is deployed to every workload Region unless you name some with `-r`:

  ```bash
  gco inference deploy my-llm -i vllm/vllm-openai:v0.32.0 --gpu-count 1 -e MODEL=Qwen/Qwen2.5-1.5B-Instruct -r us-east-1
  gco inference status my-llm
  gco inference invoke my-llm -p "Explain GPU scheduling in one sentence."
  gco inference stop my-llm -y     # scale to zero and keep the configuration
  gco inference delete my-llm -y
  ```

- **Distributed training.** [`examples/kubeflow-trainjob.yaml`](../examples/kubeflow-trainjob.yaml) runs a two-node PyTorch job through the Kubeflow Trainer `TrainJob` API, sized for CPUs so it needs no GPU quota. Submit it with `gco jobs submit-sqs examples/kubeflow-trainjob.yaml -r us-east-1`, read each rank's logs with `gco jobs logs kubeflow-trainjob-example -r us-east-1 --node 1`, and delete it with `gco jobs delete kubeflow-trainjob-example -r us-east-1 -y`. [Distributed Training](DISTRIBUTED_TRAINING.md) covers GPU variants, EFA and Spot.
- **Many short GPU jobs.** For a hyperparameter search or other back-to-back GPU trials, add `--keep-warm` to the submit command. The job runs on the keep-warm pool, which keeps an empty node for a window (10 minutes by default), so the next job starts on the same node without waiting for a new instance or pulling its image again. A waiting node bills at its full price; [Keep-Warm GPU Nodes](GPU_KEEP_WARM.md) explains the window, the idle-time records and the cost.
- **Experiment tracking.** [`examples/mlflow-tracking-job.yaml`](../examples/mlflow-tracking-job.yaml) logs a run to the bundled MLflow server and reads it back. Submit it with `gco jobs submit-direct examples/mlflow-tracking-job.yaml -r us-east-1 --via-ssm auto -y`. [Monitoring](MONITORING.md) explains the MLflow and Grafana UIs and how to open them.
- **Schedulers.** Kueue (quotas and queueing) and Volcano (gang scheduling) are on by default. The [Schedulers overview](SCHEDULERS.md) helps you choose, and [`examples/`](../examples/README.md) has a manifest for each.
- **AWS accelerators.** [`examples/trainium-job.yaml`](../examples/trainium-job.yaml) and [`examples/inferentia-job.yaml`](../examples/inferentia-job.yaml) request `aws.amazon.com/neuron`, which starts a node from the Neuron pool.

**Done when:** you have sent a prompt to your own endpoint and read the logs of both ranks of a TrainJob.

### Operator: run it day to day

- **Watch the fleet.** `gco status --watch 10` keeps the whole fleet on one screen; `--fail-on-findings` makes it exit non-zero for scripts.
- **Get paged.** Every CloudWatch alarm notifies an SNS topic of its stack, and nothing is delivered until you subscribe. Subscribe your on-call channel to the monitoring stack's `AlertTopicArn` output and to each regional stack's `RegionalAlertTopicArn` output; [Maintenance](MAINTENANCE.md#monitoring-and-alerting) lists the other topics and the alarms behind each one.
- **Explain a job.** `gco jobs events <job> -r <region>` tells you why a pod waited or why its node went away, including Spot interruptions. [Troubleshooting](TROUBLESHOOTING.md) and the [Runbooks](RUNBOOKS.md) handle the common failures.
- **Track spend.** `gco costs summary` reports spend by Region from Cost Explorer, and the [Cost Monitoring guide](COST_MONITORING.md) adds per-workload cost from OpenCost.
- **Upgrade.** `gco upgrade --check` shows whether a newer release exists and what an upgrade would do. Read [Upgrading](UPGRADING.md) first, because an upgrade recreates the regional stacks.

**Done when:** an alarm topic has a confirmed subscription, and you can explain a job's scheduling from `gco jobs events`.

### Platform engineer: shape the platform

- **Configuration.** `cdk.json` is the whole configuration: Regions, features, NodePools, job admission policy and thresholds. The [Customization Guide](CUSTOMIZATION.md) explains each setting, and `gco stacks deploy-all -y` applies a change.
- **The policy jobs meet.** `gco jobs policy -r us-east-1` prints the admission policy a Region enforces, which can differ from your local `cdk.json` when the stacks came from another checkout.
- **Capacity you own.** `gco capacity find-blocks` and `gco capacity reserve` buy Capacity Blocks for ML, and `gco nodepools` creates the NodePool that runs pods on them or on an On-Demand Capacity Reservation. [SageMaker HyperPod](HYPERPOD.md) and [AWS PCS](PCS.md) attach as optional compute backends.
- **Optional features.** FSx for Lustre, Valkey, Aurora pgvector, the [Analytics Environment](ANALYTICS.md), [EKS Capabilities](EKS_CAPABILITIES.md), [GitOps with Argo CD](GITOPS.md) and [Crossplane](CROSSPLANE.md) are each one setting away.
- **How it fits together.** [Architecture](ARCHITECTURE.md) covers the global and regional stacks, networking, security layers and in-cluster TLS. [Image Mirror](IMAGE_MIRROR.md) and [Forking](FORKING.md) cover running GCO from your own registry and repository.

**Done when:** you have added a Region or changed a NodePool and deployed the change.

### Builder: put your own scheduler on top

[Manifold Bio](../README.md#in-production-at-manifold-bio) built Regatta, its own scheduler for protein design campaigns, on GCO's API and held 4,540 GPUs at once across nine Regions. The same API is open to you:

- **The API.** Every operation the CLI performs is an IAM-authenticated (SigV4) HTTP call. The [API Reference](API.md) documents every route, with OpenAPI documents in [`docs/openapi/`](openapi/README.md).
- **Clients.** [Client Examples](client-examples/README.md) submit jobs from Python with boto3, with curl, and through `aws-sigv4-proxy`. [IAM Policies](iam-policies/README.md) has least-privilege policies for callers.
- **Agents.** The [GCO MCP server](../gco_mcp/README.md) exposes the same operations as tools to AI agents, and `gco autopilot` starts Claude Code, OpenAI Codex or OpenCode on Amazon Bedrock with it already connected ([Autopilot Guide](AUTOPILOT.md)).

**Done when:** a script of yours submits a job through the API with your own credentials.

### Contributor: change GCO itself

- **Rules of the road.** [Project Tenets](../TENETS.md) rank the trade-offs, and [Contributing](../CONTRIBUTING.md) covers the dev container, the checks to run before a pull request, and the CI pipeline.
- **Why things are the way they are.** The [Architecture Decision Records](adr/README.md) record the larger decisions and their alternatives.
- **How changes are proven.** `gco examples validate --static-only` is the minimum check for any change under `examples/`. [Example Validation](EXAMPLE_VALIDATION.md) and [Live Release Validation](LIVE_RELEASE_VALIDATION.md) describe the harnesses that deploy a real topology, run every check against it, and tear it down.
- **Maintenance.** The [Maintenance Guide](MAINTENANCE.md) covers EKS version upgrades, new instance types and dependency updates.

**Done when:** your change passes the checks in Contributing, and you know which live validation it needs.

## Learn by asking

The [GCO MCP server](../gco_mcp/README.md) lets an AI assistant answer questions from GCO's own docs and source, so you can ask instead of searching. Install it with the buttons in the [README](../README.md#install-the-mcp-server), or start `gco autopilot`. Questions that fit each level:

- Level 0: *"Why does GCO reject a GPU job without a toleration?"*
- Level 1: *"What happens between `gco jobs submit-sqs` and my pod running?"*
- Level 3: *"Which buckets can a job write to, and how does it get credentials?"*
- ML track: *"How does the inference monitor turn `gco inference deploy` into pods?"*
- Platform track: *"What changes in the regional stack when I enable FSx for Lustre?"*

## Clean up

```bash
gco inference delete my-llm -y   # any endpoints you deployed
gco stacks destroy-all -y        # every stack; this stops the platform cost
```

`destroy-all` removes every stack, then sweeps the leftovers it knows about. Some resources are kept on purpose, such as retained ECR repositories; [`gco stacks destroy-all`](CLI.md#gco-stacks-destroy-all) lists what it removes. To stop paying for the workload Regions while you keep the control plane, add `--keep-control-plane`.

## At a glance

| Level | You run | Done when | Cost |
|-------|---------|-----------|------|
| 0. Explore for free | `gco examples validate --static-only`, `gco jobs check-policy --offline`, `gco capacity check` | The policy check passes, then fails without the toleration | $0 |
| 1. Deploy and run a first job | `gco stacks deploy-all -y`, `gco jobs submit-sqs examples/simple-job.yaml` | The logs say `Hello from GCO!` | About $0.77 an hour from here on |
| 2. Run a GPU job | `gco jobs submit-sqs examples/gpu-job.yaml` | The logs show `nvidia-smi` | Plus a GPU node for minutes |
| 3. Keep job outputs | `gco storage sync`, `gco files download --via-ssm auto` | Both outputs are on your machine | Plus pennies |
| 4. Pipelines and repeatable jobs | `gco dag run`, `gco templates run`, `--auto-region` | Both pipeline steps complete | Plus pennies |
| Tracks | The track's commands | The track's check | Depends on the track |

---

**Next steps:** [Core Concepts](CONCEPTS.md) explains the ideas behind each level, the [CLI Reference](CLI.md) lists every command, and the [Documentation Index](README.md) has every guide.
