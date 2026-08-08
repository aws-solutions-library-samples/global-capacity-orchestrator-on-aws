"""Shared plumbing for the Floci-backed integration layer.

Floci (https://github.com/floci-io/floci) is a local AWS emulator: real wire
protocol on one HTTP endpoint, no account, no credentials. These tests sit
between the in-process moto mocks and a real-account deployment: production
GCO code issues genuine SDK requests over HTTP and the emulator persists and
returns real service state. See docs/FLOCI_TESTING.md for the layer map and
known limitations.

Opt-in contract
---------------
Every Floci test module declares::

    pytestmark = floci_test_markers()

which applies the ``floci`` marker and skips the whole module unless
``GCO_FLOCI_ENDPOINT`` is set (the same opt-in-env pattern as
``mooncake_image`` / ``helm_online``). Unit shards therefore skip these
modules in milliseconds; the dedicated floci-tests workflow sets the
endpoint and runs them for real.

Real-AWS safety
---------------
Pointing this layer at a real AWS endpoint must be impossible to do by
accident. ``verified_floci_endpoint`` enforces two independent properties
before any test runs and FAILS (never skips) on violation:

1. the endpoint must be plain ``http://`` on localhost or a private/compose
   hostname — every real AWS endpoint is HTTPS on ``*.amazonaws.com``; and
2. STS must echo the session's throwaway 12-digit access-key id back as the
   caller account — Floci's documented multi-account behavior. Real AWS can
   never do this: a fabricated key id fails authentication outright.

Isolation
---------
Each pytest session invents a random 12-digit account id and uses it as the
``AWS_ACCESS_KEY_ID``. Under Floci's per-account isolation, concurrent or
repeated sessions against one long-lived local emulator cannot see each
other's resources; CI additionally gets a fresh in-memory emulator per job.
Resource names still carry a per-test suffix so a single session never
collides with itself.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Iterator
from urllib.parse import urlparse

import boto3
import pytest

# Documented Floci-gap shims live in tests/_floci_gap_shims.py (pytest-free
# so harness subprocesses can load them through sitecustomize); re-exported
# here because this module is the test layer's front door.
from tests._floci_gap_shims import (  # noqa: F401  (re-export)
    apply_known_floci_gap_shims,
    shim_floci_get_stack_policy,
    shim_floci_missing_global_accelerator,
)

#: Environment variable that opts a run into the Floci layer. The value is
#: the emulator's base URL, e.g. ``http://127.0.0.1:4566``.
FLOCI_ENDPOINT_ENV = "GCO_FLOCI_ENDPOINT"

#: Hostnames acceptable for an emulator endpoint. Anything else — above all
#: anything resembling a real AWS hostname — is rejected loudly.
_ALLOWED_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "floci"})

_SKIP_REASON = (
    f"Floci integration layer is opt-in: set {FLOCI_ENDPOINT_ENV} to the emulator URL "
    "(e.g. http://127.0.0.1:4566) to run these tests"
)


def floci_test_markers() -> list[pytest.MarkDecorator]:
    """The ``pytestmark`` every Floci test module declares."""
    return [
        pytest.mark.floci,
        pytest.mark.skipif(not os.environ.get(FLOCI_ENDPOINT_ENV), reason=_SKIP_REASON),
    ]


def _reject(endpoint: str, problem: str) -> None:
    pytest.fail(
        f"Refusing to run Floci tests against {endpoint!r}: {problem}. "
        "This guard exists so the emulator layer can never touch real AWS. "
        f"Point {FLOCI_ENDPOINT_ENV} at a local Floci container instead.",
        pytrace=False,
    )


@pytest.fixture(scope="session")
def verified_floci_endpoint() -> Iterator[str]:
    """The emulator URL, with session env applied and safety proven first.

    Applies the AWS environment for the whole session (endpoint, dummy
    credentials carrying a random 12-digit account id, region), verifies the
    endpoint is genuinely an emulator, and restores the prior environment on
    teardown so a subsequent non-Floci test in the same process cannot
    inherit emulator credentials.
    """
    endpoint = os.environ[FLOCI_ENDPOINT_ENV].rstrip("/")

    parsed = urlparse(endpoint)
    if parsed.scheme != "http":
        _reject(endpoint, "emulator endpoints are plain http; https implies a real service")
    if (parsed.hostname or "") not in _ALLOWED_HOSTNAMES:
        _reject(
            endpoint,
            f"hostname {parsed.hostname!r} is not an allowed emulator host "
            f"({', '.join(sorted(_ALLOWED_HOSTNAMES))})",
        )

    session_account = "9" + "".join(secrets.choice("0123456789") for _ in range(11))
    saved = {
        key: os.environ.get(key)
        for key in (
            "AWS_ENDPOINT_URL",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_REGION",
            "AWS_REGION",
            "AWS_PROFILE",
        )
    }
    os.environ.pop("AWS_PROFILE", None)
    os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ.update(
        AWS_ENDPOINT_URL=endpoint,
        AWS_ACCESS_KEY_ID=session_account,
        AWS_SECRET_ACCESS_KEY="floci-test-secret",
        AWS_DEFAULT_REGION="us-east-1",
        AWS_REGION="us-east-1",
    )
    try:
        # The identity echo is the hard proof this is an emulator: real AWS
        # rejects a fabricated key id at the signature layer, so the only way
        # this call returns our invented account is Floci's documented
        # 12-digit-AKID account routing.
        identity = boto3.client("sts").get_caller_identity()
        if identity.get("Account") != session_account:
            _reject(
                endpoint,
                "STS did not echo the session's throwaway account id "
                f"(got {identity.get('Account')!r}); this does not behave like Floci",
            )
        yield endpoint
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session")
def floci_account(verified_floci_endpoint: str) -> str:
    """The session's isolated 12-digit emulator account id."""
    return os.environ["AWS_ACCESS_KEY_ID"]


