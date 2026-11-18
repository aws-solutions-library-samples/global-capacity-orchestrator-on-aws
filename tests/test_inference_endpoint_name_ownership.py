"""Endpoint names and object ownership: no endpoint touches another endpoint's objects.

The inference monitor names an endpoint's Kubernetes objects after it
(``{name}-canary``, ``-prefill``, ``-decode``, ``-proxy`` and more), so the
canary of ``llama`` and the primary Deployment of an endpoint named
``llama-canary`` share one name. Before the ``gco.io/endpoint`` label, every
pass for ``llama`` read that Deployment as a stale canary of its own and
deleted it (finding I2). These checks drive the monitor's real cleanup,
authorization and create paths against mocked Kubernetes clients, and the
deploy-time name rules that keep new endpoints from colliding at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from gco.services.inference_monitor import (
    ENDPOINT_NAME_LABEL,
    ForeignEndpointResourceError,
    InferenceMonitor,
    ReconcileFencedError,
)
from gco.services.inference_store import (
    ENDPOINT_NAME_MAX_LENGTH,
    check_endpoint_name_collisions,
    endpoint_object_names,
    validate_endpoint_name,
)

NAMESPACE = "gco-inference"
REGION = "us-east-1"
LEGACY_LABELS = {"project": "gco", "gco.io/type": "inference"}


def _record(name: str, *, deleting: bool = False) -> dict[str, Any]:
    record: dict[str, Any] = {
        "endpoint_name": name,
        "lifecycle_id": f"life-{name}",
        "region_generations": {REGION: f"gen-{name}"},
        "desired_state": "deleted" if deleting else "running",
        "target_regions": [REGION],
        "updated_at": "snapshot",
        "spec": {"image": "vllm/vllm-openai:v0.6.0"},
    }
    if deleting:
        record["deletion_generation"] = f"del-{name}"
    return record


def _monitor(*records: dict[str, Any]) -> InferenceMonitor:
    """A monitor reconciling ``records[0]``, whose store holds every record."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api"),
        patch("gco.services.inference_monitor.client.CoreV1Api"),
        patch("gco.services.inference_monitor.client.NetworkingV1Api"),
        patch("gco.services.inference_monitor.client.DiscoveryV1Api"),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        monitor = InferenceMonitor(
            cluster_id="cluster", region=REGION, store=MagicMock(), namespace=NAMESPACE
        )
    by_name = {record["endpoint_name"]: record for record in records}
    monitor.store.get_endpoint.side_effect = lambda name, **_kwargs: by_name.get(name)
    monitor._endpoint_names = frozenset(by_name)
    monitor._active_authority = monitor._authority_from_endpoint(records[0])
    return monitor


def _metadata(
    name: str, *, lifecycle: str | None, owner: str | None, uid: str
) -> client.V1ObjectMeta:
    labels = {"app": name, **LEGACY_LABELS}
    if owner is not None:
        labels[ENDPOINT_NAME_LABEL] = owner
    annotations = {} if lifecycle is None else {"gco.io/lifecycle-id": lifecycle}
    return client.V1ObjectMeta(
        name=name,
        namespace=NAMESPACE,
        labels=labels,
        annotations=annotations,
        uid=uid,
        resource_version="7",
    )


def _deployment(
    name: str, *, lifecycle: str | None, owner: str | None, uid: str = "uid-deploy"
) -> client.V1Deployment:
    return client.V1Deployment(
        metadata=_metadata(name, lifecycle=lifecycle, owner=owner, uid=uid),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": name}),
                spec=client.V1PodSpec(containers=[client.V1Container(name="inference")]),
            ),
        ),
    )


def _service(
    name: str, *, lifecycle: str | None, owner: str | None, uid: str = "uid-svc"
) -> client.V1Service:
    return client.V1Service(metadata=_metadata(name, lifecycle=lifecycle, owner=owner, uid=uid))


