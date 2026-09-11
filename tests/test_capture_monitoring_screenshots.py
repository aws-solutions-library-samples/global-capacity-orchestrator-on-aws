"""Browser-free tests for ``scripts/capture_monitoring_screenshots.py``.

Covers the two Playwright-driven capture functions (``capture`` for the
curated Grafana dashboards and ``capture_opencost_ui`` for the native OpenCost
SPA) plus the CLI wiring in ``main``. ``playwright.sync_api`` is replaced in
``sys.modules`` by an in-memory fake, so the tests pin what the script asks
of the browser (one headless Chromium launch, a 1600x900 context carrying a
proactive HTTP basic-auth header, kiosk-mode dashboard URLs with optional
``from``/``to`` overrides, a fixed panel-render wait, full-page screenshots,
and ``browser.close()`` even when a navigation fails) and what lands on disk
(one PNG per dashboard under the output directory, created on demand) without
launching a browser or contacting Grafana. The dashboard-lockstep and
docs-reference invariants live in ``tests/test_cluster_observability_screenshots.py``.
"""

from __future__ import annotations

import base64
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scripts import capture_monitoring_screenshots as screenshots

# ---------------------------------------------------------------------------
# Fake Playwright
# ---------------------------------------------------------------------------


@dataclass
class _FakeBrowserSession:
    """Everything the script did to the browser, in order."""

    calls: list[tuple[Any, ...]] = field(default_factory=list)
    closed: bool = False
    #: When set, ``page.goto`` raises it to simulate a navigation failure.
    goto_error: Exception | None = None


class _FakePage:
    def __init__(self, session: _FakeBrowserSession) -> None:
        self._session = session

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self._session.calls.append(("goto", url, wait_until))
        if self._session.goto_error is not None:
            raise self._session.goto_error

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self._session.calls.append(("wait_for_timeout", timeout_ms))

    def screenshot(self, path: str, full_page: bool = False) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + Path(path).name.encode())
        self._session.calls.append(("screenshot", path, full_page))


class _FakeContext:
    def __init__(self, session: _FakeBrowserSession) -> None:
        self._session = session

    def new_page(self) -> _FakePage:
        self._session.calls.append(("new_page",))
        return _FakePage(self._session)


class _FakeBrowser:
    def __init__(self, session: _FakeBrowserSession) -> None:
        self._session = session

    def new_context(self, **kwargs: Any) -> _FakeContext:
        self._session.calls.append(("new_context", kwargs))
        return _FakeContext(self._session)

    def close(self) -> None:
        self._session.closed = True
        self._session.calls.append(("close",))


class _FakeChromium:
    def __init__(self, session: _FakeBrowserSession) -> None:
        self._session = session

    def launch(self, headless: bool = False) -> _FakeBrowser:
        self._session.calls.append(("launch", headless))
        return _FakeBrowser(self._session)


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> _FakeBrowserSession:
    """Install a fake ``playwright.sync_api`` and hand back its recorder."""
    session = _FakeBrowserSession()

    @contextmanager
    def sync_playwright() -> Any:
        session.calls.append(("sync_playwright.enter",))
        try:
            yield types.SimpleNamespace(chromium=_FakeChromium(session))
        finally:
            session.calls.append(("sync_playwright.exit",))

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.__dict__["sync_playwright"] = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    return session


def _urls(session: _FakeBrowserSession) -> list[str]:
    return [call[1] for call in session.calls if call[0] == "goto"]


# ---------------------------------------------------------------------------
# capture()
# ---------------------------------------------------------------------------


