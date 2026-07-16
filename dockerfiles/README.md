# Dockerfiles

This directory contains Dockerfiles for the Kubernetes services deployed to the EKS cluster.

## Table of Contents

- [Files](#files)
- [Usage](#usage)

## Files

- `health-monitor-dockerfile` - Health monitoring service that tracks cluster resource utilization
- `manifest-processor-dockerfile` - Manifest processing service that validates and applies Kubernetes manifests
- `inference-monitor-dockerfile` - Inference endpoint reconciliation controller that manages K8s resources from DynamoDB state
- `inference-proxy-dockerfile` - In-cluster proxy that routes authenticated inference requests to endpoint backends
- `queue-processor-dockerfile` - SQS consumer that processes manifests submitted via `gco jobs submit-sqs` (KEDA ScaledJob)

## Usage

These Dockerfiles are automatically built by CDK during deployment. The images are pushed to ECR and referenced in the Kubernetes deployments.

To modify a service:

1. Edit the service code in `gco/services/`
2. Run `gco stacks deploy-all -y` to rebuild and deploy
