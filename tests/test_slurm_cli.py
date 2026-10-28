"""Tests for ``gco slurm`` — cli/slurm.py and cli/commands/slurm_cmd.py.

The business-logic module is tested directly (time windows, batch scripts
and their ``#SBATCH`` directives, the API body, memory units, the human
renderings, the login-pod ``kubectl exec`` argv and its process wrappers
against a patched ``subprocess.run``). The command group runs through
``CliRunner`` with the API client faked (``cli.aws_client.get_aws_client``)
and, for ``shell`` / ``output``, the cluster tunnel, kubeconfig update and
process wrappers faked the way the Crossplane and GitOps groups' tests do,
proving: region pinning (flag, root default, neither → usage error), the
exact request each command sends, table and machine output, the API error
→ exit 1 path on every command, the cancel confirmation, the window
defaults of ``accounting``, and the exact ``kubectl exec`` argv with and
without tunnel flags.
"""

from __future__ import annotations

import json
import re
import subprocess
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from cli import slurm as slurm_lib
from cli.aws_client import APIRequestError
from cli.main import cli
from cli.slurm import SlurmCliError

_SERVER = "https://127.0.0.1:8443"
_SNI = "abc.gr7.us-east-1.eks.amazonaws.com"
SCRIPT = "#!/bin/bash\n#SBATCH --job-name=train\n#SBATCH -p gpu\nsrun hostname\n"


# ---------------------------------------------------------------------------
# cli/slurm.py
# ---------------------------------------------------------------------------


class TestTimeWindow:
    @pytest.mark.parametrize(
        ("since", "expected"),
        [
            ("30m", "now-30minutes"),
            ("24H", "now-24hours"),
            ("7d", "now-7days"),
            ("2w", "now-2weeks"),
            ("90s", "now-90seconds"),
        ],
    )
    def test_since_renders_slurm_relative_times(self, since, expected):
        assert slurm_lib.slurm_time_window(since, None, None) == {"start": expected}

    @pytest.mark.parametrize("since", ["7", "7 days", "d7", "7y", "0d", ""])
    def test_bad_since_is_refused(self, since):
        if not since:
            assert slurm_lib.slurm_time_window(since, None, None) == {}
            return
        with pytest.raises(SlurmCliError, match="--since"):
            slurm_lib.slurm_time_window(since, None, None)

    def test_since_and_start_are_alternatives(self):
        with pytest.raises(SlurmCliError, match="either --since or --start"):
            slurm_lib.slurm_time_window("1d", "2026-10-01", None)

    @pytest.mark.parametrize(
        "start",
        [
            "2026-10-01",
            "2026-10-01T00:00",
            "2026-10-01 00:00:00",
            "09:30",
            "10/01/26",
            "1700000000",
            "now-2days",
            "now",
        ],
    )
    def test_start_and_end_pass_through_slurm_times(self, start):
        assert slurm_lib.slurm_time_window(None, f" {start} ", start) == {
            "start": start,
            "end": start,
        }

    @pytest.mark.parametrize("value", ["yesterday", "2026-13", "rm -rf /", "now-2fortnights"])
    def test_bad_absolute_times_are_refused(self, value):
        with pytest.raises(SlurmCliError, match="--start"):
            slurm_lib.slurm_time_window(None, value, None)
        with pytest.raises(SlurmCliError, match="--end"):
            slurm_lib.slurm_time_window(None, None, value)


class TestBatchScript:
    def test_reads_a_script(self, tmp_path):
        path = tmp_path / "job.sbatch"
        path.write_text(SCRIPT)
        assert slurm_lib.read_batch_script(path) == SCRIPT

    def test_missing_file(self, tmp_path):
        with pytest.raises(SlurmCliError, match="cannot read"):
            slurm_lib.read_batch_script(tmp_path / "absent")

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty"
        path.write_text("  \n")
        with pytest.raises(SlurmCliError, match="is empty"):
            slurm_lib.read_batch_script(path)

    def test_oversized_file(self, tmp_path):
        path = tmp_path / "big"
        path.write_bytes(b"#!/bin/bash\n" + b"x" * slurm_lib.MAX_SCRIPT_BYTES)
        with pytest.raises(SlurmCliError, match="limited to"):
            slurm_lib.read_batch_script(path)

    def test_binary_file(self, tmp_path):
        path = tmp_path / "bin"
        path.write_bytes(b"#!/bin/bash\n\xff\xfe")
        with pytest.raises(SlurmCliError, match="not UTF-8"):
            slurm_lib.read_batch_script(path)

    def test_missing_shebang(self, tmp_path):
        path = tmp_path / "noshebang"
        path.write_text("echo hi\n")
        with pytest.raises(SlurmCliError, match="interpreter line"):
            slurm_lib.read_batch_script(path)


