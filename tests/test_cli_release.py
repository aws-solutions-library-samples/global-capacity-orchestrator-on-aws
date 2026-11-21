"""Unit tests for ``gco release validate``.

The command is a no-prompt wrapper around the live-validation harness: it
derives commit/branch/run-id/report-dir, demands unambiguous consent flags,
and executes ``python -m scripts.live_release_validation`` as a subprocess.
These tests fake every subprocess (both the ``git`` derivations and the
harness invocation) so they are hermetic: CI checkouts are detached-HEAD,
and nothing here may ever actually launch a validation run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.commands import release_cmd
from cli.commands.release_cmd import release

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeProcesses:
    """Route git calls to canned answers and capture the harness launch."""

    def __init__(self, tmp_path: Path, harness_returncode: int = 0):
        self.repo_root = tmp_path
        (tmp_path / "cdk.json").write_text("{}")
        (tmp_path / "scripts").mkdir()
        self.harness_calls: list[dict] = []
        self.harness_returncode = harness_returncode

    def __call__(self, command, **kwargs):
        if command[0] != "git":  # pragma: no cover - defensive
            raise AssertionError(f"the harness runs through _run_harness, not {command}")
        outputs = {
            ("rev-parse", "--show-toplevel"): str(self.repo_root),
            ("rev-parse", "HEAD"): "a" * 40,
            ("symbolic-ref", "--short", "HEAD"): "test/floci-integration",
        }
        key = tuple(command[1:])
        if key not in outputs:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected git call: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=outputs[key] + "\n", stderr="")

    def run_harness(self, command, **kwargs):
        self.harness_calls.append({"command": command, **kwargs})
        return self.harness_returncode

    def install(self, monkeypatch) -> FakeProcesses:
        monkeypatch.setattr("cli.commands.release_cmd.subprocess.run", self)
        monkeypatch.setattr("cli.commands.release_cmd._run_harness", self.run_harness)
        return self


@pytest.fixture
def fake_processes(tmp_path, monkeypatch):
    return FakeProcesses(tmp_path).install(monkeypatch)


def _invoke(*args: str):
    return CliRunner().invoke(release, ["validate", *args])


CONSENT = "--i-understand-this-deploys-and-destroys-infrastructure"
INFERENCE = (
    "--inference-region",
    "us-east-1",
    "--inference-vllm-image",
    "registry.example/vllm@sha256:" + "a" * 64,
    "--inference-vllm-model-id",
    "test/qwen3-vllm-model",
    "--inference-vllm-model-revision",
    "b" * 40,
    "--inference-sglang-image",
    "registry.example/sglang@sha256:" + "c" * 64,
    "--inference-sglang-model-id",
    "test/sglang-model",
    "--inference-sglang-model-revision",
    "d" * 40,
    "--inference-triton-image",
    "registry.example/tritonserver@sha256:" + "e" * 64,
)
BASE = (
    "--expected-account",
    "123456789012",
    CONSENT,
    "--confirm-kms-key-deletion",
    *INFERENCE,
)


class TestConsentGates:
    def test_refuses_without_the_consent_flag(self, fake_processes):
        result = _invoke("--expected-account", "123456789012")
        assert result.exit_code != 0
        assert CONSENT in result.output, "the error must name the exact flag to add"
        assert fake_processes.harness_calls == [], "no consent, no subprocess — ever"

    def test_refuses_a_malformed_account(self, fake_processes):
        result = _invoke("--expected-account", "12345", CONSENT)
        assert result.exit_code != 0
        assert "12-digit" in result.output
        assert fake_processes.harness_calls == []

    def test_deploy_requires_kms_confirmation(self, fake_processes):
        result = _invoke("--expected-account", "123456789012", CONSENT)
        assert result.exit_code != 0
        assert "--confirm-kms-key-deletion" in result.output
        assert fake_processes.harness_calls == []

    def test_non_deploy_actions_skip_the_kms_gate(self, fake_processes):
        result = _invoke(
            "--expected-account", "123456789012", CONSENT, "--actions", "preflight,baseline"
        )
        assert result.exit_code == 0, result.output
        assert len(fake_processes.harness_calls) == 1
        command = fake_processes.harness_calls[0]["command"]
        assert "--confirm-kms-key-deletion" not in command
        assert "--inference-vllm-image" not in command
        assert "--inference-sglang-image" not in command
        assert "--inference-triton-image" not in command
        assert "--confirm-inference-deployment" not in command

    def test_workload_actions_imply_deploy_for_the_kms_gate(self, fake_processes):
        """`--actions api` expands to deploy inside the harness; the KMS
        consent gate must fire for it exactly as it does for `deploy`."""
        for actions in ("api", "sqs", "schedulers", "destroy"):
            result = _invoke("--expected-account", "123456789012", CONSENT, "--actions", actions)
            assert result.exit_code != 0, f"--actions {actions} skipped the KMS gate"
            assert "--confirm-kms-key-deletion" in result.output
        assert fake_processes.harness_calls == []

    def test_inference_framework_images_require_distinct_digests(self, fake_processes):
        same = "registry.example/sglang@sha256:" + "a" * 64
        result = _invoke(*BASE, "--inference-sglang-image", same)
        assert result.exit_code != 0
        assert "distinct immutable digests" in result.output
        assert fake_processes.harness_calls == []
        triton_same = "registry.example/tritonserver@sha256:" + "a" * 64
        result = _invoke(*BASE, "--inference-triton-image", triton_same)
        assert result.exit_code != 0
        assert "distinct immutable digests" in result.output
        assert fake_processes.harness_calls == []

    def test_inference_requires_the_triton_image(self, fake_processes):
        without_triton = [
            value for value in BASE if not value.startswith("registry.example/tritonserver")
        ]
        without_triton.remove("--inference-triton-image")
        result = _invoke(*without_triton)
        assert result.exit_code != 0
        assert "--inference-triton-image" in result.output
        assert fake_processes.harness_calls == []
        result = _invoke(*BASE, "--inference-triton-image", "nvcr.io/nvidia/tritonserver:26.09-py3")
        assert result.exit_code != 0
        assert "--inference-triton-image must be an immutable" in result.output
        assert fake_processes.harness_calls == []

    def test_resume_requires_run_id_and_report_dir(self, fake_processes):
        result = _invoke(*BASE, "--resume")
        assert result.exit_code != 0
        assert "--run-id" in result.output and "--report-dir" in result.output
        assert fake_processes.harness_calls == []

    def test_empty_actions_are_rejected(self, fake_processes):
        result = _invoke(*BASE, "--actions", " , ")
        assert result.exit_code != 0
        assert "at least one action" in result.output


class TestHarnessInvocation:
    def test_derives_identity_and_composes_the_harness_command(self, fake_processes):
        result = _invoke(*BASE)
        assert result.exit_code == 0, result.output
        call = fake_processes.harness_calls[0]
        command = call["command"]
        assert command[1:3] == ["-m", "scripts.live_release_validation"]

        def value_of(flag: str) -> str:
            return command[command.index(flag) + 1]

        assert value_of("--expected-account") == "123456789012"
        assert value_of("--expected-sha") == "a" * 40
        assert value_of("--expected-branch") == "test/floci-integration"
        assert value_of("--actions") == "all"
        assert value_of("--inference-region") == "us-east-1"
        assert value_of("--inference-vllm-image").endswith("a" * 64)
        assert value_of("--inference-vllm-model-id") == "test/qwen3-vllm-model"
        assert value_of("--inference-vllm-model-revision") == "b" * 40
        assert value_of("--inference-sglang-image").endswith("c" * 64)
        assert value_of("--inference-sglang-model-id") == "test/sglang-model"
        assert value_of("--inference-sglang-model-revision") == "d" * 40
        assert value_of("--inference-triton-image").endswith("e" * 64)
        assert not any("tgi" in value for value in command)
        # The matrix is GPU-only, so the default is one GPU per replica, never 0.
        assert value_of("--inference-gpu-count") == "1"
        assert "--confirm-inference-deployment" in command
        assert value_of("--repo-root") == str(fake_processes.repo_root)
        assert "--confirm-kms-key-deletion" in command
        run_id = value_of("--run-id")
        assert run_id.endswith("-" + "a" * 12), "run id must embed the derived SHA prefix"
        report_dir = Path(value_of("--report-dir"))
        assert report_dir.name == run_id
        assert not str(report_dir).startswith(str(fake_processes.repo_root)), (
            "the default report directory must live OUTSIDE the checkout, or the "
            "harness's clean-worktree preflight would fail on its own output"
        )
        assert value_of("--checkpoint") == str(report_dir / "checkpoint.json")
        assert call["cwd"] == fake_processes.repo_root
        # Echoed derivations keep the operator informed without a prompt.
        assert "run-id:" in result.output and "branch:" in result.output

    def test_uppercase_image_tag_is_accepted_with_digest(self, fake_processes):
        image = "registry.example/team/vllm:CUDA12@sha256:" + "7" * 64
        result = _invoke(*BASE, "--inference-vllm-image", image)
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        assert command[command.index("--inference-vllm-image") + 1] == image

    def test_unbounded_numeric_registry_port_is_controlled_validation_error(self, fake_processes):
        image = "registry.example:" + "9" * 5_000 + "/team/vllm:CUDA12@sha256:" + "e" * 64
        result = _invoke(*BASE, "--inference-vllm-image", image)
        assert result.exit_code != 0
        assert "immutable lowercase @sha256" in result.output
        assert "Exceeds the limit" not in result.output
        assert fake_processes.harness_calls == []

    def test_explicit_overrides_and_protected_stacks_are_forwarded(self, fake_processes, tmp_path):
        report_dir = tmp_path / "reports"
        result = _invoke(
            *BASE,
            "--run-id",
            "run-42",
            "--report-dir",
            str(report_dir),
            "--resume",
            "--protected-stack",
            "SharedAlarms",
            "--protected-stack",
            "OrgTrail",
            "--profile",
            "single-region",
        )
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        assert "--resume" in command
        joined = " ".join(command)
        assert "--protected-stack SharedAlarms" in joined
        assert "--protected-stack OrgTrail" in joined
        assert "--profile single-region" in joined
        assert "--run-id run-42" in joined

    def test_harness_exit_code_propagates(self, tmp_path, monkeypatch):
        FakeProcesses(tmp_path, harness_returncode=3).install(monkeypatch)
        result = _invoke(*BASE)
        assert result.exit_code == 3, "operators script on the harness's exit code"

    def test_the_harness_runs_through_the_interrupt_relay(self, fake_processes, tmp_path):
        report_dir = tmp_path / "reports"
        result = _invoke(*BASE, "--run-id", "run-7", "--report-dir", str(report_dir))
        assert result.exit_code == 0, result.output
        (call,) = fake_processes.harness_calls
        assert call["cwd"] == fake_processes.repo_root
        assert call["resume_hint"] == (
            "Finish the teardown by running the same command again with "
            f"--resume --run-id run-7 --report-dir {report_dir}."
        )

    def test_emulator_endpoint_sets_the_verified_env_pair(self, fake_processes):
        result = _invoke(*BASE, "--emulator-endpoint", "http://127.0.0.1:4566/")
        assert result.exit_code == 0, result.output
        env = fake_processes.harness_calls[0]["env"]
        assert env["GCO_LIVE_VALIDATION_EMULATOR"] == "http://127.0.0.1:4566"
        assert env["AWS_ENDPOINT_URL"] == "http://127.0.0.1:4566"
        assert "emulator:" in result.output

    def test_without_emulator_flag_no_emulator_env_leaks(self, fake_processes):
        result = _invoke(*BASE)
        assert result.exit_code == 0
        env = fake_processes.harness_calls[0]["env"]
        assert "GCO_LIVE_VALIDATION_EMULATOR" not in env

    def test_optional_schedulers_flag_is_forwarded_verbatim(self, fake_processes):
        result = _invoke(*BASE, "--optional-schedulers", "yunikorn,slurm")
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        index = command.index("--optional-schedulers")
        assert command[index + 1] == "yunikorn,slurm"
        assert "schedulers:" in result.output, "the echo must show the forced enablement"

    def test_optional_schedulers_absent_by_default(self, fake_processes):
        result = _invoke(*BASE)
        assert result.exit_code == 0
        assert "--optional-schedulers" not in fake_processes.harness_calls[0]["command"]


class TestEksCapabilitiesPassThrough:
    """`gco release validate` is the documented one-command path for the live
    leg, so the harness's `--eks-capabilities` surface must reach it verbatim."""

    def test_eks_capabilities_are_forwarded_verbatim(self, fake_processes):
        result = _invoke(*BASE, "--eks-capabilities", "ack,kro")
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        harness_start = command.index("scripts.live_release_validation")
        tail = command[harness_start:]
        assert tail[tail.index("--eks-capabilities") + 1] == "ack,kro"
        assert not any(flag.startswith("--argocd-") for flag in tail)
        assert "capabilities: ack,kro" in result.output, "the echo must show the enablement"

    def test_all_is_forwarded(self, fake_processes):
        result = _invoke(*BASE, "--eks-capabilities", "all")
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        assert command[command.index("--eks-capabilities") + 1] == "all"

    def test_eks_capabilities_absent_by_default(self, fake_processes):
        result = _invoke(*BASE)
        assert result.exit_code == 0
        command = fake_processes.harness_calls[0]["command"]
        assert not any(flag.startswith(("--eks-capabilities", "--argocd-")) for flag in command)
        assert "capabilities:" not in result.output

    def test_the_retired_argocd_options_are_gone(self, fake_processes):
        for extra in (
            ("--argocd-identity", "SSO_GROUP:g-1"),
            ("--argocd-gitops-repo-url", "https://github.com/example/gitops.git"),
            ("--no-argocd-gitops",),
        ):
            result = _invoke(*BASE, "--eks-capabilities", "kro", *extra)
            assert result.exit_code != 0, extra
            assert "No such option" in result.output, result.output
        assert fake_processes.harness_calls == []

    def test_malformed_capability_lists_are_rejected_before_launch(self, fake_processes):
        for value, fragment in (
            (" , ", "at least one capability"),
            ("kro,flux", "flux"),
            # Argo CD is a Helm chart now (helm.argocd), not a capability.
            ("argocd", "argocd"),
            ("all,kro", "cannot be combined"),
        ):
            result = _invoke(*BASE, "--eks-capabilities", value)
            assert result.exit_code != 0, value
            assert fragment in result.output, result.output
        assert fake_processes.harness_calls == []


