"""
Tests for gco/config/config_loader.ConfigLoader.

Drives ConfigLoader against a MockApp/MockNode pair that surfaces a
hand-crafted CDK context dict. Verifies happy-path loading of every
top-level field (project_name, deployment_regions, kubernetes_version,
resource_thresholds, global_accelerator, inference_proxy,
manifest_processor, job_validation_policy, api_gateway, tags) and
that missing required fields raise ConfigValidationError with an
informative message. Companion suite to test_config_loader_validation.py
which drills into the validation rules themselves.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError
from gco.stacks.constants import cloudformation_region_partitions


class MockNode:
    """Mock CDK Node for testing."""

    def __init__(self, context: dict):
        self._context = context

    def try_get_context(self, key: str):
        return self._context.get(key)

    def get_all_context(self) -> dict:
        return self._context


class MockApp:
    """Mock CDK App for testing."""

    def __init__(self, context: dict):
        self.node = MockNode(context)


def minimal_context() -> dict:
    """The smallest context that passes validation: every optional block is omitted.

    Validation always runs, so the getters' defaults for omitted blocks are
    observed through a context that carries only the required fields.
    """
    return {
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
        },
        "kubernetes_version": "1.37",
        "resource_thresholds": {"cpu_threshold": 80, "memory_threshold": 80, "gpu_threshold": -1},
        "manifest_processor": {
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
        },
        "job_validation_policy": {"allowed_namespaces": ["gco-jobs"], "resource_quotas": {}},
        "api_gateway": {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        },
    }


@pytest.fixture
def valid_context():
    """Create valid configuration context."""
    return {
        "project_name": "gco",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-east-1", "us-west-2"],
        },
        "kubernetes_version": "1.37",
        "resource_thresholds": {"cpu_threshold": 80, "memory_threshold": 85, "gpu_threshold": 90},
        "global_accelerator": {
            "name": "gco-accelerator",
            "health_check_interval": 30,
            "health_check_path": "/api/v1/health",
        },
        "alb_config": {
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        },
        "inference_proxy": {
            "tls_proxy_cpu_request_millicores": 100,
            "tls_proxy_cpu_target_utilization_percentage": 70,
        },
        "manifest_processor": {
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
            "validation_enabled": True,
        },
        "job_validation_policy": {
            "allowed_namespaces": ["gco-jobs"],
            "resource_quotas": {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
            },
        },
        "api_gateway": {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        },
        "tags": {"Environment": "test"},
    }


class TestConfigLoaderValidation:
    """Tests for configuration validation."""

    def test_valid_configuration(self, valid_context):
        """Test that valid configuration passes validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.get_project_name() == "gco"

    def test_missing_required_field(self, valid_context):
        """Test that missing required field raises error."""
        del valid_context["deployment_regions"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="Required configuration field 'deployment_regions' must be a non-empty object",
        ):
            ConfigLoader(app)

    def test_malformed_deployment_regions_object(self, valid_context):
        """Non-object Region configuration fails with an actionable error."""
        valid_context["deployment_regions"] = ["us-east-1"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match=re.escape("deployment_regions must be an object, got list")
        ):
            ConfigLoader(app)

    def test_empty_context_is_validated(self):
        """An empty context is validated like any other, so required fields fail it."""
        with pytest.raises(
            ConfigValidationError,
            match="Required configuration field 'kubernetes_version' is missing",
        ):
            ConfigLoader(MockApp({}))

    def test_missing_project_name_defaults_and_still_validates(self):
        """Without project_name the documented default applies and every block is checked.

        An absent project_name used to skip all validation, so an invalid region,
        Global Accelerator interval, TLS lifetime or API Gateway log level was
        synthesized as written.
        """
        context = minimal_context()
        config = ConfigLoader(MockApp(context))
        assert config.get_project_name() == "gco"
        assert config.get_regions() == ["us-east-1"]
        invalid_blocks = {
            "deployment_regions": {**context["deployment_regions"], "regional": ["us-east-99"]},
            "global_accelerator": {"health_check_interval": 15},
            "backend_tls": {"leaf_validity_days": 5000},
            "api_gateway": {**context["api_gateway"], "log_level": "DEBUG"},
        }
        for key, value in invalid_blocks.items():
            with pytest.raises(ConfigValidationError):
                ConfigLoader(MockApp({**context, key: value}))

    @pytest.mark.parametrize("project_name", ["", "Invalid_Name", 7])
    def test_explicit_project_name_is_never_replaced_by_the_default(self, project_name):
        """Only an absent project_name defaults; an explicit invalid one is an error."""
        context = {**minimal_context(), "project_name": project_name}
        with pytest.raises(ConfigValidationError, match="Invalid project_name"):
            ConfigLoader(MockApp(context))


def test_config_and_public_stack_exports_import_in_a_clean_interpreter() -> None:
    """Cold import order must not recreate the ConfigLoader/stack cycle."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gco.config import ConfigLoader; from gco.stacks import GCOGlobalStack",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


class TestRegionValidation:
    """Tests for region validation."""

    def test_valid_regions(self, valid_context):
        """SDK-known regions outside the former project subset pass validation."""
        valid_context["deployment_regions"]["regional"] = ["ap-south-1"]
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.get_regions() == ["ap-south-1"]

    def test_invalid_region(self, valid_context):
        """Test invalid region raises error."""
        valid_context["deployment_regions"]["regional"] = ["us-east-1", "invalid-region"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="Invalid region 'invalid-region'"):
            ConfigLoader(app)

    @pytest.mark.parametrize("field", ["global", "api_gateway", "monitoring"])
    def test_invalid_stack_region(self, valid_context, field):
        """Every scalar stack Region must be SDK-known for CloudFormation."""
        valid_context["deployment_regions"][field] = "invalid-region"
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=rf"deployment_regions\.{field} is not a valid region: 'invalid-region'",
        ):
            ConfigLoader(app)

    def test_duplicate_regions(self, valid_context):
        """Test duplicate regions raises error."""
        valid_context["deployment_regions"]["regional"] = ["us-east-1", "us-east-1"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="Duplicate regions"):
            ConfigLoader(app)

    def test_mixed_aws_partitions(self, valid_context):
        """A deployment cannot span accounts or credentials partitions."""
        valid_context["deployment_regions"]["regional"] = ["us-east-1", "cn-north-1"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="single AWS partition"):
            ConfigLoader(app)

    def test_single_noncommercial_partition_is_valid_without_global_accelerator(
        self, valid_context
    ):
        """A coherent SDK partition stays deployable through regional IAM APIs."""
        region, partition = next(
            (region, partition)
            for region, partition in cloudformation_region_partitions().items()
            if partition != "aws"
        )
        valid_context["deployment_regions"] = {
            "global": region,
            "api_gateway": region,
            "monitoring": region,
            "regional": [region],
        }
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_deployment_partition() == partition
        assert config.supports_global_accelerator() is False

    def test_more_than_ten_regions(self, valid_context):
        """The deployment contract has no project-specific Region-count cap."""
        regions = sorted(
            region
            for region, partition in cloudformation_region_partitions().items()
            if partition == "aws"
        )[:11]
        assert len(regions) == 11
        valid_context["deployment_regions"]["regional"] = regions
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.get_regions() == regions


class TestResourceThresholdsValidation:
    """Tests for resource threshold validation."""

    def test_valid_thresholds(self, valid_context):
        """Test valid thresholds pass validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        thresholds = config.get_resource_thresholds()
        assert thresholds.cpu_threshold == 80
        assert thresholds.memory_threshold == 85
        assert thresholds.gpu_threshold == 90

    def test_threshold_out_of_range(self, valid_context):
        """Test threshold out of range raises error."""
        valid_context["resource_thresholds"]["cpu_threshold"] = 150
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="cpu_threshold must be an integer between 0 and 100"
        ):
            ConfigLoader(app)

    def test_missing_threshold(self, valid_context):
        """Test missing threshold raises error."""
        del valid_context["resource_thresholds"]["gpu_threshold"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match=r"resource_thresholds\.gpu_threshold is required"
        ):
            ConfigLoader(app)


class TestGlobalAcceleratorValidation:
    """Tests for Global Accelerator config validation."""

    def test_valid_ga_config(self, valid_context):
        """Test valid GA config passes validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        ga_config = config.get_global_accelerator_config()
        assert ga_config["name"] == "gco-accelerator"

    @pytest.mark.parametrize("path", ["/api/v1/jobs", "/health", "/api/v1/health/", "/"])
    def test_health_check_path_that_needs_authentication_fails(self, valid_context, path):
        """A probe of an authenticated path gets 401, so GA would mark every Region unhealthy."""
        valid_context["global_accelerator"]["health_check_path"] = path
        with pytest.raises(ConfigValidationError) as raised:
            ConfigLoader(MockApp(valid_context))
        message = str(raised.value)
        assert message.startswith(f"global_accelerator.health_check_path {path!r} needs request")
        assert "/api/v1/health, /healthz, /metrics, /readyz" in message

    @pytest.mark.parametrize("path", ["/healthz", "/readyz", "/api/v1/health?source=ga"])
    def test_health_check_path_the_services_answer_unauthenticated(self, valid_context, path):
        valid_context["global_accelerator"]["health_check_path"] = path
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_global_accelerator_config()["health_check_path"] == path

    def test_invalid_health_check_path(self, valid_context):
        """Test health check path not starting with / raises error."""
        valid_context["global_accelerator"]["health_check_path"] = "api/health"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="health_check_path must start with '/'"):
            ConfigLoader(app)

    @pytest.mark.parametrize("interval", [0, 15, 60, True, "30"])
    def test_invalid_health_check_interval(self, valid_context, interval):
        """The API accepts only 10 or 30 seconds; everything else fails synth."""
        valid_context["global_accelerator"]["health_check_interval"] = interval
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("global_accelerator.health_check_interval must be one of"),
        ):
            ConfigLoader(app)

    @pytest.mark.parametrize("interval", [10, 30])
    def test_valid_health_check_interval(self, valid_context, interval):
        """Both API-supported probe intervals pass validation."""
        valid_context["global_accelerator"]["health_check_interval"] = interval
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_global_accelerator_config()["health_check_interval"] == interval

    @pytest.mark.parametrize("threshold", [0, 11, -1, True, "3"])
    def test_invalid_health_check_threshold(self, valid_context, threshold):
        """ThresholdCount outside the API's 1-10 range fails synth."""
        valid_context["global_accelerator"]["health_check_threshold"] = threshold
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("global_accelerator.health_check_threshold must be an integer between"),
        ):
            ConfigLoader(app)

    def test_valid_health_check_threshold(self, valid_context):
        """An in-range threshold passes validation and survives the merge."""
        valid_context["global_accelerator"]["health_check_threshold"] = 5
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_global_accelerator_config()["health_check_threshold"] == 5

    def test_client_affinity_defaults_to_none(self, valid_context):
        """Test client_affinity defaults to NONE when omitted."""
        valid_context["global_accelerator"].pop("client_affinity", None)
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        ga_config = config.get_global_accelerator_config()
        # When the key is present in context it is returned as-is; when the
        # whole context is absent the loader falls back to a default dict that
        # pins NONE. Either way, an omitted key is valid and resolves to NONE
        # at the stack layer.
        assert ga_config.get("client_affinity", "NONE") == "NONE"

    @pytest.mark.parametrize("affinity", ["NONE", "SOURCE_IP", "source_ip", "none"])
    def test_valid_client_affinity(self, valid_context, affinity):
        """Test accepted client_affinity values pass validation."""
        valid_context["global_accelerator"]["client_affinity"] = affinity
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.get_global_accelerator_config()["client_affinity"] == affinity

    def test_invalid_client_affinity(self, valid_context):
        """Test an unknown client_affinity value raises an error."""
        valid_context["global_accelerator"]["client_affinity"] = "STICKY"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="client_affinity must be one of"):
            ConfigLoader(app)

    def test_non_string_client_affinity(self, valid_context):
        """Test a non-string client_affinity value raises an error."""
        valid_context["global_accelerator"]["client_affinity"] = 1
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="client_affinity must be one of"):
            ConfigLoader(app)


