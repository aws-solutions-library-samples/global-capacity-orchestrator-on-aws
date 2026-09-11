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
- KEDA teardown deletes and waits for all of its custom resources before
  Helm removes the operator and CRDs.

These tests mock ``subprocess.run`` directly so they never invoke
``helm`` or ``kubectl`` for real.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

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

    def test_values_file_is_removed_after_success(self):
        config = self._minimal_config()
        config["values"] = {"apiToken": "sensitive-test-value"}
        observed_paths = []

        def run_helm(args, _kubeconfig):
            values_path = Path(args[args.index("--values") + 1])
            assert values_path.exists()
            assert stat.S_IMODE(values_path.stat().st_mode) == 0o600
            observed_paths.append(values_path)
            return 0, "ok", ""

        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm", side_effect=run_helm),
        ):
            ok, _ = helm_handler.install_chart("volcano", config, "/tmp/kube", None)

        assert ok is True
        assert len(observed_paths) == 1
        assert not observed_paths[0].exists()

    def test_values_file_is_removed_when_helm_raises(self):
        config = self._minimal_config()
        config["values"] = {"apiToken": "sensitive-test-value"}
        observed_paths = []

        def run_helm(args, _kubeconfig):
            values_path = Path(args[args.index("--values") + 1])
            assert values_path.exists()
            observed_paths.append(values_path)
            raise RuntimeError("helm crashed")

        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm", side_effect=run_helm),
            pytest.raises(RuntimeError, match="helm crashed"),
        ):
            helm_handler.install_chart("volcano", config, "/tmp/kube", None)

        assert len(observed_paths) == 1
        assert not observed_paths[0].exists()


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


class TestKedaCustomResourceCleanup:
    """KEDA instances must disappear while its finalizer controller is live."""

    def test_discovers_and_deletes_namespaced_then_cluster_resources(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(
                    0,
                    stdout=(
                        "scaledjobs.keda.sh\n"
                        "scaledobjects.keda.sh\n"
                        "triggerauthentications.keda.sh\n"
                    ),
                ),
                _completed(0, stdout="clustertriggerauthentications.keda.sh\n"),
                _completed(0, stdout="cloudeventsources.eventing.keda.sh\n"),
                _completed(0),
                _completed(0, stdout="deleted namespaced resources"),
                _completed(0, stdout="deleted cluster resources"),
            ]

            success, message = helm_handler._delete_chart_custom_resources("keda", "/tmp/kc")

        assert success is True
        assert "5 keda custom resource type" in message
        namespaced_delete = mock_run.call_args_list[4].args[0]
        cluster_delete = mock_run.call_args_list[5].args[0]
        assert namespaced_delete.index("delete") < namespaced_delete.index("--all-namespaces")
        assert "scaledjobs.keda.sh" in namespaced_delete[namespaced_delete.index("delete") + 1]
        assert "--wait=true" in namespaced_delete
        assert "--all-namespaces" not in cluster_delete
        assert "clustertriggerauthentications.keda.sh" in cluster_delete

    def test_discovery_failure_blocks_cleanup(self):
        with patch.object(
            helm_handler.subprocess,
            "run",
            return_value=_completed(1, stderr="api discovery unavailable"),
        ) as mock_run:
            success, message = helm_handler._delete_chart_custom_resources("keda", "/tmp/kc")

        assert success is False
        assert "api discovery unavailable" in message
        assert mock_run.call_count == 1

    def test_keda_cleanup_runs_before_helm_uninstall(self):
        calls = []

        def _cleanup(_chart_name, _kubeconfig):
            calls.append("cleanup")
            return True, "clean"

        def _helm(*_args, **_kwargs):
            calls.append("helm")
            return 0, "", ""

        with (
            patch.object(helm_handler, "_delete_chart_custom_resources", side_effect=_cleanup),
            patch.object(helm_handler, "run_helm", side_effect=_helm),
        ):
            success, _ = helm_handler.uninstall_chart("keda", "keda", "/tmp/kc")

        assert success is True
        assert calls == ["cleanup", "helm"]

    def test_cleanup_failure_prevents_helm_uninstall(self):
        with (
            patch.object(
                helm_handler,
                "_delete_chart_custom_resources",
                return_value=(False, "scaledjobs remain"),
            ),
            patch.object(helm_handler, "run_helm") as mock_run,
        ):
            success, message = helm_handler.uninstall_chart("keda", "keda", "/tmp/kc")

        assert success is False
        assert "scaledjobs remain" in message
        mock_run.assert_not_called()

    def test_non_finalizer_chart_uninstall_skips_custom_resource_cleanup(self):
        with (
            patch.object(helm_handler, "_delete_chart_custom_resources") as mock_cleanup,
            patch.object(helm_handler, "run_helm", return_value=(0, "", "")),
        ):
            success, _ = helm_handler.uninstall_chart("volcano", "volcano", "/tmp/kc")

        assert success is True
        mock_cleanup.assert_not_called()


class TestKueueCustomResourceCleanup:
    """Kueue instances must disappear while its finalizer controller is live.

    Uninstalling the chart first removes the controller that clears kueue
    finalizers, leaving CRDs wedged in Terminating — live release validation
    run sched241-350ffc7d deadlocked teardown on exactly this (the default
    gco-cluster-queue / gco-default-flavor objects). The purge also
    self-heals the already-wedged state: when the delete wait stalls,
    finalizers are stripped and the delete retried once.
    """

    def test_kueue_uninstall_purges_custom_resources_first(self):
        calls = []

        def _cleanup(chart_name, _kubeconfig):
            calls.append(f"cleanup:{chart_name}")
            return True, "clean"

        def _helm(*_args, **_kwargs):
            calls.append("helm")
            return 0, "", ""

        with (
            patch.object(helm_handler, "_delete_chart_custom_resources", side_effect=_cleanup),
            patch.object(helm_handler, "run_helm", side_effect=_helm),
        ):
            success, _ = helm_handler.uninstall_chart("kueue", "kueue", "/tmp/kc")
        assert success is True
        assert calls == ["cleanup:kueue", "helm"]

    def test_kueue_discovery_targets_the_kueue_api_group(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="localqueues.kueue.x-k8s.io\nworkloads.kueue.x-k8s.io\n"),
                _completed(
                    0, stdout="clusterqueues.kueue.x-k8s.io\nresourceflavors.kueue.x-k8s.io\n"
                ),
                _completed(0, stdout="deleted namespaced"),
                _completed(0, stdout="deleted cluster-scoped"),
            ]
            success, message = helm_handler._delete_chart_custom_resources("kueue", "/tmp/kc")
        assert success is True
        assert "4 kueue custom resource type" in message
        discovery = mock_run.call_args_list[0].args[0]
        assert "--api-group=kueue.x-k8s.io" in discovery

    def test_stalled_delete_strips_finalizers_and_retries_once(self):
        """The wedged-CRD state recovers without failing teardown."""
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                # discovery: namespaced then cluster-scoped
                _completed(0, stdout="localqueues.kueue.x-k8s.io\n"),
                _completed(
                    0, stdout="clusterqueues.kueue.x-k8s.io\nresourceflavors.kueue.x-k8s.io\n"
                ),
                # namespaced delete succeeds
                _completed(0),
                # cluster-scoped delete stalls on finalizers
                _completed(1, stderr="timed out waiting for the condition"),
                # finalizer strip: list + patch per type
                _completed(0, stdout="clusterqueue.kueue.x-k8s.io/gco-cluster-queue\n"),
                _completed(0),
                _completed(0, stdout="resourceflavor.kueue.x-k8s.io/gco-default-flavor\n"),
                _completed(0),
                # retry delete now drains instantly
                _completed(0),
            ]
            success, message = helm_handler._delete_chart_custom_resources("kueue", "/tmp/kc")
        assert success is True
        assert "3 kueue custom resource type" in message
        patch_command = mock_run.call_args_list[5].args[0]
        assert "patch" in patch_command
        assert '{"metadata":{"finalizers":[]}}' in patch_command
        retry_command = mock_run.call_args_list[8].args[0]
        assert "delete" in retry_command and "--all-namespaces" not in retry_command

    def test_failed_finalizer_strip_blocks_teardown(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout=""),
                _completed(0, stdout="clusterqueues.kueue.x-k8s.io\n"),
                _completed(1, stderr="timed out waiting for the condition"),
                # strip: list ok, patch fails hard
                _completed(0, stdout="clusterqueue.kueue.x-k8s.io/gco-cluster-queue\n"),
                _completed(1, stderr="admission webhook denied"),
            ]
            success, message = helm_handler._delete_chart_custom_resources("kueue", "/tmp/kc")
        assert success is False
        assert "finalizer removal also failed" in message
        assert "admission webhook denied" in message

    def test_strip_helper_handles_namespaced_instances(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="gco-jobs,default-queue\n"),
                _completed(0),
            ]
            error = helm_handler._strip_custom_resource_finalizers(
                "/tmp/kc", ["localqueues.kueue.x-k8s.io"], namespaced=True
            )
        assert error is None
        patch_command = mock_run.call_args_list[1].args[0]
        assert patch_command[patch_command.index("-n") + 1] == "gco-jobs"
        assert "default-queue" in patch_command

    def test_strip_helper_tolerates_vanished_resource_types(self):
        """A CRD that finished deleting mid-strip is success, not failure."""
        with patch.object(
            helm_handler.subprocess,
            "run",
            return_value=_completed(1, stderr="the server doesn't have a resource type"),
        ):
            error = helm_handler._strip_custom_resource_finalizers(
                "/tmp/kc", ["clusterqueues.kueue.x-k8s.io"], namespaced=False
            )
        assert error is None


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
        with (
            patch.object(
                helm_handler,
                "_delete_chart_custom_resources",
                return_value=(True, "clean"),
            ),
            patch.object(
                helm_handler, "run_helm", return_value=(1, "", "release: not found")
            ) as mock_run,
        ):
            success, message = helm_handler.uninstall_chart("keda", "keda", "/tmp/kc")

        assert success is True
        assert "already uninstalled" in message
        args = mock_run.call_args.args[0]
        assert args[args.index("--timeout") + 1] == helm_handler.HELM_UNINSTALL_TIMEOUT
        assert mock_run.call_args.kwargs["command_timeout_seconds"] == 75

    def test_lbc_not_found_uninstall_uses_dedicated_budget(self):
        with patch.object(
            helm_handler, "run_helm", return_value=(1, "", "release: not found")
        ) as mock_run:
            success, message = helm_handler.uninstall_chart(
                helm_handler.LBC_CHART_NAME,
                "kube-system",
                "/tmp/kc",
            )

        assert success is True
        assert "already uninstalled" in message
        args = mock_run.call_args.args[0]
        assert args[args.index("--timeout") + 1] == helm_handler.LBC_UNINSTALL_TIMEOUT
        assert (
            mock_run.call_args.kwargs["command_timeout_seconds"]
            == helm_handler.LBC_UNINSTALL_COMMAND_TIMEOUT_SECONDS
        )

    def test_lbc_install_bootstraps_gateway_crds_before_helm(self):
        event = {
            **self._BASE_EVENT,
            "Chart": helm_handler.LBC_CHART_NAME,
            "EnabledCharts": [helm_handler.LBC_CHART_NAME],
            "KedaOperatorRoleArn": None,
        }
        order = []

        def apply_crds(kubeconfig):
            order.append(("crds", kubeconfig))
            return []

        def install(chart_name, config, kubeconfig, value_overrides):
            order.append(("helm", chart_name, kubeconfig))
            return True, "Successfully installed controller"

        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(helm_handler, "_apply_gateway_crds", side_effect=apply_crds),
            patch.object(helm_handler, "install_chart", side_effect=install),
            patch.object(helm_handler, "_record_addon_status"),
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(event)

        assert result["status"] == "installed"
        assert order == [
            ("crds", "/tmp/kc"),
            ("helm", helm_handler.LBC_CHART_NAME, "/tmp/kc"),
        ]

    def test_generic_not_found_uninstall_is_failure(self):
        """A Kubernetes/resource NotFound must not be mistaken for release absence."""
        error = 'Error: services "keda-operator" not found while uninstalling release'
        with (
            patch.object(
                helm_handler,
                "_delete_chart_custom_resources",
                return_value=(True, "clean"),
            ),
            patch.object(helm_handler, "run_helm", return_value=(1, "", error)),
        ):
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


