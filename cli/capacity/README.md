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
| `checker.py` | Single-region capacity checker. Queries EC2 spot placement scores, spot price history, on-demand availability, instance type specs, and Capacity Block offerings. Also manages reserved capacity end to end: On-Demand Capacity Reservations (list/find/create/cancel, with parallel multi-region search and On-Demand pricing) and Capacity Blocks (search/purchase). |
| `blocks.py` | Pure primitives shared by the reserved-capacity flows: Capacity Block duration math, instance-type alias normalization, per-hour / per-GPU-hour pricing (offerings and ODCRs), and offering/reservation de-dup, sort, and rank helpers. |
| `multi_region.py` | Cross-region aggregation. Queries all deployed regions in parallel, computes weighted scores (spot score, price trend, queue depth, GPU utilization), and ranks regions. |
| `advisor.py` | AI-powered recommendations via Amazon Bedrock. Gathers capacity data from all regions and sends it to an LLM for analysis with workload-specific context. |
| `models.py` | Data models — `CapacityEstimate`, `SpotPriceInfo`, `InstanceTypeInfo`, `GPU_INSTANCE_SPECS` lookup table. |
| `__init__.py` | Package exports and `compute_weighted_score()` utility. |

## How Region Recommendation Works

When you run `gco capacity recommend-region -i g5.xlarge`:

1. `MultiRegionCapacityChecker` queries all regions in parallel via `CapacityChecker`
2. For each region, it collects: spot placement score, current spot price, price trend (7-day), queue depth, running GPU jobs, cluster health
3. Each signal is normalized to 0–1 and multiplied by a weight
4. Regions are ranked by composite score (higher = better)
5. The top region is returned with a breakdown of why it scored highest

## Adding a New Signal

1. Add the data collection to `checker.py` (e.g. a new AWS API call)
2. Include it in the `CapacityEstimate` model in `models.py`
3. Add normalization and weighting in `multi_region.py`'s `compute_weighted_score()`
4. Update the Bedrock prompt in `advisor.py` if the signal should influence AI recommendations
