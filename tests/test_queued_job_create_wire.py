"""The central queue's Job create, through the real kubernetes client to a fake API server.

Every other test of ``apply_queued_job`` mocks the client, so none of them can
see what kubernetes-client 37.0 changed: the generated typed methods now
validate a ``dict`` body against Pydantic models before sending it, and those
models type every resource quantity as a strict string. A user manifest that
says ``nvidia.com/gpu: 1`` (the examples' form, which the API server accepts)
is rejected client-side before any request is made. ``create_job_from_manifest``
exists to send the manifest as written; these tests run it, and the typed
method it replaces, against an in-process HTTP server that stands in for
kube-apiserver and records exactly what arrived.

Nothing here is mocked below ``ManifestProcessor``: the client's serializer,
``urllib3`` transport, response decoding and exception mapping all run for
real, under whichever client version is installed.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import urllib3
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from gco.k8s_objects import ObjectShapeError
from gco.services.manifest_processor import (
    ManifestProcessor,
    RetryableQueuedJobApplyError,
    create_job_from_manifest,
)

NAMESPACE = "gco-jobs"
JOBS_PATH = f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs"
BEHAVIOUR_ANNOTATION = "test.gco.io/fake-apiserver-behaviour"


class _Recorded:
    """One request the fake server saw."""

    def __init__(self, method: str, path: str, headers: dict[str, str], body: Any) -> None:
        self.method = method
        self.path = path
        self.headers = headers  # lower-cased names
        self.body = body


class FakeApiServer:
    """kube-apiserver for one Job create: records requests, answers by job name.

    The behaviour is selected by the ``BEHAVIOUR_ANNOTATION`` on the posted Job:

    - ``conflict``: 409 ``AlreadyExists``, the Status body kube-apiserver sends.
    - ``throttled``: 429.
    - ``slow``: sleeps past the client's timeout before answering.
    - anything else: 201 with the Job as the server would store it — a UID
      added, resource quantities canonicalized to strings (what a real
      apiserver does on the way out), and an extra field this client's
      models do not know, as a newer apiserver may add.
    """

    def __init__(self) -> None:
        self.requests: list[_Recorded] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # silence
                return

            def _send(self, code: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                server.requests.append(
                    _Recorded(
                        "GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None
                    )
                )
                name = self.path.rsplit("/", 1)[-1]
                self._send(
                    404,
                    {
                        "kind": "Status",
                        "apiVersion": "v1",
                        "metadata": {},
                        "status": "Failure",
                        "message": f'jobs.batch "{name}" not found',
                        "reason": "NotFound",
                        "details": {"name": name, "group": "batch", "kind": "jobs"},
                        "code": 404,
                    },
                )

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                body = json.loads(raw) if raw else None
                server.requests.append(
                    _Recorded(
                        "POST",
                        self.path,
                        {k.lower(): v for k, v in self.headers.items()},
                        body,
                    )
                )
                metadata = (body or {}).get("metadata") or {}
                # Behaviour is keyed on an annotation, not the name: the
                # processor renames a queued Job deterministically.
                name = (metadata.get("annotations") or {}).get(BEHAVIOUR_ANNOTATION, "")
                if name == "conflict":
                    self._send(
                        409,
                        {
                            "kind": "Status",
                            "apiVersion": "v1",
                            "metadata": {},
                            "status": "Failure",
                            "message": 'jobs.batch "conflict" already exists',
                            "reason": "AlreadyExists",
                            "details": {"name": "conflict", "group": "batch", "kind": "jobs"},
                            "code": 409,
                        },
                    )
                    return
                if name == "throttled":
                    self._send(
                        429,
                        {
                            "kind": "Status",
                            "apiVersion": "v1",
                            "status": "Failure",
                            "message": "Too many requests, please try again later.",
                            "reason": "TooManyRequests",
                            "code": 429,
                        },
                    )
                    return
                if name == "slow":
                    time.sleep(1.5)
                stored = json.loads(json.dumps(body))
                stored["metadata"]["uid"] = "server-assigned-uid"
                stored["metadata"]["resourceVersion"] = "12345"
                stored["metadata"]["futureMetadataField"] = {"unknown": "to this client"}
                for container in stored["spec"]["template"]["spec"]["containers"]:
                    resources = container.get("resources") or {}
                    for section in ("limits", "requests"):
                        if section in resources:
                            resources[section] = {k: str(v) for k, v in resources[section].items()}
                stored["status"] = {}
                self._send(201, stored)

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def last(self) -> _Recorded:
        return self.requests[-1]


@pytest.fixture
def api_server() -> Iterator[FakeApiServer]:
    server = FakeApiServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def api_client(api_server: FakeApiServer) -> Iterator[client.ApiClient]:
    configuration = client.Configuration()
    configuration.host = f"http://127.0.0.1:{api_server.port}"
    # The spelling load_incluster_config / load_kube_config use for a bearer token.
    configuration.api_key = {"authorization": "Bearer test-token"}
    with client.ApiClient(configuration) as api:
        yield api


def _gpu_job(behaviour: str = "create") -> dict[str, Any]:
    """A user manifest in the examples' style: unquoted quantities.

    ``behaviour`` selects the fake server's answer (see :class:`FakeApiServer`).
    """
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "trainer",
            "namespace": NAMESPACE,
            "annotations": {"gco.io/queue-job-id": "queue-1", BEHAVIOUR_ANNOTATION: behaviour},
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "main",
                            "image": "python:3.14",
                            "resources": {
                                "limits": {"nvidia.com/gpu": 1, "memory": "2Gi"},
                                "requests": {"cpu": 0.5, "memory": "2Gi"},
                            },
                        }
                    ],
                }
            },
        },
    }


class TestCreateJobFromManifest:
    def test_sends_the_manifest_as_written_and_returns_the_servers_job(
        self, api_server: FakeApiServer, api_client: client.ApiClient
    ) -> None:
        created = create_job_from_manifest(api_client, NAMESPACE, _gpu_job(), request_timeout=5)

        request = api_server.last
        assert (request.method, request.path) == ("POST", JOBS_PATH)
        # The body is the caller's JSON: the number is still a number.
        sent = request.body["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert sent == {
            "limits": {"nvidia.com/gpu": 1, "memory": "2Gi"},
            "requests": {"cpu": 0.5, "memory": "2Gi"},
        }
        # Explicit Content-Type (kubernetes-client/python#2782 leaves it off
        # the generated create), the bearer token, and a JSON Accept.
        assert request.headers["content-type"] == "application/json"
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["accept"] == "application/json"
        # The server's answer comes back whole, unknown fields included.
        assert created["metadata"]["uid"] == "server-assigned-uid"
        assert created["metadata"]["futureMetadataField"] == {"unknown": "to this client"}
        assert created["metadata"]["annotations"]["gco.io/queue-job-id"] == "queue-1"

    def test_unknown_fields_in_the_manifest_reach_the_server(
        self, api_server: FakeApiServer, api_client: client.ApiClient
    ) -> None:
        """A field newer than the client's models is the API server's to judge."""
        manifest = _gpu_job()
        manifest["spec"]["template"]["spec"]["schedulingGates"] = [{"name": "example.com/gate"}]
        manifest["spec"]["template"]["spec"]["fieldFromAFutureKubernetes"] = {"x": 1}
        create_job_from_manifest(api_client, NAMESPACE, manifest, request_timeout=5)
        pod_spec = api_server.last.body["spec"]["template"]["spec"]
        assert pod_spec["fieldFromAFutureKubernetes"] == {"x": 1}
        assert pod_spec["schedulingGates"] == [{"name": "example.com/gate"}]

    def test_api_errors_keep_their_status_for_the_callers_classification(
        self, api_server: FakeApiServer, api_client: client.ApiClient
    ) -> None:
        with pytest.raises(ApiException) as conflict:
            create_job_from_manifest(api_client, NAMESPACE, _gpu_job("conflict"), request_timeout=5)
        assert conflict.value.status == 409
        assert "already exists" in str(conflict.value.body)

        with pytest.raises(ApiException) as throttled:
            create_job_from_manifest(
                api_client, NAMESPACE, _gpu_job("throttled"), request_timeout=5
            )
        assert throttled.value.status == 429

    def test_timeout_is_a_transport_error_not_an_api_answer(
        self, api_server: FakeApiServer, api_client: client.ApiClient
    ) -> None:
        """The caller treats a non-ApiException as 'inconclusive, retry adoption'."""
        started = time.monotonic()
        with pytest.raises(urllib3.exceptions.HTTPError) as timeout:
            create_job_from_manifest(api_client, NAMESPACE, _gpu_job("slow"), request_timeout=0.3)
        assert not isinstance(timeout.value, ApiException)
        assert time.monotonic() - started < 1.5

    def test_refuses_a_non_object_answer(self, api_client: client.ApiClient) -> None:
        response = MagicMock()
        response.read.return_value = None
        api = MagicMock(wraps=api_client)
        api.param_serialize.return_value = ("POST", "http://x", {}, "{}", [])
        api.call_api.return_value = response
        api.response_deserialize.return_value = MagicMock(data=["not", "a", "job"])
        with pytest.raises(ObjectShapeError, match="the Job create response is not a JSON object"):
            create_job_from_manifest(api, NAMESPACE, _gpu_job(), request_timeout=5)


class TestTypedCreateForContrast:
    """Why the helper exists: the generated typed method, same server, same manifest.

    Pinned client (37.x): the ``dict`` body is validated against the Pydantic
    models before any request, and ``resources.limits`` is typed
    ``Dict[str, StrictStr]``, so the number is refused. The server never sees
    the request. (36.x forwarded the body untouched; the API server accepts
    it, since ``Quantity`` unmarshals from a JSON number.)
    """

    def test_typed_create_rejects_the_unquoted_quantity_before_any_request(
        self, api_server: FakeApiServer, api_client: client.ApiClient
    ) -> None:
        batch = client.BatchV1Api(api_client)
        with pytest.raises(ValueError, match=r"nvidia\.com/gpu") as rejected:
            batch.create_namespaced_job(namespace=NAMESPACE, body=_gpu_job(), _request_timeout=5)
        assert "Input should be a valid string" in str(rejected.value)
        assert api_server.requests == []


class TestApplyQueuedJobEndToEnd:
    """ManifestProcessor.apply_queued_job with a real client below it."""

    def _processor(self, api_client: client.ApiClient) -> ManifestProcessor:
        processor = object.__new__(ManifestProcessor)
        processor.cluster_id = "cluster"
        processor.region = "us-east-1"
        processor._k8s_timeout = 5
        processor.api_client = api_client
        processor.batch_v1 = client.BatchV1Api(api_client)
        processor.core_v1 = MagicMock()
        processor.custom_objects = MagicMock()
        processor.allowed_namespaces = {NAMESPACE}
        processor.validate_manifest = MagicMock(return_value=(True, None))  # type: ignore[method-assign]
        processor._inject_security_defaults = MagicMock()  # type: ignore[method-assign]
        return processor

    def test_creates_the_gpu_job_and_binds_the_servers_identity(
        self, api_server: FakeApiServer, api_client: client.ApiClient
    ) -> None:
        processor = self._processor(api_client)
        # The adoption read answers 404 (no GET route on the fake server), so
        # the processor proceeds to create.
        status = processor.apply_queued_job(_gpu_job(), NAMESPACE, "queue-1")

        assert status.status == "created"
        assert status.uid == "server-assigned-uid"
        assert status.namespace == NAMESPACE
        assert status.name == processor.queued_job_name("trainer", "queue-1")
        request = api_server.last
        assert request.path == JOBS_PATH
        assert request.headers["content-type"] == "application/json"
        container = request.body["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
        # The queue's own identity marks travelled with the manifest.
        assert request.body["metadata"]["name"] == status.name
        assert request.body["metadata"]["labels"]["gco.io/managed-by"] == "central-queue"
        assert request.body["metadata"]["annotations"]["gco.io/queue-job-id"] == "queue-1"

    def test_timeout_on_create_is_inconclusive_and_retryable(
        self, api_server: FakeApiServer, api_client: client.ApiClient
    ) -> None:
        processor = self._processor(api_client)
        processor._k8s_timeout = 0.3
        with pytest.raises(RetryableQueuedJobApplyError, match="inconclusive"):
            processor.apply_queued_job(_gpu_job("slow"), NAMESPACE, "queue-1")
