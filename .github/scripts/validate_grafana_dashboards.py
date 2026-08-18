#!/usr/bin/env python3
"""Validate the curated Grafana dashboards against the Grafana we actually ship.

The GCO dashboards live as JSON payloads inside ConfigMap manifests under
``lambda/kubectl-applier-simple/manifests/`` and are imported at runtime by
the kube-prometheus-stack Grafana sidecar (label ``grafana_dashboard="1"``).
Nothing in that pipeline rejects a malformed or schema-incompatible dashboard
loudly — it just fails to appear in the UI. This script closes that gap in CI
by provisioning the extracted dashboards into the exact Grafana image the
pinned chart version bundles and asserting each one loads.

Subcommands:

``extract``
    Pull every dashboard JSON out of the given ConfigMap manifests, resolving
    ``{{UPPER_SNAKE}}`` feature placeholders first (any value works — only
    resolution matters here; the applier's substitution fidelity is covered by
    ``tests/test_grafana_dashboards.py`` through the real handler). Writes
    ``dashboards/<uid>.json`` plus a file-provisioning provider under
    ``provisioning/`` shaped exactly like the sidecar's load path.

``chart-version``
    Print the pinned ``kube-prometheus-stack`` chart version and repo URL from
    ``lambda/helm-installer/charts.yaml``, so the workflow resolves the
    Grafana image from the same pin the deployment uses (no second pin to
    drift). Appends ``version=``/``repo_url=`` lines to ``$GITHUB_OUTPUT``
    when set.

``verify``
    Wait for a running Grafana's ``/api/health``, then assert every extracted
    dashboard round-trips: ``GET /api/dashboards/uid/<uid>`` answers 200, the
    title matches the source, and ``meta.provisioned`` is true.

Importable (``extract_dashboards()``, ``read_chart_pin()``, ``verify()``) so
pytest can hold the extraction in lockstep with the applier-path tests.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

# Matches the applier's feature-gate regex: deliberately UPPER_SNAKE only, so
# Grafana's own lowercase legend tokens ({{gpu}}, {{namespace}}, {{Hostname}})
# pass through untouched.
_FEATURE_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")

_SIDECAR_LABEL = "grafana_dashboard"

_PROVIDER_YAML = """\
apiVersion: 1
providers:
  - name: gco-dashboards
    type: file
    updateIntervalSeconds: 10
    options:
      path: /var/lib/grafana/dashboards
"""


class ValidationError(Exception):
    """A dashboard payload or Grafana response failed validation."""


def _resolve_placeholders(content: str) -> str:
    """Resolve every UPPER_SNAKE feature placeholder so YAML parses."""
    return _FEATURE_PLACEHOLDER_RE.sub("true", content)


def extract_dashboards(manifest_paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Return ``{uid: dashboard}`` for every sidecar-labeled ConfigMap payload.

    Raises :class:`ValidationError` for anything the Grafana sidecar would
    swallow silently: unparseable JSON, a missing/duplicate ``uid``, or a
    missing ``title``.
    """
    dashboards: dict[str, dict[str, Any]] = {}
    for path in manifest_paths:
        content = _resolve_placeholders(path.read_text(encoding="utf-8"))
        for document in yaml.safe_load_all(content):
            if not isinstance(document, dict) or document.get("kind") != "ConfigMap":
                continue
            metadata = document.get("metadata") or {}
            labels = metadata.get("labels") or {}
            if str(labels.get(_SIDECAR_LABEL, "")) != "1":
                continue
            name = metadata.get("name", "<unnamed>")
            for key, payload in (document.get("data") or {}).items():
                if not str(key).endswith(".json"):
                    continue
                where = f"{path.name} ConfigMap {name} data {key}"
                try:
                    dashboard = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise ValidationError(f"{where}: invalid JSON: {exc}") from exc
                uid = dashboard.get("uid")
                if not isinstance(uid, str) or not uid:
                    raise ValidationError(f"{where}: dashboard has no uid")
                if uid in dashboards:
                    raise ValidationError(f"{where}: duplicate dashboard uid {uid!r}")
                if not dashboard.get("title"):
                    raise ValidationError(f"{where}: dashboard has no title")
                dashboards[uid] = dashboard
    if not dashboards:
        raise ValidationError("no sidecar-labeled dashboard ConfigMaps found")
    return dashboards


def read_chart_pin(charts_yaml: Path, chart: str = "kube-prometheus-stack") -> dict[str, str]:
    """Read the pinned version and repo URL for ``chart`` from charts.yaml."""
    data = yaml.safe_load(charts_yaml.read_text(encoding="utf-8"))
    entries = data.get("charts", data) if isinstance(data, dict) else {}
    for entry in entries.values():
        if isinstance(entry, dict) and entry.get("chart") == chart:
            version = str(entry.get("version", "")).strip()
            repo_url = str(entry.get("repo_url", "")).strip()
            if not version or not repo_url:
                raise ValidationError(f"{chart} entry is missing version or repo_url")
            return {"version": version, "repo_url": repo_url}
    raise ValidationError(f"no {chart} entry found in {charts_yaml}")


