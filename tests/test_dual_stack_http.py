"""Tests for cli/_dual_stack_http.py: HTTPS connects that survive a broken IPv6 path.

The GCO APIs are dual-stack by default, so their host names resolve to IPv6
and IPv4 addresses. urllib3 tries the addresses in ``getaddrinfo`` order with
the whole connect timeout each; on a network that drops IPv6, every IPv6
address would cost a full timeout before the first IPv4 address. These tests
pin the replacement connect: families alternate, every address but the last
gets at most ``ADDRESS_ATTEMPT_SECONDS``, and failures surface as urllib3's
own exceptions.
"""

from __future__ import annotations

import socket
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from urllib3.exceptions import (
    ConnectTimeoutError,
    LocationParseError,
    NameResolutionError,
    NewConnectionError,
)

from cli import _dual_stack_http as dual_stack

V6_A = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2600:1f18::1", 443, 0, 0))
V6_B = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2600:1f18::2", 443, 0, 0))
V6_C = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2600:1f18::3", 443, 0, 0))
V4_A = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
V4_B = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 443))


class TestInterleaveFamilies:
    def test_alternates_starting_with_the_resolvers_first_family(self) -> None:
        assert dual_stack._interleave_families([V6_A, V6_B, V6_C, V4_A]) == [
            V6_A,
            V4_A,
            V6_B,
            V6_C,
        ]
        assert dual_stack._interleave_families([V4_A, V4_B, V6_A]) == [V4_A, V6_A, V4_B]

    def test_one_family_and_no_answer_keep_their_order(self) -> None:
        assert dual_stack._interleave_families([V4_A, V4_B]) == [V4_A, V4_B]
        assert dual_stack._interleave_families([]) == []


class _FakeSockets:
    """Stands in for socket.socket: each address connects or fails as configured."""

    def __init__(self, outcomes: dict[Any, BaseException | None]) -> None:
        self.outcomes = outcomes
        self.created: list[MagicMock] = []

    def __call__(self, family: int, kind: int, proto: int) -> MagicMock:
        sock = MagicMock(name=f"socket-{len(self.created)}")

        def connect(sockaddr: Any) -> None:
            outcome = self.outcomes[sockaddr[0]]
            if outcome is not None:
                raise outcome

        sock.connect.side_effect = connect
        self.created.append(sock)
        return sock


def _connect_with(
    answers: list[Any],
    outcomes: dict[Any, BaseException | None],
    timeout: Any = 30.0,
    **kwargs: Any,
) -> tuple[Any, _FakeSockets]:
    sockets = _FakeSockets(outcomes)
    with (
        patch.object(dual_stack.socket, "getaddrinfo", return_value=answers),
        patch.object(dual_stack.socket, "socket", side_effect=sockets),
    ):
        result = dual_stack._connect(
            ("api.example", 443),
            timeout,
            kwargs.get("source_address"),
            kwargs.get("socket_options"),
        )
    return result, sockets


class TestConnect:
    def test_a_dropped_ipv6_path_costs_one_short_attempt_before_ipv4(self) -> None:
        result, sockets = _connect_with(
            [V6_A, V6_B, V6_C, V4_A],
            {"2600:1f18::1": TimeoutError("timed out"), "192.0.2.1": None},
        )
        first, second = sockets.created
        assert result is second
        first.settimeout.assert_called_once_with(dual_stack.ADDRESS_ATTEMPT_SECONDS)
        first.close.assert_called_once()
        # The IPv4 address was not the last candidate, so its connect was bounded
        # too; once it answered, the socket got the whole timeout back for TLS.
        assert [call.args for call in second.settimeout.call_args_list] == [
            (dual_stack.ADDRESS_ATTEMPT_SECONDS,),
            (30.0,),
        ]
        second.connect.assert_called_once_with(("192.0.2.1", 443))

    def test_the_last_address_keeps_the_whole_connect_timeout(self) -> None:
        result, sockets = _connect_with(
            [V6_A, V4_A],
            {"2600:1f18::1": OSError(101, "Network is unreachable"), "192.0.2.1": None},
        )
        assert result is sockets.created[1]
        assert sockets.created[1].settimeout.call_args_list[0].args == (30.0,)

    def test_a_connect_timeout_below_the_attempt_limit_is_never_raised(self) -> None:
        _, sockets = _connect_with(
            [V6_A, V4_A], {"2600:1f18::1": TimeoutError(), "192.0.2.1": None}, timeout=0.5
        )
        assert sockets.created[0].settimeout.call_args.args == (0.5,)

    def test_the_default_timeout_sentinel_leaves_the_socket_default(self) -> None:
        sentinel = object()
        with patch.object(dual_stack.socket, "getdefaulttimeout", return_value=None):
            _, sockets = _connect_with(
                [V6_A, V4_A], {"2600:1f18::1": TimeoutError(), "192.0.2.1": None}, timeout=sentinel
            )
        assert sockets.created[0].settimeout.call_args.args == (dual_stack.ADDRESS_ATTEMPT_SECONDS,)
        assert [call.args for call in sockets.created[1].settimeout.call_args_list] == [
            (None,),
            (None,),
        ]

    def test_a_kernel_without_ipv6_moves_on_to_ipv4(self) -> None:
        """socket(AF_INET6) itself fails where IPv6 is disabled; nothing is left to close."""
        ipv4 = MagicMock(name="ipv4-socket")

        def make_socket(family: int, kind: int, proto: int) -> MagicMock:
            if family == socket.AF_INET6:
                raise OSError(97, "Address family not supported by protocol")
            return ipv4

        with (
            patch.object(dual_stack.socket, "getaddrinfo", return_value=[V6_A, V4_A]),
            patch.object(dual_stack.socket, "socket", side_effect=make_socket),
        ):
            assert dual_stack._connect(("api.example", 443), 30.0, None, None) is ipv4
        ipv4.connect.assert_called_once_with(("192.0.2.1", 443))

    def test_every_address_failing_raises_the_last_error(self) -> None:
        refused = ConnectionRefusedError(111, "Connection refused")
        with pytest.raises(ConnectionRefusedError) as raised:
            _connect_with([V6_A, V4_A], {"2600:1f18::1": TimeoutError(), "192.0.2.1": refused})
        assert raised.value is refused

    def test_socket_options_and_source_address_reach_every_attempt(self) -> None:
        options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
        _, sockets = _connect_with(
            [V4_A],
            {"192.0.2.1": None},
            socket_options=options,
            source_address=("0.0.0.0", 0),
        )
        sockets.created[0].setsockopt.assert_called_once_with(*options[0])
        sockets.created[0].bind.assert_called_once_with(("0.0.0.0", 0))

    def test_a_bracketed_ipv6_literal_is_resolved_without_brackets(self) -> None:
        with (
            patch.object(dual_stack.socket, "getaddrinfo", return_value=[V6_A]) as resolve,
            patch.object(
                dual_stack.socket, "socket", side_effect=_FakeSockets({"2600:1f18::1": None})
            ),
        ):
            dual_stack._connect(("[2600:1f18::1]", 443), 5.0, None, None)
        assert resolve.call_args.args[0] == "2600:1f18::1"

    def test_no_addresses_and_unencodable_hosts_fail_like_urllib3(self) -> None:
        with (
            patch.object(dual_stack.socket, "getaddrinfo", return_value=[]),
            pytest.raises(OSError, match="getaddrinfo returns an empty list"),
        ):
            dual_stack._connect(("api.example", 443), 5.0, None, None)
        with pytest.raises(LocationParseError):
            dual_stack._connect(("a" * 64 + ".example", 443), 5.0, None, None)

    def test_a_real_listener_accepts_the_connection(self) -> None:
        with socket.create_server(("127.0.0.1", 0)) as server:
            port = server.getsockname()[1]
            sock = dual_stack._connect(("127.0.0.1", port), 5.0, None, None)
            with sock:
                assert sock.getpeername() == ("127.0.0.1", port)
                assert sock.gettimeout() == 5.0


