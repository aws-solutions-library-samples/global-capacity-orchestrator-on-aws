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
| `multi_region.py` | Cross-region aggregation. Queries each deployed region in turn, computes weighted scores (spot placement, spot-to-on-demand price ratio, queue depth, GPU utilization, running jobs, Capacity Block trend), and ranks regions. |
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

## Adding a New Signal

1. Add the data collection to `checker.py` (e.g. a new AWS API call)
2. Include it in the `CapacityEstimate` model in `models.py`
3. Add normalization and weighting in `multi_region.py`'s `compute_weighted_score()`
4. Update the [Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) prompt in `advisor.py` if the signal should influence AI recommendations
