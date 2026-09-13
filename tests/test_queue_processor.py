"""
Tests for the GCO queue processor (gco/services/queue_processor.py).

Covers manifest validation (namespace allowlist, security policy toggles,
image registry allowlist, resource caps), SA-token auto-mount injection,
and SQS message processing. Includes structural parity checks that fail
loudly if the REST manifest processor gains a security toggle the SQS
path doesn't mirror. An autouse fixture scrubs BLOCK_* env vars before
every test so cross-file ordering can't leak state into the module's
import-time constants.
"""

from __future__ import annotations

import importlib
import json
import os
from unittest.mock import MagicMock

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────


# Env vars the queue_processor reads at module-load time. Other test files
# may leak values for these into os.environ (e.g. by importing modules that
# set defaults for their own tests). We scrub them before every test here so
# ``_reload()`` always sees a known-good state. Any test that needs a non-
# default value uses ``monkeypatch.setenv`` explicitly.
_QP_ENV_VARS = (
    "BLOCK_PRIVILEGED",
    "BLOCK_PRIVILEGE_ESCALATION",
    "BLOCK_HOST_NETWORK",
    "BLOCK_HOST_PID",
    "BLOCK_HOST_IPC",
    "BLOCK_HOST_PATH",
    "BLOCK_ADDED_CAPABILITIES",
    "BLOCK_RUN_AS_ROOT",
    "ALLOWED_NAMESPACES",
    "ALLOWED_KINDS",
    "MAX_CPU_PER_MANIFEST",
    "MAX_MEMORY_PER_MANIFEST",
    "MAX_GPU_PER_MANIFEST",
    "TRUSTED_REGISTRIES",
    "TRUSTED_DOCKERHUB_ORGS",
    "REQUIRE_ACCELERATOR_TOLERATION",
    "JOBS_TABLE_NAME",
    "DYNAMODB_REGION",
    "QP_JOB_QUEUE_MAX_RECEIVE_COUNT",
    "KUBERNETES_SERVICE_HOST",
    "SERVICE_ACCOUNT_NAME",
)


@pytest.fixture(autouse=True)
def _scrub_qp_env(monkeypatch):
    """Delete queue-processor env vars before each test for hermeticity."""
    for name in _QP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _reload():
    """Re-import the module so module-level env reads pick up monkeypatch values."""
    import gco.services.queue_processor as qp

    importlib.reload(qp)
    return qp


def _job(name="test-job", namespace="gco-jobs", gpu=0, privileged=False, escalation=False):
    """Build a valid Job manifest."""
    c = {
        "name": "worker",
        "image": "python:3.14-slim",
        "securityContext": {"allowPrivilegeEscalation": escalation},
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
    }
    if privileged:
        c["securityContext"]["privileged"] = True
    pod_spec: dict = {"restartPolicy": "Never", "containers": [c]}
    if gpu:
        c["resources"]["limits"] = {"nvidia.com/gpu": str(gpu)}
        # A GPU job must carry a matching toleration or it is rejected before
        # the resource cap is ever checked (REQUIRE_ACCELERATOR_TOLERATION).
        pod_spec["tolerations"] = [
            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
        ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"template": {"spec": pod_spec}},
    }


def _sqs_resp(manifests, job_id="abc123", receive_count=None, **body_overrides):
    """Build an SQS receive_message response.

    ``receive_count`` fills ``Attributes.ApproximateReceiveCount`` (omitted
    when ``None``, matching a receive without ``AttributeNames``).
    ``body_overrides`` replace or add top-level message-body fields.
    """
    body = {
        "job_id": job_id,
        "manifests": manifests,
        "namespace": "gco-jobs",
        "priority": 0,
        "submitted_at": "2026-03-26T12:00:00+00:00",
    }
    body.update(body_overrides)
    message = {
        "ReceiptHandle": "receipt-xyz",
        "Body": json.dumps(body),
    }
    if receive_count is not None:
        message["Attributes"] = {"ApproximateReceiveCount": str(receive_count)}
    return {"Messages": [message]}


@pytest.fixture(autouse=True)
def _env(monkeypatch, _scrub_qp_env):
    # Depends on _scrub_qp_env so the scrub deterministically runs first;
    # autouse fixtures of equal scope have no guaranteed relative order, and
    # the scrub deletes the exact variables this fixture sets. (Latent until
    # the shipped default GPU cap rose above this fixture's value.)
    monkeypatch.setenv("JOB_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ALLOWED_NAMESPACES", "gco-jobs")
    monkeypatch.setenv("MAX_GPU_PER_MANIFEST", "4")


# ── Validation ───────────────────────────────────────────────────────────


class TestValidateManifest:
    def test_valid_job(self):
        qp = _reload()
        assert qp.validate_manifest(_job()) == (True, "")

    def test_missing_kind(self):
        qp = _reload()
        ok, r = qp.validate_manifest({"apiVersion": "v1", "metadata": {"name": "x"}})
        assert not ok and "kind" in r

    def test_missing_api_version(self):
        qp = _reload()
        ok, r = qp.validate_manifest({"kind": "Job", "metadata": {"name": "x"}})
        assert not ok and "apiVersion" in r

    def test_missing_name(self):
        qp = _reload()
        ok, r = qp.validate_manifest({"apiVersion": "v1", "kind": "Job", "metadata": {}})
        assert not ok and "name" in r

    def test_no_metadata(self):
        qp = _reload()
        ok, r = qp.validate_manifest({"apiVersion": "v1", "kind": "Job"})
        assert not ok and "name" in r

    def test_disallowed_namespace(self):
        qp = _reload()
        ok, r = qp.validate_manifest(_job(namespace="kube-system"))
        assert not ok and "kube-system" in r

    def test_implicit_namespace_defaults_to_gco_jobs(self):
        qp = _reload()
        manifest = _job()
        del manifest["metadata"]["namespace"]
        assert qp.validate_manifest(manifest)[0] is True

    def test_default_namespace_rejected_by_stock_policy(self):
        qp = _reload()
        ok, reason = qp.validate_manifest(_job(namespace="default"))
        assert not ok
        assert "default" in reason

    def test_disallowed_resource_kind(self):
        qp = _reload()
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "credentials", "namespace": "gco-jobs"},
        }
        ok, reason = qp.validate_manifest(manifest)
        assert not ok
        assert "not allowed" in reason

    def test_custom_allowed_resource_kinds(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_KINDS", "Job")
        qp = _reload()
        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "config", "namespace": "gco-jobs"},
        }
        assert qp.validate_manifest(_job())[0] is True
        assert qp.validate_manifest(config_map)[0] is False

    def test_explicit_empty_allowed_kinds_denies_all(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_KINDS", "")
        qp = _reload()

        assert not qp.ALLOWED_KINDS
        valid, reason = qp.validate_manifest(_job())
        assert valid is False
        assert "not allowed" in reason

    def test_explicit_empty_allowed_namespaces_denies_all(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_NAMESPACES", "")
        qp = _reload()

        assert not qp.ALLOWED_NAMESPACES
        valid, reason = qp.validate_manifest(_job())
        assert valid is False
        assert "not in allowed list" in reason

    def test_privileged_blocked(self):
        qp = _reload()
        ok, r = qp.validate_manifest(_job(privileged=True))
        assert not ok and "privileged" in r

    def test_escalation_blocked(self):
        qp = _reload()
        ok, r = qp.validate_manifest(_job(escalation=True))
        assert not ok and "Escalation" in r

    def test_gpu_within_limit(self):
        qp = _reload()
        assert qp.validate_manifest(_job(gpu=4))[0] is True

    def test_gpu_exceeds_limit(self):
        qp = _reload()
        ok, r = qp.validate_manifest(_job(gpu=8))
        assert not ok and "GPU" in r

    def test_gpu_from_requests(self):
        qp = _reload()
        m = _job()
        m["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "requests": {"nvidia.com/gpu": "8"}
        }
        # Carry a toleration so the cap (not the toleration rule) is what rejects.
        m["spec"]["template"]["spec"]["tolerations"] = [
            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
        ]
        ok, r = qp.validate_manifest(m)
        assert not ok and "GPU" in r

    def test_cronjob_privileged(self):
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "c", "namespace": "gco-jobs"},
            "spec": {
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "w",
                                        "image": "x",
                                        "securityContext": {"privileged": True},
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        }
        ok, r = qp.validate_manifest(m)
        assert not ok and "privileged" in r

    def test_configmap_passes(self):
        qp = _reload()
        m = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "c", "namespace": "gco-jobs"},
            "data": {"k": "v"},
        }
        assert qp.validate_manifest(m)[0] is True


