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

`cdk-dia` is a Node CLI, fetched on demand via `npx` (pinned to
`CDK_DIA_VERSION` in `generate.py`), so Node.js must be installed. No global
`npm install` is required — the same way this repo invokes `npx cdk`.

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
| `global-stack.png` | Global Accelerator and endpoint groups |
| `api-gateway-stack.png` | API Gateway with IAM authentication |
| `regional-stack.png` | EKS cluster, ALB, SQS, EFS, and services |
| `regional-api-stack.png` | Regional API Gateway with VPC Lambda (private access). The regional stack is synthesized alongside it (its VPC construct is a constructor input), but the diagram is scoped to the regional-api stack via `--include`. |
| `monitoring-stack.png` | CloudWatch dashboards, alarms, and SNS. The full app is synthesized so the monitoring stack can read attributes from the other stacks, but the diagram is scoped to the monitoring stack via `--include`. |
| `analytics-stack.png` | SageMaker Studio, EMR Serverless, Cognito, and the presigned-URL Lambda |
| `full-architecture.png` | Complete infrastructure (collapsed overview) |
| `full-architecture-detailed.png` | Complete infrastructure (expanded, `--no-collapse`) |

## Stack Overview

### Global Stack

- AWS Global Accelerator
- TCP Listeners (ports 80, 443)
- Endpoint groups per region
- SSM parameters for cross-region sharing

### API Gateway Stack

- REST API with IAM authentication
- Lambda proxy function
- Secrets Manager for API keys
- WAF WebACL with AWS managed rules
- CloudWatch logging

### Regional Stack

- EKS cluster with Auto Mode
- Application Load Balancer
- SQS job queue with DLQ
- EFS for persistent storage
- Manifest processor deployment
- Health monitor deployment
- KEDA for autoscaling
- Network policies

### Regional API Gateway Stack

- Regional REST API with IAM authentication
- VPC Lambda proxy function
- Direct access to internal ALB
- Used when public access is disabled

### Monitoring Stack

- CloudWatch dashboard
- Regional alarms (CPU, memory, SQS)
- Composite alarms
- SNS alert topic
- Log groups

### Analytics Stack

- SageMaker Studio domain (VPC-only, IAM auth)
- EMR Serverless Spark application
- Cognito user pool and hosted UI domain
- Analytics KMS key
- Private-isolated VPC with SageMaker, ECR, STS, CloudWatch Logs, and EFS endpoints
- Studio EFS file system for per-user home folders
- Studio-only S3 bucket plus its access-logs sidecar
- Presigned-URL Lambda that fronts `/studio/login`

## Requirements

The diagram generator requires:

- The `[cdk]` extra (`pip install -e '.[cdk]'`) — CDK libraries used to
  synthesize the app in-process.
- [cdk-dia](https://github.com/pistazie/cdk-dia) — fetched on demand via
  `npx` (Node.js required).
- Graphviz `dot` (see Prerequisites above).

## Customization

Edit `diagrams/infra_diagrams/generate.py` to customize:

- Which stacks each diagram includes (via each builder's returned
  `--include` list)
- Collapsed vs expanded views (cdk-dia's `--collapse` / `--no-collapse`)
- The pinned cdk-dia version (`CDK_DIA_VERSION`)
