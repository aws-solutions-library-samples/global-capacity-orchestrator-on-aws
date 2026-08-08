"""Documented Floci-gap shims, importable without pytest.

Two Floci 1.6.0 gaps affect GCO's AWS surface (both probed empirically;
see docs/FLOCI_TESTING.md):

* CloudFormation ``GetStackPolicy`` responses omit the
  ``GetStackPolicyResult`` wrapper element, so botocore's parser raises
  ``KeyError`` whether or not the stack has a policy. Real AWS returns a
  parseable empty result for a policy-less stack — which is what every GCO
  stack is — and the harness already tolerates exactly that shape.
* Global Accelerator is absent from the emulator's service catalog
  (``UnknownOperationException``), while the harness's fail-closed
  inventory requires its scanner to complete.

Each shim registers a botocore ``before-send`` handler that answers exactly
one read-only operation with the response real AWS would give for the
resources GCO actually creates (no stack policy; no accelerators an
emulator could host). They live strictly in the test layer: in-process
Floci tests apply them to their sessions, and the E2E injects them into
harness subprocesses through ``tests/_floci_sitecustomize/``. Production
code never imports this module. Delete it when a Floci release closes both
gaps.

Kept free of pytest imports on purpose so harness subprocesses can load it
through sitecustomize without dragging the test framework along.
"""

from __future__ import annotations

import io
import json

import urllib3
from botocore.awsrequest import AWSResponse

_EMPTY_STACK_POLICY_XML = (
    b'<GetStackPolicyResponse xmlns="http://cloudformation.amazonaws.com/doc/2010-05-15/">'
    b"<GetStackPolicyResult/>"
    b"<ResponseMetadata><RequestId>floci-gap-shim</RequestId></ResponseMetadata>"
    b"</GetStackPolicyResponse>"
)

_EMPTY_ACCELERATORS_JSON = json.dumps({"Accelerators": []}).encode()


def _local_response(request, body: bytes, content_type: str) -> AWSResponse:
    raw = urllib3.HTTPResponse(
        body=io.BytesIO(body),
        status=200,
        headers={"Content-Type": content_type},
        preload_content=False,
    )
    return AWSResponse(
        url=request.url,
        status_code=200,
        headers={"Content-Type": content_type},
        raw=raw,
    )


def shim_floci_get_stack_policy(events) -> None:
    """Answer CloudFormation ``GetStackPolicy`` with the no-policy shape."""

    def _synthesize(request, **_kwargs):
        return _local_response(request, _EMPTY_STACK_POLICY_XML, "text/xml")

    events.register("before-send.cloudformation.GetStackPolicy", _synthesize)


def shim_floci_missing_global_accelerator(events) -> None:
    """Answer Global Accelerator ``ListAccelerators`` with an empty list."""

    def _synthesize(request, **_kwargs):
        return _local_response(request, _EMPTY_ACCELERATORS_JSON, "application/x-amz-json-1.1")

    events.register("before-send.global-accelerator.ListAccelerators", _synthesize)


def apply_known_floci_gap_shims(events) -> None:
    """Install every documented Floci-gap shim on a botocore event system."""
    shim_floci_get_stack_policy(events)
    shim_floci_missing_global_accelerator(events)
