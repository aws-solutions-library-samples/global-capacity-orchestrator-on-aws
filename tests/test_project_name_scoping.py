"""Project-name scoping guardrails (issue #139).

Two GCO deployments must be able to coexist in the **same AWS account** — in the
same region(s) *or* different regions — by using a different ``project_name``
alone. That only holds if every physical resource name that is unique per
account+region (or globally) derives from ``project_name`` (and, for resources
that can exist once per region within a single deployment, from the region).

These tests are the executable contract for that guarantee:

* :class:`TestProjectNameValidation` — ``ConfigLoader`` rejects malformed
  ``project_name`` up front (fail at synth, not mid-deploy).
* :class:`TestBackwardCompatibility` — the default ``project_name="gco"`` still
  renders the exact pre-#139 physical names (no resource replacement on upgrade).
* :class:`TestResourceTypeClassification` — **airtight regression guard**: every
  resource type that carries a concrete physical name in a full synth is
  explicitly classified as either collision-prone (must embed ``project_name``)
  or documented-safe. A newly introduced named resource type fails this test
  until a human classifies it, so the coverage below can never silently rot.
* :class:`TestNoCollisionsAcrossProjectNames` — for several ``project_name``
  permutations, every collision-prone physical name embeds the project name, and
  two deployments share **zero** collision-prone names — both when they target
  the **same** regions and when they target **different** regions.
* :class:`TestMultiRegionDeployment` — a single deployment spanning multiple
  regions does not self-collide on globally-unique names, and a deployment whose
  global region overlaps a regional region does not collide across its co-located
  stacks.

Synths are full-app (``app.py`` topology) with the analytics environment enabled
so analytics-stack names are covered too. Docker image assets (regional tier)
are mocked by ``synth_all_stacks`` so this needs no Docker. Results are cached
per (project, regions) as small name-sets to keep memory bounded.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError
from tests._analytics_cdk_overlays import build_overlay, synth_all_stacks

# ---------------------------------------------------------------------------
# Physical-name classification
# ---------------------------------------------------------------------------
# CloudFormation resource types whose physical name is unique per account+region
# (or globally — S3 bucket names and Cognito domain prefixes are global), mapped
# to the Properties key that holds that name. Every value collected from these
# MUST embed ``project_name`` or two deployments collide. Derived from a full
# multi-region + analytics synth audit; ``TestResourceTypeClassification`` fails
# if a synth ever produces a named type absent from this map (and the safe set
# below), so this list cannot silently fall behind the code.
_COLLISION_PRONE_NAME_PROPS: dict[str, tuple[str, ...]] = {
    "AWS::S3::Bucket": ("BucketName",),
    "AWS::SSM::Parameter": ("Name",),
    "AWS::SecretsManager::Secret": ("Name",),
    "AWS::WAFv2::WebACL": ("Name",),
    "AWS::Logs::LogGroup": ("LogGroupName",),
    "AWS::DynamoDB::Table": ("TableName",),
    "AWS::IAM::Role": ("RoleName",),
    "AWS::IAM::ManagedPolicy": ("ManagedPolicyName",),
    "AWS::Lambda::Function": ("FunctionName",),
    "AWS::SQS::Queue": ("QueueName",),
    "AWS::SNS::Topic": ("TopicName",),
    "AWS::ECR::Repository": ("RepositoryName",),
    "AWS::KMS::Alias": ("AliasName",),
    "AWS::EKS::Cluster": ("Name",),
    "AWS::EMRServerless::Application": ("Name",),
    "AWS::SageMaker::Domain": ("DomainName",),
    "AWS::Cognito::UserPoolDomain": ("Domain",),
    "AWS::CloudWatch::Alarm": ("AlarmName",),
    "AWS::CloudWatch::CompositeAlarm": ("AlarmName",),
    "AWS::CloudWatch::Dashboard": ("DashboardName",),
    "AWS::ElasticLoadBalancingV2::LoadBalancer": ("Name",),
    "AWS::ElasticLoadBalancingV2::TargetGroup": ("Name",),
    "AWS::StepFunctions::StateMachine": ("StateMachineName",),
    "AWS::Backup::BackupVault": ("BackupVaultName",),
    "AWS::Events::Rule": ("Name",),
}

# (Type, property) pairs that carry a name-like string but are NOT
# account+region-unique, so sharing the value across deployments cannot collide.
# Each is justified; add here (not to the map above) only with a reason.
_NON_COLLISION_NAME_PROPS: dict[tuple[str, str], str] = {
    ("AWS::ApiGateway::RestApi", "Name"): "REST API names need not be unique (#139).",
    ("AWS::ApiGateway::Authorizer", "Name"): "Scoped within its REST API.",
    ("AWS::ApiGateway::RequestValidator", "Name"): "Scoped within its REST API.",
    ("AWS::ApiGateway::Stage", "StageName"): "Scoped within its REST API (e.g. 'prod').",
    (
        "AWS::EKS::Addon",
        "AddonName",
    ): "AWS addon id, scoped within the cluster (e.g. 'metrics-server').",
    (
        "AWS::EC2::VPCEndpoint",
        "ServiceName",
    ): "AWS service id (com.amazonaws.<region>.<svc>), not ours.",
    ("AWS::SNS::Topic", "DisplayName"): "Human label, not the physical TopicName; not unique.",
    ("AWS::IAM::Policy", "PolicyName"): "Inline policy, scoped to its (project-scoped) principal.",
    ("AWS::GlobalAccelerator::Accelerator", "Name"): (
        "Global Accelerator names are not uniqueness-constrained; the value is "
        "operator config (global_accelerator.name in cdk.json), not a #139 name."
    ),
    ("AWS::CloudFormation::CustomResource", "ProjectName"): (
        "Input property passed to a custom resource; its value IS the project_name, "
        "not a resource identifier."
    ),
    ("AWS::CloudWatch::Alarm", "MetricName"): (
        "The CloudWatch metric the alarm watches (e.g. '5XXError'), not the alarm's "
        "physical name; scoped to its metric namespace."
    ),
    ("AWS::EC2::EIP", "Domain"): "EIP domain type ('vpc'/'standard'), not a name.",
    ("AWS::EC2::SecurityGroup", "GroupName"): (
        "Security group names are unique per VPC, not per account — each deployment "
        "(and region) has its own VPC — so they cannot collide across deployments. "
        "They are also project+region-scoped in this codebase as defense-in-depth."
    ),
}

# Globally-unique namespaces (a name here must be unique across the *entire*
# account, so within one multi-region deployment no two stacks may share one).
_GLOBAL_NAMESPACE_TYPES = frozenset(
    {"AWS::S3::Bucket", "AWS::IAM::Role", "AWS::IAM::ManagedPolicy", "AWS::Cognito::UserPoolDomain"}
)

_ACCOUNT = "111122223333"


def _render(value: Any) -> str | None:
    """Render a name value to a concrete string, resolving AccountId/Region tokens.

    Account-agnostic synths leave the account as a ``{"Ref": "AWS::AccountId"}``
    token; region is a literal because the synth pins each stack's region. Returns
    ``None`` for values that don't resolve to a concrete string (pure tokens we
    don't model), which callers treat as "not a static physical name".
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "Ref" in value:
            return {
                "AWS::AccountId": _ACCOUNT,
                "AWS::Region": "AWS::Region",
                "AWS::Partition": "aws",
                "AWS::URLSuffix": "amazonaws.com",
            }.get(value["Ref"])
        if "Fn::Join" in value:
            sep, parts = value["Fn::Join"]
            rendered = [_render(p) for p in parts]
            if all(isinstance(p, str) for p in rendered):
                return sep.join(rendered)  # type: ignore[arg-type]
        if "Fn::Sub" in value:
            body = value["Fn::Sub"]
            return body if isinstance(body, str) else _render(body[0])
    return None


