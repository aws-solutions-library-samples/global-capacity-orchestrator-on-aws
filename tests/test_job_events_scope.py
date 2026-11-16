"""Tests for the widened job events read (gco.services.api_shared._collect_job_events).

The gap this closes, observed live on a Spot reclamation: the Job's own events
and its live pods' events never say why a node went away. Karpenter / EKS Auto
Mode publish that on the Node and the NodeClaim (SpotInterrupted,
InstanceTerminating, DisruptionTerminating), the pod only gets "Evicted pod:
Forceful Termination", and once the eviction deletes the pod, listing events
for pods that still exist drops even that. The collector therefore covers
every pod the Job has had within the event TTL, the nodes those pods ran on,
and the NodeClaims behind the nodes, and reports what it covered and what it
could not resolve.

The fakes here answer ``list_namespaced_event`` by field selector the way the
apiserver does, so the tests pin the exact selectors and the ``default``
namespace the cluster-scoped reads must use.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from kubernetes.client.rest import ApiException

from gco.services.api_shared import (
    CLUSTER_SCOPED_EVENT_NAMESPACE,
    _collect_job_events,
    _event_sort_key,
    _job_pod_name_pattern,
    _nodeclaims_for_nodes,
)

JOB = "trainer"
NAMESPACE = "gco-jobs"
T0 = datetime(2026, 11, 15, 9, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 15, 10, 0, 5, tzinfo=UTC)
T3 = datetime(2026, 11, 15, 10, 1, 0, tzinfo=UTC)


def _event(
    kind: str,
    name: str,
    reason: str,
    message: str,
    *,
    host: str | None = None,
    last: datetime | None = None,
    event_time: datetime | None = None,
    event_type: str = "Normal",
    namespace: str | None = NAMESPACE,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=event_type,
        reason=reason,
        message=message,
        count=1,
        first_timestamp=last,
        last_timestamp=last,
        event_time=event_time,
        source=SimpleNamespace(component="kubelet" if host else "controller", host=host),
        involved_object=SimpleNamespace(kind=kind, name=name, namespace=namespace),
    )


def _pod(name: str, node: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name), spec=SimpleNamespace(node_name=node)
    )


def _items(values: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(items=list(values))


class FakeCoreV1:
    """Answers event listings by field selector, like the apiserver."""

    _RELATED = re.compile(r"^involvedObject\.kind=(\w+),involvedObject\.name=(.+)$")

    def __init__(
        self,
        *,
        job_events: list[Any],
        pods: list[Any],
        pod_events: list[Any],
        related: dict[tuple[str, str], Any] | None = None,
    ) -> None:
        self.job_events = job_events
        self.pods = pods
        self.pod_events = pod_events
        self.related = related or {}
        self.event_calls: list[tuple[str, str]] = []

    def list_namespaced_event(self, namespace: str, field_selector: str) -> SimpleNamespace:
        self.event_calls.append((namespace, field_selector))
        if field_selector == f"involvedObject.name={JOB},involvedObject.kind=Job":
            assert namespace == NAMESPACE
            return _items(self.job_events)
        if field_selector == "involvedObject.kind=Pod":
            assert namespace == NAMESPACE
            return _items(self.pod_events)
        match = self._RELATED.match(field_selector)
        assert match, field_selector
        assert namespace == CLUSTER_SCOPED_EVENT_NAMESPACE
        answer = self.related.get((match.group(1), match.group(2)), [])
        if isinstance(answer, Exception):
            raise answer
        return _items(answer)

    def list_namespaced_pod(self, namespace: str, label_selector: str) -> SimpleNamespace:
        assert namespace == NAMESPACE
        assert label_selector == f"job-name={JOB}"
        return _items(self.pods)


def _batch(completion_mode: str | None = None) -> MagicMock:
    batch_v1 = MagicMock()
    batch_v1.read_namespaced_job.return_value = SimpleNamespace(
        spec=SimpleNamespace(completion_mode=completion_mode)
    )
    return batch_v1


def _custom(nodeclaims: list[dict[str, Any]] | Any) -> MagicMock:
    custom_objects = MagicMock()
    custom_objects.list_cluster_custom_object.return_value = (
        {"items": nodeclaims} if isinstance(nodeclaims, list) else nodeclaims
    )
    return custom_objects


def _collect(core_v1: FakeCoreV1, batch_v1: Any = None, custom_objects: Any = None) -> dict:
    return _collect_job_events(
        core_v1,
        batch_v1 if batch_v1 is not None else _batch(),
        custom_objects if custom_objects is not None else _custom([]),
        namespace=NAMESPACE,
        job_name=JOB,
    )


def _by_kind(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e["involvedObject"]["kind"] == kind]


class TestSpotReclamationScenario:
    """The live incident: pod evicted, deleted, its node reclaimed, replacement running."""

    def _core(self, **overrides: Any) -> FakeCoreV1:
        job_events = [
            _event("Job", JOB, "SuccessfulCreate", "Created pod: trainer-abcde", last=T0),
            _event("Job", JOB, "SuccessfulCreate", "Created pod: trainer-fghij", last=T2),
        ]
        pods = [_pod("trainer-fghij", "i-0replacement")]
        pod_events = [
            # The deleted pod: scheduler (eventTime only), kubelet (source.host),
            # Karpenter's nomination, and the eviction with no cause.
            _event(
                "Pod",
                "trainer-abcde",
                "Scheduled",
                "Successfully assigned gco-jobs/trainer-abcde to i-0evicted",
                event_time=T0,
            ),
            _event("Pod", "trainer-abcde", "Started", "Started container main", host="i-0evicted"),
            _event(
                "Pod",
                "trainer-abcde",
                "Nominated",
                "Pod should schedule on: nodeclaim/gpu-abc12, node/i-0evicted",
                last=T0,
            ),
            _event("Pod", "trainer-abcde", "Evicted", "Evicted pod: Forceful Termination", last=T2),
            # The live replacement.
            _event(
                "Pod",
                "trainer-fghij",
                "Scheduled",
                "Successfully assigned gco-jobs/trainer-fghij to i-0replacement",
                event_time=T2,
            ),
            # Another job's pod in the same namespace: must stay out.
            _event("Pod", "other-job-abcde", "Started", "Started container main", host="i-0other"),
            # A pod event without a usable name: skipped, not a crash.
            SimpleNamespace(
                type="Normal",
                reason="Started",
                message="",
                count=1,
                first_timestamp=None,
                last_timestamp=None,
                event_time=None,
                source=None,
                involved_object=None,
            ),
        ]
        related = {
            ("Node", "i-0evicted"): [
                _event(
                    "Node",
                    "i-0evicted",
                    "SpotInterrupted",
                    "Spot interruption warning was triggered",
                    event_type="Warning",
                    last=T1,
                    namespace=None,
                ),
                _event(
                    "Node",
                    "i-0evicted",
                    "TerminatingOnInterruption",
                    "Interruption triggered termination for the Node",
                    event_type="Warning",
                    last=T1,
                    namespace=None,
                ),
            ],
            ("NodeClaim", "gpu-abc12"): [
                _event(
                    "NodeClaim",
                    "gpu-abc12",
                    "SpotInterrupted",
                    "Spot interruption warning was triggered",
                    event_type="Warning",
                    last=T1,
                    namespace=None,
                ),
            ],
            ("NodeClaim", "gpu-xyz34"): [
                _event(
                    "NodeClaim",
                    "gpu-xyz34",
                    "DisruptionLaunching",
                    "Launching NodeClaim: Underutilized",
                    last=T3,
                    namespace=None,
                ),
            ],
        }
        kwargs: dict[str, Any] = {
            "job_events": job_events,
            "pods": pods,
            "pod_events": pod_events,
            "related": related,
        }
        kwargs.update(overrides)
        return FakeCoreV1(**kwargs)

    def _nodeclaims(self) -> list[dict[str, Any]]:
        return [
            {"metadata": {"name": "gpu-xyz34"}, "status": {"nodeName": "i-0replacement"}},
            # The reclaimed node's NodeClaim is already gone from the listing;
            # only the Nominated record still names it.
            {"metadata": {"name": "gpu-unrelated"}, "status": {"nodeName": "i-0elsewhere"}},
            # Malformed entries the listing could carry: never a crash.
            {"metadata": {"name": "gpu-no-status"}},
            {"metadata": "not-a-dict", "status": {"nodeName": "i-0replacement"}},
            "not-a-dict",
        ]

    def test_returns_node_and_nodeclaim_events_behind_the_evicted_pod(self) -> None:
        core = self._core()
        result = _collect(core, custom_objects=_custom(self._nodeclaims()))
        events = result["events"]

        node_reasons = {e["reason"] for e in _by_kind(events, "Node")}
        assert node_reasons == {"SpotInterrupted", "TerminatingOnInterruption"}
        claim_events = _by_kind(events, "NodeClaim")
        assert {(e["involvedObject"]["name"], e["reason"]) for e in claim_events} == {
            ("gpu-abc12", "SpotInterrupted"),
            ("gpu-xyz34", "DisruptionLaunching"),
        }
        # Every event says what it is about, which is how a caller attributes it.
        assert all(e["involvedObject"]["kind"] for e in events)

    def test_returns_the_deleted_pods_events_and_says_the_pod_is_gone(self) -> None:
        result = _collect(self._core(), custom_objects=_custom(self._nodeclaims()))
        pod_events = _by_kind(result["events"], "Pod")
        names = {e["involvedObject"]["name"] for e in pod_events}
        assert names == {"trainer-abcde", "trainer-fghij"}, "foreign pod leaked in"
        assert any(e["message"] == "Evicted pod: Forceful Termination" for e in pod_events)
        assert result["scope"]["pods"] == ["trainer-abcde", "trainer-fghij"]
        assert result["scope"]["deleted_pods"] == ["trainer-abcde"]

    def test_resolves_nodes_and_nodeclaims_from_every_record_a_pod_left(self) -> None:
        result = _collect(self._core(), custom_objects=_custom(self._nodeclaims()))
        scope = result["scope"]
        # i-0evicted from the deleted pod's Scheduled / source.host / Nominated
        # records, i-0replacement from the live pod's spec.nodeName.
        assert scope["nodes"] == ["i-0evicted", "i-0replacement"]
        # gpu-abc12 from the Nominated record (NodeClaim already deleted),
        # gpu-xyz34 from the live NodeClaim listing by status.nodeName.
        assert scope["nodeclaims"] == ["gpu-abc12", "gpu-xyz34"]
        assert scope["errors"] == []

    def test_reads_cluster_scoped_events_from_the_default_namespace(self) -> None:
        core = self._core()
        _collect(core, custom_objects=_custom(self._nodeclaims()))
        related_calls = [
            (ns, sel) for ns, sel in core.event_calls if sel.startswith("involvedObject.kind=N")
        ]
        assert related_calls == [
            ("default", "involvedObject.kind=Node,involvedObject.name=i-0evicted"),
            ("default", "involvedObject.kind=Node,involvedObject.name=i-0replacement"),
            ("default", "involvedObject.kind=NodeClaim,involvedObject.name=gpu-abc12"),
            ("default", "involvedObject.kind=NodeClaim,involvedObject.name=gpu-xyz34"),
        ]
        # One listing of the namespace's pod events replaces the per-pod reads.
        assert core.event_calls.count((NAMESPACE, "involvedObject.kind=Pod")) == 1

    def test_events_are_newest_first_with_event_time_as_a_fallback(self) -> None:
        result = _collect(self._core(), custom_objects=_custom(self._nodeclaims()))
        keys = [_event_sort_key(e) for e in result["events"]]
        assert keys == sorted(keys, reverse=True)
        # The scheduler's eventTime-only record is dated, not sorted to the end
        # as an empty key.
        scheduled = next(e for e in result["events"] if e["reason"] == "Scheduled")
        assert scheduled["eventTime"] is not None
        assert _event_sort_key(scheduled) == scheduled["eventTime"]
        # The nameless event contributed nothing.
        assert all(e["involvedObject"]["name"] for e in result["events"])

    def test_nodeclaim_listing_queries_karpenter_v1(self) -> None:
        custom_objects = _custom(self._nodeclaims())
        _collect(self._core(), custom_objects=custom_objects)
        custom_objects.list_cluster_custom_object.assert_called_once_with(
            group="karpenter.sh", version="v1", plural="nodeclaims"
        )


class TestDegradedLookups:
    """Everything beyond the Job and pod events is best-effort and says so."""

    def _core(self) -> FakeCoreV1:
        return FakeCoreV1(
            job_events=[],
            pods=[_pod("trainer-fghij", "i-0replacement"), _pod("trainer-nonode", None)],
            pod_events=[],
            related={
                ("Node", "i-0replacement"): Exception("(403) Forbidden"),
            },
        )

    def test_job_read_failure_is_recorded_and_the_plain_shape_still_matches(self) -> None:
        core = FakeCoreV1(
            job_events=[],
            pods=[],
            pod_events=[
                _event("Pod", "trainer-zzzzz", "Evicted", "Evicted pod: Drifted"),
                _event("Pod", "trainer-3-zzzzz", "Started", "indexed shape"),
            ],
        )
        batch_v1 = MagicMock()
        batch_v1.read_namespaced_job.side_effect = Exception("(404) Not Found")
        result = _collect(core, batch_v1=batch_v1)
        assert result["scope"]["errors"] == ["Job gco-jobs/trainer: (404) Not Found"]
        # Without the Job, only the plain "<job>-<suffix>" shape is trusted.
        assert result["scope"]["pods"] == ["trainer-zzzzz"]

    def test_api_exceptions_are_reduced_to_status_and_reason(self) -> None:
        """An ApiException's multi-line str (headers, body) does not land in scope.errors."""
        refused = ApiException(status=403, reason="Forbidden")
        refused.body = '{"kind":"Status","message":"nodeclaims.karpenter.sh is forbidden"}'
        core = FakeCoreV1(
            job_events=[],
            pods=[_pod("trainer-a1b2c", "i-0abc")],
            pod_events=[],
            related={("Node", "i-0abc"): refused},
        )
        custom_objects = MagicMock()
        custom_objects.list_cluster_custom_object.side_effect = ApiException(
            status=404, reason="Not Found"
        )
        result = _collect(core, custom_objects=custom_objects)
        assert result["scope"]["errors"] == [
            "NodeClaims: (404) Not Found",
            "Node i-0abc events: (403) Forbidden",
        ]

    def test_nodeclaim_listing_failure_keeps_node_events(self) -> None:
        core = FakeCoreV1(
            job_events=[],
            pods=[_pod("trainer-fghij", "i-0replacement")],
            pod_events=[],
            related={
                ("Node", "i-0replacement"): [
                    _event("Node", "i-0replacement", "InstanceUnhealthy", "unhealthy")
                ]
            },
        )
        custom_objects = MagicMock()
        custom_objects.list_cluster_custom_object.side_effect = Exception("(404) Not Found")
        result = _collect(core, custom_objects=custom_objects)
        assert [e["reason"] for e in _by_kind(result["events"], "Node")] == ["InstanceUnhealthy"]
        assert result["scope"]["nodeclaims"] == []
        assert result["scope"]["errors"] == ["NodeClaims: (404) Not Found"]

    def test_refused_event_read_is_recorded_per_target_and_others_continue(self) -> None:
        core = FakeCoreV1(
            job_events=[],
            pods=[_pod("trainer-a1b2c", "i-0refused"), _pod("trainer-d3e4f", "i-0readable")],
            pod_events=[],
            related={
                ("Node", "i-0refused"): Exception("(403) Forbidden"),
                ("Node", "i-0readable"): [
                    _event(
                        "Node", "i-0readable", "DisruptionTerminating", "Disrupting Node: Drifted"
                    )
                ],
                ("NodeClaim", "gpu-ok"): Exception("(403) Forbidden"),
            },
        )
        custom_objects = _custom(
            [{"metadata": {"name": "gpu-ok"}, "status": {"nodeName": "i-0readable"}}]
        )
        result = _collect(core, custom_objects=custom_objects)
        assert [e["reason"] for e in result["events"]] == ["DisruptionTerminating"]
        assert result["scope"]["errors"] == [
            "Node i-0refused events: (403) Forbidden",
            "NodeClaim gpu-ok events: (403) Forbidden",
        ]
        assert result["scope"]["nodes"] == ["i-0readable", "i-0refused"]
        assert result["scope"]["nodeclaims"] == ["gpu-ok"]

    def test_pods_without_a_node_skip_every_node_lookup(self) -> None:
        core = FakeCoreV1(job_events=[], pods=[_pod("trainer-a1b2c", None)], pod_events=[])
        custom_objects = _custom([])
        result = _collect(core, custom_objects=custom_objects)
        custom_objects.list_cluster_custom_object.assert_not_called()
        assert len(core.event_calls) == 2
        assert result["scope"] == {
            "pods": ["trainer-a1b2c"],
            "deleted_pods": [],
            "nodes": [],
            "nodeclaims": [],
            "errors": [],
        }

    def test_pods_with_malformed_metadata_contribute_nothing(self) -> None:
        core = FakeCoreV1(
            job_events=[],
            pods=[SimpleNamespace(metadata=None, spec=None), SimpleNamespace()],
            pod_events=[],
        )
        result = _collect(core)
        assert result["scope"]["pods"] == []
        assert result["scope"]["nodes"] == []

    def test_required_reads_raise(self) -> None:
        """The Job events and pod listing are the endpoint; their failure is the caller's."""
        core = MagicMock()
        core.list_namespaced_event.side_effect = RuntimeError("apiserver down")
        with pytest.raises(RuntimeError, match="apiserver down"):
            _collect(core)


