# Traffic Dial Controller Lambda

Scheduled, health-driven convergence of [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html)
traffic dials. An **opt-in add-on to the GCO global stack** (commercial `aws`
partition only) that reads each workload region's `ClusterHealthy` signal and
moves that region's endpoint-group `TrafficDialPercentage` toward its observed
health, so a region whose GPU pools are degraded sheds *new* connections
gradually instead of waiting for a binary health-check failover. Existing
connections are never terminated by a dial change.

## Table of Contents

- [How it fits in](#how-it-fits-in)
- [Configuration](#configuration)
- [Control flow](#control-flow)
- [Safety rails](#safety-rails)
- [Outputs](#outputs)
- [IAM permissions](#iam-permissions)
- [Packaging](#packaging)

## How it fits in

`GCOGlobalStack._create_traffic_dial_controller()` (in
`gco/stacks/global_stack.py`) creates the [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html),
an [EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-what-is.html)
schedule (`TrafficDialSchedule`) and a dead-letter queue when
`global_accelerator.traffic_dial.enabled` is true. It is deliberately a single
writer that lives next to the accelerator it manages: regional writers would
need leader election and new cross-region IAM to mutate one global resource.

The health signal is the per-cluster `ClusterHealthy` metric the
health-monitor service already publishes to the `GCO/HealthMonitor` namespace
in every workload region (`gco/services/metrics_publisher.py`);
`tests/test_health_metric_contract.py` pins the two ends of that contract.
`gco capacity traffic-dial show|set|clear` (`cli/capacity/traffic_dial.py`) is
the operator surface.

## Configuration

All knobs live under `global_accelerator.traffic_dial` in `cdk.json` and reach
the handler as environment variables:

| `cdk.json` key | Env var | Default | Description |
|-----|-----|---------|-------------|
| `enabled` | — | `false` | Deploy the controller (opt-in) |
| `mode` | `MODE` | `monitor` | `monitor` computes and publishes decisions but never writes; `enforce` also applies them via `UpdateEndpointGroup`. Only `enforce` mode is granted that IAM action |
| `interval_minutes` | — | `5` | Run cadence (1-1440) |
| `lookback_minutes` | `LOOKBACK_MINUTES` | `15` | Health window averaged per region (1-1440) |
| `min_dial_percentage` | `MIN_DIAL_PERCENTAGE` | `10` | Floor a degraded region can be dialed down to (0-100) |
| `max_step_percentage` | `MAX_STEP_PERCENTAGE` | `20` | Largest dial change one run may apply, in either direction (1-100) |
| `full_health_percentage` | `FULL_HEALTH_PERCENTAGE` | `95` | Healthy percent at or above which a region is restored toward 100 (1-100) |

The stack also passes `LISTENER_ARN` (the accelerator listener whose endpoint
groups are managed), `PROJECT_NAME` (prefix for SSM paths and cluster names)
and `REGIONS` (comma-separated workload regions). Run in `monitor` mode first
and watch the `GCO/TrafficDial` metrics before promoting to `enforce`. See
[Customization Guide → Traffic Dial Controller](../../docs/CUSTOMIZATION.md#traffic-dial-controller).

## Control flow

Each scheduled run is phased:

0. **Accelerator readiness** — the accelerator must be `DEPLOYED`; a cycle
   that lands mid-deployment is skipped entirely, because endpoint-group
   updates submitted while a previous change is converging extend the window
   in which the served configuration is unknown.
1. **Current state** — `ListEndpointGroups` on the listener yields each
   region's endpoint-group ARN and currently served dial.
2. **Manual overrides** — `gco capacity traffic-dial set` records an override
   parameter per region; the controller never touches an overridden region
   until `gco capacity traffic-dial clear` removes it.
3. **Per-region decision** — the region's healthy fraction over the lookback
   window maps to a target dial: at or above `FULL_HEALTH_PERCENTAGE` the
   target is 100, below it `max(MIN_DIAL_PERCENTAGE, round(healthy_percent))`.
   The applied change per run is bounded by `MAX_STEP_PERCENTAGE` in both
   directions (gradual drain, gradual restore).
4. **Last-healthy-region guard** — if every non-overridden decision lands
   below 100, the region with the best health signal is forced back to 100,
   bypassing the step limit (dialing *up* is safe).
5. **Enforcement** — in `enforce` mode changed dials are applied via
   `UpdateEndpointGroup` carrying *only* `TrafficDialPercentage`.
6. **Publication** — every decision is emitted to CloudWatch and the full run
   is stored in SSM for `gco capacity traffic-dial show`.

The generated [flowchart](../../diagrams/code_diagrams/README.md#lambda) for
`lambda_handler` shows the same phases as a diagram.

## Safety rails

- Missing telemetry holds the current dial: an absent signal must never look
  like ideal health, and equally must never trigger a drain.
- The last-healthy-region guard keeps one fully dialed region at all times.
  The dial gates only first-choice traffic and redirects the remainder to the
  next-closest group, and Global Accelerator does not document the resulting
  distribution once *every* group sits below 100, so the guard keeps a
  deterministic absorber for redirected traffic.
- `UpdateEndpointGroup` omits `EndpointConfigurations` on purpose: the API
  patches omitted fields, and passing an empty list would detach the region's
  ALB.
- Manual overrides always win, and the controller is a no-op in `monitor`
  mode.

## Outputs

- CloudWatch metrics in the `GCO/TrafficDial` namespace, one decision per
  region per run.
- The `/{project}/traffic-dial/state` SSM parameter with the full last run;
  overrides live under the same `/{project}/traffic-dial` tree. These are
  runtime parameters written outside CloudFormation; a fully successful
  `gco stacks destroy-all` purges the tree so a stale override cannot pin a
  region's dial in the account's next deployment.

## IAM permissions

Read-only against Global Accelerator (`DescribeAccelerator`,
`ListEndpointGroups`), CloudWatch `GetMetricData`/`PutMetricData`, and SSM
`GetParameter`/`GetParametersByPath`/`PutParameter` under the project's
`traffic-dial` tree. `globalaccelerator:UpdateEndpointGroup` is granted only
when `mode` is `enforce`. The Global Accelerator control plane is homed in
`us-west-2` in the commercial partition (same convention as
`lambda/ga-registration`).

## Packaging

Plain (zip) Lambda packaged via `Code.from_asset("lambda/traffic-dial-controller")`.
The handler is self-contained — boto3 and the standard library only, both
provided by the Lambda runtime — and does not import the CLI or `gco`
packages, matching the convention used by the other GCO Lambdas.