# Polling a Grafana that is still booting sees the whole zoo of transport
# failures: connection refused, docker-proxy accepting then resetting the
# socket (raw ConnectionResetError, not wrapped in URLError), and half-open
# responses (http.client.RemoteDisconnected). OSError covers URLError,
# TimeoutError, and every Connection*Error; HTTPException covers the
# half-open cases. urllib.error.HTTPError is caught separately first, so
# real HTTP status codes are still returned rather than swallowed here.
_RETRIABLE_FETCH_ERRORS = (OSError, http.client.HTTPException, json.JSONDecodeError)


def _get(url: str, auth: tuple[str, str] | None = None, timeout: float = 10.0) -> tuple[int, Any]:
    """GET a Grafana API URL, returning (status, parsed JSON or None)."""
    if not url.startswith(("http://", "https://")):
        raise ValidationError(f"refusing non-HTTP URL {url!r}")
    request = urllib.request.Request(url)  # noqa: S310 - scheme validated above
    if auth is not None:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        # The URL is assembled from the --url flag this CI job passes for its
        # own localhost container plus repo-controlled dashboard uids, and the
        # scheme check above rejects anything that is not plain HTTP(S).
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except _RETRIABLE_FETCH_ERRORS:
        return 0, None


def verify(
    url: str,
    dashboards_dir: Path,
    user: str,
    password: str,
    timeout_seconds: float = 180.0,
) -> list[str]:
    """Assert every extracted dashboard is provisioned in the running Grafana.

    Returns a list of failure messages (empty when everything passed).
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        status, health = _get(f"{url}/api/health")
        if status == 200 and isinstance(health, dict) and health.get("database") == "ok":
            print(f"Grafana healthy: version {health.get('version', '<unknown>')}")
            break
        if time.monotonic() >= deadline:
            return [f"Grafana at {url} did not become healthy within {timeout_seconds:.0f}s"]
        time.sleep(2)

    sources = sorted(dashboards_dir.glob("*.json"))
    if not sources:
        return [f"no extracted dashboards found under {dashboards_dir}"]

    failures: list[str] = []
    for source_path in sources:
        source = json.loads(source_path.read_text(encoding="utf-8"))
        uid, title = source["uid"], source["title"]
        # File provisioning is asynchronous; give the provisioner a moment
        # per dashboard rather than one global sleep.
        item_deadline = time.monotonic() + 30
        while True:
            status, body = _get(f"{url}/api/dashboards/uid/{uid}", auth=(user, password))
            if status == 200 or time.monotonic() >= item_deadline:
                break
            time.sleep(2)
        if status != 200 or not isinstance(body, dict):
            failures.append(f"{uid}: Grafana answered {status}, expected 200")
            continue
        loaded_title = (body.get("dashboard") or {}).get("title")
        provisioned = (body.get("meta") or {}).get("provisioned")
        if loaded_title != title:
            failures.append(f"{uid}: loaded title {loaded_title!r} != source {title!r}")
        elif provisioned is not True:
            failures.append(f"{uid}: dashboard loaded but meta.provisioned is {provisioned!r}")
        else:
            print(f"PASS {uid}: provisioned as {title!r}")
    return failures


def _cmd_extract(args: argparse.Namespace) -> int:
    dashboards = extract_dashboards([Path(item) for item in args.manifest])
    out_dir = Path(args.out_dir)
    dashboards_dir = out_dir / "dashboards"
    provisioning_dir = out_dir / "provisioning"
    dashboards_dir.mkdir(parents=True, exist_ok=True)
    provisioning_dir.mkdir(parents=True, exist_ok=True)
    (provisioning_dir / "gco-dashboards.yaml").write_text(_PROVIDER_YAML, encoding="utf-8")
    for uid, dashboard in sorted(dashboards.items()):
        target = dashboards_dir / f"{uid}.json"
        target.write_text(json.dumps(dashboard, indent=2), encoding="utf-8")
        print(f"extracted uid={uid} title={dashboard['title']!r} -> {target}")
    print(f"extracted {len(dashboards)} dashboard(s)")
    return 0


def _cmd_chart_version(args: argparse.Namespace) -> int:
    pin = read_chart_pin(Path(args.charts_yaml))
    lines = [f"version={pin['version']}", f"repo_url={pin['repo_url']}"]
    for line in lines:
        print(line)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    failures = verify(
        args.url.rstrip("/"),
        Path(args.dashboards_dir),
        user=os.environ.get("GRAFANA_USER", "admin"),
        password=os.environ.get("GRAFANA_PASSWORD", "admin"),
        timeout_seconds=args.timeout,
    )
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="extract dashboard JSONs from manifests")
    extract.add_argument("--manifest", action="append", required=True)
    extract.add_argument("--out-dir", required=True)
    extract.set_defaults(func=_cmd_extract)

    chart = subparsers.add_parser("chart-version", help="print the pinned chart version")
    chart.add_argument(
        "--charts-yaml",
        default="lambda/helm-installer/charts.yaml",
    )
    chart.set_defaults(func=_cmd_chart_version)

    check = subparsers.add_parser("verify", help="assert dashboards provisioned in Grafana")
    check.add_argument("--url", default="http://127.0.0.1:3000")
    check.add_argument("--dashboards-dir", required=True)
    check.add_argument("--timeout", type=float, default=180.0)
    check.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValidationError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
