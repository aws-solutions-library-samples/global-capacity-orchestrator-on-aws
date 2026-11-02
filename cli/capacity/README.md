# Capacity

GPU capacity checking, region recommendation, and AI-powered advisory. This package provides the intelligence behind `gco capacity` commands.

## Table of Contents

- [Architecture](#architecture)
- [Files](#files)
- [How Region Recommendation Works](#how-region-recommendation-works)
- [Adding a New Signal](#adding-a-new-signal)

## Architecture

Three layers, each building on the previous:

```text
CapacityChecker (single-region AWS queries)
    ↓
MultiRegionCapacityChecker (cross-region aggregation + weighted scoring)
    ↓
BedrockCapacityAdvisor (AI-powered natural language recommendations)
```

## Files

| File | Description |
|------|-------------|
| `checker.py` | Single-region capacity checker. Queries [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html) spot placement scores, spot price history, on-demand availability, instance type specs, and Capacity Block offerings. Also manages reserved capacity end to end: On-Demand Capacity Reservations (list/find/create/cancel, with parallel multi-region search and On-Demand pricing) and [Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html) (search/purchase). |
| `blocks.py` | Pure primitives shared by the reserved-capacity flows: Capacity Block duration math, instance-type alias normalization, per-hour / per-GPU-hour pricing (offerings and ODCRs), and offering/reservation de-dup, sort, and rank helpers. |
| `training_plans.py` | [SageMaker Training Plan](https://docs.aws.amazon.com/sagemaker/latest/dg/reserve-capacity-with-training-plans.html) lookups behind `gco capacity find-training-plans` and `training-plans`: `search_offerings()` fans `SearchTrainingPlanOfferings` out across Regions and ranks the offerings cheapest per instance-hour first; `list_plans()` pages `ListTrainingPlans` with the account's reserved capacity. Instance types take the Capacity Block aliases and gain the `ml.` prefix; one Region's error is recorded while the others answer, and only when every Region refuses does the lookup raise. Read-only: purchasing a plan stays a console or `aws sagemaker create-training-plan` action. |
| `multi_region.py` | Cross-region aggregation. Queries each deployed region in turn, computes weighted scores (spot placement, spot-to-on-demand price ratio, queue depth, GPU utilization, running jobs, Capacity Block trend), and ranks regions. With `backend="hyperpod"` or `"pcs"` it ranks only the Regions running that backend, on the backend's own capacity (below), and raises `NoEligibleRegionError` when none does. |
| `backends.py` | Per-Region [SageMaker HyperPod](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html) and [AWS PCS](https://docs.aws.amazon.com/pcs/latest/userguide/what-is-service.html) capacity as the operator's credentials can see it, without a node inventory: for HyperPod the cluster the stack's `HyperpodClusterName` output names (`DescribeCluster`: status, each instance group's type and current/target counts) with the GPU count of each `ml.*` type taken from EC2's description of its EC2 twin; for PCS the cluster behind `PcsClusterId` (`GetCluster`), its compute node groups' scaling bounds (`ListComputeNodeGroups`/`GetComputeNodeGroup`, the login group excluded), the instances PCS has launched for them (the `aws:pcs:compute-node-group-id` tag) and Slurm's pending/running jobs and idle nodes through the regional API's `/api/v1/slurm/status?backend=pcs`. Each source fails on its own as a named missing signal. |
| `placement.py` | What a job manifest asks of a Region before `gco jobs submit-sqs --auto-region` picks one: whether it is routed to HyperPod (`gco.job_admission.hyperpod_routing`, the same definition the manifest processor uses) and the `nvidia.com/gpu` it requests across its pod specs, replicas weighted. |
| `advisor.py` | AI-powered recommendations via Amazon Bedrock. Gathers capacity data from all regions and sends it to an LLM for analysis with workload-specific context. |
| `models.py` | Data models — `CapacityEstimate`, `SpotPriceInfo`, `InstanceTypeInfo`, and `instance_type_info_from_ec2()` which maps a `DescribeInstanceTypes` record onto it. No checked-in instance specification table: characteristics are always resolved live. |
| `__init__.py` | Package exports and `compute_weighted_score()` utility. |

## How Region Recommendation Works

When you run `gco capacity recommend-region -i g5.xlarge`:

1. `MultiRegionCapacityChecker` discovers the deployed regional stacks and reads each region's cluster telemetry, one region after another: queue depth and running jobs from the job queue (SQS), GPU utilization from Container Insights
2. For the requested instance type it then asks `CapacityChecker`, region by region, for the spot placement score, the spot-to-on-demand price ratio, and the Capacity Block offering trend
3. Each signal is normalized to 0–1 (0 = best) and multiplied by a weight
4. Regions are ranked by composite score (lower = better); regions missing queue or GPU telemetry rank behind regions that report it
5. The top region is returned with a breakdown of why it scored best

`--backend hyperpod` and `--backend pcs` replace steps 2–4 with the backend's own ranking (`backends.py` reads the signals; `multi_region.py` scores them, lower is better):

- **HyperPod**: only Regions whose stack publishes a HyperPod cluster qualify (with `--instance-type`, only those with an instance group of that type). 1000 points for each of: a cluster that is not `InService` with every group at its target count, a GPU pool known to be smaller than `--gpu-count`, a cluster SageMaker could not describe, a missing job-queue signal; then `queue depth × 10 + running jobs × 5` for the Region's shared job queue. Ties go to the larger GPU pool, then more nodes, then the Region name. Busy GPUs on HyperPod nodes are not visible from outside the cluster (the CloudWatch agent does not run on them and Kueue's usage has no API route), so the pool size and the Region's queue stand in for load.
- **PCS**: only Regions whose stack publishes a PCS cluster qualify. 1000 points for a cluster that is not `ACTIVE` and for each source that could not be read, 500 when the compute node groups are at their maximum with no idle Slurm node, then `pending Slurm jobs × 10 + running Slurm jobs × 5`. Ties go to the larger headroom (instances the node groups may still launch), then more idle nodes, then the Region name. The Region's EKS job queue plays no part.

`gco jobs submit-sqs --auto-region` reads the manifest first (`placement.py`) and uses the HyperPod ranking for a manifest routed to HyperPod; `gco slurm submit --auto-region` uses the PCS ranking. Both fail when no Region runs the backend rather than queueing work where it could never start.

## Adding a New Signal

1. Add the data collection to `checker.py` (e.g. a new AWS API call)
2. Include it in the `CapacityEstimate` model in `models.py`
3. Add normalization and weighting in `multi_region.py`'s `compute_weighted_score()`
4. Update the [Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) prompt in `advisor.py` if the signal should influence AI recommendations