def unique_name(prefix: str) -> str:
    """A collision-proof resource name that still reads in failure output."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Production-shaped resource factories
# ---------------------------------------------------------------------------
# Schemas mirror gco/stacks/global_stack.py exactly (table names, keys, and
# GSIs) so the store classes run against the same shape CDK deploys. The E2E
# test goes further and materializes these from a real CloudFormation stack;
# these factories exist for the focused per-store tests where a full stack
# per test would only add time.


def create_templates_table(dynamodb: object, table_name: str) -> None:
    dynamodb.create_table(  # type: ignore[attr-defined]
        TableName=table_name,
        AttributeDefinitions=[{"AttributeName": "template_name", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "template_name", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)  # type: ignore[attr-defined]


def create_webhooks_table(dynamodb: object, table_name: str) -> None:
    dynamodb.create_table(  # type: ignore[attr-defined]
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "webhook_id", "AttributeType": "S"},
            {"AttributeName": "namespace", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "webhook_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "namespace-index",
                "KeySchema": [{"AttributeName": "namespace", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)  # type: ignore[attr-defined]


def create_jobs_table(dynamodb: object, table_name: str) -> None:
    dynamodb.create_table(  # type: ignore[attr-defined]
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "job_id", "AttributeType": "S"},
            {"AttributeName": "target_region", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "status_work", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "region-status-index",
                "KeySchema": [
                    {"AttributeName": "target_region", "KeyType": "HASH"},
                    {"AttributeName": "status", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "region-status-work-index",
                "KeySchema": [
                    {"AttributeName": "target_region", "KeyType": "HASH"},
                    {"AttributeName": "status_work", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)  # type: ignore[attr-defined]


def create_job_queue(
    sqs: object,
    name_prefix: str,
    *,
    max_receive_count: int = 3,
    visibility_timeout: int = 300,
) -> dict:
    """A main queue + DLQ pair matching the regional stack's redrive wiring."""
    dlq_url = sqs.create_queue(QueueName=f"{name_prefix}-dlq")["QueueUrl"]  # type: ignore[attr-defined]
    dlq_arn = sqs.get_queue_attributes(  # type: ignore[attr-defined]
        QueueUrl=dlq_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    queue_url = sqs.create_queue(  # type: ignore[attr-defined]
        QueueName=name_prefix,
        Attributes={
            "VisibilityTimeout": str(visibility_timeout),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(max_receive_count)}
            ),
        },
    )["QueueUrl"]
    return {"queue_url": queue_url, "dlq_url": dlq_url, "dlq_arn": dlq_arn}
