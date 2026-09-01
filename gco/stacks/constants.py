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

from collections.abc import Collection, Mapping
from functools import lru_cache
from types import MappingProxyType

# Resource-governance defaults live in gco.resource_governance (a
# runtime-shippable module) because the manifest/queue processors need the
# same values inside their container images, whose build context deliberately
# excludes gco/stacks/** (see _SERVICE_IMAGE_COMMON_EXCLUDES in the regional
# stack). Re-exported here so synth-side code keeps one import home.
from gco.resource_governance import (
    DEFAULT_MANIFEST_RESOURCE_CAPS as DEFAULT_MANIFEST_RESOURCE_CAPS,
)
from gco.resource_governance import (
    DEFAULT_RESOURCE_QUOTA as DEFAULT_RESOURCE_QUOTA,
)
from gco.resource_governance import (
    parse_k8s_quantity as parse_k8s_quantity,
)

# ---------------------------------------------------------------------------
# Lambda Runtimes
# ---------------------------------------------------------------------------
# Keep every Lambda language runtime here rather than spelling enum members in
# individual stacks. The monthly dependency scan compares these constants with
# the newest managed runtimes exposed by aws-cdk-lib and checks the Node major
# against .nvmrc, package.json, and Dockerfile.dev.
LAMBDA_PYTHON_RUNTIME = "PYTHON_3_14"
"""CDK enum name for Python Lambdas (``lambda_.Runtime.PYTHON_3_14``)."""

LAMBDA_NODEJS_RUNTIME = "NODEJS_24_X"
"""CDK enum name for Node.js Lambdas (``lambda_.Runtime.NODEJS_24_X``)."""


# ---------------------------------------------------------------------------
# Deployment Region Contract
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def cloudformation_region_partitions() -> Mapping[str, str]:
    """Return immutable SDK-known CloudFormation Region-to-partition metadata.

    Botocore endpoint metadata covers every AWS partition and requires neither
    credentials nor a network request. Keeping this dynamic avoids a project
    allowlist that would reject opt-in, sovereign, or newly supported Regions
    already known to the installed SDK.
    """
    import boto3

    session = boto3.Session()
    region_partitions: dict[str, str] = {}
    for partition in session.get_available_partitions():
        for region in session.get_available_regions(
            "cloudformation",
            partition_name=partition,
        ):
            recorded_partition = region_partitions.setdefault(region, partition)
            if recorded_partition != partition:
                raise RuntimeError(
                    "AWS SDK endpoint metadata assigns CloudFormation region "
                    f"{region!r} to both {recorded_partition!r} and {partition!r}"
                )
    if not region_partitions:
        raise RuntimeError("AWS SDK endpoint metadata contains no CloudFormation regions")
    return MappingProxyType(region_partitions)


@lru_cache(maxsize=1)
def known_cloudformation_regions() -> frozenset[str]:
    """Return every AWS SDK-known Region that exposes CloudFormation."""
    return frozenset(cloudformation_region_partitions())


def validated_deployment_partition(
    regions: Collection[object],
    *,
    region_partitions: Mapping[str, str] | None = None,
) -> str:
    """Require a deployment topology to resolve to exactly one AWS partition.

    A single credentials/account context and this application's cross-stack
    references cannot span commercial, China, GovCloud, or ISO partitions.
    Region count remains deliberately unlimited within the selected partition.
    """
    if not regions:
        raise ValueError("At least one deployment region must be specified")

    metadata = (
        cloudformation_region_partitions() if region_partitions is None else region_partitions
    )
    if not metadata:
        raise RuntimeError("AWS SDK endpoint metadata contains no CloudFormation regions")

    regions_by_partition: dict[str, list[str]] = {}
    for region in regions:
        if not isinstance(region, str) or region not in metadata:
            raise ValueError(
                f"Invalid region {region!r}; expected an AWS region with a "
                "CloudFormation endpoint known to the installed SDK"
            )
        partition = metadata[region]
        regions_by_partition.setdefault(partition, []).append(region)

    if len(regions_by_partition) != 1:
        details = "; ".join(
            f"{partition}: {', '.join(sorted(partition_regions))}"
            for partition, partition_regions in sorted(regions_by_partition.items())
        )
        raise ValueError(
            f"Deployment regions must all belong to a single AWS partition; found {details}"
        )
    return next(iter(regions_by_partition))


