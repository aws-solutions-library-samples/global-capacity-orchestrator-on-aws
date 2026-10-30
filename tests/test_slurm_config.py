"""Tests for gco/slurm_config.py — the Slurm cluster block (``helm.slurm``).

The module is the single source of the block's shape: the config loader, the
regional stack, the kubectl-applier manifests, the ``gco slurm`` CLI and the
live validation harness all call it. These tests pin the defaults, every
validation branch, the derived views, the chart values the regional stack
hands the helm installer (NodeSets, partitions, accounting, storage mounts,
the KEDA ScaledObjects rendered through ``extraObjects``) and the
kubectl-applier replacements for the Slurm manifests, plus the loader wiring.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import aws_cdk as cdk
import pytest
import yaml

from gco import slurm_config as sc
from gco.config.config_loader import ConfigLoader, ConfigValidationError

QUOTA: dict[str, str] = {
    "max_cpu": "400",
    "max_memory": "4096Gi",
    "max_gpu": "32",
    "max_pods": "50",
    "container_max_cpu": "192",
    "container_max_memory": "2048Gi",
    "container_max_gpu": "8",
}


def _validate(raw: object = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("resource_quota", QUOTA)
    return sc.validate_slurm_config(raw, **kwargs)


def _on(**overrides: Any) -> dict[str, Any]:
    return {"enabled": True, **overrides}


def _partition(**overrides: Any) -> dict[str, Any]:
    return {"node_pool": "cpu", "cpu": "4", "memory": "16Gi", **overrides}


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_absent_block_is_off_with_every_default(self) -> None:
        config = _validate(None)
        assert config["enabled"] is False
        assert config["cluster_name"] == "gco-slurm"
        assert config["controller"] == {"state_storage_size": "20Gi"}
        assert config["restapi"] == {"replicas": 2}
        assert config["accounting"]["enabled"] is True
        assert config["accounting"]["backup"] == {
            "enabled": True,
            "schedule": "0 3 * * *",
            "retention_days": 30,
        }
        assert config["accounting"]["external"]["host"] == ""
        assert config["metrics"] == {"enabled": True}
        assert config["login"] == {"enabled": True, "replicas": 1}
        assert config["storage"] == {"home": "efs", "scratch": "auto"}
        assert config["slurm_conf"] == {}
        assert config["slurmdbd_conf"] == {}
        assert set(config["partitions"]) == {"cpu", "gpu", "gpu-efa"}

    def test_shipped_partitions_are_a_cpu_and_a_single_gpu_partition_scaling_from_zero(
        self,
    ) -> None:
        config = _validate(None)
        cpu, gpu, efa = (config["partitions"][k] for k in ("cpu", "gpu", "gpu-efa"))
        assert cpu["enabled"] and cpu["default"] and cpu["node_pool"] == "cpu"
        assert cpu["gpus"] == 0 and (cpu["min_nodes"], cpu["max_nodes"]) == (0, 8)
        assert gpu["enabled"] and not gpu["default"] and gpu["node_pool"] == "gpu"
        assert gpu["gpus"] == 1 and gpu["exclusive"] is True
        assert efa["enabled"] is False and efa["node_pool"] == "gpu-efa"
        assert efa["gpus"] == 8 and efa["efa_interfaces"] == 32
        assert efa["capacity_type"] == "on-demand"
        assert list(sc.enabled_partitions(config)) == ["cpu", "gpu"]
        assert sc.autoscaled_partitions(config) == ["cpu", "gpu"]

    def test_defaults_are_copied_not_shared(self) -> None:
        config = _validate(None)
        config["partitions"]["cpu"]["max_nodes"] = 99
        config["slurm_conf"]["X"] = ["y"]
        assert sc.SLURM_DEFAULTS["partitions"]["cpu"]["max_nodes"] == 8
        assert sc.SLURM_DEFAULTS["slurm_conf"] == {}
        assert "_cpu_cores" not in sc.SLURM_DEFAULTS["partitions"]["cpu"]

    def test_merge_does_not_alias_the_caller_block(self) -> None:
        raw = _on(partitions={"cpu": {"features": ["fast"]}})
        config = _validate(raw)
        config["partitions"]["cpu"]["features"].append("x")
        assert raw["partitions"]["cpu"]["features"] == ["fast"]

    @pytest.mark.parametrize(
        "raw",
        [
            _on(controller=None),
            _on(accounting=None),
            _on(accounting={"backup": None, "external": None}),
            _on(partitions=None),
        ],
    )
    def test_json_null_nested_blocks_keep_their_defaults(self, raw: dict[str, Any]) -> None:
        config = _validate(raw)
        assert config["controller"] == sc.SLURM_DEFAULTS["controller"]
        assert config["accounting"]["backup"] == sc.SLURM_DEFAULTS["accounting"]["backup"]
        assert set(config["partitions"]) == {"cpu", "gpu", "gpu-efa"}

    def test_partition_entries_merge_over_the_shipped_definition(self) -> None:
        config = _validate(_on(partitions={"gpu": {"max_nodes": 16}}))
        gpu = config["partitions"]["gpu"]
        assert gpu["max_nodes"] == 16 and gpu["node_pool"] == "gpu" and gpu["gpus"] == 1
        assert "cpu" in config["partitions"]

    def test_new_partition_starts_from_the_partition_defaults(self) -> None:
        config = _validate(_on(partitions={"bigmem": {"memory": "256Gi"}}))
        bigmem = config["partitions"]["bigmem"]
        assert bigmem["node_pool"] == "cpu" and bigmem["cpu"] == "4"
        assert bigmem["memory"] == "256Gi" and bigmem["default"] is False
        assert bigmem["_memory_bytes"] == 256 * 1024**3 and bigmem["_cpu_cores"] == 4.0

    def test_fixed_names_the_manifests_api_and_cli_share(self) -> None:
        assert sc.SLURM_HELM_KEY == "slurm"
        assert sc.SLURM_OPERATOR_CHART_NAME == "slinky-slurm-operator"
        assert sc.SLURM_CLUSTER_CHART_NAME == "slinky-slurm"
        assert sc.SLURM_NAMESPACE == "gco-jobs"
        assert sc.SLURM_CONTROLLER_NAME == "slinky-slurm-controller"
        assert sc.SLURM_RESTAPI_NAME == "slinky-slurm-restapi"
        assert sc.SLURM_LOGIN_NAME == "slinky-slurm-login-gco"
        assert sc.SLURM_JWT_SECRET == "slinky-slurm-auth-jwt"
        assert sc.SLURM_ACCOUNTING_DB_NAME == "slinky-slurm-mariadb"
        assert sc.SLURM_STORAGE_CLASS == "gco-slurm-gp3"
        assert (sc.SLURM_CONTROLLER_PORT, sc.SLURM_RESTAPI_PORT) == (6817, 6820)
        assert sc.nodeset_name("gpu") == "slinky-slurm-worker-gpu"
        assert sc.scaled_object_name("gpu") == "slinky-slurm-autoscale-gpu"
        assert "neuron" not in sc.NODE_POOLS


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestBlockShape:
    def test_errors_are_value_errors(self) -> None:
        assert issubclass(sc.SlurmConfigError, ValueError)

    @pytest.mark.parametrize("raw", [[], "on", 1, True])
    def test_block_must_be_an_object(self, raw: object) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"helm\.slurm must be an object"):
            _validate(raw)

    def test_unknown_top_level_key_is_named(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"helm\.slurm contains unknown key\(s\): nodes"
        ):
            _validate(_on(nodes=3))

    @pytest.mark.parametrize(
        "key", ["controller", "restapi", "accounting", "metrics", "login", "autoscaling", "storage"]
    )
    def test_sub_blocks_must_be_objects_with_known_keys(self, key: str) -> None:
        with pytest.raises(sc.SlurmConfigError, match=rf"helm\.slurm\.{key} must be an object"):
            _validate(_on(**{key: "yes"}))
        with pytest.raises(sc.SlurmConfigError, match=rf"helm\.slurm\.{key} contains unknown key"):
            _validate(_on(**{key: {"bogus": 1}}))

    @pytest.mark.parametrize("nested", ["backup", "external"])
    def test_accounting_nested_blocks_are_checked(self, nested: str) -> None:
        with pytest.raises(sc.SlurmConfigError, match=rf"accounting\.{nested} must be an object"):
            _validate(_on(accounting={nested: 1}))
        with pytest.raises(sc.SlurmConfigError, match=rf"accounting\.{nested} contains unknown"):
            _validate(_on(accounting={nested: {"nope": 1}}))

    def test_enabled_must_be_boolean(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"helm\.slurm\.enabled must be a boolean"):
            _validate({"enabled": "true"})

    @pytest.mark.parametrize("name", ["", "   ", 7, "bad name", "a" * 64])
    def test_cluster_name_must_be_a_slurm_cluster_name(self, name: object) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"helm\.slurm\.cluster_name"):
            _validate(_on(cluster_name=name))

    def test_cluster_name_is_stripped(self) -> None:
        assert _validate(_on(cluster_name="  hpc_01 "))["cluster_name"] == "hpc_01"

    @pytest.mark.parametrize("size", ["", "lots", 0, "-1Gi", True, None, [1]])
    def test_controller_state_storage_size_must_be_a_positive_quantity(self, size: object) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"controller\.state_storage_size"):
            _validate(_on(controller={"state_storage_size": size}))

    @pytest.mark.parametrize("replicas", [0, 11, "2", 2.0, True])
    def test_restapi_replicas_bounds(self, replicas: object) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"restapi\.replicas must be an integer"):
            _validate(_on(restapi={"replicas": replicas}))

    def test_accounting_fields(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"accounting\.enabled must be a boolean"):
            _validate(_on(accounting={"enabled": 1}))
        with pytest.raises(sc.SlurmConfigError, match=r"accounting\.storage_size"):
            _validate(_on(accounting={"storage_size": "big"}))
        with pytest.raises(sc.SlurmConfigError, match=r"backup\.enabled must be a boolean"):
            _validate(_on(accounting={"backup": {"enabled": "yes"}}))
        with pytest.raises(sc.SlurmConfigError, match=r"backup\.retention_days"):
            _validate(_on(accounting={"backup": {"retention_days": 0}}))

    @pytest.mark.parametrize("schedule", ["daily", "0 3 * *", "0 3 * * * *", 3, "a b c d e"])
    def test_backup_schedule_must_be_five_field_cron(self, schedule: object) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"backup\.schedule must be a five-field cron"
        ):
            _validate(_on(accounting={"backup": {"schedule": schedule}}))

    @pytest.mark.parametrize(
        "schedule", ["0 3 * * *", "*/15 * * * *", "30 2 1-5 * 1,3", "0 0 1 * 0"]
    )
    def test_backup_schedule_accepts_common_cron_shapes(self, schedule: str) -> None:
        config = _validate(_on(accounting={"backup": {"schedule": schedule}}))
        assert config["accounting"]["backup"]["schedule"] == schedule

    @pytest.mark.parametrize(
        "key", ["host", "database", "username", "password_secret", "password_key"]
    )
    def test_external_database_fields_must_be_strings(self, key: str) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=rf"accounting\.external\.{key} must be a string"
        ):
            _validate(_on(accounting={"external": {key: 5}}))

    def test_external_database_port_bounds(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"accounting\.external\.port"):
            _validate(_on(accounting={"external": {"port": 70000}}))

    @pytest.mark.parametrize("key", ["database", "username", "password_secret", "password_key"])
    def test_external_host_requires_the_connection_fields(self, key: str) -> None:
        external = {"host": "db.example.internal", "password_secret": "slurm-db", key: " "}
        with pytest.raises(
            sc.SlurmConfigError, match=rf"external\.{key} is required when external\.host"
        ):
            _validate(_on(accounting={"external": external}))

    def test_external_password_without_host_is_an_error(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"password_secret is set but external\.host is empty"
        ):
            _validate(_on(accounting={"external": {"password_secret": "slurm-db"}}))

    def test_external_database_is_accepted_and_stripped(self) -> None:
        config = _validate(
            _on(accounting={"external": {"host": " db.example.internal ", "password_secret": "s"}})
        )
        assert config["accounting"]["external"]["host"] == "db.example.internal"
        assert sc.accounting_in_cluster(config) is False

    def test_metrics_login_and_autoscaling_fields(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"metrics\.enabled must be a boolean"):
            _validate(_on(metrics={"enabled": "on"}))
        with pytest.raises(sc.SlurmConfigError, match=r"login\.enabled must be a boolean"):
            _validate(_on(login={"enabled": 1}))
        with pytest.raises(sc.SlurmConfigError, match=r"login\.replicas must be an integer"):
            _validate(_on(login={"replicas": 0}))
        with pytest.raises(sc.SlurmConfigError, match=r"autoscaling\.pending_jobs_threshold"):
            _validate(_on(autoscaling={"pending_jobs_threshold": 0}))
        with pytest.raises(sc.SlurmConfigError, match=r"autoscaling\.cooldown_seconds"):
            _validate(_on(autoscaling={"cooldown_seconds": 10}))
        with pytest.raises(sc.SlurmConfigError, match=r"autoscaling\.polling_interval_seconds"):
            _validate(_on(autoscaling={"polling_interval_seconds": 1}))

    def test_storage_choices(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"storage\.home must be 'efs' or 'none'"):
            _validate(_on(storage={"home": "nfs"}))
        with pytest.raises(
            sc.SlurmConfigError, match=r"storage\.scratch must be 'auto', 'fsx' or 'none'"
        ):
            _validate(_on(storage={"scratch": "ebs"}))

    def test_scratch_fsx_requires_fsx_lustre(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"scratch is 'fsx' but fsx_lustre is not enabled"
        ):
            _validate(_on(storage={"scratch": "fsx"}), fsx_enabled=False)
        assert (
            _validate(_on(storage={"scratch": "fsx"}), fsx_enabled=True)["storage"]["scratch"]
            == "fsx"
        )


class TestConfMaps:
    def test_values_normalize_to_lists_of_strings(self) -> None:
        config = _validate(
            _on(
                slurm_conf={
                    "MaxJobCount": 50000,
                    "DebugFlags": ["Backfill", "Gres"],
                    "KillWait": " 60 ",
                }
            )
        )
        assert config["slurm_conf"] == {
            "MaxJobCount": ["50000"],
            "DebugFlags": ["Backfill", "Gres"],
            "KillWait": ["60"],
        }

    def test_must_be_an_object(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"helm\.slurm\.slurm_conf must be an object"):
            _validate(_on(slurm_conf="MaxJobCount=1"))

    @pytest.mark.parametrize("key", ["Max Jobs", "1abc", "", "a" * 65, "Key-Name"])
    def test_keys_must_be_parameter_names(self, key: str) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"is not a Slurm configuration parameter name"
        ):
            _validate(_on(slurm_conf={key: "1"}))

    @pytest.mark.parametrize(
        "key", [*sorted(sc.OPERATOR_OWNED_SLURM_CONF_KEYS)[:6], "PartitionName"]
    )
    def test_operator_owned_slurm_conf_keys_are_rejected(self, key: str) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"is managed by the slurm-operator"):
            _validate(_on(slurm_conf={key: "x"}))

    @pytest.mark.parametrize("key", ["StorageHost", "DbdHost", "StoragePass"])
    def test_operator_owned_slurmdbd_conf_keys_are_rejected(self, key: str) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"slurmdbd_conf\." + key + " is managed"):
            _validate(_on(slurmdbd_conf={key: "x"}))

    def test_slurmdbd_conf_is_accepted(self) -> None:
        config = _validate(_on(slurmdbd_conf={"PurgeJobAfter": "12month", "CommitDelay": 1}))
        assert config["slurmdbd_conf"] == {"PurgeJobAfter": ["12month"], "CommitDelay": ["1"]}

    @pytest.mark.parametrize("value", [True, None, {"a": 1}, [True], [None]])
    def test_values_must_be_strings_or_numbers(self, value: object) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"must be a string, a number or a list of them"
        ):
            _validate(_on(slurm_conf={"MaxJobCount": value}))

    @pytest.mark.parametrize("value", ["", "  ", "a\nb", ["ok", ""]])
    def test_values_must_be_single_line_and_non_empty(self, value: object) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"must be non-empty single-line strings"):
            _validate(_on(slurm_conf={"MaxJobCount": value}))

    def test_empty_list_is_rejected(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"must not be an empty list"):
            _validate(_on(slurm_conf={"DebugFlags": []}))


class TestPartitions:
    def test_partitions_must_be_an_object(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"helm\.slurm\.partitions must be an object"):
            _validate(_on(partitions=["cpu"]))

    def test_partition_must_be_an_object(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"partitions\.cpu must be an object"):
            _validate(_on(partitions={"cpu": "big"}))

    @pytest.mark.parametrize("name", ["Cpu", "-gpu", "gpu_", "a" * 33, "1gpu", "gpu-", "gp.u"])
    def test_partition_name_must_be_a_short_dns_label(self, name: str) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"invalid partition name"):
            _validate(_on(partitions={name: _partition()}))

    @pytest.mark.parametrize("name", ["a", "x1", "big-mem", "a" * 32])
    def test_partition_name_shapes_accepted(self, name: str) -> None:
        config = _validate(_on(partitions={name: _partition()}))
        assert name in config["partitions"]

    def test_reserved_partition_name(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"partitions\.all is reserved by the Slinky chart"
        ):
            _validate(_on(partitions={"all": _partition()}))

    def test_unknown_partition_key_is_named(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"partitions\.cpu contains unknown key\(s\): instance_type"
        ):
            _validate(_on(partitions={"cpu": {"instance_type": "c6i.4xlarge"}}))

    @pytest.mark.parametrize("key", ["enabled", "default", "exclusive"])
    def test_partition_booleans(self, key: str) -> None:
        with pytest.raises(sc.SlurmConfigError, match=rf"partitions\.cpu\.{key} must be a boolean"):
            _validate(_on(partitions={"cpu": {key: "yes"}}))

    def test_node_pool_must_be_known(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"node_pool must be one of cpu, gpu, gpu-efa, got 'neuron'"
        ):
            _validate(_on(partitions={"cpu": {"node_pool": "neuron"}}))

    @pytest.mark.parametrize("field", ["cpu", "memory"])
    def test_cpu_and_memory_are_positive_quantities(self, field: str) -> None:
        with pytest.raises(sc.SlurmConfigError, match=rf"partitions\.cpu\.{field}"):
            _validate(_on(partitions={"cpu": {field: "0"}}))
        with pytest.raises(sc.SlurmConfigError, match=rf"partitions\.cpu\.{field}"):
            _validate(_on(partitions={"cpu": {field: "many"}}))

    def test_numeric_cpu_is_accepted(self) -> None:
        config = _validate(_on(partitions={"cpu": {"cpu": 16}}))
        assert config["partitions"]["cpu"]["_cpu_cores"] == 16.0

    @pytest.mark.parametrize(
        ("field", "value"), [("gpus", -1), ("gpus", 65), ("gpus", "1"), ("efa_interfaces", 65)]
    )
    def test_accelerator_counts_are_bounded_integers(self, field: str, value: object) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=rf"partitions\.gpu\.{field} must be an integer"
        ):
            _validate(_on(partitions={"gpu": {field: value}}))

    def test_gpus_need_a_gpu_pool(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"gpus is 2 but node_pool 'cpu' has no GPUs"):
            _validate(_on(partitions={"cpu": {"gpus": 2}}))

    def test_gpu_pool_needs_gpus(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"node_pool 'gpu' schedules onto GPU nodes but gpus is 0"
        ):
            _validate(_on(partitions={"gpu": {"gpus": 0}}))

    def test_efa_needs_the_efa_pool(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"efa_interfaces is 4 but node_pool 'gpu' has no EFA"
        ):
            _validate(_on(partitions={"gpu": {"efa_interfaces": 4}}))

    def test_efa_pool_needs_efa_interfaces(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"is the EFA pool but efa_interfaces is 0"):
            _validate(_on(partitions={"gpu-efa": {"enabled": True, "efa_interfaces": 0}}))

    def test_cpu_must_be_whole_cores(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"cpu must be a whole number of cores"):
            _validate(_on(partitions={"cpu": {"cpu": "7500m"}}))
        with pytest.raises(sc.SlurmConfigError, match=r"cpu must be a whole number of cores"):
            _validate(_on(partitions={"cpu": {"cpu": "500m"}}))
        assert (
            _validate(_on(partitions={"cpu": {"cpu": 2}}))["partitions"]["cpu"]["_cpu_cores"] == 2
        )

    def test_memory_needs_room_for_slurmd(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"memory must be at least 1Gi"):
            _validate(_on(partitions={"cpu": {"memory": "512Mi"}}))

    @pytest.mark.parametrize(
        ("memory", "expected"),
        [("1Gi", 1), ("2Gi", 1024), ("16Gi", 15360), ("32Gi", 31130), ("1024Gi", 996148)],
    )
    def test_real_memory_keeps_headroom(self, memory: str, expected: int) -> None:
        assert sc.slurm_real_memory_mib(int(sc.parse_k8s_quantity(memory))) == expected

    def test_capacity_type_choices(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"capacity_type must be one of any, spot, on-demand"
        ):
            _validate(_on(partitions={"cpu": {"capacity_type": "reserved"}}))

    def test_node_bounds(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"cpu\.min_nodes must be an integer between 0 and 1024"
        ):
            _validate(_on(partitions={"cpu": {"min_nodes": -1}}))
        with pytest.raises(
            sc.SlurmConfigError, match=r"cpu\.max_nodes must be an integer between 1 and 1024"
        ):
            _validate(_on(partitions={"cpu": {"max_nodes": 0}}))
        with pytest.raises(
            sc.SlurmConfigError, match=r"min_nodes must not exceed max_nodes, got 9 > 8"
        ):
            _validate(_on(partitions={"cpu": {"min_nodes": 9}}))

    @pytest.mark.parametrize("value", ["forever", "1:2", "1-2-3", 60, "", "1:60:00x"])
    def test_max_time_must_be_a_slurm_time(self, value: object) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"max_time must be a Slurm time limit"):
            _validate(_on(partitions={"cpu": {"max_time": value}}))

    @pytest.mark.parametrize(
        "value",
        [
            "60",
            "30:00",
            "12:00:00",
            "2-0",
            "2-12",
            "2-12:30",
            "7-00:00:00",
            "UNLIMITED",
            "INFINITE",
            " 60 ",
        ],
    )
    def test_max_time_shapes_accepted(self, value: str) -> None:
        config = _validate(_on(partitions={"cpu": {"max_time": value}}))
        assert config["partitions"]["cpu"]["max_time"] == value.strip()

    @pytest.mark.parametrize("features", ["fast", [1], ["bad feature"], ["ok", "-x"]])
    def test_features_must_be_feature_names(self, features: object) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"features must be a list of Slurm feature names"
        ):
            _validate(_on(partitions={"cpu": {"features": features}}))

    def test_duplicate_features(self) -> None:
        with pytest.raises(sc.SlurmConfigError, match=r"features lists a feature twice"):
            _validate(_on(partitions={"cpu": {"features": ["a", "a"]}}))

    def test_at_least_one_enabled_partition(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"must declare at least one enabled partition"
        ):
            _validate(_on(partitions={"cpu": {"enabled": False}, "gpu": {"enabled": False}}))

    def test_exactly_one_default_partition(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError,
            match=r"exactly one enabled partition default: true.*found \['cpu', 'gpu'\]",
        ):
            _validate(_on(partitions={"gpu": {"default": True}}))
        with pytest.raises(sc.SlurmConfigError, match=r"found none"):
            _validate(_on(partitions={"cpu": {"default": False}}))

    def test_disabled_partitions_do_not_count_as_default(self) -> None:
        config = _validate(_on(partitions={"cpu": {"enabled": False}, "gpu": {"default": True}}))
        assert list(sc.enabled_partitions(config)) == ["gpu"]


class TestDeploymentCoherence:
    def test_autoscaling_requires_observability(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError,
            match=r"partitions cpu, gpu autoscale.*cluster_observability is disabled",
        ):
            _validate(_on(), observability_enabled=False)

    def test_fixed_size_partitions_do_not_need_observability(self) -> None:
        raw = _on(partitions={"cpu": {"min_nodes": 2, "max_nodes": 2}, "gpu": {"enabled": False}})
        config = _validate(raw, observability_enabled=False)
        assert sc.autoscaled_partitions(config) == []

    def test_autoscaling_requires_metrics(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError, match=r"metrics\.enabled is false but a partition autoscales"
        ):
            _validate(_on(metrics={"enabled": False}))

    def test_disabled_block_skips_the_deployment_checks(self) -> None:
        config = _validate(
            {"enabled": False, "metrics": {"enabled": False}},
            observability_enabled=False,
            resource_quota={"max_pods": "1"},
        )
        assert config["enabled"] is False

    def test_no_quota_means_no_fit_check(self) -> None:
        config = _validate(_on(partitions={"cpu": {"max_nodes": 1000}}), resource_quota=None)
        assert config["partitions"]["cpu"]["max_nodes"] == 1000

    @pytest.mark.parametrize(
        ("field", "value", "quota_key"),
        [
            ("cpu", "200", "container_max_cpu"),
            ("memory", "3000Gi", "container_max_memory"),
        ],
    )
    def test_node_larger_than_the_container_ceiling(
        self, field: str, value: str, quota_key: str
    ) -> None:
        with pytest.raises(
            sc.SlurmConfigError,
            match=rf"partitions\.cpu\.{field}=.* exceeds resource_quota\.{quota_key}",
        ):
            _validate(_on(partitions={"cpu": {field: value}}))

    def test_more_gpus_than_the_container_ceiling(self) -> None:
        with pytest.raises(
            sc.SlurmConfigError,
            match=r"partitions\.gpu\.gpus=9 exceeds resource_quota\.container_max_gpu",
        ):
            _validate(_on(partitions={"gpu": {"gpus": 9}}))

    @pytest.mark.parametrize(
        ("overrides", "quota_key"),
        [
            ({"cpu": {"max_nodes": 60}}, "max_cpu"),
            ({"cpu": {"memory": "1000Gi", "max_nodes": 8}}, "max_memory"),
            ({"gpu": {"gpus": 8, "max_nodes": 8}}, "max_gpu"),
            ({"cpu": {"cpu": "1", "memory": "1Gi", "max_nodes": 48}}, "max_pods"),
        ],
    )
    def test_partitions_at_max_nodes_must_fit_the_namespace_ceiling(
        self, overrides: dict[str, Any], quota_key: str
    ) -> None:
        with pytest.raises(
            sc.SlurmConfigError,
            match=rf"need .* of resource_quota\.{quota_key} but the gco-jobs ceiling",
        ):
            _validate(_on(partitions=overrides))

    def test_unparseable_quota_values_are_left_to_the_regional_stack(self) -> None:
        quota = dict(QUOTA, container_max_cpu="lots", max_pods="many")
        assert _validate(_on(), resource_quota=quota)["enabled"] is True

    def test_control_plane_pods_count_every_shape(self) -> None:
        assert sc.control_plane_pods(_validate(_on())) == 1 + 2 + 1 + 1 + 1
        external = _validate(_on(accounting={"external": {"host": "db", "password_secret": "s"}}))
        assert sc.control_plane_pods(external) == 1 + 2 + 1 + 1
        bare = _validate(
            _on(accounting={"enabled": False}, login={"enabled": False}, restapi={"replicas": 1})
        )
        assert sc.control_plane_pods(bare) == 2


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------


class TestDerivedViews:
    def test_slurm_enabled(self) -> None:
        assert sc.slurm_enabled(_validate(None)) is False
        assert sc.slurm_enabled(_validate(_on())) is True

    def test_scratch_backend(self) -> None:
        auto = _validate(_on())
        assert sc.scratch_backend(auto, fsx_enabled=True) == "fsx"
        assert sc.scratch_backend(auto, fsx_enabled=False) == "none"
        assert (
            sc.scratch_backend(_validate(_on(storage={"scratch": "none"})), fsx_enabled=True)
            == "none"
        )
        assert (
            sc.scratch_backend(
                _validate(_on(storage={"scratch": "fsx"}), fsx_enabled=True), fsx_enabled=True
            )
            == "fsx"
        )

    def test_accounting_in_cluster(self) -> None:
        assert sc.accounting_in_cluster(_validate(_on())) is True
        assert sc.accounting_in_cluster(_validate(_on(accounting={"enabled": False}))) is False


# ---------------------------------------------------------------------------
# Chart values
# ---------------------------------------------------------------------------


@pytest.fixture
def values() -> dict[str, Any]:
    return sc.slurm_chart_values(_validate(_on()), fsx_enabled=False)


class TestChartValues:
    def test_cluster_name_and_controller(self, values: dict[str, Any]) -> None:
        assert values["clusterName"] == "gco-slurm"
        controller = values["controller"]
        assert controller["metadata"]["annotations"] == {"karpenter.sh/do-not-disrupt": "true"}
        assert controller["persistence"] == {
            "enabled": True,
            "existingClaim": "",
            "storageClassName": "gco-slurm-gp3",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "20Gi"}},
        }
        assert controller["metrics"] == {"enabled": True, "serviceMonitor": {"enabled": False}}
        assert controller["podSpec"]["priorityClassName"] == "gco-platform-critical"
        assert controller["podSpec"]["nodeSelector"]["karpenter.sh/capacity-type"] == "on-demand"
        assert controller["podSpec"]["affinity"] == sc.PLATFORM_POD_AFFINITY

    def test_every_non_worker_pod_stays_off_accelerator_pools(self, values: dict[str, Any]) -> None:
        """Regression: the controller landed on a g4dn (LimitRange gpu:0 default +
        ExtendedResourceToleration) and do-not-disrupt pinned it there."""
        pods = {
            "controller": values["controller"]["podSpec"],
            "restapi": values["restapi"]["podSpec"],
            "accounting": values["accounting"]["podSpec"],
            "login": values["loginsets"]["gco"]["podSpec"],
        }
        for name, spec in pods.items():
            assert spec["nodeSelector"] == sc.PLATFORM_POD_NODE_SELECTOR, name
            assert spec["affinity"] == sc.PLATFORM_POD_AFFINITY, name
            assert spec["nodeSelector"] is not sc.PLATFORM_POD_NODE_SELECTOR, name
            assert spec["affinity"] is not sc.PLATFORM_POD_AFFINITY, name
        [term] = sc.PLATFORM_POD_AFFINITY["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"]
        assert term == {"matchExpressions": [{"key": "workload-type", "operator": "DoesNotExist"}]}
        # Workers are the exception: they live on the pool their partition names.
        for name, nodeset in values["nodesets"].items():
            assert "affinity" not in nodeset["podSpec"], name
            assert nodeset["podSpec"]["nodeSelector"]["workload-type"], name

    def test_slurm_conf_layers(self, values: dict[str, Any]) -> None:
        conf = values["controller"]["extraConfMap"]
        assert conf["SelectType"] == ["select/cons_tres"]
        assert conf["GresTypes"] == ["gpu"] and conf["AccountingStorageTRES"] == ["gres/gpu"]
        for key in conf:
            assert key not in sc.OPERATOR_OWNED_SLURM_CONF_KEYS
        assert values["configFiles"]["cgroup.conf"] == sc.CGROUP_CONF
        assert "ConstrainDevices=yes" in values["configFiles"]["cgroup.conf"]
        assert values["configFiles"]["gres.conf"] == "AutoDetect=nvidia\n"

    def test_operator_slurm_conf_overrides_the_base_layer(self) -> None:
        config = _validate(_on(slurm_conf={"MinJobAge": 30, "MaxJobCount": 50000}))
        conf = sc.slurm_chart_values(config, fsx_enabled=False)["controller"]["extraConfMap"]
        assert conf["MinJobAge"] == ["30"] and conf["MaxJobCount"] == ["50000"]

    def test_cpu_only_cluster_has_no_gpu_configuration(self) -> None:
        config = _validate(_on(partitions={"gpu": {"enabled": False}}))
        values = sc.slurm_chart_values(config, fsx_enabled=False)
        assert "gres.conf" not in values["configFiles"]
        assert "GresTypes" not in values["controller"]["extraConfMap"]
        assert list(values["nodesets"]) == ["cpu"]

    def test_restapi_and_accounting(self, values: dict[str, Any]) -> None:
        assert values["restapi"]["replicas"] == 2
        assert values["restapi"]["pdb"] == {"enabled": True, "minAvailable": 1}
        accounting = values["accounting"]
        assert accounting["enabled"] is True
        assert accounting["storageConfig"] == {
            "host": "slinky-slurm-mariadb",
            "port": 3306,
            "database": "slurm_acct_db",
            "username": "slurm",
            "passwordKeyRef": {"name": "slinky-slurm-mariadb", "key": "password"},
        }
        assert accounting["podSpec"]["priorityClassName"] == "gco-platform-critical"

    def test_single_restapi_replica_has_no_pdb(self) -> None:
        values = sc.slurm_chart_values(_validate(_on(restapi={"replicas": 1})), fsx_enabled=False)
        assert values["restapi"]["pdb"]["enabled"] is False

    def test_external_accounting_database(self) -> None:
        external = {
            "host": "db.example.internal",
            "port": 3307,
            "database": "acct",
            "username": "u",
            "password_secret": "slurm-db",
            "password_key": "pw",
        }
        config = _validate(
            _on(accounting={"external": external}, slurmdbd_conf={"PurgeJobAfter": "12month"})
        )
        accounting = sc.slurm_chart_values(config, fsx_enabled=False)["accounting"]
        assert accounting["storageConfig"] == {
            "host": "db.example.internal",
            "port": 3307,
            "database": "acct",
            "username": "u",
            "passwordKeyRef": {"name": "slurm-db", "key": "pw"},
        }
        assert accounting["extraConfMap"] == {"PurgeJobAfter": ["12month"]}

    def test_accounting_off(self) -> None:
        values = sc.slurm_chart_values(
            _validate(_on(accounting={"enabled": False})), fsx_enabled=False
        )
        assert values["accounting"] == {"enabled": False}

    def test_login_set_is_cluster_ip_with_home(self, values: dict[str, Any]) -> None:
        login = values["loginsets"]["gco"]
        assert login["enabled"] is True and login["replicas"] == 1
        assert login["service"] == {"spec": {"type": "ClusterIP"}}
        assert login["login"]["volumeMounts"] == [
            {"name": "home", "mountPath": "/home", "subPath": "slurm/home"}
        ]
        assert login["podSpec"]["volumes"] == [
            {"name": "home", "persistentVolumeClaim": {"claimName": "gco-shared-storage"}}
        ]
        assert login["podSpec"]["nodeSelector"]["karpenter.sh/capacity-type"] == "on-demand"
        # The init container creates the API's default working directory on
        # the empty shared claim (observed live: /home starts empty and the
        # slurm user's passwd home is /home/slurm).
        [init] = login["podSpec"]["initContainers"]
        assert init["name"] == "home-init"
        assert init["image"] == "public.ecr.aws/docker/library/alpine:3.24.3"
        assert init["command"] == ["sh", "-c", "mkdir -p /home/slurm && chmod 2775 /home/slurm"]
        assert init["volumeMounts"] == [
            {"name": "home", "mountPath": "/home", "subPath": "slurm/home"}
        ]
        assert init["securityContext"]["runAsNonRoot"] is True
        assert sc.SLURM_DEFAULT_WORKING_DIRECTORY == "/home/slurm"

    def test_accounting_keeps_the_job_comment(self, values: dict[str, Any]) -> None:
        """The API records the caller in the comment; slurmdbd drops it without this flag."""
        assert values["controller"]["extraConfMap"]["AccountingStoreFlags"] == ["job_comment"]

    def test_chart_catch_all_partition_is_off(self, values: dict[str, Any]) -> None:
        assert values["partitions"] == {"all": {"enabled": False}}

    def test_cpu_nodeset(self, values: dict[str, Any]) -> None:
        cpu = values["nodesets"]["cpu"]
        assert cpu["enabled"] is True and cpu["replicas"] == 0
        resources = cpu["slurmd"]["resources"]
        assert resources["requests"] == resources["limits"] == {"cpu": "8", "memory": "32Gi"}
        assert cpu["extraConfMap"] == {
            "CPUs": ["8"],
            "Boards": ["1"],
            "SocketsPerBoard": ["1"],
            "CoresPerSocket": ["8"],
            "ThreadsPerCore": ["1"],
            "RealMemory": ["31130"],
        }
        assert cpu["partition"] == {
            "enabled": True,
            "configMap": {
                "Default": "YES",
                "State": "UP",
                "MaxTime": "UNLIMITED",
                "DefMemPerCPU": "3891",
            },
        }
        assert cpu["podSpec"]["nodeSelector"] == {
            "kubernetes.io/os": "linux",
            "kubernetes.io/arch": "amd64",
            "workload-type": "cpu",
        }
        assert cpu["podSpec"]["tolerations"] == []

    def test_gpu_nodeset(self, values: dict[str, Any]) -> None:
        gpu = values["nodesets"]["gpu"]
        assert gpu["slurmd"]["resources"]["limits"]["nvidia.com/gpu"] == "1"
        assert gpu["extraConfMap"] == {
            "CPUs": ["8"],
            "Boards": ["1"],
            "SocketsPerBoard": ["1"],
            "CoresPerSocket": ["8"],
            "ThreadsPerCore": ["1"],
            "RealMemory": ["31130"],
            "Gres": ["gpu:1"],
        }
        assert gpu["partition"]["configMap"] == {
            "Default": "NO",
            "State": "UP",
            "MaxTime": "2-00:00:00",
            "DefMemPerCPU": "3891",
            "OverSubscribe": "EXCLUSIVE",
        }
        assert gpu["podSpec"]["nodeSelector"]["workload-type"] == "gpu"
        assert gpu["podSpec"]["tolerations"] == [
            {"key": "nvidia.com/gpu", "operator": "Equal", "value": "true", "effect": "NoSchedule"}
        ]

    def test_efa_nodeset_with_features_capacity_type_and_fsx_scratch(self) -> None:
        raw = _on(
            partitions={"gpu-efa": {"enabled": True, "features": ["h100", "efa"], "min_nodes": 1}}
        )
        # Two p5-class nodes exceed the default 400-vCPU namespace quota on
        # their own; an EFA deployment raises resource_quota, as the docs say.
        config = _validate(raw, fsx_enabled=True, resource_quota=dict(QUOTA, max_cpu="1000"))
        values = sc.slurm_chart_values(config, fsx_enabled=True)
        efa = values["nodesets"]["gpu-efa"]
        assert efa["replicas"] == 1
        assert efa["slurmd"]["resources"]["requests"] == {
            "cpu": "180",
            "memory": "1800Gi",
            "nvidia.com/gpu": "8",
            "vpc.amazonaws.com/efa": "32",
        }
        efa_memory_bytes = int(
            sc.parse_k8s_quantity(sc.SLURM_DEFAULTS["partitions"]["gpu-efa"]["memory"])
        )
        assert efa["extraConfMap"] == {
            "CPUs": ["180"],
            "Boards": ["1"],
            "SocketsPerBoard": ["1"],
            "CoresPerSocket": ["180"],
            "ThreadsPerCore": ["1"],
            "RealMemory": [str(sc.slurm_real_memory_mib(efa_memory_bytes))],
            "Gres": ["gpu:8"],
            "Features": ["h100", "efa"],
        }
        assert efa["podSpec"]["nodeSelector"]["karpenter.sh/capacity-type"] == "on-demand"
        assert {t["key"] for t in efa["podSpec"]["tolerations"]} == {
            "nvidia.com/gpu",
            "vpc.amazonaws.com/efa",
        }
        assert efa["slurmd"]["volumeMounts"][1] == {"name": "scratch", "mountPath": "/scratch"}
        assert efa["podSpec"]["volumes"][1] == {
            "name": "scratch",
            "persistentVolumeClaim": {"claimName": "gco-fsx-storage"},
        }

    def test_no_home_no_scratch(self) -> None:
        config = _validate(_on(storage={"home": "none", "scratch": "none"}))
        values = sc.slurm_chart_values(config, fsx_enabled=True)
        assert values["nodesets"]["cpu"]["slurmd"]["volumeMounts"] == []
        assert values["nodesets"]["cpu"]["podSpec"]["volumes"] == []
        assert values["loginsets"]["gco"]["login"]["volumeMounts"] == []
        # No shared home: nothing to create, and no volume for an init to mount.
        assert values["loginsets"]["gco"]["podSpec"]["initContainers"] == []

    def test_nodeset_values_do_not_alias_each_other(self, values: dict[str, Any]) -> None:
        values["nodesets"]["cpu"]["podSpec"]["volumes"].clear()
        assert values["nodesets"]["gpu"]["podSpec"]["volumes"]
        assert values["loginsets"]["gco"]["podSpec"]["volumes"]

    def test_values_round_trip_through_yaml(self, values: dict[str, Any]) -> None:
        assert yaml.safe_load(yaml.safe_dump(values)) == values


class TestScaledObjects:
    def test_one_scaled_object_per_autoscaled_partition(self, values: dict[str, Any]) -> None:
        objects = values["extraObjects"]
        assert [o["metadata"]["name"] for o in objects] == [
            "slinky-slurm-autoscale-cpu",
            "slinky-slurm-autoscale-gpu",
        ]
        for obj in objects:
            assert obj["apiVersion"] == "keda.sh/v1alpha1" and obj["kind"] == "ScaledObject"
            assert obj["metadata"]["namespace"] == "gco-jobs"
            assert obj["metadata"]["labels"]["app.kubernetes.io/part-of"] == "slurm"

    def test_scale_target_bounds_and_idle(self, values: dict[str, Any]) -> None:
        cpu = values["extraObjects"][0]["spec"]
        assert cpu["scaleTargetRef"] == {
            "apiVersion": "slinky.slurm.net/v1beta1",
            "kind": "NodeSet",
            "name": "slinky-slurm-worker-cpu",
        }
        assert cpu["idleReplicaCount"] == 0
        assert cpu["minReplicaCount"] == 1 and cpu["maxReplicaCount"] == 8
        assert cpu["pollingInterval"] == 30 and cpu["cooldownPeriod"] == 300
        behavior = cpu["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
        assert behavior["scaleDown"]["stabilizationWindowSeconds"] == 600

    def test_triggers_read_pending_jobs_and_largest_pending_job(
        self, values: dict[str, Any]
    ) -> None:
        pending, largest = values["extraObjects"][1]["spec"]["triggers"]
        assert pending["type"] == largest["type"] == "prometheus"
        assert pending["metricType"] == largest["metricType"] == "Value"
        assert pending["metadata"]["serverAddress"] == sc.SLURM_PROMETHEUS_URL
        # Every selector names the Slinky controller's scrape job: the AWS PCS
        # backend's controller serves identically named series into the same
        # Prometheus under job="pcs-slurm-controller", and an unscoped sum
        # would scale Slinky's workers for PCS's queue.
        gpu = 'job="slinky-slurm-controller", partition="gpu"'
        assert sc.SLURM_METRICS_JOB == sc.SLURM_CONTROLLER_NAME == "slinky-slurm-controller"
        assert f"slurm_partition_jobs_pending{{{gpu}}}" in pending["metadata"]["query"]
        assert pending["metadata"]["threshold"] == "1"
        assert f"slurm_partition_jobs_max_job_nodes_nohold{{{gpu}}}" in largest["metadata"]["query"]
        assert largest["metadata"]["threshold"] == "1"
        for trigger in (pending, largest):
            query = trigger["metadata"]["query"]
            # Busy nodes are ALLOCATED or MIXED: a worker running one 1-CPU job
            # is MIXED, and counting only ALLOCATED nodes read it as idle, so
            # KEDA scaled the NodeSet to zero under a running job (live,
            # 2026-10-28).
            assert f"slurm_partition_nodes_alloc{{{gpu}}}" in query
            assert f"slurm_partition_nodes_mixed{{{gpu}}}" in query
            # Every series is guarded so a missing metric never reads as idle.
            assert query.count("or vector(0)") == 3
            assert query.count("slurm_") == query.count(sc.SLURM_METRICS_JOB)

    def test_floor_above_zero_has_no_idle_replica_count(self) -> None:
        raw = _on(
            partitions={"cpu": {"min_nodes": 2}},
            autoscaling={
                "pending_jobs_threshold": 3,
                "cooldown_seconds": 120,
                "polling_interval_seconds": 15,
            },
        )
        spec = sc.slurm_chart_values(_validate(raw), fsx_enabled=False)["extraObjects"][0]["spec"]
        assert "idleReplicaCount" not in spec
        assert spec["minReplicaCount"] == 2
        assert spec["triggers"][0]["metadata"]["threshold"] == "3"
        assert spec["pollingInterval"] == 15 and spec["cooldownPeriod"] == 120

    def test_fixed_size_partition_has_no_scaled_object(self) -> None:
        raw = _on(partitions={"cpu": {"min_nodes": 3, "max_nodes": 3}})
        values = sc.slurm_chart_values(_validate(raw), fsx_enabled=False)
        assert [o["metadata"]["name"] for o in values["extraObjects"]] == [
            "slinky-slurm-autoscale-gpu"
        ]
        assert values["nodesets"]["cpu"]["replicas"] == 3


# ---------------------------------------------------------------------------
# kubectl-applier replacements
# ---------------------------------------------------------------------------


class TestReplacements:
    def test_disabled_emits_only_the_api_gate_as_false(self) -> None:
        """The API pod's GCO_SLURM_ENABLED must always resolve (core manifest)."""
        assert sc.compute_slurm_replacements(_validate(_on()), enabled=False) == {
            "{{SLURM_API_ENABLED}}": "false"
        }

    def test_in_cluster_accounting_with_backup(self) -> None:
        replacements = sc.compute_slurm_replacements(_validate(_on()), enabled=True)
        assert replacements == {
            "{{SLURM_API_ENABLED}}": "true",
            "{{SLURM_CLUSTER_NAME}}": "gco-slurm",
            "{{SLURM_ACCOUNTING_DB_ENABLED}}": "true",
            "{{SLURM_ACCOUNTING_DB_STORAGE_SIZE}}": "20Gi",
            "{{SLURM_ACCOUNTING_BACKUP_ENABLED}}": "true",
            "{{SLURM_ACCOUNTING_BACKUP_SCHEDULE}}": "0 3 * * *",
        }
        assert set(replacements) <= set(sc.SLURM_MANIFEST_TOKENS)

    def test_backup_off_leaves_the_backup_tokens_unresolved(self) -> None:
        config = _validate(_on(accounting={"backup": {"enabled": False}}))
        replacements = sc.compute_slurm_replacements(config, enabled=True)
        assert "{{SLURM_ACCOUNTING_DB_ENABLED}}" in replacements
        assert not any("BACKUP" in token for token in replacements)

    def test_external_database_leaves_the_database_tokens_unresolved(self) -> None:
        config = _validate(_on(accounting={"external": {"host": "db", "password_secret": "s"}}))
        assert sc.compute_slurm_replacements(config, enabled=True) == {
            "{{SLURM_API_ENABLED}}": "true",
            "{{SLURM_CLUSTER_NAME}}": "gco-slurm",
        }

    def test_every_token_is_upper_snake(self) -> None:
        for token in sc.SLURM_MANIFEST_TOKENS:
            assert re.fullmatch(r"\{\{[A-Z0-9_]+\}\}", token)