class TestNamespaceConfig:
    def test_custom_namespaces(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_NAMESPACES", "team-a,team-b")
        qp = _reload()
        assert qp.validate_manifest(_job(namespace="team-a"))[0] is True
        assert qp.validate_manifest(_job(namespace="gco-jobs"))[0] is False

    def test_gpu_limit_from_env(self, monkeypatch):
        monkeypatch.setenv("MAX_GPU_PER_MANIFEST", "2")
        qp = _reload()
        assert qp.validate_manifest(_job(gpu=2))[0] is True
        assert qp.validate_manifest(_job(gpu=3))[0] is False


# ── Apply Manifest ───────────────────────────────────────────────────────


class TestApplyManifest:
    def _setup_mocks(self):
        qp = _reload()
        mock_dyn_cls = MagicMock()
        mock_resource = MagicMock()
        mock_resource.namespaced = True
        mock_dyn_cls.return_value.resources.get.return_value = mock_resource
        qp.dynamic = MagicMock()
        qp.dynamic.DynamicClient = mock_dyn_cls
        qp.client = MagicMock()
        return qp, mock_resource

    def test_create(self):
        qp, res = self._setup_mocks()
        assert qp.apply_manifest(_job()).status == "created"

    def test_update_on_409(self):
        from kubernetes.client.rest import ApiException

        qp, res = self._setup_mocks()
        res.create.side_effect = ApiException(status=409)
        assert qp.apply_manifest(_job()).status == "updated"

    def test_create_failed(self):
        from kubernetes.client.rest import ApiException

        qp, res = self._setup_mocks()
        res.create.side_effect = ApiException(status=403, reason="Forbidden")
        result = qp.apply_manifest(_job())
        assert result.status == "failed"
        assert "Create failed" in (result.message or "")

    def test_patch_failed(self):
        from kubernetes.client.rest import ApiException

        qp, res = self._setup_mocks()
        res.create.side_effect = ApiException(status=409)
        res.patch.side_effect = ApiException(status=422, reason="Unprocessable")
        result = qp.apply_manifest(_job())
        assert result.status == "failed"
        assert "Patch failed" in (result.message or "")

    def test_unknown_resource(self):
        from kubernetes.dynamic.exceptions import ResourceNotFoundError

        qp = _reload()
        qp.client = MagicMock()
        mock_dyn = MagicMock()
        mock_dyn.return_value.resources.get.side_effect = ResourceNotFoundError("nope")
        qp.dynamic = MagicMock()
        qp.dynamic.DynamicClient = mock_dyn
        result = qp.apply_manifest(_job())
        assert result.status == "failed"
        assert "Unsupported Kubernetes resource" in (result.message or "")

    def test_finished_job_deleted(self):
        qp, res = self._setup_mocks()
        qp.time = MagicMock()
        res.get.return_value = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        assert qp.apply_manifest(_job()).status == "created"
        res.delete.assert_called_once()

    def test_false_terminal_condition_is_not_deleted(self):
        qp, res = self._setup_mocks()
        qp.time = MagicMock()
        res.get.return_value = {"status": {"conditions": [{"type": "Failed", "status": "False"}]}}

        assert qp.apply_manifest(_job()).status == "created"

        res.delete.assert_not_called()

    def test_non_namespaced(self):
        qp, res = self._setup_mocks()
        res.namespaced = False
        m = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "ns"}}
        assert qp.apply_manifest(m).status == "created"

    def test_non_namespaced_update(self):
        from kubernetes.client.rest import ApiException

        qp, res = self._setup_mocks()
        res.namespaced = False
        res.create.side_effect = ApiException(status=409)
        m = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "ns"}}
        assert qp.apply_manifest(m).status == "updated"


# ── SQS Processing ──────────────────────────────────────────────────────


class TestProcessOneMessage:
    @staticmethod
    def _result(qp, status="created", name="x"):
        return qp.ResourceStatus(
            api_version="batch/v1",
            kind="Job",
            name=name,
            namespace="gco-jobs",
            status=status,
            message=f"{status} for test",
        )

    def _setup(self, manifests, apply_status="created"):
        qp = _reload()
        mock_sqs = MagicMock()
        qp.boto3 = MagicMock()
        qp.boto3.client.return_value = mock_sqs
        mock_sqs.receive_message.return_value = _sqs_resp(manifests)
        qp.apply_manifest = MagicMock(return_value=self._result(qp, apply_status))
        return qp, mock_sqs

    def test_success(self):
        qp, sqs = self._setup([_job()])
        assert qp.process_one_message() is True
        sqs.delete_message.assert_called_once()

    def test_no_messages(self):
        qp = _reload()
        mock_sqs = MagicMock()
        qp.boto3 = MagicMock()
        qp.boto3.client.return_value = mock_sqs
        mock_sqs.receive_message.return_value = {"Messages": []}
        assert qp.process_one_message() is True
        mock_sqs.delete_message.assert_not_called()

    def test_validation_failure(self):
        qp, sqs = self._setup([_job(namespace="kube-system")])
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()

    def test_apply_failure(self):
        qp, sqs = self._setup([_job()], "failed")
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()

    def test_multiple_success(self):
        qp, sqs = self._setup([_job(name="a"), _job(name="b")])
        assert qp.process_one_message() is True
        assert qp.apply_manifest.call_count == 2

    def test_partial_failure(self):
        qp, sqs = self._setup([_job(name="a"), _job(name="b")])
        qp.apply_manifest.side_effect = [
            self._result(qp, "created", "a"),
            self._result(qp, "failed", "b"),
        ]
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()

    def test_empty_queue_url(self, monkeypatch):
        monkeypatch.setenv("JOB_QUEUE_URL", "")
        qp = _reload()
        assert qp.process_one_message() is False

    def test_empty_manifests_is_poison_message(self):
        qp, sqs = self._setup([])
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()
        qp.apply_manifest.assert_not_called()

    def test_malformed_json_is_retained(self):
        qp, sqs = self._setup([_job()])
        sqs.receive_message.return_value = {
            "Messages": [{"ReceiptHandle": "receipt-xyz", "Body": "{not-json"}]
        }
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()
        qp.apply_manifest.assert_not_called()

    def test_entire_batch_is_validated_before_apply(self):
        qp, sqs = self._setup([_job(name="valid"), _job(name="invalid", namespace="default")])
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()
        qp.apply_manifest.assert_not_called()

    def test_unsupported_kind_is_retained(self):
        unsupported = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "credentials", "namespace": "gco-jobs"},
        }
        qp, sqs = self._setup([unsupported])
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()
        qp.apply_manifest.assert_not_called()


# ── Main / K8s Config ────────────────────────────────────────────────────


class TestMain:
    def test_success(self):
        qp = _reload()
        qp.load_k8s = MagicMock()
        qp.process_one_message = MagicMock(return_value=True)
        qp.main()

    def test_failure_exits(self):
        qp = _reload()
        qp.load_k8s = MagicMock()
        qp.process_one_message = MagicMock(return_value=False)
        with pytest.raises(SystemExit) as exc:
            qp.main()
        assert exc.value.code == 1

    def test_config_failure_drains_with_record_and_exits(self):
        """A configuration failure is terminal: drain one message, exit 1."""
        qp = _reload()
        qp.load_k8s = MagicMock(side_effect=qp.KubernetesConfigurationError("no token"))
        qp.drain_one_message_after_config_failure = MagicMock(return_value=True)
        qp.process_one_message = MagicMock()
        with pytest.raises(SystemExit) as exc:
            qp.main()
        assert exc.value.code == 1
        qp.drain_one_message_after_config_failure.assert_called_once_with("no token")
        qp.process_one_message.assert_not_called()


