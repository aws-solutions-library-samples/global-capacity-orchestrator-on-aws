"""
SSM Parameter Store helpers shared across ``cli/``, ``mcp/`` and ``gco/services/``.

Four free functions cover every shape callers in the tree currently
reach for:

* :func:`get_ssm_parameter` — fetch a value, propagate errors verbatim.
  Use when the parameter is required and a missing parameter is a hard
  failure. The :class:`botocore.exceptions.ClientError` raised by the
  underlying ``ssm:GetParameter`` carries a ``ParameterNotFound`` code
  that callers can match if they need to map missing into a friendlier
  domain error.
* :func:`get_ssm_parameter_optional` — fetch a value, return ``None`` on
  the specific ``ParameterNotFound`` case while still propagating any
  other error (permission denied, throttled, etc.). Use when a missing
  parameter is a non-fatal "absent" signal but real errors should
  surface.
* :func:`check_ssm_parameter` — return ``(True, "")`` iff the parameter
  exists, or ``(False, str(exc))`` on any error including
  ``ParameterNotFound``. Use for diagnostics-style "is this thing here?"
  checks where a transport error is treated the same as absent.
* :func:`put_ssm_parameter` — write a parameter. Errors propagate
  verbatim. Use for the rare cases (e.g. ALB hostname drift correction
  in ``gco/services/health_monitor.py``) where the CLI / monitor needs
  to write back to SSM.

Architectural rationale. ``mcp/`` is forbidden from importing ``cli/``
directly (the runtime tool surface shells out via subprocess instead),
but ``mcp/`` already imports ``gco/services/...`` for shared service
helpers. Putting these helpers under ``gco/services/`` lets every
concrete callsite — the CLI's :class:`cli.models.ModelManager`, the
analytics helpers in :mod:`cli.analytics_user_mgmt`, the
``HealthMonitor`` in :mod:`gco.services.health_monitor`, and the
:class:`mcp.mission.state.DynamoDBBackend` — share one implementation
without re-introducing the ``mcp -> cli`` import edge.

Each function lazy-imports ``boto3`` so the helper module's import
surface stays free of SDK dependencies; tests that don't exercise SSM
can monkeypatch ``boto3.client`` without dragging the whole CLI in.
The same lazy-import pattern is already used in
:func:`cli.analytics_user_mgmt.check_ssm_parameter` and the
:class:`mcp.mission.state.DynamoDBBackend` — this module just
consolidates it.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "check_ssm_parameter",
    "get_ssm_parameter",
    "get_ssm_parameter_optional",
    "put_ssm_parameter",
]


def _ssm_client(region: str | None) -> Any:
    """Construct a fresh ``ssm`` boto3 client.

    ``boto3`` is imported inside the function so a caller that monkey-
    patches ``boto3.client`` for a test never has to also intercept a
    module-level cached client. The fresh client is also free of
    cross-test leakage: a permission-denied response in one test does
    not poison the credential cache for the next.
    """
    import boto3

    if region is None:
        return boto3.client("ssm")
    return boto3.client("ssm", region_name=region)


def get_ssm_parameter(name: str, *, region: str | None = None) -> str:
    """Fetch an SSM parameter value; propagate errors verbatim.

    Args:
        name: Fully-qualified parameter name (e.g. ``"/gco/foo"``).
        region: Optional AWS region. ``None`` lets boto3's default
            chain (env var, config file, instance metadata) decide.

    Returns:
        The parameter's ``Value`` field as a string.

    Raises:
        botocore.exceptions.ClientError: ``ParameterNotFound`` when the
            parameter is missing, plus any other boto3 client error
            (throttled, access denied, etc.).
        botocore.exceptions.BotoCoreError: For transport / credential
            failures.

    The function does not catch any exception — errors carry the
    underlying ``Code`` field that callers wanting domain-specific
    messages can match on.
    """
    response = _ssm_client(region).get_parameter(Name=name)
    return str(response["Parameter"]["Value"])


def get_ssm_parameter_optional(name: str, *, region: str | None = None) -> str | None:
    """Fetch an SSM parameter value or return ``None`` if absent.

    Distinguishes the ``ParameterNotFound`` case (returns ``None``)
    from every other error (re-raised verbatim). This matches the
    pattern :class:`gco.services.health_monitor.HealthMonitor` reaches
    for when it has to read-then-maybe-write a drift-tracker
    parameter — a missing value is not an error, but a permission
    denied or a throttle is.

    Args:
        name: Fully-qualified parameter name.
        region: Optional AWS region.

    Returns:
        The parameter's ``Value`` as a string, or ``None`` if the
        parameter does not exist.

    Raises:
        botocore.exceptions.ClientError: Any error other than
            ``ParameterNotFound``.
        botocore.exceptions.BotoCoreError: Transport / credential
            failures.
    """
    from botocore.exceptions import ClientError

    client = _ssm_client(region)
    try:
        response = client.get_parameter(Name=name)
    except ClientError as exc:
        # ``ParameterNotFound`` is the only error this helper translates
        # to ``None``; every other code propagates so the caller sees
        # the real failure.
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
    return str(response["Parameter"]["Value"])


def check_ssm_parameter(name: str, *, region: str | None = None) -> tuple[bool, str]:
    """Return ``(True, "")`` iff the parameter exists, ``(False, error)`` otherwise.

    Diagnostic-style helper that flattens every kind of failure
    (missing, throttled, denied, transport error) into a single
    boolean. The returned error string is the underlying exception's
    ``str()`` so a caller logging the result has the original message.

    Args:
        name: Fully-qualified parameter name.
        region: Optional AWS region.

    Returns:
        ``(True, "")`` when the parameter resolves; ``(False, str(exc))``
        on any error including ``ParameterNotFound``.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        _ssm_client(region).get_parameter(Name=name)
    except (ClientError, BotoCoreError) as exc:
        return False, str(exc)
    return True, ""


def put_ssm_parameter(
    name: str,
    value: str,
    *,
    region: str | None = None,
    parameter_type: str = "String",
    overwrite: bool = True,
) -> None:
    """Write or overwrite an SSM parameter. Errors propagate verbatim.

    Args:
        name: Fully-qualified parameter name.
        value: New string value to store.
        region: Optional AWS region.
        parameter_type: SSM parameter type — ``"String"`` (default),
            ``"StringList"``, or ``"SecureString"``.
        overwrite: When ``True`` (default), passes ``Overwrite=True``
            to ``ssm:PutParameter`` so an existing parameter is
            replaced. When ``False``, the call fails with
            ``ParameterAlreadyExists`` if the name is taken.

    Raises:
        botocore.exceptions.ClientError: For SSM-side errors (insufficient
            permission, parameter type mismatch, etc.).
        botocore.exceptions.BotoCoreError: For transport / credential
            failures.

    The helper does not catch any exception — callers that need to
    classify failures (e.g. retry on throttle) match on the
    underlying ``Code`` field themselves.
    """
    _ssm_client(region).put_parameter(
        Name=name,
        Value=value,
        Type=parameter_type,
        Overwrite=overwrite,
    )