class TestSbatchDirectives:
    def test_reads_the_leading_directive_block_only(self):
        script = (
            "#!/bin/bash\n"
            "\n"
            "# a plain comment\n"
            "#SBATCH --job-name=train --partition gpu\n"
            "#SBATCH -N 2 -n16 -c 4\n"
            "#SBATCH --gpus-per-node=a100:8 --mem 64G\n"
            "#SBATCH -t 04:00:00 -D /home/slurm/run\n"
            '#SBATCH --comment "nightly run" --exclusive --unknown-flag 3\n'
            "#SBATCH --mail-user=nobody   # trailing comment\n"
            "echo start\n"
            "#SBATCH --job-name=ignored-after-commands\n"
        )
        assert slurm_lib.sbatch_directives(script) == {
            "name": "train",
            "partition": "gpu",
            "nodes": 2,
            "tasks": 16,
            "cpus_per_task": 4,
            "gpus_per_node": 8,
            "memory_mb": 65536,
            "time_limit": "04:00:00",
            "working_directory": "/home/slurm/run",
            "comment": "nightly run",
            "exclusive": True,
        }

    def test_unbalanced_quotes_and_dangling_options_are_skipped(self):
        script = '#!/bin/bash\n#SBATCH --comment "open\n#SBATCH --partition\n#SBATCH -J\nhostname\n'
        assert slurm_lib.sbatch_directives(script) == {}

    def test_a_non_numeric_count_is_an_error(self):
        with pytest.raises(SlurmCliError, match="is not a number"):
            slurm_lib.sbatch_directives("#!/bin/bash\n#SBATCH --nodes=two\n")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("4000", 4000), ("4G", 4096), ("512M", 512), ("1T", 1048576), ("2048K", 2), ("1k", 1)],
    )
    def test_memory_units(self, value, expected):
        assert slurm_lib.memory_mb(value) == expected

    def test_bad_memory(self):
        with pytest.raises(SlurmCliError, match="invalid memory"):
            slurm_lib.memory_mb("lots")


class TestSubmissionBody:
    def test_flags_override_directives_and_none_is_skipped(self):
        body = slurm_lib.submission_body(
            SCRIPT,
            flags={"name": None, "partition": "cpu", "nodes": 3, "exclusive": False},
            environment=["OMP_NUM_THREADS=4", "EMPTY="],
        )
        assert body == {
            "script": SCRIPT,
            "name": "train",
            "partition": "cpu",
            "nodes": 3,
            "environment": {"OMP_NUM_THREADS": "4", "EMPTY": ""},
        }

    def test_exclusive_flag_is_kept_when_set(self):
        assert (
            slurm_lib.submission_body("#!/bin/bash\n", flags={"exclusive": True})["exclusive"]
            is True
        )

    @pytest.mark.parametrize("item", ["NOEQUALS", "1BAD=x", "with space=1"])
    def test_bad_environment_entries(self, item):
        with pytest.raises(SlurmCliError, match="--env"):
            slurm_lib.submission_body("#!/bin/bash\n", flags={}, environment=[item])


STATUS = {
    "api_version": "v0.0.44",
    "controller": [
        {"hostname": "c-0", "responding": True, "latency_us": 412, "mode": "primary"},
        {"hostname": "c-1", "responding": False, "latency_us": None, "mode": "backup"},
    ],
    "nodes": {"total": 3, "by_state": {"IDLE": 1, "MIXED": 1}, "scaled_in": 1},
    "jobs": {"total": 0, "by_state": {}},
    "partitions": [
        {
            "name": "cpu",
            "state": ["UP", "DEFAULT"],
            "default": True,
            "total_nodes": 2,
            "total_cpus": 8,
            "max_time_minutes": 1440,
            "node_names": "cpu-[0-1]",
        },
        {
            "name": "gpu",
            "state": ["UP"],
            "default": False,
            "total_nodes": None,
            "total_cpus": None,
            "max_time_minutes": None,
            "node_names": "",
        },
    ],
}
NODES = [
    {
        "name": "cpu-0",
        "state": ["IDLE"],
        "cpus": 4,
        "cpus_allocated": 0,
        "real_memory_mb": 7000,
        "memory_allocated_mb": 0,
        "gres": "",
        "gres_used": "",
        "partitions": ["cpu"],
        "reason": "",
    },
    {
        "name": "cpu-1",
        "state": ["DOWN", "DRAIN"],
        "scaled_in": True,
        "cpus": 8,
        "real_memory_mb": 31130,
        "partitions": ["cpu"],
        "reason": "slurm-operator: Pod is terminating",
    },
    {
        "name": "gpu-0",
        "state": ["DOWN"],
        "cpus": 8,
        "cpus_allocated": 8,
        "real_memory_mb": 30000,
        "memory_allocated_mb": 30000,
        "gres": "gpu:1",
        "gres_used": "gpu:1(IDX:0)",
        "partitions": [],
        "reason": "Not responding",
    },
]
JOB = {
    "job_id": 42,
    "name": "train",
    "user": "slurm",
    "account": "root",
    "partition": "gpu",
    "state": ["RUNNING"],
    "state_reason": "None",
    "nodes": "gpu-0",
    "node_count": 1,
    "tres_requested": "cpu=8,node=1",
    "tres_allocated": "cpu=8,node=1",
    "submit_time": 1_700_000_000,
    "start_time": 1_700_000_010,
    "end_time": None,
    "time_limit_minutes": 60,
    "exit_code": {"status": ["SUCCESS"], "return_code": 0},
    "comment": "gco:alice",
    "working_directory": "/home/slurm",
    "standard_output": "/home/slurm/slurm-42.out",
    "standard_error": "/home/slurm/slurm-42.err",
}
PENDING = {
    **JOB,
    "job_id": 43,
    "state": ["PENDING"],
    "state_reason": "Resources",
    "nodes": "",
    "node_count": None,
    "start_time": 0,
}
ACCOUNTING = {
    "job_id": 42,
    "name": "train",
    "user": "slurm",
    "partition": "gpu",
    "state": ["COMPLETED"],
    "elapsed_seconds": 3725,
    "exit_code": {"status": ["SUCCESS"], "return_code": 0},
    "start_time": 1_700_000_010,
    "comment": "gco:alice",
}


