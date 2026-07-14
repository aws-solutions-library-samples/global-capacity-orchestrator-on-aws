"""Grafana user management over the admin HTTP API (cluster observability).

Grafana uses its own native user database (self sign-up and anonymous access are
disabled), so ``gco monitoring users`` drives Grafana's admin HTTP API rather
than Cognito. Because Grafana is a private ``ClusterIP`` Service, these calls go
through a ``gco monitoring open`` port-forward (default ``http://localhost:3000``).

Admin credentials come from the chart-generated ``kube-prometheus-stack-grafana``
Secret (read here via ``kubectl``) unless the caller supplies them explicitly.

The HTTP functions are thin, mockable wrappers over ``requests`` (unit-tested by
patching ``requests``), mirroring :mod:`cli.analytics_user_mgmt`'s shape.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import subprocess

import requests

DEFAULT_GRAFANA_URL = "http://localhost:3000"
DEFAULT_NAMESPACE = "monitoring"
DEFAULT_ADMIN_SECRET = "kube-prometheus-stack-grafana"

_HTTP_TIMEOUT_SECONDS = 15
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")
_SECRET_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$")


def generate_password(n_bytes: int = 24) -> str:
    """Return a cryptographically strong, URL-safe password."""
    return secrets.token_urlsafe(n_bytes)


def read_grafana_admin_credentials(
    namespace: str = DEFAULT_NAMESPACE, secret_name: str = DEFAULT_ADMIN_SECRET
) -> tuple[str, str]:
    """Read ``(admin_user, admin_password)`` from the Grafana Secret via kubectl.

    Requires cluster access (the same connectivity ``gco monitoring open`` needs).
    Raises ``RuntimeError`` if kubectl fails or the keys are absent.
    """
    if not _NAMESPACE_RE.match(namespace):
        raise ValueError(f"Invalid namespace {namespace!r}")
    if not _SECRET_RE.match(secret_name):
        raise ValueError(f"Invalid secret name {secret_name!r}")

    cmd = ["kubectl", "get", "secret", secret_name, "-n", namespace, "-o", "json"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True
        )  # nosemgrep: dangerous-subprocess-use-audit - inputs validated above; list form, no shell=True
    except FileNotFoundError as exc:
        raise RuntimeError(
            "kubectl not found. Install kubectl and ensure it's on your PATH."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to read Secret {namespace}/{secret_name}: {result.stderr.strip()}"
        )

    data = json.loads(result.stdout or "{}").get("data", {})
    if "admin-user" not in data or "admin-password" not in data:
        raise RuntimeError(
            f"Secret {namespace}/{secret_name} does not carry admin-user/admin-password"
        )
    user = base64.b64decode(data["admin-user"]).decode("utf-8")
    password = base64.b64decode(data["admin-password"]).decode("utf-8")
    return user, password


def create_user(
    base_url: str,
    auth: tuple[str, str],
    *,
    login: str,
    password: str,
    email: str | None = None,
    name: str | None = None,
) -> int:
    """Create a Grafana user via ``POST /api/admin/users``; return the new id."""
    body: dict[str, str] = {"login": login, "password": password, "name": name or login}
    if email:
        body["email"] = email
    resp = requests.post(
        f"{base_url}/api/admin/users", auth=auth, json=body, timeout=_HTTP_TIMEOUT_SECONDS
    )
    resp.raise_for_status()
    return int(resp.json()["id"])


def list_users(base_url: str, auth: tuple[str, str]) -> list[dict[str, object]]:
    """List organisation users via ``GET /api/org/users``."""
    resp = requests.get(f"{base_url}/api/org/users", auth=auth, timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    result = resp.json()
    return result if isinstance(result, list) else []


def lookup_user_id(base_url: str, auth: tuple[str, str], login_or_email: str) -> int:
    """Resolve a login/email to a numeric user id via ``GET /api/users/lookup``."""
    resp = requests.get(
        f"{base_url}/api/users/lookup",
        params={"loginOrEmail": login_or_email},
        auth=auth,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return int(resp.json()["id"])


def delete_user(base_url: str, auth: tuple[str, str], user_id: int) -> None:
    """Delete a Grafana user via ``DELETE /api/admin/users/<id>``."""
    resp = requests.delete(
        f"{base_url}/api/admin/users/{int(user_id)}",
        auth=auth,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
