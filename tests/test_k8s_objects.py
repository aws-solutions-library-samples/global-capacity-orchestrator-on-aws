"""``gco.k8s_objects`` narrows the Optional fields of kubernetes-client models.

kubernetes client 37 types ``metadata``, ``spec`` and ``status`` as Optional
on every model. On an object the API server returns they are never absent, so
the services narrow them with :func:`present` rather than branching on a
``None`` no real response carries; the ``object``-typed answers of the
custom-objects API are narrowed to the ``dict`` they are with
:func:`json_object`. A value that is not what the server gives raises
:class:`ObjectShapeError`, naming the field or the answer, so a hand-built
object in a test fails loudly instead of as an ``AttributeError`` later on.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from kubernetes.client.models import V1Job, V1JobStatus, V1ObjectMeta

from gco import k8s_objects
from gco.k8s_objects import ObjectShapeError, json_object, present


class TestPresent:
    def test_returns_the_field_itself_when_set(self) -> None:
        metadata = V1ObjectMeta(name="train-1", namespace="gco-jobs")
        job = V1Job(metadata=metadata, status=V1JobStatus(active=1))

        assert present(job.metadata, "job.metadata") is metadata
        assert present(job.status, "job.status").active == 1

    def test_names_the_missing_field(self) -> None:
        job = V1Job(metadata=V1ObjectMeta(name="train-1"))

        with pytest.raises(ObjectShapeError, match=r"^job\.status is missing") as excinfo:
            present(job.status, "job.status")
        assert "always carries it" in str(excinfo.value)

    def test_the_error_is_a_value_error(self) -> None:
        # Callers that catch the generic failure of a Kubernetes read
        # (``except Exception``) keep catching this; nothing narrower than
        # ValueError is expected to.
        assert issubclass(ObjectShapeError, ValueError)

    def test_falsy_values_other_than_none_are_present(self) -> None:
        # ``present`` is a None check, not a truthiness check: an empty
        # annotations map or a zero count is a value the server sent.
        assert present({}, "metadata.annotations") == {}
        assert present(0, "status.active") == 0
        assert present("", "metadata.namespace") == ""


class TestJsonObject:
    def test_returns_the_dict_it_was_given(self) -> None:
        answer: object = {"items": [{"metadata": {"name": "gpu-pool"}}]}

        narrowed = json_object(answer, "the NodePool list")

        assert narrowed is answer
        assert narrowed["items"][0]["metadata"]["name"] == "gpu-pool"

    @pytest.mark.parametrize(
        ("answer", "type_name"),
        [(["not", "an", "object"], "list"), ("text", "str"), (None, "NoneType"), (3, "int")],
    )
    def test_rejects_anything_that_is_not_a_json_object(
        self, answer: object, type_name: str
    ) -> None:
        with pytest.raises(ObjectShapeError) as excinfo:
            json_object(answer, "the pod metrics")

        message = str(excinfo.value)
        assert message.startswith("the pod metrics is not a JSON object")
        assert message.endswith(f"answered with {type_name}")


def test_module_is_standard_library_only() -> None:
    # Every service image copies gco/ wholesale, so this module must not pull
    # the kubernetes client (or anything else) into the import graph of a
    # service that does not already have it.
    source = Path(k8s_objects.__file__).read_text(encoding="utf-8")
    imports = re.findall(r"^(?:import|from)\s+(\S+)", source, flags=re.MULTILINE)
    assert imports == ["__future__", "typing"]
    assert all(name in sys.stdlib_module_names for name in imports)