class TestLoadK8s:
    def test_incluster(self):
        qp = _reload()
        qp.config = MagicMock()
        qp.load_k8s()
        qp.config.load_incluster_config.assert_called_once()

    def test_fallback(self):
        from kubernetes.config import ConfigException

        qp = _reload()
        mock_cfg = MagicMock()
        mock_cfg.load_incluster_config.side_effect = ConfigException("nope")
        mock_cfg.ConfigException = ConfigException
        qp.config = mock_cfg
        qp.load_k8s()
        mock_cfg.load_kube_config.assert_called_once()

    @staticmethod
    def _incluster_failure(qp):
        from kubernetes.config import ConfigException

        mock_cfg = MagicMock()
        mock_cfg.load_incluster_config.side_effect = ConfigException(
            "Service host/port is not set."
        )
        mock_cfg.ConfigException = ConfigException
        qp.config = mock_cfg
        return mock_cfg

    def test_in_pod_failure_is_terminal_and_names_token_path_and_sa(self, monkeypatch):
        """Inside a pod the fallback must not run; the error is actionable."""
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.100.0.1")
        monkeypatch.setenv("SERVICE_ACCOUNT_NAME", "gco-manifest-processor-sa")
        qp = _reload()
        mock_cfg = self._incluster_failure(qp)

        real_exists = os.path.exists
        monkeypatch.setattr(
            "os.path.exists",
            lambda path: False if path == qp._SERVICEACCOUNT_TOKEN_PATH else real_exists(path),
        )

        with pytest.raises(qp.KubernetesConfigurationError) as error:
            qp.load_k8s()
        detail = str(error.value)
        assert "/var/run/secrets/kubernetes.io/serviceaccount/token" in detail
        assert "gco-manifest-processor-sa" in detail
        assert "automountServiceAccountToken" in detail
        mock_cfg.load_kube_config.assert_not_called()

    def test_in_pod_failure_with_token_present_reports_the_original_error(self, monkeypatch):
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.100.0.1")
        monkeypatch.setenv("SERVICE_ACCOUNT_NAME", "gco-manifest-processor-sa")
        qp = _reload()
        mock_cfg = self._incluster_failure(qp)

        real_exists = os.path.exists
        monkeypatch.setattr(
            "os.path.exists",
            lambda path: True if path == qp._SERVICEACCOUNT_TOKEN_PATH else real_exists(path),
        )

        with pytest.raises(qp.KubernetesConfigurationError) as error:
            qp.load_k8s()
        detail = str(error.value)
        assert "token exists" in detail
        assert "gco-manifest-processor-sa" in detail
        assert "Service host/port is not set." in detail
        mock_cfg.load_kube_config.assert_not_called()

    def test_in_pod_failure_without_sa_env_still_raises(self, monkeypatch):
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.100.0.1")
        qp = _reload()
        self._incluster_failure(qp)

        real_exists = os.path.exists
        monkeypatch.setattr(
            "os.path.exists",
            lambda path: False if path == qp._SERVICEACCOUNT_TOKEN_PATH else real_exists(path),
        )

        with pytest.raises(qp.KubernetesConfigurationError) as error:
            qp.load_k8s()
        assert "SERVICE_ACCOUNT_NAME unset" in str(error.value)

    def test_off_cluster_fallback_failure_is_wrapped(self):
        """A broken local kubeconfig raises the specific error, not ConfigException."""
        from kubernetes.config import ConfigException

        qp = _reload()
        mock_cfg = MagicMock()
        mock_cfg.load_incluster_config.side_effect = ConfigException("not in cluster")
        mock_cfg.load_kube_config.side_effect = ConfigException("no kubeconfig")
        mock_cfg.ConfigException = ConfigException
        qp.config = mock_cfg
        with pytest.raises(qp.KubernetesConfigurationError) as error:
            qp.load_k8s()
        assert "KUBERNETES_SERVICE_HOST unset" in str(error.value)


class TestBoundedError:
    def test_short_text_passes_through(self):
        qp = _reload()
        assert qp._bounded_error("boom") == "boom"

    def test_long_text_is_truncated(self):
        qp = _reload()
        bounded = qp._bounded_error("x" * 5000)
        assert bounded == "x" * 2000 + "...[truncated]"


class TestRecordJobFailure:
    def _qp(self, monkeypatch, table="gco-jobs"):
        if table is not None:
            monkeypatch.setenv("JOBS_TABLE_NAME", table)
        qp = _reload()
        qp.JobStore = MagicMock()
        return qp

    def test_records_failed_with_bounded_error(self, monkeypatch):
        qp = self._qp(monkeypatch)
        qp.JobStore.return_value.record_job_failure.return_value = True

        assert qp._record_job_failure(
            "abc123",
            namespace="gco-jobs",
            error="y" * 5000,
            message="SQS job could not be applied to Kubernetes",
            priority=7,
            submitted_at="2026-03-26T12:00:00+00:00",
        )
        qp.JobStore.assert_called_once_with(table_name="gco-jobs")
        kwargs = qp.JobStore.return_value.record_job_failure.call_args.kwargs
        assert qp.JobStore.return_value.record_job_failure.call_args.args == ("abc123",)
        assert kwargs["target_region"] == "us-east-1"
        assert kwargs["namespace"] == "gco-jobs"
        assert kwargs["error"] == "y" * 2000 + "...[truncated]"
        assert kwargs["message"] == "SQS job could not be applied to Kubernetes"
        assert kwargs["priority"] == 7
        assert kwargs["submitted_at"] == "2026-03-26T12:00:00+00:00"

    def test_without_table_env_records_nothing(self, monkeypatch):
        qp = self._qp(monkeypatch, table=None)
        assert not qp._record_job_failure("abc123", namespace="gco-jobs", error="e", message="m")
        qp.JobStore.assert_not_called()

    def test_store_exception_is_swallowed(self, monkeypatch):
        qp = self._qp(monkeypatch)
        qp.JobStore.return_value.record_job_failure.side_effect = RuntimeError("ddb down")
        assert not qp._record_job_failure("abc123", namespace="gco-jobs", error="e", message="m")

    def test_existing_record_left_untouched(self, monkeypatch):
        qp = self._qp(monkeypatch)
        qp.JobStore.return_value.record_job_failure.return_value = False
        assert not qp._record_job_failure("abc123", namespace="gco-jobs", error="e", message="m")


class TestDrainAfterConfigFailure:
    def _qp(self, monkeypatch, table="gco-jobs"):
        if table is not None:
            monkeypatch.setenv("JOBS_TABLE_NAME", table)
        qp = _reload()
        qp.boto3 = MagicMock()
        qp.JobStore = MagicMock()
        qp.JobStore.return_value.record_job_failure.return_value = True
        return qp, qp.boto3.client.return_value

    def test_drains_one_message_with_record(self, monkeypatch):
        qp, sqs = self._qp(monkeypatch)
        sqs.receive_message.return_value = _sqs_resp([_job()], receive_count=1)

        assert qp.drain_one_message_after_config_failure("no token at /var/run/...")

        kwargs = qp.JobStore.return_value.record_job_failure.call_args.kwargs
        assert qp.JobStore.return_value.record_job_failure.call_args.args == ("abc123",)
        assert "could not initialize Kubernetes credentials" in kwargs["error"]
        assert "no token at /var/run/..." in kwargs["error"]
        assert kwargs["message"] == "Queue processor configuration failure"
        sqs.delete_message.assert_called_once()

    def test_empty_queue_drains_nothing(self, monkeypatch):
        qp, sqs = self._qp(monkeypatch)
        sqs.receive_message.return_value = {"Messages": []}
        assert not qp.drain_one_message_after_config_failure("boom")
        sqs.delete_message.assert_not_called()

    def test_without_queue_url_reports_nothing(self, monkeypatch):
        monkeypatch.delenv("JOB_QUEUE_URL", raising=False)
        monkeypatch.setenv("JOBS_TABLE_NAME", "gco-jobs")
        qp = _reload()
        qp.boto3 = MagicMock()
        assert not qp.drain_one_message_after_config_failure("boom")
        qp.boto3.client.assert_not_called()

    def test_without_table_env_leaves_queue_untouched(self, monkeypatch):
        qp, _sqs = self._qp(monkeypatch, table=None)
        assert not qp.drain_one_message_after_config_failure("boom")
        qp.boto3.client.assert_not_called()

    def test_malformed_body_is_retained_for_dlq(self, monkeypatch):
        qp, sqs = self._qp(monkeypatch)
        sqs.receive_message.return_value = {
            "Messages": [{"ReceiptHandle": "receipt-xyz", "Body": "not json"}]
        }
        assert not qp.drain_one_message_after_config_failure("boom")
        qp.JobStore.return_value.record_job_failure.assert_not_called()
        sqs.delete_message.assert_not_called()

    def test_missing_job_id_is_retained_for_dlq(self, monkeypatch):
        qp, sqs = self._qp(monkeypatch)
        sqs.receive_message.return_value = _sqs_resp([_job()], job_id="")
        assert not qp.drain_one_message_after_config_failure("boom")
        sqs.delete_message.assert_not_called()

    def test_unrecorded_failure_keeps_the_message(self, monkeypatch):
        qp, sqs = self._qp(monkeypatch)
        qp.JobStore.return_value.record_job_failure.return_value = False
        sqs.receive_message.return_value = _sqs_resp([_job()])
        assert not qp.drain_one_message_after_config_failure("boom")
        sqs.delete_message.assert_not_called()

    def test_non_string_priority_and_submitted_at_are_normalized(self, monkeypatch):
        qp, sqs = self._qp(monkeypatch)
        sqs.receive_message.return_value = _sqs_resp([_job()], priority="high", submitted_at=12345)
        assert qp.drain_one_message_after_config_failure("boom")
        kwargs = qp.JobStore.return_value.record_job_failure.call_args.kwargs
        assert kwargs["priority"] == 0
        assert kwargs["submitted_at"] is None


