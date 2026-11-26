"""Every dashboard page renders against the in-memory fleet without an exception."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from tests._dashboard_fakes import APP, FakeFleet, fake_fleet_fixture  # noqa: F401

PAGES = (
    "app_pages/overview.py",
    "app_pages/jobs.py",
    "app_pages/queue.py",
    "app_pages/inference.py",
    "app_pages/capacity.py",
    "app_pages/costs.py",
    "app_pages/platform.py",
    "app_pages/console.py",
)


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(fake_fleet: FakeFleet, page: str) -> None:
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    app.switch_page(page)
    app.run()
    assert not app.exception, [e.message for e in app.exception]
    assert not app.error, [e.value for e in app.error]