class TestGatewayCrdBootstrap:
    """Pinned Gateway bundles are verified, securely applied, and validated."""

    @staticmethod
    def _crd(name="widgets.example.test", *, established=True):
        return {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": name},
            "status": {
                "conditions": [
                    {
                        "type": "Established",
                        "status": "True" if established else "False",
                    }
                ]
            },
        }

    @classmethod
    def _body(cls):
        return yaml.safe_dump(cls._crd()).encode("utf-8")

    @staticmethod
    def _bundle(body, *, size=None, sha256=None, name="test-gateway-bundle"):
        return helm_handler._PinnedManifestBundle(
            name=name,
            url=f"https://example.test/{name}.yaml",
            size=len(body) if size is None else size,
            sha256=hashlib.sha256(body).hexdigest() if sha256 is None else sha256,
            object_count=1,
            crd_count=1,
        )

    @staticmethod
    def _response(body, status_code=200):
        response = MagicMock()
        response.status = status_code
        response.data = body
        return response

    def test_verified_download_uses_exact_bytes_mode_0600_and_always_cleans_up(self):
        body = self._body()
        bundle = self._bundle(body)
        response = self._response(body)
        pool = MagicMock()
        pool.request.return_value = response
        manifest_path = None

        with (
            patch.object(helm_handler.urllib3, "PoolManager", return_value=pool),
            helm_handler._verified_gateway_crd_bundle(bundle) as (path, resources),
        ):
            manifest_path = path
            assert Path(path).read_bytes() == body
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            assert resources == [self._crd()]

        assert manifest_path is not None
        assert not Path(manifest_path).exists()
        response.release_conn.assert_called_once_with()
        request = pool.request.call_args
        assert request.args == ("GET", bundle.url)
        retries = request.kwargs["retries"]
        assert isinstance(retries, helm_handler.urllib3.Retry)
        assert retries.total == helm_handler.GATEWAY_CRD_HTTP_MAX_REDIRECTS
        assert retries.redirect == helm_handler.GATEWAY_CRD_HTTP_MAX_REDIRECTS
        assert retries.connect == 0
        assert retries.read == 0
        assert retries.status == 0
        assert request.kwargs["redirect"] is True
        assert request.kwargs["headers"] == {"User-Agent": "gco-helm-installer/1"}

    @pytest.mark.parametrize(
        ("status_code", "size_delta", "sha256", "message"),
        [
            (503, 0, None, "HTTP 503"),
            (200, 1, None, "size mismatch"),
            (200, 0, "0" * 64, "SHA-256 mismatch"),
        ],
    )
    def test_verified_download_rejects_status_size_and_hash_mismatch(
        self, status_code, size_delta, sha256, message
    ):
        body = self._body()
        bundle = self._bundle(body, size=len(body) + size_delta, sha256=sha256)
        response = self._response(body, status_code)
        pool = MagicMock()
        pool.request.return_value = response

        with (
            patch.object(helm_handler.urllib3, "PoolManager", return_value=pool),
            pytest.raises(RuntimeError, match=message),
            helm_handler._verified_gateway_crd_bundle(bundle),
        ):
            pytest.fail("invalid bundle must not be yielded")

        response.release_conn.assert_called_once_with()

    def test_verified_bundles_apply_server_side_in_pinned_order(self):
        body = self._body()
        bundles = (
            self._bundle(body, name="first"),
            self._bundle(body, name="second"),
        )
        events = []

        @helm_handler.contextlib.contextmanager
        def verified(bundle):
            path = f"/tmp/{bundle.name}.yaml"
            events.append(("verified", bundle.name))
            yield path, [self._crd(name=f"{bundle.name}.example.test")]

        def run_kubectl(args, kubeconfig, **kwargs):
            events.append(("applied", Path(args[-1]).stem))
            assert kubeconfig == "/tmp/kc"
            assert args[:4] == [
                "apply",
                "--server-side=true",
                "--force-conflicts",
                "--field-manager=gco-helm-installer",
            ]
            assert kwargs["command_timeout_seconds"] == 180
            return 0, "applied", ""

        with (
            patch.object(helm_handler, "PINNED_GATEWAY_CRD_BUNDLES", bundles),
            patch.object(helm_handler, "_verified_gateway_crd_bundle", side_effect=verified),
            patch.object(helm_handler, "run_kubectl", side_effect=run_kubectl),
        ):
            evidence = helm_handler._apply_gateway_crds("/tmp/kc")

        assert events == [
            ("verified", "first"),
            ("applied", "first"),
            ("verified", "second"),
            ("applied", "second"),
        ]
        assert [item["bundle"] for item in evidence] == ["first", "second"]

    def test_live_crd_validation_requires_exact_identity_and_established_true(self):
        body = self._body()
        bundle = self._bundle(body)
        expected = [self._crd()]
        live = self._crd()

        @helm_handler.contextlib.contextmanager
        def verified(_bundle):
            yield "/tmp/test-gateway-bundle.yaml", expected

        with (
            patch.object(helm_handler, "PINNED_GATEWAY_CRD_BUNDLES", (bundle,)),
            patch.object(helm_handler, "_verified_gateway_crd_bundle", side_effect=verified),
            patch.object(
                helm_handler,
                "run_kubectl",
                return_value=(0, json.dumps({"kind": "List", "items": [live]}), ""),
            ) as run_kubectl,
        ):
            evidence = helm_handler._validate_gateway_crds(
                "/tmp/kc", helm_handler.time.monotonic() + 60
            )

        assert evidence == [
            {
                "bundle": bundle.name,
                "object_count": 1,
                "crd_count": 1,
                "sha256": bundle.sha256,
            }
        ]
        assert run_kubectl.call_args.args[:2] == (
            ["get", "-f", "/tmp/test-gateway-bundle.yaml", "-o", "json"],
            "/tmp/kc",
        )

    @pytest.mark.parametrize(
        ("live", "message"),
        [
            (_crd.__func__(name="foreign.example.test"), "missing=.*widgets.example.test"),
            (_crd.__func__(established=False), "Established=True"),
        ],
    )
    def test_live_crd_validation_rejects_identity_or_established_drift(self, live, message):
        body = self._body()
        bundle = self._bundle(body)
        expected = [self._crd()]

        @helm_handler.contextlib.contextmanager
        def verified(_bundle):
            yield "/tmp/test-gateway-bundle.yaml", expected

        with (
            patch.object(helm_handler, "PINNED_GATEWAY_CRD_BUNDLES", (bundle,)),
            patch.object(helm_handler, "_verified_gateway_crd_bundle", side_effect=verified),
            patch.object(
                helm_handler,
                "run_kubectl",
                return_value=(0, json.dumps({"kind": "List", "items": [live]}), ""),
            ),
            pytest.raises(RuntimeError, match=message),
        ):
            helm_handler._validate_gateway_crds("/tmp/kc", helm_handler.time.monotonic() + 60)


