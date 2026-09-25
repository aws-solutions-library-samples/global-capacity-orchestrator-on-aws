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


def shim_floci_zone_id_lookup(events) -> None:
    """Answer zone-id-filtered ``DescribeAvailabilityZones`` with real mappings.

    Third documented gap: Floci's EC2 does not model Availability Zone IDs,
    but a credentialed CDK synth runs the regional stack's fail-closed
    EKS-unsupported-AZ resolution, which filters
    ``DescribeAvailabilityZones`` by ``zone-id`` and refuses to proceed when
    any requested ID is missing (gco/stacks/regional_stack.py — correct
    behavior against real AWS, where every ID resolves).

    The shim intercepts ONLY requests carrying a ``zone-id`` filter (the
    exact query that code path issues; unfiltered calls still reach the
    emulator) and answers with the canonical id→name mapping for the IDs in
    ``gco/stacks/constants.EKS_UNSUPPORTED_AZ_IDS``, using the reference
    account layout. That keeps the fail-closed production logic exercised
    end to end instead of bypassed.
    """
    from urllib.parse import parse_qs

    # Canonical name for each unsupported zone id in the reference layout.
    zone_names = {
        "use1-az3": ("us-east-1c", "us-east-1"),
        "usw1-az2": ("us-west-1b", "us-west-1"),
        "cac1-az3": ("ca-central-1c", "ca-central-1"),
    }

    def _synthesize(request, **_kwargs):
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        params = parse_qs(body or "")
        if params.get("Filter.1.Name") != ["zone-id"]:
            return None  # not the fail-closed lookup; let the emulator answer
        requested = [
            value[0] for key, value in sorted(params.items()) if key.startswith("Filter.1.Value.")
        ]
        items = []
        for zone_id in requested:
            if zone_id not in zone_names:
                continue
            name, region = zone_names[zone_id]
            items.append(
                f"<item><zoneId>{zone_id}</zoneId><zoneName>{name}</zoneName>"  # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format - EC2 XML wire payload served to botocore in-process, not HTML; values come from the hardcoded zone_names dict above
                f"<regionName>{region}</regionName><state>available</state></item>"  # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format - continuation of the same hardcoded XML payload
            )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<DescribeAvailabilityZonesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
            "<requestId>floci-gap-shim</requestId>"
            f"<availabilityZoneInfo>{''.join(items)}</availabilityZoneInfo>"
            "</DescribeAvailabilityZonesResponse>"
        )
        return _local_response(request, xml.encode(), "text/xml")

    events.register("before-send.ec2.DescribeAvailabilityZones", _synthesize)


def apply_known_floci_gap_shims(events) -> None:
    """Install every documented Floci-gap shim on a botocore event system."""
    shim_floci_get_stack_policy(events)
    shim_floci_missing_global_accelerator(events)
    shim_floci_zone_id_lookup(events)
