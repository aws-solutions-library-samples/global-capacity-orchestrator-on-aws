"""Validate every Helm chart (name, version) pinned in charts.yaml.

Confirms that each chart entry in ``lambda/helm-installer/charts.yaml`` is a
real, installable Helm chart at the pinned version — i.e. the
``(chart, version)`` combination is something Helm can actually pull and
render for a cluster, not a typo or a tag that never shipped. The
helm-installer Lambda trusts these pins blindly at deploy time, so a bad pin
today only surfaces as a failed ``helm upgrade --install`` mid-deploy; this
check moves that failure left into CI.

Two layers, matching what each CI stage can afford:

  Structural (offline, always): pure-Python checks on the parsed YAML — no
    network, no ``helm`` binary. Every entry needs a ``chart``, a SemVer-ish
    ``version`` and a ``repo_url``; an ``oci://`` ``repo_url`` must set
    ``use_oci: true`` (and vice-versa); a classic HTTP(S) repo needs a
    ``repo_name`` for ``helm repo add``. Catches the obvious mistakes before
    spending a network round-trip.

  Online (needs ``helm`` + network): for every chart, build the *same*
    reference the installer Lambda builds (see ``handler.install_chart``),
    then

      * ``helm show chart <ref> --version <ver>`` — proves Helm can pull the
        chart metadata at exactly the pinned version; and
      * ``helm template <ref> --version <ver> --values <configured>`` —
        proves the chart renders to Kubernetes manifests (installable) with
        the values ``charts.yaml`` ships.

    Every network-touching helm call (repo add/update, show chart, template)
    is issued through ``_run_with_retry``, which retries a fixed number of
    times with exponential backoff and takes the first success. An intermittent
    registry blip is ridden out in-process instead of forcing a manual job
    rerun, while a genuinely bad pin fails on every attempt and still surfaces.

By default *every* chart in the file is validated, including entries with
``enabled: false``: those are toggled on via ``cdk.json``, so their pinned
``(name, version)`` must be valid too.

Usage::

    # Structural only (no helm needed):
    python3 .github/scripts/validate_helm_charts.py --mode offline

    # Full check (requires helm on PATH + network) — the CI gate:
    python3 .github/scripts/validate_helm_charts.py --mode online

    # Auto: structural always, online when helm happens to be installed:
    python3 .github/scripts/validate_helm_charts.py

    # Point at a non-default charts.yaml (used by the test suite):
    python3 .github/scripts/validate_helm_charts.py --charts /path/to/charts.yaml

Exit codes::

    0  all validated charts are well-formed (and, when online, resolvable
       + renderable at their pinned versions)
    1  one or more charts failed validation
    2  unexpected I/O / argument error (charts.yaml missing or unparseable,
       or --mode online requested without a helm binary)

The module is importable from the test suite — call ``validate_structure()``,
``build_refs()`` or ``validate_online()`` directly to exercise the logic
against fixtures.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess  # nosec B404 - used only to invoke the pinned `helm` binary with fixed argv
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# charts.yaml lives next to the helm-installer Lambda handler. This script is
# .github/scripts/validate_helm_charts.py, so the repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CHARTS = _REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"

# Lenient SemVer-ish matcher for the pinned chart ``version``. Helm requires
# SemVer2 chart versions, so anything failing this is guaranteed to fail a real
# ``helm ... --version`` resolve too — we just catch it offline first. Accepts
# an optional leading "v" (cert-manager / aws-efa tag their charts that way)
# and an optional pre-release / build-metadata suffix.
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+){1,2}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class ChartRef:
    """Everything needed to resolve one ``charts.yaml`` entry with Helm."""

    name: str
    chart: str
    version: str
    repo_name: str
    repo_url: str
    use_oci: bool
    namespace: str
    enabled: bool
    values: dict[str, Any]

    def reference(self) -> str:
        """Build the Helm chart reference exactly like ``handler.install_chart``.

        OCI charts are addressed by their full ``oci://.../<chart>`` URL;
        classic HTTP(S) repos are addressed as ``<repo_name>/<chart>`` after
        the repo has been added with ``helm repo add``.
        """
        if self.use_oci:
            return f"{self.repo_url}/{self.chart}"
        return f"{self.repo_name}/{self.chart}"


def load_charts(path: Path) -> dict[str, Any]:
    """Parse ``charts.yaml`` and return the ``charts:`` mapping.

    Raises ``FileNotFoundError`` when the file is absent and ``ValueError``
    when the top-level ``charts:`` mapping is missing or malformed; ``main()``
    turns both into exit code 2.
    """
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    charts = data.get("charts")
    if not isinstance(charts, dict):
        raise ValueError("missing or malformed top-level 'charts:' mapping")
    return charts


def validate_structure(charts: dict[str, Any], *, enabled_only: bool = False) -> list[str]:
    """Return a list of structural problems (empty == all well-formed).

    These are the offline, pure-Python checks: enough to guarantee we can even
    build a Helm reference for each entry and that OCI/classic metadata is
    self-consistent. Every returned string names the offending chart so the
    failure is actionable from the CI log alone.
    """
    errors: list[str] = []
    if not charts:
        return ["charts.yaml contains no chart entries under 'charts:'"]

    for name, cfg in charts.items():
        if not isinstance(cfg, dict):
            errors.append(f"{name}: entry is not a mapping")
            continue
        if enabled_only and not cfg.get("enabled", False):
            continue

        chart = cfg.get("chart")
        version = cfg.get("version")
        repo_url = cfg.get("repo_url")
        repo_name = cfg.get("repo_name")
        use_oci = bool(cfg.get("use_oci", False))

        if not (isinstance(chart, str) and chart.strip()):
            errors.append(f"{name}: missing or empty 'chart'")

        if not (isinstance(version, str) and version.strip()):
            errors.append(f"{name}: missing or empty 'version'")
        elif not _VERSION_RE.match(version.strip()):
            errors.append(f"{name}: version {version!r} is not a valid SemVer chart version")

        if not (isinstance(repo_url, str) and repo_url.strip()):
            errors.append(f"{name}: missing or empty 'repo_url'")
        else:
            is_oci_url = repo_url.startswith("oci://")
            if is_oci_url and not use_oci:
                errors.append(f"{name}: repo_url is oci:// but use_oci is not set to true")
            if use_oci and not is_oci_url:
                errors.append(
                    f"{name}: use_oci is true but repo_url {repo_url!r} is not an oci:// URL"
                )
            if not use_oci and not is_oci_url:
                if not repo_url.startswith(("http://", "https://")):
                    errors.append(
                        f"{name}: classic repo_url {repo_url!r} must be http(s):// or oci://"
                    )
                if not (isinstance(repo_name, str) and repo_name.strip()):
                    errors.append(f"{name}: non-OCI chart needs a 'repo_name' for 'helm repo add'")

    return errors


def build_refs(charts: dict[str, Any], *, enabled_only: bool = False) -> list[ChartRef]:
    """Build a ``ChartRef`` for every entry well-formed enough to resolve.

    Entries too malformed to build a reference (no chart / version / repo_url,
    or a classic repo with no repo_name) are skipped here — ``validate_structure``
    already reports them, so there is no point trying to hit Helm for them.
    """
    refs: list[ChartRef] = []
    for name, cfg in charts.items():
        if not isinstance(cfg, dict):
            continue
        if enabled_only and not cfg.get("enabled", False):
            continue

        chart = cfg.get("chart")
        version = cfg.get("version")
        repo_url = cfg.get("repo_url")
        use_oci = bool(cfg.get("use_oci", False))
        repo_name = cfg.get("repo_name") or ""

        if not (isinstance(chart, str) and chart.strip()):
            continue
        if not (isinstance(version, str) and version.strip()):
            continue
        if not (isinstance(repo_url, str) and repo_url.strip()):
            continue
        if not use_oci and not str(repo_name).strip():
            continue

        values = cfg.get("values")
        refs.append(
            ChartRef(
                name=str(name),
                chart=chart.strip(),
                version=version.strip(),
                repo_name=str(repo_name).strip(),
                repo_url=repo_url.strip(),
                use_oci=use_oci,
                namespace=str(cfg.get("namespace", "default")),
                enabled=bool(cfg.get("enabled", False)),
                values=values if isinstance(values, dict) else {},
            )
        )
    return refs


def _run(cmd: list[str], env: dict[str, str], *, timeout: int = 120) -> tuple[int, str, str]:
    """Run a command with a fixed argv (never a shell), returning (rc, out, err).

    A subprocess timeout maps to ``(-1, "", "timeout: ...")`` so callers get a
    uniform failure contract instead of an exception — mirrors the same pattern
    in the helm-installer Lambda's ``run_helm``.
    """
    try:
        proc = (
            subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed argv, no shell=True
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                check=False,
            )
        )
    except subprocess.TimeoutExpired as exc:
        return -1, "", f"timeout: command exceeded {exc.timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def _run_with_retry(
    cmd: list[str],
    env: dict[str, str],
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 20.0,
    timeout: int = 120,
    verbose: bool = False,
    description: str = "",
) -> tuple[int, str, str]:
    """Run a network-touching helm command up to ``attempts`` times; first success wins.

    The command is retried on any non-zero exit, regardless of why it failed:
    the first attempt that succeeds (rc 0) is returned immediately, and if none
    do, the last failure is returned. Between attempts we sleep an exponentially
    growing delay (``base_delay`` doubling each round, capped at ``max_delay``)
    to give a blipping registry a moment to recover.

    This is the guard against intermittent registry failures (timeouts, resets,
    5xx) that otherwise force a manual rerun of the ``integration:helm:charts-valid``
    job. A genuinely bad pin fails on every attempt and still surfaces, just a
    few seconds later.
    """
    result: tuple[int, str, str] = (1, "", "")
    for attempt in range(1, attempts + 1):
        result = _run(cmd, env, timeout=timeout)
        if result[0] == 0:
            return result
        if attempt < attempts:
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if verbose:
                label = description or " ".join(cmd)
                print(
                    f"  attempt {attempt}/{attempts} failed, retrying in "
                    f"{delay:.1f}s — {label}: {_tail(result[2], 200)}"
                )
            time.sleep(delay)
    return result


def _helm_env(helm_home: Path) -> dict[str, str]:
    """Return an environment that isolates Helm's cache/config/data in a temp dir.

    Keeps the check hermetic: it never reads or clobbers a developer's real
    ``helm repo`` list, and CI starts from a clean slate every run.
    """
    env = os.environ.copy()
    env["HELM_CACHE_HOME"] = str(helm_home / "cache")
    env["HELM_CONFIG_HOME"] = str(helm_home / "config")
    env["HELM_DATA_HOME"] = str(helm_home / "data")
    return env


def _tail(text: str, limit: int = 400) -> str:
    """Trim helm stderr to the last ``limit`` chars so reports stay readable."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _chart_version_from_show(show_output: str) -> str | None:
    """Extract the ``version`` field from ``helm show chart`` YAML output."""
    try:
        meta = yaml.safe_load(show_output)
    except yaml.YAMLError:
        return None
    if isinstance(meta, dict):
        version = meta.get("version")
        if isinstance(version, str):
            return version
    return None