def _serve(monitor: InferenceMonitor, *objects: Any) -> None:
    """Answer Deployment and Service reads from ``objects``; everything else is absent."""
    deployments = {o.metadata.name: o for o in objects if isinstance(o, client.V1Deployment)}
    services = {o.metadata.name: o for o in objects if isinstance(o, client.V1Service)}

    def reader(found: dict[str, Any]) -> Any:
        def read(name: str, namespace: str, **_kwargs: Any) -> Any:
            assert namespace == NAMESPACE
            if name in found:
                return found[name]
            raise ApiException(status=404)

        return read

    monitor.apps_v1.read_namespaced_deployment.side_effect = reader(deployments)
    monitor.core_v1.read_namespaced_service.side_effect = reader(services)


# ---------------------------------------------------------------------------
# An endpoint without a canary never deletes the endpoint named {name}-canary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "owner", ["llama-canary", None], ids=["labelled", "unlabelled-from-an-older-release"]
)
def test_canary_cleanup_leaves_the_endpoint_named_like_the_canary_alone(
    owner: str | None,
) -> None:
    monitor = _monitor(_record("llama"), _record("llama-canary"))
    _serve(
        monitor,
        _deployment("llama-canary", lifecycle="life-llama-canary", owner=owner),
        _service("llama-canary", lifecycle="life-llama-canary", owner=owner),
    )

    monitor._cleanup_canary("llama", NAMESPACE)

    monitor.apps_v1.delete_namespaced_deployment.assert_not_called()
    monitor.core_v1.delete_namespaced_service.assert_not_called()
    monitor.apps_v1.patch_namespaced_deployment.assert_not_called()
    monitor.core_v1.patch_namespaced_service.assert_not_called()


def test_unlabelled_stale_canary_only_this_endpoint_derives_is_still_handed_off() -> None:
    """Without a name collision an older release's stale canary is removed as before."""
    monitor = _monitor(_record("llama"))
    _serve(monitor, _deployment("llama-canary", lifecycle="life-old", owner=None))

    with pytest.raises(ReconcileFencedError, match="stale authority handoff"):
        monitor._cleanup_canary("llama", NAMESPACE)

    [deleted] = monitor.apps_v1.delete_namespaced_deployment.call_args_list
    assert deleted.args == ("llama-canary", NAMESPACE)
    assert deleted.kwargs["body"].preconditions.uid == "uid-deploy"


def test_stale_object_labelled_for_this_endpoint_is_handed_off_despite_a_collision() -> None:
    """The label, not the name, decides: a stale canary labelled ``llama`` is llama's."""
    monitor = _monitor(_record("llama"), _record("llama-canary"))
    _serve(monitor, _deployment("llama-canary", lifecycle="life-old", owner="llama"))

    with pytest.raises(ReconcileFencedError, match="stale authority handoff"):
        monitor._cleanup_canary("llama", NAMESPACE)

    monitor.apps_v1.delete_namespaced_deployment.assert_called_once()


def test_own_unlabelled_object_is_claimed_even_when_another_endpoint_derives_its_name() -> None:
    """The lifecycle annotation proves ownership; the claim adds the label, metadata only."""
    monitor = _monitor(_record("llama-canary"), _record("llama"))
    live = _deployment("llama-canary", lifecycle="life-llama-canary", owner=None)
    claimed = _deployment("llama-canary", lifecycle="life-llama-canary", owner="llama-canary")
    claimed.metadata.annotations = dict(monitor._active_authority.annotations)
    monitor.apps_v1.read_namespaced_deployment.side_effect = [live, claimed]

    assert monitor._get_deployment("llama-canary", NAMESPACE) is claimed

    [claim] = monitor.apps_v1.patch_namespaced_deployment.call_args_list
    assert claim.kwargs["body"] == {
        "metadata": {
            "resourceVersion": "7",
            "annotations": monitor._active_authority.annotations,
            "labels": {ENDPOINT_NAME_LABEL: "llama-canary"},
        }
    }


def test_claim_of_an_object_already_labelled_sends_no_label() -> None:
    monitor = _monitor(_record("llama"))
    live = _deployment("llama", lifecycle="life-llama", owner="llama")
    claimed = _deployment("llama", lifecycle="life-llama", owner="llama")
    claimed.metadata.annotations = dict(monitor._active_authority.annotations)
    monitor.apps_v1.read_namespaced_deployment.side_effect = [live, claimed]

    monitor._get_deployment("llama", NAMESPACE)

    [claim] = monitor.apps_v1.patch_namespaced_deployment.call_args_list
    assert "labels" not in claim.kwargs["body"]["metadata"]