class TestKubernetesVersionPassThrough:
    """`--kubernetes-version` is how a release run deploys the minor a feature it
    turns on supports (SageMaker HyperPod's node AMI trails new EKS minors); the
    wrapper forwards it, stripped, and says so."""

    def test_the_minor_is_forwarded_and_echoed(self, fake_processes):
        result = _invoke(*BASE, "--kubernetes-version", " 1.36 ")
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        assert command[command.index("--kubernetes-version") + 1] == "1.36"
        assert "kubernetes: 1.36 (this run's EKS minor, in place of cdk.json's)" in result.output

    def test_absent_by_default(self, fake_processes):
        result = _invoke(*BASE)
        assert result.exit_code == 0, result.output
        assert "--kubernetes-version" not in fake_processes.harness_calls[0]["command"]
        assert "kubernetes:" not in result.output

    @pytest.mark.parametrize("value", ["1", "v1.36", "1.36.0", "latest", " "])
    def test_anything_but_a_minor_is_rejected_before_launch(self, fake_processes, value):
        result = _invoke(*BASE, "--kubernetes-version", value)
        assert result.exit_code != 0, value
        assert "--kubernetes-version must be a Kubernetes minor" in result.output, result.output
        assert fake_processes.harness_calls == []


class TestRepoRootValidation:
    def test_refuses_a_non_gco_checkout(self, tmp_path, monkeypatch):
        bare = tmp_path / "bare"
        bare.mkdir()

        def fake_run(command, **kwargs):
            assert command[0] == "git"
            return subprocess.CompletedProcess(command, 0, stdout=str(bare) + "\n", stderr="")

        monkeypatch.setattr("cli.commands.release_cmd.subprocess.run", fake_run)
        result = _invoke(*BASE)
        assert result.exit_code != 0
        assert "not a GCO checkout" in result.output

    def test_git_failure_is_reported_not_raised(self, tmp_path, monkeypatch):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: not a repo")

        monkeypatch.setattr("cli.commands.release_cmd.subprocess.run", fake_run)
        result = _invoke(*BASE)
        assert result.exit_code != 0
        assert "fatal: not a repo" in result.output


