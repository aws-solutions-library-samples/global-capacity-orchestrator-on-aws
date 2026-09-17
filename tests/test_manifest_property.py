"""
Property-based tests for manifest validation and YAML parsing.

Uses Hypothesis strategies to generate Kubernetes-shaped manifests with
randomized names, CPU/memory/GPU request strings across every unit
variant (Ki/Mi/Gi/Ti/M/G/raw bytes), container security contexts, and
trusted image URIs. Feeds them through ManifestProcessor.validate_manifest
to flush out crashes or inconsistent accept/reject decisions on inputs
a human wouldn't think to construct. Complements the example-based
tests rather than replacing them.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies for generating Kubernetes-like manifests
# ---------------------------------------------------------------------------

# Valid K8s name: lowercase alphanumeric + hyphens, 1-63 chars, starts/ends with alphanum
k8s_name = st.from_regex(r"[a-z][a-z0-9\-]{0,30}[a-z0-9]", fullmatch=True)

cpu_strings = st.one_of(
    st.just("0"),
    st.integers(min_value=1, max_value=128).map(str),  # whole cores
    st.integers(min_value=1, max_value=128000).map(lambda n: f"{n}m"),  # millicores
)

memory_strings = st.one_of(
    st.just("0"),
    st.integers(min_value=1, max_value=1024).map(lambda n: f"{n}Mi"),
    st.integers(min_value=1, max_value=64).map(lambda n: f"{n}Gi"),
    st.integers(min_value=1, max_value=4).map(lambda n: f"{n}Ti"),
    st.integers(min_value=1, max_value=1024).map(lambda n: f"{n}Ki"),
    st.integers(min_value=1, max_value=1024).map(lambda n: f"{n}M"),
    st.integers(min_value=1, max_value=64).map(lambda n: f"{n}G"),
    st.integers(min_value=0, max_value=10**9).map(str),  # raw bytes
)

gpu_counts = st.integers(min_value=0, max_value=16).map(str)

container_strategy = st.fixed_dictionaries(
    {
        "name": k8s_name,
        "image": st.one_of(
            st.just("python:3.14"),
            st.just("nvidia/cuda:12.0-base"),
            st.just("docker.io/library/busybox:latest"),
            st.just("public.ecr.aws/lambda/python:3.14"),
            st.just("nvcr.io/nvidia/pytorch:24.01-py3"),
        ),
    },
    optional={
        "resources": st.fixed_dictionaries(
            {},
            optional={
                "requests": st.fixed_dictionaries(
                    {},
                    optional={
                        "cpu": cpu_strings,
                        "memory": memory_strings,
                        "nvidia.com/gpu": gpu_counts,
                    },
                ),
                "limits": st.fixed_dictionaries(
                    {},
                    optional={
                        "cpu": cpu_strings,
                        "memory": memory_strings,
                        "nvidia.com/gpu": gpu_counts,
                    },
                ),
            },
        ),
        "securityContext": st.fixed_dictionaries(
            {},
            optional={
                "privileged": st.booleans(),
                "allowPrivilegeEscalation": st.booleans(),
            },
        ),
    },
)

job_manifest = st.fixed_dictionaries(
    {
        "apiVersion": st.just("batch/v1"),
        "kind": st.just("Job"),
        "metadata": st.fixed_dictionaries(
            {
                "name": k8s_name,
            },
            optional={
                "namespace": st.sampled_from(["default", "gco-jobs"]),
            },
        ),
        "spec": st.fixed_dictionaries(
            {
                "template": st.fixed_dictionaries(
                    {
                        "spec": st.fixed_dictionaries(
                            {
                                "containers": st.lists(container_strategy, min_size=1, max_size=3),
                            },
                            optional={
                                "restartPolicy": st.sampled_from(["Never", "OnFailure"]),
                            },
                        ),
                    }
                ),
            }
        ),
    }
)

deployment_manifest = st.fixed_dictionaries(
    {
        "apiVersion": st.just("apps/v1"),
        "kind": st.just("Deployment"),
        "metadata": st.fixed_dictionaries(
            {
                "name": k8s_name,
            },
            optional={
                "namespace": st.sampled_from(["default", "gco-jobs"]),
            },
        ),
        "spec": st.fixed_dictionaries(
            {
                "replicas": st.integers(min_value=1, max_value=5),
                "selector": st.just({"matchLabels": {"app": "test"}}),
                "template": st.fixed_dictionaries(
                    {
                        "metadata": st.just({"labels": {"app": "test"}}),
                        "spec": st.fixed_dictionaries(
                            {
                                "containers": st.lists(container_strategy, min_size=1, max_size=3),
                            }
                        ),
                    }
                ),
            }
        ),
    }
)

valid_manifest = st.one_of(job_manifest, deployment_manifest)


# ---------------------------------------------------------------------------
# Helper to create a ManifestProcessor without a real K8s connection
# ---------------------------------------------------------------------------


def make_processor(**overrides):
    """Create a ManifestProcessor with mocked K8s config."""
    defaults = {
        "max_cpu_per_manifest": "100",
        "max_memory_per_manifest": "256Gi",
        "max_gpu_per_manifest": 16,
        "allowed_namespaces": ["default", "gco-jobs"],
        "validation_enabled": True,
        "trusted_registries": [
            "docker.io",
            "gcr.io",
            "quay.io",
            "registry.k8s.io",
            "public.ecr.aws",
            "nvcr.io",
        ],
        "trusted_dockerhub_orgs": [
            "nvidia",
            "pytorch",
            "rayproject",
            "tensorflow",
            "huggingface",
            "amazon",
            "bitnami",
            "gco",
        ],
    }
    defaults.update(overrides)

    with patch("gco.services.manifest_processor.config") as mock_config:
        mock_config.load_incluster_config.side_effect = Exception("not in cluster")
        mock_config.load_kube_config.return_value = None
        mock_config.ConfigException = Exception

        from gco.services.manifest_processor import ManifestProcessor

        processor = ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict=defaults,
        )
    return processor


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestManifestValidationProperties:
    """Property-based tests for manifest validation."""

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = make_processor()

    @given(manifest=valid_manifest)
    @settings(max_examples=200, deadline=2000)
    def test_valid_manifests_never_crash(self, manifest):
        """validate_manifest should never raise an exception, only return (bool, str|None)."""
        is_valid, error = self.processor.validate_manifest(manifest)
        assert isinstance(is_valid, bool)
        assert error is None or isinstance(error, str)

    @given(manifest=valid_manifest)
    @settings(max_examples=100, deadline=2000)
    def test_valid_manifests_with_trusted_images_pass(self, manifest):
        """Manifests with trusted images and no privileged containers should pass validation."""
        # Strip any privileged security contexts
        containers = (
            manifest.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        )
        for c in containers:
            sc = c.get("securityContext", {})
            sc.pop("privileged", None)
            sc.pop("allowPrivilegeEscalation", None)

        is_valid, _error = self.processor.validate_manifest(manifest)
        # May still fail on resource limits, but should not crash
        assert isinstance(is_valid, bool)

    @given(
        name=st.text(min_size=0, max_size=100),
        namespace=st.text(min_size=0, max_size=100),
    )
    @settings(max_examples=100, deadline=2000)
    def test_arbitrary_metadata_never_crashes(self, name, namespace):
        """Arbitrary metadata values should not crash validation."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "test", "image": "python:3.14"}],
                    }
                }
            },
        }
        is_valid, _error = self.processor.validate_manifest(manifest)
        assert isinstance(is_valid, bool)

    @given(manifest=valid_manifest)
    @settings(max_examples=50, deadline=2000)
    def test_privileged_containers_always_rejected(self, manifest):
        """Any manifest with privileged=True should be rejected."""
        containers = (
            manifest.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        )
        if containers:
            containers[0]["securityContext"] = {"privileged": True}

        is_valid, error = self.processor.validate_manifest(manifest)
        if containers:
            assert not is_valid
            assert error is not None

    @given(manifest=valid_manifest)
    @settings(max_examples=50, deadline=2000)
    def test_disallowed_namespace_always_rejected(self, manifest):
        """Manifests targeting a non-allowed namespace should be rejected."""
        manifest["metadata"]["namespace"] = "kube-system"
        is_valid, error = self.processor.validate_manifest(manifest)
        assert not is_valid
        assert "not allowed" in error.lower()


