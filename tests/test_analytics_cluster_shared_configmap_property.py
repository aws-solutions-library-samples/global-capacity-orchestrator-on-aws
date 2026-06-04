"""
Property-based test — the cluster-shared ConfigMap is always present.

*For any* ``analytics_environment.enabled ∈ {true, false}`` **and** for every
region in ``cdk.json.deployment_regions.regional``, the synthesized regional
CloudFormation template SHALL contain the ``gco-cluster-shared-bucket``
ConfigMap (embedded in the kubectl-applier ``CustomResource``'s
``ImageReplacements`` property, keys ``{{CLUSTER_SHARED_BUCKET}}`` /
``{{CLUSTER_SHARED_BUCKET_ARN}}`` / ``{{CLUSTER_SHARED_BUCKET_REGION}}``) with
non-empty, structurally valid values.

Since the three values are CDK tokens at synth time (they resolve from the
cross-region ``AwsCustomResource`` that reads the ``/gco/cluster-shared-bucket/*``
SSM parameters), the property asserts *structure* (non-empty string or token
dict); exact ARN/name/region values are not asserted — they resolve at deploy
time, not synth time.

A secondary direct Hypothesis-driven unit property exercises the pure helper
``gco.stacks.regional_stack._compute_kubectl_cluster_shared_replacements``
with random text values, asserting the returned dict always has the three
keys with exact value round-trips.
"""

from __future__ import annotations

from functools import cache
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gco.stacks.regional_stack import (
    GCORegionalStack,
    SharedBucketIdentity,
    _compute_kubectl_cluster_shared_replacements,
)

# Reuse the battle-tested MockConfigLoader + helm-installer patch pattern from
# tests/test_regional_stack.py so the property test does not have to
# re-implement a synth fixture. We import MockConfigLoader by name and pull
# the helm-installer mock staticmethod off a module-level reference; we do
# NOT import the existing `TestRegionalStackSynthesis` class under its
# canonical name, because pytest would re-collect it as part of this module.
from tests.test_regional_stack import (
    MockConfigLoader,
)
from tests.test_regional_stack import TestRegionalStackSynthesis as _RegionalStackSynthesisFixtures

# Regions the regional_stack fixture is known to support under MockConfigLoader.
_CANDIDATE_REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "ap-southeast-1",
]


class _RegionalMockConfig(MockConfigLoader):
    """Mock ConfigLoader whose ``get_regions`` / ``get_cluster_config`` return
    a Hypothesis-selected region.

    The base ``MockConfigLoader`` hard-codes ``"us-east-1"``; we override only
    the two methods the regional stack actually consults at synth time so the
    fixture continues to work for every other stack it gets plugged into.
    """

    def __init__(self, app: cdk.App | None, region: str) -> None:
        super().__init__(app, fsx_enabled=False)
        self._region = region

    def get_regions(self) -> list[str]:
        return [self._region]

    def get_cluster_config(self, region: str) -> Any:
        from gco.models import ClusterConfig

        return ClusterConfig(
            region=region,
            cluster_name=f"gco-test-{region}",
            kubernetes_version="1.36",
            addons=["metrics-server"],
            resource_thresholds=self.get_resource_thresholds(),
        )


def _synth_regional(analytics_enabled: bool, region: str, logical_name: str) -> assertions.Template:
    """Synthesize a regional stack for the given analytics toggle + region.

    Mirrors the Docker + helm-installer patching pattern from
    ``tests/test_regional_stack.py`` so no real Docker daemon is required
    in the hot property-test loop.
    """
    context = {
        "analytics_environment": {
            "enabled": analytics_enabled,
            "hyperpod": {"enabled": False},
            "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
            "efs": {"removal_policy": "destroy"},
            "studio": {"user_profile_name_prefix": None},
        },
    }
    app = cdk.App(context=context)
    config = _RegionalMockConfig(app, region=region)

    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            _RegionalStackSynthesisFixtures._mock_helm_installer,
        ),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = f"123456789012.dkr.ecr.{region}.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image

        stack = GCORegionalStack(
            app,
            logical_name,
            config=config,
            region=region,
            auth_secret_arn=f"arn:aws:secretsmanager:{region}:123456789012:secret:test-secret",  # nosec B106
            env=cdk.Environment(account="123456789012", region=region),
        )
        return assertions.Template.from_stack(stack)


