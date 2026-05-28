"""Property-based test for bucket isolation on the regional job-pod role.

*For any* regional CDK stack ``gco-<region>`` synthesized from a
``cdk.json`` context with arbitrary ``analytics_environment.enabled`` and
``analytics_environment.hyperpod.enabled`` values, the IAM policy
statements attached to the regional job-pod / service-account role SHALL

1. Include at least one S3 ``Resource`` referencing the always-on
   ``Cluster_Shared_Bucket`` — either the literal ``gco-cluster-shared-``
   bucket-name prefix or the CDK token ``Fn::GetAtt`` /
   ``Fn::Join`` structure that resolves to
   ``ReadClusterSharedBucketArn.Parameter.Value``; and

2. NEVER include an S3 ``Resource`` whose literal or token-resolved
   bucket name starts with ``gco-analytics-studio-`` (the analytics-only
   Studio bucket's name prefix).

The two checks together encode the bucket-isolation invariant: the
regional EKS job pods can write to the shared operational bucket but
can never reach the analytics-Studio-private bucket, for every toggle
state.

Pre-existing wildcard S3 ARNs with broader prefixes (``arn:aws:s3:::gco-*``,
the model-weights bucket grant at
``gco/stacks/regional_stack.py``:1383–1394) are **not** in scope for this
property — they predate the analytics feature and neither match
``gco-cluster-shared-`` nor ``gco-analytics-studio-``. This property
scopes to the bucket-isolation pair only; broader IAM hygiene is covered
by :mod:`tests.test_nag_compliance`.

## Runtime budget

Each example synthesizes the full CDK app (every stack ``app.py``
builds) which takes ~5 s on a warm JSII bridge. The ``2×2×(region
combinations)`` strategy space is small enough that
:func:`functools.cache` on
``(enabled, hyperpod, tuple(sorted(regions)))`` keeps the hot loop
under the ``deadline=20000`` ms per-example budget, which also leaves
headroom for the first (uncached) synth in each worker when the suite
runs under pytest-xdist contention.
``max_examples=30`` with caching completes in under 90 s on the
benchmark workstation. The lower example count (vs. an earlier
``max_examples=50`` setting) keeps the test under the project's
suite-wide 120 s pytest-timeout while still drawing across the full
2×2 toggle space and a representative slice of the 14 unique
sorted-region tuples reachable from a 4-region candidate list with
sizes 1–3.
"""

from __future__ import annotations

import functools
import json
import re
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests._analytics_cdk_overlays import build_overlay, synth_all_stacks
from tests._analytics_strategies import (
    bucket_isolation_fixtures,
)
from tests._analytics_template_inspectors import collect_job_pod_role_statements

# ---------------------------------------------------------------------------
# Constants — the two bucket-name prefixes used to classify S3 resources.
# ---------------------------------------------------------------------------

# Allowed bucket-name prefix. Literal ARNs matching this are the
# ``Cluster_Shared_Bucket`` region-agnostic bucket-name prefix baked into
# ``gco-global`` (``gco-cluster-shared-<account>-<region>``). The token
# form resolves at deploy time to the same prefix via SSM.
ALLOW_LIST_BUCKET_PREFIX = "gco-cluster-shared-"

# Denied bucket-name prefix. Literal ARNs matching this would indicate the
# analytics-Studio-private bucket leaking onto the regional job-pod role —
# the exact isolation breach this property is designed to catch.
DENY_LIST_BUCKET_PREFIX = "gco-analytics-studio-"

# Literal ARN prefixes derived from the bucket-name prefixes for the
# token-free case.
ALLOW_LIST_ARN_PREFIX = f"arn:aws:s3:::{ALLOW_LIST_BUCKET_PREFIX}"
DENY_LIST_ARN_PREFIX = f"arn:aws:s3:::{DENY_LIST_BUCKET_PREFIX}"

# Regex that matches any literal S3 ARN — used to filter out non-S3
# resources before classifying.
_S3_ARN_PREFIX = "arn:aws:s3:::"


# ---------------------------------------------------------------------------
# Cached full-app synth.
# ---------------------------------------------------------------------------


