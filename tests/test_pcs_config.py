"""Tests for gco/pcs_config.py — the AWS PCS block (``pcs`` / ``pcs_regions``).

The module is the single source of the block's shape: the config loader, the
regional stack, the kubectl-applier manifests, the ``gco stacks pcs`` and
``gco slurm`` CLIs, the MCP tools and the live validation harness all call it.
These tests pin the defaults, every validation branch, the derived views (the
JWT identity claims among them) and the loader wiring, including the per-region
overlays and the run-scoped ``feature_enabled_overrides=pcs`` context.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import aws_cdk as cdk
import pytest
import yaml

from gco import pcs_config as pc
from gco.config.config_loader import ConfigLoader, ConfigValidationError


def _validate(raw: object = None, **kwargs: Any) -> dict[str, Any]:
    return pc.validate_pcs_config(raw, **kwargs)


def _on(**overrides: Any) -> dict[str, Any]:
    return {"enabled": True, **overrides}


def _managed(**overrides: Any) -> dict[str, Any]:
    return _on(mode="managed", managed=overrides)


def _group(**overrides: Any) -> dict[str, Any]:
    return {"name": "gco-compute", "instance_types": ["c6i.large"], **overrides}


def _queue(**overrides: Any) -> dict[str, Any]:
    return {"name": "gco", "node_groups": ["gco-compute"], "default": True, **overrides}


def _groups_and_queues(*groups: dict[str, Any], queues: list[dict[str, Any]] | None = None):
    return {
        "node_groups": list(groups) or [_group()],
        "queues": queues if queues is not None else [_queue()],
    }


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_absent_block_is_off_with_every_default(self) -> None:
        config = _validate(None)
        assert config["enabled"] is False
        assert config["mode"] == "managed"
        assert config["cluster_id"] is None
        assert config["endpoint_cidr"] is None
        assert config["identity"] == {
            "name": "ec2-user",
            "uid": 1000,
            "gid": 1000,
            "gids": [],
            "dir": "/home/ec2-user",
            "shell": "/bin/bash",
            "gecos": None,
        }
        managed = config["managed"]
        assert managed["cluster_name"] is None
        assert managed["slurm_version"] == "25.11"
        assert managed["size"] == "SMALL"
        assert managed["accounting"] == {"enabled": True, "purge_days": 30}
        assert managed["scale_down_idle_seconds"] == 600
        assert managed["storage"] == {"home": "efs", "scratch": "auto"}
        assert managed["login"] == {
            "enabled": True,
            "instance_type": "t3.medium",
            "ami": "sample",
            "availability_zones": [],
            "_architecture": "x86_64",
        }
        assert managed["metrics"] == {"enabled": False}
        assert managed["lifecycle_logs"] == {"enabled": True}
        assert managed["slurm_custom_settings"] == {}
        assert managed["removal_policy"] == "destroy"

    def test_empty_block_equals_absent_block(self) -> None:
        assert _validate({}) == _validate(None)

    def test_default_node_group_scales_from_zero_on_demand_with_the_sample_ami(self) -> None:
        [group] = _validate(None)["managed"]["node_groups"]
        assert group["name"] == "gco-compute"
        assert group["instance_types"] == ["c6i.large"]
        assert (group["min_nodes"], group["max_nodes"]) == (0, 4)
        assert group["purchase_option"] == "ONDEMAND"
        assert group["spot_allocation_strategy"] is None
        assert group["capacity_reservation"] is None
        assert group["availability_zones"] == []
        assert group["ami"] == "sample"
        assert group["efa"] is False
        assert group["efa_interfaces"] == 1
        assert group["placement_group"] is False
        assert group["slurm_custom_settings"] == {}
        assert group["gres_custom_settings"] == []
        assert group["scale_down_idle_seconds"] is None
        assert group["_architecture"] == "x86_64"

    def test_default_queue_is_the_default_and_maps_the_default_group(self) -> None:
        [queue] = _validate(None)["managed"]["queues"]
        assert queue == {
            "name": "gco",
            "node_groups": ["gco-compute"],
            "default": True,
            "slurm_custom_settings": {},
        }

    def test_defaults_do_not_alias_the_module_constant(self) -> None:
        config = _validate(None)
        config["managed"]["node_groups"][0]["max_nodes"] = 99
        config["identity"]["gids"].append(5)
        assert pc.PCS_DEFAULTS["managed"]["node_groups"][0] == {
            "name": "gco-compute",
            "instance_types": ["c6i.large"],
            "min_nodes": 0,
            "max_nodes": 4,
        }
        assert pc.PCS_DEFAULTS["identity"]["gids"] == []

    def test_the_constants_the_other_surfaces_share(self) -> None:
        assert pc.PCS_FEATURE_KEY == "pcs"
        assert pc.PCS_REGIONS_KEY == "pcs_regions"
        assert pc.SLURM_BACKENDS == ("slinky", "pcs")
        assert pc.PCS_RESTAPI_PORT == 6820
        assert pc.PCS_SUPPORTED_SLURM_VERSIONS == ("25.11", "26.05")
        assert pc.PCS_HOME_EFS_PATH == "/pcs/home"
        assert pc.PCS_HOME_MOUNT_PATH == "/home"
        assert pc.PCS_SCRATCH_MOUNT_PATH == "/scratch"


# ---------------------------------------------------------------------------
# Top-level validation
# ---------------------------------------------------------------------------


class TestTopLevel:
    def test_block_must_be_an_object(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"^pcs must be an object"):
            _validate([])

    def test_unknown_keys_fail_by_name(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs contains unknown key\(s\): colour"):
            _validate({"colour": "blue"})

    @pytest.mark.parametrize("value", ["true", 1, None])
    def test_enabled_must_be_a_boolean(self, value: object) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.enabled must be a boolean"):
            _validate({"enabled": value})

    def test_mode_must_be_known(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.mode must be one of managed, existing"):
            _validate({"mode": "hosted"})

    def test_existing_mode_requires_a_cluster_id(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.cluster_id is required"):
            _validate(_on(mode="existing"))

    def test_existing_mode_with_a_cluster_id_is_valid_and_keeps_the_managed_defaults(self) -> None:
        config = _validate(_on(mode="existing", cluster_id="pcs_0123abcd"))
        assert config["cluster_id"] == "pcs_0123abcd"
        assert config["managed"]["queues"][0]["name"] == "gco"

    @pytest.mark.parametrize("value", ["my-cluster", "pcs-abc", "", 7])
    def test_cluster_id_must_be_a_generated_pcs_id(self, value: object) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.cluster_id"):
            _validate(_on(mode="existing", cluster_id=value))

    def test_cluster_id_is_stripped(self) -> None:
        assert _validate({"cluster_id": " pcs_abc "})["cluster_id"] == "pcs_abc"

    def test_endpoint_cidr_is_normalized(self) -> None:
        assert _validate({"endpoint_cidr": "10.1.2.3/16"})["endpoint_cidr"] == "10.1.0.0/16"

    @pytest.mark.parametrize("value", ["not-a-cidr", "10.0.0.0/33", ""])
    def test_endpoint_cidr_must_be_a_cidr(self, value: str) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.endpoint_cidr"):
            _validate({"endpoint_cidr": value})

    def test_endpoint_cidr_must_be_ipv4(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"must be IPv4"):
            _validate({"endpoint_cidr": "fd00::/8"})

    def test_a_null_nested_block_keeps_its_defaults(self) -> None:
        config = _validate({"identity": None, "managed": None})
        assert config["identity"]["name"] == "ec2-user"
        assert config["managed"]["size"] == "SMALL"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_unknown_keys_fail_by_name(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity contains unknown key"):
            _validate({"identity": {"password": "x"}})

    def test_identity_must_be_an_object(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity must be an object"):
            _validate({"identity": "ec2-user"})

    @pytest.mark.parametrize("name", ["", "Root", "1abc", "a" * 33, "with space"])
    def test_name_must_be_a_posix_user_name(self, name: str) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity\.name"):
            _validate({"identity": {"name": name}})

    def test_name_is_stripped(self) -> None:
        config = _validate({"identity": {"name": " hpcuser "}})
        assert config["identity"]["name"] == "hpcuser"
        assert config["identity"]["dir"] == "/home/hpcuser"

    @pytest.mark.parametrize("key", ["uid", "gid"])
    @pytest.mark.parametrize("value", [-1, True, "1000", 2**31])
    def test_uid_and_gid_are_bounded_integers(self, key: str, value: object) -> None:
        with pytest.raises(pc.PcsConfigError, match=rf"pcs\.identity\.{key} must be an integer"):
            _validate({"identity": {key: value}})

    def test_gids_must_be_a_list_of_integers(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity\.gids must be a list"):
            _validate({"identity": {"gids": 100}})
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity\.gids\[1\] must be an integer"):
            _validate({"identity": {"gids": [100, "200"]}})
        assert _validate({"identity": {"gids": [100, 200]}})["identity"]["gids"] == [100, 200]

    def test_dir_defaults_to_home_of_name_and_must_be_absolute_when_set(self) -> None:
        # The all-default block is managed mode with the EFS home, where
        # the directory must sit straight under /home; existing mode and a
        # managed cluster without the shared home accept any absolute path.
        assert _validate({"identity": {"dir": "/home/me"}})["identity"]["dir"] == "/home/me"
        existing = _on(mode="existing", cluster_id="pcs_abc", identity={"dir": "/scratch/me"})
        assert _validate(existing)["identity"]["dir"] == "/scratch/me"
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity\.dir must be an absolute path"):
            _validate({"identity": {"dir": "relative"}})
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity\.dir must be a non-empty"):
            _validate({"identity": {"dir": "  "}})

    def test_dir_must_sit_straight_under_home_when_the_managed_cluster_mounts_efs(self) -> None:
        for directory in ("/scratch/me", "/home/team/me", "/home", "/home/Me"):
            with pytest.raises(
                pc.PcsConfigError, match=r"pcs\.identity\.dir must be a directory straight under"
            ):
                _validate(_managed() | {"identity": {"dir": directory}})
        # The directory need not be named after the identity: the lifecycle
        # action creates whatever /home/<x> the block names.
        config = _validate(_managed() | {"identity": {"name": "hpc", "dir": "/home/shared-hpc"}})
        assert config["identity"]["dir"] == "/home/shared-hpc"

    def test_dir_is_required_without_a_shared_home_in_managed_mode(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity\.dir is required"):
            _validate(_managed(storage={"home": "none"}))
        config = _validate(_managed(storage={"home": "none"}) | {"identity": {"dir": "/tmp"}})
        assert config["identity"]["dir"] == "/tmp"

    def test_dir_is_not_required_without_a_shared_home_in_existing_mode(self) -> None:
        config = _validate(
            _on(mode="existing", cluster_id="pcs_abc", managed={"storage": {"home": "none"}})
        )
        assert config["identity"]["dir"] == "/home/ec2-user"

    def test_shell_must_be_an_absolute_path(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity\.shell must be an absolute"):
            _validate({"identity": {"shell": "bash"}})

    def test_gecos_must_be_a_string_when_set(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.identity\.gecos must be a string"):
            _validate({"identity": {"gecos": 5}})
        assert _validate({"identity": {"gecos": "HPC"}})["identity"]["gecos"] == "HPC"


# ---------------------------------------------------------------------------
# Managed block
# ---------------------------------------------------------------------------


class TestManaged:
    def test_managed_must_be_an_object(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.managed must be an object"):
            _validate({"managed": True})

    def test_unknown_keys_fail_by_name_at_every_level(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.managed contains unknown key"):
            _validate(_managed(partitions={}))
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.managed\.accounting contains unknown"):
            _validate(_managed(accounting={"backup": True}))
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.managed\.login must be an object"):
            _validate(_managed(login=True))

    def test_a_null_sub_block_keeps_its_defaults(self) -> None:
        config = _validate(_managed(accounting=None, login=None, storage=None))
        assert config["managed"]["accounting"] == {"enabled": True, "purge_days": 30}
        assert config["managed"]["login"]["instance_type"] == "t3.medium"
        assert config["managed"]["storage"] == {"home": "efs", "scratch": "auto"}

    def test_cluster_name_follows_the_pcs_pattern(self) -> None:
        assert _validate(_managed(cluster_name="my-hpc"))["managed"]["cluster_name"] == "my-hpc"
        for bad in ("pcs_abc", "1abc", "ab", "a" * 41, "has_underscore"):
            with pytest.raises(pc.PcsConfigError, match=r"pcs\.managed\.cluster_name"):
                _validate(_managed(cluster_name=bad))

    def test_slurm_version_must_serve_the_api_plugin_version(self) -> None:
        with pytest.raises(
            pc.PcsConfigError, match=r"pcs\.managed\.slurm_version must be one of 25\.11, 26\.05"
        ):
            _validate(_managed(slurm_version="25.05"))

    def test_size_must_be_a_pcs_size(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.managed\.size must be one of"):
            _validate(_managed(size="XL"))

    def test_accounting_purge_days_is_minus_one_or_one_to_ten_thousand(self) -> None:
        assert _validate(_managed(accounting={"purge_days": -1}))["managed"]["accounting"] == {
            "enabled": True,
            "purge_days": -1,
        }
        for bad in (0, -2, 10001, True, "30"):
            with pytest.raises(pc.PcsConfigError, match=r"accounting\.purge_days"):
                _validate(_managed(accounting={"purge_days": bad}))

    def test_accounting_enabled_must_be_a_boolean(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"accounting\.enabled must be a boolean"):
            _validate(_managed(accounting={"enabled": "yes"}))

    @pytest.mark.parametrize("value", [0, 10_000_001, "600"])
    def test_scale_down_idle_seconds_is_bounded(self, value: object) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.managed\.scale_down_idle_seconds"):
            _validate(_managed(scale_down_idle_seconds=value))

    def test_storage_choices(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"storage\.home must be one of efs, none"):
            _validate(_managed(storage={"home": "nfs"}))
        with pytest.raises(pc.PcsConfigError, match=r"storage\.scratch must be one of"):
            _validate(_managed(storage={"scratch": "ebs"}))

    def test_scratch_fsx_needs_fsx(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"fsx_lustre is not enabled"):
            _validate(_managed(storage={"scratch": "fsx"}))
        config = _validate(_managed(storage={"scratch": "fsx"}), fsx_enabled=True)
        assert config["managed"]["storage"]["scratch"] == "fsx"

    def test_login_fields(self) -> None:
        config = _validate(
            _managed(
                login={
                    "enabled": False,
                    "instance_type": "c7g.medium",
                    "ami": "ami-0123456789abcdef0",
                    "availability_zones": ["us-east-1a"],
                }
            )
        )
        assert config["managed"]["login"] == {
            "enabled": False,
            "instance_type": "c7g.medium",
            "ami": "ami-0123456789abcdef0",
            "availability_zones": ["us-east-1a"],
            "_architecture": "arm64",
        }
        with pytest.raises(pc.PcsConfigError, match=r"login\.enabled must be a boolean"):
            _validate(_managed(login={"enabled": 1}))
        with pytest.raises(pc.PcsConfigError, match=r"login\.instance_type has an invalid"):
            _validate(_managed(login={"instance_type": "medium"}))
        with pytest.raises(pc.PcsConfigError, match=r"login\.ami must be"):
            _validate(_managed(login={"ami": "ubuntu"}))
        with pytest.raises(pc.PcsConfigError, match=r"login\.availability_zones has an invalid"):
            _validate(_managed(login={"availability_zones": ["us-east-1"]}))

    def test_metrics_and_lifecycle_logs_are_booleans(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"metrics\.enabled must be a boolean"):
            _validate(_managed(metrics={"enabled": "on"}))
        with pytest.raises(pc.PcsConfigError, match=r"lifecycle_logs\.enabled must be a boolean"):
            _validate(_managed(lifecycle_logs={"enabled": None}))

    def test_cluster_custom_settings_are_normalized(self) -> None:
        config = _validate(
            _managed(
                slurm_custom_settings={
                    "MaxJobCount": 20000,
                    "SchedulerParameters": " bf_continue ",
                },
                slurmdbd_custom_settings={"PurgeJobAfter": "90days"},
                cgroup_custom_settings={"ConstrainCores": "yes"},
            )
        )
        managed = config["managed"]
        assert managed["slurm_custom_settings"] == {
            "MaxJobCount": "20000",
            "SchedulerParameters": "bf_continue",
        }
        assert managed["slurmdbd_custom_settings"] == {"PurgeJobAfter": "90days"}
        assert managed["cgroup_custom_settings"] == {"ConstrainCores": "yes"}

    def test_metrics_add_the_pcs_documented_slurm_settings(self) -> None:
        # Off: the operator's settings, untouched.
        plain = _validate(_managed(slurm_custom_settings={"MaxJobCount": 20000}))
        assert pc.managed_slurm_custom_settings(plain) == {"MaxJobCount": "20000"}
        assert not pc.metrics_enabled(plain)
        # On: MetricsType and the HTTP listener, merged into existing
        # CommunicationParameters without duplicating enable_http.
        config = _validate(
            _managed(
                metrics={"enabled": True},
                slurm_custom_settings={
                    "MaxJobCount": 20000,
                    "CommunicationParameters": "block_null_hash, enable_http",
                },
            )
        )
        assert pc.metrics_enabled(config)
        assert pc.managed_slurm_custom_settings(config) == {
            "MaxJobCount": "20000",
            "CommunicationParameters": "block_null_hash,enable_http",
            "MetricsType": "metrics/openmetrics",
        }
        bare = _validate(_managed(metrics={"enabled": True}))
        assert pc.managed_slurm_custom_settings(bare) == {
            "MetricsType": "metrics/openmetrics",
            "CommunicationParameters": "enable_http",
        }
        # The operator may spell the plugin out, but no other one exists.
        spelled = _validate(
            _managed(
                metrics={"enabled": True},
                slurm_custom_settings={"MetricsType": "metrics/openmetrics"},
            )
        )
        assert pc.managed_slurm_custom_settings(spelled)["MetricsType"] == "metrics/openmetrics"
        with pytest.raises(pc.PcsConfigError, match=r"MetricsType is 'metrics/other' but"):
            _validate(
                _managed(
                    metrics={"enabled": True},
                    slurm_custom_settings={"MetricsType": "metrics/other"},
                )
            )
        with pytest.raises(pc.PcsConfigError, match=r"PrivateData cannot be set with metrics"):
            _validate(
                _managed(metrics={"enabled": True}, slurm_custom_settings={"PrivateData": "jobs"})
            )
        # Without metrics, PrivateData and MetricsType are the operator's business.
        kept = _validate(
            _managed(slurm_custom_settings={"PrivateData": "jobs", "MetricsType": "metrics/other"})
        )
        assert kept["managed"]["slurm_custom_settings"]["PrivateData"] == "jobs"
        assert (pc.PCS_CONTROLLER_PORT, pc.PCS_RESTAPI_PORT) == (6817, 6820)

    def test_slurmdbd_settings_need_accounting(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"slurmdbd_custom_settings needs accounting"):
            _validate(
                _managed(
                    accounting={"enabled": False},
                    slurmdbd_custom_settings={"PurgeJobAfter": "90days"},
                )
            )
        # Accounting off with no slurmdbd settings is fine.
        assert _validate(_managed(accounting={"enabled": False}))["managed"]["accounting"] == {
            "enabled": False,
            "purge_days": 30,
        }

    def test_custom_settings_validation(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"slurm_custom_settings must be an object"):
            _validate(_managed(slurm_custom_settings=["a=b"]))
        with pytest.raises(pc.PcsConfigError, match=r"key 'bad key' is not a Slurm parameter"):
            _validate(_managed(slurm_custom_settings={"bad key": "x"}))
        with pytest.raises(pc.PcsConfigError, match=r"MaxTime must be a string or a number"):
            _validate(_managed(slurm_custom_settings={"MaxTime": True}))
        with pytest.raises(pc.PcsConfigError, match=r"non-empty single-line value"):
            _validate(_managed(slurm_custom_settings={"MaxTime": "a\nb"}))
        with pytest.raises(pc.PcsConfigError, match=r"non-empty single-line value"):
            _validate(_managed(slurm_custom_settings={"MaxTime": "  "}))

    def test_removal_policy_choices(self) -> None:
        assert _validate(_managed(removal_policy="retain"))["managed"]["removal_policy"] == "retain"
        with pytest.raises(pc.PcsConfigError, match=r"removal_policy must be one of"):
            _validate(_managed(removal_policy="keep"))

    def test_managed_block_is_validated_even_in_existing_mode(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"pcs\.managed\.size"):
            _validate(_on(mode="existing", cluster_id="pcs_abc", managed={"size": "XL"}))


# ---------------------------------------------------------------------------
# Node groups
# ---------------------------------------------------------------------------


class TestNodeGroups:
    def test_must_be_a_non_empty_list(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"node_groups must be a list"):
            _validate(_managed(node_groups={"a": {}}))
        with pytest.raises(pc.PcsConfigError, match=r"must declare at least one compute node"):
            _validate(_managed(node_groups=[]))

    def test_entries_must_be_objects_with_a_valid_name(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"node_groups\[0\] must be an object"):
            _validate(_managed(node_groups=["cpu"]))
        with pytest.raises(pc.PcsConfigError, match=r"node_groups\[0\]\.name must be a non-empty"):
            _validate(_managed(node_groups=[{"instance_types": ["c6i.large"]}]))
        for bad in ("pcs_x", "ab", "9abc", "a" * 26, "has_underscore"):
            with pytest.raises(
                pc.PcsConfigError, match=r"node_groups\[0\]\.name .* is not a PCS compute node"
            ):
                _validate(_managed(**_groups_and_queues(_group(name=bad))))

    def test_unknown_keys_fail_by_name(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"node_groups\[0\] contains unknown key"):
            _validate(_managed(**_groups_and_queues(_group(cpu=4))))

    def test_names_are_unique_and_login_is_reserved(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"names a node group twice"):
            _validate(
                _managed(
                    node_groups=[_group(), _group()],
                    queues=[_queue()],
                )
            )
        with pytest.raises(pc.PcsConfigError, match=r"must not name a group 'login'"):
            _validate(
                _managed(
                    node_groups=[_group(name="login")],
                    queues=[_queue(node_groups=["login"])],
                )
            )

    def test_instance_types_are_validated(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"instance_types must be a list of non-empty"):
            _validate(_managed(**_groups_and_queues(_group(instance_types="c6i.large"))))
        with pytest.raises(pc.PcsConfigError, match=r"must name at least one EC2 instance type"):
            _validate(_managed(**_groups_and_queues(_group(instance_types=[]))))
        with pytest.raises(pc.PcsConfigError, match=r"invalid EC2 instance type 'large'"):
            _validate(_managed(**_groups_and_queues(_group(instance_types=["large"]))))
        with pytest.raises(pc.PcsConfigError, match=r"lists an instance type twice"):
            _validate(
                _managed(**_groups_and_queues(_group(instance_types=["c6i.large", "c6i.large"])))
            )

    def test_instance_types_must_share_an_architecture(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"mixes architectures \(arm64, x86_64\)"):
            _validate(
                _managed(**_groups_and_queues(_group(instance_types=["c6i.large", "c7g.large"])))
            )
        config = _validate(
            _managed(**_groups_and_queues(_group(instance_types=["c7g.large", "c7gn.xlarge"])))
        )
        assert config["managed"]["node_groups"][0]["_architecture"] == "arm64"

    @pytest.mark.parametrize(
        ("instance_type", "architecture"),
        [
            ("c6i.large", "x86_64"),
            ("p5.48xlarge", "x86_64"),
            ("p6-b200.48xlarge", "x86_64"),
            ("g6e.xlarge", "x86_64"),
            ("hpc7a.96xlarge", "x86_64"),
            ("u7i-12tb.224xlarge", "x86_64"),
            ("m5zn.large", "x86_64"),
            ("c7g.large", "arm64"),
            ("c7gn.16xlarge", "arm64"),
            ("m6gd.large", "arm64"),
            ("hpc7g.16xlarge", "arm64"),
            ("g5g.xlarge", "arm64"),
            ("im4gn.large", "arm64"),
            ("a1.large", "arm64"),
        ],
    )
    def test_instance_architecture(self, instance_type: str, architecture: str) -> None:
        assert pc.instance_architecture(instance_type) == architecture

    def test_scaling_bounds(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.min_nodes must be an integer"):
            _validate(_managed(**_groups_and_queues(_group(min_nodes=-1))))
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.max_nodes must be an integer"):
            _validate(_managed(**_groups_and_queues(_group(max_nodes=0))))
        with pytest.raises(pc.PcsConfigError, match=r"min_nodes must not exceed max_nodes"):
            _validate(_managed(**_groups_and_queues(_group(min_nodes=5, max_nodes=4))))

    def test_purchase_option_defaults_to_on_demand_and_must_be_known(self) -> None:
        config = _validate(_managed(**_groups_and_queues(_group(purchase_option="SPOT"))))
        assert config["managed"]["node_groups"][0]["purchase_option"] == "SPOT"
        with pytest.raises(pc.PcsConfigError, match=r"purchase_option must be one of"):
            _validate(_managed(**_groups_and_queues(_group(purchase_option="RESERVED"))))

    def test_reservation_types_imply_their_purchase_option(self) -> None:
        for kind, option in pc.CAPACITY_RESERVATION_TYPES.items():
            config = _validate(
                _managed(
                    **_groups_and_queues(
                        _group(
                            availability_zones=["us-east-1a"],
                            capacity_reservation={"type": kind, "id": "cr-0123456789abcdef0"},
                        )
                    )
                )
            )
            group = config["managed"]["node_groups"][0]
            assert group["purchase_option"] == option
            assert group["capacity_reservation"] == {"type": kind, "id": "cr-0123456789abcdef0"}

    def test_explicit_purchase_option_must_agree_with_the_reservation(self) -> None:
        with pytest.raises(
            pc.PcsConfigError,
            match=r"purchase_option is 'ONDEMAND' but capacity_reservation\.type "
            r"'capacity_block' requires 'CAPACITY_BLOCK'",
        ):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(
                            purchase_option="ONDEMAND",
                            availability_zones=["us-east-1a"],
                            capacity_reservation={"type": "capacity_block", "id": "cr-0123456789"},
                        )
                    )
                )
            )
        config = _validate(
            _managed(
                **_groups_and_queues(
                    _group(
                        purchase_option="ONDEMAND",
                        availability_zones=["us-east-1a"],
                        capacity_reservation={"type": "odcr", "id": "cr-0123456789"},
                    )
                )
            )
        )
        assert config["managed"]["node_groups"][0]["purchase_option"] == "ONDEMAND"

    @pytest.mark.parametrize("option", ["CAPACITY_BLOCK", "INTERRUPTIBLE_CAPACITY_RESERVATION"])
    def test_reservation_backed_purchase_options_need_a_reservation(self, option: str) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"needs capacity_reservation to name"):
            _validate(_managed(**_groups_and_queues(_group(purchase_option=option))))

    def test_capacity_reservation_shape(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"capacity_reservation must be an object"):
            _validate(_managed(**_groups_and_queues(_group(capacity_reservation="cr-1"))))
        with pytest.raises(pc.PcsConfigError, match=r"capacity_reservation contains unknown key"):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(
                            capacity_reservation={"type": "odcr", "id": "cr-0123456789", "az": 1}
                        )
                    )
                )
            )
        with pytest.raises(pc.PcsConfigError, match=r"capacity_reservation\.type must be one of"):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(capacity_reservation={"type": "block", "id": "cr-0123456789"})
                    )
                )
            )
        with pytest.raises(pc.PcsConfigError, match=r"capacity_reservation\.id must be a capacity"):
            _validate(
                _managed(
                    **_groups_and_queues(_group(capacity_reservation={"type": "odcr", "id": "abc"}))
                )
            )

    def test_a_capacity_block_pins_one_instance_type(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"exactly the Capacity Block's instance type"):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(
                            instance_types=["p5.48xlarge", "p5e.48xlarge"],
                            capacity_reservation={"type": "capacity_block", "id": "cr-0123456789"},
                        )
                    )
                )
            )

    def test_spot_allocation_strategy(self) -> None:
        config = _validate(
            _managed(
                **_groups_and_queues(
                    _group(purchase_option="SPOT", spot_allocation_strategy="lowest-price")
                )
            )
        )
        assert config["managed"]["node_groups"][0]["spot_allocation_strategy"] == "lowest-price"
        with pytest.raises(pc.PcsConfigError, match=r"spot_allocation_strategy must be one of"):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(purchase_option="SPOT", spot_allocation_strategy="cheapest")
                    )
                )
            )
        with pytest.raises(pc.PcsConfigError, match=r"applies to SPOT node groups only"):
            _validate(
                _managed(**_groups_and_queues(_group(spot_allocation_strategy="lowest-price")))
            )

    def test_availability_zones(self) -> None:
        config = _validate(
            _managed(**_groups_and_queues(_group(availability_zones=["us-east-1a", "us-east-1b"])))
        )
        assert config["managed"]["node_groups"][0]["availability_zones"] == [
            "us-east-1a",
            "us-east-1b",
        ]
        with pytest.raises(pc.PcsConfigError, match=r"invalid availability zone 'us-east-1'"):
            _validate(_managed(**_groups_and_queues(_group(availability_zones=["us-east-1"]))))
        with pytest.raises(pc.PcsConfigError, match=r"lists an availability zone twice"):
            _validate(
                _managed(
                    **_groups_and_queues(_group(availability_zones=["us-east-1a", "us-east-1a"]))
                )
            )

    def test_gov_and_iso_zones_are_accepted(self) -> None:
        for zone in ("us-gov-west-1a", "us-iso-east-1b", "us-isob-east-1a", "cn-north-1a"):
            config = _validate(_managed(**_groups_and_queues(_group(availability_zones=[zone]))))
            assert config["managed"]["node_groups"][0]["availability_zones"] == [zone]

    def test_ami_choices(self) -> None:
        for ami in ("sample", "dlami", "ami-0123456789abcdef0", "ami-12345678"):
            config = _validate(_managed(**_groups_and_queues(_group(ami=ami))))
            assert config["managed"]["node_groups"][0]["ami"] == ami
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.ami must be 'sample', 'dlami'"):
            _validate(_managed(**_groups_and_queues(_group(ami="ubuntu"))))
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.ami must be"):
            _validate(_managed(**_groups_and_queues(_group(ami="ami-XYZ"))))

    def test_efa_and_placement_group_are_booleans(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.efa must be a boolean"):
            _validate(_managed(**_groups_and_queues(_group(efa="yes"))))
        with pytest.raises(pc.PcsConfigError, match=r"placement_group must be a boolean"):
            _validate(_managed(**_groups_and_queues(_group(placement_group=1))))

    def test_node_group_custom_settings_and_gres_records(self) -> None:
        config = _validate(
            _managed(
                **_groups_and_queues(
                    _group(
                        instance_types=["p5.48xlarge"],
                        slurm_custom_settings={"Gres": "gpu:h100:8", "Weight": 10},
                        gres_custom_settings=[
                            {"Name": "gpu", "Type": "h100", "File": "/dev/nvidia[0-7]", "Count": 8}
                        ],
                        scale_down_idle_seconds=120,
                    )
                )
            )
        )
        group = config["managed"]["node_groups"][0]
        assert group["slurm_custom_settings"] == {"Gres": "gpu:h100:8", "Weight": "10"}
        assert group["gres_custom_settings"] == [
            {"Name": "gpu", "Type": "h100", "File": "/dev/nvidia[0-7]", "Count": "8"}
        ]
        assert group["scale_down_idle_seconds"] == 120

    def test_gres_record_validation(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"gres_custom_settings must be a list"):
            _validate(_managed(**_groups_and_queues(_group(gres_custom_settings={"Name": "gpu"}))))
        with pytest.raises(pc.PcsConfigError, match=r"gres_custom_settings\[0\] must be an object"):
            _validate(_managed(**_groups_and_queues(_group(gres_custom_settings=["Name=gpu"]))))
        with pytest.raises(pc.PcsConfigError, match=r"gres_custom_settings\[0\] must not be empty"):
            _validate(_managed(**_groups_and_queues(_group(gres_custom_settings=[{}]))))
        with pytest.raises(pc.PcsConfigError, match=r"unknown gres\.conf attribute 'Device'"):
            _validate(
                _managed(**_groups_and_queues(_group(gres_custom_settings=[{"Device": "x"}])))
            )
        with pytest.raises(pc.PcsConfigError, match=r"\[0\]\.Count must be a string or an integer"):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(gres_custom_settings=[{"Name": "gpu", "Count": True}])
                    )
                )
            )
        with pytest.raises(pc.PcsConfigError, match=r"must declare Name or AutoDetect"):
            _validate(_managed(**_groups_and_queues(_group(gres_custom_settings=[{"Count": 8}]))))
        config = _validate(
            _managed(**_groups_and_queues(_group(gres_custom_settings=[{"AutoDetect": "nvml"}])))
        )
        assert config["managed"]["node_groups"][0]["gres_custom_settings"] == [
            {"AutoDetect": "nvml"}
        ]

    def test_scale_down_idle_seconds_override_is_bounded(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.scale_down_idle_seconds"):
            _validate(_managed(**_groups_and_queues(_group(scale_down_idle_seconds=0))))

    def test_a_reservation_needs_exactly_one_availability_zone(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"capacity_reservation needs exactly one"):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(capacity_reservation={"type": "odcr", "id": "cr-0123456789"})
                    )
                )
            )
        with pytest.raises(pc.PcsConfigError, match=r"capacity_reservation needs exactly one"):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(
                            availability_zones=["us-east-1a", "us-east-1b"],
                            capacity_reservation={"type": "odcr", "id": "cr-0123456789"},
                        )
                    )
                )
            )

    def test_a_placement_group_needs_exactly_one_availability_zone(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"placement_group needs exactly one"):
            _validate(_managed(**_groups_and_queues(_group(placement_group=True))))
        config = _validate(
            _managed(
                **_groups_and_queues(
                    _group(placement_group=True, availability_zones=["us-east-1a"])
                )
            )
        )
        assert config["managed"]["node_groups"][0]["placement_group"] is True

    def test_efa_needs_exactly_one_availability_zone(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.efa needs exactly one"):
            _validate(_managed(**_groups_and_queues(_group(efa=True))))
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.efa needs exactly one"):
            _validate(
                _managed(
                    **_groups_and_queues(
                        _group(efa=True, availability_zones=["us-east-1a", "us-east-1b"])
                    )
                )
            )

    def test_efa_interfaces(self) -> None:
        config = _validate(
            _managed(
                **_groups_and_queues(
                    _group(
                        instance_types=["p5.48xlarge"],
                        efa=True,
                        efa_interfaces=32,
                        availability_zones=["us-east-1a"],
                    )
                )
            )
        )
        group = config["managed"]["node_groups"][0]
        assert (group["efa"], group["efa_interfaces"]) == (True, 32)
        assert _validate(None)["managed"]["node_groups"][0]["efa_interfaces"] == 1
        with pytest.raises(pc.PcsConfigError, match=r"gco-compute\.efa_interfaces must be an"):
            _validate(_managed(**_groups_and_queues(_group(efa_interfaces=0))))


# ---------------------------------------------------------------------------
# Queues
# ---------------------------------------------------------------------------


class TestQueues:
    def test_must_be_a_non_empty_list(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"queues must be a list"):
            _validate(_managed(queues={"gco": {}}))
        with pytest.raises(pc.PcsConfigError, match=r"queues must declare at least one queue"):
            _validate(_managed(queues=[]))

    def test_entries_must_be_objects_with_a_valid_name(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"queues\[0\] must be an object"):
            _validate(_managed(queues=["gco"]))
        with pytest.raises(pc.PcsConfigError, match=r"queues\[0\]\.name must be a non-empty"):
            _validate(_managed(queues=[{"node_groups": ["gco-compute"]}]))
        with pytest.raises(
            pc.PcsConfigError, match=r"queues\[0\]\.name 'pcs_q' is not a PCS queue"
        ):
            _validate(_managed(queues=[_queue(name="pcs_q")]))

    def test_unknown_keys_fail_by_name(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"queues\[0\] contains unknown key"):
            _validate(_managed(queues=[_queue(priority=1)]))

    def test_node_groups_must_exist_and_be_listed_once(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"gco\.node_groups must name at least one"):
            _validate(_managed(queues=[_queue(node_groups=[])]))
        with pytest.raises(pc.PcsConfigError, match=r"names undeclared node group\(s\): gpu"):
            _validate(_managed(queues=[_queue(node_groups=["gco-compute", "gpu"])]))
        with pytest.raises(pc.PcsConfigError, match=r"gco\.node_groups lists a node group twice"):
            _validate(_managed(queues=[_queue(node_groups=["gco-compute", "gco-compute"])]))
        with pytest.raises(pc.PcsConfigError, match=r"gco\.node_groups must be a list"):
            _validate(_managed(queues=[_queue(node_groups="gco-compute")]))

    def test_exactly_one_default(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"exactly one queue default: true.*found none"):
            _validate(_managed(queues=[_queue(default=False)]))
        with pytest.raises(
            pc.PcsConfigError, match=r"exactly one queue default: true.*found \['gco', 'other'\]"
        ):
            _validate(_managed(queues=[_queue(), _queue(name="other")]))
        with pytest.raises(pc.PcsConfigError, match=r"gco\.default must be a boolean"):
            _validate(_managed(queues=[_queue(default="yes")]))

    def test_names_are_unique(self) -> None:
        with pytest.raises(pc.PcsConfigError, match=r"queues names a queue twice"):
            _validate(_managed(queues=[_queue(), _queue(default=False)]))

    def test_queue_custom_settings(self) -> None:
        config = _validate(
            _managed(queues=[_queue(slurm_custom_settings={"MaxTime": "2-00:00:00"})])
        )
        assert config["managed"]["queues"][0]["slurm_custom_settings"] == {"MaxTime": "2-00:00:00"}
        with pytest.raises(pc.PcsConfigError, match=r"slurm_custom_settings\.Default is managed"):
            _validate(_managed(queues=[_queue(slurm_custom_settings={"Default": "YES"})]))

    def test_multiple_groups_and_queues(self) -> None:
        config = _validate(
            _managed(
                node_groups=[
                    _group(),
                    _group(
                        name="gpu",
                        instance_types=["g6e.xlarge"],
                        ami="dlami",
                        efa=True,
                        availability_zones=["us-east-1a"],
                    ),
                ],
                queues=[_queue(), _queue(name="gpu", node_groups=["gpu"], default=False)],
            )
        )
        assert pc.node_group_names(config) == ["gco-compute", "gpu"]
        assert pc.default_queue(config) == "gco"


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------


class TestDerivedViews:
    def test_enabled_and_mode_predicates(self) -> None:
        assert pc.pcs_enabled(_validate(None)) is False
        assert pc.pcs_enabled(_validate(_on())) is True
        assert pc.is_managed(_validate(None)) is True
        assert pc.is_managed(_validate(_on(mode="existing", cluster_id="pcs_abc"))) is False

    def test_managed_cluster_name_defaults_to_project_dash_pcs(self) -> None:
        assert pc.managed_cluster_name(_validate(None), "gco") == "gco-pcs"
        assert pc.managed_cluster_name(_validate(_managed(cluster_name="hpc")), "gco") == "hpc"
        with pytest.raises(pc.PcsConfigError, match=r"derived PCS cluster name"):
            pc.managed_cluster_name(_validate(None), "x" * 40)

    def test_scratch_backend_resolution(self) -> None:
        assert pc.scratch_backend(_validate(None), fsx_enabled=False) == "none"
        assert pc.scratch_backend(_validate(None), fsx_enabled=True) == "fsx"
        config = _validate(_managed(storage={"scratch": "none"}))
        assert pc.scratch_backend(config, fsx_enabled=True) == "none"
        config = _validate(_managed(storage={"scratch": "fsx"}), fsx_enabled=True)
        assert pc.scratch_backend(config, fsx_enabled=True) == "fsx"

    def test_cluster_arn(self) -> None:
        assert (
            pc.pcs_cluster_arn(
                partition="aws", region="us-east-1", account="123456789012", cluster_id="pcs_abc"
            )
            == "arn:aws:pcs:us-east-1:123456789012:cluster/pcs_abc"
        )

    @pytest.mark.parametrize(
        ("slinky", "pcs", "expected"),
        [
            (False, False, []),
            (True, False, ["slinky"]),
            (False, True, ["pcs"]),
            (True, True, ["slinky", "pcs"]),
        ],
    )
    def test_enabled_slurm_backends(self, slinky: bool, pcs: bool, expected: list[str]) -> None:
        assert pc.enabled_slurm_backends(slinky=slinky, pcs=pcs) == expected

    def test_identity_claims_match_what_pcs_requires(self) -> None:
        claims = pc.identity_claims(_validate(None)["identity"])
        assert claims == {
            "uid": 1000,
            "gid": 1000,
            "id": {
                "name": "ec2-user",
                "gecos": "ec2-user",
                "dir": "/home/ec2-user",
                "shell": "/bin/bash",
                "gids": [1000],
            },
        }

    def test_identity_claims_carry_supplementary_groups_once(self) -> None:
        identity = _validate(
            {
                "identity": {
                    "name": "hpc",
                    "uid": 2000,
                    "gid": 2000,
                    "gids": [2000, 3000],
                    "gecos": "HPC",
                }
            }
        )["identity"]
        claims = pc.identity_claims(identity)
        assert claims["id"]["gids"] == [2000, 3000]
        assert claims["id"]["gecos"] == "HPC"
        assert claims["id"]["dir"] == "/home/hpc"


# ---------------------------------------------------------------------------
# kubectl-applier replacements
# ---------------------------------------------------------------------------


class TestReplacements:
    def _replacements(self, config: dict[str, Any], **kwargs: Any) -> dict[str, str]:
        kwargs.setdefault("enabled", True)
        kwargs.setdefault("slinky_enabled", False)
        kwargs.setdefault("cluster_id", "pcs_abc123")
        kwargs.setdefault("vpc_cidr", "10.0.0.0/16")
        return pc.compute_pcs_replacements(config, **kwargs)

    def test_disabled_emits_only_the_always_present_tokens_as_empty(self) -> None:
        replacements = self._replacements(_validate(None), enabled=False)
        assert replacements == {
            "{{SLURM_BACKENDS}}": "",
            "{{PCS_CLUSTER_ID}}": "",
            "{{PCS_IDENTITY_JSON}}": "",
            "{{PCS_DEFAULT_PARTITION}}": "",
        }
        assert set(replacements) == set(pc.PCS_ALWAYS_EMITTED_TOKENS)

    def test_disabled_pcs_still_names_slinky_in_the_backend_list(self) -> None:
        replacements = self._replacements(_validate(None), enabled=False, slinky_enabled=True)
        assert replacements["{{SLURM_BACKENDS}}"] == "slinky"

    def test_enabled_managed_emits_every_token_but_the_metrics_gate(self) -> None:
        config = _validate(_on())
        replacements = self._replacements(config, slinky_enabled=True)
        assert set(replacements) == set(pc.PCS_MANIFEST_TOKENS) - {
            "{{PCS_METRICS_ENABLED}}",
            "{{PCS_CONTROLLER_ADDRESS}}",
        }
        assert replacements["{{SLURM_BACKENDS}}"] == "slinky,pcs"
        assert replacements["{{PCS_ENABLED}}"] == "true"
        assert replacements["{{PCS_RESTAPI_CIDR}}"] == "10.0.0.0/16"
        assert replacements["{{PCS_CLUSTER_ID}}"] == "pcs_abc123"
        assert replacements["{{PCS_DEFAULT_PARTITION}}"] == "gco"
        assert json.loads(replacements["{{PCS_IDENTITY_JSON}}"]) == {
            "name": "ec2-user",
            "uid": 1000,
            "gid": 1000,
            "gids": [],
            "dir": "/home/ec2-user",
            "shell": "/bin/bash",
            "gecos": None,
        }

    def test_pcs_only_backend_list(self) -> None:
        assert self._replacements(_validate(_on()))["{{SLURM_BACKENDS}}"] == "pcs"

    def test_metrics_on_emits_the_observability_gate_with_the_controller_address(self) -> None:
        config = _validate(_managed(metrics={"enabled": True}))
        replacements = self._replacements(config, controller_address="10.0.1.5:6817")
        assert set(replacements) == set(pc.PCS_MANIFEST_TOKENS)
        assert replacements["{{PCS_METRICS_ENABLED}}"] == "true"
        assert replacements["{{PCS_CONTROLLER_ADDRESS}}"] == "10.0.1.5:6817"
        # A deploy-time token passes through untouched, like the cluster id.
        token = "${Token[TOKEN.456]}:6817"
        assert (
            self._replacements(config, controller_address=token)["{{PCS_CONTROLLER_ADDRESS}}"]
            == token
        )

    def test_metrics_on_without_a_controller_address_is_a_programming_error(self) -> None:
        config = _validate(_managed(metrics={"enabled": True}))
        with pytest.raises(pc.PcsConfigError, match=r"needs the controller address"):
            self._replacements(config)

    def test_existing_mode_never_emits_the_metrics_gate(self) -> None:
        # ``metrics`` lives under ``managed``: an operator-run cluster is scraped
        # by its operator, so the gate stays closed even with the key on.
        config = _validate(
            _on(mode="existing", cluster_id="pcs_mine", managed={"metrics": {"enabled": True}})
        )
        replacements = self._replacements(
            config, cluster_id="pcs_mine", controller_address="10.0.1.5:6817"
        )
        assert "{{PCS_METRICS_ENABLED}}" not in replacements
        assert not pc.metrics_enabled(config)

    def test_existing_mode_has_no_default_partition_and_passes_the_cluster_id_through(
        self,
    ) -> None:
        config = _validate(_on(mode="existing", cluster_id="pcs_mine"))
        replacements = self._replacements(config, cluster_id="pcs_mine")
        assert replacements["{{PCS_DEFAULT_PARTITION}}"] == ""
        assert replacements["{{PCS_CLUSTER_ID}}"] == "pcs_mine"

    def test_endpoint_cidr_overrides_the_vpc_cidr(self) -> None:
        config = _validate(_on(endpoint_cidr="192.168.0.0/20"))
        assert self._replacements(config)["{{PCS_RESTAPI_CIDR}}"] == "192.168.0.0/20"

    def test_the_identity_json_is_escaped_for_a_single_quoted_yaml_scalar(self) -> None:
        config = _validate(_on(identity={"gecos": "O'Brien"}))
        rendered = self._replacements(config)["{{PCS_IDENTITY_JSON}}"]
        assert "''" in rendered
        assert yaml.safe_load(f"value: '{rendered}'") == {
            "value": json.dumps(
                {
                    "dir": "/home/ec2-user",
                    "gecos": "O'Brien",
                    "gid": 1000,
                    "gids": [],
                    "name": "ec2-user",
                    "shell": "/bin/bash",
                    "uid": 1000,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        }

    def test_a_cloudformation_token_cluster_id_is_passed_through_untouched(self) -> None:
        token = "${Token[TOKEN.123]}"
        assert self._replacements(_validate(_on()), cluster_id=token)["{{PCS_CLUSTER_ID}}"] == token

    def test_every_token_is_upper_snake(self) -> None:
        for token in pc.PCS_MANIFEST_TOKENS:
            assert re.fullmatch(r"\{\{[A-Z0-9_]+\}\}", token)
        assert set(pc.PCS_MANIFEST_TOKENS) >= pc.PCS_ALWAYS_EMITTED_TOKENS


# ---------------------------------------------------------------------------
# Config loader wiring
# ---------------------------------------------------------------------------


def _loader(valid_cdk_context: dict[str, Any], **context: Any) -> ConfigLoader:
    merged = copy.deepcopy(valid_cdk_context)
    merged.update(context)
    return ConfigLoader(cdk.App(context=merged))


class TestConfigLoaderWiring:
    def test_absent_block_reads_as_off(self, valid_cdk_context: dict[str, Any]) -> None:
        config = _loader(valid_cdk_context).get_pcs_config()
        assert config["enabled"] is False
        assert config == _validate(None)

    def test_valid_block_is_returned_with_defaults(self, valid_cdk_context: dict[str, Any]) -> None:
        loader = _loader(valid_cdk_context, pcs={"enabled": True, "managed": {"size": "MEDIUM"}})
        config = loader.get_pcs_config("us-east-1")
        assert config["enabled"] is True
        assert config["managed"]["size"] == "MEDIUM"
        assert config["managed"]["accounting"]["enabled"] is True

    def test_malformed_block_fails_at_construction(self, valid_cdk_context: dict[str, Any]) -> None:
        with pytest.raises(ConfigValidationError, match=r"pcs\.managed\.slurm_version"):
            _loader(valid_cdk_context, pcs={"managed": {"slurm_version": "25.05"}})

    def test_region_overlay_patches_the_global_block(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(
            valid_cdk_context,
            pcs={"managed": {"size": "MEDIUM", "accounting": {"purge_days": 7}}},
            pcs_regions={
                "us-east-1": {"enabled": True, "managed": {"accounting": {"enabled": False}}}
            },
        )
        assert loader.get_pcs_config()["enabled"] is False
        regional = loader.get_pcs_config("us-east-1")
        assert regional["enabled"] is True
        assert regional["managed"]["size"] == "MEDIUM"
        # One level of the overlay replaces one key and keeps its siblings.
        assert regional["managed"]["accounting"] == {"enabled": False, "purge_days": 7}
        assert loader.get_pcs_config("eu-west-1") == loader.get_pcs_config()

    def test_region_overlay_without_a_global_block(self, valid_cdk_context: dict[str, Any]) -> None:
        loader = _loader(
            valid_cdk_context,
            pcs_regions={
                "us-east-1": {"enabled": True, "mode": "existing", "cluster_id": "pcs_abc"}
            },
        )
        assert loader.get_pcs_config("us-east-1")["cluster_id"] == "pcs_abc"
        assert loader.get_pcs_config()["enabled"] is False

    def test_region_overlay_errors_name_the_region(self, valid_cdk_context: dict[str, Any]) -> None:
        with pytest.raises(
            ConfigValidationError, match=r"pcs_regions\.us-east-1 must be an object"
        ):
            _loader(valid_cdk_context, pcs_regions={"us-east-1": True})
        with pytest.raises(ConfigValidationError, match=r"pcs_regions must be an object keyed"):
            _loader(valid_cdk_context, pcs_regions=["us-east-1"])
        with pytest.raises(ConfigValidationError, match=r"pcs\.cluster_id is required"):
            _loader(valid_cdk_context, pcs_regions={"us-east-1": {"mode": "existing"}})

    def test_feature_override_forces_enabled(self, valid_cdk_context: dict[str, Any]) -> None:
        loader = _loader(valid_cdk_context, feature_enabled_overrides="pcs")
        assert loader.get_pcs_config()["enabled"] is True
        assert loader.get_pcs_config("us-east-1")["enabled"] is True
        loader = _loader(valid_cdk_context, feature_enabled_overrides="fsx_lustre,pcs")
        assert loader.get_pcs_config()["enabled"] is True
        assert loader.get_fsx_lustre_config("us-east-1")["enabled"] is True

    def test_feature_override_does_not_change_the_other_knobs(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(
            valid_cdk_context,
            pcs={"mode": "existing", "cluster_id": "pcs_abc"},
            feature_enabled_overrides="pcs",
        )
        config = loader.get_pcs_config("us-east-1")
        assert (config["enabled"], config["mode"], config["cluster_id"]) == (
            True,
            "existing",
            "pcs_abc",
        )

    def test_scratch_fsx_needs_fsx_in_the_region(self, valid_cdk_context: dict[str, Any]) -> None:
        pcs = {"enabled": True, "managed": {"storage": {"scratch": "fsx"}}}
        with pytest.raises(ConfigValidationError, match=r"fsx_lustre is not enabled"):
            _loader(valid_cdk_context, pcs=pcs)
        loader = _loader(valid_cdk_context, pcs=pcs, fsx_lustre={"enabled": True})
        assert loader.get_pcs_config("us-east-1")["managed"]["storage"]["scratch"] == "fsx"
        assert loader._pcs_fsx_enabled("us-east-1") is True
        assert loader._pcs_fsx_enabled(None) is True

    def test_scratch_fsx_per_region_follows_the_regional_fsx_toggle(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(
            valid_cdk_context,
            pcs={"enabled": True},
            pcs_regions={"us-east-1": {"managed": {"storage": {"scratch": "fsx"}}}},
            fsx_lustre_regions={"us-east-1": {"enabled": True}},
        )
        assert loader.get_pcs_config("us-east-1")["managed"]["storage"]["scratch"] == "fsx"
        assert loader._pcs_fsx_enabled("eu-west-1") is False

    def test_fsx_lookup_is_skipped_unless_the_block_asks_for_it(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(valid_cdk_context, pcs={"enabled": True})
        assert loader._pcs_fsx_enabled(None) is False
        assert loader._pcs_fsx_enabled("us-east-1") is False

    def test_fsx_availability_everywhere_needs_at_least_one_region(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        """With no deployment regions nothing has FSx, so the strict setting is rejected."""
        context = copy.deepcopy(valid_cdk_context)
        context["deployment_regions"]["regional"] = []
        with pytest.raises(ConfigValidationError, match=r"fsx_lustre is not enabled"):
            _loader(
                context,
                pcs={"managed": {"storage": {"scratch": "fsx"}}},
                fsx_lustre={"enabled": True},
            )

    def test_getter_reports_errors_as_config_validation_errors(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(valid_cdk_context)
        original = loader._raw_pcs_config
        loader._raw_pcs_config = lambda region=None: {"enabled": "yes"}  # type: ignore[method-assign]
        try:
            with pytest.raises(ConfigValidationError, match=r"pcs\.enabled must be a boolean"):
                loader.get_pcs_config()
        finally:
            loader._raw_pcs_config = original  # type: ignore[method-assign]

    def test_pcs_is_a_feature_override_name(self) -> None:
        from gco.config.config_loader import FEATURE_OVERRIDE_KEYS, parse_feature_enabled_overrides
        from gco.enablement_overrides import route_enablement_overrides

        assert "pcs" in FEATURE_OVERRIDE_KEYS
        assert parse_feature_enabled_overrides("pcs") == frozenset({"pcs"})
        assert route_enablement_overrides(["pcs,slurm"]) == {
            "feature_enabled_overrides": "pcs",
            "helm_enabled_overrides": "slurm",
        }


class TestModuleHygiene:
    def test_module_imports_nothing_from_aws_cdk(self) -> None:
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(pc))
        imported = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "aws_cdk" not in imported
        assert "constructs" not in imported

    def test_name_patterns_match_the_pcs_api_reference(self) -> None:
        assert pc.PCS_NAME_PATTERN.match("compute-1")
        assert not pc.PCS_NAME_PATTERN.match("pcs_compute")
        assert pc.PCS_CLUSTER_NAME_PATTERN.match("a" * 40)
        assert not pc.PCS_CLUSTER_NAME_PATTERN.match("a" * 41)
        assert re.fullmatch(r"pcs_[a-zA-Z0-9]+", "pcs_abc123")
