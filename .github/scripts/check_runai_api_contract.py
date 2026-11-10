"""Check the NVIDIA Run:ai API contract ``gco capacity runai`` depends on.

``cli/capacity/runai.py`` reads six routes of the Run:ai control-plane REST
API. ``gco/config/runai_api_contract.json`` declares them: method and path,
the query parameters and parameter values the reader sends, the form fields
of the token request, and every response field the reader reads. NVIDIA
publishes the API reference per version (``/api/<major.minor>/...`` and the
unversioned latest under ``/api/...``), each page with its OpenAPI document
embedded, so the contract can be checked without a Run:ai installation.

This script reads the documentation sitemap, checks every published version
from the contract's ``min_version`` on plus latest, and prints one
``version|route|finding|action`` row per problem (``--format rows``, the
dependency scan's surface) or the same as JSON (``--format json``):

* a route the reader calls is missing, or marked deprecated;
* a parameter, parameter value, request field or response field is missing;
* a published version newer than every ``reviewed_versions`` entry satisfies
  the contract: record it as reviewed (a reminder, not a break).

Exit status: 0 when the reference could be read (rows may still be printed),
2 when the sitemap or a page could not be fetched or parsed. The dependency
scan treats 2 as a skip, never as an all-clear. Standard library only: the
dependency-scan job runs it without installing the project.

Usage::

    python3 .github/scripts/check_runai_api_contract.py --format rows
    python3 .github/scripts/check_runai_api_contract.py --docs-root file:///tmp/fixture
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "gco" / "config" / "runai_api_contract.json"
#: The docs site refuses Python's default User-Agent with a 403.
USER_AGENT = "gco-runai-api-contract-check/1"
LATEST = "latest"
_OPENAPI_BLOCK = re.compile(r"```json\n(\{\"openapi\".*?)\n```", re.S)
_VERSION_SITEMAP = re.compile(r"/api/(\d+\.\d+)/sitemap-pages\.xml$")
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")

Fetch = Callable[[str], str]


class ContractCheckError(RuntimeError):
    """The reference could not be read: a skip, not a finding."""


@dataclass(frozen=True)
class Finding:
    version: str
    route: str
    finding: str
    action: str

    def row(self) -> str:
        return "|".join(
            " ".join(part.replace("|", "/").split())
            for part in (self.version, self.route, self.finding, self.action)
        )


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def fetch_url(url: str, *, attempts: int = 3, timeout: float = 30.0) -> str:
    """GET ``url`` (``https://`` or ``file://``) as text, retrying transient failures."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed docs host or test fixture
                body: bytes = response.read()
                return body.decode("utf-8")
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
            attempt += 1
            if url.startswith("file:") or attempt >= attempts:
                raise ContractCheckError(f"cannot read {url}: {exc}") from exc
            time.sleep(2 ** (attempt - 1))


def load_contract(path: Path) -> dict[str, Any]:
    try:
        contract: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractCheckError(f"cannot read the contract {path}: {exc}") from exc
    if not isinstance(contract, dict) or not isinstance(contract.get("routes"), list):
        raise ContractCheckError(f"{path}: a contract needs a routes list")
    return contract


def published_versions(sitemap: str, min_version: str) -> list[str]:
    """Versioned API references in the sitemap index from ``min_version`` on, ascending."""
    versions = set()
    for loc in _LOC.findall(sitemap):
        match = _VERSION_SITEMAP.search(loc)
        if match:
            versions.add(match.group(1))
    floor = _version_key(min_version)
    return sorted((v for v in versions if _version_key(v) >= floor), key=_version_key)


def page_url(docs_root: str, version: str, page: str) -> str:
    prefix = "/api" if version == LATEST else f"/api/{version}"
    return f"{docs_root.rstrip('/')}{prefix}/{page}.md"


def openapi_index(pages: Iterable[str]) -> tuple[dict[tuple[str, str], Any], dict[str, Any]]:
    """Merge every embedded OpenAPI document into (operations, components)."""
    operations: dict[tuple[str, str], Any] = {}
    components: dict[str, Any] = {}
    for text in pages:
        for block in _OPENAPI_BLOCK.findall(text):
            try:
                document = json.loads(block)
            except json.JSONDecodeError as exc:
                raise ContractCheckError(
                    f"an embedded OpenAPI document is not JSON: {exc}"
                ) from exc
            for path, methods in (document.get("paths") or {}).items():
                for method, operation in (methods or {}).items():
                    operations[(method.upper(), path)] = operation
            for kind, items in (document.get("components") or {}).items():
                components.setdefault(kind, {}).update(items or {})
    return operations, components


def _deref(node: Any, components: Mapping[str, Any], seen: tuple[str, ...] = ()) -> Any:
    """Follow ``$ref`` chains and fold ``allOf``/``oneOf``/``anyOf`` into one schema."""
    while isinstance(node, dict) and "$ref" in node:
        kind, name = str(node["$ref"]).split("/")[-2:]
        if name in seen:
            return {}
        seen = (*seen, name)
        node = (components.get(kind) or {}).get(name, {})
    if not isinstance(node, dict):
        return {}
    properties = dict(node.get("properties") or {})
    items = node.get("items")
    for key in ("allOf", "oneOf", "anyOf"):
        for sub in node.get(key) or []:
            resolved = _deref(sub, components, seen)
            properties.update(resolved.get("properties") or {})
            if items is None and "items" in resolved:
                items = resolved["items"]
    merged = dict(node)
    if properties:
        merged["properties"] = properties
    if items is not None:
        merged["items"] = items
    return merged


def has_field(schema: Any, path: str, components: Mapping[str, Any]) -> bool:
    """Whether ``path`` (``a.b``, ``a[].b``, ``[].b``) resolves inside ``schema``."""
    node = _deref(schema, components)
    parts = path.split(".")
    if parts[0] == "[]":
        if node.get("type") != "array" and "items" not in node:
            return False
        node = _deref(node.get("items", {}), components)
        parts = parts[1:]
    for part in parts:
        is_array = part.endswith("[]")
        name = part[:-2] if is_array else part
        prop = (node.get("properties") or {}).get(name)
        if prop is None:
            return False
        node = _deref(prop, components)
        if is_array:
            node = _deref(node.get("items", {}), components)
    return True


def _response_schema(operation: Mapping[str, Any], components: Mapping[str, Any]) -> Any:
    responses = operation.get("responses") or {}
    for code in ("200", "201"):
        if code in responses:
            response = _deref(responses[code], components)
            content = response.get("content") or {}
            return (content.get("application/json") or {}).get("schema", {})
    return {}


def _request_schema(operation: Mapping[str, Any], components: Mapping[str, Any]) -> Any:
    body = _deref(operation.get("requestBody") or {}, components)
    content = body.get("content") or {}
    for media in ("application/x-www-form-urlencoded", "application/json"):
        if media in content:
            return content[media].get("schema", {})
    return {}


def _parameters(operation: Mapping[str, Any], components: Mapping[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for parameter in operation.get("parameters") or []:
        resolved = _deref(parameter, components)
        name = resolved.get("name")
        if isinstance(name, str):
            found[name] = _deref(resolved.get("schema") or {}, components)
    return found


def _allowed_values(schema: Mapping[str, Any], components: Mapping[str, Any]) -> set[str] | None:
    """A parameter's enum (its items' enum for an array), or ``None`` when unconstrained."""
    enum = schema.get("enum")
    if enum is None and schema.get("type") == "array":
        enum = _deref(schema.get("items") or {}, components).get("enum")
    return {str(value) for value in enum} if isinstance(enum, list) else None


_ACTION_BREAK = "update cli/capacity/runai.py and gco/config/runai_api_contract.json"


def check_version(
    version: str,
    contract: Mapping[str, Any],
    operations: Mapping[tuple[str, str], Any],
    components: Mapping[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    for route in contract["routes"]:
        label = f"{route['method']} {route['path']}"
        operation = operations.get((route["method"], route["path"]))
        if operation is None:
            findings.append(
                Finding(version, label, "route not in the API reference", _ACTION_BREAK)
            )
            continue
        if operation.get("deprecated"):
            findings.append(
                Finding(
                    version,
                    label,
                    "route marked deprecated",
                    "move the reader to the replacement route before NVIDIA removes it",
                )
            )
        parameters = _parameters(operation, components)
        for name in route.get("parameters", []):
            if name not in parameters:
                findings.append(Finding(version, label, f"parameter {name} missing", _ACTION_BREAK))
        for name, values in (route.get("parameter_values") or {}).items():
            allowed = _allowed_values(parameters.get(name, {}), components)
            if allowed is None:
                continue
            for value in values:
                if value not in allowed:
                    findings.append(
                        Finding(version, label, f"{name}={value} not accepted", _ACTION_BREAK)
                    )
        request = _request_schema(operation, components)
        for field in route.get("request_fields", []):
            if not has_field(request, field, components):
                findings.append(
                    Finding(version, label, f"request field {field} missing", _ACTION_BREAK)
                )
        response = _response_schema(operation, components)
        for field in route.get("response_fields", []):
            if not has_field(response, field, components):
                findings.append(
                    Finding(version, label, f"response field {field} missing", _ACTION_BREAK)
                )
    return findings


def run(contract: Mapping[str, Any], docs_root: str, fetch: Fetch = fetch_url) -> list[Finding]:
    """Every finding across the versions the contract covers."""
    sitemap = fetch(f"{docs_root.rstrip('/')}/sitemap.xml")
    versions = published_versions(sitemap, str(contract["min_version"]))
    if not versions:
        raise ContractCheckError(
            f"the sitemap lists no API reference version from {contract['min_version']} on"
        )
    pages = sorted({str(route["page"]) for route in contract["routes"]})
    reviewed = [str(v) for v in contract.get("reviewed_versions") or []]
    newest_reviewed = max((_version_key(v) for v in reviewed), default=())
    findings: list[Finding] = []
    for version in [*versions, LATEST]:
        operations, components = openapi_index(
            fetch(page_url(docs_root, version, page)) for page in pages
        )
        if not operations:
            raise ContractCheckError(f"no OpenAPI document found in the {version} reference pages")
        version_findings = check_version(version, contract, operations, components)
        findings.extend(version_findings)
        if version != LATEST and not version_findings and _version_key(version) > newest_reviewed:
            findings.append(
                Finding(
                    version,
                    "-",
                    "published; the contract holds",
                    "add it to reviewed_versions in gco/config/runai_api_contract.json",
                )
            )
    return findings


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument(
        "--docs-root", help="documentation site root (default: the contract's docs_root)"
    )
    parser.add_argument("--format", choices=("rows", "json"), default="rows")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, fetch: Fetch = fetch_url) -> int:
    args = _parse_args(argv)
    try:
        contract = load_contract(args.contract)
        findings = run(contract, args.docs_root or str(contract["docs_root"]), fetch)
    except ContractCheckError as exc:
        print(f"Run:ai API contract check skipped: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps([finding.__dict__ for finding in findings], indent=2))
    else:
        for finding in findings:
            print(finding.row())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