def test_generated_admin_secret_claim_keeps_its_exact_ownership_labels() -> None:
    """The Secret's ownership check compares its labels exactly, so no label is added."""
    monitor = _monitor(_record("foo"))
    live = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name="foo-admin",
            labels={"app": "foo-admin", **LEGACY_LABELS},
            annotations={"gco.io/lifecycle-id": "life-foo"},
            uid="uid-secret",
            resource_version="3",
        ),
        type="Opaque",
        data={"ADMIN_API_KEY": "a2V5"},
    )
    claimed = live.model_copy(deep=True)
    claimed.metadata.annotations = dict(monitor._active_authority.annotations)
    monitor.core_v1.read_namespaced_secret.side_effect = [live, claimed]

    assert monitor._provision_admin_api_key_secret("foo-admin", NAMESPACE, "life-foo") == (
        "foo-admin"
    )

    [claim] = monitor.core_v1.patch_namespaced_secret.call_args_list
    assert "labels" not in claim.kwargs["body"]["metadata"]


def test_an_endpoint_name_no_label_can_carry_is_not_stamped() -> None:
    record = _record("x" * 64)
    monitor = _monitor(record)

    assert monitor._owned_labels({"app": "x"}) == {"app": "x"}


# ---------------------------------------------------------------------------
# Deleting an endpoint leaves the other endpoint's objects and completes
# ---------------------------------------------------------------------------


def test_deleting_an_endpoint_leaves_the_other_endpoints_objects_and_completes() -> None:
    monitor = _monitor(_record("llama", deleting=True), _record("llama-canary"))
    monitor.discovery_v1 = MagicMock()
    _serve(
        monitor,
        _deployment("llama-canary", lifecycle="life-llama-canary", owner="llama-canary"),
        _service("llama-canary", lifecycle="life-llama-canary", owner="llama-canary"),
    )
    not_found = ApiException(status=404)
    monitor.core_v1.read_namespaced_secret.side_effect = not_found
    # Any object labelled for another endpoint is left alone, whatever its kind.
    foreign_config_map = client.V1ConfigMap(
        metadata=_metadata("llama-tls-proxy", lifecycle="life-x", owner="llama-tls", uid="u")
    )

    def read_config_map(name: str, namespace: str, **_kwargs: Any) -> Any:
        if name == "llama-tls-proxy":
            return foreign_config_map
        raise not_found

    monitor.core_v1.read_namespaced_config_map.side_effect = read_config_map
    monitor.networking_v1.read_namespaced_ingress.side_effect = not_found
    # The other endpoint's running pods and its Service's endpoint slice.
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name="llama-canary-abc",
            labels={"app": "llama-canary", **LEGACY_LABELS},
            owner_references=[SimpleNamespace(kind="ReplicaSet", name="llama-canary-rs")],
        )
    )
    replica_set = SimpleNamespace(
        metadata=SimpleNamespace(
            name="llama-canary-rs",
            labels={"app": "llama-canary", **LEGACY_LABELS},
            owner_references=[SimpleNamespace(kind="Deployment", name="llama-canary")],
        )
    )
    endpoint_slice = SimpleNamespace(
        metadata=SimpleNamespace(
            name="llama-canary-x", labels={"kubernetes.io/service-name": "llama-canary"}
        )
    )
    monitor.apps_v1.list_namespaced_replica_set.return_value = MagicMock(items=[replica_set])
    monitor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
    monitor.core_v1.list_namespaced_endpoints.return_value = MagicMock(
        items=[SimpleNamespace(metadata=SimpleNamespace(name="llama-canary", labels={}))]
    )
    monitor.discovery_v1.list_namespaced_endpoint_slice.return_value = MagicMock(
        items=[endpoint_slice]
    )
    hpa = MagicMock()
    hpa.read_namespaced_horizontal_pod_autoscaler.side_effect = not_found
    custom = MagicMock()
    custom.get_namespaced_custom_object.side_effect = not_found

    with (
        patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=hpa),
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
    ):
        result = monitor._delete_resources("llama", NAMESPACE, expected_lifecycle_id="life-llama")

    assert result.complete is True, result
    monitor.apps_v1.delete_namespaced_deployment.assert_not_called()
    monitor.core_v1.delete_namespaced_service.assert_not_called()
    monitor.core_v1.delete_namespaced_config_map.assert_not_called()