class TestPodNameRecords:
    """Which pods count as the Job's: live, recorded by the Job, or shaped like its pods."""

    def test_deleted_pod_recorded_by_the_job_is_covered_without_any_pod_event(self) -> None:
        core = FakeCoreV1(
            job_events=[
                _event("Job", JOB, "SuccessfulDelete", "Deleted pod: trainer-gone1"),
                _event("Job", JOB, "SuccessfulCreate", "Created pod: trainer-live2"),
                _event("Job", JOB, "Completed", "Job completed"),
            ],
            pods=[_pod("trainer-live2", None)],
            pod_events=[],
        )
        result = _collect(core)
        assert result["scope"]["pods"] == ["trainer-gone1", "trainer-live2"]
        assert result["scope"]["deleted_pods"] == ["trainer-gone1"]

    def test_indexed_job_admits_the_index_shape(self) -> None:
        core = FakeCoreV1(
            job_events=[],
            pods=[],
            pod_events=[
                _event("Pod", "trainer-0-a1b2c", "Started", ""),
                _event("Pod", "trainer-12-d3e4f", "Started", ""),
                _event("Pod", "trainer-g5h6j", "Started", ""),
                _event("Pod", "trainer", "Started", "the Job's own name is not a pod"),
                _event("Pod", "trainer-a1b2c-extra", "Started", "too long"),
                _event("Pod", "retrainer-a1b2c", "Started", "different job"),
            ],
        )
        result = _collect(core, batch_v1=_batch("Indexed"))
        assert result["scope"]["pods"] == ["trainer-0-a1b2c", "trainer-12-d3e4f", "trainer-g5h6j"]

    def test_scheduled_record_that_does_not_parse_adds_no_node(self) -> None:
        core = FakeCoreV1(
            job_events=[],
            pods=[],
            pod_events=[
                _event("Pod", "trainer-a1b2c", "Scheduled", "unexpected wording"),
                _event("Pod", "trainer-a1b2c", "Nominated", "Pod should schedule on: "),
            ],
        )
        result = _collect(core)
        assert result["scope"]["nodes"] == []
        assert result["scope"]["nodeclaims"] == []


