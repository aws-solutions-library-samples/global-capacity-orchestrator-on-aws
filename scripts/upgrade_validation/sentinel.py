"""A job template written through the API before the upgrade and read back after it.

``gco upgrade`` promises that the control plane's shared state (the DynamoDB
tables among it) survives: only the workload tier is destroyed and recreated.
The sentinel tests that promise the way an operator would notice it breaking.
It is created through the public API while the base release serves it, and
after the upgrade the recreated regional stack must return it unchanged, with
its original creation time, so the item was neither lost nor rewritten. It is
deleted afterwards; the table itself goes with the global stack at teardown.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any

import requests

from cli.aws_client import APIRequestError
from scripts.live_release_validation.models import RunContext

TEMPLATES_PATH = "/api/v1/templates"
#: Statuses that mean the API is not serving yet (or throttled), not an answer.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
RETRY_INTERVAL_SECONDS = 20.0

Sleeper = Callable[[float], None]
Clock = Callable[[], float]


def sentinel_name(run_id: str) -> str:
    """A template name unique to the run that fits the API's 63-character limit."""
    return "gco-upgrade-sentinel-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]


def sentinel_body(run_id: str) -> dict[str, Any]:
    """The template the run writes; never submitted as a Job."""
    return {
        "name": sentinel_name(run_id),
        "description": f"Upgrade-validation sentinel for run {run_id}",
        "manifest": {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "{{name}}", "namespace": "gco-jobs"},
            "spec": {
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {"name": "sentinel", "image": "{{image}}", "command": ["true"]}
                        ],
                    }
                }
            },
        },
        "parameters": {"image": "public.ecr.aws/docker/library/busybox:1.38.0"},
    }


def _call(
    ctx: RunContext,
    method: str,
    path: str,
    *,
    deadline: float,
    body: dict[str, Any] | None = None,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """One API call, retried while the API is not answering yet, until ``deadline``."""
    attempts = 0
    while True:
        attempts += 1
        try:
            response: dict[str, Any] = ctx.aws_client.call_api(
                method=method,
                path=path,
                body=body,
                max_attempts=1 if method == "GET" else None,
            )
            return response
        except APIRequestError as exc:
            if exc.status_code not in RETRYABLE_STATUS_CODES:
                raise
            error: Exception = exc
        except requests.RequestException as exc:
            error = exc
        if clock() >= deadline:
            raise RuntimeError(
                f"{method} {path} did not succeed in {attempts} attempt(s): "
                f"{type(error).__name__}: {error}"
            ) from error
        sleep(RETRY_INTERVAL_SECONDS)


def _require_written_template(template: Any, body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(template, dict) or not template.get("created_at"):
        raise RuntimeError(f"The API returned no template record for {body['name']}")
    drift = sorted(
        key
        for key in ("name", "description", "manifest", "parameters")
        if template.get(key) != body[key]
    )
    if drift:
        raise RuntimeError(f"Template {body['name']} differs from what was written: {drift}")
    return template


def create_sentinel(
    ctx: RunContext,
    *,
    timeout_seconds: float,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """Write the run's sentinel template, or confirm an earlier attempt wrote it."""
    body = sentinel_body(ctx.settings.run_id)
    deadline = clock() + timeout_seconds
    try:
        response = _call(
            ctx, "POST", TEMPLATES_PATH, body=body, deadline=deadline, sleep=sleep, clock=clock
        )
        template = response.get("template")
    except APIRequestError as exc:
        if exc.status_code != 409:
            raise
        # A POST whose response was lost may still have written the item.
        template = read_sentinel(ctx, body["name"], deadline=deadline, sleep=sleep, clock=clock)
    return _require_written_template(template, body)


def read_sentinel(
    ctx: RunContext,
    name: str,
    *,
    deadline: float,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> Any:
    """The sentinel template as the API returns it now."""
    response = _call(
        ctx, "GET", f"{TEMPLATES_PATH}/{name}", deadline=deadline, sleep=sleep, clock=clock
    )
    return response.get("template")


def verify_sentinel(
    ctx: RunContext,
    written: dict[str, Any],
    *,
    timeout_seconds: float,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """Require the sentinel unchanged, including the creation time it was written with."""
    body = sentinel_body(ctx.settings.run_id)
    template = _require_written_template(
        read_sentinel(
            ctx, body["name"], deadline=clock() + timeout_seconds, sleep=sleep, clock=clock
        ),
        body,
    )
    if template.get("created_at") != written.get("created_at"):
        raise RuntimeError(
            f"Template {body['name']} was recreated: created {template.get('created_at')}, "
            f"written {written.get('created_at')}"
        )
    return template


def delete_sentinel(
    ctx: RunContext,
    *,
    timeout_seconds: float,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """Delete the sentinel; a 404 means it is already gone."""
    name = sentinel_name(ctx.settings.run_id)
    try:
        _call(
            ctx,
            "DELETE",
            f"{TEMPLATES_PATH}/{name}",
            deadline=clock() + timeout_seconds,
            sleep=sleep,
            clock=clock,
        )
    except APIRequestError as exc:
        if exc.status_code != 404:
            raise
        return {"name": name, "deleted": False, "already_absent": True}
    return {"name": name, "deleted": True}
