"""Dependency maintenance commands.

``gco deps scan`` wraps the repository's dependency scanner
(``.github/scripts/dependency-scan.sh``) — the same script the monthly
``deps-scan`` workflow runs to build the rolling
"[Automated] Dependency updates available" issue — so an operator or agent
can generate the exact same update list on demand instead of waiting for
the schedule or hand-assembling the invocation.

The scanner communicates through the GitHub Actions file-output protocol
(``$GITHUB_OUTPUT``); this wrapper points that at a private temp file and
reads back ``has_drift`` / ``scan_complete`` / ``report_path``, so the
script itself runs bit-for-bit the way CI runs it.

Two operating modes:

* Full scan (default) — every surface the workflow checks: Python/npm
  pins, Docker images, Helm charts, EKS add-ons, Dockerfile.dev ARGs,
  autopilot pins, pre-commit hooks, CI tooling, version consistency,
  suppression expiries, lockfile freshness, and the accelerator-catalog /
  Karpenter NodePool policy. Surfaces that need AWS credentials or tools
  the host is missing are skipped and reported as incomplete, exactly as
  in CI.
* ``--nodepools-only`` — just the accelerator-catalog / NodePool freshness
  check (``scripts/accelerator_catalog.py``): the deterministic offline
  validation always runs; the live EC2 catalog comparison runs when AWS
  credentials resolve and is reported as skipped otherwise.

Honest side-effect warning: the full scan's Python surface runs
``pip install -e ".[<every extra>]"`` into the *active environment* (that
is how it asks pip for outdated direct pins), mirroring the throwaway CI
environment. Run it from the dev container or a dedicated venv if that
matters to you.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn, cast

import click

from ..autopilot import (
    CLAUDE_CODE_PACKAGE,
    CLAUDE_CODE_VERSION,
    COMPANION_MCP_SERVERS,
    AutopilotEngine,
    build_claude_env,
    build_mcp_config,
    claude_install_command,
    config_path,
    effective_aws_region,
    exec_claude,
    find_claude_binary,
    install_claude_code,
    resolve_engine,
    resolve_model,
)
from ..config import GCOConfig
from ..maintenance import (
    DENIED_TOOL_RULES,
    PERMISSION_MODE,
    ROLLING_ISSUE_LABELS,
    ROLLING_ISSUE_TITLE,
    FindingsDocument,
    FindingsError,
    Tier,
    allowed_tool_rules,
    build_maintenance_argv,
    compose_prompt,
    default_branch_name,
    extract_embedded_findings,
    load_findings_file,
    parse_findings,
    plan_summary,
    worktree_path_for,
)
from ..output import confirm, get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)

#: Tools the full scan shells out to, and the surfaces that go incomplete
#: without them. Missing entries are warnings, not errors — the scanner
#: records the gap and keeps going, exactly as it does in CI.
_OPTIONAL_TOOLS: tuple[tuple[str, str], ...] = (
    ("jq", "Python-package report rendering"),
    ("curl", "npm / GitHub / endoflife.date lookups"),
    ("skopeo", "Docker image tag and digest checks"),
    ("helm", "Helm chart version checks"),
    ("aws", "EKS / Aurora / EMR / Bedrock / online accelerator checks"),
)

_SCAN_SCRIPT = Path(".github") / "scripts" / "dependency-scan.sh"
_CATALOG_SCRIPT = Path("scripts") / "accelerator_catalog.py"


def _fail(message: str) -> NoReturn:
    raise click.ClickException(message)


def _repo_root() -> Path:
    """Resolve the checkout root; the scanner only exists in a git checkout."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _fail(
            "gco deps scan must run from inside a GCO checkout "
            "(the dependency scanner lives under .github/scripts/)"
        )
    root = Path(result.stdout.strip())
    if not (root / _SCAN_SCRIPT).is_file():
        _fail(f"{root} has no {_SCAN_SCRIPT} — not a GCO checkout?")
    return root


