"""Narrowing for the optional fields of kubernetes-client model objects.

Since kubernetes client 37.0 the models are generated from the OpenAPI spec
with Pydantic, and ``metadata``, ``spec`` and ``status`` are ``Optional``:
true to the wire format, where a request body may omit them. On an object the
API server has *returned* they are never absent. The server fills metadata on
every object, defaults the spec, and attaches a status (``{}`` at the least)
to every kind that has one. GCO reads these fields on returned objects, and on
bodies it built itself a few lines up with the field set, so it narrows them
with :func:`present` instead of guarding each access with a branch no real
object takes. A ``None`` there means the object is not what the code was
written for (a hand-built one in a test, a double) and raises
:class:`ObjectShapeError`, which names the field.

The same applies to the ``object``-typed results of the custom-objects API
and of a response decoded without a model: the server answers with a JSON
object, and :func:`json_object` narrows it to the ``dict`` it is.

Standard library only: every service image copies ``gco/`` wholesale, so
anything imported here is imported by every service.
"""

from __future__ import annotations

from typing import Any


class ObjectShapeError(ValueError):
    """A Kubernetes API object did not have the shape the API server gives it."""


def present[T](value: T | None, field: str) -> T:
    """Return ``value``, a field the object always carries.


    Set by the API server on every object it returns, or by the construction
    that built the object. ``field`` names it for the error, as the caller
    reads it (``"pod.status"``).
    """
    if value is None:
        raise ObjectShapeError(
            f"{field} is missing from a Kubernetes object that always carries it"
        )
    return value


def json_object(value: object, what: str) -> dict[str, Any]:
    """Return ``value``, a decoded JSON object the API server answered with, as a ``dict``.

    ``what`` names the answer for the error (``"the Job create response"``).
    """
    if not isinstance(value, dict):
        raise ObjectShapeError(
            f"{what} is not a JSON object: the Kubernetes API answered with {type(value).__name__}"
        )
    return value
