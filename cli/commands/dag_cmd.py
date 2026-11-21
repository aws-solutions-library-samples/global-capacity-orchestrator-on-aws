"""DAG pipeline commands."""

import dataclasses
import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


def _execution_order(dag_def: Any) -> list[Any]:
    """The DAG's steps in dependency order (each step after its dependencies).

    Only called on a validated DAG, which has no cycles or unknown
    dependencies, so every pass places at least one step.
    """
    completed: set[str] = set()
    ordered: list[Any] = []
    remaining = list(dag_def.steps)
    while remaining:
        batch = [s for s in remaining if all(d in completed for d in s.depends_on)]
        for step in batch:
            ordered.append(step)
            completed.add(step.name)
            remaining.remove(step)
    return ordered


def _definition_document(dag_def: Any, errors: list[str]) -> dict[str, Any]:
    """The machine-readable answer of ``dag validate`` and ``dag run --dry-run``."""
    document: dict[str, Any] = {
        "name": dag_def.name,
        "valid": not errors,
        "errors": list(errors),
        "region": dag_def.region,
        "namespace": dag_def.namespace,
        "steps": [
            {"name": step.name, "manifest": step.manifest, "depends_on": list(step.depends_on)}
            for step in dag_def.steps
        ],
    }
    if not errors:
        document["execution_order"] = [step.name for step in _execution_order(dag_def)]
    return document


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
    and downstream steps are skipped if a dependency fails. With -o
    json/yaml the command prints one document: the steps with their final
    status and a top-level status of succeeded or failed (exit 1 when a step
    failed), or for --dry-run and an invalid DAG the same document as
    `gco dag validate`.

    Examples:
        gco dag run pipeline.yaml
        gco dag run pipeline.yaml -r us-east-1
        gco dag run pipeline.yaml --dry-run
    """
    from ..dag import get_dag_runner, load_dag

    formatter = get_output_formatter(config)
    machine = config.output_format != "table"

    try:
        dag_def = load_dag(dag_file)
        errors = dag_def.validate()

        if errors:
            for err in errors:
                formatter.print_error(err)
            if machine:
                formatter.print(_definition_document(dag_def, errors))
            sys.exit(1)

        if dry_run:
            if machine:
                formatter.print(_definition_document(dag_def, []))
                return
            formatter.print_success(f"DAG '{dag_def.name}' is valid ({len(dag_def.steps)} steps)")
            print("\n  Execution order:")
            for order, step in enumerate(_execution_order(dag_def), start=1):
                deps = f" (after: {', '.join(step.depends_on)})" if step.depends_on else ""
                print(f"  {order}. {step.name} → {step.manifest}{deps}")
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
                if not machine:
                    print()
                formatter.print_info(msg)

        result = runner.run(
            dag_def,
            region=region,
            timeout_per_step=timeout,
            progress_callback=on_progress,
        )

        if machine:
            formatter.print(
                {
                    **dataclasses.asdict(result),
                    "status": "failed" if result.has_failures() else "succeeded",
                }
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

    With -o json/yaml it prints one document: name, valid, errors, region,
    namespace, steps and (when valid) execution_order; exit 1 when invalid.

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
            if config.output_format != "table":
                formatter.print(_definition_document(dag_def, errors))
            sys.exit(1)

        if config.output_format != "table":
            formatter.print(_definition_document(dag_def, []))
            return

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
