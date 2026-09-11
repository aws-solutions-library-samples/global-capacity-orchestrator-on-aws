"""Offline tests for ``scripts/example_job_validation``.

Two jobs: (1) run the harness's static example checks as CI tests, so any
change to ``examples/`` that breaks a documented contract (parse failure,
untrusted image, disallowed kind, catalog drift, missing spec) fails the
PR that made it; and (2) pin the harness's own plumbing — spec registry
shape, selection, settings identity, mutation application, and the action
registry — without any AWS access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.example_job_validation import drivers, static_checks
from scripts.example_job_validation.models import ExampleRunSettings
from scripts.example_job_validation.registry import build_action_registry
from scripts.example_job_validation.specs import (
    COMPANION,
    EXAMPLE_SPECS,
    REMOVE_VALUE,
    SUBMISSION_PATHS,
    required_feature_overrides,
    required_helm_overrides,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The static checks ARE the CI gate for examples/ changes.
# ---------------------------------------------------------------------------


class TestStaticChecksAsCiGate:
    def test_every_static_check_passes(self) -> None:
        findings = static_checks.run_static_checks(REPO_ROOT)
        failures = [
            f"{finding.example} :: {finding.check} :: {finding.detail}"
            for finding in findings
            if not finding.passed
        ]
        assert not failures, (
            "Static example validation failed — run "
            "`python -m scripts.example_job_validation --static-only` locally. "
            "If you changed an example's behavior (not just formatting), a live "
            "run is also required: `gco examples validate --examples <name> ...` "
            "(see docs/EXAMPLE_VALIDATION.md).\n" + "\n".join(failures)
        )

    def test_every_example_file_has_a_spec(self) -> None:
        files = set(static_checks.example_names(REPO_ROOT))
        assert files == set(EXAMPLE_SPECS), (
            f"only in examples/: {sorted(files - set(EXAMPLE_SPECS))}; "
            f"only in specs: {sorted(set(EXAMPLE_SPECS) - files)}"
        )

    def test_specs_use_known_enumerations(self) -> None:
        for name, spec in EXAMPLE_SPECS.items():
            assert spec.submission in SUBMISSION_PATHS, name
            assert spec.timeout_seconds > 0, name

    def test_companion_specs_have_no_live_requirements(self) -> None:
        for name, spec in EXAMPLE_SPECS.items():
            if spec.submission == COMPANION:
                assert not spec.helm_overrides and not spec.feature_overrides, name


# ---------------------------------------------------------------------------
# Spec-derived enablement
# ---------------------------------------------------------------------------


class TestDerivedOverrides:
    def test_full_selection_needs_every_override(self) -> None:
        names = sorted(EXAMPLE_SPECS)
        # kubeflow-trainjob and mlflow-tracking-job deliberately contribute
        # nothing here: the trainer chart and the observability/MLflow bundle
        # are on by default, so a stock deploy already satisfies them.
        assert required_helm_overrides(names) == ("slurm", "yunikorn")
        assert required_feature_overrides(names) == (
            "aurora_pgvector",
            "fsx_lustre",
            "valkey",
            "vector_store",
        )

    def test_narrow_selection_needs_nothing(self) -> None:
        assert required_helm_overrides(["simple-job"]) == ()
        assert required_feature_overrides(["simple-job"]) == ()

    def test_selection_drives_settings_context(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, examples=("slurm-cluster-job", "valkey-cache-job"))
        assert settings.optional_schedulers == ("slurm",)
        assert settings.feature_overrides == ("valkey",)
        context = settings.extra_cdk_context()
        assert context["gco_live_validation_disable_efs_automatic_backups"] == "true"
        assert context["helm_enabled_overrides"] == "slurm"
        assert context["feature_enabled_overrides"] == "valkey"

    def test_identity_pins_selection(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, examples=("simple-job",))
        identity = settings.identity()
        assert identity["selected_examples"] == ["simple-job"]
        assert identity["feature_overrides"] == []
        assert identity["extra_cdk_context"] == {
            "gco_live_validation_disable_efs_automatic_backups": "true"
        }

    def test_unknown_example_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown example name"):
            _settings(tmp_path, examples=("nope",))

    def test_max_parallel_is_not_part_of_resume_identity(self, tmp_path: Path) -> None:
        """Parallelism changes pacing, not what is validated: a checkpointed
        run must be resumable with a different --max-parallel."""
        import dataclasses

        settings = _settings(tmp_path, examples=("simple-job",))
        throttled = dataclasses.replace(settings, max_parallel_examples=2)
        assert settings.identity() == throttled.identity()

    def test_negative_max_parallel_rejected(self, tmp_path: Path) -> None:
        import dataclasses

        settings = _settings(tmp_path, examples=("simple-job",))
        with pytest.raises(ValueError, match="max_parallel_examples"):
            dataclasses.replace(settings, max_parallel_examples=-1)


def _settings(tmp_path: Path, examples: tuple[str, ...]) -> ExampleRunSettings:
    report_dir = tmp_path / "report"
    return ExampleRunSettings(
        run_id="test-run",
        repo_root=REPO_ROOT,
        report_dir=report_dir,
        checkpoint_path=report_dir / "checkpoint.json",
        expected_account="1" * 12,
        expected_sha="a" * 40,
        expected_branch="main",
        profile="configured",
        requested_actions=("all",),
        selected_examples=examples,
    )


# ---------------------------------------------------------------------------
# Action registry shape
# ---------------------------------------------------------------------------


class TestActionRegistry:
    def test_order_and_dependencies(self) -> None:
        registry = build_action_registry()
        assert list(registry) == [
            "preflight",
            "static",
            "baseline",
            "deploy",
            "examples",
            "destroy",
            "final-inventory",
        ]
        assert registry["examples"].dependencies == ("deploy",)
        assert "static" in registry["deploy"].dependencies

    def test_runner_derives_deploy_dependents(self) -> None:
        from scripts.live_release_validation.runner import LiveValidationRunner

        derived = LiveValidationRunner._derive_deploy_dependent_actions(build_action_registry())
        assert derived == frozenset({"deploy", "examples"})

    def test_live_registry_guard_covers_every_deploy_dependent(self) -> None:
        """The derivation covers every reviewed live deploy-dependent action.

        Earlier literals omitted ``opencost`` and later ``inference`` even
        though both depend on topology (and therefore deploy). Deriving from
        the dependency graph closes the runtime gap; this explicit set forces
        each newly registered action to receive a human review here too.
        Every action in it refuses to resume once the checkpoint records teardown,
        which is why a read-only action like ``policy`` still belongs — there
        is nothing to read once the cluster is gone.
        """
        from scripts.live_release_validation.registry import (
            build_action_registry as build_live_registry,
        )
        from scripts.live_release_validation.runner import LiveValidationRunner

        derived = LiveValidationRunner._derive_deploy_dependent_actions(build_live_registry())
        assert derived == frozenset(
            {
                "deploy",
                "topology",
                "inference",
                "policy",
                "api",
                "sqs",
                "central-queue",
                "schedulers",
                "opencost",
                "convergence",
            }
        )


# ---------------------------------------------------------------------------
# Mutation application
# ---------------------------------------------------------------------------


class TestMutations:
    def test_vllm_mutations_replace_model_env(self) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "inference-vllm")
        path, disclosed = drivers.apply_mutations(parsed)
        try:
            import yaml

            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
            env = {
                entry["name"]: entry.get("value")
                for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
            }
            assert env["MODEL"] == "facebook/opt-125m"
            assert env["MAX_MODEL_LEN"] == "2048"
            assert disclosed == EXAMPLE_SPECS["inference-vllm"].mutations
        finally:
            path.unlink(missing_ok=True)

    def test_unmutated_example_submits_the_shipped_file(self) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "simple-job")
        path, disclosed = drivers.apply_mutations(parsed)
        assert path == parsed.path
        assert disclosed == {}

    def test_every_declared_mutation_lands(self) -> None:
        """A mutation that matches nothing would silently validate the wrong thing."""
        import yaml

        for name, spec in EXAMPLE_SPECS.items():
            if not spec.mutations:
                continue
            parsed = static_checks.parse_example(REPO_ROOT, name)
            path, _ = drivers.apply_mutations(parsed)
            try:
                mutated = path.read_text(encoding="utf-8")
                for key, replacement in spec.mutations.items():
                    if replacement == REMOVE_VALUE:
                        target = key.rsplit(".", 1)[-1]
                        assert target not in mutated, f"{name}: {target!r} should have been removed"
                        continue
                    assert replacement in mutated, (
                        f"{name}: mutation value {replacement!r} not present after application"
                    )
                # The result must remain valid YAML.
                assert list(yaml.safe_load_all(mutated))
            finally:
                path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI argument surface
# ---------------------------------------------------------------------------


class TestMainArgs:
    def test_static_only_smoke(self, capsys: pytest.CaptureFixture[str]) -> None:
        import sys
        from unittest.mock import patch

        from scripts.example_job_validation.__main__ import main

        with patch.object(sys, "argv", ["prog", "--static-only", "--examples", "simple-job"]):
            assert main() == 0
        out = capsys.readouterr().out
        assert "0 failed" in out

    def test_selection_excludes_skipped(self) -> None:
        from scripts.example_job_validation.__main__ import _build_parser, _select_examples

        parser = _build_parser()
        args = parser.parse_args(["--examples", "simple-job,gpu-job", "--skip-examples", "gpu-job"])
        assert _select_examples(parser, args) == ("simple-job",)

    def test_default_selection_is_every_example(self) -> None:
        from scripts.example_job_validation.__main__ import _build_parser, _select_examples

        parser = _build_parser()
        args = parser.parse_args([])
        assert _select_examples(parser, args) == tuple(sorted(EXAMPLE_SPECS))


# ---------------------------------------------------------------------------
# Resource-governance fit + admission fail-fast
# (live regression: run ex241-df723811 — the old per-container ceilings
# rejected the EFA training example at admission and the waiter burned the
# full timeout against a permanently podless Job)
# ---------------------------------------------------------------------------


def _parsed_job_example(resources: dict, *, parallelism: int = 1) -> static_checks.ParsedExample:
    spec = EXAMPLE_SPECS["gpu-job"]
    return static_checks.ParsedExample(
        name="synthetic-job",
        path=REPO_ROOT / "examples" / "gpu-job.yaml",
        spec=spec,
        documents=[
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "synthetic", "namespace": "gco-jobs"},
                "spec": {
                    "parallelism": parallelism,
                    "template": {
                        "spec": {"containers": [{"name": "main", "resources": resources}]}
                    },
                },
            }
        ],
    )


class TestResourceGovernanceFit:
    def test_full_node_slice_fits_default_limit_range(self) -> None:
        findings = static_checks.check_resource_governance_fit(
            _parsed_job_example(
                {
                    "requests": {"cpu": "8", "memory": "64Gi", "nvidia.com/gpu": "8"},
                    "limits": {"cpu": "192", "memory": "2048Gi", "nvidia.com/gpu": "8"},
                },
                parallelism=2,
            )
        )
        assert findings
        assert all(finding.passed for finding in findings), [
            finding.detail for finding in findings if not finding.passed
        ]

    def test_container_over_limit_range_fails(self) -> None:
        findings = static_checks.check_resource_governance_fit(
            _parsed_job_example({"limits": {"nvidia.com/gpu": "16"}})
        )
        failures = [finding for finding in findings if not finding.passed]
        assert failures, "a 16-GPU container must fail the default 8-GPU ceiling"
        assert "container_max_gpu" in failures[0].detail

    def test_aggregate_over_namespace_quota_fails(self) -> None:
        findings = static_checks.check_resource_governance_fit(
            _parsed_job_example(
                {"requests": {"nvidia.com/gpu": "8"}},
                parallelism=5,  # 40 GPUs aggregate > 32 default quota
            )
        )
        failures = [finding for finding in findings if not finding.passed]
        assert failures
        assert "max_gpu" in failures[0].detail

    def test_other_namespaces_are_not_governed(self) -> None:
        parsed = _parsed_job_example({"limits": {"nvidia.com/gpu": "16"}})
        parsed.documents[0]["metadata"]["namespace"] = "gco-inference"
        assert static_checks.check_resource_governance_fit(parsed) == []


class TestJobAdmissionFailFast:
    @staticmethod
    def _kubectl(events_message: str):
        def kubectl(*args: str, timeout: int = 120, **_kwargs):
            if args[1] == "events":
                return 0, events_message, ""
            if args[1] == "job":
                return 0, '{"status": {"conditions": []}}', ""
            raise AssertionError(f"unexpected kubectl call: {args}")

        return kubectl

    def test_forbidden_pods_fail_immediately_with_the_event_reason(self) -> None:
        parsed = _parsed_job_example({"requests": {"cpu": "1"}})
        message = (
            'pods "synthetic-x" is forbidden: maximum nvidia.com/gpu usage per '
            "Container is 8, but limit is 16"
        )
        with pytest.raises(drivers.ExampleValidationError, match="rejected at admission"):
            drivers.wait_jobs_complete(parsed, self._kubectl(message), timeout=3600)

    def test_running_job_without_rejection_times_out_normally(self, monkeypatch) -> None:
        monkeypatch.setattr(drivers, "_POLL_SECONDS", 0)
        monkeypatch.setattr(
            drivers, "_pod_diagnostics", lambda *_args, **_kwargs: "synthetic=Pending"
        )
        parsed = _parsed_job_example({"requests": {"cpu": "1"}})
        with pytest.raises(drivers.ExampleValidationError, match="timeout after"):
            drivers.wait_jobs_complete(parsed, self._kubectl(""), timeout=0)

    def test_exceeded_quota_is_transient_and_never_fails_fast(self, monkeypatch) -> None:
        """Parallel submission fills the namespace quota transiently; the Job
        controller retries pod creation as peers finish, so a quota-shaped
        forbidden event must wait, not kill the example."""
        monkeypatch.setattr(drivers, "_POLL_SECONDS", 0)
        monkeypatch.setattr(
            drivers, "_pod_diagnostics", lambda *_args, **_kwargs: "synthetic=Pending"
        )
        parsed = _parsed_job_example({"requests": {"cpu": "1"}})
        message = (
            'pods "synthetic-x" is forbidden: exceeded quota: gco-jobs-quota, '
            "requested: requests.nvidia.com/gpu=16, used: requests.nvidia.com/gpu=20, "
            "limited: requests.nvidia.com/gpu=32"
        )
        with pytest.raises(drivers.ExampleValidationError, match="timeout after"):
            drivers.wait_jobs_complete(parsed, self._kubectl(message), timeout=0)

    def test_permanent_rejection_is_caught_even_alongside_quota_noise(self) -> None:
        """Each event message is evaluated separately: a transient quota event
        in the same stream must not mask a permanent LimitRange rejection."""
        parsed = _parsed_job_example({"requests": {"cpu": "1"}})
        events = (
            'pods "synthetic-x" is forbidden: exceeded quota: gco-jobs-quota, '
            "requested: requests.cpu=8, used: requests.cpu=396, limited: requests.cpu=400\n"
            'pods "synthetic-x" is forbidden: maximum nvidia.com/gpu usage per '
            "Container is 8, but limit is 16"
        )
        with pytest.raises(drivers.ExampleValidationError, match="rejected at admission"):
            drivers.wait_jobs_complete(parsed, self._kubectl(events), timeout=3600)


# ---------------------------------------------------------------------------
# Parallel example execution inside the examples action.
# ---------------------------------------------------------------------------


class TestParallelExamples:
    """The examples action runs pending examples concurrently and preserves
    registry order, checkpoint semantics, and failure reporting."""

    @staticmethod
    def _ctx(tmp_path: Path, selected: list[str], *, max_parallel: int = 0, prior=None):
        import threading
        from types import SimpleNamespace

        return SimpleNamespace(
            settings=SimpleNamespace(
                selected_examples=tuple(selected),
                max_parallel_examples=max_parallel,
                repo_root=tmp_path,
            ),
            deployment_regions=["us-east-1"],
            config=SimpleNamespace(project_name="gco"),
            checkpoint=SimpleNamespace(state={"examples": dict(prior or {})}),
            state_lock=threading.RLock(),
            persist=lambda: None,
        )

    @staticmethod
    def _fake_session(monkeypatch) -> None:
        import contextlib

        from scripts.example_job_validation import actions

        @contextlib.contextmanager
        def fake_session(_repo_root, _cluster, _region):
            yield lambda *args, **kwargs: (0, "", "")

        monkeypatch.setattr(actions.kube, "cluster_session", fake_session)

    def test_examples_overlap_and_summary_keeps_registry_order(self, monkeypatch, tmp_path):
        import threading

        from scripts.example_job_validation import actions

        names = sorted(EXAMPLE_SPECS)[:3]
        self._fake_session(monkeypatch)
        barrier = threading.Barrier(len(names), timeout=15)
        finished: list[str] = []

        def fake_run(_ctx, name, _region, _kubectl):
            # Every example must be inside its thread simultaneously for the
            # barrier to release; a serial loop would deadlock and break it.
            barrier.wait()
            finished.append(name)
            return drivers.ExampleRunResult(name=name, status="passed", submission="s")

        monkeypatch.setattr(actions, "_run_one_example", fake_run)
        ctx = self._ctx(tmp_path, names)
        summary = actions.action_examples(ctx)

        assert summary["passed"] == len(names)
        assert summary["max_parallel"] == len(names)
        assert [item["name"] for item in summary["results"]] == names
        assert sorted(ctx.checkpoint.state["examples"]) == names

    def test_max_parallel_one_runs_serially(self, monkeypatch, tmp_path):
        import threading

        from scripts.example_job_validation import actions

        names = sorted(EXAMPLE_SPECS)[:3]
        self._fake_session(monkeypatch)
        active = 0
        peak = 0
        gauge = threading.Lock()

        def fake_run(_ctx, name, _region, _kubectl):
            nonlocal active, peak
            with gauge:
                active += 1
                peak = max(peak, active)
            with gauge:
                active -= 1
            return drivers.ExampleRunResult(name=name, status="passed", submission="s")

        monkeypatch.setattr(actions, "_run_one_example", fake_run)
        summary = actions.action_examples(self._ctx(tmp_path, names, max_parallel=1))

        assert peak == 1
        assert summary["max_parallel"] == 1
        assert summary["passed"] == len(names)

    def test_checkpointed_passes_are_not_rerun(self, monkeypatch, tmp_path):
        from scripts.example_job_validation import actions

        names = sorted(EXAMPLE_SPECS)[:2]
        self._fake_session(monkeypatch)
        ran: list[str] = []

        def fake_run(_ctx, name, _region, _kubectl):
            ran.append(name)
            return drivers.ExampleRunResult(name=name, status="passed", submission="s")

        monkeypatch.setattr(actions, "_run_one_example", fake_run)
        prior = {names[0]: {"status": "passed", "submission": "s"}}
        summary = actions.action_examples(self._ctx(tmp_path, names, prior=prior))

        assert ran == [names[1]]
        assert summary["passed"] == 2
        assert [item["name"] for item in summary["results"]] == names

    def test_failed_examples_raise_with_their_names(self, monkeypatch, tmp_path):
        from scripts.example_job_validation import actions

        names = sorted(EXAMPLE_SPECS)[:2]
        self._fake_session(monkeypatch)

        def fake_run(_ctx, name, _region, _kubectl):
            status = "failed" if name == names[0] else "passed"
            return drivers.ExampleRunResult(name=name, status=status, submission="s")

        monkeypatch.setattr(actions, "_run_one_example", fake_run)
        with pytest.raises(RuntimeError, match=names[0]):
            actions.action_examples(self._ctx(tmp_path, names))

    def test_unexpected_crash_is_attributed_to_its_example(self, monkeypatch, tmp_path):
        from scripts.example_job_validation import actions

        names = sorted(EXAMPLE_SPECS)[:2]
        self._fake_session(monkeypatch)

        def fake_run(_ctx, name, _region, _kubectl):
            if name == names[1]:
                raise ValueError("boom")
            return drivers.ExampleRunResult(name=name, status="passed", submission="s")

        monkeypatch.setattr(actions, "_run_one_example", fake_run)
        with pytest.raises(RuntimeError, match=f"example {names[1]} crashed"):
            actions.action_examples(self._ctx(tmp_path, names))


class TestDagCleanup:
    """DAG examples are pipeline SPECS, not Kubernetes manifests: cleanup
    must delete the step manifests the DAG ran, never `kubectl delete -f`
    the spec file itself (kubectl cannot decode it — observed live in run
    ex241-4bf01801, where an otherwise-successful DAG failed at cleanup)."""

    def test_dag_cleanup_deletes_step_manifests_not_the_spec(self) -> None:
        from scripts.example_job_validation.static_checks import parse_example

        parsed = parse_example(REPO_ROOT, "pipeline-dag")
        deleted: list[str] = []

        def kubectl(*args: str, timeout: int = 120, **_kwargs):
            assert args[0] == "delete"
            deleted.append(args[2])
            return 0, "job.batch/x deleted", ""

        evidence = drivers.cleanup_example(parsed, parsed.path, kubectl)
        assert deleted, "DAG cleanup deleted nothing"
        assert all(path.endswith(".yaml") for path in deleted)
        assert not any(path.endswith("pipeline-dag.yaml") for path in deleted)
        step_names = {Path(path).stem for path in deleted}
        assert step_names == {"dag-step-preprocess", "dag-step-train"}
        assert evidence["deleted"] == ["job.batch/x deleted", "job.batch/x deleted"]

    def test_dag_cleanup_surfaces_step_delete_failures(self) -> None:
        from scripts.example_job_validation.static_checks import parse_example

        parsed = parse_example(REPO_ROOT, "pipeline-dag")

        def kubectl(*_args: str, timeout: int = 120, **_kwargs):
            return 1, "", "boom"

        with pytest.raises(drivers.ExampleValidationError, match="cleanup failed for pipeline-dag"):
            drivers.cleanup_example(parsed, parsed.path, kubectl)


# ---------------------------------------------------------------------------
# Setup drivers: registry pin, fail-closed dispatch, waiters, corpus revert.
# ---------------------------------------------------------------------------


class TestSetupDriverRegistry:
    def test_every_spec_driver_is_implemented(self) -> None:
        """A spec may only name drivers the dispatcher knows; anything else
        would fail at live runtime instead of in CI."""
        for name, spec in EXAMPLE_SPECS.items():
            if spec.setup_driver:
                assert spec.setup_driver in drivers.KNOWN_SETUP_DRIVERS, (
                    f"{name} names setup driver {spec.setup_driver!r} which is not in "
                    "drivers.KNOWN_SETUP_DRIVERS"
                )

    def test_unknown_driver_fails_closed_at_dispatch(self, monkeypatch, tmp_path) -> None:
        """An unimplemented driver name must fail the example loudly, never
        run without its precondition and report an unearned pass."""
        import dataclasses
        from types import SimpleNamespace

        from scripts.example_job_validation import actions

        bogus = dataclasses.replace(EXAMPLE_SPECS["simple-job"], setup_driver="bogus-driver")
        monkeypatch.setitem(actions.EXAMPLE_SPECS, "simple-job", bogus)
        ctx = SimpleNamespace(
            settings=SimpleNamespace(repo_root=REPO_ROOT, run_id="t"),
            session=None,
        )
        result = actions._run_one_example(
            ctx, "simple-job", "us-east-1", lambda *_a, **_k: (0, "", "")
        )
        assert result.status == "failed"
        assert "not implemented" in result.detail


class TestReadinessWaiters:
    """trainer-runtime-ready and mlflow-ready: pure waits, actionable errors."""

    @staticmethod
    def _kubectl(responses: dict[str, tuple[int, str, str]]):
        def kubectl(*args: str, timeout: int = 120, **_kwargs):
            for token, response in responses.items():
                if token in " ".join(args):
                    return response
            raise AssertionError(f"unexpected kubectl call: {args}")

        return kubectl

    def test_trainer_runtime_ready_returns_evidence(self) -> None:
        runtime = '{"metadata": {"name": "torch-distributed", "creationTimestamp": "2026-08-13T00:00:00Z"}}'
        kubectl = self._kubectl(
            {
                "get crd": (0, "", ""),
                "clustertrainingruntime": (0, runtime, ""),
            }
        )
        evidence = drivers.wait_trainer_runtime_ready(kubectl, timeout=5)
        assert evidence["runtime"] == "torch-distributed"
        assert evidence["crd"] == "trainjobs.trainer.kubeflow.org"

    def test_trainer_missing_crd_error_is_actionable(self, monkeypatch) -> None:
        monkeypatch.setattr(drivers, "_POLL_SECONDS", 0)
        kubectl = self._kubectl({"get crd": (1, "", "NotFound")})
        with pytest.raises(drivers.ExampleValidationError, match="helm.kubeflow_trainer"):
            drivers.wait_trainer_runtime_ready(kubectl, timeout=0)

    def test_trainer_missing_runtime_error_is_actionable(self, monkeypatch) -> None:
        monkeypatch.setattr(drivers, "_POLL_SECONDS", 0)
        kubectl = self._kubectl(
            {
                "get crd": (0, "", ""),
                "clustertrainingruntime": (1, "", "NotFound"),
            }
        )
        with pytest.raises(drivers.ExampleValidationError, match="torch-distributed"):
            drivers.wait_trainer_runtime_ready(kubectl, timeout=0)

    def test_mlflow_ready_returns_evidence(self) -> None:
        deployment = (
            '{"status": {"readyReplicas": 1, '
            '"conditions": [{"type": "Available", "status": "True"}]}}'
        )
        kubectl = self._kubectl({"get deployment mlflow": (0, deployment, "")})
        evidence = drivers.wait_mlflow_ready(kubectl, timeout=5)
        assert evidence == {"deployment": "monitoring/mlflow", "ready_replicas": 1}

    def test_mlflow_missing_error_is_actionable(self, monkeypatch) -> None:
        monkeypatch.setattr(drivers, "_POLL_SECONDS", 0)
        kubectl = self._kubectl({"get deployment mlflow": (1, "", "NotFound")})
        with pytest.raises(drivers.ExampleValidationError, match="cluster_observability.mlflow"):
            drivers.wait_mlflow_ready(kubectl, timeout=0)


class TestTrainJobWaiter:
    @staticmethod
    def _kubectl(payload: str, code: int = 0):
        def kubectl(*args: str, timeout: int = 120, **_kwargs):
            if args[1] == "trainjob":
                return code, payload, ""
            if args[0] == "describe":
                return 0, "describe-tail", ""
            raise AssertionError(f"unexpected kubectl call: {args}")

        return kubectl

    def _parsed(self):
        return static_checks.parse_example(REPO_ROOT, "kubeflow-trainjob")

    def test_complete_condition_returns_gang_evidence(self) -> None:
        import json as _json

        payload = _json.dumps(
            {
                "status": {
                    "conditions": [{"type": "Complete", "status": "True"}],
                    "jobsStatus": [{"name": "node", "succeeded": 2, "active": 0, "failed": 0}],
                }
            }
        )
        evidence = drivers.wait_trainjob_completes(
            self._parsed(), self._kubectl(payload), timeout=5
        )
        assert evidence["condition"] == "Complete"
        assert evidence["trainjob"] == "gco-jobs/kubeflow-trainjob-example"
        assert evidence["jobsStatus"][0]["succeeded"] == 2

    def test_failed_condition_raises_with_message(self) -> None:
        import json as _json

        payload = _json.dumps(
            {
                "status": {
                    "conditions": [
                        {"type": "Failed", "status": "True", "message": "backoff exceeded"}
                    ]
                }
            }
        )
        with pytest.raises(drivers.ExampleValidationError, match="backoff exceeded"):
            drivers.wait_trainjob_completes(self._parsed(), self._kubectl(payload), timeout=5)

    def test_false_terminal_condition_keeps_waiting_to_timeout(self, monkeypatch) -> None:
        import json as _json

        monkeypatch.setattr(drivers, "_POLL_SECONDS", 0)
        payload = _json.dumps({"status": {"conditions": [{"type": "Failed", "status": "False"}]}})
        with pytest.raises(drivers.ExampleValidationError, match="did not complete"):
            drivers.wait_trainjob_completes(self._parsed(), self._kubectl(payload), timeout=0)


class TestVectorDemoCorpusDriver:
    """The vector precondition ingests the documented way and reverts exactly."""

    def _driver(self, session=None):
        return drivers.VectorDemoCorpus(repo_root=REPO_ROOT, session=session, region="us-east-1")

    def test_create_runs_the_documented_command_and_records_keys(self, monkeypatch) -> None:
        import json as _json

        seen: dict[str, object] = {}

        def fake_run_cli(args, repo_root, timeout=600):
            seen["args"] = args
            seen["repo_root"] = repo_root
            seen["timeout"] = timeout
            return (
                0,
                _json.dumps(
                    {
                        "bucket": "gco-cluster-shared-x",
                        "uploaded": ["vector-corpus/a.md", "vector-corpus/b.md"],
                        "chunks_by_source": {"vector-corpus/a.md": 3},
                    }
                ),
                "",
            )

        monkeypatch.setattr(drivers, "_run_cli", fake_run_cli)
        driver = self._driver()
        evidence = driver.create()

        assert seen["args"] == [
            "gco",
            "vector",
            "ingest",
            "--demo",
            "--wait",
            "--output",
            "json",
        ]
        assert seen["repo_root"] == REPO_ROOT
        assert seen["timeout"] == 900
        assert driver.bucket == "gco-cluster-shared-x"
        assert driver.uploaded == ["vector-corpus/a.md", "vector-corpus/b.md"]
        assert evidence["command"] == "gco vector ingest --demo --wait"

    def test_create_failure_raises_with_output(self, monkeypatch) -> None:
        monkeypatch.setattr(drivers, "_run_cli", lambda *_a, **_k: (1, "", "no such feature"))
        with pytest.raises(drivers.ExampleValidationError, match="no such feature"):
            self._driver().create()

    def test_destroy_removes_exactly_the_recorded_corpus(self, monkeypatch) -> None:
        deleted_items: list[dict] = []
        deleted_objects: list[tuple[str, str]] = []

        class FakeDynamo:
            def __init__(self):
                self.scans = 0

            def scan(self, **kwargs):
                # Two pages for the first key to prove pagination; the doc_ids
                # are distinct per source key.
                source = kwargs["ExpressionAttributeValues"][":source"]["S"]
                if source == "vector-corpus/a.md":
                    self.scans += 1
                    if self.scans == 1:
                        return {
                            "Items": [{"doc_id": {"S": "a-0"}}],
                            "LastEvaluatedKey": {"doc_id": {"S": "a-0"}},
                        }
                    return {"Items": [{"doc_id": {"S": "a-1"}}]}
                return {"Items": [{"doc_id": {"S": "b-0"}}]}

            def batch_write_item(self, RequestItems):
                deleted_items.append(RequestItems)
                return {}

        class FakeS3:
            def delete_object(self, Bucket, Key):
                deleted_objects.append((Bucket, Key))

        fake_dynamo = FakeDynamo()

        class FakeSession:
            def client(self, service, region_name=None):
                return fake_dynamo if service == "dynamodb" else FakeS3()

        class FakeClient:
            def __init__(self, query_region=None):
                pass

            def _resolve_table_name(self):
                return "gco-vector-store"

            def _resolve_bucket(self):
                return "gco-cluster-shared-x", "us-east-2"

        import cli.vector_store as vector_store_module

        monkeypatch.setattr(vector_store_module, "VectorStoreClient", FakeClient)

        driver = self._driver(session=FakeSession())
        driver.bucket = "gco-cluster-shared-x"
        driver.uploaded = ["vector-corpus/a.md", "vector-corpus/b.md"]
        driver.destroy()

        doc_ids = [
            request["DeleteRequest"]["Key"]["doc_id"]["S"]
            for batch in deleted_items
            for request in batch["gco-vector-store"]
        ]
        assert sorted(doc_ids) == ["a-0", "a-1", "b-0"]
        assert deleted_objects == [
            ("gco-cluster-shared-x", "vector-corpus/a.md"),
            ("gco-cluster-shared-x", "vector-corpus/b.md"),
        ]

    def test_destroy_refuses_a_changed_bucket(self, monkeypatch) -> None:
        class FakeClient:
            def __init__(self, query_region=None):
                pass

            def _resolve_table_name(self):
                return "gco-vector-store"

            def _resolve_bucket(self):
                return "some-other-bucket", "us-east-2"

        import cli.vector_store as vector_store_module

        monkeypatch.setattr(vector_store_module, "VectorStoreClient", FakeClient)

        driver = self._driver(session=None)
        driver.bucket = "gco-cluster-shared-x"
        driver.uploaded = ["vector-corpus/a.md"]
        with pytest.raises(drivers.ExampleValidationError, match="refusing to delete"):
            driver.destroy()

    def test_destroy_is_a_noop_before_create(self) -> None:
        # No AWS clients are constructed when nothing was uploaded.
        self._driver(session=None).destroy()


# ---------------------------------------------------------------------------
# CLI entry point: argument validation, settings derivation, and main() driven
# entirely through argv with the runner and the local-execution guard faked.
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path: Path) -> Path:
    """A directory that satisfies repository_root() without touching git."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "cdk.json").write_text("{}", encoding="utf-8")
    return root


