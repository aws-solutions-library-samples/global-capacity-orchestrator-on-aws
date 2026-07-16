"""
Tests for the job-DAG pipeline feature in cli/dag.py.

Covers DagStep/DagDefinition construction, validate() across its
failure modes (duplicate step names, unknown dependencies, cycle
detection, missing manifest files), get_ready_steps topological
walk, load_dag YAML parsing, and the DagRunner execution loop that
submits jobs, polls for completion, and handles failures. Uses
tmp_path-backed YAML manifests so the filesystem paths resolve
during validation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from cli.dag import DagDefinition, DagRunner, DagStep, load_dag

# =============================================================================
# DagStep Tests
# =============================================================================


class TestDagStep:
    def test_defaults(self):
        step = DagStep(name="train", manifest="train.yaml")
        assert step.status == "pending"
        assert step.depends_on == []
        assert step.job_name is None

    def test_with_dependencies(self):
        step = DagStep(name="train", manifest="train.yaml", depends_on=["preprocess"])
        assert step.depends_on == ["preprocess"]


# =============================================================================
# DagDefinition Tests
# =============================================================================


class TestDagDefinition:
    def _make_dag(self, steps):
        return DagDefinition(name="test", steps=steps)

    def test_simple_valid_dag(self, tmp_path):
        (tmp_path / "a.yaml").write_text("kind: Job")
        (tmp_path / "b.yaml").write_text("kind: Job")
        dag = self._make_dag(
            [
                DagStep(name="a", manifest=str(tmp_path / "a.yaml")),
                DagStep(name="b", manifest=str(tmp_path / "b.yaml"), depends_on=["a"]),
            ]
        )
        assert dag.validate() == []

    def test_duplicate_step_names(self, tmp_path):
        (tmp_path / "a.yaml").write_text("kind: Job")
        dag = self._make_dag(
            [
                DagStep(name="a", manifest=str(tmp_path / "a.yaml")),
                DagStep(name="a", manifest=str(tmp_path / "a.yaml")),
            ]
        )
        errors = dag.validate()
        assert any("Duplicate" in e for e in errors)

    def test_unknown_dependency(self, tmp_path):
        (tmp_path / "a.yaml").write_text("kind: Job")
        dag = self._make_dag(
            [
                DagStep(name="a", manifest=str(tmp_path / "a.yaml"), depends_on=["nonexistent"]),
            ]
        )
        errors = dag.validate()
        assert any("unknown step" in e for e in errors)

    def test_cycle_detection(self, tmp_path):
        (tmp_path / "a.yaml").write_text("kind: Job")
        (tmp_path / "b.yaml").write_text("kind: Job")
        dag = self._make_dag(
            [
                DagStep(name="a", manifest=str(tmp_path / "a.yaml"), depends_on=["b"]),
                DagStep(name="b", manifest=str(tmp_path / "b.yaml"), depends_on=["a"]),
            ]
        )
        errors = dag.validate()
        assert any("Cycle" in e for e in errors)

    def test_missing_manifest(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="/nonexistent/path.yaml"),
            ]
        )
        errors = dag.validate()
        assert any("not found" in e for e in errors)

    def test_get_ready_steps_no_deps(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="a.yaml"),
                DagStep(name="b", manifest="b.yaml"),
            ]
        )
        ready = dag.get_ready_steps()
        assert len(ready) == 2

    def test_get_ready_steps_with_deps(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="a.yaml", status="succeeded"),
                DagStep(name="b", manifest="b.yaml", depends_on=["a"]),
                DagStep(name="c", manifest="c.yaml", depends_on=["b"]),
            ]
        )
        ready = dag.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].name == "b"

    def test_get_ready_steps_blocked(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="a.yaml", status="running"),
                DagStep(name="b", manifest="b.yaml", depends_on=["a"]),
            ]
        )
        ready = dag.get_ready_steps()
        assert len(ready) == 0

    def test_is_complete(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="a.yaml", status="succeeded"),
                DagStep(name="b", manifest="b.yaml", status="failed"),
            ]
        )
        assert dag.is_complete()

    def test_is_not_complete(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="a.yaml", status="succeeded"),
                DagStep(name="b", manifest="b.yaml", status="pending"),
            ]
        )
        assert not dag.is_complete()

    def test_has_failures(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="a.yaml", status="succeeded"),
                DagStep(name="b", manifest="b.yaml", status="failed"),
            ]
        )
        assert dag.has_failures()

    def test_no_failures(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="a.yaml", status="succeeded"),
                DagStep(name="b", manifest="b.yaml", status="succeeded"),
            ]
        )
        assert not dag.has_failures()

    def test_skipped_counts_as_complete(self):
        dag = self._make_dag(
            [
                DagStep(name="a", manifest="a.yaml", status="failed"),
                DagStep(name="b", manifest="b.yaml", status="skipped"),
            ]
        )
        assert dag.is_complete()


# =============================================================================
# load_dag Tests
# =============================================================================


class TestLoadDag:
    def test_load_valid_dag(self, tmp_path):
        dag_yaml = {
            "name": "my-pipeline",
            "region": "us-east-1",
            "namespace": "gco-jobs",
            "steps": [
                {"name": "step1", "manifest": "a.yaml"},
                {"name": "step2", "manifest": "b.yaml", "depends_on": ["step1"]},
            ],
        }
        dag_file = tmp_path / "dag.yaml"
        dag_file.write_text(yaml.dump(dag_yaml))

        dag = load_dag(str(dag_file))
        assert dag.name == "my-pipeline"
        assert dag.region == "us-east-1"
        assert len(dag.steps) == 2
        assert dag.steps[1].depends_on == ["step1"]

    def test_load_minimal_dag(self, tmp_path):
        dag_yaml = {
            "steps": [{"name": "only", "manifest": "job.yaml"}],
        }
        dag_file = tmp_path / "dag.yaml"
        dag_file.write_text(yaml.dump(dag_yaml))

        dag = load_dag(str(dag_file))
        assert dag.name == "dag"  # defaults to filename stem
        assert dag.namespace == "gco-jobs"
        assert dag.region is None

    def test_load_example_dag(self):
        dag = load_dag("examples/pipeline-dag.yaml")
        assert dag.name == "example-pipeline"
        assert len(dag.steps) == 2
        assert dag.steps[1].depends_on == ["preprocess"]


# =============================================================================
# DagRunner Tests
# =============================================================================


class TestDagRunner:
    @patch("cli.dag.get_job_manager")
    def test_run_simple_dag(self, mock_jm_factory):
        mock_jm = MagicMock()
        mock_jm_factory.return_value = mock_jm
        mock_jm._aws_client.discover_regional_stacks.return_value = {"us-east-1": {}}
        mock_jm.load_manifests.return_value = [{"metadata": {"name": "test-job"}}]

        # Mock wait_for_job to return a completed job
        mock_job = MagicMock()
        mock_job.status = "succeeded"
        mock_job.is_complete = True
        mock_jm.wait_for_job.return_value = mock_job

        dag = DagDefinition(
            name="test",
            steps=[DagStep(name="a", manifest="examples/simple-job.yaml")],
            region="us-east-1",
        )

        runner = DagRunner(job_manager=mock_jm)
        result = runner.run(dag)

        assert result.steps[0].status == "succeeded"
        assert not result.has_failures()

    @patch("cli.dag.get_job_manager")
    def test_run_dag_with_failure_skips_downstream(self, mock_jm_factory):
        mock_jm = MagicMock()
        mock_jm_factory.return_value = mock_jm
        mock_jm._aws_client.discover_regional_stacks.return_value = {"us-east-1": {}}
        mock_jm.load_manifests.return_value = [{"metadata": {"name": "test-job"}}]

        # First job fails
        mock_jm.submit_job.side_effect = Exception("Submit failed")

        dag = DagDefinition(
            name="test",
            steps=[
                DagStep(name="a", manifest="examples/simple-job.yaml"),
                DagStep(name="b", manifest="examples/simple-job.yaml", depends_on=["a"]),
            ],
            region="us-east-1",
        )

        runner = DagRunner(job_manager=mock_jm)
        result = runner.run(dag)

        assert result.steps[0].status == "failed"
        assert result.steps[1].status == "skipped"
        assert result.has_failures()

    @patch("cli.dag.get_job_manager")
    def test_run_dag_failure_skips_transitive_dependents(self, mock_jm_factory):
        mock_jm = MagicMock()
        mock_jm_factory.return_value = mock_jm
        mock_jm.submit_job.side_effect = RuntimeError("submit failed")
        mock_jm.load_manifests.return_value = [{"kind": "Job", "metadata": {"name": "a"}}]

        dag = DagDefinition(
            name="transitive-failure",
            steps=[
                DagStep(name="a", manifest="examples/simple-job.yaml"),
                DagStep(name="b", manifest="examples/simple-job.yaml", depends_on=["a"]),
                DagStep(name="c", manifest="examples/simple-job.yaml", depends_on=["b"]),
            ],
            region="us-east-1",
        )

        result = DagRunner(job_manager=mock_jm).run(dag)

        assert [step.status for step in result.steps] == ["failed", "skipped", "skipped"]
        mock_jm.submit_job.assert_called_once()

    @patch("cli.dag.get_job_manager")
    def test_run_waits_for_returned_job_identity(self, mock_jm_factory):
        mock_jm = MagicMock()
        mock_jm_factory.return_value = mock_jm
        mock_jm.load_manifests.return_value = [
            {"kind": "Job", "metadata": {"name": "requested", "namespace": "requested-ns"}}
        ]
        mock_jm.submit_job.return_value = {
            "resources": [{"kind": "Job", "name": "generated-abc", "namespace": "actual-ns"}]
        }
        mock_jm.wait_for_job.return_value = MagicMock(status="succeeded")
        dag = DagDefinition(
            name="generated-name",
            steps=[DagStep(name="a", manifest="examples/simple-job.yaml")],
            region="us-east-1",
        )

        result = DagRunner(job_manager=mock_jm).run(dag)

        assert result.steps[0].job_name == "generated-abc"
        mock_jm.wait_for_job.assert_called_once_with(
            job_name="generated-abc",
            namespace="actual-ns",
            region="us-east-1",
            timeout_seconds=3600,
            poll_interval=10,
        )

    @patch("cli.dag.get_job_manager")
    def test_run_dag_no_regions(self, mock_jm_factory):
        mock_jm = MagicMock()
        mock_jm_factory.return_value = mock_jm
        mock_jm._aws_client.discover_regional_stacks.return_value = {}

        dag = DagDefinition(
            name="test",
            steps=[DagStep(name="a", manifest="a.yaml")],
        )

        runner = DagRunner(job_manager=mock_jm)
        with pytest.raises(ValueError, match="No deployed regions"):
            runner.run(dag)


# =============================================================================
# CLI Tests
# =============================================================================


class TestDagCLI:
    def test_dag_help(self):
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "--help"])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "validate" in result.output

    def test_dag_validate_example(self):
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "validate", "examples/pipeline-dag.yaml"])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_dag_dry_run_example(self):
        from cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "run", "examples/pipeline-dag.yaml", "--dry-run"])
        assert result.exit_code == 0
        assert "Execution order" in result.output
        assert "preprocess" in result.output
        assert "train" in result.output

    def test_dag_validate_invalid(self, tmp_path):
        from cli.main import cli

        dag_yaml = {
            "steps": [
                {"name": "a", "manifest": "/nonexistent.yaml", "depends_on": ["b"]},
            ],
        }
        dag_file = tmp_path / "bad.yaml"
        dag_file.write_text(yaml.dump(dag_yaml))

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "validate", str(dag_file)])
        assert result.exit_code == 1
        assert "unknown step" in result.output


# =============================================================================
# Extended DAG Tests
# =============================================================================


class TestDagDefinitionExtended:
    """Extended tests for DagDefinition."""

    def test_three_level_chain(self):
        dag = DagDefinition(
            name="chain",
            steps=[
                DagStep(name="a", manifest="a.yaml", status="succeeded"),
                DagStep(name="b", manifest="b.yaml", depends_on=["a"], status="succeeded"),
                DagStep(name="c", manifest="c.yaml", depends_on=["b"]),
            ],
        )
        ready = dag.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].name == "c"

    def test_parallel_steps(self):
        dag = DagDefinition(
            name="parallel",
            steps=[
                DagStep(name="a", manifest="a.yaml"),
                DagStep(name="b", manifest="b.yaml"),
                DagStep(name="c", manifest="c.yaml"),
            ],
        )
        ready = dag.get_ready_steps()
        assert len(ready) == 3

    def test_diamond_dependency(self):
        dag = DagDefinition(
            name="diamond",
            steps=[
                DagStep(name="a", manifest="a.yaml", status="succeeded"),
                DagStep(name="b", manifest="b.yaml", depends_on=["a"], status="succeeded"),
                DagStep(name="c", manifest="c.yaml", depends_on=["a"], status="succeeded"),
                DagStep(name="d", manifest="d.yaml", depends_on=["b", "c"]),
            ],
        )
        ready = dag.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].name == "d"

    def test_diamond_blocked(self):
        dag = DagDefinition(
            name="diamond",
            steps=[
                DagStep(name="a", manifest="a.yaml", status="succeeded"),
                DagStep(name="b", manifest="b.yaml", depends_on=["a"], status="succeeded"),
                DagStep(name="c", manifest="c.yaml", depends_on=["a"], status="running"),
                DagStep(name="d", manifest="d.yaml", depends_on=["b", "c"]),
            ],
        )
        ready = dag.get_ready_steps()
        assert len(ready) == 0  # d blocked because c is still running

    def test_validate_self_dependency(self, tmp_path):
        (tmp_path / "a.yaml").write_text("kind: Job")
        dag = DagDefinition(
            name="self",
            steps=[DagStep(name="a", manifest=str(tmp_path / "a.yaml"), depends_on=["a"])],
        )
        errors = dag.validate()
        assert any("Cycle" in e for e in errors)


class TestDagRunnerExtended:
    """Extended DagRunner tests."""

    @patch("cli.dag.get_job_manager")
    def test_run_with_timeout(self, mock_jm_factory):
        mock_jm = MagicMock()
        mock_jm_factory.return_value = mock_jm
        mock_jm._aws_client.discover_regional_stacks.return_value = {"us-east-1": {}}
        mock_jm.load_manifests.return_value = [{"metadata": {"name": "test"}}]
        mock_jm.wait_for_job.side_effect = TimeoutError("Timed out")

        dag = DagDefinition(
            name="timeout",
            steps=[
                DagStep(name="slow", manifest="examples/simple-job.yaml"),
                DagStep(name="after", manifest="examples/simple-job.yaml", depends_on=["slow"]),
            ],
            region="us-east-1",
        )

        runner = DagRunner(job_manager=mock_jm)
        result = runner.run(dag)

        assert result.steps[0].status == "failed"
        assert "Timed" in (result.steps[0].error or "")
        assert result.steps[1].status == "skipped"

    @patch("cli.dag.get_job_manager")
    def test_run_with_progress_callback(self, mock_jm_factory):
        mock_jm = MagicMock()
        mock_jm_factory.return_value = mock_jm
        mock_jm._aws_client.discover_regional_stacks.return_value = {"us-east-1": {}}
        mock_jm.load_manifests.return_value = [{"metadata": {"name": "test"}}]

        mock_job = MagicMock()
        mock_job.status = "succeeded"
        mock_job.is_complete = True
        mock_jm.wait_for_job.return_value = mock_job

        events = []

        def callback(step_name, status, msg):
            events.append((step_name, status))

        dag = DagDefinition(
            name="cb",
            steps=[DagStep(name="a", manifest="examples/simple-job.yaml")],
            region="us-east-1",
        )

        runner = DagRunner(job_manager=mock_jm)
        runner.run(dag, progress_callback=callback)

        statuses = [s for _, s in events]
        assert "started" in statuses
        assert "running" in statuses
        assert "succeeded" in statuses


# ============================================================================
# DAG CLI command coverage — run and validate error paths
# ============================================================================


class TestDagCmdRunCoverage:
    """Cover dag run command error and dry-run paths."""

    def test_validation_errors_exit_1(self, tmp_path):
        from cli.main import cli

        dag_yaml = {
            "name": "bad",
            "steps": [
                {"name": "a", "manifest": "/nonexistent.yaml", "depends_on": ["missing"]},
            ],
        }
        dag_file = tmp_path / "bad.yaml"
        dag_file.write_text(yaml.dump(dag_yaml))

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "run", str(dag_file)])
        assert result.exit_code == 1

    def test_dry_run_topological_order(self, tmp_path):
        from cli.main import cli

        m_a = tmp_path / "a.yaml"
        m_b = tmp_path / "b.yaml"
        m_a.write_text("kind: Job\napiVersion: batch/v1")
        m_b.write_text("kind: Job\napiVersion: batch/v1")

        dag_yaml = {
            "name": "test-dag",
            "steps": [
                {"name": "preprocess", "manifest": str(m_a)},
                {"name": "train", "manifest": str(m_b), "depends_on": ["preprocess"]},
            ],
        }
        dag_file = tmp_path / "dag.yaml"
        dag_file.write_text(yaml.dump(dag_yaml))

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "run", str(dag_file), "--dry-run"])
        assert result.exit_code == 0
        assert "Execution order" in result.output
        assert "preprocess" in result.output
        assert "train" in result.output

    @patch("cli.dag.get_dag_runner")
    def test_failure_exits_1(self, mock_runner_fn, tmp_path):
        from cli.dag import DagDefinition, DagStep
        from cli.main import cli

        m = tmp_path / "job.yaml"
        m.write_text("kind: Job\napiVersion: batch/v1")
        dag_yaml = {"name": "fail", "steps": [{"name": "s1", "manifest": str(m)}]}
        dag_file = tmp_path / "dag.yaml"
        dag_file.write_text(yaml.dump(dag_yaml))

        mock_runner = MagicMock()
        mock_runner.run.return_value = DagDefinition(
            name="fail",
            steps=[DagStep(name="s1", manifest=str(m), status="failed")],
        )
        mock_runner_fn.return_value = mock_runner

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "run", str(dag_file)])
        assert result.exit_code == 1

    @patch("cli.dag.load_dag")
    def test_exception_during_load(self, mock_load, tmp_path):
        from cli.main import cli

        dag_file = tmp_path / "dag.yaml"
        dag_file.write_text("name: test\nsteps: []")
        mock_load.side_effect = RuntimeError("parse error")

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "run", str(dag_file)])
        assert result.exit_code == 1
        assert "DAG execution failed" in result.output


class TestDagCmdValidateCoverage:
    """Cover dag validate error path."""

    @patch("cli.dag.load_dag")
    def test_load_error(self, mock_load, tmp_path):
        from cli.main import cli

        dag_file = tmp_path / "dag.yaml"
        dag_file.write_text("name: test\nsteps: []")
        mock_load.side_effect = RuntimeError("bad yaml")

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "validate", str(dag_file)])
        assert result.exit_code == 1
        assert "Failed to load DAG" in result.output


class TestDagRunProgressCallback:
    """Drive every branch of ``dag_cmd.dag_run.on_progress``.

    The callback fires per-step from inside ``DagRunner.run`` with one
    of six status values — ``started``, ``running``, ``succeeded``,
    ``failed``, ``skipped``, or anything containing ``completed``.
    Each branch routes to a different formatter call. The existing
    happy-path test stubs ``runner.run`` with a return value but never
    invokes the callback, leaving lines 74-86 of ``cli/commands/dag_cmd.py``
    untested.
    """

    @patch("cli.dag.get_dag_runner")
    def test_every_status_branch(self, mock_runner_fn, tmp_path):
        from cli.dag import DagDefinition, DagStep
        from cli.main import cli

        m = tmp_path / "job.yaml"
        m.write_text("kind: Job\napiVersion: batch/v1")
        dag_yaml = {
            "name": "demo",
            "steps": [
                {"name": "s1", "manifest": str(m)},
                {"name": "s2", "manifest": str(m), "depends_on": ["s1"]},
            ],
        }
        dag_file = tmp_path / "dag.yaml"
        dag_file.write_text(yaml.dump(dag_yaml))

        # Stub runner that drives the progress_callback through every
        # branch before returning a successful DagDefinition.
        def fake_run(_dag_def, *, region, timeout_per_step, progress_callback):
            progress_callback("s1", "started", "Starting s1")
            progress_callback("s1", "running", "kubectl apply...")
            progress_callback("s1", "succeeded", "ok")
            progress_callback("s2", "skipped", "dep failed")
            progress_callback("s2", "failed", "boom")
            progress_callback("", "completed in 1s", "DAG done")
            return DagDefinition(
                name="demo",
                steps=[
                    DagStep(name="s1", manifest=str(m), status="succeeded"),
                    DagStep(name="s2", manifest=str(m), status="failed"),
                ],
            )

        mock_runner = MagicMock()
        mock_runner.run.side_effect = fake_run
        mock_runner_fn.return_value = mock_runner

        runner = CliRunner()
        result = runner.invoke(cli, ["dag", "run", str(dag_file)])

        # Failures in the returned DagDefinition propagate as exit 1.
        assert result.exit_code == 1
        # And every status branch surfaced its expected formatter call.
        assert "Starting s1" in result.output
        assert "kubectl apply" in result.output
        assert "Completed" in result.output  # succeeded branch
        assert "dep failed" in result.output  # skipped branch
        assert "boom" in result.output  # failed branch
        assert "DAG done" in result.output  # "completed" branch
