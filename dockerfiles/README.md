# Dockerfiles

This directory contains Dockerfiles for the Kubernetes services deployed to the [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) cluster.

## Table of Contents

- [Files](#files)
- [Usage](#usage)

## Files

Every file here is named `Dockerfile.<service>`, and that name is the catalog:
`gco/service_images.py` discovers this directory at runtime, so the regional
stack's image assets, the CLI's shipped-image inventory and the CI build/scan
matrix all follow from the filename with no list to keep in sync. The prefix
also makes the files recognizable to Dockerfile tooling (`checkov`, `hadolint`)
that keys on `Dockerfile*`.

- `Dockerfile.health-monitor` - Health monitoring service that tracks cluster resource utilization
- `Dockerfile.manifest-processor` - Manifest processing service that validates and applies Kubernetes manifests
- `Dockerfile.inference-monitor` - Inference endpoint reconciliation controller that manages K8s resources from [DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html) state
- `Dockerfile.inference-proxy` - In-cluster proxy that routes authenticated inference requests to endpoint backends
- `Dockerfile.queue-processor` - [SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) consumer that processes manifests submitted via `gco jobs submit-sqs` (KEDA ScaledJob)
- `Dockerfile.cost-monitor` - Cost reporting service that writes scheduled [OpenCost](https://opencost.io/) allocation reports (Parquet) to the central cost report bucket and serves the `/api/v1/cost/*` surface

## Usage

These Dockerfiles are automatically built by [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) during deployment. The images are pushed to [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) and referenced in the Kubernetes deployments.

Each image is a two-stage distroless build. The pinned `python:X.Y.Z-slim` builder stage installs the locked dependency set, applies Debian security patches behind the `APT_SECURITY_EPOCH` cache-buster, and precompiles the app tree; `build_scratch_rootfs.py` (shared by all six builds) then stages a minimal root filesystem — interpreter, site-packages, app tree, the exact ELF closure of those binaries, CA trust anchors, zoneinfo, and dpkg `status.d` metadata so [Trivy](https://trivy.dev/) keeps scanning the shipped Debian libraries — which the final `FROM scratch` stage copies wholesale. The deployed images contain no shell, package manager, or coreutils, so kubelet `exec` probes and `preStop` hooks in the deployment manifests use `["python", "-c", ...]`, never `/bin/sh`. Each final stage runs `runtime_smoke.py` as the runtime user, reached through a [BuildKit](https://docs.docker.com/build/buildkit/) bind mount that exists only for that one RUN — the deployed image ships no build tooling (a delete in a later layer would merely hide the bytes; layers are additive, so they are never written in at all). The smoke imports every stdlib C extension the builder stage could import — the set is derived programmatically by `build_scratch_rootfs.py` into `runtime_smoke_manifest.json`, so there is no hand-maintained module list — plus the service entry module (the script's first argument), and verifies NSS identity, CA trust anchors, and tzdata. Builder-to-scratch import parity is a stronger completeness proof than the ELF closure alone (it also catches `dlopen`'d libraries `ldd` cannot see), so a dependency whose shared-library needs aren't satisfied fails the build rather than the deployment.

The four traced API images (health-monitor, manifest-processor, inference-proxy, cost-monitor) also pass `--tracing`. `gco.services.tracing` imports [OpenTelemetry](https://opentelemetry.io/), botocore's SigV4 signer and httpx2 only when `GCO_TRACING_ENABLED=true`, so importing the entry module never loads them and a missing package would otherwise show up only as a runtime warning with tracing off. With the flag, the smoke parses `gco/services/tracing.py` as the image ships it, performs every import its functions contain the way each statement runs (`from package import name` needs `name`), and checks that httpx2's default trust store (truststore, reading the image's CA bundle) loads CA anchors, the public trust the X-Ray exporter uses. Like the extension set, the import list is derived rather than kept by hand, so a new deferred import is checked from the build it lands in. Neither check opens a connection. `tests/test_distroless_build_scripts.py` keeps the flag and the tracing roots in the `image-*` groups in step with the tracing module. The inference-monitor and queue-processor images do not trace and ship no OpenTelemetry.

Each production service has one matching `image-*` group under `pyproject.toml`'s `[project.optional-dependencies]`. During a build, Python 3.14's `tomllib` writes only that group's direct roots to a temporary `requirements-runtime.txt`, with `requirements-lock.txt` as the transitive constraint. The temporary file is removed in the same image layer. There are intentionally no per-image requirements files and the Dockerfiles do not run `pip install ".[image-*]"`, which would also install the CLI's base dependencies.

To modify a service:

1. Edit the service code in `gco/services/`.
2. Update only its matching `image-*` group in `pyproject.toml` if runtime dependencies changed.
3. Regenerate `requirements-lock.txt` when dependency versions changed.
4. Run `gco stacks deploy-all -y` to rebuild and deploy.

To add a service, add `Dockerfile.<service>` here and its `image-<service>`
dependency group, then build the asset in `gco/stacks/regional_stack.py` with
`self._service_image_asset(...)`. Discovery picks the file up from there:
`tests/test_service_images.py` and the CI container-scan matrix need no edit.
