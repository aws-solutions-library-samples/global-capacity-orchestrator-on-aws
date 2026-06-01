"""Pinned version constants for GCO infrastructure.

Single source of truth for all version-pinned infrastructure components.
Centralising these makes it easy to:

1. See every pinned version at a glance
2. Update versions in one place
3. Let the dependency scanner (`.github/scripts/dependency-scan.sh`)
   find them with a simple import instead of regex scraping
4. Write tests that assert versions haven't drifted

When updating a version here, also check:
- ``lambda/helm-installer/charts.yaml`` for Helm chart versions
- ``requirements-lock.txt`` for Python dependency versions
- ``cdk.json`` context for ``kubernetes_version``

The dependency scanner runs monthly and opens an issue when any of
these fall behind the latest available release.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Lambda Runtime
# ---------------------------------------------------------------------------
# All Lambda functions in GCO use the same Python runtime. Changing this
# single constant updates every function across all stacks.
LAMBDA_PYTHON_RUNTIME = "PYTHON_3_14"
"""CDK enum name for the Lambda runtime (e.g. ``lambda_.Runtime.PYTHON_3_14``)."""

# ---------------------------------------------------------------------------
# EKS Add-on Versions
# ---------------------------------------------------------------------------
# Pinned to specific eksbuild versions for reproducible deployments.
# The dependency scanner checks ``aws eks describe-addon-versions`` monthly
# and opens an issue when newer builds are available.

EKS_ADDON_POD_IDENTITY_AGENT = "v1.3.10-eksbuild.3"
"""EKS Pod Identity Agent — enables IRSA and Pod Identity for service accounts."""

EKS_ADDON_METRICS_SERVER = "v0.8.1-eksbuild.6"
"""Kubernetes Metrics Server — provides CPU/memory metrics for HPA and ``kubectl top``."""

EKS_ADDON_EFS_CSI_DRIVER = "v3.2.0-eksbuild.1"
"""Amazon EFS CSI Driver — mounts EFS file systems as Kubernetes persistent volumes."""

EKS_ADDON_CLOUDWATCH_OBSERVABILITY = "v5.4.0-eksbuild.1"
"""Amazon CloudWatch Observability — Container Insights, Prometheus metrics, FluentBit logs."""

EKS_ADDON_FSX_CSI_DRIVER = "v1.9.0-eksbuild.1"
"""Amazon FSx CSI Driver — mounts FSx for Lustre file systems as Kubernetes persistent volumes."""

# ---------------------------------------------------------------------------
# Aurora PostgreSQL Engine Version
# ---------------------------------------------------------------------------
# Pinned to a specific minor version. The dependency scanner checks
# ``aws rds describe-db-engine-versions`` monthly for newer releases
# within the same major line.

AURORA_POSTGRES_VERSION = "VER_17_9"
"""CDK enum name for the Aurora PostgreSQL engine version (e.g. ``rds.AuroraPostgresEngineVersion.VER_17_9``)."""

AURORA_POSTGRES_VERSION_DISPLAY = "17.9"
"""Human-readable version string for documentation and logging."""
# ---------------------------------------------------------------------------
# Analytics Environment Constants
# ---------------------------------------------------------------------------
# Pinned values consumed by the optional analytics environment (SageMaker
# Studio, EMR Serverless, Cognito hosted UI, and the always-on
# Cluster_Shared_Bucket in ``GCOGlobalStack``). Keeping them here lets the
# analytics stack, the regional stack, the global stack, and the tests import
# from a single source of truth.

EMR_SERVERLESS_RELEASE_LABEL = "emr-7.13.0"
"""EMR Serverless Spark release label used for ``emrserverless.CfnApplication``.

Pinned to a stable Spark release so analytics workloads get a reproducible
runtime across deployments. Update alongside the EKS add-ons above when a
newer EMR release is validated against the studio notebooks.
"""

SAGEMAKER_ROLE_NAME_PREFIX = "AmazonSageMaker"
"""Required prefix for the SageMaker Studio execution role name.

Amazon SageMaker requires execution roles used by Studio domains to have a
name that starts with ``AmazonSageMaker`` so that AWS-managed policies and
service-linked trust relationships resolve correctly. Any role name generated
for ``SageMaker_Execution_Role`` must begin with this prefix.
"""

COGNITO_DOMAIN_PREFIX_DEFAULT = "gco-studio"
"""Default prefix for the Cognito hosted-UI domain.

The full domain prefix is assembled at synth time by appending the account
id (e.g. ``gco-studio-123456789012``) so it stays globally unique within
``cognito.UserPoolDomain``. Operators may override the prefix through the
``analytics_environment.cognito.domain_prefix`` field in ``cdk.json``.
"""

STUDIO_PRESIGNED_URL_EXPIRY_SECONDS = 300
"""Default expiry (in seconds) for SageMaker Studio presigned domain URLs.

Five minutes matches the shortest window accepted by
``CreatePresignedDomainUrl`` while still giving a user enough time to click
the link after the ``/studio/login`` Lambda returns it. The presigned-URL
Lambda reads this through the ``URL_EXPIRES_SECONDS`` environment variable
and callers may override it per-request.
"""

CLUSTER_SHARED_BUCKET_NAME_PREFIX = "gco-cluster-shared"
"""Name prefix for the always-on ``Cluster_Shared_Bucket`` in ``GCOGlobalStack``.

The full bucket name is ``gco-cluster-shared-<account>-<global-region>``.
The prefix is what IAM policies and cdk-nag allow-list assertions
scope against, so it must stay stable across refactors even when the region
or account suffix changes.
"""

CLUSTER_SHARED_SSM_PARAMETER_PREFIX = "/gco/cluster-shared-bucket"
"""SSM parameter namespace for the cluster-shared bucket metadata.

``GCOGlobalStack`` writes ``<prefix>/name``, ``<prefix>/arn``, and
``<prefix>/region`` under this path; ``GCORegionalStack`` (always) and
``GCOAnalyticsStack`` (when enabled) read them back via
``cr.AwsCustomResource`` against the global region. Treat the full paths as
the contract — this prefix is the single place to change if the namespace
ever moves.
"""