_IDENTITY_ARGS = [
    "--expected-account",
    "1" * 12,
    "--expected-sha",
    "A" * 40,
    "--expected-branch",
    "  main  ",
]


class TestSplitNames:
    def test_deduplicates_and_strips(self) -> None:
        from scripts.example_job_validation.__main__ import _split_names

        assert _split_names(" a, b ,a,,c") == ("a", "b", "c")

    def test_empty_selection_is_rejected(self) -> None:
        import argparse

        from scripts.example_job_validation.__main__ import _split_names

        with pytest.raises(argparse.ArgumentTypeError, match="at least one name"):
            _split_names(" , ")


class TestSelectExamples:
    def test_unknown_names_are_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        from scripts.example_job_validation.__main__ import _build_parser, _select_examples

        parser = _build_parser()
        args = parser.parse_args(["--examples", "simple-job,zzz", "--skip-examples", "aaa"])
        with pytest.raises(SystemExit) as excinfo:
            _select_examples(parser, args)
        assert excinfo.value.code == 2
        assert "Unknown example name(s): aaa, zzz" in capsys.readouterr().err

    def test_epilog_lists_actions_and_examples(self) -> None:
        from scripts.example_job_validation.__main__ import _build_parser

        epilog = _build_parser().epilog or ""
        assert "Actions: preflight, static, baseline, deploy, examples, destroy" in epilog
        assert ", ".join(sorted(EXAMPLE_SPECS)) in epilog