def test_capture_screenshots_every_dashboard_in_kiosk_mode(
    browser: _FakeBrowserSession, tmp_path: Path
) -> None:
    output_dir = tmp_path / "images"

    written = screenshots.capture("http://localhost:3000/", "admin", "s3cr3t", output_dir)

    assert written == [output_dir / shot.filename for shot in screenshots.SCREENSHOTS]
    assert sorted(path.name for path in output_dir.iterdir()) == sorted(
        shot.filename for shot in screenshots.SCREENSHOTS
    )
    for path in written:
        assert path.read_bytes().startswith(b"\x89PNG")

    # Trailing slash stripped, kiosk mode, one navigation per dashboard uid.
    assert _urls(browser) == [
        f"http://localhost:3000/d/{shot.dashboard_uid}?kiosk" for shot in screenshots.SCREENSHOTS
    ]

    launch, context = browser.calls[1], browser.calls[2]
    assert launch == ("launch", True)
    assert context == (
        "new_context",
        {
            "viewport": {"width": 1600, "height": 900},
            "extra_http_headers": {
                "Authorization": "Basic " + base64.b64encode(b"admin:s3cr3t").decode()
            },
        },
    )
    assert browser.calls[3] == ("new_page",)

    # Per dashboard: goto(load) -> fixed render wait -> full-page screenshot.
    per_dashboard = browser.calls[4:-2]
    assert len(per_dashboard) == 3 * len(screenshots.SCREENSHOTS)
    for index, shot in enumerate(screenshots.SCREENSHOTS):
        goto, wait, shot_call = per_dashboard[3 * index : 3 * index + 3]
        assert goto == ("goto", f"http://localhost:3000/d/{shot.dashboard_uid}?kiosk", "load")
        assert wait == ("wait_for_timeout", screenshots._PANEL_RENDER_WAIT_MS)
        assert shot_call == ("screenshot", str(output_dir / shot.filename), True)

    assert browser.calls[-2:] == [("close",), ("sync_playwright.exit",)]
    assert browser.closed is True


@pytest.mark.parametrize(
    ("time_from", "time_to", "query"),
    [
        ("now-30m", "now", "?kiosk&from=now-30m&to=now"),
        ("now-30m", None, "?kiosk&from=now-30m"),
        (None, "now", "?kiosk&to=now"),
        (None, None, "?kiosk"),
    ],
)
def test_capture_overrides_the_saved_time_range_only_when_asked(
    browser: _FakeBrowserSession,
    tmp_path: Path,
    time_from: str | None,
    time_to: str | None,
    query: str,
) -> None:
    screenshots.capture(
        "http://grafana.internal:3000",
        "viewer",
        "pw",
        tmp_path,
        time_from=time_from,
        time_to=time_to,
    )
    assert _urls(browser) == [
        f"http://grafana.internal:3000/d/{shot.dashboard_uid}{query}"
        for shot in screenshots.SCREENSHOTS
    ]