def _is_name_like_key(key: str) -> bool:
    """Top-level Properties keys that hold a physical-name-like string."""
    return key.endswith("Name") or key == "Domain"


def _discover_named_props(templates: dict[str, dict[str, Any]]) -> set[tuple[str, str]]:
    """Every (resource type, name-like property) that holds a concrete string."""
    found: set[tuple[str, str]] = set()
    for template in templates.values():
        for resource in (template.get("Resources") or {}).values():
            rtype = resource.get("Type", "")
            for key, raw in (resource.get("Properties") or {}).items():
                if _is_name_like_key(key) and isinstance(_render(raw), str):
                    found.add((rtype, key))
    return found


def _collision_prone_names(templates: dict[str, dict[str, Any]]) -> set[str]:
    """All collision-prone physical names (rendered) + CFN export names.

    Export names are prefixed ``EXPORT:`` so they can't accidentally match a
    resource name. Auto-generated cross-stack exports embed the stack name
    (``<project>-<tier>``) and so are already project-scoped.
    """
    names: set[str] = set()
    for template in templates.values():
        for resource in (template.get("Resources") or {}).values():
            for prop in _COLLISION_PRONE_NAME_PROPS.get(resource.get("Type", ""), ()):
                rendered = _render((resource.get("Properties") or {}).get(prop))
                if isinstance(rendered, str):
                    names.add(rendered)
        for output in (template.get("Outputs") or {}).values():
            export = _render((output.get("Export") or {}).get("Name"))
            if isinstance(export, str):
                names.add("EXPORT:" + export)
    return names