class TestSettingsFromArgs:
    @staticmethod
    def _parser(monkeypatch):
        from scripts.example_job_validation.__main__ import _build_parser

        for name in (
            "GCO_LIVE_EXPECTED_ACCOUNT",
            "GCO_LIVE_EXPECTED_SHA",
            "GCO_LIVE_EXPECTED_BRANCH",
        ):
            monkeypatch.delenv(name, raising=False)
        return _build_parser()

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            ([], "--expected-account must be an exact 12-digit"),
            (["--expected-account", "12345"], "--expected-account must be an exact 12-digit"),
            (["--expected-account", "1" * 12], "--expected-sha must be an exact 40-character"),
            (
                ["--expected-account", "1" * 12, "--expected-sha", "xyz"],
                "--expected-sha must be an exact 40-character",
            ),
            (
                ["--expected-account", "1" * 12, "--expected-sha", "a" * 40],
                "--expected-branch is required",
            ),
            (
                [
                    "--expected-account",
                    "1" * 12,
                    "--expected-sha",
                    "a" * 40,
                    "--expected-branch",
                    "   ",
                ],
                "--expected-branch is required",
            ),
            ([*_IDENTITY_ARGS, "--run-id", "bad/slash"], "--run-id must be 1-80 safe"),
            ([*_IDENTITY_ARGS, "--run-id", "x" * 81], "--run-id must be 1-80 safe"),
            ([*_IDENTITY_ARGS, "--max-parallel", "-1"], "--max-parallel must be >= 0"),
        ],
    )
    def test_identity_and_shape_errors(
        self, monkeypatch, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
    ) -> None:
        from scripts.example_job_validation.__main__ import _settings_from_args

        parser = self._parser(monkeypatch)
        args = parser.parse_args(argv)
        with pytest.raises(SystemExit) as excinfo:
            _settings_from_args(parser, args)
        assert excinfo.value.code == 2
        assert message in capsys.readouterr().err

    def test_defaults_derive_run_id_and_paths_from_the_checkout(self, monkeypatch, tmp_path):
        import re

        from scripts.example_job_validation.__main__ import _settings_from_args

        root = _fake_repo(tmp_path)
        parser = self._parser(monkeypatch)
        args = parser.parse_args(
            [*_IDENTITY_ARGS, "--repo-root", str(root), "--examples", "simple-job,gpu-job"]
        )
        settings = _settings_from_args(parser, args)

        assert re.fullmatch(r"\d{8}T\d{6}Z-a{12}", settings.run_id), settings.run_id
        assert settings.repo_root == root.resolve()
        assert settings.report_dir == root.resolve() / ".example-job-validation" / settings.run_id
        assert settings.checkpoint_path == settings.report_dir / "checkpoint.json"
        assert settings.expected_sha == "a" * 40  # lower-cased
        assert settings.expected_branch == "main"  # stripped
        assert settings.profile == "configured"
        assert settings.requested_actions == ("all",)
        assert settings.protected_stack_names == ("CDKToolkit", "GCOGitHubOIDCStack")
        assert settings.confirm_kms_key_deletion is False
        assert settings.resume is False
        assert settings.selected_examples == ("simple-job", "gpu-job")
        assert settings.max_parallel_examples == 0

    def test_explicit_run_paths_and_flags_are_honored(self, monkeypatch, tmp_path):
        from scripts.example_job_validation.__main__ import _settings_from_args

        root = _fake_repo(tmp_path)
        parser = self._parser(monkeypatch)
        args = parser.parse_args(
            [
                *_IDENTITY_ARGS,
                "--repo-root",
                str(root),
                "--run-id",
                "ex.1_run-A",
                "--report-dir",
                "reports/run-a",
                "--checkpoint",
                str(tmp_path / "elsewhere" / "cp.json"),
                "--protected-stack",
                "Extra",
                "--protected-stack",
                "CDKToolkit",
                "--confirm-kms-key-deletion",
                "--resume",
                "--actions",
                "static,deploy",
                "--max-parallel",
                "2",
                "--skip-examples",
                "pipeline-dag",
            ]
        )
        # A checkpoint outside the report directory violates the settings
        # contract, which surfaces as the model's own ValueError.
        with pytest.raises(ValueError, match="direct child of the report directory"):
            _settings_from_args(parser, args)

        args.checkpoint = "reports/run-a/cp.json"
        settings = _settings_from_args(parser, args)
        assert settings.run_id == "ex.1_run-A"
        assert settings.report_dir == root.resolve() / "reports" / "run-a"
        assert settings.checkpoint_path == settings.report_dir / "cp.json"
        assert settings.protected_stack_names == ("CDKToolkit", "GCOGitHubOIDCStack", "Extra")
        assert settings.confirm_kms_key_deletion is True
        assert settings.resume is True
        assert settings.requested_actions == ("static", "deploy")
        assert settings.max_parallel_examples == 2
        assert "pipeline-dag" not in settings.selected_examples
        assert len(settings.selected_examples) == len(EXAMPLE_SPECS) - 1