class TestReleaseConvergenceValidation:
    """The validation action proves exact Helm state and live readiness."""

    RELEASE = "demo-release"
    CHART = "demo-chart"
    VERSION = "1.2.3"
    NAMESPACE = "demo-system"

    def _charts(self, *, wait=True, include_disabled=False):
        charts = {
            self.RELEASE: {
                "enabled": True,
                "repo_name": "demo",
                "repo_url": "https://example.invalid/charts",
                "chart": self.CHART,
                "version": self.VERSION,
                "namespace": self.NAMESPACE,
                "wait": wait,
                "values": {"nested": {"default": True}},
            }
        }
        if include_disabled:
            charts["disabled-release"] = {
                "enabled": False,
                "repo_name": "demo",
                "repo_url": "https://example.invalid/charts",
                "chart": "disabled-chart",
                "version": "9.8.7",
                "namespace": "disabled-system",
            }
        return {"charts": charts}

    def _event(self, *, enabled=None, action=None):
        event = {
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "EnabledCharts": [self.RELEASE] if enabled is None else enabled,
            "Charts": {},
            "DeploymentToken": "deploy-2026-07-18T01:02:03Z",
        }
        if action:
            event["Action"] = action
        return event

    def _helm_success(self, manifest, *, status="deployed", chart_version=None):
        expected_chart = chart_version or f"{self.CHART}-{self.VERSION}"

        def _run(args, _kubeconfig, **_kwargs):
            if args[0] == "status":
                return 0, json.dumps({"info": {"status": status}}), ""
            if args[0] == "list":
                return (
                    0,
                    json.dumps(
                        [
                            {
                                "name": self.RELEASE,
                                "namespace": self.NAMESPACE,
                                "status": "deployed",
                                "chart": expected_chart,
                            }
                        ]
                    ),
                    "",
                )
            if args[:2] == ["get", "manifest"]:
                return 0, manifest, ""
            raise AssertionError(f"unexpected helm invocation: {args}")

        return _run

    @staticmethod
    def _expected_deployment():
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "demo-controller", "namespace": "demo-system"},
            "spec": {"replicas": 1},
        }

    @classmethod
    def _live_deployment(cls, *, ready=True):
        deployment = cls._expected_deployment()
        deployment["metadata"]["generation"] = 4
        ready_replicas = 1 if ready else 0
        deployment["status"] = {
            "observedGeneration": 4,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": ready_replicas,
            "availableReplicas": ready_replicas,
            "conditions": [{"type": "Available", "status": "True" if ready else "False"}],
        }
        return deployment

    @staticmethod
    def _service():
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "demo-webhook", "namespace": "demo-system"},
            "spec": {"selector": {"app": "demo"}},
        }

    @staticmethod
    def _live_list(*resources):
        return json.dumps({"apiVersion": "v1", "kind": "List", "items": list(resources)})

    @staticmethod
    def _manifest(*resources):
        return helm_handler.yaml.safe_dump_all(resources)

    def test_all_enabled_release_is_exact_deployed_and_ready(self):
        # The rendered List document must expand to both objects. Runtime
        # version override exercises the same recursive merge used by install.
        expected_deployment = self._expected_deployment()
        service = self._service()
        manifest = helm_handler.yaml.safe_dump(
            {"apiVersion": "v1", "kind": "List", "items": [expected_deployment, service]}
        )
        event = self._event()
        event["Charts"] = {
            self.RELEASE: {
                "version": "1.2.4",
                "values": {"nested": {"runtime": True}},
            }
        }

        def _kubectl(args, _kubeconfig, **_kwargs):
            if "endpointslices.discovery.k8s.io" in args:
                return (
                    0,
                    json.dumps(
                        {
                            "apiVersion": "discovery.k8s.io/v1",
                            "kind": "EndpointSliceList",
                            "items": [{"endpoints": [{"conditions": {"ready": True}}]}],
                        }
                    ),
                    "",
                )
            return 0, self._live_list(self._live_deployment(), service), ""

        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(
                helm_handler,
                "run_helm",
                side_effect=self._helm_success(manifest, chart_version=f"{self.CHART}-1.2.4"),
            ) as mock_helm,
            patch.object(helm_handler, "run_kubectl", side_effect=_kubectl),
        ):
            evidence = helm_handler.validate_releases(event, "/tmp/kubeconfig")

        assert evidence["status"] == "validated"
        assert evidence["DeploymentToken"] == event["DeploymentToken"]
        assert evidence["expected_release_count"] == 1
        assert evidence["validated_release_count"] == 1
        assert evidence["expected_resource_count"] == 2
        assert evidence["validated_resource_count"] == 2
        assert evidence["releases"] == [
            {
                "release": self.RELEASE,
                "namespace": self.NAMESPACE,
                "chart": self.CHART,
                "version": "1.2.4",
                "enabled": True,
                "status": "deployed",
                "resource_count": 2,
            }
        ]
        helm_args = [call.args[0] for call in mock_helm.call_args_list]
        assert helm_args[0] == [
            "status",
            self.RELEASE,
            "-n",
            self.NAMESPACE,
            "-o",
            "json",
        ]
        assert helm_args[1] == [
            "list",
            "-n",
            self.NAMESPACE,
            "--filter",
            f"^{self.RELEASE}$",
            "-o",
            "json",
        ]
        assert helm_args[2] == ["get", "manifest", self.RELEASE, "-n", self.NAMESPACE]
        assert all(
            call.kwargs["command_timeout_seconds"]
            == helm_handler.HELM_VALIDATION_COMMAND_TIMEOUT_SECONDS
            for call in mock_helm.call_args_list
        )
        assert all(call.kwargs["log_output"] is False for call in mock_helm.call_args_list)

    def test_disabled_release_accepts_only_exact_helm_absence(self):
        charts = self._charts()
        charts["charts"] = {
            "disabled-release": {
                "chart": "disabled-chart",
                "version": "9.8.7",
                "namespace": "disabled-system",
            }
        }
        event = self._event(enabled=[])
        with (
            patch.object(helm_handler, "load_charts_config", return_value=charts),
            patch.object(
                helm_handler,
                "run_helm",
                return_value=(1, "", "Error: release: not found\n"),
            ) as mock_helm,
            patch.object(helm_handler, "run_kubectl") as mock_kubectl,
        ):
            evidence = helm_handler.validate_releases(event, "/tmp/kubeconfig")

        assert evidence["validated_release_count"] == 1
        assert evidence["releases"][0]["status"] == "absent"
        assert evidence["releases"][0]["resource_count"] == 0
        mock_kubectl.assert_not_called()
        assert mock_helm.call_args.args[0] == [
            "status",
            "disabled-release",
            "-n",
            "disabled-system",
            "-o",
            "json",
        ]

    @pytest.mark.parametrize(
        ("helm_result", "message"),
        [
            ((1, "", "release not found"), "absence is ambiguous"),
            ((1, "extra output", "Error: release: not found"), "absence is ambiguous"),
            ((0, json.dumps({"info": {"status": "deployed"}}), ""), "still present"),
        ],
    )
    def test_disabled_release_rejects_present_or_ambiguous_results(self, helm_result, message):
        charts = self._charts()
        charts["charts"] = {
            "disabled-release": {
                "chart": "disabled-chart",
                "version": "9.8.7",
                "namespace": "disabled-system",
            }
        }
        with (
            patch.object(helm_handler, "load_charts_config", return_value=charts),
            patch.object(helm_handler, "run_helm", return_value=helm_result),
            pytest.raises(RuntimeError, match=message),
        ):
            helm_handler.validate_releases(self._event(enabled=[]), "/tmp/kubeconfig")

    @pytest.mark.parametrize("failure", ["missing", "mismatched", "stale"])
    def test_missing_mismatched_or_stale_release_fails(self, failure):
        manifest = self._manifest(self._expected_deployment())

        def _helm(args, _kubeconfig, **_kwargs):
            if args[0] == "status":
                if failure == "missing":
                    return 1, "", "Error: release: not found"
                status = "pending-upgrade" if failure == "stale" else "deployed"
                return 0, json.dumps({"info": {"status": status}}), ""
            if args[0] == "list":
                chart = "demo-chart-1.2.2" if failure == "mismatched" else "demo-chart-1.2.3"
                return (
                    0,
                    json.dumps(
                        [
                            {
                                "name": self.RELEASE,
                                "namespace": self.NAMESPACE,
                                "status": "deployed",
                                "chart": chart,
                            }
                        ]
                    ),
                    "",
                )
            return 0, manifest, ""

        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "run_helm", side_effect=_helm),
            patch.object(helm_handler, "run_kubectl") as mock_kubectl,
            pytest.raises(RuntimeError),
        ):
            helm_handler.validate_releases(self._event(), "/tmp/kubeconfig")
        mock_kubectl.assert_not_called()

    def test_one_missing_rendered_object_fails_identity_comparison(self):
        deployment = self._expected_deployment()
        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo-config", "namespace": self.NAMESPACE},
        }
        manifest = self._manifest(deployment, config_map)
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(
                helm_handler,
                "run_kubectl",
                return_value=(0, self._live_list(self._live_deployment()), ""),
            ),
            pytest.raises(RuntimeError, match="missing=.*ConfigMap"),
        ):
            helm_handler.validate_releases(self._event(), "/tmp/kubeconfig")

    def test_unready_deployment_fails(self):
        manifest = self._manifest(self._expected_deployment())
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(
                helm_handler,
                "run_kubectl",
                return_value=(0, self._live_list(self._live_deployment(ready=False)), ""),
            ),
            pytest.raises(RuntimeError, match="not converged"),
        ):
            helm_handler.validate_releases(self._event(), "/tmp/kubeconfig")

    def test_wait_false_chart_is_still_readiness_gated(self):
        custom_resource = {
            "apiVersion": "example.io/v1",
            "kind": "Widget",
            "metadata": {"name": "demo-widget", "namespace": self.NAMESPACE},
        }
        live = {
            **custom_resource,
            "status": {
                "conditions": [{"type": "Ready", "status": "False", "message": "still reconciling"}]
            },
        }
        manifest = self._manifest(custom_resource)
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts(wait=False)),
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(
                helm_handler,
                "run_kubectl",
                return_value=(0, self._live_list(live), ""),
            ),
            pytest.raises(RuntimeError, match="Ready=False"),
        ):
            helm_handler.validate_releases(self._event(), "/tmp/kubeconfig")

    def test_selector_service_without_ready_endpoint_slice_fails(self):
        service = self._service()
        manifest = self._manifest(service)
        kubectl_results = [
            (0, self._live_list(service), ""),
            (
                0,
                json.dumps(
                    {
                        "apiVersion": "discovery.k8s.io/v1",
                        "kind": "EndpointSliceList",
                        "items": [
                            {
                                "endpoints": [
                                    {"conditions": {"ready": False}},
                                    {"conditions": {"ready": False}},
                                ]
                            }
                        ],
                    }
                ),
                "",
            ),
        ]
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(helm_handler, "run_kubectl", side_effect=kubectl_results) as mock_kubectl,
            # An infinite poll interval exceeds any remaining budget, so the
            # readiness poll degenerates to the single observation under test.
            patch.object(helm_handler, "ENDPOINT_READINESS_POLL_SECONDS", float("inf")),
            pytest.raises(RuntimeError, match="no ready, non-terminating EndpointSlice endpoint"),
        ):
            helm_handler.validate_releases(self._event(), "/tmp/kubeconfig")

        endpoint_args = mock_kubectl.call_args_list[1].args[0]
        assert "endpointslices.discovery.k8s.io" in endpoint_args
        assert f"kubernetes.io/service-name={service['metadata']['name']}" in endpoint_args

    def test_slow_starting_service_endpoint_converges_within_deadline(self):
        """Regression: Grafana's first-boot migrations outlived a one-shot check.

        A live run crash-looped Grafana because its fresh-PVC migrations ran
        past the probe budget; even after that was fixed, endpoint readiness
        arrives minutes after installation. The validator polls until the
        shared deadline, so a not-ready-then-ready Service must pass.
        """
        service = self._service()
        manifest = self._manifest(service)
        not_ready = json.dumps(
            {
                "apiVersion": "discovery.k8s.io/v1",
                "kind": "EndpointSliceList",
                "items": [{"endpoints": [{"conditions": {"ready": False}}]}],
            }
        )
        ready = json.dumps(
            {
                "apiVersion": "discovery.k8s.io/v1",
                "kind": "EndpointSliceList",
                "items": [{"endpoints": [{"conditions": {"ready": True}}]}],
            }
        )
        kubectl_results = [
            (0, self._live_list(service), ""),
            (0, not_ready, ""),
            (0, not_ready, ""),
            (0, ready, ""),
        ]
        sleeps: list[float] = []
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(helm_handler, "run_kubectl", side_effect=kubectl_results) as mock_kubectl,
            patch.object(helm_handler.time, "sleep", side_effect=sleeps.append),
        ):
            result = helm_handler.validate_releases(self._event(), "/tmp/kubeconfig")

        assert result["status"] == "validated"
        assert result["validated_release_count"] == 1
        assert sleeps == [helm_handler.ENDPOINT_READINESS_POLL_SECONDS] * 2
        endpoint_queries = sum(
            "endpointslices.discovery.k8s.io" in call.args[0]
            for call in mock_kubectl.call_args_list
        )
        assert endpoint_queries == 3

    @pytest.mark.parametrize(
        "conditions",
        [
            {},
            {"ready": True, "terminating": True},
        ],
    )
    def test_service_requires_explicitly_ready_nonterminating_endpoint(self, conditions):
        service = self._service()
        manifest = self._manifest(service)
        kubectl_results = [
            (0, self._live_list(service), ""),
            (
                0,
                json.dumps(
                    {
                        "apiVersion": "discovery.k8s.io/v1",
                        "kind": "EndpointSliceList",
                        "items": [{"endpoints": [{"conditions": conditions}]}],
                    }
                ),
                "",
            ),
        ]
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(helm_handler, "run_kubectl", side_effect=kubectl_results),
            patch.object(helm_handler, "ENDPOINT_READINESS_POLL_SECONDS", float("inf")),
            pytest.raises(RuntimeError, match="no ready, non-terminating EndpointSlice endpoint"),
        ):
            helm_handler.validate_releases(self._event(), "/tmp/kubeconfig")

    def _required_readiness_resource(self, kind, *, ready):
        resource = {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {"name": f"demo-{kind.lower()}", "namespace": self.NAMESPACE},
        }
        condition_status = "True" if ready else "False"
        if kind == "StatefulSet":
            resource.update(
                {
                    "apiVersion": "apps/v1",
                    "metadata": {**resource["metadata"], "generation": 3},
                    "spec": {"replicas": 2},
                    "status": {
                        "observedGeneration": 3,
                        "currentReplicas": 2,
                        "updatedReplicas": 2 if ready else 1,
                        "readyReplicas": 2,
                    },
                }
            )
        elif kind == "DaemonSet":
            resource.update(
                {
                    "apiVersion": "apps/v1",
                    "metadata": {**resource["metadata"], "generation": 3},
                    "status": {
                        "observedGeneration": 3,
                        "desiredNumberScheduled": 2,
                        "currentNumberScheduled": 2,
                        "updatedNumberScheduled": 2,
                        "numberReady": 2,
                        "numberAvailable": 2 if ready else 1,
                        "numberMisscheduled": 0,
                    },
                }
            )
        elif kind in ("Job", "Pod"):
            condition = "Complete" if kind == "Job" else "Ready"
            resource["status"] = {"conditions": [{"type": condition, "status": condition_status}]}
        elif kind == "PersistentVolumeClaim":
            resource["status"] = {"phase": "Bound" if ready else "Pending"}
        elif kind == "PersistentVolume":
            resource["status"] = {"phase": "Available" if ready else "Released"}
        elif kind == "Ingress":
            resource["apiVersion"] = "networking.k8s.io/v1"
            resource["status"] = {
                "loadBalancer": {"ingress": [{"hostname": "demo.example.com"}] if ready else []}
            }
        elif kind == "CustomResourceDefinition":
            resource["apiVersion"] = "apiextensions.k8s.io/v1"
            resource["status"] = {
                "conditions": [{"type": "Established", "status": condition_status}]
            }
        elif kind == "APIService":
            resource["apiVersion"] = "apiregistration.k8s.io/v1"
            resource["status"] = {"conditions": [{"type": "Available", "status": condition_status}]}
        elif kind == "HorizontalPodAutoscaler":
            resource["apiVersion"] = "autoscaling/v2"
            resource["metadata"]["generation"] = 3
            resource["status"] = {
                "observedGeneration": 3,
                "conditions": [
                    {"type": "AbleToScale", "status": condition_status},
                    {"type": "ScalingActive", "status": condition_status},
                ],
            }
        elif kind == "PodDisruptionBudget":
            resource["apiVersion"] = "policy/v1"
            resource["metadata"]["generation"] = 3
            resource["status"] = {
                "observedGeneration": 3,
                "currentHealthy": 2 if ready else 0,
                "desiredHealthy": 1,
            }
        else:
            raise AssertionError(f"unsupported readiness kind: {kind}")
        return resource

    @pytest.mark.parametrize(
        "resource",
        [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "scaled-down", "generation": 2},
                "spec": {"replicas": 0},
                "status": {
                    "observedGeneration": 2,
                    "conditions": [{"type": "Available", "status": "True"}],
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": "scaled-down", "generation": 2},
                "spec": {"replicas": 0},
                "status": {"observedGeneration": 2},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "DaemonSet",
                "metadata": {"name": "no-eligible-nodes", "generation": 2},
                "status": {"observedGeneration": 2, "desiredNumberScheduled": 0},
            },
        ],
        ids=["deployment", "statefulset", "daemonset"],
    )
    def test_zero_desired_workloads_accept_omitted_zero_counters(self, resource):
        helm_handler._validate_resource_readiness(resource)

    def test_daemonset_still_requires_desired_counter(self):
        resource = self._required_readiness_resource("DaemonSet", ready=True)
        del resource["status"]["desiredNumberScheduled"]
        with pytest.raises(RuntimeError, match="no desiredNumberScheduled"):
            helm_handler._validate_resource_readiness(resource)

    @pytest.mark.parametrize(
        "kind",
        [
            "StatefulSet",
            "DaemonSet",
            "Job",
            "Pod",
            "PersistentVolumeClaim",
            "PersistentVolume",
            "Ingress",
            "CustomResourceDefinition",
            "APIService",
            "HorizontalPodAutoscaler",
            "PodDisruptionBudget",
        ],
    )
    def test_each_required_resource_kind_accepts_ready_state(self, kind):
        helm_handler._validate_resource_readiness(
            self._required_readiness_resource(kind, ready=True)
        )

    @pytest.mark.parametrize(
        "kind",
        [
            "StatefulSet",
            "DaemonSet",
            "Job",
            "Pod",
            "PersistentVolumeClaim",
            "PersistentVolume",
            "Ingress",
            "CustomResourceDefinition",
            "APIService",
            "HorizontalPodAutoscaler",
            "PodDisruptionBudget",
        ],
    )
    def test_each_required_resource_kind_rejects_unready_state(self, kind):
        with pytest.raises(RuntimeError):
            helm_handler._validate_resource_readiness(
                self._required_readiness_resource(kind, ready=False)
            )

    @pytest.mark.parametrize("kind", ["StatefulSet", "DaemonSet"])
    def test_controller_readiness_rejects_stale_generation(self, kind):
        resource = self._required_readiness_resource(kind, ready=True)
        resource["status"]["observedGeneration"] = 2
        with pytest.raises(RuntimeError, match="stale generation"):
            helm_handler._validate_resource_readiness(resource)

    @pytest.mark.parametrize("kind", ["HorizontalPodAutoscaler", "PodDisruptionBudget"])
    def test_policy_readiness_rejects_stale_generation(self, kind):
        resource = self._required_readiness_resource(kind, ready=True)
        resource["status"]["observedGeneration"] = 2
        with pytest.raises(RuntimeError, match="stale generation"):
            helm_handler._validate_resource_readiness(resource)

    def test_terminating_resource_is_not_ready(self):
        resource = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "terminating-config",
                "namespace": self.NAMESPACE,
                "deletionTimestamp": "2026-07-18T01:02:03Z",
            },
        }
        with pytest.raises(RuntimeError, match="terminating"):
            helm_handler._validate_resource_readiness(resource)

    def test_daemonset_with_misscheduled_pod_is_not_converged(self):
        resource = self._required_readiness_resource("DaemonSet", ready=True)
        resource["status"]["numberMisscheduled"] = 1
        with pytest.raises(RuntimeError, match="numberMisscheduled"):
            helm_handler._validate_resource_readiness(resource)

    def test_hpa_requires_active_scaling(self):
        resource = self._required_readiness_resource("HorizontalPodAutoscaler", ready=True)
        for condition in resource["status"]["conditions"]:
            if condition["type"] == "ScalingActive":
                condition["status"] = "False"
        with pytest.raises(RuntimeError, match="ScalingActive=True"):
            helm_handler._validate_resource_readiness(resource)

    def test_manifest_omitting_namespace_requires_release_namespace_for_live_object(self):
        expected = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo-config"},
        }
        wrong_namespace = {
            **expected,
            "metadata": {"name": "demo-config", "namespace": "other-system"},
        }
        with pytest.raises(RuntimeError, match="wrong namespace"):
            helm_handler._compare_resource_identities(
                [expected], [wrong_namespace], self.RELEASE, self.NAMESPACE
            )

        # Namespace-less live objects remain valid for cluster-scoped kinds.
        cluster_object = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "demo-role"},
        }
        helm_handler._compare_resource_identities(
            [cluster_object], [cluster_object], self.RELEASE, self.NAMESPACE
        )

    def test_templated_namespace_on_cluster_scoped_object_accepts_cluster_return(self):
        """Regression: kueue failed live validation on a real cluster.

        The kueue chart templates ``metadata.namespace: kueue-system`` onto its
        cluster-scoped MutatingWebhookConfiguration. The API server discards
        the field, so kubectl returns the object without a namespace and the
        exact ``(identity, namespace)`` match rejected a healthy release.
        """
        rendered = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "MutatingWebhookConfiguration",
            "metadata": {
                "name": "kueue-mutating-webhook-configuration",
                "namespace": "kueue-system",
            },
        }
        live = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "MutatingWebhookConfiguration",
            "metadata": {"name": "kueue-mutating-webhook-configuration"},
        }
        helm_handler._compare_resource_identities([rendered], [live], "kueue", "kueue-system")

    def test_explicit_namespace_still_rejects_wrong_namespace_return(self):
        # The cluster-scope fallback must not weaken the namespaced check: a
        # live object in a different namespace is still a validation failure.
        rendered = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo-config", "namespace": "demo-system"},
        }
        live = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo-config", "namespace": "other-system"},
        }
        with pytest.raises(RuntimeError, match="wrong namespace") as excinfo:
            helm_handler._compare_resource_identities(
                [rendered], [live], self.RELEASE, self.NAMESPACE
            )
        assert "other-system" in str(excinfo.value)

    def test_cross_namespace_rendered_objects_are_retrieved_per_namespace(self):
        """Regression: one ``-n`` for a mixed-namespace manifest broke live runs.

        KEDA, cert-manager, and kueue render kube-system auth-reader
        RoleBindings and kube-prometheus-stack renders kube-system metric
        Services; kubectl refuses ``get -f`` when an object's namespace does
        not match the single requested namespace, which failed validation for
        every such chart on a real cluster.
        """
        expected_deployment = self._expected_deployment()
        auth_reader = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "demo-auth-reader", "namespace": "kube-system"},
        }
        manifest = self._manifest(expected_deployment, auth_reader)
        kubectl_calls = []

        def _kubectl(args, _kubeconfig, **_kwargs):
            requested_namespace = args[args.index("-n") + 1]
            with open(args[args.index("-f") + 1], encoding="utf-8") as manifest_file:
                group = list(helm_handler.yaml.safe_load_all(manifest_file))
            kubectl_calls.append((requested_namespace, [doc["kind"] for doc in group]))
            if requested_namespace == "kube-system":
                assert group == [auth_reader]
                return 0, self._live_list(auth_reader), ""
            assert requested_namespace == self.NAMESPACE
            assert group == [expected_deployment]
            return 0, self._live_list(self._live_deployment()), ""

        with (
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(helm_handler, "run_kubectl", side_effect=_kubectl),
        ):
            count = helm_handler._validate_enabled_release(
                self.RELEASE,
                self.CHART,
                self.VERSION,
                self.NAMESPACE,
                "/tmp/kubeconfig",
                deadline=time.monotonic() + 60,
            )

        assert count == 2
        assert sorted(call[0] for call in kubectl_calls) == [self.NAMESPACE, "kube-system"]

    def test_systemic_timeout_stops_further_release_checks(self):
        charts = self._charts(include_disabled=True)
        with (
            patch.object(helm_handler, "load_charts_config", return_value=charts),
            patch.object(
                helm_handler,
                "run_helm",
                return_value=(-1, "", "timeout: helm command exceeded 120s"),
            ) as mock_helm,
            pytest.raises(RuntimeError, match="timed out"),
        ):
            helm_handler.validate_releases(self._event(), "/tmp/kubeconfig")

        assert mock_helm.call_count == 1

    def test_handle_task_success_cleans_files_records_status_and_returns_token(self):
        fd, kubeconfig = tempfile.mkstemp(prefix="helm-validation-test-kube-")
        os.close(fd)
        manifest = self._manifest(self._expected_deployment())
        manifest_paths = []

        def _kubectl(args, _kubeconfig, **_kwargs):
            manifest_path = args[args.index("-f") + 1]
            manifest_paths.append(manifest_path)
            assert os.path.exists(manifest_path)
            assert stat.S_IMODE(os.stat(manifest_path).st_mode) == 0o600
            return 0, self._live_list(self._live_deployment()), ""

        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value=kubeconfig),
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(helm_handler, "run_kubectl", side_effect=_kubectl),
            patch.object(helm_handler, "_record_addon_status") as mock_status,
        ):
            evidence = helm_handler.handle_task(self._event(action="validate_releases"))

        assert evidence["DeploymentToken"] == self._event()["DeploymentToken"]
        assert not os.path.exists(kubeconfig)
        assert manifest_paths and all(not os.path.exists(path) for path in manifest_paths)
        mock_status.assert_called_once()
        assert mock_status.call_args.args[:2] == ("helm-validation", "validated")

    def test_handle_task_failure_cleans_files_and_records_failed_status(self):
        fd, kubeconfig = tempfile.mkstemp(prefix="helm-validation-test-kube-")
        os.close(fd)
        manifest = self._manifest(self._expected_deployment())
        manifest_paths = []

        def _kubectl(args, _kubeconfig, **_kwargs):
            manifest_paths.append(args[args.index("-f") + 1])
            return 1, "", 'deployments.apps "demo-controller" not found'

        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value=kubeconfig),
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "run_helm", side_effect=self._helm_success(manifest)),
            patch.object(helm_handler, "run_kubectl", side_effect=_kubectl),
            patch.object(helm_handler, "_record_addon_status") as mock_status,
            pytest.raises(RuntimeError, match="helm release validation failed"),
        ):
            helm_handler.handle_task(self._event(action="validate_releases"))

        assert not os.path.exists(kubeconfig)
        assert manifest_paths and all(not os.path.exists(path) for path in manifest_paths)
        mock_status.assert_called_once()
        assert mock_status.call_args.args[:2] == ("helm-validation", "failed")

    def test_handle_task_cleanup_error_records_failed_not_validated(self):
        evidence = {
            "validated_release_count": 1,
            "expected_release_count": 1,
            "validated_resource_count": 1,
            "expected_resource_count": 1,
        }
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(helm_handler, "validate_releases", return_value=evidence),
            patch.object(helm_handler.os, "remove", side_effect=PermissionError("unlink denied")),
            patch.object(helm_handler, "_record_addon_status") as mock_status,
            pytest.raises(RuntimeError, match="unlink denied"),
        ):
            helm_handler.handle_task(self._event(action="validate_releases"))

        mock_status.assert_called_once()
        assert mock_status.call_args.args[:2] == ("helm-validation", "failed")

    def test_run_kubectl_is_bounded_and_never_uses_a_shell(self):
        with patch.object(
            helm_handler.subprocess, "run", return_value=_completed(0, stdout="{}")
        ) as mock_run:
            code, stdout, stderr = helm_handler.run_kubectl(
                ["get", "pods", "-o", "json"], "/tmp/kubeconfig"
            )

        assert (code, stdout, stderr) == (0, "{}", "")
        command = mock_run.call_args.args[0]
        assert command[:4] == [
            "kubectl",
            "--kubeconfig",
            "/tmp/kubeconfig",
            "--request-timeout=30s",
        ]
        assert mock_run.call_args.kwargs["timeout"] == 120
        assert "shell" not in mock_run.call_args.kwargs


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

    def test_missing_namespace_is_idempotent_absence(self):
        """A deploy that failed before base manifests never created gco-system.

        Regression (2026-09 live validation, run sched241-1ae7c0d3): the
        quiesce step treated the namespace NotFound as fatal, the HelmTeardown
        custom resource FAILED, and the whole stack wedged DELETE_FAILED.
        Nothing-was-ever-there must succeed exactly like
        deployment-already-gone.
        """
        absence = 'Error from server (NotFound): namespaces "gco-system" not found'
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(1, stderr=absence),
                _completed(1, stderr=absence),
            ]
            success, message = helm_handler.quiesce_health_monitor("/tmp/kc")

        assert success is True
        assert message == "Health monitor quiesced"

    def test_missing_deployment_is_idempotent_absence(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(
                    1,
                    stderr=(
                        'Error from server (NotFound): deployments.apps "health-monitor" not found'
                    ),
                ),
                _completed(1, stderr="error: no matching resources found"),
            ]
            success, message = helm_handler.quiesce_health_monitor("/tmp/kc")

        assert success is True
        assert message == "Health monitor quiesced"

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