def validated_regional_deployment_regions(
    value: object,
    *,
    known_regions: Collection[str] | None = None,
) -> tuple[str, ...]:
    """Return a non-empty, unique list of SDK-known workload Regions.

    There is deliberately no project-specific allowlist or maximum count. The
    optional ``known_regions`` argument lets callers reuse endpoint metadata
    they have already loaded while preserving this one validation contract.
    """
    if not isinstance(value, list) or not value:
        raise ValueError("At least one region must be specified")

    regions: list[str] = []
    valid_regions = (
        known_cloudformation_regions() if known_regions is None else frozenset(known_regions)
    )
    if not valid_regions:
        raise RuntimeError("AWS SDK endpoint metadata contains no CloudFormation regions")

    for region in value:
        if not isinstance(region, str) or region not in valid_regions:
            raise ValueError(
                f"Invalid region {region!r}; expected an AWS region with a "
                "CloudFormation endpoint known to the installed SDK"
            )
        regions.append(region)
    if len(regions) != len(set(regions)):
        raise ValueError("Duplicate regions found in configuration")
    return tuple(regions)


# ---------------------------------------------------------------------------
# HTTP Request Body Limits
# ---------------------------------------------------------------------------
DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
"""Default hard cap shared by API ingress and in-cluster request middleware."""

MAX_CONFIGURABLE_REQUEST_BODY_BYTES = 10 * 1024 * 1024
"""Largest supported cap; matches API Gateway's request payload ceiling."""

# The cross-region aggregator has a deliberately read-mostly regional contract.
# Keep both its identity policy and each regional API resource policy generated
# from this exact allowlist so a compromised aggregator cannot reach unrelated
# control-plane mutations exposed by the regional greedy route.
AGGREGATOR_REGIONAL_API_ROUTES = (
    ("GET", "api/v1/jobs"),
    ("DELETE", "api/v1/jobs"),
    ("GET", "api/v1/health"),
    ("GET", "api/v1/status"),
    # Read-only introspection of the region's deployed job validation policy.
    # Callers need it before submission to know whether a manifest will be
    # admitted; it exposes the same class of deployment metadata as
    # api/v1/status (which already reports the caps and namespace allowlist)
    # and mutates nothing.
    ("GET", "api/v1/policy"),
)


def validated_request_body_limit(value: object) -> int:
    """Return a safe request-body limit or reject an inconsistent deployment."""
    if type(value) is not int or value < 1 or value > MAX_CONFIGURABLE_REQUEST_BODY_BYTES:
        raise ValueError(
            "max_request_body_bytes must be an integer between 1 and "
            f"{MAX_CONFIGURABLE_REQUEST_BODY_BYTES}"
        )
    return value


# ---------------------------------------------------------------------------
# API Gateway Auth Secret
# ---------------------------------------------------------------------------
# Physical name of the Secrets Manager secret that holds the rotating HMAC
# signing key used by trusted API Gateway proxy Lambdas. It is created by
# ``GCOApiGatewayGlobalStack`` (in the ``api_gateway`` region) and read by the
# regional service-account role and regional API proxy Lambda. The historical
# ``api-gateway-auth-token`` suffix is retained to avoid replacing deployments.