class TestMainEntryPoint:
    """main() end to end: every argv shape, with the runner and guard faked."""

    @staticmethod
    def _argv(monkeypatch, *args: str) -> None:
        import sys

        monkeypatch.setattr(sys, "argv", ["prog", *args])

    @staticmethod
    def _main_module():
        import scripts.example_job_validation.__main__ as main_module

        return main_module

    def test_list_actions_prints_registry_and_exits_zero(self, monkeypatch, capsys) -> None:
        self._argv(monkeypatch, "--list-actions")
        assert self._main_module().main() == 0
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert [line.split()[0] for line in lines] == list(build_action_registry())
        assert "preflight" in lines[0] and "[depends: none]" in lines[0]
        assert "[depends: deploy]" in next(line for line in lines if line.startswith("examples"))

    def test_static_only_reports_failures_and_exits_one(self, monkeypatch, capsys) -> None:
        main_module = self._main_module()
        findings = [
            static_checks.StaticFinding(example="*", check="spec/file symmetry", passed=True),
            static_checks.StaticFinding(
                example="simple-job", check="spec shape", passed=False, detail="bad path"
            ),
        ]
        seen: dict[str, object] = {}

        def fake_static(root, names):
            seen["root"] = root
            seen["names"] = names
            return findings

        monkeypatch.setattr(main_module, "run_static_checks", fake_static)
        self._argv(
            monkeypatch, "--static-only", "--repo-root", str(REPO_ROOT), "--examples", "simple-job"
        )
        assert main_module.main() == 1
        out = capsys.readouterr().out
        assert seen == {"root": REPO_ROOT, "names": ["simple-job"]}
        assert "[ok ] *: spec/file symmetry" in out
        assert "[FAIL] simple-job: spec shape — bad path" in out
        assert "2 checks, 1 failed" in out

    def test_refuses_to_start_when_local_execution_guard_rejects(self, monkeypatch, capsys) -> None:
        main_module = self._main_module()

        def guard() -> None:
            raise RuntimeError("must not run in GitHub Actions")

        monkeypatch.setattr(main_module, "require_local_execution", guard)
        monkeypatch.setattr(
            main_module,
            "LiveValidationRunner",
            lambda *a, **k: pytest.fail("runner must not be built"),
        )
        self._argv(monkeypatch, *_IDENTITY_ARGS)
        assert main_module.main() == 1
        err = capsys.readouterr().err
        assert "Example validation could not start: must not run in GitHub Actions" in err

    def test_live_run_builds_runner_with_example_registry_and_report_identity(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        from types import SimpleNamespace

        main_module = self._main_module()
        root = _fake_repo(tmp_path)
        built: list[object] = []

        class FakeRunner:
            def __init__(self, settings, registry=None):
                self.settings = settings
                self.registry = registry
                self.report = SimpleNamespace(title="", report_stem="")
                built.append(self)

            def run(self) -> int:
                return 3

        monkeypatch.setattr(main_module, "require_local_execution", lambda: None)
        monkeypatch.setattr(main_module, "LiveValidationRunner", FakeRunner)
        self._argv(
            monkeypatch,
            *_IDENTITY_ARGS,
            "--repo-root",
            str(root),
            "--report-dir",
            str(tmp_path / "report"),
            "--examples",
            "simple-job",
            "--max-parallel",
            "1",
        )
        assert main_module.main() == 3

        (runner,) = built
        assert runner.report.title == "GCO Example Job Validation"
        assert runner.report.report_stem == "example-job-validation"
        assert list(runner.registry) == list(build_action_registry())
        assert runner.settings.selected_examples == ("simple-job",)
        assert runner.settings.max_parallel_examples == 1
        assert runner.settings.report_dir == tmp_path / "report"
        assert capsys.readouterr().err == ""

    def test_usage_errors_inside_main_become_exit_one_without_a_report(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        main_module = self._main_module()
        monkeypatch.setattr(main_module, "require_local_execution", lambda: None)
        monkeypatch.setattr(
            main_module,
            "LiveValidationRunner",
            lambda *a, **k: pytest.fail("runner must not be built"),
        )
        self._argv(
            monkeypatch,
            "--expected-account",
            "12",
            "--report-dir",
            str(tmp_path / "report"),
        )
        assert main_module.main() == 1
        err = capsys.readouterr().err
        assert "--expected-account must be an exact 12-digit" in err
        assert "Example validation could not start: SystemExit: 2" in err
        assert not (tmp_path / "report").exists()

    def test_keyboard_interrupt_before_runner_is_exit_130(self, monkeypatch, tmp_path, capsys):
        main_module = self._main_module()
        root = _fake_repo(tmp_path)

        def interrupted(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(main_module, "require_local_execution", lambda: None)
        monkeypatch.setattr(main_module, "LiveValidationRunner", interrupted)
        self._argv(
            monkeypatch,
            *_IDENTITY_ARGS,
            "--repo-root",
            str(root),
            "--report-dir",
            str(tmp_path / "report"),
        )
        assert main_module.main() == 130
        assert "interrupted before the runner initialized" in capsys.readouterr().err
        assert not (tmp_path / "report").exists()

    def test_runner_startup_failure_writes_a_failed_report(self, monkeypatch, tmp_path, capsys):
        import json

        main_module = self._main_module()
        root = _fake_repo(tmp_path)

        def exploding(*_args, **_kwargs):
            raise RuntimeError("cdk context unreadable")

        monkeypatch.setattr(main_module, "require_local_execution", lambda: None)
        monkeypatch.setattr(main_module, "LiveValidationRunner", exploding)
        report_dir = tmp_path / "report"
        self._argv(
            monkeypatch,
            *_IDENTITY_ARGS,
            "--repo-root",
            str(root),
            "--run-id",
            "run-7",
            "--report-dir",
            str(report_dir),
            "--actions",
            "static",
            "--examples",
            "simple-job",
        )
        assert main_module.main() == 1
        err = capsys.readouterr().err
        assert "could not start: RuntimeError: cdk context unreadable" in err

        payload = json.loads(
            (report_dir / "example-job-validation.json").read_text(encoding="utf-8")
        )
        assert payload["run_id"] == "run-7"
        assert payload["status"] == "failed"
        assert payload["title"] == "GCO Example Job Validation"
        assert payload["report_stem"] == "example-job-validation"
        assert payload["selected_actions"] == ["static"]
        assert payload["identity"]["selected_examples"] == ["simple-job"]
        assert "RuntimeError: cdk context unreadable" in payload["fatal_error"]
        markdown = (report_dir / "example-job-validation.md").read_text(encoding="utf-8")
        assert markdown.startswith("# GCO Example Job Validation")
        assert "**FAILED**" in markdown


# ---------------------------------------------------------------------------
# Static checks: parse guard, catalog literal reading, and workload shapes.
# ---------------------------------------------------------------------------


class TestStaticCheckEdges:
    def test_parse_example_rejects_names_without_a_spec(self) -> None:
        with pytest.raises(KeyError, match="No validation spec for example 'nope'"):
            static_checks.parse_example(REPO_ROOT, "nope")

    @staticmethod
    def _catalog_root(tmp_path: Path, source: str) -> Path:
        docs = tmp_path / "gco_mcp" / "resources"
        docs.mkdir(parents=True)
        (docs / "docs.py").write_text(source, encoding="utf-8")
        return tmp_path

    def test_catalog_literal_is_read_without_importing(self, tmp_path: Path) -> None:
        root = self._catalog_root(
            tmp_path,
            "import sys\nsys.exit('must not execute')\n"
            "OTHER = 1\n"
            'EXAMPLE_METADATA: dict[str, dict] = {"simple-job": {"submission": "gco jobs submit-sqs"}}\n',
        )
        assert static_checks._catalog_metadata(root) == {
            "simple-job": {"submission": "gco jobs submit-sqs"}
        }

    def test_catalog_must_be_a_dict_literal(self, tmp_path: Path) -> None:
        root = self._catalog_root(tmp_path, "EXAMPLE_METADATA = ['simple-job']\n")
        with pytest.raises(RuntimeError, match="not a dict literal"):
            static_checks._catalog_metadata(root)

    def test_missing_catalog_literal_is_reported(self, tmp_path: Path) -> None:
        root = self._catalog_root(tmp_path, "SOMETHING_ELSE = {}\nx: int = 1\n")
        with pytest.raises(RuntimeError, match="EXAMPLE_METADATA literal not found"):
            static_checks._catalog_metadata(root)

    def test_pod_spec_extraction_per_workload_kind(self) -> None:
        template = {"spec": {"containers": [{"name": "c"}]}}
        assert static_checks._pod_spec_and_parallelism(
            {"spec": {"replicas": 3, "template": template}}, "Deployment"
        ) == (template["spec"], 3)
        assert static_checks._pod_spec_and_parallelism(
            {"spec": {"template": template}}, "StatefulSet"
        ) == (template["spec"], 1)
        assert static_checks._pod_spec_and_parallelism(
            {"spec": {"containers": [{"name": "c"}]}}, "Pod"
        ) == ({"containers": [{"name": "c"}]}, 1)
        assert static_checks._pod_spec_and_parallelism({}, "Pod") == (None, 1)
        assert static_checks._pod_spec_and_parallelism({"spec": {}}, "Service") == (None, 1)

    def test_deployment_replicas_multiply_into_the_namespace_quota(self) -> None:
        parsed = _parsed_job_example({"requests": {"nvidia.com/gpu": "8"}})
        parsed.documents[0] = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "serving", "namespace": "gco-jobs"},
            "spec": {
                "replicas": 5,  # 40 GPUs aggregate > 32 default quota
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "main", "resources": {"requests": {"nvidia.com/gpu": "8"}}}
                        ]
                    }
                },
            },
        }
        findings = static_checks.check_resource_governance_fit(parsed)
        failures = [finding for finding in findings if not finding.passed]
        # gpu-job travels the SQS path, so the front-door manifest cap is
        # checked against the same 5x aggregate as the namespace quota.
        assert [finding.check for finding in failures] == [
            "ResourceQuota fit (Deployment/serving: 5x pod requests.nvidia.com/gpu)",
            "manifest-cap fit (Deployment/serving: 5x pod requests.nvidia.com/gpu)",
        ]
        assert all("aggregate 40 exceeds" in finding.detail for finding in failures)

    def test_run_static_checks_skips_names_without_a_spec(self, monkeypatch) -> None:
        """A name the shape check tolerates but the registry lacks yields no
        further findings and is never parsed (parse_example would raise)."""
        monkeypatch.setattr(
            static_checks,
            "check_spec_shape",
            lambda name: static_checks.StaticFinding(
                example=name, check="spec shape", passed=name in EXAMPLE_SPECS
            ),
        )
        monkeypatch.setattr(
            static_checks,
            "parse_example",
            lambda _root, name: pytest.fail(f"{name} must not be parsed"),
        )
        findings = static_checks.run_static_checks(REPO_ROOT, ["unregistered"])
        symmetry = static_checks.check_registry_symmetry(REPO_ROOT)
        assert [finding.check for finding in findings] == [
            *[finding.check for finding in symmetry],
            "spec shape",
        ]
        assert findings[-1].example == "unregistered"
        assert findings[-1].passed is False


# ---------------------------------------------------------------------------
# Drivers: submission paths, criteria waiters, cleanup, and setup drivers,
# with kubectl scripted, the CLI faked, and the clock under test control.
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic ``time.monotonic``/``time.sleep`` for the driver waiters."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _install_clock(monkeypatch) -> _FakeClock:
    from types import SimpleNamespace

    clock = _FakeClock()
    monkeypatch.setattr(
        drivers, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    return clock


class _ScriptedKubectl:
    """kubectl stand-in routed by argument prefix; records every invocation.

    A route value is one ``(code, stdout, stderr)`` tuple, or a list of them
    consumed in order (the last one repeats once exhausted).
    """

    def __init__(self, routes: dict[tuple[str, ...], object]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    def __call__(self, *args: str, timeout: int = 120, **kwargs):
        self.calls.append((args, {"timeout": timeout, **kwargs}))
        for prefix, response in self.routes.items():
            if args[: len(prefix)] == prefix:
                if isinstance(response, list):
                    return response.pop(0) if len(response) > 1 else response[0]
                return response
        raise AssertionError(f"unexpected kubectl call: {args}")

    def commands(self, *prefix: str) -> list[tuple[str, ...]]:
        return [args for args, _ in self.calls if args[: len(prefix)] == prefix]


def _synthetic_parsed(name: str, spec, documents: list[dict]) -> static_checks.ParsedExample:
    return static_checks.ParsedExample(
        name=name, path=REPO_ROOT / "examples" / f"{name}.yaml", spec=spec, documents=documents
    )


class TestRunCli:
    def test_runs_in_the_checkout_with_captured_text_output(self, monkeypatch, tmp_path):
        import subprocess

        seen: dict[str, object] = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen.update(kwargs)
            return subprocess.CompletedProcess(args, 3, stdout="queued\n", stderr="warn")

        monkeypatch.setattr(drivers.subprocess, "run", fake_run)
        assert drivers._run_cli(["gco", "jobs", "submit"], tmp_path, timeout=7) == (
            3,
            "queued\n",
            "warn",
        )
        assert seen == {
            "args": ["gco", "jobs", "submit"],
            "cwd": tmp_path,
            "capture_output": True,
            "text": True,
            "timeout": 7,
        }


class TestMutationChannels:
    @staticmethod
    def _deployment(args: list[str]) -> dict:
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "srv", "namespace": "gco-inference"},
            "spec": {"template": {"spec": {"containers": [{"name": "srv", "args": list(args)}]}}},
        }

    def test_args_channel_replaces_the_value_following_the_flag(self, monkeypatch, tmp_path):
        import dataclasses

        import yaml

        monkeypatch.setattr(drivers.tempfile, "tempdir", str(tmp_path))
        spec = dataclasses.replace(
            EXAMPLE_SPECS["inference-sglang"],
            mutations={
                "Deployment.args.--model-path": "facebook/opt-125m",
                "Deployment.args.--trust-remote-code": "ignored: flag is last",
            },
        )
        parsed = _synthetic_parsed(
            "synthetic-args",
            spec,
            [
                {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "srv"}},
                self._deployment(
                    ["--model-path", "meta/gated", "--port", "8000", "--trust-remote-code"]
                ),
            ],
        )
        path, disclosed = drivers.apply_mutations(parsed)
        assert path.parent == tmp_path
        assert path.name.endswith("-synthetic-args.yaml")
        assert disclosed == spec.mutations
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        assert documents[0]["kind"] == "Service"
        assert documents[1]["spec"]["template"]["spec"]["containers"][0]["args"] == [
            "--model-path",
            "facebook/opt-125m",
            "--port",
            "8000",
            "--trust-remote-code",
        ]

    def test_unknown_channel_is_a_spec_bug(self) -> None:
        import dataclasses

        spec = dataclasses.replace(
            EXAMPLE_SPECS["inference-sglang"], mutations={"Deployment.volumes.MODEL": "x"}
        )
        parsed = _synthetic_parsed("synthetic-bad", spec, [self._deployment([])])
        with pytest.raises(
            ValueError, match="Unsupported mutation channel in 'Deployment.volumes.MODEL'"
        ):
            drivers.apply_mutations(parsed)


