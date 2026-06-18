"""
End-to-end tests that cdk.json resource quota values flow into the
Kubernetes manifests applied on the cluster.

Replays the kubectl-applier Lambda's {{MP_*}} and {{QP_*}} placeholder
substitutions against 31-manifest-processor.yaml and
post-helm-sqs-consumer.yaml, then asserts the rendered output contains
the expected `value: "..."` env var entries and no leftover
placeholders. Complementary assertions check that the raw manifests
still carry the placeholder tokens so they're wired into the pipeline.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest


class TestResourceQuotaTemplateVars:
    """Verify resource quota template variables are replaced in manifests."""

    def test_manifest_processor_quotas_replaced(self):
        """Simulates the kubectl-applier replacement and verifies env vars are set."""
        from pathlib import Path

        content = Path(
            "lambda/kubectl-applier-simple/manifests/31-manifest-processor.yaml"
        ).read_text()

        # Simulate the replacements the CDK stack would produce
        replacements = {
            "{{MP_MAX_CPU_PER_MANIFEST}}": "96",
            "{{MP_MAX_MEMORY_PER_MANIFEST}}": "192Gi",
            "{{MP_MAX_GPU_PER_MANIFEST}}": "8",
            "{{MP_MAX_REQUEST_BODY_BYTES}}": "1048576",
            "{{MP_ALLOWED_NAMESPACES}}": "default,gco-jobs",
            # Image registry allowlist — augmented with the project's own
            # ECR registry hostnames in regional_stack.py via
            # ``_augment_trusted_registries_with_project_ecr``. The test
            # only needs the rendered substitution to be a non-empty
            # string; the augmentation itself is covered by
            # tests/test_trusted_registries_augmentation.py.
            "{{MP_TRUSTED_REGISTRIES}}": "docker.io,public.ecr.aws,123456789012.dkr.ecr.us-east-2.amazonaws.com",
            "{{MP_TRUSTED_DOCKERHUB_ORGS}}": "nvidia,pytorch",
        }
        for key, value in replacements.items():
            content = content.replace(key, value)

        # After replacement, the rendered manifest should contain the values
        assert 'value: "96"' in content
        assert 'value: "192Gi"' in content
        assert 'value: "8"' in content
        # And no unreplaced MP_ placeholders
        assert "{{MP_" not in content

    def test_queue_processor_quotas_replaced(self):
        """Simulates the kubectl-applier replacement and verifies env vars are set."""
        from pathlib import Path

        content = Path(
            "lambda/kubectl-applier-simple/manifests/post-helm-sqs-consumer.yaml"
        ).read_text()

        replacements = {
            "{{QP_MAX_CPU_PER_MANIFEST}}": "48",
            "{{QP_MAX_MEMORY_PER_MANIFEST}}": "128Gi",
            "{{QP_MAX_GPU_PER_MANIFEST}}": "4",
        }
        for key, value in replacements.items():
            content = content.replace(key, value)

        assert 'value: "48"' in content
        assert 'value: "128Gi"' in content
        assert 'value: "4"' in content
        assert "{{QP_MAX_CPU" not in content
        assert "{{QP_MAX_MEMORY" not in content
        assert "{{QP_MAX_GPU" not in content


class TestManifestProcessorEnvVars:
    """Verify the manifest processor K8s manifest has resource quota env vars."""

    @pytest.fixture
    def manifest_content(self):
        path = Path("lambda/kubectl-applier-simple/manifests/31-manifest-processor.yaml")
        return path.read_text()

    def test_has_max_cpu_env(self, manifest_content):
        assert "MAX_CPU_PER_MANIFEST" in manifest_content
        assert "{{MP_MAX_CPU_PER_MANIFEST}}" in manifest_content

    def test_has_max_memory_env(self, manifest_content):
        assert "MAX_MEMORY_PER_MANIFEST" in manifest_content
        assert "{{MP_MAX_MEMORY_PER_MANIFEST}}" in manifest_content

    def test_has_max_gpu_env(self, manifest_content):
        assert "MAX_GPU_PER_MANIFEST" in manifest_content
        assert "{{MP_MAX_GPU_PER_MANIFEST}}" in manifest_content


class TestQueueProcessorEnvVars:
    """Verify the queue processor K8s manifest has resource quota env vars."""

    @pytest.fixture
    def manifest_content(self):
        path = Path("lambda/kubectl-applier-simple/manifests/post-helm-sqs-consumer.yaml")
        return path.read_text()

    def test_has_max_cpu_env(self, manifest_content):
        assert "MAX_CPU_PER_MANIFEST" in manifest_content
        assert "{{QP_MAX_CPU_PER_MANIFEST}}" in manifest_content

    def test_has_max_memory_env(self, manifest_content):
        assert "MAX_MEMORY_PER_MANIFEST" in manifest_content
        assert "{{QP_MAX_MEMORY_PER_MANIFEST}}" in manifest_content

    def test_has_max_gpu_env(self, manifest_content):
        assert "MAX_GPU_PER_MANIFEST" in manifest_content
        assert "{{QP_MAX_GPU_PER_MANIFEST}}" in manifest_content


class TestResourceQuotaErrorMessages:
    """Verify resource limit errors include specific details and hints."""

    @pytest.fixture
    def processor(self):
        from gco.services.manifest_processor import ManifestProcessor

        with patch("gco.services.manifest_processor.config"):
            return ManifestProcessor(
                cluster_id="test",
                region="us-east-1",
                config_dict={
                    "max_cpu_per_manifest": "10",
                    "max_memory_per_manifest": "32Gi",
                    "max_gpu_per_manifest": 4,
                    "allowed_namespaces": ["default", "gco-jobs"],
                    "validation_enabled": True,
                },
            )

    def _make_job(self, cpu="1", memory="1Gi", gpu="0"):
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test", "namespace": "gco-jobs"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "main",
                                "image": "docker.io/python:3.12",
                                "resources": {
                                    "limits": {
                                        "cpu": cpu,
                                        "memory": memory,
                                        "nvidia.com/gpu": gpu,
                                    }
                                },
                            }
                        ]
                    }
                }
            },
        }

    def test_cpu_error_shows_values(self, processor):
        _, error = processor.validate_manifest(self._make_job(cpu="20"))
        assert "CPU" in error
        assert "20000m" in error
        assert "10000m" in error

    def test_memory_error_shows_values(self, processor):
        _, error = processor.validate_manifest(self._make_job(memory="64Gi"))
        assert "Memory" in error
        assert "64" in error
        assert "32" in error

    def test_gpu_error_shows_values(self, processor):
        _, error = processor.validate_manifest(self._make_job(gpu="8"))
        assert "GPU" in error
        assert "8" in error
        assert "4" in error

    def test_error_includes_cdk_hint(self, processor):
        _, error = processor.validate_manifest(self._make_job(cpu="20"))
        assert "cdk.json" in error

    def test_multiple_limits_exceeded(self, processor):
        _, error = processor.validate_manifest(self._make_job(cpu="20", memory="64Gi", gpu="8"))
        assert "CPU" in error
        assert "Memory" in error
        assert "GPU" in error

    def test_within_limits_passes(self, processor):
        valid, _ = processor.validate_manifest(self._make_job(cpu="4", memory="16Gi", gpu="2"))
        assert valid is True


# ── Template placeholder coverage (regression test) ─────────────────────
#
# Bug history: 31-manifest-processor.yaml introduced a {{MP_MAX_REQUEST_BODY_BYTES}}
# placeholder but the CDK stack's template_replacements dict was never
# updated to provide it. The kubectl-applier Lambda skips any manifest
# that still contains an unreplaced {{...}} placeholder (it treats that as
# "optional feature not enabled"). Result: the manifest-processor deployment
# was never re-applied, so new pod images (and the rest of the manifest
# changes along with them) never rolled out.
#
# This test enumerates every {{...}} placeholder in every manifest YAML
# and asserts that the CDK stack's regional_stack.py source file contains
# a matching string literal. If a placeholder is added to a manifest, CDK
# must be updated to provide it.


class TestManifestTemplatePlaceholderCoverage:
    """Every {{...}} placeholder in kubectl-applier manifests must have a
    corresponding replacement string literal in the CDK stack source."""

    MANIFEST_DIR = Path("lambda/kubectl-applier-simple/manifests")
    CDK_STACK = Path("gco/stacks/regional_stack.py")

    # Placeholders that are intentionally only provided for specific
    # features (FSx, Valkey, Mooncake store, etc.) and may be absent when
    # those features are disabled. The kubectl-applier's "skip on unreplaced
    # placeholder" behavior is correct for these.
    OPTIONAL_PLACEHOLDERS = frozenset(
        {
            "FSX_FILE_SYSTEM_ID",
            "FSX_DNS_NAME",
            "FSX_MOUNT_NAME",
            "FSX_SECURITY_GROUP_ID",
            "PRIVATE_SUBNET_ID",
            "VALKEY_ENDPOINT",
            "VALKEY_PORT",
        }
    )
    # Note: MOONCAKE_MASTER_IMAGE has a CDK replacement — it feeds the inference
    # monitor's env, and the monitor creates the shared Mooncake master on
    # demand (create-if-absent). There is no static master manifest.

    def _iter_placeholders(self):
        """Yield (filename, placeholder_name) for every {{NAME}} in every
        manifest YAML."""
        pattern = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
        for yaml_path in sorted(self.MANIFEST_DIR.glob("*.yaml")):
            try:
                content = yaml_path.read_text()
            except OSError:
                continue
            for match in pattern.finditer(content):
                yield yaml_path.name, match.group(1)

    def test_every_placeholder_has_cdk_replacement(self):
        """For every {{NAME}} placeholder used in a manifest, the CDK stack
        source must contain either {{NAME}} (as a dict key) somewhere, or
        NAME must be in OPTIONAL_PLACEHOLDERS."""
        cdk_src = self.CDK_STACK.read_text()

        missing = set()
        for filename, placeholder in self._iter_placeholders():
            if placeholder in self.OPTIONAL_PLACEHOLDERS:
                continue
            needle = "{{" + placeholder + "}}"
            if needle not in cdk_src:
                missing.add((filename, placeholder))

        assert not missing, (
            "The following kubectl-applier manifest placeholders have no "
            "matching replacement in gco/stacks/regional_stack.py. Any manifest "
            "containing these placeholders will be SKIPPED at deploy time by "
            "the kubectl-applier Lambda, leaving the associated Deployment/"
            "resource stale. Either add the replacement to the CDK stack or "
            "add the placeholder to OPTIONAL_PLACEHOLDERS in this test if "
            "it's intentionally feature-gated:\n  "
            + "\n  ".join(f"{fn}: {{{{{p}}}}}" for fn, p in sorted(missing))
        )
