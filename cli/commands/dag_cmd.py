"""DAG pipeline commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def dag(config: Any) -> None:
    """Run multi-step job pipelines with dependencies."""


@dag.command("run")
@click.argument("dag_file", type=click.Path(exists=True))
@click.option("--region", "-r", help="Region to run in (default: from DAG file or first deployed)")
@click.option(
    "--timeout", "-t", default=3600, type=int, help="Timeout per step in seconds (default: 3600)"
)
@click.option("--dry-run", is_flag=True, help="Validate the DAG without running it")
@pass_config
def dag_run(config: Any, dag_file: Any, region: Any, timeout: Any, dry_run: Any) -> None:
    """Run a DAG pipeline from a YAML definition.

    The DAG file defines steps with dependencies. Steps run in order,
    and downstream steps are skipped if a dependency fails.

    Examples:
        gco dag run pipeline.yaml
        gco dag run pipeline.yaml -r us-east-1
        gco dag run pipeline.yaml --dry-run
    """
    from ..dag import get_dag_runner, load_dag

    formatter = get_output_formatter(config)

    try:
        dag_def = load_dag(dag_file)
        errors = dag_def.validate()

        if errors:
            for err in errors:
                formatter.print_error(err)
            sys.exit(1)

        if dry_run:
            formatter.print_success(f"DAG '{dag_def.name}' is valid ({len(dag_def.steps)} steps)")
            print("\n  Execution order:")
            # Show topological order
            completed: set[str] = set()
            order = 1
            remaining = list(dag_def.steps)
            while remaining:
                batch = [s for s in remaining if all(d in completed for d in s.depends_on)]
                for step in batch:
                    deps = f" (after: {', '.join(step.depends_on)})" if step.depends_on else ""
                    print(f"  {order}. {step.name} → {step.manifest}{deps}")
                    completed.add(step.name)
                    remaining.remove(step)
                    order += 1
            print()
            return

        runner = get_dag_runner(config)

        def on_progress(step_name: str, status: str, msg: str) -> None:
            if status == "started":
                formatter.print_info(msg)
            elif status == "running":
                formatter.print_info(f"  [{step_name}] {msg}")
            elif status == "succeeded":
                formatter.print_success(f"  [{step_name}] ✓ Completed")
            elif status == "failed":
                formatter.print_error(f"  [{step_name}] ✗ {msg}")
            elif status == "skipped":
                formatter.print_info(f"  [{step_name}] ⊘ {msg}")
            elif "completed" in status:
                print()
                formatter.print_info(msg)

        result = runner.run(
            dag_def,
            region=region,
            timeout_per_step=timeout,
            progress_callback=on_progress,
        )

        if result.has_failures():
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"DAG execution failed: {e}")
        sys.exit(1)


@dag.command("validate")
@click.argument("dag_file", type=click.Path(exists=True))
@pass_config
def dag_validate(config: Any, dag_file: Any) -> None:
    """Validate a DAG definition without running it.

    Examples:
        gco dag validate pipeline.yaml
    """
    from ..dag import load_dag

    formatter = get_output_formatter(config)

    try:
        dag_def = load_dag(dag_file)
        errors = dag_def.validate()

        if errors:
            formatter.print_error(f"DAG '{dag_def.name}' has {len(errors)} error(s):")
            for err in errors:
                formatter.print_error(f"  - {err}")
            sys.exit(1)

        formatter.print_success(f"DAG '{dag_def.name}' is valid")
        print(f"  Steps: {len(dag_def.steps)}")
        print(f"  Region: {dag_def.region or '(auto-detect)'}")
        print(f"  Namespace: {dag_def.namespace}")
        for step in dag_def.steps:
            deps = f" → depends on: {', '.join(step.depends_on)}" if step.depends_on else ""
            print(f"    - {step.name}: {step.manifest}{deps}")
        print()

    except Exception as e:
        formatter.print_error(f"Failed to load DAG: {e}")
        sys.exit(1)
