"""Repository-wide contracts for root JSON/YAML output normalization."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import click
import pytest
import yaml
from click.testing import CliRunner

from cli.config import GCOConfig
from cli.output import OutputFormatter, StructuredOutputGroup


def _contract_cli() -> click.Group:
    @click.group(cls=StructuredOutputGroup)
    @click.option(
        "--output",
        "output_format",
        type=click.Choice(["table", "json", "yaml"]),
        default="table",
    )
    @click.pass_context
    def root(ctx: click.Context, output_format: str) -> None:
        ctx.obj = GCOConfig(output_format=output_format)

    @root.command()
    @click.pass_obj
    def document(config: GCOConfig) -> None:
        OutputFormatter(config).print({"value": 1})

    @root.command()
    def empty() -> None:
        return None

    @root.command()
    @click.pass_obj
    def scalar(config: GCOConfig) -> None:
        OutputFormatter(config).print("hello")

    @root.command()
    @click.pass_obj
    def null(config: GCOConfig) -> None:
        OutputFormatter(config).print(None)

    @root.command()
    def raw() -> None:
        click.echo("human output")

    @root.command("raw-json")
    def raw_json() -> None:
        click.echo(json.dumps({"looks": "structured"}))

    @root.command("raw-yaml-multiline")
    def raw_yaml_multiline() -> None:
        click.echo("line one\nline two")

    @root.command("raw-yaml-mapping")
    def raw_yaml_mapping() -> None:
        click.echo("warning: detail")

    @root.command("raw-yaml-list")
    def raw_yaml_list() -> None:
        click.echo("- first\n- second")

    @root.command()
    @click.pass_obj
    def messages(config: GCOConfig) -> None:
        formatter = OutputFormatter(config)
        formatter.print_info("working")
        formatter.print_success("done")

    @root.command()
    @click.pass_obj
    def multiple(config: GCOConfig) -> None:
        formatter = OutputFormatter(config)
        formatter.print({"first": 1})
        formatter.print({"second": 2})

    @root.command()
    @click.pass_obj
    def warning(config: GCOConfig) -> None:
        click.echo("advisory", err=True)
        OutputFormatter(config).print({"ok": True})

    @root.command()
    def fail() -> None:
        click.echo("partial stdout")
        click.echo("failure detail", err=True)
        raise click.exceptions.Exit(7)

    @root.command()
    def crash() -> None:
        click.echo("partial crash output")
        raise RuntimeError("boom")

    @root.command("exit-zero")
    @click.pass_obj
    def exit_zero(config: GCOConfig) -> None:
        OutputFormatter(config).print({"ok": True})
        raise click.exceptions.Exit(0)

    @root.command()
    def table() -> None:
        click.echo("table output")

    return root


def _load_one(stdout: str, output_format: str):
    if output_format == "json":
        return json.loads(stdout)
    documents = list(yaml.safe_load_all(stdout))
    assert len(documents) == 1
    return documents[0]


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_existing_document_keeps_its_schema_and_requested_format(output_format: str) -> None:
    result = CliRunner().invoke(_contract_cli(), ["--output", output_format, "document"])

    assert result.exit_code == 0, result.output
    assert _load_one(result.stdout, output_format) == {"value": 1}
    if output_format == "yaml":
        assert not result.stdout.lstrip().startswith("{")


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_empty_and_human_message_successes_get_one_status_document(output_format: str) -> None:
    for command in ("empty", "messages"):
        result = CliRunner().invoke(_contract_cli(), ["--output", output_format, command])
        assert result.exit_code == 0, result.output
        assert _load_one(result.stdout, output_format) == {"status": "ok"}
        assert result.stderr == ""


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_scalar_and_raw_output_are_wrapped_without_prose_leakage(output_format: str) -> None:
    scalar = CliRunner().invoke(_contract_cli(), ["--output", output_format, "scalar"])
    null = CliRunner().invoke(_contract_cli(), ["--output", output_format, "null"])
    raw = CliRunner().invoke(_contract_cli(), ["--output", output_format, "raw"])

    assert _load_one(scalar.stdout, output_format) == {"status": "ok", "result": "hello"}
    assert _load_one(null.stdout, output_format) is None
    assert _load_one(raw.stdout, output_format) == {
        "status": "ok",
        "output": "human output",
    }


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_unregistered_json_looking_text_is_still_raw(output_format: str) -> None:
    result = CliRunner().invoke(_contract_cli(), ["--output", output_format, "raw-json"])

    assert result.exit_code == 0, result.output
    assert _load_one(result.stdout, output_format) == {
        "status": "ok",
        "output": '{"looks": "structured"}',
    }


@pytest.mark.parametrize(
    ("command", "raw_text"),
    [
        ("raw-yaml-multiline", "line one\nline two"),
        ("raw-yaml-mapping", "warning: detail"),
        ("raw-yaml-list", "- first\n- second"),
    ],
)
def test_yaml_like_prose_is_not_reinterpreted(command: str, raw_text: str) -> None:
    result = CliRunner().invoke(_contract_cli(), ["--output", "yaml", command])

    assert result.exit_code == 0, result.output
    assert _load_one(result.stdout, "yaml") == {
        "status": "ok",
        "output": raw_text,
    }


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_multiple_documents_are_contained_in_one_output_envelope(output_format: str) -> None:
    result = CliRunner().invoke(_contract_cli(), ["--output", output_format, "multiple"])

    assert result.exit_code == 0, result.output
    payload = _load_one(result.stdout, output_format)
    assert payload["status"] == "ok"
    assert "first" in payload["output"]
    assert "second" in payload["output"]


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_stderr_warning_stays_separate_from_one_success_document(output_format: str) -> None:
    result = CliRunner().invoke(_contract_cli(), ["--output", output_format, "warning"])

    assert result.exit_code == 0
    assert _load_one(result.stdout, output_format) == {"ok": True}
    assert result.stderr == "advisory\n"


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_nonzero_exit_preserves_original_streams_and_exit_code(output_format: str) -> None:
    result = CliRunner().invoke(_contract_cli(), ["--output", output_format, "fail"])

    assert result.exit_code == 7
    assert result.stdout == "partial stdout\n"
    assert result.stderr == "failure detail\n"


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_unexpected_exception_preserves_stdout_for_existing_error_handling(
    output_format: str,
) -> None:
    result = CliRunner().invoke(_contract_cli(), ["--output", output_format, "crash"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert result.stdout == "partial crash output\n"


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_explicit_zero_exit_is_still_normalized(output_format: str) -> None:
    result = CliRunner().invoke(_contract_cli(), ["--output", output_format, "exit-zero"])

    assert result.exit_code == 0
    assert _load_one(result.stdout, output_format) == {"ok": True}


def test_table_mode_is_byte_for_byte_unaffected() -> None:
    result = CliRunner().invoke(_contract_cli(), ["table"])

    assert result.exit_code == 0
    assert result.stdout == "table output\n"


def test_real_stdout_capture_includes_inherited_child_process_output() -> None:
    script = """