class TestConnectionErrors:
    """The connection raises what urllib3's own HTTPConnection raises."""

    @staticmethod
    def _connection() -> dual_stack._InterleavingHTTPSConnection:
        return dual_stack._InterleavingHTTPSConnection("api.example", 443, timeout=3.0)

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (socket.gaierror(-2, "Name or service not known"), NameResolutionError),
            (TimeoutError("timed out"), ConnectTimeoutError),
            (ConnectionRefusedError(111, "Connection refused"), NewConnectionError),
        ],
    )
    def test_connect_failures_map_to_urllib3_exceptions(
        self, error: OSError, expected: type[Exception]
    ) -> None:
        with (
            patch.object(dual_stack, "_connect", side_effect=error),
            pytest.raises(expected),
        ):
            self._connection()._new_conn()

    def test_a_connected_socket_is_audited_and_returned(self) -> None:
        sock = MagicMock()
        with (
            patch.object(dual_stack, "_connect", return_value=sock) as connect,
            patch.object(dual_stack.sys, "audit") as audit,
        ):
            connection = self._connection()
            assert connection._new_conn() is sock
        connect.assert_called_once_with(("api.example", 443), 3.0, None, connection.socket_options)
        audit.assert_called_once_with("http.client.connect", connection, "api.example", 443)


class TestRequest:
    def test_https_pools_use_the_interleaving_connection_and_http_pools_do_not(self) -> None:
        adapter = dual_stack._InterleavingAdapter()
        https_pool = adapter.poolmanager.connection_from_url("https://api.example/")
        http_pool = adapter.poolmanager.connection_from_url("http://api.example/")
        assert isinstance(https_pool, dual_stack._InterleavingHTTPSConnectionPool)
        assert https_pool.ConnectionCls is dual_stack._InterleavingHTTPSConnection
        assert not isinstance(http_pool, dual_stack._InterleavingHTTPSConnectionPool)

    def test_request_sends_through_a_session_with_the_adapter_and_closes_it(self) -> None:
        seen: dict[str, Any] = {}
        response = requests.Response()

        def fake_request(session: requests.Session, **kwargs: Any) -> requests.Response:
            seen["adapter"] = session.get_adapter("https://api.example/prod")
            seen["kwargs"] = kwargs
            return response

        with (
            patch.object(requests.Session, "request", autospec=True, side_effect=fake_request),
            patch.object(requests.Session, "close", autospec=True) as close,
        ):
            result = dual_stack.request(
                "GET", "https://api.example/prod", headers={"a": "b"}, timeout=30
            )
        assert result is response
        assert isinstance(seen["adapter"], dual_stack._InterleavingAdapter)
        assert seen["kwargs"] == {
            "method": "GET",
            "url": "https://api.example/prod",
            "headers": {"a": "b"},
            "timeout": 30,
        }
        close.assert_called_once()

    def test_a_plain_http_request_round_trips(self) -> None:
        """The session still serves http:// with requests' own adapter."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = dual_stack.request(
                "GET", f"http://127.0.0.1:{server.server_address[1]}/", timeout=5
            )
        finally:
            server.shutdown()
            server.server_close()
        assert response.status_code == 200
        assert response.text == "ok"
