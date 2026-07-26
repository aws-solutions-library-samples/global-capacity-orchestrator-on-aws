# GCO Infrastructure Diagrams

This directory contains tools and auto-generated architecture diagrams for the GCO infrastructure.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Generated Diagrams](#generated-diagrams)
- [Stack Overview](#stack-overview)
- [Requirements](#requirements)
- [Customization](#customization)

## Prerequisites

### Graphviz Installation

The diagram generator uses [cdk-dia](https://github.com/pistazie/cdk-dia),
which requires Graphviz to be installed globally for PNG output.

**macOS (Homebrew):**

```bash
brew install graphviz
```

**Ubuntu/Debian:**

```bash
sudo apt-get install graphviz
```

**Amazon Linux / RHEL / CentOS:**

```bash
sudo yum install graphviz
```

**Windows:**
Download from <https://graphviz.org/download/> and add to PATH.

### Node.js

Run ``npm ci --ignore-scripts --no-audit --no-fund`` at the repository root.
The exact ``cdk-dia`` release and its transitive graph are pinned in
``package.json`` / ``package-lock.json``; the generator executes that local
binary. No global or on-demand npm install is used.

## Quick Start

```bash
# Generate all diagrams
python diagrams/infra_diagrams/generate.py

# Generate specific stack diagram
python diagrams/infra_diagrams/generate.py --stack global
python diagrams/infra_diagrams/generate.py --stack api-gateway
python diagrams/infra_diagrams/generate.py --stack regional
python diagrams/infra_diagrams/generate.py --stack regional-api
python diagrams/infra_diagrams/generate.py --stack monitoring
python diagrams/infra_diagrams/generate.py --stack analytics
```

## Generated Diagrams

After running the generator, diagrams are saved to `diagrams/infra_diagrams/`:

| Diagram | Description |
|---------|-------------|
| `global-stack.png` | [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html) and endpoint groups |
| `api-gateway-stack.png` | Global [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html), HMAC proxy/secret rotation, [SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html) aggregator, and backend TLS certificate manager |
| `regional-stack.png` | [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster, [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html), [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html), [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html), and services |
| `regional-api-stack.png` | Always-deployed aggregation bridge with a [VPC](https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html) [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html); direct caller access is a separate policy opt-in. The regional stack is synthesized alongside it (its VPC construct is a constructor input), but the diagram is scoped to the regional-api stack via `--include`. |
| `monitoring-stack.png` | [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html) dashboards, alarms, and SNS. The full app is synthesized so the monitoring stack can read attributes from the other stacks, but the diagram is scoped to the monitoring stack via `--include`. |
| `analytics-stack.png` | [SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html) Studio, [EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/emr-serverless.html), [Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html), and the presigned-URL Lambda |
| `full-architecture.png` | Complete infrastructure (collapsed overview), including the optional analytics stack and its API Gateway wiring |
| `full-architecture-detailed.png` | Complete infrastructure (expanded, `--no-collapse`), including the optional analytics stack |

## Stack Overview

### Global Stack

- AWS Global Accelerator
- TCP listener on port 443 only; Layer 4 pass-through does not terminate TLS
- HTTPS/443 endpoint groups per region
- [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) parameters for cross-region sharing

### API Gateway Stack

- REST API with [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) authentication and AWS-managed client TLS
- Global HMAC proxy and SigV4 cross-region aggregator
- KMS-encrypted HMAC and private-root [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) secrets
- Scheduled TLS manager that reimports short-lived leaves into stable regional ACM ARNs
- Public root trust and certificate ARN publication in SSM
- [WAF](https://docs.aws.amazon.com/waf/latest/developerguide/what-is-aws-waf.html) WebACL with AWS managed rules
- CloudWatch logging, expiry metrics, alarms, and encrypted rotation DLQ

### Regional Stack

- EKS cluster with Auto Mode
- Internal Application Load Balancer with HTTPS/443 private-root ACM certificate
- HTTP target-group hop from ALB to Kubernetes pods after TLS termination
- SQS job queue with DLQ
- EFS for persistent storage
- Manifest processor deployment
- Health monitor deployment
- KEDA for autoscaling
- Network policies

### Regional API Gateway Stack

- Always-deployed regional REST API with AWS-managed TLS and IAM authentication
- Resource policy always admits the exact aggregator role
- Optional policy admission for other same-account direct callers
- VPC Lambda proxy with HMAC signing and private-root TLS to the internal ALB

### Monitoring Stack

- CloudWatch dashboard
- Regional alarms (CPU, memory, SQS)
- Composite alarms
- [SNS](https://docs.aws.amazon.com/sns/latest/dg/welcome.html) alert topic
- Log groups

### Analytics Stack

- SageMaker Studio domain (VPC-only, IAM auth)
- EMR Serverless Spark application
- Cognito user pool and hosted UI domain
- Analytics [KMS](https://docs.aws.amazon.com/kms/latest/developerguide/overview.html) key
- Private-isolated VPC with SageMaker, [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html), [STS](https://docs.aws.amazon.com/STS/latest/APIReference/), [CloudWatch Logs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/WhatIsCloudWatchLogs.html), and EFS endpoints
- Studio EFS file system for per-user home folders
- Studio-only [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html) bucket plus its access-logs sidecar
- Presigned-URL Lambda that fronts `/studio/login`

## Requirements

The diagram generator requires:

- The `[cdk]` extra (`pip install -e '.[cdk]'`) — [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) libraries used to
  synthesize the app in-process.
- [cdk-dia](https://github.com/pistazie/cdk-dia) — installed from the root
  ``package.json`` / ``package-lock.json`` and executed locally.
- Graphviz `dot` (see Prerequisites above).

## Customization

Edit `diagrams/infra_diagrams/generate.py` to customize:

- Which stacks each diagram includes (via each builder's returned
  `--include` list)
- Collapsed vs expanded views (cdk-dia's `--collapse` / `--no-collapse`)
- The locked cdk-dia version in the root ``package.json``
