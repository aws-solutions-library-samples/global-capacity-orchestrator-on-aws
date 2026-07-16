"""Job DAG (Directed Acyclic Graph) runner for GCO.

Allows defining multi-step ML pipelines where jobs run in dependency
order. Each step can reference the output of a previous step via
shared EFS storage.

DAG definition format (YAML):
    name: my-pipeline
    region: us-east-1
    namespace: gco-jobs
    steps:
      - name: preprocess
        manifest: examples/preprocess-job.yaml
      - name: train
        manifest: examples/train-job.yaml
        depends_on: [preprocess]
      - name: evaluate
        manifest: examples/evaluate-job.yaml
        depends_on: [train]
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .config import GCOConfig, get_config
from .jobs import JobManager, get_job_manager, resolve_submission_identity

logger = logging.getLogger(__name__)


@dataclass
class DagStep:
    """A single step in a DAG."""

    name: str
    manifest: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"  # pending, running, succeeded, failed, skipped
    job_name: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


@dataclass
class DagDefinition:
    """A DAG pipeline definition."""

    name: str
    steps: list[DagStep]
    region: str | None = None
    namespace: str = "gco-jobs"

    def validate(self) -> list[str]:
        """Validate the DAG structure. Returns list of errors."""
        errors: list[str] = []
        step_names = {s.name for s in self.steps}

        # Check for duplicate step names
        if len(step_names) != len(self.steps):
            errors.append("Duplicate step names found")

        # Check dependencies exist
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in step_names:
                    errors.append(f"Step '{step.name}' depends on unknown step '{dep}'")

        # Check for cycles
        if not errors:
            visited: set[str] = set()
            in_stack: set[str] = set()
            dep_map = {s.name: s.depends_on for s in self.steps}

            def has_cycle(node: str) -> bool:
                visited.add(node)
                in_stack.add(node)
                for dep in dep_map.get(node, []):
                    if dep not in visited:
                        if has_cycle(dep):
                            return True
                    elif dep in in_stack:
                        return True
                in_stack.discard(node)
                return False

            for step in self.steps:
                if step.name not in visited and has_cycle(step.name):
                    errors.append("Cycle detected in DAG dependencies")
                    break

        # Check manifest files exist
        for step in self.steps:
            if not Path(step.manifest).exists():
                errors.append(f"Manifest not found for step '{step.name}': {step.manifest}")

        return errors

    def get_ready_steps(self) -> list[DagStep]:
        """Get steps whose dependencies are all satisfied."""
        completed = {s.name for s in self.steps if s.status == "succeeded"}
        ready = []
        for step in self.steps:
            if step.status != "pending":
                continue
            if all(dep in completed for dep in step.depends_on):
                ready.append(step)
        return ready

    def is_complete(self) -> bool:
        """Check if all steps are done (succeeded, failed, or skipped)."""
        return all(s.status in ("succeeded", "failed", "skipped") for s in self.steps)

    def has_failures(self) -> bool:
        """Check if any step failed."""
        return any(s.status == "failed" for s in self.steps)


def load_dag(path: str) -> DagDefinition:
    """Load a DAG definition from a YAML file."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    steps = []
    for step_data in data.get("steps", []):
        steps.append(
            DagStep(
                name=step_data["name"],
                manifest=step_data["manifest"],
                depends_on=step_data.get("depends_on", []),
            )
        )

    return DagDefinition(
        name=data.get("name", Path(path).stem),
        steps=steps,
        region=data.get("region"),
        namespace=data.get("namespace", "gco-jobs"),
    )


