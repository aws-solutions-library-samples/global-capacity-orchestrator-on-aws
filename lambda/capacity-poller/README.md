# Capacity Poller Lambda

The capacity poller is the data-collection half of the **Historical Capacity
Surface** - an **optional add-on to the GCO global stack** (not a separate
stack). It is **enabled by default** (`historical.enabled: true` in `cdk.json`);
set that flag to `false` to opt out.

On a fixed [EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-what-is.html) schedule (every 15 minutes by default) it snapshots
capacity signals for a watched set of instance types across the enabled regions
and writes them to the `{project}-capacity-history` [DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html) table, which the
`gco capacity history` / `gco capacity predict` commands and the [Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) advisor
read back.

## Table of Contents

- [How it fits in](#how-it-fits-in)
- [Configuration](#configuration)
- [What it records](#what-it-records)
- [IAM permissions](#iam-permissions)
- [Cost model](#cost-model)
- [Local testing](#local-testing)

## How it fits in

This [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html), its DynamoDB table, the EventBridge schedule, and a dead-letter
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
| `capacity_block_duration_hours` | `24` | Short [Capacity Block](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html) probe duration (soonest-available, 1-day block). |
| `capacity_block_long_duration_hours` | `1512` | Long Capacity Block probe duration in hours (63 days). Tracks extended-term block availability; set `0` to disable the long probe. |
| `watch_instance_types` | 78 NVIDIA GPU / AWS Neuron types | Instance types to snapshot. Must exactly match `gco/config/accelerator_catalog.json`; offline validation guards both `cdk.json` and the `ConfigLoader` fallback. |
| `enabled_regions` | `[]` (all deployed) | Regions to poll. |

The Lambda reads these via environment variables `CAPACITY_HISTORY_TABLE_NAME`,
`WATCH_INSTANCE_TYPES`, `ENABLED_REGIONS`, `CAPACITY_HISTORY_RETENTION_DAYS`,
`CAPACITY_BLOCK_DURATION_HOURS`, and `CAPACITY_BLOCK_LONG_DURATION_HOURS`. AWS
accepts Capacity Block durations in 1-day increments up to 14 days, then 7-day
increments up to 182 days, so the long duration default of 1512h (63 days = 9
weeks) is a valid block length.

## What it records

Per `(instance_type, region)` per run: `spot_score`, `spot_price`, `az_count`,
`capacity_blocks_available` / `capacity_blocks_total` (the short, soonest-available
block tier), and `capacity_blocks_long_available` / `capacity_blocks_long_total`
(the long-duration tier — e.g. a 63-day block — so history and alerting can tell
whether *extended-term* capacity exists, not just the soonest 1-day block).
(`queue_depth` is a cluster-level signal this EC2-only poller does not collect;
the store treats a missing metric as absent rather than zero. The long tier is
likewise omitted when `capacity_block_long_duration_hours` is `0`.)

## IAM permissions

A least-privilege role: [CloudWatch Logs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/WhatIsCloudWatchLogs.html) (basic execution), `dynamodb:PutItem` /
`dynamodb:BatchWriteItem` on the history table only, and the read-only [EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html)
capacity APIs `DescribeSpotPriceHistory`, `GetSpotPlacementScores`,
`DescribeCapacityBlockOfferings`, `DescribeCapacityReservations`, and
`DescribeAvailabilityZones`.

## Cost model

The poller is intentionally cheap. The dominant inputs are the schedule cadence
and the number of `watch_instance_types` x `enabled_regions` pairs polled each
run.

Worked example - default cadence (15 min, ~2,920 runs/month), 78 instance types
x 1 region (the default `regional` deployment) = 78 pairs/run and 227,760
pair snapshots:

| Component | Basis | Est. monthly cost |
|-----------|-------|-------------------|
| Lambda compute | ~2,920 runs x ~125 s x 256 MB ~= 91,000 GB-s | $0 within the 400,000 GB-s free tier (~23% of it); ~$1.52 beyond it |
| Lambda requests | ~2,920 requests | < $0.01 |
| DynamoDB writes | ~227,760 on-demand writes (<= 1 KB) | ~$0.28 |
| DynamoDB storage | ~150 MB steady state (90-day TTL) + PITR | < $0.05 |
| EC2 describe/score APIs | DescribeSpotPriceHistory, GetSpotPlacementScores, etc. | $0 (no per-call charge) |
| EventBridge schedule | scheduled rule on the default bus | $0 |
| [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html) Logs | a few MB/month | < $0.05 (mostly free tier) |
| [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) DLQ | only on delivery failure | ~$0 |

**Total: effectively $0/month within free-tier allowances, and well under
$1/month otherwise.** Cost scales roughly linearly with `watch_instance_types`
x `enabled_regions` and inversely with `poll_interval_minutes`. Even at 78 types
x 2 regions = 156 pairs/run, Lambda compute (~182,000 GB-s) stays within the
free tier and 455,520 DynamoDB writes are about $0.57/month.

Figures use us-east-1 on-demand pricing and are estimates; validate against AWS
Pricing / [Cost Explorer](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html) for your account and regions.

## Local testing

The handler is self-contained (boto3 + stdlib). Unit tests live in
`tests/test_capacity_poller_handler.py` (mocked EC2 + DynamoDB). To exercise it
against a live table, set the environment variables above and call
`lambda_handler({}, None)`.
