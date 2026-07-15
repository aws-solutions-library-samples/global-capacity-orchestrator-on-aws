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
    "QUOTA_MAX_CPU": "100",
    "QUOTA_MAX_MEMORY": "512Gi",
    "QUOTA_MAX_GPU": "32",
    "QUOTA_MAX_PODS": "50",
    "LIMIT_MAX_CPU": "10",
    "LIMIT_MAX_MEMORY": "64Gi",
    "LIMIT_MAX_GPU": "4",
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
        assert hard["requests.cpu"] == "100"
        assert hard["requests.memory"] == "512Gi"
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
        in a pod gets the max value (4) as an implicit request, so a
        3-container control-plane pod (e.g. the Slinky Slurm controller with
        slurmctld + log sidecar + OTel sidecar) ends up demanding a single
        node with 12 GPUs — unsatisfiable on any g4dn/g5/g6/p3/p4/p5 instance.
        """
        default = limit_range["spec"]["limits"][0]["default"]
        request = limit_range["spec"]["limits"][0]["defaultRequest"]
        assert default["nvidia.com/gpu"] == 0 or default["nvidia.com/gpu"] == "0"
        assert request["nvidia.com/gpu"] == 0 or request["nvidia.com/gpu"] == "0"

    def test_max_values_from_cdk_json(self, limit_range):
        maxes = limit_range["spec"]["limits"][0]["max"]
        assert maxes["cpu"] == 10 or maxes["cpu"] == "10"
        assert maxes["memory"] == "64Gi"
        assert maxes["nvidia.com/gpu"] == 4 or maxes["nvidia.com/gpu"] == "4"


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
    """Regression guard: Python defaults must match cdk.json defaults."""

    @pytest.fixture
    def cdk_defaults(self):
        with CDK_JSON_PATH.open() as f:
            cdk = json.load(f)
        return cdk["context"]["resource_quota"]

    @pytest.fixture
    def stack_source(self):
        return REGIONAL_STACK_PATH.read_text()

    @pytest.mark.parametrize(
        "cdk_key,default_value",
        [
            ("max_cpu", '"100"'),
            ("max_memory", '"512Gi"'),
            ("max_gpu", '"32"'),
            ("max_pods", '"50"'),
            ("container_max_cpu", '"10"'),
            ("container_max_memory", '"64Gi"'),
            ("container_max_gpu", '"4"'),
        ],
    )
    def test_stack_default_matches_cdk_json(
        self, stack_source, cdk_defaults, cdk_key, default_value
    ):
        # The `.get(key, default)` call in regional_stack.py must use the same
        # value as cdk.json -- otherwise operators on older config files would
        # get silently different limits than operators with fresh defaults.
        assert cdk_key in cdk_defaults, f"cdk.json missing resource_quota.{cdk_key}"
        unquoted_default = default_value.strip('"')
        assert cdk_defaults[cdk_key] == unquoted_default, (
            f"cdk.json resource_quota.{cdk_key}={cdk_defaults[cdk_key]!r} "
            f"does not match expected {unquoted_default!r}"
        )
        # And the matching `.get(cdk_key, default_value)` must live in the stack.
        needle = f'resource_quota.get("{cdk_key}", {default_value})'
        assert needle in stack_source, (
            f"Expected `{needle}` in regional_stack.py but did not find it. "
            "The Python default and cdk.json default have drifted."
        )