class TestFinalReceiveFailureRecording:
    """A message's LAST delivery converts its failure into a FAILED record.

    Earlier receives keep the pre-existing retain-for-retry semantics with
    no record, so a transient failure that succeeds on a later receive
    never leaves a terminal record behind.
    """

    def _setup(self, manifests, monkeypatch, receive_count, apply_status="failed"):
        monkeypatch.setenv("JOBS_TABLE_NAME", "gco-jobs")
        qp = _reload()
        mock_sqs = MagicMock()
        qp.boto3 = MagicMock()
        qp.boto3.client.return_value = mock_sqs
        mock_sqs.receive_message.return_value = _sqs_resp(manifests, receive_count=receive_count)
        result = qp.ResourceStatus(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="gco-jobs",
            status=apply_status,
            message=f"{apply_status} for test",
        )
        qp.apply_manifest = MagicMock(return_value=result)
        qp.JobStore = MagicMock()
        qp.JobStore.return_value.record_job_failure.return_value = True
        return qp, mock_sqs

    def test_apply_failure_on_final_receive_records_failed(self, monkeypatch):
        qp, sqs = self._setup([_job()], monkeypatch, receive_count=3)
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()
        kwargs = qp.JobStore.return_value.record_job_failure.call_args.kwargs
        assert qp.JobStore.return_value.record_job_failure.call_args.args == ("abc123",)
        assert kwargs["message"] == "SQS job could not be applied to Kubernetes"
        assert "manifest[0] Job/test-job" in kwargs["error"]
        assert kwargs["target_region"] == "us-east-1"
        assert kwargs["namespace"] == "gco-jobs"
        assert kwargs["submitted_at"] == "2026-03-26T12:00:00+00:00"

    def test_apply_exception_on_final_receive_records_bounded_error(self, monkeypatch):
        qp, sqs = self._setup([_job()], monkeypatch, receive_count=3)
        qp.apply_manifest = MagicMock(side_effect=RuntimeError("z" * 5000))
        assert qp.process_one_message() is False
        kwargs = qp.JobStore.return_value.record_job_failure.call_args.kwargs
        assert "manifest[0] apply raised" in kwargs["error"]
        assert kwargs["error"].endswith("...[truncated]")
        assert len(kwargs["error"]) == 2000 + len("...[truncated]")

    def test_apply_failure_before_final_receive_records_nothing(self, monkeypatch):
        qp, sqs = self._setup([_job()], monkeypatch, receive_count=1)
        assert qp.process_one_message() is False
        sqs.delete_message.assert_not_called()
        qp.JobStore.return_value.record_job_failure.assert_not_called()

    def test_prevalidation_failure_on_final_receive_records_failed(self, monkeypatch):
        qp, _sqs = self._setup([_job(namespace="kube-system")], monkeypatch, receive_count=3)
        assert qp.process_one_message() is False
        kwargs = qp.JobStore.return_value.record_job_failure.call_args.kwargs
        assert kwargs["message"] == "SQS job failed prevalidation"
        assert "manifest[0]:" in kwargs["error"]

    def test_prevalidation_failure_before_final_receive_records_nothing(self, monkeypatch):
        qp, _sqs = self._setup([_job(namespace="kube-system")], monkeypatch, receive_count=1)
        assert qp.process_one_message() is False
        qp.JobStore.return_value.record_job_failure.assert_not_called()

    def test_success_on_final_receive_records_nothing(self, monkeypatch):
        qp, sqs = self._setup([_job()], monkeypatch, receive_count=3, apply_status="created")
        assert qp.process_one_message() is True
        sqs.delete_message.assert_called_once()
        qp.JobStore.return_value.record_job_failure.assert_not_called()

    def test_unreadable_receive_count_is_treated_as_first_receive(self, monkeypatch):
        qp, _sqs = self._setup([_job()], monkeypatch, receive_count="not-a-number")
        assert qp.process_one_message() is False
        qp.JobStore.return_value.record_job_failure.assert_not_called()

    def test_missing_attributes_default_to_first_receive(self, monkeypatch):
        qp, _sqs = self._setup([_job()], monkeypatch, receive_count=None)
        assert qp.process_one_message() is False
        qp.JobStore.return_value.record_job_failure.assert_not_called()

    def test_recording_disabled_without_table_env(self, monkeypatch):
        monkeypatch.delenv("JOBS_TABLE_NAME", raising=False)
        qp = _reload()
        mock_sqs = MagicMock()
        qp.boto3 = MagicMock()
        qp.boto3.client.return_value = mock_sqs
        mock_sqs.receive_message.return_value = _sqs_resp([_job()], receive_count=3)
        result = qp.ResourceStatus(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="gco-jobs",
            status="failed",
            message="failed for test",
        )
        qp.apply_manifest = MagicMock(return_value=result)
        qp.JobStore = MagicMock()
        assert qp.process_one_message() is False
        qp.JobStore.assert_not_called()

    def test_non_int_priority_in_body_is_normalized(self, monkeypatch):
        qp, _sqs = self._setup([_job()], monkeypatch, receive_count=3)
        qp.boto3.client.return_value.receive_message.return_value = _sqs_resp(
            [_job()], receive_count=3, priority="urgent", submitted_at=99
        )
        assert qp.process_one_message() is False
        kwargs = qp.JobStore.return_value.record_job_failure.call_args.kwargs
        assert kwargs["priority"] == 0
        assert kwargs["submitted_at"] is None


# ── Env-string parsing (regression tests) ───────────────────────────────
#
# Bug history: the queue processor crashed with
#   ValueError: invalid literal for int() with base 10: '32Gi'
# because cdk.json stores max_memory_per_manifest as "32Gi" (a K8s-style
# suffix) while the module previously did a bare int() on that env var.
# Similarly, MAX_CPU_PER_MANIFEST is set as "10" (meaning 10 cores = 10000
# millicores) but bare int() would treat it as 10 millicores. These tests
# pin the parsing helpers so the same bug can't come back.


class TestParseCpuString:
    """Validates: queue_processor._parse_cpu_string correctly converts
    Kubernetes-style CPU strings to millicores (regression for deploy-time
    crash when MAX_CPU_PER_MANIFEST='10' was read as 10 millicores)."""

    def test_millicore_suffix(self):
        qp = _reload()
        assert qp._parse_cpu_string("500m") == 500

    def test_whole_cores(self):
        qp = _reload()
        assert qp._parse_cpu_string("10") == 10000  # 10 cores = 10000 millicores

    def test_single_core(self):
        qp = _reload()
        assert qp._parse_cpu_string("1") == 1000

    def test_empty_returns_zero(self):
        qp = _reload()
        assert qp._parse_cpu_string("") == 0


class TestParseMemoryString:
    """Validates: queue_processor._parse_memory_string correctly converts
    Kubernetes-style memory strings to bytes (regression for deploy-time
    crash when MAX_MEMORY_PER_MANIFEST='32Gi' raised ValueError)."""

    def test_gi_suffix(self):
        qp = _reload()
        assert qp._parse_memory_string("32Gi") == 32 * 1024**3

    def test_mi_suffix(self):
        qp = _reload()
        assert qp._parse_memory_string("256Mi") == 256 * 1024**2

    def test_ki_suffix(self):
        qp = _reload()
        assert qp._parse_memory_string("1024Ki") == 1024 * 1024

    def test_ti_suffix(self):
        qp = _reload()
        assert qp._parse_memory_string("1Ti") == 1024**4

    def test_decimal_g(self):
        qp = _reload()
        assert qp._parse_memory_string("2G") == 2 * 1000**3

    def test_bare_bytes(self):
        qp = _reload()
        assert qp._parse_memory_string("34359738368") == 34359738368

    def test_empty_returns_zero(self):
        qp = _reload()
        assert qp._parse_memory_string("") == 0


class TestCdkJsonEnvValues:
    """Regression tests pinning queue-processor module-level constants
    against the exact values shipped in cdk.json.

    cdk.json stores these as Kubernetes-style strings (e.g. "32Gi", "10")
    and injects them verbatim into the queue-processor pod env via the
    kubectl-applier manifest. The module MUST be able to parse them at
    import time, otherwise the pod crash-loops and no SQS messages are
    consumed. Do not regress the parsing helpers.
    """

    def test_cdk_default_memory_value_parses(self, monkeypatch):
        # Matches cdk.json::job_validation_policy.resource_quotas.max_memory_per_manifest
        monkeypatch.setenv("MAX_MEMORY_PER_MANIFEST", "32Gi")
        qp = _reload()
        assert qp.MAX_MEMORY == 32 * 1024**3

    def test_cdk_default_cpu_value_parses(self, monkeypatch):
        # Matches cdk.json::job_validation_policy.resource_quotas.max_cpu_per_manifest
        monkeypatch.setenv("MAX_CPU_PER_MANIFEST", "10")
        qp = _reload()
        # "10" means 10 cores → 10000 millicores
        assert qp.MAX_CPU == 10_000

    def test_cdk_default_gpu_value_parses(self, monkeypatch):
        monkeypatch.setenv("MAX_GPU_PER_MANIFEST", "4")
        qp = _reload()
        assert qp.MAX_GPU == 4

    def test_module_imports_with_cdk_defaults(self, monkeypatch):
        """Importing the module with the literal cdk.json defaults must
        not raise (this was the original crash)."""
        monkeypatch.setenv("MAX_CPU_PER_MANIFEST", "10")
        monkeypatch.setenv("MAX_MEMORY_PER_MANIFEST", "32Gi")
        monkeypatch.setenv("MAX_GPU_PER_MANIFEST", "4")
        # Should not raise ValueError
        qp = _reload()
        assert qp.MAX_CPU > 0
        assert qp.MAX_MEMORY > 0
        assert qp.MAX_GPU > 0

    def test_module_imports_with_various_sizes(self, monkeypatch):
        """Extended set of realistic operator-customizable values that
        appear in docs/RUNBOOKS.md, examples/README.md, etc."""
        for cpu, mem in [
            ("10", "32Gi"),  # cdk.json default
            ("32", "128Gi"),  # RUNBOOKS example
            ("96", "192Gi"),  # examples/README.md
            ("500m", "512Mi"),  # small workload
        ]:
            monkeypatch.setenv("MAX_CPU_PER_MANIFEST", cpu)
            monkeypatch.setenv("MAX_MEMORY_PER_MANIFEST", mem)
            qp = _reload()
            assert qp.MAX_CPU > 0, f"CPU {cpu!r} parsed to {qp.MAX_CPU}"
            assert qp.MAX_MEMORY > 0, f"Memory {mem!r} parsed to {qp.MAX_MEMORY}"


