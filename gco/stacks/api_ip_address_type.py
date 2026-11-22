"""The IP address type of the API Gateway REST APIs GCO creates.

``api_gateway.ip_address_type`` in ``cdk.json`` applies to the global API and
to every regional API bridge. ``dualstack`` makes API Gateway answer the
default ``execute-api`` host names with IPv6 (AAAA) records as well as IPv4,
so clients on IPv6-only or IPv6-preferring networks reach GCO directly.
API Gateway applies a change of IP address type to the existing endpoint,
without a new deployment of the API. Nothing behind API Gateway changes:
GCO's VPCs, clusters and internal load balancers stay IPv4.
"""

from __future__ import annotations

from aws_cdk import aws_apigateway as apigateway

_IP_ADDRESS_TYPES = {
    "ipv4": apigateway.IpAddressType.IPV4,
    "dualstack": apigateway.IpAddressType.DUAL_STACK,
}


def api_ip_address_type(value: str) -> apigateway.IpAddressType:
    """The CDK value for ``api_gateway.ip_address_type`` (``ipv4`` or ``dualstack``)."""
    try:
        return _IP_ADDRESS_TYPES[value]
    except KeyError:
        raise ValueError(
            f"api_gateway.ip_address_type must be one of {list(_IP_ADDRESS_TYPES)}, got {value!r}"
        ) from None
