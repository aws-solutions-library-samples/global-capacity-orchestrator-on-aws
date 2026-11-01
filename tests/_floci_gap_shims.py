"""Documented Floci-gap shims, importable without pytest.

Five emulator gaps affect GCO's AWS surface (each probed empirically, last
against Floci 2.4.0; see docs/FLOCI_TESTING.md):

* CloudFormation ``GetStackPolicy`` responses omit the
  ``GetStackPolicyResult`` wrapper element, so botocore's parser raises
  ``KeyError`` whether or not the stack has a policy. Real AWS returns a
  parseable empty result for a policy-less stack — which is what every GCO
  stack is — and the harness already tolerates exactly that shape.
* X-Ray is absent from the emulator's service catalog
  (``UnknownOperationException`` on every operation tried, re-probed against
  Floci 2.4.0), while the harness's
  baseline records each Region's trace segment destination before anything
  deploys, so the Transaction Search state the run changes can be restored.
* SageMaker ``ListClusters`` (the HyperPod cluster listing) is not among the
  SageMaker actions Floci 2.4.0 models (``ValidationException: Action
  ListClusters is not supported``), while the harness's inventory lists the
  project's HyperPod clusters in every Region to prove them absent.
* The public global-infrastructure parameters
  (``/aws/service/global-infrastructure/services/<service>/regions``) are not
  served: ``GetParametersByPath`` answers an empty page. The inventory reads
  AWS PCS's Regions there because botocore names none for it.
* AWS PCS is absent from the catalog entirely (``UnknownOperationException``
  on ``ListClusters``, ``GetCluster`` and ``CreateCluster``), while the
  inventory lists the project's PCS clusters in every PCS Region.

The Availability Zone id shim this module used to carry is gone: Floci 2.4.0
reports the canonical zone ids and honors the ``zone-id`` filter (an unknown
id answers an empty list), so the regional stack's fail-closed resolution now
runs against the emulator unshimmed.

Each shim registers a botocore ``before-send`` handler that answers exactly
one read-only operation with the response real AWS would give for the
resources GCO actually creates (no stack policy; the
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

# What ListClusters returns for an account with no HyperPod clusters, which
# an emulator account is: Floci models no SageMaker CreateCluster either, so
# nothing in a Floci run can have created one.
_NO_HYPERPOD_CLUSTERS_JSON = json.dumps({"ClusterSummaries": []}).encode()

# What PCS ListClusters returns for an account with no clusters, which an
# emulator account is: PCS is absent from Floci's catalog, CreateCluster
# included.
_NO_PCS_CLUSTERS_JSON = json.dumps({"clusters": []}).encode()

# The Regions the shim says AWS PCS is offered in: a fixture, not a copy of
# AWS's list. Real AWS serves the live list under
# /aws/service/global-infrastructure/services/pcs/regions and the inventory
# reads it from there, so nothing here has to track service expansion. The
# fixture is the shape the Floci tests need — the Regions the emulator
# enables (where the pcs scanner must run) and a Region PCS is not offered in
# left out (us-west-1), which is what proves the inventory took the
# parameters' list and not botocore's every-Region one.
_PCS_REGIONS_PARAMETER_PATH = "/aws/service/global-infrastructure/services/pcs/regions"
_PCS_REGIONS_FIXTURE = (
    "eu-west-1",
    "us-east-1",
    "us-east-2",
)


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


def shim_floci_sagemaker_list_clusters(events) -> None:
    """Answer SageMaker ``ListClusters`` with the no-clusters shape.

    The only SageMaker call the Floci layers reach is the harness inventory's
    HyperPod scanner (``scripts/live_release_validation/inventory/scanners.py``),
    which lists every Region's clusters to prove the project's absent. Floci
    2.4.0 answers ``ValidationException: Action ListClusters is not supported``
    (SageMaker is in its catalog; this action is not). Real AWS returns an
    empty page for an account with no HyperPod clusters, and an emulator
    account can have none: ``CreateCluster`` is not modeled either. Every
    other SageMaker operation stays unshimmed; the scanner's ownership
    matching keeps its coverage in patched-client unit tests.
    """

    def _synthesize(request, **_kwargs):
        return _local_response(request, _NO_HYPERPOD_CLUSTERS_JSON, "application/x-amz-json-1.1")

    events.register("before-send.sagemaker.ListClusters", _synthesize)


def shim_floci_pcs_service_regions(events) -> None:
    """Answer the public ``…/services/pcs/regions`` parameters with a fixture list.

    Floci 2.4.0 serves none of the public global-infrastructure parameters
    (``GetParametersByPath`` under that tree answers an empty page). The
    harness inventory reads AWS PCS's Regions from exactly that path, because
    botocore's endpoint data names no Region for PCS, and fails the
    collection closed when nothing names one. The shim answers only the PCS
    path, with the Regions the emulator enables (``_PCS_REGIONS_FIXTURE``,
    deliberately not a copy of AWS's list, which production reads live);
    every other ``GetParametersByPath`` (the harness's own parameters, the
    CloudFormation fixtures' SSM markers) reaches the emulator.
    """

    def _synthesize(request, **_kwargs):
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        try:
            path = str(json.loads(body or "{}").get("Path") or "")
        except ValueError:
            return None
        if path.rstrip("/") != _PCS_REGIONS_PARAMETER_PATH:
            return None  # not the PCS Region list; let the emulator answer
        parameters = [
            {
                "Name": f"{_PCS_REGIONS_PARAMETER_PATH}/{region}",
                "Type": "String",
                "Value": region,
                "Version": 1,
                "DataType": "text",
                "ARN": f"arn:aws:ssm:us-east-1::parameter{_PCS_REGIONS_PARAMETER_PATH}/{region}",
            }
            for region in _PCS_REGIONS_FIXTURE
        ]
        return _local_response(
            request, json.dumps({"Parameters": parameters}).encode(), "application/x-amz-json-1.1"
        )

    events.register("before-send.ssm.GetParametersByPath", _synthesize)


def shim_floci_pcs_list_clusters(events) -> None:
    """Answer AWS PCS ``ListClusters`` with the no-clusters shape.

    PCS is absent from Floci 2.4.0's catalog: ``ListClusters``, ``GetCluster``
    and ``CreateCluster`` all answer ``UnknownOperationException``. The
    harness inventory lists every PCS Region's clusters to prove the
    project's absent; real AWS returns an empty page for an account with no
    clusters, and an emulator account can have none. Every other PCS
    operation stays unshimmed (``cli.pcs_preflight`` and the PCS backend's
    ``GetCluster`` keep their coverage in patched-client unit tests).
    """

    def _synthesize(request, **_kwargs):
        return _local_response(request, _NO_PCS_CLUSTERS_JSON, "application/x-amz-json-1.0")

    events.register("before-send.pcs.ListClusters", _synthesize)


def apply_known_floci_gap_shims(events) -> None:
    """Install every documented Floci-gap shim on a botocore event system."""
    shim_floci_get_stack_policy(events)
    shim_floci_missing_xray(events)
    shim_floci_sagemaker_list_clusters(events)
    shim_floci_pcs_service_regions(events)
    shim_floci_pcs_list_clusters(events)