class TestRenderings:
    def test_status(self):
        text = slurm_lib.render_status(STATUS, "us-east-1")
        assert "Slurm — us-east-1 (slurmrestd v0.0.44)" in text
        assert "Controller c-0: UP (412 µs) [primary]" in text
        assert "Controller c-1: DOWN [backup]" in text
        assert "Nodes: 3 (IDLE 1, MIXED 1) + 1 scaled in" in text
        assert "scaled in" not in slurm_lib.render_status(
            {**STATUS, "nodes": {"total": 0, "by_state": {}, "scaled_in": 0}}, "r"
        )
        assert "Jobs:  0 " in text
        assert "cpu*" in text and "1440m" in text and "infinite" in text and "cpu-[0-1]" in text

    def test_partitions_empty(self):
        assert "(no partitions)" in slurm_lib.render_partitions([], "us-east-1")

    def test_nodes(self):
        text = slurm_lib.render_nodes(NODES, "us-east-1")
        assert "Slurm nodes — us-east-1 (3)" in text
        assert "0/4" in text and "30000/30000" in text and "gpu:1(IDX:0)" in text
        assert "reason: Not responding" in text
        assert "SCALED-IN" in text and "Pod is terminating" not in text
        assert "(no nodes" in slurm_lib.render_nodes([], "us-east-1")

    def test_queue(self):
        text = slurm_lib.render_queue([JOB, PENDING], 2, "us-east-1")
        assert "(2 of 2 shown)" in text
        assert "gpu-0" in text and "Resources" in text and "2023-11-14 22:13:20" in text
        assert "(queue is empty)" in slurm_lib.render_queue([], 0, "us-east-1")

    def test_job(self):
        text = slurm_lib.render_job(JOB, "us-east-1")
        assert "42 (train)" in text and "60 min" in text
        # The state reason only explains a wait: shown for PENDING, dropped for
        # the stale value both daemons keep afterwards ("None" on a running
        # job, "ReqNodeNotAvail" on a finished one, seen live).
        assert re.search(r"State\s+RUNNING\n", text)
        assert re.search(r"State\s+PENDING — Resources\n", slurm_lib.render_job(PENDING, "r"))
        assert re.search(
            r"State\s+COMPLETED\n",
            slurm_lib.render_job(
                {**JOB, "state": ["COMPLETED"], "state_reason": "ReqNodeNotAvail"}, "r"
            ),
        )
        assert re.search(
            r"State\s+PENDING\n",
            slurm_lib.render_job({**JOB, "state": "PENDING", "state_reason": "None"}, "r"),
        )
        assert re.search(r"Source\s+slurmctld", text)
        assert "slurmdbd (finished" in slurm_lib.render_job({**JOB, "source": "slurmdbd"}, "r")
        assert "SUCCESS rc=0" in text and "/home/slurm/slurm-42.out" in text
        sparse = slurm_lib.render_job(
            {"job_id": 1, "exit_code": None, "time_limit_minutes": None}, "r"
        )
        assert "infinite" in sparse and "Exit" in sparse

    def test_accounting(self):
        text = slurm_lib.render_accounting(
            [ACCOUNTING, {"job_id": 2, "exit_code": {}}], 2, "us-east-1"
        )
        assert "01:02:05" in text and "gco:alice" in text
        assert "(no accounting records" in slurm_lib.render_accounting([], 0, "us-east-1")
        assert slurm_lib._elapsed(-1) == "-" and slurm_lib._elapsed("x") == "-"
        assert (
            slurm_lib._when("x") == "-"
            and slurm_lib._join("A") == "A"
            and slurm_lib._join(None) == "-"
        )