class TestCdkJsonContract:
    """Strict contract test: the values declared in cdk.json MUST be
    parseable by queue_processor at import time. This runs the actual
    cdk.json through the actual parser, catching drift between the two.
    """

    def test_cdk_json_queue_processor_defaults_parse(self, monkeypatch):
        """Resource caps now live under the shared job_validation_policy
        section — both the queue processor and manifest processor read them
        from there. Confirm the values that end up in the queue processor's
        env are still parseable."""
        import json
        from pathlib import Path

        cdk_json = json.loads((Path(__file__).resolve().parent.parent / "cdk.json").read_text())
        quotas = cdk_json["context"]["job_validation_policy"]["resource_quotas"]

        monkeypatch.setenv("MAX_CPU_PER_MANIFEST", str(quotas["max_cpu_per_manifest"]))
        monkeypatch.setenv("MAX_MEMORY_PER_MANIFEST", str(quotas["max_memory_per_manifest"]))
        monkeypatch.setenv("MAX_GPU_PER_MANIFEST", str(quotas["max_gpu_per_manifest"]))

        # Must not raise — this is the contract.
        qp = _reload()
        assert qp.MAX_CPU > 0
        assert qp.MAX_MEMORY > 0
        assert qp.MAX_GPU > 0

    def test_cdk_json_manifest_processor_defaults_parse(self, monkeypatch):
        """Same values via the same shared section — pinned separately so
        a future drift between MP_* and QP_* placeholder wiring gets
        caught here."""
        import json
        from pathlib import Path

        cdk_json = json.loads((Path(__file__).resolve().parent.parent / "cdk.json").read_text())
        quotas = cdk_json["context"]["job_validation_policy"]["resource_quotas"]

        qp = _reload()
        # These helpers should handle whatever operators configure.
        assert qp._parse_cpu_string(str(quotas["max_cpu_per_manifest"])) > 0
        assert qp._parse_memory_string(str(quotas["max_memory_per_manifest"])) > 0


# ── Security check parity with manifest_processor (regression tests) ─────
#
# The queue processor MUST enforce the same security checks as the REST
# manifest_processor service, otherwise submitters who produce SQS
# messages directly bypass the REST path's allowlists. These tests pin
# the parity.


