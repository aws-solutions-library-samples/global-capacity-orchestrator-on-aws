"""Kubernetes API group/version pins shared by the GCO services.

One source for API versions that more than one service calls through the
generic ``CustomObjectsApi`` (which takes the group and version as plain
strings, so nothing in the client library pins them). Standard library only:
every service image copies ``gco/`` wholesale, so anything imported here is
imported by every service.
"""

METRICS_API_GROUP = "metrics.k8s.io"
"""API group of the Kubernetes resource-metrics API served by metrics-server."""

METRICS_API_VERSION = "v1beta1"
"""Version of the resource-metrics API the services read.

``metrics.k8s.io/v1`` is GA in Kubernetes 1.37, but the metrics-server
add-on GCO pins (``EKS_ADDON_METRICS_SERVER`` in ``gco/stacks/constants.py``,
a ``v0.9.x`` build) registers only ``v1beta1`` in its ``pkg/api/install.go``;
upstream ``master`` registers ``v1`` as well, so the first release to carry it
is expected to be ``v0.10.x``. Flip this to ``"v1"`` once the pinned add-on is
such a release (``tests/test_k8s_api_versions.py`` fails on a ``v0.10+`` pin
while this still says ``v1beta1``, as the reminder). The RBAC grants in
``lambda/kubectl-applier-simple/manifests/02-rbac.yaml`` name only the group,
so they need no change when the version moves.
"""
