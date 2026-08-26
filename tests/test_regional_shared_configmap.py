"""The ``gco-regional-shared-bucket`` ConfigMap is always applied.

The regional stack provisions ``gco-regional-shared-<account>-<region>``
unconditionally and grants the in-region pod role read/write on it. For a pod
to *use* that grant it has to learn the bucket's name, which it does through
the ``gco-regional-shared-bucket`` ConfigMap applied into ``gco-jobs``,
``gco-system``, and ``gco-inference``.

That ConfigMap is only useful if its three ``{{REGIONAL_SHARED_BUCKET*}}``
placeholders resolve. The applier deliberately skips any manifest still
carrying an unresolved ``{{UPPER_SNAKE}}`` token after substitution — that is
how optional features (FSx, Valkey, Aurora) are pruned — so a missing
replacement would silently drop the ConfigMap instead of failing the deploy.
These tests pin the replacements so that silent-drop failure mode cannot
reappear:

* the pure helper emits exactly the three expected keys;
* all three land in the convergence pipeline's ``ImageReplacements``
  execution input, which is what both kubectl passes read;
* the region value is the stack's own region (this bucket is same-region by
  construction, unlike ``Cluster_Shared_Bucket``);
* the name and ARN point at the one regional bucket this stack creates, not
  at the cluster-shared bucket.

The manifest file itself is covered by ``tests/test_kubectl_applier.py`` and
the schema validator; here we only assert the CDK-side wiring.
"""

from __future__ import annotations

from typing import Any

from gco.stacks.regional_stack import _compute_kubectl_regional_shared_replacements
from tests.test_mooncake_regional_bucket_synthesis import (
    _ACCOUNT,
    _REGION,
    _expected_bucket_name,
    _regional_bucket_logical_id,
    _regional_template_json,
)

# The ConfigMap in 27-storage-regional-shared-bucket.yaml reads exactly these
# three placeholders. Keep this tuple in lockstep with that manifest.
EXPECTED_KEYS = (
    "{{REGIONAL_SHARED_BUCKET}}",
    "{{REGIONAL_SHARED_BUCKET_ARN}}",
    "{{REGIONAL_SHARED_BUCKET_REGION}}",
)


def _image_replacements(template: dict[str, Any]) -> dict[str, Any]:
    """Return the single ``ImageReplacements`` map from the synthesized template.

    ``_apply_kubernetes_manifests`` puts the replacements into the convergence
    pipeline's execution input rather than onto a synchronous kubectl custom
    resource; both the base and post-Helm kubectl tasks then read
    ``$.ImageReplacements``. Asserting there is exactly one such map keeps this
    test honest if that plumbing is ever restructured.
    """
    maps = [
        properties["ImageReplacements"]
        for resource in template.get("Resources", {}).values()
        if isinstance(properties := resource.get("Properties", {}), dict)
        and isinstance(properties.get("ImageReplacements"), dict)
    ]
    assert len(maps) == 1, f"expected exactly one ImageReplacements map, found {len(maps)}"
    return maps[0]


class TestRegionalSharedReplacementHelper:
    """The pure helper is the single source of the three placeholder keys."""

    def test_emits_exactly_the_expected_keys(self) -> None:
        replacements = _compute_kubectl_regional_shared_replacements(
            name="bucket-name",
            arn="arn:aws:s3:::bucket-name",
            region="us-west-2",
        )
        assert set(replacements) == set(EXPECTED_KEYS)

    def test_passes_values_through_unchanged(self) -> None:
        replacements = _compute_kubectl_regional_shared_replacements(
            name="bucket-name",
            arn="arn:aws:s3:::bucket-name",
            region="us-west-2",
        )
        assert replacements["{{REGIONAL_SHARED_BUCKET}}"] == "bucket-name"
        assert replacements["{{REGIONAL_SHARED_BUCKET_ARN}}"] == "arn:aws:s3:::bucket-name"
        assert replacements["{{REGIONAL_SHARED_BUCKET_REGION}}"] == "us-west-2"


class TestRegionalSharedConfigMapAlwaysPresent:
    """Every regional template carries the three replacements, unconditionally."""

    def test_all_three_keys_present(self) -> None:
        replacements = _image_replacements(_regional_template_json())
        missing = [key for key in EXPECTED_KEYS if key not in replacements]
        assert not missing, (
            f"ImageReplacements is missing {missing} — the "
            f"gco-regional-shared-bucket ConfigMap is always-on, and an absent "
            f"replacement would make the applier skip it silently. "
            f"Present keys: {sorted(replacements)}"
        )

    def test_no_key_is_empty(self) -> None:
        replacements = _image_replacements(_regional_template_json())
        for key in EXPECTED_KEYS:
            value = replacements[key]
            assert value not in (None, ""), f"{key} resolved to an empty value"

    def test_region_is_the_stacks_own_region(self) -> None:
        """Same-region by construction — this is the whole point of the bucket."""
        replacements = _image_replacements(_regional_template_json())
        assert replacements["{{REGIONAL_SHARED_BUCKET_REGION}}"] == _REGION

    def test_name_and_arn_reference_the_regional_bucket(self) -> None:
        """The values point at this stack's own bucket, not the shared one."""
        template = _regional_template_json()
        replacements = _image_replacements(template)
        logical_id = _regional_bucket_logical_id(template)

        assert replacements["{{REGIONAL_SHARED_BUCKET}}"] == {"Ref": logical_id}
        assert replacements["{{REGIONAL_SHARED_BUCKET_ARN}}"] == {"Fn::GetAtt": [logical_id, "Arn"]}

    def test_distinct_from_the_cluster_shared_replacements(self) -> None:
        """The two ConfigMaps must not collide: different keys, different sources.

        A pod is expected to be able to ``envFrom`` both
        ``gco-regional-shared-bucket`` and ``gco-cluster-shared-bucket`` at
        once, which only works while the two sets stay disjoint.
        """
        replacements = _image_replacements(_regional_template_json())
        cluster_keys = {
            "{{CLUSTER_SHARED_BUCKET}}",
            "{{CLUSTER_SHARED_BUCKET_ARN}}",
            "{{CLUSTER_SHARED_BUCKET_REGION}}",
        }
        assert cluster_keys.issubset(replacements), "cluster-shared keys regressed"
        assert not cluster_keys & set(EXPECTED_KEYS)

        for cluster_key, regional_key in zip(
            sorted(cluster_keys), sorted(EXPECTED_KEYS), strict=True
        ):
            assert replacements[cluster_key] != replacements[regional_key], (
                f"{cluster_key} and {regional_key} resolve to the same value; "
                f"the regional bucket must not alias the cluster-shared bucket"
            )

    def test_bucket_name_shape_is_account_and_region_scoped(self) -> None:
        """Sanity-check the physical name the ConfigMap will carry at deploy time."""
        assert _expected_bucket_name().endswith(f"-{_ACCOUNT}-{_REGION}")