def _invoke_upgrade(*args: str):
    return CliRunner().invoke(release, ["validate-upgrade", *args])


UPGRADE_BASE = ("--expected-account", "123456789012", CONSENT, "--confirm-kms-key-deletion")


class TestValidateUpgrade:
    """``gco release validate-upgrade`` wraps ``scripts.upgrade_validation`` the same way."""

    def test_refuses_without_consent_or_with_a_malformed_account(self, fake_processes):
        result = _invoke_upgrade("--expected-account", "123456789012")
        assert result.exit_code != 0 and CONSENT in result.output
        result = _invoke_upgrade("--expected-account", "12345", CONSENT)
        assert result.exit_code != 0 and "12-digit" in result.output
        assert fake_processes.harness_calls == []

    def test_every_action_past_prepare_requires_kms_confirmation(self, fake_processes):
        for actions in ("all", "deploy", "upgrade", "verify-upgrade", "destroy"):
            result = _invoke_upgrade(
                "--expected-account", "123456789012", CONSENT, "--actions", actions
            )
            assert result.exit_code != 0, actions
            assert "--confirm-kms-key-deletion" in result.output
        assert fake_processes.harness_calls == []
        result = _invoke_upgrade(
            "--expected-account", "123456789012", CONSENT, "--actions", "preflight,prepare,baseline"
        )
        assert result.exit_code == 0, result.output
        command = fake_processes.harness_calls[0]["command"]
        assert "--confirm-kms-key-deletion" not in command
        assert command[command.index("--actions") + 1] == "baseline,preflight,prepare"

    def test_resume_requires_run_id_and_report_dir_and_actions_are_required(self, fake_processes):
        result = _invoke_upgrade(*UPGRADE_BASE, "--resume")
        assert result.exit_code != 0 and "--run-id" in result.output
        result = _invoke_upgrade(*UPGRADE_BASE, "--actions", " , ")
        assert result.exit_code != 0 and "at least one action" in result.output
        assert fake_processes.harness_calls == []

    def test_derives_identity_and_composes_the_harness_command(self, fake_processes):
        result = _invoke_upgrade(*UPGRADE_BASE)
        assert result.exit_code == 0, result.output
        call = fake_processes.harness_calls[0]
        command = call["command"]
        assert command[1:3] == ["-m", "scripts.upgrade_validation"]

        def value_of(flag: str) -> str:
            return command[command.index(flag) + 1]

        assert value_of("--expected-sha") == "a" * 40
        assert value_of("--expected-branch") == "test/floci-integration"
        assert value_of("--actions") == "all"
        run_id = value_of("--run-id")
        report_dir = Path(value_of("--report-dir"))
        assert report_dir == Path.home() / "gco-upgrade-validation-reports" / run_id
        assert value_of("--checkpoint") == str(report_dir / "checkpoint.json")
        assert "--base-ref" not in command and "--resume" not in command
        assert call["cwd"] == fake_processes.repo_root
        assert "base:       the newest earlier release" in result.output

    def test_explicit_options_are_forwarded(self, fake_processes, tmp_path):
        result = _invoke_upgrade(
            *UPGRADE_BASE,
            "--base-ref",
            "v8.7.0",
            "--run-id",
            "run-9",
            "--report-dir",
            str(tmp_path / "reports"),
            "--resume",
            "--protected-stack",
            "SharedAlarms",
        )
        assert result.exit_code == 0, result.output
        joined = " ".join(fake_processes.harness_calls[0]["command"])
        for fragment in (
            "--base-ref v8.7.0",
            "--run-id run-9",
            f"--report-dir {tmp_path / 'reports'}",
            "--resume",
            "--protected-stack SharedAlarms",
            "--confirm-kms-key-deletion",
        ):
            assert fragment in joined, fragment
        assert "base:       v8.7.0" in result.output

    def test_harness_exit_code_propagates(self, tmp_path, monkeypatch):
        FakeProcesses(tmp_path, harness_returncode=4).install(monkeypatch)
        assert _invoke_upgrade(*UPGRADE_BASE).exit_code == 4

    def test_the_harness_runs_through_the_interrupt_relay(self, fake_processes, tmp_path):
        report_dir = tmp_path / "reports"
        result = _invoke_upgrade(
            *UPGRADE_BASE, "--run-id", "run-8", "--report-dir", str(report_dir)
        )
        assert result.exit_code == 0, result.output
        (call,) = fake_processes.harness_calls
        assert call["cwd"] == fake_processes.repo_root
        assert call["env"] == dict(os.environ)
        assert "--resume --run-id run-8 --report-dir" in call["resume_hint"]