def _job_with_image(image, namespace="gco-jobs"):
    """Build a minimal Job manifest carrying a given container image."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "img-test", "namespace": namespace},
        "spec": {
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "worker",
                            "image": image,
                            "securityContext": {"allowPrivilegeEscalation": False},
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                        }
                    ],
                }
            }
        },
    }


def _job_with_init_container(image, init_privileged=False, init_gpu=0):
    """Build a Job manifest with an init container so we can test that
    security checks cover init containers."""
    init_sc = {"allowPrivilegeEscalation": False}
    if init_privileged:
        init_sc["privileged"] = True
    init_container = {
        "name": "setup",
        "image": image,
        "securityContext": init_sc,
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
    }
    pod_spec: dict = {"restartPolicy": "Never", "initContainers": [init_container]}
    if init_gpu:
        init_container["resources"]["limits"] = {"nvidia.com/gpu": str(init_gpu)}
        # Carry a toleration so a GPU init container is judged by the resource
        # cap, not rejected first by the toleration requirement.
        pod_spec["tolerations"] = [
            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
        ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "init-test", "namespace": "gco-jobs"},
        "spec": {
            "template": {
                "spec": {
                    **pod_spec,
                    "containers": [
                        {
                            "name": "worker",
                            "image": "busybox:1.38.0",
                            "securityContext": {"allowPrivilegeEscalation": False},
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                        }
                    ],
                }
            }
        },
    }


class TestImageRegistryAllowlist:
    """The queue processor must reject images from registries outside the
    configured allowlist, matching manifest_processor._validate_image_sources."""

    def test_official_dockerhub_image_allowed(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_REGISTRIES", "nvcr.io,public.ecr.aws")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "nvidia,pytorch")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_image("busybox:1.38.0"))
        assert ok, err

    def test_trusted_registry_domain_allowed(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_REGISTRIES", "nvcr.io")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_image("nvcr.io/nvidia/pytorch:24.10-py3"))
        assert ok, err

    def test_trusted_dockerhub_org_allowed(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_REGISTRIES", "")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "nvidia")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_image("nvidia/cuda:12.4.1"))
        assert ok, err

    def test_untrusted_registry_rejected(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_REGISTRIES", "nvcr.io,public.ecr.aws")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "nvidia,pytorch")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_image("evil.example.com/malicious:latest"))
        assert not ok
        assert "untrusted image source" in err.lower()
        assert "evil.example.com" in err

    def test_untrusted_dockerhub_org_rejected(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_REGISTRIES", "")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "nvidia")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_image("attacker/payload:v1"))
        assert not ok
        assert "untrusted" in err.lower()

    def test_dependency_confusion_substring_rejected(self, monkeypatch):
        """'gco-malicious/evil' must NOT match trusted entry 'gco'."""
        monkeypatch.setenv("TRUSTED_REGISTRIES", "")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "gco")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_image("gco-malicious/evil:v1"))
        assert not ok, "'gco-malicious' must not be treated as trusted org 'gco'"

    def test_multilevel_registry_path_allowed(self, monkeypatch):
        """A trusted registry entry should also cover multi-level paths."""
        monkeypatch.setenv("TRUSTED_REGISTRIES", "public.ecr.aws")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_image("public.ecr.aws/lambda/python:3.14"))
        assert ok, err

    def test_empty_allowlists_use_secure_defaults(self, monkeypatch):
        """Missing deployment wiring must retain the REST processor defaults."""
        monkeypatch.setenv("TRUSTED_REGISTRIES", "")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_image("evil.example.com/malicious:latest"))
        assert not ok
        assert "untrusted" in err.lower()
        assert "nvcr.io" in qp.TRUSTED_REGISTRIES
        assert "nvidia" in qp.TRUSTED_DOCKERHUB_ORGS

    def test_init_container_image_rejected(self, monkeypatch):
        """initContainers carry real risk (they run with pod privileges
        before the main container) and must be subject to the same check."""
        monkeypatch.setenv("TRUSTED_REGISTRIES", "nvcr.io")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "nvidia")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_init_container("evil.example.com/bad:latest"))
        assert not ok
        assert "initcontainer" in err.lower()


class TestInitContainerSecurityChecks:
    """Privileged/escalation flags on init containers must be rejected
    just like on regular containers."""

    def test_privileged_init_container_rejected(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_REGISTRIES", "")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "")
        qp = _reload()
        ok, err = qp.validate_manifest(
            _job_with_init_container("busybox:1.38.0", init_privileged=True)
        )
        assert not ok
        assert "privileged" in err.lower()


class TestResourceAccountingMatchesManifestProcessor:
    """The queue_processor must sum resources across all container kinds,
    matching manifest_processor._validate_resource_limits exactly."""

    def test_init_container_cpu_counted_against_budget(self, monkeypatch):
        """A 20-core init container must be caught by a 10-core cap even
        though K8s's scheduler wouldn't treat it as additive."""
        monkeypatch.setenv("MAX_CPU_PER_MANIFEST", "10")  # 10 cores
        monkeypatch.setenv("MAX_MEMORY_PER_MANIFEST", "32Gi")
        monkeypatch.setenv("MAX_GPU_PER_MANIFEST", "4")
        monkeypatch.setenv("TRUSTED_REGISTRIES", "")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "")
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "bigcpu", "namespace": "gco-jobs"},
            "spec": {
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "initContainers": [
                            {
                                "name": "setup",
                                "image": "busybox:1.38.0",
                                "securityContext": {"allowPrivilegeEscalation": False},
                                "resources": {"requests": {"cpu": "20"}},  # 20 cores
                            }
                        ],
                        "containers": [
                            {
                                "name": "worker",
                                "image": "busybox:1.38.0",
                                "securityContext": {"allowPrivilegeEscalation": False},
                                "resources": {"requests": {"cpu": "100m"}},
                            }
                        ],
                    }
                }
            },
        }
        ok, err = qp.validate_manifest(m)
        assert not ok
        assert "CPU" in err

    def test_init_container_gpu_counted_against_budget(self, monkeypatch):
        monkeypatch.setenv("MAX_GPU_PER_MANIFEST", "4")
        monkeypatch.setenv("MAX_CPU_PER_MANIFEST", "10")
        monkeypatch.setenv("MAX_MEMORY_PER_MANIFEST", "32Gi")
        monkeypatch.setenv("TRUSTED_REGISTRIES", "")
        monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "")
        qp = _reload()
        ok, err = qp.validate_manifest(_job_with_init_container("busybox:1.38.0", init_gpu=99))
        assert not ok
        assert "GPU" in err


class TestQueueProcessorMirrorsManifestProcessor:
    """Structural test: the queue_processor must apply the same set of
    security checks as the manifest_processor service. If either side
    gains a new check, this test file needs a counterpart to keep the
    two in lockstep.
    """

    def test_all_listed_checks_are_implemented(self):
        """Sanity: the checks enumerated in the queue_processor docstring
        actually correspond to code paths that reject bad input."""
        qp = _reload()
        # Each of these inputs targets one of the documented checks; they
        # should all be rejected. If any is accepted, a check went missing.
        rejections = [
            # (1) namespace allowlist
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "p", "namespace": "kube-system"},
                "spec": {"containers": [{"name": "c", "image": "busybox"}]},
            },
            # (2) privileged container
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "p", "namespace": "gco-jobs"},
                "spec": {
                    "containers": [
                        {
                            "name": "c",
                            "image": "busybox",
                            "securityContext": {"privileged": True},
                        }
                    ]
                },
            },
        ]
        for manifest in rejections:
            ok, _err = qp.validate_manifest(manifest)
            assert not ok, f"manifest should have been rejected: {manifest}"


# ── SA token auto-mount injection parity (regression tests) ──────────────
#
# Bug history: The REST manifest_processor injects
# automountServiceAccountToken: false into every user-submitted pod spec
# via _inject_security_defaults(). The SQS queue_processor was missing
# the equivalent injection — jobs submitted via SQS got the
# ServiceAccount default of automount=true, which meant every pod could
# reach the K8s API via its projected SA token. Verified live on the
# deployed cluster by submitting a Job via SQS and checking for the
# token file inside the container.
#
# These tests pin that the SQS path now injects the same defaults as
# the REST path.


class TestExtractPodSpec:
    """queue_processor._extract_pod_spec must handle every workload shape
    the REST path handles (Deployment, Job, CronJob, Pod)."""

    def test_job(self):
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "j"},
            "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}},
        }
        ps = qp._extract_pod_spec(m)
        assert ps is not None
        assert ps["containers"][0]["name"] == "c"

    def test_deployment(self):
        qp = _reload()
        m = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "d"},
            "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}},
        }
        ps = qp._extract_pod_spec(m)
        assert ps is not None

    def test_cronjob(self):
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "cj"},
            "spec": {
                "schedule": "*/5 * * * *",
                "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "c"}]}}}},
            },
        }
        ps = qp._extract_pod_spec(m)
        assert ps is not None
        assert ps["containers"][0]["name"] == "c"

    def test_bare_pod(self):
        qp = _reload()
        m = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "p"},
            "spec": {"containers": [{"name": "c"}]},
        }
        ps = qp._extract_pod_spec(m)
        assert ps is not None

    def test_non_workload_returns_none(self):
        qp = _reload()
        m = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "cm"},
            "data": {"k": "v"},
        }
        assert qp._extract_pod_spec(m) is None


class TestInjectSecurityDefaults:
    """queue_processor._inject_security_defaults must set
    automountServiceAccountToken: false on user-submitted pod specs,
    matching the REST manifest_processor semantics."""

    def test_sets_automount_false_on_job(self):
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "j"},
            "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}},
        }
        qp._inject_security_defaults(m)
        assert m["spec"]["template"]["spec"]["automountServiceAccountToken"] is False

    def test_sets_automount_false_on_deployment(self):
        qp = _reload()
        m = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "d"},
            "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}},
        }
        qp._inject_security_defaults(m)
        assert m["spec"]["template"]["spec"]["automountServiceAccountToken"] is False

    def test_sets_automount_false_on_cronjob(self):
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "cj"},
            "spec": {
                "schedule": "*/5 * * * *",
                "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "c"}]}}}},
            },
        }
        qp._inject_security_defaults(m)
        ps = m["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert ps["automountServiceAccountToken"] is False

    def test_sets_automount_false_on_bare_pod(self):
        qp = _reload()
        m = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "p"},
            "spec": {"containers": [{"name": "c"}]},
        }
        qp._inject_security_defaults(m)
        assert m["spec"]["automountServiceAccountToken"] is False

    def test_does_not_override_explicit_true(self):
        """If the user explicitly opts in, we leave their choice alone.
        This matches the REST path's setdefault() semantics exactly."""
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "j"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "c"}],
                        "automountServiceAccountToken": True,
                    }
                }
            },
        }
        qp._inject_security_defaults(m)
        assert m["spec"]["template"]["spec"]["automountServiceAccountToken"] is True

    def test_does_not_override_explicit_false(self):
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "j"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "c"}],
                        "automountServiceAccountToken": False,
                    }
                }
            },
        }
        qp._inject_security_defaults(m)
        assert m["spec"]["template"]["spec"]["automountServiceAccountToken"] is False

    def test_non_workload_unchanged(self):
        import copy

        qp = _reload()
        m = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "cm"},
            "data": {"k": "v"},
        }
        original = copy.deepcopy(m)
        qp._inject_security_defaults(m)
        assert m == original


class TestApplyManifestInjectsDefaults:
    """The apply_manifest() entry point must call _inject_security_defaults
    so every SQS-submitted manifest has its SA-token auto-mount disabled
    before it reaches the K8s API."""

    def test_apply_manifest_injects_automount_false(self):
        """apply_manifest mutates the manifest in-place before applying."""
        qp = _reload()
        m = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "j", "namespace": "gco-jobs"},
            "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "busybox"}]}}},
        }

        # Patch the dynamic client so apply doesn't actually hit K8s.
        with pytest.MonkeyPatch.context() as mp:
            mock_dyn = MagicMock()
            mock_resource = MagicMock()
            mock_resource.create.return_value = None
            mock_dyn.resources.get.return_value = mock_resource
            mp.setattr("gco.services.queue_processor.dynamic.DynamicClient", lambda _: mock_dyn)
            mp.setattr("gco.services.queue_processor.client.ApiClient", lambda: MagicMock())
            # get() raises NotFound so apply_manifest takes the create path
            from kubernetes.client.rest import ApiException

            mock_resource.get.side_effect = ApiException(status=404)

            qp.apply_manifest(m)

        # The manifest was mutated in-place — automount is now False
        assert m["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


# ── Security policy toggles ──────────────────────────────────────────────
#
# Parity with tests/test_security_policy_toggles.py for the REST
# manifest_processor. Every toggle that exists in
# cdk.json::job_validation_policy.manifest_security_policy must be enforced on
# BOTH submission paths — otherwise an attacker holding sqs:SendMessage
# could bypass checks by routing through the SQS path.


def _cronjob_manifest(pod_spec_overrides=None, container_overrides=None):
    """Build a CronJob manifest for policy testing."""
    container = {
        "name": "worker",
        "image": "python:3.14-slim",
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
    }
    if container_overrides:
        container.update(container_overrides)
    pod_spec = {"restartPolicy": "Never", "containers": [container]}
    if pod_spec_overrides:
        pod_spec.update(pod_spec_overrides)
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "cj", "namespace": "gco-jobs"},
        "spec": {
            "schedule": "0 * * * *",
            "jobTemplate": {"spec": {"template": {"spec": pod_spec}}},
        },
    }


def _pod_manifest(pod_spec_overrides=None, container_overrides=None):
    """Build a bare Pod manifest for policy testing."""
    container = {
        "name": "worker",
        "image": "python:3.14-slim",
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
    }
    if container_overrides:
        container.update(container_overrides)
    pod_spec = {"restartPolicy": "Never", "containers": [container]}
    if pod_spec_overrides:
        pod_spec.update(pod_spec_overrides)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "p", "namespace": "gco-jobs"},
        "spec": pod_spec,
    }


class TestBlockHostNetworkToggle:
    """BLOCK_HOST_NETWORK env var toggle (default: true)."""

    def test_host_network_rejected_by_default(self):
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"hostNetwork": True})
        ok, r = qp.validate_manifest(m)
        assert not ok and "hostNetwork" in r

    def test_host_network_accepted_when_disabled(self, monkeypatch):
        monkeypatch.setenv("BLOCK_HOST_NETWORK", "false")
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"hostNetwork": True})
        assert qp.validate_manifest(m)[0] is True