class TestSubmitExample:
    @staticmethod
    def _parsed_for(submission: str) -> static_checks.ParsedExample:
        import dataclasses

        spec = dataclasses.replace(EXAMPLE_SPECS["simple-job"], submission=submission)
        return _synthetic_parsed("simple-job", spec, [])

    @pytest.mark.parametrize(
        ("submission", "argv", "timeout"),
        [
            (
                drivers.SUBMIT_DIRECT,
                ["gco", "jobs", "submit-direct", "MANIFEST", "-r", "us-east-1"],
                600,
            ),
            (
                drivers.SUBMIT_SQS,
                ["gco", "jobs", "submit-sqs", "MANIFEST", "--region", "us-east-1"],
                600,
            ),
            (
                drivers.SUBMIT_API,
                ["gco", "jobs", "submit", "MANIFEST", "--region", "us-east-1"],
                600,
            ),
            (drivers.DAG_RUN, ["gco", "dag", "run", "MANIFEST", "-r", "us-east-1"], 1800),
        ],
    )
    def test_cli_paths_run_the_documented_command(
        self, monkeypatch, tmp_path, submission: str, argv: list[str], timeout: int
    ) -> None:
        seen: dict[str, object] = {}

        def fake_run_cli(args, repo_root, timeout=600):
            seen["args"] = args
            seen["repo_root"] = repo_root
            seen["timeout"] = timeout
            return 0, "x" * 2000 + "submitted", ""

        monkeypatch.setattr(drivers, "_run_cli", fake_run_cli)
        manifest = tmp_path / "m.yaml"
        evidence = drivers.submit_example(
            self._parsed_for(submission),
            manifest,
            repo_root=REPO_ROOT,
            region="us-east-1",
            kubectl=lambda *a, **k: pytest.fail("kubectl must not be used for CLI paths"),
        )
        assert seen["args"] == [str(manifest) if item == "MANIFEST" else item for item in argv]
        assert seen["repo_root"] == REPO_ROOT
        assert seen["timeout"] == timeout
        assert evidence["command"] == " ".join(argv[:3]) + " examples/simple-job.yaml"
        assert len(evidence["output"]) == 1500 and evidence["output"].endswith("submitted")

    def test_cli_failure_reports_stdout_when_stderr_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(drivers, "_run_cli", lambda *_a, **_k: (4, "  queue missing  ", ""))
        with pytest.raises(
            drivers.ExampleValidationError,
            match=r"gco jobs submit-sqs failed \(exit 4\): queue missing$",
        ):
            drivers.submit_example(
                self._parsed_for(drivers.SUBMIT_SQS),
                tmp_path / "m.yaml",
                repo_root=REPO_ROOT,
                region="us-east-1",
                kubectl=lambda *a, **k: (0, "", ""),
            )

    def test_kubectl_apply_path(self, tmp_path) -> None:
        kubectl = _ScriptedKubectl({("apply",): (0, "job.batch/x created\n", "")})
        evidence = drivers.submit_example(
            self._parsed_for(drivers.KUBECTL_APPLY),
            tmp_path / "m.yaml",
            repo_root=REPO_ROOT,
            region="us-east-1",
            kubectl=kubectl,
        )
        assert kubectl.calls == [(("apply", "-f", str(tmp_path / "m.yaml")), {"timeout": 120})]
        assert evidence == {
            "command": "kubectl apply -f examples/simple-job.yaml",
            "output": "job.batch/x created",
        }

    def test_kubectl_apply_failure(self, tmp_path) -> None:
        kubectl = _ScriptedKubectl({("apply",): (1, "", "error: unable to recognize\n")})
        with pytest.raises(
            drivers.ExampleValidationError, match="kubectl apply failed: error: unable to recognize"
        ):
            drivers.submit_example(
                self._parsed_for(drivers.KUBECTL_APPLY),
                tmp_path / "m.yaml",
                repo_root=REPO_ROOT,
                region="us-east-1",
                kubectl=kubectl,
            )

    def test_companion_artifacts_have_no_live_submission(self, tmp_path) -> None:
        with pytest.raises(
            drivers.ExampleValidationError, match="No live submission for companion-artifact"
        ):
            drivers.submit_example(
                self._parsed_for(COMPANION),
                tmp_path / "m.yaml",
                repo_root=REPO_ROOT,
                region="us-east-1",
                kubectl=lambda *a, **k: (0, "", ""),
            )


class TestJobStatus:
    @staticmethod
    def _status(code: int, payload: str) -> tuple[str, str]:
        kubectl = _ScriptedKubectl({("get", "job"): (code, payload, "")})
        return drivers._job_status(kubectl, "gco-jobs", "j")

    def test_missing_job(self) -> None:
        assert self._status(1, "") == ("missing", "")

    def test_complete_job(self) -> None:
        payload = '{"status": {"conditions": [{"type": "Complete", "status": "True"}]}}'
        assert self._status(0, payload) == ("complete", "")

    def test_failed_job_carries_the_condition_message(self) -> None:
        payload = (
            '{"status": {"conditions": [{"type": "Complete", "status": "False"}, '
            '{"type": "Failed", "status": "True", "message": "BackoffLimitExceeded"}]}}'
        )
        assert self._status(0, payload) == ("failed", "BackoffLimitExceeded")

    def test_null_conditions_mean_running(self) -> None:
        assert self._status(0, '{"status": {"conditions": null}}') == ("running", "")


class TestWaitJobsComplete:
    _COMPLETE = '{"status": {"conditions": [{"type": "Complete", "status": "True"}]}}'
    _RUNNING = '{"status": {"active": 1}}'
    _FAILED = (
        '{"status": {"conditions": [{"type": "Failed", "status": "True", '
        '"message": "BackoffLimitExceeded"}]}}'
    )

    @staticmethod
    def _two_jobs() -> static_checks.ParsedExample:
        parsed = static_checks.parse_example(REPO_ROOT, "sqs-job-submission")
        assert len([doc for doc in parsed.documents if doc["kind"] == "Job"]) == 2
        return parsed

    def test_no_jobs_in_file_is_a_spec_error(self) -> None:
        parsed = _synthetic_parsed(
            "svc-only",
            EXAMPLE_SPECS["simple-job"],
            [{"kind": "Service", "metadata": {"name": "s", "namespace": "gco-jobs"}}],
        )
        with pytest.raises(drivers.ExampleValidationError, match="defines no Jobs"):
            drivers.wait_jobs_complete(parsed, lambda *a, **k: (0, "", ""), timeout=10)

    def test_polls_until_every_job_completes(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        parsed = self._two_jobs()
        names = [doc["metadata"]["name"] for doc in parsed.documents if doc["kind"] == "Job"]
        kubectl = _ScriptedKubectl(
            {
                ("get", "job", names[0]): (0, self._COMPLETE, ""),
                ("get", "job", names[1]): [(0, self._RUNNING, ""), (0, self._COMPLETE, "")],
                # An events lookup failure is not a rejection; quota-only
                # FailedCreate events are transient and never fail fast.
                ("get", "events"): [
                    (1, "", "events unavailable"),
                    (0, 'pods "x" is forbidden: exceeded quota: gco-jobs-quota\n', ""),
                ],
            }
        )
        evidence = drivers.wait_jobs_complete(parsed, kubectl, timeout=600)
        assert evidence == {"jobs": {f"gco-jobs/{name}": "complete" for name in names}}
        assert clock.sleeps == [drivers._POLL_SECONDS]
        assert len(kubectl.commands("get", "job")) == 4
        events = kubectl.commands("get", "events")
        assert len(events) == 4
        assert events[1][2:6] == (
            "-n",
            "gco-jobs",
            "--field-selector",
            f"involvedObject.kind=Job,involvedObject.name={names[1]},reason=FailedCreate",
        )

    def test_failed_job_surfaces_condition_and_log_tail(self) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "simple-job")
        kubectl = _ScriptedKubectl(
            {
                ("get", "job"): (0, self._FAILED, ""),
                ("logs",): (0, "step 1\nTraceback: boom\n", ""),
            }
        )
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers.wait_jobs_complete(parsed, kubectl, timeout=600)
        message = str(excinfo.value)
        assert "Job gco-jobs/hello-gco failed: BackoffLimitExceeded" in message
        assert "last logs: step 1\nTraceback: boom" in message
        (logs_call,) = kubectl.calls[1:]
        assert logs_call == (
            ("logs", "job/hello-gco", "-n", "gco-jobs", "--tail", "40"),
            {"timeout": 60},
        )

    def test_timeout_lists_states_and_pod_phases(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        parsed = static_checks.parse_example(REPO_ROOT, "simple-job")
        kubectl = _ScriptedKubectl(
            {
                ("get", "job"): (0, self._RUNNING, ""),
                ("get", "events"): (0, "", ""),
                ("get", "pods"): (0, "hello-gco-abc=Pending \n", ""),
            }
        )
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers.wait_jobs_complete(parsed, kubectl, timeout=2 * drivers._POLL_SECONDS)
        assert str(excinfo.value) == (
            f"timeout after {2 * drivers._POLL_SECONDS}s: gco-jobs/hello-gco=running; "
            "pods: hello-gco-abc=Pending"
        )
        assert clock.sleeps == [drivers._POLL_SECONDS, drivers._POLL_SECONDS]
        (pods_call,) = kubectl.commands("get", "pods")
        assert pods_call[2:6] == ("-n", "gco-jobs", "-l", "job-name=hello-gco")


class TestWaitDeploymentAvailable:
    @staticmethod
    def _parsed(*, with_service: bool = True) -> static_checks.ParsedExample:
        parsed = static_checks.parse_example(REPO_ROOT, "inference-vllm")
        if not with_service:
            parsed.documents = [doc for doc in parsed.documents if doc["kind"] != "Service"]
        return parsed

    def test_available_with_service_endpoints(self) -> None:
        kubectl = _ScriptedKubectl(
            {
                ("wait",): (0, "deployment.apps/vllm-llama3 condition met", ""),
                ("get", "endpoints"): (0, "10.0.1.5 10.0.2.9\n", ""),
            }
        )
        evidence = drivers.wait_deployment_available(self._parsed(), kubectl, timeout=900)
        assert evidence == {
            "deployment": "gco-inference/vllm-llama3=Available",
            "service_endpoints": "10.0.1.5 10.0.2.9",
        }
        wait_args, wait_kwargs = kubectl.calls[0]
        assert wait_args == (
            "wait",
            "deployment/vllm-llama3",
            "-n",
            "gco-inference",
            "--for",
            "condition=Available",
            "--timeout=900s",
        )
        assert wait_kwargs == {"timeout": 960}
        assert kubectl.calls[1][0][:5] == ("get", "endpoints", "vllm-llama3", "-n", "gco-inference")

    def test_available_without_a_service_skips_the_endpoint_check(self) -> None:
        kubectl = _ScriptedKubectl({("wait",): (0, "", "")})
        evidence = drivers.wait_deployment_available(
            self._parsed(with_service=False), kubectl, timeout=60
        )
        assert evidence == {"deployment": "gco-inference/vllm-llama3=Available"}
        assert len(kubectl.calls) == 1

    def test_service_without_endpoints_fails(self) -> None:
        kubectl = _ScriptedKubectl({("wait",): (0, "", ""), ("get", "endpoints"): (0, "  \n", "")})
        with pytest.raises(
            drivers.ExampleValidationError,
            match="Service gco-inference/vllm-llama3 has no endpoints",
        ):
            drivers.wait_deployment_available(self._parsed(), kubectl, timeout=60)

    def test_never_available_reports_pods_and_describe_tail(self) -> None:
        kubectl = _ScriptedKubectl(
            {
                ("wait",): (1, "", "error: timed out waiting for the condition\n"),
                ("get", "pods"): (0, "vllm-llama3-1=Pending", ""),
                ("describe",): (0, "Events:\n  FailedScheduling insufficient nvidia.com/gpu", ""),
            }
        )
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers.wait_deployment_available(self._parsed(), kubectl, timeout=60)
        message = str(excinfo.value)
        assert message.startswith(
            "Deployment gco-inference/vllm-llama3 never became Available: "
            "error: timed out waiting for the condition; pods: vllm-llama3-1=Pending; "
        )
        assert message.endswith(
            "describe tail: Events:\n  FailedScheduling insufficient nvidia.com/gpu"
        )
        assert kubectl.commands("get", "pods")[0][2:6] == (
            "-n",
            "gco-inference",
            "-l",
            "app=vllm-llama3",
        )
        assert kubectl.calls[2] == (
            ("describe", "deployment/vllm-llama3", "-n", "gco-inference"),
            {"timeout": 60},
        )

    def test_missing_deployment_is_a_spec_error(self) -> None:
        parsed = _synthetic_parsed("svc-only", EXAMPLE_SPECS["inference-vllm"], [])
        with pytest.raises(drivers.ExampleValidationError, match="no Deployment found"):
            drivers.wait_deployment_available(parsed, lambda *a, **k: (0, "", ""), timeout=60)


class TestWaitRayClusterReady:
    @staticmethod
    def _parsed() -> static_checks.ParsedExample:
        return static_checks.parse_example(REPO_ROOT, "ray-cluster")

    def test_ready_once_head_and_min_workers_report(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "raycluster"): [
                    (1, "", "not found"),
                    (0, '{"status": {"state": "ready", "readyWorkerReplicas": 0}}', ""),
                    (0, '{"status": {"state": "Ready", "readyWorkerReplicas": 1}}', ""),
                ]
            }
        )
        evidence = drivers.wait_raycluster_ready(self._parsed(), kubectl, timeout=600)
        assert evidence == {
            "raycluster": "gco-jobs/ray-cluster",
            "state": "Ready",
            "ready_workers": 1,
        }
        assert clock.sleeps == [drivers._POLL_SECONDS] * 2
        assert kubectl.calls[0][0] == (
            "get",
            "raycluster",
            "ray-cluster",
            "-n",
            "gco-jobs",
            "-o",
            "json",
        )

    def test_timeout_carries_the_describe_tail(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "raycluster"): (0, '{"status": {"state": "unhealthy"}}', ""),
                ("describe",): (0, "Conditions: HeadPodNotReady", ""),
            }
        )
        with pytest.raises(
            drivers.ExampleValidationError,
            match="RayCluster gco-jobs/ray-cluster not ready after 30s; tail: Conditions: HeadPodNotReady",
        ):
            drivers.wait_raycluster_ready(self._parsed(), kubectl, timeout=30)
        assert kubectl.calls[-1] == (
            ("describe", "raycluster", "ray-cluster", "-n", "gco-jobs"),
            {"timeout": 60},
        )


