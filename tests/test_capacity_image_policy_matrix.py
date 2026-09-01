"""Focused capacity-rendering, advisor-evidence, and image-safety tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli.capacity.advisor import BedrockCapacityAdvisor
from cli.capacity.models import InstanceTypeInfo
from cli.images import ImageManager


def _advisor() -> BedrockCapacityAdvisor:
    return object.__new__(BedrockCapacityAdvisor)


def _image_manager() -> ImageManager:
    config = SimpleNamespace(
        project_name="gco",
        global_region="us-east-2",
        default_region="us-east-2",
    )
    manager = ImageManager(config=config, region="us-east-2")
    manager._account_id_cache = "123456789012"
    return manager


class TestCapacityAdvisorEvidence:
    def test_prompt_renders_every_signal_gap_and_historical_interpretation(self) -> None:
        data = {
            "timestamp": "2026-08-31T00:00:00Z",
            "regions_analyzed": ["us-east-1", "us-west-2"],
            "instance_types_analyzed": ["p5.48xlarge"],
            "cluster_metrics": [
                {
                    "region": "us-east-1",
                    "queue_depth": 2,
                    "running_jobs": 3,
                    "gpu_utilization": 40.0,
                    "cpu_utilization": 20.0,
                }
            ],
            "spot_data": {
                "p5.48xlarge": {
                    "us-east-1": {
                        "placement_scores": {"regional": 8},
                        "prices": [
                            {"current": 4.0},
                            {"current": 6.0},
                        ],
                        "price_trends": {
                            "us-east-1a": {
                                "direction": "falling",
                                "normalized_slope": -0.25,
                                "price_changes": 3,
                            }
                        },
                    }
                }
            },
            "on_demand_data": {
                "p5.48xlarge": {
                    "us-east-1": {"price_per_hour": 10.0, "available": True},
                    "us-west-2": {"price_per_hour": None, "available": None},
                }
            },
            "reservations": {
                "p5.48xlarge": {
                    "us-east-1": [
                        {
                            "az": "us-east-1a",
                            "available": 2,
                            "total": 4,
                            "utilization_pct": 50,
                        }
                    ]
                }
            },
            "capacity_blocks": {
                "p5.48xlarge": {
                    "us-west-2": [
                        {
                            "az": "us-west-2b",
                            "duration_hours": 24,
                            "start_date": "2026-09-01",
                            "upfront_fee": 1200,
                        }
                    ]
                }
            },
            "capacity_block_trends": {
                "p5.48xlarge": {
                    "us-east-1": {"trend_score": 0.4, "interpretation": "capacity growing"}
                }
            },
            "weighted_recommendation": {
                "instance_type": "p5.48xlarge",
                "scoring_method": "weighted",
                "all_regions": [
                    {
                        "region": "us-east-1",
                        "score": 1.2,
                        "spot_placement_score": 0.8,
                        "spot_price_ratio": 0.5,
                        "capacity_block_trend": 0.0,
                        "queue_depth": 2,
                        "gpu_utilization": 40,
                    },
                    {"region": "us-west-2", "score": 4.5},
                ],
            },
            "data_gaps": [
                {
                    "source": "spot placement score",
                    "error": "Throttling",
                    "region": "us-west-2",
                    "instance_type": "p5.48xlarge",
                },
                {
                    "source": "spot placement score",
                    "error": "Throttling",
                    "region": "us-west-2",
                    "instance_type": "p4d.24xlarge",
                },
            ],
        }
        historical = {
            "low": {
                "instance_type": "p5.48xlarge",
                "region": "us-east-1",
                "current_spot_score": 1,
                "p25": 2,
                "p50": 5,
                "p75": 8,
                "best_windows": [{"day": "Mon", "hour": 3, "avg": 9}],
            },
            "normal": {
                "instance_type": "p5.48xlarge",
                "region": "us-west-2",
                "current_spot_score": 5,
                "p25": 2,
                "p50": 5,
                "p75": 8,
                "best_windows": [],
            },
            "high": {
                "instance_type": "p4d.24xlarge",
                "region": "eu-west-1",
                "current_spot_score": 9,
                "p25": 2,
                "p50": 5,
                "p75": 8,
                "best_windows": [],
            },
        }

        prompt = _advisor()._build_prompt(
            data,
            workload_description="Distributed training",
            requirements={
                "gpu_required": True,
                "min_gpus": 8,
                "min_memory_gb": 512,
                "fault_tolerance": "high",
                "max_cost_per_hour": 20,
            },
            historical_context=historical,
        )

        for fragment in (
            "WORKLOAD DESCRIPTION",
            "Minimum GPUs: 8",
            "7-day spot price trend by AZ",
            "ON-DEMAND PRICING",
            "unknown (lookup failed)",
            "CAPACITY RESERVATIONS",
            "CAPACITY BLOCK OFFERINGS",
            "CAPACITY BLOCK AVAILABILITY TRENDS",
            "spot availability 80%",
            "spot/on-demand price ratio 0.50",
            "block trend +0.00",
            "GPU util 40%",
            "DATA GAPS",
            "p4d.24xlarge, p5.48xlarge",
            "likely transient contention",
            "within normal range",
            "unusually favorable",
            "Mon 03:00 (avg 9)",
        ):
            assert fragment in prompt

    def test_historical_lookup_failure_does_not_discard_later_regions(self) -> None:
        store = MagicMock()

        def stats(_instance: str, region: str) -> dict[str, Any]:
            if region == "us-east-1":
                raise RuntimeError("one region failed")
            if region == "us-west-2":
                return {"metrics": {}}
            return {"metrics": {"spot_score": {"p25": 2, "p50": 5, "p75": 8}}}

        store.get_statistics.side_effect = stats
        store.get_temporal_patterns.side_effect = [
            RuntimeError("patterns unavailable"),
            {"best_windows": [{"day": "Tue", "hour": 4, "avg": 9}]},
        ]
        capacity_data = {
            "spot_data": {
                "p5.48xlarge": {
                    "skip-no-current": {"placement_scores": {}},
                    "us-east-1": {"placement_scores": {"regional": 3}},
                    "us-west-2": {"placement_scores": {"regional": 4}},
                    "eu-west-1": {"placement_scores": {"regional": 5}},
                    "ap-southeast-2": {"placement_scores": {"regional": 9}},
                }
            }
        }
        with patch("cli.capacity.history.get_capacity_history_store", return_value=store):
            result = _advisor()._gather_historical_context(capacity_data)

        assert set(result) == {
            "p5.48xlarge#eu-west-1",
            "p5.48xlarge#ap-southeast-2",
        }
        assert result["p5.48xlarge#eu-west-1"]["best_windows"] == []
        assert result["p5.48xlarge#ap-southeast-2"]["best_windows"][0]["day"] == "Tue"

    def test_history_store_unavailable_degrades_to_empty_context(self) -> None:
        with patch(
            "cli.capacity.history.get_capacity_history_store",
            side_effect=RuntimeError("not deployed"),
        ):
            assert _advisor()._gather_historical_context({}) == {}


class TestInstanceInfoRendering:
    @staticmethod
    def _info() -> InstanceTypeInfo:
        return InstanceTypeInfo(
            instance_type="p5.48xlarge",
            region="us-east-1",
            vcpus=192,
            cores=96,
            threads_per_core=2,
            memory_gib=2048,
            architecture="x86_64",
            architectures=["x86_64"],
            processor_manufacturer="Intel",
            sustained_clock_speed_ghz=3.2,
            gpu_count=8,
            gpu_memory_gib=640,
            gpu_devices=[{"count": 8, "manufacturer": "NVIDIA", "name": "H100", "memory_gib": 80}],
            neuron_count=2,
            neuron_memory_gib=64,
            neuron_devices=[{"count": 2, "name": "Neuron", "core_count": 4, "core_version": 2}],
            inference_accelerators=[{"name": "Inferentia"}],
            inference_accelerator_count=1,
            efa_supported=True,
            efa_max_interfaces=32,
            network_performance="3200 Gigabit",
            maximum_network_interfaces=15,
            maximum_network_cards=4,
            instance_storage_total_gb=30000,
            instance_storage_disks=[{"count": 8, "size_gb": 3750, "type": "nvme"}],
            ebs_optimized_support="default",
            ebs_maximum_iops=160000,
            ebs_maximum_throughput_mbps=10000,
            supported_usage_classes=["on-demand", "spot", "capacity-block"],
            supported_placement_strategies=["cluster", "partition"],
            current_generation=True,
        )

    def test_rich_table_prints_every_optional_hardware_section(self) -> None:
        from cli.main import cli

        checker = MagicMock()
        checker.get_instance_info.return_value = self._info()
        with patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker):
            result = CliRunner().invoke(
                cli,
                ["capacity", "instance-info", "p5.48xlarge", "--region", "us-east-1"],
            )

        assert result.exit_code == 0, result.output
        for fragment in (
            "96 cores x 2 threads",
            "Intel @ 3.2 GHz",
            "8x NVIDIA H100",
            "Neuron",
            "Inferentia",
            "max 32 interfaces",
            "Network cards:    4",
            "8x 3750 GB nvme",
            "160000 IOPS",
            "Capacity blocks:  yes",
            "Placement:        cluster, partition",
            "Current gen:      yes",
        ):
            assert fragment in result.output
        checker.get_instance_info.assert_called_once_with("p5.48xlarge", region="us-east-1")

    def test_structured_output_forwards_dataclass_without_table(self) -> None:
        from cli.main import cli

        info = self._info()
        checker = MagicMock()
        checker.get_instance_info.return_value = info
        formatter = MagicMock()
        with (
            patch("cli.commands.capacity_cmd.get_capacity_checker", return_value=checker),
            patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
        ):
            result = CliRunner().invoke(
                cli,
                ["--output", "json", "capacity", "instance-info", "p5.48xlarge"],
            )
        assert result.exit_code == 0, result.output
        formatter.print.assert_called_once_with(info)
        assert "NETWORK" not in result.output


class TestImageBuildSafety:
    def test_dockerfile_must_be_a_real_descendant_of_context(self, tmp_path: Path) -> None:
        manager = _image_manager()
        context = tmp_path / "app"
        sibling = tmp_path / "app-evil"
        context.mkdir()
        sibling.mkdir()
        (sibling / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        manager._runtime_or_error = MagicMock(return_value="docker")  # type: ignore[method-assign]

        with pytest.raises(ValueError, match="inside the build context"):
            manager.build(
                str(context),
                "service",
                tag="v1",
                dockerfile="../app-evil/Dockerfile",
            )
        manager._runtime_or_error.assert_not_called()

    def test_build_orders_validation_build_push_and_retain(self, tmp_path: Path) -> None:
        manager = _image_manager()
        context = tmp_path / "app"
        context.mkdir()
        dockerfile = context / "Containerfile"
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        manager._runtime_or_error = MagicMock(return_value="podman")  # type: ignore[method-assign]
        manager.init = MagicMock()  # type: ignore[method-assign]
        manager._check_tag_immutable_collision = MagicMock()  # type: ignore[method-assign]
        manager._ecr_login = MagicMock()  # type: ignore[method-assign]
        manager._registry_host = MagicMock(return_value="registry.example")  # type: ignore[method-assign]
        manager._image_size_bytes = MagicMock(return_value=123)  # type: ignore[method-assign]
        manager._apply_retain_tag = MagicMock()  # type: ignore[method-assign]
        runs = [
            MagicMock(stdout="", stderr=""),
            MagicMock(stdout="digest: sha256:" + "a" * 64, stderr=""),
        ]
        with patch("cli.images.subprocess.run", side_effect=runs) as run:
            result = manager.build(
                str(context),
                "service",
                tag="v1",
                dockerfile="Containerfile",
                build_args={"MODEL": "x", "MODE": "test"},
                platform="linux/arm64",
                retain=True,
                quiet=True,
            )

        uri = "registry.example/gco/service:v1"
        build_command = run.call_args_list[0].args[0]
        assert build_command[:7] == [
            "podman",
            "build",
            "-t",
            uri,
            "--platform",
            "linux/arm64",
            "-f",
        ]
        assert build_command[8:10] == ["--build-arg", "MODEL=x"]
        assert build_command[10:12] == ["--build-arg", "MODE=test"]
        assert run.call_args_list[0].kwargs == {
            "check": True,
            "cwd": str(context.resolve()),
            "capture_output": True,
            "text": True,
        }
        assert run.call_args_list[1] == call(
            ["podman", "push", uri],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(context.resolve()),
        )
        manager._apply_retain_tag.assert_called_once_with("service")
        assert result["digest"] == "sha256:" + "a" * 64
        assert result["size_bytes"] == 123
        assert result["retain"] is True

    def test_push_quiet_and_retain_apply_to_local_tag(self) -> None:
        manager = _image_manager()
        manager._runtime_or_error = MagicMock(return_value="docker")  # type: ignore[method-assign]
        manager.init = MagicMock()  # type: ignore[method-assign]
        manager._check_tag_immutable_collision = MagicMock()  # type: ignore[method-assign]
        manager._ecr_login = MagicMock()  # type: ignore[method-assign]
        manager._registry_host = MagicMock(return_value="registry.example")  # type: ignore[method-assign]
        manager._image_size_bytes = MagicMock(return_value=None)  # type: ignore[method-assign]
        manager._apply_retain_tag = MagicMock()  # type: ignore[method-assign]
        with patch(
            "cli.images.subprocess.run",
            side_effect=[
                MagicMock(stdout="", stderr=""),
                MagicMock(stdout="", stderr="digest: sha256:" + "b" * 64),
            ],
        ) as run:
            result = manager.push("service", "v2", "local/service:test", retain=True, quiet=True)
        uri = "registry.example/gco/service:v2"
        assert run.call_args_list[0] == call(
            ["docker", "tag", "local/service:test", uri],
            check=True,
            capture_output=True,
            text=True,
        )
        manager._apply_retain_tag.assert_called_once_with("service")
        assert result["digest"] == "sha256:" + "b" * 64


class TestImageCatalogAndReplication:
    def test_local_maintained_catalog_known_unknown_and_tag_override(self) -> None:
        manager = _image_manager()
        maintained = {
            "z-service": "dockerfiles/z",
            "a-service": "dockerfiles/a",
        }
        with patch("cli.images._MAINTAINED_IMAGES", maintained):
            manager.get_uri = MagicMock(  # type: ignore[method-assign]
                side_effect=lambda name, tag: f"registry/gco/{name}:{tag}"
            )
            rows = manager.list_maintained_images("release")
            assert [row["name"] for row in rows] == ["z-service", "a-service"]
            assert rows[0]["repository"] == "gco/z-service"
            assert rows[1]["dockerfile"] == "dockerfiles/a"
            known = manager.get_maintained_image("a-service", "v2")
            assert known["uri"] == "registry/gco/a-service:v2"
            with pytest.raises(ValueError, match="Known images: a-service, z-service"):
                manager.get_maintained_image("missing")
        assert manager.default_disaggregated_image_uri().endswith(":v0.28.0")
        assert manager.default_disaggregated_image_uri("custom") == ("vllm/vllm-openai:custom")

    @pytest.mark.parametrize(
        "configuration",
        ["not-a-mapping", {"rules": "not-a-list"}],
    )
    def test_replication_configuration_normalizes_malformed_shapes(
        self, configuration: Any
    ) -> None:
        manager = _image_manager()
        ecr = MagicMock()
        ecr.get_replication_configuration.return_value = {
            "registryId": "123",
            "replicationConfiguration": configuration,
        }
        registry, normalized = manager._current_replication_configuration(ecr)
        assert registry == "123"
        assert normalized["rules"] == []

    def test_replication_regions_filters_duplicates_and_falls_back(self) -> None:
        manager = _image_manager()
        with patch(
            "cli.images._load_cdk_json",
            return_value={"regional": ["us-west-2", "", 42, "us-west-2", "eu-west-1"]},
        ):
            assert manager._replication_regions() == ["us-west-2", "eu-west-1"]
        with patch("cli.images._load_cdk_json", return_value={"regional": "bad"}):
            assert manager._replication_regions() == ["us-east-2"]
        manager.config.default_region = None
        with patch("cli.images._load_cdk_json", return_value={}):
            assert manager._replication_regions() == []

    def test_replication_sync_replaces_only_managed_filter(self) -> None:
        manager = _image_manager()
        managed = {"filter": "gco/", "filterType": "PREFIX_MATCH"}
        unrelated = {"filter": "other/", "filterType": "PREFIX_MATCH"}
        current = {
            "rules": [
                "malformed",
                {"destinations": [{"region": "old"}], "repositoryFilters": [managed]},
                {
                    "destinations": [{"region": "keep", "registryId": "999"}],
                    "repositoryFilters": [managed, unrelated],
                },
                {
                    "destinations": [{"region": "third", "registryId": "999"}],
                    "repositoryFilters": [unrelated],
                },
            ]
        }
        ecr = MagicMock()
        ecr.put_replication_configuration.return_value = {"registryId": "response"}
        manager._ecr_client = MagicMock(return_value=ecr)  # type: ignore[method-assign]
        manager._current_replication_configuration = MagicMock(  # type: ignore[method-assign]
            return_value=("existing", current)
        )
        manager._replication_regions = MagicMock(  # type: ignore[method-assign]
            return_value=["us-east-2", "us-west-2", "eu-west-1"]
        )

        result = manager.replication_sync()

        rules = result["configuration"]["rules"]
        assert len(rules) == 3
        assert rules[0]["repositoryFilters"] == [unrelated]
        assert rules[0]["destinations"] == [{"region": "keep", "registryId": "999"}]
        assert rules[1]["destinations"][0]["region"] == "third"
        assert rules[2] == {
            "destinations": [
                {"region": "us-west-2", "registryId": "123456789012"},
                {"region": "eu-west-1", "registryId": "123456789012"},
            ],
            "repositoryFilters": [managed],
        }
        assert result["registry_id"] == "response"

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ({"imageDetails": [{"imageSizeInBytes": 123}]}, 123),
            ({"imageDetails": [{"imageSizeInBytes": "123"}]}, None),
            ({"imageDetails": []}, None),
        ],
    )
    def test_image_size_is_best_effort(
        self, response: dict[str, Any], expected: int | None
    ) -> None:
        manager = _image_manager()
        ecr = MagicMock()
        ecr.describe_images.return_value = response
        manager._ecr_client = MagicMock(return_value=ecr)  # type: ignore[method-assign]
        assert manager._image_size_bytes("service", "v1") == expected
        ecr.describe_images.side_effect = RuntimeError("eventual consistency")
        assert manager._image_size_bytes("service", "v1") is None

    def test_replication_status_isolates_digest_and_repository_failures(self) -> None:
        manager = _image_manager()
        manager.list_repos = MagicMock(  # type: ignore[method-assign]
            return_value=[{"name": "gco/a"}, {"name": "gco/b"}]
        )
        ecr = MagicMock()
        first = MagicMock()
        first.paginate.return_value = [
            {"imageDetails": [{"imageDigest": "sha:a"}, {"imageDigest": "sha:b"}]}
        ]
        second = MagicMock()
        second.paginate.side_effect = ClientError(
            {"Error": {"Code": "Denied", "Message": "denied"}}, "DescribeImages"
        )
        ecr.get_paginator.side_effect = [first, second]
        ecr.describe_image_replication_status.side_effect = [
            ClientError({"Error": {"Code": "Pending", "Message": "wait"}}, "Describe"),
            {
                "replicationStatuses": [
                    {"region": "us-west-2", "status": "COMPLETE", "registryId": "123"}
                ]
            },
        ]
        manager._ecr_client = MagicMock(return_value=ecr)  # type: ignore[method-assign]

        assert manager.replication_status() == [
            {
                "repository": "gco/a",
                "digest": "sha:b",
                "region": "us-west-2",
                "status": "COMPLETE",
                "registry_id": "123",
            }
        ]


class TestCapacityHistoryAndPredictionEdges:
    def test_history_stats_and_patterns_table_and_structured(self) -> None:
        from cli.main import cli

        store = MagicMock()
        store.get_statistics.return_value = {
            "sample_count": 2,
            "metrics": {
                "spot_score": {
                    "count": 2,
                    "min": 2,
                    "p25": 3,
                    "p50": 5,
                    "p75": 7,
                    "max": 8,
                    "mean": 5,
                    "stddev": 3,
                }
            },
        }
        store.get_temporal_patterns.return_value = {
            "metric": "spot_score",
            "patterns": {"Monday": {3: {"avg": 8.5}}},
            "best_windows": [{"day": "Monday", "hour": 3, "avg": 8.5, "count": 2}],
        }
        formatter = MagicMock()
        formatter.format.return_value = "STATS TABLE"
        with (
            patch("cli.capacity.history.get_capacity_history_store", return_value=store),
            patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
        ):
            stats = CliRunner().invoke(
                cli,
                [
                    "capacity",
                    "history",
                    "stats",
                    "-i",
                    "p5.48xlarge",
                    "-r",
                    "us-east-1",
                ],
            )
            patterns = CliRunner().invoke(
                cli,
                [
                    "capacity",
                    "history",
                    "patterns",
                    "-i",
                    "p5.48xlarge",
                    "-r",
                    "us-east-1",
                ],
            )
        assert stats.exit_code == 0
        assert "STATS TABLE" in stats.output
        assert patterns.exit_code == 0
        assert "Monday" in patterns.output
        assert "Best windows" in patterns.output

        with (
            patch("cli.capacity.history.get_capacity_history_store", return_value=store),
            patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
        ):
            structured = CliRunner().invoke(
                cli,
                [
                    "--output",
                    "json",
                    "capacity",
                    "history",
                    "patterns",
                    "-i",
                    "p5.48xlarge",
                    "-r",
                    "us-east-1",
                ],
            )
        assert structured.exit_code == 0
        formatter.print.assert_called_with(store.get_temporal_patterns.return_value)

    @pytest.mark.parametrize("command", ["stats", "patterns"])
    def test_history_empty_and_failure_paths(self, command: str) -> None:
        from cli.main import cli

        store = MagicMock()
        if command == "stats":
            store.get_statistics.return_value = {"sample_count": 0, "metrics": {}}
        else:
            store.get_temporal_patterns.return_value = {"patterns": {}}
        formatter = MagicMock()
        with (
            patch("cli.capacity.history.get_capacity_history_store", return_value=store),
            patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "capacity",
                    "history",
                    command,
                    "-i",
                    "p5.48xlarge",
                    "-r",
                    "us-east-1",
                ],
            )
        assert result.exit_code == 0
        assert "No historical samples" in formatter.print_warning.call_args.args[0]

        method = store.get_statistics if command == "stats" else store.get_temporal_patterns
        method.side_effect = RuntimeError("history corrupt")
        with (
            patch("cli.capacity.history.get_capacity_history_store", return_value=store),
            patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
        ):
            failed = CliRunner().invoke(
                cli,
                [
                    "capacity",
                    "history",
                    command,
                    "-i",
                    "p5.48xlarge",
                    "-r",
                    "us-east-1",
                ],
            )
        assert failed.exit_code == 1
        assert "history corrupt" in formatter.print_error.call_args.args[0]

    def test_prediction_table_all_regions_raw_and_structured(self) -> None:
        from cli.capacity.advisor import CapacityPredictionResult
        from cli.main import cli

        predictions = [
            CapacityPredictionResult(
                instance_type="p5.48xlarge",
                region="us-east-1",
                confidence="high",
                best_windows=[{"day": "Mon", "hour_range": "03-05", "why": "quiet"}],
                avoid_windows=[{"day": "Fri", "hour_range": "18-20", "why": "busy"}],
                reasoning="First reason. Second reason",
                raw_response='{"raw": true}',
            ),
            CapacityPredictionResult(
                instance_type="p5.48xlarge",
                region="us-west-2",
                confidence="low",
                best_windows=[],
                reasoning="",
            ),
        ]
        advisor = MagicMock()
        advisor.predict_capacity_windows_all_regions.return_value = predictions
        with patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor):
            result = CliRunner().invoke(
                cli,
                [
                    "capacity",
                    "predict",
                    "-i",
                    "p5.48xlarge",
                    "--all-regions",
                    "--raw",
                ],
            )
        assert result.exit_code == 0, result.output
        for fragment in (
            "2 region(s)",
            "+ Mon 03-05: quiet",
            "Windows to avoid",
            "First reason",
            '{"raw": true}',
            "no clear best window",
        ):
            assert fragment in result.output

        formatter = MagicMock()
        advisor.predict_capacity_window.return_value = predictions[0]
        with (
            patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor),
            patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
        ):
            structured = CliRunner().invoke(
                cli,
                [
                    "--output",
                    "json",
                    "capacity",
                    "predict",
                    "-i",
                    "p5.48xlarge",
                    "-r",
                    "us-east-1",
                ],
            )
        assert structured.exit_code == 0
        payload = formatter.print.call_args.args[0]
        assert payload["region"] == "us-east-1"
        assert payload["confidence"] == "high"

    @pytest.mark.parametrize("failure", ["value", "empty", "generic"])
    def test_prediction_degraded_and_error_paths(self, failure: str) -> None:
        from cli.main import cli

        advisor = MagicMock()
        if failure == "value":
            advisor.predict_capacity_window.side_effect = ValueError("no samples")
        elif failure == "empty":
            advisor.predict_capacity_windows_all_regions.return_value = []
        else:
            advisor.predict_capacity_window.side_effect = RuntimeError("bedrock denied")
        formatter = MagicMock()
        args = ["capacity", "predict", "-i", "p5.48xlarge"]
        args += ["--all-regions"] if failure == "empty" else ["-r", "us-east-1"]
        with (
            patch("cli.capacity.get_bedrock_capacity_advisor", return_value=advisor),
            patch("cli.commands.capacity_cmd.get_output_formatter", return_value=formatter),
        ):
            result = CliRunner().invoke(cli, args)
        if failure in {"value", "empty"}:
            assert result.exit_code == 0
            formatter.print_warning.assert_called()
        else:
            assert result.exit_code == 1
            assert "bedrock denied" in formatter.print_error.call_args.args[0]


class TestImageCollisionAndPruneEdges:
    @staticmethod
    def _ecr() -> MagicMock:
        ecr = MagicMock()

        class RepoMissing(ClientError):
            pass

        class ImageMissing(ClientError):
            pass

        ecr.exceptions.RepositoryNotFoundException = RepoMissing
        ecr.exceptions.ImageNotFoundException = ImageMissing
        return ecr

    @pytest.mark.parametrize("native", [True, False])
    def test_collision_missing_repository_is_clean(self, native: bool) -> None:
        manager = _image_manager()
        ecr = self._ecr()
        if native:
            ecr.describe_repositories.side_effect = ecr.exceptions.RepositoryNotFoundException(
                {"Error": {"Code": "RepositoryNotFoundException", "Message": "missing"}},
                "DescribeRepositories",
            )
        else:
            ecr.describe_repositories.side_effect = ClientError(
                {"Error": {"Code": "RepositoryNotFoundException", "Message": "missing"}},
                "DescribeRepositories",
            )
        manager._ecr_client = MagicMock(return_value=ecr)  # type: ignore[method-assign]
        manager._check_tag_immutable_collision("service", "v1")
        ecr.describe_images.assert_not_called()

    def test_collision_empty_mutable_and_unexpected_repository_responses(self) -> None:
        manager = _image_manager()
        ecr = self._ecr()
        manager._ecr_client = MagicMock(return_value=ecr)  # type: ignore[method-assign]
        ecr.describe_repositories.return_value = {"repositories": []}
        manager._check_tag_immutable_collision("service", "v1")
        ecr.describe_repositories.return_value = {
            "repositories": [{"imageTagMutability": "MUTABLE"}]
        }
        manager._check_tag_immutable_collision("service", "v1")
        ecr.describe_repositories.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "DescribeRepositories",
        )
        with pytest.raises(ClientError):
            manager._check_tag_immutable_collision("service", "v1")

    @pytest.mark.parametrize("native", [True, False])
    def test_collision_missing_image_tag_is_clean(self, native: bool) -> None:
        manager = _image_manager()
        ecr = self._ecr()
        manager._ecr_client = MagicMock(return_value=ecr)  # type: ignore[method-assign]
        ecr.describe_repositories.return_value = {
            "repositories": [{"imageTagMutability": "IMMUTABLE"}]
        }
        if native:
            ecr.describe_images.side_effect = ecr.exceptions.ImageNotFoundException(
                {"Error": {"Code": "ImageNotFoundException", "Message": "missing"}},
                "DescribeImages",
            )
        else:
            ecr.describe_images.side_effect = ClientError(
                {"Error": {"Code": "ImageNotFoundException", "Message": "missing"}},
                "DescribeImages",
            )
        manager._check_tag_immutable_collision("service", "v1")

    def test_collision_existing_immutable_tag_and_unexpected_image_error(self) -> None:
        manager = _image_manager()
        ecr = self._ecr()
        manager._ecr_client = MagicMock(return_value=ecr)  # type: ignore[method-assign]
        ecr.describe_repositories.return_value = {
            "repositories": [{"imageTagMutability": "IMMUTABLE"}]
        }
        ecr.describe_images.return_value = {"imageDetails": [{"imageDigest": "sha:a"}]}
        with pytest.raises(RuntimeError, match="already exists on immutable repo"):
            manager._check_tag_immutable_collision("service", "v1")
        ecr.describe_images.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeImages"
        )
        with pytest.raises(ClientError):
            manager._check_tag_immutable_collision("service", "v1")

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_prune_skips_fresh_and_malformed_rows_and_isolates_repo_error(
        self, dry_run: bool
    ) -> None:
        from datetime import UTC, datetime, timedelta

        manager = _image_manager()
        manager.list_repos = MagicMock(  # type: ignore[method-assign]
            return_value=[{"name": "gco/a"}, {"name": "gco/b"}]
        )
        ecr = self._ecr()
        first = MagicMock()
        first.paginate.return_value = [
            {
                "imageDetails": [
                    {
                        "imageDigest": "sha:fresh",
                        "imagePushedAt": datetime.now(UTC),
                        "imageSizeInBytes": 1,
                    },
                    {"imagePushedAt": datetime.now(UTC) - timedelta(days=60)},
                    {
                        "imageDigest": "sha:old",
                        "imagePushedAt": datetime.now(UTC) - timedelta(days=60),
                        "imageSizeInBytes": 50,
                    },
                    {
                        "imageDigest": "sha:unknown-date",
                        "imageSizeInBytes": "not-an-int",
                    },
                ]
            }
        ]
        second = MagicMock()
        second.paginate.side_effect = ClientError(
            {"Error": {"Code": "Denied", "Message": "denied"}}, "DescribeImages"
        )
        ecr.get_paginator.side_effect = [first, second]
        manager._ecr_client = MagicMock(return_value=ecr)  # type: ignore[method-assign]
        result = manager.prune(dry_run=dry_run)
        assert result == {
            "dry_run": dry_run,
            "repos_touched": 1,
            "tags_deleted": 2,
            "bytes_freed": 50,
        }
        if dry_run:
            ecr.batch_delete_image.assert_not_called()
        else:
            ecr.batch_delete_image.assert_called_once_with(
                repositoryName="gco/a",
                imageIds=[
                    {"imageDigest": "sha:old"},
                    {"imageDigest": "sha:unknown-date"},
                ],
            )