def test_capture_closes_the_browser_when_a_navigation_fails(
    browser: _FakeBrowserSession, tmp_path: Path
) -> None:
    browser.goto_error = TimeoutError("net::ERR_CONNECTION_REFUSED at http://localhost:3000")
    output_dir = tmp_path / "images"

    with pytest.raises(TimeoutError, match="ERR_CONNECTION_REFUSED"):
        screenshots.capture("http://localhost:3000", "admin", "pw", output_dir)

    assert browser.closed is True
    assert browser.calls[-2:] == [("close",), ("sync_playwright.exit",)]
    # The failure happened on the first dashboard: nothing was screenshotted,
    # but the output directory was already prepared.
    assert len(_urls(browser)) == 1
    assert output_dir.is_dir()
    assert list(output_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# capture_opencost_ui()
# ---------------------------------------------------------------------------


def test_capture_opencost_ui_screenshots_the_unauthenticated_spa(
    browser: _FakeBrowserSession, tmp_path: Path
) -> None:
    output_dir = tmp_path / "nested" / "images"

    out = screenshots.capture_opencost_ui("http://localhost:9091/", output_dir)

    assert out == output_dir / screenshots.OPENCOST_UI_FILENAME
    assert out.read_bytes().startswith(b"\x89PNG")
    assert [path.name for path in output_dir.iterdir()] == [screenshots.OPENCOST_UI_FILENAME]
    assert browser.calls == [
        ("sync_playwright.enter",),
        ("launch", True),
        # No Authorization header: the UI sits behind the port-forward.
        ("new_context", {"viewport": {"width": 1600, "height": 900}}),
        ("new_page",),
        ("goto", "http://localhost:9091", "load"),
        ("wait_for_timeout", screenshots._PANEL_RENDER_WAIT_MS),
        ("screenshot", str(out), True),
        ("close",),
        ("sync_playwright.exit",),
    ]


def test_capture_opencost_ui_closes_the_browser_when_navigation_fails(
    browser: _FakeBrowserSession, tmp_path: Path
) -> None:
    browser.goto_error = RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(RuntimeError, match="has been closed"):
        screenshots.capture_opencost_ui("http://localhost:9091", tmp_path)

    assert browser.closed is True
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_forwards_every_option_to_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake_capture(
        grafana_url: str,
        username: str,
        password: str,
        output_dir: Path,
        time_from: str | None = None,
        time_to: str | None = None,
    ) -> list[Path]:
        seen.update(
            grafana_url=grafana_url,
            username=username,
            password=password,
            output_dir=output_dir,
            time_from=time_from,
            time_to=time_to,
        )
        return [output_dir / "grafana-gpu-dcgm.png", output_dir / "grafana-cost.png"]

    monkeypatch.setattr(screenshots, "capture", fake_capture)

    rc = screenshots.main(
        [
            "--grafana-url",
            "http://grafana.internal:3000",
            "--username",
            "viewer",
            "--password",
            "pw",
            "--output-dir",
            str(tmp_path),
            "--from",
            "now-30m",
            "--to",
            "now",
        ]
    )

    assert rc == 0
    assert seen == {
        "grafana_url": "http://grafana.internal:3000",
        "username": "viewer",
        "password": "pw",
        "output_dir": tmp_path,
        "time_from": "now-30m",
        "time_to": "now",
    }
    assert capsys.readouterr().out == (
        f"wrote {tmp_path / 'grafana-gpu-dcgm.png'}\nwrote {tmp_path / 'grafana-cost.png'}\n"
    )


def test_main_defaults_target_the_local_port_forward_and_repo_images_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_capture(
        grafana_url: str, username: str, password: str, output_dir: Path, **kw: Any
    ) -> list[Path]:
        seen.update(grafana_url=grafana_url, username=username, output_dir=output_dir, **kw)
        return []

    monkeypatch.setattr(screenshots, "capture", fake_capture)
    assert screenshots.main(["--password", "pw"]) == 0
    assert seen == {
        "grafana_url": "http://localhost:3000",
        "username": "admin",
        "output_dir": screenshots.IMAGES_DIR,
        "time_from": None,
        "time_to": None,
    }


def test_main_requires_a_password(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        screenshots.main([])
    assert excinfo.value.code == 2
    assert "--password" in capsys.readouterr().err


def test_main_end_to_end_with_the_fake_browser(
    browser: _FakeBrowserSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = screenshots.main(
        [
            "--password",
            "pw",
            "--output-dir",
            str(tmp_path),
            "--opencost-url",
            "http://localhost:9091",
        ]
    )

    assert rc == 0
    expected = [tmp_path / shot.filename for shot in screenshots.SCREENSHOTS] + [
        tmp_path / screenshots.OPENCOST_UI_FILENAME
    ]
    assert capsys.readouterr().out == "".join(f"wrote {path}\n" for path in expected)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(p.name for p in expected)
    # Two independent browser sessions: one for Grafana, one for OpenCost.
    assert browser.calls.count(("launch", True)) == 2
    assert browser.calls.count(("close",)) == 2


def test_main_reports_opencost_failures_after_grafana_succeeded(
    browser: _FakeBrowserSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_opencost(url: str, output_dir: Path) -> Path:
        raise ConnectionError(f"could not reach {url}")

    monkeypatch.setattr(screenshots, "capture_opencost_ui", fail_opencost)

    rc = screenshots.main(
        ["--password", "pw", "--output-dir", str(tmp_path), "--opencost-url", "http://x:9091"]
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "screenshot capture failed: could not reach http://x:9091\n"
    # The Grafana dashboards were still written before the failure surfaced.
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        shot.filename for shot in screenshots.SCREENSHOTS
    )
