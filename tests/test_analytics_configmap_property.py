"""Property-based test for the SageMaker-grant-toggle invariant.

*For any* ``analytics_environment.enabled ∈ {true, false}``, the
biconditional holds:

* When ``enabled=False``, the synthesized app contains **no**
  ``gco-analytics`` stack (the analytics stack is never instantiated in
  ``app.py`` / :func:`tests._analytics_cdk_overlays.synth_all_stacks`).
  Trivially no SageMaker execution role exists, so the cluster-shared
  grant cannot be present.

* When ``enabled=True``, the synthesized ``gco-analytics`` template
  contains:

  1. An ``AWS::IAM::Policy`` attached to ``SageMaker_Execution_Role``
     whose statement set includes S3 RW actions
     (``s3:GetObject|PutObject|DeleteObject|ListBucket|GetBucketLocation``)
     on a resource token referencing the cross-region SSM reader
     ``ReadClusterSharedBucketArn.Parameter.Value``; and
  2. A KMS ``Decrypt`` / ``GenerateDataKey`` statement with a
     ``kms:ViaService`` condition scoping the grant to the
     ``s3.<global-region>.<AWS::URLSuffix>`` service.

The ``gco-cluster-shared-bucket`` ConfigMap itself is covered by
:mod:`tests.test_analytics_cluster_shared_configmap_property` — it's
always present regardless of the analytics toggle because
``Cluster_Shared_Bucket`` lives in ``GCOGlobalStack``, not
``GCOAnalyticsStack``.

## Runtime budget

``max_examples=20, deadline=20000`` keeps the test under ~2 min even
without caching. With :func:`functools.cache` keyed on ``enabled``
(cardinality 2) the hot loop reuses one cached synth per toggle value
and completes in ~15 s.
"""

from __future__ import annotations

import functools
import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests._analytics_cdk_overlays import build_overlay, synth_all_stacks
from tests._analytics_strategies import sagemaker_grant_toggle_fixtures
from tests._analytics_template_inspectors import collect_sagemaker_role_statements

# ---------------------------------------------------------------------------
# Cached synth keyed on ``enabled`` only.
# ---------------------------------------------------------------------------

# A deterministic single-region fixture is enough here — the SageMaker
# role lives on the analytics stack whose region is derived from
# ``deployment_regions.api_gateway`` (baseline cdk.json = ``us-east-2``).
# Varying the regional-region list doesn't change anything on the
# analytics template side.
_REGIONS_FIXED: list[str] = ["us-east-1"]


@functools.cache
def _cached_analytics_statements(enabled: bool) -> tuple[str, ...]:
    """Return the JSON-serialized SageMaker role statements for the given
    ``enabled`` value, as a hashable tuple.

    When ``enabled=False`` the analytics stack is never instantiated, so
    the tuple is empty. When ``enabled=True`` every statement on the
    SageMaker execution role is serialized with sorted keys for stable
    output.
    """
    overlay = build_overlay(enabled, False, _REGIONS_FIXED)
    templates = synth_all_stacks(overlay)
    analytics = templates.get("gco-analytics")
    if analytics is None:
        return ()
    statements = collect_sagemaker_role_statements(analytics)
    return tuple(json.dumps(st, sort_keys=True) for st in statements)


@functools.cache
def _cached_analytics_present(enabled: bool) -> bool:
    """Return True iff ``gco-analytics`` appears in the synthesized app
    for the given ``enabled`` value."""
    overlay = build_overlay(enabled, False, _REGIONS_FIXED)
    templates = synth_all_stacks(overlay)
    return "gco-analytics" in templates


