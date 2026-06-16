"""End-to-end reconciliation of a distributed inference endpoint.

These checks drive the monitor's real entry point, ``_reconcile_running``, the
same way the poll loop does: a persisted endpoint spec flows through the store,
into the monitor, and out as Kubernetes objects. Nothing in the distributed
materialization path is stubbed — the in-region service resolution, the
regional-scope boundary check, the shared-master gate, the transport ConfigMap,
the role Deployments, the prefill-decode proxy, and the role-keyed status write
all run for real against a stand-in Kubernetes API and a stand-in DynamoDB
table.

Two behaviors are pinned:

- Reconciling a split prefill/decode endpoint lays down both role Deployments,
  the proxy Deployment, the proxy Service, the proxy Ingress, and writes a
  role-keyed status that breaks the endpoint down by prefill and decode.
- Changing the prefill/decode counts on the persisted spec and reconciling
  again rescales the existing role Deployments to the new counts.

The stand-in Kubernetes API is stateful: objects created on the first pass are
visible on the second, so a re-reconcile takes the scale-existing branch rather
than recreating. The stand-in DynamoDB table is the in-memory backing for a
real ``InferenceEndpointStore``, so the spec change genuinely round-trips
through the store layer before the monitor reads it back.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from kubernetes.client.rest import ApiException

NAMESPACE = "gco-inference"
OWN_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Stand-in Kubernetes API (stateful)
# ---------------------------------------------------------------------------


def _name_of(obj) -> str:
    """Pull the metadata name off a Kubernetes object or a plain mapping."""
    meta = getattr(obj, "metadata", None)
    if meta is not None:
        return meta.name
    return obj["metadata"]["name"]


class _FakeAppsApi:
    """In-memory stand-in for the apps API used by the monitor.

    Deployments and StatefulSets created here persist for the lifetime of the
    instance, so a later read sees them and a re-reconcile scales in place. The
    shared master StatefulSet always reports one Ready replica, so the master
    gate opens immediately.
    """

    def __init__(self) -> None:
        self.deployments: dict[str, object] = {}
        self.stateful_sets: dict[str, object] = {}

    def read_namespaced_deployment(self, name, namespace, **_kw):
        if name not in self.deployments:
            raise ApiException(status=404, reason="Not Found")
        return self.deployments[name]

    def create_namespaced_deployment(self, namespace, deployment, **_kw):
        name = _name_of(deployment)
        if name in self.deployments:
            raise ApiException(status=409, reason="Conflict")
        self.deployments[name] = deployment
        return deployment

    def patch_namespaced_deployment(self, name, namespace, body=None, **_kw):
        deployment = self.deployments[name]
        replicas = (body or {}).get("spec", {}).get("replicas")
        if replicas is not None:
            deployment.spec.replicas = replicas
        return deployment

    def create_namespaced_stateful_set(self, namespace, stateful_set, **_kw):
        name = _name_of(stateful_set)
        if name in self.stateful_sets:
            raise ApiException(status=409, reason="Conflict")
        self.stateful_sets[name] = stateful_set
        return stateful_set

    def read_namespaced_stateful_set_status(self, name, namespace, **_kw):
        if name not in self.stateful_sets:
            raise ApiException(status=404, reason="Not Found")
        # The shared master is up: one Ready replica opens the gate.
        return SimpleNamespace(status=SimpleNamespace(ready_replicas=1))


class _FakeCoreApi:
    """In-memory stand-in for the core API used by the monitor."""

    def __init__(self) -> None:
        self.services: dict[str, object] = {}
        self.config_maps: dict[str, object] = {}
        # The proxy admin key Secret is present with a non-empty value, so the
        # proxy front is allowed to materialize.
        self._secret = SimpleNamespace(
            string_data={"ADMIN_API_KEY": "an-admin-key"}, data=None
        )

    def create_namespaced_service(self, namespace, service, **_kw):
        name = _name_of(service)
        if name in self.services:
            raise ApiException(status=409, reason="Conflict")
        self.services[name] = service
        return service

    def read_namespaced_secret(self, name, namespace, **_kw):
        return self._secret

    def create_namespaced_config_map(self, namespace, config_map, **_kw):
        name = _name_of(config_map)
        if name in self.config_maps:
            raise ApiException(status=409, reason="Conflict")
        self.config_maps[name] = config_map
        return config_map

    def patch_namespaced_config_map(self, name, namespace, config_map, **_kw):
        self.config_maps[name] = config_map
        return config_map


class _FakeNetworkingApi:
    """In-memory stand-in for the networking API used by the monitor."""

    def __init__(self) -> None:
        self.ingresses: dict[str, object] = {}
        self.network_policies: dict[str, object] = {}

    def create_namespaced_network_policy(self, namespace, policy, **_kw):
        name = _name_of(policy)
        if name in self.network_policies:
            raise ApiException(status=409, reason="Conflict")
        self.network_policies[name] = policy
        return policy

    def create_namespaced_ingress(self, namespace, ingress, **_kw):
        name = _name_of(ingress)
        if name in self.ingresses:
            raise ApiException(status=409, reason="Conflict")
        self.ingresses[name] = ingress
        return ingress

    def patch_namespaced_ingress(self, name, namespace, ingress, **_kw):
        self.ingresses[name] = ingress
        return ingress


# ---------------------------------------------------------------------------
# Stand-in DynamoDB table (in-memory) backing a real store
# ---------------------------------------------------------------------------


def _conditional_check_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        "PutItem",
    )


class _FakeDynamoTable:
    """In-memory backing for the operations the endpoint store performs.

    Supports the put/get/update operations the store uses to create an
    endpoint, read it back, change its spec, and record per-region status.
    """

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def put_item(self, Item, ConditionExpression=None, **_kw):  # noqa: N803
        key = Item["endpoint_name"]
        if ConditionExpression and "attribute_not_exists" in ConditionExpression:
            if key in self.items:
                raise _conditional_check_error()
        self.items[key] = copy.deepcopy(Item)
        return {}

    def get_item(self, Key, **_kw):  # noqa: N803
        item = self.items.get(Key["endpoint_name"])
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def update_item(
        self,
        Key,  # noqa: N803
        UpdateExpression,  # noqa: N803
        ExpressionAttributeValues=None,  # noqa: N803
        ExpressionAttributeNames=None,  # noqa: N803
        ConditionExpression=None,  # noqa: N803
        ReturnValues=None,  # noqa: N803
        **_kw,
    ):
        key = Key["endpoint_name"]
        if ConditionExpression and "attribute_exists" in ConditionExpression:
            if key not in self.items:
                raise _conditional_check_error()
        item = self.items.setdefault(key, {"endpoint_name": key})
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}

        if "region_status.#r" in UpdateExpression:
            region = names["#r"]
            item.setdefault("region_status", {})[region] = copy.deepcopy(values[":s"])
        elif "spec.replicas" in UpdateExpression:
            item.setdefault("spec", {})["replicas"] = values[":r"]
        elif "SET spec = :s" in UpdateExpression:
            item["spec"] = copy.deepcopy(values[":s"])
            if ":ds" in values:
                item["desired_state"] = values[":ds"]
        elif "desired_state = :s" in UpdateExpression:
            item["desired_state"] = values[":s"]

        if ":u" in values:
            item["updated_at"] = values[":u"]

        if ReturnValues == "ALL_NEW":
            return {"Attributes": copy.deepcopy(item)}
        return {}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_monitor(apps, core, networking, store, region: str = OWN_REGION):
    """Build a monitor wired to the stand-in clients and store."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api"),
        patch("gco.services.inference_monitor.client.CoreV1Api"),
        patch("gco.services.inference_monitor.client.NetworkingV1Api"),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        from gco.services.inference_monitor import InferenceMonitor

        monitor = InferenceMonitor(
            cluster_id="test-cluster",
            region=region,
            store=store,
            namespace=NAMESPACE,
            reconcile_interval=5,
        )
    monitor.apps_v1 = apps
    monitor.core_v1 = core
    monitor.networking_v1 = networking
    return monitor