def _versions_match(resolved: str, requested: str) -> bool:
    """Compare versions ignoring a leading ``v`` on either side."""
    return resolved.lstrip("v") == requested.lstrip("v")


def _render_chart(
    ref: ChartRef,
    ref_str: str,
    helm_binary: str,
    env: dict[str, str],
    *,
    verbose: bool = False,
) -> str | None:
    """``helm template`` the chart with its shipped values; return an error or None.

    Rendering with the exact ``values`` block from ``charts.yaml`` proves the
    chart is installable *as GCO configures it*, not just with upstream
    defaults. No cluster is contacted, but templating a remote ``--version``
    ref still pulls the chart from its registry, so it goes through
    ``_run_with_retry`` to ride out the same transient blips as the resolve
    step.
    """
    args = [
        helm_binary,
        "template",
        ref.name,
        ref_str,
        "--version",
        ref.version,
        "--namespace",
        ref.namespace,
    ]

    values_path: str | None = None
    if ref.values:
        fd, values_path = tempfile.mkstemp(suffix=".yaml", prefix=f"{ref.name}-values-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(ref.values, fh)
        except Exception:
            os.close(fd)
            raise
        args.extend(["--values", values_path])

    try:
        rc, _out, err = _run_with_retry(
            args,
            env,
            timeout=180,
            verbose=verbose,
            description=f"helm template {ref.name}",
        )
    finally:
        if values_path:
            with contextlib.suppress(OSError):
                os.remove(values_path)

    if rc != 0:
        return f"chart failed to render (helm template): {_tail(err)}"
    return None


def validate_online(
    refs: list[ChartRef],
    *,
    helm_binary: str = "helm",
    skip_template: bool = False,
    verbose: bool = False,
) -> list[str]:
    """Resolve (and, unless skipped, render) each chart at its pinned version.

    Returns a list of human-readable error strings (empty == every chart is
    resolvable and renderable). Runs against an isolated Helm home so it is
    safe to invoke on a developer machine.
    """
    errors: list[str] = []
    if not refs:
        return errors

    with tempfile.TemporaryDirectory(prefix="gco-helm-validate-") as tmp:
        env = _helm_env(Path(tmp))

        # 1. Add every classic (non-OCI) repo once, then refresh the index.
        classic = {ref.repo_name: ref.repo_url for ref in refs if not ref.use_oci}
        for repo_name, repo_url in classic.items():
            rc, _out, err = _run_with_retry(
                [helm_binary, "repo", "add", repo_name, repo_url, "--force-update"],
                env,
                verbose=verbose,
                description=f"helm repo add {repo_name}",
            )
            if rc != 0:
                errors.append(f"helm repo add {repo_name} ({repo_url}) failed: {_tail(err)}")
        if classic:
            # Index refresh is network-bound too — retry it so a blip here
            # doesn't cascade into spurious "cannot resolve" errors below.
            _run_with_retry(
                [helm_binary, "repo", "update"],
                env,
                timeout=180,
                verbose=verbose,
                description="helm repo update",
            )

        # 2. Resolve + render each chart at exactly its pinned version.
        for ref in refs:
            ref_str = ref.reference()
            label = f"{ref.name} {ref.version} ({ref_str})"

            rc, out, err = _run_with_retry(
                [helm_binary, "show", "chart", ref_str, "--version", ref.version],
                env,
                verbose=verbose,
                description=f"helm show chart {ref.name}",
            )
            if rc != 0:
                errors.append(
                    f"{ref.name}: helm cannot resolve {ref_str} at version "
                    f"{ref.version!r}: {_tail(err)}"
                )
                if verbose:
                    print(f"FAIL  resolve  {label}")
                continue

            resolved = _chart_version_from_show(out)
            if resolved and not _versions_match(resolved, ref.version):
                errors.append(
                    f"{ref.name}: requested version {ref.version!r} but helm resolved {resolved!r}"
                )
            if verbose:
                print(f"ok    resolve  {label}")

            if skip_template:
                continue

            render_error = _render_chart(ref, ref_str, helm_binary, env, verbose=verbose)
            if render_error:
                errors.append(f"{ref.name}: {render_error}")
                if verbose:
                    print(f"FAIL  render   {label}")
            elif verbose:
                print(f"ok    render   {label}")

    return errors


# ---------------------------------------------------------------------------
# Gateway API / aws-load-balancer-controller lockstep
#
# The controller is built against an exact ``sigs.k8s.io/gateway-api`` release
# (declared in its go.mod) and its own gateway CRDs ship per controller tag.
# GCO pins three coupled artifacts in two different files:
#
#   * the aws-load-balancer-controller chart version (charts.yaml, this file's
#     usual input),
#   * the ``gateway-api-standard-vX.Y.Z`` CRD bundle, and
#   * the ``aws-lbc-gateway-vX.Y.Z`` CRD bundle
#     (both in lambda/helm-installer/handler.py PINNED_GATEWAY_CRD_BUNDLES).
#
# When the chart moved to 3.5.0 while the standard bundle stayed at v1.5.0,
# the controller silently stopped reconciling gateways — nothing failed until
# the live release validation deployed the pair. These checks encode the
# contract so the drift fails CI instead:
#
#   offline: the aws-lbc-gateway bundle version must equal the pinned chart
#     version (they ship from the same controller tag).
#   online: the controller tag's go.mod names its required gateway-api
#     release; the pinned standard bundle must be at least that (major.minor).
# ---------------------------------------------------------------------------

_HANDLER_PATH = _REPO_ROOT / "lambda" / "helm-installer" / "handler.py"
_LBC_CHART_KEY = "aws-load-balancer-controller"
_GATEWAY_API_BUNDLE_RE = re.compile(r'name="gateway-api-standard-v(\d+\.\d+\.\d+)"')
_LBC_BUNDLE_RE = re.compile(r'name="aws-lbc-gateway-v(\d+\.\d+\.\d+)"')
_GO_MOD_GATEWAY_API_RE = re.compile(r"^\s*sigs\.k8s\.io/gateway-api\s+v(\d+\.\d+\.\d+)", re.M)
_LBC_GO_MOD_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/kubernetes-sigs/"
    "aws-load-balancer-controller/v{version}/go.mod"
)
_GO_MOD_FETCH_ATTEMPTS = 3
_GO_MOD_FETCH_TIMEOUT_SECONDS = 15