class TestReleaseSetExpectations:
    """The validate_releases expected set is deployment-config-driven.

    There is deliberately no fixed release list anywhere: every entry in the
    REAL charts.yaml is an expected release, and EnabledCharts decides which
    must be deployed versus absent. These pins prove the two 6.0 charts ride
    that mechanism — present in the expected set, enabled exactly when the
    convergence payload enables them — so ValidateHelmReleases needs no
    per-chart wiring.
    """

    def test_new_charts_are_expected_and_enabled_when_converged_on(self):
        configurations, enabled = helm_handler._release_configurations(
            {"EnabledCharts": ["keda", "kubeflow-trainer", "mlflow"], "Charts": {}}
        )
        names = [release for release, _ in configurations]
        assert "kubeflow-trainer" in names
        assert "mlflow" in names
        assert enabled == {"keda", "kubeflow-trainer", "mlflow"}

    def test_new_charts_are_expected_absent_when_not_enabled(self):
        configurations, enabled = helm_handler._release_configurations(
            {"EnabledCharts": ["keda"], "Charts": {}}
        )
        names = {release for release, _ in configurations}
        # Still in the expected set (from charts.yaml), so validation asserts
        # their ABSENCE — flipping a toggle off is verified, not ignored.
        assert {"kubeflow-trainer", "mlflow"} <= names
        assert enabled == {"keda"}

    def test_release_metadata_resolves_for_both_charts(self):
        configurations, _ = helm_handler._release_configurations(
            {"EnabledCharts": [], "Charts": {}}
        )
        by_name = dict(configurations)
        chart, version, namespace = helm_handler._release_metadata(
            "kubeflow-trainer", by_name["kubeflow-trainer"]
        )
        assert (chart, namespace) == ("kubeflow-trainer", "kubeflow-trainer")
        assert version == by_name["kubeflow-trainer"]["version"]
        chart, version, namespace = helm_handler._release_metadata("mlflow", by_name["mlflow"])
        assert (chart, namespace) == ("mlflow", "monitoring")
        assert version == by_name["mlflow"]["version"]


class TestDiagnosticAndBudgetHelpers:
    """Bounded diagnostics and the invocation-wide validation time budget."""

    def test_empty_diagnostic_is_labelled_rather_than_blank(self):
        assert helm_handler._bounded_diagnostic("") == "<empty>"
        assert helm_handler._bounded_diagnostic("   \n") == "<empty>"

    def test_short_diagnostic_is_returned_verbatim_after_strip(self):
        assert helm_handler._bounded_diagnostic("  boom \n") == "boom"

    def test_long_diagnostic_is_truncated_with_dropped_count(self):
        text = "x" * (helm_handler.MAX_VALIDATION_DIAGNOSTIC_CHARS + 25)
        result = helm_handler._bounded_diagnostic(text)
        assert result.startswith("x" * helm_handler.MAX_VALIDATION_DIAGNOSTIC_CHARS)
        assert result.endswith("... [truncated 25 chars]")
        assert helm_handler._bounded_diagnostic("abcdef", limit=4) == "abcd... [truncated 2 chars]"

    def test_exhausted_budget_raises_validation_timeout(self):
        with pytest.raises(helm_handler._ValidationTimeout, match="exhausted"):
            helm_handler._validation_command_timeout(time.monotonic() - 1, 120)

    def test_remaining_budget_caps_the_command_timeout(self):
        assert helm_handler._validation_command_timeout(time.monotonic() + 5000, 120) == 120
        # Under a second-and-a-bit of budget still yields at least one second.
        assert helm_handler._validation_command_timeout(time.monotonic() + 1.5, 120) == 1


class TestRecordAddonStatus:
    """Per-chart outcomes are published to SSM best-effort, never fatally."""

    def test_writes_json_status_parameter_under_project_and_region(self, monkeypatch):
        monkeypatch.setenv("PROJECT_NAME", "gco")
        monkeypatch.setenv("REGION", "us-east-1")
        ssm = MagicMock()
        with patch.object(helm_handler.boto3, "client", return_value=ssm) as mock_client:
            helm_handler._record_addon_status("keda", "installed", "Successfully installed keda")

        mock_client.assert_called_once_with("ssm")
        ssm.put_parameter.assert_called_once()
        kwargs = ssm.put_parameter.call_args.kwargs
        assert kwargs["Name"] == "/gco/addons/us-east-1/keda"
        assert kwargs["Type"] == "String"
        assert kwargs["Overwrite"] is True
        payload = json.loads(kwargs["Value"])
        assert payload["chart"] == "keda"
        assert payload["status"] == "installed"
        assert payload["message"] == "Successfully installed keda"
        assert isinstance(payload["updated_at"], int)

    def test_message_is_truncated_to_parameter_friendly_size(self, monkeypatch):
        monkeypatch.setenv("PROJECT_NAME", "gco")
        monkeypatch.setenv("REGION", "us-east-1")
        ssm = MagicMock()
        with patch.object(helm_handler.boto3, "client", return_value=ssm):
            helm_handler._record_addon_status("keda", "failed", "e" * 3000)

        payload = json.loads(ssm.put_parameter.call_args.kwargs["Value"])
        assert payload["message"] == "e" * 1024

    def test_ssm_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("PROJECT_NAME", "gco")
        monkeypatch.setenv("REGION", "us-east-1")
        ssm = MagicMock()
        ssm.put_parameter.side_effect = Exception("ThrottlingException")
        with patch.object(helm_handler.boto3, "client", return_value=ssm):
            helm_handler._record_addon_status("keda", "installed", "ok")

    def test_skips_ssm_when_project_or_region_is_unset(self, monkeypatch):
        monkeypatch.setenv("PROJECT_NAME", "gco")
        monkeypatch.delenv("REGION", raising=False)
        with patch.object(helm_handler.boto3, "client") as mock_client:
            helm_handler._record_addon_status("keda", "installed", "ok")
        mock_client.assert_not_called()


class TestLoadChartsConfig:
    """charts.yaml loading degrades to an empty chart set instead of crashing."""

    def test_missing_charts_file_yields_empty_chart_set(self, tmp_path):
        with patch.object(helm_handler, "CHARTS_CONFIG_PATH", tmp_path / "absent.yaml"):
            assert helm_handler.load_charts_config() == {"charts": {}}

    def test_non_mapping_document_yields_empty_chart_set(self, tmp_path):
        charts_file = tmp_path / "charts.yaml"
        charts_file.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        with patch.object(helm_handler, "CHARTS_CONFIG_PATH", charts_file):
            assert helm_handler.load_charts_config() == {"charts": {}}

    def test_mapping_document_is_returned_as_is(self, tmp_path):
        charts_file = tmp_path / "charts.yaml"
        charts_file.write_text("charts:\n  keda:\n    namespace: keda\n", encoding="utf-8")
        with patch.object(helm_handler, "CHARTS_CONFIG_PATH", charts_file):
            assert helm_handler.load_charts_config() == {"charts": {"keda": {"namespace": "keda"}}}

    def test_real_charts_file_loads_as_mapping(self):
        assert isinstance(helm_handler.load_charts_config().get("charts"), dict)


class TestSendResponse:
    """The CloudFormation callback is a bounded PUT that never raises."""

    _EVENT = {
        "ResponseURL": "https://cloudformation-custom-resource-response.example.test/callback",
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/gco/0000",
        "RequestId": "request-1",
        "LogicalResourceId": "HelmCharts",
    }

    def test_puts_json_body_with_default_reason(self):
        context = MagicMock()
        context.log_stream_name = "2026/09/01/[$LATEST]abc"
        pool = MagicMock()
        with patch.object(helm_handler.urllib3, "PoolManager", return_value=pool):
            helm_handler.send_response(
                dict(self._EVENT),
                context,
                helm_handler.SUCCESS,
                {"Results": "{}"},
                "helm-HelmCharts",
            )

        pool.request.assert_called_once()
        request = pool.request.call_args
        assert request.args == ("PUT", self._EVENT["ResponseURL"])
        assert request.kwargs["headers"] == {"Content-Type": "application/json"}
        assert request.kwargs["timeout"] == 10.0
        body = json.loads(request.kwargs["body"].decode("utf-8"))
        assert body == {
            "Status": "SUCCESS",
            "Reason": "See CloudWatch Log Stream: 2026/09/01/[$LATEST]abc",
            "PhysicalResourceId": "helm-HelmCharts",
            "StackId": self._EVENT["StackId"],
            "RequestId": "request-1",
            "LogicalResourceId": "HelmCharts",
            "Data": {"Results": "{}"},
        }

    def test_explicit_reason_overrides_log_stream_hint(self):
        pool = MagicMock()
        with patch.object(helm_handler.urllib3, "PoolManager", return_value=pool):
            helm_handler.send_response(
                dict(self._EVENT),
                MagicMock(),
                helm_handler.FAILED,
                {},
                "helm-HelmCharts",
                "Failed charts: keda",
            )

        body = json.loads(pool.request.call_args.kwargs["body"].decode("utf-8"))
        assert body["Status"] == "FAILED"
        assert body["Reason"] == "Failed charts: keda"

    def test_callback_transport_failure_is_logged_not_raised(self, caplog):
        pool = MagicMock()
        pool.request.side_effect = helm_handler.urllib3.exceptions.MaxRetryError(
            pool, self._EVENT["ResponseURL"], reason="connection refused"
        )
        with (
            patch.object(helm_handler.urllib3, "PoolManager", return_value=pool),
            caplog.at_level(logging.ERROR),
        ):
            helm_handler.send_response(
                dict(self._EVENT), MagicMock(), helm_handler.SUCCESS, {}, "helm-HelmCharts"
            )

        assert "Failed to send response" in caplog.text


class TestEksAuthentication:
    """EKS bearer tokens and kubeconfig files are produced offline from STS signing."""

    def test_token_is_presigned_sts_url_in_k8s_aws_v1_format(self):
        presigned = (
            "https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity"
            "&Version=2011-06-15&X-Amz-Signature=deadbeef"
        )
        session = MagicMock()
        session.client.return_value.meta.service_model.service_id = "STS"
        signer = MagicMock()
        signer.generate_presigned_url.return_value = presigned
        with (
            patch.object(helm_handler.boto3, "Session", return_value=session),
            patch("botocore.signers.RequestSigner", return_value=signer) as signer_cls,
        ):
            token = helm_handler.get_eks_token("gco-us-east-1", "us-east-1")

        expected_suffix = base64.urlsafe_b64encode(presigned.encode()).decode().rstrip("=")
        assert token == f"k8s-aws-v1.{expected_suffix}"
        assert "=" not in token
        session.client.assert_called_once_with("sts", region_name="us-east-1")
        signer_cls.assert_called_once_with(
            "STS",
            "us-east-1",
            "sts",
            "v4",
            session.get_credentials.return_value,
            session.events,
        )
        presign = signer.generate_presigned_url.call_args
        params = presign.args[0]
        assert params["method"] == "GET"
        assert params["url"].startswith(
            "https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity"
        )
        assert params["headers"] == {"x-k8s-aws-id": "gco-us-east-1"}
        assert presign.kwargs == {
            "region_name": "us-east-1",
            "expires_in": 60,
            "operation_name": "",
        }

    def test_kubeconfig_embeds_cluster_endpoint_ca_and_token_in_private_file(self):
        eks = MagicMock()
        eks.describe_cluster.return_value = {
            "cluster": {
                "endpoint": "https://ABCDEF0123456789.gr7.us-east-1.eks.amazonaws.com",
                "certificateAuthority": {"data": "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg=="},
            }
        }
        with (
            patch.object(helm_handler.boto3, "client", return_value=eks) as mock_client,
            patch.object(helm_handler, "get_eks_token", return_value="k8s-aws-v1.dG9rZW4"),
        ):
            path = helm_handler.configure_kubeconfig("gco-us-east-1", "us-east-1")

        try:
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            kubeconfig = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        finally:
            os.remove(path)

        mock_client.assert_called_once_with("eks", region_name="us-east-1")
        eks.describe_cluster.assert_called_once_with(name="gco-us-east-1")
        assert kubeconfig["apiVersion"] == "v1"
        assert kubeconfig["kind"] == "Config"
        assert kubeconfig["current-context"] == "gco-us-east-1"
        assert kubeconfig["clusters"] == [
            {
                "name": "gco-us-east-1",
                "cluster": {
                    "server": "https://ABCDEF0123456789.gr7.us-east-1.eks.amazonaws.com",
                    "certificate-authority-data": "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg==",
                },
            }
        ]
        assert kubeconfig["contexts"] == [
            {
                "name": "gco-us-east-1",
                "context": {"cluster": "gco-us-east-1", "user": "gco-us-east-1"},
            }
        ]
        assert kubeconfig["users"] == [
            {"name": "gco-us-east-1", "user": {"token": "k8s-aws-v1.dG9rZW4"}}
        ]

    def test_kubeconfig_write_failure_removes_partial_credential_file(self):
        eks = MagicMock()
        eks.describe_cluster.return_value = {
            "cluster": {
                "endpoint": "https://example.test",
                "certificateAuthority": {"data": "Q0E="},
            }
        }
        created: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            created.append(path)
            return fd, path

        with (
            patch.object(helm_handler.boto3, "client", return_value=eks),
            patch.object(helm_handler, "get_eks_token", return_value="k8s-aws-v1.dG9rZW4"),
            patch.object(helm_handler.tempfile, "mkstemp", side_effect=recording_mkstemp),
            patch.object(helm_handler.yaml, "dump", side_effect=OSError("No space left on device")),
            pytest.raises(OSError, match="No space left"),
        ):
            helm_handler.configure_kubeconfig("gco-us-east-1", "us-east-1")

        assert len(created) == 1
        assert not os.path.exists(created[0])


