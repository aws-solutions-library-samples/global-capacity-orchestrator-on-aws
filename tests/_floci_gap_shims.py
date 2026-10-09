"""Documented Floci-gap shims, importable without pytest.

Three emulator gaps affect GCO's AWS surface (each probed empirically; see
docs/FLOCI_TESTING.md):

* CloudFormation ``GetStackPolicy`` responses omit the
  ``GetStackPolicyResult`` wrapper element, so botocore's parser raises
  ``KeyError`` whether or not the stack has a policy. Real AWS returns a
  parseable empty result for a policy-less stack — which is what every GCO
  stack is — and the harness already tolerates exactly that shape.
* EC2 does not answer the canonical Availability Zone IDs: Floci 2.2.0
  reports ``<region>-azN`` and ignores the ``zone-id`` filter (see
  :func:`shim_floci_zone_id_lookup`).
* X-Ray is absent from the emulator's service catalog
  (``UnknownOperationException`` on every operation tried, re-probed against
  Floci 2.2.0), while the harness's
  baseline records each Region's trace segment destination before anything
  deploys, so the Transaction Search state the run changes can be restored.

Each shim registers a botocore ``before-send`` handler that answers exactly
one read-only operation with the response real AWS would give for the
resources GCO actually creates (no stack policy; the canonical zone ids; the
X-Ray destination of an account that never turned Transaction Search on,
which a fabricated emulator account cannot have done).
They live strictly in the test layer: in-process Floci tests apply them to
their sessions, and the E2E injects them into harness subprocesses through
``tests/_floci_sitecustomize/``. Production code never imports this module.
Delete each shim when a Floci release closes its gap.

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


# What GetTraceSegmentDestination returns for an account that has never
# enabled CloudWatch Transaction Search: segments still go to X-Ray.
_DEFAULT_TRACE_SEGMENT_DESTINATION_JSON = json.dumps(
    {"Destination": "XRay", "Status": "ACTIVE"}
).encode()


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


def shim_floci_zone_id_lookup(events) -> None:
    """Answer zone-id-filtered ``DescribeAvailabilityZones`` with real mappings.

    Second documented gap: Floci's EC2 does not answer canonical Availability
    Zone IDs (2.2.0 reports ``<region>-azN`` and ignores the ``zone-id`` filter),
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


def shim_floci_missing_xray(events) -> None:
    """Answer X-Ray ``GetTraceSegmentDestination`` with the account default.

    The only X-Ray call the Floci E2E reaches is the harness baseline's
    read-only record of each Region's destination
    (``scripts/live_release_validation/ownership/transaction_search.py``).
    CloudWatch Logs ``DescribeResourcePolicies``, the other half of that
    record, is modeled by the emulator and needs no shim. The mutating
    ``UpdateTraceSegmentDestination`` stays unshimmed on purpose: only a
    deployed topology's cleanup calls it, and the E2E never deploys.
    """

    def _synthesize(request, **_kwargs):
        return _local_response(request, _DEFAULT_TRACE_SEGMENT_DESTINATION_JSON, "application/json")

    events.register("before-send.xray.GetTraceSegmentDestination", _synthesize)


def apply_known_floci_gap_shims(events) -> None:
    """Install every documented Floci-gap shim on a botocore event system."""
    shim_floci_get_stack_policy(events)
    shim_floci_zone_id_lookup(events)
    shim_floci_missing_xray(events)
