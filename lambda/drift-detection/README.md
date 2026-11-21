# CloudFormation Drift Detection Lambda

This scheduled [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) detects [CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) stack drift and publishes an [SNS](https://docs.aws.amazon.com/sns/latest/dg/welcome.html)
alert when detection fails or managed resources differ from the template.

## Table of Contents

- [Flow](#flow)
- [Environment](#environment)
- [Alert Behavior](#alert-behavior)
- [IAM Permissions](#iam-permissions)
- [Operational Notes](#operational-notes)
- [Source](#source)

## Flow

1. Start `DetectStackDrift` for the configured stack.
2. Poll `DescribeStackDriftDetectionStatus` to a bounded terminal state.
3. If the detection is still running when polling gives up, publish a
   "did not finish" notice and stop: the stack's drift status is not final yet.
4. After a completed detection, return without notification unless the stack
   is `DRIFTED` (`IN_SYNC`, `NOT_CHECKED` and `UNKNOWN` are not drift). A
   failed detection (`DETECTION_FAILED`: it failed for at least one resource,
   and the results of the others are available) is always reported.
5. List the resource drifts and publish a compact JSON SNS notification.

## Environment

| Variable | Required | Purpose |
|---|---:|---|
| `STACK_NAME` | Yes | CloudFormation stack to inspect |
| `SNS_TOPIC_ARN` | Yes | Destination for failure and drift alerts |
| `REGION` | No | AWS region override; falls back to `AWS_REGION` |
| `POLL_INTERVAL_SECONDS` | No | Poll delay; default `10` |
| `POLL_MAX_ATTEMPTS` | No | Poll bound; default `60` |

## Alert Behavior

Detection failures, detections that did not finish within the polling window,
and detected drift publish JSON summaries, each under its own subject. Drift
and failure alerts list the `MODIFIED` and `DELETED` resources with logical
ID, physical ID, resource type, and drift status (`drifted_resources`), the
resources detection could not check, with CloudFormation's
`DriftStatusReason` (`failed_resources`), and apart (`not_checked_resources`)
the resources whose type it does not support (`UNSUPPORTED`). A failed
detection that found drift among the resources it did check, such as a
regional stack whose Aurora secret it cannot read, is published under the
drift subject with the failure's `reason`; without drift it is published as
a failure. SNS subjects are truncated defensively to the service's
100-character limit.

## IAM Permissions

CloudFormation runs drift detection with the caller's credentials, so the
function's role carries what the
[drift detection considerations](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-stack-drift.html#drift-considerations)
list:

- `cloudformation:DetectStackDrift`, `DetectStackResourceDrift`,
  `DescribeStackResourceDrifts`, `DescribeStackResource` and
  `DescribeStackResources` on its own stack's ARN, and
  `DescribeStackDriftDetectionStatus` and `BatchDescribeTypeConfigurations`
  (no resource-level permissions) on `*`.
- A separate `DriftDetectionReadPolicy` with the read permissions of every
  resource type in the stack, from each type's CloudFormation registry read
  handler (`gco/stacks/drift_read_permissions.py`). S3, SSM parameter and
  Lambda reads name the stack's own buckets, parameters and functions; the
  other Describe/Get/List calls are on `*`.
- `sns:Publish` to the alert topic and use of its KMS key.

No secret value is readable: with `aurora_pgvector` enabled, CloudFormation
cannot compare the Aurora secret and its target attachment (their read
handlers call `secretsmanager:GetSecretValue`), so the run ends
`DETECTION_FAILED` while every other resource is still compared. Grant the
role `secretsmanager:GetSecretValue` on that secret if you want them checked.

## Operational Notes

The default polling window is ten minutes, below Lambda's maximum runtime.
[EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-what-is.html) payload contents are ignored; one deployed function is bound to one
stack through environment configuration.

## Source

- [`handler.py`](handler.py) implements detection, polling, and notification.
- [Generated control-flow diagram](../../diagrams/code_diagrams/lambda/drift-detection/handler.lambda_handler.html)