# --------------------------------------------------------------------------
# Interrupts reach the harness, and the command waits for its cleanup
# --------------------------------------------------------------------------

#: A stand-in harness. Like the real ones it turns SIGINT, SIGTERM and SIGHUP
#: into a cleanup that outlasts the 0.25 seconds ``subprocess.run`` used to
#: allow before killing it, and logs every signal it gets, so a test can count
#: the deliveries. ``FAKE_HARNESS_LOG``/``FAKE_HARNESS_MODE`` (else argv 1
#: and 2) say where to log and how to behave.
_FAKE_HARNESS = r"""
import os
import signal
import sys
import time
from pathlib import Path

log = Path(os.environ.get("FAKE_HARNESS_LOG") or sys.argv[1])
mode = os.environ.get("FAKE_HARNESS_MODE") or sys.argv[2]
received = []


def note(text):
    with log.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def interrupted(signum, frame):
    received.append(signum)
    note("signal " + signal.Signals(signum).name)


for name in ("SIGINT", "SIGTERM", "SIGHUP"):
    signal.signal(getattr(signal, name), signal.SIG_IGN if mode == "ignore" else interrupted)
note("ready")
if mode == "exit":
    sys.exit(3)
if mode == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
deadline = time.monotonic() + 60
while not received and time.monotonic() < deadline:
    time.sleep(0.01)
if mode == "cleanup":
    time.sleep(1.0)
    note("cleanup finished")
    sys.exit(128 + received[0])
note("cleanup started")
while len(received) < 2 and time.monotonic() < deadline:
    time.sleep(0.01)
note("report written")
sys.exit(128 + received[0])
"""