class TestBlockHostPidToggle:
    """BLOCK_HOST_PID env var toggle (default: true)."""

    def test_host_pid_rejected_by_default(self):
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"hostPID": True})
        ok, r = qp.validate_manifest(m)
        assert not ok and "hostPID" in r

    def test_host_pid_accepted_when_disabled(self, monkeypatch):
        monkeypatch.setenv("BLOCK_HOST_PID", "false")
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"hostPID": True})
        assert qp.validate_manifest(m)[0] is True


class TestBlockHostIpcToggle:
    """BLOCK_HOST_IPC env var toggle (default: true)."""

    def test_host_ipc_rejected_by_default(self):
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"hostIPC": True})
        ok, r = qp.validate_manifest(m)
        assert not ok and "hostIPC" in r

    def test_host_ipc_accepted_when_disabled(self, monkeypatch):
        monkeypatch.setenv("BLOCK_HOST_IPC", "false")
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"hostIPC": True})
        assert qp.validate_manifest(m)[0] is True


class TestBlockHostPathToggle:
    """BLOCK_HOST_PATH env var toggle (default: true)."""

    def test_host_path_rejected_by_default(self):
        qp = _reload()
        m = _pod_manifest(
            pod_spec_overrides={
                "volumes": [{"name": "data", "hostPath": {"path": "/etc"}}],
            }
        )
        ok, r = qp.validate_manifest(m)
        assert not ok and "hostPath" in r

    def test_host_path_accepted_when_disabled(self, monkeypatch):
        monkeypatch.setenv("BLOCK_HOST_PATH", "false")
        qp = _reload()
        m = _pod_manifest(
            pod_spec_overrides={
                "volumes": [{"name": "data", "hostPath": {"path": "/etc"}}],
            }
        )
        assert qp.validate_manifest(m)[0] is True


class TestBlockAddedCapabilitiesToggle:
    """BLOCK_ADDED_CAPABILITIES env var toggle (default: true)."""

    def test_capabilities_add_rejected_by_default(self):
        qp = _reload()
        m = _pod_manifest(
            container_overrides={
                "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
            }
        )
        ok, r = qp.validate_manifest(m)
        assert not ok and "capabilities" in r.lower()

    def test_capabilities_add_accepted_when_disabled(self, monkeypatch):
        monkeypatch.setenv("BLOCK_ADDED_CAPABILITIES", "false")
        qp = _reload()
        m = _pod_manifest(
            container_overrides={
                "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
            }
        )
        assert qp.validate_manifest(m)[0] is True


class TestBlockRunAsRootToggle:
    """BLOCK_RUN_AS_ROOT env var toggle (default: false).

    Matches manifest_processor default so GPU/ML containers that need
    root (e.g. conda-based PyTorch images) continue to work.
    """

    def test_run_as_root_allowed_by_default(self):
        qp = _reload()
        m = _pod_manifest(container_overrides={"securityContext": {"runAsUser": 0}})
        assert qp.validate_manifest(m)[0] is True

    def test_run_as_root_rejected_when_enabled_on_container(self, monkeypatch):
        monkeypatch.setenv("BLOCK_RUN_AS_ROOT", "true")
        qp = _reload()
        m = _pod_manifest(container_overrides={"securityContext": {"runAsUser": 0}})
        ok, r = qp.validate_manifest(m)
        assert not ok and "runAsUser" in r

    def test_run_as_root_rejected_when_enabled_on_pod(self, monkeypatch):
        monkeypatch.setenv("BLOCK_RUN_AS_ROOT", "true")
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"securityContext": {"runAsUser": 0}})
        ok, r = qp.validate_manifest(m)
        assert not ok and "runAsUser" in r


class TestBlockPrivilegedPodSecurityContextToggle:
    """Pod-level privileged security context (separate from per-container)."""

    def test_pod_privileged_rejected_by_default(self):
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"securityContext": {"privileged": True}})
        ok, r = qp.validate_manifest(m)
        assert not ok and "privileged" in r.lower()

    def test_pod_privileged_accepted_when_disabled(self, monkeypatch):
        monkeypatch.setenv("BLOCK_PRIVILEGED", "false")
        qp = _reload()
        m = _pod_manifest(pod_spec_overrides={"securityContext": {"privileged": True}})
        assert qp.validate_manifest(m)[0] is True


class TestSecurityPolicyInitContainerCoverage:
    """Every toggle iterates init + ephemeral containers too, not just main.

    Regression pin: a bypass attempt that smuggles a privileged init
    container must be rejected just as rigorously as the main container.
    """

    def test_init_container_privileged_blocked(self):
        qp = _reload()
        m = _pod_manifest()
        m["spec"]["initContainers"] = [
            {
                "name": "setup",
                "image": "busybox:1.38.0",
                "securityContext": {"privileged": True},
            }
        ]
        ok, r = qp.validate_manifest(m)
        assert not ok and "privileged" in r.lower()

    def test_init_container_capabilities_blocked(self):
        qp = _reload()
        m = _pod_manifest()
        m["spec"]["initContainers"] = [
            {
                "name": "setup",
                "image": "busybox:1.38.0",
                "securityContext": {"capabilities": {"add": ["SYS_ADMIN"]}},
            }
        ]
        ok, r = qp.validate_manifest(m)
        assert not ok and "capabilities" in r.lower()


class TestCronJobSecurityPolicyChecks:
    """Security policy checks fire on CronJob pod specs too."""

    def test_cronjob_host_network_blocked(self):
        qp = _reload()
        m = _cronjob_manifest(pod_spec_overrides={"hostNetwork": True})
        ok, r = qp.validate_manifest(m)
        assert not ok and "hostNetwork" in r

    def test_cronjob_host_path_volume_blocked(self):
        qp = _reload()
        m = _cronjob_manifest(
            pod_spec_overrides={
                "volumes": [{"name": "data", "hostPath": {"path": "/etc"}}],
            }
        )
        ok, r = qp.validate_manifest(m)
        assert not ok and "hostPath" in r


class TestEnvBoolParser:
    """Sanity checks for the shared fail-closed env-var boolean parser."""

    def test_env_bool_true_variants(self, monkeypatch):
        qp = _reload()
        for value in ("true", "True", "TRUE", "1", "yes", "on"):
            monkeypatch.setenv("X", value)
            assert qp.parse_boolean_environment("X", False) is True
        monkeypatch.delenv("X")
        assert qp.parse_boolean_environment("X", False) is False

    def test_env_bool_false_variants(self, monkeypatch):
        qp = _reload()
        for value in ("false", "False", "0", "no", "off"):
            monkeypatch.setenv("X", value)
            assert qp.parse_boolean_environment("X", True) is False
        for value in ("", "   "):
            monkeypatch.setenv("X", value)
            assert qp.parse_boolean_environment("X", True) is True

    @pytest.mark.parametrize("value", ("treu", "2", "${UNRESOLVED_BOOLEAN}"))
    def test_env_bool_rejects_malformed_values(self, monkeypatch, value):
        qp = _reload()
        monkeypatch.setenv("X", value)
        with pytest.raises(ValueError, match="X must be an explicit boolean value"):
            qp.parse_boolean_environment("X", True)

    @pytest.mark.parametrize(
        "name",
        (
            "BLOCK_PRIVILEGED",
            "BLOCK_PRIVILEGE_ESCALATION",
            "BLOCK_HOST_NETWORK",
            "BLOCK_HOST_PID",
            "BLOCK_HOST_IPC",
            "BLOCK_HOST_PATH",
            "BLOCK_ADDED_CAPABILITIES",
            "BLOCK_RUN_AS_ROOT",
            "REQUIRE_ACCELERATOR_TOLERATION",
        ),
    )
    def test_malformed_boolean_rejects_queue_worker_startup(self, monkeypatch, name):
        """Every queue-worker admission toggle fails closed during import."""
        monkeypatch.setenv(name, "${UNRESOLVED_BOOLEAN}")
        with pytest.raises(ValueError, match=rf"{name} must be an explicit boolean value"):
            _reload()


