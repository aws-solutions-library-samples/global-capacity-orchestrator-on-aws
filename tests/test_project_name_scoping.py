"""Project-name scoping guardrails (issue #139).

Two GCO deployments must be able to coexist in the *same account and region*
by using a different ``project_name`` alone. That only holds if every physical
resource name that is unique per account+region (or globally) derives from
``project_name``. Historically a set of names were hardcoded to a literal
``gco`` prefix, so a second deployment collided with the first.

These tests are the executable contract for the fix:

* :class:`TestProjectNameValidation` — ``ConfigLoader`` rejects malformed
  ``project_name`` values up front (so a bad value fails at synth, not
  mid-deploy).
* :class:`TestBackwardCompatibility` — synthesizing with the default
  ``project_name="gco"`` still renders the exact pre-#139 physical names, so
  upgrading an existing deployment replaces no resources.
* :class:`TestNoCollisionsAcrossProjectNames` — for a range of
  ``project_name`` permutations, every unique physical name embeds the project
  name, and two distinct deployments share **zero** unique names.

The synth is the full app (``app.py`` topology) with the analytics environment
enabled so the analytics-stack names (Studio bucket, SageMaker role, Studio
domain, EMR app, Cognito domain) are covered too. Synths are cached per
``project_name`` as small name-sets (not full templates) to keep memory low.
"""

from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError
from tests._analytics_cdk_overlays import build_overlay, synth_all_stacks

# ---------------------------------------------------------------------------
# Physical-name extraction
# ---------------------------------------------------------------------------
# CloudFormation resource types whose physical name must be unique per
# account+region (S3 bucket names and Cognito domains are globally unique).
# The value is the ``Properties`` key holding that name. Resources that CDK
# auto-names (no explicit name property) are intentionally excluded: their
# generated names embed the stack name, which already carries the project
# prefix, so they cannot collide across deployments. ``AWS::ApiGateway::RestApi``
# is deliberately omitted — REST API names need not be unique (see #139).
_NAME_PROPERTY_BY_TYPE: dict[str, str] = {
    "AWS::S3::Bucket": "BucketName",
    "AWS::SSM::Parameter": "Name",
    "AWS::SecretsManager::Secret": "Name",
    "AWS::WAFv2::WebACL": "Name",
    "AWS::Logs::LogGroup": "LogGroupName",
    "AWS::DynamoDB::Table": "TableName",
    "AWS::IAM::Role": "RoleName",
    "AWS::SageMaker::Domain": "DomainName",
    "AWS::EMRServerless::Application": "Name",
    "AWS::Cognito::UserPoolDomain": "Domain",
}