@cache
def _cached_image_replacements(analytics_enabled: bool, region: str) -> tuple[tuple[str, str], ...]:
    """Return the ``ImageReplacements`` for a given ``(enabled, region)``
    as a frozen tuple of ``(key, json_value)`` pairs.

    Caching is safe because the template output is a pure function of the
    inputs here — the synth fixture has no global side effects. The cache
    keeps the property test under its per-example deadline by reusing
    synth output across the (small, finite) strategy space of
    ``2 × len(_CANDIDATE_REGIONS) = 8`` combinations.

    Returned as a tuple so it is hashable — callers materialize it back to
    a dict per-example.
    """
    import json as _json

    template = _synth_regional(
        analytics_enabled=analytics_enabled,
        region=region,
        logical_name=f"cp5-cache-{'on' if analytics_enabled else 'off'}-{region}",
    )
    replacements = _extract_image_replacements(template)
    # Serialize dict values that are CloudFormation intrinsic dicts so the
    # cache entry is hashable. Strings pass through unchanged.
    return tuple(
        (k, _json.dumps(v, sort_keys=True) if isinstance(v, dict) else v)
        for k, v in replacements.items()
    )


def _extract_image_replacements(
    template: assertions.Template,
) -> dict[str, Any]:
    """Return the ``KubectlApplyManifests`` CustomResource's
    ``ImageReplacements`` property as a flat dict.

    The custom resource synthesizes with type
    ``AWS::CloudFormation::CustomResource`` and logical id
    ``KubectlApplyManifests``.
    """
    resources = template.to_json().get("Resources", {})
    kubectl = resources.get("KubectlApplyManifests")
    assert kubectl is not None, (
        "Expected a KubectlApplyManifests CustomResource in the regional "
        "template. Present logical ids starting with 'KubectlApply': "
        f"{sorted(k for k in resources if k.startswith('KubectlApply'))}"
    )
    replacements: dict[str, Any] = kubectl.get("Properties", {}).get("ImageReplacements", {})
    return replacements


def _assert_replacement_value_is_structurally_valid(key: str, value: Any) -> None:
    """Assert ``value`` is a non-empty string or a non-empty token dict.

    At synth time the three ``{{CLUSTER_SHARED_BUCKET*}}`` values are CDK
    tokens — CloudFormation intrinsic functions like ``Fn::GetAtt`` wrapped
    in dicts. The property asserts shape only:

    - A string must be non-empty.
    - A dict must be non-empty and, if it has an ``Fn::GetAtt`` / ``Ref`` /
      ``Fn::Join`` key, the nested value must also be non-empty.

    Exact ARN / name / region values are not asserted because the tokens
    resolve at deploy time, not synth time.
    """
    assert value not in (None, "", {}), (
        f"ImageReplacements[{key!r}] must be non-empty (string or token dict). Got: {value!r}"
    )
    if isinstance(value, str):
        assert value, f"ImageReplacements[{key!r}] is a string but empty. Got: {value!r}"
    elif isinstance(value, dict):
        # Most commonly Fn::GetAtt from the cross-region SSM reader. The
        # CDK allow-list of intrinsic function keys at this position.
        assert any(k in value for k in ("Fn::GetAtt", "Ref", "Fn::Join", "Fn::Sub")), (
            f"ImageReplacements[{key!r}] is a dict but contains no "
            f"recognized CloudFormation intrinsic function key. Got: {value!r}"
        )
        # Specifically for the cluster-shared bucket, expect Fn::GetAtt into
        # the AwsCustomResource reading the SSM parameter.
        if "Fn::GetAtt" in value:
            getatt = value["Fn::GetAtt"]
            assert isinstance(getatt, list) and len(getatt) == 2, (
                f"Fn::GetAtt in ImageReplacements[{key!r}] should be a "
                f"2-element list. Got: {getatt!r}"
            )
            logical_id, attr = getatt
            assert isinstance(logical_id, str) and logical_id, (
                f"Fn::GetAtt target logical id must be a non-empty string; got {logical_id!r}"
            )
            assert isinstance(attr, str) and attr, (
                f"Fn::GetAtt attribute must be a non-empty string; got {attr!r}"
            )
    else:
        pytest.fail(
            f"ImageReplacements[{key!r}] has unexpected type "
            f"{type(value).__name__}. Expected str or dict. Got: {value!r}"
        )


# -----------------------------------------------------------------------------
# Property — cluster-shared ConfigMap always present (template-level)
# -----------------------------------------------------------------------------