class TestRegionOverlayNames:
    """Per-Region overlays are keyed by real Regions; undeployed ones only warn."""

    @pytest.mark.parametrize("key", ["fsx_lustre_regions", "pcs_regions", "hyperpod_regions"])
    def test_an_unknown_region_name_fails(self, valid_context, key):
        valid_context[key] = {"us-east1": {"enabled": True}}
        with pytest.raises(
            ConfigValidationError,
            match=rf"{key}\.us-east1 is not an AWS Region known to the installed SDK",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("key", ["fsx_lustre_regions", "pcs_regions", "hyperpod_regions"])
    def test_a_region_that_is_not_deployed_warns(self, valid_context, key):
        valid_context[key] = {"eu-west-1": {"enabled": False}}
        config = ConfigLoader(MockApp(valid_context))
        assert [w for w in config.warnings if w.startswith(key)] == [
            f"{key}.eu-west-1 has no effect: eu-west-1 is not in deployment_regions.regional, "
            "so no regional stack reads it (expected while staging a Region before adding it)."
        ]

    def test_deployed_regions_draw_no_warning(self, valid_context):
        valid_context["fsx_lustre_regions"] = {"us-east-1": {"enabled": True}}
        config = ConfigLoader(MockApp(valid_context))
        assert not [w for w in config.warnings if "fsx_lustre_regions" in w]


class TestKubernetesVersion:
    """kubernetes_version is a quoted "<major>.<minor>" string."""

    @pytest.mark.parametrize("version", ["1.37", "1.30", "1.0", "2.10"])
    def test_minor_version_strings_are_accepted(self, valid_context, version):
        valid_context["kubernetes_version"] = version
        assert ConfigLoader(MockApp(valid_context)).get_kubernetes_version() == version

    @pytest.mark.parametrize("version", [1.30, 1.37, 2, "1.30.1", "v1.30", "1.030", " 1.30", True])
    def test_anything_else_fails_at_synth(self, valid_context, version):
        """A JSON number 1.30 used to become the version "1.3"."""
        valid_context["kubernetes_version"] = version
        with pytest.raises(
            ConfigValidationError, match=r'kubernetes_version must be a "<major>\.<minor>" string'
        ):
            ConfigLoader(MockApp(valid_context))


class TestPublicAccessCidrs:
    """eks_cluster.public_access_cidrs is a list of IPv4 CIDRs."""

    @pytest.mark.parametrize("cidrs", [[], ["203.0.113.7/32"], ["0.0.0.0/0", "10.1.0.0/16"]])
    def test_lists_of_cidrs_are_accepted(self, valid_context, cidrs):
        valid_context["eks_cluster"] = {
            "endpoint_access": "PUBLIC_AND_PRIVATE",
            "public_access_cidrs": cidrs,
        }
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_eks_cluster_config()["public_access_cidrs"] == cidrs

    @pytest.mark.parametrize(
        ("cidrs", "invalid"),
        [
            ("203.0.113.7/32", "'203.0.113.7/32'"),
            (["203.0.113.7"], "'203.0.113.7'"),
            (["203.0.113.0/33"], "'203.0.113.0/33'"),
            (["300.0.0.0/8"], "'300.0.0.0/8'"),
            (["2001:db8::/32"], "'2001:db8::/32'"),
            ([24], "24"),
            ({"cidr": "10.0.0.0/8"}, "{'cidr': '10.0.0.0/8'}"),
        ],
    )
    def test_anything_else_fails_at_synth(self, valid_context, cidrs, invalid):
        """A string used to be split into one-character "CIDRs"."""
        valid_context["eks_cluster"] = {"public_access_cidrs": cidrs}
        with pytest.raises(ConfigValidationError) as raised:
            ConfigLoader(MockApp(valid_context))
        message = str(raised.value)
        assert message.startswith("eks_cluster.public_access_cidrs must be a list of IPv4 CIDR")
        assert message.endswith(f"(invalid: {invalid})")


class TestManifestProcessorPolicyDefaults:
    """job_validation_policy has one source of defaults, not a second copy in the getter."""

    def test_omitted_trust_lists_are_not_invented_by_the_getter(self, valid_context):
        """The getter's lists (with "gco" as a registry, without vllm) never reached a service."""
        mp_config = ConfigLoader(MockApp(valid_context)).get_manifest_processor_config()
        assert "trusted_registries" not in mp_config
        assert "trusted_dockerhub_orgs" not in mp_config

    def test_the_policy_block_is_merged_as_written(self, valid_context):
        valid_context["job_validation_policy"]["trusted_registries"] = ["registry.example.com"]
        mp_config = ConfigLoader(MockApp(valid_context)).get_manifest_processor_config()
        assert mp_config["trusted_registries"] == ["registry.example.com"]
        assert (
            mp_config["resource_quotas"]
            == valid_context["job_validation_policy"]["resource_quotas"]
        )


class TestManifestProcessorImage:
    """manifest_processor.image was required but never read: deprecated, no longer required."""

    def test_absent_image_is_valid(self, valid_context):
        assert "image" not in valid_context["manifest_processor"]
        config = ConfigLoader(MockApp(valid_context))
        assert not [w for w in config.warnings if "manifest_processor.image" in w]
        assert "image" not in config.get_manifest_processor_config()

    def test_an_image_key_warns_and_is_ignored(self, valid_context):
        valid_context["manifest_processor"]["image"] = "example.com/anything:1"
        config = ConfigLoader(MockApp(valid_context))
        assert [w for w in config.warnings if w.startswith("manifest_processor.image")] == [
            "manifest_processor.image is deprecated and ignored: nothing reads it: the regional "
            "stack builds the manifest processor image from "
            "dockerfiles/Dockerfile.manifest-processor and deploys it from ECR. Remove it from "
            "cdk.json."
        ]


class TestPendingThresholdDefaults:
    """An omitted pending_* threshold means the same thing to the loader and the monitor."""

    def test_omitted_keys_match_the_health_monitor_fallbacks(self, valid_context, monkeypatch):
        from gco.services.health_monitor import thresholds_from_env

        for variable in (
            "PENDING_PODS_THRESHOLD",
            "PENDING_REQUESTED_CPU_VCPUS",
            "PENDING_REQUESTED_MEMORY_GB",
            "PENDING_REQUESTED_GPUS",
        ):
            monkeypatch.delenv(variable, raising=False)
        loaded = ConfigLoader(MockApp(valid_context)).get_resource_thresholds()
        monitor = thresholds_from_env()
        for field in (
            "pending_pods_threshold",
            "pending_requested_cpu_vcpus",
            "pending_requested_memory_gb",
            "pending_requested_gpus",
        ):
            assert getattr(loaded, field) == getattr(monitor, field), field
        assert loaded.pending_requested_gpus == 8

    def test_the_shipped_file_disables_the_pending_gpu_check(self):
        import json
        from pathlib import Path

        context = json.loads(
            (Path(__file__).resolve().parents[1] / "cdk.json").read_text(encoding="utf-8")
        )["context"]
        thresholds = ConfigLoader(MockApp(context)).get_resource_thresholds()
        assert thresholds.pending_requested_gpus == -1
        assert thresholds.is_disabled("pending_requested_gpus")


class TestResourceQuota:
    """resource_quota is merged over its defaults and validated by the loader too."""

    def test_absent_block_is_the_default_quota(self, valid_context):
        from gco.resource_governance import DEFAULT_RESOURCE_QUOTA

        assert ConfigLoader(MockApp(valid_context)).get_resource_quota() == dict(
            DEFAULT_RESOURCE_QUOTA
        )

    def test_partial_block_keeps_the_other_defaults(self, valid_context):
        valid_context["resource_quota"] = {"max_cpu": "500", "_comment_max_cpu": "doc"}
        quota = ConfigLoader(MockApp(valid_context)).get_resource_quota()
        assert quota["max_cpu"] == "500"
        assert "_comment_max_cpu" not in quota

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            ({"max_cpus": "1"}, "has unknown key(s) ['max_cpus']"),
            (
                {"max_cpu": "lots"},
                "resource_quota.max_cpu='lots' is not a valid Kubernetes quantity",
            ),
            ({"container_max_cpu": "500"}, "resource_quota.container_max_cpu='500' exceeds"),
            ([], "'resource_quota' must be an object"),
        ],
    )
    def test_invalid_blocks_fail_validation(self, valid_context, block, message):
        valid_context["resource_quota"] = block
        with pytest.raises(ConfigValidationError, match=re.escape(message)):
            ConfigLoader(MockApp(valid_context))

    def test_the_stack_keeps_its_old_import(self):
        from gco.resource_governance import validated_resource_quota
        from gco.stacks.regional_stack import _validated_resource_quota

        assert _validated_resource_quota is validated_resource_quota


class TestGlobalAcceleratorName:
    """global_accelerator.name follows the CreateAccelerator naming rule."""

    @pytest.mark.parametrize("name", ["gco-accelerator", "A", "team.b-accelerator", "x" * 64])
    def test_valid_names_are_accepted(self, valid_context, name):
        valid_context["global_accelerator"]["name"] = name
        assert ConfigLoader(MockApp(valid_context)).get_global_accelerator_config()["name"] == name

    @pytest.mark.parametrize(
        "name", ["", "-gco", "gco-", ".gco", "gco.", "gco accelerator", "gco_x", "x" * 65, 5]
    )
    def test_invalid_names_fail_at_synth(self, valid_context, name):
        valid_context["global_accelerator"]["name"] = name
        with pytest.raises(
            ConfigValidationError, match=r"global_accelerator\.name must be 1-64 letters"
        ):
            ConfigLoader(MockApp(valid_context))

    def test_the_default_name_follows_the_project(self, valid_context):
        del valid_context["global_accelerator"]["name"]
        valid_context["project_name"] = "team-b"
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_global_accelerator_config()["name"] == "team-b-accelerator"


class TestTrafficDialValidation:
    """Tests for the global_accelerator.traffic_dial sub-block."""

    def test_defaults_are_disabled_monitor(self, valid_context):
        """The dial controller defaults to disabled, monitor mode."""
        config = ConfigLoader(MockApp(valid_context))
        dial = config.get_global_accelerator_config()["traffic_dial"]
        assert dial == {
            "enabled": False,
            "mode": "monitor",
            "interval_minutes": 5,
            "lookback_minutes": 15,
            "min_dial_percentage": 10,
            "max_step_percentage": 20,
            "full_health_percentage": 95,
        }

    def test_partial_traffic_dial_block_deep_merges(self, valid_context):
        """Overriding one dial knob keeps the sub-block's other defaults."""
        valid_context["global_accelerator"]["traffic_dial"] = {"enabled": True}
        config = ConfigLoader(MockApp(valid_context))
        dial = config.get_global_accelerator_config()["traffic_dial"]
        assert dial["enabled"] is True
        assert dial["mode"] == "monitor"
        assert dial["min_dial_percentage"] == 10

    def test_non_mapping_traffic_dial_rejected(self, valid_context):
        """A scalar traffic_dial value fails with a precise message."""
        valid_context["global_accelerator"]["traffic_dial"] = 5
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("global_accelerator.traffic_dial must be an object, got int: 5"),
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("enabled", [1, "true", None])
    def test_non_bool_enabled_rejected(self, valid_context, enabled):
        """traffic_dial.enabled must be a literal JSON boolean."""
        valid_context["global_accelerator"]["traffic_dial"] = {"enabled": enabled}
        with pytest.raises(
            ConfigValidationError,
            match=r"global_accelerator\.traffic_dial\.enabled must be a bool",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("mode", ["MONITOR", "Enforce", "monitor", "enforce"])
    def test_mode_is_case_insensitive(self, valid_context, mode):
        """Both controller modes validate regardless of case."""
        valid_context["global_accelerator"]["traffic_dial"] = {"mode": mode}
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_global_accelerator_config()["traffic_dial"]["mode"] == mode

    @pytest.mark.parametrize("mode", ["observe", "dry-run", 1, None])
    def test_invalid_mode_rejected(self, valid_context, mode):
        """Unknown controller modes fail synth."""
        valid_context["global_accelerator"]["traffic_dial"] = {"mode": mode}
        with pytest.raises(
            ConfigValidationError,
            match=r"global_accelerator\.traffic_dial\.mode must be one of",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("interval_minutes", 0),
            ("interval_minutes", 1_441),
            ("lookback_minutes", 0),
            ("min_dial_percentage", -1),
            ("min_dial_percentage", 101),
            ("max_step_percentage", 0),
            ("full_health_percentage", 0),
            ("full_health_percentage", 101),
            ("max_step_percentage", True),
            ("interval_minutes", "5"),
        ],
    )
    def test_out_of_range_dial_knobs_rejected(self, valid_context, field, value):
        """Every numeric dial knob enforces its documented range."""
        valid_context["global_accelerator"]["traffic_dial"] = {field: value}
        with pytest.raises(
            ConfigValidationError,
            match=rf"global_accelerator\.traffic_dial\.{field} must be an integer between",
        ):
            ConfigLoader(MockApp(valid_context))

    def test_min_dial_zero_is_valid(self, valid_context):
        """A zero floor is legal — TrafficDialPercentage accepts 0."""
        valid_context["global_accelerator"]["traffic_dial"] = {"min_dial_percentage": 0}
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_global_accelerator_config()["traffic_dial"]["min_dial_percentage"] == 0


