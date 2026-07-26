# Dockerfiles

This directory contains Dockerfiles for the Kubernetes services deployed to the [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster.

## Table of Contents

- [Files](#files)
- [Usage](#usage)

## Files

- `health-monitor-dockerfile` - Health monitoring service that tracks cluster resource utilization
- `manifest-processor-dockerfile` - Manifest processing service that validates and applies Kubernetes manifests
- `inference-monitor-dockerfile` - Inference endpoint reconciliation controller that manages K8s resources from [DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html) state
- `inference-proxy-dockerfile` - In-cluster proxy that routes authenticated inference requests to endpoint backends
- `queue-processor-dockerfile` - [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) consumer that processes manifests submitted via `gco jobs submit-sqs` (KEDA ScaledJob)

## Usage

These Dockerfiles are automatically built by [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) during deployment. The images are pushed to [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) and referenced in the Kubernetes deployments.

Each production service has one matching `image-*` group under `pyproject.toml`'s `[project.optional-dependencies]`. During a build, Python 3.14's `tomllib` writes only that group's direct roots to a temporary `requirements-runtime.txt`, with `requirements-lock.txt` as the transitive constraint. The temporary file is removed in the same image layer. There are intentionally no per-image requirements files and the Dockerfiles do not run `pip install ".[image-*]"`, which would also install the CLI's base dependencies.

To modify a service:

1. Edit the service code in `gco/services/`.
2. Update only its matching `image-*` group in `pyproject.toml` if runtime dependencies changed.
3. Regenerate `requirements-lock.txt` when dependency versions changed.
4. Run `gco stacks deploy-all -y` to rebuild and deploy.