def api_gateway_auth_secret_name(project_name: str) -> str:
    """Secrets Manager name for the proxy-to-backend HMAC signing key.

    Derived from ``project_name`` (``<project_name>/api-gateway-auth-token``)
    so two deployments in the same account+region do not collide on the secret
    name. For the default ``project_name="gco"`` this renders
    ``gco/api-gateway-auth-token`` — byte-for-byte identical to the pre-#139
    literal, so existing deployments see no resource replacement.

    Single source of truth shared by three call sites that must agree exactly:

    1. ``GCOApiGatewayGlobalStack._create_secret`` — the ``secret_name`` the
       secret is actually created with.
    2. ``GCORegionalStack`` — the deterministic IAM ``Resource`` ARN granting
       the service-account role read access to the secret. Built from this
       name plus the API Gateway region and account so it renders identically
       whether the API Gateway stack is cross-region or co-located with the
       regional stack (see issue #125 — a synthesis-time cross-stack export
       token used to leak into the ARN and dodge the cdk-nag suppression in
       single-region topologies).
    3. ``gco.stacks.nag_suppressions.add_iam_suppressions`` — the
       ``AwsSolutions-IAM5`` acknowledgment scoped to this exact ARN.

    Keep the three call sites in lockstep by calling this helper with the
    stack's ``project_name`` rather than re-typing the name.
    """
    return f"{project_name}/api-gateway-auth-token"  # nosec B105 — secret path/name, not a credential


def cross_region_aggregator_role_name(project_name: str) -> str:
    """IAM role name used by regional API resource-policy principals.

    IAM roles are global within an account, and the role ARN is embedded in
    API Gateway resource policies synthesized in other regions. A deterministic
    project-scoped physical name avoids an unsupported cross-region
    CloudFormation export. ``project_name`` is validated at 31 characters, so
    this 24-character suffix keeps the result below IAM's 64-character limit.
    """
    return f"{project_name}-cross-region-aggregator"


# ---------------------------------------------------------------------------
# Backend TLS private PKI
# ---------------------------------------------------------------------------


def backend_tls_server_name(project_name: str) -> str:
    """Private certificate identity asserted by every backend TLS client.

    The name deliberately does not need public DNS. Proxy clients connect to
    Global Accelerator or an internal ALB's real DNS name while sending this
    value as SNI and verifying it against the deployment-local root CA.
    """
    return f"backend.{project_name}.gco.internal"


def backend_tls_root_secret_name(project_name: str) -> str:
    """Secrets Manager name containing the deployment-local root private key."""
    return f"{project_name}/backend-tls/root-ca"


def backend_tls_root_ca_parameter_name(project_name: str) -> str:
    """SSM parameter containing only the public root trust bundle."""
    return f"/{project_name}/backend-tls/root-ca.pem"


def backend_tls_certificate_parameter_prefix(project_name: str) -> str:
    """SSM prefix under which regional imported-certificate ARNs are stored."""
    return f"/{project_name}/backend-tls/certificate-arn/"


def backend_tls_certificate_arn_parameter_name(project_name: str, region: str) -> str:
    """SSM parameter containing one region's stable imported ACM ARN."""
    return f"{backend_tls_certificate_parameter_prefix(project_name)}{region}"


# ---------------------------------------------------------------------------
# EKS Add-on Versions
# ---------------------------------------------------------------------------
# Pinned to specific eksbuild versions for reproducible deployments.
# The dependency scanner checks ``aws eks describe-addon-versions`` monthly
# and opens an issue when newer builds are available.

EKS_ADDON_POD_IDENTITY_AGENT = "v1.4.0-eksbuild.1"
"""EKS Pod Identity Agent — enables IRSA and Pod Identity for service accounts."""

EKS_ADDON_METRICS_SERVER = "v0.9.0-eksbuild.7"
"""Kubernetes Metrics Server — provides CPU/memory metrics for HPA and ``kubectl top``."""

EKS_ADDON_EFS_CSI_DRIVER = "v3.4.2-eksbuild.1"
"""Amazon EFS CSI Driver — mounts EFS file systems as Kubernetes persistent volumes."""