class TestApiGatewayValidation:
    """Tests for API Gateway config validation."""

    def test_valid_api_gateway_config(self, valid_context):
        """Test valid API Gateway config passes validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        api_config = config.get_api_gateway_config()
        assert api_config["throttle_rate_limit"] == 1000

    def test_invalid_log_level(self, valid_context):
        """Test invalid log level raises error."""
        valid_context["api_gateway"]["log_level"] = "DEBUG"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="log_level must be one of"):
            ConfigLoader(app)

    def test_burst_less_than_rate(self, valid_context):
        """Test burst limit less than rate limit raises error."""
        valid_context["api_gateway"]["throttle_burst_limit"] = 500
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="throttle_burst_limit must be greater than or equal"
        ):
            ConfigLoader(app)

    def test_invalid_boolean_flag(self, valid_context):
        """Test non-boolean metrics_enabled raises error."""
        valid_context["api_gateway"]["metrics_enabled"] = "yes"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="metrics_enabled must be a boolean"):
            ConfigLoader(app)

    def test_ip_address_type_defaults_to_dualstack(self, valid_context):
        valid_context["api_gateway"].pop("ip_address_type", None)
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_api_gateway_config()["ip_address_type"] == "dualstack"

    @pytest.mark.parametrize("value", ["ipv4", "dualstack"])
    def test_ip_address_type_accepts_api_gateways_values(self, valid_context, value):
        valid_context["api_gateway"]["ip_address_type"] = value
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_api_gateway_config()["ip_address_type"] == value

    @pytest.mark.parametrize("value", ["ipv6", "DUALSTACK", "dual-stack", "", None, True])
    def test_ip_address_type_rejects_anything_else(self, valid_context, value):
        valid_context["api_gateway"]["ip_address_type"] = value
        with pytest.raises(
            ConfigValidationError,
            match=r"api_gateway.ip_address_type must be one of \['ipv4', 'dualstack'\]",
        ):
            ConfigLoader(MockApp(valid_context))

    def test_ip_address_type_values_match_the_stacks(self):
        """Config validation and synthesis accept the same values and share a default."""
        from gco.config.config_loader import (
            API_GATEWAY_IP_ADDRESS_TYPES,
            DEFAULT_API_GATEWAY_IP_ADDRESS_TYPE,
        )
        from gco.stacks.api_ip_address_type import _IP_ADDRESS_TYPES, DEFAULT_IP_ADDRESS_TYPE

        assert set(API_GATEWAY_IP_ADDRESS_TYPES) == set(_IP_ADDRESS_TYPES)
        assert DEFAULT_API_GATEWAY_IP_ADDRESS_TYPE == DEFAULT_IP_ADDRESS_TYPE == "dualstack"

    def test_shipped_cdk_json_makes_the_apis_dual_stack(self):
        import json

        context = json.loads(Path("cdk.json").read_text(encoding="utf-8"))["context"]
        assert context["api_gateway"]["ip_address_type"] == "dualstack"

    @pytest.mark.parametrize("value", [True, False, "yes"])
    def test_regional_api_enabled_warns_and_is_ignored(self, valid_context, value):
        """The resource policies no longer read the flag, so any value only warns.

        It used to admit every principal of the account to the regional
        bridges; callers now need their own execute-api:Invoke permission
        whatever it says. Nothing reads it, so not even its type is checked.
        """
        valid_context["api_gateway"]["regional_api_enabled"] = value
        config = ConfigLoader(MockApp(valid_context))
        assert [w for w in config.warnings if w.startswith("api_gateway.")] == [
            "api_gateway.regional_api_enabled is deprecated and ignored: it no longer changes "
            "anything: the API resource policies admit no principal of the account, so a caller "
            "reaches the global API and every regional API bridge with its own "
            "execute-api:Invoke permission, whatever this says (docs/UPGRADING.md, API calls "
            "need an IAM permission). Remove it from cdk.json."
        ]

    def test_regional_api_enabled_has_no_default(self, valid_context):
        """Without the key, the loader supplies none: nothing reads it."""
        config = ConfigLoader(MockApp(valid_context))
        assert "regional_api_enabled" not in config.get_api_gateway_config()
        assert not [w for w in config.warnings if "regional_api_enabled" in w]


class TestConfigLoaderGetters:
    """Tests for ConfigLoader getter methods."""

    def test_get_cluster_config(self, valid_context):
        """Test getting cluster config for a region."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        cluster_config = config.get_cluster_config("us-east-1")

        assert cluster_config.region == "us-east-1"
        assert cluster_config.cluster_name == "gco-us-east-1"
        assert cluster_config.kubernetes_version == "1.37"

    def test_get_tags(self, valid_context):
        """Test getting tags."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        tags = config.get_tags()
        assert tags["Environment"] == "test"

    def test_get_manifest_processor_config(self, valid_context):
        """Test getting manifest processor config."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        mp_config = config.get_manifest_processor_config()
        assert mp_config["replicas"] == 3
        assert mp_config["allowed_namespaces"] == ["gco-jobs"]

    def test_alb_config_is_deprecated_and_its_values_are_ignored(self, valid_context):
        """alb_config validates with a warning; the ALB probe comes from gateway_health_check."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert not hasattr(config, "get_alb_config")
        assert config.get_gateway_health_check_config()["interval_seconds"] == 15
        assert [w.split(":")[0] for w in config.warnings] == [
            "alb_config is deprecated and ignored"
        ]


class TestInferenceProxyConfig:
    """Strict optional inference TLS proxy autoscaling configuration."""

    DEFAULTS = {
        "tls_proxy_cpu_request_millicores": 100,
        "tls_proxy_cpu_target_utilization_percentage": 70,
        "min_replicas": 3,
        "max_replicas": 10,
    }

    def test_omitted_section_uses_defaults(self, valid_context):
        """Legacy contexts without the optional block retain current behavior."""
        del valid_context["inference_proxy"]
        assert ConfigLoader(MockApp(valid_context)).get_inference_proxy_config() == self.DEFAULTS

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ({}, DEFAULTS),
            (
                {"tls_proxy_cpu_request_millicores": 125},
                {**DEFAULTS, "tls_proxy_cpu_request_millicores": 125},
            ),
            (
                {"tls_proxy_cpu_target_utilization_percentage": 85},
                {**DEFAULTS, "tls_proxy_cpu_target_utilization_percentage": 85},
            ),
            (
                {"min_replicas": 2, "max_replicas": 4},
                {**DEFAULTS, "min_replicas": 2, "max_replicas": 4},
            ),
        ],
    )
    def test_empty_and_partial_sections_merge_defaults(self, valid_context, configured, expected):
        """Empty and one-field objects preserve every unspecified default."""
        valid_context["inference_proxy"] = configured
        assert ConfigLoader(MockApp(valid_context)).get_inference_proxy_config() == expected

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("tls_proxy_cpu_request_millicores", 1),
            ("tls_proxy_cpu_request_millicores", 250),
            ("tls_proxy_cpu_target_utilization_percentage", 1),
            ("tls_proxy_cpu_target_utilization_percentage", 100),
            ("min_replicas", 1),
            ("max_replicas", 100),
        ],
    )
    def test_exact_boundaries_are_accepted(self, valid_context, field, value):
        """Both inclusive boundaries survive validation and default merging."""
        valid_context["inference_proxy"] = {field: value}
        assert ConfigLoader(MockApp(valid_context)).get_inference_proxy_config()[field] == value

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("tls_proxy_cpu_request_millicores", 0),
            ("tls_proxy_cpu_request_millicores", 251),
            ("tls_proxy_cpu_request_millicores", True),
            ("tls_proxy_cpu_request_millicores", 100.0),
            ("tls_proxy_cpu_request_millicores", "100"),
            ("tls_proxy_cpu_target_utilization_percentage", 0),
            ("tls_proxy_cpu_target_utilization_percentage", 101),
            ("tls_proxy_cpu_target_utilization_percentage", False),
            ("tls_proxy_cpu_target_utilization_percentage", 70.0),
            ("tls_proxy_cpu_target_utilization_percentage", "70"),
            ("min_replicas", 0),
            ("min_replicas", 51),
            ("max_replicas", 0),
            ("max_replicas", 101),
            ("max_replicas", "10"),
        ],
    )
    def test_out_of_range_and_non_exact_integers_are_rejected(self, valid_context, field, value):
        """Booleans, floats, strings, and values outside the range fail closed."""
        valid_context["inference_proxy"] = {field: value}
        with pytest.raises(
            ConfigValidationError,
            match=rf"inference_proxy\.{field} must be an integer between",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("value", [True, 1, 1.5, "invalid", []])
    def test_non_object_section_is_rejected(self, valid_context, value):
        """Malformed non-null sections never alias omission."""
        valid_context["inference_proxy"] = value
        with pytest.raises(ConfigValidationError, match="inference_proxy must be an object"):
            ConfigLoader(MockApp(valid_context))

    def test_json_null_uses_defaults_at_real_cdk_boundary(self):
        """CDK normalizes top-level JSON null to omission before validation."""
        from aws_cdk import App

        app = App(context={**minimal_context(), "inference_proxy": None})

        assert "inference_proxy" not in app.node.get_all_context()
        assert ConfigLoader(app).get_inference_proxy_config() == self.DEFAULTS

    def test_max_replicas_below_min_is_rejected(self, valid_context):
        """The HPA ceiling can never sit under its floor."""
        valid_context["inference_proxy"] = {"min_replicas": 5, "max_replicas": 4}
        with pytest.raises(
            ConfigValidationError,
            match=r"inference_proxy\.max_replicas must be at least inference_proxy\.min_replicas",
        ):
            ConfigLoader(MockApp(valid_context))

    def test_unknown_key_error_uses_fully_qualified_paths(self, valid_context):
        """Typos identify both the rejected and allowed fully qualified keys."""
        valid_context["inference_proxy"] = {"tls_proxy_cpu_request_millicore": 100}
        with pytest.raises(ConfigValidationError) as exc_info:
            ConfigLoader(MockApp(valid_context))

        message = str(exc_info.value)
        assert "inference_proxy.tls_proxy_cpu_request_millicore" in message
        assert "inference_proxy.tls_proxy_cpu_request_millicores" in message
        assert "inference_proxy.tls_proxy_cpu_target_utilization_percentage" in message


class TestManifestProcessorSizingConfig:
    """manifest_processor.replicas / resource_limits / autoscaling render into the Deployment.

    These keys used to be validated and then ignored (the manifest hardcoded
    three replicas and 1000m/2Gi). Now they are the source of truth for
    ``{{MP_REPLICAS}}``, ``{{MP_CPU_LIMIT}}``, ``{{MP_MEMORY_LIMIT}}`` and the
    optional ``35-manifest-processor-hpa.yaml`` gate, so validation has to
    catch everything Kubernetes would reject at apply time.
    """

    AUTOSCALING_DEFAULTS = {
        "enabled": False,
        "max_replicas": 6,
        "cpu_target_utilization_percentage": 70,
    }

    def test_defaults_when_autoscaling_is_omitted(self, valid_context):
        """No autoscaling block means off, with the documented bounds."""
        config = ConfigLoader(MockApp(valid_context)).get_manifest_processor_config()
        assert config["replicas"] == 3
        assert config["resource_limits"] == {"cpu": "1000m", "memory": "2Gi"}
        assert config["autoscaling"] == self.AUTOSCALING_DEFAULTS

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (None, AUTOSCALING_DEFAULTS),
            ({}, AUTOSCALING_DEFAULTS),
            ({"enabled": True}, {**AUTOSCALING_DEFAULTS, "enabled": True}),
            ({"max_replicas": 12}, {**AUTOSCALING_DEFAULTS, "max_replicas": 12}),
            (
                {"cpu_target_utilization_percentage": 55},
                {**AUTOSCALING_DEFAULTS, "cpu_target_utilization_percentage": 55},
            ),
        ],
    )
    def test_null_empty_and_partial_autoscaling_merge_defaults(
        self, valid_context, configured, expected
    ):
        """A nested JSON null or a one-key object keeps every other default."""
        valid_context["manifest_processor"]["autoscaling"] = configured
        config = ConfigLoader(MockApp(valid_context)).get_manifest_processor_config()
        assert config["autoscaling"] == expected

    def test_requests_constant_matches_the_manifest(self):
        """The floor the loader enforces is the request the manifest declares."""
        import yaml

        from gco.config import config_loader as module

        manifest = (
            Path(__file__).resolve().parent.parent
            / "lambda"
            / "kubectl-applier-simple"
            / "manifests"
            / "31-manifest-processor.yaml"
        ).read_text(encoding="utf-8")
        manifest = re.sub(r"\{\{[A-Z0-9_]+\}\}", "1", manifest)
        deployment = next(
            doc for doc in yaml.safe_load_all(manifest) if doc and doc["kind"] == "Deployment"
        )
        container = next(
            item
            for item in deployment["spec"]["template"]["spec"]["containers"]
            if item["name"] == "manifest-processor"
        )
        assert container["resources"]["requests"] == module._MANIFEST_PROCESSOR_CONTAINER_REQUESTS

    @pytest.mark.parametrize(
        "limits",
        [
            {"cpu": "500m", "memory": "1Gi"},
            {"cpu": "2", "memory": "4096Mi"},
            {"cpu": "1.5", "memory": "3G"},
        ],
    )
    def test_limits_at_or_above_the_requests_are_accepted(self, valid_context, limits):
        valid_context["manifest_processor"]["resource_limits"] = limits
        config = ConfigLoader(MockApp(valid_context)).get_manifest_processor_config()
        assert config["resource_limits"] == limits

    @pytest.mark.parametrize(
        ("resource", "value", "message"),
        [
            ("cpu", 1000, "must be a Kubernetes quantity string"),
            ("cpu", "", "must be a Kubernetes quantity string"),
            ("cpu", "one-core", "must be a Kubernetes quantity string"),
            ("memory", "2Gib", "must be a Kubernetes quantity string"),
            ("cpu", "499m", "must be at least the container request of '500m'"),
            ("memory", "512Mi", "must be at least the container request of '1Gi'"),
        ],
    )
    def test_malformed_or_undersized_limits_are_rejected(
        self, valid_context, resource, value, message
    ):
        """Anything the kubelet would reject fails at synth, not mid-deploy."""
        valid_context["manifest_processor"]["resource_limits"][resource] = value
        with pytest.raises(
            ConfigValidationError,
            match=rf"manifest_processor\.resource_limits\.{resource} {message}",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("value", [True, 1, "on", []])
    def test_non_object_autoscaling_is_rejected(self, valid_context, value):
        valid_context["manifest_processor"]["autoscaling"] = value
        with pytest.raises(
            ConfigValidationError, match=r"manifest_processor\.autoscaling must be an object"
        ):
            ConfigLoader(MockApp(valid_context))

    def test_unknown_autoscaling_key_lists_the_allowed_ones(self, valid_context):
        valid_context["manifest_processor"]["autoscaling"] = {"min_replicas": 2}
        with pytest.raises(ConfigValidationError) as exc_info:
            ConfigLoader(MockApp(valid_context))
        message = str(exc_info.value)
        assert (
            "manifest_processor.autoscaling contains unknown key(s): "
            "manifest_processor.autoscaling.min_replicas" in message
        )
        assert (
            "manifest_processor.autoscaling.cpu_target_utilization_percentage, "
            "manifest_processor.autoscaling.enabled, manifest_processor.autoscaling.max_replicas"
        ) in message

    @pytest.mark.parametrize("value", [None, "true", 1, 0])
    def test_autoscaling_enabled_must_be_a_boolean(self, valid_context, value):
        valid_context["manifest_processor"]["autoscaling"] = {"enabled": value}
        with pytest.raises(
            ConfigValidationError,
            match=r"manifest_processor\.autoscaling\.enabled must be a boolean",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("max_replicas", 0),
            ("max_replicas", 101),
            ("max_replicas", "6"),
            ("max_replicas", 6.0),
            ("max_replicas", True),
            ("cpu_target_utilization_percentage", 0),
            ("cpu_target_utilization_percentage", 101),
            ("cpu_target_utilization_percentage", "70"),
        ],
    )
    def test_autoscaling_bounds_must_be_exact_integers_in_range(self, valid_context, key, value):
        valid_context["manifest_processor"]["autoscaling"] = {key: value}
        with pytest.raises(
            ConfigValidationError,
            match=rf"manifest_processor\.autoscaling\.{key} must be an integer between 1 and 100",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize(
        ("key", "value"),
        [("max_replicas", 1), ("max_replicas", 100), ("cpu_target_utilization_percentage", 1)],
    )
    def test_autoscaling_boundaries_are_inclusive(self, valid_context, key, value):
        valid_context["manifest_processor"]["replicas"] = 1
        valid_context["manifest_processor"]["autoscaling"] = {key: value}
        config = ConfigLoader(MockApp(valid_context)).get_manifest_processor_config()
        assert config["autoscaling"][key] == value

    def test_autoscaling_ceiling_below_the_replica_floor_is_rejected(self, valid_context):
        """The HPA floor is ``replicas``; the ceiling can never sit under it."""
        valid_context["manifest_processor"]["replicas"] = 4
        valid_context["manifest_processor"]["autoscaling"] = {"max_replicas": 3}
        with pytest.raises(
            ConfigValidationError,
            match=(
                r"manifest_processor\.autoscaling\.max_replicas must be at least "
                r"manifest_processor\.replicas, got 3 < 4"
            ),
        ):
            ConfigLoader(MockApp(valid_context))


class TestNetworkPostureConfig:
    """eks_cluster.network_policy_enforcement and the vpc_endpoints block."""

    def test_enforcement_defaults_on(self, valid_context):
        config = ConfigLoader(MockApp(valid_context)).get_eks_cluster_config()
        assert config["network_policy_enforcement"] is True

    @pytest.mark.parametrize("value", [False, True])
    def test_enforcement_accepts_literal_booleans(self, valid_context, value):
        valid_context["eks_cluster"] = {"network_policy_enforcement": value}
        config = ConfigLoader(MockApp(valid_context)).get_eks_cluster_config()
        assert config["network_policy_enforcement"] is value
        # The merge keeps the other eks_cluster defaults intact.
        assert config["endpoint_access"] == "PRIVATE"

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_enforcement_rejects_non_booleans(self, valid_context, value):
        """The value renders verbatim into the kube-system ConfigMap."""
        valid_context["eks_cluster"] = {"network_policy_enforcement": value}
        with pytest.raises(
            ConfigValidationError,
            match=r"eks_cluster\.network_policy_enforcement must be a boolean",
        ):
            ConfigLoader(MockApp(valid_context))

    def test_vpc_endpoints_default_to_the_free_gateway_endpoints(self, valid_context):
        valid_context.pop("vpc_endpoints", None)
        config = ConfigLoader(MockApp(valid_context)).get_vpc_endpoints_config()
        assert config == {"gateway": ["s3", "dynamodb"], "interface": []}

    def test_vpc_endpoints_partial_block_keeps_the_other_default(self, valid_context):
        valid_context["vpc_endpoints"] = {"interface": ["sts", "ecr.api"]}
        config = ConfigLoader(MockApp(valid_context)).get_vpc_endpoints_config()
        assert config == {"gateway": ["s3", "dynamodb"], "interface": ["sts", "ecr.api"]}

    def test_vpc_endpoints_returns_copies(self, valid_context):
        """Callers mutating the result never bleed into the module defaults."""
        loader = ConfigLoader(MockApp(valid_context))
        loader.get_vpc_endpoints_config()["gateway"].append("mutated")
        assert loader.get_vpc_endpoints_config()["gateway"] == ["s3", "dynamodb"]

    def test_every_supported_interface_service_is_accepted(self, valid_context):
        from gco.config import config_loader as module

        valid_context["vpc_endpoints"] = {
            "gateway": [],
            "interface": list(module.VPC_INTERFACE_ENDPOINT_SERVICES),
        }
        config = ConfigLoader(MockApp(valid_context)).get_vpc_endpoints_config()
        assert config["gateway"] == []
        assert config["interface"] == list(module.VPC_INTERFACE_ENDPOINT_SERVICES)

    @pytest.mark.parametrize("value", [True, 1, "s3", ["s3"]])
    def test_vpc_endpoints_must_be_an_object(self, valid_context, value):
        valid_context["vpc_endpoints"] = value
        with pytest.raises(ConfigValidationError, match="vpc_endpoints must be an object"):
            ConfigLoader(MockApp(valid_context))

    def test_vpc_endpoints_unknown_key_lists_the_allowed_ones(self, valid_context):
        valid_context["vpc_endpoints"] = {"gateways": ["s3"]}
        with pytest.raises(ConfigValidationError) as exc_info:
            ConfigLoader(MockApp(valid_context))
        assert "vpc_endpoints contains unknown key(s): vpc_endpoints.gateways" in str(
            exc_info.value
        )
        assert "allowed keys: vpc_endpoints.gateway, vpc_endpoints.interface" in str(exc_info.value)

    @pytest.mark.parametrize(
        ("key", "value"),
        [("gateway", "s3"), ("gateway", [3]), ("interface", {"sts": True}), ("interface", [None])],
    )
    def test_vpc_endpoint_lists_must_be_lists_of_strings(self, valid_context, key, value):
        valid_context["vpc_endpoints"] = {key: value}
        with pytest.raises(
            ConfigValidationError, match=rf"vpc_endpoints\.{key} must be a list of strings"
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize(
        ("key", "value", "unsupported"),
        [
            ("gateway", ["s3", "sts"], "sts"),
            ("interface", ["ecr", "s3"], "ecr, s3"),
        ],
    )
    def test_unsupported_endpoint_services_fail_at_synth(
        self, valid_context, key, value, unsupported
    ):
        """A typo must not silently create nothing (interface endpoints bill)."""
        valid_context["vpc_endpoints"] = {key: value}
        with pytest.raises(ConfigValidationError) as exc_info:
            ConfigLoader(MockApp(valid_context))
        message = str(exc_info.value)
        assert f"vpc_endpoints.{key} contains unsupported service(s): {unsupported}" in message
        assert "supported:" in message

    def test_duplicate_endpoint_services_are_rejected(self, valid_context):
        valid_context["vpc_endpoints"] = {"interface": ["sts", "sts"]}
        with pytest.raises(
            ConfigValidationError, match=r"vpc_endpoints\.interface lists a service twice"
        ):
            ConfigLoader(MockApp(valid_context))


class TestDefaultValues:
    """Tests for default configuration values."""

    def test_default_project_name(self):
        """Test default project name."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        assert config.get_project_name() == "gco"

    def test_default_regions(self):
        """Test default regions."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        assert config.get_regions() == ["us-east-1"]

    def test_default_kubernetes_version(self):
        """Test default Kubernetes version."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        assert config.get_kubernetes_version() == "1.37"

    def test_omitted_valkey_config_is_disabled(self):
        """Valkey is opt-in when the entire context block is omitted."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        assert config.get_valkey_config()["enabled"] is False

    def test_default_resource_thresholds(self):
        """The optional thresholds a resource_thresholds block leaves out take their defaults."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        thresholds = config.get_resource_thresholds()
        # cpu/memory/gpu are required; minimal_context() sets the shipped values.
        assert thresholds.cpu_threshold == 80
        assert thresholds.memory_threshold == 80
        assert thresholds.gpu_threshold == -1
        assert thresholds.pending_pods_threshold == 10
        assert thresholds.pending_requested_cpu_vcpus == 100
        assert thresholds.pending_requested_memory_gb == 200
        assert thresholds.pending_requested_gpus == 8