class TestWaitVcjobCompletes:
    @staticmethod
    def _parsed() -> static_checks.ParsedExample:
        return static_checks.parse_example(REPO_ROOT, "volcano-gang-job")

    @staticmethod
    def _phase(phase: str) -> str:
        return f'{{"status": {{"state": {{"phase": "{phase}"}}}}}}'

    def test_completed(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "vcjob"): [
                    (1, "", "not found"),
                    (0, self._phase("Running"), ""),
                    (0, self._phase("Completed"), ""),
                ]
            }
        )
        evidence = drivers.wait_vcjob_completes(self._parsed(), kubectl, timeout=600)
        assert evidence == {"vcjob": "gco-jobs/distributed-training", "phase": "Completed"}
        assert clock.sleeps == [drivers._POLL_SECONDS] * 2
        assert kubectl.calls[0][0] == (
            "get",
            "vcjob",
            "distributed-training",
            "-n",
            "gco-jobs",
            "-o",
            "json",
        )

    @pytest.mark.parametrize("phase", ["Failed", "Aborted", "Terminated"])
    def test_terminal_failure_phases(self, phase: str) -> None:
        kubectl = _ScriptedKubectl({("get", "vcjob"): (0, self._phase(phase), "")})
        with pytest.raises(
            drivers.ExampleValidationError,
            match=f"vcjob gco-jobs/distributed-training reached phase {phase}",
        ):
            drivers.wait_vcjob_completes(self._parsed(), kubectl, timeout=600)

    def test_timeout(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl({("get", "vcjob"): (0, self._phase("Pending"), "")})
        with pytest.raises(
            drivers.ExampleValidationError,
            match="vcjob gco-jobs/distributed-training did not complete within 15s",
        ):
            drivers.wait_vcjob_completes(self._parsed(), kubectl, timeout=15)


class TestTrainJobWaiterEdges:
    @staticmethod
    def _parsed() -> static_checks.ParsedExample:
        return static_checks.parse_example(REPO_ROOT, "kubeflow-trainjob")

    def test_non_true_and_non_terminal_conditions_are_skipped(self, monkeypatch) -> None:
        import json

        clock = _install_clock(monkeypatch)
        ready = json.dumps(
            {
                "status": {
                    "conditions": [
                        {"type": "Failed", "status": "False"},
                        {"type": "Created", "status": "True"},
                        {"type": "Complete", "status": "True"},
                    ],
                    "jobsStatus": [{"name": "node", "succeeded": 2}],
                }
            }
        )
        kubectl = _ScriptedKubectl(
            {("get", "trainjob"): [(1, "", "not found"), (0, "{}", ""), (0, ready, "")]}
        )
        evidence = drivers.wait_trainjob_completes(self._parsed(), kubectl, timeout=600)
        assert evidence == {
            "trainjob": "gco-jobs/kubeflow-trainjob-example",
            "condition": "Complete",
            "jobsStatus": [{"name": "node", "succeeded": 2}],
        }
        assert clock.sleeps == [drivers._POLL_SECONDS] * 2


class TestWaitScaledJobScales:
    @staticmethod
    def _parsed() -> static_checks.ParsedExample:
        return static_checks.parse_example(REPO_ROOT, "keda-scaled-job")

    def test_returns_once_keda_spawns_jobs(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        spawned = " ".join(f"sqs-scaling-observer-{index}" for index in range(6))
        kubectl = _ScriptedKubectl(
            {
                ("get", "jobs"): [
                    (1, "", "error"),
                    (0, "", ""),
                    (0, spawned + "\n", ""),
                ]
            }
        )
        evidence = drivers.wait_scaledjob_scales(self._parsed(), kubectl, timeout=600)
        assert evidence == {
            "scaledjob": "gco-jobs/sqs-scaling-observer",
            "spawned_jobs": [f"sqs-scaling-observer-{index}" for index in range(5)],
        }
        assert clock.sleeps == [drivers._POLL_SECONDS] * 2
        assert kubectl.calls[0][0][2:6] == (
            "-n",
            "gco-jobs",
            "-l",
            "scaledjob.keda.sh/name=sqs-scaling-observer",
        )

    def test_timeout_carries_the_describe_tail(self, monkeypatch) -> None:
        _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl(
            {
                ("get", "jobs"): (0, "", ""),
                ("describe",): (0, "Conditions: Ready False", ""),
            }
        )
        with pytest.raises(
            drivers.ExampleValidationError,
            match="ScaledJob gco-jobs/sqs-scaling-observer spawned no Jobs within 15s; tail: Conditions: Ready False",
        ):
            drivers.wait_scaledjob_scales(self._parsed(), kubectl, timeout=15)
        assert kubectl.calls[-1] == (
            ("describe", "scaledjob", "sqs-scaling-observer", "-n", "gco-jobs"),
            {"timeout": 60},
        )


class TestCleanupExample:
    def test_manifest_delete_failure_is_surfaced(self, tmp_path) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "simple-job")
        kubectl = _ScriptedKubectl({("delete",): (1, "", "  error: the server is down  ")})
        with pytest.raises(
            drivers.ExampleValidationError,
            match="cleanup failed for simple-job: error: the server is down",
        ):
            drivers.cleanup_example(parsed, tmp_path / "m.yaml", kubectl)
        assert kubectl.calls == [
            (
                ("delete", "-f", str(tmp_path / "m.yaml"), "--ignore-not-found", "--wait=true"),
                {"timeout": 300},
            )
        ]

    def test_manifest_delete_reports_the_deleted_objects(self, tmp_path) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "simple-job")
        kubectl = _ScriptedKubectl({("delete",): (0, "\njob.batch/simple-example deleted\n\n", "")})
        assert drivers.cleanup_example(parsed, tmp_path / "m.yaml", kubectl) == {
            "deleted": ["job.batch/simple-example deleted"]
        }


class _FakeSqs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def create_queue(self, **kwargs):
        self.calls.append(("create_queue", kwargs))
        return {
            "QueueUrl": f"https://sqs.us-east-1.amazonaws.com/111122223333/{kwargs['QueueName']}"
        }

    def get_queue_attributes(self, **kwargs):
        self.calls.append(("get_queue_attributes", kwargs))
        return {"Attributes": {"QueueArn": "arn:aws:sqs:us-east-1:111122223333:demo"}}

    def set_queue_attributes(self, **kwargs):
        self.calls.append(("set_queue_attributes", kwargs))

    def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))

    def delete_queue(self, **kwargs):
        self.calls.append(("delete_queue", kwargs))


class _FakeSession:
    """boto3 Session stand-in handing out pre-built per-service clients."""

    def __init__(self, **clients) -> None:
        self.clients = clients
        self.requests: list[tuple[str, str | None]] = []

    def client(self, service: str, region_name: str | None = None):
        self.requests.append((service, region_name))
        if service not in self.clients:
            raise AssertionError(f"unexpected boto3 client: {service}")
        return self.clients[service]