# ---------------------------------------------------------------------------
# Ingresses older releases created are removed with the endpoint (finding I5)
# ---------------------------------------------------------------------------


def _legacy_ingress(name: str, app: str, *, role: str | None = None) -> client.V1Ingress:
    """An Ingress as releases up to v4.1.0 created it: monitor labels, no lifecycle."""
    labels = {"app": app, **LEGACY_LABELS}
    if role is not None:
        labels["gco.io/role"] = role
    return client.V1Ingress(
        metadata=client.V1ObjectMeta(
            name=name, namespace=NAMESPACE, labels=labels, uid=f"uid-{name}", resource_version="4"
        )
    )


class _IngressApi:
    """Ingress reads, metadata patches and UID-preconditioned deletes over live objects."""

    def __init__(self, monitor: InferenceMonitor, *ingresses: client.V1Ingress) -> None:
        self.live = {ingress.metadata.name: ingress for ingress in ingresses}
        self.read_names: list[str] = []
        self.deleted: list[str] = []
        networking = monitor.networking_v1
        networking.read_namespaced_ingress.side_effect = self.read
        networking.patch_namespaced_ingress.side_effect = self.patch
        networking.delete_namespaced_ingress.side_effect = self.delete

    def read(self, name: str, namespace: str, **_kwargs: Any) -> client.V1Ingress:
        assert namespace == NAMESPACE
        self.read_names.append(name)
        if name not in self.live:
            raise ApiException(status=404)
        return self.live[name]

    def patch(self, name: str, namespace: str, *, body: Any, **_kwargs: Any) -> None:
        claimed = self.live[name].model_copy(deep=True)
        metadata = body["metadata"]
        assert metadata["resourceVersion"] == claimed.metadata.resource_version
        claimed.metadata.annotations = dict(metadata["annotations"])
        claimed.metadata.labels = {**claimed.metadata.labels, **metadata.get("labels", {})}
        claimed.metadata.resource_version = str(int(claimed.metadata.resource_version) + 1)
        self.live[name] = claimed

    def delete(self, name: str, namespace: str, *, body: Any, **_kwargs: Any) -> None:
        current = self.live[name]
        assert body.preconditions.uid == current.metadata.uid
        assert body.preconditions.resource_version == current.metadata.resource_version
        del self.live[name]
        self.deleted.append(name)


def _delete_with_everything_else_absent(monitor: InferenceMonitor, name: str) -> Any:
    """Run the endpoint cleanup with every kind but Ingresses already absent."""
    not_found = ApiException(status=404)
    monitor.discovery_v1 = MagicMock()
    for read in (
        monitor.apps_v1.read_namespaced_deployment,
        monitor.core_v1.read_namespaced_service,
        monitor.core_v1.read_namespaced_config_map,
        monitor.core_v1.read_namespaced_secret,
    ):
        read.side_effect = not_found
    for listing in (
        monitor.apps_v1.list_namespaced_replica_set,
        monitor.core_v1.list_namespaced_pod,
        monitor.core_v1.list_namespaced_endpoints,
        monitor.discovery_v1.list_namespaced_endpoint_slice,
    ):
        listing.return_value = MagicMock(items=[])
    hpa = MagicMock()
    hpa.read_namespaced_horizontal_pod_autoscaler.side_effect = not_found
    custom = MagicMock()
    custom.get_namespaced_custom_object.side_effect = not_found
    with (
        patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=hpa),
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
    ):
        return monitor._delete_resources(name, NAMESPACE, expected_lifecycle_id=f"life-{name}")