class TestNamePattern:
    def test_plain_and_indexed_shapes(self) -> None:
        plain = _job_pod_name_pattern("trainer", indexed=False)
        assert plain.match("trainer-a1b2c")
        assert not plain.match("trainer-0-a1b2c")
        assert not plain.match("trainer-A1B2C")
        indexed = _job_pod_name_pattern("trainer", indexed=True)
        assert indexed.match("trainer-a1b2c")
        assert indexed.match("trainer-0-a1b2c")
        assert not indexed.match("trainer-0-a1b2c-x")

    def test_regex_metacharacters_in_the_job_name_are_literal(self) -> None:
        pattern = _job_pod_name_pattern("a.b", indexed=False)
        assert pattern.match("a.b-a1b2c")
        assert not pattern.match("axb-a1b2c")

    def test_long_job_names_are_truncated_the_way_kubernetes_truncates_them(self) -> None:
        """A 60-character Job gets pods named from its first 58 characters."""
        job_name = "j" * 60
        pattern = _job_pod_name_pattern(job_name, indexed=False)
        assert pattern.match("j" * 58 + "a1b2c")
        assert not pattern.match(job_name + "-a1b2c")


class TestNodeClaimsForNodes:
    def test_non_dict_listing_matches_nothing(self) -> None:
        assert _nodeclaims_for_nodes(_custom(MagicMock()), {"i-0abc"}) == set()
        assert _nodeclaims_for_nodes(_custom({"items": "not-a-list"}), {"i-0abc"}) == set()

    def test_matches_by_status_node_name(self) -> None:
        custom_objects = _custom(
            [
                {"metadata": {"name": "gpu-1"}, "status": {"nodeName": "i-0abc"}},
                {"metadata": {"name": "gpu-2"}, "status": {"nodeName": "i-0def"}},
                {"metadata": {"name": ""}, "status": {"nodeName": "i-0abc"}},
            ]
        )
        assert _nodeclaims_for_nodes(custom_objects, {"i-0abc"}) == {"gpu-1"}


@pytest.mark.asyncio
async def test_route_returns_the_collected_events_and_scope() -> None:
    import gco.services.api_routes.jobs as routes

    processor = MagicMock()
    processor.cluster_id = "cluster"
    processor.region = "us-east-1"
    collected = {
        "events": [{"reason": "SpotInterrupted", "involvedObject": {"kind": "Node"}}],
        "scope": {"pods": [], "deleted_pods": [], "nodes": ["i-0"], "nodeclaims": [], "errors": []},
    }
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
        patch.object(routes, "_collect_job_events", return_value=collected) as collect,
    ):
        response = await routes.get_job_events("gco-jobs", "trainer")
    collect.assert_called_once_with(
        processor.core_v1,
        processor.batch_v1,
        processor.custom_objects,
        namespace="gco-jobs",
        job_name="trainer",
    )
    import json

    body = json.loads(response.body)
    assert body["count"] == 1
    assert body["events"] == collected["events"]
    assert body["scope"] == collected["scope"]

    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
        patch.object(routes, "_collect_job_events", side_effect=RuntimeError("boom")),
        pytest.raises(HTTPException) as error,
    ):
        await routes.get_job_events("gco-jobs", "trainer")
    assert error.value.status_code == 500
