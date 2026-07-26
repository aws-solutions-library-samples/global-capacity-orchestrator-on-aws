# GCO Core

[CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) infrastructure stacks, Kubernetes services, data models, and configuration for the Global Capacity Orchestrator.

## Table of Contents

- [Structure](#structure)
  - [stacks/](#stacks)
  - [services/](#services)
  - [models/](#models)
  - [config/](#config)

## Structure

### stacks/

AWS CDK stack definitions that create the cloud infrastructure.

| File | Description |
|------|-------------|
| `global_stack.py` | [Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html), [SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html) parameters, [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html) model bucket, [DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html) tables |
| `regional_stack.py` | Per-region [VPC](https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html), [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster, [ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html), [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html), [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) functions, container images |
| `api_gateway_global_stack.py` | Edge-optimized [API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html) with [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) auth and CloudFront |
| `regional_api_gateway_stack.py` | Regional API Gateway for private VPC access |
| `monitoring_stack.py` | Cross-region [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html) dashboards, alarms, and [SNS](https://docs.aws.amazon.com/sns/latest/dg/welcome.html) alerts |
| `nag_suppressions.py` | CDK-nag compliance suppressions (AWS Solutions, HIPAA, NIST, PCI) |

### services/

Kubernetes microservices that run inside the EKS clusters.

| File | Description |
|------|-------------|
| `health_monitor.py` | Cluster health monitoring with configurable resource thresholds |
| `health_api.py` | Health check HTTP API endpoints |
| `manifest_processor.py` | Processes submitted Kubernetes manifests and applies them to the cluster |
| `manifest_api.py` | REST API for manifest submission, job listing, and status |
| `inference_monitor.py` | Reconciles inference endpoint desired state (DynamoDB) with actual K8s resources |
| `inference_store.py` | DynamoDB-backed store for inference endpoint specs and status |
| `queue_processor.py` | [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) queue consumer that processes job submissions from the regional queue |
| `metrics_publisher.py` | Publishes custom CloudWatch metrics for monitoring |
| `template_store.py` | DynamoDB-backed store for reusable job templates |
| `webhook_dispatcher.py` | Dispatches webhook notifications on job lifecycle events |
| `auth_middleware.py` | Validates short-lived, request-bound HMAC envelopes with key-rotation and replay protection |
| `api_shared.py` | Shared utilities for the FastAPI-based services |
| `structured_logging.py` | JSON structured logging configuration |

### models/

Python data models used across the codebase.

| File | Description |
|------|-------------|
| `manifest_models.py` | Manifest submission and validation models |
| `health_models.py` | Health check response models |
| `cluster_models.py` | Cluster and node information models |
| `inference_models.py` | Inference endpoint spec and status models |

### config/

Configuration loading and validation.

| File | Description |
|------|-------------|
| `config_loader.py` | Loads and validates configuration from cdk.json, environment variables, and user config files |
