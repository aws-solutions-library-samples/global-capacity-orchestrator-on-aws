"""
Tests for Grafana admin credential rotation (cluster observability).

Covers two things:

1. ``gco/services/grafana_rotator.py`` — the rotation logic that reads the
   current admin credential from the chart-generated Secret, stages the new
   password in it, resets the live password through Grafana's admin HTTP API,
   then moves the new value into ``admin-password``. The Kubernetes client and
   ``requests`` are mocked so no cluster or network is touched; the tests
   assert the API contract (URLs, basic auth, body), that the value written
   back to the Secret is exactly the one the API was reset to, and, against a
   stateful fake Secret and Grafana, that a failure at any step leaves the
   Secret holding the password Grafana accepts and the next run settles it.

2. ``lambda/kubectl-applier-simple/manifests/post-helm-grafana-credential-rotation.yaml``
   — the gated CronJob + least-privilege RBAC that runs the rotator on a
   schedule using an existing GCO image.
"""

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import yaml
from kubernetes.client.exceptions import ApiException

from gco.services import grafana_rotator

PROJECT_ROOT = Path(__file__).parent.parent
ROTATION_MANIFEST = (
    PROJECT_ROOT
    / "lambda"
    / "kubectl-applier-simple"
    / "manifests"
    / "post-helm-grafana-credential-rotation.yaml"
)

SECRET_NAME = "kube-prometheus-stack-grafana"
NAMESPACE = "monitoring"
# The unit tests drive the rotator over a plain-http override (no CA bundle
# needed); the shipped CronJob calls the verified-HTTPS grafana-tls Service.
# tests/test_grafana_rotator_tls.py covers the https trust path.
SERVICE_URL = "http://kube-prometheus-stack-grafana.monitoring.svc"
MANIFEST_SERVICE_URL = "https://grafana-tls.monitoring.svc.cluster.local:3443"
GATE_ANNOTATION = "gco.io/cluster-observability-enabled"
GATE_PLACEHOLDER = "{{CLUSTER_OBSERVABILITY_ENABLED}}"


# Test fixture: the literal is a fake "current" password used only to build the
# mock Secret payload the rotator reads back; it is not a real credential.
# nosemgrep: hardcoded-password-default-argument
def _secret(user: str = "admin", password: str = "old-password") -> MagicMock:
    """A fake read_namespaced_secret return with base64 data, like the API."""
    secret = MagicMock()
    secret.data = {
        "admin-user": base64.b64encode(user.encode()).decode(),
        "admin-password": base64.b64encode(password.encode()).decode(),
    }
    return secret


# ---------------------------------------------------------------------------
# Rotator logic
# ---------------------------------------------------------------------------