EKS_ADDON_CLOUDWATCH_OBSERVABILITY = "v6.6.0-eksbuild.1"
"""Amazon CloudWatch Observability — Container Insights, Prometheus metrics, FluentBit logs."""

EKS_ADDON_FSX_CSI_DRIVER = "v1.10.0-eksbuild.1"
"""Amazon FSx CSI Driver — mounts FSx for Lustre file systems as Kubernetes persistent volumes."""

# ---------------------------------------------------------------------------
# EKS Cluster Subnet Constraints
# ---------------------------------------------------------------------------
# A few Availability Zones cannot host the subnets you pass when creating an
# EKS cluster (the control-plane elastic network interfaces). EKS rejects
# cluster creation if any supplied subnet is in one of these zones. The
# constraint is published by *Availability Zone ID* (e.g. ``use1-az3``), which
# is stable across accounts — unlike the AZ *name* (``us-east-1e``), which AWS
# randomizes per account. Match by ID, then resolve to this account's names.
# Source: https://docs.aws.amazon.com/eks/latest/userguide/network-reqs.html
# ("Subnet requirements for clusters" — disallowed Availability Zone IDs).

EKS_UNSUPPORTED_AZ_IDS: dict[str, tuple[str, ...]] = {
    "us-east-1": ("use1-az3",),
    "us-west-1": ("usw1-az2",),
    "ca-central-1": ("cac1-az3",),
}
"""AWS-region → Availability Zone IDs that cannot hold EKS cluster subnets.

The regional VPC deliberately spans every AZ in the region (one public + one
private subnet each), but the EKS cluster's control-plane subnet selection must
exclude any subnet in these zones or ``CreateCluster`` fails with
``InvalidParameterException``. Regions absent from this map have no such
restriction. Keep in sync with the AWS EKS networking requirements doc.
"""

# ---------------------------------------------------------------------------
# Aurora PostgreSQL Engine Version
# ---------------------------------------------------------------------------
# Pinned to a specific minor version. The dependency scanner checks
# ``aws rds describe-db-engine-versions`` monthly for newer releases
# within the same major line.

AURORA_POSTGRES_VERSION = "17.10"
"""Aurora PostgreSQL engine version, applied via ``rds.AuroraPostgresEngineVersion.of()``.

A plain ``major.minor`` string rather than a CDK enum name: enum members lag
new RDS minor releases, which needlessly coupled an Aurora engine bump to an
aws-cdk-lib release. The dependency scanner validates this pin directly
against ``rds describe-db-engine-versions`` — the authoritative source — so
a newer minor is reported the day RDS ships it, independent of the CDK
library's enum catalog.
"""
# ---------------------------------------------------------------------------
# Analytics Environment Constants
# ---------------------------------------------------------------------------
# Pinned values consumed by the optional analytics environment (SageMaker
# Studio, EMR Serverless, Cognito hosted UI, and the always-on
# Cluster_Shared_Bucket in ``GCOGlobalStack``). Keeping them here lets the
# analytics stack, the regional stack, the global stack, and the tests import
# from a single source of truth.

EMR_SERVERLESS_RELEASE_LABEL = "emr-7.14.0"
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


def cognito_domain_prefix_default(project_name: str) -> str:
    """Default prefix for the Cognito hosted-UI domain.

    Derived from ``project_name`` (``<project_name>-studio``). The full domain
    prefix is assembled at synth time by appending the account id (e.g.
    ``gco-studio-123456789012``) so it stays globally unique within
    ``cognito.UserPoolDomain``. Operators may override the prefix through the
    ``analytics_environment.cognito.domain_prefix`` field in ``cdk.json``.

    For ``project_name="gco"`` this renders ``gco-studio`` — identical to the
    pre-#139 literal.
    """
    return f"{project_name}-studio"


STUDIO_PRESIGNED_URL_EXPIRY_SECONDS = 300
"""Default expiry (in seconds) for SageMaker Studio presigned domain URLs.

Five minutes matches the shortest window accepted by
``CreatePresignedDomainUrl`` while still giving a user enough time to click
the link after the ``/studio/login`` Lambda returns it. The presigned-URL
Lambda reads this through the ``URL_EXPIRES_SECONDS`` environment variable
and callers may override it per-request.
"""