class TestRunHelmEnvironment:
    """``run_helm`` layers caller-provided variables over the Lambda helm homes."""

    def test_extra_env_is_merged_into_the_subprocess_environment(self):
        with patch.object(
            helm_handler.subprocess, "run", return_value=_completed(0, stdout="v3")
        ) as mock_run:
            code, stdout, _ = helm_handler.run_helm(
                ["version"], "/tmp/kube", env={"HELM_REGISTRY_CONFIG": "/tmp/registry.json"}
            )

        assert (code, stdout) == (0, "v3")
        env = mock_run.call_args.kwargs["env"]
        assert env["HELM_REGISTRY_CONFIG"] == "/tmp/registry.json"
        assert env["KUBECONFIG"] == "/tmp/kube"
        assert env["HELM_CACHE_HOME"] == "/tmp/.helm/cache"
        assert mock_run.call_args.args[0] == ["helm", "version"]

    def test_explicit_command_timeout_overrides_environment_default(self, monkeypatch):
        monkeypatch.setenv("HELM_CMD_TIMEOUT_SECONDS", "42")
        with patch.object(helm_handler.subprocess, "run", return_value=_completed(0)) as mock_run:
            helm_handler.run_helm(["version"], "/tmp/kube")
            helm_handler.run_helm(["version"], "/tmp/kube", command_timeout_seconds=7)

        assert mock_run.call_args_list[0].kwargs["timeout"] == 42
        assert mock_run.call_args_list[1].kwargs["timeout"] == 7


class TestClearStuckReleaseSecretDeletion:
    """Secret deletion is per-secret best-effort and reports whether anything cleared."""

    _STUCK = json.dumps({"info": {"status": "pending-upgrade"}})

    def test_failed_secret_listing_clears_nothing(self):
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, self._STUCK, "")),
            patch.object(
                helm_handler.subprocess,
                "run",
                return_value=_completed(1, stderr="Error from server (Forbidden)"),
            ) as mock_run,
        ):
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is False
        assert mock_run.call_count == 1

    def test_empty_secret_listing_clears_nothing(self):
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, self._STUCK, "")),
            patch.object(
                helm_handler.subprocess, "run", return_value=_completed(0, stdout="  \n")
            ) as mock_run,
        ):
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is False
        assert mock_run.call_count == 1

    def test_timed_out_and_failed_deletes_do_not_stop_remaining_secrets(self):
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, self._STUCK, "")),
            patch.object(helm_handler.subprocess, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(
                    0,
                    stdout=(
                        "sh.helm.release.v1.foo.v7 sh.helm.release.v1.foo.v8 "
                        "sh.helm.release.v1.foo.v9"
                    ),
                ),
                subprocess.TimeoutExpired(cmd=["kubectl"], timeout=15),
                _completed(1, stderr="Error from server (Conflict)"),
                _completed(0, stdout='secret "sh.helm.release.v1.foo.v9" deleted'),
            ]
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is True

        deleted = [call.args[0][5] for call in mock_run.call_args_list[1:]]
        assert deleted == [
            "sh.helm.release.v1.foo.v7",
            "sh.helm.release.v1.foo.v8",
            "sh.helm.release.v1.foo.v9",
        ]

    def test_all_deletes_failing_reports_nothing_cleared(self):
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, self._STUCK, "")),
            patch.object(helm_handler.subprocess, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0, stdout="sh.helm.release.v1.foo.v7"),
                _completed(1, stderr="Error from server (Conflict)"),
            ]
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is False

    def test_non_object_status_payload_is_treated_as_not_stuck(self):
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, "[1, 2]", "")),
            patch.object(helm_handler.subprocess, "run") as mock_run,
        ):
            assert helm_handler._clear_stuck_release("foo", "ns", "/tmp/kube") is False
        mock_run.assert_not_called()


class TestAddHelmRepo:
    """Repo registration needs both ``repo add --force-update`` and ``repo update``."""

    def test_add_failure_short_circuits_before_update(self):
        with patch.object(helm_handler, "run_helm", return_value=(1, "", "bad url")) as mock_run:
            assert (
                helm_handler.add_helm_repo("volcano-sh", "https://charts.test", "/tmp/kc") is False
            )
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == [
            "repo",
            "add",
            "volcano-sh",
            "https://charts.test",
            "--force-update",
        ]

    def test_update_failure_is_reported(self):
        with patch.object(helm_handler, "run_helm") as mock_run:
            mock_run.side_effect = [(0, "", ""), (1, "", "index fetch failed")]
            assert (
                helm_handler.add_helm_repo("volcano-sh", "https://charts.test", "/tmp/kc") is False
            )
        assert mock_run.call_args_list[1].args[0] == ["repo", "update", "volcano-sh"]

    def test_add_and_update_success(self):
        with patch.object(helm_handler, "run_helm", return_value=(0, "", "")) as mock_run:
            assert (
                helm_handler.add_helm_repo("volcano-sh", "https://charts.test", "/tmp/kc") is True
            )
        assert mock_run.call_count == 2


class TestInstallChartConfiguration:
    """Chart config shapes the ``helm upgrade --install`` argv and values file."""

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

    def test_value_overrides_are_deep_merged_into_the_values_file(self):
        config = self._config(values={"controller": {"replicas": 1, "image": "a"}, "keep": True})
        observed = {}

        def run_helm(args, _kubeconfig):
            with open(args[args.index("--values") + 1], encoding="utf-8") as values_file:
                observed["values"] = yaml.safe_load(values_file)
            return 0, "ok", ""

        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm", side_effect=run_helm),
        ):
            ok, _ = helm_handler.install_chart(
                "volcano", config, "/tmp/kube", {"controller": {"replicas": 3}}
            )

        assert ok is True
        assert observed["values"] == {
            "controller": {"replicas": 3, "image": "a"},
            "keep": True,
        }
        # The caller's config mapping is left untouched by the merge.
        assert config["values"]["controller"]["replicas"] == 1

    def test_repo_registration_failure_aborts_before_helm_upgrade(self):
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=False),
            patch.object(helm_handler, "_clear_stuck_release") as mock_clear,
            patch.object(helm_handler, "run_helm") as mock_run,
        ):
            ok, message = helm_handler.install_chart("volcano", self._config(), "/tmp/kube")

        assert ok is False
        assert message == "Failed to add repo volcano-sh"
        mock_clear.assert_not_called()
        mock_run.assert_not_called()

    def test_oci_chart_skips_repo_registration_and_uses_full_reference(self):
        config = self._config(
            use_oci=True,
            repo_url="oci://public.ecr.aws/aws-controllers-k8s",
            chart="s3-chart",
            version=None,
            create_namespace=False,
        )
        with (
            patch.object(helm_handler, "add_helm_repo") as mock_add,
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler, "run_helm", return_value=(0, "ok", "")) as mock_run,
        ):
            ok, _ = helm_handler.install_chart("ack-s3", config, "/tmp/kube")

        assert ok is True
        mock_add.assert_not_called()
        args = mock_run.call_args.args[0]
        assert args[:4] == [
            "upgrade",
            "--install",
            "ack-s3",
            "oci://public.ecr.aws/aws-controllers-k8s/s3-chart",
        ]
        assert "--version" not in args
        assert "--create-namespace" not in args
        assert "--values" not in args

    def test_values_file_open_failure_closes_descriptor_and_removes_file(self):
        config = self._config(values={"apiToken": "sensitive-test-value"})
        created: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            created.append(path)
            return fd, path

        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release"),
            patch.object(helm_handler.tempfile, "mkstemp", side_effect=recording_mkstemp),
            patch.object(helm_handler.os, "fdopen", side_effect=OSError("EMFILE")),
            patch.object(helm_handler, "run_helm") as mock_run,
            pytest.raises(OSError, match="EMFILE"),
        ):
            helm_handler.install_chart("volcano", config, "/tmp/kube")

        mock_run.assert_not_called()
        assert len(created) == 1
        assert not os.path.exists(created[0])

    def test_second_failure_after_clearing_stuck_state_is_reported(self):
        stuck_err = "another operation (install/upgrade/rollback) is in progress"
        with (
            patch.object(helm_handler, "add_helm_repo", return_value=True),
            patch.object(helm_handler, "_clear_stuck_release") as mock_clear,
            patch.object(helm_handler, "run_helm") as mock_run,
        ):
            mock_run.side_effect = [
                (1, "", stuck_err),
                (1, "", "Error: UPGRADE FAILED: context deadline exceeded"),
            ]
            ok, message = helm_handler.install_chart("volcano", self._config(), "/tmp/kube")

        assert ok is False
        assert (
            message == "Failed to install volcano: Error: UPGRADE FAILED: context deadline exceeded"
        )
        assert mock_clear.call_count == 2


class TestFinalizerStripTimeouts:
    """Finalizer removal reports bounded-timeout failures and skips unparsable lines."""

    def test_listing_timeout_is_reported(self):
        with patch.object(
            helm_handler.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30),
        ):
            error = helm_handler._strip_custom_resource_finalizers(
                "/tmp/kc", ["clusterqueues.kueue.x-k8s.io"], namespaced=False
            )
        assert error == (
            "Timed out listing clusterqueues.kueue.x-k8s.io instances for finalizer removal"
        )

    def test_patch_timeout_is_reported_with_the_object(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="clusterqueue.kueue.x-k8s.io/gco-cluster-queue\n"),
                subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30),
            ]
            error = helm_handler._strip_custom_resource_finalizers(
                "/tmp/kc", ["clusterqueues.kueue.x-k8s.io"], namespaced=False
            )
        assert error == (
            "Timed out removing finalizers from clusterqueue.kueue.x-k8s.io/gco-cluster-queue"
        )

    def test_namespaced_lines_without_a_name_are_skipped(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="gco-jobs,\nbare-line\ngco-jobs,default-queue\n"),
                _completed(0),
            ]
            error = helm_handler._strip_custom_resource_finalizers(
                "/tmp/kc", ["localqueues.kueue.x-k8s.io"], namespaced=True
            )
        assert error is None
        assert mock_run.call_count == 2
        patch_command = mock_run.call_args_list[1].args[0]
        assert "default-queue" in patch_command

    def test_already_gone_object_during_patch_is_not_an_error(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="clusterqueue.kueue.x-k8s.io/gco-cluster-queue\n"),
                _completed(1, stderr='Error from server (NotFound): "gco-cluster-queue" not found'),
            ]
            error = helm_handler._strip_custom_resource_finalizers(
                "/tmp/kc", ["clusterqueues.kueue.x-k8s.io"], namespaced=False
            )
        assert error is None


class TestCustomResourceDeleteTimeouts:
    """Every bounded kubectl step in the pre-uninstall purge fails loudly on timeout."""

    def test_discovery_timeout_blocks_cleanup(self):
        with patch.object(
            helm_handler.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["kubectl"], timeout=10),
        ):
            success, message = helm_handler._delete_chart_custom_resources("kueue", "/tmp/kc")
        assert success is False
        assert message == "Timed out discovering kueue.x-k8s.io custom resources"

    def test_delete_wait_timeout_strips_finalizers_and_retries(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout=""),
                _completed(0, stdout="clusterqueues.kueue.x-k8s.io\n"),
                subprocess.TimeoutExpired(cmd=["kubectl"], timeout=55),
                _completed(0, stdout="clusterqueue.kueue.x-k8s.io/gco-cluster-queue\n"),
                _completed(0),
                _completed(0),
            ]
            success, message = helm_handler._delete_chart_custom_resources("kueue", "/tmp/kc")
        assert success is True
        assert "1 kueue custom resource type" in message
        assert mock_run.call_count == 6

    def test_retry_timeout_after_finalizer_removal_fails_teardown(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout=""),
                _completed(0, stdout="clusterqueues.kueue.x-k8s.io\n"),
                _completed(1, stderr="timed out waiting for the condition"),
                _completed(0, stdout="clusterqueue.kueue.x-k8s.io/gco-cluster-queue\n"),
                _completed(0),
                subprocess.TimeoutExpired(cmd=["kubectl"], timeout=55),
            ]
            success, message = helm_handler._delete_chart_custom_resources("kueue", "/tmp/kc")
        assert success is False
        assert message == (
            "Timed out deleting cluster-scoped kueue custom resources even after finalizer removal"
        )

    def test_retry_failure_after_finalizer_removal_fails_teardown(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="localqueues.kueue.x-k8s.io\n"),
                _completed(0, stdout=""),
                _completed(1, stderr="timed out waiting for the condition"),
                _completed(0, stdout="gco-jobs,default-queue\n"),
                _completed(0),
                _completed(1, stderr="Error from server (Forbidden): cannot delete"),
            ]
            success, message = helm_handler._delete_chart_custom_resources("kueue", "/tmp/kc")
        assert success is False
        assert message == (
            "Failed to delete namespaced kueue custom resources even after finalizer removal: "
            "Error from server (Forbidden): cannot delete"
        )


class TestHealthMonitorQuiesceTimeouts:
    """Scale and wait steps surface timeouts and non-absence wait failures."""

    def test_scale_timeout(self):
        with patch.object(
            helm_handler.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30),
        ):
            success, message = helm_handler.quiesce_health_monitor("/tmp/kc")
        assert (success, message) == (False, "Timed out scaling health-monitor deployment to zero")

    def test_wait_timeout(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),
                subprocess.TimeoutExpired(cmd=["kubectl"], timeout=135),
            ]
            success, message = helm_handler.quiesce_health_monitor("/tmp/kc")
        assert (success, message) == (
            False,
            "Timed out waiting for health-monitor pods to terminate",
        )

    def test_wait_failure_other_than_absence_is_fatal(self):
        with patch.object(helm_handler.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),
                _completed(1, stderr="error: timed out waiting for the condition on pods/hm-1"),
            ]
            success, message = helm_handler.quiesce_health_monitor("/tmp/kc")
        assert success is False
        assert message == (
            "Failed waiting for health-monitor pods: "
            "error: timed out waiting for the condition on pods/hm-1"
        )


