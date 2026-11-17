"""``gco/services/k8s_client.py``: every Kubernetes request carries a timeout.

kubernetes-client 37 builds the ``urllib3`` timeout only from a call's own
``_request_timeout``; ``None``, the default of every generated method and of the
dynamic client, reaches urllib3 as no timeout at all, and ``Configuration`` has
no setting to fall back on. These tests intercept the client at its urllib3
pool and check what timeout each kind of request arrives with, then run the
two behaviours that matter against real sockets: a stalled API server fails
the call instead of hanging it, and a streamed body that keeps arriving is
read to the end.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import urllib3
from kubernetes import client, dynamic

from gco.services.k8s_client import (
    DEFAULT_K8S_API_TIMEOUT_SECONDS,
    BoundedApiClient,
    k8s_api_timeout_from_env,
)

HOST = "https://kubernetes.test.invalid"


class RecordingPool:
    """Stands in for the urllib3 pool: records each request, answers with canned JSON."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> urllib3.HTTPResponse:
        path = url.removeprefix(HOST).split("?", 1)[0]
        self.requests.append((method, path, kwargs.get("timeout")))
        payload = self.routes[path]
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return urllib3.HTTPResponse(
            body=body,
            status=200,
            headers={"Content-Type": "application/json"},
            preload_content=False,
        )

    def timeouts(self) -> list[Any]:
        return [timeout for _, _, timeout in self.requests]


def _client(pool: RecordingPool, timeout: float = 7) -> BoundedApiClient:
    configuration = client.Configuration()
    configuration.host = HOST
    api = BoundedApiClient(configuration, default_request_timeout=timeout)
    api.rest_client.pool_manager = pool  # type: ignore[assignment]
    return api


def _total(timeout: Any) -> float | None:
    assert isinstance(timeout, urllib3.Timeout), f"unbounded request: timeout={timeout!r}"
    return timeout.total


NAMESPACES = {"apiVersion": "v1", "kind": "NamespaceList", "metadata": {}, "items": []}


class TestTypedMethods:
    def test_a_call_without_a_timeout_reaches_urllib3_bounded(self) -> None:
        pool = RecordingPool({"/api/v1/namespaces": NAMESPACES})
        client.CoreV1Api(_client(pool, timeout=7)).list_namespace(limit=1)
        assert [_total(t) for t in pool.timeouts()] == [7]

    def test_a_plain_api_client_sends_no_timeout(self) -> None:
        """The gap the bounded client closes, pinned against the installed client."""
        pool = RecordingPool({"/api/v1/namespaces": NAMESPACES})
        configuration = client.Configuration()
        configuration.host = HOST
        plain = client.ApiClient(configuration)
        plain.rest_client.pool_manager = pool  # type: ignore[assignment]
        client.CoreV1Api(plain).list_namespace(limit=1)
        assert pool.timeouts() == [None]

    def test_a_callers_total_timeout_is_kept(self) -> None:
        pool = RecordingPool({"/api/v1/namespaces": NAMESPACES})
        client.CoreV1Api(_client(pool, timeout=7)).list_namespace(_request_timeout=3)
        assert [_total(t) for t in pool.timeouts()] == [3]

    def test_a_callers_connect_and_read_pair_is_kept(self) -> None:
        """A watch or followed log passes a longer read timeout; it must survive."""
        pool = RecordingPool({"/api/v1/namespaces": NAMESPACES})
        client.CoreV1Api(_client(pool, timeout=7)).list_namespace(_request_timeout=(2, 600))
        (timeout,) = pool.timeouts()
        assert isinstance(timeout, urllib3.Timeout)
        assert (timeout.connect_timeout, timeout.read_timeout, timeout.total) == (2, 600, None)

    def test_a_streamed_pod_log_read_is_bounded_and_returns_the_raw_body(self) -> None:
        log_path = "/api/v1/namespaces/gco-jobs/pods/train-0/log"
        pool = RecordingPool({log_path: b"step=1\nstep=2\n"})
        response = client.CoreV1Api(_client(pool)).read_namespaced_pod_log(
            name="train-0", namespace="gco-jobs", _preload_content=False
        )
        assert response.data == b"step=1\nstep=2\n"
        assert [_total(t) for t in pool.timeouts()] == [7]