class TestLoginPodCommands:
    def test_exec_argv_with_and_without_tunnel_flags(self):
        assert slurm_lib.login_exec_command(["cat", "/x"]) == [
            "kubectl",
            "exec",
            "deploy/slinky-slurm-login-gco",
            "-n",
            "gco-jobs",
            "-c",
            "login",
            "--",
            "cat",
            "/x",
        ]
        assert slurm_lib.login_exec_command(
            ["bash", "-l"], interactive=True, server=_SERVER, tls_server_name=_SNI
        ) == [
            "kubectl",
            "exec",
            "-it",
            "deploy/slinky-slurm-login-gco",
            "-n",
            "gco-jobs",
            "-c",
            "login",
            "--server",
            _SERVER,
            "--tls-server-name",
            _SNI,
            "--",
            "bash",
            "-l",
        ]

    @pytest.mark.parametrize(
        "path", ["relative/out", "/home/../etc/passwd", "/home/a;rm", "", "/x y"]
    )
    def test_remote_path_validation(self, path):
        with pytest.raises(SlurmCliError, match="refusing to read"):
            slurm_lib.validate_remote_path(path)
        assert (
            slurm_lib.validate_remote_path("/home/slurm/slurm-42.out") == "/home/slurm/slurm-42.out"
        )

    def test_run_foreground(self, monkeypatch):
        calls: list[Any] = []

        def fake_run(cmd: Any, **kwargs: Any) -> Any:
            calls.append((cmd, kwargs))
            return types.SimpleNamespace(returncode=3)

        monkeypatch.setattr(slurm_lib.subprocess, "run", fake_run)
        assert slurm_lib.run_foreground(("kubectl", "exec")) == 3
        assert calls == [(["kubectl", "exec"], {"check": False})]

        def missing(cmd: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("kubectl")

        monkeypatch.setattr(slurm_lib.subprocess, "run", missing)
        with pytest.raises(RuntimeError, match="kubectl not found"):
            slurm_lib.run_foreground(["kubectl"])

    def test_run_capture(self, monkeypatch):
        monkeypatch.setattr(
            slurm_lib.subprocess,
            "run",
            lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout="hello\n", stderr=""),
        )
        assert slurm_lib.run_capture(["kubectl", "exec"]) == "hello\n"

        monkeypatch.setattr(
            slurm_lib.subprocess,
            "run",
            lambda cmd, **kw: types.SimpleNamespace(
                returncode=1, stdout="", stderr="no such file\n"
            ),
        )
        with pytest.raises(RuntimeError, match=r"kubectl exec failed \(1\): no such file"):
            slurm_lib.run_capture(["kubectl", "exec"])

        def timeout(cmd: Any, **kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        monkeypatch.setattr(slurm_lib.subprocess, "run", timeout)
        with pytest.raises(RuntimeError, match="Timed out after 5s running kubectl exec"):
            slurm_lib.run_capture(["kubectl", "exec"], timeout=5)

        def missing(cmd: Any, **kw: Any) -> Any:
            raise FileNotFoundError("kubectl")

        monkeypatch.setattr(slurm_lib.subprocess, "run", missing)
        with pytest.raises(RuntimeError, match="kubectl not found"):
            slurm_lib.run_capture(["kubectl"])


# ---------------------------------------------------------------------------
# gco slurm (Click)
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def api() -> Iterator[MagicMock]:
    client = MagicMock()
    client.call_api.return_value = {}
    with patch("cli.aws_client.get_aws_client", return_value=client):
        yield client


def _request(api: MagicMock) -> dict[str, Any]:
    return dict(api.call_api.call_args.kwargs)


class TestRegionPinning:
    def test_explicit_region_wins(self, runner, api):
        api.call_api.return_value = STATUS
        result = runner.invoke(cli, ["-r", "us-west-2", "slurm", "status", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert _request(api)["region"] == "us-east-1"

    def test_root_region_is_the_default(self, runner, api):
        api.call_api.return_value = STATUS
        result = runner.invoke(cli, ["-r", "us-west-2", "slurm", "status"])
        assert result.exit_code == 0, result.output
        assert _request(api)["region"] == "us-west-2"

    def test_no_region_anywhere_is_a_usage_error(self, api):
        from cli.commands import slurm_cmd

        with pytest.raises(click.UsageError, match="need a region"):
            slurm_cmd._slurm_region(types.SimpleNamespace(default_region="  "), None)
        with pytest.raises(click.UsageError, match="need a region"):
            slurm_cmd._slurm_region(types.SimpleNamespace(default_region=None), "")
        assert (
            slurm_cmd._slurm_region(types.SimpleNamespace(default_region=None), " eu-west-1 ")
            == "eu-west-1"
        )
        api.call_api.assert_not_called()


class TestClusterViews:
    def test_status_table_and_json(self, runner, api):
        api.call_api.return_value = STATUS
        result = runner.invoke(cli, ["slurm", "status", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert "Controller c-0: UP" in result.output
        assert _request(api) == {
            "method": "GET",
            "path": "/api/v1/slurm/status",
            "region": "us-east-1",
        }

        result = runner.invoke(cli, ["--output", "json", "slurm", "status", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == STATUS

    def test_partitions(self, runner, api):
        api.call_api.return_value = {"partitions": STATUS["partitions"]}
        result = runner.invoke(cli, ["slurm", "partitions", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert "cpu*" in result.output
        assert _request(api)["path"] == "/api/v1/slurm/partitions"
        result = runner.invoke(cli, ["--output", "yaml", "slurm", "partitions", "-r", "us-east-1"])
        assert result.exit_code == 0 and "name: cpu" in result.stdout

    def test_nodes_with_partition_filter(self, runner, api):
        api.call_api.return_value = {"nodes": NODES}
        result = runner.invoke(cli, ["slurm", "nodes", "-r", "us-east-1", "-p", "gpu"])
        assert result.exit_code == 0, result.output
        assert "gpu:1(IDX:0)" in result.output
        assert _request(api) == {
            "method": "GET",
            "path": "/api/v1/slurm/nodes",
            "region": "us-east-1",
            "params": {"partition": "gpu"},
        }
        result = runner.invoke(cli, ["--output", "json", "slurm", "nodes", "-r", "us-east-1"])
        assert json.loads(result.stdout) == {"nodes": NODES}

    @pytest.mark.parametrize(
        ("args", "verb"),
        [
            (["status"], "read Slurm status"),
            (["partitions"], "list Slurm partitions"),
            (["nodes"], "list Slurm nodes"),
            (["queue"], "list Slurm jobs"),
            (["job", "42"], "read Slurm job 42"),
            (["cancel", "42", "-y"], "cancel Slurm job 42"),
            (["accounting"], "read Slurm accounting"),
        ],
    )
    def test_api_failures_exit_1(self, runner, api, args, verb):
        api.call_api.side_effect = APIRequestError(
            404, "Slurm is not enabled in this region. Set helm.slurm.enabled to true"
        )
        result = runner.invoke(cli, ["slurm", *args, "-r", "us-east-1"])
        assert result.exit_code == 1
        assert f"✗ Failed to {verb}: API request failed: Slurm is not enabled" in result.output


class TestQueueAndJob:
    def test_queue_filters_and_table(self, runner, api):
        api.call_api.return_value = {"jobs": [JOB, PENDING], "total": 2}
        result = runner.invoke(
            cli,
            [
                "slurm",
                "queue",
                "-r",
                "us-east-1",
                "-s",
                "pending",
                "-p",
                "gpu",
                "-n",
                "train",
                "-l",
                "5",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "(2 of 2 shown)" in result.output
        assert _request(api)["params"] == {
            "state": "PENDING",
            "partition": "gpu",
            "name": "train",
            "limit": "5",
        }

    def test_queue_json_and_defaults(self, runner, api):
        api.call_api.return_value = {"jobs": [], "total": 0}
        result = runner.invoke(cli, ["--output", "json", "slurm", "queue", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {"jobs": [], "total": 0}
        assert _request(api)["params"] == {
            "state": None,
            "partition": None,
            "name": None,
            "limit": "200",
        }

    def test_job(self, runner, api):
        api.call_api.return_value = {"job": JOB}
        result = runner.invoke(cli, ["slurm", "job", "42", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert "42 (train)" in result.output
        assert _request(api)["path"] == "/api/v1/slurm/jobs/42"
        result = runner.invoke(cli, ["--output", "json", "slurm", "job", "42", "-r", "us-east-1"])
        assert json.loads(result.stdout) == {"job": JOB}

    def test_job_id_must_be_an_integer(self, runner, api):
        result = runner.invoke(cli, ["slurm", "job", "abc", "-r", "us-east-1"])
        assert result.exit_code == 2
        api.call_api.assert_not_called()


class TestSubmit:
    def test_directives_then_flags_then_the_api(self, runner, api, tmp_path):
        script = tmp_path / "train.sbatch"
        script.write_text(SCRIPT)
        api.call_api.return_value = {
            "job_id": 42,
            "name": "train",
            "partition": "cpu",
            "message": "queued",
        }

        result = runner.invoke(
            cli,
            [
                "slurm",
                "submit",
                str(script),
                "-r",
                "us-east-1",
                "-p",
                "cpu",
                "-N",
                "2",
                "-n",
                "4",
                "-c",
                "2",
                "--gpus-per-node",
                "0",
                "--mem",
                "8G",
                "-t",
                "01:00:00",
                "-D",
                "/home/slurm",
                "-e",
                "A=1",
                "-e",
                "B=2",
                "--comment",
                "hi",
                "--exclusive",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "✓ Submitted job 42 (train) to cpu in us-east-1" in result.output
        assert "ℹ queued" in result.output
        assert _request(api) == {
            "method": "POST",
            "path": "/api/v1/slurm/jobs",
            "region": "us-east-1",
            "body": {
                "script": SCRIPT,
                "name": "train",
                "partition": "cpu",
                "nodes": 2,
                "tasks": 4,
                "cpus_per_task": 2,
                "gpus_per_node": 0,
                "memory_mb": 8192,
                "time_limit": "01:00:00",
                "working_directory": "/home/slurm",
                "comment": "hi",
                "exclusive": True,
                "environment": {"A": "1", "B": "2"},
            },
        }

    def test_minimal_submit_in_json_mode(self, runner, api, tmp_path):
        script = tmp_path / "hello.sh"
        script.write_text("#!/bin/bash\nhostname\n")
        api.call_api.return_value = {
            "job_id": 7,
            "name": "gco-job",
            "partition": None,
            "message": "",
        }

        result = runner.invoke(
            cli, ["--output", "json", "slurm", "submit", str(script), "-r", "us-east-1"]
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["job_id"] == 7
        assert _request(api)["body"] == {"script": "#!/bin/bash\nhostname\n"}

    def test_bad_script_fails_before_the_api(self, runner, api, tmp_path):
        script = tmp_path / "bad.sh"
        script.write_text("echo no shebang\n")
        result = runner.invoke(cli, ["slurm", "submit", str(script), "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "Failed to prepare bad.sh" in result.output
        api.call_api.assert_not_called()

    def test_bad_memory_flag_fails_before_the_api(self, runner, api, tmp_path):
        script = tmp_path / "ok.sh"
        script.write_text(SCRIPT)
        result = runner.invoke(
            cli, ["slurm", "submit", str(script), "-r", "us-east-1", "--mem", "lots"]
        )
        assert result.exit_code == 1
        assert "invalid memory" in result.output
        api.call_api.assert_not_called()

    def test_slurm_rejection(self, runner, api, tmp_path):
        script = tmp_path / "ok.sh"
        script.write_text(SCRIPT)
        api.call_api.side_effect = APIRequestError(400, "Invalid partition name specified")
        result = runner.invoke(cli, ["slurm", "submit", str(script), "-r", "us-east-1"])
        assert result.exit_code == 1
        assert (
            "✗ Failed to submit ok.sh: API request failed: Invalid partition name specified"
            in result.output
        )

    def test_missing_script_is_a_usage_error(self, runner, api, tmp_path):
        result = runner.invoke(cli, ["slurm", "submit", str(tmp_path / "nope"), "-r", "us-east-1"])
        assert result.exit_code == 2


class TestCancel:
    def test_confirms_then_cancels(self, runner, api):
        api.call_api.return_value = {"job_id": 42, "cancelled": True}
        result = runner.invoke(cli, ["slurm", "cancel", "42", "-r", "us-east-1"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "Cancel Slurm job 42 in us-east-1?" in result.output
        assert "✓ Cancelled job 42 in us-east-1" in result.output
        assert _request(api) == {
            "method": "DELETE",
            "path": "/api/v1/slurm/jobs/42",
            "region": "us-east-1",
            "params": {"signal": None},
        }

    def test_declining_aborts_without_a_request(self, runner, api):
        result = runner.invoke(cli, ["slurm", "cancel", "42", "-r", "us-east-1"], input="n\n")
        assert result.exit_code == 1
        api.call_api.assert_not_called()

    def test_yes_and_signal(self, runner, api):
        api.call_api.return_value = {"job_id": 42, "cancelled": True}
        result = runner.invoke(
            cli,
            ["--output", "json", "slurm", "cancel", "42", "-r", "us-east-1", "-y", "-s", "sigusr1"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {"job_id": 42, "cancelled": True}
        assert _request(api)["params"] == {"signal": "SIGUSR1"}


class TestAccounting:
    def test_default_window_is_one_day(self, runner, api):
        api.call_api.return_value = {"jobs": [ACCOUNTING], "total": 1}
        result = runner.invoke(cli, ["slurm", "accounting", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert "gco:alice" in result.output
        assert _request(api)["params"] == {
            "start": "now-1days",
            "user": None,
            "partition": None,
            "state": None,
            "job_name": None,
            "limit": "200",
        }

    def test_filters_and_explicit_window(self, runner, api):
        api.call_api.return_value = {"jobs": [], "total": 0}
        result = runner.invoke(
            cli,
            [
                "--output",
                "json",
                "slurm",
                "accounting",
                "-r",
                "us-east-1",
                "--start",
                "2026-10-01",
                "--end",
                "2026-10-08",
                "-u",
                "alice",
                "-p",
                "gpu",
                "-s",
                "completed,failed",
                "-n",
                "train",
                "-l",
                "10",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {"jobs": [], "total": 0}
        assert _request(api)["params"] == {
            "start": "2026-10-01",
            "end": "2026-10-08",
            "user": "alice",
            "partition": "gpu",
            "state": "COMPLETED,FAILED",
            "job_name": "train",
            "limit": "10",
        }

    def test_since_overrides_the_default(self, runner, api):
        api.call_api.return_value = {"jobs": [], "total": 0}
        result = runner.invoke(cli, ["slurm", "accounting", "-r", "us-east-1", "--since", "7d"])
        assert result.exit_code == 0, result.output
        assert _request(api)["params"]["start"] == "now-7days"

    def test_bad_window_fails_before_the_api(self, runner, api):
        result = runner.invoke(cli, ["slurm", "accounting", "-r", "us-east-1", "--since", "soon"])
        assert result.exit_code == 1
        assert "invalid --since" in result.output
        api.call_api.assert_not_called()


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "tunnels": [],
        "kubeconfig": [],
        "session": (_SERVER, _SNI),
        "runs": [],
    }

    @contextmanager
    def fake_tunnel(formatter: Any, **kwargs: Any) -> Iterator[Any]:
        state["tunnels"].append(kwargs)
        server, sni = state["session"]
        yield types.SimpleNamespace(server=server, tls_server_name=sni)

    monkeypatch.setattr("cli.cluster_tunnel.open_api_server_tunnel", fake_tunnel)
    monkeypatch.setattr(
        "cli.kubectl_helpers.update_kubeconfig",
        lambda cluster, region: state["kubeconfig"].append((cluster, region)),
    )
    return state


class TestShell:
    def test_execs_an_interactive_shell_through_the_tunnel(self, runner, cluster, monkeypatch):
        monkeypatch.setattr(
            slurm_lib, "run_foreground", lambda cmd: cluster["runs"].append(list(cmd)) or 0
        )
        result = runner.invoke(
            cli, ["slurm", "shell", "-r", "us-west-2", "--via-ssm", "i-0123456789abcdef0"]
        )
        assert result.exit_code == 0, result.output
        assert cluster["kubeconfig"] == [("gco-us-west-2", "us-west-2")]
        assert cluster["tunnels"][0]["via_ssm"] == "i-0123456789abcdef0"
        assert cluster["tunnels"][0]["cluster"] == "gco-us-west-2"
        assert cluster["runs"] == [
            [
                "kubectl",
                "exec",
                "-it",
                "deploy/slinky-slurm-login-gco",
                "-n",
                "gco-jobs",
                "-c",
                "login",
                "--server",
                _SERVER,
                "--tls-server-name",
                _SNI,
                "--",
                "bash",
                "-l",
            ]
        ]
        assert "Opening a shell on gco-jobs/slinky-slurm-login-gco in us-west-2" in result.output

    def test_shell_exit_code_is_propagated(self, runner, cluster, monkeypatch):
        cluster["session"] = (None, None)
        monkeypatch.setattr(
            slurm_lib, "run_foreground", lambda cmd: cluster["runs"].append(list(cmd)) or 130
        )
        result = runner.invoke(cli, ["slurm", "shell", "-r", "us-east-1"])
        assert result.exit_code == 130
        assert "--server" not in cluster["runs"][0]

    def test_shell_refuses_machine_output(self, runner, cluster, monkeypatch):
        monkeypatch.setattr(slurm_lib, "run_foreground", lambda cmd: pytest.fail("must not run"))
        result = runner.invoke(cli, ["--output", "json", "slurm", "shell", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "interactive" in result.output
        assert cluster["kubeconfig"] == []

    def test_failures_exit_1(self, runner, monkeypatch):
        def fail(cluster: str, region: str) -> None:
            raise RuntimeError("no such cluster")

        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", fail)
        result = runner.invoke(cli, ["slurm", "shell", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "no such cluster" in result.output


class TestOutput:
    def test_cats_the_stdout_file_from_the_login_pod(self, runner, api, cluster, monkeypatch):
        api.call_api.return_value = {"job": JOB}
        monkeypatch.setattr(
            slurm_lib, "run_capture", lambda cmd: cluster["runs"].append(list(cmd)) or "hello world"
        )
        result = runner.invoke(
            cli, ["slurm", "output", "42", "-r", "us-east-1", "--via-ssm", "auto", "-y"]
        )
        assert result.exit_code == 0, result.output
        assert result.output.endswith("hello world\n")
        assert _request(api) == {
            "method": "GET",
            "path": "/api/v1/slurm/jobs/42",
            "region": "us-east-1",
        }
        assert cluster["tunnels"][0]["via_ssm"] == "auto"
        assert cluster["tunnels"][0]["assume_yes"] is True
        assert cluster["runs"] == [
            [
                "kubectl",
                "exec",
                "deploy/slinky-slurm-login-gco",
                "-n",
                "gco-jobs",
                "-c",
                "login",
                "--server",
                _SERVER,
                "--tls-server-name",
                _SNI,
                "--",
                "cat",
                "/home/slurm/slurm-42.out",
            ]
        ]

    def test_an_empty_output_file_prints_nothing(self, runner, api, cluster, monkeypatch):
        api.call_api.return_value = {"job": JOB}
        monkeypatch.setattr(slurm_lib, "run_capture", lambda cmd: "")
        result = runner.invoke(cli, ["slurm", "output", "42", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert result.stdout == ""

    def test_stderr_tail_and_json_document(self, runner, api, cluster, monkeypatch):
        api.call_api.return_value = {"job": JOB}
        monkeypatch.setattr(
            slurm_lib, "run_capture", lambda cmd: cluster["runs"].append(list(cmd)) or "err\n"
        )
        result = runner.invoke(
            cli,
            [
                "--output",
                "json",
                "slurm",
                "output",
                "42",
                "-r",
                "us-east-1",
                "--stderr",
                "--tail",
                "20",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {
            "job_id": 42,
            "region": "us-east-1",
            "path": "/home/slurm/slurm-42.err",
            "content": "err\n",
        }
        assert cluster["runs"][0][-4:] == ["tail", "-n", "20", "/home/slurm/slurm-42.err"]

    def test_follow_streams_in_the_foreground(self, runner, api, cluster, monkeypatch):
        api.call_api.return_value = {"job": JOB}
        monkeypatch.setattr(
            slurm_lib, "run_foreground", lambda cmd: cluster["runs"].append(list(cmd)) or 0
        )
        result = runner.invoke(cli, ["slurm", "output", "42", "-r", "us-east-1", "--follow"])
        assert result.exit_code == 0, result.output
        assert "Following /home/slurm/slurm-42.out" in result.output
        assert cluster["runs"][0][-3:] == ["tail", "-f", "/home/slurm/slurm-42.out"]

    def test_follow_exit_code_is_propagated(self, runner, api, cluster, monkeypatch):
        api.call_api.return_value = {"job": JOB}
        monkeypatch.setattr(slurm_lib, "run_foreground", lambda cmd: 130)
        result = runner.invoke(cli, ["slurm", "output", "42", "-r", "us-east-1", "-f"])
        assert result.exit_code == 130

    def test_follow_refuses_machine_output(self, runner, api, cluster):
        result = runner.invoke(
            cli, ["--output", "json", "slurm", "output", "42", "-r", "us-east-1", "-f"]
        )
        assert result.exit_code == 1
        assert "--follow streams a terminal" in result.output
        api.call_api.assert_not_called()

    def test_api_failure_exits_1(self, runner, api, cluster):
        api.call_api.side_effect = APIRequestError(404, "Slurm job 42 not found")
        result = runner.invoke(cli, ["slurm", "output", "42", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "Failed to read Slurm job 42" in result.output
        assert cluster["kubeconfig"] == []

    def test_unusable_path_exits_1(self, runner, api, cluster):
        api.call_api.return_value = {"job": {**JOB, "standard_output": "slurm-%j.out"}}
        result = runner.invoke(cli, ["slurm", "output", "42", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "Failed to locate the standard output of job 42" in result.output
        assert cluster["kubeconfig"] == []

    def test_kubectl_failure_exits_1(self, runner, api, cluster, monkeypatch):
        api.call_api.return_value = {"job": JOB}

        def fail(cmd: Any) -> str:
            raise RuntimeError("kubectl exec failed (1): No such file")

        monkeypatch.setattr(slurm_lib, "run_capture", fail)
        result = runner.invoke(cli, ["slurm", "output", "42", "-r", "us-east-1"])
        assert result.exit_code == 1
        assert "No such file" in result.output


def test_group_help_lists_every_command(runner) -> None:
    result = runner.invoke(cli, ["slurm", "--help"])
    assert result.exit_code == 0
    for command in (
        "status",
        "partitions",
        "nodes",
        "queue",
        "submit",
        "job",
        "cancel",
        "accounting",
        "shell",
        "output",
    ):
        assert command in result.output
