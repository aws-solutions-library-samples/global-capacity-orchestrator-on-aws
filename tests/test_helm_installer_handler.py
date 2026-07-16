"""Unit tests for the helm-installer Lambda handler.

Focus areas:
- ``run_helm`` maps ``subprocess.TimeoutExpired`` to a typed
  ``(-1, "", "timeout: ...")`` tuple instead of raising.
- ``_clear_stuck_release`` detects releases stuck in ``pending-*`` state
  and deletes just the offending release secret(s), preserving history
  for ``deployed`` / ``superseded`` / ``failed`` revisions.
- ``install_chart`` runs the stuck-release preflight before every
  ``helm upgrade --install`` so interrupted prior upgrades never block
  the current deploy.

These tests mock ``subprocess.run`` directly so they never invoke
``helm`` or ``kubectl`` for real.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module

# Load the handler under a unique ``sys.modules`` name via the shared
# helper so this file doesn't collide with other Lambda handler tests
# that use the legacy ``sys.path.insert + import handler`` pattern.
# See ``tests/_lambda_imports.py`` for the full rationale.
helm_handler = load_lambda_module("helm-installer")


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a ``subprocess.CompletedProcess``-shaped MagicMock."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestRunHelmTimeoutHandling:
    """``run_helm`` should convert subprocess timeouts to a typed failure."""

    def test_timeout_returns_negative_one_and_typed_stderr(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["helm"], timeout=300)
            code, stdout, stderr = helm_handler.run_helm(["upgrade", "foo"], "/tmp/kube")
        assert code == -1
        assert stdout == ""
        assert "timeout" in stderr.lower()
        assert "300" in stderr

    def test_successful_run_passes_through_returncode(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="ok", stderr="")
            code, stdout, stderr = helm_handler.run_helm(["upgrade", "foo"], "/tmp/kube")
        assert code == 0
        assert stdout == "ok"
        assert stderr == ""

    def test_non_zero_exit_propagates_stderr(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.return_value = _completed(1, stdout="", stderr="boom")
            code, _, stderr = helm_handler.run_helm(["upgrade", "foo"], "/tmp/kube")
        assert code == 1
        assert stderr == "boom"


class TestClearStuckRelease:
    """Recovery from releases left in ``pending-*`` state by prior failures."""

    def test_returns_false_when_release_not_installed(self):
        # helm status exits non-zero when no release exists.
        with patch.object(helm_handler, "run_helm", return_value=(1, "", "not found")):
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is False

    def test_returns_false_for_deployed_release(self):
        status_json = json.dumps({"info": {"status": "deployed"}})
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, status_json, "")),
            patch.object(helm_handler.subprocess, "run") as mock_run,
        ):
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is False
            mock_run.assert_not_called()

    @pytest.mark.parametrize(
        "stuck_status",
        ["pending-install", "pending-upgrade", "pending-rollback"],
    )
    def test_deletes_secret_for_each_pending_status(self, stuck_status):
        status_json = json.dumps({"info": {"status": stuck_status}})
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, status_json, "")),
            patch.object(helm_handler.subprocess, "run") as mock_run,
        ):
            mock_run.side_effect = [
                # kubectl get secrets -l ... -o jsonpath=...
                _completed(0, stdout="sh.helm.release.v1.foo.v2"),
                # kubectl delete secret ...
                _completed(0),
            ]
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is True
        # Verify the label selector scoped the delete to the exact stuck status.
        list_call_args = mock_run.call_args_list[0][0][0]
        label_flag_idx = list_call_args.index("-l")
        assert f"status={stuck_status}" in list_call_args[label_flag_idx + 1]
        assert "name=foo" in list_call_args[label_flag_idx + 1]

    def test_preserves_deployed_history_secrets(self):
        """Deletion is label-scoped; deployed/superseded/failed revisions stay."""
        status_json = json.dumps({"info": {"status": "pending-upgrade"}})
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, status_json, "")),
            patch.object(helm_handler.subprocess, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0, stdout="sh.helm.release.v1.foo.v2"),
                _completed(0),
            ]
            helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube")
        get_cmd = mock_run.call_args_list[0][0][0]
        # Selector filters on status=pending-upgrade, so deployed/superseded
        # revisions are never returned by this kubectl call and therefore
        # never deleted.
        assert "status=pending-upgrade" in " ".join(get_cmd)

    def test_handles_kubectl_timeout_gracefully(self):
        status_json = json.dumps({"info": {"status": "pending-upgrade"}})
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, status_json, "")),
            patch.object(helm_handler.subprocess, "run") as mock_run,
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["kubectl"], timeout=15)
            # No exception should escape the handler.
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is False

    def test_handles_malformed_status_json(self):
        with patch.object(helm_handler, "run_helm", return_value=(0, "not-json", "")):
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is False


class TestInstallChartPreflight:
    """``install_chart`` must run the stuck-release preflight before every upgrade."""

    def _minimal_config(self):
        return {
            "repo_name": "volcano-sh",
            "repo_url": "https://volcano-sh.github.io/helm-charts",
            "chart": "volcano",
            "version": "1.15.0",
            "namespace": "volcano-system",
            "create_namespace": True,
            "values": {},
        }

    def test_preflight_runs_before_upgrade(self):
        config = self._minimal_config()
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release") as mock_clear,
            patch.object(helm_handler, "run_helm", return_value=(0, "ok", "")) as mock_run,
        ):
            ok, _ = helm_handler.install_chart("volcano", config, "/tmp/kube", None)
        assert ok is True
        mock_clear.assert_called_once_with("volcano", "volcano-system", "/tmp/kube")
        # Preflight must be called before run_helm(upgrade).
        assert mock_clear.call_count == 1
        assert mock_run.call_count == 1

    def test_another_operation_in_progress_clears_and_retries_once(self):
        """Post-upgrade recovery: if helm still complains, clear + retry."""
        config = self._minimal_config()
        stuck_err = (
            "Error: UPGRADE FAILED: another operation (install/upgrade/rollback) is in progress"
        )
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release") as mock_clear,
            patch.object(helm_handler, "run_helm") as mock_run,
        ):
            mock_run.side_effect = [
                (1, "", stuck_err),  # first upgrade attempt
                (0, "ok", ""),  # retry after clearing
            ]
            ok, message = helm_handler.install_chart("volcano", config, "/tmp/kube", None)
        assert ok is True
        assert "after clearing stuck state" in message
        # Preflight + post-failure recovery = 2 clear calls.
        assert mock_clear.call_count == 2
        assert mock_run.call_count == 2

    def test_no_rollback_wait_subprocess_on_failure(self):
        """Regression: the old path ran ``helm rollback --wait`` which hung.

        The new path never invokes rollback at all — it only deletes stuck
        release secrets. This test asserts ``run_helm`` is never called with
        ``rollback`` as its first arg on the ``another operation in
        progress`` recovery path.
        """
        config = self._minimal_config()
        stuck_err = "another operation (install/upgrade/rollback) is in progress"
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm") as mock_run,
        ):
            mock_run.side_effect = [
                (1, "", stuck_err),
                (0, "ok", ""),
            ]
            helm_handler.install_chart("volcano", config, "/tmp/kube", None)
        invoked_args = [call.args[0] for call in mock_run.call_args_list]
        assert not any(args and args[0] == "rollback" for args in invoked_args)

    def test_non_recoverable_failure_surfaces_to_caller(self):
        """A genuine chart failure (not a stuck-state lock) returns False."""
        config = self._minimal_config()
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm") as mock_run,
        ):
            mock_run.return_value = (1, "", "Error: invalid chart values")
            ok, message = helm_handler.install_chart("volcano", config, "/tmp/kube", None)
        assert ok is False
        assert "invalid chart values" in message


class TestInstallChartWaitControl:
    """``install_chart`` honors per-chart ``wait`` / ``wait_timeout`` config."""

    def _config(self, **overrides):
        config = {
            "repo_name": "volcano-sh",
            "repo_url": "https://volcano-sh.github.io/helm-charts",
            "chart": "volcano",
            "version": "1.15.0",
            "namespace": "volcano-system",
            "create_namespace": True,
            "values": {},
        }
        config.update(overrides)
        return config

    def _upgrade_args(self, mock_run):
        """Return the argv of the ``helm upgrade --install`` invocation."""
        for call in mock_run.call_args_list:
            args = call.args[0]
            if args and args[0] == "upgrade":
                return args
        raise AssertionError("no `helm upgrade` invocation captured")

    def test_defaults_include_wait_and_10m_timeout(self):
        """Backward-compatible default: ``--wait --timeout 10m``."""
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm", return_value=(0, "ok", "")) as mock_run,
        ):
            ok, _ = helm_handler.install_chart("volcano", self._config(), "/tmp/kube", None)
        assert ok is True
        args = self._upgrade_args(mock_run)
        assert "--wait" in args
        assert "--timeout" in args
        assert args[args.index("--timeout") + 1] == "10m"

    def test_wait_false_omits_wait_flag(self):
        """``wait: false`` drops ``--wait`` so the install returns after apply."""
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm", return_value=(0, "ok", "")) as mock_run,
        ):
            helm_handler.install_chart(
                "volcano", self._config(wait=False, wait_timeout="8m"), "/tmp/kube", None
            )
        args = self._upgrade_args(mock_run)
        assert "--wait" not in args
        # ``--timeout`` is still passed (it also bounds pre-install hook waits).
        assert args[args.index("--timeout") + 1] == "8m"

    def test_custom_wait_timeout_is_passed_through(self):
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm", return_value=(0, "ok", "")) as mock_run,
        ):
            helm_handler.install_chart(
                "volcano", self._config(wait_timeout="3m"), "/tmp/kube", None
            )
        args = self._upgrade_args(mock_run)
        assert "--wait" in args  # wait defaults to True
        assert args[args.index("--timeout") + 1] == "3m"


class TestHandleTask:
    """``handle_task`` performs exactly one helm op per call and raises on failure."""

    _BASE_EVENT = {
        "Action": "install_chart",
        "Chart": "keda",
        "ClusterName": "gco-us-east-1",
        "Region": "us-east-1",
        "EnabledCharts": ["keda"],
        "Charts": {},
        "KedaOperatorRoleArn": "arn:aws:iam::123456789012:role/keda",
    }

    def test_install_enabled_chart_calls_install(self):
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler, "install_chart", return_value=(True, "Successfully installed keda")
            ) as mock_install,
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(dict(self._BASE_EVENT))

        assert result["status"] == "installed"
        assert result["chart"] == "keda"
        mock_install.assert_called_once()

    def test_install_injects_keda_role_annotation(self):
        captured = {}

        def _capture(chart_name, config, kubeconfig, value_overrides):
            captured["config"] = config
            return (True, "Successfully installed keda")

        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(helm_handler, "install_chart", side_effect=_capture),
            patch.object(helm_handler.os, "remove"),
        ):
            helm_handler.handle_task(dict(self._BASE_EVENT))

        ann = captured["config"]["values"]["serviceAccount"]["operator"]["annotations"]
        assert ann["eks.amazonaws.com/role-arn"] == self._BASE_EVENT["KedaOperatorRoleArn"]

    def test_disabled_chart_on_install_pass_uninstalls(self):
        event = dict(self._BASE_EVENT)
        event["EnabledCharts"] = []  # keda not enabled this pass
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler, "uninstall_chart", return_value=(True, "Successfully uninstalled")
            ) as mock_uninstall,
            patch.object(helm_handler, "install_chart") as mock_install,
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(event)

        assert result["status"] == "uninstalled"
        mock_uninstall.assert_called_once()
        mock_install.assert_not_called()

    def test_uninstall_failure_raises(self):
        event = dict(self._BASE_EVENT)
        event["Action"] = "uninstall_chart"
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(helm_handler, "uninstall_chart", return_value=(False, "api timeout")),
            patch.object(helm_handler, "_record_addon_status") as mock_status,
            patch.object(helm_handler.os, "remove"),
            pytest.raises(RuntimeError, match="helm uninstall keda failed"),
        ):
            helm_handler.handle_task(event)

        mock_status.assert_called_once_with("keda", "failed", "api timeout")

    def test_not_found_uninstall_remains_idempotent_success(self):
        with patch.object(
            helm_handler, "run_helm", return_value=(1, "", "release: not found")
        ) as mock_run:
            success, message = helm_handler.uninstall_chart("keda", "keda", "/tmp/kc")

        assert success is True
        assert "already uninstalled" in message
        args = mock_run.call_args.args[0]
        assert args[args.index("--timeout") + 1] == helm_handler.HELM_UNINSTALL_TIMEOUT
        assert mock_run.call_args.kwargs["command_timeout_seconds"] == 150

    def test_generic_not_found_uninstall_is_failure(self):
        """A Kubernetes/resource NotFound must not be mistaken for release absence."""
        error = 'Error: services "keda-operator" not found while uninstalling release'
        with patch.object(helm_handler, "run_helm", return_value=(1, "", error)):
            success, message = helm_handler.uninstall_chart("keda", "keda", "/tmp/kc")

        assert success is False
        assert error in message

    def test_install_failure_raises(self):
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(helm_handler, "install_chart", return_value=(False, "boom")),
            patch.object(helm_handler.os, "remove"),
            pytest.raises(RuntimeError, match="helm install keda failed"),
        ):
            helm_handler.handle_task(dict(self._BASE_EVENT))

    def test_kubeconfig_always_removed(self):
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(helm_handler, "install_chart", return_value=(True, "Successfully ok")),
            patch.object(helm_handler.os, "remove") as mock_remove,
        ):
            helm_handler.handle_task(dict(self._BASE_EVENT))
        mock_remove.assert_called_once_with("/tmp/kc")

    def test_legacy_delete_reports_failed_uninstall_to_cloudformation(self):
        event = {
            "RequestType": "Delete",
            "LogicalResourceId": "HelmCharts",
            "PhysicalResourceId": "helm-charts",
            "ResourceProperties": {
                "ClusterName": "gco-us-east-1",
                "Region": "us-east-1",
            },
        }
        charts = {"charts": {"keda": {"enabled": True, "namespace": "keda"}}}
        with (
            patch.object(helm_handler, "load_charts_config", return_value=charts),
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(helm_handler, "uninstall_chart", return_value=(False, "forbidden")),
            patch.object(helm_handler, "send_response") as mock_send,
            patch.object(helm_handler.os, "remove"),
        ):
            helm_handler.lambda_handler(event, MagicMock())

        assert mock_send.call_args.args[2] == helm_handler.FAILED
        assert "keda" in mock_send.call_args.args[5]

    def test_lambda_handler_dispatches_action_events(self):
        with patch.object(
            helm_handler, "handle_task", return_value={"chart": "keda", "status": "installed"}
        ) as mock_task:
            out = helm_handler.lambda_handler(dict(self._BASE_EVENT), MagicMock())
        assert out["status"] == "installed"
        mock_task.assert_called_once()


class TestHealthMonitorQuiesce:
    """Delete-time quiescence must scale to zero and wait for all replicas."""

    def test_scale_and_wait_succeed(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [_completed(0), _completed(0)]
            success, message = helm_handler.quiesce_health_monitor("/tmp/kc")

        assert success is True
        assert message == "Health monitor quiesced"
        scale_command = mock_run.call_args_list[0].args[0]
        wait_command = mock_run.call_args_list[1].args[0]
        assert "deployment/health-monitor" in scale_command
        assert "--replicas=0" in scale_command
        assert "--for=delete" in wait_command
        assert "--selector=app=health-monitor" in wait_command

    def test_scale_failure_is_not_masked(self):
        with patch.object(
            helm_handler.subprocess,
            "run",
            return_value=_completed(1, stderr="Error from server (Forbidden): denied"),
        ):
            success, message = helm_handler.quiesce_health_monitor("/tmp/kc")

        assert success is False
        assert "Forbidden" in message

    def test_handle_task_surfaces_quiesce_failure_and_cleans_kubeconfig(self):
        event = {
            "Action": "quiesce_health_monitor",
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
        }
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler,
                "quiesce_health_monitor",
                return_value=(False, "pods remain"),
            ),
            patch.object(helm_handler.os, "remove") as mock_remove,
            pytest.raises(RuntimeError, match="pods remain"),
        ):
            helm_handler.handle_task(event)

        mock_remove.assert_called_once_with("/tmp/kc")
