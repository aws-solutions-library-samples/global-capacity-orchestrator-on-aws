"""Read-only GPU capacity from an NVIDIA Run:ai control plane.

``gco capacity runai`` (and the ``runai_capacity`` MCP tool, which shells out to
it) reports, for every Run:ai cluster and node pool the access key can see: the
GPUs the pool has, how many are allocated, how many whole GPUs a new workload
could be given, the GPU models its nodes carry with the EC2 families that carry
the same model, and each project's quota headroom on the pool.

What it deliberately is not (docs/adr/0006-runai-read-only-capacity-source.md):

* **Not a scheduler.** One token request and GET requests; nothing is created,
  changed or submitted. No GCO job path imports this module
  (``tests/test_capacity_runai.py`` guards it) and every row says
  ``submit_compatible: false``.
* **Not part of the AWS Region ranking.** A Run:ai row has no AWS Region,
  Availability Zone, price or reservation, so ``recommend-region`` and the
  capacity scoring never see it.

What it is careful about (TENETS.md, "Tell the Truth About State and Capacity"):

* Telemetry is a metrics snapshot, not scheduler state, so every row carries
  ``observed_at`` and ``valid_until`` (``TELEMETRY_VALIDITY_SECONDS`` later).
* A value the control plane did not return is ``null`` with a note, never 0.
  A 403 is an error, never "0 free GPUs".
* Fractional GPU sharing means ``FREE_GPUS`` alone can overstate whole GPUs, so
  ``whole_gpus_upper_bound`` is the smaller of ``FREE_GPUS`` and the whole GPUs
  left after ``ALLOCATED_GPUS``.

The routes and fields read here are declared in
``gco/config/runai_api_contract.json``; the monthly dependency scan checks them
against every NVIDIA Run:ai API reference version from 2.24 on
(``.github/scripts/check_runai_api_contract.py``).

Configuration comes only from the environment, so an MCP agent cannot point the
reader (and its client secret) at another host:

``GCO_RUNAI_URL``            the control plane, ``https://host[:port]``
``GCO_RUNAI_CLIENT_ID``      a service account or user access key
``GCO_RUNAI_CLIENT_SECRET``  its secret
``GCO_RUNAI_CA_FILE``        optional PEM bundle for a certificate signed by a
                             private CA; when set it is the only trust anchor
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

ENV_URL = "GCO_RUNAI_URL"
ENV_CLIENT_ID = "GCO_RUNAI_CLIENT_ID"
ENV_CLIENT_SECRET = "GCO_RUNAI_CLIENT_SECRET"  # nosec B105 - an environment variable name
ENV_CA_FILE = "GCO_RUNAI_CA_FILE"

#: The first published API reference with every route below.
MIN_SUPPORTED_VERSION = "2.24"
#: The API's page-size ceiling (``limit`` accepts 1..500).
MAX_PAGE_SIZE = 500
#: How long a telemetry-derived number should be trusted after it was observed.
#: Telemetry is a metrics snapshot rather than scheduler state, so it can trail
#: the scheduler, and the API reference documents no freshness bound; a
#: capacity number much older than a metrics collection cycle is a guess.
TELEMETRY_VALIDITY_SECONDS = 90
#: Refuse to page past this many items from one list route: a server that
#: ignores ``offset`` must not loop the reader forever.
MAX_ITEMS_PER_ROUTE = 100_000
_TIMEOUT = (10.0, 30.0)

TOKEN_PATH = "/api/v2/token"  # nosec B105 - a route, not a secret
CLUSTERS_PATH = "/api/v1/clusters"
NODE_POOLS_PATH = "/api/v1/node-pools"
NODES_PATH = "/api/v1/nodes"
TELEMETRY_PATH = "/api/v1/nodes/telemetry"
QUOTAS_PATH = "/api/v1/org-unit/projects/node-pool-resources"
#: Every route the reader calls, as ``(method, path)``. The contract file must
#: declare exactly these (tests/test_capacity_runai.py checks it).
ROUTES = (
    ("POST", TOKEN_PATH),
    ("GET", CLUSTERS_PATH),
    ("GET", NODE_POOLS_PATH),
    ("GET", NODES_PATH),
    ("GET", TELEMETRY_PATH),
    ("GET", QUOTAS_PATH),
)
TELEMETRY_TYPES = ("TOTAL_GPUS", "ALLOCATED_GPUS", "FREE_GPUS")

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "gco" / "config" / "accelerator_catalog.json"
#: Vendor and product-line words that prefix a GPU model in a Kubernetes GPU
#: product label (``Tesla-T4``, ``NVIDIA-H100-80GB-HBM3``) but not in EC2's
#: ``GpuInfo.Gpus[].Name`` (``T4``, ``H100``).
_VENDOR_TOKENS = frozenset({"NVIDIA", "TESLA"})


class RunAIError(RuntimeError):
    """The capacity could not be read. The message is safe to show."""


class RunAIConfigError(RunAIError):
    """The environment does not describe a usable control plane."""


class RunAIAuthError(RunAIError):
    """The control plane rejected the credentials or the token (401)."""


class RunAIForbidden(RunAIError):
    """The access key may not read a route (403). Never reported as 0 GPUs."""


class RunAIUnsupported(RunAIError):
    """The control plane does not serve a route the reader needs (404)."""


class RunAIResponseError(RunAIError):
    """A response did not have the shape the API reference documents."""


@dataclass(frozen=True)
class RunAISettings:
    url: str
    client_id: str
    client_secret: str = field(repr=False)
    ca_file: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RunAISettings:
        source = os.environ if env is None else env
        url = source.get(ENV_URL, "").strip()
        client_id = source.get(ENV_CLIENT_ID, "").strip()
        client_secret = source.get(ENV_CLIENT_SECRET, "").strip()
        missing = [
            name
            for name, value in (
                (ENV_URL, url),
                (ENV_CLIENT_ID, client_id),
                (ENV_CLIENT_SECRET, client_secret),
            )
            if not value
        ]
        if missing:
            raise RunAIConfigError(
                f"set {', '.join(missing)} to read Run:ai capacity (see docs/RUNAI.md)"
            )
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise RunAIConfigError(f"{ENV_URL} must be an https:// URL, got {url!r}")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise RunAIConfigError(
                f"{ENV_URL} must be the control plane's origin (https://host[:port]), got {url!r}"
            )
        ca_value = source.get(ENV_CA_FILE, "").strip()
        ca_file = Path(ca_value).expanduser() if ca_value else None
        if ca_file is not None and not ca_file.is_file():
            raise RunAIConfigError(f"{ENV_CA_FILE} names {ca_file}, which is not a readable file")
        return cls(
            url=f"https://{parts.netloc}",
            client_id=client_id,
            client_secret=client_secret,
            ca_file=ca_file,
        )


def _new_session() -> requests.Session:
    session = requests.Session()
    # Proxies, .netrc credentials and REQUESTS_CA_BUNDLE come from the
    # environment only when trust_env is on; the reader's trust is GCO_RUNAI_*
    # alone, like GCO's other non-AWS HTTP clients.
    session.trust_env = False
    return session


class RunAIClient:
    """One authenticated read of a control plane: a token, then GETs."""

    def __init__(
        self,
        settings: RunAISettings,
        *,
        page_size: int = MAX_PAGE_SIZE,
        session: requests.Session | None = None,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise RunAIConfigError(f"page size must be between 1 and {MAX_PAGE_SIZE}")
        self.settings = settings
        self.page_size = page_size
        self._session = session if session is not None else _new_session()
        self._token: str | None = None
        self.requests_made: list[str] = []

    def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.settings.url}{path}"
        label = f"{method} {path}"
        self.requests_made.append(label)
        verify: str | bool = str(self.settings.ca_file) if self.settings.ca_file else True
        try:
            response = self._session.request(
                method, url, timeout=_TIMEOUT, verify=verify, allow_redirects=False, **kwargs
            )
        except requests.exceptions.SSLError as exc:
            raise RunAIError(
                f"TLS verification of {self.settings.url} failed ({exc.__class__.__name__}). If "
                f"its certificate is signed by a private CA, set {ENV_CA_FILE} to that CA's PEM "
                "bundle"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RunAIError(
                f"cannot reach {self.settings.url} ({exc.__class__.__name__})"
            ) from exc
        status = response.status_code
        if 300 <= status < 400:
            raise RunAIError(
                f"{label}: the control plane redirected (HTTP {status}); set {ENV_URL} to the "
                "URL it serves the API on. The reader does not follow redirects, so a secret "
                "is never sent to another host"
            )
        if status == 401:
            raise RunAIAuthError(
                f"{label}: 401 Unauthorized, the control plane rejected the "
                + ("client credentials" if path == TOKEN_PATH else "token")
            )
        if status == 403:
            raise RunAIForbidden(
                f"{label}: 403 Forbidden, the access key may not read this. Grant its role "
                "read access to clusters, nodes, node pools and projects on every cluster it "
                "should report"
            )
        if status == 404:
            raise RunAIUnsupported(
                f"{label}: 404 Not Found, this control plane does not serve the route; "
                f"the reader needs NVIDIA Run:ai {MIN_SUPPORTED_VERSION} or later"
            )
        if not 200 <= status < 300:
            raise RunAIError(f"{label}: HTTP {status}")
        try:
            return response.json()
        except ValueError as exc:
            raise RunAIResponseError(f"{label}: the response is not JSON") from exc

    def authenticate(self) -> None:
        """Exchange the client credentials for an access token (once per client)."""
        if self._token is not None:
            return
        body = self._send(
            "POST",
            TOKEN_PATH,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
            },
            headers={"Accept": "application/json"},
        )
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise RunAIResponseError(f"POST {TOKEN_PATH}: no access_token in the response")
        self._token = token

    def get(self, path: str, params: list[tuple[str, str | int]] | None = None) -> Any:
        self.authenticate()
        return self._send(
            "GET",
            path,
            params=params or [],
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._token}"},
        )

    def get_paged(self, path: str, collection: str) -> list[dict[str, Any]]:
        """Every item of a ``limit``/``offset`` list route.

        The reference documents an optional ``next`` pointer whose units it does
        not pin down, so the reader advances ``offset`` by what it received and
        stops on a short page, an empty page, or an explicit empty ``next``.
        A page that repeats the previous one means the server ignored ``offset``,
        which is an error rather than a reason to loop.
        """
        items: list[dict[str, Any]] = []
        offset = 0
        previous: list[Any] | None = None
        while True:
            page = self.get(path, [("offset", offset), ("limit", self.page_size)])
            batch = page.get(collection) if isinstance(page, dict) else None
            if not isinstance(batch, list):
                raise RunAIResponseError(f"GET {path}: the response has no {collection} list")
            if batch and batch == previous:
                raise RunAIResponseError(
                    f"GET {path}: offset {offset} returned the previous page again; the "
                    "server is not paging"
                )
            items.extend(item for item in batch if isinstance(item, dict))
            if len(items) > MAX_ITEMS_PER_ROUTE:
                raise RunAIResponseError(f"GET {path}: more than {MAX_ITEMS_PER_ROUTE} items")
            if not batch or ("next" in page and page["next"] in (None, "")):
                break
            if "next" not in page and len(batch) < self.page_size:
                break
            previous = batch
            offset += len(batch)
        return items


# ---------------------------------------------------------------------------
# GPU model matching against the accelerator catalog
# ---------------------------------------------------------------------------


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token in re.split(r"[\s_-]+", value.upper()) if token)


def load_gpu_models(catalog_path: Path = _CATALOG_PATH) -> dict[str, list[str]]:
    """EC2 GPU model -> the catalog families that carry it."""
    try:
        catalog: object = json.loads(catalog_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    families = catalog.get("families") if isinstance(catalog, dict) else None
    models: dict[str, list[str]] = {}
    for name, policy in (families or {}).items():
        model = policy.get("gpu_model") if isinstance(policy, dict) else None
        if isinstance(model, str) and model:
            models.setdefault(model, []).append(name)
    return {model: sorted(names) for model, names in models.items()}


def match_gpu_model(gpu_type: str, models: Mapping[str, list[str]]) -> str | None:
    """The EC2 GPU model a Run:ai ``gpuType`` names, or ``None``.

    Run:ai reports the node's GPU product label (``Tesla-T4``, ``NVIDIA-A10G``,
    ``NVIDIA-H100-80GB-HBM3``); EC2 names the model alone (``T4``, ``A10G``,
    ``H100``). After the vendor words, the longest EC2 model whose words begin
    the label wins, so ``L40S`` never matches ``L4`` and memory or form-factor
    suffixes are ignored.
    """
    words = list(_tokens(gpu_type))
    while words and words[0] in _VENDOR_TOKENS:
        words.pop(0)
    best: tuple[int, str] | None = None
    for model in models:
        model_words = _tokens(model)
        if (
            model_words
            and tuple(words[: len(model_words)]) == model_words
            and (best is None or len(model_words) > best[0])
        ):
            best = (len(model_words), model)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Turning the documents into rows
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=datetime.UTC)


def _iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


@dataclass
class Telemetry:
    """One telemetry type: a value per (cluster id, node pool name)."""

    values: dict[tuple[str, str], float | None]
    observed_at: datetime.datetime | None
    notes: list[str]


def parse_telemetry(document: Any, telemetry_type: str) -> Telemetry:
    if not isinstance(document, dict) or not isinstance(document.get("values"), list):
        raise RunAIResponseError(f"GET {TELEMETRY_PATH} ({telemetry_type}): no values list")
    values: dict[tuple[str, str], float | None] = {}
    notes: list[str] = []
    for entry in document["values"]:
        if not isinstance(entry, dict):
            continue
        groups = {
            str(group.get("key", "")).lower(): str(group.get("value", ""))
            for group in entry.get("groups") or []
            if isinstance(group, dict)
        }
        cluster_id, pool = groups.get("clusterid", ""), groups.get("nodepool", "")
        if not cluster_id or not pool:
            notes.append(
                f"{telemetry_type}: a value without a ClusterId and Nodepool group was ignored"
            )
            continue
        key = (cluster_id, pool)
        if key in values:
            notes.append(f"{telemetry_type}: {pool} reported twice; the first value is kept")
            continue
        number = _number(entry.get("value"))
        if number is None:
            notes.append(f"{telemetry_type}: {pool} reported {entry.get('value')!r}, not a number")
        values[key] = number
    return Telemetry(values=values, observed_at=_timestamp(document.get("timestamp")), notes=notes)


def _quota_row(item: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    project = str(item.get("orgUnitName") or item.get("orgUnitId") or "?")
    gpu = (
        ((item.get("resources") or {}).get("gpu") or {})
        if isinstance(item.get("resources"), dict)
        else {}
    )
    deserved = _number(gpu.get("deserved")) if isinstance(gpu, dict) else None
    limit = _number(gpu.get("limit")) if isinstance(gpu, dict) else None
    status = item.get("quotaStatus")
    allocated_doc = status.get("allocated") if isinstance(status, dict) else None
    if isinstance(allocated_doc, dict):
        if "gpu" in allocated_doc:
            allocated = _number(allocated_doc.get("gpu"))
        else:
            # The schema makes the number optional inside a present object.
            # Read an omitted number as zero (JSON encoders commonly omit zero
            # values) and say so; a missing object stays unknown.
            allocated = 0.0
            notes.append(f"project {project}: quotaStatus.allocated has no gpu value; read as 0")
    else:
        allocated = None
        notes.append(f"project {project}: no quotaStatus.allocated; allocation unknown")
    headroom = (
        deserved - allocated
        if deserved is not None and deserved >= 0 and allocated is not None
        else None
    )
    return (
        {
            "project": project,
            "project_id": str(item.get("orgUnitId") or ""),
            "deserved_gpus": deserved,
            "limit_gpus": limit,
            "allocated_gpus": allocated,
            "headroom_gpus": headroom,
        },
        notes,
    )


@dataclass(frozen=True)
class Request:
    cluster: str | None = None
    node_pool: str | None = None
    gpu_count: int | None = None
    project: str | None = None


def build_report(
    *,
    url: str,
    clusters: list[Any],
    node_pools: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    quotas: list[dict[str, Any]],
    telemetry: Mapping[str, Telemetry],
    queried_at: datetime.datetime,
    request: Request,
    gpu_models: Mapping[str, list[str]],
) -> dict[str, Any]:
    """The report ``gco capacity runai`` prints: one row per cluster and node pool."""
    notes: list[str] = []
    cluster_info: dict[str, dict[str, Any]] = {}
    for cluster in clusters:
        if isinstance(cluster, dict) and cluster.get("uuid"):
            cluster_info[str(cluster["uuid"])] = {
                "id": str(cluster["uuid"]),
                "name": str(cluster.get("name") or cluster["uuid"]),
                "version": cluster.get("version"),
            }
    for kind in TELEMETRY_TYPES:
        notes.extend(telemetry[kind].notes)
    stamps = [t.observed_at for t in telemetry.values() if t.observed_at is not None]
    if stamps:
        observed_at = min(stamps)
    else:
        observed_at = queried_at
        notes.append("telemetry carried no timestamp; observed_at is when GCO asked")
    valid_until = observed_at + datetime.timedelta(seconds=TELEMETRY_VALIDITY_SECONDS)

    gpu_types: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for node in nodes:
        info = node.get("gpuInfo")
        if not isinstance(info, dict) or not info.get("gpuType"):
            continue
        key = (str(node.get("clusterUuid") or ""), str(node.get("nodePool") or ""))
        gpu_type = str(info["gpuType"])
        entry = gpu_types.setdefault(key, {}).setdefault(
            gpu_type,
            {"gpu_type": gpu_type, "nodes": 0, "ready_nodes": 0, "gpus": 0},
        )
        entry["nodes"] += 1
        entry["ready_nodes"] += 1 if node.get("status") == "Ready" else 0
        count = _number(info.get("gpuCount"))
        entry["gpus"] += int(count) if count is not None else 0

    pool_quotas: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in quotas:
        key = (str(item.get("clusterId") or ""), str(item.get("nodePoolName") or ""))
        quota, quota_notes = _quota_row(item)
        if request.project is not None and quota["project"] != request.project:
            continue
        pool_quotas.setdefault(key, []).append(quota)
        notes.extend(quota_notes)

    rows: list[dict[str, Any]] = []
    for pool in sorted(
        node_pools, key=lambda p: (str(p.get("clusterName") or ""), str(p.get("name") or ""))
    ):
        cluster_id = str(pool.get("clusterId") or "")
        info = cluster_info.get(
            cluster_id,
            {"id": cluster_id, "name": str(pool.get("clusterName") or cluster_id), "version": None},
        )
        name = str(pool.get("name") or "")
        if request.cluster is not None and request.cluster not in (info["id"], info["name"]):
            continue
        if request.node_pool is not None and request.node_pool != name:
            continue
        key = (cluster_id, name)
        row_notes: list[str] = []
        counts: dict[str, float | None] = {}
        for kind, label in zip(
            TELEMETRY_TYPES, ("total_gpus", "allocated_gpus", "free_gpus"), strict=True
        ):
            series = telemetry[kind].values
            counts[label] = series.get(key)
            if key not in series:
                row_notes.append(f"no {kind} value for this pool; reported as unknown, not 0")
        total, allocated, free = counts["total_gpus"], counts["allocated_gpus"], counts["free_gpus"]
        bound: int | None = None
        if total is not None and allocated is not None:
            bound = max(0, math.floor(total - allocated + 1e-9))
            if free is not None:
                bound = min(bound, math.floor(free + 1e-9))
        types = []
        for entry in sorted(gpu_types.get(key, {}).values(), key=lambda e: e["gpu_type"]):
            model = match_gpu_model(entry["gpu_type"], gpu_models)
            types.append(
                {
                    **entry,
                    "ec2_gpu_model": model,
                    "ec2_families": list(gpu_models.get(model, [])) if model else [],
                }
            )
        row: dict[str, Any] = {
            "source": "runai",
            "submit_compatible": False,
            "cluster": info["name"],
            "cluster_id": cluster_id,
            "node_pool": name,
            "node_pool_id": str(pool.get("id") or ""),
            "phase": pool.get("phase"),
            "is_default": bool(pool.get("isDefault")),
            "gpu_types": types,
            "total_gpus": total,
            "allocated_gpus": allocated,
            "free_gpus": free,
            "whole_gpus_upper_bound": bound,
            "projects": pool_quotas.get(key, []),
            "observed_at": _iso(observed_at),
            "valid_until": _iso(valid_until),
            "notes": row_notes,
        }
        if request.gpu_count is not None:
            row["fits_gpu_count"] = None if bound is None else bound >= request.gpu_count
        if request.project is not None:
            headrooms = [p["headroom_gpus"] for p in row["projects"]]
            known = [h for h in headrooms if h is not None]
            row["project_headroom_gpus"] = known[0] if known else None
            if request.gpu_count is not None:
                row["within_project_quota"] = None if not known else known[0] >= request.gpu_count
        rows.append(row)
    return {
        "source": "runai",
        "submit_compatible": False,
        "control_plane": url,
        "observed_at": _iso(observed_at),
        "valid_until": _iso(valid_until),
        "request": {
            "cluster": request.cluster,
            "node_pool": request.node_pool,
            "gpu_count": request.gpu_count,
            "project": request.project,
        },
        "clusters": sorted(cluster_info.values(), key=lambda c: c["name"]),
        "rows": rows,
        "notes": notes,
    }


def read_capacity(
    settings: RunAISettings,
    request: Request | None = None,
    *,
    page_size: int = MAX_PAGE_SIZE,
    session: requests.Session | None = None,
    now: datetime.datetime | None = None,
    gpu_models: Mapping[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Read the control plane once and build the report.

    One token request and, when every list fits one page, seven GETs: clusters,
    node pools, nodes, project quotas and three telemetry types.
    """
    client = RunAIClient(settings, page_size=page_size, session=session)
    queried_at = now or datetime.datetime.now(datetime.UTC)
    clusters = client.get(CLUSTERS_PATH)
    if not isinstance(clusters, list):
        raise RunAIResponseError(f"GET {CLUSTERS_PATH}: the response is not a list")
    node_pools = client.get_paged(NODE_POOLS_PATH, "nodepools")
    nodes = client.get_paged(NODES_PATH, "nodes")
    quotas = client.get_paged(QUOTAS_PATH, "nodePoolResources")
    telemetry = {
        kind: parse_telemetry(
            client.get(
                TELEMETRY_PATH,
                [("telemetryType", kind), ("groupBy", "ClusterId"), ("groupBy", "Nodepool")],
            ),
            kind,
        )
        for kind in TELEMETRY_TYPES
    }
    return build_report(
        url=settings.url,
        clusters=clusters,
        node_pools=node_pools,
        nodes=nodes,
        quotas=quotas,
        telemetry=telemetry,
        queried_at=queried_at,
        request=request or Request(),
        gpu_models=load_gpu_models() if gpu_models is None else gpu_models,
    )


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "?"
    return f"{value:g}" if isinstance(value, float) else str(value)