def _names_by_type(templates: dict[str, dict[str, Any]], types: frozenset[str]) -> list[str]:
    """Rendered physical names for the given resource types (list keeps duplicates)."""
    out: list[str] = []
    for template in templates.values():
        for resource in (template.get("Resources") or {}).values():
            rtype = resource.get("Type", "")
            if rtype not in types:
                continue
            for prop in _COLLISION_PRONE_NAME_PROPS.get(rtype, ()):
                rendered = _render((resource.get("Properties") or {}).get(prop))
                if isinstance(rendered, str):
                    out.append(rendered)
    return out


# ---------------------------------------------------------------------------
# Cached synth, keyed by (project, global_region, regional-tuple).
# ---------------------------------------------------------------------------
_TEMPLATE_CACHE: dict[tuple[str, str, tuple[str, ...]], dict[str, dict[str, Any]]] = {}


def _synth(
    project_name: str,
    *,
    global_region: str = "us-east-2",
    regional: tuple[str, ...] = ("us-east-1",),
) -> dict[str, dict[str, Any]]:
    """Full-app synth for a (project, regions) topology with analytics enabled."""
    key = (project_name, global_region, regional)
    if key not in _TEMPLATE_CACHE:
        overlay = {
            "project_name": project_name,
            "deployment_regions": {
                "global": global_region,
                "api_gateway": global_region,
                "monitoring": global_region,
                "regional": list(regional),
            },
            "analytics_environment": {
                "enabled": True,
                "hyperpod": {"enabled": False},
                "canvas": {"enabled": False},
                "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
                "efs": {"removal_policy": "destroy"},
                "studio": {"user_profile_name_prefix": None},
            },
        }
        _TEMPLATE_CACHE[key] = synth_all_stacks(overlay)
    return _TEMPLATE_CACHE[key]


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

    @pytest.mark.parametrize("name", ["gco", "gco-staging", "acme", "p1", "team-a-prod", "a" * 31])
    def test_valid_names_accepted(self, valid_cdk_context: dict[str, Any], name: str) -> None:
        assert self._load(valid_cdk_context, name).get_project_name() == name

    @pytest.mark.parametrize(
        "name",
        ["GCO", "gco_staging", "gco.staging", "1gco", "-gco", "g", "a" * 32, "gco staging", ""],
    )
    def test_invalid_names_rejected(self, valid_cdk_context: dict[str, Any], name: str) -> None:
        with pytest.raises(ConfigValidationError):
            self._load(valid_cdk_context, name)


