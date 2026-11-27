"""Placing Jobs on the keep-warm GPU pool: ``--keep-warm`` and the admission check.

``gco.job_admission.apply_keep_warm`` is what every submit command's
``--keep-warm`` runs: it adds the ``gco.aws/keep-warm`` node selector and the
toleration for the pool's taint, and refuses what could not use the pool
before any request is made. ``check_keep_warm`` holds a manifest written by
hand to the same rules on the REST and SQS paths and in the CLI's policy
pre-check. Both judge a pod's required node affinity as well as its node
selector (``gco.keep_warm.keep_warm_affinity_conflicts``), and a required node
affinity that pins a pod to the pool selects it as the node selector does.
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
from gco.keep_warm import (
    KEEP_WARM_INSTANCE_FAMILIES,
    KEEP_WARM_LABEL,
    KEEP_WARM_NODE_LABELS,
    KEEP_WARM_TOLERATION,
    NodeSelectorRequirement,
    affinity_selects_keep_warm,
    keep_warm_affinity_conflicts,
)

GPU_TOLERATION = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
INSTANCE_TYPE = "node.kubernetes.io/instance-type"
CAPACITY_PROVIDER = "gco.aws/capacity-provider"
#: What a refusal adds when an instance type or family is what no keep-warm node carries.
FAMILIES = "(the pool's instance families are g4dn, g5, g6, g6e, g6f, g7, g7e, gr6, gr6f)"


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


def _expression(key: str, operator: str, *values: str) -> dict[str, Any]:
    """A ``matchExpressions`` entry; ``Exists`` and ``DoesNotExist`` carry no values."""
    expression: dict[str, Any] = {"key": key, "operator": operator}
    if operator not in ("Exists", "DoesNotExist"):
        expression["values"] = list(values)
    return expression


def _term(*expressions: Any) -> dict[str, Any]:
    return {"matchExpressions": list(expressions)}


def _affinity(*terms: Any) -> dict[str, Any]:
    """A pod ``affinity`` whose required node affinity has these ``nodeSelectorTerms``."""
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": list(terms)}
        }
    }


def _preferred(*terms: Any) -> dict[str, Any]:
    """A pod ``affinity`` that only prefers these terms."""
    return {
        "nodeAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {"weight": 1, "preference": term} for term in terms
            ]
        }
    }


#: An instance type outside the pool's families, and the requirement it reads as.
P4D = _expression(INSTANCE_TYPE, "In", "p4d.24xlarge")
P4D_REQUIREMENT = NodeSelectorRequirement(INSTANCE_TYPE, "In", ("p4d.24xlarge",))
#: The expression that pins a pod to the pool when every term carries it.
PINNED = _expression(KEEP_WARM_LABEL, "In", "true")


def _pinned(**pod: Any) -> dict[str, Any]:
    """A Job that selects the pool by its required node affinity alone, with the toleration."""
    pod.setdefault("affinity", _affinity(_term(PINNED)))
    manifest = _job(**pod)
    _template(manifest)["tolerations"].append(dict(KEEP_WARM_TOLERATION))
    return manifest


#: A runtime patch's pod spec that selects the pool the way ``--keep-warm`` does.
TRAINJOB_SELECTION = {
    "nodeSelector": {KEEP_WARM_LABEL: "true"},
    "tolerations": [dict(GPU_TOLERATION), dict(KEEP_WARM_TOLERATION)],
}


def _trainjob(*pod_specs: dict[str, Any]) -> dict[str, Any]:
    """A one-GPU TrainJob whose runtime patches set these pod specs on its ``node`` job, in order."""
    return {
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
                                            "spec": {"template": {"spec": copy.deepcopy(pod_spec)}}
                                        },
                                    }
                                ]
                            }
                        }
                    },
                }
                for pod_spec in pod_specs
            ],
        },
    }


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

    def test_a_node_affinity_no_keep_warm_node_satisfies_is_refused(self) -> None:
        with pytest.raises(
            ValueError,
            match=r"^Job trial selects gco\.aws/keep-warm=true but requires "
            r"node\.kubernetes\.io/instance-type in \[p4d\.24xlarge\], which no keep-warm node "
            r"carries \(the pool's instance families are g4dn, ",
        ):
            ja.apply_keep_warm([_job(affinity=_affinity(_term(P4D)))])

    def test_a_node_affinity_a_keep_warm_node_satisfies_is_kept(self) -> None:
        affinity = _affinity(_term(_expression(INSTANCE_TYPE, "In", "g5.4xlarge", "p4d.24xlarge")))
        manifest = _job(affinity=copy.deepcopy(affinity))

        ja.apply_keep_warm([manifest])

        assert _template(manifest)["affinity"] == affinity
        assert _template(manifest)["nodeSelector"] == {KEEP_WARM_LABEL: "true"}

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


class TestKeepWarmAffinityConflicts:
    """``gco.keep_warm.keep_warm_affinity_conflicts``: what no keep-warm node satisfies.

    Terms are ORed and the expressions of a term ANDed, as the scheduler reads
    them, and each expression is judged on its own label.
    """

    @pytest.mark.parametrize(
        ("key", "operator", "values", "satisfiable"),
        [
            # A label the pool fixes: always present, with one of its values.
            ("kubernetes.io/arch", "In", ["amd64"], True),
            ("kubernetes.io/arch", "In", ["arm64"], False),
            ("kubernetes.io/arch", "In", ["arm64", "amd64"], True),
            ("kubernetes.io/arch", "NotIn", ["arm64"], True),
            ("kubernetes.io/arch", "NotIn", ["amd64"], False),
            ("kubernetes.io/arch", "Exists", [], True),
            ("kubernetes.io/arch", "DoesNotExist", [], False),
            ("kubernetes.io/arch", "Gt", ["1"], False),
            ("kubernetes.io/arch", "Lt", ["1"], False),
            ("karpenter.sh/nodepool", "In", ["gpu-keep-warm-pool"], True),
            ("karpenter.sh/nodepool", "In", ["another-pool"], False),
            ("karpenter.sh/capacity-type", "NotIn", ["spot"], True),
            ("karpenter.sh/capacity-type", "NotIn", ["spot", "on-demand"], False),
            ("eks.amazonaws.com/instance-family", "In", ["p5", "g6"], True),
            ("eks.amazonaws.com/instance-family", "In", ["p5"], False),
            (
                "eks.amazonaws.com/instance-family",
                "NotIn",
                sorted(KEEP_WARM_INSTANCE_FAMILIES),
                False,
            ),
            (KEEP_WARM_LABEL, "In", ["true"], True),
            (KEEP_WARM_LABEL, "In", ["false"], False),
            # The instance type: some type in one of the pool's families.
            (INSTANCE_TYPE, "In", ["g5.4xlarge", "p4d.24xlarge"], True),
            (INSTANCE_TYPE, "In", ["p4d.24xlarge"], False),
            (INSTANCE_TYPE, "In", [], False),
            (INSTANCE_TYPE, "NotIn", ["g5.4xlarge"], True),
            (INSTANCE_TYPE, "Exists", [], True),
            (INSTANCE_TYPE, "DoesNotExist", [], False),
            (INSTANCE_TYPE, "Gt", ["4"], False),
            (INSTANCE_TYPE, "Lt", ["4"], False),
            # SageMaker HyperPod's capacity provider: never on a keep-warm node.
            (CAPACITY_PROVIDER, "In", ["hyperpod"], False),
            (CAPACITY_PROVIDER, "NotIn", ["hyperpod"], True),
            (CAPACITY_PROVIDER, "Exists", [], False),
            (CAPACITY_PROVIDER, "DoesNotExist", [], True),
            (CAPACITY_PROVIDER, "Gt", ["1"], False),
            (CAPACITY_PROVIDER, "Lt", ["1"], False),
            # A label the pool does not fix is not judged.
            ("topology.kubernetes.io/zone", "In", ["us-east-1a"], True),
            ("topology.kubernetes.io/zone", "DoesNotExist", [], True),
            ("example.com/rack", "Gt", ["not-a-number"], True),
        ],
    )
    def test_each_operator_is_judged_on_its_label(
        self, key: str, operator: str, values: list[str], satisfiable: bool
    ) -> None:
        affinity = _affinity(_term(_expression(key, operator, *values)))
        expected = [] if satisfiable else [[NodeSelectorRequirement(key, operator, tuple(values))]]
        assert keep_warm_affinity_conflicts(affinity) == expected

    @pytest.mark.parametrize(
        ("operator", "values", "satisfiable"),
        [
            ("Gt", ["6"], True),
            ("Gt", ["7"], False),
            ("Lt", ["8"], True),
            ("Lt", ["7"], False),
            ("Gt", ["+6"], True),
            ("Gt", [str(-(2**63))], True),
            ("Gt", ["six"], False),
            # Kubernetes parses an operand as Go's strconv.ParseInt does: no
            # spaces, no digit separators, 64 bits.
            ("Gt", [" 6"], False),
            ("Lt", ["8_0"], False),
            ("Lt", [str(2**63)], False),
            # It also requires exactly one operand.
            ("Gt", [], False),
            ("Lt", ["6", "8"], False),
        ],
    )
    def test_gt_and_lt_compare_one_integer_with_the_label_value(
        self, operator: str, values: list[str], satisfiable: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No label the pool fixes has a numeric value, so give one two values,
        # one of them a number.
        monkeypatch.setitem(KEEP_WARM_NODE_LABELS, "example.com/generation", frozenset({"7", "x"}))
        affinity = _affinity(_term(_expression("example.com/generation", operator, *values)))
        assert (keep_warm_affinity_conflicts(affinity) == []) is satisfiable

    def test_one_term_a_keep_warm_node_satisfies_is_enough(self) -> None:
        affinity = _affinity(_term(P4D), _term(_expression(INSTANCE_TYPE, "In", "g6.xlarge")))
        assert keep_warm_affinity_conflicts(affinity) == []

    def test_each_term_lists_only_the_expressions_that_fail_it(self) -> None:
        zone = _expression("topology.kubernetes.io/zone", "In", "us-east-1a")
        arm = _expression("kubernetes.io/arch", "In", "arm64")
        affinity = _affinity(
            _term(zone, arm), _term(_expression("kubernetes.io/arch", "In", "amd64"), P4D)
        )
        assert keep_warm_affinity_conflicts(affinity) == [
            [NodeSelectorRequirement("kubernetes.io/arch", "In", ("arm64",))],
            [P4D_REQUIREMENT],
        ]

    @pytest.mark.parametrize(
        "term",
        [{}, {"matchExpressions": [], "matchFields": []}, {"matchExpressions": None}],
        ids=["no-fields", "empty-lists", "null"],
    )
    def test_an_empty_term_matches_no_node(self, term: dict[str, Any]) -> None:
        assert keep_warm_affinity_conflicts(_affinity(term)) == [[]]

    def test_match_fields_are_not_judged(self) -> None:
        node_name = {"key": "metadata.name", "operator": "In", "values": ["ip-10-0-0-1"]}
        assert keep_warm_affinity_conflicts(_affinity({"matchFields": [node_name]})) == []
        assert keep_warm_affinity_conflicts(
            _affinity({"matchExpressions": [P4D], "matchFields": [node_name]})
        ) == [[P4D_REQUIREMENT]]

    def test_preferred_terms_are_not_judged(self) -> None:
        assert keep_warm_affinity_conflicts(_preferred(_term(P4D))) == []

    @pytest.mark.parametrize(
        "expression",
        [
            "node.kubernetes.io/instance-type In p4d.24xlarge",
            None,
            {"operator": "In", "values": ["p4d.24xlarge"]},
            {"key": 5, "operator": "In", "values": ["p4d.24xlarge"]},
            {"key": INSTANCE_TYPE, "values": ["p4d.24xlarge"]},
            {"key": INSTANCE_TYPE, "operator": "Matches", "values": ["p4d.24xlarge"]},
            {"key": INSTANCE_TYPE, "operator": "In", "values": "p4d.24xlarge"},
            {"key": INSTANCE_TYPE, "operator": "In", "values": [24]},
            {"key": INSTANCE_TYPE, "operator": "In"},
            {"key": CAPACITY_PROVIDER, "operator": "Gt", "values": [1]},
        ],
        ids=[
            "string",
            "null",
            "no-key",
            "key-not-a-string",
            "no-operator",
            "unknown-operator",
            "values-not-a-list",
            "values-not-strings",
            "no-values",
            "gt-operand-not-a-string",
        ],
    )
    def test_an_expression_the_api_would_refuse_is_left_to_it(self, expression: Any) -> None:
        assert keep_warm_affinity_conflicts(_affinity(_term(expression))) == []
        # It does not save a term another expression fails.
        assert keep_warm_affinity_conflicts(_affinity(_term(expression, P4D))) == [
            [P4D_REQUIREMENT]
        ]

    @pytest.mark.parametrize(
        "affinity",
        [
            None,
            "junk",
            {"nodeAffinity": "junk"},
            {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": "junk"}},
            {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": "junk"}
                }
            },
            _affinity(),
            _affinity("junk", _term(P4D)),
            _affinity({"matchExpressions": P4D}),
            _affinity({"matchExpressions": [], "matchFields": "junk"}),
        ],
        ids=[
            "null",
            "string",
            "node-affinity",
            "required",
            "terms",
            "no-terms",
            "term",
            "match-expressions",
            "match-fields",
        ],
    )
    def test_a_shape_the_api_would_refuse_is_left_to_it(self, affinity: Any) -> None:
        assert keep_warm_affinity_conflicts(affinity) == []


class TestAffinitySelectsKeepWarm:
    """``gco.keep_warm.affinity_selects_keep_warm``: a required node affinity that pins the pool."""

    @pytest.mark.parametrize(
        ("affinity", "selects"),
        [
            (_affinity(_term(PINNED)), True),
            (_affinity(_term(_expression(KEEP_WARM_LABEL, "Exists"))), True),
            (_affinity(_term(_expression(KEEP_WARM_LABEL, "In", "false", "true"))), True),
            (_affinity(_term(P4D, PINNED), _term(_expression(KEEP_WARM_LABEL, "Exists"))), True),
            (_affinity(_term(_expression(KEEP_WARM_LABEL, "In", "false"))), False),
            (_affinity(_term(_expression(KEEP_WARM_LABEL, "NotIn", "false"))), False),
            (_affinity(_term(PINNED), _term(_expression(INSTANCE_TYPE, "In", "g6.xlarge"))), False),
            (_affinity(_term(_expression(INSTANCE_TYPE, "In", "g6.xlarge"))), False),
            (_affinity(), False),
            (_affinity({}), False),
            (_affinity("junk"), False),
            (_affinity({"matchExpressions": PINNED}), False),
            (_affinity(_term({"key": KEEP_WARM_LABEL, "operator": "In", "values": "true"})), False),
            (_preferred(_term(PINNED)), False),
            (None, False),
        ],
        ids=[
            "in-true",
            "exists",
            "in-false-or-true",
            "every-term",
            "in-false",
            "not-in-false",
            "one-term-of-two",
            "no-keep-warm-label",
            "no-terms",
            "empty-term",
            "term-not-a-mapping",
            "match-expressions-not-a-list",
            "malformed-expression",
            "preferred",
            "no-affinity",
        ],
    )
    def test_only_when_every_term_requires_the_label(self, affinity: Any, selects: bool) -> None:
        assert affinity_selects_keep_warm(affinity) is selects


class TestCheckKeepWarmNodeAffinity:
    """``check_keep_warm`` refuses a selection whose required node affinity no keep-warm node meets."""

    def test_an_instance_type_outside_the_pool_is_refused(self) -> None:
        assert ja.check_keep_warm(_selected(affinity=_affinity(_term(P4D)))) == (
            False,
            "Job trial selects gco.aws/keep-warm=true but requires "
            "node.kubernetes.io/instance-type in [p4d.24xlarge], which no keep-warm node carries "
            f"{FAMILIES}, so it would never schedule; drop one or the other "
            "(docs/GPU_KEEP_WARM.md)",
        )

    def test_the_refusal_docs_gpu_keep_warm_shows_is_the_one_admission_gives(self) -> None:
        doc = Path(__file__).resolve().parents[1] / "docs" / "GPU_KEEP_WARM.md"
        ok, message = ja.check_keep_warm(_selected(name="train", affinity=_affinity(_term(P4D))))
        assert ok is False
        assert f"```text\n{message}\n```" in doc.read_text(encoding="utf-8")

    def test_one_instance_type_in_the_pool_is_enough(self) -> None:
        affinity = _affinity(_term(_expression(INSTANCE_TYPE, "In", "g5.4xlarge", "p4d.24xlarge")))
        assert ja.check_keep_warm(_selected(affinity=affinity)) == (True, None)

    @pytest.mark.parametrize(
        ("expression", "named"),
        [
            (_expression("kubernetes.io/arch", "In", "arm64"), "kubernetes.io/arch in [arm64]"),
            (
                _expression("karpenter.sh/nodepool", "In", "another-pool"),
                "karpenter.sh/nodepool in [another-pool]",
            ),
            (
                _expression("kubernetes.io/arch", "NotIn", "amd64", "arm64"),
                "kubernetes.io/arch not in [amd64, arm64]",
            ),
            (_expression(CAPACITY_PROVIDER, "Exists"), "gco.aws/capacity-provider to exist"),
            (_expression("workload-type", "DoesNotExist"), "workload-type not to exist"),
            (_expression(INSTANCE_TYPE, "Gt", "4"), "node.kubernetes.io/instance-type > 4"),
            (_expression(CAPACITY_PROVIDER, "Lt", "4"), "gco.aws/capacity-provider < 4"),
            (
                _expression(CAPACITY_PROVIDER, "Gt", "4", "8"),
                "gco.aws/capacity-provider > [4, 8]",
            ),
        ],
        ids=["arch", "nodepool", "not-in", "exists", "does-not-exist", "gt", "lt", "gt-two"],
    )
    def test_the_refusal_names_what_no_keep_warm_node_carries(
        self, expression: dict[str, Any], named: str
    ) -> None:
        ok, message = ja.check_keep_warm(_selected(affinity=_affinity(_term(expression))))
        assert ok is False
        assert (
            f"Job trial selects gco.aws/keep-warm=true but requires {named}, which no keep-warm "
            "node carries"
        ) in str(message)

    @pytest.mark.parametrize(
        ("expression", "lists_families"),
        [
            (P4D, True),
            (_expression("eks.amazonaws.com/instance-family", "In", "p5"), True),
            (_expression(INSTANCE_TYPE, "DoesNotExist"), True),
            (_expression("kubernetes.io/arch", "In", "arm64"), False),
            (_expression(CAPACITY_PROVIDER, "In", "hyperpod"), False),
        ],
        ids=["instance-type", "instance-family", "no-instance-type", "arch", "hyperpod"],
    )
    def test_the_pools_families_are_named_only_for_an_instance_type_or_family(
        self, expression: dict[str, Any], lists_families: bool
    ) -> None:
        ok, message = ja.check_keep_warm(_selected(affinity=_affinity(_term(expression))))
        assert ok is False
        assert (FAMILIES in str(message)) is lists_families

    def test_a_term_a_keep_warm_node_satisfies_admits_the_pod(self) -> None:
        affinity = _affinity(_term(P4D), _term(_expression(INSTANCE_TYPE, "In", "g6e.12xlarge")))
        assert ja.check_keep_warm(_selected(affinity=affinity)) == (True, None)

    def test_every_term_no_keep_warm_node_satisfies_is_named(self) -> None:
        affinity = _affinity(
            _term(_expression("kubernetes.io/arch", "In", "arm64")),
            _term(P4D, _expression("karpenter.sh/capacity-type", "In", "reserved")),
            {},
        )
        ok, message = ja.check_keep_warm(_selected(affinity=affinity))
        assert ok is False
        assert (
            "requires kubernetes.io/arch in [arm64]; or node.kubernetes.io/instance-type in "
            "[p4d.24xlarge] and karpenter.sh/capacity-type in [reserved]; or an empty term, "
            f"which matches no node, which no keep-warm node carries {FAMILIES}, so it would "
            "never schedule"
        ) in str(message)

    def test_the_node_selector_is_judged_first(self) -> None:
        manifest = _selected(
            nodeSelector={KEEP_WARM_LABEL: "true", "kubernetes.io/arch": "arm64"},
            affinity=_affinity(_term(P4D)),
        )
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "selects gco.aws/keep-warm=true and kubernetes.io/arch=arm64, which" in str(message)

    def test_preferred_terms_are_not_judged(self) -> None:
        assert ja.check_keep_warm(_selected(affinity=_preferred(_term(P4D)))) == (True, None)

    def test_a_pod_that_does_not_select_the_pool_is_not_judged(self) -> None:
        assert ja.check_keep_warm(_job(affinity=_affinity(_term(P4D)))) == (True, None)

    def test_a_malformed_affinity_is_left_to_the_api(self) -> None:
        affinity = _affinity(_term({"key": INSTANCE_TYPE, "operator": "In", "values": "p4d"}))
        assert ja.check_keep_warm(_selected(affinity=affinity)) == (True, None)


class TestSelectionByNodeAffinity:
    """A pod pinned to the pool by its required node affinity is judged as one that selects it."""

    def test_a_complete_pod_is_admitted(self) -> None:
        manifest = _pinned()
        assert "nodeSelector" not in _template(manifest)
        assert ja.check_keep_warm(manifest) == (True, None)

    def test_without_the_toleration_it_is_refused(self) -> None:
        ok, message = ja.check_keep_warm(_job(affinity=_affinity(_term(PINNED))))
        assert ok is False
        assert "Job trial selects gco.aws/keep-warm=true without a toleration" in str(message)

    def test_without_a_gpu_it_is_refused(self) -> None:
        manifest = _pinned()
        _template(manifest)["containers"][0].pop("resources")
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "requests no nvidia.com/gpu" in str(message)

    @pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
    def test_a_long_running_kind_is_refused(self, kind: str) -> None:
        manifest = _pinned()
        manifest["kind"] = kind
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert f"{kind} trial selects gco.aws/keep-warm=true, but its pods never finish" in str(
            message
        )

    def test_the_hyperpod_queue_label_is_refused(self) -> None:
        manifest = _pinned()
        manifest["metadata"]["labels"] = {ja.KUEUE_QUEUE_NAME_LABEL: "gco-hyperpod"}
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "whose flavor sends its pods to SageMaker HyperPod nodes" in str(message)

    def test_a_daemonset_may_select_the_pool_without_a_gpu(self) -> None:
        manifest = _pinned()
        manifest["kind"] = "DaemonSet"
        _template(manifest)["containers"][0].pop("resources")
        assert ja.check_keep_warm(manifest) == (True, None)

    def test_its_affinity_is_judged_without_a_node_selector(self) -> None:
        affinity = _affinity(_term(_expression(KEEP_WARM_LABEL, "Exists"), P4D))
        ok, message = ja.check_keep_warm(_pinned(affinity=affinity))
        assert ok is False
        assert (
            "Job trial selects gco.aws/keep-warm=true but requires "
            "node.kubernetes.io/instance-type in [p4d.24xlarge]"
        ) in str(message)

    def test_a_node_selector_that_is_not_a_mapping_is_not_read(self) -> None:
        assert ja.check_keep_warm(_pinned(nodeSelector=["junk"])) == (True, None)

    @pytest.mark.parametrize(
        "affinity",
        [
            _affinity(_term(PINNED), _term(_expression(INSTANCE_TYPE, "In", "g6.xlarge"))),
            _affinity(_term(_expression(INSTANCE_TYPE, "In", "g6.xlarge"))),
            _affinity(_term(_expression(KEEP_WARM_LABEL, "NotIn", "false"))),
        ],
        ids=["one-term-of-two", "instance-type-in-the-pool", "not-in"],
    )
    def test_an_affinity_that_also_allows_other_nodes_is_not_judged(self, affinity: Any) -> None:
        # Neither a toleration nor a GPU: a judged pod would be refused.
        assert ja.check_keep_warm(_job(gpu=False, affinity=affinity)) == (True, None)


class TestTrainJobNodeAffinity:
    """A TrainJob's pod templates carry the required node affinity of their runtime patches."""

    def test_a_patch_that_selects_the_pool_with_an_affinity_it_cannot_meet_is_refused(
        self,
    ) -> None:
        ok, message = ja.check_keep_warm(
            _trainjob({**TRAINJOB_SELECTION, "affinity": _affinity(_term(P4D))})
        )
        assert ok is False
        assert str(message).startswith(
            "TrainJob tj selects gco.aws/keep-warm=true but requires "
            "node.kubernetes.io/instance-type in [p4d.24xlarge], which no keep-warm node carries"
        )

    def test_the_affinity_may_come_from_another_patch(self) -> None:
        manifest = _trainjob(TRAINJOB_SELECTION, {"affinity": _affinity(_term(P4D))})
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "requires node.kubernetes.io/instance-type in [p4d.24xlarge]" in str(message)

    def test_a_later_patch_without_a_node_affinity_keeps_the_earlier_one(self) -> None:
        manifest = _trainjob(
            {"affinity": _affinity(_term(P4D))},
            {**TRAINJOB_SELECTION, "affinity": {"podAntiAffinity": {}}},
        )
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "requires node.kubernetes.io/instance-type in [p4d.24xlarge]" in str(message)

    def test_a_later_patch_replaces_the_required_node_affinity(self) -> None:
        manifest = _trainjob(
            {**TRAINJOB_SELECTION, "affinity": _affinity(_term(P4D))},
            {"affinity": _affinity(_term(_expression(INSTANCE_TYPE, "In", "g6.xlarge")))},
        )
        assert ja.check_keep_warm(manifest) == (True, None)

    def test_a_template_pinned_by_its_affinity_alone_is_judged(self) -> None:
        manifest = _trainjob(
            {"affinity": _affinity(_term(PINNED)), "tolerations": [dict(GPU_TOLERATION)]}
        )
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "TrainJob tj selects gco.aws/keep-warm=true without a toleration" in str(message)

    def test_each_replicated_job_keeps_its_own_affinity(self) -> None:
        manifest = _trainjob({**TRAINJOB_SELECTION, "affinity": _affinity(_term(P4D))})
        replicated_jobs = manifest["spec"]["runtimePatches"][0]["trainingRuntimeSpec"]["template"][
            "spec"
        ]["replicatedJobs"]
        # An entry that is no replicated job first, and a launcher that fits the pool after.
        replicated_jobs.insert(0, "junk")
        launcher_affinity = _affinity(_term(_expression(INSTANCE_TYPE, "In", "g6.xlarge")))
        replicated_jobs.append(
            {
                "name": "launcher",
                "template": {"spec": {"template": {"spec": {"affinity": launcher_affinity}}}},
            }
        )
        ok, message = ja.check_keep_warm(manifest)
        assert ok is False
        assert "requires node.kubernetes.io/instance-type in [p4d.24xlarge]" in str(message)

    @pytest.mark.parametrize(
        "spec",
        [
            "junk",
            {"runtimePatches": "junk"},
            {"runtimePatches": [{"trainingRuntimeSpec": {"template": {"spec": {}}}}]},
            {
                "runtimePatches": [
                    {"trainingRuntimeSpec": {"template": {"spec": {"replicatedJobs": ["junk"]}}}}
                ]
            },
        ],
        ids=["spec", "patches", "replicated-jobs", "replicated-job"],
    )
    def test_patches_that_name_no_replicated_job_select_nothing(self, spec: Any) -> None:
        manifest = {"apiVersion": ja.TRAINJOB_API_VERSION, "kind": "TrainJob", "spec": spec}
        assert ja.check_keep_warm(manifest) == (True, None)