def render_table(report: Mapping[str, Any]) -> str:
    """The report as the terminal shows it."""
    request = report["request"]
    lines = [
        f"\n  NVIDIA Run:ai capacity — {report['control_plane']} (read-only; not ranked with AWS Regions)",
        f"  Observed {report['observed_at']}, valid until {report['valid_until']}",
        "  " + "-" * 100,
    ]
    rows: Iterable[Mapping[str, Any]] = report["rows"]
    header = f"  {'CLUSTER':<18} {'NODE POOL':<18} {'GPU TYPES':<26} {'TOTAL':>5} {'ALLOC':>6} {'FREE':>5} {'WHOLE':>5}"
    if request["gpu_count"] is not None:
        header += f" {'FITS ' + str(request['gpu_count']):>7}"
    if request["project"] is not None:
        header += f" {'HEADROOM':>8}"
    lines.append(header)
    any_row = False
    for row in rows:
        any_row = True
        types = (
            ",".join(
                f"{t['gpu_type']}" + (f"({t['ec2_gpu_model']})" if t["ec2_gpu_model"] else "")
                for t in row["gpu_types"]
            )
            or "-"
        )
        line = (
            f"  {row['cluster'][:18]:<18} {row['node_pool'][:18]:<18} {types[:26]:<26} "
            f"{_fmt(row['total_gpus']):>5} {_fmt(row['allocated_gpus']):>6} "
            f"{_fmt(row['free_gpus']):>5} {_fmt(row['whole_gpus_upper_bound']):>5}"
        )
        if request["gpu_count"] is not None:
            fits = row.get("fits_gpu_count")
            line += f" {'?' if fits is None else ('yes' if fits else 'no'):>7}"
        if request["project"] is not None:
            line += f" {_fmt(row.get('project_headroom_gpus')):>8}"
        lines.append(line)
    if not any_row:
        lines.append("  (no node pools match)")
    notes = [
        *report["notes"],
        *(f"{r['cluster']}/{r['node_pool']}: {n}" for r in report["rows"] for n in r["notes"]),
    ]
    if notes:
        lines.append("")
        lines.extend(f"  note: {note}" for note in notes)
    lines.append("")
    lines.append("  WHOLE = whole GPUs a new workload could be given; ? = not reported (never 0).")
    return "\n".join(lines)
