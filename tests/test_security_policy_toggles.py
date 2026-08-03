"""
Tests for the configurable manifest_security_policy toggles on ManifestProcessor.

Exercises each toggle from cdk.json's job_validation_policy.manifest_security_policy
independently in both ON and OFF states: block_privileged, block_privilege_escalation,
block_host_network, block_host_pid, block_host_ipc, block_host_path,
block_added_capabilities, block_run_as_root. Uses a _make_processor helper
that builds a ManifestProcessor with the toggle overrides plus a
_job_manifest helper that applies targeted pod-spec/container overrides so
each test stays focused on one toggle.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from kubernetes import config as k8s_config

# Shared test fixture: a benign /tmp path used inside a Kubernetes manifest
# literal, not a filesystem operation. Pulled out as a module-level constant
# so `# nosec B108` stays pinned to this single line regardless of how the
# formatter reflows the manifest dict below.
_FIXTURE_HOST_PATH = "/tmp"  # nosec B108 - K8s manifest fixture string, not a filesystem operation
_UNSET = object()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_k8s_config():
    """Mock Kubernetes configuration loading."""
    with patch("gco.services.manifest_processor.config") as mock_config:
        mock_config.ConfigException = k8s_config.ConfigException
        mock_config.load_incluster_config.side_effect = k8s_config.ConfigException("Not in cluster")
        mock_config.load_kube_config.return_value = None
        yield mock_config


def _make_processor(mock_k8s_config, manifest_security_policy=_UNSET, extra_config=None):
    """Create a ManifestProcessor with custom manifest_security_policy overrides."""
    from gco.services.manifest_processor import ManifestProcessor

    config_dict = {
        "max_cpu_per_manifest": "10",
        "max_memory_per_manifest": "32Gi",
        "max_gpu_per_manifest": 4,
        "allowed_namespaces": ["default", "gco-jobs"],
        "validation_enabled": True,
    }
    if manifest_security_policy is not _UNSET:
        config_dict["manifest_security_policy"] = manifest_security_policy
    if extra_config:
        config_dict.update(extra_config)

    with patch("gco.services.manifest_processor.client"):
        return ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict=config_dict,
        )


def _job_manifest(
    pod_spec_overrides=None,
    container_overrides=None,
    kind="Job",
    namespace="default",
):
    """Build a manifest with optional overrides."""
    container = {"name": "worker", "image": "python:3.14"}
    if container_overrides:
        container.update(container_overrides)

    pod_spec = {"containers": [container], "restartPolicy": "Never"}
    if pod_spec_overrides:
        pod_spec.update(pod_spec_overrides)

    manifest = {
        "apiVersion": "batch/v1",
        "kind": kind,
        "metadata": {"name": "test-resource", "namespace": namespace},
        "spec": {"template": {"spec": pod_spec}},
    }
    return manifest


# ===========================================================================
# Individual toggle tests
# ===========================================================================


class TestBlockPrivilegedToggle:
    """block_privileged toggle."""

    def test_privileged_accepted_when_disabled(self, mock_k8s_config):
        """block_privileged: false → privileged containers accepted."""
        processor = _make_processor(
            mock_k8s_config, manifest_security_policy={"block_privileged": False}
        )
        manifest = _job_manifest(container_overrides={"securityContext": {"privileged": True}})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"Should accept privileged when disabled: {error}"

    def test_privileged_rejected_when_enabled(self, mock_k8s_config):
        """block_privileged: true (default) → privileged containers rejected."""
        processor = _make_processor(mock_k8s_config)
        manifest = _job_manifest(container_overrides={"securityContext": {"privileged": True}})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


class TestBlockPrivilegeEscalationToggle:
    """block_privilege_escalation toggle."""

    def test_priv_escalation_accepted_when_disabled(self, mock_k8s_config):
        """block_privilege_escalation: false → allowPrivilegeEscalation accepted."""
        processor = _make_processor(
            mock_k8s_config,
            manifest_security_policy={"block_privilege_escalation": False},
        )
        manifest = _job_manifest(
            container_overrides={"securityContext": {"allowPrivilegeEscalation": True}}
        )
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"Should accept priv escalation when disabled: {error}"

    def test_priv_escalation_rejected_when_enabled(self, mock_k8s_config):
        """block_privilege_escalation: true (default) → rejected."""
        processor = _make_processor(mock_k8s_config)
        manifest = _job_manifest(
            container_overrides={"securityContext": {"allowPrivilegeEscalation": True}}
        )
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


class TestBlockHostNetworkToggle:
    """block_host_network toggle."""

    def test_host_network_accepted_when_disabled(self, mock_k8s_config):
        """block_host_network: false → hostNetwork accepted."""
        processor = _make_processor(
            mock_k8s_config,
            manifest_security_policy={"block_host_network": False},
        )
        manifest = _job_manifest(pod_spec_overrides={"hostNetwork": True})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"Should accept hostNetwork when disabled: {error}"

    def test_host_network_rejected_when_enabled(self, mock_k8s_config):
        """block_host_network: true (default) → hostNetwork rejected."""
        processor = _make_processor(mock_k8s_config)
        manifest = _job_manifest(pod_spec_overrides={"hostNetwork": True})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


class TestBlockHostPIDToggle:
    """block_host_pid toggle."""

    def test_host_pid_accepted_when_disabled(self, mock_k8s_config):
        """block_host_pid: false → hostPID accepted."""
        processor = _make_processor(
            mock_k8s_config,
            manifest_security_policy={"block_host_pid": False},
        )
        manifest = _job_manifest(pod_spec_overrides={"hostPID": True})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"Should accept hostPID when disabled: {error}"

    def test_host_pid_rejected_when_enabled(self, mock_k8s_config):
        """block_host_pid: true (default) → hostPID rejected."""
        processor = _make_processor(mock_k8s_config)
        manifest = _job_manifest(pod_spec_overrides={"hostPID": True})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


class TestBlockHostIPCToggle:
    """block_host_ipc toggle."""

    def test_host_ipc_accepted_when_disabled(self, mock_k8s_config):
        """block_host_ipc: false → hostIPC accepted."""
        processor = _make_processor(
            mock_k8s_config,
            manifest_security_policy={"block_host_ipc": False},
        )
        manifest = _job_manifest(pod_spec_overrides={"hostIPC": True})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"Should accept hostIPC when disabled: {error}"

    def test_host_ipc_rejected_when_enabled(self, mock_k8s_config):
        """block_host_ipc: true (default) → hostIPC rejected."""
        processor = _make_processor(mock_k8s_config)
        manifest = _job_manifest(pod_spec_overrides={"hostIPC": True})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


class TestBlockHostPathToggle:
    """block_host_path toggle."""

    def test_host_path_accepted_when_disabled(self, mock_k8s_config):
        """block_host_path: false → hostPath volumes accepted."""
        processor = _make_processor(
            mock_k8s_config,
            manifest_security_policy={"block_host_path": False},
        )
        manifest = _job_manifest(
            pod_spec_overrides={
                "volumes": [{"name": "host-vol", "hostPath": {"path": _FIXTURE_HOST_PATH}}]
            }
        )
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"Should accept hostPath when disabled: {error}"

    def test_host_path_rejected_when_enabled(self, mock_k8s_config):
        """block_host_path: true (default) → hostPath volumes rejected."""
        processor = _make_processor(mock_k8s_config)
        manifest = _job_manifest(
            pod_spec_overrides={
                "volumes": [{"name": "host-vol", "hostPath": {"path": _FIXTURE_HOST_PATH}}]
            }
        )
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


class TestBlockAddedCapabilitiesToggle:
    """block_added_capabilities toggle."""

    def test_capabilities_add_accepted_when_disabled(self, mock_k8s_config):
        """block_added_capabilities: false → capabilities.add accepted."""
        processor = _make_processor(
            mock_k8s_config,
            manifest_security_policy={"block_added_capabilities": False},
        )
        manifest = _job_manifest(
            container_overrides={"securityContext": {"capabilities": {"add": ["SYS_ADMIN"]}}}
        )
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"Should accept capabilities.add when disabled: {error}"

    def test_capabilities_add_rejected_when_enabled(self, mock_k8s_config):
        """block_added_capabilities: true (default) → capabilities.add rejected."""
        processor = _make_processor(mock_k8s_config)
        manifest = _job_manifest(
            container_overrides={"securityContext": {"capabilities": {"add": ["SYS_ADMIN"]}}}
        )
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


class TestBlockRunAsRootToggle:
    """block_run_as_root toggle."""

    def test_run_as_root_rejected_when_enabled(self, mock_k8s_config):
        """block_run_as_root: true → runAsUser: 0 rejected."""
        processor = _make_processor(
            mock_k8s_config,
            manifest_security_policy={"block_run_as_root": True},
        )
        manifest = _job_manifest(container_overrides={"securityContext": {"runAsUser": 0}})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False, "runAsUser: 0 should be rejected when block_run_as_root is True"
        assert "root" in error.lower() or "runasuser" in error.lower()

    def test_run_as_root_accepted_when_disabled(self, mock_k8s_config):
        """block_run_as_root: false (default) → runAsUser: 0 accepted."""
        processor = _make_processor(mock_k8s_config)
        manifest = _job_manifest(container_overrides={"securityContext": {"runAsUser": 0}})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"runAsUser: 0 should be allowed by default: {error}"

    def test_run_as_root_pod_level_rejected_when_enabled(self, mock_k8s_config):
        """block_run_as_root: true → pod-level runAsUser: 0 rejected."""
        processor = _make_processor(
            mock_k8s_config,
            manifest_security_policy={"block_run_as_root": True},
        )
        manifest = _job_manifest(pod_spec_overrides={"securityContext": {"runAsUser": 0}})
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


# ===========================================================================
# Malformed policy values fail closed
# ===========================================================================


class TestMalformedSecurityPolicy:
    """Direct construction cannot bypass deployment-time validation."""

    @pytest.mark.parametrize(
        "policy",
        (
            None,
            "false",
            0,
            [],
            {"block_host_path": None},
            {"block_privileged": "false"},
            {"unknown_toggle": True},
        ),
    )
    def test_rejects_malformed_policy(self, mock_k8s_config, policy):
        with pytest.raises(ValueError, match="manifest_security_policy"):
            _make_processor(mock_k8s_config, manifest_security_policy=policy)

    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("require_accelerator_toleration", "true"),
            ("require_accelerator_toleration", None),
            ("validation_enabled", "false"),
            ("validation_enabled", 1),
        ),
    )
    def test_rejects_malformed_adjacent_boolean_controls(self, mock_k8s_config, field, value):
        with pytest.raises(ValueError, match=rf"{field} must be a boolean"):
            _make_processor(mock_k8s_config, extra_config={field: value})


# ===========================================================================
# allowed_kinds configurability
# ===========================================================================


class TestAllowedKindsConfigurability:
    """allowed_kinds list is configurable via config_dict."""

    def test_default_allowed_kinds_accepts_standard_kinds(self, mock_k8s_config):
        """Default allowed_kinds accepts Job, Deployment, etc."""
        processor = _make_processor(mock_k8s_config)
        for kind in [
            "Job",
            "Deployment",
            "CronJob",
            "StatefulSet",
            "DaemonSet",
            "Service",
            "ConfigMap",
            "Pod",
        ]:
            manifest = _job_manifest(kind=kind)
            if kind == "CronJob":
                manifest["spec"] = {
                    "schedule": "*/5 * * * *",
                    "jobTemplate": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "worker",
                                            "image": "python:3.14",
                                        }
                                    ]
                                }
                            }
                        }
                    },
                }
            is_valid, error = processor.validate_manifest(manifest)
            assert is_valid is True, f"Default allowed_kinds should accept {kind}: {error}"

    def test_custom_allowed_kinds_rejects_deployment(self, mock_k8s_config):
        """Custom allowed_kinds: ["Job", "Service"] → Deployment rejected."""
        processor = _make_processor(
            mock_k8s_config,
            extra_config={"allowed_kinds": ["Job", "Service"]},
        )
        manifest = _job_manifest(kind="Deployment")
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False, "Deployment should be rejected with custom allowed_kinds"
        assert "not allowed" in error.lower()

    def test_custom_allowed_kinds_accepts_job(self, mock_k8s_config):
        """Custom allowed_kinds: ["Job", "Service"] → Job accepted."""
        processor = _make_processor(
            mock_k8s_config,
            extra_config={"allowed_kinds": ["Job", "Service"]},
        )
        manifest = _job_manifest(kind="Job")
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"Job should be accepted with custom allowed_kinds: {error}"

    def test_empty_allowed_kinds_rejects_everything(self, mock_k8s_config):
        """Empty allowed_kinds: everything rejected."""
        processor = _make_processor(
            mock_k8s_config,
            extra_config={"allowed_kinds": []},
        )
        for kind in ["Job", "Deployment", "Service", "Pod"]:
            manifest = _job_manifest(kind=kind)
            is_valid, error = processor.validate_manifest(manifest)
            assert is_valid is False, f"Empty allowed_kinds should reject {kind}"

    def test_adding_non_default_kind(self, mock_k8s_config):
        """Adding a non-default kind: ["Job", "NetworkPolicy"] → NetworkPolicy accepted."""
        processor = _make_processor(
            mock_k8s_config,
            extra_config={"allowed_kinds": ["Job", "NetworkPolicy"]},
        )
        manifest = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "test-netpol", "namespace": "default"},
            "spec": {},
        }
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is True, f"NetworkPolicy should be accepted: {error}"

    def test_adding_non_default_kind_rejects_unlisted(self, mock_k8s_config):
        """["Job", "NetworkPolicy"] → Deployment rejected."""
        processor = _make_processor(
            mock_k8s_config,
            extra_config={"allowed_kinds": ["Job", "NetworkPolicy"]},
        )
        manifest = _job_manifest(kind="Deployment")
        is_valid, error = processor.validate_manifest(manifest)
        assert is_valid is False


# ===========================================================================
# Default values — no manifest_security_policy or allowed_kinds in config
# ===========================================================================


class TestDefaultValues:
    """Defaults work correctly when config keys are absent."""

    def test_default_security_policy_blocks_privileged(self, mock_k8s_config):
        """No manifest_security_policy in config → block_privileged defaults to True."""
        processor = _make_processor(mock_k8s_config)
        assert processor.block_privileged is True

    def test_default_security_policy_blocks_privilege_escalation(self, mock_k8s_config):
        """No manifest_security_policy in config → block_privilege_escalation defaults to True."""
        processor = _make_processor(mock_k8s_config)
        assert processor.block_privilege_escalation is True

    def test_default_security_policy_blocks_host_network(self, mock_k8s_config):
        """No manifest_security_policy in config → block_host_network defaults to True."""
        processor = _make_processor(mock_k8s_config)
        assert processor.block_host_network is True

    def test_default_security_policy_blocks_host_pid(self, mock_k8s_config):
        """No manifest_security_policy in config → block_host_pid defaults to True."""
        processor = _make_processor(mock_k8s_config)
        assert processor.block_host_pid is True

    def test_default_security_policy_blocks_host_ipc(self, mock_k8s_config):
        """No manifest_security_policy in config → block_host_ipc defaults to True."""
        processor = _make_processor(mock_k8s_config)
        assert processor.block_host_ipc is True

    def test_default_security_policy_blocks_host_path(self, mock_k8s_config):
        """No manifest_security_policy in config → block_host_path defaults to True."""
        processor = _make_processor(mock_k8s_config)
        assert processor.block_host_path is True

    def test_default_security_policy_blocks_added_capabilities(self, mock_k8s_config):
        """No manifest_security_policy in config → block_added_capabilities defaults to True."""
        processor = _make_processor(mock_k8s_config)
        assert processor.block_added_capabilities is True

    def test_default_security_policy_allows_run_as_root(self, mock_k8s_config):
        """No manifest_security_policy in config → block_run_as_root defaults to False."""
        processor = _make_processor(mock_k8s_config)
        assert processor.block_run_as_root is False

    def test_default_allowed_kinds_used_when_absent(self, mock_k8s_config):
        """No allowed_kinds in config → default list is used."""
        processor = _make_processor(mock_k8s_config)
        expected = {
            "Job",
            "CronJob",
            "Deployment",
            "StatefulSet",
            "DaemonSet",
            "Service",
            "ConfigMap",
            "Pod",
        }
        assert processor.allowed_kinds == expected


# ===========================================================================
# All toggles disabled — dangerous manifests pass
# ===========================================================================


class TestAllTogglesDisabled:
    """All security checks disabled — dangerous manifests pass."""

    @pytest.fixture
    def permissive_processor(self, mock_k8s_config):
        """Processor with all security toggles disabled."""
        return _make_processor(
            mock_k8s_config,
            manifest_security_policy={
                "block_privileged": False,
                "block_privilege_escalation": False,
                "block_host_network": False,
                "block_host_pid": False,
                "block_host_ipc": False,
                "block_host_path": False,
                "block_added_capabilities": False,
                "block_run_as_root": False,
            },
        )

    def test_privileged_passes(self, permissive_processor):
        manifest = _job_manifest(container_overrides={"securityContext": {"privileged": True}})
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"

    def test_priv_escalation_passes(self, permissive_processor):
        manifest = _job_manifest(
            container_overrides={"securityContext": {"allowPrivilegeEscalation": True}}
        )
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"

    def test_host_network_passes(self, permissive_processor):
        manifest = _job_manifest(pod_spec_overrides={"hostNetwork": True})
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"

    def test_host_pid_passes(self, permissive_processor):
        manifest = _job_manifest(pod_spec_overrides={"hostPID": True})
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"

    def test_host_ipc_passes(self, permissive_processor):
        manifest = _job_manifest(pod_spec_overrides={"hostIPC": True})
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"

    def test_host_path_passes(self, permissive_processor):
        manifest = _job_manifest(
            pod_spec_overrides={"volumes": [{"name": "host-vol", "hostPath": {"path": "/"}}]}
        )
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"

    def test_capabilities_add_passes(self, permissive_processor):
        manifest = _job_manifest(
            container_overrides={
                "securityContext": {"capabilities": {"add": ["SYS_ADMIN", "NET_RAW"]}}
            }
        )
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"

    def test_run_as_root_passes(self, permissive_processor):
        manifest = _job_manifest(container_overrides={"securityContext": {"runAsUser": 0}})
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"

    def test_everything_dangerous_at_once_passes(self, permissive_processor):
        """A manifest with every dangerous pattern at once passes."""
        manifest = _job_manifest(
            pod_spec_overrides={
                "hostNetwork": True,
                "hostPID": True,
                "hostIPC": True,
                "securityContext": {"runAsUser": 0},
                "volumes": [{"name": "host-vol", "hostPath": {"path": "/"}}],
            },
            container_overrides={
                "securityContext": {
                    "privileged": True,
                    "allowPrivilegeEscalation": True,
                    "runAsUser": 0,
                    "capabilities": {"add": ["SYS_ADMIN"]},
                }
            },
        )
        is_valid, error = permissive_processor.validate_manifest(manifest)
        assert is_valid is True, f"Should pass with all toggles disabled: {error}"


# ===========================================================================
# All toggles enabled (including block_run_as_root) — everything rejected
# ===========================================================================


class TestAllTogglesEnabled:
    """All security checks enabled (including block_run_as_root)."""

    @pytest.fixture
    def strict_processor(self, mock_k8s_config):
        """Processor with all security toggles enabled."""
        return _make_processor(
            mock_k8s_config,
            manifest_security_policy={
                "block_privileged": True,
                "block_privilege_escalation": True,
                "block_host_network": True,
                "block_host_pid": True,
                "block_host_ipc": True,
                "block_host_path": True,
                "block_added_capabilities": True,
                "block_run_as_root": True,
            },
        )

    def test_privileged_rejected(self, strict_processor):
        manifest = _job_manifest(container_overrides={"securityContext": {"privileged": True}})
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_priv_escalation_rejected(self, strict_processor):
        manifest = _job_manifest(
            container_overrides={"securityContext": {"allowPrivilegeEscalation": True}}
        )
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_host_network_rejected(self, strict_processor):
        manifest = _job_manifest(pod_spec_overrides={"hostNetwork": True})
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_host_pid_rejected(self, strict_processor):
        manifest = _job_manifest(pod_spec_overrides={"hostPID": True})
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_host_ipc_rejected(self, strict_processor):
        manifest = _job_manifest(pod_spec_overrides={"hostIPC": True})
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_host_path_rejected(self, strict_processor):
        manifest = _job_manifest(
            pod_spec_overrides={"volumes": [{"name": "host-vol", "hostPath": {"path": "/"}}]}
        )
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_capabilities_add_rejected(self, strict_processor):
        manifest = _job_manifest(
            container_overrides={"securityContext": {"capabilities": {"add": ["NET_RAW"]}}}
        )
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_run_as_root_container_rejected(self, strict_processor):
        manifest = _job_manifest(container_overrides={"securityContext": {"runAsUser": 0}})
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_run_as_root_pod_level_rejected(self, strict_processor):
        manifest = _job_manifest(pod_spec_overrides={"securityContext": {"runAsUser": 0}})
        is_valid, _ = strict_processor.validate_manifest(manifest)
        assert is_valid is False

    def test_safe_manifest_still_accepted(self, strict_processor):
        """A clean manifest with no dangerous patterns still passes."""
        manifest = _job_manifest()
        is_valid, error = strict_processor.validate_manifest(manifest)
        assert is_valid is True, f"Safe manifest should pass even with strict policy: {error}"