class TestSubmissionPathParity:
    """The REST service and the SQS processor give a keep-warm manifest the same verdict."""

    CASES = [
        (_selected(), True),
        (_job(nodeSelector={KEEP_WARM_LABEL: "true"}), False),
        (_job(), True),
        (_selected(affinity=_affinity(_term(P4D))), False),
        (_pinned(), True),
    ]

    @pytest.mark.parametrize(("manifest", "expected"), CASES)
    def test_rest_and_sqs_agree(
        self, manifest: dict[str, Any], expected: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gco.services import queue_processor
        from gco.services.manifest_processor import ManifestProcessor

        with (
            patch("gco.services.manifest_processor.config"),
            patch("gco.services.manifest_processor.client"),
        ):
            processor = ManifestProcessor(
                "c", "us-east-1", {"require_accelerator_toleration": False}
            )
        processor.runtime_class_exists = lambda name: True
        monkeypatch.setattr(queue_processor, "runtime_class_exists", lambda name: True)
        monkeypatch.setattr(
            queue_processor, "JOB_VALIDATION_POLICY", processor.job_validation_policy()
        )

        rest_ok, rest_message = processor.validate_manifest(copy.deepcopy(manifest))
        sqs_ok, sqs_message = queue_processor.validate_manifest(copy.deepcopy(manifest))

        assert (sqs_ok, sqs_message) == (rest_ok, rest_message)
        assert rest_ok is expected
        if not expected:
            assert str(rest_message).startswith("Keep-warm validation failed: ")


class TestPolicyPreCheck:
    @pytest.mark.parametrize(
        ("manifest", "reason"),
        [
            (_job(nodeSelector={KEEP_WARM_LABEL: "true"}), "without a toleration"),
            (
                _selected(affinity=_affinity(_term(P4D))),
                "requires node.kubernetes.io/instance-type in [p4d.24xlarge]",
            ),
        ],
        ids=["no-toleration", "node-affinity"],
    )
    def test_a_keep_warm_refusal_is_a_finding(self, manifest: dict[str, Any], reason: str) -> None:
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
        issues = evaluate_manifest(copy.deepcopy(manifest), policy)
        assert [issue.check for issue in issues] == [CHECK_KEEP_WARM]
        assert reason in issues[0].message


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

    @pytest.mark.parametrize(
        ("manifest", "reason"),
        [
            (_job(gpu=False), r"requests no nvidia\.com/gpu"),
            (
                _job(affinity=_affinity(_term(P4D))),
                r"requires node\.kubernetes\.io/instance-type in \[p4d\.24xlarge\]",
            ),
        ],
        ids=["no-gpu", "node-affinity"],
    )
    @pytest.mark.parametrize("method", ["submit_job", "submit_job_direct", "submit_job_sqs"])
    def test_every_transport_refuses_before_any_request(
        self, method: str, manifest: dict[str, Any], reason: str, tmp_path: Path
    ) -> None:
        manager = _job_manager()
        kwargs: dict[str, Any] = {"keep_warm": True}
        if method != "submit_job":
            kwargs["region"] = "us-east-1"
        with pytest.raises(ValueError, match=reason):
            getattr(manager, method)(_write(tmp_path, manifest), **kwargs)
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