class TestClusterSharedConfigMapAlwaysPresent:
    """The ``gco-cluster-shared-bucket`` ConfigMap replacements are
    always present in every regional template with non-empty structurally
    valid values, for every ``enabled ∈ {true, false}`` and every region
    in ``cdk.json.deployment_regions.regional``.
    """

    @classmethod
    def setup_class(cls) -> None:
        """Warm the ``_cached_image_replacements`` cache for every
        ``(enabled, region)`` combination Hypothesis can produce.

        The underlying regional-stack synth takes ~2 seconds per call (CDK
        JSII boundary + CloudFormation rendering) — warming the full 8-entry
        cache in a ``setup_class`` hook moves that cost outside each
        Hypothesis example, so the per-example deadline of 5000 ms applies
        only to dict materialization + structural assertions.
        """
        for enabled in (False, True):
            for region in _CANDIDATE_REGIONS:
                _cached_image_replacements(enabled, region)

    @settings(
        max_examples=50,
        deadline=10000,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
        ],
    )
    @given(
        enabled=st.booleans(),
        regions=st.lists(
            st.sampled_from(_CANDIDATE_REGIONS),
            min_size=1,
            max_size=3,
            unique=True,
        ),
    )
    def test_cluster_shared_replacements_always_present(
        self, enabled: bool, regions: list[str]
    ) -> None:
        """For any ``enabled`` and any ``regions`` list, every regional
        template contains the three ``{{CLUSTER_SHARED_BUCKET*}}``
        replacements with non-empty structurally valid values.
        """
        import json as _json

        for region in regions:
            cached = _cached_image_replacements(enabled, region)
            # Materialize the cache entry back into a {key: value} dict.
            replacements: dict[str, Any] = {}
            for key, value in cached:
                # Values stored as JSON strings were originally dicts;
                # deserialize them so downstream assertions can introspect
                # the CloudFormation intrinsic function shape.
                if isinstance(value, str) and value.startswith("{"):
                    try:
                        replacements[key] = _json.loads(value)
                        continue
                    except ValueError:
                        pass
                replacements[key] = value

            for key in (
                "{{CLUSTER_SHARED_BUCKET}}",
                "{{CLUSTER_SHARED_BUCKET_ARN}}",
                "{{CLUSTER_SHARED_BUCKET_REGION}}",
            ):
                assert key in replacements, (
                    f"Region={region!r}, enabled={enabled}: "
                    f"ImageReplacements must contain {key!r} — the "
                    f"gco-cluster-shared-bucket ConfigMap is always-on. "
                    f"Present keys: {sorted(replacements)}"
                )
                _assert_replacement_value_is_structurally_valid(key, replacements[key])


# -----------------------------------------------------------------------------
# Direct Hypothesis property on the pure helper
# -----------------------------------------------------------------------------


# Arbitrary non-empty text values matching the shape of CDK's resolved tokens.
# Deliberately unconstrained beyond "non-empty string" — the helper is a pure
# mapping, so any text input should round-trip to the three output keys.
_shared_text = st.text(min_size=1, max_size=128)


class TestComputeKubectlClusterSharedReplacementsRoundTrip:
    """Direct property test on the pure helper
    ``_compute_kubectl_cluster_shared_replacements``.

    The helper is a trivial mapping from a ``SharedBucketIdentity`` to a
    three-key dict. This property asserts that for arbitrary
    ``SharedBucketIdentity(name, arn, region)`` inputs the returned dict
    always contains exactly the three ``{{CLUSTER_SHARED_BUCKET*}}`` keys
    and each value round-trips the input byte-for-byte.
    """

    @settings(
        max_examples=50,
        deadline=10000,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
        ],
    )
    @given(
        name=_shared_text,
        arn=_shared_text,
        region=_shared_text,
    )
    def test_round_trip(self, name: str, arn: str, region: str) -> None:
        """Every field round-trips exactly into the expected key."""
        identity = SharedBucketIdentity(name=name, arn=arn, region=region)
        result = _compute_kubectl_cluster_shared_replacements(identity)

        assert set(result.keys()) == {
            "{{CLUSTER_SHARED_BUCKET}}",
            "{{CLUSTER_SHARED_BUCKET_ARN}}",
            "{{CLUSTER_SHARED_BUCKET_REGION}}",
        }, (
            "Helper must return exactly the three CLUSTER_SHARED_BUCKET* "
            f"keys — no more, no less. Got: {sorted(result)}"
        )
        assert result["{{CLUSTER_SHARED_BUCKET}}"] == name
        assert result["{{CLUSTER_SHARED_BUCKET_ARN}}"] == arn
        assert result["{{CLUSTER_SHARED_BUCKET_REGION}}"] == region