class TestRotatorUnits:
    def test_generate_password_is_strong_and_unique(self) -> None:
        a = grafana_rotator.generate_password()
        b = grafana_rotator.generate_password()
        assert a != b
        assert len(a) >= 24

    def test_read_admin_credentials_decodes_base64(self) -> None:
        core = MagicMock()
        core.read_namespaced_secret.return_value = _secret("admin", "s3cret")
        user, password = grafana_rotator.read_admin_credentials(core, NAMESPACE, SECRET_NAME)
        assert (user, password) == ("admin", "s3cret")
        core.read_namespaced_secret.assert_called_once_with(SECRET_NAME, NAMESPACE)

    def test_read_admin_credentials_missing_key_raises(self) -> None:
        core = MagicMock()
        secret = MagicMock()
        secret.data = {"admin-user": base64.b64encode(b"admin").decode()}  # no password
        core.read_namespaced_secret.return_value = secret
        with pytest.raises(KeyError):
            grafana_rotator.read_admin_credentials(core, NAMESPACE, SECRET_NAME)

    def test_get_admin_user_id_calls_api_user(self) -> None:
        resp = MagicMock()
        resp.json.return_value = {"id": 7, "login": "admin"}
        with patch.object(grafana_rotator.requests, "get", return_value=resp) as mock_get:
            user_id = grafana_rotator.get_admin_user_id(SERVICE_URL, ("admin", "pw"))
        assert user_id == 7
        args, kwargs = mock_get.call_args
        assert args[0] == f"{SERVICE_URL}/api/user"
        assert kwargs["auth"] == ("admin", "pw")
        resp.raise_for_status.assert_called_once()

    def test_reset_admin_password_puts_to_admin_api(self) -> None:
        resp = MagicMock()
        with patch.object(grafana_rotator.requests, "put", return_value=resp) as mock_put:
            grafana_rotator.reset_admin_password(SERVICE_URL, ("admin", "pw"), 7, "new-pw")
        args, kwargs = mock_put.call_args
        assert args[0] == f"{SERVICE_URL}/api/admin/users/7/password"
        assert kwargs["auth"] == ("admin", "pw")
        assert kwargs["json"] == {"password": "new-pw"}
        resp.raise_for_status.assert_called_once()

    def test_patch_secret_password_writes_base64(self) -> None:
        core = MagicMock()
        grafana_rotator.patch_secret_password(core, NAMESPACE, SECRET_NAME, "new-pw")
        core.patch_namespaced_secret.assert_called_once()
        name, ns, body = core.patch_namespaced_secret.call_args.args
        assert (name, ns) == (SECRET_NAME, NAMESPACE)
        assert body["data"]["admin-password"] == base64.b64encode(b"new-pw").decode()

    def test_rotate_resets_then_persists_same_password(self) -> None:
        """End to end: the password PUT to Grafana is the same one written to
        the Secret, and the reset happens before the Secret is patched."""
        core = MagicMock()
        core.read_namespaced_secret.return_value = _secret("admin", "old-password")
        get_resp = MagicMock()
        get_resp.json.return_value = {"id": 1}
        put_resp = MagicMock()

        with (
            patch.object(grafana_rotator, "generate_password", return_value="brand-new-pw"),
            patch.object(grafana_rotator.requests, "get", return_value=get_resp),
            patch.object(grafana_rotator.requests, "put", return_value=put_resp) as mock_put,
        ):
            grafana_rotator.rotate(core, NAMESPACE, SECRET_NAME, SERVICE_URL)

        # Reset used the CURRENT creds and the NEW password.
        assert mock_put.call_args.kwargs["auth"] == ("admin", "old-password")
        assert mock_put.call_args.kwargs["json"] == {"password": "brand-new-pw"}
        # Secret patched with the SAME new password.
        body = core.patch_namespaced_secret.call_args.args[2]
        assert body["data"]["admin-password"] == base64.b64encode(b"brand-new-pw").decode()

    def test_main_returns_1_on_request_error(self) -> None:
        with (
            patch.object(grafana_rotator.config, "load_incluster_config"),
            patch.object(grafana_rotator.client, "CoreV1Api", return_value=MagicMock()),
            patch.object(grafana_rotator, "rotate", side_effect=requests.RequestException("boom")),
        ):
            assert grafana_rotator.main() == 1

    def test_main_returns_0_on_success(self) -> None:
        with (
            patch.object(grafana_rotator.config, "load_incluster_config"),
            patch.object(grafana_rotator.client, "CoreV1Api", return_value=MagicMock()),
            patch.object(grafana_rotator, "rotate") as mock_rotate,
        ):
            assert grafana_rotator.main() == 0
        mock_rotate.assert_called_once()


class FakeSecretApi:
    """The Grafana Secret with merge-patch semantics (a ``None`` value deletes the key)."""

    def __init__(self, password: str) -> None:
        self.data = {
            "admin-user": base64.b64encode(b"admin").decode(),
            "admin-password": base64.b64encode(password.encode()).decode(),
        }
        self.patch_failures: list[bool] = []  # one entry per patch call; True fails it

    def read_namespaced_secret(self, name: str, namespace: str) -> MagicMock:
        return MagicMock(data=dict(self.data))

    def patch_namespaced_secret(self, name: str, namespace: str, body: dict) -> None:
        if self.patch_failures and self.patch_failures.pop(0):
            raise ApiException(status=500, reason="etcdserver: request timed out")
        for key, value in body["data"].items():
            if value is None:
                self.data.pop(key, None)
            else:
                self.data[key] = value

    def password(self, key: str = "admin-password") -> str | None:
        value = self.data.get(key)
        return base64.b64decode(value).decode() if value else None


class FakeGrafana:
    """Grafana's admin API: GET /api/user authenticates, PUT resets the password."""

    def __init__(self, password: str) -> None:
        self.password = password
        self.lose_reset_response = False

    def get(self, url: str, auth: tuple[str, str], timeout: int, verify: object) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200 if auth[1] == self.password else 401
        if resp.status_code == 401:
            resp.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
        resp.json.return_value = {"id": 1}
        return resp

    def put(self, url: str, auth: tuple[str, str], json: dict, timeout: int, verify: object):
        if auth[1] != self.password:
            raise requests.HTTPError("401 Unauthorized")
        self.password = json["password"]
        if self.lose_reset_response:
            raise requests.ConnectionError("connection reset after the request was sent")
        return MagicMock()