def _lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _wait_for(log: Path, line: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while line not in _lines(log):
        if time.monotonic() > deadline:  # pragma: no cover - diagnostic
            raise AssertionError(f"{line!r} never appeared; log: {_lines(log)}")
        time.sleep(0.01)


def _signal_self_after(log: Path, line: str, *signums: int, pause: float = 0.0) -> threading.Thread:
    """Signal this process, the command's, once ``line`` is in the harness's log."""

    def send() -> None:
        _wait_for(log, line)
        for signum in signums:
            os.kill(os.getpid(), signum)
            time.sleep(pause)

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def stray_signals() -> Iterator[list[int]]:
    """Catch the relayed signals outside ``_run_harness``, so none can end the test run."""
    caught: list[int] = []

    def catch(signum: int, _frame: object) -> None:
        caught.append(signum)

    saved = {signum: signal.getsignal(signum) for signum in release_cmd._HARNESS_INTERRUPTS}
    for signum in saved:
        signal.signal(signum, catch)
    try:
        yield caught
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


@pytest.mark.skipif(os.name != "posix", reason="POSIX sessions, process groups and signals")
class TestHarnessInterrupts:
    """``_run_harness`` with a real harness process and real signals."""

    @staticmethod
    def _harness(tmp_path: Path, mode: str) -> tuple[list[str], Path]:
        script = tmp_path / "harness.py"
        script.write_text(_FAKE_HARNESS, encoding="utf-8")
        log = tmp_path / "harness.log"
        return [sys.executable, str(script), str(log), mode], log

    @staticmethod
    def _run(command: list[str], tmp_path: Path, **timeouts: float) -> int:
        return release_cmd._run_harness(
            command,
            cwd=tmp_path,
            env=dict(os.environ),
            resume_hint="Resume it with --resume.",
            **timeouts,
        )

    def test_an_interrupt_is_passed_on_once_and_its_cleanup_awaited(
        self, tmp_path, stray_signals, capsys
    ):
        command, log = self._harness(tmp_path, "cleanup")
        sender = _signal_self_after(log, "ready", signal.SIGINT)

        assert self._run(command, tmp_path) == 130
        sender.join()
        assert _lines(log) == ["ready", "signal SIGINT", "cleanup finished"]
        notice = capsys.readouterr().err
        assert "passed SIGINT on to the validation harness" in notice
        assert "Waiting up to 6 hours" in notice
        # The command's own handlers are back, and nothing reached them.
        assert all(
            getattr(signal.getsignal(signum), "__name__", "") == "catch"
            for signum in release_cmd._HARNESS_INTERRUPTS
        )
        assert stray_signals == []

    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
    def test_sigterm_and_sighup_are_passed_on_too(self, tmp_path, stray_signals, signum):
        command, log = self._harness(tmp_path, "cleanup")
        sender = _signal_self_after(log, "ready", signum)

        assert self._run(command, tmp_path) == 128 + signum
        sender.join()
        assert _lines(log) == ["ready", f"signal {signal.Signals(signum).name}", "cleanup finished"]

    def test_a_second_interrupt_reaches_the_harness_once_more(self, tmp_path, stray_signals):
        command, log = self._harness(tmp_path, "until-second")
        first = _signal_self_after(log, "ready", signal.SIGINT)
        second = _signal_self_after(log, "cleanup started", signal.SIGINT)

        assert self._run(command, tmp_path) == 130
        first.join()
        second.join()
        assert _lines(log) == [
            "ready",
            "signal SIGINT",
            "cleanup started",
            "signal SIGINT",
            "report written",
        ]

    def test_a_cleanup_past_the_timeout_is_interrupted_again(self, tmp_path, stray_signals, capsys):
        command, log = self._harness(tmp_path, "until-second")
        sender = _signal_self_after(log, "ready", signal.SIGTERM)

        assert self._run(command, tmp_path, interrupt_timeout=0.5) == 143
        sender.join()
        assert _lines(log) == [
            "ready",
            "signal SIGTERM",
            "cleanup started",
            "signal SIGTERM",
            "report written",
        ]
        notice = capsys.readouterr().err
        assert "still running 0.5 seconds after the interrupt" in notice
        assert "waiting up to 5 minutes" in notice

    def test_a_harness_that_never_exits_is_killed_with_the_resume_hint(
        self, tmp_path, stray_signals, capsys
    ):
        command, log = self._harness(tmp_path, "ignore")
        sender = _signal_self_after(log, "ready", signal.SIGINT)
        started = time.monotonic()

        assert self._run(command, tmp_path, interrupt_timeout=0.3, report_timeout=0.3) == 137
        sender.join()
        assert time.monotonic() - started < 30
        notice = capsys.readouterr().err
        assert "did not exit, so it was killed. Resume it with --resume." in notice

    def test_a_signal_ignored_on_entry_stays_ignored(self, tmp_path, stray_signals):
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            command, log = self._harness(tmp_path, "cleanup")
            sender = _signal_self_after(log, "ready", signal.SIGHUP, signal.SIGTERM, pause=0.5)

            assert self._run(command, tmp_path) == 143
            sender.join()
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        finally:
            signal.signal(signal.SIGHUP, previous)
        assert _lines(log) == ["ready", "signal SIGTERM", "cleanup finished"]

    def test_a_harness_that_exits_on_its_own_keeps_its_status(self, tmp_path, stray_signals):
        command, log = self._harness(tmp_path, "exit")
        assert self._run(command, tmp_path) == 3
        assert _lines(log) == ["ready"]

    def test_a_harness_a_signal_ended_exits_128_plus_the_signal(self, tmp_path, stray_signals):
        command, _log = self._harness(tmp_path, "crash")
        assert self._run(command, tmp_path) == 128 + signal.SIGKILL

    def test_a_harness_that_already_exited_is_not_an_error_to_signal(self, tmp_path):
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        process.wait()
        release_cmd._signal_harness(process, signal.SIGINT)

    def test_a_handler_python_cannot_reinstall_comes_back_as_the_default(
        self, tmp_path, stray_signals, monkeypatch
    ):
        # signal.signal returns None for a handler installed outside Python.
        calls: list[tuple[int, object]] = []
        real_signal = signal.signal

        def outside_python(signum, handler):
            calls.append((signum, handler))
            real_signal(signum, handler)

        monkeypatch.setattr(release_cmd.signal, "signal", outside_python)
        command, _log = self._harness(tmp_path, "exit")
        assert self._run(command, tmp_path) == 3
        monkeypatch.undo()
        restores = calls[len(release_cmd._HARNESS_INTERRUPTS) :]
        assert restores == [(signum, signal.SIG_DFL) for signum in release_cmd._HARNESS_INTERRUPTS]

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(21600, "6 hours"), (300, "5 minutes"), (90, "90 seconds"), (0.5, "0.5 seconds")],
    )
    def test_durations_read_naturally(self, seconds, text):
        assert release_cmd._describe_seconds(seconds) == text

    def test_a_closed_terminal_does_not_end_the_wait(self, monkeypatch):
        def closed(*_args, **_kwargs):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(release_cmd.click, "echo", closed)
        release_cmd._notice("anything")


