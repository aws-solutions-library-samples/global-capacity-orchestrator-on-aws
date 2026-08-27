"""The live-validation ``policy`` action.

The regression this action exists for is subtle in exactly one way, and that way
is what most of these tests are about: ``GET /api/v1/policy`` degrades to **HTTP
200** with a per-namespace ``{"status": "unavailable"}`` when it cannot read the
cluster. On 2026-08-26 all ten harness actions were green while
``cluster_enforcement."gco-jobs"`` was ``403 Forbidden``, because nothing looked
at the body. So ``test_degraded_enforcement_fails_even_though_http_is_200`` is
the load-bearing test here; if it ever passes a degraded payload, the action is
back to proving nothing.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.live_release_validation.actions.policy import action_policy
from scripts.live_release_validation.checks.policy import _validate_region_policy
from scripts.live_release_validation.registry import build_action_registry

ACCOUNT = "111122223333"


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "service": "GCO Manifest Processor API",
        "cluster_id": "gco-us-east-1",
        "region": "us-east-1",
        "source": "deployed-cluster-runtime",
        "policy": {
            "validation_enabled": True,
            "manifest_caps": {
                "max_cpu_millicores": 384_000,
                "max_memory_bytes": 4096 * 1024**3,
                "max_gpu_count": 16,
            },
            "allowed_namespaces": ["gco-jobs"],
            "allowed_kinds": ["Job", "Pod"],
            "trusted_registries": [
                "docker.io",
                f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com",
            ],
            "trusted_dockerhub_orgs": ["pytorch"],
            "require_accelerator_toleration": True,
        },
        "cluster_enforcement": {
            "gco-jobs": {
                "status": "ok",
                "resource_quotas": {"gco-jobs-quota": {"cpu": "384", "memory": "4096Gi"}},
                "limit_ranges": {"gco-jobs-limits": [{"type": "Container", "max": {"cpu": "192"}}]},
            }
        },
    }
    payload.update(overrides)
    return payload


def _ctx(payload: dict[str, Any] | None = None, *, status_code: int = 200) -> SimpleNamespace:
    response = MagicMock()
    response.ok = 200 <= status_code < 300
    response.status_code = status_code
    response.text = "body"
    response.json.return_value = _payload() if payload is None else payload

    aws_client = MagicMock()
    aws_client.make_authenticated_request.return_value = response

    checkpoint = SimpleNamespace(state={})
    return SimpleNamespace(
        settings=SimpleNamespace(expected_account=ACCOUNT, poll_interval_seconds=1),
        checkpoint=checkpoint,
        cdk_context={"deployment_regions": {"regional": ["us-east-1"]}},
        deployment_regions=("us-east-1",),
        config=SimpleNamespace(project_name="gco", global_region="us-east-2"),
        aws_client=aws_client,
        session=MagicMock(),
        state_lock=threading.Lock(),
        persist_callback=lambda _cp: None,
    )


class TestTheRegressionItCatches:
    def test_degraded_enforcement_fails_even_though_http_is_200(self) -> None:
        """The whole point: a 200 whose body says the read failed must fail."""
        payload = _payload(
            cluster_enforcement={"gco-jobs": {"status": "unavailable", "reason": "403 Forbidden"}}
        )
        ctx = _ctx(payload)
        assert ctx.aws_client.make_authenticated_request.return_value.ok is True

        with pytest.raises(RuntimeError) as excinfo:
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]

        message = str(excinfo.value)
        assert "403 Forbidden" in message
        # The message must point at the fix, not just report the symptom.
        assert "resourcequotas" in message
        assert "limitranges" in message

    def test_missing_namespace_entry_fails(self) -> None:
        ctx = _ctx(_payload(cluster_enforcement={"other-ns": {"status": "ok"}}))
        with pytest.raises(RuntimeError, match="no cluster_enforcement for allowed namespace"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]

    def test_absent_enforcement_object_fails(self) -> None:
        ctx = _ctx(_payload(cluster_enforcement={}))
        with pytest.raises(RuntimeError, match="no cluster_enforcement object"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]

    def test_status_ok_with_no_quota_fails(self) -> None:
        """Claiming ok while reporting nothing would satisfy a naive check."""
        ctx = _ctx(
            _payload(
                cluster_enforcement={
                    "gco-jobs": {"status": "ok", "resource_quotas": {}, "limit_ranges": {}}
                }
            )
        )
        with pytest.raises(RuntimeError, match="no ResourceQuota"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]

    def test_status_ok_with_no_limit_range_fails(self) -> None:
        ctx = _ctx(
            _payload(
                cluster_enforcement={
                    "gco-jobs": {
                        "status": "ok",
                        "resource_quotas": {"q": {"cpu": "1"}},
                        "limit_ranges": {},
                    }
                }
            )
        )
        with pytest.raises(RuntimeError, match="no LimitRange"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]

    def test_quota_without_a_cpu_or_memory_ceiling_fails(self) -> None:
        ctx = _ctx(
            _payload(
                cluster_enforcement={
                    "gco-jobs": {
                        "status": "ok",
                        "resource_quotas": {"q": {"pods": "10"}},
                        "limit_ranges": {"l": [{"type": "Container"}]},
                    }
                }
            )
        )
        with pytest.raises(RuntimeError, match="no cpu/memory ceiling"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]


class TestSynthTimeEcrAugmentation:
    def test_missing_project_ecr_fails(self) -> None:
        """CDK appends these; their absence rejects every project-built image."""
        payload = _payload()
        payload["policy"]["trusted_registries"] = ["docker.io"]
        with pytest.raises(RuntimeError, match="no project ECR registry"):
            _validate_region_policy(_ctx(payload), "us-east-1")  # type: ignore[arg-type]

    def test_another_accounts_ecr_does_not_satisfy_it(self) -> None:
        payload = _payload()
        payload["policy"]["trusted_registries"] = [
            "docker.io",
            "999988887777.dkr.ecr.us-east-1.amazonaws.com",
        ]
        with pytest.raises(RuntimeError, match="no project ECR registry"):
            _validate_region_policy(_ctx(payload), "us-east-1")  # type: ignore[arg-type]

    def test_present_augmentation_is_recorded_as_evidence(self) -> None:
        result = _validate_region_policy(_ctx(), "us-east-1")  # type: ignore[arg-type]
        assert result["synth_time_ecr_registries"] == [f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com"]


class TestIdentityGuards:
    def test_wrong_region_in_the_body_fails(self) -> None:
        ctx = _ctx(_payload(region="us-west-2"))
        with pytest.raises(RuntimeError, match="returned Region"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]

    def test_wrong_cluster_id_fails(self) -> None:
        ctx = _ctx(_payload(cluster_id="gco-somewhere-else"))
        with pytest.raises(RuntimeError, match="cluster_id"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]

    def test_wrong_source_fails(self) -> None:
        """The endpoint must name the deployed runtime as its origin."""
        ctx = _ctx(_payload(source="cdk.json"))
        with pytest.raises(RuntimeError, match="source"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]

    def test_non_2xx_fails(self) -> None:
        ctx = _ctx(status_code=503)
        with pytest.raises(RuntimeError, match="Policy readback for us-east-1 failed"):
            _validate_region_policy(ctx, "us-east-1")  # type: ignore[arg-type]


class TestFrontDoorGuards:
    @pytest.mark.parametrize("key", ["max_cpu_millicores", "max_memory_bytes", "max_gpu_count"])
    def test_a_zero_cap_fails(self, key: str) -> None:
        payload = _payload()
        payload["policy"]["manifest_caps"][key] = 0
        with pytest.raises(RuntimeError, match="non-positive"):
            _validate_region_policy(_ctx(payload), "us-east-1")  # type: ignore[arg-type]

    def test_empty_allowed_namespaces_fails(self) -> None:
        payload = _payload()
        payload["policy"]["allowed_namespaces"] = []
        with pytest.raises(RuntimeError, match="no allowed_namespaces"):
            _validate_region_policy(_ctx(payload), "us-east-1")  # type: ignore[arg-type]

    def test_empty_policy_object_fails(self) -> None:
        with pytest.raises(RuntimeError, match="no policy object"):
            _validate_region_policy(_ctx(_payload(policy={})), "us-east-1")  # type: ignore[arg-type]


class TestHappyPath:
    def test_valid_payload_returns_structured_evidence(self) -> None:
        result = _validate_region_policy(_ctx(), "us-east-1")  # type: ignore[arg-type]
        assert result["region"] == "us-east-1"
        assert result["cluster_enforcement"]["gco-jobs"]["status"] == "ok"
        assert result["policy"]["max_gpu_count"] == 16

    def test_action_uses_the_regional_transport(self) -> None:
        ctx = _ctx()
        action_policy(ctx)  # type: ignore[arg-type]
        kwargs = ctx.aws_client.make_authenticated_request.call_args.kwargs
        assert kwargs["path"] == "/api/v1/policy"
        assert kwargs["method"] == "GET"

    def test_action_records_evidence_in_the_checkpoint(self) -> None:
        ctx = _ctx()
        evidence = action_policy(ctx)  # type: ignore[arg-type]
        assert evidence["result"] == "passed"
        assert ctx.checkpoint.state["policy"] is evidence
        assert "us-east-1" in evidence["regions"]

    def test_failure_evidence_is_persisted_before_raising(self) -> None:
        """A crashed run must still show what the endpoint said."""
        ctx = _ctx(
            _payload(cluster_enforcement={"gco-jobs": {"status": "unavailable", "reason": "403"}})
        )
        with pytest.raises(RuntimeError):
            action_policy(ctx)  # type: ignore[arg-type]
        assert ctx.checkpoint.state["policy"]["result"] == "failed"
        assert "403" in ctx.checkpoint.state["policy"]["error"]


class TestRegistryWiring:
    def test_registered_after_topology_and_before_the_lifecycles(self) -> None:
        names = list(build_action_registry())
        assert names.index("policy") > names.index("topology")
        assert names.index("policy") < names.index("api")

    def test_depends_on_topology_only(self) -> None:
        assert build_action_registry()["policy"].dependencies == ("topology",)

    def test_is_read_only_so_it_precedes_the_mutating_lifecycles(self) -> None:
        """Ordering intent: a broken policy surface should be reported early."""
        names = list(build_action_registry())
        for mutating in ("api", "sqs", "central-queue", "schedulers"):
            assert names.index("policy") < names.index(mutating)
