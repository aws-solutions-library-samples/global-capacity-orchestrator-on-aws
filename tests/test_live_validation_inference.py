"""Credential-free tests for the main live-validation inference action."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
import yaml
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli._image_reference import immutable_sha256_digest
from cli.inference import InferenceManager
from cli.main import cli
from gco.services.inference_store import InferenceEndpointStore
from scripts.example_job_validation import kube
from scripts.live_release_validation.checks import inference as lifecycle_module
from scripts.live_release_validation.checks import inference_inventory as inventory_module
from scripts.live_release_validation.checks import inference_runtime as runtime_module
from scripts.live_release_validation.checks.inference import (
    KUBERNETES_INVENTORY_KINDS,
    OWNER_LABEL,
    EndpointPlan,
    ManagedInferenceLifecycle,
    ManagedInferenceValidationError,
    build_delete_command,
    build_deploy_command,
    build_endpoint_plans,
    build_health_command,
    build_invoke_command,
    build_models_command,
    extract_generated_text,
    initialize_run_state,
)
from scripts.live_release_validation.models import (
    InferenceRuntimeSpec,
    RunSettings,
    ensure_private_run_directory,
)
from scripts.live_release_validation.registry import build_action_registry
from scripts.live_release_validation.runner import LiveValidationRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
VLLM_IMAGE = "registry.example/vllm@sha256:" + "a" * 64
SGLANG_IMAGE = "registry.example/sglang@sha256:" + "b" * 64
VLLM_REVISION = "c" * 40
SGLANG_REVISION = "d" * 40
OWNER_NONCE = "e" * 64
LIFECYCLE_ID = "f" * 64


def _runtime(
    framework: Literal["vllm", "sglang"],
    *,
    image: str | None = None,
    model_id: str | None = None,
    revision: str | None = None,
) -> InferenceRuntimeSpec:
    if framework == "vllm":
        return InferenceRuntimeSpec(
            framework="vllm",
            image=image or VLLM_IMAGE,
            model_id=model_id or "test/vllm-model",
            model_revision=revision or VLLM_REVISION,
            port=8000,
        )
    return InferenceRuntimeSpec(
        framework="sglang",
        image=image or SGLANG_IMAGE,
        model_id=model_id or "test/sglang-model",
        model_revision=revision or SGLANG_REVISION,
        port=30000,
    )


def _runtime_matrix() -> tuple[InferenceRuntimeSpec, ...]:
    return (_runtime("vllm"), _runtime("sglang"))


def _settings(tmp_path: Path, **changes: Any) -> RunSettings:
    report_dir = tmp_path / "report"
    settings = RunSettings(
        run_id="managed-test",
        repo_root=REPO_ROOT,
        report_dir=report_dir,
        checkpoint_path=report_dir / "checkpoint.json",
        expected_account="1" * 12,
        expected_sha="a" * 40,
        expected_branch="feature/test",
        profile="configured",
        requested_actions=("all",),
        inference_enabled=True,
        selected_region="us-east-1",
        inference_runtimes=_runtime_matrix(),
        poll_interval_seconds=1,
        command_timeout_seconds=2,
        readiness_timeout_seconds=2,
        hpa_timeout_seconds=2,
        deletion_timeout_seconds=2,
        monitor_interval_seconds=1,
        consent=True,
    )
    return dataclasses.replace(settings, **changes) if changes else settings


class _FakeTable:
    def __init__(self, item: dict[str, Any] | None = None) -> None:
        self.item = item
        self.get_calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        return {"Item": self.item} if self.item is not None else {}


class _FakeDynamoResource:
    def __init__(self, table: _FakeTable) -> None:
        self.table = table
        self.table_names: list[str] = []

    def Table(self, name: str) -> _FakeTable:
        self.table_names.append(name)
        return self.table


class _FakeSession:
    def __init__(self, table: _FakeTable) -> None:
        self.resource_object = _FakeDynamoResource(table)
        self.resource_calls: list[tuple[str, str | None]] = []

    def resource(self, service: str, region_name: str | None = None) -> _FakeDynamoResource:
        self.resource_calls.append((service, region_name))
        return self.resource_object


def _ctx(tmp_path: Path, table: _FakeTable | None = None) -> Any:
    checkpoint = SimpleNamespace(state={})
    persisted: list[dict[str, Any]] = []
    return SimpleNamespace(
        checkpoint=checkpoint,
        persist=lambda: persisted.append(dict(checkpoint.state)),
        session=_FakeSession(table or _FakeTable()),
        config=SimpleNamespace(project_name="gco", global_region="us-west-2"),
        persisted=persisted,
    )


def _empty_kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
    return 0, json.dumps({"items": []}), ""


def _lifecycle(
    tmp_path: Path,
    *,
    settings: RunSettings | None = None,
    table: _FakeTable | None = None,
    kubectl: Any = _empty_kubectl,
) -> tuple[ManagedInferenceLifecycle, tuple[EndpointPlan, ...], list[dict[str, Any]], Any]:
    selected_settings = settings or _settings(tmp_path)
    context = _ctx(tmp_path, table)
    plans, state = initialize_run_state(context, selected_settings)
    runner = ManagedInferenceLifecycle(
        ctx=context,
        settings=selected_settings,
        plans=plans,
        state=state,
        kubectl=kubectl,
        kubeconfig_path=selected_settings.kubeconfig_path,
    )
    return runner, plans, runner.records, context


def _owned_item(
    settings: RunSettings,
    plan: EndpointPlan,
    owner_nonce: str = OWNER_NONCE,
    lifecycle_id: str = LIFECYCLE_ID,
) -> dict[str, Any]:
    runtime = plan.runtime
    spec: dict[str, Any] = {
        "image": runtime.image,
        "framework": runtime.framework,
        "port": runtime.port,
        "replicas": plan.replicas,
        "gpu_count": settings.gpu_count,
        "health_check_path": settings.health_path,
        "env": settings.framework_env(runtime),
        "args": list(settings.deploy_extra_args(runtime)),
    }
    if plan.autoscaling:
        spec["autoscaling"] = {
            "enabled": True,
            "min_replicas": settings.hpa_min_replicas,
            "max_replicas": settings.hpa_max_replicas,
            "metrics": [{"type": "cpu", "target": settings.hpa_cpu_target}],
        }
    return {
        "endpoint_name": plan.name,
        "desired_state": "running",
        "target_regions": [settings.selected_region],
        "namespace": settings.namespace,
        "lifecycle_id": lifecycle_id,
        "labels": {OWNER_LABEL: owner_nonce},
        "spec": spec,
        "region_status": {settings.selected_region: {"state": "running"}},
    }


class TestRegistry:
    def test_inference_is_first_class_after_topology(self) -> None:
        registry = build_action_registry()
        names = list(registry)
        assert names.index("inference") == names.index("topology") + 1
        assert registry["inference"].dependencies == ("topology",)
        assert "inference" in LiveValidationRunner._derive_deploy_dependent_actions(registry)


class TestStrictSettings:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("selected_region", "us-west-2"),
            (
                "inference_runtimes",
                (
                    _runtime("vllm", image="registry.example/vllm@sha256:" + "9" * 64),
                    _runtime("sglang"),
                ),
            ),
            (
                "inference_runtimes",
                (_runtime("vllm", model_id="other/model"), _runtime("sglang")),
            ),
            (
                "inference_runtimes",
                (_runtime("vllm", revision="8" * 40), _runtime("sglang")),
            ),
            ("request_prompt", "A different deterministic prompt"),
            ("gpu_count", 1),
            ("hpa_cpu_target", 55),
            ("command_timeout_seconds", 3),
            ("readiness_timeout_seconds", 3),
            ("deletion_timeout_seconds", 3),
            ("monitor_interval_seconds", 2),
        ],
    )
    def test_every_managed_input_changes_resume_identity(
        self, tmp_path: Path, field: str, value: Any
    ) -> None:
        original = _settings(tmp_path)
        changed = dataclasses.replace(original, **{field: value})
        assert changed.identity() != original.identity()

    @pytest.mark.parametrize(
        "image",
        [
            "registry.example/validator:latest",
            "registry.example/validator@sha256:abc",
            "registry.example/validator@sha256:" + "A" * 64,
            "registry.example/validator@sha512:" + "a" * 64,
        ],
    )
    def test_digest_pin_is_mandatory_and_strict(self, tmp_path: Path, image: str) -> None:
        with pytest.raises(ValueError, match="@sha256"):
            _settings(
                tmp_path,
                inference_runtimes=(_runtime("vllm", image=image), _runtime("sglang")),
            )

    def test_digest_parser_is_linear_on_repeated_slash_prefixes(self, tmp_path: Path) -> None:
        adversarial = "!/" * 20_000 + "image@sha256:" + "a" * 64
        assert immutable_sha256_digest(adversarial) is None
        with pytest.raises(ValueError, match="@sha256"):
            _settings(
                tmp_path,
                inference_runtimes=(
                    _runtime("vllm", image=adversarial),
                    _runtime("sglang"),
                ),
            )

    def test_digest_parser_accepts_standard_uppercase_tags(self, tmp_path: Path) -> None:
        image = "registry.example/team/vllm:CUDA12_4@sha256:" + "6" * 64
        assert immutable_sha256_digest(image) == "6" * 64
        settings = _settings(
            tmp_path,
            inference_runtimes=(
                _runtime("vllm", image=image),
                _runtime("sglang"),
            ),
        )
        assert settings.inference_runtimes[0].image == image

    @pytest.mark.parametrize(
        "name",
        [
            "repo::TAG",
            "registry.example:abc/team/repo:TAG",
            "registry.example:0/team/repo:TAG",
            "repo..name:TAG",
            "team/Uppercase-repository:TAG",
        ],
    )
    def test_digest_parser_rejects_malformed_repository_grammar(self, name: str) -> None:
        assert immutable_sha256_digest(name + "@sha256:" + "a" * 64) is None

    def test_digest_parser_rejects_unbounded_numeric_port_without_raising(
        self, tmp_path: Path
    ) -> None:
        image = "registry.example:" + "9" * 5_000 + "/team/vllm:CUDA12@sha256:" + "a" * 64
        assert immutable_sha256_digest(image) is None
        with pytest.raises(ValueError, match="immutable lowercase @sha256"):
            _settings(
                tmp_path,
                inference_runtimes=(
                    _runtime("vllm", image=image),
                    _runtime("sglang"),
                ),
            )

    def test_framework_images_must_be_independent_digests(self, tmp_path: Path) -> None:
        same_digest = "7" * 64
        with pytest.raises(ValueError, match="distinct immutable digests"):
            _settings(
                tmp_path,
                inference_runtimes=(
                    _runtime(
                        "vllm",
                        image="registry.example/vllm@sha256:" + same_digest,
                    ),
                    _runtime(
                        "sglang",
                        image="registry.example/sglang@sha256:" + same_digest,
                    ),
                ),
            )

    @pytest.mark.parametrize("revision", ["main", "A" * 40, "a" * 39])
    def test_model_revision_is_full_immutable_commit(self, tmp_path: Path, revision: str) -> None:
        with pytest.raises(ValueError, match="model_revision"):
            _settings(
                tmp_path,
                inference_runtimes=(
                    _runtime("vllm", revision=revision),
                    _runtime("sglang"),
                ),
            )

    def test_exact_four_endpoints_and_fixed_hpa_bounds(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="exactly four"):
            _settings(tmp_path, endpoint_count=3)
        with pytest.raises(ValueError, match="min_replicas=max_replicas=2"):
            _settings(tmp_path, hpa_max_replicas=3)

    def test_explicit_consent_is_required(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="consent"):
            _settings(tmp_path, consent=False)

    def test_request_contracts_are_literal_and_distinct(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        vllm, sglang = settings.inference_runtimes
        assert vllm.request_path == "/v1/completions"
        assert settings.request_body(vllm) == {
            "max_tokens": 8,
            "model": "test/vllm-model",
            "prompt": "Reply with a short deterministic validation response.",
            "stream": False,
            "temperature": 0,
        }
        # SGLang is driven through its native API on purpose: its
        # OpenAI-compatible surface would collapse into the vLLM contract and
        # let one response shape satisfy both adapters.
        assert sglang.request_path == "/generate"
        assert settings.request_body(sglang) == {
            "sampling_params": {"max_new_tokens": 8, "temperature": 0},
            "text": "Reply with a short deterministic validation response.",
        }
        identities = settings.identity()["inference"]["runtimes"]
        assert identities[0]["request_contract"]["body"] == settings.request_body(vllm)
        assert identities[1]["request_contract"]["body"] == settings.request_body(sglang)
        assert identities[0]["request_contract"] != identities[1]["request_contract"]
        assert identities[0]["request_contract"]["response"] == "choices[0].text:non-empty-string"
        assert identities[1]["request_contract"]["response"] == "text:non-empty-string"
        assert set(identities[0]["request_contract"]["body"]).isdisjoint(
            identities[1]["request_contract"]["body"]
        )


class TestNamesAndOwnership:
    def test_fresh_runs_get_distinct_random_dns_safe_names(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        first_context = _ctx(tmp_path)
        first_plans, first_state = initialize_run_state(first_context, settings)
        second_context = _ctx(tmp_path)
        second_plans, second_state = initialize_run_state(second_context, settings)
        assert first_state["owner_nonce"] != second_state["owner_nonce"]
        assert {plan.name for plan in first_plans}.isdisjoint({plan.name for plan in second_plans})
        for plan in (*first_plans, *second_plans):
            assert len(plan.name) <= 63
            assert re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", plan.name)

    def test_plan_and_random_nonce_persist_before_lifecycle(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        context = _ctx(tmp_path)
        plans, state = initialize_run_state(context, settings)
        assert len(plans) == 4
        assert [(plan.runtime.framework, plan.role) for plan in plans] == [
            ("vllm", "baseline"),
            ("vllm", "hpa"),
            ("sglang", "baseline"),
            ("sglang", "hpa"),
        ]
        assert state["phase"] == "planned"
        assert re.fullmatch(r"[0-9a-f]{64}", state["owner_nonce"])
        assert [item["name"] for item in state["plan"]] == [plan.name for plan in plans]
        assert context.persisted
        resumed_plans, resumed_state = initialize_run_state(context, settings)
        assert resumed_state["owner_nonce"] == state["owner_nonce"]
        assert resumed_plans == plans

    def test_resume_refuses_changed_nonce_or_plan(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        context = _ctx(tmp_path)
        initialize_run_state(context, settings)
        context.checkpoint.state["inference_validation"]["owner_nonce"] = "other"
        with pytest.raises(ManagedInferenceValidationError, match="owner nonce"):
            initialize_run_state(context, settings)

    def test_colliding_ddb_marker_is_refused_before_kubernetes_or_deploy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path)
        plan = build_endpoint_plans(settings, OWNER_NONCE)[0]
        collision = _owned_item(settings, plan)
        collision["labels"] = {OWNER_LABEL: "another-run"}
        kubectl_called = False

        def forbidden_kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            nonlocal kubectl_called
            kubectl_called = True
            raise AssertionError("Kubernetes must not be queried after a DDB collision")

        runner, plans, records, context = _lifecycle(
            tmp_path,
            settings=settings,
            table=_FakeTable(collision),
            kubectl=forbidden_kubectl,
        )
        monkeypatch.setattr(
            runner,
            "_run_command",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("deploy must not run")),
        )
        with pytest.raises(ManagedInferenceValidationError, match="collision"):
            runner.ensure_owned_endpoint(plans[0], records[0])
        assert kubectl_called is False
        assert context.session.resource_object.table.get_calls[0]["ConsistentRead"] is True

    def test_cleanup_never_deletes_a_colliding_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path)
        plan = build_endpoint_plans(settings, OWNER_NONCE)[0]
        collision = _owned_item(settings, plan)
        collision["labels"] = {OWNER_LABEL: "another-run"}
        runner, plans, records, _ = _lifecycle(
            tmp_path, settings=settings, table=_FakeTable(collision)
        )
        called: list[str] = []
        monkeypatch.setattr(
            runner,
            "_run_command",
            lambda *args, **kwargs: called.append("delete") or "",
        )
        with pytest.raises(ManagedInferenceValidationError, match="refused"):
            runner.cleanup_endpoint(plans[0], records[0])
        assert called == []


class TestCommandsAndResponses:
    def test_commands_are_argument_arrays_with_noninteractive_delete(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        vllm_baseline, vllm_hpa, sglang_baseline, sglang_hpa = build_endpoint_plans(
            settings, OWNER_NONCE
        )
        baseline_command = build_deploy_command(settings, vllm_baseline, OWNER_NONCE)
        hpa_command = build_deploy_command(settings, vllm_hpa, OWNER_NONCE)
        sglang_command = build_deploy_command(settings, sglang_baseline, OWNER_NONCE)
        sglang_hpa_command = build_deploy_command(settings, sglang_hpa, OWNER_NONCE)
        invoke_command = build_invoke_command(settings, vllm_baseline)
        health_command = build_health_command(settings, vllm_baseline)
        models_command = build_models_command(settings, vllm_baseline)
        delete_command = build_delete_command(
            settings,
            vllm_baseline,
            OWNER_NONCE,
            LIFECYCLE_ID,
        )

        for command in (
            baseline_command,
            hpa_command,
            sglang_command,
            sglang_hpa_command,
            invoke_command,
            health_command,
            models_command,
            delete_command,
        ):
            assert isinstance(command, list)
            assert all(isinstance(value, str) for value in command)
        assert baseline_command[:7] == [
            sys.executable,
            "-m",
            "cli.main",
            "--output",
            "json",
            "inference",
            "deploy",
        ]
        assert baseline_command[baseline_command.index("--framework") + 1] == "vllm"
        assert baseline_command[baseline_command.index("--gpu-count") + 1] == "0"
        assert "--autoscale-metric" not in baseline_command
        assert hpa_command[hpa_command.index("--autoscale-metric") + 1] == "cpu:70"
        assert hpa_command[hpa_command.index("--min-replicas") + 1] == "2"
        assert hpa_command[hpa_command.index("--max-replicas") + 1] == "2"
        assert baseline_command[baseline_command.index("--port") + 1] == "8000"
        assert "MODEL=test/vllm-model" in baseline_command
        assert baseline_command[baseline_command.index("--extra-args=--model") + 1 :][:5] == [
            "--extra-args",
            "test/vllm-model",
            "--extra-args=--revision",
            "--extra-args",
            VLLM_REVISION,
        ]
        assert sglang_command[sglang_command.index("--framework") + 1] == "sglang"
        assert sglang_command[sglang_command.index("--port") + 1] == "30000"
        # SGLang shares the ``MODEL`` environment convention with vLLM; the
        # launcher itself receives the immutable model on argv.
        assert "MODEL=test/sglang-model" in sglang_command
        assert sglang_command[sglang_command.index("--extra-args=--model-path") + 1 :][:5] == [
            "--extra-args",
            "test/sglang-model",
            "--extra-args=--revision",
            "--extra-args",
            SGLANG_REVISION,
        ]
        assert "--extra-args=--model" not in sglang_command
        assert "--root-path" not in sglang_command
        assert not any(value.startswith("MODEL_ID=") for value in sglang_command)
        assert not any(value.startswith("PORT=") for value in sglang_command)
        assert sglang_hpa_command[sglang_hpa_command.index("--autoscale-metric") + 1] == "cpu:70"
        assert json.loads(invoke_command[invoke_command.index("--data") + 1]) == (
            settings.request_body(vllm_baseline.runtime)
        )
        assert health_command[-2:] == ["--region", settings.selected_region]
        assert models_command[-2:] == ["--region", settings.selected_region]
        assert delete_command[-1] == "--yes"
        assert delete_command[delete_command.index("--expected-owner-label") + 1] == (
            f"{OWNER_LABEL}={OWNER_NONCE}"
        )
        assert delete_command[delete_command.index("--expected-lifecycle-id") + 1] == (LIFECYCLE_ID)

    def test_subprocess_boundary_forces_no_shell_and_kubeconfig(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        captured: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["command"] = command
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

        monkeypatch.setattr(lifecycle_module.subprocess, "run", fake_run)
        runner._run_command(records[0], "probe", ["gco", "inference", "list"])
        assert captured["command"] == ["gco", "inference", "list"]
        assert captured["shell"] is False
        assert captured["env"]["KUBECONFIG"] == str(runner.kubeconfig_path)

    def test_subprocess_timeout_is_clamped_to_phase_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        captured: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

        monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: 9.0)
        monkeypatch.setattr(lifecycle_module.subprocess, "run", fake_run)
        runner._run_command(
            records[0],
            "health",
            ["gco", "inference", "health"],
            deadline=10.25,
        )

        assert captured["timeout"] == pytest.approx(1.25)

    @pytest.mark.parametrize(
        ("framework", "payload", "expected"),
        [
            ("vllm", {"choices": [{"text": " generated "}]}, "generated"),
            ("sglang", {"text": " sglang ", "meta_info": {"finish_reason": "x"}}, "sglang"),
        ],
    )
    def test_exact_invoke_response_schemas(
        self, framework: str, payload: Any, expected: str
    ) -> None:
        output = "INFO POST /private/path\n" + json.dumps(payload)
        assert extract_generated_text(output, framework) == expected

    @pytest.mark.parametrize(
        ("framework", "payload"),
        [
            ("vllm", {"text": "wrong-framework"}),
            ("vllm", {"generated_text": "retired-contract"}),
            ("vllm", {"choices": [{"message": {"content": "chat"}}]}),
            ("sglang", {"choices": [{"text": "wrong-framework"}]}),
            ("sglang", [{"text": "list-is-not-the-selected-contract"}]),
            ("sglang", {"generated_text": "retired-contract"}),
            ("vllm", {"choices": []}),
            ("vllm", {"choices": [{"text": "  "}]}),
            ("sglang", {"text": ""}),
            ("sglang", {"text": "   "}),
            ("sglang", {"text": ["not", "a", "string"]}),
            ("sglang", {"not_text": "value"}),
        ],
    )
    def test_cross_framework_empty_or_alternate_schemas_are_rejected(
        self, framework: str, payload: Any
    ) -> None:
        with pytest.raises(ManagedInferenceValidationError, match="schema"):
            extract_generated_text(json.dumps(payload), framework)

    def test_non_json_invoke_output_is_rejected(self) -> None:
        with pytest.raises(ManagedInferenceValidationError, match="JSON"):
            extract_generated_text("backend returned plain text", "vllm")


class TestSequentialAndFinallyBehavior:
    def test_endpoints_run_strictly_sequentially_after_prior_absence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        events: list[str] = []

        def fake_run(plan: EndpointPlan, record: dict[str, Any]) -> bool:
            if plan.ordinal > 1:
                assert records[plan.ordinal - 2]["absence_proven"] is True
            events.append(f"run-{plan.ordinal}")
            record["invoke_evidence"] = {"generated_text_non_empty": True}
            return True

        def fake_cleanup(plan: EndpointPlan, record: dict[str, Any]) -> dict[str, Any]:
            events.append(f"cleanup-{plan.ordinal}")
            record["absence_proven"] = True
            return {"ddb_absent": True, "kubernetes_counts": {}}

        monkeypatch.setattr(runner, "run_endpoint", fake_run)
        monkeypatch.setattr(runner, "cleanup_endpoint", fake_cleanup)
        summary = runner.execute()
        assert events == [
            *[
                event
                for ordinal in range(1, 5)
                for event in (f"run-{ordinal}", f"cleanup-{ordinal}")
            ],
            *[f"cleanup-{ordinal}" for ordinal in range(1, 5)],
        ]
        assert summary["execution"] == "strictly-sequential"
        assert summary["frameworks"] == {
            "vllm": {"baseline": True, "hpa": True, "invocations": 2, "model_info": 0},
            "sglang": {"baseline": True, "hpa": True, "invocations": 2, "model_info": 0},
        }
        assert summary["all_endpoints_absent"] is True
        serialized = json.dumps(summary)
        assert all(plan.name not in serialized for plan in runner.plans)

    @pytest.mark.parametrize(
        ("failing_method", "failing_ordinal"),
        [
            ("ensure_owned_endpoint", 1),
            ("wait_for_ddb_running", 1),
            ("wait_for_kubernetes_ready", 1),
            ("verify_backend_probes", 1),
            ("invoke", 1),
            ("verify_no_container_restarts", 1),
            ("verify_hpa_stability", 2),
        ],
    )
    def test_each_phase_failure_runs_inner_and_aggregate_cleanup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failing_method: str,
        failing_ordinal: int,
    ) -> None:
        runner, _, _, _ = _lifecycle(tmp_path)
        events: list[str] = []

        def phase_stub(plan: EndpointPlan, record: dict[str, Any]) -> None:
            events.append(f"{failing_method}-{plan.ordinal}")
            if plan.ordinal == failing_ordinal:
                raise RuntimeError("private phase detail")

        for method in (
            "ensure_owned_endpoint",
            "wait_for_ddb_running",
            "wait_for_kubernetes_ready",
            "verify_backend_probes",
            "invoke",
            "verify_no_container_restarts",
            "verify_hpa_stability",
        ):
            monkeypatch.setattr(
                runner,
                method,
                phase_stub if method == failing_method else lambda plan, record: None,
            )

        def cleanup(plan: EndpointPlan, record: dict[str, Any]) -> dict[str, Any]:
            events.append(f"cleanup-{plan.ordinal}")
            record["absence_proven"] = True
            return {}

        monkeypatch.setattr(runner, "cleanup_endpoint", cleanup)
        with pytest.raises(ManagedInferenceValidationError, match="all cleanup attempts"):
            runner.execute()
        assert f"cleanup-{failing_ordinal}" in events
        assert all(f"cleanup-{ordinal}" in events for ordinal in range(1, 5))

    def test_keyboard_interrupt_still_runs_all_cleanup_without_baseexception_catch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, _, _ = _lifecycle(tmp_path)
        cleanup_events: list[int] = []
        monkeypatch.setattr(
            runner,
            "run_endpoint",
            lambda plan, record: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

        def cleanup(plan: EndpointPlan, record: dict[str, Any]) -> dict[str, Any]:
            cleanup_events.append(plan.ordinal)
            record["absence_proven"] = True
            return {}

        monkeypatch.setattr(runner, "cleanup_endpoint", cleanup)
        with pytest.raises(KeyboardInterrupt):
            runner.execute()
        assert cleanup_events == [1, 1, 2, 3, 4]

    def test_cleanup_continues_after_one_cleanup_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, _, _ = _lifecycle(tmp_path)
        cleanup_events: list[int] = []

        monkeypatch.setattr(
            runner,
            "run_endpoint",
            lambda plan, record: (_ for _ in ()).throw(RuntimeError("phase failed")),
        )

        def cleanup(plan: EndpointPlan, record: dict[str, Any]) -> dict[str, Any]:
            cleanup_events.append(plan.ordinal)
            if plan.ordinal == 1:
                raise RuntimeError("cleanup failed")
            record["absence_proven"] = True
            return {}

        monkeypatch.setattr(runner, "cleanup_endpoint", cleanup)
        with pytest.raises(ManagedInferenceValidationError, match="cleanup failures"):
            runner.execute()
        assert cleanup_events[-4:] == [1, 2, 3, 4]

    def test_completed_resume_phase_is_not_recreated_or_reinvoked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        records[0]["validation_complete"] = True
        calls: list[str] = []
        monkeypatch.setattr(
            runner,
            "prove_absence",
            lambda record: calls.append("absence") or {"ddb_absent": True},
        )
        monkeypatch.setattr(
            runner,
            "ensure_owned_endpoint",
            lambda plan, record: (_ for _ in ()).throw(AssertionError("must not create")),
        )
        monkeypatch.setattr(
            runner,
            "invoke",
            lambda plan, record: (_ for _ in ()).throw(AssertionError("must not invoke")),
        )
        assert runner.run_endpoint(plans[0], records[0]) is False
        assert calls == ["absence"]

    def test_ddb_running_wait_keeps_idle_cluster_tunnel_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

        def kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            calls.append((args, kwargs))
            return 0, "ok\n", ""

        settings = _settings(tmp_path, readiness_timeout_seconds=5, poll_interval_seconds=1)
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings, kubectl=kubectl)
        waiting = _owned_item(settings, plans[0], runner.owner_nonce)
        waiting["region_status"] = {settings.selected_region: {"state": "pending"}}
        running = _owned_item(settings, plans[0], runner.owner_nonce)
        observations = iter((waiting, running))
        monkeypatch.setattr(runner, "_strong_get", lambda record: next(observations))
        clock = SimpleNamespace(now=1.0)
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: float(clock.now))
        monkeypatch.setattr(
            runtime_module.time,
            "sleep",
            lambda seconds: setattr(clock, "now", clock.now + float(seconds)),
        )

        runner.wait_for_ddb_running(plans[0], records[0])

        assert calls == [
            (
                ("--request-timeout=5s", "get", "--raw=/readyz"),
                {"timeout": 5.0},
            )
        ]
        assert clock.now == 2.0
        assert records[0]["phase"] == "ddb-running"
        assert records[0]["tunnel_heartbeats"][0]["healthy"] is True

    def test_owned_record_wait_keeps_idle_cluster_tunnel_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

        def kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            calls.append((args, kwargs))
            return 0, "ok\n", ""

        settings = _settings(tmp_path, readiness_timeout_seconds=5, poll_interval_seconds=1)
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings, kubectl=kubectl)
        owned = _owned_item(settings, plans[0], runner.owner_nonce)
        observations = iter((None, owned))
        monkeypatch.setattr(runner, "_strong_get", lambda record: next(observations))
        clock = SimpleNamespace(now=1.0)
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: float(clock.now))
        monkeypatch.setattr(
            runtime_module.time,
            "sleep",
            lambda seconds: setattr(clock, "now", clock.now + float(seconds)),
        )

        result = runner._wait_for_owned_record(plans[0], records[0])

        assert result is owned
        assert calls == [
            (
                ("--request-timeout=5s", "get", "--raw=/readyz"),
                {"timeout": 5.0},
            )
        ]
        assert clock.now == 2.0
        assert records[0]["phase"] == "ownership-confirmed"
        assert records[0]["tunnel_heartbeats"][0]["healthy"] is True

    @pytest.mark.parametrize("waiter", ["ddb-running", "owned-record"])
    def test_ddb_only_waits_never_exceed_their_deadline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        waiter: str,
    ) -> None:
        calls: list[dict[str, Any]] = []

        def kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            del args
            calls.append(kwargs)
            return 0, "ok\n", ""

        settings = _settings(tmp_path, readiness_timeout_seconds=2, poll_interval_seconds=10)
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings, kubectl=kubectl)
        pending = _owned_item(settings, plans[0], runner.owner_nonce)
        pending["region_status"] = {settings.selected_region: {"state": "pending"}}
        monkeypatch.setattr(
            runner,
            "_strong_get",
            (lambda record: pending) if waiter == "ddb-running" else (lambda record: None),
        )
        clock = SimpleNamespace(now=1.0)
        sleeps: list[float] = []
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: float(clock.now))

        def sleep(seconds: float) -> None:
            sleeps.append(float(seconds))
            clock.now += float(seconds)

        monkeypatch.setattr(runtime_module.time, "sleep", sleep)

        wait = (
            runner.wait_for_ddb_running
            if waiter == "ddb-running"
            else runner._wait_for_owned_record
        )
        with pytest.raises(ManagedInferenceValidationError, match="before timeout") as excinfo:
            wait(plans[0], records[0])

        if waiter == "ddb-running":
            # The heartbeat, then the timeout diagnosis (pods, events) on the
            # command budget — the expired phase deadline does not gate it.
            assert calls == [{"timeout": 2.0}, {"timeout": 2.0}, {"timeout": 2.0}]
            (snapshot,) = records[0]["workload_diagnostics"]
            assert snapshot["reason"] == "ddb-running-timeout"
            # This fake answers every read with the heartbeat's "ok": the
            # snapshot records that the pod list could not be decoded instead
            # of masking the timeout, and the error still names the outcome.
            assert snapshot["pods_error"].startswith("kubectl returned non-JSON output")
            assert snapshot["summary"].startswith("no pods observed (kubectl returned non-JSON")
            assert str(excinfo.value).endswith(f"({snapshot['summary']})")
            assert records[0]["last_ddb_observation"]["regional"] == {"state": "pending"}
        else:
            assert calls == [{"timeout": 2.0}]
        assert sleeps == [2.0]
        assert clock.now == 3.0

    @pytest.mark.parametrize("waiter", ["ddb-running", "owned-record"])
    def test_ddb_only_waits_propagate_cancellation_without_sleeping(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        waiter: str,
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        pending = _owned_item(runner.settings, plans[0], runner.owner_nonce)
        pending["region_status"] = {runner.settings.selected_region: {"state": "pending"}}
        monkeypatch.setattr(
            runner,
            "_strong_get",
            (lambda record: pending) if waiter == "ddb-running" else (lambda record: None),
        )
        monkeypatch.setattr(
            runner,
            "keep_cluster_tunnel_alive",
            lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        sleeps: list[float] = []
        monkeypatch.setattr(runtime_module.time, "sleep", lambda seconds: sleeps.append(seconds))

        wait = (
            runner.wait_for_ddb_running
            if waiter == "ddb-running"
            else runner._wait_for_owned_record
        )
        with pytest.raises(KeyboardInterrupt):
            wait(plans[0], records[0])

        assert sleeps == []


class TestHpaProof:
    def test_hpa_target_and_bounds_are_exact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        hpa = {
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": plans[1].name,
                },
                "minReplicas": 2,
                "maxReplicas": 2,
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": 70,
                            },
                        },
                    }
                ],
            }
        }
        monkeypatch.setattr(runner, "_kubectl_json", lambda *args, **kwargs: hpa)
        assert runner._hpa_matches(plans[1], records[1]) is True
        hpa["spec"]["scaleTargetRef"]["name"] = "other"
        assert runner._hpa_matches(plans[1], records[1]) is False

    def test_two_full_monitor_intervals_require_three_ready_observations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        observations: list[int] = []
        sleeps: list[int] = []
        monkeypatch.setattr(runner, "_hpa_matches", lambda plan, record, **kwargs: True)

        def ready(*args: Any, **kwargs: Any) -> tuple[bool, dict[str, int]]:
            observations.append(len(observations) + 1)
            return True, {
                "desired": 2,
                "ready": 2,
                "available": 2,
                "updated": 2,
                "ready_pods": 2,
            }

        monkeypatch.setattr(runner, "_deployment_ready_snapshot", ready)
        monkeypatch.setattr(
            runtime_module.time,
            "sleep",
            lambda seconds: sleeps.append(seconds),
        )
        runner.verify_hpa_stability(plans[1], records[1])
        assert len(observations) == 3
        assert sleeps == [runner.settings.monitor_interval_seconds] * 2
        assert records[1]["phase"] == "hpa-stable"

    def test_stability_fails_if_replica_count_drops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        readiness = iter((True, True, False))
        monkeypatch.setattr(runner, "_hpa_matches", lambda plan, record, **kwargs: True)
        monkeypatch.setattr(
            runner,
            "_deployment_ready_snapshot",
            lambda *args, **kwargs: (next(readiness), {"desired": 2}),
        )
        monkeypatch.setattr(runtime_module.time, "sleep", lambda seconds: None)
        with pytest.raises(ManagedInferenceValidationError, match="remain stable"):
            runner.verify_hpa_stability(plans[1], records[1])


def _owned_inventory_item(summary_key: str, endpoint_name: str) -> dict[str, Any]:
    labels = {"app": endpoint_name, "project": "gco", "gco.io/type": "inference"}
    if summary_key == "deployments":
        return {"metadata": {"name": endpoint_name}}
    if summary_key == "replica_sets":
        return {
            "metadata": {
                "name": f"{endpoint_name}-rs",
                "labels": labels,
                "ownerReferences": [{"kind": "Deployment", "name": endpoint_name}],
            }
        }
    if summary_key == "pods":
        return {"metadata": {"name": f"{endpoint_name}-pod", "labels": labels}}
    if summary_key in {"services", "endpoints"}:
        return {"metadata": {"name": endpoint_name}}
    if summary_key == "endpoint_slices":
        return {
            "metadata": {
                "name": f"{endpoint_name}-slice",
                "labels": {"kubernetes.io/service-name": endpoint_name},
            }
        }
    if summary_key == "hpas":
        return {"metadata": {"name": f"keda-hpa-{endpoint_name}"}}
    if summary_key == "scaled_objects":
        return {"metadata": {"name": endpoint_name}}
    if summary_key == "config_maps":
        return {"metadata": {"name": f"{endpoint_name}-mooncake"}}
    if summary_key == "generated_admin_secrets":
        return {"metadata": {"name": f"{endpoint_name}-admin"}}
    if summary_key in {"legacy_ingresses", "legacy_http_routes"}:
        return {"metadata": {"name": f"{endpoint_name}-proxy"}}
    raise AssertionError(f"Unhandled inventory kind: {summary_key}")


class TestStrongAbsence:
    def test_complete_inventory_queries_every_endpoint_owned_kind(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        endpoint_name = ""
        queried: list[str] = []
        kind_by_resource = {kind.resource: kind for kind in KUBERNETES_INVENTORY_KINDS}

        def inventory_kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            resource = args[1]
            queried.append(resource)
            kind = kind_by_resource[resource]
            payload = {
                "items": [
                    _owned_inventory_item(kind.summary_key, endpoint_name),
                    _owned_inventory_item(kind.summary_key, f"{endpoint_name}-v2"),
                    {"metadata": {"name": "mooncake-master"}},
                ]
            }
            return 0, json.dumps(payload), ""

        runner, plans, records, _ = _lifecycle(
            tmp_path,
            settings=settings,
            kubectl=inventory_kubectl,
        )
        endpoint_name = plans[0].name
        inventory = runner.kubernetes_inventory(records[0])
        assert queried == [kind.resource for kind in KUBERNETES_INVENTORY_KINDS]
        assert set(inventory) == {kind.summary_key for kind in KUBERNETES_INVENTORY_KINDS}
        for kind in KUBERNETES_INVENTORY_KINDS:
            expected_name = _owned_inventory_item(kind.summary_key, endpoint_name)["metadata"][
                "name"
            ]
            assert inventory[kind.summary_key] == [expected_name]
        assert all("mooncake-master" not in names for names in inventory.values())

    @pytest.mark.parametrize("kind", KUBERNETES_INVENTORY_KINDS, ids=lambda item: item.summary_key)
    def test_each_owned_kind_blocks_absence(self, tmp_path: Path, kind: Any) -> None:
        settings = _settings(tmp_path)
        endpoint_name = ""

        def one_kind(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            items = (
                [_owned_inventory_item(kind.summary_key, endpoint_name)]
                if args[1] == kind.resource
                else []
            )
            return 0, json.dumps({"items": items}), ""

        table = _FakeTable()
        runner, plans, records, context = _lifecycle(
            tmp_path,
            settings=settings,
            table=table,
            kubectl=one_kind,
        )
        endpoint_name = plans[0].name
        absent, evidence = runner.absence_snapshot(records[0])
        assert absent is False
        assert evidence["ddb_absent"] is True
        assert evidence["kubernetes_counts"][kind.summary_key] == 1
        assert table.get_calls[-1]["ConsistentRead"] is True
        assert context.session.resource_calls == [("dynamodb", "us-west-2")]

    def test_ddb_presence_blocks_absence_even_when_kubernetes_is_empty(
        self, tmp_path: Path
    ) -> None:
        table = _FakeTable({"endpoint_name": "present"})
        runner, _, records, _ = _lifecycle(tmp_path, table=table)
        absent, evidence = runner.absence_snapshot(records[0])
        assert absent is False
        assert evidence["ddb_absent"] is False
        assert all(count == 0 for count in evidence["kubernetes_counts"].values())
        assert table.get_calls[-1]["ConsistentRead"] is True

    def test_both_strong_ddb_and_full_kubernetes_absence_pass(self, tmp_path: Path) -> None:
        table = _FakeTable()
        runner, _, records, _ = _lifecycle(tmp_path, table=table)
        absent, evidence = runner.absence_snapshot(records[0])
        assert absent is True
        assert evidence["ddb_absent"] is True
        assert set(evidence["kubernetes_counts"]) == {
            kind.summary_key for kind in KUBERNETES_INVENTORY_KINDS
        }
        assert all(count == 0 for count in evidence["kubernetes_counts"].values())

    def test_optional_missing_crds_count_as_absent_but_builtin_failure_propagates(
        self, tmp_path: Path
    ) -> None:
        optional = {kind.resource for kind in KUBERNETES_INVENTORY_KINDS if kind.optional_api}

        def missing_optional(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[1] in optional:
                return 1, "", 'error: the server doesn\'t have a resource type "x"'
            return 0, json.dumps({"items": []}), ""

        runner, _, records, _ = _lifecycle(tmp_path, kubectl=missing_optional)
        assert runner.kubernetes_inventory(records[0])

        def broken_builtin(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return 1, "", "forbidden"

        runner, _, records, _ = _lifecycle(tmp_path, kubectl=broken_builtin)
        with pytest.raises(ManagedInferenceValidationError, match="Kubernetes read failed"):
            runner.kubernetes_inventory(records[0])

    def test_keda_generated_hpa_uses_only_exact_role_names(self, tmp_path: Path) -> None:
        endpoint_name = ""

        def keda_inventory(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            items = []
            if args[1] == "horizontalpodautoscalers.autoscaling":
                items = [
                    {"metadata": {"name": f"keda-hpa-{endpoint_name}"}},
                    {"metadata": {"name": f"keda-hpa-{endpoint_name}-prefill"}},
                    {"metadata": {"name": f"keda-hpa-{endpoint_name}-decode"}},
                    {"metadata": {"name": f"keda-hpa-{endpoint_name}-v2"}},
                ]
            return 0, json.dumps({"items": items}), ""

        runner, plans, records, _ = _lifecycle(tmp_path, kubectl=keda_inventory)
        endpoint_name = plans[0].name
        inventory = runner.kubernetes_inventory(records[0])
        assert inventory["hpas"] == sorted(
            [
                f"keda-hpa-{endpoint_name}",
                f"keda-hpa-{endpoint_name}-prefill",
                f"keda-hpa-{endpoint_name}-decode",
            ]
        )
        assert f"keda-hpa-{endpoint_name}-v2" not in inventory["hpas"]

    def test_absence_requires_two_stable_sweeps_and_resets_on_reappearance(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, _, records, _ = _lifecycle(tmp_path)
        outcomes = iter(
            [
                (True, {"ddb_absent": True, "kubernetes_counts": {}}),
                (False, {"ddb_absent": False, "kubernetes_counts": {"pods": 1}}),
                (True, {"ddb_absent": True, "kubernetes_counts": {}}),
                (True, {"ddb_absent": True, "kubernetes_counts": {}}),
            ]
        )
        sleeps: list[float] = []
        monkeypatch.setattr(runner, "absence_snapshot", lambda *args, **kwargs: next(outcomes))
        monkeypatch.setattr(inventory_module.time, "sleep", sleeps.append)

        evidence = runner.prove_absence(records[0])

        assert evidence["stable_absence_observations"] == 2
        assert records[0]["consecutive_absent_observations"] == 2
        assert sleeps == [1.0, 1.0, 1.0]


class TestIsolatedKubeconfig:
    @staticmethod
    def _fake_subprocess(
        kubeconfig_path: Path,
        calls: list[tuple[list[str], dict[str, Any]]],
    ) -> Any:
        def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            if command[:3] == ["aws", "eks", "update-kubeconfig"]:
                assert command[command.index("--kubeconfig") + 1] == str(kubeconfig_path)
                config = {
                    "apiVersion": "v1",
                    "clusters": [
                        {
                            "name": "arn:aws:eks:us-east-1:111111111111:cluster/test-cluster",
                            "cluster": {
                                "server": "https://real.eks.amazonaws.com",
                                "certificate-authority-data": "CA",
                            },
                        }
                    ],
                    "contexts": [],
                    "current-context": "",
                    "kind": "Config",
                    "preferences": {},
                    "users": [],
                }
                kubeconfig_path.write_text(yaml.safe_dump(config), encoding="utf-8")
                kubeconfig_path.chmod(0o600)
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

        return fake_run

    @pytest.mark.parametrize("active", [False, True], ids=("public", "tunnel"))
    def test_isolated_public_and_tunnel_paths_never_use_home_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        active: bool,
    ) -> None:
        from cli import cluster_tunnel

        report_dir = tmp_path / "report"
        report_dir.mkdir(mode=0o700)
        path = report_dir / "kubeconfig"
        calls: list[tuple[list[str], dict[str, Any]]] = []
        monkeypatch.setattr(
            kube.subprocess,
            "run",
            self._fake_subprocess(path, calls),
        )
        session = SimpleNamespace(
            active=active,
            server="https://127.0.0.1:8443" if active else None,
            tls_server_name="real.eks.amazonaws.com" if active else None,
        )

        @contextlib.contextmanager
        def fake_tunnel(*args: Any, **kwargs: Any):
            yield session

        monkeypatch.setattr(cluster_tunnel, "open_api_server_tunnel", fake_tunnel)
        with kube.cluster_session(
            REPO_ROOT,
            "test-cluster",
            "us-east-1",
            kubeconfig_path=path,
        ) as kubectl:
            code, _, _ = kubectl("get", "pods")
            assert code == 0

        access_command, access_kwargs = calls[0]
        assert access_command == ["gco", "stacks", "access", "--region", "us-east-1"]
        assert access_kwargs["env"]["KUBECONFIG"] == str(path)
        aws_command, aws_kwargs = next(
            (command, kwargs) for command, kwargs in calls if command[0] == "aws"
        )
        assert "--kubeconfig" in aws_command
        assert aws_kwargs["shell"] is False
        kubectl_command, kubectl_kwargs = calls[-1]
        assert kubectl_command[:3] == ["kubectl", "--kubeconfig", str(path)]
        assert kubectl_kwargs["env"]["KUBECONFIG"] == str(path)
        assert kubectl_kwargs["shell"] is False

        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        cluster = config["clusters"][0]["cluster"]
        if active:
            assert cluster["server"] == "https://127.0.0.1:8443"
            assert cluster["tls-server-name"] == "real.eks.amazonaws.com"
        else:
            assert cluster["server"] == "https://real.eks.amazonaws.com"
            assert "tls-server-name" not in cluster
        assert stat_mode(path) == 0o600

    def test_tunnel_session_waits_for_api_before_yield(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli import cluster_tunnel

        report_dir = tmp_path / "report"
        report_dir.mkdir(mode=0o700)
        path = report_dir / "kubeconfig"
        calls: list[tuple[list[str], dict[str, Any]]] = []
        base_run = self._fake_subprocess(path, calls)
        probe_attempts = 0

        def delayed_api(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal probe_attempts
            result = base_run(command, **kwargs)
            if command[0] == "kubectl" and "--raw=/readyz" in command:
                probe_attempts += 1
                if probe_attempts == 1:
                    return subprocess.CompletedProcess(
                        command, 1, stdout="", stderr="Unable to connect: EOF"
                    )
                if probe_attempts == 2:
                    return subprocess.CompletedProcess(
                        command, 1, stdout="", stderr="dial tcp 127.0.0.1:8443: connection refused"
                    )
                if probe_attempts == 3:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        stdout="[+]ping ok\n[-]etcd failed: HTTP 500",
                        stderr="Error from server (InternalError)",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")
            return result

        class _Process:
            @staticmethod
            def poll() -> None:
                return None

        session = SimpleNamespace(
            active=True,
            server="https://127.0.0.1:8443",
            tls_server_name="real.eks.amazonaws.com",
            process=_Process(),
        )

        @contextlib.contextmanager
        def fake_tunnel(*args: Any, **kwargs: Any):
            yield session

        monkeypatch.setattr(kube.subprocess, "run", delayed_api)
        monkeypatch.setattr(kube.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(cluster_tunnel, "open_api_server_tunnel", fake_tunnel)

        with kube.cluster_session(
            REPO_ROOT,
            "test-cluster",
            "us-east-1",
            kubeconfig_path=path,
        ) as kubectl:
            assert probe_attempts == 4
            assert kubectl("get", "pods")[0] == 0

        probe_commands = [command for command, _kwargs in calls if "--raw=/readyz" in command]
        assert len(probe_commands) == 4
        assert all("--request-timeout=5s" in command for command in probe_commands)
        assert calls[-1][0][-2:] == ["get", "pods"]

    def test_api_readiness_rejects_permanent_error(self) -> None:
        attempts = 0

        def forbidden(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            nonlocal attempts
            attempts += 1
            return 1, "", "Error from server (Forbidden): forbidden"

        with pytest.raises(RuntimeError, match=r"permanent.*Forbidden"):
            kube._wait_for_cluster_api(forbidden, tunnel_process=None)
        assert attempts == 1

    def test_api_readiness_deadline_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = iter((0.0, 0.0, 2.0))
        monkeypatch.setattr(kube.time, "monotonic", lambda: next(clock))

        def refused(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return 1, "", "dial tcp 127.0.0.1:8443: connection refused"

        with pytest.raises(RuntimeError, match=r"within 1\.0s after 1 attempt"):
            kube._wait_for_cluster_api(
                refused,
                tunnel_process=None,
                timeout_seconds=1,
                poll_interval_seconds=0.1,
            )

    def test_api_readiness_surfaces_tunnel_exit(self) -> None:
        class _Process:
            @staticmethod
            def poll() -> int:
                return 42

            @staticmethod
            def communicate(timeout: float | None = None) -> tuple[bytes, bytes]:
                return b"", b"Session Manager channel closed"

        def must_not_probe(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            raise AssertionError("kubectl must not run after the tunnel exits")

        with pytest.raises(RuntimeError, match=r"exit code 42.*Session Manager channel closed"):
            kube._wait_for_cluster_api(must_not_probe, tunnel_process=_Process())

    def test_access_failure_propagates_before_tunnel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli import cluster_tunnel

        path = tmp_path / "kubeconfig"
        opened: list[bool] = []

        def failed_access(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")

        @contextlib.contextmanager
        def forbidden_tunnel(*args: Any, **kwargs: Any):
            opened.append(True)
            yield None

        monkeypatch.setattr(kube.subprocess, "run", failed_access)
        monkeypatch.setattr(cluster_tunnel, "open_api_server_tunnel", forbidden_tunnel)
        with (
            pytest.raises(RuntimeError, match="stacks access"),
            kube.cluster_session(
                REPO_ROOT,
                "test-cluster",
                "us-east-1",
                kubeconfig_path=path,
            ),
        ):
            pass
        assert opened == []

    def test_update_kubeconfig_failure_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli import cluster_tunnel

        report_dir = tmp_path / "report"
        report_dir.mkdir()
        path = report_dir / "kubeconfig"
        calls = 0

        def failing_aws(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal calls
            calls += 1
            if command[0] == "aws":
                raise subprocess.CalledProcessError(1, command, stderr="failed")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        @contextlib.contextmanager
        def public_tunnel(*args: Any, **kwargs: Any):
            yield SimpleNamespace(active=False, server=None, tls_server_name=None)

        monkeypatch.setattr(kube.subprocess, "run", failing_aws)
        monkeypatch.setattr(cluster_tunnel, "open_api_server_tunnel", public_tunnel)
        with (
            pytest.raises(subprocess.CalledProcessError),
            kube.cluster_session(
                REPO_ROOT,
                "test-cluster",
                "us-east-1",
                kubeconfig_path=path,
            ),
        ):
            pass
        assert calls == 2

    def test_main_inference_artifacts_are_allowed_in_private_report_dir(
        self, tmp_path: Path
    ) -> None:
        report_dir = tmp_path / "report"
        report_dir.mkdir(mode=0o700)
        for name in (
            "checkpoint.json",
            "live-release-validation.json",
            "live-release-validation.md",
            "kubeconfig",
        ):
            path = report_dir / name
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o600)
        ensure_private_run_directory(report_dir, report_dir / "checkpoint.json")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class TestAtomicOwnedDelete:
    def test_store_owner_condition_is_atomic_with_desired_state_update(self) -> None:
        class UpdateTable:
            def __init__(self) -> None:
                self.kwargs: dict[str, Any] = {}

            def update_item(self, **kwargs: Any) -> dict[str, Any]:
                self.kwargs = kwargs
                return {"Attributes": {"endpoint_name": "ep", "desired_state": "deleted"}}

        table = UpdateTable()
        store = object.__new__(InferenceEndpointStore)
        store._table = table
        result = store.update_desired_state(
            "ep",
            "deleted",
            expected_label=(OWNER_LABEL, OWNER_NONCE),
            expected_lifecycle_id=LIFECYCLE_ID,
        )
        assert result and result["desired_state"] == "deleted"
        assert table.kwargs["ConditionExpression"] == (
            "attribute_exists(endpoint_name) AND labels.#expected_label = "
            ":expected_label_value AND lifecycle_id = :expected_lifecycle_id"
        )
        assert table.kwargs["ExpressionAttributeNames"] == {"#expected_label": OWNER_LABEL}
        values = table.kwargs["ExpressionAttributeValues"]
        assert values[":expected_label_value"] == OWNER_NONCE
        assert values[":expected_lifecycle_id"] == LIFECYCLE_ID
        assert "if_not_exists(deletion_generation" in table.kwargs["UpdateExpression"]

    def test_replacement_race_fails_condition_instead_of_deleting(self) -> None:
        class ReplacedTable:
            def update_item(self, **kwargs: Any) -> dict[str, Any]:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "owner changed",
                        }
                    },
                    "UpdateItem",
                )

        store = object.__new__(InferenceEndpointStore)
        store._table = ReplacedTable()
        assert (
            store.update_desired_state(
                "ep",
                "deleted",
                expected_label=(OWNER_LABEL, OWNER_NONCE),
                expected_lifecycle_id=LIFECYCLE_ID,
            )
            is None
        )

    def test_manager_forwards_owner_and_lifecycle_conditions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, tuple[str, str] | None, str | None]] = []

        class Store:
            def update_desired_state(
                self,
                endpoint_name: str,
                desired_state: str,
                *,
                expected_label: tuple[str, str] | None = None,
                expected_lifecycle_id: str | None = None,
            ) -> dict[str, Any]:
                calls.append((endpoint_name, desired_state, expected_label, expected_lifecycle_id))
                return {"endpoint_name": endpoint_name}

        manager = object.__new__(InferenceManager)
        monkeypatch.setattr(manager, "_get_store", lambda: Store())
        condition = (OWNER_LABEL, OWNER_NONCE)
        assert manager.delete(
            "ep",
            expected_owner_label=condition,
            expected_lifecycle_id=LIFECYCLE_ID,
        )
        assert calls == [("ep", "deleted", condition, LIFECYCLE_ID)]

    def test_hidden_cli_option_reaches_manager_without_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, tuple[str, str] | None, str | None]] = []

        class Manager:
            def delete(
                self,
                endpoint_name: str,
                *,
                expected_owner_label: tuple[str, str] | None = None,
                expected_lifecycle_id: str | None = None,
            ) -> dict[str, Any]:
                calls.append((endpoint_name, expected_owner_label, expected_lifecycle_id))
                return {"endpoint_name": endpoint_name}

        monkeypatch.setattr("cli.inference.get_inference_manager", lambda config: Manager())
        result = CliRunner().invoke(
            cli,
            [
                "inference",
                "delete",
                "ep",
                "--expected-owner-label",
                f"{OWNER_LABEL}={OWNER_NONCE}",
                "--expected-lifecycle-id",
                LIFECYCLE_ID,
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        assert calls == [("ep", (OWNER_LABEL, OWNER_NONCE), LIFECYCLE_ID)]


class TestAdditionalResumeAndDeadlineSafety:
    @pytest.mark.parametrize(
        "record_updates",
        [
            {
                "phase": "invoked",
                "invoke_evidence": {"generated_text_non_empty": True},
            },
            {
                "phase": "invoked",
                "validation_steps_complete": True,
                "absence_proven": True,
                "invoke_evidence": {"generated_text_non_empty": True},
            },
        ],
    )
    def test_post_invoke_crash_windows_never_recreate_or_reinvoke(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        record_updates: dict[str, Any],
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        records[0].update(record_updates)
        monkeypatch.setattr(
            runner,
            "ensure_owned_endpoint",
            lambda plan, record: (_ for _ in ()).throw(AssertionError("must not create")),
        )
        monkeypatch.setattr(
            runner,
            "invoke",
            lambda plan, record: (_ for _ in ()).throw(AssertionError("must not invoke")),
        )
        assert runner.run_endpoint(plans[0], records[0]) is False
        assert records[0]["validation_steps_complete"] is True
        assert records[0]["phase"] == "validation-complete-resume"

    def test_successful_invoke_journal_recovers_without_replaying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan = plans[0]
        record = records[0]
        command = build_invoke_command(runner.settings, plan)
        record["invoke_journal"] = {
            "status": "succeeded",
            "framework": plan.runtime.framework,
            "request_path": plan.runtime.request_path,
            "argv": command,
            "returncode": 0,
            "stdout": json.dumps({"choices": [{"text": "recovered"}]}),
            "stderr": "",
        }
        monkeypatch.setattr(
            runner,
            "_run_command",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("successful invocation must not be replayed")
            ),
        )

        assert runner.run_endpoint(plan, record) is False
        assert record["invoke_evidence"] == {
            "framework": "vllm",
            "generated_text_non_empty": True,
            "generated_text_length": len("recovered"),
            "replayed": False,
        }
        assert record["validation_steps_complete"] is True

    def test_ambiguous_invoke_journal_fails_closed_without_replaying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan = plans[0]
        record = records[0]
        record["invoke_journal"] = {
            "status": "started",
            "framework": plan.runtime.framework,
            "request_path": plan.runtime.request_path,
            "argv": build_invoke_command(runner.settings, plan),
        }
        monkeypatch.setattr(
            runner,
            "_run_command",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("ambiguous invocation must not be replayed")
            ),
        )

        with pytest.raises(ManagedInferenceValidationError, match="non-replayable"):
            runner.run_endpoint(plan, record)

    def test_deletion_deadline_bounds_serial_inventory_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(
            tmp_path,
            command_timeout_seconds=300,
            deletion_timeout_seconds=1,
        )
        clock = SimpleNamespace(now=0.0)
        observed_timeouts: list[float] = []

        def monotonic() -> float:
            return float(clock.now)

        def slow_kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            timeout = float(kwargs["timeout"])
            observed_timeouts.append(timeout)
            clock.now += timeout
            return 0, json.dumps({"items": []}), ""

        runner, _, records, _ = _lifecycle(
            tmp_path,
            settings=settings,
            kubectl=slow_kubectl,
        )
        monkeypatch.setattr(inventory_module.time, "monotonic", monotonic)
        with pytest.raises(ManagedInferenceValidationError, match=r"deadline|timeout"):
            runner.prove_absence(records[0])
        assert observed_timeouts == [1.0]
        assert clock.now == 1.0

    def test_kubernetes_collision_prevents_deploy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path)
        endpoint_name = ""

        def colliding_kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            items = (
                [
                    {
                        "metadata": {
                            "name": f"{endpoint_name}-replica",
                            "labels": {
                                "app": endpoint_name,
                                "project": "gco",
                                "gco.io/type": "inference",
                            },
                            "ownerReferences": [{"kind": "Deployment", "name": endpoint_name}],
                        }
                    }
                ]
                if args[1] == "replicasets.apps"
                else []
            )
            return 0, json.dumps({"items": items}), ""

        runner, plans, records, _ = _lifecycle(
            tmp_path,
            settings=settings,
            kubectl=colliding_kubectl,
        )
        endpoint_name = plans[0].name
        monkeypatch.setattr(
            runner,
            "_run_command",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deploy must not run")),
        )
        with pytest.raises(ManagedInferenceValidationError, match="name collision"):
            runner.ensure_owned_endpoint(plans[0], records[0])


class TestWireContractAndHpaDeadline:
    @pytest.mark.parametrize("framework", ["vllm", "sglang"])
    def test_real_invoke_cli_sends_exact_identity_body(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        framework: str,
    ) -> None:
        settings = _settings(tmp_path)
        plan = next(
            plan
            for plan in build_endpoint_plans(settings, OWNER_NONCE)
            if plan.runtime.framework == framework and plan.role == "baseline"
        )
        captured: dict[str, Any] = {}

        class Manager:
            def get_endpoint(self, endpoint_name: str) -> dict[str, Any]:
                return {
                    "endpoint_name": endpoint_name,
                    "ingress_path": f"/inference/{endpoint_name}",
                    "spec": {
                        "image": plan.runtime.image,
                        "framework": plan.runtime.framework,
                    },
                }

        class Response:
            ok = True
            status_code = 200
            text = ""

            @staticmethod
            def json() -> Any:
                if framework == "sglang":
                    return {"text": "ok", "meta_info": {}}
                return {"choices": [{"text": "ok"}]}

        class Client:
            def make_authenticated_request(self, **kwargs: Any) -> Response:
                captured.update(kwargs)
                return Response()

        monkeypatch.setattr("cli.inference.get_inference_manager", lambda config: Manager())
        monkeypatch.setattr("cli.aws_client.get_aws_client", lambda config: Client())
        command = build_invoke_command(settings, plan)
        assert "--no-stream" not in command
        result = CliRunner().invoke(cli, command[3:])
        assert result.exit_code == 0, result.output
        assert captured["body"] == settings.request_body(plan.runtime)
        assert captured["stream"] is False

    def test_hpa_stability_refuses_intervals_that_do_not_fit_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(
            tmp_path,
            hpa_timeout_seconds=3,
            monitor_interval_seconds=1,
            hpa_stability_intervals=2,
        )
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings)
        clock = SimpleNamespace(now=0.0)
        snapshots: list[int] = []

        monkeypatch.setattr(
            runtime_module.time,
            "monotonic",
            lambda: float(clock.now),
        )
        monkeypatch.setattr(
            runtime_module.time,
            "sleep",
            lambda seconds: setattr(clock, "now", clock.now + float(seconds)),
        )
        monkeypatch.setattr(
            runner,
            "_hpa_matches",
            lambda plan, record, **kwargs: True,
        )

        def near_deadline(*args: Any, **kwargs: Any) -> tuple[bool, dict[str, int]]:
            snapshots.append(len(snapshots) + 1)
            if len(snapshots) == 1:
                clock.now = 1.5
            return True, {"desired": 2, "ready": 2, "ready_pods": 2}

        monkeypatch.setattr(runner, "_deployment_ready_snapshot", near_deadline)
        with pytest.raises(ManagedInferenceValidationError, match="phase deadline"):
            runner.verify_hpa_stability(plans[1], records[1])
        assert snapshots == [1, 2]
        assert clock.now == 2.5


class TestBackendProbeContracts:
    def test_cluster_tunnel_heartbeat_is_bounded_and_rate_limited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

        def kubectl(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            calls.append((args, kwargs))
            return 0, "ok\n", ""

        runner, _, records, _ = _lifecycle(tmp_path, kubectl=kubectl)
        clock = SimpleNamespace(now=300.0)
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: float(clock.now))

        heartbeat = runner.keep_cluster_tunnel_alive(records[0], 0.0, deadline=304.0)
        clock.now = 301.0
        assert runner.keep_cluster_tunnel_alive(records[0], heartbeat) == heartbeat

        assert heartbeat == 300.0
        assert calls == [(("--request-timeout=5s", "get", "--raw=/readyz"), {"timeout": 4.0})]
        assert records[0]["tunnel_heartbeats"] == [
            {
                "started_at_monotonic": 300.0,
                "healthy": True,
                "returncode": 0,
                "stderr": "",
            }
        ]

    def test_cluster_tunnel_heartbeat_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, records, _ = _lifecycle(
            tmp_path,
            kubectl=lambda *args, **kwargs: (1, "", "connection refused"),
        )
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: 300.0)

        with pytest.raises(ManagedInferenceValidationError, match="heartbeat failed"):
            runner.keep_cluster_tunnel_alive(records[0], 0.0)
        assert records[0]["tunnel_heartbeats"][0]["healthy"] is False

    def test_vllm_requires_healthy_response_and_configured_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        runner.table.item = _owned_item(runner.settings, plans[0], runner.owner_nonce)
        stages: list[str] = []

        def run_command(
            record: dict[str, Any],
            stage: str,
            command: list[str],
            **kwargs: Any,
        ) -> str:
            del record, command, kwargs
            stages.append(stage)
            if stage == "health":
                return json.dumps({"status": "healthy", "http_status": 200})
            return json.dumps({"data": [{"id": plans[0].runtime.model_id}]})

        monkeypatch.setattr(runner, "_run_command", run_command)
        runner.verify_backend_probes(plans[0], records[0])

        assert stages == ["health", "model-info"]
        assert records[0]["backend_probe_evidence"] == {
            "health": {
                "healthy": True,
                "http_status": 200,
                "path": "/health",
            },
            "model_info": {
                "path": "/v1/models",
                "configured_model_present": True,
                "model_revision_pinned": True,
                "model_count": 1,
            },
        }

    def test_sglang_requires_health_and_exact_server_info_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path)
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings)
        plan = plans[2]
        record = records[2]
        assert plan.runtime.framework == "sglang"
        runner.table.item = _owned_item(settings, plan, runner.owner_nonce)
        stages: list[str] = []
        commands: list[list[str]] = []

        def run_command(
            checkpoint: dict[str, Any],
            stage: str,
            command: list[str],
            **kwargs: Any,
        ) -> str:
            del checkpoint, kwargs
            stages.append(stage)
            commands.append(command)
            if stage == "health":
                return json.dumps({"status": "healthy", "http_status": 200})
            # ``gco inference models --framework sglang`` projects the identity
            # keys of ``/server_info``; the launcher reports the model path and
            # revision it was actually started with.
            return json.dumps(
                {
                    "model_path": plan.runtime.model_id,
                    "served_model_name": plan.runtime.model_id,
                    "revision": plan.runtime.model_revision,
                    "tokenizer_path": plan.runtime.model_id,
                    "version": "0.5.19",
                }
            )

        monkeypatch.setattr(runner, "_run_command", run_command)
        runner.verify_backend_probes(plan, record)

        assert stages == ["health", "model-info"]
        assert commands[1][commands[1].index("--framework") + 1] == "sglang"
        assert record["backend_probe_evidence"]["model_info"] == {
            "path": "/server_info",
            "configured_model_present": True,
            "configured_revision_present": True,
        }

    @pytest.mark.parametrize(
        "server_info",
        [
            {"model_path": "other/model", "revision": "d" * 40},
            {"model_path": "test/sglang-model", "revision": "0" * 40},
            {"model_path": "test/sglang-model"},
            {"served_model_name": "test/sglang-model", "revision": "d" * 40},
            ["test/sglang-model"],
        ],
    )
    def test_sglang_server_info_must_match_model_path_and_revision(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        server_info: Any,
    ) -> None:
        settings = _settings(tmp_path)
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings)
        plan = plans[2]
        record = records[2]
        runner.table.item = _owned_item(settings, plan, runner.owner_nonce)

        def run_command(
            checkpoint: dict[str, Any],
            stage: str,
            command: list[str],
            **kwargs: Any,
        ) -> str:
            del checkpoint, command, kwargs
            if stage == "health":
                return json.dumps({"status": "healthy", "http_status": 200})
            return json.dumps(server_info)

        monkeypatch.setattr(runner, "_run_command", run_command)
        with pytest.raises(ManagedInferenceValidationError, match="health/model probe failed"):
            runner.verify_backend_probes(plan, record)
        assert (
            "SGLang /server_info did not report the exact model path and revision"
            in record["failures"][-1]["error"]
        )
        assert "backend_probe_evidence" not in record

    def test_health_502_then_healthy_converges_and_records_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan = plans[0]
        record = records[0]
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        clock = SimpleNamespace(now=0.0)
        stages: list[str] = []
        deadlines: list[float | None] = []

        monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: float(clock.now))
        monkeypatch.setattr(
            lifecycle_module.time,
            "sleep",
            lambda seconds: setattr(clock, "now", clock.now + float(seconds)),
        )

        def run_command(
            checkpoint: dict[str, Any],
            stage: str,
            command: list[str],
            *,
            deadline: float | None = None,
        ) -> str:
            del checkpoint, command
            stages.append(stage)
            deadlines.append(deadline)
            if stage == "health" and stages.count("health") == 1:
                return json.dumps(
                    {
                        "status": "unhealthy",
                        "http_status": 502,
                        "path": f"/inference/{plan.name}/health",
                        "latency_ms": 7500.0,
                        "body": {"message": "Internal server error"},
                    }
                )
            if stage == "health":
                return json.dumps({"status": "healthy", "http_status": 200})
            return json.dumps({"data": [{"id": plan.runtime.model_id}]})

        monkeypatch.setattr(runner, "_run_command", run_command)
        runner.verify_backend_probes(plan, record)

        assert stages == ["health", "health", "model-info"]
        assert deadlines == [2.0, 2.0, None]
        assert clock.now == 1.0
        assert len(runner.table.get_calls) == 2
        assert [item["classification"] for item in record["backend_probe_attempts"]] == [
            "retryable-unhealthy",
            "healthy",
        ]
        assert record["backend_probe_attempts"][0]["http_status"] == 502
        assert "Internal server error" in record["backend_probe_attempts"][0]["body_summary"]

    def test_persistent_health_502_exhausts_deadline_before_model_or_invoke(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan = plans[0]
        record = records[0]
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        clock = SimpleNamespace(now=0.0)
        stages: list[str] = []
        monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: float(clock.now))
        monkeypatch.setattr(
            lifecycle_module.time,
            "sleep",
            lambda seconds: setattr(clock, "now", clock.now + float(seconds)),
        )

        def unhealthy(
            checkpoint: dict[str, Any],
            stage: str,
            command: list[str],
            **kwargs: Any,
        ) -> str:
            del checkpoint, command, kwargs
            stages.append(stage)
            return json.dumps(
                {
                    "status": "unhealthy",
                    "http_status": 502,
                    "body": {"message": "still unavailable"},
                }
            )

        monkeypatch.setattr(runner, "_run_command", unhealthy)
        with pytest.raises(ManagedInferenceValidationError, match="health/model"):
            runner.verify_backend_probes(plan, record)

        assert stages == ["health", "health"]
        assert clock.now == 2.0
        assert len(record["backend_probe_attempts"]) == 2
        assert all(
            item["classification"] == "retryable-unhealthy"
            for item in record["backend_probe_attempts"]
        )

    def test_malformed_health_output_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan = plans[0]
        record = records[0]
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        stages: list[str] = []

        def malformed(
            checkpoint: dict[str, Any], stage: str, command: list[str], **kwargs: Any
        ) -> str:
            del checkpoint, command, kwargs
            stages.append(stage)
            return "not JSON"

        monkeypatch.setattr(runner, "_run_command", malformed)
        monkeypatch.setattr(
            lifecycle_module.time,
            "sleep",
            lambda _seconds: (_ for _ in ()).throw(AssertionError("must not retry")),
        )
        with pytest.raises(ManagedInferenceValidationError, match="health/model"):
            runner.verify_backend_probes(plan, record)

        assert stages == ["health"]
        assert record["backend_probe_attempts"][0]["classification"] == "malformed-output"

    @pytest.mark.parametrize("http_status", [401, 403])
    def test_auth_health_failure_is_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http_status: int
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        plan = plans[0]
        record = records[0]
        runner.table.item = _owned_item(runner.settings, plan, runner.owner_nonce)
        stages: list[str] = []

        def denied(
            checkpoint: dict[str, Any], stage: str, command: list[str], **kwargs: Any
        ) -> str:
            del checkpoint, command, kwargs
            stages.append(stage)
            return json.dumps({"status": "unhealthy", "http_status": http_status})

        monkeypatch.setattr(runner, "_run_command", denied)
        monkeypatch.setattr(
            lifecycle_module.time,
            "sleep",
            lambda _seconds: (_ for _ in ()).throw(AssertionError("must not retry")),
        )
        with pytest.raises(ManagedInferenceValidationError, match="health/model"):
            runner.verify_backend_probes(plan, record)

        assert stages == ["health"]
        assert record["backend_probe_attempts"][0]["classification"] == "terminal-contract"


class TestDefaultKubeconfigCompatibility:
    def test_default_cluster_session_preserves_historical_command_shapes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli import cluster_tunnel

        calls: list[tuple[list[str], dict[str, Any]]] = []

        def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

        @contextlib.contextmanager
        def public_tunnel(*args: Any, **kwargs: Any):
            yield SimpleNamespace(active=False, server=None, tls_server_name=None)

        monkeypatch.setattr(kube.subprocess, "run", fake_run)
        monkeypatch.setattr(cluster_tunnel, "open_api_server_tunnel", public_tunnel)

        with kube.cluster_session(REPO_ROOT, "test-cluster", "us-east-1") as kubectl:
            assert kubectl("get", "pods")[0] == 0

        access_command, access_kwargs = calls[0]
        assert access_command == ["gco", "stacks", "access", "--region", "us-east-1"]
        assert access_kwargs["env"] is None
        aws_command, aws_kwargs = calls[1]
        assert aws_command == [
            "aws",
            "eks",
            "update-kubeconfig",
            "--name",
            "test-cluster",
            "--region",
            "us-east-1",
        ]
        assert aws_kwargs["env"] is None
        kubectl_command, kubectl_kwargs = calls[2]
        assert kubectl_command == ["kubectl", "get", "pods"]
        assert kubectl_kwargs["env"] is None
        assert all(kwargs["shell"] is False for _, kwargs in calls)


class TestMainInferenceActionIntegration:
    def test_main_run_settings_execute_without_sibling_settings_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.live_release_validation.actions import inference as action_module

        settings = _settings(tmp_path)
        context = _ctx(tmp_path)
        context.settings = settings
        context.deployment_regions = (settings.selected_region,)
        context.config = SimpleNamespace(
            project_name="gco",
            global_region="us-west-2",
        )
        plans, state = initialize_run_state(context, settings)

        @contextlib.contextmanager
        def cluster_session(*args: Any, **kwargs: Any):
            assert kwargs["gco_command"] == (sys.executable, "-m", "cli.main")
            yield _empty_kubectl

        lifecycle = SimpleNamespace(
            verify_shared_proxy_autoscaling=lambda state: state.update(
                {"shared_proxy_autoscaling": {"phase": "verified"}}
            ),
            execute=lambda: {"all_endpoints_absent": True},
        )
        monkeypatch.setattr(action_module, "initialize_run_state", lambda ctx, cfg: (plans, state))
        monkeypatch.setattr(action_module.kube, "cluster_session", cluster_session)
        monkeypatch.setattr(
            action_module,
            "ManagedInferenceLifecycle",
            lambda **kwargs: lifecycle,
        )

        assert action_module.action_inference(context) == {"all_endpoints_absent": True}


class TestIncarnationRotation:
    def test_cleaned_preinvoke_resume_archives_and_rotates_lifecycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        record = records[0]
        probe_attempts = [
            {
                "attempt": 1,
                "classification": "retryable-unhealthy",
                "http_status": 502,
            }
        ]
        heartbeats = [{"started_at_monotonic": 240.0, "healthy": True}]
        record.update(
            {
                "lifecycle_id": LIFECYCLE_ID,
                "phase": "kubernetes-ready",
                "cleanup_phase": "absent",
                "absence_proven": True,
                "backend_probe_attempts": probe_attempts,
                "tunnel_heartbeats": heartbeats,
            }
        )
        evidence = {
            "ddb_absent": True,
            "kubernetes_counts": {},
            "stable_absence_observations": 2,
        }
        monkeypatch.setattr(runner, "prove_absence", lambda checkpoint: evidence)

        runner._prepare_incarnation_for_resume(plans[0], record)

        assert record["incarnation"] == 2
        assert record["lifecycle_id"] is None
        assert record["cleanup_phase"] == "not-started"
        assert record["closed_incarnations"] == [
            {
                "number": 1,
                "lifecycle_id": LIFECYCLE_ID,
                "invoked": False,
                "cleanup_phase": "absent",
                "absence_evidence": evidence,
                "commands": [],
                "backend_probe_attempts": probe_attempts,
                "tunnel_heartbeats": heartbeats,
                "cleanup_attempts": [],
                "failures": [],
            }
        ]
        assert "backend_probe_attempts" not in record
        assert "tunnel_heartbeats" not in record

    def test_closed_lifecycle_is_never_readopted_and_new_lifecycle_binds(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings)
        plan = plans[0]
        record = records[0]
        record["closed_incarnations"] = [{"number": 1, "lifecycle_id": LIFECYCLE_ID}]
        record["incarnation"] = 2
        record["lifecycle_id"] = None

        with pytest.raises(ManagedInferenceValidationError, match="closed lifecycle"):
            runner._verify_item_contract(
                plan,
                _owned_item(settings, plan, lifecycle_id=LIFECYCLE_ID),
                record,
            )

        replacement_id = "1" * 64
        runner._verify_item_contract(
            plan,
            _owned_item(settings, plan, lifecycle_id=replacement_id),
            record,
        )
        assert record["lifecycle_id"] == replacement_id


class TestSharedProxyAutoscalingProof:
    @staticmethod
    def _deployment(cpu_request: str = "100m") -> dict[str, Any]:
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "api-tls-proxy",
                                "resources": {"requests": {"cpu": cpu_request}},
                            }
                        ]
                    }
                }
            }
        }

    @staticmethod
    def _hpa(*, target: int = 70, active: bool = True) -> dict[str, Any]:
        return {
            "metadata": {"generation": 4},
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "inference-proxy",
                },
                "metrics": [
                    {
                        "type": "ContainerResource",
                        "containerResource": {
                            "name": "cpu",
                            "container": "api-tls-proxy",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": target,
                            },
                        },
                    }
                ],
            },
            "status": {
                "observedGeneration": 4,
                "conditions": [
                    {
                        "type": "ScalingActive",
                        "status": "True" if active else "False",
                        "reason": "ValidMetricFound" if active else "FailedGetResourceMetric",
                    }
                ],
                "currentMetrics": [
                    {
                        "type": "ContainerResource",
                        "containerResource": {
                            "name": "cpu",
                            "container": "api-tls-proxy",
                            "current": {"averageUtilization": 42},
                        },
                    }
                ],
            },
        }

    def test_exact_tls_sidecar_request_and_active_metric_are_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _, _, _ = _lifecycle(tmp_path)

        def kubectl_json(record: dict[str, Any], *args: str, **kwargs: Any) -> Any:
            del record, kwargs
            return self._deployment() if args[1] == "deployment" else self._hpa()

        monkeypatch.setattr(runner, "_kubectl_json", kubectl_json)
        runner.verify_shared_proxy_autoscaling(runner.state)
        evidence = runner.state["shared_proxy_autoscaling"]
        assert evidence["phase"] == "verified"
        assert evidence["last_observed"]["tls_cpu_request"] == "100m"
        assert evidence["last_observed"]["tls_cpu_target"] == 70
        assert evidence["last_observed"]["scaling_active"] is True
        assert evidence["last_observed"]["active_tls_metric_count"] == 1

    def test_inactive_or_wrong_tls_metric_times_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(tmp_path, hpa_timeout_seconds=1, poll_interval_seconds=1)
        runner, _, _, _ = _lifecycle(tmp_path, settings=settings)
        clock = SimpleNamespace(now=0.0)

        def kubectl_json(record: dict[str, Any], *args: str, **kwargs: Any) -> Any:
            del record, kwargs
            return (
                self._deployment()
                if args[1] == "deployment"
                else self._hpa(target=85, active=False)
            )

        monkeypatch.setattr(runner, "_kubectl_json", kubectl_json)
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: float(clock.now))
        monkeypatch.setattr(
            runtime_module.time,
            "sleep",
            lambda seconds: setattr(clock, "now", clock.now + float(seconds)),
        )
        with pytest.raises(ManagedInferenceValidationError, match="TLS autoscaling"):
            runner.verify_shared_proxy_autoscaling(runner.state)


class _DiagnosticsKubectl:
    """kubectl fake for the workload-diagnostics reads, dispatching on argv."""

    def __init__(self, plan_name: str) -> None:
        self.plan_name = plan_name
        self.calls: list[tuple[str, ...]] = []
        self.pods: Any = {"items": []}
        self.events: Any = {"items": []}
        self.nodes: dict[str, Any] = {}
        self.logs: dict[tuple[str, str, bool], Any] = {}
        self.failures: dict[str, Any] = {}

    def __call__(self, *args: str, timeout: float) -> tuple[int, str, str]:
        del timeout
        self.calls.append(args)
        kind = args[1] if args[0] == "get" else args[0]
        failure = self.failures.get(kind)
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, tuple):
            return failure
        if args[0] == "logs":
            key = (args[1], args[args.index("--container") + 1], "--previous" in args)
            return 0, str(self.logs.get(key, f"log of {key}")), ""
        if kind == "pods":
            return 0, json.dumps(self.pods), ""
        if kind == "events":
            return 0, json.dumps(self.events), ""
        if kind == "node":
            return 0, json.dumps(self.nodes.get(args[2], {})), ""
        raise AssertionError(f"unexpected kubectl call {args}")


def _crash_looping_pod(name: str, node: str = "i-node-a") -> dict[str, Any]:
    return {
        "metadata": {"name": f"{name}-6fb59fd4d4-tfdqf"},
        "spec": {"nodeName": node},
        "status": {
            "phase": "Running",
            "startTime": "2026-09-18T16:12:50Z",
            "conditions": [
                {"type": "PodScheduled", "status": "True"},
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "ContainersNotReady",
                    "message": "containers with unready status: [inference]",
                },
                "not-a-dict",
            ],
            "containerStatuses": [
                {
                    "name": "inference",
                    "ready": False,
                    "restartCount": 8,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 5m0s restarting failed container",
                        }
                    },
                    "lastState": {
                        "terminated": {
                            "reason": "Error",
                            "exitCode": 1,
                            "finishedAt": "2026-09-18T16:37:58Z",
                        }
                    },
                },
                {
                    "name": "sidecar",
                    "ready": False,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "2026-09-18T16:18:30Z"}},
                },
                {"name": "ready-helper", "ready": True, "restartCount": 0, "state": {}},
                "corrupt-status",
            ],
        },
    }


class TestWorkloadDiagnostics:
    """The checkpoint must explain a stalled endpoint, not just time it out."""

    def _runner(
        self, tmp_path: Path, **changes: Any
    ) -> tuple[Any, Any, dict[str, Any], _DiagnosticsKubectl]:
        settings = _settings(tmp_path, **changes)
        plans, _ = initialize_run_state(_ctx(tmp_path, None), settings)
        kubectl = _DiagnosticsKubectl(plans[0].name)
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings, kubectl=kubectl)
        return runner, plans[0], records[0], kubectl

    def test_snapshot_names_the_crash_loop_its_output_and_the_gpu(self, tmp_path: Path) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.pods = {"items": [_crash_looping_pod(plan.name), "not-a-pod"]}
        kubectl.nodes["i-node-a"] = {
            "metadata": {
                "labels": {
                    "node.kubernetes.io/instance-type": "g4dn.xlarge",
                    "eks.amazonaws.com/instance-gpu-name": "t4",
                    "eks.amazonaws.com/instance-family": "g4dn",
                    "unrelated": "label",
                }
            },
            "status": {"allocatable": {"nvidia.com/gpu": "1"}},
        }
        kubectl.logs[(f"{plan.name}-6fb59fd4d4-tfdqf", "inference", True)] = (
            "RuntimeError: no kernel image is available for execution on the device\n"
        )
        kubectl.events = {
            "items": [
                {
                    "involvedObject": {"kind": "Pod", "name": f"{plan.name}-6fb59fd4d4-tfdqf"},
                    "type": "Warning",
                    "reason": "BackOff",
                    "count": 9,
                    "lastTimestamp": "2026-09-18T16:40:00Z",
                    "message": "Back-off restarting failed container inference",
                },
                {
                    "involvedObject": {"kind": "Pod", "name": "some-other-workload-abc"},
                    "reason": "Scheduled",
                    "lastTimestamp": "2026-09-18T16:39:00Z",
                    "message": "ignored: not this endpoint",
                },
                {
                    "involvedObject": {"kind": "Deployment", "name": plan.name},
                    "type": "Normal",
                    "reason": "ScalingReplicaSet",
                    "count": 1,
                    "eventTime": "2026-09-18T16:12:46Z",
                    "message": 42,
                },
                "corrupt-event",
            ]
        }

        snapshot = runner.capture_workload_diagnostics(plan, record, reason="unit-test")

        (pod,) = snapshot["pods"]
        assert pod["node"] == "i-node-a"
        assert pod["conditions"] == [
            {
                "type": "Ready",
                "status": "False",
                "reason": "ContainersNotReady",
                "message": "containers with unready status: [inference]",
            }
        ]
        crashed, sidecar, helper = pod["containers"]
        assert crashed["state"] == {
            "status": "waiting",
            "reason": "CrashLoopBackOff",
            "message": "back-off 5m0s restarting failed container",
        }
        assert crashed["last_state"] == {
            "status": "terminated",
            "reason": "Error",
            "exitCode": 1,
            "finishedAt": "2026-09-18T16:37:58Z",
        }
        assert "no kernel image is available" in crashed["previous_log_tail"]
        assert crashed["log_tail"].startswith("log of")
        # Unready without restarts: current output only. Ready: nothing fetched.
        assert "log_tail" in sidecar and "previous_log_tail" not in sidecar
        assert sidecar["state"] == {"status": "running", "startedAt": "2026-09-18T16:18:30Z"}
        assert "log_tail" not in helper and helper["state"] is None
        assert snapshot["nodes"] == {
            "i-node-a": {
                "labels": {
                    "node.kubernetes.io/instance-type": "g4dn.xlarge",
                    "eks.amazonaws.com/instance-family": "g4dn",
                    "eks.amazonaws.com/instance-gpu-name": "t4",
                },
                "allocatable_gpus": "1",
            }
        }
        # Events: this endpoint's only, oldest first, message trimmed to text.
        assert [event["reason"] for event in snapshot["events"]] == ["ScalingReplicaSet", "BackOff"]
        assert snapshot["events"][0]["object"] == f"Deployment/{plan.name}"
        assert snapshot["events"][0]["message"] == 42
        assert snapshot["events"][1]["count"] == 9
        assert snapshot["summary"] == (
            f"{plan.name}-6fb59fd4d4-tfdqf: Running, "
            "inference CrashLoopBackOff, last exit 1 Error (restarts=8), "
            "sidecar running (restarts=0), ready-helper ? (restarts=0), on g4dn.xlarge/t4"
        )
        assert record["last_workload_summary"] == snapshot["summary"]
        assert record["workload_diagnostics"] == [snapshot]
        # Reads: pods, the crashed container's two log tails, the sidecar's
        # current tail, the node, then events.
        assert [call[0] for call in kubectl.calls] == ["get", "logs", "logs", "logs", "get", "get"]
        assert kubectl.calls[1][-1] == "--tail=40"
        assert kubectl.calls[2][-2:] == ("--tail=40", "--previous")

    def test_snapshot_summarizes_an_unscheduled_pod_and_a_missing_node_read(
        self, tmp_path: Path
    ) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.pods = {
            "items": [
                {
                    "metadata": {"name": f"{plan.name}-pending"},
                    "spec": {"nodeName": "i-node-b"},
                    "status": {
                        "phase": "Pending",
                        "conditions": [
                            {
                                "type": "PodScheduled",
                                "status": "False",
                                "reason": "Unschedulable",
                                "message": "0/3 nodes are available: insufficient nvidia.com/gpu",
                            }
                        ],
                        "containerStatuses": "not-a-list",
                    },
                }
            ]
        }
        kubectl.failures["node"] = (
            1,
            "",
            'Error from server (NotFound): nodes "i-node-b" not found',
        )

        snapshot = runner.capture_workload_diagnostics(plan, record, reason="unit-test")

        assert snapshot["pods"][0]["containers"] == []
        assert snapshot["nodes"] == {
            "i-node-b": {
                "error": 'kubectl exited 1: Error from server (NotFound): nodes "i-node-b" not found'
            }
        }
        assert snapshot["summary"] == f"{plan.name}-pending: Pending, unscheduled (Unschedulable)"

    def test_snapshot_records_unavailable_reads_instead_of_raising(self, tmp_path: Path) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.failures["pods"] = OSError("tunnel closed")
        kubectl.failures["events"] = (1, "", "forbidden")

        snapshot = runner.capture_workload_diagnostics(plan, record, reason="unit-test")

        assert snapshot["pods"] == []
        assert snapshot["pods_error"] == "OSError: tunnel closed"
        assert snapshot["events"] == "<unavailable: kubectl exited 1: forbidden>"
        assert snapshot["summary"] == "no pods observed (OSError: tunnel closed)"

    def test_log_tail_reports_its_own_failure_and_payload_shapes_are_tolerated(
        self, tmp_path: Path
    ) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.pods = {
            "items": [
                {
                    "metadata": {"name": f"{plan.name}-x"},
                    "spec": {},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"name": "inference", "ready": False, "restartCount": 1}
                        ],
                    },
                }
            ]
        }
        kubectl.failures["logs"] = RuntimeError("logs unavailable")
        kubectl.events = {"items": "corrupt"}
        record["workload_diagnostics"] = "corrupt-ring"

        snapshot = runner.capture_workload_diagnostics(plan, record, reason="unit-test")

        (container,) = snapshot["pods"][0]["containers"]
        assert container["log_tail"] == "<unavailable: RuntimeError: logs unavailable>"
        assert container["previous_log_tail"] == "<unavailable: RuntimeError: logs unavailable>"
        assert container["state"] is None and container["last_state"] is None
        assert snapshot["nodes"] == {}  # no nodeName: nothing to look up
        assert snapshot["events"] == []
        assert snapshot["summary"] == f"{plan.name}-x: Running, inference ? (restarts=1)"
        assert record["workload_diagnostics"] == [snapshot]

    def test_events_payload_that_is_not_an_object_yields_no_events(self, tmp_path: Path) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.events = ["not", "an", "object"]
        kubectl.pods = ["not", "an", "object"]
        snapshot = runner.capture_workload_diagnostics(plan, record, reason="unit-test")
        assert snapshot["events"] == []
        assert snapshot["pods"] == []
        assert snapshot["summary"] == "no pods observed"

    def test_ring_keeps_the_most_recent_snapshots_only(self, tmp_path: Path) -> None:
        runner, plan, record, _ = self._runner(tmp_path)
        for index in range(runtime_module._DIAGNOSTICS_RING_SIZE + 3):
            runner.capture_workload_diagnostics(plan, record, reason=f"capture-{index}")
        ring = record["workload_diagnostics"]
        assert len(ring) == runtime_module._DIAGNOSTICS_RING_SIZE
        assert ring[0]["reason"] == "capture-3"
        assert ring[-1]["reason"] == f"capture-{runtime_module._DIAGNOSTICS_RING_SIZE + 2}"

    def test_periodic_capture_fires_once_per_interval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, _ = self._runner(tmp_path)
        clock = SimpleNamespace(now=1000.0)
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: float(clock.now))
        mark = runner._diagnostics_due(record, plan, 1000.0, "wait")
        assert mark == 1000.0 and "workload_diagnostics" not in record
        clock.now += runtime_module._DIAGNOSTICS_INTERVAL_SECONDS
        mark = runner._diagnostics_due(record, plan, mark, "wait")
        assert mark == clock.now
        assert [snap["reason"] for snap in record["workload_diagnostics"]] == ["wait"]

    def test_ddb_running_wait_takes_periodic_snapshots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl = self._runner(
            tmp_path, readiness_timeout_seconds=1200, poll_interval_seconds=200
        )
        kubectl.pods = {"items": [_crash_looping_pod(plan.name)]}
        pending = _owned_item(runner.settings, plan, runner.owner_nonce)
        pending["region_status"] = {
            runner.settings.selected_region: {"state": "creating", "message": "rolling out"}
        }
        monkeypatch.setattr(runner, "_strong_get", lambda record: pending)
        monkeypatch.setattr(runner, "keep_cluster_tunnel_alive", lambda *a, **k: 0.0)
        clock = SimpleNamespace(now=0.0)
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: float(clock.now))
        monkeypatch.setattr(
            runtime_module.time,
            "sleep",
            lambda seconds: setattr(clock, "now", clock.now + float(seconds)),
        )

        with pytest.raises(ManagedInferenceValidationError, match="before timeout") as excinfo:
            runner.wait_for_ddb_running(plan, record)

        reasons = [snap["reason"] for snap in record["workload_diagnostics"]]
        # 1200s wait, 200s polls, 300s interval: periodic captures on the
        # polls at 400 and 800 seconds, then the capture the timeout takes.
        assert reasons == ["ddb-running-wait", "ddb-running-wait", "ddb-running-timeout"]
        assert record["last_ddb_observation"]["regional"] == {
            "state": "creating",
            "message": "rolling out",
        }
        assert "inference CrashLoopBackOff, last exit 1 Error (restarts=8)" in str(excinfo.value)

    def test_kubernetes_ready_timeout_carries_the_diagnosis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plan, record, kubectl = self._runner(
            tmp_path, readiness_timeout_seconds=2, poll_interval_seconds=1
        )
        crash_pod = _crash_looping_pod(plan.name)
        kubectl.pods = {"items": [crash_pod]}
        monkeypatch.setattr(
            runner,
            "_deployment_ready_snapshot",
            lambda *args, **kwargs: (False, {"desired": 1, "ready": 0, "ready_pods": 0}),
        )
        clock = SimpleNamespace(now=0.0)
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: float(clock.now))
        monkeypatch.setattr(
            runtime_module.time,
            "sleep",
            lambda seconds: setattr(clock, "now", clock.now + float(seconds)),
        )

        with pytest.raises(ManagedInferenceValidationError, match="readiness was not") as excinfo:
            runner.wait_for_kubernetes_ready(plan, record)

        assert record["workload_diagnostics"][-1]["reason"] == "kubernetes-ready-timeout"
        assert "CrashLoopBackOff" in str(excinfo.value)

    def test_bare_conditions_and_reasonless_exits_are_summarized_plainly(
        self, tmp_path: Path
    ) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.pods = {
            "items": [
                {
                    "metadata": {"name": f"{plan.name}-y"},
                    "spec": {},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Initialized", "status": "False"}],
                        "containerStatuses": [
                            {
                                "name": "inference",
                                "ready": True,
                                "restartCount": 2,
                                "state": {"running": {}},
                                "lastState": {"terminated": {"exitCode": 137}},
                            }
                        ],
                    },
                }
            ]
        }
        snapshot = runner.capture_workload_diagnostics(plan, record, reason="unit-test")
        assert snapshot["pods"][0]["conditions"] == [{"type": "Initialized", "status": "False"}]
        (container,) = snapshot["pods"][0]["containers"]
        # Restarted but currently ready: the previous attempt's output is the
        # interesting part, and the current one is fetched alongside it.
        assert set(container) >= {"log_tail", "previous_log_tail"}
        assert (
            snapshot["summary"]
            == f"{plan.name}-y: Running, inference running, last exit 137 (restarts=2)"
        )


def _probe_killed_pod(name: str, node: str = "i-node-g5") -> dict[str, Any]:
    """A serving pod that liveness killed once: Running, ready, restartCount 1, exit 0.

    The shape observed on 2026-09-18: SGLang's ``/health`` took 1.0 s, the
    probe timeout was 1 s, so the kubelet sent SIGTERM and the server exited
    cleanly — nothing about the pod's *current* state says anything is wrong.
    """
    return {
        "metadata": {"name": f"{name}-5cd6678d9-rvl79"},
        "spec": {"nodeName": node},
        "status": {
            "phase": "Running",
            "startTime": "2026-09-18T19:54:39Z",
            "conditions": [
                {"type": "PodScheduled", "status": "True"},
                {"type": "Ready", "status": "True"},
            ],
            "containerStatuses": [
                {
                    "name": "inference",
                    "ready": True,
                    "restartCount": 1,
                    "state": {"running": {"startedAt": "2026-09-18T20:03:47Z"}},
                    "lastState": {
                        "terminated": {
                            "reason": "Completed",
                            "exitCode": 0,
                            "startedAt": "2026-09-18T20:00:22Z",
                            "finishedAt": "2026-09-18T20:03:46Z",
                        }
                    },
                }
            ],
        },
    }


class TestRestartAudit:
    """A leg that restarted on its way to serving is a failed leg, however ready it looks."""

    def _runner(self, tmp_path: Path) -> tuple[Any, Any, dict[str, Any], _DiagnosticsKubectl]:
        settings = _settings(tmp_path)
        plans, _ = initialize_run_state(_ctx(tmp_path, None), settings)
        kubectl = _DiagnosticsKubectl(plans[0].name)
        runner, plans, records, _ = _lifecycle(tmp_path, settings=settings, kubectl=kubectl)
        return runner, plans[0], records[0], kubectl

    def test_serving_pods_with_no_restarts_pass_and_are_recorded(self, tmp_path: Path) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        pod = _probe_killed_pod(plan.name)
        pod["status"]["containerStatuses"][0]["restartCount"] = 0
        del pod["status"]["containerStatuses"][0]["lastState"]
        kubectl.pods = {"items": [pod]}

        runner.verify_no_container_restarts(plan, record)

        assert record["phase"] == "restart-audited"
        assert record["restart_audit"] == {
            "pods": [f"{plan.name}-5cd6678d9-rvl79"],
            "restarted": [],
        }
        assert record["workload_diagnostics"][-1]["reason"] == "restart-audit"

    def test_a_probe_killed_container_fails_the_leg_and_names_the_cause(
        self, tmp_path: Path
    ) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.pods = {"items": [_probe_killed_pod(plan.name)]}
        kubectl.nodes["i-node-g5"] = {
            "metadata": {
                "labels": {
                    "node.kubernetes.io/instance-type": "g5.xlarge",
                    "eks.amazonaws.com/instance-gpu-name": "a10g",
                }
            }
        }
        kubectl.logs[(f"{plan.name}-5cd6678d9-rvl79", "inference", True)] = (
            "SIGTERM received. signum=None frame=None. Draining requests and shutting down...\n"
        )
        kubectl.events = {
            "items": [
                {
                    "involvedObject": {"kind": "Pod", "name": f"{plan.name}-5cd6678d9-rvl79"},
                    "type": "Warning",
                    "reason": "Unhealthy",
                    "count": 5,
                    "lastTimestamp": "2026-09-18T20:03:41Z",
                    "message": (
                        'Liveness probe failed: Get "http://10.0.11.96:30000/health": '
                        "context deadline exceeded"
                    ),
                },
                {
                    "involvedObject": {"kind": "Pod", "name": f"{plan.name}-5cd6678d9-rvl79"},
                    "type": "Normal",
                    "reason": "Killing",
                    "count": 1,
                    "lastTimestamp": "2026-09-18T20:03:41Z",
                    "message": "Container inference failed liveness probe, will be restarted",
                },
            ]
        }

        with pytest.raises(ManagedInferenceValidationError) as excinfo:
            runner.verify_no_container_restarts(plan, record)

        message = str(excinfo.value)
        assert f"{plan.name}-5cd6678d9-rvl79/inference restarted 1x" in message
        assert "last exit 0 Completed (restarts=1), on g5.xlarge/a10g" in message
        assert record["restart_audit"]["restarted"] == [
            f"{plan.name}-5cd6678d9-rvl79/inference restarted 1x"
        ]
        assert record["phase"] != "restart-audited"
        snapshot = record["workload_diagnostics"][-1]
        assert snapshot["reason"] == "restart-audit"
        (container,) = snapshot["pods"][0]["containers"]
        assert "SIGTERM received" in container["previous_log_tail"]
        assert [event["reason"] for event in snapshot["events"]] == ["Unhealthy", "Killing"]

    def test_the_audit_fails_closed_when_pods_cannot_be_listed(self, tmp_path: Path) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.failures["pods"] = (1, "", "forbidden")

        with pytest.raises(ManagedInferenceValidationError, match="could not list"):
            runner.verify_no_container_restarts(plan, record)
        assert "restart_audit" not in record

    def test_the_audit_fails_closed_when_no_pods_exist(self, tmp_path: Path) -> None:
        runner, plan, record, kubectl = self._runner(tmp_path)
        kubectl.pods = {"items": []}

        with pytest.raises(ManagedInferenceValidationError, match="found no pods"):
            runner.verify_no_container_restarts(plan, record)

    def test_run_endpoint_audits_restarts_last_so_the_whole_leg_is_covered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, plans, records, _ = _lifecycle(tmp_path)
        order: list[str] = []
        for method in (
            "ensure_owned_endpoint",
            "wait_for_ddb_running",
            "wait_for_kubernetes_ready",
            "verify_hpa_stability",
            "verify_backend_probes",
            "invoke",
            "verify_no_container_restarts",
        ):
            monkeypatch.setattr(
                runner,
                method,
                lambda plan, record, _name=method: order.append(_name),
            )

        assert runner.run_endpoint(plans[1], records[1]) is True

        assert plans[1].autoscaling is True
        assert order == [
            "ensure_owned_endpoint",
            "wait_for_ddb_running",
            "wait_for_kubernetes_ready",
            "verify_hpa_stability",
            "verify_backend_probes",
            "invoke",
            "verify_no_container_restarts",
        ]
        assert records[1]["validation_steps_complete"] is True