@pytest.fixture
def grafana():
    live = FakeGrafana("old-password")
    with (
        patch.object(grafana_rotator.requests, "get", live.get),
        patch.object(grafana_rotator.requests, "put", live.put),
        patch.object(grafana_rotator.time, "sleep") as sleep,
    ):
        live.sleep = sleep
        yield live


class TestRotationNeverLosesThePassword:
    """Every failure leaves the Secret holding the password Grafana accepts."""

    def test_a_failed_final_write_is_finished_by_the_next_run(self, grafana) -> None:
        core = FakeSecretApi("old-password")
        core.patch_failures = [False, True, True, True]  # stage ok, every promotion fails

        with pytest.raises(ApiException):
            grafana_rotator.rotate(core, NAMESPACE, SECRET_NAME, SERVICE_URL)

        # Grafana moved on; the Secret still has the live password, staged.
        assert grafana.password != "old-password"
        assert core.password("admin-password-pending") == grafana.password
        assert grafana.sleep.call_count == 2

        grafana_rotator.rotate(core, NAMESPACE, SECRET_NAME, SERVICE_URL)

        assert core.password() == grafana.password
        assert "admin-password-pending" not in core.data

    def test_a_transient_final_write_failure_is_retried(self, grafana) -> None:
        core = FakeSecretApi("old-password")
        core.patch_failures = [False, True, False]

        grafana_rotator.rotate(core, NAMESPACE, SECRET_NAME, SERVICE_URL)

        assert core.password() == grafana.password != "old-password"
        assert "admin-password-pending" not in core.data

    def test_a_reset_whose_response_is_lost_is_settled_in_the_same_run(self, grafana) -> None:
        core = FakeSecretApi("old-password")
        grafana.lose_reset_response = True

        with pytest.raises(requests.ConnectionError):
            grafana_rotator.rotate(core, NAMESPACE, SECRET_NAME, SERVICE_URL)

        assert core.password() == grafana.password != "old-password"
        assert "admin-password-pending" not in core.data

    def test_a_reset_that_never_applied_discards_the_staged_password(self, grafana) -> None:
        core = FakeSecretApi("old-password")
        with (
            patch.object(
                grafana_rotator.requests, "put", side_effect=requests.ConnectionError("refused")
            ),
            pytest.raises(requests.ConnectionError),
        ):
            grafana_rotator.rotate(core, NAMESPACE, SECRET_NAME, SERVICE_URL)

        assert core.password() == grafana.password == "old-password"
        assert "admin-password-pending" not in core.data

    def test_a_failed_staging_write_leaves_grafana_untouched(self, grafana) -> None:
        core = FakeSecretApi("old-password")
        core.patch_failures = [True]

        with pytest.raises(ApiException):
            grafana_rotator.rotate(core, NAMESPACE, SECRET_NAME, SERVICE_URL)

        assert grafana.password == "old-password" == core.password()

    def test_neither_password_working_fails_without_changing_anything(self, grafana) -> None:
        core = FakeSecretApi("stale-password")
        core.data["admin-password-pending"] = base64.b64encode(b"other-stale").decode()

        with pytest.raises(grafana_rotator.RotationStateError, match="reset the admin password"):
            grafana_rotator.rotate(core, NAMESPACE, SECRET_NAME, SERVICE_URL)
        with (
            patch.object(grafana_rotator.config, "load_incluster_config"),
            patch.object(grafana_rotator.client, "CoreV1Api", return_value=core),
        ):
            assert grafana_rotator.main() == 1

        assert grafana.password == "old-password"
        assert core.password() == "stale-password"


# ---------------------------------------------------------------------------
# Rotation manifest (CronJob + RBAC)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rotation_docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(ROTATION_MANIFEST.read_text()) if d]


def test_rotation_manifest_is_post_helm() -> None:
    assert ROTATION_MANIFEST.name.startswith("post-helm-")


