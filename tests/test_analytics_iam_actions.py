"""Every IAM action the analytics stack grants names a real action.

IAM accepts a policy that names an action no service has: the statement is
stored, and it allows nothing. The analytics stack's HyperPod statement
granted ``sagemaker:ClusterInstance`` and ``sagemaker:ClusterInstanceGroup``
for releases before anyone noticed. This test resolves each action against
the operations botocore's service models list (offline, no AWS call), plus a
short list of IAM actions that are not API operations, and each wildcard
against the same names, so a typo or an invented action fails here.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from functools import cache
from typing import Any, cast

import aws_cdk as cdk
import botocore.exceptions
import botocore.session
import pytest
from aws_cdk import assertions

from gco.config.config_loader import ConfigLoader
from gco.stacks.analytics_stack import GCOAnalyticsStack
from tests.test_analytics_stack import _AnalyticsMockConfig

#: IAM service prefix -> botocore service model, where the two differ.
_SERVICE_MODELS = {
    "elasticfilesystem": "efs",
    "states": "stepfunctions",
}

#: IAM actions that are not API operations (authorization-only actions) or
#: whose service botocore does not model. Each one is documented in the
#: Service Authorization Reference.
_IAM_ONLY_ACTIONS = frozenset(
    {
        # NFS client access to an EFS file system.
        "elasticfilesystem:ClientMount",
        "elasticfilesystem:ClientWrite",
        "elasticfilesystem:ClientRootAccess",
        # Studio's EMR Serverless panel and SparkMagic open Livy sessions.
        "emr-serverless:AccessLivyEndpoints",
        # Calling an IAM-authorized API Gateway method.
        "execute-api:Invoke",
        # Passing a role to a service.
        "iam:PassRole",
        # The Lambda Invoke operation's action.
        "lambda:InvokeFunction",
        # S3 bucket listing (the ListObjects / ListObjectsV2 operations).
        "s3:ListBucket",
        # PutObjectTagging on a specific object version (grant_write).
        "s3:PutObjectVersionTagging",
    }
)

#: Services whose actions botocore does not model at all; a wildcard on
#: them cannot be resolved, so it must be one of these exact strings.
_UNMODELED_WILDCARDS = frozenset({"sagemaker-mlflow:*"})


@cache
def _operations(prefix: str) -> frozenset[str]:
    session = botocore.session.get_session()
    model = session.get_service_model(_SERVICE_MODELS.get(prefix, prefix))
    return frozenset(model.operation_names)


def _policy_actions(node: Any) -> Iterator[str]:
    """Every ``Action`` / ``NotAction`` string of every policy statement in ``node``."""
    if isinstance(node, dict):
        statements = node.get("Statement")
        if isinstance(statements, list):
            for statement in statements:
                if not isinstance(statement, dict):
                    continue
                for key in ("Action", "NotAction"):
                    value = statement.get(key, [])
                    yield from [value] if isinstance(value, str) else value
        for value in node.values():
            yield from _policy_actions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _policy_actions(value)


def _template(**config: Any) -> dict[str, Any]:
    mock = _AnalyticsMockConfig(**config)
    stack = GCOAnalyticsStack(
        cdk.App(),
        "test-analytics-stack",
        config=cast(ConfigLoader, mock),
        env=cdk.Environment(account="123456789012", region=mock.get_api_gateway_region()),
    )
    return assertions.Template.from_stack(stack).to_json()


def _unknown(action: str) -> bool:
    """True when ``action`` names (or, as a wildcard, matches) no IAM action."""
    if action == "*" or action in _IAM_ONLY_ACTIONS or action in _UNMODELED_WILDCARDS:
        return False
    prefix, _, name = action.partition(":")
    operations = _operations(prefix)
    if "*" in name or "?" in name:
        return not fnmatch.filter(operations, name)
    return name not in operations


def test_every_action_the_stack_grants_exists() -> None:
    """With every analytics sub-toggle on, so each optional statement is checked."""
    template = _template(hyperpod_enabled=True, canvas_enabled=True)
    actions = sorted(set(_policy_actions(template["Resources"])))
    assert "sagemaker:CreatePresignedDomainUrl" in actions
    assert "execute-api:Invoke" in actions

    assert [action for action in actions if _unknown(action)] == []


@pytest.mark.parametrize(
    "action",
    [
        "sagemaker:ClusterInstance",
        "sagemaker:ClusterInstanceGroup",
        "sagemaker:DescribeTrainingJobs",
    ],
)
def test_the_check_refuses_actions_no_service_has(action: str) -> None:
    prefix, _, name = action.partition(":")
    assert name not in _operations(prefix)


def test_iam_only_actions_are_not_api_operations() -> None:
    """Keep the exception list honest: an entry that is an API operation
    belongs to the general check instead."""
    for action in _IAM_ONLY_ACTIONS:
        prefix, _, name = action.partition(":")
        try:
            operations = _operations(prefix)
        except botocore.exceptions.UnknownServiceError:
            continue
        assert name not in operations, action