def _serialize(value: Any) -> str:
    """Canonicalize a template value (plain string or CFN intrinsic) to a string.

    Names can be plain strings (``"gco-api-gateway-waf"``) or intrinsic-function
    dicts (``{"Fn::Join": ["", ["gco-cluster-shared-", {"Ref": "AWS::AccountId"},
    "-us-east-1"]]}``). Serializing with sorted keys gives a stable, comparable
    form for both, and keeps the literal segments (which carry the project
    prefix) intact for substring checks.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _collect_unique_names(templates: dict[str, dict[str, Any]]) -> set[str]:
    """Return every must-be-unique physical name across all stack templates.

    Includes CloudFormation export names (``Outputs[*].Export.Name``), which
    must be unique per account+region, prefixed with ``EXPORT:`` so they never
    accidentally match a resource name.
    """
    names: set[str] = set()
    for template in templates.values():
        for resource in (template.get("Resources") or {}).values():
            prop = _NAME_PROPERTY_BY_TYPE.get(resource.get("Type", ""))
            if prop is None:
                continue
            value = (resource.get("Properties") or {}).get(prop)
            if value is None:
                # Auto-named resource (no explicit name) — cannot collide.
                continue
            names.add(_serialize(value))
        for output in (template.get("Outputs") or {}).values():
            export_name = (output.get("Export") or {}).get("Name")
            if export_name is not None:
                names.add("EXPORT:" + _serialize(export_name))
    return names


# ---------------------------------------------------------------------------
# Cached synth → name-set, keyed by project_name (memory-frugal).
# ---------------------------------------------------------------------------
_NAME_SET_CACHE: dict[str, frozenset[str]] = {}


def _synthesize(
    project_name: str, *, regions: tuple[str, ...] = ("us-east-1",)
) -> dict[str, dict[str, Any]]:
    """Full-app synth for ``project_name`` with the analytics environment on."""
    overlay = build_overlay(enabled=True, hyperpod_enabled=False, regions=list(regions))
    overlay["project_name"] = project_name
    return synth_all_stacks(overlay)


def _unique_names(project_name: str) -> frozenset[str]:
    """Return (and cache) the set of must-be-unique names for ``project_name``.

    Only the small name-set is retained; the full templates are dropped after
    extraction so the module never holds more than one synth's worth of
    template data at a time.
    """
    if project_name not in _NAME_SET_CACHE:
        templates = _synthesize(project_name)
        _NAME_SET_CACHE[project_name] = frozenset(_collect_unique_names(templates))
    return _NAME_SET_CACHE[project_name]


# ---------------------------------------------------------------------------
# ConfigLoader.project_name validation
# ---------------------------------------------------------------------------


class TestProjectNameValidation:
    """``ConfigLoader`` enforces the ``project_name`` format at load time."""

    @staticmethod
    def _load(valid_cdk_context: dict[str, Any], name: object) -> ConfigLoader:
        context = dict(valid_cdk_context)
        context["project_name"] = name
        return ConfigLoader(cdk.App(context=context))

    @pytest.mark.parametrize(
        "name",
        ["gco", "gco-staging", "acme", "p1", "team-a-prod", "a" * 31],
    )
    def test_valid_names_accepted(self, valid_cdk_context: dict[str, Any], name: str) -> None:
        config = self._load(valid_cdk_context, name)
        assert config.get_project_name() == name

    @pytest.mark.parametrize(
        "name",
        [
            "GCO",  # uppercase (illegal in S3/Cognito)
            "gco_staging",  # underscore
            "gco.staging",  # dot
            "1gco",  # leading digit
            "-gco",  # leading hyphen
            "g",  # too short (needs >= 2 chars)
            "a" * 32,  # too long (> 31 chars)
            "gco staging",  # space
            "",  # empty
        ],
    )
    def test_invalid_names_rejected(self, valid_cdk_context: dict[str, Any], name: str) -> None:
        with pytest.raises(ConfigValidationError):
            self._load(valid_cdk_context, name)


# ---------------------------------------------------------------------------
# Backward compatibility: default "gco" renders the pre-#139 literals
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Default ``project_name="gco"`` must render byte-for-byte the old names.

    If any of these regressed, upgrading an existing ``gco`` deployment would
    rename (and therefore replace) a stateful resource — the exact outcome the
    fix must avoid.
    """

    def test_default_gco_renders_legacy_names(self) -> None:
        blob = "\n".join(_unique_names("gco"))
        expected_fragments = [
            # Global stack — cluster-shared bucket + SSM registry
            "gco-cluster-shared-",
            "/gco/cluster-shared-bucket/name",
            "/gco/cluster-shared-bucket/arn",
            "/gco/cluster-shared-bucket/region",
            # DynamoDB tables
            "gco-jobs",
            "gco-job-templates",
            "gco-inference-endpoints",
            # Regional stack — regional-shared bucket + SSM registry
            "gco-regional-shared-",
            "/gco/regional-shared-bucket/name",
            # API Gateway stack — secret, WAF, log groups, exports
            "gco/api-gateway-auth-token",
            "gco-api-gateway-waf",
            "/aws/apigateway/gco-global",
            "aws-waf-logs-gco-api-gateway",
            "gco-global-api-endpoint",
            "gco-auth-secret-arn",
            "gco-waf-webacl-arn",
            # Analytics stack — Studio bucket, SageMaker role, domain, EMR app
            "gco-analytics-studio-",
            "AmazonSageMaker-gco-analytics-exec-",
            "gco-studio-",
            "gco-spark-",
        ]
        missing = [frag for frag in expected_fragments if frag not in blob]
        assert not missing, (
            "project_name='gco' no longer renders these legacy names (upgrading "
            f"would replace resources): {missing}"
        )


# ---------------------------------------------------------------------------
# No collisions across project_name permutations
# ---------------------------------------------------------------------------


class TestNoCollisionsAcrossProjectNames:
    """Every unique name embeds project_name; distinct deployments never share one."""

    # Permutations chosen to exercise hyphens, digits, and the ``gco`` substring
    # while all differing from each other and from the default. None is a
    # substring of another, so the "embeds project_name" check is meaningful.
    PERMUTATIONS = ["acme", "gco-staging", "p1team", "team-b"]

    @pytest.mark.parametrize("project_name", PERMUTATIONS)
    def test_every_unique_name_embeds_project_name(self, project_name: str) -> None:
        names = _unique_names(project_name)
        assert names, "expected at least one must-be-unique physical name in the synth"
        offenders = [n for n in names if project_name not in n]
        assert not offenders, (
            f"these unique names are not scoped to project_name={project_name!r} and "
            f"would collide with another deployment in the same account+region: {offenders}"
        )

    @pytest.mark.parametrize("other", ["acme", "gco-staging", "p1team"])
    def test_two_deployments_share_no_unique_names(self, other: str) -> None:
        gco_names = _unique_names("gco")
        other_names = _unique_names(other)
        overlap = gco_names & other_names
        assert not overlap, (
            f"project_name='gco' and project_name={other!r} share these physical "
            f"names (collision in the same account+region): {sorted(overlap)}"
        )
