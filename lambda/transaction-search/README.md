# Transaction Search

[CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom-resource handler that switches on [CloudWatch Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search.html) in a regional stack's Region.

GCO's API services (health-monitor, manifest-processor, inference-proxy, cost-monitor) export OpenTelemetry spans straight to the [X-Ray OTLP endpoint](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html) (`https://xray.<region>.amazonaws.com/v1/traces`), which only accepts spans once Transaction Search is on. Transaction Search is an account-level setting, configured per Region and shared with every other traced workload in the account, so this resource only ever adds: it never turns Transaction Search off and never removes the policy it writes.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [Packaging](#packaging)
- [CloudFormation Properties](#cloudformation-properties)
- [Output](#output)
- [IAM Permissions](#iam-permissions)
- [Dependencies](#dependencies)

## Trigger

CloudFormation Custom Resource (`TransactionSearch`) behind a [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) `cr.Provider` with only an `onEvent` handler (`handler.lambda_handler`). The regional stack creates it only while both `tracing.enabled` and `tracing.enable_transaction_search` are true in `cdk.json`.

## How It Works

### Create/Update

1. Calls `xray:GetTraceSegmentDestination`.
2. When the destination is already `CloudWatchLogs` (status `ACTIVE`, or `PENDING` while a switch someone else started completes), changes nothing.
3. Otherwise writes the CloudWatch Logs resource policy `gco-transaction-search-xray-access` (the policy from the [AWS enablement guide](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search-getting-started.html): principal `xray.amazonaws.com`, `logs:PutLogEvents` on the `aws/spans` and `/aws/application-signals/data` log groups of this Region, conditioned on this account's X-Ray as `aws:SourceArn` and `aws:SourceAccount`), then calls `xray:UpdateTraceSegmentDestination` with `CloudWatchLogs`.

X-Ray takes a few minutes to report the new destination as `ACTIVE`; spans become searchable in the `aws/spans` log group after that. The resource only re-runs when its properties change, so re-enable Transaction Search in the console if it is switched off out of band.

### Delete

No-op. Other workloads in the account may rely on Transaction Search, so GCO leaves it and the policy in place. Turn it off in the CloudWatch console once nothing uses it.

### Failures

Any AWS error fails the resource, and the stack operation with it, with a message naming the `tracing.enable_transaction_search` opt-out. Set it to `false` when your organization manages Transaction Search itself, or in partitions where it is unavailable.

CloudFormation's native `AWS::XRay::TransactionSearchConfig` is not used because it can only be created while Transaction Search is off, and it would tie a shared account-level setting to one stack's lifecycle.

## Packaging

Plain (zip) Python Lambda deployed with `lambda_.Code.from_asset("lambda/transaction-search")`. It runs outside the VPC because it only calls public AWS APIs, like the GA deregistration guard.

## CloudFormation Properties

| Property | Required | Description |
|----------|----------|-------------|
| `Region` | Yes | Region whose Transaction Search setting is managed (the stack's Region) |
| `AccountId` | Yes | Account that owns the X-Ray data, used in the resource policy |
| `Partition` | Yes | AWS partition, used in the resource policy ARNs |
| `ProjectName` | No | Project prefix for the physical resource ID (default `gco`) |

## Output

`Data` attributes (all strings):

| Attribute | Description |
|-----------|-------------|
| `Destination` | Trace segment destination after the call (`CloudWatchLogs`) |
| `Status` | `ACTIVE` or `PENDING` |
| `Changed` | `true` when this invocation switched the destination, `false` when it was already on |

## IAM Permissions

The execution role holds the permissions AWS lists as prerequisites for the principal that enables Transaction Search, minus the indexing-rule APIs GCO does not call. The log-group, Application Signals, service-linked-role and CloudTrail-channel grants are part of that list, so they are granted even though the handler itself only calls the X-Ray and CloudWatch Logs APIs.

- `xray:GetTraceSegmentDestination`, `xray:UpdateTraceSegmentDestination`, `logs:PutResourcePolicy`, `logs:DescribeResourcePolicies`, `application-signals:StartDiscovery` on `*` (no resource-level scoping)
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutRetentionPolicy` on the `aws/spans` and `/aws/application-signals/data` log groups in this Region
- `iam:CreateServiceLinkedRole` (condition `iam:AWSServiceName` = `application-signals.cloudwatch.amazonaws.com`) and `iam:GetRole` on the `AWSServiceRoleForCloudWatchApplicationSignals` service-linked role
- `cloudtrail:CreateServiceLinkedChannel` on `channel/aws-service-channel/application-signals/*` in this Region
- X-Ray write access for the function's own active tracing

## Dependencies

- `boto3` / `botocore` (AWS Lambda Python runtime built-in)