def test_deleting_an_endpoint_removes_the_ingresses_older_releases_created() -> None:
    monitor = _monitor(_record("llama", deleting=True))
    ingresses = _IngressApi(
        monitor,
        _legacy_ingress("inference-llama", "llama"),
        _legacy_ingress("inference-llama-proxy", "llama-proxy", role="proxy"),
    )

    result = _delete_with_everything_else_absent(monitor, "llama")

    assert result.complete is True, result
    assert ingresses.deleted == ["inference-llama", "inference-llama-proxy"]
    # No release ever created an Ingress under the endpoint's object names.
    assert set(ingresses.read_names) == {"inference-llama", "inference-llama-proxy"}


def test_legacy_ingress_of_an_endpoint_named_like_another_kinds_object_is_removed() -> None:
    """``inference-llama`` names that endpoint's Deployment, never an Ingress of it."""
    monitor = _monitor(_record("llama", deleting=True), _record("inference-llama"))
    ingresses = _IngressApi(monitor, _legacy_ingress("inference-llama", "llama"))

    result = _delete_with_everything_else_absent(monitor, "llama")

    assert result.complete is True, result
    assert ingresses.deleted == ["inference-llama"]


def test_unlabelled_legacy_ingress_another_endpoint_also_derives_is_left_alone() -> None:
    """``inference-foo-proxy`` is foo's PD-proxy Ingress or the Ingress of ``foo-proxy``."""
    monitor = _monitor(_record("foo", deleting=True), _record("foo-proxy"))
    ingresses = _IngressApi(
        monitor,
        _legacy_ingress("inference-foo", "foo"),
        _legacy_ingress("inference-foo-proxy", "foo-proxy"),
    )

    result = _delete_with_everything_else_absent(monitor, "foo")

    assert result.complete is True, result
    assert ingresses.deleted == ["inference-foo"]
    monitor.networking_v1.patch_namespaced_ingress.assert_called_once()
    assert "inference-foo-proxy" in ingresses.live


# ---------------------------------------------------------------------------
# Creating an object never takes over another endpoint's object
# ---------------------------------------------------------------------------


def test_pd_proxy_never_patches_a_classic_endpoint_named_like_the_proxy() -> None:
    monitor = _monitor(_record("foo"), _record("foo-proxy"))
    monitor.core_v1.read_namespaced_secret.return_value = client.V1Secret(
        string_data={"ADMIN_API_KEY": "router-key"}
    )
    monitor.apps_v1.create_namespaced_deployment.side_effect = ApiException(status=409)
    _serve(monitor, _deployment("foo-proxy", lifecycle="life-foo-proxy", owner="foo-proxy"))
    spec = {
        "image": "vllm/vllm-openai:v0.6.0",
        "mooncake": {
            "mode": "disaggregated",
            "proxy": {"image": "vllm/vllm-openai:v0.6.0", "admin_api_key_secret": "foo-admin"},
        },
    }

    with pytest.raises(ForeignEndpointResourceError, match="belongs to endpoint foo-proxy"):
        monitor._create_pd_proxy("foo", NAMESPACE, spec, {"lifecycle_id": "life-foo"})

    monitor.apps_v1.patch_namespaced_deployment.assert_not_called()
    monitor.apps_v1.delete_namespaced_deployment.assert_not_called()


def test_created_objects_carry_the_endpoint_label_on_metadata_not_pod_templates() -> None:
    monitor = _monitor(_record("llama"))
    # The post-create read sees the object this pass created.
    created = SimpleNamespace(
        metadata=SimpleNamespace(
            annotations=monitor._active_authority.annotations, uid="uid-1", resource_version="1"
        )
    )
    monitor.apps_v1.read_namespaced_deployment.return_value = created
    monitor.core_v1.read_namespaced_service.return_value = created

    monitor._create_deployment("llama", NAMESPACE, _record("llama")["spec"])
    monitor._create_service("llama", NAMESPACE, {})

    [created] = monitor.apps_v1.create_namespaced_deployment.call_args_list
    deployment = created.args[1]
    assert deployment.metadata.labels[ENDPOINT_NAME_LABEL] == "llama"
    assert ENDPOINT_NAME_LABEL not in deployment.spec.template.metadata.labels
    [service_call] = monitor.core_v1.create_namespaced_service.call_args_list
    assert service_call.args[1].metadata.labels[ENDPOINT_NAME_LABEL] == "llama"