def _disaggregated_spec(prefill: int, decode: int) -> dict:
    """A split-mode endpoint spec with the given prefill/decode counts."""
    return {
        "image": "vllm/vllm-openai:v0.6.0",
        "mooncake": {
            "mode": "disaggregated",
            "topology": {"prefill": prefill, "decode": decode},
            "transfer": {"protocol": "rdma"},
            "proxy": {
                "image": "gco/pd-proxy:pinned",
                "admin_api_key_secret": "chat-admin",
            },
        },
    }


class _RecordingStore:
    """A minimal store that records the status writes the monitor makes."""

    def __init__(self) -> None:
        self.status_writes: list[dict] = []

    def update_region_status(
        self,
        endpoint_name,
        region,
        state,
        replicas_ready=0,
        replicas_desired=0,
        error=None,
        extra=None,
    ):
        self.status_writes.append(
            {
                "endpoint": endpoint_name,
                "region": region,
                "state": state,
                "replicas_ready": replicas_ready,
                "replicas_desired": replicas_desired,
                "error": error,
                "extra": extra,
            }
        )

    def update_desired_state(self, endpoint_name, desired_state):  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Reconcile a split endpoint front to back
# ---------------------------------------------------------------------------


def test_split_endpoint_materializes_roles_proxy_and_role_keyed_status():
    """A split endpoint lays down both roles, the proxy front, and role status.

    Reconciling a prefill/decode endpoint through the monitor's entry point
    creates the ``chat-prefill`` and ``chat-decode`` role Deployments, the
    ``chat-proxy`` Deployment and Service, and the proxy Ingress that routes
    ``/v1`` to the proxy. The status written back breaks the endpoint down by
    role, with each role's desired count reflecting the requested counts.
    """
    apps = _FakeAppsApi()
    core = _FakeCoreApi()
    networking = _FakeNetworkingApi()
    store = _RecordingStore()
    monitor = _make_monitor(apps, core, networking, store)

    spec = _disaggregated_spec(prefill=2, decode=3)
    endpoint = {
        "endpoint_name": "chat",
        "desired_state": "deploying",
        "target_regions": [OWN_REGION],
        "spec": spec,
        "namespace": NAMESPACE,
    }

    action = asyncio.run(
        monitor._reconcile_running("chat", NAMESPACE, spec, endpoint)
    )

    # The distributed branch owned the reconcile.
    assert action is not None
    assert action["action"] == "reconcile_mooncake"
    assert action["endpoint"] == "chat"

    # Both role Deployments exist; the bare single-instance name does not.
    assert "chat-prefill" in apps.deployments
    assert "chat-decode" in apps.deployments
    assert "chat" not in apps.deployments
    assert apps.deployments["chat-prefill"].spec.replicas == 2
    assert apps.deployments["chat-decode"].spec.replicas == 3

    # The proxy front is materialized: Deployment, Service, and Ingress.
    assert "chat-proxy" in apps.deployments
    assert "chat-proxy" in core.services
    assert "inference-chat-proxy" in networking.ingresses

    # The proxy Ingress routes only the /v1 serving prefix to the proxy Service.
    ingress = networking.ingresses["inference-chat-proxy"]
    routes = [p for rule in ingress.spec.rules for p in rule.http.paths]
    assert [r.path for r in routes] == ["/v1"]
    assert routes[0].backend.service.name == "chat-proxy"

    # The shared transport ConfigMap landed before the roles.
    assert "chat-mooncake" in core.config_maps

    # The status write is role-keyed: prefill and decode each carry their
    # observed/desired counts, and the desired counts match the request.
    role_writes = [w for w in store.status_writes if (w["extra"] or {}).get("roles")]
    assert role_writes, "expected a role-keyed status write"
    roles = role_writes[-1]["extra"]["roles"]
    assert roles["prefill"]["desired"] == 2
    assert roles["decode"]["desired"] == 3