class TestDynamicClient:
    def test_discovery_reads_and_resource_calls_are_bounded(self, tmp_path: Any) -> None:
        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "settings", "namespace": "gco-jobs"},
            "data": {"k": "v"},
        }
        pool = RecordingPool(
            {
                "/version": {"major": "1", "minor": "33", "gitVersion": "v1.33.0"},
                "/apis": {"kind": "APIGroupList", "apiVersion": "v1", "groups": []},
                "/api/v1": {
                    "kind": "APIResourceList",
                    "groupVersion": "v1",
                    "resources": [
                        {
                            "name": "configmaps",
                            "singularName": "configmap",
                            "namespaced": True,
                            "kind": "ConfigMap",
                            "verbs": ["create", "delete", "get", "list", "patch"],
                        }
                    ],
                },
                "/api/v1/namespaces/gco-jobs/configmaps": configmap,
                "/api/v1/namespaces/gco-jobs/configmaps/settings": configmap,
            }
        )
        # A private cache file: a warm discovery cache would skip the reads.
        dyn = dynamic.DynamicClient(_client(pool), cache_file=str(tmp_path / "discovery.json"))
        configmaps = dyn.resources.get(api_version="v1", kind="ConfigMap")
        configmaps.get(name="settings", namespace="gco-jobs")
        configmaps.create(body=configmap, namespace="gco-jobs")

        methods_and_paths = [(method, path) for method, path, _ in pool.requests]
        assert ("GET", "/version") in methods_and_paths
        assert ("GET", "/api/v1") in methods_and_paths
        assert ("GET", "/api/v1/namespaces/gco-jobs/configmaps/settings") in methods_and_paths
        assert ("POST", "/api/v1/namespaces/gco-jobs/configmaps") in methods_and_paths
        assert {_total(t) for t in pool.timeouts()} == {7}


class TestAgainstRealSockets:
    @pytest.fixture
    def silent_server(self) -> Iterator[int]:
        """Accepts connections and never answers, like a wedged API server."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        held: list[socket.socket] = []
        stop = threading.Event()

        def accept() -> None:
            listener.settimeout(0.1)
            while not stop.is_set():
                try:
                    held.append(listener.accept()[0])
                except OSError:
                    continue

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1]
        finally:
            stop.set()
            thread.join(2)
            for connection in held:
                connection.close()
            listener.close()

    def test_a_stalled_api_server_fails_the_call_instead_of_hanging_it(
        self, silent_server: int
    ) -> None:
        configuration = client.Configuration()
        configuration.host = f"http://127.0.0.1:{silent_server}"
        core = client.CoreV1Api(BoundedApiClient(configuration, default_request_timeout=0.2))
        outcome: dict[str, Any] = {}

        def call() -> None:
            try:
                core.list_namespace(limit=1)
            except Exception as error:
                outcome["error"] = error

        worker = threading.Thread(target=call, daemon=True)
        started = time.monotonic()
        worker.start()
        # Each of urllib3's attempts (the first plus three retries) is bounded.
        worker.join(10)
        assert not worker.is_alive(), "the request hung past every bound"
        assert isinstance(outcome.get("error"), urllib3.exceptions.HTTPError)
        assert time.monotonic() - started < 5

    def test_a_streamed_body_longer_than_the_timeout_is_read_to_the_end(self) -> None:
        """The read timeout is per socket read, so a slow but steady log is not cut short."""

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Transfer-Encoding", "chunked")
                # One request per connection, so shutdown() is not left
                # waiting on a kept-alive socket.
                self.send_header("Connection", "close")
                self.end_headers()
                for line in (b"one\n", b"two\n", b"three\n", b"four\n"):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                    self.wfile.flush()
                    time.sleep(0.15)
                self.wfile.write(b"0\r\n\r\n")
                self.close_connection = True

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            configuration = client.Configuration()
            configuration.host = f"http://127.0.0.1:{server.server_port}"
            core = client.CoreV1Api(BoundedApiClient(configuration, default_request_timeout=0.4))
            started = time.monotonic()
            response = core.read_namespaced_pod_log(
                name="train-0", namespace="gco-jobs", _preload_content=False
            )
            assert response.data == b"one\ntwo\nthree\nfour\n"
            # Longer than the 0.4 s timeout end to end, yet never cut off.
            assert time.monotonic() - started > 0.4
        finally:
            server.shutdown()
            server.server_close()


class TestTimeoutFromEnvironment:
    def test_defaults_to_thirty_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("K8S_API_TIMEOUT", raising=False)
        assert k8s_api_timeout_from_env() == DEFAULT_K8S_API_TIMEOUT_SECONDS == 30
        assert BoundedApiClient().default_request_timeout == 30

    def test_reads_k8s_api_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("K8S_API_TIMEOUT", "9")
        assert k8s_api_timeout_from_env() == 9
        assert BoundedApiClient().default_request_timeout == 9

    @pytest.mark.parametrize("value", ["0", "-5", "thirty", "1.5"])
    def test_refuses_anything_but_a_positive_integer(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Zero is refused too: the client reads a zero timeout as no timeout."""
        monkeypatch.setenv("K8S_API_TIMEOUT", value)
        with pytest.raises(
            ValueError, match=f"K8S_API_TIMEOUT must be a positive number of seconds, got '{value}'"
        ):
            k8s_api_timeout_from_env()

    def test_an_explicit_default_wins_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("K8S_API_TIMEOUT", "9")
        assert BoundedApiClient(default_request_timeout=(1, 2)).default_request_timeout == (1, 2)