class TestSecurityPolicyParityWithManifestProcessor:
    """The queue_processor must mirror EVERY toggle the manifest_processor
    exposes. This prevents drift when a new toggle is added to one side
    but forgotten on the other — the SQS path would silently accept
    submissions the REST path rejects (or vice versa).
    """

    # Keep this list aligned with
    # gco/services/manifest_processor.py::ManifestProcessor.__init__
    # and with cdk.json::job_validation_policy.manifest_security_policy.
    EXPECTED_TOGGLES = {
        "BLOCK_PRIVILEGED",
        "BLOCK_PRIVILEGE_ESCALATION",
        "BLOCK_HOST_NETWORK",
        "BLOCK_HOST_PID",
        "BLOCK_HOST_IPC",
        "BLOCK_HOST_PATH",
        "BLOCK_ADDED_CAPABILITIES",
        "BLOCK_RUN_AS_ROOT",
    }

    def test_queue_processor_exposes_every_expected_toggle(self):
        """Every toggle listed in EXPECTED_TOGGLES must exist as a
        module-level constant in queue_processor so the KEDA ScaledJob
        manifest has something to populate."""
        qp = _reload()
        missing = {name for name in self.EXPECTED_TOGGLES if not hasattr(qp, name)}
        assert not missing, (
            f"queue_processor is missing security-policy toggles {missing}. "
            "Add them to queue_processor.py and wire them in "
            "regional_stack.py + post-helm-sqs-consumer.yaml."
        )

    def test_manifest_processor_has_matching_attribute_for_each_toggle(self):
        """Every toggle the queue_processor exposes must have a matching
        attribute on the manifest_processor, so a config flip on one side
        also flips the other.

        Structural check: we scan the manifest_processor source for the
        ``self.block_xxx = security_policy.get(...)`` pattern rather than
        instantiating the processor, to keep the test fast and free of
        k8s client mocks.
        """
        import re
        from pathlib import Path

        mp_src = Path("gco/services/manifest_processor.py").read_text()

        # Extract every 'self.block_xxx = security_policy.get("block_xxx", ...)'
        # attribute name from the manifest_processor source.
        pattern = re.compile(r'self\.(block_[a-z_]+)\s*=\s*security_policy\.get\("(block_[a-z_]+)"')
        mp_toggles = set()
        for m in pattern.finditer(mp_src):
            # Sanity-check the attribute name matches the dict key.
            assert m.group(1) == m.group(2), (
                f"manifest_processor attribute {m.group(1)} doesn't match "
                f"cdk.json key {m.group(2)!r}"
            )
            mp_toggles.add(m.group(1).upper())

        assert mp_toggles == self.EXPECTED_TOGGLES, (
            f"manifest_processor toggles {mp_toggles} differ from "
            f"EXPECTED_TOGGLES {self.EXPECTED_TOGGLES}. If a new toggle was "
            "added to manifest_processor, add it to queue_processor.py, "
            "regional_stack.py, post-helm-sqs-consumer.yaml, and this test."
        )


# ── TrainJob validation (SQS path) ───────────────────────────────────────


def _trainjob(
    name="test-trainjob",
    namespace="gco-jobs",
    image="pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime",
    num_nodes=None,
    resources_per_node=None,
    runtime_patches=None,
):
    """Build a Kubeflow Trainer v2 TrainJob manifest."""
    trainer = {}
    if image is not None:
        trainer["image"] = image
    if num_nodes is not None:
        trainer["numNodes"] = num_nodes
    if resources_per_node is not None:
        trainer["resourcesPerNode"] = resources_per_node
    spec = {"runtimeRef": {"name": "torch-distributed"}}
    if trainer:
        spec["trainer"] = trainer
    if runtime_patches is not None:
        spec["runtimePatches"] = runtime_patches
    return {
        "apiVersion": "trainer.kubeflow.org/v1alpha1",
        "kind": "TrainJob",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def _patch_with_pod_spec(pod_spec):
    """Wrap a pod spec in the runtimePatches nesting the trainer API uses."""
    return {
        "trainingRuntimeSpec": {
            "template": {
                "spec": {
                    "replicatedJobs": [
                        {"name": "node", "template": {"spec": {"template": {"spec": pod_spec}}}}
                    ]
                }
            }
        }
    }


_GPU_TOLERATION_PATCH = _patch_with_pod_spec(
    {
        "containers": [],
        "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
    }
)


class TestTrainJobValidation:
    """The SQS path applies the full TrainJob decomposition, like the REST path.

    A TrainJob has no spec.template, so before the decomposition every check
    here passed vacuously — these tests pin the SQS-side closure of that hole.
    """

    def test_trusted_trainjob_accepted(self):
        qp = _reload()
        ok, reason = qp.validate_manifest(
            _trainjob(resources_per_node={"requests": {"cpu": "500m", "memory": "512Mi"}})
        )
        assert (ok, reason) == (True, "")

    def test_trainjob_kind_allowed_by_default(self):
        qp = _reload()
        assert "TrainJob" in qp.ALLOWED_KINDS

    def test_untrusted_trainer_image_rejected(self):
        qp = _reload()
        ok, reason = qp.validate_manifest(_trainjob(image="evil.example.com/malicious:latest"))
        assert not ok
        assert "untrusted image source" in reason.lower()
        assert "evil.example.com" in reason

    def test_untrusted_runtime_patch_image_rejected(self):
        qp = _reload()
        ok, reason = qp.validate_manifest(
            _trainjob(
                runtime_patches=[
                    _patch_with_pod_spec(
                        {"containers": [{"name": "smuggled", "image": "evil.example.com/bd:1"}]}
                    )
                ],
            )
        )
        assert not ok
        assert "evil.example.com" in reason

    def test_hostpath_in_runtime_patch_rejected(self):
        qp = _reload()
        ok, reason = qp.validate_manifest(
            _trainjob(
                runtime_patches=[
                    _patch_with_pod_spec(
                        {
                            "containers": [{"name": "worker", "image": "python:3.14-slim"}],
                            "volumes": [{"name": "host", "hostPath": {"path": "/etc"}}],
                        }
                    )
                ],
            )
        )
        assert not ok
        assert "hostPath" in reason

    def test_privileged_in_runtime_patch_rejected(self):
        qp = _reload()
        ok, reason = qp.validate_manifest(
            _trainjob(
                runtime_patches=[
                    _patch_with_pod_spec(
                        {
                            "containers": [
                                {
                                    "name": "worker",
                                    "image": "python:3.14-slim",
                                    "securityContext": {"privileged": True},
                                }
                            ]
                        }
                    )
                ],
            )
        )
        assert not ok
        assert "privileged" in reason

    def test_resource_caps_scale_with_num_nodes(self):
        """MAX_GPU_PER_MANIFEST=4 (autouse fixture): 2x1 passes, 8x1 must not."""
        qp = _reload()

        ok, reason = qp.validate_manifest(
            _trainjob(
                num_nodes=2,
                resources_per_node={"limits": {"nvidia.com/gpu": "1"}},
                runtime_patches=[_GPU_TOLERATION_PATCH],
            )
        )
        assert ok, reason

        ok, reason = qp.validate_manifest(
            _trainjob(
                num_nodes=8,
                resources_per_node={"limits": {"nvidia.com/gpu": "1"}},
                runtime_patches=[_GPU_TOLERATION_PATCH],
            )
        )
        assert not ok
        assert "GPU 8 exceeds max 4" in reason

    def test_gpu_without_toleration_rejected_with_trainjob_hint(self):
        qp = _reload()
        ok, reason = qp.validate_manifest(
            _trainjob(num_nodes=2, resources_per_node={"limits": {"nvidia.com/gpu": "1"}})
        )
        assert not ok
        assert "toleration" in reason
        assert "kubeflow-trainjob.yaml" in reason

    def test_toleration_in_runtime_patch_satisfies_trainer_gpu_request(self):
        """Tolerations union across pod-spec views: request in spec.trainer,
        toleration in a runtimePatches pod spec."""
        qp = _reload()
        ok, reason = qp.validate_manifest(
            _trainjob(
                num_nodes=1,
                resources_per_node={"limits": {"nvidia.com/gpu": "1"}},
                runtime_patches=[_GPU_TOLERATION_PATCH],
            )
        )
        assert ok, reason

    def test_inject_security_defaults_reaches_embedded_pod_specs(self):
        qp = _reload()
        manifest = _trainjob(
            runtime_patches=[
                _patch_with_pod_spec(
                    {"containers": [{"name": "worker", "image": "python:3.14-slim"}]}
                )
            ],
        )
        qp._inject_security_defaults(manifest)
        embedded = manifest["spec"]["runtimePatches"][0]["trainingRuntimeSpec"]["template"]["spec"][
            "replicatedJobs"
        ][0]["template"]["spec"]["template"]["spec"]
        assert embedded["automountServiceAccountToken"] is False
        assert "automountServiceAccountToken" not in manifest["spec"]["trainer"]

    def test_apply_manifest_missing_addon_hint(self):
        """ResourceNotFoundError for TrainJob keeps the stable DLQ prefix and
        appends the enable-the-addon remedy."""
        from kubernetes.dynamic.exceptions import ResourceNotFoundError

        qp = _reload()
        qp.client = MagicMock()
        mock_dyn = MagicMock()
        mock_dyn.return_value.resources.get.side_effect = ResourceNotFoundError("no CRD")
        qp.dynamic = MagicMock()
        qp.dynamic.DynamicClient = mock_dyn
        result = qp.apply_manifest(_trainjob())
        assert result.status == "failed"
        assert "Unsupported Kubernetes resource" in (result.message or "")
        assert "kubeflow-trainer addon" in (result.message or "")