class TestKedaDemoQueue:
    def test_create_provisions_policy_and_seeds_messages(self) -> None:
        import json

        sqs = _FakeSqs()
        session = _FakeSession(sqs=sqs)
        queue = drivers.KedaDemoQueue(session=session, region="us-east-1", run_id="r" * 100)
        evidence = queue.create("arn:aws:iam::111122223333:role/keda-operator")

        assert session.requests == [("sqs", "us-east-1")]
        assert evidence == {
            "queue_arn": "arn:aws:sqs:us-east-1:111122223333:demo",
            "seeded_messages": 10,
        }
        names = [name for name, _ in sqs.calls]
        assert names == [
            "create_queue",
            "get_queue_attributes",
            "set_queue_attributes",
            *["send_message"] * 10,
        ]
        created = sqs.calls[0][1]["QueueName"]
        assert created.startswith("gco-keda-demo-rrr") and len(created) == 80
        assert queue.queue_url.endswith(created)
        assert sqs.calls[1][1] == {"QueueUrl": queue.queue_url, "AttributeNames": ["QueueArn"]}
        policy = json.loads(sqs.calls[2][1]["Attributes"]["Policy"])
        assert sqs.calls[2][1]["QueueUrl"] == queue.queue_url
        (statement,) = policy["Statement"]
        assert statement["Principal"] == {"AWS": "arn:aws:iam::111122223333:role/keda-operator"}
        assert statement["Action"] == ["sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        assert statement["Resource"] == queue.queue_arn
        bodies = [kwargs["MessageBody"] for name, kwargs in sqs.calls if name == "send_message"]
        assert bodies == [f"demo-{index}" for index in range(10)]

    def test_destroy_deletes_only_a_created_queue(self) -> None:
        sqs = _FakeSqs()
        session = _FakeSession(sqs=sqs)
        queue = drivers.KedaDemoQueue(session=session, region="us-east-1", run_id="r")
        queue.destroy()
        assert session.requests == [] and sqs.calls == []

        queue.queue_url = "https://sqs.us-east-1.amazonaws.com/111122223333/q"
        queue.destroy()
        assert sqs.calls == [("delete_queue", {"QueueUrl": queue.queue_url})]


class TestVectorDemoCorpusCreateErrors:
    def _driver(self) -> drivers.VectorDemoCorpus:
        return drivers.VectorDemoCorpus(repo_root=REPO_ROOT, session=None, region="us-east-1")

    def test_non_json_output_is_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(drivers, "_run_cli", lambda *_a, **_k: (0, "Ingest complete!", ""))
        with pytest.raises(
            drivers.ExampleValidationError, match="emitted non-JSON output: Ingest complete!"
        ):
            self._driver().create()

    @pytest.mark.parametrize(
        "summary", ['{"bucket": "", "uploaded": ["k"]}', '{"bucket": "b", "uploaded": []}', "{}"]
    )
    def test_summary_without_revertable_keys_is_rejected(self, monkeypatch, summary: str) -> None:
        monkeypatch.setattr(drivers, "_run_cli", lambda *_a, **_k: (0, summary, ""))
        with pytest.raises(drivers.ExampleValidationError, match="no bucket/keys to revert later"):
            self._driver().create()


class TestReadinessWaiterPolling:
    def test_trainer_runtime_wait_polls_until_the_crd_is_served(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        runtime = '{"metadata": {"name": "torch-distributed"}}'
        kubectl = _ScriptedKubectl(
            {
                ("get", "crd"): [(1, "", "NotFound"), (0, "", "")],
                ("get", "clustertrainingruntime"): (0, runtime, ""),
            }
        )
        evidence = drivers.wait_trainer_runtime_ready(kubectl, timeout=300)
        assert evidence == {
            "crd": "trainjobs.trainer.kubeflow.org",
            "runtime": "torch-distributed",
            "runtime_created": "",
        }
        assert clock.sleeps == [drivers._POLL_SECONDS]

    def test_mlflow_wait_reports_the_last_conditions_on_timeout(self, monkeypatch) -> None:
        clock = _install_clock(monkeypatch)
        rolling = (
            '{"status": {"conditions": [{"type": "Progressing", "status": "True"}, '
            '{"type": "Available", "status": "False"}]}}'
        )
        kubectl = _ScriptedKubectl(
            {("get", "deployment", "mlflow"): [(1, "", "NotFound"), (0, rolling, "")]}
        )
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers.wait_mlflow_ready(kubectl, timeout=drivers._POLL_SECONDS)
        message = str(excinfo.value)
        assert message.startswith(
            f"MLflow tracking server not Available within {drivers._POLL_SECONDS}s"
        )
        assert message.endswith(
            'Last state: conditions: [{"type": "Progressing", "status": "True"}, '
            '{"type": "Available", "status": "False"}]'
        )
        assert clock.sleeps == [drivers._POLL_SECONDS]


# ---------------------------------------------------------------------------
# The examples action: static gate, capacity skip, KEDA plumbing, and the full
# per-example lifecycle dispatch for every setup driver and submission shape.
# ---------------------------------------------------------------------------


def _live_ctx(session=None, *, run_id: str = "run-1"):
    from types import SimpleNamespace

    return SimpleNamespace(
        settings=SimpleNamespace(repo_root=REPO_ROOT, run_id=run_id), session=session
    )


def _record_run_cli(monkeypatch, respond) -> list[tuple[list[str], Path, int]]:
    calls: list[tuple[list[str], Path, int]] = []

    def fake_run_cli(args, repo_root, timeout=600):
        calls.append((list(args), repo_root, timeout))
        return respond(args)

    monkeypatch.setattr(drivers, "_run_cli", fake_run_cli)
    return calls


_JOB_COMPLETE = '{"status": {"conditions": [{"type": "Complete", "status": "True"}]}}'
_MLFLOW_AVAILABLE = (
    '{"status": {"readyReplicas": 1, "conditions": [{"type": "Available", "status": "True"}]}}'
)
_TRAINJOB_COMPLETE = (
    '{"status": {"conditions": [{"type": "Complete", "status": "True"}], "jobsStatus": []}}'
)


class TestActionStatic:
    def test_selected_examples_pass_and_are_summarized(self, tmp_path) -> None:
        from scripts.example_job_validation import actions

        ctx = _live_ctx()
        ctx.settings.selected_examples = ("simple-job",)
        details = actions.action_static(ctx)
        assert details["failed"] == []
        # registry symmetry (2) + spec shape + documented path + transport,
        # namespace and governance findings for the one selected example.
        assert details["checked"] > 4

    def test_empty_selection_checks_every_example(self, monkeypatch) -> None:
        from scripts.example_job_validation import actions

        seen: list[tuple[Path, object]] = []
        monkeypatch.setattr(
            actions, "run_static_checks", lambda root, names: seen.append((root, names)) or []
        )
        ctx = _live_ctx()
        ctx.settings.selected_examples = ()
        assert actions.action_static(ctx) == {"checked": 0, "failed": []}
        assert seen == [(REPO_ROOT, None)]

    def test_failures_raise_with_their_details(self, monkeypatch) -> None:
        from scripts.example_job_validation import actions

        findings = [
            static_checks.StaticFinding(example="*", check="spec/file symmetry", passed=True),
            static_checks.StaticFinding(
                example="gpu-job",
                check="trusted image sources (Job/x)",
                passed=False,
                detail="docker.io",
            ),
        ]
        monkeypatch.setattr(actions, "run_static_checks", lambda root, names: findings)
        ctx = _live_ctx()
        ctx.settings.selected_examples = ("gpu-job",)
        with pytest.raises(RuntimeError) as excinfo:
            actions.action_static(ctx)
        assert str(excinfo.value) == (
            "1 static example check(s) failed: [{'example': 'gpu-job', "
            "'check': 'trusted image sources (Job/x)', 'detail': 'docker.io'}]"
        )


class _FakeQuotas:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def get_service_quota(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TestCapacitySkipReason:
    def test_no_quota_code_means_no_lookup(self) -> None:
        from scripts.example_job_validation import actions

        session = _FakeSession()
        assert actions._capacity_skip_reason(_live_ctx(session), "us-east-1", "") is None
        assert session.requests == []

    def test_zero_quota_skips_with_the_quota_name(self) -> None:
        from scripts.example_job_validation import actions

        quotas = _FakeQuotas(
            {"Quota": {"Value": 0.0, "QuotaName": "Running On-Demand P instances"}}
        )
        session = _FakeSession(**{"service-quotas": quotas})
        reason = actions._capacity_skip_reason(_live_ctx(session), "us-west-2", "L-417A185B")
        assert reason == (
            "account quota 'Running On-Demand P instances' is 0 vCPUs — "
            "no capacity for this example"
        )
        assert session.requests == [("service-quotas", "us-west-2")]
        assert quotas.calls == [{"ServiceCode": "ec2", "QuotaCode": "L-417A185B"}]
        assert not drivers.BOTO_CLIENT_LOCK.locked()

    def test_positive_quota_does_not_skip(self) -> None:
        from scripts.example_job_validation import actions

        quotas = _FakeQuotas({"Quota": {"Value": 96}})
        session = _FakeSession(**{"service-quotas": quotas})
        assert actions._capacity_skip_reason(_live_ctx(session), "us-east-1", "L-1945791B") is None

    def test_lookup_failure_skips_rather_than_failing_the_run(self) -> None:
        from scripts.example_job_validation import actions

        quotas = _FakeQuotas(PermissionError("AccessDenied"))
        session = _FakeSession(**{"service-quotas": quotas})
        reason = actions._capacity_skip_reason(_live_ctx(session), "us-east-1", "L-2C3B7624")
        assert reason == (
            "quota L-2C3B7624 lookup failed (PermissionError); treating as unavailable"
        )


class TestKedaPlumbing:
    def test_operator_role_is_read_from_the_first_namespace_that_has_it(self) -> None:
        from scripts.example_job_validation import actions

        kubectl = _ScriptedKubectl(
            {
                ("get", "serviceaccount", "keda-operator", "-n", "keda"): (1, "", "NotFound"),
                ("get", "serviceaccount", "keda-operator", "-n", "gco-system"): (
                    0,
                    "arn:aws:iam::111122223333:role/keda-operator\n",
                    "",
                ),
            }
        )
        assert (
            actions._keda_operator_role_arn(kubectl)
            == "arn:aws:iam::111122223333:role/keda-operator"
        )
        assert [args[4] for args, _ in kubectl.calls] == ["keda", "gco-system"]
        assert kubectl.calls[0][0][-1] == (
            "jsonpath={.metadata.annotations.eks\\.amazonaws\\.com/role-arn}"
        )

    def test_missing_annotation_everywhere_is_an_example_failure(self) -> None:
        from scripts.example_job_validation import actions

        kubectl = _ScriptedKubectl({("get", "serviceaccount"): (0, "   ", "")})
        with pytest.raises(
            drivers.ExampleValidationError,
            match="role annotation not found in keda/gco-system/kube-system",
        ):
            actions._keda_operator_role_arn(kubectl)
        assert [args[4] for args, _ in kubectl.calls] == ["keda", "gco-system", "kube-system"]

    def test_prepare_manifest_substitutes_only_queue_triggers(self, monkeypatch, tmp_path):
        import yaml

        from scripts.example_job_validation import actions

        monkeypatch.setattr(drivers.tempfile, "tempdir", str(tmp_path))
        source = tmp_path / "keda.yaml"
        source.write_text(
            "\n".join(
                [
                    "apiVersion: v1",
                    "kind: ServiceAccount",
                    "metadata: {name: worker}",
                    "---",
                    "apiVersion: keda.sh/v1alpha1",
                    "kind: ScaledJob",
                    "metadata: {name: observer}",
                    "spec:",
                    "  triggers:",
                    "    - type: aws-sqs-queue",
                    "      metadata: {queueURL: PLACEHOLDER, awsRegion: eu-west-1, queueLength: '5'}",
                    "    - type: cron",
                    "      metadata: {start: '0 * * * *'}",
                    "    - type: memory",
                    "---",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        parsed = _synthetic_parsed("keda-scaled-job", EXAMPLE_SPECS["keda-scaled-job"], [])
        path = actions._prepare_keda_manifest(
            parsed, "https://sqs.us-east-2.amazonaws.com/111122223333/demo", "us-east-2", source
        )
        assert path.parent == tmp_path and path.name.endswith("-keda-scaled-job.yaml")
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        assert [doc["kind"] for doc in documents] == ["ServiceAccount", "ScaledJob"]
        assert documents[1]["spec"]["triggers"] == [
            {
                "type": "aws-sqs-queue",
                "metadata": {
                    "queueURL": "https://sqs.us-east-2.amazonaws.com/111122223333/demo",
                    "awsRegion": "us-east-2",
                    "queueLength": "5",
                },
            },
            {"type": "cron", "metadata": {"start": "0 * * * *"}},
            {"type": "memory"},
        ]


class TestRunOneExample:
    """Every dispatch branch of _run_one_example against scripted kubectl/CLI."""

    def test_companion_artifacts_pass_without_touching_the_cluster(self) -> None:
        from scripts.example_job_validation import actions

        result = actions._run_one_example(
            _live_ctx(),
            "dag-step-preprocess",
            "us-east-1",
            lambda *a, **k: pytest.fail("no kubectl"),
        )
        assert result.to_dict() == {
            "name": "dag-step-preprocess",
            "status": "passed",
            "submission": COMPANION,
            "duration_seconds": 0.0,
            "detail": "companion artifact: step manifest executed via pipeline-dag",
            "mutations": {},
            "evidence": {},
        }

    def test_zero_quota_skips_before_any_submission(self) -> None:
        from scripts.example_job_validation import actions

        quotas = _FakeQuotas({"Quota": {"Value": 0, "QuotaName": "Running On-Demand P instances"}})
        session = _FakeSession(**{"service-quotas": quotas})
        result = actions._run_one_example(
            _live_ctx(session),
            "efa-distributed-training",
            "us-east-1",
            lambda *a, **k: pytest.fail("no kubectl"),
        )
        assert result.status == "skipped"
        assert result.submission == EXAMPLE_SPECS["efa-distributed-training"].submission
        assert "Running On-Demand P instances" in result.detail
        assert quotas.calls == [{"ServiceCode": "ec2", "QuotaCode": "L-417A185B"}]

    def test_keda_example_runs_the_full_documented_lifecycle(self, monkeypatch, tmp_path):
        import yaml

        from scripts.example_job_validation import actions

        monkeypatch.setattr(drivers.tempfile, "tempdir", str(tmp_path))
        sqs = _FakeSqs()
        session = _FakeSession(sqs=sqs)
        kubectl = _ScriptedKubectl(
            {
                ("get", "serviceaccount"): (0, "arn:aws:iam::111122223333:role/keda\n", ""),
                ("apply",): (0, "scaledjob.keda.sh/sqs-scaling-observer created", ""),
                ("get", "jobs"): (0, "sqs-scaling-observer-a1b2c", ""),
                ("delete",): (0, "scaledjob.keda.sh/sqs-scaling-observer deleted", ""),
            }
        )
        result = actions._run_one_example(
            _live_ctx(session, run_id="ex-42"), "keda-scaled-job", "us-east-1", kubectl
        )

        assert result.status == "passed", result.detail
        assert result.mutations == {
            "ScaledJob.triggers.queueURL": "disposable demo queue for this run"
        }
        assert result.evidence["setup"] == {
            "queue_arn": "arn:aws:sqs:us-east-1:111122223333:demo",
            "seeded_messages": 10,
        }
        assert result.evidence["submission"] == {
            "command": "kubectl apply -f examples/keda-scaled-job.yaml",
            "output": "scaledjob.keda.sh/sqs-scaling-observer created",
        }
        assert result.evidence["criteria"] == {
            "scaledjob": "gco-jobs/sqs-scaling-observer",
            "spawned_jobs": ["sqs-scaling-observer-a1b2c"],
        }
        assert result.evidence["cleanup"] == {
            "deleted": ["scaledjob.keda.sh/sqs-scaling-observer deleted"]
        }
        assert result.duration_seconds >= 0

        # The applied (and later deleted) manifest is the disclosed temp copy
        # pointing at this run's queue, not the shipped placeholder.
        (apply_call,) = kubectl.commands("apply")
        (delete_call,) = kubectl.commands("delete")
        applied = Path(apply_call[2])
        assert applied.parent == tmp_path and applied.name.endswith("-keda-scaled-job.yaml")
        assert Path(delete_call[2]) == applied
        scaled_job = next(
            doc
            for doc in yaml.safe_load_all(applied.read_text(encoding="utf-8"))
            if doc["kind"] == "ScaledJob"
        )
        queue_url = "https://sqs.us-east-1.amazonaws.com/111122223333/gco-keda-demo-ex-42"
        assert scaled_job["spec"]["triggers"][0]["metadata"]["queueURL"] == queue_url
        assert scaled_job["spec"]["triggers"][0]["metadata"]["awsRegion"] == "us-east-1"
        # The demo queue is torn down after the example, whatever happened.
        assert sqs.calls[0][1] == {"QueueName": "gco-keda-demo-ex-42"}
        assert sqs.calls[-1] == ("delete_queue", {"QueueUrl": queue_url})

    def test_keda_failure_after_queue_creation_still_destroys_the_queue(
        self, monkeypatch, tmp_path
    ) -> None:
        from scripts.example_job_validation import actions

        monkeypatch.setattr(drivers.tempfile, "tempdir", str(tmp_path))
        _install_clock(monkeypatch)
        sqs = _FakeSqs()
        session = _FakeSession(sqs=sqs)
        kubectl = _ScriptedKubectl(
            {
                ("get", "serviceaccount"): (0, "arn:aws:iam::111122223333:role/keda", ""),
                ("apply",): (0, "created", ""),
                ("get", "jobs"): (0, "", ""),
                ("describe",): (0, "no scaling activity", ""),
                ("delete",): (0, "deleted", ""),
            }
        )
        result = actions._run_one_example(
            _live_ctx(session), "keda-scaled-job", "us-east-1", kubectl
        )

        assert result.status == "failed"
        assert "spawned no Jobs within" in result.detail
        assert "no scaling activity" in result.detail
        assert set(result.evidence) == {"setup", "submission"}
        assert result.mutations == {
            "ScaledJob.triggers.queueURL": "disposable demo queue for this run"
        }
        assert len(kubectl.commands("delete")) == 1
        assert sqs.calls[-1][0] == "delete_queue"

    def test_setup_failure_before_submission_cleans_up_and_swallows_cleanup_errors(self) -> None:
        from scripts.example_job_validation import actions

        session = _FakeSession()  # any client request would fail the test
        kubectl = _ScriptedKubectl(
            {
                ("get", "serviceaccount"): (1, "", "NotFound"),
                ("delete",): (1, "", "error: connection refused"),
            }
        )
        result = actions._run_one_example(
            _live_ctx(session), "keda-scaled-job", "us-east-1", kubectl
        )
        assert result.status == "failed"
        assert result.detail.startswith("KEDA operator service-account role annotation not found")
        assert result.evidence == {}
        assert result.mutations == {}
        (delete_call,) = kubectl.commands("delete")
        assert Path(delete_call[2]) == REPO_ROOT / "examples" / "keda-scaled-job.yaml"
        assert session.requests == []

    def test_vector_example_ingests_the_demo_corpus_and_reverts_it(self, monkeypatch) -> None:
        from scripts.example_job_validation import actions

        events: list[str] = []

        class FakeCorpus:
            def __init__(self, *, repo_root, session, region):
                assert (repo_root, session, region) == (REPO_ROOT, "session", "us-east-1")

            def create(self):
                events.append("create")
                return {"command": "gco vector ingest --demo --wait", "uploaded": ["a.md"]}

            def destroy(self):
                events.append("destroy")

        monkeypatch.setattr(drivers, "VectorDemoCorpus", FakeCorpus)
        cli_calls = _record_run_cli(
            monkeypatch, lambda args: (0, "submitted vector-store-search-example", "")
        )
        kubectl = _ScriptedKubectl(
            {
                ("get", "job"): (0, _JOB_COMPLETE, ""),
                ("get", "events"): (0, "", ""),
                ("delete",): (0, "job.batch/vector-store-search-example deleted", ""),
            }
        )
        result = actions._run_one_example(
            _live_ctx("session"), "vector-store-search-job", "us-east-1", kubectl
        )

        assert result.status == "passed", result.detail
        assert events == ["create", "destroy"]
        assert result.evidence["setup"]["uploaded"] == ["a.md"]
        assert result.evidence["criteria"] == {
            "jobs": {"gco-jobs/vector-store-search-example": "complete"}
        }
        assert result.evidence["cleanup"] == {
            "deleted": ["job.batch/vector-store-search-example deleted"]
        }
        (submit,) = cli_calls
        manifest = REPO_ROOT / "examples" / "vector-store-search-job.yaml"
        assert submit == (
            ["gco", "jobs", "submit-direct", str(manifest), "-r", "us-east-1"],
            REPO_ROOT,
            600,
        )
        assert result.evidence["submission"]["command"] == (
            "gco jobs submit-direct examples/vector-store-search-job.yaml"
        )
        # The unmutated example is submitted and deleted as the shipped file.
        assert Path(kubectl.commands("delete")[0][2]) == manifest

    def test_trainjob_example_waits_for_the_runtime_then_submits_over_sqs(self, monkeypatch):
        from scripts.example_job_validation import actions

        cli_calls = _record_run_cli(monkeypatch, lambda args: (0, "queued", ""))
        kubectl = _ScriptedKubectl(
            {
                ("get", "crd", "trainjobs.trainer.kubeflow.org"): (0, "", ""),
                ("get", "clustertrainingruntime", "torch-distributed"): (
                    0,
                    '{"metadata": {"name": "torch-distributed", "creationTimestamp": "2026-01-01T00:00:00Z"}}',
                    "",
                ),
                ("get", "trainjob", "kubeflow-trainjob-example"): (0, _TRAINJOB_COMPLETE, ""),
                ("delete",): (
                    0,
                    "trainjob.trainer.kubeflow.org/kubeflow-trainjob-example deleted",
                    "",
                ),
            }
        )
        result = actions._run_one_example(_live_ctx(), "kubeflow-trainjob", "eu-west-1", kubectl)

        assert result.status == "passed", result.detail
        assert result.evidence["setup"] == {
            "crd": "trainjobs.trainer.kubeflow.org",
            "runtime": "torch-distributed",
            "runtime_created": "2026-01-01T00:00:00Z",
        }
        assert result.evidence["criteria"]["condition"] == "Complete"
        assert cli_calls == [
            (
                [
                    "gco",
                    "jobs",
                    "submit-sqs",
                    str(REPO_ROOT / "examples" / "kubeflow-trainjob.yaml"),
                    "--region",
                    "eu-west-1",
                ],
                REPO_ROOT,
                600,
            )
        ]
        assert [args[:2] for args, _ in kubectl.calls] == [
            ("get", "crd"),
            ("get", "clustertrainingruntime"),
            ("get", "trainjob"),
            ("delete", "-f"),
        ]

    def test_mlflow_example_waits_for_the_tracking_server_first(self, monkeypatch) -> None:
        from scripts.example_job_validation import actions

        cli_calls = _record_run_cli(
            monkeypatch, lambda args: (0, "job/mlflow-tracking-example created", "")
        )
        kubectl = _ScriptedKubectl(
            {
                ("get", "deployment", "mlflow", "-n", "monitoring"): (0, _MLFLOW_AVAILABLE, ""),
                ("get", "job", "mlflow-tracking-example"): (0, _JOB_COMPLETE, ""),
                ("get", "events"): (0, "", ""),
                ("delete",): (0, "job.batch/mlflow-tracking-example deleted", ""),
            }
        )
        result = actions._run_one_example(_live_ctx(), "mlflow-tracking-job", "us-east-1", kubectl)

        assert result.status == "passed", result.detail
        assert result.evidence["setup"] == {"deployment": "monitoring/mlflow", "ready_replicas": 1}
        assert result.evidence["criteria"] == {
            "jobs": {"gco-jobs/mlflow-tracking-example": "complete"}
        }
        assert cli_calls[0][0][:3] == ["gco", "jobs", "submit-direct"]
        assert kubectl.calls[0][0][:3] == ("get", "deployment", "mlflow")

    def test_dag_example_has_no_waiter_and_cleans_up_its_step_manifests(self, monkeypatch):
        from scripts.example_job_validation import actions

        cli_calls = _record_run_cli(
            monkeypatch, lambda args: (0, "pipeline completed: 2/2 steps", "")
        )
        kubectl = _ScriptedKubectl({("delete",): (0, "job.batch/step deleted", "")})
        result = actions._run_one_example(_live_ctx(), "pipeline-dag", "us-east-1", kubectl)

        assert result.status == "passed", result.detail
        assert set(result.evidence) == {"submission", "cleanup"}
        assert result.evidence["submission"] == {
            "command": "gco dag run examples/pipeline-dag.yaml",
            "output": "pipeline completed: 2/2 steps",
        }
        assert result.evidence["cleanup"] == {"deleted": ["job.batch/step deleted"] * 2}
        assert cli_calls == [
            (
                [
                    "gco",
                    "dag",
                    "run",
                    str(REPO_ROOT / "examples" / "pipeline-dag.yaml"),
                    "-r",
                    "us-east-1",
                ],
                REPO_ROOT,
                1800,
            )
        ]
        deleted = {Path(args[2]).name for args in kubectl.commands("delete")}
        assert deleted == {"dag-step-preprocess.yaml", "dag-step-train.yaml"}


class TestActionExamplesCheckpointShortCircuit:
    def test_fully_checkpointed_selection_never_opens_a_cluster_session(
        self, monkeypatch, tmp_path
    ):
        from scripts.example_job_validation import actions

        names = sorted(EXAMPLE_SPECS)[:2]
        monkeypatch.setattr(
            actions.kube,
            "cluster_session",
            lambda *a, **k: pytest.fail("no example is pending; the tunnel must not open"),
        )
        monkeypatch.setattr(
            actions, "_run_one_example", lambda *a, **k: pytest.fail("nothing should run")
        )
        prior = {name: {"status": "passed", "submission": "s"} for name in names}
        ctx = TestParallelExamples._ctx(tmp_path, names, prior=prior)
        summary = actions.action_examples(ctx)

        assert summary["passed"] == len(names)
        assert summary["failed"] == 0 and summary["skipped"] == 0
        assert summary["max_parallel"] == 0
        assert [item["detail"] for item in summary["results"]] == [
            "checkpoint: already passed in this run"
        ] * len(names)
        assert ctx.checkpoint.state["examples_summary"] == summary


# ---------------------------------------------------------------------------
# kube: API readiness probe edges, kubeconfig rewriting/hardening, argv guards.
# ---------------------------------------------------------------------------


def _install_kube_clock(monkeypatch) -> _FakeClock:
    from types import SimpleNamespace

    from scripts.example_job_validation import kube

    clock = _FakeClock()
    monkeypatch.setattr(kube, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    return clock


class TestClusterApiReadinessEdges:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"timeout_seconds": 0}, "timeout_seconds must be positive"),
            ({"poll_interval_seconds": 0}, "poll_interval_seconds must be positive"),
        ],
    )
    def test_rejects_non_positive_waits(self, kwargs: dict, message: str) -> None:
        from scripts.example_job_validation import kube

        with pytest.raises(ValueError, match=message):
            kube._wait_for_cluster_api(
                lambda *a, **k: pytest.fail("must not probe"), tunnel_process=None, **kwargs
            )

    def test_deadline_reached_after_a_sleep_reports_the_last_transient_error(self, monkeypatch):
        from scripts.example_job_validation import kube

        clock = _install_kube_clock(monkeypatch)
        probes: list[tuple[tuple[str, ...], int]] = []

        def refused(*args: str, timeout: int = 0, **_kwargs) -> tuple[int, str, str]:
            probes.append((args, timeout))
            return 1, "", "dial tcp 127.0.0.1:8443: connect: connection refused"

        with pytest.raises(RuntimeError) as excinfo:
            kube._wait_for_cluster_api(
                refused, tunnel_process=None, timeout_seconds=1.0, poll_interval_seconds=1.0
            )
        assert str(excinfo.value) == (
            "Kubernetes API did not become ready through the SSM tunnel within 1.0s after "
            "1 attempt(s). Last transient error: dial tcp 127.0.0.1:8443: connect: "
            "connection refused"
        )
        # One probe, sized to the remaining budget, then one bounded sleep.
        assert probes == [(("--request-timeout=2s", "get", "--raw=/readyz"), 2)]
        assert clock.sleeps == [1.0]

    def test_probe_timeout_is_transient(self, monkeypatch) -> None:
        import subprocess

        from scripts.example_job_validation import kube

        clock = _install_kube_clock(monkeypatch)
        attempts = 0

        def slow_then_ready(*args: str, timeout: int = 0, **_kwargs) -> tuple[int, str, str]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise subprocess.TimeoutExpired(["kubectl", *args], timeout)
            return 0, "ok", ""

        kube._wait_for_cluster_api(slow_then_ready, tunnel_process=None, poll_interval_seconds=2.0)
        assert attempts == 2
        assert clock.sleeps == [2.0]

    def test_probe_timeout_text_is_the_last_error_on_expiry(self, monkeypatch) -> None:
        import subprocess

        from scripts.example_job_validation import kube

        _install_kube_clock(monkeypatch)

        def hangs(*args: str, timeout: int = 0, **_kwargs) -> tuple[int, str, str]:
            raise subprocess.TimeoutExpired(["kubectl", *args], timeout)

        with pytest.raises(
            RuntimeError, match=r"Last transient error: kubectl readiness probe timed out after 2s$"
        ):
            kube._wait_for_cluster_api(
                hangs, tunnel_process=None, timeout_seconds=1.0, poll_interval_seconds=1.0
            )


class TestQuietFormatter:
    def test_every_level_prints_the_tunnel_prefix(self, capsys) -> None:
        from scripts.example_job_validation import kube

        formatter = kube._QuietFormatter()
        formatter.print_info("opening")
        formatter.print_success("ready")
        formatter.print_warning("slow")
        formatter.print_error("closed")
        assert capsys.readouterr().out.splitlines() == [
            "[tunnel] opening",
            "[tunnel] ready",
            "[tunnel] slow",
            "[tunnel] closed",
        ]


class TestIsolatedKubeconfigHardening:
    def test_directories_and_symlinks_are_rejected(self, tmp_path: Path) -> None:
        from scripts.example_job_validation import kube

        with pytest.raises(ValueError, match="must be a regular file"):
            kube._validate_and_secure_isolated_kubeconfig(tmp_path)

        target = tmp_path / "real"
        target.write_text("{}", encoding="utf-8")
        link = tmp_path / "link"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="must be a regular file"):
            kube._validate_and_secure_isolated_kubeconfig(link)

    def test_foreign_owner_is_rejected(self, tmp_path: Path, monkeypatch) -> None:
        import os
        from types import SimpleNamespace

        from scripts.example_job_validation import kube

        path = tmp_path / "kubeconfig"
        path.write_text("{}", encoding="utf-8")
        owner = path.lstat().st_uid
        monkeypatch.setattr(kube, "os", SimpleNamespace(name=os.name, geteuid=lambda: owner + 1))
        with pytest.raises(PermissionError, match="not owned by this user"):
            kube._validate_and_secure_isolated_kubeconfig(path)

    def test_owned_regular_file_is_made_private(self, tmp_path: Path) -> None:
        import stat

        from scripts.example_job_validation import kube

        path = tmp_path / "kubeconfig"
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o644)
        kube._validate_and_secure_isolated_kubeconfig(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_windows_skips_posix_mode_bits(self, tmp_path: Path, monkeypatch) -> None:
        import os
        import stat
        from types import SimpleNamespace

        from scripts.example_job_validation import kube

        path = tmp_path / "kubeconfig"
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o644)
        monkeypatch.setattr(kube, "os", SimpleNamespace(name="nt", geteuid=os.geteuid))
        kube._validate_and_secure_isolated_kubeconfig(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


class TestPointKubeconfigAtTunnel:
    @staticmethod
    def _fake_refresh(monkeypatch, tmp_path: Path, config) -> tuple[Path, list[tuple], list[Path]]:
        import yaml

        from scripts.example_job_validation import kube

        path = tmp_path / "kubeconfig"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        refreshed: list[tuple] = []
        secured: list[Path] = []

        def fake_refresh(cluster_name, region, *, kubeconfig_path=None):
            refreshed.append((cluster_name, region, kubeconfig_path))
            return path

        monkeypatch.setattr(kube, "refresh_kubeconfig", fake_refresh)
        monkeypatch.setattr(kube, "_validate_and_secure_isolated_kubeconfig", secured.append)
        return path, refreshed, secured

    def _point(self, **kwargs) -> None:
        from scripts.example_job_validation import kube

        kube.update_and_point_kubeconfig_at_tunnel(
            "gco-us-east-1",
            "us-east-1",
            "https://127.0.0.1:8443",
            "abc.eks.amazonaws.com",
            **kwargs,
        )

    @pytest.mark.parametrize("config", [[], {"clusters": "nope"}, {"users": []}])
    def test_config_without_a_cluster_list_is_rejected(self, monkeypatch, tmp_path, config):
        self._fake_refresh(monkeypatch, tmp_path, config)
        with pytest.raises(ValueError, match="Kubeconfig has no cluster list"):
            self._point()

    def test_malformed_matching_entry_is_rejected(self, monkeypatch, tmp_path) -> None:
        self._fake_refresh(
            monkeypatch,
            tmp_path,
            {
                "clusters": [
                    {"name": "arn:aws:eks:us-east-1:1:cluster/gco-us-east-1", "cluster": "x"}
                ]
            },
        )
        with pytest.raises(ValueError, match="cluster entry is malformed"):
            self._point()

    def test_missing_cluster_is_rejected(self, monkeypatch, tmp_path) -> None:
        self._fake_refresh(
            monkeypatch,
            tmp_path,
            {
                "clusters": [
                    "junk",
                    {"name": "arn:aws:eks:us-east-1:1:cluster/other", "cluster": {}},
                ]
            },
        )
        with pytest.raises(
            ValueError, match="did not contain the requested cluster: gco-us-east-1"
        ):
            self._point()

    def test_only_the_requested_cluster_is_rewritten(self, monkeypatch, tmp_path) -> None:
        import yaml

        path, refreshed, secured = self._fake_refresh(
            monkeypatch,
            tmp_path,
            {
                "clusters": [
                    "junk",
                    {
                        "name": "arn:aws:eks:us-east-1:1:cluster/other",
                        "cluster": {"server": "https://o"},
                    },
                    {
                        "name": "arn:aws:eks:us-east-1:1:cluster/gco-us-east-1",
                        "cluster": {"server": "https://real", "certificate-authority-data": "CA"},
                    },
                ]
            },
        )
        self._point()  # default kubeconfig: nothing to harden
        assert refreshed == [("gco-us-east-1", "us-east-1", None)]
        assert secured == []
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert config["clusters"][0] == "junk"
        assert config["clusters"][1]["cluster"] == {"server": "https://o"}
        assert config["clusters"][2]["cluster"] == {
            "server": "https://127.0.0.1:8443",
            "tls-server-name": "abc.eks.amazonaws.com",
            "certificate-authority-data": "CA",
        }

        self._point(kubeconfig_path=path)
        assert refreshed[-1] == ("gco-us-east-1", "us-east-1", path)
        assert secured == [path]


class TestClusterAccessArgv:
    @pytest.mark.parametrize("gco_command", [(), ("gco", ""), ("gco", 5)])
    def test_invalid_gco_command_prefix_never_spawns(self, monkeypatch, gco_command) -> None:
        from scripts.example_job_validation import kube

        monkeypatch.setattr(
            kube.subprocess, "run", lambda *a, **k: pytest.fail("subprocess must not run")
        )
        with pytest.raises(ValueError, match="gco_command must be a non-empty argv prefix"):
            kube.ensure_cluster_access_entry(REPO_ROOT, "us-east-1", gco_command=gco_command)


class TestClusterSessionKubectl:
    def test_shell_execution_is_refused_and_caller_env_passes_through(self, monkeypatch):
        import contextlib
        import subprocess
        from types import SimpleNamespace

        from cli import cluster_tunnel
        from scripts.example_job_validation import kube

        calls: list[tuple[list[str], dict]] = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        @contextlib.contextmanager
        def public_tunnel(*args, **kwargs):
            yield SimpleNamespace(active=False, server=None, tls_server_name=None)

        monkeypatch.setattr(kube.subprocess, "run", fake_run)
        monkeypatch.setattr(cluster_tunnel, "open_api_server_tunnel", public_tunnel)

        with kube.cluster_session(REPO_ROOT, "gco-us-east-1", "us-east-1") as kubectl:
            spawned_before = len(calls)
            with pytest.raises(ValueError, match="does not allow shell execution"):
                kubectl("get", "pods", shell=True)
            assert len(calls) == spawned_before
            assert kubectl("get", "pods", shell=False, env={"X": "1"}, timeout=9) == (0, "", "")

        command, kwargs = calls[-1]
        assert command == ["kubectl", "get", "pods"]
        assert kwargs["env"] == {"X": "1"}
        assert kwargs["timeout"] == 9
        assert kwargs["shell"] is False