def cluster_shared_bucket_name_prefix(project_name: str) -> str:
    """Name prefix for the always-on ``Cluster_Shared_Bucket`` in ``GCOGlobalStack``.

    Derived from ``project_name``. The full bucket name is
    ``<project_name>-cluster-shared-<account>-<global-region>``. The prefix is
    what IAM policies and cdk-nag allow-list assertions scope against, so both
    the bucket and the assertions must be built from the same ``project_name``.
    For ``project_name="gco"`` this renders ``gco-cluster-shared`` — identical
    to the pre-#139 literal.
    """
    return f"{project_name}-cluster-shared"


def cluster_shared_ssm_parameter_prefix(project_name: str) -> str:
    """SSM parameter namespace for the cluster-shared bucket metadata.

    Derived from ``project_name`` (``/<project_name>/cluster-shared-bucket``).
    ``GCOGlobalStack`` writes ``<prefix>/name``, ``<prefix>/arn``, and
    ``<prefix>/region`` under this path; ``GCORegionalStack`` (always) and
    ``GCOAnalyticsStack`` (when enabled) read them back via
    ``cr.AwsCustomResource`` against the global region. Treat the full paths as
    the contract. For ``project_name="gco"`` this renders
    ``/gco/cluster-shared-bucket``.
    """
    return f"/{project_name}/cluster-shared-bucket"


def regional_shared_bucket_name_prefix(project_name: str) -> str:
    """Name prefix for the always-on general-purpose regional bucket.

    Derived from ``project_name``. The full bucket name is
    ``<project_name>-regional-shared-<account>-<region>``. Each
    ``GCORegionalStack`` provisions exactly one such bucket per region,
    unconditionally — there is no ``cdk.json`` toggle and no feature flag
    gating its existence. It is general purpose (usable by any in-region
    workload) and is in addition to the always-on central buckets owned by
    ``GCOGlobalStack`` (the model bucket and the cluster-shared bucket). The
    prefix is what IAM policies and cdk-nag allow-list assertions scope
    against. For ``project_name="gco"`` this renders ``gco-regional-shared`` —
    identical to the pre-#139 literal.
    """
    return f"{project_name}-regional-shared"


def regional_shared_ssm_parameter_prefix(project_name: str) -> str:
    """SSM parameter namespace for the regional general-purpose bucket metadata.

    Derived from ``project_name`` (``/<project_name>/regional-shared-bucket``).
    Each ``GCORegionalStack`` writes ``<prefix>/name``, ``<prefix>/arn``, and
    ``<prefix>/region`` under this path **in its own region's** parameter
    store, exactly as the model bucket and cluster-shared bucket publish
    theirs. In-region workloads (and the regional upload surface) read them
    back to resolve the always-on regional bucket without hardcoding
    account/region into the name.

    The per-region inference monitor builds the same path at runtime from its
    injected ``PROJECT_NAME`` environment variable rather than importing this
    helper (it needs no CDK imports at runtime), so keep the two in lockstep.
    For ``project_name="gco"`` this renders ``/gco/regional-shared-bucket``.
    """
    return f"/{project_name}/regional-shared-bucket"


MOONCAKE_COLD_TIER_KEY_PREFIX = "mooncake-kv"
"""Object-key prefix for Mooncake cold-tier KV objects in the regional bucket.

The per-region inference monitor resolves an endpoint's cold-tier object-store
URI to ``s3://gco-regional-shared-<account>-<region>/mooncake-kv/<endpoint>/``,
and the ``gco inference populate-kv`` upload surface writes under the same
prefix, so operator-supplied warm-up objects land exactly where an endpoint's
pods read them. This is the shared contract between the two sides; the monitor
keeps a local copy of this value so it needs no CDK imports at runtime, so keep
the two in lockstep if the prefix ever changes.
"""

