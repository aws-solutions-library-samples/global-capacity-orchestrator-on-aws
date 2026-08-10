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