class TestFsxLustreConfig:
    """Tests for FSx for Lustre configuration."""

    def test_get_fsx_lustre_config_defaults(self):
        """Test getting FSx config with defaults."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        fsx_config = config.get_fsx_lustre_config()

        assert fsx_config["enabled"] is False
        assert fsx_config["storage_capacity_gib"] == 1200
        assert fsx_config["deployment_type"] == "SCRATCH_2"
        assert fsx_config["file_system_type_version"] == "2.15"
        # SCRATCH_2 takes no per-unit storage throughput.
        assert fsx_config["per_unit_storage_throughput"] is None
        assert fsx_config["data_compression_type"] == "LZ4"
        assert fsx_config["import_path"] is None
        assert fsx_config["export_path"] is None
        assert fsx_config["auto_import_policy"] == "NEW_CHANGED_DELETED"
        assert "node_group" not in fsx_config

    def test_get_fsx_lustre_config_custom(self, valid_context):
        """Test getting FSx config with custom values."""
        valid_context["fsx_lustre"] = {
            "enabled": True,
            "storage_capacity_gib": 2400,
            "deployment_type": "PERSISTENT_2",
            "per_unit_storage_throughput": 500,
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        fsx_config = config.get_fsx_lustre_config()

        assert fsx_config["enabled"] is True
        assert fsx_config["storage_capacity_gib"] == 2400
        assert fsx_config["deployment_type"] == "PERSISTENT_2"
        assert fsx_config["per_unit_storage_throughput"] == 500
        # Defaults should still be present
        assert fsx_config["file_system_type_version"] == "2.15"

    def test_get_fsx_lustre_config_with_region_override(self, valid_context):
        """Test getting FSx config with region-specific override."""
        valid_context["fsx_lustre"] = {
            "enabled": True,
            "storage_capacity_gib": 1200,
        }
        valid_context["fsx_lustre_regions"] = {
            "us-west-2": {
                "storage_capacity_gib": 4800,
                "deployment_type": "PERSISTENT_1",
            }
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)

        # Without region - should use global config
        fsx_config_global = config.get_fsx_lustre_config()
        assert fsx_config_global["storage_capacity_gib"] == 1200

        # With region that has override
        fsx_config_west = config.get_fsx_lustre_config(region="us-west-2")
        assert fsx_config_west["storage_capacity_gib"] == 4800
        assert fsx_config_west["deployment_type"] == "PERSISTENT_1"

        # With region that has no override
        fsx_config_east = config.get_fsx_lustre_config(region="us-east-1")
        assert fsx_config_east["storage_capacity_gib"] == 1200

    def test_fsx_node_group_is_deprecated_and_ignored(self, valid_context):
        """node_group was documented but never read: it warns and is not returned."""
        valid_context["fsx_lustre"] = {
            "enabled": True,
            "node_group": {"instance_types": ["m6i.2xlarge"], "max_size": 20},
        }
        valid_context["fsx_lustre_regions"] = {"us-west-2": {"node_group": {"max_size": 5}}}
        config = ConfigLoader(MockApp(valid_context))
        assert "node_group" not in config.get_fsx_lustre_config()
        assert "node_group" not in config.get_fsx_lustre_config(region="us-west-2")
        deprecated = [w for w in config.warnings if "node_group" in w]
        assert [w.split(" is deprecated")[0] for w in deprecated] == [
            "fsx_lustre.node_group",
            "fsx_lustre_regions.us-west-2.node_group",
        ]

    @pytest.mark.parametrize(
        ("deployment_type", "expected"),
        [("PERSISTENT_1", 200), ("PERSISTENT_2", 250), ("SCRATCH_1", None), ("SCRATCH_2", None)],
    )
    def test_fsx_throughput_defaults_per_deployment_type(
        self, valid_context, deployment_type, expected
    ):
        """PERSISTENT_2 has no 200 setting, so its default is 250; SCRATCH types take none."""
        valid_context["fsx_lustre"] = {"enabled": True, "deployment_type": deployment_type}
        fsx_config = ConfigLoader(MockApp(valid_context)).get_fsx_lustre_config()
        assert fsx_config["per_unit_storage_throughput"] == expected

    def test_fsx_scratch_ignores_a_configured_throughput(self, valid_context):
        """v8.23.0 shipped 200 next to SCRATCH_2: it is ignored, without a warning."""
        valid_context["fsx_lustre"] = {
            "deployment_type": "SCRATCH_2",
            "per_unit_storage_throughput": 200,
        }
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_fsx_lustre_config()["per_unit_storage_throughput"] is None
        assert not [w for w in config.warnings if "fsx_lustre" in w]

    @pytest.mark.parametrize(
        ("deployment_type", "allowed", "rejected"),
        [
            ("PERSISTENT_1", (50, 100, 200), (125, 250, 12, 40)),
            ("PERSISTENT_2", (125, 250, 500, 1000), (50, 100, 200)),
        ],
    )
    def test_fsx_throughput_must_suit_the_deployment_type(
        self, valid_context, deployment_type, allowed, rejected
    ):
        for value in allowed:
            valid_context["fsx_lustre"] = {
                "deployment_type": deployment_type,
                "per_unit_storage_throughput": value,
            }
            config = ConfigLoader(MockApp(valid_context))
            assert config.get_fsx_lustre_config()["per_unit_storage_throughput"] == value
        for value in (*rejected, True, "200"):
            valid_context["fsx_lustre"] = {
                "deployment_type": deployment_type,
                "per_unit_storage_throughput": value,
            }
            with pytest.raises(
                ConfigValidationError,
                match=rf"fsx_lustre\.per_unit_storage_throughput must be one of .* for "
                rf"{deployment_type}",
            ):
                ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize(("value", "enabled"), [(True, True), (False, False)])
    def test_fsx_enabled_takes_json_booleans(self, valid_context, value, enabled):
        valid_context["fsx_lustre"] = {"enabled": value}
        assert ConfigLoader(MockApp(valid_context)).get_fsx_lustre_config()["enabled"] is enabled

    def test_fsx_region_overlay_string_false_is_rejected(self, valid_context):
        """An overlay flag follows the same rule, and the error names the overlay."""
        valid_context["fsx_lustre_regions"] = {"us-west-2": {"enabled": "false"}}
        with pytest.raises(
            ConfigValidationError,
            match=r"fsx_lustre_regions\.us-west-2\.enabled must be true or false .*"
            r"which deletes the FSx for Lustre file system and the data on it",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("value", [1, 0, None, "yes", "", [True]])
    def test_fsx_enabled_rejects_non_booleans(self, valid_context, value):
        valid_context["fsx_lustre"] = {"enabled": value}
        with pytest.raises(
            ConfigValidationError, match=r"fsx_lustre\.enabled must be true or false"
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            ({"deployment_type": "PERSISTENT_3"}, r"fsx_lustre\.deployment_type must be one of"),
            ({"deployment_type": "scratch_2"}, r"fsx_lustre\.deployment_type must be one of"),
            (
                {"storage_capacity_gib": 1000},
                r"storage_capacity_gib must be an integer of at least",
            ),
            ({"storage_capacity_gib": 3600}, r"multiple of 2400 GiB for SCRATCH_2"),
            (
                {"storage_capacity_gib": 4800, "deployment_type": "SCRATCH_1"},
                r"multiple of 3600 GiB for SCRATCH_1",
            ),
            ({"storage_capacity_gib": True}, r"storage_capacity_gib must be an integer"),
            ({"file_system_type_version": "2.16"}, r"file_system_type_version must be one of"),
            ({"file_system_type_version": 2.15}, r"file_system_type_version must be one of"),
            (
                {"deployment_type": "PERSISTENT_2", "file_system_type_version": "2.10"},
                r"2\.10 is not supported by PERSISTENT_2",
            ),
            ({"data_compression_type": "ZSTD"}, r"data_compression_type must be one of"),
            ({"auto_import_policy": "ALL"}, r"auto_import_policy must be one of"),
            ({"import_path": "my-bucket/data"}, r"import_path must be an S3 URI"),
            ({"import_path": "s3://"}, r"import_path must be an S3 URI"),
            ({"export_path": "s3://bucket/out"}, r"export_path needs an import_path"),
            (
                {"import_path": "s3://a/in", "export_path": "s3://b/out"},
                r"export_path must use the import_path bucket",
            ),
            ({"node_groups": {}}, r"fsx_lustre contains unknown key\(s\): fsx_lustre\.node_groups"),
            ("on", r"fsx_lustre must be an object, got str"),
        ],
    )
    def test_fsx_invalid_values_fail_with_the_key_path(self, valid_context, block, message):
        valid_context["fsx_lustre"] = block
        with pytest.raises(ConfigValidationError, match=message):
            ConfigLoader(MockApp(valid_context))

    def test_fsx_capacities_fsx_accepts(self, valid_context):
        for deployment_type, capacity in (
            ("SCRATCH_1", 1200),
            ("SCRATCH_1", 2400),
            ("SCRATCH_1", 7200),
            ("SCRATCH_2", 4800),
            ("PERSISTENT_2", 2400),
        ):
            valid_context["fsx_lustre"] = {
                "deployment_type": deployment_type,
                "storage_capacity_gib": capacity,
            }
            config = ConfigLoader(MockApp(valid_context))
            assert config.get_fsx_lustre_config()["storage_capacity_gib"] == capacity

    def test_fsx_same_bucket_import_and_export_are_accepted(self, valid_context):
        valid_context["fsx_lustre"] = {
            "import_path": "s3://data-bucket/training",
            "export_path": "s3://data-bucket/results",
        }
        fsx_config = ConfigLoader(MockApp(valid_context)).get_fsx_lustre_config()
        assert fsx_config["export_path"] == "s3://data-bucket/results"

    def test_fsx_region_overlay_errors_name_the_overlay(self, valid_context):
        """A bad value in an overlay names fsx_lustre_regions.<region>, not the global block."""
        valid_context["fsx_lustre_regions"] = {
            "us-west-2": {"deployment_type": "PERSISTENT_2", "per_unit_storage_throughput": 200}
        }
        with pytest.raises(
            ConfigValidationError,
            match=r"fsx_lustre_regions\.us-west-2\.per_unit_storage_throughput must be one of",
        ):
            ConfigLoader(MockApp(valid_context))

    def test_fsx_region_overlay_merges_over_a_global_value(self, valid_context):
        """A global PERSISTENT_2 throughput of 500 stays valid when an overlay changes size."""
        valid_context["fsx_lustre"] = {
            "deployment_type": "PERSISTENT_2",
            "per_unit_storage_throughput": 500,
        }
        valid_context["fsx_lustre_regions"] = {"us-west-2": {"storage_capacity_gib": 2400}}
        west = ConfigLoader(MockApp(valid_context)).get_fsx_lustre_config("us-west-2")
        assert (west["deployment_type"], west["per_unit_storage_throughput"]) == (
            "PERSISTENT_2",
            500,
        )
        assert west["storage_capacity_gib"] == 2400

    @pytest.mark.parametrize(
        ("overlays", "message"),
        [
            (["us-east-1"], r"fsx_lustre_regions must be an object, got list"),
            ({"us-east-1": "on"}, r"fsx_lustre_regions\.us-east-1 must be an object, got str"),
            (
                {"us-east-1": {"enable": True}},
                r"fsx_lustre_regions\.us-east-1 contains unknown key\(s\): "
                r"fsx_lustre_regions\.us-east-1\.enable",
            ),
        ],
    )
    def test_fsx_region_overlays_must_be_objects_of_known_keys(
        self, valid_context, overlays, message
    ):
        valid_context["fsx_lustre_regions"] = overlays
        with pytest.raises(ConfigValidationError, match=message):
            ConfigLoader(MockApp(valid_context))

    def test_fsx_override_forces_enabled(self, valid_context):
        valid_context["fsx_lustre"] = {"enabled": False}
        valid_context["feature_enabled_overrides"] = "fsx_lustre"
        assert ConfigLoader(MockApp(valid_context)).get_fsx_lustre_config()["enabled"] is True


class TestEksClusterConfig:
    """Tests for EKS cluster configuration."""

    def test_get_eks_cluster_config_defaults(self):
        """Test getting EKS cluster config with defaults."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        eks_config = config.get_eks_cluster_config()

        assert eks_config["endpoint_access"] == "PRIVATE"

    def test_get_eks_cluster_config_private(self, valid_context):
        """Test getting EKS cluster config with PRIVATE endpoint."""
        valid_context["eks_cluster"] = {"endpoint_access": "PRIVATE"}
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        eks_config = config.get_eks_cluster_config()

        assert eks_config["endpoint_access"] == "PRIVATE"

    def test_get_eks_cluster_config_public_and_private(self, valid_context):
        """Test getting EKS cluster config with PUBLIC_AND_PRIVATE endpoint."""
        valid_context["eks_cluster"] = {"endpoint_access": "PUBLIC_AND_PRIVATE"}
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        eks_config = config.get_eks_cluster_config()

        assert eks_config["endpoint_access"] == "PUBLIC_AND_PRIVATE"

    def test_eks_cluster_config_invalid_endpoint_access(self, valid_context):
        """Test that invalid endpoint_access raises validation error."""
        valid_context["eks_cluster"] = {"endpoint_access": "PUBLIC"}
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="endpoint_access must be one of",
        ):
            ConfigLoader(app)


