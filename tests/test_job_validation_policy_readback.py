"""Reading back the job validation policy a region actually enforces.

A caller needs to know whether a cluster will admit a job *before* paying to
run it. Reading a local ``cdk.json`` cannot answer that: it is the input to a
deploy, not the state of one, and CDK augments ``trusted_registries`` with the
project's own ECR hostnames at synth time, so the effective allowlist is
strictly larger than the configured one.

These tests pin the read-back surface:

* ``ManifestProcessor.effective_job_validation_policy`` reports every field the
  validator actually compares against — a field that exists in ``__init__`` but
  is missing from the payload is a silent gap, because the caller cannot tell
  "not enforced" from "not reported";
* the reported values are the *instance* values, so a policy that differs from
  the defaults is reported as it is enforced;
* ``ManifestProcessor.cluster_resource_governance`` reads the second and third
  admission layers (LimitRange, ResourceQuota) from the Kubernetes API and
  fails soft, because a partial answer marked ``unavailable`` is honest whereas
  a 500 or a silently absent key is not;
* ``GET /api/v1/policy`` composes both and is JSON-serializable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from gco.services.manifest_processor import ManifestProcessor

# Reuse the manifest-API auth fixture rather than re-implementing the HMAC
# envelope: importing the autouse fixture registers it for this module too, so
# TestClient traffic runs through the real AuthenticationMiddleware exactly as
# it does in tests/test_manifest_api.py.
from tests.test_manifest_api import _seed_auth_cache  # noqa: F401


def _make_processor(**overrides: Any) -> ManifestProcessor:
    """Build a ManifestProcessor with Kubernetes config loading patched out."""
    config_dict: dict[str, Any] = {
        "max_cpu_per_manifest": "384",
        "max_memory_per_manifest": "4096Gi",
        "max_gpu_per_manifest": 16,
        "allowed_namespaces": ["gco-jobs"],
        "validation_enabled": True,
    }
    config_dict.update(overrides)
    with patch("gco.services.manifest_processor.config"):
        return ManifestProcessor(
            cluster_id="test-cluster",
            region="us-east-1",
            config_dict=config_dict,
        )


def _quota_list(name: str, hard: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(name=name),
                status=SimpleNamespace(hard=hard),
                spec=SimpleNamespace(hard=hard),
            )
        ]
    )


def _limit_range_list(name: str, limits: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(name=name),
                spec=SimpleNamespace(
                    limits=[
                        SimpleNamespace(
                            type=limit.get("type"),
                            max=limit.get("max"),
                            min=limit.get("min"),
                            default=limit.get("default"),
                            default_request=limit.get("defaultRequest"),
                        )
                        for limit in limits
                    ]
                ),
            )
        ]
    )


class TestEffectivePolicyCompleteness:
    """Every enforced dimension is reported. An unreported check is a trap."""

    def test_reports_all_top_level_dimensions(self) -> None:
        policy = _make_processor().effective_job_validation_policy()
        assert set(policy) == {
            "validation_enabled",
            "manifest_caps",
            "allowed_namespaces",
            "allowed_kinds",
            "allowed_api_versions",
            "trusted_registries",
            "trusted_dockerhub_orgs",
            "require_accelerator_toleration",
            "yaml_max_depth",
            "manifest_security_policy",
        }

    def test_reports_all_eight_security_flags(self) -> None:
        """All eight block_* toggles, so "absent" never reads as "off"."""
        policy = _make_processor().effective_job_validation_policy()
        assert set(policy["manifest_security_policy"]) == {
            "block_privileged",
            "block_privilege_escalation",
            "block_host_network",
            "block_host_pid",
            "block_host_ipc",
            "block_host_path",
            "block_added_capabilities",
            "block_run_as_root",
        }

    def test_caps_report_both_parsed_and_configured_forms(self) -> None:
        """384 vCPU and 384000 millicores are the same cap; say which is which."""
        processor = _make_processor()
        caps = processor.effective_job_validation_policy()["manifest_caps"]

        assert caps["max_cpu_millicores"] == processor.max_cpu_per_manifest
        assert caps["max_memory_bytes"] == processor.max_memory_per_manifest
        assert caps["max_gpu_count"] == processor.max_gpu_per_manifest
        assert set(caps["configured"]) == {
            "max_cpu_per_manifest",
            "max_memory_per_manifest",
            "max_gpu_per_manifest",
        }

    def test_caps_are_the_units_the_validator_compares_in(self) -> None:
        """max_cpu_millicores must be millicores, not vCPU."""
        caps = _make_processor().effective_job_validation_policy()["manifest_caps"]
        assert caps["max_cpu_millicores"] == 384_000
        assert caps["max_memory_bytes"] == 4096 * 1024**3
        assert caps["max_gpu_count"] == 16


class TestEffectivePolicyReflectsInstanceState:
    """The payload is the enforced policy, not a copy of the defaults."""

    def test_non_default_namespace_allowlist_is_reported(self) -> None:
        policy = _make_processor(
            allowed_namespaces=["gco-jobs", "team-b"]
        ).effective_job_validation_policy()
        assert policy["allowed_namespaces"] == ["gco-jobs", "team-b"]

    def test_non_default_registry_allowlist_is_reported(self) -> None:
        """The deploy-time ECR augmentation is exactly why this must be read back."""
        policy = _make_processor(
            trusted_registries=["docker.io", "123456789012.dkr.ecr.us-east-1.amazonaws.com"]
        ).effective_job_validation_policy()
        assert "123456789012.dkr.ecr.us-east-1.amazonaws.com" in policy["trusted_registries"]

    def test_disabled_validation_is_reported(self) -> None:
        policy = _make_processor(validation_enabled=False).effective_job_validation_policy()
        assert policy["validation_enabled"] is False

    def test_security_flag_flip_is_reported(self) -> None:
        policy = _make_processor(
            manifest_security_policy={"block_run_as_root": True}
        ).effective_job_validation_policy()
        assert policy["manifest_security_policy"]["block_run_as_root"] is True

    def test_collections_are_sorted_for_stable_diffing(self) -> None:
        policy = _make_processor(
            allowed_namespaces=["zeta", "alpha"]
        ).effective_job_validation_policy()
        assert policy["allowed_namespaces"] == sorted(policy["allowed_namespaces"])
        assert policy["allowed_kinds"] == sorted(policy["allowed_kinds"])

    def test_api_versions_are_scoped_to_allowed_kinds(self) -> None:
        """Reporting versions for a disallowed kind would imply it is submittable."""
        policy = _make_processor(allowed_kinds=["Job"]).effective_job_validation_policy()
        assert set(policy["allowed_api_versions"]) <= set(policy["allowed_kinds"])
        assert policy["allowed_api_versions"]["Job"] == ["batch/v1"]


class TestClusterResourceGovernance:
    """Layers 2 and 3 come from the live cluster, and degrade honestly."""

    def test_reports_quota_and_limit_range_per_namespace(self) -> None:
        processor = _make_processor()
        processor.core_v1 = MagicMock()
        processor.core_v1.list_namespaced_resource_quota.return_value = _quota_list(
            "gco-jobs-quota", {"requests.cpu": "400", "requests.nvidia.com/gpu": "32"}
        )
        processor.core_v1.list_namespaced_limit_range.return_value = _limit_range_list(
            "gco-jobs-limits", [{"type": "Container", "max": {"cpu": "192"}}]
        )

        governance = processor.cluster_resource_governance()

        assert governance["gco-jobs"]["status"] == "ok"
        assert governance["gco-jobs"]["resource_quotas"]["gco-jobs-quota"]["requests.cpu"] == "400"
        assert governance["gco-jobs"]["limit_ranges"]["gco-jobs-limits"][0]["max"] == {"cpu": "192"}

    def test_covers_every_allowed_namespace(self) -> None:
        processor = _make_processor(allowed_namespaces=["gco-jobs", "team-b"])
        processor.core_v1 = MagicMock()
        processor.core_v1.list_namespaced_resource_quota.return_value = _quota_list("q", {})
        processor.core_v1.list_namespaced_limit_range.return_value = _limit_range_list("l", [])

        assert set(processor.cluster_resource_governance()) == {"gco-jobs", "team-b"}

    def test_api_error_degrades_to_unavailable_with_a_reason(self) -> None:
        """A missing layer must be explicit, not inferred from an absent key."""
        processor = _make_processor()
        processor.core_v1 = MagicMock()
        processor.core_v1.list_namespaced_resource_quota.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        entry = processor.cluster_resource_governance()["gco-jobs"]

        assert entry["status"] == "unavailable"
        assert "403" in entry["reason"]
        assert "resource_quotas" not in entry

    def test_unexpected_error_also_degrades_rather_than_raising(self) -> None:
        processor = _make_processor()
        processor.core_v1 = MagicMock()
        processor.core_v1.list_namespaced_resource_quota.side_effect = RuntimeError("boom")

        entry = processor.cluster_resource_governance()["gco-jobs"]

        assert entry["status"] == "unavailable"
        assert "boom" in entry["reason"]

    def test_one_bad_namespace_does_not_hide_a_good_one(self) -> None:
        processor = _make_processor(allowed_namespaces=["good", "bad"])
        processor.core_v1 = MagicMock()

        def quota_side_effect(namespace: str, **_: Any) -> Any:
            if namespace == "bad":
                raise ApiException(status=404, reason="NotFound")
            return _quota_list("q", {"requests.cpu": "1"})

        processor.core_v1.list_namespaced_resource_quota.side_effect = quota_side_effect
        processor.core_v1.list_namespaced_limit_range.return_value = _limit_range_list("l", [])

        governance = processor.cluster_resource_governance()

        assert governance["good"]["status"] == "ok"
        assert governance["bad"]["status"] == "unavailable"


class TestPolicyEndpoint:
    """GET /api/v1/policy composes both layers and stays serializable."""

    @pytest.fixture
    def policy_processor(self) -> MagicMock:
        processor = MagicMock()
        processor.cluster_id = "test-cluster"
        processor.region = "us-east-1"
        processor.effective_job_validation_policy.return_value = {
            "validation_enabled": True,
            "allowed_namespaces": ["gco-jobs"],
        }
        processor.cluster_resource_governance.return_value = {
            "gco-jobs": {"status": "ok", "resource_quotas": {}, "limit_ranges": {}}
        }
        return processor

    def test_returns_policy_and_cluster_enforcement(self, policy_processor: MagicMock) -> None:
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=policy_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/policy", headers={})

        assert response.status_code == 200
        data = response.json()
        assert data["region"] == "us-east-1"
        assert data["cluster_id"] == "test-cluster"
        assert data["policy"]["allowed_namespaces"] == ["gco-jobs"]
        assert data["cluster_enforcement"]["gco-jobs"]["status"] == "ok"

    def test_names_its_source_as_the_deployed_runtime(self, policy_processor: MagicMock) -> None:
        """So a consumer never mistakes the response for a config-file read."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=policy_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/policy", headers={})

        assert response.json()["source"] == "deployed-cluster-runtime"

    def test_route_is_registered_and_advertised(self, policy_processor: MagicMock) -> None:
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=policy_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            routes = [getattr(route, "path", None) for route in app.routes]
            assert "/api/v1/policy" in routes

            with TestClient(app, raise_server_exceptions=False) as client:
                advertised = client.get("/", headers={}).json()["endpoints"]

        assert advertised["policy"] == "GET /api/v1/policy"


class TestAggregatorRouteAllowlist:
    """The cross-region aggregator may read the policy, and only read it."""

    def test_policy_is_a_read_only_entry(self) -> None:
        from gco.stacks.constants import AGGREGATOR_REGIONAL_API_ROUTES

        assert ("GET", "api/v1/policy") in AGGREGATOR_REGIONAL_API_ROUTES
        methods = {
            method for method, path in AGGREGATOR_REGIONAL_API_ROUTES if path == "api/v1/policy"
        }
        assert methods == {"GET"}