class TestRunKubectlFailures:
    """``run_kubectl`` mirrors ``run_helm``'s timeout contract and stderr logging."""

    def test_timeout_maps_to_typed_failure_tuple(self):
        with patch.object(
            helm_handler.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["kubectl"], timeout=120),
        ):
            code, stdout, stderr = helm_handler.run_kubectl(["get", "pods"], "/tmp/kc")
        assert (code, stdout) == (-1, "")
        assert stderr == "timeout: kubectl command exceeded 120s"

    def test_stderr_is_logged_when_stdout_is_empty(self, caplog):
        with (
            patch.object(
                helm_handler.subprocess,
                "run",
                return_value=_completed(1, stdout="", stderr="Error from server (Forbidden)"),
            ),
            caplog.at_level(logging.INFO),
        ):
            code, stdout, stderr = helm_handler.run_kubectl(["get", "pods"], "/tmp/kc")

        assert (code, stdout, stderr) == (1, "", "Error from server (Forbidden)")
        assert "stderr: Error from server (Forbidden)" in caplog.text
        assert "stdout:" not in caplog.text

    def test_log_output_false_suppresses_both_streams(self, caplog):
        with (
            patch.object(
                helm_handler.subprocess,
                "run",
                return_value=_completed(0, stdout="{}", stderr="warning: deprecated"),
            ),
            caplog.at_level(logging.INFO),
        ):
            helm_handler.run_kubectl(["get", "pods"], "/tmp/kc", log_output=False)

        assert "stdout:" not in caplog.text
        assert "stderr:" not in caplog.text


class TestReleaseConfigurationParsing:
    """``_release_configurations`` rejects malformed payloads with precise errors."""

    _DEFAULTS = {
        "charts": {
            "keda": {"chart": "keda", "version": "2.17.1", "namespace": "keda"},
            "kueue": {"chart": "kueue", "version": "0.14.0", "namespace": "kueue-system"},
        }
    }

    def test_null_charts_and_enabled_charts_default_to_empty(self):
        with patch.object(helm_handler, "load_charts_config", return_value=self._DEFAULTS):
            configurations, enabled = helm_handler._release_configurations(
                {"Charts": None, "EnabledCharts": None}
            )
        assert [release for release, _ in configurations] == ["keda", "kueue"]
        assert enabled == set()

    def test_runtime_only_release_appends_after_charts_yaml_order(self):
        with patch.object(helm_handler, "load_charts_config", return_value=self._DEFAULTS):
            configurations, enabled = helm_handler._release_configurations(
                {
                    "Charts": {"extra": {"chart": "extra", "version": "1.0.0"}},
                    "EnabledCharts": ["extra"],
                }
            )
        assert [release for release, _ in configurations] == ["keda", "kueue", "extra"]
        assert dict(configurations)["extra"] == {"chart": "extra", "version": "1.0.0"}
        assert enabled == {"extra"}

    @pytest.mark.parametrize(
        ("charts_config", "event", "message"),
        [
            ({"charts": ["keda"]}, {}, "charts.yaml field 'charts' must be a mapping"),
            (_DEFAULTS, {"Charts": "keda"}, "Charts must be a mapping"),
            (_DEFAULTS, {"EnabledCharts": "keda"}, "EnabledCharts must be a list"),
            (_DEFAULTS, {"EnabledCharts": [""]}, "EnabledCharts must be a list"),
            (_DEFAULTS, {"EnabledCharts": [42]}, "EnabledCharts must be a list"),
            (
                {"charts": {"keda": "not-a-mapping"}},
                {},
                "charts.yaml contains an invalid release configuration",
            ),
            (
                {"charts": {"": {"chart": "keda"}}},
                {},
                "charts.yaml contains an invalid release configuration",
            ),
            (_DEFAULTS, {"Charts": {"keda": "bad"}}, "Charts contains an invalid release override"),
            (_DEFAULTS, {"Charts": {"": {}}}, "Charts contains an invalid release override"),
        ],
    )
    def test_malformed_inputs_are_rejected(self, charts_config, event, message):
        with (
            patch.object(helm_handler, "load_charts_config", return_value=charts_config),
            pytest.raises(RuntimeError, match=message),
        ):
            helm_handler._release_configurations(event)

    def test_unknown_enabled_releases_are_named_up_to_five(self):
        unknown = ["u1", "u2", "u3", "u4", "u5", "u6"]
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._DEFAULTS),
            pytest.raises(
                RuntimeError, match="EnabledCharts has no chart configuration for"
            ) as exc,
        ):
            helm_handler._release_configurations({"EnabledCharts": ["keda", *unknown]})
        text = str(exc.value)
        assert "u1, u2, u3, u4, u5" in text
        assert "u6" not in text


class TestReleaseMetadata:
    """Every validated release needs an exact chart, version, and namespace."""

    def test_numeric_version_is_stringified(self):
        assert helm_handler._release_metadata(
            "demo", {"chart": "demo", "version": 2, "namespace": "demo"}
        ) == ("demo", "2", "demo")

    def test_namespace_defaults_to_default(self):
        assert helm_handler._release_metadata("demo", {"chart": "demo", "version": "1.0"}) == (
            "demo",
            "1.0",
            "default",
        )

    @pytest.mark.parametrize(
        ("config", "message"),
        [
            ({"version": "1.0"}, "has no valid chart name"),
            ({"chart": "", "version": "1.0"}, "has no valid chart name"),
            ({"chart": "demo"}, "has no configured chart version"),
            ({"chart": "demo", "version": ""}, "has no configured chart version"),
            ({"chart": "demo", "version": "1.0", "namespace": ""}, "has no valid namespace"),
            ({"chart": "demo", "version": "1.0", "namespace": 7}, "has no valid namespace"),
        ],
    )
    def test_missing_or_invalid_fields_are_rejected(self, config, message):
        with pytest.raises(RuntimeError, match=f"release 'demo' {message}"):
            helm_handler._release_metadata("demo", config)


class TestPayloadParsingHelpers:
    """JSON/YAML payload helpers fail with bounded, descriptive errors."""

    @pytest.mark.parametrize(
        ("output", "message"),
        [
            ("not json", "helm status returned invalid JSON: Expecting value"),
            (None, "helm status returned invalid JSON: the JSON object must be str"),
            ("[1, 2]", "helm status returned list, expected object"),
        ],
    )
    def test_parse_json_object_rejects_invalid_or_non_object_payloads(self, output, message):
        with pytest.raises(RuntimeError, match=message):
            helm_handler._parse_json_object(output, "helm status")

    def test_parse_json_object_returns_mapping(self):
        assert helm_handler._parse_json_object('{"a": 1}', "x") == {"a": 1}

    def test_flatten_skips_null_documents_and_expands_nested_lists(self):
        config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}}
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "b"}}
        documents = [
            None,
            {"kind": "List", "items": [config_map, {"kind": "List", "items": [secret]}]},
        ]
        assert helm_handler._flatten_resources(documents, "manifest") == [config_map, secret]

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            (["a string document"], "manifest contains a non-object document"),
            (
                {"kind": "List", "items": "nope"},
                "manifest contains kind List without an items list",
            ),
        ],
    )
    def test_flatten_rejects_non_object_documents_and_malformed_lists(self, value, message):
        with pytest.raises(RuntimeError, match=message):
            helm_handler._flatten_resources(value, "manifest")

    @pytest.mark.parametrize(
        "resource",
        [
            {"kind": "ConfigMap", "metadata": {"name": "a"}},
            {"apiVersion": "v1", "metadata": {"name": "a"}},
            {"apiVersion": "v1", "kind": "", "metadata": {"name": "a"}},
            {"apiVersion": "v1", "kind": "ConfigMap"},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": "not-a-mapping"},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": ""}},
        ],
        ids=["no-apiVersion", "no-kind", "empty-kind", "no-metadata", "bad-metadata", "empty-name"],
    )
    def test_core_identity_requires_api_version_kind_and_name(self, resource):
        with pytest.raises(RuntimeError, match="object without apiVersion/kind/name"):
            helm_handler._resource_core_identity(resource, "manifest")


class TestIdentityComparisonDetails:
    """Identity comparison names surplus objects and honours release-namespace defaults."""

    def test_surplus_live_object_is_reported_as_unexpected_only(self):
        expected = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}}
        surplus = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "leaked"}}
        with pytest.raises(
            RuntimeError, match="rendered 1 resources but kubectl returned 2"
        ) as exc:
            helm_handler._compare_resource_identities([expected], [expected, surplus], "r", "ns")
        text = str(exc.value)
        assert "unexpected=v1/Secret leaked" in text
        assert "missing=" not in text

    def test_namespace_less_manifest_object_matches_release_namespace_return(self):
        expected = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}}
        live = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "a", "namespace": "demo-system"},
        }
        helm_handler._compare_resource_identities([expected], [live], "r", "demo-system")


class TestReadinessEdgeCases:
    """Readiness gates tolerate odd status shapes and report each failure precisely."""

    def test_condition_lookup_returns_none_for_non_list_or_missing_conditions(self):
        assert helm_handler._condition_status({"status": {"conditions": "bad"}}, "Ready") is None
        assert (
            helm_handler._condition_status(
                {"status": {"conditions": [{"type": "Progressing", "status": "True"}]}},
                "Available",
            )
            is None
        )
        assert helm_handler._condition_status({"status": None}, "Ready") is None

    def test_non_mapping_status_is_ignored_for_generic_kinds(self):
        helm_handler._validate_resource_readiness(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}, "status": "odd"}
        )

    def test_non_list_conditions_are_ignored_for_generic_kinds(self):
        helm_handler._validate_resource_readiness(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "a"},
                "status": {"conditions": {"type": "Ready", "status": "False"}},
            }
        )

    def _deployment(self, **status):
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "ctl", "namespace": "ns", "generation": 1},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
                **status,
            },
        }

    def test_converged_deployment_without_available_condition_fails(self):
        with pytest.raises(
            RuntimeError, match="apps/v1/Deployment ns/ctl does not report Available=True"
        ):
            helm_handler._validate_resource_readiness(self._deployment())

    def test_non_integer_desired_replicas_is_invalid(self):
        deployment = self._deployment(conditions=[{"type": "Available", "status": "True"}])
        deployment["spec"]["replicas"] = "1"
        with pytest.raises(RuntimeError, match="invalid desired/status replica data"):
            helm_handler._validate_resource_readiness(deployment)

    def test_daemonset_with_non_integer_desired_counter_is_invalid(self):
        daemonset = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "agent", "namespace": "ns", "generation": 1},
            "status": {"observedGeneration": 1, "desiredNumberScheduled": "2"},
        }
        with pytest.raises(RuntimeError, match="invalid desiredNumberScheduled"):
            helm_handler._validate_resource_readiness(daemonset)


class TestServiceEndpointQuery:
    """EndpointSlice discovery skips malformed entries and surfaces query failures."""

    def _query(self, code, stdout="", stderr=""):
        return patch.object(helm_handler, "run_kubectl", return_value=(code, stdout, stderr))

    def _call(self):
        return helm_handler._service_has_ready_endpoint(
            "demo-webhook",
            "demo-system",
            "v1/Service demo-system/demo-webhook",
            "/tmp/kc",
            time.monotonic() + 60,
        )

    def test_timed_out_query_is_systemic(self):
        with (
            self._query(-1, stderr="timeout: kubectl command exceeded 120s"),
            pytest.raises(helm_handler._ValidationTimeout, match="EndpointSlice query timed out"),
        ):
            self._call()

    def test_failed_query_names_the_service(self):
        with (
            self._query(1, stderr="Error from server (Forbidden): endpointslices is forbidden"),
            pytest.raises(
                RuntimeError,
                match=r"demo-system/demo-webhook EndpointSlice query failed: Error from server",
            ),
        ):
            self._call()

    @staticmethod
    def _malformed_slices():
        return [
            "not-a-slice",
            {
                "metadata": {"deletionTimestamp": "2026-09-01T00:00:00Z"},
                "endpoints": [{"conditions": {"ready": True}}],
            },
            {"endpoints": "not-a-list"},
            {"endpoints": ["not-an-endpoint", {"conditions": "not-a-mapping"}]},
        ]

    def test_malformed_and_terminating_slices_are_skipped(self):
        payload = {"kind": "EndpointSliceList", "items": self._malformed_slices()}
        with self._query(0, stdout=json.dumps(payload)):
            assert self._call() is False

    def test_ready_endpoint_after_malformed_entries_is_found(self):
        ready_slice = {"endpoints": [{"conditions": {"ready": "True", "terminating": False}}]}
        payload = {"kind": "EndpointSliceList", "items": [*self._malformed_slices(), ready_slice]}
        with self._query(0, stdout=json.dumps(payload)):
            assert self._call() is True

    def test_single_endpoint_slice_object_is_accepted_without_items(self):
        payload = {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSlice",
            "metadata": {"name": "demo-webhook-abc"},
            "endpoints": [{"conditions": {"ready": True}}],
        }
        with self._query(0, stdout=json.dumps(payload)):
            assert self._call() is True

    def test_non_service_or_selectorless_resources_skip_endpoint_checks(self):
        with patch.object(helm_handler, "run_kubectl") as mock_kubectl:
            helm_handler._validate_service_endpoints(
                {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}},
                "/tmp/kc",
                "ns",
                time.monotonic() + 60,
            )
            helm_handler._validate_service_endpoints(
                {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "a"}, "spec": {}},
                "/tmp/kc",
                "ns",
                time.monotonic() + 60,
            )
        mock_kubectl.assert_not_called()


class TestValidationFileCleanup:
    """Validation material removal tolerates only an already-absent path."""

    def test_missing_file_is_ignored(self, tmp_path):
        helm_handler._remove_validation_file(str(tmp_path / "already-gone.yaml"))

    def test_existing_file_is_removed(self, tmp_path):
        target = tmp_path / "material.yaml"
        target.write_text("secret", encoding="utf-8")
        helm_handler._remove_validation_file(str(target))
        assert not target.exists()

    def test_directory_removal_error_propagates(self, tmp_path):
        with pytest.raises(OSError):
            helm_handler._remove_validation_file(str(tmp_path))


