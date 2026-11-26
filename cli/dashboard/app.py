"""The GCO dashboard's Streamlit entrypoint.

Started by ``gco dashboard serve`` (directly, in the dev container, or as the
CMD of the dashboard image); see docs/DASHBOARD.md. This script sets up the
page, admits the session, lays out the sidebar and hands over to the page the
navigation picked. Each page is a two-line file under ``app_pages/`` that
renders a view from :mod:`cli.dashboard.views`.
"""

from __future__ import annotations

import streamlit as st

from cli.dashboard import brand, ui

st.set_page_config(
    page_title="GCO dashboard",
    page_icon=brand.ICON_SVG,
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Get help": "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/docs/DASHBOARD.md",
        "Report a bug": "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/issues",
        "About": "Global Capacity Orchestrator on AWS: one API, every accelerator, any Region.",
    },
)

if ui.require_access():
    ui.show_flash()
    st.logo(brand.ICON_SVG, size="large", icon_image=brand.ICON_SVG)
    navigation = st.navigation(
        [
            st.Page("app_pages/overview.py", title="Fleet", icon=":material/public:", default=True),
            st.Page("app_pages/jobs.py", title="Jobs", icon=":material/work:", url_path="jobs"),
            st.Page("app_pages/queue.py", title="Queue", icon=":material/queue:", url_path="queue"),
            st.Page(
                "app_pages/inference.py",
                title="Inference",
                icon=":material/smart_toy:",
                url_path="inference",
            ),
            st.Page(
                "app_pages/capacity.py",
                title="Capacity",
                icon=":material/memory:",
                url_path="capacity",
            ),
            st.Page(
                "app_pages/costs.py", title="Costs", icon=":material/payments:", url_path="costs"
            ),
            st.Page(
                "app_pages/platform.py",
                title="Platform",
                icon=":material/dns:",
                url_path="platform",
            ),
            st.Page(
                "app_pages/console.py",
                title="Console",
                icon=":material/terminal:",
                url_path="console",
            ),
        ],
        position="top",
    )
    ui.sidebar(ui.load_topology())
    navigation.run()
