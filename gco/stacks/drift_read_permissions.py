"""Read permissions CloudFormation drift detection needs for the regional stack.

Drift detection compares every resource with its template using the
*caller's* credentials, so besides ``cloudformation:DetectStackDrift``,
``DetectStackResourceDrift`` and ``BatchDescribeTypeConfigurations`` the
caller needs read access to each resource type in the stack ("Considerations
when detecting drift", CloudFormation User Guide). Without it every run ends
``DETECTION_FAILED``.

``DRIFT_READ_ACTIONS`` lists, per resource type the regional stack can create,
the read-handler permissions from that type's CloudFormation registry schema
(``handlers.read.permissions``). The legacy types without a registry handler
(FSx file systems, inline IAM policies, SNS topic and SQS queue policies) carry
the Describe/Get call their properties come from. Three schema permissions are
left out on purpose: ``secretsmanager:GetSecretValue`` (a secret's value is
not configuration, so with Aurora on CloudFormation reports its secret and
target attachment as failed checks), ``kms:Decrypt`` (needed only for Lambda
environments and state machines encrypted with a customer key, which this
stack does not use) and ``iam:PassRole`` (no read passes a role).

``tests/test_regional_iam_scoping.py`` fails when a synthesized regional
template contains a type that is neither here nor in ``UNCHECKED_TYPES``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: Types drift detection does not check: custom resources and CDK metadata.
UNCHECKED_TYPE_PREFIXES = ("Custom::", "AWS::CloudFormation::CustomResource", "AWS::CDK::Metadata")

_S3_BUCKET_READS = (
    "s3:GetAccelerateConfiguration",
    "s3:GetLifecycleConfiguration",
    "s3:GetBucketPublicAccessBlock",
    "s3:GetAnalyticsConfiguration",
    "s3:GetBucketCORS",
    "s3:GetEncryptionConfiguration",
    "s3:GetInventoryConfiguration",
    "s3:GetBucketLogging",
    "s3:GetMetricsConfiguration",
    "s3:GetBucketNotification",
    "s3:GetBucketVersioning",
    "s3:GetReplicationConfiguration",
    "s3:GetBucketWebsite",
    "s3:GetBucketObjectLockConfiguration",
    "s3:GetBucketTagging",
    "s3:ListTagsForResource",
    "s3:GetBucketAbac",
    "s3:GetBucketOwnershipControls",
    "s3:GetIntelligentTieringConfiguration",
    "s3:GetBucketMetadataTableConfiguration",
    "s3:GetBucketDefaultStorageClass",
    "s3:ListBucket",
)

DRIFT_READ_ACTIONS: Mapping[str, tuple[str, ...]] = {
    "AWS::EC2::EIP": ("ec2:DescribeAddresses",),
    "AWS::EC2::FlowLog": ("ec2:DescribeFlowLogs",),
    "AWS::EC2::InternetGateway": ("ec2:DescribeInternetGateways",),
    "AWS::EC2::LaunchTemplate": ("ec2:DescribeLaunchTemplates",),
    "AWS::EC2::NatGateway": ("ec2:DescribeNatGateways",),
    "AWS::EC2::PlacementGroup": ("ec2:DescribePlacementGroups",),
    "AWS::EC2::Route": ("ec2:DescribeRouteTables",),
    "AWS::EC2::RouteTable": ("ec2:DescribeRouteTables",),
    "AWS::EC2::SecurityGroup": ("ec2:DescribeSecurityGroups",),
    "AWS::EC2::SecurityGroupEgress": ("ec2:DescribeSecurityGroupRules",),
    "AWS::EC2::SecurityGroupIngress": (
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSecurityGroupRules",
    ),
    "AWS::EC2::Subnet": ("ec2:DescribeSubnets", "ec2:DescribeNetworkAcls"),
    "AWS::EC2::SubnetRouteTableAssociation": ("ec2:DescribeRouteTables",),
    "AWS::EC2::VPC": (
        "ec2:DescribeNetworkAcls",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeVpcAttribute",
        "ec2:DescribeVpcEncryptionControls",
        "ec2:DescribeVpcs",
    ),
    "AWS::EC2::VPCEndpoint": (
        "ec2:DescribeVpcEndpoints",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "vpc-lattice:DescribeServiceNetworkVpcEndpointAssociation",
        "ec2:DescribeVpcs",
    ),
    "AWS::EC2::VPCGatewayAttachment": (
        "ec2:DescribeInternetGateways",
        "ec2:DescribeVpnGateways",
    ),
    "AWS::EFS::AccessPoint": ("elasticfilesystem:DescribeAccessPoints",),
    "AWS::EFS::FileSystem": (
        "elasticfilesystem:DescribeBackupPolicy",
        "elasticfilesystem:DescribeFileSystemPolicy",
        "elasticfilesystem:DescribeFileSystems",
        "elasticfilesystem:DescribeLifecycleConfiguration",
        "elasticfilesystem:DescribeReplicationConfigurations",
    ),
    "AWS::EFS::MountTarget": (
        "elasticfilesystem:DescribeMountTargets",
        "elasticfilesystem:DescribeMountTargetSecurityGroups",
    ),
    "AWS::EKS::AccessEntry": ("eks:DescribeAccessEntry", "eks:ListAssociatedAccessPolicies"),
    "AWS::EKS::Addon": ("eks:DescribeAddon",),
    "AWS::EKS::Capability": ("eks:DescribeCapability",),
    "AWS::EKS::Cluster": ("eks:DescribeCluster",),
    "AWS::EKS::PodIdentityAssociation": ("eks:DescribePodIdentityAssociation",),
    "AWS::ElastiCache::ServerlessCache": (
        "elasticache:DescribeServerlessCaches",
        "elasticache:ListTagsForResource",
    ),
    "AWS::Events::Rule": (
        "events:DescribeRule",
        "events:ListTargetsByRule",
        "events:ListTagsForResource",
    ),
    "AWS::FSx::FileSystem": ("fsx:DescribeFileSystems",),
    "AWS::IAM::InstanceProfile": ("iam:GetInstanceProfile",),
    "AWS::IAM::OIDCProvider": ("iam:GetOpenIDConnectProvider",),
    "AWS::IAM::Policy": ("iam:GetRolePolicy",),
    "AWS::IAM::Role": (
        "iam:GetRole",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
        "iam:GetRolePolicy",
    ),
    "AWS::KMS::Key": (
        "kms:DescribeKey",
        "kms:GetKeyPolicy",
        "kms:GetKeyRotationStatus",
        "kms:ListResourceTags",
    ),
    "AWS::Lambda::Function": (
        "lambda:GetFunction",
        "lambda:GetFunctionCodeSigningConfig",
        "lambda:GetFunctionRecursionConfig",
        "lambda:GetRuntimeManagementConfig",
        "lambda:GetFunctionScalingConfig",
    ),
    "AWS::Lambda::Permission": ("lambda:GetPolicy",),
    "AWS::Logs::LogGroup": (
        "logs:DescribeLogGroups",
        "logs:ListTagsForResource",
        "logs:GetDataProtectionPolicy",
        "logs:DescribeIndexPolicies",
        "logs:DescribeResourcePolicies",
    ),
    "AWS::PCS::Cluster": ("pcs:GetCluster", "pcs:ListTagsForResource"),
    "AWS::PCS::ComputeNodeGroup": ("pcs:GetComputeNodeGroup", "pcs:ListTagsForResource"),
    "AWS::PCS::Queue": ("pcs:GetQueue", "pcs:ListTagsForResource"),
    "AWS::RDS::DBCluster": ("rds:DescribeDBClusters",),
    "AWS::RDS::DBInstance": (
        "ec2:DescribeAccountAttributes",
        "ec2:DescribeAvailabilityZones",
        "ec2:DescribeInternetGateways",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcAttribute",
        "ec2:DescribeVpcs",
        "rds:DescribeDBInstances",
    ),
    "AWS::RDS::DBSubnetGroup": ("rds:DescribeDBSubnetGroups", "rds:ListTagsForResource"),
    "AWS::S3::Bucket": _S3_BUCKET_READS,
    "AWS::S3::BucketPolicy": ("s3:GetBucketPolicy",),
    "AWS::SNS::Topic": (
        "sns:GetTopicAttributes",
        "sns:ListTagsForResource",
        "sns:ListSubscriptionsByTopic",
        "sns:GetDataProtectionPolicy",
    ),
    "AWS::SNS::TopicPolicy": ("sns:GetTopicAttributes",),
    "AWS::SQS::Queue": ("sqs:GetQueueAttributes", "sqs:ListQueueTags"),
    "AWS::SQS::QueuePolicy": ("sqs:GetQueueAttributes",),
    "AWS::SSM::Parameter": ("ssm:GetParameters", "ssm:ListTagsForResource"),
    "AWS::SageMaker::Cluster": ("sagemaker:DescribeCluster", "sagemaker:ListTags"),
    "AWS::SecretsManager::Secret": ("secretsmanager:DescribeSecret",),
    "AWS::SecretsManager::SecretTargetAttachment": (),
    "AWS::StepFunctions::StateMachine": (
        "states:DescribeStateMachine",
        "states:ListTagsForResource",
    ),
}

#: Action prefixes granted on the stack's own resources rather than ``*``:
#: bucket configuration and listing, parameter values, and function details
#: (``lambda:GetFunction`` returns a link to the function's code).
_BUCKET_ACTION_PREFIX = "s3:"
_PARAMETER_ACTION_PREFIX = "ssm:"
_FUNCTION_ACTION_PREFIX = "lambda:"


@dataclass(frozen=True)
class DriftReadActions:
    """The read actions for one stack, split by the resources they may name."""

    unscoped: tuple[str, ...]
    bucket: tuple[str, ...]
    parameter: tuple[str, ...]
    function: tuple[str, ...]


def is_unchecked_type(resource_type: str) -> bool:
    """True for types drift detection never reads (custom resources, CDK metadata)."""
    return resource_type.startswith(UNCHECKED_TYPE_PREFIXES)


def drift_read_actions(resource_types: Iterable[str]) -> DriftReadActions:
    """Return the sorted read actions drift detection needs for ``resource_types``.

    Raises:
        KeyError: a type is neither mapped nor a type drift detection skips,
            so its reads would be missing and its check would fail.
    """
    actions: set[str] = set()
    for resource_type in set(resource_types):
        if is_unchecked_type(resource_type):
            continue
        actions.update(DRIFT_READ_ACTIONS[resource_type])

    def _with(prefix: str) -> tuple[str, ...]:
        return tuple(sorted(a for a in actions if a.startswith(prefix)))

    scoped_prefixes = (_BUCKET_ACTION_PREFIX, _PARAMETER_ACTION_PREFIX, _FUNCTION_ACTION_PREFIX)
    return DriftReadActions(
        unscoped=tuple(sorted(a for a in actions if not a.startswith(scoped_prefixes))),
        bucket=_with(_BUCKET_ACTION_PREFIX),
        parameter=_with(_PARAMETER_ACTION_PREFIX),
        function=_with(_FUNCTION_ACTION_PREFIX),
    )
