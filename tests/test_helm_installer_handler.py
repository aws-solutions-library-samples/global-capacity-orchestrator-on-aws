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

import hashlib
import json
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

            success, message = helm_handler._delete_keda_custom_resources("/tmp/kc")

        assert success is True
        assert "5 KEDA custom resource type" in message
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
            success, message = helm_handler._delete_keda_custom_resources("/tmp/kc")

        assert success is False
        assert "api discovery unavailable" in message
        assert mock_run.call_count == 1

    def test_keda_cleanup_runs_before_helm_uninstall(self):
        calls = []

        def _cleanup(_kubeconfig):
            calls.append("cleanup")
            return True, "clean"

        def _helm(*_args, **_kwargs):
            calls.append("helm")
            return 0, "", ""

        with (
            patch.object(helm_handler, "_delete_keda_custom_resources", side_effect=_cleanup),
            patch.object(helm_handler, "run_helm", side_effect=_helm),
        ):
            success, _ = helm_handler.uninstall_chart("keda", "keda", "/tmp/kc")

        assert success is True
        assert calls == ["cleanup", "helm"]

    def test_cleanup_failure_prevents_helm_uninstall(self):
        with (
            patch.object(
                helm_handler,
                "_delete_keda_custom_resources",
                return_value=(False, "scaledjobs remain"),
            ),
            patch.object(helm_handler, "run_helm") as mock_run,
        ):
            success, message = helm_handler.uninstall_chart("keda", "keda", "/tmp/kc")

        assert success is False
        assert "scaledjobs remain" in message
        mock_run.assert_not_called()

    def test_non_keda_uninstall_skips_custom_resource_cleanup(self):
        with (
            patch.object(helm_handler, "_delete_keda_custom_resources") as mock_cleanup,
            patch.object(helm_handler, "run_helm", return_value=(0, "", "")),
        ):
            success, _ = helm_handler.uninstall_chart("volcano", "volcano", "/tmp/kc")

        assert success is True
        mock_cleanup.assert_not_called()


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
                "_delete_keda_custom_resources",
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
                "_delete_keda_custom_resources",
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
