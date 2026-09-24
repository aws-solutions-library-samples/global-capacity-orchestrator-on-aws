"""The operator surface of EKS Capabilities: ``gco stacks capabilities``.

* ``cli/eks_capabilities.py`` — the configured-vs-live merge behind
  ``gco stacks capabilities status`` (cdk.json intent, ``ListCapabilities`` /
  ``DescribeCapability`` results, drift sentences, unmanaged capabilities, the
  missing-cluster case) and the ``eks_capabilities_status`` MCP tool.
* ``cli/argocd_ui.py`` — resolving the hosted Argo CD server URL and the
  Playwright-driven screenshot of the Applications view (persistent per-region
  browser profile, SSO button, sign-in wait, full-page PNG). ``playwright.sync_api``
  is replaced in ``sys.modules`` by an in-memory fake, as
  ``tests/test_capture_monitoring_screenshots.py`` does, so no browser runs.
* The Click commands (``status``, ``argocd open``, ``argocd screenshot``) through
  ``CliRunner`` in table and JSON modes, with the AWS/browser layers patched.

The CLI must import without ``aws_cdk``: the config module the commands read
lives at ``gco/eks_capabilities_config.py`` for that reason, pinned here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli import argocd_ui, eks_capabilities
from cli.main import cli
from gco import eks_capabilities_config as caps

_REGION = "us-east-1"
_PROJECT = "gco"
_CLUSTER = "gco-us-east-1"
_ACCOUNT = "123456789012"
_IDC_ARN = "arn:aws:sso:::instance/ssoins-1234567890abcdef"
_SERVER_URL = "https://a1b2c3d4.argocd.us-east-1.eks.amazonaws.com"
_ADMIN_MAPPING = {"role": "ADMIN", "identities": [{"id": "u-admin", "type": "SSO_USER"}]}


def _argocd_config(
    *,
    gitops: bool = False,
    regions: list[str] | None = None,
    source: str = "git",
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "argocd": {
            "enabled": True,
            "idc_instance_arn": _IDC_ARN,
            "rbac_role_mappings": [_ADMIN_MAPPING],
            "regions": regions or [],
        }
    }
    if gitops and source == "git":
        block["argocd"]["gitops"] = {
            "enabled": True,
            "source": "git",
            "repo_url": "https://github.com/example/gco-tenants.git",
            "revision": "main",
            "path": "clusters/{region}",
            "destination_namespaces": ["gco-jobs"],
            "sync_policy": "automated",
        }
    elif gitops:
        block["argocd"]["gitops"] = {"enabled": True, "sync_policy": "automated"}
    return caps.normalize_eks_capabilities_config(block)


def _live(
    type_name: str,
    *,
    name: str | None = None,
    status: str = "ACTIVE",
    server_url: str | None = None,
    issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "capabilityName": name or f"{_PROJECT}-{type_name}",
        "arn": f"arn:aws:eks:{_REGION}:{_ACCOUNT}:capability/{_CLUSTER}/{name or type_name}",
        "clusterName": _CLUSTER,
        "type": caps.CAPABILITY_TYPE_API_NAMES.get(type_name, type_name),
        "roleArn": f"arn:aws:iam::{_ACCOUNT}:role/{type_name}-capability",
        "status": status,
        "version": "3.1.0",
    }
    if server_url:
        detail["configuration"] = {"argoCd": {"serverUrl": server_url}}
    if issues:
        detail["health"] = {"issues": issues}
    return detail


def _client_error(code: str, operation: str = "DescribeCapability") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeEks:
    """Duck-typed EKS client: paginated ListCapabilities + DescribeCapability."""

    def __init__(self, pages: list[list[dict[str, Any]]], details: dict[str, dict[str, Any]]):
        self.pages = pages
        self.details = details
        self.described: list[str] = []
        self.list_error: Exception | None = None

    def get_paginator(self, operation: str) -> Any:
        assert operation == "list_capabilities"
        fake = self

        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                assert kwargs == {"clusterName": _CLUSTER}
                if fake.list_error is not None:
                    raise fake.list_error
                for page in fake.pages:
                    yield {"capabilities": page}

        return _Paginator()

    def describe_capability(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["clusterName"] == _CLUSTER
        self.described.append(kwargs["capabilityName"])
        detail = self.details.get(kwargs["capabilityName"])
        if isinstance(detail, Exception):
            raise detail
        return {"capability": detail} if detail is not None else {}


# ─── cli/eks_capabilities.py ─────────────────────────────────────────────────


class TestLoadConfig:
    def _write(self, tmp_path: Path, context: dict[str, Any]) -> Path:
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps({"context": context}), encoding="utf-8")
        return path

    def test_absent_block_reads_as_all_off(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"project_name": _PROJECT})
        assert eks_capabilities.load_eks_capabilities_config(path) == caps.EKS_CAPABILITIES_DEFAULTS

    def test_block_is_validated_against_the_regional_deployment_regions(
        self, tmp_path: Path
    ) -> None:
        path = self._write(
            tmp_path,
            {
                "deployment_regions": {"regional": [_REGION]},
                "eks_capabilities": {"kro": {"enabled": True, "regions": ["eu-west-1"]}},
            },
        )
        with pytest.raises(caps.EksCapabilitiesConfigError, match="eu-west-1"):
            eks_capabilities.load_eks_capabilities_config(path)

    def test_missing_cdk_json_is_a_runtime_error(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match=r"cdk\.json not found"):
            eks_capabilities.load_eks_capabilities_config(tmp_path / "cdk.json")

    def test_defaults_to_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(tmp_path, {"eks_capabilities": {"kro": {"enabled": True}}})
        monkeypatch.chdir(tmp_path)
        assert eks_capabilities.load_eks_capabilities_config()["kro"]["enabled"] is True

    def test_shipped_cdk_json_loads(self) -> None:
        root = Path(__file__).resolve().parent.parent
        assert (
            eks_capabilities.load_eks_capabilities_config(root / "cdk.json")
            == caps.EKS_CAPABILITIES_DEFAULTS
        )

    def test_run_scoped_overrides_merge_like_the_cdk_app(self, tmp_path: Path) -> None:
        """A harness that deployed with eks_capabilities_overrides reads the same block back."""
        path = self._write(
            tmp_path,
            {
                "deployment_regions": {"regional": [_REGION]},
                "eks_capabilities": {"argocd": {"idc_region": "us-east-2"}},
            },
        )
        overrides = json.dumps({"kro": {"enabled": True}})
        config = eks_capabilities.load_eks_capabilities_config(path, overrides=overrides)
        assert config["kro"]["enabled"] is True
        assert config["argocd"]["idc_region"] == "us-east-2"
        with pytest.raises(caps.EksCapabilitiesConfigError, match="must be a JSON object"):
            eks_capabilities.load_eks_capabilities_config(path, overrides="nope")


class TestDescribeLiveCapabilities:
    def test_describes_every_summary_across_pages(self) -> None:
        argo = _live("argocd", server_url=_SERVER_URL)
        kro = _live("kro")
        client = _FakeEks(
            pages=[
                [{"capabilityName": argo["capabilityName"]}],
                [{"capabilityName": kro["capabilityName"]}],
            ],
            details={argo["capabilityName"]: argo, kro["capabilityName"]: kro},
        )
        assert eks_capabilities.describe_live_capabilities(client, _CLUSTER) == [argo, kro]
        assert client.described == [argo["capabilityName"], kro["capabilityName"]]

    def test_falls_back_to_the_summary_when_describe_returns_nothing(self) -> None:
        summary = {"capabilityName": "gco-kro", "type": "KRO", "status": "CREATING"}
        client = _FakeEks(pages=[[summary, {"type": "ACK"}]], details={})
        assert eks_capabilities.describe_live_capabilities(client, _CLUSTER) == [summary]

    def test_no_capabilities(self) -> None:
        assert (
            eks_capabilities.describe_live_capabilities(_FakeEks(pages=[[]], details={}), _CLUSTER)
            == []
        )


class TestBuildStatus:
    def _status(
        self, config: dict[str, Any], live: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        return eks_capabilities.build_status(
            region=_REGION, project_name=_PROJECT, config=config, live=live, **kwargs
        )

    def test_all_off_and_nothing_attached_is_healthy(self) -> None:
        status = self._status(caps.EKS_CAPABILITIES_DEFAULTS, [])
        assert status["region"] == _REGION
        assert status["cluster_name"] == _CLUSTER
        assert status["cluster_found"] is True
        assert status["healthy"] is True
        assert status["unmanaged"] == []
        assert [row["type"] for row in status["capabilities"]] == ["argocd", "ack", "kro"]
        for row in status["capabilities"]:
            assert row["capability_name"] == f"{_PROJECT}-{row['type']}"
            assert (row["configured"], row["deployed"], row["status"], row["drift"]) == (
                False,
                False,
                None,
                None,
            )
        argo = status["capabilities"][0]
        assert argo["argocd_server_url"] is None
        assert argo["gitops"] == {"enabled": False}

    def test_active_argocd_with_gitops_reports_url_and_hand_off(self) -> None:
        status = self._status(
            _argocd_config(gitops=True), [_live("argocd", server_url=_SERVER_URL)]
        )
        argo = status["capabilities"][0]
        assert status["healthy"] is True
        assert argo["configured"] and argo["deployed"]
        assert argo["status"] == "ACTIVE"
        assert argo["version"] == "3.1.0"
        assert argo["arn"].startswith("arn:aws:eks:")
        assert argo["role_arn"] == f"arn:aws:iam::{_ACCOUNT}:role/argocd-capability"
        assert argo["argocd_server_url"] == _SERVER_URL
        assert argo["health_issues"] == []
        assert argo["drift"] is None
        assert argo["gitops"] == {
            "enabled": True,
            "source": "git",
            "repo_url": "https://github.com/example/gco-tenants.git",
            "revision": "main",
            "path": f"clusters/{_REGION}",
            "destination_namespaces": ["gco-jobs"],
            "sync_policy": "automated",
            "project": caps.GITOPS_PROJECT_NAME,
            "application": caps.GITOPS_ROOT_APPLICATION_NAME,
        }

    def test_codecommit_source_names_the_managed_repository(self) -> None:
        status = self._status(
            _argocd_config(gitops=True, source="codecommit"),
            [_live("argocd", server_url=_SERVER_URL)],
        )
        gitops = status["capabilities"][0]["gitops"]
        assert gitops["source"] == "codecommit"
        assert gitops["codecommit_repository"] == f"{_CLUSTER}-gitops"
        assert gitops["codecommit_branch"] == "main"
        assert (
            gitops["repo_url"]
            == f"https://git-codecommit.{_REGION}.amazonaws.com/v1/repos/{_CLUSTER}-gitops"
        )
        # The source default: the repository root.
        assert gitops["path"] == "."
        assert gitops["destination_namespaces"] == ["gco-jobs", "gco-inference"]

    def test_configured_but_not_attached_is_drift_naming_the_deploy(self) -> None:
        status = self._status(_argocd_config(), [])
        argo = status["capabilities"][0]
        assert argo["configured"] is True and argo["deployed"] is False
        assert argo["drift"] == (
            f"configured in cdk.json but not attached; run 'gco stacks deploy {_CLUSTER} -y'"
        )
        assert status["healthy"] is False

    def test_attached_but_disabled_is_drift_naming_the_removal(self) -> None:
        status = self._status(caps.EKS_CAPABILITIES_DEFAULTS, [_live("kro")])
        kro = status["capabilities"][2]
        assert kro["configured"] is False and kro["deployed"] is True
        assert "attached but disabled in cdk.json" in kro["drift"]
        assert "RETAIN" in kro["drift"]
        assert status["healthy"] is False

    def test_non_active_status_is_drift_with_health_issues(self) -> None:
        live = _live(
            "argocd",
            status="DEGRADED",
            issues=[
                {"code": "AccessDenied", "message": "role cannot assume"},
                {"code": "ClusterUnreachable"},
            ],
        )
        status = self._status(_argocd_config(), [live])
        argo = status["capabilities"][0]
        assert argo["health_issues"] == ["AccessDenied: role cannot assume", "ClusterUnreachable"]
        assert argo["drift"] == (
            "status is DEGRADED, expected ACTIVE (AccessDenied: role cannot assume; ClusterUnreachable)"
        )
        assert status["healthy"] is False

    def test_region_subset_excludes_this_region(self) -> None:
        status = self._status(_argocd_config(regions=["us-west-2"]), [])
        assert status["capabilities"][0]["configured"] is False
        assert status["healthy"] is True

    def test_unmanaged_capabilities_are_listed_separately(self) -> None:
        foreign = _live("argocd", name="team-argocd", server_url=_SERVER_URL)
        status = self._status(
            caps.EKS_CAPABILITIES_DEFAULTS, [foreign, {"capabilityName": "odd", "type": "KRO"}]
        )
        assert status["capabilities"][0]["deployed"] is False
        assert status["unmanaged"] == [
            {"capability_name": "odd", "type": "kro", "status": None, "arn": None},
            {
                "capability_name": "team-argocd",
                "type": "argocd",
                "status": "ACTIVE",
                "arn": foreign["arn"],
            },
        ]
        # A stranger's capability is visible but not drift.
        assert status["healthy"] is True

    def test_missing_cluster_is_unhealthy_but_still_reports_intent(self) -> None:
        status = self._status(_argocd_config(), [], cluster_found=False)
        assert status["cluster_found"] is False
        assert status["healthy"] is False
        assert status["capabilities"][0]["configured"] is True


class TestCapabilitiesStatus:
    def test_merges_config_with_the_described_cluster(self) -> None:
        argo = _live("argocd", server_url=_SERVER_URL)
        client = _FakeEks(
            pages=[[{"capabilityName": argo["capabilityName"]}]],
            details={argo["capabilityName"]: argo},
        )
        status = eks_capabilities.capabilities_status(
            _REGION, _PROJECT, config=_argocd_config(), eks_client=client
        )
        assert status["healthy"] is True
        assert status["capabilities"][0]["argocd_server_url"] == _SERVER_URL

    def test_missing_cluster_reads_as_not_found(self) -> None:
        client = _FakeEks(pages=[], details={})
        client.list_error = _client_error("ResourceNotFoundException", "ListCapabilities")
        status = eks_capabilities.capabilities_status(
            _REGION, _PROJECT, config=caps.EKS_CAPABILITIES_DEFAULTS, eks_client=client
        )
        assert status["cluster_found"] is False
        assert all(row["deployed"] is False for row in status["capabilities"])

    def test_other_aws_errors_propagate(self) -> None:
        client = _FakeEks(pages=[], details={})
        client.list_error = _client_error("AccessDeniedException", "ListCapabilities")
        with pytest.raises(ClientError):
            eks_capabilities.capabilities_status(
                _REGION, _PROJECT, config=caps.EKS_CAPABILITIES_DEFAULTS, eks_client=client
            )

    def test_builds_a_boto3_client_and_loads_cdk_json_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "cdk.json").write_text(json.dumps({"context": {}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        client = _FakeEks(pages=[[]], details={})
        with patch("boto3.client", return_value=client) as boto_client:
            status = eks_capabilities.capabilities_status(_REGION, _PROJECT)
        boto_client.assert_called_once_with("eks", region_name=_REGION)
        assert status["healthy"] is True


# ─── cli/argocd_ui.py ────────────────────────────────────────────────────────


class TestArgoCdUrls:
    def test_applications_url(self) -> None:
        assert argocd_ui.applications_url(_SERVER_URL + "/") == _SERVER_URL + "/applications"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (_SERVER_URL + "/applications", True),
            (_SERVER_URL + "/applications/", True),
            (_SERVER_URL + "/applications?proj=gco-tenants", True),
            (_SERVER_URL + "/applications/argocd/gco-gitops-root", True),
            (_SERVER_URL + "/login?return_url=%2Fapplications", False),
            (_SERVER_URL + "/", False),
            ("https://d-1234567890.awsapps.com/start/applications", False),
        ],
    )
    def test_is_applications_url(self, url: str, expected: bool) -> None:
        assert argocd_ui.is_applications_url(url, _SERVER_URL) is expected

    def test_default_profile_dir_is_per_region_under_gco_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GCO_ARGOCD_BROWSER_PROFILE_DIR", raising=False)
        assert (
            argocd_ui.default_profile_dir(_REGION)
            == Path.home() / ".gco" / "argocd-browser" / _REGION
        )
        monkeypatch.setenv("GCO_ARGOCD_BROWSER_PROFILE_DIR", "/tmp/profiles")
        assert argocd_ui.default_profile_dir("eu-west-1") == Path("/tmp/profiles/eu-west-1")


class TestResolveArgoCdServerUrl:
    def _client(self, detail: Any) -> _FakeEks:
        return _FakeEks(pages=[], details={f"{_PROJECT}-argocd": detail})

    def test_returns_the_published_server_url(self) -> None:
        client = self._client(_live("argocd", server_url=f" {_SERVER_URL} "))
        assert (
            argocd_ui.resolve_argocd_server_url(_REGION, _PROJECT, eks_client=client) == _SERVER_URL
        )
        assert client.described == [f"{_PROJECT}-argocd"]

    def test_missing_capability_names_the_remedy(self) -> None:
        client = self._client(_client_error("ResourceNotFoundException"))
        with pytest.raises(RuntimeError, match="gco stacks capabilities status") as excinfo:
            argocd_ui.resolve_argocd_server_url(_REGION, _PROJECT, eks_client=client)
        assert "eks_capabilities.argocd" in str(excinfo.value)

    def test_not_active_yet(self) -> None:
        client = self._client(_live("argocd", status="CREATING", server_url=_SERVER_URL))
        with pytest.raises(RuntimeError, match="is CREATING, not ACTIVE"):
            argocd_ui.resolve_argocd_server_url(_REGION, _PROJECT, eks_client=client)

    def test_active_without_a_url_yet(self) -> None:
        client = self._client(_live("argocd"))
        with pytest.raises(RuntimeError, match="no server URL yet"):
            argocd_ui.resolve_argocd_server_url(_REGION, _PROJECT, eks_client=client)

    def test_other_aws_errors_propagate(self) -> None:
        client = self._client(_client_error("AccessDeniedException"))
        with pytest.raises(ClientError):
            argocd_ui.resolve_argocd_server_url(_REGION, _PROJECT, eks_client=client)

    def test_builds_a_boto3_client_by_default(self) -> None:
        client = self._client(_live("argocd", server_url=_SERVER_URL))
        with patch("boto3.client", return_value=client) as boto_client:
            assert argocd_ui.resolve_argocd_server_url(_REGION, _PROJECT) == _SERVER_URL
        boto_client.assert_called_once_with("eks", region_name=_REGION)


# ─── Fake Playwright ─────────────────────────────────────────────────────────


@dataclass
class _BrowserSession:
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    closed: bool = False
    #: The login page is showing (an SSO button exists).
    login_page: bool = False
    #: URL the "browser" ends up on after navigation / sign-in.
    landing_url: str = _SERVER_URL + "/applications"
    #: When set, ``wait_for_url`` raises the fake Playwright TimeoutError.
    never_lands: bool = False
    existing_pages: int = 0


class _FakeTimeoutError(Exception):
    pass


class _FakeLocator:
    def __init__(self, session: _BrowserSession) -> None:
        self._session = session

    def count(self) -> int:
        return 1 if self._session.login_page else 0

    @property
    def first(self) -> _FakeLocator:
        return self

    def click(self, timeout: int | None = None) -> None:
        self._session.calls.append(("click_sso", timeout))


class _FakePage:
    def __init__(self, session: _BrowserSession) -> None:
        self._session = session

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self._session.calls.append(("goto", url, wait_until))

    def get_by_role(self, role: str, name: Any = None) -> _FakeLocator:
        self._session.calls.append(("get_by_role", role, getattr(name, "pattern", name)))
        return _FakeLocator(self._session)

    def wait_for_url(self, predicate: Any, timeout: int | None = None) -> None:
        self._session.calls.append(("wait_for_url", timeout))
        if self._session.never_lands or not predicate(self._session.landing_url):
            raise _FakeTimeoutError("Timeout exceeded")

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self._session.calls.append(("wait_for_timeout", timeout_ms))

    def screenshot(self, path: str, full_page: bool = False) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + Path(path).name.encode())
        self._session.calls.append(("screenshot", path, full_page))


class _FakeContext:
    def __init__(self, session: _BrowserSession) -> None:
        self._session = session
        self.pages = [_FakePage(session) for _ in range(session.existing_pages)]

    def new_page(self) -> _FakePage:
        self._session.calls.append(("new_page",))
        return _FakePage(self._session)

    def close(self) -> None:
        self._session.closed = True
        self._session.calls.append(("close",))


class _FakeChromium:
    def __init__(self, session: _BrowserSession) -> None:
        self._session = session

    def launch_persistent_context(self, user_data_dir: str, **kwargs: Any) -> _FakeContext:
        self._session.calls.append(("launch_persistent_context", user_data_dir, kwargs))
        return _FakeContext(self._session)


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> _BrowserSession:
    session = _BrowserSession()

    @contextmanager
    def sync_playwright() -> Any:
        session.calls.append(("sync_playwright.enter",))
        try:
            yield types.SimpleNamespace(chromium=_FakeChromium(session))
        finally:
            session.calls.append(("sync_playwright.exit",))

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.__dict__["sync_playwright"] = sync_playwright
    fake_module.__dict__["TimeoutError"] = _FakeTimeoutError
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    return session


def _names(session: _BrowserSession) -> list[str]:
    return [call[0] for call in session.calls]


class TestCaptureArgoCdScreenshot:
    def test_signed_in_profile_captures_the_applications_view(
        self, browser: _BrowserSession, tmp_path: Path
    ) -> None:
        output = tmp_path / "images" / "argocd-ui.png"
        profile = tmp_path / "profile" / _REGION

        written = argocd_ui.capture_argocd_screenshot(
            _SERVER_URL, output, profile_dir=profile, headless=True, login_timeout_seconds=7
        )

        assert written == output and output.exists()
        assert profile.is_dir()
        launch = next(call for call in browser.calls if call[0] == "launch_persistent_context")
        assert launch[1] == str(profile)
        assert launch[2] == {
            "headless": True,
            "viewport": {"width": argocd_ui.VIEWPORT_WIDTH, "height": argocd_ui.VIEWPORT_HEIGHT},
        }
        assert ("goto", _SERVER_URL + "/applications", "load") in browser.calls
        assert ("wait_for_url", 7000) in browser.calls
        assert ("wait_for_timeout", argocd_ui._RENDER_WAIT_MS) in browser.calls
        assert ("screenshot", str(output), True) in browser.calls
        # No login page -> no click; the context is always closed.
        assert "click_sso" not in _names(browser)
        assert browser.closed is True
        assert _names(browser)[-1] == "sync_playwright.exit"

    def test_reuses_the_first_existing_page(self, browser: _BrowserSession, tmp_path: Path) -> None:
        browser.existing_pages = 1
        argocd_ui.capture_argocd_screenshot(
            _SERVER_URL, tmp_path / "out.png", profile_dir=tmp_path / "p"
        )
        assert "new_page" not in _names(browser)

    def test_login_page_gets_the_sso_button_pressed_then_waits_for_sign_in(
        self, browser: _BrowserSession, tmp_path: Path
    ) -> None:
        browser.login_page = True
        argocd_ui.capture_argocd_screenshot(
            _SERVER_URL, tmp_path / "out.png", profile_dir=tmp_path / "p", headless=False
        )
        names = _names(browser)
        assert names.index("click_sso") < names.index("wait_for_url")
        role_call = next(call for call in browser.calls if call[0] == "get_by_role")
        assert role_call[1] == "button"
        assert argocd_ui._SSO_BUTTON_PATTERN.search("LOG IN VIA AWS IAM IDENTITY CENTER")
        launch = next(call for call in browser.calls if call[0] == "launch_persistent_context")
        assert launch[2]["headless"] is False
        assert ("wait_for_url", argocd_ui.DEFAULT_LOGIN_TIMEOUT_SECONDS * 1000) in browser.calls

    def test_headless_without_a_session_explains_how_to_sign_in(
        self, browser: _BrowserSession, tmp_path: Path
    ) -> None:
        browser.never_lands = True
        with pytest.raises(RuntimeError, match="rerun without --headless and sign in once"):
            argocd_ui.capture_argocd_screenshot(
                _SERVER_URL,
                tmp_path / "out.png",
                profile_dir=tmp_path / "p",
                headless=True,
                login_timeout_seconds=1,
            )
        assert browser.closed is True
        assert not (tmp_path / "out.png").exists()

    def test_headed_timeout_says_sign_in_did_not_complete(
        self, browser: _BrowserSession, tmp_path: Path
    ) -> None:
        browser.landing_url = _SERVER_URL + "/login?return_url=%2Fapplications"
        with pytest.raises(RuntimeError, match="sign-in did not complete in time"):
            argocd_ui.capture_argocd_screenshot(
                _SERVER_URL,
                tmp_path / "out.png",
                profile_dir=tmp_path / "p",
                login_timeout_seconds=2,
            )
        assert browser.closed is True

    def test_missing_playwright_names_the_install_extra(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        with pytest.raises(
            RuntimeError, match=r"pip install 'gco\[diagrams\]' && playwright install chromium"
        ):
            argocd_ui.capture_argocd_screenshot(
                _SERVER_URL, tmp_path / "out.png", profile_dir=tmp_path / "p"
            )


# ─── Click commands ──────────────────────────────────────────────────────────


def _healthy_status(**overrides: Any) -> dict[str, Any]:
    status = eks_capabilities.build_status(
        region=_REGION,
        project_name=_PROJECT,
        config=_argocd_config(gitops=True),
        live=[_live("argocd", server_url=_SERVER_URL)],
    )
    status.update(overrides)
    return status


class TestStatusCommand:
    def _invoke(
        self, args: list[str], statuses: list[Any], *, config_error: Exception | None = None
    ) -> Any:
        loaded = MagicMock(return_value=caps.EKS_CAPABILITIES_DEFAULTS)
        if config_error is not None:
            loaded.side_effect = config_error
        with (
            patch("cli.eks_capabilities.load_eks_capabilities_config", loaded),
            patch("cli.eks_capabilities.capabilities_status", side_effect=statuses) as status_fn,
            patch("cli.commands.stacks_cmd._project_name", return_value=_PROJECT),
            patch(
                "cli.commands.stacks_cmd._load_cdk_json",
                return_value={"regional": [_REGION, "us-west-2"]},
            ),
        ):
            result = CliRunner().invoke(cli, args)
        return result, status_fn

    def test_table_mode_prints_one_row_per_type_and_the_ui_hint(self) -> None:
        result, status_fn = self._invoke(["stacks", "capabilities", "status"], [_healthy_status()])
        assert result.exit_code == 0, result.output
        assert status_fn.call_args.args == (_REGION, _PROJECT)
        assert "TYPE" in result.output and "CONFIGURED" in result.output
        for type_name in caps.EKS_CAPABILITY_TYPES:
            assert type_name in result.output
        assert _SERVER_URL in result.output
        assert "gco stacks capabilities argocd open" in result.output

    def test_json_mode_emits_the_document_once(self) -> None:
        result, _ = self._invoke(
            ["--output", "json", "stacks", "capabilities", "status", "-r", _REGION],
            [_healthy_status()],
        )
        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert document["region"] == _REGION
        assert document["healthy"] is True
        assert [row["type"] for row in document["capabilities"]] == ["argocd", "ack", "kro"]
        assert document["capabilities"][0]["gitops"]["enabled"] is True

    def test_all_regions_json_is_a_list_and_drift_exits_nonzero(self) -> None:
        drifted = eks_capabilities.build_status(
            region="us-west-2", project_name=_PROJECT, config=_argocd_config(), live=[]
        )
        result, status_fn = self._invoke(
            ["--output", "json", "stacks", "capabilities", "status", "--all-regions"],
            [_healthy_status(), drifted],
        )
        assert result.exit_code == 1
        documents = json.loads(result.stdout)
        assert [doc["region"] for doc in documents] == [_REGION, "us-west-2"]
        assert documents[1]["healthy"] is False
        assert "configured in cdk.json but not attached" in result.stderr
        assert [call.args[0] for call in status_fn.call_args_list] == [_REGION, "us-west-2"]

    def test_describe_failure_is_reported_and_exits_nonzero(self) -> None:
        result, _ = self._invoke(
            ["stacks", "capabilities", "status"], [RuntimeError("eks unavailable")]
        )
        assert result.exit_code == 1
        assert "eks unavailable" in result.output

    def test_invalid_cdk_json_block_exits_before_any_aws_call(self) -> None:
        result, status_fn = self._invoke(
            ["stacks", "capabilities", "status"],
            [],
            config_error=caps.EksCapabilitiesConfigError(
                "eks_capabilities.kro.enabled must be a boolean"
            ),
        )
        assert result.exit_code == 1
        assert "eks_capabilities.kro.enabled must be a boolean" in result.output
        status_fn.assert_not_called()

    def test_missing_cluster_and_unmanaged_capabilities_are_warned(self) -> None:
        status = _healthy_status()
        status["cluster_found"] = False
        status["healthy"] = False
        status["unmanaged"] = [
            {"capability_name": "team-argocd", "type": "argocd", "status": "ACTIVE", "arn": None}
        ]
        result, _ = self._invoke(["stacks", "capabilities", "status"], [status])
        assert result.exit_code == 1
        assert "is not deployed" in result.output
        assert "team-argocd" in result.output


class TestArgoCdOpenCommand:
    def _invoke(self, args: list[str], resolver: Any) -> Any:
        with (
            patch("cli.argocd_ui.resolve_argocd_server_url", resolver),
            patch("cli.commands.stacks_cmd._project_name", return_value=_PROJECT),
            patch("cli.commands.stacks_cmd._load_cdk_json", return_value={"regional": [_REGION]}),
            patch("cli.commands.stacks_cmd.click.launch", return_value=0) as launch,
        ):
            result = CliRunner().invoke(cli, args)
        return result, launch

    def test_print_url_never_launches_a_browser(self) -> None:
        resolver = MagicMock(return_value=_SERVER_URL)
        result, launch = self._invoke(
            ["stacks", "capabilities", "argocd", "open", "--print-url"], resolver
        )
        assert result.exit_code == 0, result.output
        assert _SERVER_URL in result.output
        resolver.assert_called_once_with(_REGION, _PROJECT)
        launch.assert_not_called()

    def test_json_mode_is_a_document_without_a_browser(self) -> None:
        result, launch = self._invoke(
            ["--output", "json", "stacks", "capabilities", "argocd", "open", "-r", _REGION],
            MagicMock(return_value=_SERVER_URL),
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {"region": _REGION, "argocd_server_url": _SERVER_URL}
        launch.assert_not_called()

    def test_default_launches_the_browser(self) -> None:
        result, launch = self._invoke(
            ["stacks", "capabilities", "argocd", "open"], MagicMock(return_value=_SERVER_URL)
        )
        assert result.exit_code == 0, result.output
        launch.assert_called_once_with(_SERVER_URL)
        assert "Identity Center" in result.output

    def test_resolution_failure_exits_nonzero(self) -> None:
        result, launch = self._invoke(
            ["stacks", "capabilities", "argocd", "open"],
            MagicMock(side_effect=RuntimeError("No Argo CD capability 'gco-argocd' is attached")),
        )
        assert result.exit_code == 1
        assert "No Argo CD capability" in result.output
        launch.assert_not_called()


class TestArgoCdScreenshotCommand:
    def _invoke(self, args: list[str], capture: Any, resolver: Any | None = None) -> Any:
        resolver = resolver or MagicMock(return_value=_SERVER_URL)
        with (
            patch("cli.argocd_ui.resolve_argocd_server_url", resolver),
            patch("cli.argocd_ui.capture_argocd_screenshot", capture),
            patch("cli.commands.stacks_cmd._project_name", return_value=_PROJECT),
            patch("cli.commands.stacks_cmd._load_cdk_json", return_value={"regional": [_REGION]}),
        ):
            result = CliRunner().invoke(cli, args)
        return result

    def test_defaults_headed_capture_into_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCO_ARGOCD_BROWSER_PROFILE_DIR", raising=False)
        capture = MagicMock(side_effect=lambda url, output, **kwargs: Path(output))
        result = self._invoke(["stacks", "capabilities", "argocd", "screenshot"], capture)
        assert result.exit_code == 0, result.output
        capture.assert_called_once_with(
            _SERVER_URL,
            tmp_path / argocd_ui.DEFAULT_SCREENSHOT_FILENAME,
            profile_dir=argocd_ui.default_profile_dir(_REGION),
            headless=False,
            login_timeout_seconds=argocd_ui.DEFAULT_LOGIN_TIMEOUT_SECONDS,
        )
        assert "sign in with IAM Identity Center" in result.output
        assert str(tmp_path / argocd_ui.DEFAULT_SCREENSHOT_FILENAME) in result.output

    def test_explicit_options_and_json_document(self, tmp_path: Path) -> None:
        capture = MagicMock(side_effect=lambda url, output, **kwargs: Path(output))
        result = self._invoke(
            [
                "--output",
                "json",
                "stacks",
                "capabilities",
                "argocd",
                "screenshot",
                "-r",
                _REGION,
                "-o",
                str(tmp_path / "images" / "argocd-ui.png"),
                "--headless",
                "--login-timeout",
                "30",
                "--profile-dir",
                str(tmp_path / "profile"),
            ],
            capture,
        )
        assert result.exit_code == 0, result.output
        capture.assert_called_once_with(
            _SERVER_URL,
            tmp_path / "images" / "argocd-ui.png",
            profile_dir=tmp_path / "profile",
            headless=True,
            login_timeout_seconds=30,
        )
        assert json.loads(result.stdout) == {
            "region": _REGION,
            "argocd_server_url": _SERVER_URL,
            "screenshot": str(tmp_path / "images" / "argocd-ui.png"),
            "profile_dir": str(tmp_path / "profile"),
            "headless": True,
        }

    def test_capture_failure_exits_nonzero(self) -> None:
        result = self._invoke(
            ["stacks", "capabilities", "argocd", "screenshot"],
            MagicMock(side_effect=RuntimeError("Playwright is not installed")),
        )
        assert result.exit_code == 1
        assert "Playwright is not installed" in result.output


# ─── Import posture ──────────────────────────────────────────────────────────


def test_cli_capabilities_modules_import_without_aws_cdk() -> None:
    """The CLI reads the eks_capabilities block through a CDK-free module."""
    snippet = (
        "import sys\n"
        "import cli.eks_capabilities, cli.argocd_ui, gco.eks_capabilities_config\n"
        "assert 'aws_cdk' not in sys.modules, 'aws_cdk was imported'\n"
        "assert 'gco.config' not in sys.modules, 'gco.config was imported'\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