# ---------------------------------------------------------------------------
# Cost Monitoring Constants
# ---------------------------------------------------------------------------
# Shared contract between the monitoring stack (which owns the cost report
# bucket, Glue database/table, and Athena workgroup), the regional stacks
# (which grant the cost-monitor service write access by deterministic ARN),
# the cost-monitor service (which writes Parquet reports), and the CLI (which
# queries Athena). Everything below is derived from ``project_name`` so two
# deployments in one account never collide.

COST_REPORT_SCHEDULED_PREFIX = "reports"
"""Object-key prefix for scheduled cost allocation reports.

The cost-monitor service writes Hive-partitioned Parquet objects under
``reports/region=<region>/date=<YYYY-MM-DD>/...`` and the Glue table's
partition projection reads the same layout — keep the two in lockstep.
"""

COST_REPORT_ADHOC_PREFIX = "adhoc"
"""Object-key prefix for ad-hoc (user-requested) cost reports.

Kept out of the scheduled ``reports/`` prefix so an ad-hoc report whose
window overlaps a scheduled window can never double-count in Athena
aggregations over the scheduled table.
"""

COST_ATHENA_RESULTS_PREFIX = "athena-results"
"""Object-key prefix for Athena query results inside the cost report bucket."""


def cost_report_bucket_name_prefix(project_name: str) -> str:
    """Name prefix for the cost report bucket in ``GCOMonitoringStack``.

    Derived from ``project_name``. The full bucket name is
    ``<project_name>-cost-reports-<account>-<monitoring-region>`` — fully
    deterministic at synth time, which lets every regional stack grant its
    cost-monitor role write access by literal ARN without a cross-region
    SSM read (and without inverting the regional-before-monitoring deploy
    order). The prefix is what IAM policies and cdk-nag allow-list
    assertions scope against.
    """
    return f"{project_name}-cost-reports"


def cost_report_bucket_name(project_name: str, account: str, monitoring_region: str) -> str:
    """Deterministic physical name of the cost report bucket.

    Single source of truth shared by the monitoring stack (which creates the
    bucket), the regional stacks (which inject the name into the cost-monitor
    service environment and grant S3 access by literal ARN), and the CLI
    (which resolves the bucket for Athena result downloads and report
    listings).
    """
    return f"{cost_report_bucket_name_prefix(project_name)}-{account}-{monitoring_region}"


def cost_glue_database_name(project_name: str) -> str:
    """Glue database name for cost analytics.

    Glue database names must not contain hyphens, so the project name's
    hyphens are folded to underscores (``gco`` renders ``gco_cost``).
    """
    return f"{project_name.replace('-', '_')}_cost"


COST_GLUE_ALLOCATION_TABLE = "allocation_reports"
"""Glue table over the scheduled cost allocation reports."""


def cost_athena_workgroup_name(project_name: str) -> str:
    """Athena workgroup name for cost analytics queries."""
    return f"{project_name}-cost"


MOONCAKE_MASTER_DEFAULT_IMAGE = "vllm/vllm-openai:v0.27.1"
"""Default container image for the shared per-region Mooncake master.

The master StatefulSet runs the ``mooncake_master`` daemon (RPC + built-in HTTP
metadata server). That binary ships in the ``mooncake-transfer-engine`` package
that the upstream vLLM OpenAI server image already bundles, so the same pinned
image used for disaggregated prefill/decode pods also serves the master without
a separate build. The inference monitor reads this through the
``MOONCAKE_MASTER_IMAGE`` environment variable and a per-endpoint
``spec.mooncake.store.master_image`` overrides it.

Keep this tag in lockstep with ``cli/images.py:_DISAGGREGATED_DEFAULT_IMAGE``
(the disaggregated role-pod default); bump both together when validating a new
vLLM release and never use a mutable/rolling tag such as ``latest``.
"""
