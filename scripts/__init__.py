"""Operational scripts and validation harnesses.

A regular package (not a namespace package) so the harness packages inside it
(``scripts.live_release_validation``, ``scripts.example_job_validation``)
resolve under one canonical module name everywhere — ``python -m``, tests,
and mypy's directory scan all agree. Standalone utility scripts in this
directory are still runnable directly (``python scripts/<name>.py``).
"""