# ---------------------------------------------------------------------------
# Backward compatibility: default "gco" renders the pre-#139 literals
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Default ``project_name="gco"`` must render byte-for-byte the old names."""

    def test_default_gco_renders_legacy_names(self) -> None:
        blob = "\n".join(_collision_prone_names(_synth("gco")))
        expected = [
            "gco-cluster-shared-",
            "/gco/cluster-shared-bucket/name",
            "gco-jobs",
            "gco-inference-endpoints",
            "gco-regional-shared-",
            "/gco/regional-shared-bucket/name",
            "gco/api-gateway-auth-token",
            "gco-api-gateway-waf",
            "/aws/apigateway/gco-global",
            "aws-waf-logs-gco-api-gateway",
            "gco-global-api-endpoint",
            "gco-auth-secret-arn",
            "gco-waf-webacl-arn",
            "gco-analytics-studio-",
            "AmazonSageMaker-gco-analytics-exec-",
            "gco-studio-",
            "gco-spark-",
        ]
        missing = [frag for frag in expected if frag not in blob]
        assert not missing, (
            f"project_name='gco' no longer renders these legacy names (upgrade would "
            f"replace resources): {missing}"
        )


# ---------------------------------------------------------------------------
# Airtight guard: every named resource type is classified
# ---------------------------------------------------------------------------


class TestResourceTypeClassification:
    """Fail if a synth produces a named resource type we haven't classified.

    This is what keeps the collision coverage from silently rotting: add a new
    resource with an explicit physical name and this test fails until the type is
    put in ``_COLLISION_PRONE_NAME_PROPS`` (verified project-scoped by the tests
    below) or ``_NON_COLLISION_NAME_PROPS`` (with a documented reason).
    """

    def test_every_named_resource_type_is_classified(self) -> None:
        # Multi-region + analytics = the widest resource surface the app produces.
        templates = _synth("zzalpha", regional=("us-east-1", "us-west-2"))
        discovered = _discover_named_props(templates)
        classified = {
            (t, prop) for t, props in _COLLISION_PRONE_NAME_PROPS.items() for prop in props
        } | set(_NON_COLLISION_NAME_PROPS)
        unclassified = sorted(discovered - classified)
        assert not unclassified, (
            "Unclassified named resource properties found in the synth. Add each to "
            "_COLLISION_PRONE_NAME_PROPS (if the name must embed project_name to avoid "
            "collision) or _NON_COLLISION_NAME_PROPS (with a reason):\n  "
            + "\n  ".join(f"{t} . {p}" for t, p in unclassified)
        )
        # Sanity floor: the audit saw well over a dozen named types; a near-empty
        # discovery means the synth or the walker silently broke.
        assert len(discovered) >= 12, f"only discovered {len(discovered)} named props"


# ---------------------------------------------------------------------------
# No collisions across project names (same region AND different regions)
# ---------------------------------------------------------------------------


class TestNoCollisionsAcrossProjectNames:
    """Every collision-prone name embeds project_name; distinct deployments share none."""

    PERMUTATIONS = ["acme", "gco-staging", "p1team", "team-b"]

    @pytest.mark.parametrize("project_name", PERMUTATIONS)
    def test_every_unique_name_embeds_project_name(self, project_name: str) -> None:
        names = _collision_prone_names(_synth(project_name, regional=("us-east-1", "us-west-2")))
        assert names, "expected at least one collision-prone physical name"
        # CDK auto-generated physical names (IAM roles, composite alarms, backup
        # vaults) strip hyphens from the stack-name prefix — project "gco-staging"
        # yields e.g. "gcostagingglobalDynamoDBBackupVault<hash>". Accept that
        # hyphen-stripped form too. Uniqueness across projects still holds: the
        # <hash> derives from the full (hyphenated) construct path, which
        # test_hyphen_variant_projects_share_no_names verifies directly.
        scoped_forms = {project_name, project_name.replace("-", "")}
        offenders = sorted(n for n in names if not any(form in n for form in scoped_forms))
        assert not offenders, (
            f"these collision-prone names are not scoped to project_name={project_name!r} and "
            f"would collide with another deployment in the same account: {offenders}"
        )

    def test_two_deployments_share_no_stack_names(self) -> None:
        # CloudFormation stack names are unique per account+region and are the
        # first thing that would collide on a second deploy. app.py names every
        # stack ``<project_name>-<tier>``, so two deployments must share none.
        overlap = sorted(set(_synth("gco")) & set(_synth("acme")))
        assert not overlap, f"project_name='gco' and 'acme' share these stack names: {overlap}"

    def test_ecr_replication_filter_is_project_scoped(self) -> None:
        # The global-stack ECR replication rule must only replicate this
        # deployment's own ``<project>/`` image namespace, so two deployments
        # never cross-replicate each other's repos (#139).
        templates = _synth("acme", regional=("us-east-1", "us-west-2"))
        filters: list[str] = []
        for template in templates.values():
            for resource in (template.get("Resources") or {}).values():
                if resource.get("Type") != "AWS::ECR::ReplicationConfiguration":
                    continue
                config = (resource.get("Properties") or {}).get("ReplicationConfiguration", {})
                for rule in config.get("Rules", []):
                    for filt in rule.get("RepositoryFilters", []):
                        filters.append(filt.get("Filter"))
        assert filters, "expected an ECR replication rule with a repository filter in the synth"
        assert all(f == "acme/" for f in filters), (
            f"ECR replication filters are not scoped to project_name='acme': {filters}"
        )

    @pytest.mark.parametrize("other", ["acme", "gco-staging", "p1team"])
    def test_two_deployments_same_region_share_no_names(self, other: str) -> None:
        overlap = sorted(
            _collision_prone_names(_synth("gco")) & _collision_prone_names(_synth(other))
        )
        assert not overlap, (
            f"project_name='gco' and {other!r} in the SAME regions share these physical "
            f"names (collision): {overlap}"
        )

    def test_hyphen_variant_projects_share_no_names(self) -> None:
        # CDK strips hyphens when it auto-generates physical names, so two project
        # names that differ only by hyphens ("gco-staging" vs "gcostaging") produce
        # the same human-readable prefix on those resources. Confirm the
        # construct-path hash (and the literal hyphen in explicitly-named
        # resources) still disambiguates them so nothing collides.
        overlap = sorted(
            _collision_prone_names(_synth("gco-staging"))
            & _collision_prone_names(_synth("gcostaging"))
        )
        assert not overlap, (
            "hyphen-variant project names 'gco-staging' and 'gcostaging' share these physical "
            f"names (collision): {overlap}"
        )

    def test_two_deployments_different_regions_share_no_names(self) -> None:
        # The user's explicit scenario: same account, DIFFERENT regions, different
        # project names. 'alpha' runs global in us-east-2 / regional us-east-1;
        # 'bravo' runs global in us-west-2 / regional us-west-2.
        alpha = _collision_prone_names(
            _synth("alpha", global_region="us-east-2", regional=("us-east-1",))
        )
        bravo = _collision_prone_names(
            _synth("bravo", global_region="us-west-2", regional=("us-west-2",))
        )
        overlap = sorted(alpha & bravo)
        assert not overlap, (
            "cross-region deployments 'alpha' (us-east-*) and 'bravo' (us-west-2) share these "
            f"physical names (collision in the same account): {overlap}"
        )


# ---------------------------------------------------------------------------
# Single-deployment multi-region / co-located self-collision
# ---------------------------------------------------------------------------


class TestMultiRegionDeployment:
    """One deployment spread across regions must not collide with itself."""

    def test_single_deployment_multi_region_no_global_name_collision(self) -> None:
        # Globally-unique names (S3 buckets, IAM roles, Cognito domain) must be
        # distinct across every stack of a single multi-region deployment — this
        # catches a regional resource whose name forgot its region suffix.
        templates = _synth("multi", regional=("us-east-1", "us-west-2", "eu-west-1"))
        global_names = _names_by_type(templates, _GLOBAL_NAMESPACE_TYPES)
        dupes = sorted({n for n in global_names if global_names.count(n) > 1})
        assert not dupes, (
            "these globally-unique names appear more than once across the deployment's "
            f"stacks (a multi-region self-collision): {dupes}"
        )

    def test_global_and_regional_colocated_no_collision(self) -> None:
        # Edge case: the global tier and a regional stack in the SAME region. Every
        # collision-prone name across all stacks must still be unique.
        templates = _synth("colo", global_region="us-east-1", regional=("us-east-1",))
        names: list[str] = []
        for template in templates.values():
            for resource in (template.get("Resources") or {}).values():
                for prop in _COLLISION_PRONE_NAME_PROPS.get(resource.get("Type", ""), ()):
                    rendered = _render((resource.get("Properties") or {}).get(prop))
                    if isinstance(rendered, str):
                        names.append(rendered)
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, (
            "these collision-prone names collide between the co-located global and regional "
            f"stacks (global region == regional region): {dupes}"
        )


class TestNoHardcodedProjectPrefixInPolicies:
    """Regression guard for #139 leaks a resource-*name* scan can't see.

    ``TestNoCollisionsAcrossProjectNames`` inspects physical-name properties,
    but several #139 leaks hid where that scan never looks:

    * IAM policy **Resource ARNs** — the image-lookup Lambda's
      ``repository/gco/*`` grant and the SageMaker execution role's
      ``gco-jobs-*`` / ``parameter/gco/cluster-shared-bucket/*`` grants. Where
      a policy *and* its cdk-nag suppression were both hardcoded to ``gco``
      they stayed self-consistent, so cdk-nag stayed silent too.
    * The Valkey ElastiCache ``ServerlessCacheName`` — a feature-flagged
      resource (``valkey.enabled`` is ``false`` in the stock cdk.json), so it
      never appeared in the baseline synth the name scan runs against.

    Synthesising the full app under a foreign ``project_name`` (with analytics
    *and* valkey enabled) turns any surviving lowercase ``gco`` literal into an
    unambiguous hardcoded leak. ``_render`` resolves only AWS pseudo-parameters
    and literals — returning ``None`` for logical-id ``Ref``/``Fn::GetAtt`` — so
    CDK logical ids like ``GCOEksCluster`` never produce a false positive.
    """

    _PROJECT = "acme"
    _cache: dict[str, dict[str, Any]] | None = None

    @classmethod
    def _templates(cls) -> dict[str, dict[str, Any]]:
        if cls._cache is None:
            overlay = build_overlay(enabled=True, hyperpod_enabled=False, regions=["us-east-1"])
            overlay["project_name"] = cls._PROJECT
            overlay["valkey"] = {"enabled": True}
            cls._cache = synth_all_stacks(overlay)
        return cls._cache

    @staticmethod
    def _policy_resource_arns(
        templates: dict[str, dict[str, Any]],
    ) -> list[tuple[str, str, str]]:
        """(stack, logical_id, rendered_arn) for every IAM policy Resource entry."""
        out: list[tuple[str, str, str]] = []
        for stack_name, template in templates.items():
            for logical_id, resource in (template.get("Resources") or {}).items():
                rtype = resource.get("Type")
                props = resource.get("Properties") or {}
                docs: list[Any] = []
                if rtype in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
                    docs.append(props.get("PolicyDocument"))
                elif rtype == "AWS::IAM::Role":
                    docs.extend(p.get("PolicyDocument") for p in (props.get("Policies") or []))
                for doc in docs:
                    for stmt in (doc or {}).get("Statement") or []:
                        raw = stmt.get("Resource")
                        values = raw if isinstance(raw, list) else [raw]
                        for value in values:
                            rendered = _render(value)
                            if isinstance(rendered, str):
                                out.append((stack_name, logical_id, rendered))
        return out

    def test_no_iam_policy_resource_arn_hardcodes_stock_project(self) -> None:
        arns = self._policy_resource_arns(self._templates())
        # Non-vacuity guard: the full app has many IAM policies with concrete
        # resource ARNs, so an empty list means _render stopped resolving them
        # (and the offender check below would pass for the wrong reason).
        assert len(arns) > 20, f"expected many resolved IAM policy ARNs, got {len(arns)}"
        offenders = [
            f"{stack}/{logical_id}: {arn}"
            for stack, logical_id, arn in arns
            if "gco" in arn.lower()
        ]
        assert not offenders, (
            "IAM policy Resource ARN(s) hardcode the stock 'gco' project under "
            "project_name='acme' — they must derive from project_name (#139):\n"
            + "\n".join(sorted(offenders))
        )

    def test_valkey_serverless_cache_name_is_project_scoped(self) -> None:
        templates = self._templates()
        names = [
            _render((resource.get("Properties") or {}).get("ServerlessCacheName"))
            for template in templates.values()
            for resource in (template.get("Resources") or {}).values()
            if resource.get("Type") == "AWS::ElastiCache::ServerlessCache"
        ]
        concrete = [n for n in names if isinstance(n, str)]
        assert concrete, "expected a Valkey ElastiCache serverless cache in the synth"
        assert all(n.startswith(f"{self._PROJECT}-") for n in concrete), (
            f"ElastiCache serverless cache name(s) not scoped to project_name='acme': {concrete}"
        )
