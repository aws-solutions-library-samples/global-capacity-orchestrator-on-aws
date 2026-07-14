"""Scheduled rotation of the Grafana admin password (cluster observability).

Runs as an in-cluster ``CronJob`` (see
``lambda/kubectl-applier-simple/manifests/post-helm-grafana-credential-rotation.yaml``)
using an existing GCO service image, so no extra container image is introduced.

Why an active reset rather than a restart: the kube-prometheus-stack Grafana
subchart auto-generates the admin credential into the ``<release>-grafana``
Secret and Grafana persists it in its own database. ``GF_SECURITY_ADMIN_PASSWORD``
only seeds the password on Grafana's *first* start, so patching the Secret and
restarting the pod does not change the live password. Rotation therefore resets
the password through Grafana's admin HTTP API and then updates the Secret that
``gco monitoring`` reads at runtime.

Order is admin-API-first so the live password and the Secret only diverge for
the single Secret-patch call that follows a successful reset:

  1. Read the current admin user/password from the Secret.
  2. Generate a new strong password.
  3. ``GET  /api/user``                     -> the admin user's numeric id.
  4. ``PUT  /api/admin/users/<id>/password`` with the new password.
  5. Patch the Secret's ``admin-password`` to the new value.

Least privilege: the only Kubernetes permission the CronJob's ServiceAccount is
granted is ``get``/``patch`` on the single Grafana Secret in the ``monitoring``
namespace. The reset goes through the ClusterIP Service, so no ``pods/exec`` and
no AWS/IRSA permissions are required.
"""

import base64
import logging
import os
import secrets
import sys

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger("gco.grafana_rotator")

# Defaults match the kube-prometheus-stack release name GCO installs
# (the helm-installer uses the chart key as the release name, so the Grafana
# subchart resources are named ``kube-prometheus-stack-grafana``). All three are
# overridable via env so the manifest stays the single source of truth.
DEFAULT_NAMESPACE = "monitoring"
DEFAULT_SECRET_NAME = "kube-prometheus-stack-grafana"
DEFAULT_SERVICE_URL = "http://kube-prometheus-stack-grafana.monitoring.svc"

_ADMIN_USER_KEY = "admin-user"
_ADMIN_PASSWORD_KEY = "admin-password"

# 24 random bytes -> ~32 URL-safe characters. Comfortably above any sane
# minimum-length policy while staying a valid Grafana password.
_PASSWORD_ENTROPY_BYTES = 24
_HTTP_TIMEOUT_SECONDS = 15


def generate_password(n_bytes: int = _PASSWORD_ENTROPY_BYTES) -> str:
    """Return a cryptographically strong, URL-safe password string."""
    return secrets.token_urlsafe(n_bytes)


def read_admin_credentials(
    core_v1: client.CoreV1Api, namespace: str, secret_name: str
) -> tuple[str, str]:
    """Return ``(admin_user, admin_password)`` decoded from the Grafana Secret."""
    secret = core_v1.read_namespaced_secret(secret_name, namespace)
    data = secret.data or {}
    missing = [k for k in (_ADMIN_USER_KEY, _ADMIN_PASSWORD_KEY) if k not in data]
    if missing:
        raise KeyError(f"Secret {namespace}/{secret_name} is missing key(s): {', '.join(missing)}")
    user = base64.b64decode(data[_ADMIN_USER_KEY]).decode("utf-8")
    password = base64.b64decode(data[_ADMIN_PASSWORD_KEY]).decode("utf-8")
    return user, password


def get_admin_user_id(service_url: str, auth: tuple[str, str]) -> int:
    """Look up the authenticated admin user's numeric id via ``GET /api/user``.

    Resolving the id at runtime avoids hardcoding the bootstrap admin id (1),
    which would silently target the wrong account if the org admin ever changes.
    """
    resp = requests.get(f"{service_url}/api/user", auth=auth, timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return int(resp.json()["id"])


def reset_admin_password(
    service_url: str, auth: tuple[str, str], user_id: int, new_password: str
) -> None:
    """Reset the admin password through Grafana's admin API."""
    resp = requests.put(
        f"{service_url}/api/admin/users/{user_id}/password",
        auth=auth,
        json={"password": new_password},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def patch_secret_password(
    core_v1: client.CoreV1Api, namespace: str, secret_name: str, new_password: str
) -> None:
    """Write the new password back into the Grafana Secret (base64 ``data``)."""
    encoded = base64.b64encode(new_password.encode("utf-8")).decode("utf-8")
    core_v1.patch_namespaced_secret(
        secret_name, namespace, {"data": {_ADMIN_PASSWORD_KEY: encoded}}
    )


def rotate(core_v1: client.CoreV1Api, namespace: str, secret_name: str, service_url: str) -> None:
    """Rotate the Grafana admin password end to end.

    Reads the current credential, resets the live password through the admin
    API, then persists the new value to the Secret. The password itself is
    never logged.
    """
    user, current_password = read_admin_credentials(core_v1, namespace, secret_name)
    new_password = generate_password()
    auth = (user, current_password)
    user_id = get_admin_user_id(service_url, auth)
    reset_admin_password(service_url, auth, user_id, new_password)
    patch_secret_password(core_v1, namespace, secret_name, new_password)
    # Audit line carries only non-secret identifiers (username, user id, service
    # URL, namespace/secret names). The password itself is never logged.
    # nosemgrep: python-logger-credential-disclosure
    logger.info(
        "Rotated Grafana admin credential for user %r (id=%s) via %s (secret %s/%s)",
        user,
        user_id,
        service_url,
        namespace,
        secret_name,
    )


def main() -> int:
    """CronJob entrypoint. Returns a process exit code (0 ok, 1 on failure)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    namespace = os.environ.get("GRAFANA_NAMESPACE", DEFAULT_NAMESPACE)
    secret_name = os.environ.get("GRAFANA_ADMIN_SECRET", DEFAULT_SECRET_NAME)
    service_url = os.environ.get("GRAFANA_SERVICE_URL", DEFAULT_SERVICE_URL).rstrip("/")

    try:
        config.load_incluster_config()
    except config.ConfigException:
        # Allows local dry-runs against a configured kubeconfig; in the CronJob
        # the in-cluster path always wins.
        config.load_kube_config()

    core_v1 = client.CoreV1Api()
    try:
        rotate(core_v1, namespace, secret_name, service_url)
    except (ApiException, requests.RequestException, KeyError, ValueError) as exc:
        # Only the caught exception is logged; it carries no credential material.
        # nosemgrep: python-logger-credential-disclosure
        logger.error("Grafana admin credential rotation failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