class TestRegionAvailability:
    """Tests for region availability validation."""

    def test_validate_region_availability_success(self, valid_context):
        """Test validating region availability - success case."""
        from unittest.mock import MagicMock, patch

        app = MockApp(valid_context)
        config = ConfigLoader(app)

        with patch("gco.config.config_loader.boto3") as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.client.return_value = mock_ec2
            mock_ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-east-1"}]}

            result = config.validate_region_availability("us-east-1")
            assert result is True
            mock_ec2.describe_regions.assert_called_once_with(RegionNames=["us-east-1"])

    def test_validate_region_availability_failure(self, valid_context):
        """Test validating region availability - failure case."""
        from unittest.mock import MagicMock, patch

        app = MockApp(valid_context)
        config = ConfigLoader(app)

        with patch("gco.config.config_loader.boto3") as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.client.return_value = mock_ec2
            mock_ec2.describe_regions.side_effect = Exception("Region not available")

            result = config.validate_region_availability("invalid-region")
            assert result is False

    def test_get_available_regions_success(self, valid_context):
        """Test getting available regions - success case."""
        from unittest.mock import MagicMock, patch

        app = MockApp(valid_context)
        config = ConfigLoader(app)

        with patch("gco.config.config_loader.boto3") as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.client.return_value = mock_ec2
            mock_ec2.describe_regions.return_value = {
                "Regions": [
                    {"RegionName": "us-east-1"},
                    {"RegionName": "us-west-2"},
                    {"RegionName": "eu-west-1"},
                ]
            }

            result = config.get_available_regions()
            assert "us-east-1" in result
            assert "us-west-2" in result
            assert "eu-west-1" in result
            assert len(result) == 3

    def test_get_available_regions_failure_returns_valid_regions(self, valid_context):
        """Test getting available regions - failure returns VALID_REGIONS."""
        from unittest.mock import MagicMock, patch

        app = MockApp(valid_context)
        config = ConfigLoader(app)

        with patch("gco.config.config_loader.boto3") as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.client.return_value = mock_ec2
            mock_ec2.describe_regions.side_effect = Exception("API error")

            result = config.get_available_regions()
            # Should return VALID_REGIONS as fallback
            assert "us-east-1" in result
            assert len(result) == len(ConfigLoader.VALID_REGIONS)