# ---------------------------------------------------------------------------
# Deploy-time name rules
# ---------------------------------------------------------------------------


def test_derived_names_are_exactly_the_monitor_inventory() -> None:
    inventory = InferenceMonitor._endpoint_resource_inventory("llama")
    names = {
        *inventory.deployments,
        *inventory.services,
        *inventory.horizontal_pod_autoscalers,
        *inventory.scaled_objects,
        *inventory.config_maps,
        *inventory.legacy_http_routes,
        inventory.generated_admin_secret,
    }

    assert endpoint_object_names("llama") == names
    # The Ingresses of releases up to v4.1.0 are a kind no current object
    # shares; their names are compared among legacy Ingress names only.
    assert inventory.legacy_ingresses == ("inference-llama", "inference-llama-proxy")


@pytest.mark.parametrize(
    "name",
    ["my-llama-3", "a", "q" * ENDPOINT_NAME_MAX_LENGTH],
)
def test_valid_endpoint_names(name: str) -> None:
    validate_endpoint_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Llama",
        "3b-model",
        "llama-",
        "llama_3",
        "llama.v2",
        "q" * (ENDPOINT_NAME_MAX_LENGTH + 1),
    ],
)
def test_invalid_endpoint_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid endpoint name"):
        validate_endpoint_name(name)


def test_name_budget_leaves_room_for_the_longest_role_suffix() -> None:
    assert ENDPOINT_NAME_MAX_LENGTH + len("-prefill") == 63


@pytest.mark.parametrize(
    ("new", "existing", "shared"),
    [
        ("llama-canary", "llama", "llama-canary"),
        ("llama", "llama-canary", "llama-canary"),
        ("foo-proxy", "foo", "foo-proxy"),
        ("foo", "foo-decode", "foo-decode"),
        ("a-pd", "a", "a-pd-proxy"),
        ("keda-hpa-x", "x", "keda-hpa-x"),
    ],
)
def test_colliding_names_are_refused_in_both_directions(
    new: str, existing: str, shared: str
) -> None:
    with pytest.raises(ValueError, match=f"collides with endpoint '{existing}'.*'{shared}'"):
        check_endpoint_name_collisions(new, ["unrelated", existing])


def test_distinct_names_and_the_same_name_pass_the_collision_check() -> None:
    # The same name is create_endpoint's "already exists", not a collision.
    check_endpoint_name_collisions("llama", ["llama", "mistral", "llama2"])


@pytest.fixture
def manager() -> Any:
    from cli.inference import InferenceManager

    config = MagicMock()
    config.global_region = "us-east-2"
    with (
        patch("cli.inference.get_aws_client", return_value=MagicMock()),
        patch("cli.inference.get_config", return_value=config),
    ):
        built = InferenceManager(config=config)
    built._get_store = MagicMock(return_value=MagicMock())
    return built


def test_deploy_refuses_a_name_colliding_with_an_existing_endpoint(manager: Any) -> None:
    store = manager._get_store.return_value
    store.list_endpoints.return_value = [{"endpoint_name": "llama"}, {"no": "name"}]

    with pytest.raises(ValueError, match="collides with endpoint 'llama'"):
        manager.deploy("llama-canary", image="img:v1", target_regions=[REGION])

    store.create_endpoint.assert_not_called()


def test_deploy_refuses_an_invalid_name_before_any_lookup(manager: Any) -> None:
    with pytest.raises(ValueError, match="Invalid endpoint name"):
        manager.deploy("My_Model", image="img:v1", target_regions=[REGION])

    manager._get_store.assert_not_called()


def test_deploy_registers_a_name_that_collides_with_nothing(manager: Any) -> None:
    store = manager._get_store.return_value
    store.list_endpoints.return_value = [{"endpoint_name": "mistral"}]
    store.create_endpoint.return_value = {"endpoint_name": "llama"}

    assert manager.deploy("llama", image="img:v1", target_regions=[REGION]) == {
        "endpoint_name": "llama"
    }
