"""
Tests for the ResourceQuota + LimitRange manifest
(lambda/kubectl-applier-simple/manifests/04-resource-quotas.yaml).

Renders the manifest by replacing {{QUOTA_*}} and {{LIMIT_*}}
placeholders with the default values from cdk.json, then asserts the
resulting YAML has exactly two documents (ResourceQuota and LimitRange)
with the expected hard limits and apiVersion/namespace scoping. Also
cross-checks that the default values encoded in regional_stack.py and
cdk.json can't silently drift — if either side changes, the mirror
test fails.
"""

import json
from pathlib import Path

import pytest
import yaml

MANIFEST_PATH = Path("lambda/kubectl-applier-simple/manifests/04-resource-quotas.yaml")
CDK_JSON_PATH = Path("cdk.json")
REGIONAL_STACK_PATH = Path("gco/stacks/regional_stack.py")

PLACEHOLDERS = (
    "QUOTA_MAX_CPU",
    "QUOTA_MAX_MEMORY",
    "QUOTA_MAX_GPU",
    "QUOTA_MAX_PODS",
    "LIMIT_MAX_CPU",
    "LIMIT_MAX_MEMORY",
    "LIMIT_MAX_GPU",
)

# Default substitution values that mirror cdk.json defaults.
DEFAULT_SUBSTITUTIONS = {
    "QUOTA_MAX_CPU": "400",
    "QUOTA_MAX_MEMORY": "4096Gi",
    "QUOTA_MAX_GPU": "32",
    "QUOTA_MAX_PODS": "50",
    "LIMIT_MAX_CPU": "192",
    "LIMIT_MAX_MEMORY": "2048Gi",
    "LIMIT_MAX_GPU": "8",
}


def _render(substitutions: dict) -> str:
    """Render the manifest by replacing `{{KEY}}` placeholders with values."""
    content = MANIFEST_PATH.read_text()
    for key, value in substitutions.items():
        content = content.replace("{{" + key + "}}", str(value))
    return content


def _parse(substitutions: dict) -> list:
    """Render and load every YAML document in the manifest."""
    rendered = _render(substitutions)
    return [doc for doc in yaml.safe_load_all(rendered) if doc is not None]


def _find(docs: list, kind: str, name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"{kind}/{name} not found in manifest")


class TestManifestStructure:
    """Basic shape and identity of the manifest documents."""

    def test_manifest_file_exists(self):
        assert MANIFEST_PATH.exists(), f"Expected manifest at {MANIFEST_PATH}"

    def test_manifest_contains_all_placeholders(self):
        """Every placeholder we substitute in code must actually appear in the file."""
        raw = MANIFEST_PATH.read_text()
        for placeholder in PLACEHOLDERS:
            assert "{{" + placeholder + "}}" in raw, (
                f"Placeholder {placeholder} missing from manifest"
            )

    def test_manifest_has_exactly_two_documents(self):
        docs = _parse(DEFAULT_SUBSTITUTIONS)
        assert len(docs) == 2

    def test_manifest_has_resource_quota_and_limit_range(self):
        docs = _parse(DEFAULT_SUBSTITUTIONS)
        kinds = sorted(d["kind"] for d in docs)
        assert kinds == ["LimitRange", "ResourceQuota"]


class TestResourceQuota:
    """The ResourceQuota object and its hard limits."""

    @pytest.fixture
    def quota(self):
        docs = _parse(DEFAULT_SUBSTITUTIONS)
        return _find(docs, "ResourceQuota", "gco-jobs-quota")

    def test_api_version_and_namespace(self, quota):
        assert quota["apiVersion"] == "v1"
        assert quota["metadata"]["namespace"] == "gco-jobs"

    def test_has_required_hard_limits(self, quota):
        hard = quota["spec"]["hard"]
        assert "requests.cpu" in hard
        assert "requests.memory" in hard
        assert "requests.nvidia.com/gpu" in hard
        assert "pods" in hard

    def test_default_values_from_cdk_json(self, quota):
        hard = quota["spec"]["hard"]
        assert hard["requests.cpu"] == "400"
        assert hard["requests.memory"] == "4096Gi"
        assert hard["requests.nvidia.com/gpu"] == "32"
        assert hard["pods"] == "50"