class TestConfigValidationEdgeCases:
    """Tests for configuration validation edge cases."""

    def test_empty_regions_list_is_a_control_plane_only_topology(self, valid_context):
        """An empty workload list is valid: the control-plane stacks stand alone."""
        valid_context["deployment_regions"]["regional"] = []
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_regions() == []
        assert config.get_deployment_partition() == "aws"

    def test_non_list_regions_value_rejected(self, valid_context):
        """A scalar where the list belongs is still a configuration error."""
        valid_context["deployment_regions"]["regional"] = "us-east-1"
        with pytest.raises(ConfigValidationError, match="must be a list"):
            ConfigLoader(MockApp(valid_context))

    def test_missing_global_accelerator_config_applies_defaults(self, valid_context):
        """An absent global_accelerator block synthesizes with the defaults."""
        del valid_context["global_accelerator"]
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        ga_config = config.get_global_accelerator_config()
        assert ga_config["health_check_interval"] == 30
        assert ga_config["health_check_threshold"] == 3
        assert ga_config["health_check_path"] == "/api/v1/health"
        assert ga_config["client_affinity"] == "NONE"
        assert ga_config["traffic_dial"]["enabled"] is False
        assert ga_config["traffic_dial"]["mode"] == "monitor"

    def test_missing_alb_config_is_valid(self, valid_context):
        """alb_config is deprecated, so leaving it out is valid and warns about nothing."""
        del valid_context["alb_config"]
        app = MockApp(valid_context)
        assert ConfigLoader(app).warnings == []

    def test_missing_manifest_processor_config(self, valid_context):
        """Test that missing manifest_processor config raises error."""
        del valid_context["manifest_processor"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="manifest_processor configuration is required"
        ):
            ConfigLoader(app)

    def test_missing_api_gateway_config(self, valid_context):
        """Test that missing api_gateway config raises error."""
        del valid_context["api_gateway"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="api_gateway configuration is required"):
            ConfigLoader(app)

    def test_invalid_manifest_processor_replicas(self, valid_context):
        """Test that invalid manifest_processor replicas raises error."""
        valid_context["manifest_processor"]["replicas"] = 0
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match=r"manifest_processor\.replicas must be a positive integer"
        ):
            ConfigLoader(app)

    @pytest.mark.parametrize("value", [0, True, 10 * 1024 * 1024 + 1])
    def test_invalid_manifest_processor_request_body_limit(self, valid_context, value):
        """Request limits must remain positive integers within API Gateway's cap."""
        valid_context["manifest_processor"]["max_request_body_bytes"] = value
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("manifest_processor.max_request_body_bytes must be an integer"),
        ):
            ConfigLoader(app)

    @pytest.mark.parametrize("value", [None, "false", 1, 0])
    def test_invalid_manifest_processor_validation_enabled(self, valid_context, value):
        """The master validation switch must be a literal JSON boolean."""
        valid_context["manifest_processor"]["validation_enabled"] = value
        with pytest.raises(
            ConfigValidationError,
            match=r"manifest_processor\.validation_enabled must be a boolean",
        ):
            ConfigLoader(MockApp(valid_context))

    def test_missing_manifest_processor_resource_limits(self, valid_context):
        """Test that missing resource_limits fields raises error."""
        valid_context["manifest_processor"]["resource_limits"] = {"cpu": "1000m"}  # Missing memory
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="must contain 'cpu' and 'memory'"):
            ConfigLoader(app)

    def test_invalid_allowed_namespaces_type(self, valid_context):
        """allowed_namespaces now lives under job_validation_policy and must be a list."""
        valid_context["job_validation_policy"]["allowed_namespaces"] = "default"  # Should be list
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="allowed_namespaces must be a list"):
            ConfigLoader(app)

    def test_invalid_throttle_rate_limit(self, valid_context):
        """Test that invalid throttle_rate_limit raises error."""
        valid_context["api_gateway"]["throttle_rate_limit"] = 0
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="throttle_rate_limit must be a positive integer"
        ):
            ConfigLoader(app)

    def test_invalid_throttle_burst_limit(self, valid_context):
        """Test that invalid throttle_burst_limit raises error."""
        valid_context["api_gateway"]["throttle_burst_limit"] = -1
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="throttle_burst_limit must be a positive integer"
        ):
            ConfigLoader(app)

    def test_invalid_tracing_enabled_type(self, valid_context):
        """Test that non-boolean tracing_enabled raises error."""
        valid_context["api_gateway"]["tracing_enabled"] = "yes"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="tracing_enabled must be a boolean"):
            ConfigLoader(app)

    def test_legacy_health_check_keys_are_tolerated(self, valid_context):
        """Legacy grace-period/timeout keys are ignored, never validated.

        Global Accelerator endpoint groups have no such settings; the keys
        were historically validated but never consumed. Existing cdk.json
        files that still carry them (with any value) must keep synthesizing.
        """
        valid_context["global_accelerator"]["health_check_timeout"] = -5
        valid_context["global_accelerator"]["health_check_grace_period"] = "bogus"
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.get_global_accelerator_config()["health_check_interval"] == 30

    def test_partial_ga_block_keeps_defaults(self, valid_context):
        """A partial block keeps every unspecified default (merge, not fallback).

        Previously the getter fell back all-or-nothing, so a block missing
        health_check_path lost the default and failed validation. The merged
        getter fills it in.
        """
        del valid_context["global_accelerator"]["health_check_path"]
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        ga_config = config.get_global_accelerator_config()
        assert ga_config["health_check_path"] == "/api/v1/health"
        # Explicit keys still override their defaults.
        assert ga_config["name"] == "gco-accelerator"

    @pytest.mark.parametrize(
        "alb_config",
        [{"health_check_interval": 30}, {"healthy_threshold": -1}, {"health_check_timeout": "5"}],
    )
    def test_alb_config_contents_are_no_longer_validated(self, valid_context, alb_config):
        """The deprecated block is ignored whole: partial or invalid contents only warn."""
        valid_context["alb_config"] = alb_config
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.warnings[0].startswith("alb_config is deprecated and ignored")

    def test_missing_api_gateway_field(self, valid_context):
        """Test that missing API Gateway field raises error."""
        del valid_context["api_gateway"]["log_level"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match=r"api_gateway\.log_level is required"):
            ConfigLoader(app)

    def test_optional_pending_thresholds_negative(self, valid_context):
        """Test that -1 disables the pending pods threshold (no error)."""
        valid_context["resource_thresholds"]["pending_pods_threshold"] = -1
        app = MockApp(valid_context)
        loader = ConfigLoader(app)
        thresholds = loader.get_resource_thresholds()
        assert thresholds.pending_pods_threshold == -1

    def test_optional_pending_thresholds_invalid_negative(self, valid_context):
        """Test that negative values other than -1 raise error."""
        valid_context["resource_thresholds"]["pending_pods_threshold"] = -2
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="pending_pods_threshold"):
            ConfigLoader(app)

    def test_optional_pending_cpu_vcpus_negative(self, valid_context):
        """Test that negative pending_requested_cpu_vcpus raises error."""
        valid_context["resource_thresholds"]["pending_requested_cpu_vcpus"] = -10
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="pending_requested_cpu_vcpus must be a non-negative integer",
        ):
            ConfigLoader(app)

    def test_optional_pending_memory_gb_negative(self, valid_context):
        """Test that negative pending_requested_memory_gb raises error."""
        valid_context["resource_thresholds"]["pending_requested_memory_gb"] = -100
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="pending_requested_memory_gb must be a non-negative integer",
        ):
            ConfigLoader(app)

    def test_optional_pending_gpus_negative(self, valid_context):
        """Test that negative pending_requested_gpus raises error."""
        valid_context["resource_thresholds"]["pending_requested_gpus"] = -4
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="pending_requested_gpus must be a non-negative integer"
        ):
            ConfigLoader(app)

    def test_missing_manifest_processor_field(self, valid_context):
        """Missing a required manifest_processor-only field raises error."""
        del valid_context["manifest_processor"]["replicas"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=r"manifest_processor\.replicas is required",
        ):
            ConfigLoader(app)

    def test_missing_job_validation_policy_allowed_namespaces(self, valid_context):
        """Missing job_validation_policy.allowed_namespaces must fail loudly —
        both processors depend on the shared allowlist, so the error must
        point at the shared section, not a service-specific one."""
        del valid_context["job_validation_policy"]["allowed_namespaces"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=r"job_validation_policy\.allowed_namespaces is required",
        ):
            ConfigLoader(app)

    def test_missing_job_validation_policy_field(self, valid_context):
        """Missing job_validation_policy.resource_quotas must fail loudly —
        both processors depend on it, so the error must point at the
        shared section, not a service-specific one."""
        del valid_context["job_validation_policy"]["resource_quotas"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=r"job_validation_policy\.resource_quotas is required",
        ):
            ConfigLoader(app)

    def test_missing_job_validation_policy_section(self, valid_context):
        """Omitting the whole job_validation_policy section must fail."""
        del valid_context["job_validation_policy"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="job_validation_policy configuration is required"
        ):
            ConfigLoader(app)

    @pytest.mark.parametrize("value", [[], "invalid", 1, False])
    def test_job_validation_policy_must_be_an_object(self, valid_context, value):
        """The shared policy container cannot use a truthy non-object value."""
        valid_context["job_validation_policy"] = value
        with pytest.raises(ConfigValidationError, match="job_validation_policy must be an object"):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("value", [None, "false", 0, []])
    def test_manifest_security_policy_must_be_an_object(self, valid_context, value):
        """Null and scalar policy values fail synthesis instead of disabling checks."""
        valid_context["job_validation_policy"]["manifest_security_policy"] = value
        with pytest.raises(
            ConfigValidationError,
            match=r"job_validation_policy\.manifest_security_policy must be an object",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("value", [None, "true", 1, 0, [], {}])
    def test_manifest_security_policy_values_must_be_booleans(self, valid_context, value):
        """Policy members must be JSON booleans, not merely truthy or falsey."""
        valid_context["job_validation_policy"]["manifest_security_policy"] = {
            "block_host_path": value
        }
        with pytest.raises(
            ConfigValidationError,
            match=r"manifest_security_policy\.block_host_path must be a boolean",
        ):
            ConfigLoader(MockApp(valid_context))

    def test_manifest_security_policy_rejects_unknown_fields(self, valid_context):
        """Misspelled controls cannot be silently ignored during synthesis."""
        valid_context["job_validation_policy"]["manifest_security_policy"] = {
            "block_host_paths": True
        }
        with pytest.raises(ConfigValidationError, match=r"unsupported fields.*block_host_paths"):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("value", [None, "true", 1, 0])
    def test_require_accelerator_toleration_must_be_boolean(self, valid_context, value):
        """The adjacent default-on accelerator control is strict too."""
        valid_context["job_validation_policy"]["require_accelerator_toleration"] = value
        with pytest.raises(
            ConfigValidationError,
            match=r"job_validation_policy\.require_accelerator_toleration must be a boolean",
        ):
            ConfigLoader(MockApp(valid_context))


class TestAuroraPgvectorConfig:
    """Tests for Aurora pgvector configuration."""

    def test_get_aurora_pgvector_config_defaults(self):
        """Test getting Aurora pgvector config with defaults."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is False
        assert aurora_config["min_acu"] == 0
        assert aurora_config["max_acu"] == 16
        assert aurora_config["backup_retention_days"] == 7
        assert aurora_config["deletion_protection"] is False

    def test_get_aurora_pgvector_config_enabled(self, valid_context):
        """Test getting Aurora pgvector config when enabled."""
        valid_context["aurora_pgvector"] = {
            "enabled": True,
            "min_acu": 0,
            "max_acu": 32,
            "backup_retention_days": 14,
            "deletion_protection": True,
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is True
        assert aurora_config["min_acu"] == 0
        assert aurora_config["max_acu"] == 32
        assert aurora_config["backup_retention_days"] == 14
        assert aurora_config["deletion_protection"] is True

    def test_get_aurora_pgvector_config_partial_override(self, valid_context):
        """Test that partial overrides merge with defaults."""
        valid_context["aurora_pgvector"] = {
            "enabled": True,
            "max_acu": 64,
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is True
        assert aurora_config["max_acu"] == 64
        # Defaults should still be present
        assert aurora_config["min_acu"] == 0
        assert aurora_config["backup_retention_days"] == 7
        assert aurora_config["deletion_protection"] is False

    def test_get_aurora_pgvector_config_min_acu_zero_for_scale_to_zero(self):
        """Test that min_acu=0 is the default for scale-to-zero support."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["min_acu"] == 0, (
            "Default min_acu should be 0 to enable Aurora Serverless v2 "
            "scale-to-zero (auto-pause). Set to 0.5+ to keep always warm."
        )

    def test_get_aurora_pgvector_config_min_acu_half_disables_autopause(self, valid_context):
        """Test that min_acu=0.5 disables auto-pause."""
        valid_context["aurora_pgvector"] = {
            "enabled": True,
            "min_acu": 0.5,
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["min_acu"] == 0.5

    def test_get_aurora_pgvector_config_non_dict_context_is_rejected(self):
        """A scalar where the block belongs is an error, not a silent fall back to defaults."""
        app = MockApp({**minimal_context(), "aurora_pgvector": "invalid"})
        with pytest.raises(ConfigValidationError, match="aurora_pgvector must be an object"):
            ConfigLoader(app)

    def test_get_aurora_pgvector_config_disabled_by_default(self):
        """Test that Aurora pgvector is disabled by default."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is False


class TestValkeyConfigValidation:
    """The valkey block: strict flag, integer usage limits, known keys only."""

    def test_defaults(self, valid_context):
        assert ConfigLoader(MockApp(valid_context)).get_valkey_config() == {
            "enabled": False,
            "max_data_storage_gb": 5,
            "max_ecpu_per_second": 5000,
            "snapshot_retention_limit": 1,
        }

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            ({"enabled": "no"}, r"valkey\.enabled must be true or false"),
            (
                {"max_data_storage_gb": 0},
                r"valkey\.max_data_storage_gb must be an integer of at least 1",
            ),
            ({"max_data_storage_gb": 5.5}, r"valkey\.max_data_storage_gb must be an integer"),
            (
                {"max_ecpu_per_second": 999},
                r"valkey\.max_ecpu_per_second must be an integer of at least 1000",
            ),
            ({"max_ecpu_per_second": True}, r"valkey\.max_ecpu_per_second must be an integer"),
            (
                {"snapshot_retention_limit": -1},
                r"valkey\.snapshot_retention_limit must be an integer of at least 0",
            ),
            ({"max_storage_gb": 5}, r"valkey contains unknown key\(s\): valkey\.max_storage_gb"),
            (True, r"valkey must be an object, got bool"),
        ],
    )
    def test_invalid_values_fail_with_the_key_path(self, valid_context, block, message):
        valid_context["valkey"] = block
        with pytest.raises(ConfigValidationError, match=message):
            ConfigLoader(MockApp(valid_context))

    def test_comment_keys_are_allowed(self, valid_context):
        valid_context["valkey"] = {"_comment_limits": "caps", "enabled": True}
        assert ConfigLoader(MockApp(valid_context)).get_valkey_config()["enabled"] is True


class TestAuroraPgvectorConfigValidation:
    """The aurora_pgvector block: capacity units, retention and the two flags."""

    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            ({"min_acu": 0.5, "max_acu": 1}, (0.5, 1)),
            ({"min_acu": 0, "max_acu": 256}, (0, 256)),
            ({"min_acu": 8, "max_acu": 8}, (8, 8)),
        ],
    )
    def test_capacity_ranges_aurora_accepts(self, valid_context, block, expected):
        valid_context["aurora_pgvector"] = block
        aurora = ConfigLoader(MockApp(valid_context)).get_aurora_pgvector_config()
        assert (aurora["min_acu"], aurora["max_acu"]) == expected

    @pytest.mark.parametrize("value", [True, False])
    def test_flags_take_json_booleans(self, valid_context, value):
        valid_context["aurora_pgvector"] = {"enabled": value, "deletion_protection": value}
        aurora = ConfigLoader(MockApp(valid_context)).get_aurora_pgvector_config()
        assert aurora["enabled"] is value
        assert aurora["deletion_protection"] is value

    def test_string_deletion_protection_makes_no_claim_about_the_past(self, valid_context):
        """Earlier releases failed the synth on a string here instead of reading it."""
        valid_context["aurora_pgvector"] = {"deletion_protection": "false"}
        with pytest.raises(ConfigValidationError) as raised:
            ConfigLoader(MockApp(valid_context))
        assert str(raised.value) == (
            "aurora_pgvector.deletion_protection must be true or false (a JSON boolean), "
            "not the string 'false'."
        )

    def test_string_true_deletion_protection_reads_as_true_with_a_warning(self, valid_context):
        valid_context["aurora_pgvector"] = {"deletion_protection": "TRUE"}
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_aurora_pgvector_config()["deletion_protection"] is True
        assert any(w.startswith("aurora_pgvector.deletion_protection is") for w in config.warnings)

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            ({"min_acu": -0.5}, r"aurora_pgvector\.min_acu must be a number from 0 to 256"),
            (
                {"min_acu": 0.25},
                r"aurora_pgvector\.min_acu must be a number from 0 to 256 in steps of 0\.5",
            ),
            ({"max_acu": 0.5}, r"aurora_pgvector\.max_acu must be a number from 1 to 256"),
            ({"max_acu": 257}, r"aurora_pgvector\.max_acu must be a number from 1 to 256"),
            ({"max_acu": True}, r"aurora_pgvector\.max_acu must be a number"),
            ({"max_acu": "16"}, r"aurora_pgvector\.max_acu must be a number"),
            (
                {"min_acu": 4, "max_acu": 2},
                r"aurora_pgvector\.max_acu \(2\) must be at least aurora_pgvector\.min_acu \(4\)",
            ),
            (
                {"backup_retention_days": 0},
                r"aurora_pgvector\.backup_retention_days must be an integer between 1 and 35",
            ),
            (
                {"backup_retention_days": 36},
                r"aurora_pgvector\.backup_retention_days must be an integer between 1 and 35",
            ),
            (
                {"deletion_protection": "maybe"},
                r"aurora_pgvector\.deletion_protection must be true or false",
            ),
            (
                {"min_capacity": 1},
                r"aurora_pgvector contains unknown key\(s\): aurora_pgvector\.min_capacity",
            ),
        ],
    )
    def test_invalid_values_fail_with_the_key_path(self, valid_context, block, message):
        valid_context["aurora_pgvector"] = block
        with pytest.raises(ConfigValidationError, match=message):
            ConfigLoader(MockApp(valid_context))


class TestBlockShapes:
    """Every block is an object of known keys, checked before anything reads it."""

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (
                lambda c: c.__setitem__("cluster_observability", False),
                "cluster_observability must be an object, got bool: False",
            ),
            (
                lambda c: c.__setitem__("cost_monitoring", False),
                "cost_monitoring must be an object, got bool: False",
            ),
            (
                lambda c: c.__setitem__("mission_memory", False),
                "mission_memory must be an object, got bool: False",
            ),
            (
                lambda c: c.__setitem__("cost_monitoring", {"reports": 30}),
                "cost_monitoring.reports must be an object, got int: 30",
            ),
            (
                lambda c: c.__setitem__("cluster_observability", {"grafana": "x"}),
                "cluster_observability.grafana must be an object, got str: 'x'",
            ),
            (
                lambda c: c.__setitem__("helm", {"volcano": True}),
                "helm.volcano must be an object, got bool: True",
            ),
            (
                lambda c: c.__setitem__("resource_thresholds", 80),
                "resource_thresholds must be an object, got int: 80",
            ),
        ],
    )
    def test_a_scalar_where_a_block_belongs_is_rejected(self, valid_context, mutate, message):
        """These used to read as absent blocks (defaults on) or crash later."""
        mutate(valid_context)
        with pytest.raises(ConfigValidationError, match=re.escape(message)):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize(
        ("mutate", "unknown"),
        [
            (
                lambda c: c.__setitem__("queue_processor", {"enable": False}),
                "queue_processor.enable",
            ),
            (
                lambda c: c.__setitem__("helm", {"volcano": {"enabled": True, "version": "1"}}),
                "helm.volcano.version",
            ),
            (lambda c: c.__setitem__("helm", {"volcanoo": {"enabled": True}}), "helm.volcanoo"),
            (
                lambda c: c.__setitem__("cost_monitoring", {"reports": {"interval": 5}}),
                "cost_monitoring.reports.interval",
            ),
            (
                lambda c: c["api_gateway"].__setitem__("throttle_limit", 5),
                "api_gateway.throttle_limit",
            ),
        ],
    )
    def test_unknown_keys_fail_with_their_full_path(self, valid_context, mutate, unknown):
        mutate(valid_context)
        with pytest.raises(ConfigValidationError, match=re.escape(f"unknown key(s): {unknown};")):
            ConfigLoader(MockApp(valid_context))

    def test_comment_keys_and_null_blocks_are_allowed(self, valid_context):
        valid_context["queue_processor"] = {"_comment_enabled": "doc", "enabled": True}
        valid_context["cost_monitoring"] = None
        valid_context["helm"] = {"_comment": "charts", "kueue": {"enabled": False}}
        config = ConfigLoader(MockApp(valid_context))
        assert config.get_cost_monitoring_config()["enabled"] is True

    def test_tag_values_must_be_strings(self, valid_context):
        valid_context["tags"] = {"Environment": "test", "CostCenter": 42}
        with pytest.raises(
            ConfigValidationError, match=re.escape("tags.CostCenter must be a string, got 42")
        ):
            ConfigLoader(MockApp(valid_context))

    def test_tags_must_be_an_object(self, valid_context):
        valid_context["tags"] = ["Environment=test"]
        with pytest.raises(ConfigValidationError, match=re.escape("tags must be an object")):
            ConfigLoader(MockApp(valid_context))

    def test_legacy_global_accelerator_keys_are_deprecated(self, valid_context):
        valid_context["global_accelerator"].update(
            {"health_check_grace_period": 30, "health_check_timeout": 5}
        )
        config = ConfigLoader(MockApp(valid_context))
        assert [w.split(" is deprecated")[0] for w in config.warnings if "global_acc" in w] == [
            "global_accelerator.health_check_grace_period",
            "global_accelerator.health_check_timeout",
        ]

    def test_images_destroy_and_lifecycle_keys_are_deprecated(self, valid_context):
        """No stack creates or deletes the image repositories, so these never took effect."""
        valid_context["images"] = {
            "removal_policy": "destroy",
            "empty_on_delete": True,
            "lifecycle": {"keep_tagged": 20, "expire_untagged_days": 7},
            "replication": {"enabled": True},
        }
        config = ConfigLoader(MockApp(valid_context))
        deprecated = {w.split(" is deprecated")[0]: w for w in config.warnings if "images." in w}
        assert sorted(deprecated) == [
            "images.empty_on_delete",
            "images.lifecycle",
            "images.removal_policy",
        ]
        assert "gco images delete-repo <name> --force -y" in deprecated["images.removal_policy"]
        assert "gco images lifecycle set" in deprecated["images.lifecycle"]

    @pytest.mark.parametrize(
        "images",
        [
            {"removal_policy": "shred"},
            {"empty_on_delete": "false"},
            {"lifecycle": "keep 20"},
            {"lifecycle": {"keep_everything": True}},
        ],
    )
    def test_deprecated_images_values_are_not_validated(self, valid_context, images):
        """Ignored keys cannot fail the synth, whatever they hold."""
        valid_context["images"] = images
        config = ConfigLoader(MockApp(valid_context))
        assert any(" is deprecated and ignored" in w for w in config.warnings)

    def test_unknown_top_level_keys_in_gco_style_warn(self, valid_context):
        valid_context.update(
            {
                "cluster_observabilty": {"enabled": False},
                "@aws-cdk/core:newStyleStackSynthesis": True,
                "aws:cdk:enable-path-metadata": True,
                "availability-zones:account=123456789012:region=us-east-1": ["us-east-1a"],
                "acknowledged-issue-numbers": [1],
                "_comment_top": "doc",
                "feature_enabled_overrides": "valkey",
            }
        )
        config = ConfigLoader(MockApp(valid_context))
        unknown = [w for w in config.warnings if "is not a GCO cdk.json key" in w]
        assert unknown == [
            "cluster_observabilty is not a GCO cdk.json key and is ignored; check its "
            "spelling against docs/CUSTOMIZATION.md."
        ]

    def test_a_node_without_get_all_context_skips_the_top_level_check(self, valid_context):
        class _BareNode:
            def __init__(self, context: dict) -> None:
                self._context = context

            def try_get_context(self, key: str):
                return self._context.get(key)

        class _BareApp:
            def __init__(self, context: dict) -> None:
                self.node = _BareNode(context)

        valid_context["cluster_observabilty"] = {}
        config = ConfigLoader(_BareApp(valid_context))
        assert not [w for w in config.warnings if "is not a GCO cdk.json key" in w]

    def test_every_key_of_the_shipped_cdk_json_is_known(self):
        """The committed file draws no unknown-key warning and no deprecation."""
        import json
        from pathlib import Path

        context = json.loads(
            (Path(__file__).resolve().parents[1] / "cdk.json").read_text(encoding="utf-8")
        )["context"]
        assert ConfigLoader(MockApp(context)).warnings == []


def _stateful_flag_context(valid_context: dict, block: str, value: object) -> dict:
    """``valid_context`` with ``<block>.enabled`` set; ``block`` may be dotted once."""
    outer, _, inner = block.partition(".")
    valid_context[outer] = {inner: {"enabled": value}} if inner else {"enabled": value}
    return valid_context


#: The storage features whose "off" deletes data: (block, what turning it off
#: deletes, how to read the parsed flag back).
_STATEFUL_FLAGS = [
    pytest.param(
        "fsx_lustre",
        "the FSx for Lustre file system and the data on it",
        lambda config: config.get_fsx_lustre_config()["enabled"],
        id="fsx_lustre",
    ),
    pytest.param(
        "valkey",
        "the Valkey cache and the data in it",
        lambda config: config.get_valkey_config()["enabled"],
        id="valkey",
    ),
    pytest.param(
        "aurora_pgvector",
        "the Aurora pgvector cluster and its data",
        lambda config: config.get_aurora_pgvector_config()["enabled"],
        id="aurora_pgvector",
    ),
    pytest.param(
        "cluster_observability.mlflow",
        "the MLflow tracking server and its run-metadata volume",
        lambda config: config.get_cluster_observability_config()["mlflow"]["enabled"],
        id="mlflow",
    ),
]


class TestStatefulFlagStrings:
    """A string flag in a storage block is never quietly reinterpreted on upgrade.

    Earlier releases read these flags by truthiness, so ``"false"`` (any
    non-empty string) has been running as enabled. Reading it as false now
    would delete the resource and its data on the next deploy, so every
    string except ``"true"`` fails validation and says what to write.
    """

    @pytest.mark.parametrize(("block", "deletes", "read_flag"), _STATEFUL_FLAGS)
    @pytest.mark.parametrize("value", ["false", "False", "0", "off"])
    def test_non_empty_strings_fail_and_explain(
        self, valid_context, block, deletes, read_flag, value
    ):
        context = _stateful_flag_context(valid_context, block, value)
        with pytest.raises(ConfigValidationError) as raised:
            ConfigLoader(MockApp(context))
        assert str(raised.value) == (
            f"{block}.enabled must be true or false (a JSON boolean), not the string "
            f"{value!r}. Earlier releases read every non-empty string here as true, so this "
            "setting has been on. Write true to keep it on, or false to turn it off, which "
            f"deletes {deletes}."
        )

    @pytest.mark.parametrize(("block", "deletes", "read_flag"), _STATEFUL_FLAGS)
    def test_empty_string_fails_and_says_it_has_been_off(
        self, valid_context, block, deletes, read_flag
    ):
        context = _stateful_flag_context(valid_context, block, "")
        with pytest.raises(ConfigValidationError) as raised:
            ConfigLoader(MockApp(context))
        assert str(raised.value) == (
            f"{block}.enabled must be true or false (a JSON boolean), not the string ''. "
            "Earlier releases read an empty string here as false, so this setting has been "
            "off. Write false to keep it off, or true to turn it on."
        )

    @pytest.mark.parametrize(("block", "deletes", "read_flag"), _STATEFUL_FLAGS)
    @pytest.mark.parametrize("value", ["true", "TRUE", "True"])
    def test_string_true_keeps_the_feature_on_with_a_warning(
        self, valid_context, block, deletes, read_flag, value
    ):
        config = ConfigLoader(MockApp(_stateful_flag_context(valid_context, block, value)))
        assert read_flag(config) is True
        assert (
            f"{block}.enabled is the string {value!r}, read as true; write the JSON boolean "
            "true instead (string flags in cdk.json blocks are deprecated)."
        ) in config.warnings

    @pytest.mark.parametrize(("block", "deletes", "read_flag"), _STATEFUL_FLAGS)
    @pytest.mark.parametrize("value", [True, False])
    def test_json_booleans_are_read_as_written_without_a_warning(
        self, valid_context, block, deletes, read_flag, value
    ):
        config = ConfigLoader(MockApp(_stateful_flag_context(valid_context, block, value)))
        assert read_flag(config) is value
        assert not [w for w in config.warnings if f"{block}.enabled" in w]


class TestParseFlags:
    """The two flag parsers, called directly."""

    def test_block_flag_without_a_data_consequence(self):
        from gco.config.config_loader import parse_block_flag

        with pytest.raises(ConfigValidationError) as raised:
            parse_block_flag("no", "queue_processor.enabled", lambda _message: None)
        assert str(raised.value).endswith("Write true to keep it on, or false to turn it off.")

    def test_block_flag_rejects_non_strings_by_name(self):
        from gco.config.config_loader import parse_block_flag

        with pytest.raises(
            ConfigValidationError,
            match=r"^x\.enabled must be true or false \(a JSON boolean\), got 1$",
        ):
            parse_block_flag(1, "x.enabled", lambda _message: None)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(True, True), (False, False), ("true", True), ("FALSE", False), ("False", False)],
    )
    def test_top_level_flag_takes_both_strings(self, value, expected):
        """``--context key=value`` passes strings, so both spellings are read as written."""
        from gco.config.config_loader import parse_bool_flag

        assert parse_bool_flag(value, "gco:control-plane-only") is expected

    @pytest.mark.parametrize("value", ["yes", "", 1, None])
    def test_top_level_flag_rejects_everything_else(self, value):
        from gco.config.config_loader import parse_bool_flag

        with pytest.raises(ConfigValidationError, match=r"^k must be true or false, got "):
            parse_bool_flag(value, "k")


class TestAnalyticsEnvironmentConfig:
    """Tests for optional analytics_environment configuration."""

    def test_get_analytics_config_defaults(self):
        """Missing analytics_environment block → defaults apply."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["enabled"] is False
        assert analytics_config["hyperpod"] == {"enabled": False}
        assert analytics_config["cognito"] == {
            "domain_prefix": None,
            "removal_policy": "destroy",
        }
        assert analytics_config["efs"] == {"removal_policy": "destroy"}
        assert "studio" not in analytics_config

    def test_get_analytics_enabled_defaults_to_false(self):
        """Missing analytics_environment block → get_analytics_enabled() is False."""
        app = MockApp(minimal_context())
        config = ConfigLoader(app)

        assert config.get_analytics_enabled() is False

    def test_get_analytics_config_partial_cognito_override_merges_defaults(self, valid_context):
        """Setting cognito.domain_prefix preserves the default cognito.removal_policy."""
        valid_context["analytics_environment"] = {
            "cognito": {"domain_prefix": "my-studio"},
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["cognito"]["domain_prefix"] == "my-studio"
        # Default removal_policy must still be present after the partial override.
        assert analytics_config["cognito"]["removal_policy"] == "destroy"
        # Sibling sub-blocks must not have been dropped either.
        assert analytics_config["efs"] == {"removal_policy": "destroy"}
        assert analytics_config["hyperpod"] == {"enabled": False}
        assert analytics_config["enabled"] is False

    def test_get_analytics_config_partial_efs_override_merges_defaults(self, valid_context):
        """Setting efs.removal_policy=retain preserves cognito defaults."""
        valid_context["analytics_environment"] = {
            "enabled": True,
            "efs": {"removal_policy": "retain"},
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["enabled"] is True
        assert analytics_config["efs"]["removal_policy"] == "retain"
        # Cognito sub-block defaults untouched.
        assert analytics_config["cognito"] == {
            "domain_prefix": None,
            "removal_policy": "destroy",
        }

    def test_get_analytics_config_full_override_passthrough(self, valid_context):
        """Full override → every nested field is taken from the user block."""
        valid_context["analytics_environment"] = {
            "enabled": True,
            "hyperpod": {"enabled": True},
            "cognito": {"domain_prefix": "team-studio", "removal_policy": "retain"},
            "efs": {"removal_policy": "retain"},
            "studio": {"user_profile_name_prefix": "dev-"},
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["enabled"] is True
        assert analytics_config["hyperpod"] == {"enabled": True}
        assert analytics_config["cognito"] == {
            "domain_prefix": "team-studio",
            "removal_policy": "retain",
        }
        assert analytics_config["efs"] == {"removal_policy": "retain"}
        # Never applied: profiles are named after the Cognito username.
        assert (
            "analytics_environment.studio.user_profile_name_prefix is deprecated and ignored: "
            "it was never applied: the presigned-URL Lambda names each SageMaker Studio user "
            "profile after the Cognito username. Remove it (and the then empty studio block) "
            "from cdk.json."
        ) in config.warnings

    def test_get_analytics_enabled_true_when_enabled_true(self, valid_context):
        """get_analytics_enabled() returns True when enabled=true."""
        valid_context["analytics_environment"] = {"enabled": True}
        app = MockApp(valid_context)
        config = ConfigLoader(app)

        assert config.get_analytics_enabled() is True

    def test_get_analytics_enabled_false_when_enabled_false(self, valid_context):
        """get_analytics_enabled() returns False when enabled=false."""
        valid_context["analytics_environment"] = {"enabled": False}
        app = MockApp(valid_context)
        config = ConfigLoader(app)

        assert config.get_analytics_enabled() is False

    def test_invalid_enabled_type_raises(self, valid_context):
        """Non-bool analytics_environment.enabled → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {"enabled": "yes"}
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match=re.escape("analytics_environment.enabled must be a bool")
        ):
            ConfigLoader(app)

    def test_invalid_hyperpod_enabled_type_raises(self, valid_context):
        """Non-bool analytics_environment.hyperpod.enabled → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {"hyperpod": {"enabled": 1}}
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("analytics_environment.hyperpod.enabled must be a bool"),
        ):
            ConfigLoader(app)

    def test_invalid_canvas_enabled_type_raises(self, valid_context):
        """Non-bool analytics_environment.canvas.enabled → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {"canvas": {"enabled": "yes"}}
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("analytics_environment.canvas.enabled must be a bool"),
        ):
            ConfigLoader(app)

    def test_canvas_defaults_to_disabled_when_block_omitted(self, valid_context):
        """Omitting the canvas sub-block leaves canvas.enabled=false after merge."""
        valid_context["analytics_environment"] = {"enabled": True}
        app = MockApp(valid_context)
        cfg = ConfigLoader(app).get_analytics_config()
        assert cfg["canvas"] == {"enabled": False}, (
            "Missing canvas sub-block should merge to the default "
            f"{{'enabled': False}}, got {cfg['canvas']!r}"
        )

    def test_canvas_enabled_preserved_on_partial_override(self, valid_context):
        """Setting canvas.enabled=true does not drop sibling sub-blocks."""
        valid_context["analytics_environment"] = {
            "enabled": True,
            "canvas": {"enabled": True},
        }
        app = MockApp(valid_context)
        cfg = ConfigLoader(app).get_analytics_config()
        assert cfg["canvas"] == {"enabled": True}
        # Sibling defaults must survive — the deep-merge logic is easy
        # to regress when a new sub-block is added.
        assert cfg["hyperpod"] == {"enabled": False}
        assert cfg["efs"] == {"removal_policy": "destroy"}

    def test_invalid_cognito_removal_policy_raises(self, valid_context):
        """cognito.removal_policy not in {destroy, retain} → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {
            "cognito": {"removal_policy": "delete"},
        }
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("analytics_environment.cognito.removal_policy must be one of"),
        ):
            ConfigLoader(app)

    def test_invalid_efs_removal_policy_raises(self, valid_context):
        """efs.removal_policy not in {destroy, retain} → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {
            "efs": {"removal_policy": "keep"},
        }
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("analytics_environment.efs.removal_policy must be one of"),
        ):
            ConfigLoader(app)

    def test_non_dict_analytics_environment_context_is_rejected(self):
        """A scalar where the block belongs fails instead of reading as the defaults."""
        app = MockApp({**minimal_context(), "analytics_environment": "invalid"})
        with pytest.raises(
            ConfigValidationError,
            match=re.escape("analytics_environment must be an object, got str: 'invalid'"),
        ):
            ConfigLoader(app)


class TestAnalyticsConstants:
    """Tests for analytics constants exported from gco.stacks.constants."""

    def test_emr_serverless_release_label_prefix(self):
        """EMR_SERVERLESS_RELEASE_LABEL exists and starts with 'emr-'."""
        from gco.stacks.constants import EMR_SERVERLESS_RELEASE_LABEL

        assert isinstance(EMR_SERVERLESS_RELEASE_LABEL, str)
        assert EMR_SERVERLESS_RELEASE_LABEL.startswith("emr-"), (
            f"EMR_SERVERLESS_RELEASE_LABEL must start with 'emr-' so it is a "
            f"valid emrserverless.CfnApplication release_label, got "
            f"{EMR_SERVERLESS_RELEASE_LABEL!r}"
        )

    def test_sagemaker_role_name_prefix(self):
        """SAGEMAKER_ROLE_NAME_PREFIX is the literal 'AmazonSageMaker'."""
        from gco.stacks.constants import SAGEMAKER_ROLE_NAME_PREFIX

        assert SAGEMAKER_ROLE_NAME_PREFIX == "AmazonSageMaker"

    def test_cognito_domain_prefix_default(self):
        """cognito_domain_prefix_default renders '<project>-studio' (#139).

        Defaults to the pre-#139 literal ``gco-studio`` for project_name='gco'
        and scopes to the project name otherwise.
        """
        from gco.stacks.constants import cognito_domain_prefix_default

        assert cognito_domain_prefix_default("gco") == "gco-studio"
        assert cognito_domain_prefix_default("acme") == "acme-studio"

    def test_studio_presigned_url_expiry_seconds_positive_int(self):
        """STUDIO_PRESIGNED_URL_EXPIRY_SECONDS is a positive int."""
        from gco.stacks.constants import STUDIO_PRESIGNED_URL_EXPIRY_SECONDS

        assert isinstance(STUDIO_PRESIGNED_URL_EXPIRY_SECONDS, int)
        # Booleans are ints in Python — guard against someone typing `True`.
        assert not isinstance(STUDIO_PRESIGNED_URL_EXPIRY_SECONDS, bool)
        assert STUDIO_PRESIGNED_URL_EXPIRY_SECONDS > 0

    def test_cost_report_ssm_parameter_prefix(self):
        """cost_report_ssm_parameter_prefix renders '/<project>/cost-report-bucket'."""
        from gco.stacks.constants import cost_report_ssm_parameter_prefix

        assert cost_report_ssm_parameter_prefix("gco") == "/gco/cost-report-bucket"
        assert cost_report_ssm_parameter_prefix("acme") == "/acme/cost-report-bucket"

    def test_no_bucket_name_helper_survives(self):
        """Bucket names are CloudFormation-generated; nothing may reconstruct one."""
        import gco.stacks.constants as constants

        assert not [name for name in dir(constants) if name.endswith("_bucket_name")]
        assert not [name for name in dir(constants) if name.endswith("_bucket_name_prefix")]

    def test_cluster_shared_ssm_parameter_prefix(self):
        """cluster_shared_ssm_parameter_prefix renders '/<project>/cluster-shared-bucket' (#139)."""
        from gco.stacks.constants import cluster_shared_ssm_parameter_prefix

        assert cluster_shared_ssm_parameter_prefix("gco") == "/gco/cluster-shared-bucket"
        assert cluster_shared_ssm_parameter_prefix("acme") == "/acme/cluster-shared-bucket"
        assert cluster_shared_ssm_parameter_prefix("gco").startswith("/"), (
            "cluster_shared_ssm_parameter_prefix must be an absolute SSM path "
            "(starts with '/') so it composes into valid parameter ARNs"
        )


class TestTracingConfig:
    """The ``tracing`` block and its run-scoped ``tracing_overrides`` context.

    Tracing is on by default; the flags render verbatim into the services'
    ``GCO_TRACING_ENABLED`` env var and ``sample_ratio`` is a head-sampling
    probability, so every value is type-checked exactly at synth.
    """

    def test_absent_block_means_the_shipped_defaults(self, valid_context):
        loader = ConfigLoader(MockApp(valid_context))

        assert loader.get_tracing_config() == {
            "enabled": True,
            "sample_ratio": 0.05,
            "enable_transaction_search": True,
        }
        assert loader.get_tracing_enabled() is True
        assert loader.get_transaction_search_enabled() is True

    def test_shipped_cdk_json_block_matches_the_defaults(self, valid_context):
        import json

        from gco.config.config_loader import TRACING_DEFAULTS

        shipped = json.loads((Path(__file__).resolve().parents[1] / "cdk.json").read_text())
        valid_context["tracing"] = shipped["context"]["tracing"]

        assert ConfigLoader(MockApp(valid_context)).get_tracing_config() == TRACING_DEFAULTS

    def test_partial_block_keeps_the_other_defaults(self, valid_context):
        valid_context["tracing"] = {"sample_ratio": 0.2}

        assert ConfigLoader(MockApp(valid_context)).get_tracing_config() == {
            "enabled": True,
            "sample_ratio": 0.2,
            "enable_transaction_search": True,
        }

    @pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (1, 1.0), (0.5, 0.5)])
    def test_sample_ratio_boundaries_are_inclusive_and_normalized_to_float(
        self, valid_context, value, expected
    ):
        valid_context["tracing"] = {"sample_ratio": value}

        ratio = ConfigLoader(MockApp(valid_context)).get_tracing_config()["sample_ratio"]

        assert ratio == expected
        assert type(ratio) is float

    @pytest.mark.parametrize(
        "value", [True, False, "0.1", None, -0.01, 1.01, float("nan"), float("inf"), [0.1]]
    )
    def test_sample_ratio_must_be_a_probability(self, valid_context, value):
        valid_context["tracing"] = {"sample_ratio": value}

        with pytest.raises(
            ConfigValidationError,
            match=r"tracing\.sample_ratio must be a number from 0 to 1 \(inclusive\)",
        ):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("flag", ["enabled", "enable_transaction_search"])
    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
    def test_flags_must_be_literal_booleans(self, valid_context, flag, value):
        valid_context["tracing"] = {flag: value}

        with pytest.raises(ConfigValidationError, match=rf"tracing\.{flag} must be a boolean"):
            ConfigLoader(MockApp(valid_context))

    @pytest.mark.parametrize("value", [[], "enabled", True, 5])
    def test_block_must_be_an_object(self, valid_context, value):
        valid_context["tracing"] = value

        with pytest.raises(ConfigValidationError, match="tracing must be an object"):
            ConfigLoader(MockApp(valid_context))

    def test_unknown_key_lists_the_allowed_ones(self, valid_context):
        valid_context["tracing"] = {"sampling_ratio": 0.1}

        with pytest.raises(ConfigValidationError) as exc_info:
            ConfigLoader(MockApp(valid_context))

        message = str(exc_info.value)
        assert "tracing contains unknown key(s): tracing.sampling_ratio" in message
        assert (
            "allowed keys: tracing.enable_transaction_search, tracing.enabled, tracing.sample_ratio"
            in message
        )

    @pytest.mark.parametrize(
        ("enabled", "transaction_search", "expected"),
        [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
    )
    def test_transaction_search_needs_tracing(
        self, valid_context, enabled, transaction_search, expected
    ):
        valid_context["tracing"] = {
            "enabled": enabled,
            "enable_transaction_search": transaction_search,
        }
        loader = ConfigLoader(MockApp(valid_context))

        assert loader.get_tracing_enabled() is enabled
        assert loader.get_transaction_search_enabled() is expected

    @pytest.mark.parametrize(
        "overrides",
        ['{"sample_ratio": 1.0}', {"sample_ratio": 1.0}, ' {"sample_ratio": 1} '],
    )
    def test_overrides_deep_merge_over_the_block(self, valid_context, overrides):
        valid_context["tracing"] = {"sample_ratio": 0.05, "enable_transaction_search": False}
        valid_context["tracing_overrides"] = overrides

        assert ConfigLoader(MockApp(valid_context)).get_tracing_config() == {
            "enabled": True,
            "sample_ratio": 1.0,
            "enable_transaction_search": False,
        }

    @pytest.mark.parametrize("overrides", [None, "", "   ", {}])
    def test_empty_overrides_change_nothing(self, valid_context, overrides):
        valid_context["tracing_overrides"] = overrides

        assert ConfigLoader(MockApp(valid_context)).get_tracing_config()["sample_ratio"] == 0.05

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ("{sample_ratio: 1}", "tracing_overrides must be a JSON object: "),
            ("[1]", "tracing_overrides must be a JSON object, got list"),
            ('"on"', "tracing_overrides must be a JSON object, got str"),
            (7, "tracing_overrides must be a JSON object string or an object, got int"),
            (["sample_ratio"], "tracing_overrides must be a JSON object string or an object"),
        ],
    )
    def test_malformed_overrides_fail_at_synth(self, valid_context, overrides, message):
        valid_context["tracing_overrides"] = overrides

        with pytest.raises(ConfigValidationError) as exc_info:
            ConfigLoader(MockApp(valid_context))

        assert message in str(exc_info.value)

    def test_overrides_are_validated_as_part_of_the_merged_block(self, valid_context):
        valid_context["tracing_overrides"] = '{"sample_ratio": 2}'
        with pytest.raises(ConfigValidationError, match=r"tracing\.sample_ratio"):
            ConfigLoader(MockApp(valid_context))

        valid_context["tracing_overrides"] = '{"sampling": 1}'
        with pytest.raises(ConfigValidationError) as exc_info:
            ConfigLoader(MockApp(valid_context))
        assert "tracing (after tracing_overrides) contains unknown key(s): tracing.sampling" in str(
            exc_info.value
        )

    def test_resolver_never_mutates_its_inputs(self):
        from gco.config.config_loader import TRACING_DEFAULTS, resolve_tracing_config

        block = {"enabled": False}
        overrides = {"sample_ratio": 0.5}

        resolved = resolve_tracing_config(block, overrides)
        resolved["enabled"] = True

        assert block == {"enabled": False}
        assert overrides == {"sample_ratio": 0.5}
        assert TRACING_DEFAULTS["enabled"] is True
        assert resolve_tracing_config(None) == TRACING_DEFAULTS

    def test_deep_merge_recurses_into_nested_mappings(self):
        from gco.config.config_loader import _deep_merge

        base = {"outer": {"kept": 1, "replaced": 2}, "flat": 1}
        merged = _deep_merge(base, {"outer": {"replaced": 3}, "flat": {"now": "a map"}})

        assert merged == {"outer": {"kept": 1, "replaced": 3}, "flat": {"now": "a map"}}
        assert base == {"outer": {"kept": 1, "replaced": 2}, "flat": 1}

    def test_parse_overrides_copies_mapping_input(self):
        from gco.config.config_loader import parse_tracing_overrides

        raw = {"sample_ratio": 1.0}
        parsed = parse_tracing_overrides(raw)
        parsed["sample_ratio"] = 0.0

        assert raw == {"sample_ratio": 1.0}

    def test_xray_is_a_supported_interface_endpoint(self, valid_context):
        valid_context["vpc_endpoints"] = {"interface": ["xray"]}

        assert ConfigLoader(MockApp(valid_context)).get_vpc_endpoints_config()["interface"] == [
            "xray"
        ]