class DagRunner:
    """Executes a DAG by submitting jobs in dependency order."""

    def __init__(
        self,
        config: GCOConfig | None = None,
        job_manager: JobManager | None = None,
    ):
        self.config = config or get_config()
        self.job_manager = job_manager or get_job_manager(config)

    def run(
        self,
        dag: DagDefinition,
        region: str | None = None,
        timeout_per_step: int = 3600,
        poll_interval: int = 10,
        progress_callback: Callable[[str, str, str], None] | None = None,
    ) -> DagDefinition:
        """Execute a DAG, submitting steps as dependencies complete.

        Args:
            dag: The DAG definition to execute
            region: Override region (default: dag.region or first deployed region)
            timeout_per_step: Max seconds to wait per step
            poll_interval: Seconds between status checks
            progress_callback: Optional callable(step_name, status, message)

        Returns:
            The DAG with updated step statuses
        """
        target_region = region or dag.region
        if not target_region:
            stacks = self.job_manager._aws_client.discover_regional_stacks()
            regions = list(stacks.keys())
            if not regions:
                raise ValueError("No deployed regions found")
            target_region = regions[0]

        def _notify(step_name: str, status: str, msg: str) -> None:
            if progress_callback:
                progress_callback(step_name, status, msg)

        _notify(dag.name, "started", f"Running DAG '{dag.name}' with {len(dag.steps)} steps")

        while not dag.is_complete():
            ready = dag.get_ready_steps()

            if not ready:
                # Propagate terminal dependency failures through the entire
                # graph. A dependency skipped because of an earlier failure is
                # just as unsatisfiable as a directly failed dependency.
                pending = [s for s in dag.steps if s.status == "pending"]
                if pending:
                    blocked_names = {s.name for s in dag.steps if s.status in ("failed", "skipped")}
                    skipped_any = False
                    for step in pending:
                        if any(dep in blocked_names for dep in step.depends_on):
                            step.status = "skipped"
                            step.error = "Dependency failed or was skipped"
                            skipped_any = True
                            _notify(step.name, "skipped", "Skipped (dependency unavailable)")
                    if skipped_any:
                        continue

                    # A validated DAG cannot otherwise remain pending here;
                    # terminate conservatively rather than spin forever if a
                    # caller supplied inconsistent pre-existing step states.
                    for step in pending:
                        step.status = "skipped"
                        step.error = "Dependencies could not be satisfied"
                        _notify(step.name, "skipped", "Skipped (dependencies unresolved)")
                    continue
                break

            # Submit all ready steps
            for step in ready:
                try:
                    step.status = "running"
                    step.started_at = datetime.now(UTC).isoformat()
                    _notify(step.name, "running", f"Submitting {step.manifest}")

                    # Load the requested identity only as a fallback. The API
                    # may generate or rename the Job during submission.
                    manifests = self.job_manager.load_manifests(step.manifest)
                    requested_name = step.name
                    requested_namespace = dag.namespace
                    for manifest in manifests:
                        if manifest.get("kind") == "Job":
                            metadata = manifest.get("metadata", {})
                            requested_name = metadata.get("name") or requested_name
                            requested_namespace = metadata.get("namespace") or requested_namespace
                            break

                    submission_result = self.job_manager.submit_job(
                        manifests=step.manifest,
                        namespace=dag.namespace,
                        target_region=target_region,
                    )
                    submitted_name, submitted_namespace = resolve_submission_identity(
                        submission_result,
                        fallback_name=requested_name,
                        fallback_namespace=requested_namespace,
                    )
                    step.job_name = submitted_name or step.name

                    _notify(step.name, "running", f"Job '{step.job_name}' submitted")

                    # Wait for the actual submitted identity, not the requested
                    # manifest name that may no longer exist.
                    job_info = self.job_manager.wait_for_job(
                        job_name=step.job_name,
                        namespace=submitted_namespace or dag.namespace,
                        region=target_region,
                        timeout_seconds=timeout_per_step,
                        poll_interval=poll_interval,
                    )

                    if job_info.status in ("Complete", "Succeeded", "succeeded"):
                        step.status = "succeeded"
                        step.completed_at = datetime.now(UTC).isoformat()
                        _notify(step.name, "succeeded", f"Step '{step.name}' completed")
                    else:
                        step.status = "failed"
                        step.completed_at = datetime.now(UTC).isoformat()
                        step.error = f"Job ended with status: {job_info.status}"
                        _notify(
                            step.name, "failed", f"Step '{step.name}' failed: {job_info.status}"
                        )

                except TimeoutError as e:
                    step.status = "failed"
                    step.completed_at = datetime.now(UTC).isoformat()
                    step.error = str(e)
                    _notify(step.name, "failed", f"Step '{step.name}' timed out")

                except Exception as e:
                    step.status = "failed"
                    step.completed_at = datetime.now(UTC).isoformat()
                    step.error = str(e)
                    _notify(step.name, "failed", f"Step '{step.name}' error: {e}")

        status = "completed" if not dag.has_failures() else "completed with failures"
        succeeded = sum(1 for s in dag.steps if s.status == "succeeded")
        failed = sum(1 for s in dag.steps if s.status == "failed")
        skipped = sum(1 for s in dag.steps if s.status == "skipped")
        _notify(
            dag.name,
            status,
            f"DAG '{dag.name}': {succeeded} succeeded, {failed} failed, {skipped} skipped",
        )

        return dag


def get_dag_runner(config: GCOConfig | None = None) -> DagRunner:
    """Factory function for DagRunner."""
    return DagRunner(config)