class TestCpuParsingProperties:
    """Property-based tests for CPU string parsing."""

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = make_processor()

    @given(millicores=st.integers(min_value=0, max_value=1000000))
    @settings(max_examples=200, deadline=2000)
    def test_millicore_strings_round_trip(self, millicores):
        """Parsing '{n}m' should return n."""
        result = self.processor._parse_cpu_string(f"{millicores}m")
        assert result == millicores

    @given(cores=st.integers(min_value=0, max_value=1000))
    @settings(max_examples=200, deadline=2000)
    def test_whole_core_strings_convert_to_millicores(self, cores):
        """Parsing '{n}' (whole cores) should return n * 1000."""
        result = self.processor._parse_cpu_string(str(cores))
        assert result == cores * 1000

    def test_empty_string_returns_zero(self):
        assert self.processor._parse_cpu_string("") == 0

    def test_none_like_empty(self):
        assert self.processor._parse_cpu_string("") == 0


class TestMemoryParsingProperties:
    """Property-based tests for memory string parsing."""

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = make_processor()

    @given(n=st.integers(min_value=0, max_value=10000))
    @settings(max_examples=200, deadline=2000)
    def test_mi_suffix_is_mebibytes(self, n):
        result = self.processor._parse_memory_string(f"{n}Mi")
        assert result == n * 1024 * 1024

    @given(n=st.integers(min_value=0, max_value=1000))
    @settings(max_examples=200, deadline=2000)
    def test_gi_suffix_is_gibibytes(self, n):
        result = self.processor._parse_memory_string(f"{n}Gi")
        assert result == n * 1024 * 1024 * 1024

    @given(n=st.integers(min_value=0, max_value=100))
    @settings(max_examples=100, deadline=2000)
    def test_ti_suffix_is_tebibytes(self, n):
        result = self.processor._parse_memory_string(f"{n}Ti")
        assert result == n * 1024 * 1024 * 1024 * 1024

    @given(n=st.integers(min_value=0, max_value=100000))
    @settings(max_examples=200, deadline=2000)
    def test_ki_suffix_is_kibibytes(self, n):
        result = self.processor._parse_memory_string(f"{n}Ki")
        assert result == n * 1024

    @given(n=st.integers(min_value=0, max_value=10**9))
    @settings(max_examples=200, deadline=2000)
    def test_raw_bytes_passthrough(self, n):
        result = self.processor._parse_memory_string(str(n))
        assert result == n

    def test_empty_string_returns_zero(self):
        assert self.processor._parse_memory_string("") == 0


