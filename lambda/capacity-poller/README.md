# Capacity Poller Lambda

The capacity poller is the data-collection half of the **Historical Capacity
Surface** - an **optional add-on to the GCO global stack** (not a separate
stack). It is **enabled by default** (`historical.enabled: true` in `cdk.json`);
set that flag to `false` to opt out.

On a fixed EventBridge schedule (every 15 minutes by default) it snapshots
capacity signals for a watched set of instance types across the enabled regions
and writes them to the `{project}-capacity-history` DynamoDB table, which the
`gco capacity history` / `gco capacity predict` commands and the Bedrock advisor
read back.

## Table of Contents

- [How it fits in](#how-it-fits-in)
- [Configuration](#configuration)
- [What it records](#what-it-records)
- [IAM permissions](#iam-permissions)
- [Cost model](#cost-model)
- [Local testing](#local-testing)

## How it fits in

This Lambda, its DynamoDB table, the EventBridge schedule, and a dead-letter
queue are created by `GCOGlobalStack._create_capacity_poller()` (in
`gco/stacks/global_stack.py`) when `historical.enabled` is true. Folding it into
the global stack rather than a standalone stack lets it reuse the global stack's
DynamoDB encryption / point-in-time-recovery conventions and keeps the
deployment surface small. The handler itself is self-contained (boto3 + stdlib
only) and is packaged via `Code.from_asset("lambda/capacity-poller")`.

## Configuration

All knobs live under the `historical` block in `cdk.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Deploy the add-on (table + poller + schedule). |
| `retention_days` | `90` | DynamoDB TTL window for snapshots. |
| `poll_interval_minutes` | `15` | EventBridge schedule cadence. |
| `watch_instance_types` | 9 GPU/Trn/Inf types | Instance types to snapshot. |
| `enabled_regions` | `[]` (all deployed) | Regions to poll. |

The Lambda reads these via environment variables `CAPACITY_HISTORY_TABLE_NAME`,
`WATCH_INSTANCE_TYPES`, `ENABLED_REGIONS`, and `CAPACITY_HISTORY_RETENTION_DAYS`.

## What it records

Per `(instance_type, region)` per run: `spot_score`, `spot_price`, `az_count`,
`capacity_blocks_available`, and `capacity_blocks_total`. (`queue_depth` is a
cluster-level signal this EC2-only poller does not collect; the store treats a
missing metric as absent rather than zero.)

## IAM permissions

A least-privilege role: CloudWatch Logs (basic execution), `dynamodb:PutItem` /
`dynamodb:BatchWriteItem` on the history table only, and the read-only EC2
capacity APIs `DescribeSpotPriceHistory`, `GetSpotPlacementScores`,
`DescribeCapacityBlockOfferings`, `DescribeCapacityReservations`, and
`DescribeAvailabilityZones`.

## Cost model

The poller is intentionally cheap. The dominant inputs are the schedule cadence
and the number of `watch_instance_types` x `enabled_regions` pairs polled each
run.

Worked example - default cadence (15 min, ~2,920 runs/month), 9 instance types
x 2 regions = 18 pairs/run:

| Component | Basis | Est. monthly cost |
|-----------|-------|-------------------|
| Lambda compute | ~2,920 runs x ~30 s x 256 MB ~= 21,900 GB-s | $0 within the 400,000 GB-s free tier; ~$0.37 beyond it |
| Lambda requests | ~2,920 requests | < $0.01 |
| DynamoDB writes | ~52,600 on-demand writes (<= 1 KB) | ~$0.07 |
| DynamoDB storage | ~40 MB steady state (90-day TTL) + PITR | < $0.01 |
| EC2 describe/score APIs | DescribeSpotPriceHistory, GetSpotPlacementScores, etc. | $0 (no per-call charge) |
| EventBridge schedule | scheduled rule on the default bus | $0 |
| CloudWatch Logs | a few MB/month | < $0.05 (mostly free tier) |
| SQS DLQ | only on delivery failure | ~$0 |

**Total: effectively $0/month within free-tier allowances, and well under
$1/month otherwise.** Cost scales roughly linearly with `watch_instance_types`
x `enabled_regions` and inversely with `poll_interval_minutes`. Even at 9 types
x 5 regions on the default 15-minute cadence, DynamoDB writes are ~$0.15/month
and Lambda compute typically stays within the free tier.

Figures use us-east-1 on-demand pricing and are estimates; validate against AWS
Pricing / Cost Explorer for your account and regions.

## Local testing

The handler is self-contained (boto3 + stdlib). Unit tests live in
`tests/test_capacity_poller_handler.py` (mocked EC2 + DynamoDB). To exercise it
against a live table, set the environment variables above and call
`lambda_handler({}, None)`.
