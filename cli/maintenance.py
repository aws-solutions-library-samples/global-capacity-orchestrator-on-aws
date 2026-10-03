"""Agent-assisted dependency maintenance: findings, tiers, prompt and launch plan.

``gco deps maintain`` takes the findings document the dependency scan writes
(``.github/scripts/dependency-scan.sh``; the JSON the rolling
"[Automated] Dependency updates available" issue embeds), sorts every finding
into a tier, composes a prompt, and starts Claude Code in a fresh worktree
with it. The agent applies what its tier allows, verifies with the
repository's own gates, and opens a draft pull request whose body tells the
maintainer what to test. The scan reports facts; this module holds the
policy, and ``docs/MAINTENANCE.md`` ("Agent-assisted maintenance") is the
human-readable copy of the same table.

Tiers, by blast radius:

* ``mechanical`` — a pin with lockstep copies and a test that proves the
  copies agree: Python and npm pins, Dockerfile ``ARG``s, action pins,
  security epochs, expired suppressions, digest refreshes. The agent
  applies these and CI is sufficient proof.
* ``semantic`` — a change whose correctness only a running system shows:
  Helm chart minors, EKS add-ons, base images, runner images, Bedrock model
  defaults. The agent applies these too; the kind jobs and the maintainer's
  review decide.
* ``judgment`` — a new major, a Kubernetes or engine version, a Spark 3 to
  Spark 4 move. The agent writes the analysis and a test plan into the pull
  request and changes nothing.

A finding on a mechanical surface whose version jumps a major is promoted
to semantic; on a semantic surface, to judgment.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Schema the scan writes; anything else is refused rather than guessed at.
#: Kept in lockstep with ``FINDINGS_SCHEMA`` in lib_dependency_scan.sh.
FINDINGS_SCHEMA = "gco.dependency-scan.findings/1"

#: Markers around the document inside the issue body (HTML comments, so
#: they render as nothing). Kept in lockstep with the shell constants.
FINDINGS_BEGIN_MARKER = "<!-- gco-deps-findings:begin -->"
FINDINGS_END_MARKER = "<!-- gco-deps-findings:end -->"

#: The rolling issue the deps-scan workflow keeps, found by exact title.
ROLLING_ISSUE_TITLE = "[Automated] Dependency updates available"
ROLLING_ISSUE_LABELS = ("dependencies", "automated")

#: Where the maintainer's procedures live; every policy anchor is a heading there.
MAINTENANCE_DOC = "docs/MAINTENANCE.md"


class Tier(StrEnum):
    """What the agent may do with a finding."""

    MECHANICAL = "mechanical"
    SEMANTIC = "semantic"
    JUDGMENT = "judgment"


#: Ascending blast radius; promotion moves one step up.
_TIER_ORDER = (Tier.MECHANICAL, Tier.SEMANTIC, Tier.JUDGMENT)


def promote(tier: Tier) -> Tier:
    """The next tier up; judgment stays judgment."""
    index = _TIER_ORDER.index(tier)
    return _TIER_ORDER[min(index + 1, len(_TIER_ORDER) - 1)]


@dataclass(frozen=True)
class SurfacePolicy:
    """How findings on one scan surface are handled.

    ``tier`` is the default; ``promote_on_major`` moves a finding one tier up
    when its version jumps a major (``None`` for surfaces whose values are
    not versions). ``procedure`` is the ``docs/MAINTENANCE.md`` anchor the
    agent must read first, and ``verify`` names what proves the change, in
    the words the maintainer reads back in the pull request.
    """

    tier: Tier
    procedure: str
    verify: str
    promote_on_major: bool = True


#: One row per scan surface, named exactly as the scan's summary table.
SURFACE_POLICIES: Mapping[str, SurfacePolicy] = {
    "Python Packages": SurfacePolicy(
        Tier.MECHANICAL,
        "#routine-dependency-bumps",
        "unit tests of the importing modules; the regenerated lock passes the CI "
        "freshness check; the Lambda copies agree (test_integration "
        "DependencyVersionConsistency)",
    ),
    "npm Packages": SurfacePolicy(
        Tier.MECHANICAL,
        "#routine-dependency-bumps",
        "the graph's package-lock.json regenerated with the pinned npm; "
        "unit:node:inference-streaming-proxy for the Lambda graph; "
        "security:npm-audit for both graphs",
    ),
    "Docker Images": SurfacePolicy(
        Tier.MECHANICAL,
        "#routine-dependency-bumps",
        "the digest check in test_supply_chain_integrity; the kind job that runs "
        "the image (cluster-e2e, examples-smoke or platform-addons) and "
        "security:trivy:container-scan for a service base image",
    ),
    "Helm Charts": SurfacePolicy(
        Tier.SEMANTIC,
        "#routine-dependency-bumps",
        "integration:kind:platform-addons installs the chart against the shipped "
        "values; test_helm_charts_validation pins the chart table",
    ),
    "EKS Add-ons": SurfacePolicy(
        Tier.SEMANTIC,
        "#validating-add-on-versions",
        "the add-on resolves for the pinned Kubernetes minor (the scan already "
        "asked EKS); a live release validation run exercises the real add-on",
    ),
    "EKS Kubernetes Version": SurfacePolicy(
        Tier.JUDGMENT,
        "#upgrading-the-eks-kubernetes-version",
        "the documented upgrade sequence: files in order, add-on compatibility, "
        "version-skew rules, deploy and verify",
        promote_on_major=False,
    ),
    "Aurora PostgreSQL Engine": SurfacePolicy(
        Tier.SEMANTIC,
        "#routine-dependency-bumps",
        "test_regional_stack renders the engine version; a live release "
        "validation run upgrades the cluster in place",
    ),
    "EMR Serverless": SurfacePolicy(
        Tier.SEMANTIC,
        "#routine-dependency-bumps",
        "test_analytics_stack renders the label; the analytics example jobs run "
        "against a deployed analytics environment (a new major is a Spark major "
        "for every notebook and example job)",
    ),
    "Bedrock Default Model": SurfacePolicy(
        Tier.SEMANTIC,
        "#refreshing-the-bedrock-default-model",
        "the scaffold fixtures re-captured for the keys the procedure names; "
        "test_default_bedrock_model_consistency; an embedding model change "
        "needs a re-embedding plan, not a bump",
        promote_on_major=False,
    ),
    "Accelerator Catalog and NodePools": SurfacePolicy(
        Tier.SEMANTIC,
        "#reviewing-and-refreshing-catalog-drift",
        "python scripts/accelerator_catalog.py validate after the refresh; "
        "the NodePool and watch-list tests",
        promote_on_major=False,
    ),
    "Dockerfile.dev Pins": SurfacePolicy(
        Tier.MECHANICAL,
        "#routine-dependency-bumps",
        "integration:docker:dev-container builds the image on both "
        "architectures; verify_container_tool_versions.py checks each tool",
    ),
    "GCO Autopilot Pins": SurfacePolicy(
        Tier.MECHANICAL,
        "#routine-dependency-bumps",
        "test_cli_autopilot pins the literal versions; the autopilot boot probes "
        "install and start each engine in CI",
    ),
    "Pre-commit Hooks": SurfacePolicy(
        Tier.MECHANICAL,
        "#routine-dependency-bumps",
        "pre-commit run --all-files; the version-consistency rows for ruff",
    ),
    "CDK Enum Constants": SurfacePolicy(
        Tier.SEMANTIC,
        "#routine-dependency-bumps",
        "unit:cdk:config-matrix synthesizes every configuration; a Lambda "
        "runtime change needs the matching Python or Node release decision",
        promote_on_major=False,
    ),
    "Python Release": SurfacePolicy(
        Tier.JUDGMENT,
        "#routine-dependency-bumps",
        "wait for the Lambda managed runtime; then the Python version is "
        "coupled to LAMBDA_PYTHON_RUNTIME and the Lambda base images",
        promote_on_major=False,
    ),
    "Ruby Release": SurfacePolicy(
        Tier.JUDGMENT,
        "#routine-dependency-bumps",
        "ruby/setup-ruby publishes the series and bashcov supports it; unit:bats:shell",
        promote_on_major=False,
    ),
    "Runner Images": SurfacePolicy(
        Tier.SEMANTIC,
        "#ci-pipeline-maintenance",
        "every workflow on the new image, with attention to the tools the image "
        "ships (Docker, Podman, lcov, /tmp) and the runner allowlist contract tests",
        promote_on_major=False,
    ),
    "CI Tooling": SurfacePolicy(
        Tier.MECHANICAL,
        "#ci-pipeline-maintenance",
        "the job that installs the tool runs green; a Trivy bump re-reads the suppression files",
    ),
    "Version Consistency": SurfacePolicy(
        Tier.MECHANICAL,
        "#routine-dependency-bumps",
        "the scan's own consistency rows clear (gco deps scan)",
        promote_on_major=False,
    ),
    "Base-image Security Epochs": SurfacePolicy(
        Tier.MECHANICAL,
        "#refreshing-base-image-security-patches",
        "security:trivy:container-scan on the rebuilt images",
        promote_on_major=False,
    ),
    "Suppression Expiries": SurfacePolicy(
        Tier.MECHANICAL,
        "#renewing-cve-suppressions",
        "each entry re-checked upstream: dropped when fixed, re-dated with a "
        "fresh justification when not; the validator tests",
        promote_on_major=False,
    ),
    "Lockfile Freshness": SurfacePolicy(
        Tier.MECHANICAL,
        "#updating-a-dependency",
        "the lock regenerated inside the documented container; the CI freshness check",
        promote_on_major=False,
    ),
}

#: A surface the scan adds before this table learns about it is handled as
#: judgment: reported, never acted on.
UNKNOWN_SURFACE_POLICY = SurfacePolicy(
    Tier.JUDGMENT,
    "#routine-dependency-bumps",
    "a surface this policy table does not know; read the report and decide",
    promote_on_major=False,
)


class FindingsError(ValueError):
    """The findings document cannot be used."""


@dataclass(frozen=True)
class Finding:
    """One finding, as the scan wrote it, with the tier the policy assigns."""

    surface: str
    urgency: str
    tier: Tier
    fields: Mapping[str, str]
    procedure: str
    verify: str
    note: str = ""

    def label(self) -> str:
        """The row as one line.

        ``name: current -> latest (key=value ...)`` when the row carries a
        version pair; otherwise every field as ``key=value``. The first field
        that is not the version pair names the finding.
        """
        fields = dict(self.fields)
        current = fields.pop("current", None)
        latest = fields.pop("latest", None)
        if current is not None and latest is not None:
            name = next((str(value) for value in fields.values() if str(value)), self.surface)
            rest = [f"{key}={value}" for key, value in list(fields.items())[1:] if str(value)]
            text = f"{name}: {current} -> {latest}"
            if rest:
                text += " (" + ", ".join(rest) + ")"
        else:
            text = ", ".join(f"{key}={value}" for key, value in fields.items() if str(value))
        return f"{text} [{self.note}]" if self.note else text


@dataclass(frozen=True)
class FindingsDocument:
    """A parsed findings document."""

    generated_at: str
    scan_complete: bool
    has_drift: bool
    incomplete_reasons: tuple[str, ...]
    skipped: Mapping[str, str]
    findings: tuple[Finding, ...]

    def by_tier(self) -> dict[Tier, list[Finding]]:
        grouped: dict[Tier, list[Finding]] = {tier: [] for tier in _TIER_ORDER}
        for finding in self.findings:
            grouped[finding.tier].append(finding)
        return grouped


_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def is_major_bump(current: str, latest: str) -> bool:
    """True when the first number of each version differs.

    The first run of digits is the version's leading component whatever the
    prefix: ``v1.36.4``, ``emr-spark-8.1.0`` and ``PYTHON_3_14`` all yield
    their major. Values without a number (model ids, status words) never
    count as a major bump.
    """
    first = _VERSION_RE.search(current)
    second = _VERSION_RE.search(latest)
    if first is None or second is None:
        return False
    return first.group(1).split(".")[0] != second.group(1).split(".")[0]


def classify(surface: str, fields: Mapping[str, str]) -> tuple[Tier, str]:
    """The tier for one finding and the note explaining a promotion."""
    policy = SURFACE_POLICIES.get(surface, UNKNOWN_SURFACE_POLICY)
    tier = policy.tier
    note = ""
    if surface not in SURFACE_POLICIES:
        note = "surface unknown to the maintenance policy"
    elif policy.promote_on_major and is_major_bump(
        str(fields.get("current", "")), str(fields.get("latest", ""))
    ):
        tier = promote(tier)
        note = f"major version change, promoted to {tier.value}"
    return tier, note


def _as_mapping(value: object, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FindingsError(message)
    return value


def _as_list(value: object, message: str) -> list[Any]:
    if not isinstance(value, list):
        raise FindingsError(message)
    return value


def parse_findings(data: object) -> FindingsDocument:
    """Validate and classify a findings document."""
    document = _as_mapping(data, "findings document is not a JSON object")
    schema = document.get("schema")
    if schema != FINDINGS_SCHEMA:
        raise FindingsError(
            f"findings schema {schema!r} is not {FINDINGS_SCHEMA!r}; "
            "the scan and this CLI are from different versions"
        )
    surfaces = _as_list(document.get("surfaces"), "findings document has no surfaces list")
    skipped: dict[str, str] = {}
    findings: list[Finding] = []
    for raw_record in surfaces:
        record = _as_mapping(raw_record, "a surface record is not an object")
        surface = record.get("surface")
        if not isinstance(surface, str) or not surface:
            raise FindingsError("a surface record has no name")
        if record.get("skipped"):
            skipped[surface] = str(record["skipped"])
        rows = _as_list(record.get("findings", []), f"surface {surface!r} findings is not a list")
        policy = SURFACE_POLICIES.get(surface, UNKNOWN_SURFACE_POLICY)
        urgency = str(record.get("urgency", ""))
        for raw_row in rows:
            row = _as_mapping(raw_row, f"a finding on {surface!r} is not an object")
            fields = {str(key): str(value) for key, value in row.items()}
            tier, note = classify(surface, fields)
            findings.append(
                Finding(
                    surface=surface,
                    urgency=urgency,
                    tier=tier,
                    fields=fields,
                    procedure=policy.procedure,
                    verify=policy.verify,
                    note=note,
                )
            )
        # The accelerator surface counts findings it reports as Markdown, not
        # rows; carry one finding so the agent is pointed at the report.
        count = record.get("count", 0)
        if not rows and isinstance(count, int) and count > 0 and not record.get("skipped"):
            tier, note = classify(surface, {})
            findings.append(
                Finding(
                    surface=surface,
                    urgency=urgency,
                    tier=tier,
                    fields={"findings": f"{count} finding(s) in the surface's Markdown report"},
                    procedure=policy.procedure,
                    verify=policy.verify,
                    note=note,
                )
            )
    reasons = _as_list(document.get("incomplete_reasons", []), "incomplete_reasons is not a list")
    return FindingsDocument(
        generated_at=str(document.get("generated_at", "")),
        scan_complete=bool(document.get("scan_complete", False)),
        has_drift=bool(document.get("has_drift", False)),
        incomplete_reasons=tuple(str(reason) for reason in reasons),
        skipped=skipped,
        findings=tuple(findings),
    )


def extract_embedded_findings(issue_body: str) -> object:
    """The findings JSON embedded in a report between the two markers.

    The report wraps the document in a fenced ``json`` block; the fence
    lines are dropped. A body without both markers (an older report, or one
    whose document was too large to embed) is a :class:`FindingsError` that
    names the alternatives.
    """
    begin = issue_body.find(FINDINGS_BEGIN_MARKER)
    end = issue_body.find(FINDINGS_END_MARKER)
    if begin < 0 or end < 0 or end < begin:
        raise FindingsError(
            "the issue body carries no embedded findings document (an older report, "
            "or one too large to embed); pass --findings with the "
            "dependency-scan-findings artifact or run with --scan"
        )
    inner = issue_body[begin + len(FINDINGS_BEGIN_MARKER) : end]
    lines = [line for line in inner.strip().splitlines() if not line.strip().startswith("```")]
    try:
        return json.loads("\n".join(lines))
    except json.JSONDecodeError as exc:
        raise FindingsError(f"the embedded findings document is not valid JSON: {exc}") from exc


def load_findings_file(path: Path) -> object:
    """A findings document from a file (the workflow artifact or a local scan)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FindingsError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FindingsError(f"{path} is not valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# The launch: permissions, prompt, argv
# ---------------------------------------------------------------------------

#: Claude Code permission mode for the session: file edits inside the
#: worktree are accepted; every shell command still goes through the rules.
PERMISSION_MODE = "acceptEdits"

#: Shell commands the maintenance session may run without asking. Rules are
#: convenience, not a security boundary (Claude Code's own documentation
#: says so): the deny list below is what blocks the moves this repository
#: never wants an agent to make. ``{branch}`` is the session's branch, so
#: the one push the agent may make is to it.
ALLOWED_BASH_RULES: tuple[str, ...] = (
    "Bash(git status *)",
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(git show *)",
    "Bash(git add *)",
    "Bash(git commit *)",
    "Bash(git restore *)",
    "Bash(git stash *)",
    "Bash(git fetch *)",
    "Bash(git ls-files *)",
    "Bash(git push -u origin {branch})",
    "Bash(gh pr create *)",
    "Bash(gh pr view *)",
    "Bash(gh pr edit *)",
    "Bash(gh issue view *)",
    "Bash(gh api *)",
    "Bash(python *)",
    "Bash(python3 *)",
    "Bash(pytest *)",
    "Bash(ruff *)",
    "Bash(mypy *)",
    "Bash(bandit *)",
    "Bash(bats *)",
    "Bash(shellcheck *)",
    "Bash(actionlint *)",
    "Bash(yamllint *)",
    "Bash(markdownlint-cli2 *)",
    "Bash(npx markdownlint-cli2 *)",
    "Bash(npm *)",
    "Bash(pip *)",
    "Bash(pip-compile *)",
    "Bash(pip-audit *)",
    "Bash(pre-commit *)",
    "Bash(docker build *)",
    "Bash(docker run *)",
    "Bash(docker pull *)",
    "Bash(podman build *)",
    "Bash(podman run *)",
    "Bash(podman pull *)",
    "Bash(trivy *)",
    "Bash(bash .github/scripts/*)",
    "Bash(bash scripts/*)",
    "Bash(bash tests/*)",
    "Bash(curl -fsSL *)",
    "Bash(jq *)",
)

#: Documentation hosts the agent may read without asking, for changelogs.
ALLOWED_FETCH_DOMAINS: tuple[str, ...] = (
    "github.com",
    "raw.githubusercontent.com",
    "pypi.org",
    "www.npmjs.com",
    "registry.npmjs.org",
    "artifacthub.io",
    "docs.aws.amazon.com",
    "aws.amazon.com",
    "endoflife.date",
    "hub.docker.com",
    "gallery.ecr.aws",
    "quay.io",
)

#: Moves no maintenance session may make. Deny rules win over allow rules,
#: so these hold even against a broader passthrough allow.
DENIED_TOOL_RULES: tuple[str, ...] = (
    "Bash(git push --force*)",
    "Bash(git push -f *)",
    "Bash(git push origin main*)",
    "Bash(git push -u origin main*)",
    "Bash(git merge *)",
    "Bash(git rebase *)",
    "Bash(git reset --hard *)",
    "Bash(git checkout main*)",
    "Bash(git switch main*)",
    "Bash(git branch -D *)",
    "Bash(git worktree remove *)",
    "Bash(gh pr merge *)",
    "Bash(gh release *)",
    "Bash(rm -rf *)",
    "Bash(aws *)",
    "Bash(cdk deploy *)",
    "Bash(kubectl *)",
    "Bash(helm install *)",
    "Bash(helm upgrade *)",
)


def allowed_tool_rules(branch: str) -> tuple[str, ...]:
    """The allow rules for one session's branch."""
    bash = tuple(rule.format(branch=branch) for rule in ALLOWED_BASH_RULES)
    fetch = tuple(f"WebFetch(domain:{domain})" for domain in ALLOWED_FETCH_DOMAINS)
    return (*bash, *fetch, "WebSearch")


def default_branch_name(today: _dt.date | None = None) -> str:
    """``maint/deps-YYYY-MM-DD``."""
    day = today or _dt.datetime.now(tz=_dt.UTC).date()
    return f"maint/deps-{day.isoformat()}"


def worktree_path_for(repo_root: Path, branch: str) -> Path:
    """``<repo>/.worktrees/<branch with slashes as dashes>``."""
    return repo_root / ".worktrees" / branch.replace("/", "-")


def _finding_lines(findings: Iterable[Finding]) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        lines.append(
            f"- [{finding.surface}] {finding.label()}"
            f"  (urgency: {finding.urgency}; procedure: {MAINTENANCE_DOC}{finding.procedure})"
        )
    return lines


def compose_prompt(
    document: FindingsDocument,
    *,
    act_on: Sequence[Tier],
    branch: str,
    source: str,
    repo_url: str | None = None,
) -> str:
    """The prompt the session starts with.

    ``act_on`` are the tiers the agent may change; every other finding is
    reported only. ``source`` names where the findings came from (the issue
    URL, the artifact path, or a live scan) so the pull request can cite it.
    """
    grouped = document.by_tier()
    acting = [tier for tier in _TIER_ORDER if tier in act_on]
    reporting = [tier for tier in _TIER_ORDER if tier not in act_on]
    apply_lines: list[str] = []
    for tier in acting:
        if grouped[tier]:
            apply_lines.append(f"### {tier.value} ({len(grouped[tier])})")
            apply_lines.extend(_finding_lines(grouped[tier]))
            apply_lines.append("")
    report_lines: list[str] = []
    for tier in reporting:
        if grouped[tier]:
            report_lines.append(f"### {tier.value} ({len(grouped[tier])})")
            report_lines.extend(_finding_lines(grouped[tier]))
            report_lines.append("")
    verify_lines = sorted(
        {f"- {finding.surface}: {finding.verify}" for tier in acting for finding in grouped[tier]}
    )
    skipped_lines = [f"- {surface}: {reason}" for surface, reason in document.skipped.items()]
    incomplete_lines = [f"- {reason}" for reason in document.incomplete_reasons]
    scan_state = "complete" if document.scan_complete else "INCOMPLETE"
    repo_line = f"Repository: {repo_url}\n" if repo_url else ""

    parts = [
        "# Dependency maintenance session",
        "",
        "You are performing the monthly dependency maintenance for this repository "
        "from the dependency scan's findings. Work only in this worktree, on the "
        f"branch `{branch}`. The deliverable is one draft pull request; a human "
        "maintainer reviews and merges it. Never merge, never touch `main`.",
        "",
        f"{repo_line}Findings source: {source}",
        f"Scan generated: {document.generated_at or 'unknown'} ({scan_state})",
        "",
        "## Before you change anything",
        "",
        f"1. Read `{MAINTENANCE_DOC}` in full, then `CONTRIBUTING.md`. Every finding "
        "below names the procedure section that applies to it; follow that section, "
        "not your own recollection of how such bumps usually go.",
        "2. Read `.github/CI.md` for what each CI job proves and which contract tests "
        "pin workflow shapes.",
        "3. Run `git status` and confirm the worktree is clean and on the branch above.",
        "",
        "## Findings to apply",
        "",
        "Apply each of these, following its procedure. Make one commit per surface "
        "(or per finding when a surface mixes unrelated pins) whose message names the "
        "finding and why the new version is safe. Where a version has lockstep copies "
        "(pyproject.toml and lambda/*/requirements.txt and requirements-lock.txt; "
        "Dockerfile ARGs; workflow env pins; gco/stacks/constants.py), move every copy "
        "in the same commit.",
        "",
        *(apply_lines or ["(none)", ""]),
        "## Findings to report only",
        "",
        "Do NOT change these. For each, write a short analysis into the pull request "
        "body: what the newer version changes, which parts of this repository it "
        "touches, what would have to be tested, and your recommendation. A new major "
        "line, a Kubernetes minor, a Python or Ruby release and a Spark major are "
        "decisions the maintainer makes.",
        "",
        *(report_lines or ["(none)", ""]),
        "## Surfaces the scan skipped",
        "",
        *(skipped_lines or ["(none)"]),
        "",
        "## Lookups the scan could not complete",
        "",
        *(incomplete_lines or ["(none)"]),
        "",
        "## Rules",
        "",
        "- Regenerate `requirements-lock.txt` only with the container recipe in "
        "`CONTRIBUTING.md` (Regenerating the Lockfile). A lock compiled on the host "
        "differs from the Linux lock CI expects.",
        "- Only add a suppression (`.trivyignore`, `.pip-audit-ignore`, "
        "`.npm-audit-ignore`) when no fix exists upstream; every entry needs the "
        "advisory link, the provenance, and an expiry date per the file's header.",
        "- Never edit a test expectation except the one that pins the exact version "
        "you are bumping, and list every such edit in the pull request body under "
        "its own heading.",
        "- Every tracked `*.sh` is held at 100% shell coverage and every Python module "
        "at 100% branch coverage; a new test file needs its row in `tests/README.md`, "
        "a new script its row in the matching README. Run the doc-gate tests before "
        "you push.",
        "- Do not add dependencies, do not change code beyond what the findings "
        "require, and do not refactor on the way past.",
        "- If a finding looks wrong (the pin is already current, the scanner misread "
        'a file), do not change the scanner. Record it under "Suspected scan '
        'inaccuracy" in the pull request body and move on.',
        "- If the same approach fails twice, stop on that finding, record exactly what "
        'failed under "Could not complete", and continue with the next finding.',
        "- Nothing non-public goes into commits or the pull request: no account IDs, "
        "ARNs, hostnames, IP addresses, internal project names or private links.",
        "- Never run `aws`, `cdk deploy`, `kubectl` or `helm install`; nothing here "
        "needs a live environment.",
        "",
        "## Verification before the pull request",
        "",
        "For every change, run the targeted checks locally and record the result: "
        "`ruff check`, `ruff format --check`, `mypy` and `bandit` on changed Python; "
        "the pytest files that cover changed modules (never the whole suite, never "
        "`tests/test_cdk_synthesis_matrix.py`); `bats` for changed shell; "
        "`actionlint` and `yamllint` for changed workflows; `markdownlint-cli2` for "
        "changed Markdown. What proves each surface in CI:",
        "",
        *(verify_lines or ["- (nothing to apply)"]),
        "",
        "## Deliverable",
        "",
        f"1. `git push -u origin {branch}`.",
        "2. Open a DRAFT pull request with `gh pr create --draft` against `main`, "
        f"titled `chore(deps): maintenance {document.generated_at[:10] or 'run'}`, "
        "following `.github/pull_request_template.md`. The body must contain these "
        "sections, in this order:",
        "   - **Summary** — one bullet per surface applied: what moved, from where "
        "to where, why it is safe.",
        "   - **Report only** — the analyses for the findings you did not change.",
        '   - **Test expectations changed** — every test literal you edited, or "none".',
        "   - **Next steps for the maintainer** — one bullet per applied surface, "
        'taken from the "What proves each surface" list above, stating which CI '
        "job or local run confirms it and anything that needs a live environment.",
        '   - **Suspected scan inaccuracy** and **Could not complete** — or "none".',
        "   - **Findings source** — the line from the top of this prompt.",
        "3. Print the pull request URL as your final message and stop. Do not wait "
        "for CI, do not merge, do not open a second pull request.",
        "",
    ]
    return "\n".join(parts)


def build_maintenance_argv(
    claude_binary: str,
    *,
    mcp_config: Path,
    prompt: str,
    branch: str,
    print_mode: bool,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Claude Code argv for the maintenance session.

    Owned flags first, the maintainer's passthrough after them (so a
    passthrough can add, for example, ``--max-turns``), the prompt last.
    ``print_mode`` runs the session headless (``-p``) and exits when the
    agent stops; otherwise the prompt opens an interactive session.
    """
    argv = [
        claude_binary,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--permission-mode",
        PERMISSION_MODE,
        "--allowedTools",
        *allowed_tool_rules(branch),
        "--disallowedTools",
        *DENIED_TOOL_RULES,
        *extra_args,
    ]
    if print_mode:
        argv.append("-p")
    argv.append(prompt)
    return argv


def plan_summary(document: FindingsDocument, act_on: Sequence[Tier]) -> dict[str, Any]:
    """Counts the dry run and the JSON plan report."""
    grouped = document.by_tier()
    return {
        "apply": {tier.value: len(grouped[tier]) for tier in _TIER_ORDER if tier in act_on},
        "report_only": {
            tier.value: len(grouped[tier]) for tier in _TIER_ORDER if tier not in act_on
        },
        "skipped_surfaces": dict(document.skipped),
        "incomplete_reasons": list(document.incomplete_reasons),
    }