# ---------------------------------------------------------------------------
# Config loader wiring
# ---------------------------------------------------------------------------


def _loader(valid_cdk_context: dict[str, Any], **context: Any) -> ConfigLoader:
    merged = copy.deepcopy(valid_cdk_context)
    merged.update(context)
    return ConfigLoader(cdk.App(context=merged))


class TestConfigLoaderWiring:
    def test_absent_block_reads_as_off(self, valid_cdk_context: dict[str, Any]) -> None:
        assert _loader(valid_cdk_context).get_slurm_config()["enabled"] is False
        assert (
            _loader(valid_cdk_context, helm={"volcano": {}}).get_slurm_config()["enabled"] is False
        )
        assert _loader(valid_cdk_context, helm=[]).get_slurm_config()["enabled"] is False

    def test_valid_block_is_returned_with_defaults(self, valid_cdk_context: dict[str, Any]) -> None:
        loader = _loader(
            valid_cdk_context,
            helm={"slurm": {"enabled": True, "partitions": {"gpu": {"max_nodes": 2}}}},
        )
        config = loader.get_slurm_config()
        assert config["enabled"] is True
        assert config["partitions"]["gpu"]["max_nodes"] == 2
        assert config["accounting"]["enabled"] is True

    def test_malformed_block_fails_at_construction(self, valid_cdk_context: dict[str, Any]) -> None:
        with pytest.raises(ConfigValidationError, match=r"helm\.slurm\.partitions\.cpu\.node_pool"):
            _loader(
                valid_cdk_context, helm={"slurm": {"partitions": {"cpu": {"node_pool": "mars"}}}}
            )

    def test_quota_from_the_context_is_applied(self, valid_cdk_context: dict[str, Any]) -> None:
        with pytest.raises(ConfigValidationError, match=r"resource_quota\.max_pods"):
            _loader(
                valid_cdk_context,
                helm={"slurm": {"enabled": True}},
                resource_quota={"max_pods": "4"},
            )

    def test_observability_off_blocks_autoscaling(self, valid_cdk_context: dict[str, Any]) -> None:
        with pytest.raises(ConfigValidationError, match=r"cluster_observability is disabled"):
            _loader(
                valid_cdk_context,
                helm={"slurm": {"enabled": True}},
                cluster_observability={"enabled": False},
            )

    def test_scratch_fsx_needs_fsx_in_every_region(self, valid_cdk_context: dict[str, Any]) -> None:
        helm = {"slurm": {"enabled": True, "storage": {"scratch": "fsx"}}}
        with pytest.raises(ConfigValidationError, match=r"fsx_lustre is not enabled"):
            _loader(valid_cdk_context, helm=helm)
        loader = _loader(valid_cdk_context, helm=helm, fsx_lustre={"enabled": True})
        assert loader.get_slurm_config()["storage"]["scratch"] == "fsx"
        assert loader._slurm_fsx_available_everywhere() is True

    def test_fsx_availability_needs_at_least_one_region(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        context = copy.deepcopy(valid_cdk_context)
        context["deployment_regions"]["regional"] = []
        loader = _loader(context, fsx_lustre={"enabled": True})
        assert loader._slurm_fsx_available_everywhere() is False

    def test_getter_reports_errors_as_config_validation_errors(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(valid_cdk_context)
        # Construction already validated the block; make the raw getter return a
        # bad block afterwards so the getter's own error path is exercised.
        original = loader._raw_slurm_config
        loader._raw_slurm_config = lambda: {"enabled": "yes"}  # type: ignore[method-assign]
        try:
            with pytest.raises(
                ConfigValidationError, match=r"helm\.slurm\.enabled must be a boolean"
            ):
                loader.get_slurm_config()
        finally:
            loader._raw_slurm_config = original  # type: ignore[method-assign]
