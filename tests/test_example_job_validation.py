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
        assert required_helm_overrides(names) == ("slurm", "yunikorn")
        assert required_feature_overrides(names) == (
            "aurora_pgvector",
            "fsx_lustre",
            "valkey",
        )

    def test_narrow_selection_needs_nothing(self) -> None:
        assert required_helm_overrides(["simple-job"]) == ()
        assert required_feature_overrides(["simple-job"]) == ()

    def test_selection_drives_settings_context(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, examples=("slurm-cluster-job", "valkey-cache-job"))
        assert settings.optional_schedulers == ("slurm",)
        assert settings.feature_overrides == ("valkey",)
        context = settings.extra_cdk_context()
        assert context["helm_enabled_overrides"] == "slurm"
        assert context["feature_enabled_overrides"] == "valkey"

    def test_identity_pins_selection(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, examples=("simple-job",))
        identity = settings.identity()
        assert identity["selected_examples"] == ["simple-job"]
        assert identity["feature_overrides"] == []

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
        """The derivation covers the old hardcoded set PLUS opencost.

        The previous literal guard omitted ``opencost`` even though it
        depends on topology (and therefore deploy) — deriving from the
        dependency graph closed that gap.
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