class TestLimitRange:
    """The LimitRange object, its defaults and per-container caps."""

    @pytest.fixture
    def limit_range(self):
        docs = _parse(DEFAULT_SUBSTITUTIONS)
        return _find(docs, "LimitRange", "gco-jobs-limits")

    def test_api_version_and_namespace(self, limit_range):
        assert limit_range["apiVersion"] == "v1"
        assert limit_range["metadata"]["namespace"] == "gco-jobs"

    def test_has_container_limit_with_required_fields(self, limit_range):
        limits = limit_range["spec"]["limits"]
        assert len(limits) == 1
        container_limit = limits[0]
        assert container_limit["type"] == "Container"
        assert "default" in container_limit
        assert "defaultRequest" in container_limit
        assert "max" in container_limit

    def test_default_limits(self, limit_range):
        """Per task spec: default cpu: 1, memory: 4Gi."""
        default = limit_range["spec"]["limits"][0]["default"]
        assert default["cpu"] == 1 or default["cpu"] == "1"
        assert default["memory"] == "4Gi"

    def test_default_requests(self, limit_range):
        """Per task spec: defaultRequest cpu: 100m, memory: 256Mi."""
        request = limit_range["spec"]["limits"][0]["defaultRequest"]
        assert request["cpu"] == "100m"
        assert request["memory"] == "256Mi"

    def test_gpu_default_is_zero(self, limit_range):
        """``nvidia.com/gpu`` must be explicitly 0 in default and defaultRequest.

        Kubernetes auto-propagates LimitRange ``max`` to ``default`` when
        ``default`` is unspecified for the same resource. Without an explicit
        zero on an extended resource like ``nvidia.com/gpu``, every container
        in a pod gets the max value (8) as an implicit request, so a
        3-container control-plane pod (e.g. the Slinky Slurm controller with
        slurmctld + log sidecar + OTel sidecar) ends up demanding a single
        node with 24 GPUs — unsatisfiable on any single GPU instance GCO pools offer.
        """
        default = limit_range["spec"]["limits"][0]["default"]
        request = limit_range["spec"]["limits"][0]["defaultRequest"]
        assert default["nvidia.com/gpu"] == 0 or default["nvidia.com/gpu"] == "0"
        assert request["nvidia.com/gpu"] == 0 or request["nvidia.com/gpu"] == "0"

    def test_max_values_from_cdk_json(self, limit_range):
        # One full accelerator-node slice (p5.48xlarge / trn2.48xlarge):
        # anything smaller rejects full-node distributed-training pods at
        # admission (live run ex241-df723811).
        maxes = limit_range["spec"]["limits"][0]["max"]
        assert maxes["cpu"] == 192 or maxes["cpu"] == "192"
        assert maxes["memory"] == "2048Gi"
        assert maxes["nvidia.com/gpu"] == 8 or maxes["nvidia.com/gpu"] == "8"