def _parse_github_output(path: Path) -> dict[str, str]:
    """Parse the ``key=value`` lines the scanner writes to $GITHUB_OUTPUT."""
    outputs: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return outputs
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            outputs[key.strip()] = value.strip()
    return outputs


def _sts_identity_available() -> bool:
    """Mirror the scanner's credential preflight for the nodepools fast path."""
    if shutil.which("aws") is None:
        return False
    probe = subprocess.run(
        ["aws", "sts", "get-caller-identity"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def _run_nodepools_check(repo_root: Path) -> dict[str, object]:
    """Run the accelerator-catalog / NodePool freshness checks.

    Returns a JSON-friendly envelope with an ``offline`` section (always
    runs; deterministic) and an ``online`` section (runs when AWS
    credentials resolve, mirrors the scanner's STS preflight).
    """
    offline_report = subprocess.run(
        [sys.executable, str(_CATALOG_SCRIPT), "validate", "--format", "markdown"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if offline_report.returncode not in (0, 1):
        _fail(
            "accelerator catalog validation failed operationally: "
            + (offline_report.stderr.strip() or f"exit {offline_report.returncode}")
        )
    finding_count = len(re.findall(r"^### ", offline_report.stdout, re.M))
    offline: dict[str, object] = {
        "status": "pass" if offline_report.returncode == 0 else "findings",
        "finding_count": finding_count,
        "report_markdown": offline_report.stdout,
    }

    online: dict[str, object]
    if not _sts_identity_available():
        online = {
            "status": "skipped",
            "skip_reason": (
                "No AWS credentials available for the online EC2 catalog check "
                "(needs ec2:DescribeRegions and ec2:DescribeInstanceTypes); "
                "offline policy validation still ran."
            ),
        }
    else:
        with tempfile.TemporaryDirectory(prefix="gco-deps-") as tmp:
            online_report_path = Path(tmp) / "online.md"
            online_run = subprocess.run(
                [
                    sys.executable,
                    str(_CATALOG_SCRIPT),
                    "check-online",
                    "--report",
                    str(online_report_path),
                    "--json-summary",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if online_run.returncode not in (0, 1):
                _fail(
                    "online accelerator catalog check failed operationally: "
                    + (online_run.stderr.strip() or f"exit {online_run.returncode}")
                )
            try:
                summary = json.loads(online_run.stdout)
            except json.JSONDecodeError:
                _fail("online accelerator catalog check emitted a malformed JSON summary")
            online = {
                "status": summary.get("status", "error"),
                "drift_count": summary.get("drift_count"),
                "regions_checked": summary.get("regions_checked"),
            }
            with contextlib.suppress(OSError):
                online["report_markdown"] = online_report_path.read_text(encoding="utf-8")

    has_drift = offline["status"] != "pass" or online.get("status") == "drift"
    return {
        "nodepools_only": True,
        "has_drift": has_drift,
        "scan_complete": online.get("status") != "skipped",
        "offline": offline,
        "online": online,
    }


def _run_full_scan(repo_root: Path, *, stream: bool) -> dict[str, object]:
    """Run the full dependency scanner and return its parsed envelope."""
    missing = [
        f"{tool} ({surfaces})" for tool, surfaces in _OPTIONAL_TOOLS if shutil.which(tool) is None
    ]
    if missing:
        click.echo(
            "warning: missing tools — these surfaces will be reported as "
            "incomplete: " + "; ".join(missing),
            err=True,
        )

    with tempfile.TemporaryDirectory(prefix="gco-deps-") as tmp:
        github_output = Path(tmp) / "github-output"
        github_output.touch()
        env = dict(os.environ)
        env["GITHUB_OUTPUT"] = str(github_output)
        # Never leak into a real Actions job summary if the caller's
        # environment happens to carry one.
        env.pop("GITHUB_STEP_SUMMARY", None)
        env.setdefault("WORKFLOWS_DIR", ".github/workflows")

        result = subprocess.run(  # fixed argv, repo-owned script
            ["bash", str(_SCAN_SCRIPT)],
            cwd=repo_root,
            env=env,
            check=False,
            capture_output=not stream,
            text=True,
        )
        if result.returncode != 0:
            detail = "" if stream else f"\n{(result.stderr or '')[-2000:]}"
            _fail(f"dependency scanner exited with status {result.returncode}{detail}")

        outputs = _parse_github_output(github_output)
        has_drift = outputs.get("has_drift") == "true"
        scan_complete = outputs.get("scan_complete") == "true"

        report_markdown: str
        if has_drift:
            report_path = outputs.get("report_path", "")
            try:
                report_markdown = Path(report_path).read_text(encoding="utf-8")
            except OSError:
                _fail("the scanner reported drift but its report file is unreadable")
        else:
            report_markdown = "# Dependency Update Report\n\n" + (
                "All dependencies are up to date.\n"
                if scan_complete
                else "No drift was found in completed checks, but the scan is "
                "incomplete — zero-count surfaces are provisional. See the "
                "scan log for skipped checks.\n"
            )

        # The scanner writes the same findings as a JSON document next to the
        # report, on every run (``findings_path``). It rides along in the
        # envelope so ``gco deps maintain --scan`` and the MCP tool get the
        # rows without parsing the Markdown. Absent or unreadable is ``None``
        # rather than an error: the report is still the primary output.
        findings: object = None
        findings_path = outputs.get("findings_path", "")
        if findings_path:
            with contextlib.suppress(OSError, json.JSONDecodeError):
                findings = json.loads(Path(findings_path).read_text(encoding="utf-8"))

    envelope: dict[str, object] = {
        "has_drift": has_drift,
        "scan_complete": scan_complete,
        "report_markdown": report_markdown,
        "findings": findings,
    }
    if not stream:
        envelope["log_tail"] = (result.stdout or "").splitlines()[-40:]
    return envelope


@click.group()
def deps() -> None:
    """Dependency maintenance (update scans, NodePool registry freshness)."""


@deps.command("scan")
@click.option(
    "--nodepools-only",
    is_flag=True,
    default=False,
    help=(
        "Run only the accelerator-catalog / Karpenter NodePool freshness "
        "check instead of the full scan."
    ),
)
@click.option(
    "--report",
    "report_file",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the Markdown report to this file instead of stdout.",
)
@click.pass_context
def scan(ctx: click.Context, nodepools_only: bool, report_file: Path | None) -> None:
    """Generate the dependency update list the monthly deps-scan produces.

    Runs the same scanner as the ``deps-scan`` GitHub Actions workflow and
    prints its Markdown report, so the update list in the rolling
    "[Automated] Dependency updates available" issue can be reproduced on
    demand. Surfaces that need AWS credentials or missing host tools are
    skipped and flagged as incomplete rather than failing the run.

    With ``--nodepools-only``, runs just the accelerator catalog /
    NodePool policy checks (offline always; live EC2 comparison when AWS
    credentials resolve).

    With the global ``-o json``, prints a machine-readable envelope
    (``has_drift``, ``scan_complete``, ``report_markdown``) instead of
    the bare report — this is the shape the MCP ``deps_scan`` tool
    returns.
    """
    repo_root = _repo_root()
    json_output = bool(ctx.obj) and getattr(ctx.obj, "output_format", "table") == "json"

    if nodepools_only:
        envelope = _run_nodepools_check(repo_root)
    else:
        envelope = _run_full_scan(repo_root, stream=not json_output)

    if json_output:
        from ..output import emit_structured_document

        emit_structured_document(
            envelope,
            output_format="json",
            rendered=json.dumps(envelope, indent=2),
        )
        return

    if nodepools_only:
        offline = cast("dict[str, object]", envelope["offline"])
        online = cast("dict[str, object]", envelope["online"])
        report_markdown = str(offline.get("report_markdown", ""))
        if online.get("report_markdown"):
            report_markdown += "\n" + str(online["report_markdown"])
        click.echo(f"offline policy check: {offline['status']}", err=True)
        click.echo(f"online EC2 catalog:   {online['status']}", err=True)
    else:
        report_markdown = str(envelope["report_markdown"])
        click.echo(f"has_drift:     {envelope['has_drift']}", err=True)
        click.echo(f"scan_complete: {envelope['scan_complete']}", err=True)

    if report_file is not None:
        report_file.write_text(report_markdown, encoding="utf-8")
        click.echo(f"report:        {report_file}", err=True)
    else:
        click.echo(report_markdown)


# ---------------------------------------------------------------------------
# gco deps maintain
# ---------------------------------------------------------------------------

#: Where a maintenance session's prompt, findings copy, MCP config and plan
#: are kept for the maintainer to read back: ``<autopilot config dir>/
#: maintenance/<branch>/``.
_MAINTENANCE_DIRNAME = "maintenance"


def _gh(args: list[str], *, cwd: Path) -> str:
    """Run ``gh`` and return stdout; a failure is a ClickException with the stderr."""
    if shutil.which("gh") is None:
        _fail(
            "gh (the GitHub CLI) is required to read the dependency issue; install it or pass --findings"
        )
    result = subprocess.run(  # fixed argv, no shell
        ["gh", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        _fail(f"gh {' '.join(args[:2])} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def _rolling_issue_number(repo_root: Path) -> int:
    """The open rolling dependency issue, found by its exact title and both labels."""
    listing = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            *[arg for label in ROLLING_ISSUE_LABELS for arg in ("--label", label)],
            "--limit",
            "100",
            "--json",
            "number,title",
        ],
        cwd=repo_root,
    )
    try:
        issues = json.loads(listing)
    except json.JSONDecodeError:
        _fail("gh issue list returned something other than JSON")
    for issue in issues:
        if issue.get("title") == ROLLING_ISSUE_TITLE:
            return int(issue["number"])
    _fail(
        f"no open issue titled {ROLLING_ISSUE_TITLE!r} with labels "
        f"{', '.join(ROLLING_ISSUE_LABELS)}; the last scan was clean, or pass "
        "--issue, --findings or --scan"
    )


def _issue_findings(repo_root: Path, number: int) -> tuple[object, str]:
    """The findings embedded in issue ``number`` and the issue URL."""
    raw = _gh(["issue", "view", str(number), "--json", "body,url"], cwd=repo_root)
    try:
        issue = json.loads(raw)
    except json.JSONDecodeError:
        _fail("gh issue view returned something other than JSON")
    try:
        data = extract_embedded_findings(str(issue.get("body", "")))
    except FindingsError as exc:
        _fail(f"issue #{number}: {exc}")
    return data, str(issue.get("url") or f"issue #{number}")


def _repo_url(repo_root: Path) -> str | None:
    """The GitHub URL of this checkout, when gh can resolve it."""
    if shutil.which("gh") is None:
        return None
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "url", "--jq", ".url"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(  # fixed argv, no shell
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        _fail(f"git {' '.join(args[:2])} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def _main_checkout_root(repo_root: Path) -> Path:
    """The main checkout, even when this command runs inside a linked worktree.

    A linked worktree's ``.git`` is a file pointing at the main repository's
    ``.git`` directory; the default worktree location hangs off that
    repository, so sessions never nest worktrees inside worktrees.
    """
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo_root)
    return Path(common.strip()).parent


def _create_worktree(repo_root: Path, path: Path, branch: str) -> None:
    """A fresh branch from ``origin/main`` checked out under ``path``."""
    if path.exists():
        _fail(f"{path} already exists; pass --worktree for another path or remove it")
    _git(["fetch", "--quiet", "origin", "main"], cwd=repo_root)
    _git(
        ["worktree", "add", "--quiet", "-b", branch, str(path), "origin/main"],
        cwd=repo_root,
    )


def _session_dir(branch: str) -> Path:
    return config_path().parent / _MAINTENANCE_DIRNAME / branch.replace("/", "-")


def _companion_mcp_config(workspace: Path) -> dict[str, dict[str, dict[str, object]]]:
    """The autopilot companion servers only: no GCO server, nothing from ~/.claude."""
    full = build_mcp_config(workspace, include_companions=True)
    servers = {
        name: entry
        for name, entry in full["mcpServers"].items()
        if any(companion.name == name for companion in COMPANION_MCP_SERVERS)
    }
    return {"mcpServers": servers}


def _print_maintain_plan(plan: dict[str, object], document: FindingsDocument) -> None:
    """The table-format plan (dry run, and the confirmation before a launch)."""
    print()
    print("  GCO dependency maintenance — launch plan")
    print("  " + "-" * 68)
    print(f"  Findings:          {plan['source']}")
    state = "complete" if document.scan_complete else "INCOMPLETE"
    print(f"  Scan generated:    {document.generated_at or 'unknown'}  ({state})")
    print(f"  Worktree:          {plan['worktree']}")
    print(f"  Branch:            {plan['branch']}  (from origin/main)")
    print(f"  Engine:            Claude Code on Bedrock ({plan['model']})")
    print(
        f"  Permissions:       {plan['permission_mode']}, {plan['allowed_rules']} allow rules, "
        f"{plan['denied_rules']} deny rules"
    )
    print(
        f"  MCP servers ({len(cast('list[str]', plan['mcp_servers']))}):   "
        + (", ".join(cast("list[str]", plan["mcp_servers"])) or "(none)")
    )
    print()
    grouped = document.by_tier()
    act_on = {Tier(value) for value in cast("list[str]", plan["act_on"])}
    for tier in Tier:
        findings = grouped[tier]
        verb = "Apply" if tier in act_on else "Report only"
        print(f"  {verb} — {tier.value} ({len(findings)})")
        for finding in findings:
            print(f"    - [{finding.surface}] {finding.label()}")
    if document.skipped:
        print(f"  Skipped surfaces ({len(document.skipped)}): " + ", ".join(document.skipped))
    if document.incomplete_reasons:
        print(f"  Incomplete lookups: {len(document.incomplete_reasons)}")
    print()
    print(f"  Prompt:            {plan['prompt_path']}  ({plan['prompt_bytes']} bytes)")
    if plan["claude_binary"]:
        print(f"  Claude Code:       {plan['claude_binary']}")
    else:
        print(f"  Claude Code:       not installed — will offer: {plan['install_command']}")
    print("  " + "-" * 68)
    if plan["dry_run"]:
        print("  Dry run only — no worktree was created and nothing was launched.")
    print()


@deps.command("maintain")
@click.option(
    "--issue",
    "issue_number",
    type=int,
    default=None,
    help=(
        "Read the findings embedded in this issue. Default: the open rolling "
        f"{ROLLING_ISSUE_TITLE!r} issue."
    ),
)
@click.option(
    "--findings",
    "findings_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read the findings document from this file (the dependency-scan-findings artifact).",
)
@click.option(
    "--scan",
    "run_scan",
    is_flag=True,
    default=False,
    help="Run `gco deps scan` first and use its findings (several minutes; needs the scan's tools).",
)
@click.option(
    "--act-on",
    type=click.Choice([tier.value for tier in Tier]),
    multiple=True,
    default=(Tier.MECHANICAL.value, Tier.SEMANTIC.value),
    show_default=True,
    help="Tiers the agent may change; every other finding is reported only. Repeatable.",
)
@click.option("--branch", default=None, help="Branch name (default: maint/deps-<today>).")
@click.option(
    "--worktree",
    "worktree_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Worktree path (default: <repo>/.worktrees/<branch>).",
)
@click.option(
    "--engine",
    type=click.Choice([engine.value for engine in AutopilotEngine]),
    default=None,
    help="Agent engine. Only claude-code is supported by this command today.",
)
@click.option("--model", "-m", default=None, help="Bedrock model id override (see gco autopilot).")
@click.option(
    "--companions/--no-companions",
    default=False,
    show_default=True,
    help="Also start the autopilot companion MCP servers (documentation and search).",
)
@click.option(
    "--print",
    "print_mode",
    is_flag=True,
    default=False,
    help="Headless: run the session to completion without a TUI and exit (claude -p).",
)
@click.option(
    "--dry-run", is_flag=True, default=False, help="Show the plan and prompt; change nothing."
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Do not prompt (install, launch).")
@click.argument("engine_args", nargs=-1, type=click.UNPROCESSED)
@pass_config
def maintain(
    config: GCOConfig,
    issue_number: int | None,
    findings_file: Path | None,
    run_scan: bool,
    act_on: tuple[str, ...],
    branch: str | None,
    worktree_path: Path | None,
    engine: str | None,
    model: str | None,
    companions: bool,
    print_mode: bool,
    dry_run: bool,
    yes: bool,
    engine_args: tuple[str, ...],
) -> None:
    """Hand the dependency scan's findings to a Claude Code session.

    Reads the machine-readable findings the monthly ``deps-scan`` embeds in
    the rolling "[Automated] Dependency updates available" issue (or a
    ``--findings`` file, or a fresh ``--scan``), sorts every finding into a
    tier (mechanical, semantic, judgment; see ``docs/MAINTENANCE.md``,
    "Agent-assisted maintenance"), creates a worktree on a new branch from
    ``origin/main``, and starts Claude Code there with a prompt that applies
    the ``--act-on`` tiers, verifies with the repository's own gates, and
    opens a DRAFT pull request whose body tells you what to test. Judgment
    findings (new majors, Kubernetes and engine releases) are analysed in
    the pull request, never changed.

    The session runs with ``--permission-mode acceptEdits``, an allow list of
    the shell commands maintenance needs, and deny rules for the moves the
    repository never wants an agent to make (force pushes, merges, anything
    on ``main``, ``aws``, ``kubectl``). Arguments after ``--`` go to
    ``claude`` unchanged.

    \b
    Examples:
        gco deps maintain --dry-run
        gco deps maintain
        gco deps maintain --act-on mechanical
        gco deps maintain --findings findings.json --print
        gco deps maintain -- --max-turns 200
    """
    formatter = get_output_formatter(config)
    json_output = config.output_format != "table"
    if json_output and not dry_run:
        formatter.print_error(
            "Live maintenance sessions require terminal output. Use `--output table`, "
            "or combine machine output with `--dry-run`."
        )
        sys.exit(2)
    if sum(bool(option) for option in (issue_number is not None, findings_file, run_scan)) > 1:
        formatter.print_error("Pass only one of --issue, --findings and --scan.")
        sys.exit(1)
    try:
        resolved_engine = resolve_engine(engine)
    except ValueError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    if resolved_engine is not AutopilotEngine.CLAUDE_CODE:
        formatter.print_error(
            f"gco deps maintain supports the claude-code engine only today; "
            f"{resolved_engine.value} is not wired up."
        )
        sys.exit(1)

    repo_root = _repo_root()

    # 1. Findings.
    if findings_file is not None:
        try:
            data = load_findings_file(findings_file)
        except FindingsError as exc:
            formatter.print_error(str(exc))
            sys.exit(1)
        source = str(findings_file)
    elif run_scan:
        envelope = _run_full_scan(repo_root, stream=not json_output)
        data = envelope.get("findings")
        if data is None:
            formatter.print_error("the scan produced no findings document")
            sys.exit(1)
        source = "a live `gco deps scan` run"
    else:
        number = issue_number if issue_number is not None else _rolling_issue_number(repo_root)
        data, source = _issue_findings(repo_root, number)
    try:
        document = parse_findings(data)
    except FindingsError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)

    tiers = [Tier(value) for value in act_on]
    branch_name = branch or default_branch_name()
    worktree = (
        worktree_path or worktree_path_for(_main_checkout_root(repo_root), branch_name)
    ).expanduser()
    workspace = worktree.resolve()
    try:
        resolved_model, warnings = resolve_model(model)
    except ValueError as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
    for warning in warnings:
        formatter.print_warning(warning)
    region = effective_aws_region(config.default_region)
    prompt = compose_prompt(
        document,
        act_on=tiers,
        branch=branch_name,
        source=source,
        repo_url=_repo_url(repo_root),
    )
    mcp_config = _companion_mcp_config(workspace) if companions else {"mcpServers": {}}
    session_dir = _session_dir(branch_name)
    claude_binary = find_claude_binary()
    plan: dict[str, object] = {
        "source": source,
        "branch": branch_name,
        "worktree": str(worktree),
        "model": resolved_model,
        "region": region,
        "engine": resolved_engine.value,
        "act_on": [tier.value for tier in tiers],
        "permission_mode": PERMISSION_MODE,
        "allowed_rules": len(allowed_tool_rules(branch_name)),
        "denied_rules": len(DENIED_TOOL_RULES),
        "mcp_servers": sorted(mcp_config["mcpServers"]),
        "prompt_path": str(session_dir / "prompt.md"),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "claude_binary": claude_binary,
        "install_command": " ".join(claude_install_command()),
        "claude_code_pin": f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}",
        "print_mode": print_mode,
        "dry_run": dry_run,
        "summary": plan_summary(document, tiers),
    }
    if not document.findings:
        formatter.print_info(
            "The findings document reports nothing to apply or analyse"
            + ("" if document.scan_complete else " (but the scan was incomplete)")
            + "; there is no maintenance session to start."
        )
        if json_output:
            formatter.print(plan)
        return

    if dry_run:
        if json_output:
            formatter.print({**plan, "prompt": prompt})
        else:
            _print_maintain_plan(plan, document)
        return

    # 2. Claude Code.
    if claude_binary is None:
        formatter.print_info(f"Claude Code is not installed (pinned: {plan['claude_code_pin']}).")
        if not yes and not confirm(f"Install it now with `{plan['install_command']}`?"):
            formatter.print_error(
                f"Claude Code is required. Install it with `{plan['install_command']}` and re-run."
            )
            sys.exit(1)
        rc = install_claude_code()
        if rc != 0:
            formatter.print_error(
                "npm was not found on PATH; install Node.js/npm first."
                if rc == 127
                else f"`{plan['install_command']}` failed with exit code {rc}."
            )
            sys.exit(1)
        claude_binary = find_claude_binary()
        if claude_binary is None:
            formatter.print_error(
                "Claude Code installed but the `claude` binary is not on PATH. "
                "Open a new shell (or fix your npm global bin path) and re-run."
            )
            sys.exit(1)

    # 3. The plan, confirmed. (Machine output never reaches a launch: it is
    # refused above unless combined with --dry-run.)
    _print_maintain_plan(plan, document)
    if not yes and not confirm(f"Create the worktree and start the session on {branch_name}?"):
        formatter.print_info("Nothing created.")
        return

    # 4. Worktree and session files.
    _create_worktree(repo_root, worktree, branch_name)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        (session_dir / "findings.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        (session_dir / "mcp.json").write_text(
            json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8"
        )
        (session_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        formatter.print_error(f"Failed to write the session files under {session_dir}: {exc}")
        sys.exit(1)

    argv = build_maintenance_argv(
        claude_binary,
        mcp_config=session_dir / "mcp.json",
        prompt=prompt,
        branch=branch_name,
        print_mode=print_mode,
        extra_args=tuple(engine_args),
    )
    env = build_claude_env(resolved_model, region)
    formatter.print_info(
        f"Launching Claude Code on Bedrock ({resolved_model}) in {worktree} "
        f"(session files: {session_dir})..."
    )
    os.chdir(workspace)
    try:
        rc = exec_claude(argv, env)  # returns only on Windows
    except OSError as exc:
        formatter.print_error(
            f"Failed to launch Claude Code at {argv[0]}: {exc}. Reinstall with "
            f"`{' '.join(claude_install_command())}` and re-run."
        )
        sys.exit(1)
    sys.exit(rc)