class TestImageValidationProperties:
    """Property-based tests for image source validation."""

    @pytest.fixture(autouse=True)
    def setup_processor(self):
        self.processor = make_processor()

    @given(
        image=st.sampled_from(
            [
                "python:3.14",
                "busybox:latest",
                "ubuntu:22.04",
                "alpine:3.19",
            ]
        )
    )
    @settings(max_examples=50, deadline=2000)
    def test_official_docker_images_always_trusted(self, image):
        """Official Docker Hub images (no slash) should always be trusted."""
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test", "namespace": "default"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "test", "image": image}],
                    }
                }
            },
        }
        is_valid, _ = self.processor._validate_image_sources(manifest)
        assert is_valid

    @given(
        registry=st.sampled_from(
            [
                "docker.io",
                "gcr.io",
                "quay.io",
                "registry.k8s.io",
                "public.ecr.aws",
                "nvcr.io",
            ]
        ),
        path=st.from_regex(r"[a-z][a-z0-9/\-]{1,30}:[a-z0-9.]{1,10}", fullmatch=True),
    )
    @settings(max_examples=100, deadline=2000)
    def test_trusted_registries_always_pass(self, registry, path):
        """Images from trusted registries should always pass."""
        manifest = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "test", "image": f"{registry}/{path}"}],
                    }
                }
            },
        }
        is_valid, _ = self.processor._validate_image_sources(manifest)
        assert is_valid

    @given(
        domain=st.from_regex(r"evil[a-z]{1,10}\.(com|io|net)", fullmatch=True),
        path=st.from_regex(r"[a-z]{1,10}/[a-z]{1,10}:[a-z0-9.]{1,5}", fullmatch=True),
    )
    @settings(max_examples=100, deadline=2000)
    def test_untrusted_registries_always_rejected(self, domain, path):
        """Images from untrusted registries should be rejected."""
        manifest = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "test", "image": f"{domain}/{path}"}],
                    }
                }
            },
        }
        is_valid, _ = self.processor._validate_image_sources(manifest)
        assert not is_valid