class TestGatewayCrdBundleRejections:
    """Pinned bundle verification rejects every deviation from the recorded inventory."""

    @staticmethod
    def _crd(name="widgets.example.test"):
        return {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": name},
        }

    @staticmethod
    def _bundle(body, *, object_count=1, crd_count=1):
        return helm_handler._PinnedManifestBundle(
            name="test-gateway-bundle",
            url="https://example.test/test-gateway-bundle.yaml",
            size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            object_count=object_count,
            crd_count=crd_count,
        )

    @staticmethod
    def _pool(body, status=200):
        response = MagicMock()
        response.status = status
        response.data = body
        pool = MagicMock()
        pool.request.return_value = response
        return pool, response

    def test_non_byte_body_is_rejected_and_connection_released(self):
        body = yaml.safe_dump(self._crd()).encode("utf-8")
        bundle = self._bundle(body)
        pool, response = self._pool("text body, not bytes")
        with (
            patch.object(helm_handler.urllib3, "PoolManager", return_value=pool),
            pytest.raises(RuntimeError, match="returned a non-byte body"),
            helm_handler._verified_gateway_crd_bundle(bundle),
        ):
            pytest.fail("invalid bundle must not be yielded")
        response.release_conn.assert_called_once_with()

    def test_transport_failure_propagates_without_a_connection_to_release(self):
        body = yaml.safe_dump(self._crd()).encode("utf-8")
        bundle = self._bundle(body)
        pool = MagicMock()
        pool.request.side_effect = helm_handler.urllib3.exceptions.MaxRetryError(
            pool, bundle.url, reason="too many redirects"
        )
        with (
            patch.object(helm_handler.urllib3, "PoolManager", return_value=pool),
            pytest.raises(helm_handler.urllib3.exceptions.MaxRetryError),
            helm_handler._verified_gateway_crd_bundle(bundle),
        ):
            pytest.fail("unreachable bundle must not be yielded")

    @pytest.mark.parametrize(
        "body",
        [b"\xff\xfe\xfd not utf-8", b"apiVersion: [unclosed"],
        ids=["not-utf8", "not-yaml"],
    )
    def test_undecodable_body_is_rejected_even_when_hash_matches(self, body):
        bundle = self._bundle(body)
        pool, _ = self._pool(body)
        with (
            patch.object(helm_handler.urllib3, "PoolManager", return_value=pool),
            pytest.raises(RuntimeError, match="is not valid UTF-8 YAML"),
            helm_handler._verified_gateway_crd_bundle(bundle),
        ):
            pytest.fail("invalid bundle must not be yielded")

    @pytest.mark.parametrize(
        ("documents", "object_count", "crd_count", "message"),
        [
            ([_crd.__func__()], 2, 1, r"inventory mismatch: objects=1/2, CRDs=1/1"),
            ([_crd.__func__()], 1, 2, r"inventory mismatch: objects=1/1, CRDs=1/2"),
            ([_crd.__func__(), _crd.__func__()], 2, 2, "contains duplicate object identities"),
        ],
        ids=["object-count", "crd-count", "duplicate"],
    )
    def test_inventory_drift_is_rejected(self, documents, object_count, crd_count, message):
        body = yaml.safe_dump_all(documents).encode("utf-8")
        bundle = self._bundle(body, object_count=object_count, crd_count=crd_count)
        pool, _ = self._pool(body)
        with (
            patch.object(helm_handler.urllib3, "PoolManager", return_value=pool),
            pytest.raises(RuntimeError, match=message),
            helm_handler._verified_gateway_crd_bundle(bundle),
        ):
            pytest.fail("invalid bundle must not be yielded")

    def _patched_bundle(self):
        body = yaml.safe_dump(self._crd()).encode("utf-8")
        bundle = self._bundle(body)

        @helm_handler.contextlib.contextmanager
        def verified(_bundle):
            yield "/tmp/test-gateway-bundle.yaml", [self._crd()]

        return bundle, verified

    def test_apply_failure_names_the_bundle(self):
        bundle, verified = self._patched_bundle()
        with (
            patch.object(helm_handler, "PINNED_GATEWAY_CRD_BUNDLES", (bundle,)),
            patch.object(helm_handler, "_verified_gateway_crd_bundle", side_effect=verified),
            patch.object(
                helm_handler, "run_kubectl", return_value=(1, "", "conflict: field manager")
            ),
            pytest.raises(RuntimeError, match="failed to apply test-gateway-bundle: conflict"),
        ):
            helm_handler._apply_gateway_crds("/tmp/kc")

    def test_live_validation_timeout_is_systemic(self):
        bundle, verified = self._patched_bundle()
        with (
            patch.object(helm_handler, "PINNED_GATEWAY_CRD_BUNDLES", (bundle,)),
            patch.object(helm_handler, "_verified_gateway_crd_bundle", side_effect=verified),
            patch.object(helm_handler, "run_kubectl", return_value=(-1, "", "timeout")),
            pytest.raises(
                helm_handler._ValidationTimeout,
                match="kubectl get timed out for test-gateway-bundle",
            ),
        ):
            helm_handler._validate_gateway_crds("/tmp/kc", time.monotonic() + 60)

    def test_live_validation_retrieval_failure_names_the_bundle(self):
        bundle, verified = self._patched_bundle()
        with (
            patch.object(helm_handler, "PINNED_GATEWAY_CRD_BUNDLES", (bundle,)),
            patch.object(helm_handler, "_verified_gateway_crd_bundle", side_effect=verified),
            patch.object(helm_handler, "run_kubectl", return_value=(1, "", "Forbidden")),
            pytest.raises(
                RuntimeError, match="kubectl could not retrieve test-gateway-bundle: Forbidden"
            ),
        ):
            helm_handler._validate_gateway_crds("/tmp/kc", time.monotonic() + 60)


class TestEnabledReleaseValidationFailures:
    """Each helm/kubectl step of an enabled-release check fails with its own diagnosis."""

    RELEASE = "demo-release"
    CHART = "demo-chart"
    VERSION = "1.2.3"
    NAMESPACE = "demo-system"

    @staticmethod
    def _deployment_manifest():
        return yaml.safe_dump(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "demo-controller", "namespace": "demo-system"},
                "spec": {"replicas": 1},
            }
        )

    def _helm(self, *, list_result=None, manifest_result=None):
        default_list = (
            0,
            json.dumps(
                [
                    {
                        "name": self.RELEASE,
                        "namespace": self.NAMESPACE,
                        "status": "deployed",
                        "chart": f"{self.CHART}-{self.VERSION}",
                    }
                ]
            ),
            "",
        )
        default_manifest = (0, self._deployment_manifest(), "")

        def _run(args, _kubeconfig, **_kwargs):
            if args[0] == "status":
                return 0, json.dumps({"info": {"status": "deployed"}}), ""
            if args[0] == "list":
                return list_result or default_list
            if args[:2] == ["get", "manifest"]:
                return manifest_result or default_manifest
            raise AssertionError(f"unexpected helm invocation: {args}")

        return _run

    def _validate(self):
        return helm_handler._validate_enabled_release(
            self.RELEASE,
            self.CHART,
            self.VERSION,
            self.NAMESPACE,
            "/tmp/kubeconfig",
            time.monotonic() + 60,
        )

    @pytest.mark.parametrize(
        ("list_result", "expected", "message"),
        [
            ((-1, "", "timeout"), helm_handler._ValidationTimeout, "helm list timed out"),
            ((1, "", "storage backend unavailable"), RuntimeError, "helm list failed: storage"),
            ((0, "not json", ""), RuntimeError, "helm list for demo-release returned invalid JSON"),
            ((0, None, ""), RuntimeError, "helm list for demo-release returned invalid JSON"),
            ((0, "[]", ""), RuntimeError, "helm list returned 0 entries, expected exactly one"),
            (
                (0, "[{}, {}]", ""),
                RuntimeError,
                "helm list returned 2 entries, expected exactly one",
            ),
            ((0, '{"name": "x"}', ""), RuntimeError, "returned non-list entries"),
            ((0, "[1]", ""), RuntimeError, "helm list returned 1 entries, expected exactly one"),
        ],
        ids=[
            "timeout",
            "non-zero",
            "invalid-json",
            "none-stdout",
            "empty",
            "two-entries",
            "object",
            "non-object-entry",
        ],
    )
    def test_helm_list_failures(self, list_result, expected, message):
        with (
            patch.object(helm_handler, "run_helm", side_effect=self._helm(list_result=list_result)),
            patch.object(helm_handler, "run_kubectl") as mock_kubectl,
            pytest.raises(expected, match=message),
        ):
            self._validate()
        mock_kubectl.assert_not_called()

    @pytest.mark.parametrize(
        ("manifest_result", "expected", "message"),
        [
            ((-1, "", "timeout"), helm_handler._ValidationTimeout, "helm get manifest timed out"),
            ((1, "", "release: not found"), RuntimeError, "helm get manifest failed: release"),
            ((0, "  \n\n", ""), RuntimeError, "helm get manifest returned empty output"),
            ((0, "apiVersion: [unclosed", ""), RuntimeError, "helm manifest is invalid YAML"),
            ((0, "---\n", ""), RuntimeError, "helm get manifest yielded no Kubernetes objects"),
        ],
        ids=["timeout", "non-zero", "empty", "invalid-yaml", "no-objects"],
    )
    def test_helm_get_manifest_failures(self, manifest_result, expected, message):
        with (
            patch.object(
                helm_handler, "run_helm", side_effect=self._helm(manifest_result=manifest_result)
            ),
            patch.object(helm_handler, "run_kubectl") as mock_kubectl,
            pytest.raises(expected, match=message),
        ):
            self._validate()
        mock_kubectl.assert_not_called()

    def test_kubectl_get_timeout_is_systemic(self):
        with (
            patch.object(helm_handler, "run_helm", side_effect=self._helm()),
            patch.object(helm_handler, "run_kubectl", return_value=(-1, "", "timeout")),
            pytest.raises(
                helm_handler._ValidationTimeout,
                match="kubectl get timed out for release 'demo-release'",
            ),
        ):
            self._validate()

    def test_disabled_release_status_timeout_is_systemic(self):
        with (
            patch.object(helm_handler, "run_helm", return_value=(-1, "", "timeout")),
            pytest.raises(
                helm_handler._ValidationTimeout,
                match="helm status timed out for disabled release 'demo-release'",
            ),
        ):
            helm_handler._validate_disabled_release(
                self.RELEASE, self.NAMESPACE, "/tmp/kubeconfig", time.monotonic() + 60
            )


class TestValidateReleasesAggregation:
    """``validate_releases`` folds Gateway CRD evidence in and bounds its failure summary."""

    LBC = helm_handler.LBC_CHART_NAME

    def _charts(self, names):
        return {
            "charts": {
                name: {"chart": name, "version": "1.0.0", "namespace": f"{name}-ns"}
                for name in names
            }
        }

    def test_lbc_enabled_adds_gateway_crd_evidence_to_resource_counts(self):
        crd_evidence = [
            {"bundle": "gateway-api-standard", "object_count": 12, "crd_count": 10, "sha256": "a"},
            {"bundle": "aws-lbc-gateway", "object_count": 3, "crd_count": 3, "sha256": "b"},
        ]
        with (
            patch.object(
                helm_handler, "load_charts_config", return_value=self._charts([self.LBC, "keda"])
            ),
            patch.object(
                helm_handler, "_validate_gateway_crds", return_value=crd_evidence
            ) as mock_crds,
            patch.object(helm_handler, "_validate_enabled_release", return_value=7),
            patch.object(helm_handler, "_validate_disabled_release") as mock_disabled,
        ):
            evidence = helm_handler.validate_releases(
                {"EnabledCharts": [self.LBC], "Charts": {}}, "/tmp/kc"
            )

        mock_crds.assert_called_once()
        assert mock_crds.call_args.args[0] == "/tmp/kc"
        mock_disabled.assert_called_once()
        assert evidence["gateway_crd_bundles"] == crd_evidence
        assert evidence["expected_resource_count"] == 15 + 7
        assert evidence["validated_resource_count"] == 15 + 7
        assert evidence["enabled_release_count"] == 1
        assert evidence["disabled_release_count"] == 1

    def test_gateway_crd_timeout_propagates_unchanged(self):
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts([self.LBC])),
            patch.object(
                helm_handler,
                "_validate_gateway_crds",
                side_effect=helm_handler._ValidationTimeout("kubectl get timed out"),
            ),
            patch.object(helm_handler, "_validate_enabled_release") as mock_enabled,
            pytest.raises(helm_handler._ValidationTimeout, match="kubectl get timed out"),
        ):
            helm_handler.validate_releases({"EnabledCharts": [self.LBC], "Charts": {}}, "/tmp/kc")
        mock_enabled.assert_not_called()

    def test_gateway_crd_drift_fails_validation_before_release_checks(self):
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts([self.LBC])),
            patch.object(
                helm_handler,
                "_validate_gateway_crds",
                side_effect=RuntimeError("gateway-api-standard SHA-256 mismatch"),
            ),
            patch.object(helm_handler, "_validate_enabled_release") as mock_enabled,
            pytest.raises(
                RuntimeError,
                match="pinned Gateway CRD validation failed: gateway-api-standard SHA-256 mismatch",
            ),
        ):
            helm_handler.validate_releases({"EnabledCharts": [self.LBC], "Charts": {}}, "/tmp/kc")
        mock_enabled.assert_not_called()

    def test_failure_summary_shows_eight_releases_and_counts_the_rest(self):
        names = [f"release-{index}" for index in range(10)]
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts(names)),
            patch.object(
                helm_handler,
                "_validate_enabled_release",
                side_effect=RuntimeError("helm status is 'failed', expected exactly 'deployed'"),
            ),
            pytest.raises(RuntimeError) as exc,
        ):
            helm_handler.validate_releases({"EnabledCharts": names, "Charts": {}}, "/tmp/kc")

        text = str(exc.value)
        assert text.startswith("validated 0/10 releases; ")
        assert "release-7: helm status is 'failed'" in text
        assert "release-8" not in text
        assert text.endswith("... and 2 more failure(s)")


class TestStaleWebhookCleanup:
    """Down webhooks are removed only when their backing Service has no endpoints."""

    def test_removes_only_webhooks_whose_service_has_no_endpoints(self, caplog):
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, "", "")),
            patch.object(helm_handler.subprocess, "run") as mock_run,
            caplog.at_level(logging.WARNING),
        ):
            mock_run.side_effect = [
                # list: a blank line in the middle is skipped
                _completed(0, stdout="keda-admission\n\ncert-manager-webhook\norphan\nno-slash\n"),
                # keda-admission: healthy endpoints
                _completed(0, stdout="keda/keda-admission-webhooks"),
                _completed(0, stdout="10.0.1.5 10.0.2.6"),
                # cert-manager-webhook: no endpoints -> deleted
                _completed(0, stdout="cert-manager/cert-manager-webhook"),
                _completed(0, stdout=""),
                _completed(0, stdout='mutatingwebhookconfiguration "cert-manager-webhook" deleted'),
                # orphan: lookup fails
                _completed(1, stderr="Error from server (NotFound)"),
                # no-slash: jsonpath yielded nothing usable
                _completed(0, stdout=""),
            ]
            helm_handler._cleanup_stale_webhooks("/tmp/kc")

        commands = [call.args[0] for call in mock_run.call_args_list]
        assert commands[0][:3] == ["kubectl", "get", "mutatingwebhookconfigurations"]
        assert [command for command in commands if command[1] == "delete"] == [
            ["kubectl", "delete", "mutatingwebhookconfiguration", "cert-manager-webhook"]
        ]
        endpoint_lookups = [command for command in commands if command[2] == "endpoints"]
        assert [(command[3], command[5]) for command in endpoint_lookups] == [
            ("keda-admission-webhooks", "keda"),
            ("cert-manager-webhook", "cert-manager"),
        ]
        assert all(
            call.kwargs["env"]["KUBECONFIG"] == "/tmp/kc" for call in mock_run.call_args_list
        )
        assert "Webhook cert-manager-webhook has no ready endpoints" in caplog.text
        assert "keda-admission has no ready endpoints" not in caplog.text

    def test_listing_failure_aborts_without_touching_webhooks(self, caplog):
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, "", "")),
            patch.object(
                helm_handler.subprocess,
                "run",
                return_value=_completed(1, stderr="Error from server (Forbidden)"),
            ) as mock_run,
            caplog.at_level(logging.WARNING),
        ):
            helm_handler._cleanup_stale_webhooks("/tmp/kc")

        assert mock_run.call_count == 1
        assert "Failed to list webhooks: Error from server (Forbidden)" in caplog.text

    def test_unexpected_errors_are_non_fatal(self, caplog):
        with (
            patch.object(helm_handler, "run_helm", return_value=(0, "", "")),
            patch.object(
                helm_handler.subprocess, "run", side_effect=FileNotFoundError("kubectl: not found")
            ),
            caplog.at_level(logging.WARNING),
        ):
            helm_handler._cleanup_stale_webhooks("/tmp/kc")

        assert "Webhook cleanup failed (non-fatal): kubectl: not found" in caplog.text