def test_manifest_has_sa_role_binding_cronjob(rotation_docs: list[dict]) -> None:
    kinds = sorted(d["kind"] for d in rotation_docs)
    assert kinds == ["CronJob", "Role", "RoleBinding", "ServiceAccount"]


def test_every_object_is_gated_and_in_monitoring(rotation_docs: list[dict]) -> None:
    for d in rotation_docs:
        assert d["metadata"]["namespace"] == "monitoring", d["kind"]
        annotations = d["metadata"].get("annotations", {})
        assert annotations.get(GATE_ANNOTATION) == GATE_PLACEHOLDER, d["kind"]


def test_role_is_least_privilege(rotation_docs: list[dict]) -> None:
    role = next(d for d in rotation_docs if d["kind"] == "Role")
    assert len(role["rules"]) == 1
    rule = role["rules"][0]
    assert rule["resources"] == ["secrets"]
    assert sorted(rule["verbs"]) == ["get", "patch"]
    # Scoped to just the one chart-generated Secret — no wildcard secret access.
    assert rule["resourceNames"] == [SECRET_NAME]


def test_cronjob_runs_rotator_via_gco_image(rotation_docs: list[dict]) -> None:
    cronjob = next(d for d in rotation_docs if d["kind"] == "CronJob")
    spec = cronjob["spec"]
    assert spec["schedule"] == "{{GRAFANA_ADMIN_PASSWORD_ROTATION_SCHEDULE}}"
    assert spec["concurrencyPolicy"] == "Forbid"
    container = spec["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "{{MANIFEST_PROCESSOR_IMAGE}}"
    assert container["command"] == ["python", "-m", "gco.services.grafana_rotator"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["GRAFANA_ADMIN_SECRET"] == SECRET_NAME
    assert env["GRAFANA_SERVICE_URL"] == MANIFEST_SERVICE_URL
    assert env["GRAFANA_SERVICE_URL"] == grafana_rotator.DEFAULT_SERVICE_URL
    assert env["GCO_INTERNAL_CA_FILE"] == "/var/run/gco/ca/ca.crt"


def test_cronjob_mounts_only_the_internal_ca_certificate(rotation_docs: list[dict]) -> None:
    """The rotator verifies Grafana against the GCO internal CA and sees no private key.

    The bundle is the ``ca.crt`` key of the gco-monitoring-trust Secret (a
    cert-manager leaf in the rotator's own namespace, issued by the internal
    CA only so that its Secret carries ``ca.crt``); nothing else from that
    Secret is projected.
    """
    cronjob = next(d for d in rotation_docs if d["kind"] == "CronJob")
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    # 0440 + the pod's fsGroup, like every GCO-built pod's Secret volume.
    assert pod["securityContext"]["fsGroup"] == 10000
    assert volumes["internal-ca"]["secret"] == {
        "secretName": "gco-monitoring-trust",
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
        "defaultMode": 0o440,
    }
    (container,) = pod["containers"]
    assert container["volumeMounts"] == [
        {"name": "internal-ca", "mountPath": "/var/run/gco/ca", "readOnly": True}
    ]


def test_cronjob_pod_is_hardened(rotation_docs: list[dict]) -> None:
    cronjob = next(d for d in rotation_docs if d["kind"] == "CronJob")
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "gco-grafana-rotator"
    assert pod["securityContext"]["runAsNonRoot"] is True
    container_sc = pod["containers"][0]["securityContext"]
    assert container_sc["allowPrivilegeEscalation"] is False
    assert container_sc["readOnlyRootFilesystem"] is True
    assert container_sc["capabilities"]["drop"] == ["ALL"]


def test_cronjob_body_has_no_unreplaced_lowercase_json() -> None:
    """Sanity: the only placeholders in the manifest are UPPER_SNAKE gates so
    the applier's skip rule behaves (gate the file, not accidentally match)."""
    text = ROTATION_MANIFEST.read_text()
    # Substitute the two known gates; nothing UPPER_SNAKE should remain.
    substituted = (
        text.replace(GATE_PLACEHOLDER, "true")
        .replace("{{GRAFANA_ADMIN_PASSWORD_ROTATION_SCHEDULE}}", "0 4 1 * *")
        .replace("{{MANIFEST_PROCESSOR_IMAGE}}", "img:latest")
    )
    import re

    assert re.search(r"\{\{[A-Z0-9_]+\}\}", substituted) is None
    # And the whole thing is still valid YAML after substitution.
    assert list(yaml.safe_load_all(substituted))