import json
import subprocess
import sys
from cli.output import _StdoutCapture

capture = _StdoutCapture()
with capture:
    print("python output")
    subprocess.run([sys.executable, "-c", "print('child output')"], check=True)
print(json.dumps(capture.text))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    captured = json.loads(completed.stdout)

    assert "python output" in captured
    assert "child output" in captured


def test_fd_capture_path_is_covered_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.output import _StdoutCapture

    original_stdout = sys.stdout
    fd_stream = os.fdopen(os.dup(1), "w", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", fd_stream)
    try:
        capture = _StdoutCapture()
        with capture:
            print("fd-backed output")
    finally:
        monkeypatch.setattr(sys, "stdout", original_stdout)
        fd_stream.close()

    assert capture.text == "fd-backed output\n"


@pytest.mark.parametrize("failure_point", ["temporary", "dup2"])
def test_fd_setup_failure_falls_back_to_python_capture(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import cli.output as output_module

    original_stdout = sys.stdout
    fd_stream = os.fdopen(os.dup(1), "w", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", fd_stream)

    def fail(*_args, **_kwargs):
        raise OSError("capture setup failed")

    original_dup2 = output_module.os.dup2
    if failure_point == "temporary":
        monkeypatch.setattr(output_module.tempfile, "TemporaryFile", fail)
    else:
        monkeypatch.setattr(output_module.os, "dup2", fail)

    try:
        capture = output_module._StdoutCapture()
        with capture:
            print("fallback output")
    finally:
        if failure_point == "dup2":
            monkeypatch.setattr(output_module.os, "dup2", original_dup2)
        monkeypatch.setattr(sys, "stdout", original_stdout)
        fd_stream.close()

    assert capture.text == "fallback output\n"


def test_multiple_yaml_documents_use_one_raw_output_envelope() -> None:
    from cli.output import _structured_document_from_text

    payload = _structured_document_from_text("---\nfirst: 1\n---\nsecond: 2\n", "yaml")
    assert payload == {
        "status": "ok",
        "output": "---\nfirst: 1\n---\nsecond: 2",
    }


def test_configured_machine_format_is_used_without_explicit_root_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.output as output_module

    @click.group(cls=StructuredOutputGroup)
    @click.option("--output", "output_format", default=None)
    def configured_root(output_format: str | None) -> None:
        del output_format

    @configured_root.command()
    def leaf() -> None:
        OutputFormatter(GCOConfig(output_format="json")).print({"configured": True})

    monkeypatch.setattr(
        output_module,
        "get_config",
        lambda _path=None: GCOConfig(output_format="json"),
    )
    result = CliRunner().invoke(configured_root, ["leaf"])
    help_result = CliRunner().invoke(configured_root, ["--help"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"configured": True}
    assert help_result.exit_code == 0
    help_payload = json.loads(help_result.stdout)
    assert help_payload["status"] == "ok"
    assert "Usage:" in help_payload["output"]


def test_every_registered_cli_subsystem_is_owned_by_the_structured_root() -> None:
    from cli.main import cli

    assert isinstance(cli, StructuredOutputGroup)
    assert len(cli.commands) == 27
    assert {
        "analytics",
        "capacity",
        "inference",
        "jobs",
        "mission",
        "stacks",
        "storage",
        "swarm",
        "vector",
    } <= set(cli.commands)


def test_concurrent_fd_captures_are_serialized_and_restore_stdout() -> None:
    script = r"""
import json
import threading
import time
from cli.output import _StdoutCapture

entered = threading.Event()
attempting = threading.Event()
release = threading.Event()
results = {}

def first():
    capture = _StdoutCapture()
    with capture:
        print("first-only")
        entered.set()
        release.wait(timeout=5)
    results["first"] = capture.text

def second():
    entered.wait(timeout=5)
    attempting.set()
    capture = _StdoutCapture()
    with capture:
        print("second-only")
    results["second"] = capture.text

thread_a = threading.Thread(target=first)
thread_b = threading.Thread(target=second)
thread_a.start()
thread_b.start()
attempting.wait(timeout=5)
time.sleep(0.05)
release.set()
thread_a.join(timeout=5)
thread_b.join(timeout=5)
print(json.dumps(results, sort_keys=True))
print("stdout-restored")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    lines = completed.stdout.splitlines()
    assert json.loads(lines[0]) == {
        "first": "first-only\n",
        "second": "second-only\n",
    }
    assert lines[1] == "stdout-restored"


def test_nested_fd_captures_restore_in_lifo_order() -> None:
    script = r"""
import json
from cli.output import _StdoutCapture

outer = _StdoutCapture()
inner = _StdoutCapture()
with outer:
    print("outer-before")
    with inner:
        print("inner-only")
    print("outer-after")
print(json.dumps({"outer": outer.text, "inner": inner.text}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "inner": "inner-only\n",
        "outer": "outer-before\nouter-after\n",
    }


def test_root_output_scanner_ignores_subcommand_local_output_option() -> None:
    from cli.output import _root_output_settings

    assert _root_output_settings(["mission", "start", "--output", "yaml"]) == (None, None)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_json_formatter_rejects_nonstandard_numeric_constants(value: float) -> None:
    formatter = OutputFormatter(GCOConfig(output_format="json"))

    with pytest.raises(ValueError, match="Out of range float values"):
        formatter.format({"value": value})


def test_concurrent_structured_transactions_hold_lock_through_commit() -> None:
    script = r"""
import json
import threading
import time

import click
import cli.output as output_module
from cli.config import GCOConfig
from cli.output import OutputFormatter, StructuredOutputGroup

commit_started = threading.Event()
release_commit = threading.Event()
second_attempting = threading.Event()
errors = []
original_write = output_module._write_structured_capture

def delayed_write(text, output_format, emissions):
    if "first" in text:
        commit_started.set()
        release_commit.wait(timeout=5)
    original_write(text, output_format, emissions)

output_module._write_structured_capture = delayed_write

def make_cli(name):
    @click.group(cls=StructuredOutputGroup)
    @click.option("--output", "output_format", default="table")
    @click.pass_context
    def root(ctx, output_format):
        ctx.obj = GCOConfig(output_format=output_format)

    @root.command()
    @click.pass_obj
    def emit(config):
        OutputFormatter(config).print({"name": name})

    return root

def invoke(command, mark_attempt=False):
    try:
        if mark_attempt:
            second_attempting.set()
        command.main(args=["--output", "json", "emit"], standalone_mode=False)
    except BaseException as exc:
        errors.append(repr(exc))

first = threading.Thread(target=invoke, args=(make_cli("first"),))
second = threading.Thread(target=invoke, args=(make_cli("second"), True))
first.start()
commit_started.wait(timeout=5)
second.start()
second_attempting.wait(timeout=5)
time.sleep(0.05)
release_commit.set()
first.join(timeout=5)
second.join(timeout=5)
if errors:
    raise RuntimeError(errors)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    decoder = json.JSONDecoder()
    documents = []
    remaining = completed.stdout
    while remaining.strip():
        document, end = decoder.raw_decode(remaining.lstrip())
        documents.append(document)
        remaining = remaining.lstrip()[end:]
    assert documents == [{"name": "first"}, {"name": "second"}]


def test_fd_restore_retries_without_stranding_stdout() -> None:
    script = r"""
import json
import cli.output as output_module

real_dup2 = output_module.os.dup2
call_count = 0

def flaky_dup2(source, target):
    global call_count
    call_count += 1
    if call_count == 2:
        raise OSError("transient restore failure")
    return real_dup2(source, target)

output_module.os.dup2 = flaky_dup2
capture = output_module._StdoutCapture()
with capture:
    print("captured")
print(json.dumps({"captured": capture.text, "dup2_calls": call_count}))
print("stdout-restored")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.splitlines()
    assert json.loads(lines[0]) == {
        "captured": "captured\n",
        "dup2_calls": 3,
    }
    assert lines[1] == "stdout-restored"


def test_registered_cli_code_uses_machine_safe_prompt_helpers() -> None:
    cli_root = Path(__file__).resolve().parents[1] / "cli"
    wrapper_path = cli_root / "output.py"
    offenders: list[str] = []
    for path in cli_root.rglob("*.py"):
        if path == wrapper_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "input":
                offenders.append(f"{path.relative_to(cli_root)}:{node.lineno}: input")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "click"
                and node.func.attr in {"confirm", "prompt"}
            ):
                offenders.append(
                    f"{path.relative_to(cli_root)}:{node.lineno}: click.{node.func.attr}"
                )

    assert offenders == []


def test_crlf_fd_stream_preserves_registered_native_schema() -> None:
    script = r"""
import json
import os
import sys

import click
from cli.config import GCOConfig
from cli.output import OutputFormatter, StructuredOutputGroup

fd_stream = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\r\n")
sys.stdout = fd_stream

@click.group(cls=StructuredOutputGroup)
@click.option("--output", "output_format", default="table")
@click.pass_context
def root(ctx, output_format):
    ctx.obj = GCOConfig(output_format=output_format)

@root.command()
@click.pass_obj
def emit(config):
    OutputFormatter(config).print({"value": 1})

root.main(args=["--output", "json", "emit"], standalone_mode=False)
fd_stream.flush()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
    )

    assert b"\r\n" in completed.stdout
    assert json.loads(completed.stdout) == {"value": 1}


def test_fd_capture_decodes_with_original_stream_encoding() -> None:
    script = r"""
import json
import os
import sys
from cli.output import _StdoutCapture

original_stdout = sys.stdout
latin1_stdout = os.fdopen(os.dup(1), "w", encoding="latin-1")
sys.stdout = latin1_stdout
capture = _StdoutCapture()
with capture:
    print("caf\N{LATIN SMALL LETTER E WITH ACUTE}")
sys.stdout = original_stdout
latin1_stdout.close()
print(json.dumps(capture.text))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == "café\n"


def test_crlf_raw_success_removes_complete_trailing_line_terminator() -> None:
    script = r"""
import os
import sys

import click
from cli.output import StructuredOutputGroup

fd_stream = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\r\n")
sys.stdout = fd_stream

@click.group(cls=StructuredOutputGroup)
@click.option("--output", "output_format", default="table")
def root(output_format):
    pass

@root.command()
def raw():
    click.echo("line one\nline two")

root.main(args=["--output", "json", "raw"], standalone_mode=False)
fd_stream.flush()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
    )

    assert json.loads(completed.stdout) == {
        "status": "ok",
        "output": "line one\r\nline two",
    }


def test_crlf_nonzero_replay_preserves_original_bytes() -> None:
    script = r"""
import os
import sys

import click
from cli.output import StructuredOutputGroup

fd_stream = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\r\n")
sys.stdout = fd_stream

@click.group(cls=StructuredOutputGroup)
@click.option("--output", "output_format", default="table")
def root(output_format):
    pass

@root.command()
def fail():
    click.echo("first\nsecond")
    raise click.exceptions.Exit(7)

result = root.main(args=["--output", "json", "fail"], standalone_mode=False)
assert result == 7
fd_stream.flush()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
    )

    assert completed.stdout == b"first\r\nsecond\r\n"


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_mission_scaffold_criteria_keeps_native_sequence_at_root(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    from cli.main import cli

    monkeypatch.setenv("GCO_ENABLE_MISSION", "true")
    result = CliRunner().invoke(
        cli,
        [
            "--output",
            output_format,
            "mission",
            "scaffold-criteria",
            "--directive",
            "Find docs.",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _load_one(result.stdout, output_format)
    assert isinstance(payload, list)
    assert payload
    assert all("criterion_id" in criterion for criterion in payload)


@pytest.mark.parametrize("output_format", ["json", "yaml"])
def test_swarm_file_report_keeps_native_mapping_at_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    output_format: str,
) -> None:
    import importlib

    from cli.main import cli

    swarm_module = importlib.import_module("cli.commands.swarm_cmd")
    monkeypatch.setenv("GCO_ENABLE_SWARM", "true")
    report = {
        "session_id": "swarm-1",
        "status": "completed",
        "final_verdict": "complete",
        "lessons": ["native schema survives"],
    }
    report_path = tmp_path / "swarm-1.report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    with (
        patch.object(
            swarm_module,
            "_scaffold_plan",
            return_value={
                "plan": [{"slot": "worker-1"}],
                "sampling_path": "deterministic",
                "fallback_reason": None,
            },
        ),
        patch.object(
            swarm_module,
            "_persist_orchestrator",
            return_value={"session_id": "swarm-1"},
        ),
        patch.object(
            swarm_module,
            "_drive",
            return_value={
                **report,
                "final_report_path": str(report_path),
            },
        ),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--output",
                output_format,
                "swarm",
                "run",
                "--directive",
                "Coordinate one worker.",
            ],
        )

    assert result.exit_code == 0, result.output
    assert _load_one(result.stdout, output_format) == report