class TestHandleTaskDispatch:
    """Remaining ``handle_task`` actions: quiesce success, overrides, uninstall, unknown."""

    _EVENT = {
        "Chart": "keda",
        "ClusterName": "gco-us-east-1",
        "Region": "us-east-1",
        "EnabledCharts": ["keda"],
    }

    def test_quiesce_success_returns_message(self):
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(
                helm_handler,
                "quiesce_health_monitor",
                return_value=(True, "Health monitor quiesced"),
            ) as mock_quiesce,
        ):
            result = helm_handler.handle_task(
                {"Action": "quiesce_health_monitor", "ClusterName": "c", "Region": "us-east-1"}
            )

        assert result == {"status": "quiesced", "message": "Health monitor quiesced"}
        mock_quiesce.assert_called_once_with("/tmp/kc-missing")

    def test_chart_override_is_merged_into_config_and_passed_as_value_overrides(self):
        captured = {}

        def _install(chart_name, config, kubeconfig, value_overrides):
            captured.update(config=config, value_overrides=value_overrides)
            return True, f"Successfully installed {chart_name}"

        override = {"namespace": "keda-custom", "values": {"resources": {"limits": {"cpu": "2"}}}}
        with (
            patch.object(
                helm_handler,
                "load_charts_config",
                return_value={
                    "charts": {
                        "keda": {
                            "chart": "keda",
                            "namespace": "keda",
                            "values": {"resources": {"requests": {"cpu": "1"}}},
                        }
                    }
                },
            ),
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(helm_handler, "install_chart", side_effect=_install),
            patch.object(helm_handler, "_record_addon_status") as mock_status,
        ):
            result = helm_handler.handle_task(
                {**self._EVENT, "Action": "install_chart", "Charts": {"keda": override}}
            )

        assert result["status"] == "installed"
        assert captured["config"]["namespace"] == "keda-custom"
        assert captured["config"]["values"] == {
            "resources": {"requests": {"cpu": "1"}, "limits": {"cpu": "2"}}
        }
        assert captured["value_overrides"] == override["values"]
        mock_status.assert_called_once_with("keda", "installed", "Successfully installed keda")

    def test_uninstall_action_success_records_status_and_returns(self):
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(
                helm_handler,
                "uninstall_chart",
                return_value=(True, "Successfully uninstalled keda"),
            ) as mock_uninstall,
            patch.object(helm_handler, "install_chart") as mock_install,
            patch.object(helm_handler, "_record_addon_status") as mock_status,
        ):
            result = helm_handler.handle_task({**self._EVENT, "Action": "uninstall_chart"})

        assert result == {
            "chart": "keda",
            "status": "uninstalled",
            "message": "Successfully uninstalled keda",
        }
        mock_uninstall.assert_called_once_with("keda", "keda", "/tmp/kc-missing")
        mock_install.assert_not_called()
        mock_status.assert_called_once_with("keda", "uninstalled", "Successfully uninstalled keda")

    def test_unknown_action_raises_value_error_after_kubeconfig_cleanup(self):
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(helm_handler, "install_chart") as mock_install,
            patch.object(helm_handler, "uninstall_chart") as mock_uninstall,
            patch.object(helm_handler.os, "remove") as mock_remove,
            pytest.raises(ValueError, match="Unknown Action: 'rollback_chart'"),
        ):
            helm_handler.handle_task({**self._EVENT, "Action": "rollback_chart"})

        mock_install.assert_not_called()
        mock_uninstall.assert_not_called()
        mock_remove.assert_called_once_with("/tmp/kc")

    def test_cluster_and_region_fall_back_to_environment(self, monkeypatch):
        monkeypatch.setenv("CLUSTER_NAME", "gco-from-env")
        monkeypatch.setenv("REGION", "eu-west-1")
        with (
            patch.object(
                helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"
            ) as mock_kubeconfig,
            patch.object(helm_handler, "install_chart", return_value=(True, "Successfully ok")),
            patch.object(helm_handler, "_record_addon_status"),
        ):
            helm_handler.handle_task(
                {"Action": "install_chart", "Chart": "keda", "EnabledCharts": ["keda"]}
            )

        mock_kubeconfig.assert_called_once_with("gco-from-env", "eu-west-1")


class TestLegacyCustomResourceHandler:
    """The CloudFormation custom-resource path converges the whole chart set at once."""

    LBC = helm_handler.LBC_CHART_NAME

    @staticmethod
    def _event(request_type="Create", **props):
        return {
            "RequestType": request_type,
            "ResponseURL": "https://cloudformation-custom-resource-response.example.test/",
            "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/gco/0000",
            "RequestId": "request-1",
            "LogicalResourceId": "HelmCharts",
            "ResourceProperties": {"ClusterName": "gco-us-east-1", "Region": "us-east-1", **props},
        }

    def _charts(self):
        return {
            "charts": {
                "keda": {"enabled": True, "namespace": "keda", "values": {"a": 1}},
                "volcano": {"enabled": False, "namespace": "volcano-system"},
                self.LBC: {"enabled": True, "namespace": "kube-system"},
            }
        }

    @staticmethod
    def _response_data(mock_send):
        call = mock_send.call_args
        return call.args[2], json.loads(call.args[3]["Results"]), call.args[3], call

    def test_create_uninstalls_disabled_then_installs_enabled_with_gateway_crds(self):
        order = []

        def _uninstall(chart_name, namespace, kubeconfig):
            order.append(("uninstall", chart_name, namespace))
            return True, f"Chart {chart_name} not found (already uninstalled)"

        def _install(chart_name, config, kubeconfig, value_overrides):
            order.append(("install", chart_name, config["namespace"]))
            return True, f"Successfully installed {chart_name}"

        def _crds(kubeconfig):
            order.append(("crds", kubeconfig))
            return []

        context = MagicMock()
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(helm_handler, "uninstall_chart", side_effect=_uninstall),
            patch.object(helm_handler, "install_chart", side_effect=_install),
            patch.object(helm_handler, "_apply_gateway_crds", side_effect=_crds),
            patch.object(helm_handler.time, "sleep") as mock_sleep,
            patch.object(helm_handler, "send_response") as mock_send,
        ):
            helm_handler.lambda_handler(self._event("Create"), context)

        assert order == [
            ("uninstall", "volcano", "volcano-system"),
            ("install", "keda", "keda"),
            ("crds", "/tmp/kc-missing"),
            ("install", self.LBC, "kube-system"),
        ]
        mock_sleep.assert_not_called()
        status, results, data, call = self._response_data(mock_send)
        assert status == helm_handler.SUCCESS
        assert results == {
            "volcano": "uninstalled (disabled): Chart volcano not found (already uninstalled)",
            "keda": "Successfully installed keda",
            self.LBC: f"Successfully installed {self.LBC}",
        }
        assert data["InstalledCharts"] == f"keda,{self.LBC}"
        assert data["FailedCharts"] == ""
        assert call.args[0]["RequestType"] == "Create"
        assert call.args[1] is context
        assert call.args[4] == "helm-HelmCharts"

    def test_update_applies_overrides_enabled_list_and_keda_role_arn(self):
        installs = {}
        uninstalled = []

        def _install(chart_name, config, kubeconfig, value_overrides):
            installs[chart_name] = (config, value_overrides)
            return True, f"Successfully installed {chart_name}"

        def _uninstall(chart_name, namespace, kubeconfig):
            uninstalled.append(chart_name)
            return True, f"Chart {chart_name} not found (already uninstalled)"

        role_arn = "arn:aws:iam::123456789012:role/keda-operator"
        event = self._event(
            "Update",
            Charts={
                "keda": {"values": {"b": 2}},
                "extra": {"namespace": "extra-system", "values": {"c": 3}},
            },
            EnabledCharts=["keda", "extra"],
            KedaOperatorRoleArn=role_arn,
        )
        event["PhysicalResourceId"] = "helm-charts-existing"
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(helm_handler, "uninstall_chart", side_effect=_uninstall),
            patch.object(helm_handler, "install_chart", side_effect=_install),
            patch.object(helm_handler, "_apply_gateway_crds") as mock_crds,
            patch.object(helm_handler, "send_response") as mock_send,
        ):
            helm_handler.lambda_handler(event, MagicMock())

        assert sorted(installs) == ["extra", "keda"]
        keda_config, keda_overrides = installs["keda"]
        assert keda_config["values"]["a"] == 1
        assert keda_config["values"]["b"] == 2
        annotations = keda_config["values"]["serviceAccount"]["operator"]["annotations"]
        assert annotations["eks.amazonaws.com/role-arn"] == role_arn
        assert keda_overrides == {"b": 2}
        extra_config, extra_overrides = installs["extra"]
        assert extra_config == {"namespace": "extra-system", "values": {"c": 3}, "enabled": True}
        assert extra_overrides == {"c": 3}
        # The enabled list flips the previously-enabled controller off.
        assert sorted(uninstalled) == [self.LBC, "volcano"]
        mock_crds.assert_not_called()
        status, _, _, call = self._response_data(mock_send)
        assert status == helm_handler.SUCCESS
        assert call.args[4] == "helm-charts-existing"

    def test_webhook_failure_triggers_cleanup_and_succeeds_on_retry(self, caplog):
        attempts = {"keda": 0}

        def _install(chart_name, config, kubeconfig, value_overrides):
            if chart_name != "keda":
                return True, f"Successfully installed {chart_name}"
            attempts["keda"] += 1
            if attempts["keda"] == 1:
                return False, (
                    "Failed to install keda: Error: failed calling webhook "
                    '"validate.keda.sh": no endpoints available'
                )
            return True, "Successfully installed keda"

        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(helm_handler, "uninstall_chart", return_value=(True, "not found")),
            patch.object(helm_handler, "install_chart", side_effect=_install),
            patch.object(helm_handler, "_apply_gateway_crds", return_value=[]),
            patch.object(helm_handler, "_cleanup_stale_webhooks") as mock_cleanup,
            patch.object(helm_handler.time, "sleep") as mock_sleep,
            patch.object(helm_handler, "send_response") as mock_send,
            caplog.at_level(logging.INFO),
        ):
            helm_handler.lambda_handler(self._event("Create"), MagicMock())

        mock_cleanup.assert_called_once_with("/tmp/kc-missing")
        mock_sleep.assert_called_once_with(helm_handler.HELM_INSTALL_RETRY_DELAY_SECONDS)
        assert attempts["keda"] == 2
        assert "Retry succeeded for keda" in caplog.text
        status, results, data, _ = self._response_data(mock_send)
        assert status == helm_handler.SUCCESS
        assert results["keda"] == "Successfully installed keda"
        assert data["FailedCharts"] == ""

    def test_persistent_failure_exhausts_retries_and_reports_uninstall_failures_too(self):
        def _install(chart_name, config, kubeconfig, value_overrides):
            if chart_name == "keda":
                return False, "Failed to install keda: Error: values don't meet the specifications"
            return True, f"Successfully installed {chart_name}"

        def _uninstall(chart_name, namespace, kubeconfig):
            return False, f"Failed to uninstall {chart_name}: Forbidden"

        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(helm_handler, "uninstall_chart", side_effect=_uninstall),
            patch.object(helm_handler, "install_chart", side_effect=_install) as mock_install,
            patch.object(helm_handler, "_apply_gateway_crds", return_value=[]),
            patch.object(helm_handler, "_cleanup_stale_webhooks") as mock_cleanup,
            patch.object(helm_handler.time, "sleep") as mock_sleep,
            patch.object(helm_handler, "send_response") as mock_send,
        ):
            helm_handler.lambda_handler(self._event("Create"), MagicMock())

        mock_cleanup.assert_not_called()
        assert mock_sleep.call_count == helm_handler.HELM_INSTALL_MAX_RETRIES
        keda_attempts = [call for call in mock_install.call_args_list if call.args[0] == "keda"]
        assert len(keda_attempts) == 1 + helm_handler.HELM_INSTALL_MAX_RETRIES
        # The disabled chart is never retried as an install.
        assert all(call.args[0] != "volcano" for call in mock_install.call_args_list)
        status, results, data, call = self._response_data(mock_send)
        assert status == helm_handler.FAILED
        assert data["FailedCharts"] == "keda,volcano"
        assert data["InstalledCharts"] == self.LBC
        assert results["volcano"] == "Failed to uninstall volcano: Forbidden"
        assert call.args[5] == "Failed charts: keda, volcano"

    def test_delete_uninstalls_enabled_charts_in_reverse_order_and_skips_disabled(self):
        uninstalled = []

        def _uninstall(chart_name, namespace, kubeconfig):
            uninstalled.append((chart_name, namespace))
            return True, f"Successfully uninstalled {chart_name}"

        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(helm_handler, "uninstall_chart", side_effect=_uninstall),
            patch.object(helm_handler, "install_chart") as mock_install,
            patch.object(helm_handler, "send_response") as mock_send,
        ):
            helm_handler.lambda_handler(self._event("Delete"), MagicMock())

        assert uninstalled == [(self.LBC, "kube-system"), ("keda", "keda")]
        mock_install.assert_not_called()
        status, results, data, _ = self._response_data(mock_send)
        assert status == helm_handler.SUCCESS
        assert results == {
            self.LBC: f"Successfully uninstalled {self.LBC}",
            "keda": "Successfully uninstalled keda",
        }
        assert data["FailedCharts"] == ""

    def test_unrecognised_request_type_touches_nothing_and_reports_success(self):
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc-missing"),
            patch.object(helm_handler, "uninstall_chart") as mock_uninstall,
            patch.object(helm_handler, "install_chart") as mock_install,
            patch.object(helm_handler, "send_response") as mock_send,
        ):
            helm_handler.lambda_handler(self._event("Read"), MagicMock())

        mock_uninstall.assert_not_called()
        mock_install.assert_not_called()
        status, results, data, _ = self._response_data(mock_send)
        assert status == helm_handler.SUCCESS
        assert results == {}
        assert data == {"Results": "{}", "InstalledCharts": "", "FailedCharts": ""}

    def test_unexpected_exception_reports_failed_with_the_error_text(self):
        with (
            patch.object(helm_handler, "load_charts_config", return_value=self._charts()),
            patch.object(
                helm_handler,
                "configure_kubeconfig",
                side_effect=RuntimeError("describe_cluster: cluster not found"),
            ),
            patch.object(helm_handler, "send_response") as mock_send,
        ):
            helm_handler.lambda_handler(self._event("Create"), MagicMock())

        call = mock_send.call_args
        assert call.args[2] == helm_handler.FAILED
        assert call.args[3] == {}
        assert call.args[4] == "helm-HelmCharts"
        assert call.args[5] == "describe_cluster: cluster not found"

    def test_missing_resource_properties_is_a_failed_response(self):
        event = self._event("Create")
        del event["ResourceProperties"]
        with (
            patch.object(helm_handler, "configure_kubeconfig") as mock_kubeconfig,
            patch.object(helm_handler, "send_response") as mock_send,
        ):
            helm_handler.lambda_handler(event, MagicMock())

        mock_kubeconfig.assert_not_called()
        assert mock_send.call_args.args[2] == helm_handler.FAILED
        assert "ResourceProperties" in mock_send.call_args.args[5]
