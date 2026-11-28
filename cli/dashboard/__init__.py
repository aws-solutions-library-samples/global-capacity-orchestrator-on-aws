"""The GCO dashboard: a local Streamlit web UI over the same calls the ``gco`` commands make.

``gco dashboard`` starts it (``cli/commands/dashboard_cmd.py``); see
docs/DASHBOARD.md. The modules split by concern so each is testable alone:
``data`` makes every AWS and API call (no Streamlit), ``frames`` shapes the
answers into tables, ``charts`` draws them, ``ui`` caches reads and holds
the shared widgets, ``views`` lays out one page each, and ``server`` /
``container`` start the server in this process or in its own container.
Nothing here imports Streamlit at package import time, so the ``gco`` CLI
loads ``server`` and ``container`` without the optional extra.
"""