def _deserialize_statements(
    frozen: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Rebuild the statement list from the JSON-serialized cache entry."""
    return [json.loads(s) for s in frozen]


# ---------------------------------------------------------------------------
# Predicates — each one tests one half of the biconditional.
# ---------------------------------------------------------------------------

_CLUSTER_SHARED_TOKEN_LOGICAL_ID = "ReadClusterSharedBucketArn"

_REQUIRED_S3_ACTIONS = frozenset(
    {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
    }
)

_REQUIRED_KMS_ACTIONS = frozenset({"kms:Decrypt", "kms:GenerateDataKey"})


def _as_list(value: Any) -> list[Any]:
    """Return ``value`` as a list. Shared helper for IAM fields that may
    be a single value or a list of values."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _flat_repr(value: Any) -> str:
    """Return a stable JSON string for dict/list values so prefix search
    works. Strings pass through unchanged."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _resource_references_cluster_shared(resource: Any) -> bool:
    """True if ``resource`` is a token that resolves to the cross-region
    SSM reader for the cluster-shared bucket ARN."""
    return _CLUSTER_SHARED_TOKEN_LOGICAL_ID in _flat_repr(resource)


def _statement_has_cluster_shared_s3_grant(statement: dict[str, Any]) -> bool:
    """True if ``statement`` is an S3 RW grant on the cluster-shared bucket.

    Matches both the inline policy variant (statement's ``Resource`` is a
    list containing both ``<arn>`` and ``<arn>/*`` tokens) and any future
    refactor that splits the two resources across sibling statements.
    """
    actions = set(_as_list(statement.get("Action")))
    if not _REQUIRED_S3_ACTIONS.issubset(actions):
        return False
    resources = _as_list(statement.get("Resource"))
    return any(_resource_references_cluster_shared(r) for r in resources)


def _statement_has_kms_via_s3_grant(
    statement: dict[str, Any],
) -> bool:
    """True if ``statement`` is a ``kms:Decrypt`` + ``kms:GenerateDataKey``
    grant with a ``kms:ViaService`` condition keyed on a partition-aware
    ``s3.<region>.<AWS::URLSuffix>`` endpoint.

    The condition's region component is not pinned. CloudFormation token
    objects are accepted only when they contain ``AWS::URLSuffix``; concrete
    strings remain accepted for direct predicate unit tests.
    """
    actions = set(_as_list(statement.get("Action")))
    if not _REQUIRED_KMS_ACTIONS.issubset(actions):
        return False
    conditions = statement.get("Condition") or {}
    if not isinstance(conditions, dict):
        return False
    string_equals = conditions.get("StringEquals") or {}
    if not isinstance(string_equals, dict):
        return False
    via = string_equals.get("kms:ViaService")
    if isinstance(via, str):
        return via.startswith("s3.") and "." in via.removeprefix("s3.")
    if not isinstance(via, (dict, list)):
        return False
    rendered = _flat_repr(via)
    return "s3." in rendered and "AWS::URLSuffix" in rendered


def _has_cluster_shared_grant(statements: list[dict[str, Any]]) -> bool:
    """True if the role's statements contain both halves of the
    cluster-shared grant (S3 RW + KMS-via-S3 conditional)."""
    return any(_statement_has_cluster_shared_s3_grant(s) for s in statements) and any(
        _statement_has_kms_via_s3_grant(s) for s in statements
    )


# ---------------------------------------------------------------------------
# Biconditional property.
# ---------------------------------------------------------------------------


class TestSagemakerGrantToggleInvariant:
    """The SageMaker cluster-shared grant exists if and only if
    ``analytics_environment.enabled`` is true.
    """

    @classmethod
    def setup_class(cls) -> None:
        """Pre-warm the two cache entries the hot loop draws from."""
        for enabled in (False, True):
            _cached_analytics_statements(enabled)
            _cached_analytics_present(enabled)

    @settings(
        max_examples=20,
        deadline=20000,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
            HealthCheck.data_too_large,
        ],
    )
    @given(enabled=sagemaker_grant_toggle_fixtures)
    def test_biconditional(self, enabled: bool) -> None:
        """The SageMaker cluster-shared grant is present iff
        ``analytics_environment.enabled`` is true.
        """
        analytics_present = _cached_analytics_present(enabled)
        frozen = _cached_analytics_statements(enabled)
        statements = _deserialize_statements(frozen)

        if not enabled:
            assert not analytics_present, (
                "When analytics is disabled, gco-analytics must not be "
                "synthesized — but it was. The full analytics stack "
                "must be absent under enabled=False."
            )
            assert not statements, (
                "When analytics is disabled, the SageMaker execution "
                "role must not exist. Got statements: "
                f"{statements!r}"
            )
            assert not _has_cluster_shared_grant(statements), (
                "When analytics is disabled, no cluster-shared grant "
                "may be present. Got: "
                f"{statements!r}"
            )
            return

        # enabled=True branch — the biconditional's positive direction.
        assert analytics_present, (
            "When analytics is enabled, gco-analytics must be synthesized — but it was absent."
        )
        assert statements, (
            "When analytics is enabled, the SageMaker execution role "
            "must exist with at least one statement. Got none."
        )
        assert _has_cluster_shared_grant(statements), (
            "When analytics is enabled, the SageMaker execution role "
            "must carry both halves of the cluster-shared grant "
            "(S3 RW on a token referencing "
            f"{_CLUSTER_SHARED_TOKEN_LOGICAL_ID} and KMS "
            "Decrypt/GenerateDataKey with a kms:ViaService=s3.* "
            "condition). Statements observed: "
            f"{statements!r}"
        )


# ---------------------------------------------------------------------------
# Complementary bi-directional example test.
# ---------------------------------------------------------------------------


class TestSagemakerGrantBidirectional:
    """Bi-directional check: toggle ``false → true`` and back again and
    assert the cluster-shared grant tracks the toggle on every pass.

    This is the non-Hypothesis counterpart to
    :class:`TestSagemakerGrantToggleInvariant` — it exercises the same
    biconditional with concrete fixtures and serves as a fast smoke
    signal when the full property test is skipped under ``-k``.
    """

    def test_disabled_then_enabled(self) -> None:
        off_statements = _deserialize_statements(_cached_analytics_statements(False))
        on_statements = _deserialize_statements(_cached_analytics_statements(True))

        assert not _has_cluster_shared_grant(off_statements), (
            "enabled=False must not produce a cluster-shared grant."
        )
        assert _has_cluster_shared_grant(on_statements), (
            "enabled=True must produce a cluster-shared grant."
        )

    def test_enabled_then_disabled(self) -> None:
        # Exercising the same cached entries in reverse order — asserts
        # the cache produces stable results regardless of access order.
        on_statements = _deserialize_statements(_cached_analytics_statements(True))
        off_statements = _deserialize_statements(_cached_analytics_statements(False))

        assert _has_cluster_shared_grant(on_statements)
        assert not _has_cluster_shared_grant(off_statements)


# ---------------------------------------------------------------------------
# Unit tests on the predicates — exercised across arbitrary noise.
# ---------------------------------------------------------------------------


class TestPredicatesTotality:
    """Every predicate must be total: arbitrary malformed statements
    can't raise. Only totality is asserted; the classification result
    is not."""

    def test_kms_via_s3_grant_predicate_accepts_url_suffix_token(self) -> None:
        statement = {
            "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
            "Condition": {
                "StringEquals": {
                    "kms:ViaService": {
                        "Fn::Join": [
                            "",
                            [
                                "s3.us-east-2.",
                                {"Ref": "AWS::URLSuffix"},
                            ],
                        ]
                    }
                }
            },
        }
        assert _statement_has_kms_via_s3_grant(statement)

    @settings(max_examples=25, deadline=None)
    @given(
        noise=st.recursive(
            st.one_of(st.text(), st.booleans(), st.integers(), st.none()),
            lambda children: st.one_of(
                st.lists(children, max_size=3),
                st.dictionaries(st.text(max_size=8), children, max_size=3),
            ),
            max_leaves=5,
        )
    )
    def test_cluster_shared_s3_grant_predicate_is_total(self, noise: Any) -> None:
        if not isinstance(noise, dict):
            return
        # pass/fail is irrelevant; we assert no exceptions
        _ = _statement_has_cluster_shared_s3_grant(noise)

    @settings(max_examples=25, deadline=None)
    @given(
        noise=st.recursive(
            st.one_of(st.text(), st.booleans(), st.integers(), st.none()),
            lambda children: st.one_of(
                st.lists(children, max_size=3),
                st.dictionaries(st.text(max_size=8), children, max_size=3),
            ),
            max_leaves=5,
        )
    )
    def test_kms_via_s3_grant_predicate_is_total(self, noise: Any) -> None:
        if not isinstance(noise, dict):
            return
        _ = _statement_has_kms_via_s3_grant(noise)
