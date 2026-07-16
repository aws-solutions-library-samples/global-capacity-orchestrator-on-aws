# CloudFormation Drift Detection Lambda

This scheduled Lambda detects CloudFormation stack drift and publishes an SNS
alert when detection fails or managed resources differ from the template.

## Table of Contents

- [Flow](#flow)
- [Environment](#environment)
- [Alert Behavior](#alert-behavior)
- [Operational Notes](#operational-notes)
- [Source](#source)

## Flow

1. Start `DetectStackDrift` for the configured stack.
2. Poll `DescribeStackDriftDetectionStatus` to a bounded terminal state.
3. Return without notification when the stack is `IN_SYNC`.
4. List drifted resources and publish a compact JSON SNS notification otherwise.

## Environment

| Variable | Required | Purpose |
|---|---:|---|
| `STACK_NAME` | Yes | CloudFormation stack to inspect |
| `SNS_TOPIC_ARN` | Yes | Destination for failure and drift alerts |
| `REGION` | No | AWS region override; falls back to `AWS_REGION` |
| `POLL_INTERVAL_SECONDS` | No | Poll delay; default `10` |
| `POLL_MAX_ATTEMPTS` | No | Poll bound; default `60` |

## Alert Behavior

Detection failures and detected drift publish JSON summaries. Drift alerts list
logical ID, physical ID, resource type, and drift status. SNS subjects are
truncated defensively to the service's 100-character limit.

## Operational Notes

The default polling window is ten minutes, below Lambda's maximum runtime.
EventBridge payload contents are ignored; one deployed function is bound to one
stack through environment configuration.

## Source

- [`handler.py`](handler.py) implements detection, polling, and notification.
- [Generated control-flow diagram](../../diagrams/code_diagrams/lambda/drift-detection/handler.lambda_handler.html)
