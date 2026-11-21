"""Example Jobs that allow no retries survive a preemption.

GCO's platform pods run at the ``gco-platform-critical`` priority class
(lambda/kubectl-applier-simple/manifests/05-priority-classes.yaml), which
preempts pods at the default priority when nodes are full. A Job whose
``backoffLimit`` is 0 fails on its first pod failure, and a preempted pod
counts as one unless a ``podFailurePolicy`` rule ignores the
``DisruptionTarget`` condition the scheduler sets on it. A live example run
lost ``pipeline-dag`` that way: the SQS queue processor preempted the
preprocess step's pod, and the Job failed with ``BackoffLimitExceeded``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
MANIFESTS_DIR = REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"


def _job_specs(document: dict[str, Any]) -> list[dict[str, Any]]:
    """The Job specs a document runs: a Job's own, a CronJob's or a ScaledJob's template."""
    kind = document.get("kind")
    spec = document.get("spec") or {}
    if kind == "Job":
        return [spec]
    if kind == "CronJob":
        return [(spec.get("jobTemplate") or {}).get("spec") or {}]
    if kind == "ScaledJob":
        return [spec.get("jobTargetRef") or {}]
    return []


def _ignores_disruptions(job_spec: dict[str, Any]) -> bool:
    """Whether a rule ignores pods the scheduler preempted or the API evicted."""
    rules = (job_spec.get("podFailurePolicy") or {}).get("rules") or []
    return any(
        rule.get("action") == "Ignore"
        and any(
            condition.get("type") == "DisruptionTarget"
            and condition.get("status", "True") == "True"
            for condition in rule.get("onPodConditions") or []
        )
        for rule in rules
    )


def _no_retry_jobs() -> list[tuple[str, dict[str, Any]]]:
    """``(workload, Job spec)`` for every example Job spec with ``backoffLimit: 0``."""
    found = []
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(document, dict):
                continue
            name = (document.get("metadata") or {}).get("name")
            for job_spec in _job_specs(document):
                if job_spec.get("backoffLimit") == 0:
                    found.append((f"{path.name}:{document.get('kind')}/{name}", job_spec))
    return found


def test_platform_pods_can_preempt_default_priority_pods() -> None:
    """The reason the rule matters: the platform class preempts workloads."""
    text = (MANIFESTS_DIR / "05-priority-classes.yaml").read_text(encoding="utf-8")
    platform = yaml.safe_load(text)

    assert platform["metadata"]["name"] == "gco-platform-critical"
    assert platform["value"] > 0
    assert platform["preemptionPolicy"] == "PreemptLowerPriority"


def test_no_retry_examples_exist() -> None:
    """The check below would pass vacuously if no example set ``backoffLimit: 0``."""
    names = {workload.split(":", 1)[0] for workload, _spec in _no_retry_jobs()}
    assert {"dag-step-preprocess.yaml", "dag-step-train.yaml"} <= names


@pytest.mark.parametrize(
    ("workload", "job_spec"),
    _no_retry_jobs(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_no_retry_job_ignores_disruptions(workload: str, job_spec: dict[str, Any]) -> None:
    assert _ignores_disruptions(job_spec), (
        f"{workload} sets backoffLimit: 0 without a podFailurePolicy rule that ignores "
        "DisruptionTarget, so one preemption by a gco-platform-critical pod fails it"
    )
    # The Job API accepts podFailurePolicy only with restartPolicy Never.
    assert job_spec["template"]["spec"]["restartPolicy"] == "Never"


@pytest.mark.parametrize(
    ("job_spec", "expected"),
    [
        ({}, False),
        ({"podFailurePolicy": {"rules": []}}, False),
        (
            {
                "podFailurePolicy": {
                    "rules": [
                        {"action": "FailJob", "onPodConditions": [{"type": "DisruptionTarget"}]}
                    ]
                }
            },
            False,
        ),
        (
            {
                "podFailurePolicy": {
                    "rules": [
                        {
                            "action": "Ignore",
                            "onPodConditions": [{"type": "DisruptionTarget", "status": "False"}],
                        }
                    ]
                }
            },
            False,
        ),
        (
            {
                "podFailurePolicy": {
                    "rules": [
                        {"action": "Ignore", "onExitCodes": {"operator": "In", "values": [42]}}
                    ]
                }
            },
            False,
        ),
        (
            {
                "podFailurePolicy": {
                    "rules": [
                        {"action": "Ignore", "onPodConditions": [{"type": "DisruptionTarget"}]}
                    ]
                }
            },
            True,
        ),
        (
            {
                "podFailurePolicy": {
                    "rules": [
                        {
                            "action": "Ignore",
                            "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}],
                        }
                    ]
                }
            },
            True,
        ),
    ],
    ids=[
        "no-policy",
        "no-rules",
        "fail-job",
        "condition-false",
        "exit-codes-only",
        "ignore-disruption",
        "ignore-disruption-explicit-status",
    ],
)
def test_the_rule_matcher(job_spec: dict[str, Any], expected: bool) -> None:
    assert _ignores_disruptions(job_spec) is expected


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"kind": "Job", "spec": {"backoffLimit": 0}}, [{"backoffLimit": 0}]),
        (
            {"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"backoffLimit": 1}}}},
            [{"backoffLimit": 1}],
        ),
        (
            {"kind": "ScaledJob", "spec": {"jobTargetRef": {"backoffLimit": 0}}},
            [{"backoffLimit": 0}],
        ),
        ({"kind": "CronJob", "spec": {}}, [{}]),
        ({"kind": "Deployment", "spec": {}}, []),
    ],
    ids=["job", "cronjob", "scaledjob", "cronjob-without-template", "not-a-job"],
)
def test_the_job_spec_finder(document: dict[str, Any], expected: list[dict[str, Any]]) -> None:
    assert _job_specs(document) == expected