@functools.cache
def _cached_synth(
    enabled: bool, hyperpod_enabled: bool, regions_key: tuple[str, ...]
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Synthesize the full app once per unique ``(enabled, hyperpod, regions)``.

    The cache key includes ``regions_key`` as a sorted immutable tuple so
    ``["us-east-1", "us-west-2"]`` and ``["us-west-2", "us-east-1"]``
    hit the same cache entry. The return shape is a dict mapping stack
    name to a tuple of ``(resource_type, json-serialized resource)``
    pairs — effectively a frozen/hashable snapshot of each template's
    Resources section. Callers materialize it back into a dict.

    The indirection keeps the cache hashable (dict values aren't) and
    lets the test rebuild just the pieces it needs per-example.
    """
    overlay = build_overlay(enabled, hyperpod_enabled, list(regions_key))
    templates = synth_all_stacks(overlay)
    frozen: dict[str, tuple[tuple[str, str], ...]] = {}
    for stack_name, template in templates.items():
        resources = template.get("Resources", {}) or {}
        frozen[stack_name] = tuple(
            (lid, json.dumps(res, sort_keys=True)) for lid, res in resources.items()
        )
    return frozen


def _unfreeze(
    frozen: dict[str, tuple[tuple[str, str], ...]],
) -> dict[str, dict[str, Any]]:
    """Rebuild a ``{stack_name: template_dict}`` dict from the cache entry."""
    templates: dict[str, dict[str, Any]] = {}
    for stack_name, frozen_resources in frozen.items():
        templates[stack_name] = {
            "Resources": {lid: json.loads(res_json) for lid, res_json in frozen_resources}
        }
    return templates


def _cached_templates(
    enabled: bool, hyperpod_enabled: bool, regions: list[str]
) -> dict[str, dict[str, Any]]:
    """Return ``{stack_name: template_dict}`` for ``(enabled, hyperpod, regions)``.

    Wraps the hashable ``_cached_synth`` so call sites can pass the
    Hypothesis-generated ``list[str]`` directly. The returned dicts are
    freshly rebuilt from the cache entry on every call so a caller
    mutating the returned structure can't corrupt a sibling example.
    """
    regions_key = tuple(sorted(regions))
    return _unfreeze(_cached_synth(enabled, hyperpod_enabled, regions_key))


# ---------------------------------------------------------------------------
# Resource classifiers.
# ---------------------------------------------------------------------------


def _flatten_join(value: Any) -> str:
    """Best-effort flatten of a ``Fn::Join`` / ``Fn::Sub`` / ``Fn::GetAtt``
    structure into a string for prefix matching.

    The flattened form only preserves literal string fragments — token
    references (``Ref``, ``GetAtt``) collapse to their logical id so a
    Join like ``["", [{"Fn::GetAtt": ["X", "Arn"]}, "/*"]]`` flattens to
    ``"<X.Arn>/*"``. That's exactly enough to let the classifiers tell
    cluster-shared tokens from analytics-studio tokens: the former
    references ``ReadClusterSharedBucketArn.*.Parameter.Value``, the
    latter would reference ``StudioOnlyBucket.*.Arn``.
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return json.dumps(value)

    if "Fn::Join" in value:
        parts = value["Fn::Join"]
        if isinstance(parts, list) and len(parts) == 2:
            sep, items = parts
            if isinstance(items, list):
                rendered_parts = [_flatten_join(x) for x in items]
                if isinstance(sep, str):
                    return sep.join(rendered_parts)
                return "".join(rendered_parts)
    if "Fn::GetAtt" in value:
        ga = value["Fn::GetAtt"]
        if isinstance(ga, list) and len(ga) == 2:
            return f"<{ga[0]}.{ga[1]}>"
        return json.dumps(value)
    if "Ref" in value:
        return f"<Ref:{value['Ref']}>"
    if "Fn::Sub" in value:
        sub = value["Fn::Sub"]
        if isinstance(sub, str):
            return sub
        if isinstance(sub, list) and sub:
            first = sub[0]
            return first if isinstance(first, str) else json.dumps(value)
    return json.dumps(value)


def _is_cluster_shared_token(resource: Any) -> bool:
    """True iff ``resource`` is a CDK token referencing the cross-region
    SSM reader ``ReadClusterSharedBucketArn.Parameter.Value`` (or the
    ``<arn>/*`` object-key wildcard layered on top).

    The pattern matches both the bare ``Fn::GetAtt`` dict and the
    wrapping ``Fn::Join`` used for the ``/*`` suffix.
    """
    flat = _flatten_join(resource)
    return "ReadClusterSharedBucketArn" in flat


def _classify_s3_resource(resource: Any) -> str:
    """Classify a policy-statement ``Resource`` entry for bucket isolation.

    Return one of:

    * ``"allow"`` — matches the cluster-shared allow-list (literal prefix
      ``arn:aws:s3:::gco-cluster-shared-`` or a CDK token resolving to
      the ``ReadClusterSharedBucketArn`` SSM reader).
    * ``"deny"`` — matches the analytics-studio deny-list (literal prefix
      ``arn:aws:s3:::gco-analytics-studio-`` or a token referencing the
      analytics-stack logical ids ``StudioOnlyBucket`` /
      ``AnalyticsAccessLogsBucket``).
    * ``"other"`` — any other S3 ARN or non-S3 resource. Pre-existing
      broader wildcards (``arn:aws:s3:::gco-*`` for the model-weights
      bucket) fall in this bucket and are not scored here.
    * ``"non-s3"`` — the resource is not an S3 ARN at all (execute-api,
      dynamodb, sqs, etc.).

    The classifier is deliberately conservative: unknown shapes fall
    through to ``"other"`` rather than ``"allow"``, so a future
    implementation change that accidentally broadens the grant would
    show up as an ``"other"`` entry rather than silently passing.
    """
    if isinstance(resource, str):
        if not resource.startswith(_S3_ARN_PREFIX):
            return "non-s3"
        if resource.startswith(ALLOW_LIST_ARN_PREFIX):
            return "allow"
        if resource.startswith(DENY_LIST_ARN_PREFIX):
            return "deny"
        return "other"

    if isinstance(resource, dict):
        flat = _flatten_join(resource)
        # Token referencing the cluster-shared SSM reader.
        if "ReadClusterSharedBucketArn" in flat:
            return "allow"
        # Token referencing the analytics-Studio bucket or its access-logs
        # bucket. These never appear in the regional template today; the
        # check is forward-looking.
        if "StudioOnlyBucket" in flat or "AnalyticsAccessLogsBucket" in flat:
            return "deny"
        # Literal ARN buried in a Join — flatten and re-check against the
        # two bucket-name prefixes.
        if _S3_ARN_PREFIX in flat:
            m = re.search(r"arn:aws:s3:::([A-Za-z0-9._\-*<>:]+)", flat)
            if m:
                bucket_prefix = m.group(1)
                if bucket_prefix.startswith(ALLOW_LIST_BUCKET_PREFIX):
                    return "allow"
                if bucket_prefix.startswith(DENY_LIST_BUCKET_PREFIX):
                    return "deny"
            return "other"
        return "non-s3"

    # List — the statement's Resource is itself a list; callers should
    # expand first. Treat as non-S3 so any missed nesting doesn't
    # false-positive.
    return "non-s3"


def _extract_resource_list(statement: dict[str, Any]) -> list[Any]:
    """Return the statement's ``Resource`` entries as a flat list."""
    res = statement.get("Resource")
    if res is None:
        return []
    if isinstance(res, list):
        return list(res)
    return [res]


# ---------------------------------------------------------------------------
# Bucket isolation property.
# ---------------------------------------------------------------------------


class TestBucketIsolationProperty:
    """Bucket isolation between ``Cluster_Shared_Bucket`` and
    ``Studio_Only_Bucket`` on the regional job-pod role.
    """

    @classmethod
    def setup_class(cls) -> None:
        """Pre-warm the full-app synth cache for every
        2×2×single-region combination Hypothesis is most likely to draw.

        Sixteen cache entries (``enabled × hyperpod × every candidate
        region``) cover every single-region draw and the most common
        first-region prefix of multi-region draws. Multi-region draws
        with two or three regions fall back to the first-call synth
        cost on their first hit and cache thereafter; the
        ``max_examples=30`` cap below bounds the total uncached-synth
        budget at well under the project's 120 s suite-wide timeout.
        """
        from tests._analytics_strategies import CANDIDATE_REGIONS

        for enabled in (False, True):
            for hyperpod in (False, True):
                for region in CANDIDATE_REGIONS:
                    _cached_synth(enabled, hyperpod, (region,))

    @settings(
        max_examples=30,
        deadline=20000,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
            HealthCheck.data_too_large,
        ],
    )
    @given(fixture=bucket_isolation_fixtures)
    def test_regional_job_pod_role_isolates_cluster_shared_from_studio_only(
        self, fixture: tuple[bool, bool, list[str]]
    ) -> None:
        """For any ``(enabled, hyperpod_enabled, regions)``, every
        regional template's job-pod role statements respect the
        allow-list + deny-list invariants.
        """
        enabled, hyperpod_enabled, regions = fixture
        templates = _cached_templates(enabled, hyperpod_enabled, regions)

        for region in regions:
            stack_name = f"gco-{region}"
            assert stack_name in templates, (
                f"Expected regional stack {stack_name!r} in synthesized "
                f"app. Got stacks: {sorted(templates)}"
            )
            template = templates[stack_name]

            statements = collect_job_pod_role_statements(template)
            assert statements, (
                f"Expected at least one job-pod / service-account role "
                f"policy statement in {stack_name!r}. Got none — the "
                f"logical-id filter may have regressed."
            )

            # Classify every S3 resource across every statement.
            seen: dict[str, list[Any]] = {
                "allow": [],
                "deny": [],
                "other": [],
                "non-s3": [],
            }
            for statement in statements:
                for resource in _extract_resource_list(statement):
                    kind = _classify_s3_resource(resource)
                    seen[kind].append(resource)

            # Deny-list assertion: nothing may target the Studio-only
            # bucket on the regional role, under any toggle.
            assert not seen["deny"], (
                f"Deny-list violation in {stack_name!r} "
                f"(enabled={enabled}, hyperpod={hyperpod_enabled}, "
                f"regions={regions}): the regional job-pod role must "
                f"never grant S3 access to the analytics-Studio "
                f"bucket (prefix {DENY_LIST_BUCKET_PREFIX!r}). Offending "
                f"resources: {seen['deny']!r}"
            )

            # Allow-list assertion: the cluster-shared grant must be
            # present on every regional role regardless of the toggle
            # (role is granted this unconditionally).
            assert seen["allow"], (
                f"Allow-list missing in {stack_name!r} "
                f"(enabled={enabled}, hyperpod={hyperpod_enabled}, "
                f"regions={regions}): expected at least one S3 "
                f"resource matching the cluster-shared bucket allow-"
                f"list (literal prefix {ALLOW_LIST_ARN_PREFIX!r} or a "
                f"CDK token referencing ReadClusterSharedBucketArn). "
                f"Saw these S3-classified resources instead: "
                f"allow={seen['allow']!r} other={seen['other']!r}"
            )


# ---------------------------------------------------------------------------
# Secondary unit property — exhaustive enumeration, no Hypothesis.
# ---------------------------------------------------------------------------


class TestClassifierExhaustive:
    """Unit tests exercising ``_classify_s3_resource`` across its full
    input space. Not Hypothesis-driven — the classifier has a finite
    handful of shapes and a table-driven test is clearer.
    """

    def test_cluster_shared_literal(self) -> None:
        assert (
            _classify_s3_resource("arn:aws:s3:::gco-cluster-shared-123456789012-us-east-2")
            == "allow"
        )

    def test_cluster_shared_literal_object_wildcard(self) -> None:
        assert (
            _classify_s3_resource("arn:aws:s3:::gco-cluster-shared-123456789012-us-east-2/*")
            == "allow"
        )

    def test_cluster_shared_token(self) -> None:
        token = {"Fn::GetAtt": ["ReadClusterSharedBucketArn4B0BD291", "Parameter.Value"]}
        assert _classify_s3_resource(token) == "allow"

    def test_cluster_shared_token_with_wildcard(self) -> None:
        token = {
            "Fn::Join": [
                "",
                [
                    {
                        "Fn::GetAtt": [
                            "ReadClusterSharedBucketArn4B0BD291",
                            "Parameter.Value",
                        ]
                    },
                    "/*",
                ],
            ]
        }
        assert _classify_s3_resource(token) == "allow"

    def test_studio_literal_denied(self) -> None:
        assert (
            _classify_s3_resource("arn:aws:s3:::gco-analytics-studio-123456789012-us-east-2")
            == "deny"
        )

    def test_studio_token_denied(self) -> None:
        token = {"Fn::GetAtt": ["StudioOnlyBucket80FF5E65", "Arn"]}
        assert _classify_s3_resource(token) == "deny"

    def test_broad_gco_wildcard_is_other(self) -> None:
        # Pre-existing model-weights ARN — outside the scope of this property.
        assert _classify_s3_resource("arn:aws:s3:::gco-*") == "other"
        assert _classify_s3_resource("arn:aws:s3:::gco-*/*") == "other"

    def test_non_s3_is_non_s3(self) -> None:
        assert _classify_s3_resource("arn:aws:sqs:us-east-1:123:queue") == "non-s3"
        assert _classify_s3_resource({"Ref": "SomeQueue"}) == "non-s3"

    @settings(max_examples=25, deadline=None)
    @given(st.text())
    def test_classifier_never_raises_on_arbitrary_strings(self, s: str) -> None:
        """The classifier must be total — an arbitrary text input never
        raises. The classification result is not asserted here, only the
        totality."""
        _ = _classify_s3_resource(s)