# ---------------------------------------------------------------------------
# A persisted topology change re-reconciles replica counts
# ---------------------------------------------------------------------------


def test_topology_change_rescales_role_deployments():
    """Changing the persisted counts rescales the existing role Deployments.

    The endpoint is created in the store with a 2p3d shape and reconciled, so
    both role Deployments exist at those counts. The spec is then updated in the
    store to a 5p1d shape — a genuine round-trip through the store layer — and
    the monitor reconciles the reloaded spec. Because the role Deployments
    already exist, the second pass scales them in place to the new counts rather
    than recreating them.
    """
    apps = _FakeAppsApi()
    core = _FakeCoreApi()
    networking = _FakeNetworkingApi()

    table = _FakeDynamoTable()
    with patch("gco.services.inference_store.boto3") as mock_boto3:
        mock_boto3.resource.return_value.Table.return_value = table
        from gco.services.inference_store import InferenceEndpointStore

        store = InferenceEndpointStore(table_name="endpoints", region=OWN_REGION)

        monitor = _make_monitor(apps, core, networking, store)

        store.create_endpoint(
            "chat",
            _disaggregated_spec(prefill=2, decode=3),
            [OWN_REGION],
            namespace=NAMESPACE,
        )

        first = store.get_endpoint("chat")
        asyncio.run(
            monitor._reconcile_running("chat", NAMESPACE, first["spec"], first)
        )

        assert apps.deployments["chat-prefill"].spec.replicas == 2
        assert apps.deployments["chat-decode"].spec.replicas == 3

        # Persist a topology change through the store (the spec round-trips
        # through the stand-in DynamoDB table).
        changed = copy.deepcopy(first["spec"])
        changed["mooncake"]["topology"] = {"prefill": 5, "decode": 1}
        store.update_spec("chat", changed)

        reloaded = store.get_endpoint("chat")
        assert reloaded["spec"]["mooncake"]["topology"] == {"prefill": 5, "decode": 1}

        asyncio.run(
            monitor._reconcile_running("chat", NAMESPACE, reloaded["spec"], reloaded)
        )

    # The same Deployment objects were rescaled in place to the new counts.
    assert apps.deployments["chat-prefill"].spec.replicas == 5
    assert apps.deployments["chat-decode"].spec.replicas == 1
