"""The endpoint store reads every page of the endpoints table.

A DynamoDB scan returns at most 1 MB per call and reports where it stopped in
``LastEvaluatedKey``. ``InferenceEndpointStore.list_endpoints`` read one page
and returned it, so past 1 MB of records the inference monitor's reconcile
loop never saw the later endpoints, and the name-collision check of
``gco inference deploy`` could not refuse a name that collides with one of
them. These checks fill a moto table past one page (moto applies the same
1 MB page limit and returns items in key order) and drive a fake table that
returns one record per page.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from gco.services.inference_store import InferenceEndpointStore

TABLE = "gco-inference-endpoints"
REGION = "us-east-1"
# Three 300 KB records fill moto's 1 MB page, so a fourth starts page two.
_PADDING = {"padding": "x" * 300_000}
_FILLERS = tuple(f"filler-{index}" for index in range(4))


@pytest.fixture
def table() -> Iterator[Any]:
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        created = dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "endpoint_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "endpoint_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        created.wait_until_exists()
        yield created


@pytest.fixture
def store(table: Any) -> InferenceEndpointStore:
    built = InferenceEndpointStore(table_name=TABLE, region=REGION)
    built._table = table
    for name in _FILLERS:
        built.create_endpoint(name, {"image": "img:v1"}, [REGION], labels=_PADDING)
    return built


def _first_page_names(table: Any) -> set[str]:
    """The names one bare scan returns, after checking the table spans more pages."""
    page = table.scan()
    assert "LastEvaluatedKey" in page
    return {item["endpoint_name"] for item in page["Items"]}


def test_list_endpoints_returns_records_past_the_first_page(
    store: InferenceEndpointStore, table: Any
) -> None:
    store.create_endpoint("llama", {"image": "img:v1"}, [REGION])
    assert "llama" not in _first_page_names(table)

    names = {endpoint["endpoint_name"] for endpoint in store.list_endpoints()}

    assert names == {*_FILLERS, "llama"}


def test_list_endpoint_filters_cover_every_page(store: InferenceEndpointStore, table: Any) -> None:
    store.create_endpoint("llama", {"image": "img:v1"}, ["eu-west-1"])
    assert "llama" not in _first_page_names(table)

    in_region = store.list_endpoints(target_region="eu-west-1")
    deploying = store.list_endpoints(desired_state="deploying")

    assert [endpoint["endpoint_name"] for endpoint in in_region] == ["llama"]
    assert {endpoint["endpoint_name"] for endpoint in deploying} == {*_FILLERS, "llama"}


def test_deploy_refuses_a_name_colliding_with_an_endpoint_past_the_first_page(
    store: InferenceEndpointStore, table: Any
) -> None:
    from cli.inference import InferenceManager

    store.create_endpoint("llama", {"image": "img:v1"}, [REGION])
    assert "llama" not in _first_page_names(table)
    config = MagicMock()
    config.global_region = REGION
    with (
        patch("cli.inference.get_aws_client", return_value=MagicMock()),
        patch("cli.inference.get_config", return_value=config),
    ):
        manager = InferenceManager(config=config)
    manager._get_store = MagicMock(return_value=store)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="collides with endpoint 'llama'"):
        manager.deploy("llama-canary", image="img:v1", target_regions=[REGION])

    assert store.get_endpoint("llama-canary", consistent_read=True) is None


def test_monitor_reconciles_endpoints_past_the_first_page(
    store: InferenceEndpointStore, table: Any
) -> None:
    from gco.services import inference_monitor as im

    store.create_endpoint("llama", {"image": "img:v1"}, [REGION])
    assert "llama" not in _first_page_names(table)
    with (
        patch.object(im.config, "load_incluster_config"),
        patch.object(im.client, "AppsV1Api"),
        patch.object(im.client, "CoreV1Api"),
        patch.object(im.client, "NetworkingV1Api"),
        patch.object(im.client, "DiscoveryV1Api"),
    ):
        monitor = im.InferenceMonitor("cluster", REGION, store)
    reconcile_endpoint = AsyncMock(return_value=None)
    monitor._reconcile_endpoint = reconcile_endpoint  # type: ignore[method-assign]

    asyncio.run(monitor.reconcile())

    reconciled = {call.args[0]["endpoint_name"] for call in reconcile_endpoint.await_args_list}
    assert reconciled == {*_FILLERS, "llama"}
    # The ownership checks compare object labels against every stored name.
    assert monitor._endpoint_names == frozenset({*_FILLERS, "llama"})


class _OneRecordPerPageTable:
    """A table whose scan returns one record per page, recording each request."""

    def __init__(self, names: list[str]) -> None:
        self._items = [{"endpoint_name": name, "created_at": name} for name in names]
        self.requests: list[dict[str, Any]] = []

    def scan(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        names = [item["endpoint_name"] for item in self._items]
        start = request.get("ExclusiveStartKey")
        index = 0 if start is None else names.index(start["endpoint_name"]) + 1
        page: dict[str, Any] = {"Items": [self._items[index]]}
        if index + 1 < len(self._items):
            page["LastEvaluatedKey"] = {"endpoint_name": names[index]}
        return page


def test_list_endpoints_resumes_each_scan_where_the_last_page_stopped() -> None:
    fake = _OneRecordPerPageTable(["a", "b", "c"])
    with patch("boto3.resource"):
        store = InferenceEndpointStore(table_name=TABLE, region=REGION)
    store._table = fake

    listed = store.list_endpoints()

    assert [endpoint["endpoint_name"] for endpoint in listed] == ["c", "b", "a"]
    assert fake.requests == [
        {},
        {"ExclusiveStartKey": {"endpoint_name": "a"}},
        {"ExclusiveStartKey": {"endpoint_name": "b"}},
    ]
