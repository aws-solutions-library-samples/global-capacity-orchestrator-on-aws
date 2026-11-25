"""Placing Jobs on the keep-warm GPU pool: ``--keep-warm`` and the admission check.

``gco.job_admission.apply_keep_warm`` is what every submit command's
``--keep-warm`` runs: it adds the ``gco.aws/keep-warm`` node selector and the
toleration for the pool's taint, and refuses what could not use the pool
before any request is made. ``check_keep_warm`` holds a manifest written by
hand to the same rules on the REST and SQS paths and in the CLI's policy
pre-check.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from gco import job_admission as ja
from gco.keep_warm import KEEP_WARM_LABEL, KEEP_WARM_TOLERATION

GPU_TOLERATION = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}


def _pod_spec(*, gpu: bool = True, **extra: Any) -> dict[str, Any]:
    container: dict[str, Any] = {"name": "trial", "image": "busybox:1.38.0"}
    if gpu:
        container["resources"] = {"limits": {"nvidia.com/gpu": 1}}
    spec: dict[str, Any] = {"containers": [container], "restartPolicy": "Never"}
    if gpu:
        spec["tolerations"] = [dict(GPU_TOLERATION)]
    spec.update(extra)
    return spec


def _job(name: str = "trial", **pod: Any) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": "gco-jobs"},
        "spec": {"template": {"spec": _pod_spec(**pod)}},
    }


def _selected(**pod: Any) -> dict[str, Any]:
    """A Job that selects the keep-warm pool by hand, with its toleration."""
    pod.setdefault("nodeSelector", {KEEP_WARM_LABEL: "true"})
    manifest = _job(**pod)
    manifest["spec"]["template"]["spec"]["tolerations"].append(dict(KEEP_WARM_TOLERATION))
    return manifest


def _template(manifest: dict[str, Any]) -> dict[str, Any]:
    _, pod_spec = ja.pod_template(manifest)
    assert pod_spec is not None
    return pod_spec


class TestApplyKeepWarm:
    def test_adds_the_selector_and_toleration_to_each_job(self) -> None:
        manifests = [_job("a"), _job("b")]

        ja.apply_keep_warm(manifests)

        for manifest in manifests:
            pod_spec = _template(manifest)
            assert pod_spec["nodeSelector"] == {KEEP_WARM_LABEL: "true"}
            assert pod_spec["tolerations"] == [GPU_TOLERATION, KEEP_WARM_TOLERATION]

    def test_keeps_other_selectors_and_an_existing_toleration(self) -> None:
        manifest = _job(nodeSelector={"node.kubernetes.io/instance-type": "g6.xlarge"})
        _template(manifest)["tolerations"].append(
            {"key": KEEP_WARM_LABEL, "operator": "Exists", "effect": "NoSchedule"}
        )

        ja.apply_keep_warm([manifest])

        pod_spec = _template(manifest)
        assert pod_spec["nodeSelector"] == {
            "node.kubernetes.io/instance-type": "g6.xlarge",
            KEEP_WARM_LABEL: "true",
        }
        assert len(pod_spec["tolerations"]) == 2

    def test_matching_pool_and_architecture_selectors_are_allowed(self) -> None:
        manifest = _job(
            nodeSelector={
                "karpenter.sh/nodepool": "gpu-keep-warm-pool",
                "kubernetes.io/arch": "amd64",
                KEEP_WARM_LABEL: "true",
            }
        )
        ja.apply_keep_warm([manifest])
        assert _template(manifest)["nodeSelector"][KEEP_WARM_LABEL] == "true"

    def test_cronjobs_and_pods_are_placed_and_podless_kinds_pass_through(self) -> None:
        cronjob = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "nightly"},
            "spec": {"jobTemplate": {"spec": {"template": {"spec": _pod_spec()}}}},
        }
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p"}, "spec": _pod_spec()}
        config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}}

        ja.apply_keep_warm([config_map, cronjob, pod])

        assert _template(cronjob)["nodeSelector"] == {KEEP_WARM_LABEL: "true"}
        assert pod["spec"]["nodeSelector"] == {KEEP_WARM_LABEL: "true"}
        assert "spec" not in config_map

    @pytest.mark.parametrize(
        ("selector", "message"),
        [
            ({"karpenter.sh/nodepool": "gpu-x86-pool"}, "karpenter.sh/nodepool=gpu-x86-pool"),
            ({"kubernetes.io/arch": "arm64"}, "kubernetes.io/arch=arm64"),
            ({KEEP_WARM_LABEL: "false"}, f"{KEEP_WARM_LABEL}=false"),
            ({"workload-type": "inference"}, "workload-type=inference"),
            ({"eks.amazonaws.com/instance-family": "p5"}, "eks.amazonaws.com/instance-family=p5"),
            (
                {"node.kubernetes.io/instance-type": "p5.48xlarge"},
                "node.kubernetes.io/instance-type=p5.48xlarge",
            ),
            ({"gco.aws/capacity-provider": "hyperpod"}, "gco.aws/capacity-provider=hyperpod"),
        ],
        ids=[
            "other-pool",
            "arm64",
            "label-false",
            "workload-type",
            "instance-family",
            "instance-type",
            "hyperpod-selector",
        ],
    )
    def test_a_conflicting_selector_is_refused(
        self, selector: dict[str, str], message: str
    ) -> None:
        with pytest.raises(
            ValueError, match=f"Job trial selects {message}, which no keep-warm node carries"
        ):
            ja.apply_keep_warm([_job(nodeSelector=selector)])

    def test_every_conflict_is_named(self) -> None:
        selector = {"workload-type": "inference", "kubernetes.io/arch": "arm64"}
        with pytest.raises(
            ValueError, match=r"selects kubernetes\.io/arch=arm64, workload-type=inference, which"
        ):
            ja.apply_keep_warm([_job(nodeSelector=selector)])

    def test_labels_the_pool_does_not_fix_are_not_judged(self) -> None:
        manifest = _job(
            nodeSelector={
                "topology.kubernetes.io/zone": "us-east-1a",
                "eks.amazonaws.com/instance-family": "g6e",
                "karpenter.sh/capacity-type": "spot",
                "workload-type": "gpu",
            }
        )
        ja.apply_keep_warm([manifest])
        assert _template(manifest)["nodeSelector"][KEEP_WARM_LABEL] == "true"

    def test_a_job_without_a_gpu_request_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"requests no nvidia\.com/gpu"):
            ja.apply_keep_warm([_job(gpu=False)])

    @pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "DaemonSet", "TrainJob"])
    def test_other_kinds_are_refused(self, kind: str) -> None:
        manifest = _job()
        manifest["kind"] = kind
        with pytest.raises(ValueError, match=f"{kind} trial: --keep-warm keeps a node"):
            ja.apply_keep_warm([manifest])

    def test_a_job_without_a_pod_template_is_refused(self) -> None:
        manifest = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {}, "spec": {}}
        with pytest.raises(ValueError, match=r"Job \?: --keep-warm keeps a node"):
            ja.apply_keep_warm([manifest])

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("nodeSelector", ["x"], "nodeSelector must be a mapping"),
            ("tolerations", {"key": "x"}, "tolerations must be a list"),
        ],
    )
    def test_malformed_scheduling_fields_are_refused(
        self, field: str, value: Any, message: str
    ) -> None:
        manifest = _job()
        _template(manifest)[field] = value
        with pytest.raises(ValueError, match=message):
            ja.apply_keep_warm([manifest])

    def test_a_submission_with_nothing_to_place_is_refused(self) -> None:
        with pytest.raises(ValueError, match="--keep-warm found nothing to place"):
            ja.apply_keep_warm([{"apiVersion": "v1", "kind": "Service", "metadata": {}}])


class TestCheckKeepWarm:
    def test_a_manifest_that_does_not_select_the_pool_is_not_judged(self) -> None:
        assert ja.check_keep_warm(_job(gpu=False)) == (True, None)

    def test_a_correct_selection_passes(self) -> None:
        assert ja.check_keep_warm(_selected()) == (True, None)

    def test_tolerating_the_taint_without_selecting_the_label_is_not_selecting(self) -> None:
        manifest = _job(gpu=False, tolerations=[{"operator": "Exists"}])
        assert ja.check_keep_warm(manifest) == (True, None)

    def test_selection_without_the_toleration_is_refused(self) -> None:
        ok, message = ja.check_keep_warm(_job(nodeSelector={KEEP_WARM_LABEL: "true"}))
        assert ok is False
        assert "without a toleration for the gco.aws/keep-warm=true:NoSchedule taint" in str(
            message
        )

    def test_selection_without_a_gpu_is_refused(self) -> None:
        manifest = _selected()
        _template(manifest)["containers"][0].pop("resources")
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "requests no nvidia.com/gpu" in str(message)

    @pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
    def test_long_running_kinds_are_refused(self, kind: str) -> None:
        manifest = _selected()
        manifest["kind"] = kind
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "its pods never finish" in str(message)

    def test_a_daemonset_may_select_the_pool_without_a_gpu(self) -> None:
        manifest = _selected()
        manifest["kind"] = "DaemonSet"
        _template(manifest)["containers"][0].pop("resources")
        assert ja.check_keep_warm(manifest) == (True, None)

    def test_a_trainjob_runtime_patch_that_selects_the_pool_is_judged(self) -> None:
        manifest = {
            "apiVersion": ja.TRAINJOB_API_VERSION,
            "kind": "TrainJob",
            "metadata": {"name": "tj"},
            "spec": {
                "trainer": {"image": "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime"},
                "runtimePatches": [
                    {
                        "manager": "gco",
                        "trainingRuntimeSpec": {
                            "template": {
                                "spec": {
                                    "replicatedJobs": [
                                        {
                                            "name": "node",
                                            "template": {
                                                "spec": {
                                                    "template": {
                                                        "spec": {
                                                            "nodeSelector": {
                                                                KEEP_WARM_LABEL: "true"
                                                            },
                                                            "tolerations": [dict(GPU_TOLERATION)],
                                                        }
                                                    }
                                                }
                                            },
                                        }
                                    ]
                                }
                            }
                        },
                    }
                ],
            },
        }
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "TrainJob tj selects gco.aws/keep-warm=true without a toleration" in str(message)

    def test_a_trainjob_that_selects_the_pool_correctly_passes(self) -> None:
        manifest = {
            "apiVersion": ja.TRAINJOB_API_VERSION,
            "kind": "TrainJob",
            "metadata": {"name": "tj"},
            "spec": {
                "trainer": {
                    "image": "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime",
                    "resourcesPerNode": {"limits": {"nvidia.com/gpu": 1}},
                },
                "runtimePatches": [
                    {
                        "manager": "gco",
                        "trainingRuntimeSpec": {
                            "template": {
                                "spec": {
                                    "replicatedJobs": [
                                        {
                                            "name": "node",
                                            "template": {
                                                "spec": {
                                                    "template": {
                                                        "spec": {
                                                            "nodeSelector": {
                                                                KEEP_WARM_LABEL: "true"
                                                            },
                                                            "tolerations": [
                                                                dict(GPU_TOLERATION),
                                                                dict(KEEP_WARM_TOLERATION),
                                                            ],
                                                        }
                                                    }
                                                }
                                            },
                                        }
                                    ]
                                }
                            }
                        },
                    }
                ],
            },
        }
        assert ja.check_keep_warm(manifest) == (True, None)

    def test_a_non_list_toleration_field_counts_as_none(self) -> None:
        manifest = _job(nodeSelector={KEEP_WARM_LABEL: "true"})
        _template(manifest)["tolerations"] = "junk"
        ok, _ = ja.check_keep_warm(manifest)
        assert ok is False

    @pytest.mark.parametrize(
        "extra",
        [
            {"workload-type": "inference"},
            {"node.kubernetes.io/instance-type": "g5g.xlarge"},
            {"gco.aws/capacity-provider": "hyperpod"},
        ],
    )
    def test_a_selection_no_keep_warm_node_matches_is_refused(self, extra: dict[str, str]) -> None:
        ((key, value),) = extra.items()
        ok, message = ja.check_keep_warm(_selected(nodeSelector={KEEP_WARM_LABEL: "true", **extra}))
        assert ok is False
        assert f"selects gco.aws/keep-warm=true and {key}={value}, which no keep-warm node" in str(
            message
        )

    def test_the_hyperpod_queue_label_is_refused(self) -> None:
        manifest = _selected()
        manifest["metadata"]["labels"] = {ja.KUEUE_QUEUE_NAME_LABEL: "gco-hyperpod"}
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "whose flavor sends its pods to SageMaker HyperPod nodes" in str(message)

    def test_the_default_kueue_queue_label_is_allowed(self) -> None:
        manifest = _selected()
        manifest["metadata"]["labels"] = {ja.KUEUE_QUEUE_NAME_LABEL: "gco-default"}
        assert ja.check_keep_warm(manifest) == (True, None)


class TestSubmissionPathParity:
    """The REST service and the SQS processor give a keep-warm manifest the same verdict."""

    CASES = [
        (_selected(), True),
        (_job(nodeSelector={KEEP_WARM_LABEL: "true"}), False),
        (_job(), True),
    ]

    @pytest.mark.parametrize(("manifest", "expected"), CASES)
    def test_rest_and_sqs_agree(
        self, manifest: dict[str, Any], expected: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gco.services import queue_processor
        from gco.services.manifest_processor import ManifestProcessor

        processor = ManifestProcessor.__new__(ManifestProcessor)
        processor.validation_enabled = True
        processor.yaml_max_depth = 50
        processor.allowed_namespaces = {"gco-jobs"}
        processor.allowed_kinds = set(ja.DEFAULT_ALLOWED_KINDS)
        processor.require_accelerator_toleration = False
        processor._validate_resource_limits = lambda m: (True, "")  # type: ignore[method-assign]
        processor._validate_security_context = lambda m: (True, None)  # type: ignore[method-assign]
        processor._validate_image_sources = lambda m: (True, None)  # type: ignore[method-assign]
        processor.runtime_class_exists = lambda name: True
        monkeypatch.setattr(queue_processor, "runtime_class_exists", lambda name: True)
        monkeypatch.setattr(queue_processor, "REQUIRE_ACCELERATOR_TOLERATION", False)

        rest_ok, rest_message = processor.validate_manifest(copy.deepcopy(manifest))
        sqs_ok, sqs_message = queue_processor.validate_manifest(copy.deepcopy(manifest))

        assert rest_ok is sqs_ok is expected
        if not expected:
            assert str(rest_message).startswith("Keep-warm validation failed: ")
            assert rest_message == sqs_message


class TestPolicyPreCheck:
    def test_a_selection_without_the_toleration_is_a_finding(self) -> None:
        from cli.job_policy import CHECK_KEEP_WARM, evaluate_manifest

        policy = ja.JobValidationPolicy.from_policy_document(
            {
                "manifest_caps": {
                    "max_cpu_millicores": 64_000,
                    "max_memory_bytes": 512 * 2**30,
                    "max_gpu_count": 8,
                },
                "allowed_namespaces": ["gco-jobs"],
                "allowed_kinds": list(ja.DEFAULT_ALLOWED_KINDS),
                "trusted_registries": ["docker.io"],
                "require_accelerator_toleration": True,
            }
        )
        issues = evaluate_manifest(_job(nodeSelector={KEEP_WARM_LABEL: "true"}), policy)
        assert [issue.check for issue in issues] == [CHECK_KEEP_WARM]


# ---------------------------------------------------------------------------
# --keep-warm on the CLI
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, *documents: dict[str, Any]) -> str:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump_all(documents), encoding="utf-8")
    return str(path)


def _job_manager() -> Any:
    from cli.jobs import JobManager

    manager = JobManager.__new__(JobManager)
    manager.config = MagicMock(default_namespace="gco-jobs")
    manager._aws_client = MagicMock()
    return manager


class TestJobManagerKeepWarm:
    def test_api_submission_sends_the_placed_manifest(self, tmp_path: Path) -> None:
        manager = _job_manager()
        manager.submit_job(_write(tmp_path, _job()), keep_warm=True)
        sent = manager._aws_client.submit_manifests.call_args.kwargs["manifests"]
        assert sent[0]["spec"]["template"]["spec"]["nodeSelector"] == {KEEP_WARM_LABEL: "true"}

    def test_sqs_submission_queues_the_placed_manifest(self, tmp_path: Path) -> None:
        manager = _job_manager()
        manager._aws_client.get_regional_stack.return_value = MagicMock(stack_name="gco-us-east-1")
        cloudformation, sqs = MagicMock(), MagicMock()
        cloudformation.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "JobQueueUrl", "OutputValue": "https://q"}]}]
        }
        sqs.send_message.return_value = {"MessageId": "m-1"}
        with patch(
            "boto3.client", side_effect=lambda name, **_: {"sqs": sqs}.get(name, cloudformation)
        ):
            manager.submit_job_sqs(_write(tmp_path, _job()), region="us-east-1", keep_warm=True)
        body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
        pod_spec = body["manifests"][0]["spec"]["template"]["spec"]
        assert KEEP_WARM_TOLERATION in pod_spec["tolerations"]

    @pytest.mark.parametrize("method", ["submit_job", "submit_job_direct", "submit_job_sqs"])
    def test_every_transport_refuses_before_any_request(self, method: str, tmp_path: Path) -> None:
        manager = _job_manager()
        kwargs: dict[str, Any] = {"keep_warm": True}
        if method != "submit_job":
            kwargs["region"] = "us-east-1"
        with pytest.raises(ValueError, match=r"requests no nvidia\.com/gpu"):
            getattr(manager, method)(_write(tmp_path, _job(gpu=False)), **kwargs)
        assert manager._aws_client.mock_calls == []


class TestKeepWarmOption:
    @pytest.mark.parametrize(
        ("command", "method", "extra"),
        [
            ("submit", "submit_job", []),
            ("submit-direct", "submit_job_direct", ["-r", "us-east-1"]),
            ("submit-sqs", "submit_job_sqs", ["-r", "us-east-1"]),
        ],
    )
    def test_reaches_the_job_manager(
        self, command: str, method: str, extra: list[str], tmp_path: Path
    ) -> None:
        from cli.main import cli

        manifest_path = _write(tmp_path, _job())
        with patch("cli.commands.jobs_cmd.get_job_manager") as get_manager:
            getattr(get_manager.return_value, method).return_value = {}
            result = CliRunner().invoke(
                cli, ["jobs", command, manifest_path, "--keep-warm", *extra]
            )
        assert result.exit_code == 0, result.output
        assert getattr(get_manager.return_value, method).call_args.kwargs["keep_warm"] is True

    def test_defaults_to_off(self, tmp_path: Path) -> None:
        from cli.main import cli

        with patch("cli.commands.jobs_cmd.get_job_manager") as get_manager:
            get_manager.return_value.submit_job.return_value = {}
            result = CliRunner().invoke(cli, ["jobs", "submit", _write(tmp_path, _job())])
        assert result.exit_code == 0, result.output
        assert get_manager.return_value.submit_job.call_args.kwargs["keep_warm"] is False

    def test_submit_queue_places_the_posted_manifest(self, tmp_path: Path) -> None:
        from cli.main import cli

        with patch("cli.aws_client.get_aws_client") as get_client:
            get_client.return_value.call_api.return_value = {"job": {"job_id": "j-1"}}
            result = CliRunner().invoke(
                cli,
                [
                    "jobs",
                    "submit-queue",
                    _write(tmp_path, _job()),
                    "-r",
                    "us-east-1",
                    "--keep-warm",
                ],
            )
        assert result.exit_code == 0, result.output
        posted = get_client.return_value.call_api.call_args.kwargs["body"]["manifest"]
        assert posted["spec"]["template"]["spec"]["nodeSelector"] == {KEEP_WARM_LABEL: "true"}

    def test_submit_queue_refuses_what_could_not_use_the_pool(self, tmp_path: Path) -> None:
        from cli.main import cli

        with patch("cli.aws_client.get_aws_client") as get_client:
            result = CliRunner().invoke(
                cli,
                [
                    "jobs",
                    "submit-queue",
                    _write(tmp_path, _job(gpu=False)),
                    "-r",
                    "us-east-1",
                    "--keep-warm",
                ],
            )
        assert result.exit_code == 1
        assert "requests no nvidia.com/gpu" in result.output
        get_client.return_value.call_api.assert_not_called()

    def test_the_policy_pre_check_judges_the_placed_manifest(self, tmp_path: Path) -> None:
        from cli.main import cli

        manifest_path = _write(tmp_path, _job())
        with (
            patch("cli.commands.jobs_cmd.get_job_manager") as get_manager,
            patch("cli.job_policy.fetch_region_policies", return_value=[]),
            patch("cli.job_policy.region_verdicts", return_value=[]) as verdicts,
        ):
            manager = get_manager.return_value
            manager.load_manifests.return_value = [_job()]
            manager.submit_job.return_value = {}
            result = CliRunner().invoke(
                cli,
                [
                    "jobs",
                    "submit",
                    manifest_path,
                    "-r",
                    "us-east-1",
                    "--check-policy",
                    "--keep-warm",
                ],
            )
        assert result.exit_code == 0, result.output
        judged = verdicts.call_args.args[0]
        assert judged[0]["spec"]["template"]["spec"]["nodeSelector"] == {KEEP_WARM_LABEL: "true"}


class TestMcpTools:
    @pytest.mark.parametrize(
        ("tool", "kwargs", "argv"),
        [
            ("submit_job_api", {}, ["jobs", "submit", "job.yaml", "--keep-warm"]),
            (
                "submit_job_sqs",
                {"region": "us-east-1"},
                ["jobs", "submit-sqs", "job.yaml", "-r", "us-east-1", "--keep-warm"],
            ),
        ],
    )
    def test_keep_warm_reaches_the_cli(
        self, tool: str, kwargs: dict[str, Any], argv: list[str]
    ) -> None:
        # Through the gco_mcp sys.path entry, as run_mcp imports it: the package
        # path ``gco_mcp.tools.jobs`` would load a second copy of the module whose
        # ``@mcp.tool`` decorators replace the server's registrations, failing
        # a later ``test_tools_are_importable`` on the same worker.
        from tools import jobs as tools

        function = getattr(tools, tool)
        function = getattr(function, "fn", function)
        with (
            patch.object(tools.cli_runner, "reject_paths_outside_project_root", return_value=None),
            patch.object(tools.cli_runner, "_run_cli", return_value="ok") as run_cli,
        ):
            assert function("job.yaml", keep_warm=True, **kwargs) == "ok"
        run_cli.assert_called_once_with(*argv)