class TestPlaceholderSubstitution:
    """Parameterized checks that user-supplied config values flow through."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {
                "QUOTA_MAX_CPU": "200",
                "QUOTA_MAX_MEMORY": "1Ti",
                "QUOTA_MAX_GPU": "32",
                "QUOTA_MAX_PODS": "100",
                "LIMIT_MAX_CPU": "20",
                "LIMIT_MAX_MEMORY": "128Gi",
                "LIMIT_MAX_GPU": "8",
            },
            {
                "QUOTA_MAX_CPU": "50",
                "QUOTA_MAX_MEMORY": "256Gi",
                "QUOTA_MAX_GPU": "4",
                "QUOTA_MAX_PODS": "25",
                "LIMIT_MAX_CPU": "5",
                "LIMIT_MAX_MEMORY": "32Gi",
                "LIMIT_MAX_GPU": "2",
            },
            {
                "QUOTA_MAX_CPU": "1",
                "QUOTA_MAX_MEMORY": "1Gi",
                "QUOTA_MAX_GPU": "0",
                "QUOTA_MAX_PODS": "1",
                "LIMIT_MAX_CPU": "1",
                "LIMIT_MAX_MEMORY": "1Gi",
                "LIMIT_MAX_GPU": "0",
            },
        ],
    )
    def test_substitution_produces_expected_quota_and_limit(self, overrides):
        docs = _parse(overrides)

        quota = _find(docs, "ResourceQuota", "gco-jobs-quota")
        hard = quota["spec"]["hard"]
        # ResourceQuota values are always rendered as strings by safe_load because
        # the template wraps them in quotes.
        assert hard["requests.cpu"] == overrides["QUOTA_MAX_CPU"]
        assert hard["requests.memory"] == overrides["QUOTA_MAX_MEMORY"]
        assert hard["requests.nvidia.com/gpu"] == overrides["QUOTA_MAX_GPU"]
        assert hard["pods"] == overrides["QUOTA_MAX_PODS"]

        limit_range = _find(docs, "LimitRange", "gco-jobs-limits")
        maxes = limit_range["spec"]["limits"][0]["max"]
        # Some `max` fields are unquoted in the template so int-coercion happens
        # for plain integer overrides. Compare as strings to be safe.
        assert str(maxes["cpu"]) == overrides["LIMIT_MAX_CPU"]
        assert str(maxes["memory"]) == overrides["LIMIT_MAX_MEMORY"]
        assert str(maxes["nvidia.com/gpu"]) == overrides["LIMIT_MAX_GPU"]

    def test_no_unsubstituted_placeholders_remain(self):
        rendered = _render(DEFAULT_SUBSTITUTIONS)
        assert "{{" not in rendered
        assert "}}" not in rendered


class TestDefaultsMatchCdkJson:
    """Regression guard: constants defaults must match cdk.json defaults."""

    @pytest.fixture
    def cdk_defaults(self):
        with CDK_JSON_PATH.open() as f:
            cdk = json.load(f)
        return cdk["context"]["resource_quota"]

    def test_cdk_json_mirrors_default_resource_quota(self, cdk_defaults):
        # DEFAULT_RESOURCE_QUOTA (gco/stacks/constants.py) is the single
        # source of truth the regional stack merges context over; cdk.json
        # documents the same values for operators. If either side drifts,
        # operators on older config files get silently different limits
        # than operators with fresh defaults.
        from gco.stacks.constants import DEFAULT_RESOURCE_QUOTA

        assert dict(cdk_defaults) == dict(DEFAULT_RESOURCE_QUOTA)

    def test_stack_reads_defaults_from_constants(self):
        # The stack must consume the shared defaults (via the validated
        # helper), not carry its own inline copies.
        source = REGIONAL_STACK_PATH.read_text()
        assert "_validated_resource_quota(" in source
        assert 'resource_quota.get("max_cpu"' not in source


class TestValidatedResourceQuota:
    """Synth-time validation of the resource_quota context.

    The values are substituted verbatim into the gco-jobs ResourceQuota and
    LimitRange manifests; before this validation a typo or an incoherent
    pair deployed silently and surfaced only as pods forbidden at admission
    (live example-job validation run ex241-df723811).
    """

    @staticmethod
    def _validate(raw):
        from gco.stacks.regional_stack import _validated_resource_quota

        return _validated_resource_quota(raw)

    def test_empty_context_yields_defaults(self):
        from gco.stacks.constants import DEFAULT_RESOURCE_QUOTA

        assert self._validate({}) == dict(DEFAULT_RESOURCE_QUOTA)

    def test_partial_override_merges_over_defaults(self):
        merged = self._validate({"container_max_gpu": "4"})
        assert merged["container_max_gpu"] == "4"
        assert merged["max_gpu"] == "32"

    def test_non_mapping_context_is_rejected(self):
        with pytest.raises(ValueError, match="must be an object"):
            self._validate("not-a-dict")

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValueError, match="unknown key"):
            self._validate({"max_cpus": "100"})

    def test_unparseable_quantity_is_rejected(self):
        with pytest.raises(ValueError, match="not a valid Kubernetes quantity"):
            self._validate({"container_max_memory": "64G i"})

    def test_container_ceiling_may_not_exceed_namespace_ceiling(self):
        with pytest.raises(ValueError, match="could never be admitted"):
            self._validate({"container_max_gpu": "64"})

    def test_mixed_units_compare_correctly(self):
        # 1Ti fits inside the 4096Gi namespace default; quantities must be
        # compared numerically, not lexically.
        merged = self._validate({"container_max_memory": "1Ti"})
        assert merged["container_max_memory"] == "1Ti"


class TestValidatedManifestCaps:
    """Synth-time validation of job_validation_policy.resource_quotas.

    These caps are what the manifest/queue processors enforce per submitted
    manifest; the layering invariant (container LimitRange <= per-manifest
    cap <= namespace quota) is what keeps the three enforcement layers
    telling one story. The old defaults disagreed (4-GPU manifest cap vs
    the platform's own 16-GPU EFA training example).
    """

    @staticmethod
    def _validate(raw, quota=None):
        from gco.stacks.regional_stack import (
            _validated_manifest_caps,
            _validated_resource_quota,
        )

        return _validated_manifest_caps(raw, _validated_resource_quota(quota or {}))

    def test_empty_context_yields_defaults(self):
        from gco.stacks.constants import DEFAULT_MANIFEST_RESOURCE_CAPS

        assert self._validate({}) == {
            key: str(value) for key, value in DEFAULT_MANIFEST_RESOURCE_CAPS.items()
        }

    def test_partial_override_merges_over_defaults(self):
        merged = self._validate({"max_gpu_per_manifest": "8"})
        assert merged["max_gpu_per_manifest"] == "8"
        assert merged["max_cpu_per_manifest"] == "384"

    def test_non_mapping_context_is_rejected(self):
        with pytest.raises(ValueError, match="must be an object"):
            self._validate(["max_gpu_per_manifest"])

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValueError, match="unknown key"):
            self._validate({"max_gpus_per_manifest": 8})

    def test_unparseable_quantity_is_rejected(self):
        with pytest.raises(ValueError, match="not a valid Kubernetes quantity"):
            self._validate({"max_memory_per_manifest": "lots"})

    def test_cap_below_container_ceiling_is_rejected(self):
        # A manifest cap below the LimitRange container maximum means the
        # front door rejects manifests whose single container the namespace
        # would happily admit.
        with pytest.raises(ValueError, match="below resource_quota.container_max_gpu"):
            self._validate({"max_gpu_per_manifest": 4})

    def test_cap_above_namespace_quota_is_rejected(self):
        # A manifest cap above the namespace quota accepts manifests whose
        # pods can never all run.
        with pytest.raises(ValueError, match="exceeds resource_quota.max_gpu"):
            self._validate({"max_gpu_per_manifest": 64})

    def test_custom_quota_moves_the_bounds(self):
        merged = self._validate(
            {"max_gpu_per_manifest": 4},
            quota={"container_max_gpu": "2", "max_gpu": "8"},
        )
        assert merged["max_gpu_per_manifest"] == "4"


class TestManifestCapsMatchCdkJson:
    """Regression guard: manifest-cap constants must match cdk.json."""

    def test_cdk_json_mirrors_default_manifest_caps(self):
        from gco.stacks.constants import DEFAULT_MANIFEST_RESOURCE_CAPS

        with CDK_JSON_PATH.open() as f:
            cdk = json.load(f)
        documented = cdk["context"]["job_validation_policy"]["resource_quotas"]
        assert documented == dict(DEFAULT_MANIFEST_RESOURCE_CAPS)

    def test_stack_reads_caps_from_constants(self):
        source = REGIONAL_STACK_PATH.read_text()
        assert "_validated_manifest_caps(" in source
        # The old inline fallback copies are exactly what let the defaults
        # drift apart; the stack must not carry its own values.
        assert '"max_gpu_per_manifest", 4' not in source
        assert '.get("max_cpu_per_manifest", "10")' not in source