def parse_pinned_gateway_bundles(handler_source: str) -> tuple[str | None, str | None]:
    """Return (gateway-api standard bundle version, aws-lbc bundle version)."""
    gateway_api = _GATEWAY_API_BUNDLE_RE.search(handler_source)
    lbc = _LBC_BUNDLE_RE.search(handler_source)
    return (
        gateway_api.group(1) if gateway_api else None,
        lbc.group(1) if lbc else None,
    )


def gateway_api_requirement_from_go_mod(go_mod_text: str) -> str | None:
    """Return the gateway-api release the controller's go.mod declares."""
    match = _GO_MOD_GATEWAY_API_RE.search(go_mod_text)
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def fetch_lbc_go_mod(controller_version: str) -> str:
    """Fetch the controller tag's go.mod, retrying transient HTTP failures."""
    import urllib.error
    import urllib.request

    url = _LBC_GO_MOD_URL_TEMPLATE.format(version=controller_version)
    last_error: Exception | None = None
    for attempt in range(1, _GO_MOD_FETCH_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed https host
                url, timeout=_GO_MOD_FETCH_TIMEOUT_SECONDS
            ) as response:
                return str(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < _GO_MOD_FETCH_ATTEMPTS:
                time.sleep(2**attempt)
    raise RuntimeError(f"could not fetch {url}: {last_error}")


def validate_gateway_lockstep(
    charts: dict[str, Any],
    *,
    handler_source: str | None = None,
    go_mod_fetcher: Any = None,
    online: bool = False,
    require_entry: bool = True,
) -> list[str]:
    """Check the chart / CRD-bundle lockstep contract; return error strings.

    Offline: the ``aws-lbc-gateway`` bundle version must equal the pinned
    chart version. Online (adds one HTTPS fetch): the pinned standard
    gateway-api bundle must satisfy the requirement in the controller tag's
    go.mod. With ``require_entry`` (the default, used for the repository's
    real charts.yaml) a missing chart entry or renamed handler constants are
    reported rather than skipped — this guard exists precisely for refactors
    that move them. ``main()`` disables ``require_entry`` for explicitly
    supplied ``--charts`` fixture files that legitimately omit the entry.
    """
    errors: list[str] = []
    entry = charts.get(_LBC_CHART_KEY)
    if not isinstance(entry, dict) or not entry.get("version"):
        if require_entry:
            return [f"gateway lockstep: no {_LBC_CHART_KEY!r} entry with a version in charts.yaml"]
        return []
    chart_version = str(entry["version"]).lstrip("v")

    if handler_source is None:
        try:
            handler_source = _HANDLER_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            return [f"gateway lockstep: cannot read {_HANDLER_PATH}: {exc}"]

    gateway_api_pin, lbc_pin = parse_pinned_gateway_bundles(handler_source)
    if gateway_api_pin is None or lbc_pin is None:
        return [
            "gateway lockstep: PINNED_GATEWAY_CRD_BUNDLES in "
            "lambda/helm-installer/handler.py no longer names a "
            "'gateway-api-standard-vX.Y.Z' and an 'aws-lbc-gateway-vX.Y.Z' "
            "bundle; update this check alongside any rename"
        ]

    if lbc_pin != chart_version:
        errors.append(
            f"gateway lockstep: aws-lbc-gateway CRD bundle v{lbc_pin} does not match "
            f"the pinned {_LBC_CHART_KEY} chart {chart_version} — the controller's "
            "gateway CRDs ship per controller tag; bump both together "
            "(charts.yaml + PINNED_GATEWAY_CRD_BUNDLES in lambda/helm-installer/handler.py)"
        )

    if not online:
        return errors

    fetcher = go_mod_fetcher or fetch_lbc_go_mod
    try:
        go_mod_text = fetcher(chart_version)
    except RuntimeError as exc:
        errors.append(f"gateway lockstep: {exc}")
        return errors

    required = gateway_api_requirement_from_go_mod(go_mod_text)
    if required is None:
        errors.append(
            f"gateway lockstep: go.mod for {_LBC_CHART_KEY} v{chart_version} does not "
            "declare sigs.k8s.io/gateway-api — upstream layout changed; update this check"
        )
        return errors

    if _version_tuple(gateway_api_pin)[:2] < _version_tuple(required)[:2]:
        errors.append(
            f"gateway lockstep: {_LBC_CHART_KEY} {chart_version} is built against "
            f"gateway-api v{required} (its go.mod), but the pinned standard CRD bundle "
            f"is v{gateway_api_pin}. Upgrading the controller without its Gateway API "
            "CRDs silently stops gateway reconciliation (caught live, 2026-08). Bump "
            "'gateway-api-standard' in lambda/helm-installer/handler.py to at least "
            f"v{required} in the same change"
        )
    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--charts",
        type=Path,
        default=_DEFAULT_CHARTS,
        help="Path to charts.yaml (defaults to lambda/helm-installer/charts.yaml).",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "offline", "online"),
        default="auto",
        help=(
            "auto (default): structural checks always, online checks when helm "
            "is on PATH. offline: structural only. online: require helm and run "
            "the resolve/render checks (fail if helm is missing)."
        ),
    )
    parser.add_argument(
        "--skip-template",
        action="store_true",
        help="Online mode only: resolve each chart but skip the helm template render.",
    )
    parser.add_argument(
        "--enabled-only",
        action="store_true",
        help="Validate only charts with enabled: true (default: validate every entry).",
    )
    parser.add_argument(
        "--helm-binary",
        default="helm",
        help="Helm executable to use (default: 'helm' on PATH).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print a per-chart resolve/render line during the online pass.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        charts = load_charts(args.charts)
    except FileNotFoundError:
        print(f"ERROR: charts file not found: {args.charts}", file=sys.stderr)
        return 2
    except (yaml.YAMLError, ValueError) as exc:
        print(f"ERROR: could not parse {args.charts}: {exc}", file=sys.stderr)
        return 2

    errors = validate_structure(charts, enabled_only=args.enabled_only)

    helm_available = shutil.which(args.helm_binary) is not None
    run_online = False
    if args.mode == "online":
        if not helm_available:
            print(
                f"ERROR: --mode online requires the '{args.helm_binary}' binary on PATH.",
                file=sys.stderr,
            )
            return 2
        run_online = True
    elif args.mode == "auto":
        run_online = helm_available
        if not helm_available:
            print(
                "note: 'helm' not found on PATH; running structural checks only. "
                "Pass --mode online in CI to require the resolve/render checks."
            )

    errors.extend(
        validate_gateway_lockstep(
            charts,
            online=run_online,
            # Fixture chart files passed via --charts may legitimately omit
            # the controller entry; the repository's real charts.yaml may not.
            require_entry=args.charts.resolve() == _DEFAULT_CHARTS.resolve(),
        )
    )

    refs = build_refs(charts, enabled_only=args.enabled_only)
    if run_online:
        errors.extend(
            validate_online(
                refs,
                helm_binary=args.helm_binary,
                skip_template=args.skip_template,
                verbose=args.verbose,
            )
        )

    if errors:
        print()
        print(f"ERROR: {len(errors)} Helm chart validation problem(s) found:")
        for err in errors:
            print(f"  - {err}")
        print()
        print(
            "Fix the pinned chart name/version in lambda/helm-installer/charts.yaml "
            "so every entry is a real, installable Helm chart."
        )
        return 1

    scope = "enabled" if args.enabled_only else "all"
    if run_online:
        rendered = "" if args.skip_template else " + rendered"
        print(
            f"OK: {len(refs)} Helm chart(s) ({scope}) are well-formed and "
            f"resolvable{rendered} at their pinned versions."
        )
    else:
        print(f"OK: {len(refs)} Helm chart(s) ({scope}) are structurally valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