@pytest.mark.skipif(os.name != "posix", reason="POSIX sessions, process groups and signals")
def test_ctrl_c_on_gco_release_validate_waits_for_the_harness_cleanup(tmp_path):
    """The terminal's Ctrl-C on the real command: the harness finishes its cleanup.

    The terminal sends SIGINT to its whole foreground process group. Before
    the fix the harness was in that group with the command, and the command's
    ``subprocess.run`` killed it 0.25 seconds into its cleanup.
    """
    checkout = tmp_path / "checkout"
    package = checkout / "scripts" / "live_release_validation"
    package.mkdir(parents=True)
    (checkout / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(_FAKE_HARNESS, encoding="utf-8")
    (checkout / "cdk.json").write_text("{}", encoding="utf-8")
    git = ["git", "-C", str(checkout)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "test/interrupts"], check=True)
    subprocess.run(
        [
            *git,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    log = tmp_path / "harness.log"
    environment = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "FAKE_HARNESS_LOG": str(log),
        "FAKE_HARNESS_MODE": "cleanup",
    }
    command = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cli.main",
            "release",
            "validate",
            "--expected-account",
            "123456789012",
            CONSENT,
            "--actions",
            "preflight",
            "--run-id",
            "run-1",
            "--report-dir",
            str(tmp_path / "reports"),
        ],
        cwd=checkout,
        env=environment,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(log, "ready")
        os.killpg(command.pid, signal.SIGINT)
        _stdout, stderr = command.communicate(timeout=60)
    finally:
        if command.poll() is None:  # pragma: no cover - only when the test fails
            os.killpg(command.pid, signal.SIGKILL)
            command.wait()

    assert _lines(log) == ["ready", "signal SIGINT", "cleanup finished"], stderr
    assert command.returncode == 130, stderr
    assert "passed SIGINT on to the validation harness" in stderr
