"""Fleet-status rendering and section-boundary regression tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.capacity.multi_region import RegionCapacity
from cli.commands.status_cmd import (
    _render_addons,
    _render_capacity,
    _render_costs,
    _render_inference,
    _render_nodepools,
    _render_section,
    _render_stacks,
    status,
)
from cli.config import GCOConfig
from cli.costs import CostSummary, ResourceCost
from cli.status import (
    SECTION_ADDONS,
    SECTION_CAPACITY,
    SECTION_COSTS,
    SECTION_INFERENCE,
    SECTION_JOBS,
    SECTION_NODEPOOLS,
    SECTION_POLICY,
    SECTION_QUEUE,
    SECTION_REGIONS,
    SECTION_STACKS,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PARTIAL,
    Section,
    _gather_capacity,
    _gather_costs,
    _gather_nodepools,
    _gather_policy,
    _gather_stacks,
    gather_fleet_status,
)


def _config() -> GCOConfig:
    return GCOConfig(
        project_name="test-gco",
        default_region="us-east-1",
        output_format="table",
    )


def test_capacity_and_inference_render_each_returned_record() -> None:
    capacity_lines = _render_capacity(
        {
            "by_region": {
                "us-east-1": {
                    "queue_depth": 2,
                    "running_jobs": 1,
                    "gpu_utilization": 75.5,
                    "cpu_utilization": 20.0,
                    "telemetry_status": "partial",
                    "unavailable_signals": ["cpu"],
                }
            }
        }
    )
    inference_lines = _render_inference(
        {
            "count": 1,
            "totals": {"running": 1},
            "endpoints": [
                {
                    "endpoint_name": "llm",
                    "desired_state": "running",
                    "target_regions": ["us-east-1", "us-west-2"],
                    "namespace": "models",
                }
            ],
        }
    )

    assert "queue 2" in capacity_lines[0]
    assert "unavailable: cpu" in capacity_lines[0]
    assert inference_lines == [
        "endpoints: 1  (running 1)",
        "llm  running  regions [us-east-1,us-west-2]  namespace models",
    ]


def test_gathered_cost_schema_renders_without_translation_or_key_loss() -> None:
    tracker = MagicMock()
    tracker.get_cost_summary.return_value = CostSummary(
        total=12.34,
        currency="USD",
        period_start="2026-01-01",
        period_end="2026-01-31",
        by_service=[ResourceCost(service="Amazon EC2", amount=10.5)],
    )
    tracker.get_cost_allocation_tag_status.return_value = [
        {"tag_key": "Project", "status": "Active"}
    ]
    with patch("cli.costs.get_cost_tracker", return_value=tracker):
        section = _gather_costs(_config(), True)

    lines = _render_costs(section.data)

    assert lines[0] == "total $12.34 over 30 days"
    assert any("Amazon EC2" in line and "$10.50" in line for line in lines)
    assert "cost allocation tags: Project=Active" in lines
    assert any(line.startswith("as of ") for line in lines)
    assert "total_cost" not in section.data


def test_cost_renderer_distinguishes_an_empty_successful_tag_list() -> None:
    lines = _render_costs(
        {
            "total": 0.0,
            "window_days": 30,
            "by_service": [],
            "allocation_tags": [],
        }
    )
    assert lines == ["total $0.00 over 30 days", "cost allocation tags: none"]


def test_cost_renderer_keeps_partial_service_data_when_total_is_absent() -> None:
    lines = _render_costs(
        {
            "by_service": [{"service": "Amazon EC2", "amount": 4.25}],
            "as_of": "2026-01-31T00:00:00Z",
        }
    )

    assert lines == [
        "Amazon EC2                                $4.25",
        "as of 2026-01-31T00:00:00Z",
    ]


def test_nodepool_renderer_covers_reachable_empty_and_unreachable_regions() -> None:
    lines = _render_nodepools(
        {
            "by_region": {
                "us-east-1": {"nodepools": []},
                "us-west-2": {"nodepools": [{"name": "gpu"}, {"name": "batch"}]},
                "eu-west-1": {"note": "endpoint not reachable"},
            }
        }
    )

    assert "0 nodepools: none" in lines[0]
    assert "2 nodepools: gpu, batch" in lines[1]
    assert "endpoint not reachable" in lines[2]


def _addon(name: str, status: str, health: str, message: str = "") -> dict[str, Any]:
    """One record of the ``addons`` section, as the gatherer writes it."""
    return {
        "name": name,
        "status": status,
        "health": health,
        "message": message,
        "updated_at": None,
    }


def test_addon_renderer_lists_failures_then_the_other_problems_then_each_region() -> None:
    lines = _render_addons(
        {
            "by_region": {
                "us-east-1": {
                    "readable": True,
                    "addons": [
                        _addon(
                            "_teardown",
                            "teardown-active",
                            "in-progress",
                            "execution teardown-abc123",
                        ),
                        _addon(
                            "base-manifests",
                            "failed",
                            "unhealthy",
                            "04-resource-quotas.yaml:ValidatingAdmissionPolicy/gco-jobs-container-gpu-limit",
                        ),
                        _addon("keda", "installed", "healthy", "keda installed"),
                    ],
                },
                "us-west-2": {
                    "readable": True,
                    "addons": [
                        _addon("kueue", "failed", "unhealthy"),
                        _addon("volcano", "unknown", "unknown", "not-json"),
                    ],
                },
                "eu-west-1": {"readable": True, "addons": [_addon("keda", "installed", "healthy")]},
            }
        }
    )

    assert lines == [
        "us-east-1        failed           base-manifests  "
        "04-resource-quotas.yaml:ValidatingAdmissionPolicy/gco-jobs-container-gpu-limit",
        "us-west-2        failed           kueue",
        "us-east-1        teardown-active  _teardown       execution teardown-abc123",
        "us-west-2        unknown          volcano         not-json",
        "us-east-1        1 of 3 healthy",
        "us-west-2        0 of 2 healthy",
        "eu-west-1        1 of 1 healthy",
    ]


def test_addon_renderer_shows_one_line_per_region_when_all_is_well() -> None:
    lines = _render_addons(
        {
            "by_region": {
                "us-east-1": {
                    "readable": True,
                    "addons": [
                        _addon("base-manifests", "applied", "healthy", "applied=41 expected=41"),
                        _addon("keda", "installed", "healthy", "keda installed"),
                    ],
                },
                "us-west-2": {"readable": True, "addons": []},
                "eu-west-1": {"readable": False, "addons": []},
            }
        }
    )

    assert lines == [
        "us-east-1        2 of 2 healthy",
        "us-west-2        no add-on status recorded yet",
        "eu-west-1        could not be read",
    ]


def test_addon_renderer_cuts_a_long_failure_to_one_line() -> None:
    message = "helm install volcano failed:\n" + "x" * 200

    [failed, _summary] = _render_addons(
        {
            "by_region": {
                "us-east-1": {
                    "readable": True,
                    "addons": [_addon("volcano", "failed", "unhealthy", message)],
                }
            }
        }
    )

    assert failed.startswith(
        "us-east-1        failed           volcano  helm install volcano failed: x"
    )
    assert failed.endswith("x…")
    assert len(failed.split("  ")[-1]) == 80


def test_an_empty_addons_section_names_each_region_without_records() -> None:
    lines = _render_section(
        Section(
            name=SECTION_ADDONS,
            status=STATUS_EMPTY,
            data={"by_region": {"us-east-1": {"readable": True, "addons": []}}},
        )
    )

    assert lines == [
        "addons [empty]  ·  gco stacks addons status --all-regions",
        "  us-east-1        no add-on status recorded yet",
    ]


def test_an_unreadable_addons_section_shows_only_why() -> None:
    lines = _render_section(
        Section(
            name=SECTION_ADDONS,
            status=STATUS_ERROR,
            data={"by_region": {"us-east-1": {"readable": False, "addons": []}}},
            reason="add-on status could not be read in any region",
            errors=["us-east-1: RuntimeError: Unable to locate credentials"],
        )
    )

    assert lines == [
        "addons [error]  ·  gco stacks addons status --all-regions",
        "  add-on status could not be read in any region",
        "  error: us-east-1: RuntimeError: Unable to locate credentials",
    ]


def test_section_renderer_explains_empty_results_and_truncates_long_error_lists() -> None:
    empty = _render_section(Section(name=SECTION_POLICY, status=STATUS_EMPTY))
    failed = _render_section(
        Section(
            name=SECTION_POLICY,
            status=STATUS_ERROR,
            reason="policy read failed",
            errors=[f"error-{index}" for index in range(7)],
        )
    )

    assert "nothing here" in empty[1]
    assert sum(line.startswith("  error:") for line in failed) == 5
    assert failed[-1] == "  ... and 2 more error(s)"


def test_status_watch_treats_keyboard_interrupt_as_a_clean_exit() -> None:
    with patch("cli.commands.status_cmd._watch_loop", side_effect=KeyboardInterrupt):
        result = CliRunner().invoke(status, ["--watch", "5"], obj=_config())

    assert result.exit_code == 0, result.output


def test_capacity_gather_ignores_sweep_results_outside_the_workload() -> None:
    checker = MagicMock()
    checker._last_region_errors = []
    checker.get_all_regions_capacity.return_value = [
        RegionCapacity(region="us-west-2", telemetry_status="complete"),
        RegionCapacity(region="us-east-1", telemetry_status="complete"),
    ]
    with patch("cli.capacity.get_multi_region_capacity_checker", return_value=checker):
        section = _gather_capacity(
            _config(),
            ["us-east-1"],
            ["us-east-1"],
            {"us-east-1": MagicMock()},
        )

    assert section.status == STATUS_OK
    assert list(section.data["by_region"]) == ["us-east-1"]


def test_nodepool_gather_continues_after_a_public_endpoint_listing_failure() -> None:
    def list_pools(cluster: str, _region: str):
        if cluster.endswith("us-east-1"):
            raise RuntimeError("Kubernetes API unavailable")
        return [{"name": "gpu", "status": "Ready", "capacity_types": "spot"}]

    with (
        patch(
            "cli.kubectl_helpers.describe_cluster_access",
            return_value={"public": True, "private": True},
        ),
        patch("cli.nodepools.list_cluster_nodepools", side_effect=list_pools),
    ):
        section = _gather_nodepools(_config(), True, ["us-east-1", "us-west-2"])

    assert section.status == STATUS_PARTIAL
    assert section.data["by_region"]["us-east-1"]["note"] == "nodepool listing failed"
    assert section.data["by_region"]["us-west-2"]["nodepools"][0]["name"] == "gpu"
    assert any("Kubernetes API unavailable" in error for error in section.errors)


def test_policy_gather_appends_registry_drift_to_the_common_drift_payload() -> None:
    policies = [
        SimpleNamespace(
            ok=True,
            region="us-east-1",
            reason=None,
            enforcement_gaps={},
        ),
        SimpleNamespace(
            ok=True,
            region="us-west-2",
            reason=None,
            enforcement_gaps={},
        ),
    ]
    registry_difference = SimpleNamespace(
        field="trusted_registries",
        values={"us-east-1": ["docker.io"], "us-west-2": ["public.ecr.aws"]},
    )
    with (
        patch("cli.aws_client.get_aws_client", return_value=MagicMock()),
        patch("cli.job_policy.fetch_region_policies", return_value=policies),
        patch("cli.job_policy.detect_policy_drift", return_value=[]),
        patch("cli.job_policy.registry_drift", return_value=registry_difference),
        patch("cli.job_policy.ecr_augmentation", return_value={}),
    ):
        section = _gather_policy(_config(), True, ["us-east-1", "us-west-2"])

    assert section.data["agree"] is False
    assert section.data["drift"] == [
        {
            "field": "trusted_registries",
            "values": {
                "us-east-1": ["docker.io"],
                "us-west-2": ["public.ecr.aws"],
            },
        }
    ]


def _ok_section(name: str) -> Section:
    return Section(name=name, status=STATUS_OK)


def test_shared_regional_probe_failure_degrades_queue_and_capacity_independently() -> None:
    regions = Section(
        name=SECTION_REGIONS,
        status=STATUS_OK,
        data={"workload": ["us-east-1"]},
    )
    queue_gather = MagicMock(return_value=_ok_section(SECTION_QUEUE))
    capacity_gather = MagicMock(return_value=_ok_section(SECTION_CAPACITY))
    with (
        patch("cli.status.resolve_regions", return_value=regions),
        patch("cli.status._load_cdk_json", return_value={"regional": ["us-east-1"]}),
        patch("cli.status._probe_regional_stacks", side_effect=RuntimeError("probe down")),
        patch("cli.status._gather_stacks", return_value=_ok_section(SECTION_STACKS)),
        patch("cli.status._gather_addons", return_value=_ok_section(SECTION_ADDONS)),
        patch("cli.status._gather_queue", queue_gather),
        patch("cli.status._gather_capacity", capacity_gather),
        patch("cli.status._gather_jobs", return_value=_ok_section(SECTION_JOBS)),
        patch("cli.status._gather_inference", return_value=_ok_section(SECTION_INFERENCE)),
        patch("cli.status._gather_costs", return_value=_ok_section(SECTION_COSTS)),
        patch("cli.status._gather_nodepools", return_value=_ok_section(SECTION_NODEPOOLS)),
        patch("cli.status._gather_policy", return_value=_ok_section(SECTION_POLICY)),
    ):
        document = gather_fleet_status(_config())

    assert document.sections[SECTION_QUEUE].status == STATUS_OK
    assert document.sections[SECTION_CAPACITY].status == STATUS_OK
    assert queue_gather.call_args.args[2] == {"us-east-1": None}
    assert capacity_gather.call_args.args[3] == {"us-east-1": None}


def test_status_watch_returns_cleanly_when_the_loop_stops_normally() -> None:
    with patch("cli.commands.status_cmd._watch_loop", return_value=None) as watch_loop:
        result = CliRunner().invoke(status, ["--watch", "5"], obj=_config())

    assert result.exit_code == 0, result.output
    watch_loop.assert_called_once()


def test_a_partial_stacks_section_lists_every_stack_and_marks_the_unreadable_one() -> None:
    """One unreadable stack used to reduce the block to its reason and errors."""
    from botocore.exceptions import ClientError

    from cli.stacks import StackInfo

    def describe(name: str, region: str) -> StackInfo | None:
        if name == "test-gco-api-gateway":
            raise ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "DescribeStacks"
            )
        if name == "test-gco-us-east-1-persistent":
            return StackInfo(name=name, status="CREATE_COMPLETE", region=region)
        if name in {"test-gco-regional-api-us-east-1", "test-gco-analytics"}:
            return None
        return StackInfo(name=name, status="UPDATE_COMPLETE", region=region)

    manager = MagicMock()
    manager.get_stack_status.side_effect = describe
    regions = {"global": "us-east-2", "api_gateway": "us-east-2", "monitoring": "us-east-2"}
    with patch("cli.stacks.get_stack_manager", return_value=manager):
        section = _gather_stacks(_config(), regions, ["us-east-1"])

    lines = _render_section(section)

    assert section.status == STATUS_PARTIAL
    assert lines[0] == "stacks [partial]  ·  gco stacks status <name> --region <region>"
    assert [line.split() for line in lines[1:6]] == [
        ["test-gco-global", "UPDATE_COMPLETE", "healthy", "us-east-2"],
        ["test-gco-api-gateway", "could", "not", "be", "read", "unknown", "us-east-2"],
        ["test-gco-monitoring", "UPDATE_COMPLETE", "healthy", "us-east-2"],
        ["test-gco-us-east-1", "UPDATE_COMPLETE", "healthy", "us-east-1"],
        ["test-gco-us-east-1-persistent", "CREATE_COMPLETE", "healthy", "us-east-1", "(optional)"],
    ]
    assert lines[6] == "  1 of 7 stacks could not be read"
    assert lines[7].startswith("  error: test-gco-api-gateway in us-east-2: ClientError: ")
    assert "Rate exceeded" in lines[7]
    assert len(lines) == 8


def test_an_absent_stack_still_reads_as_a_dash() -> None:
    [line] = _render_stacks(
        {
            "expected": [
                {
                    "name": "test-gco-us-east-1",
                    "region": "us-east-1",
                    "status": None,
                    "health": "not-deployed",
                }
            ]
        }
    )

    assert line.split() == ["test-gco-us-east-1", "-", "not-deployed", "us-east-1"]


@pytest.mark.parametrize(
    ("section", "read_line"),
    [
        (
            Section(
                name=SECTION_QUEUE,
                status=STATUS_PARTIAL,
                data={
                    "by_region": {
                        "us-east-1": {"available": 3, "in_flight": 1, "delayed": 0, "dlq": 0}
                    },
                    "totals": {"available": 3, "in_flight": 1, "delayed": 0, "dlq": 0},
                },
                reason="queue depth unavailable for 1 of 2 regions",
                errors=["us-west-2: RuntimeError: queue absent"],
            ),
            "us-east-1        available 3",
        ),
        (
            Section(
                name=SECTION_CAPACITY,
                status=STATUS_PARTIAL,
                data={
                    "by_region": {
                        "us-east-1": {
                            "queue_depth": 0,
                            "running_jobs": 2,
                            "gpu_utilization": 40.0,
                            "cpu_utilization": 10.0,
                            "telemetry_status": "complete",
                            "unavailable_signals": [],
                        }
                    }
                },
                reason="1 of 2 regions reported incomplete telemetry",
                errors=["us-west-2: gpu: AccessDenied"],
            ),
            "telemetry complete",
        ),
        (
            Section(
                name=SECTION_COSTS,
                status=STATUS_PARTIAL,
                data={
                    "total": 12.34,
                    "window_days": 30,
                    "by_service": [{"service": "Amazon EC2", "amount": 10.5}],
                    "allocation_tags": None,
                },
                reason="cost total read, but the allocation-tag status could not be",
                errors=["cost allocation tag status: AccessDenied"],
            ),
            "total $12.34 over 30 days",
        ),
        (
            Section(
                name=SECTION_NODEPOOLS,
                status=STATUS_PARTIAL,
                data={
                    "by_region": {
                        "us-east-1": {"nodepools": [{"name": "gpu"}]},
                        "us-west-2": {"note": "nodepool listing failed"},
                    }
                },
                reason="nodepools listed in 1 of 2 regions",
                errors=["us-west-2: Kubernetes API unavailable"],
            ),
            "1 nodepools: gpu",
        ),
        (
            Section(
                name=SECTION_ADDONS,
                status=STATUS_PARTIAL,
                data={
                    "by_region": {
                        "us-east-1": {
                            "readable": True,
                            "addons": [_addon("base-manifests", "applied", "healthy")],
                        },
                        "us-west-2": {"readable": False, "addons": []},
                    }
                },
                reason="add-on status could not be read in 1 of 2 regions",
                errors=["us-west-2: ClientError: AccessDeniedException"],
            ),
            "us-east-1        1 of 1 healthy",
        ),
    ],
    ids=["queue", "capacity", "costs", "nodepools", "addons"],
)
def test_a_partial_section_renders_what_it_read_before_its_reason(
    section: Section, read_line: str
) -> None:
    lines = _render_section(section)

    read = next(index for index, line in enumerate(lines) if read_line in line)
    reason = lines.index(f"  {section.reason}")
    assert 0 < read < reason
    assert lines[reason + 1 :] == [f"  error: {section.errors[0]}"]
