"""Tests for ``cli/maintenance.py``: the policy behind ``gco deps maintain``.

The scan writes facts; this module decides what an agent may do with them.
These tests pin the decisions: every scan surface has a policy row whose
procedure anchor exists in ``docs/MAINTENANCE.md``; a major-version jump
promotes a finding one tier; a surface the table does not know is reported,
never acted on; the findings document is validated against the schema the
shell scan writes (the constants are read from ``lib_dependency_scan.sh`` so
the two cannot drift); the embedded document is lifted out of an issue body
between the markers; and the prompt and ``claude`` argv carry exactly the
rules, tiers and deliverable the maintainer relies on.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from cli import maintenance
from cli.maintenance import (
    ALLOWED_BASH_RULES,
    ALLOWED_FETCH_DOMAINS,
    DENIED_TOOL_RULES,
    FINDINGS_BEGIN_MARKER,
    FINDINGS_END_MARKER,
    FINDINGS_SCHEMA,
    MAINTENANCE_DOC,
    PERMISSION_MODE,
    SURFACE_POLICIES,
    UNKNOWN_SURFACE_POLICY,
    Finding,
    FindingsDocument,
    FindingsError,
    Tier,
    allowed_tool_rules,
    build_maintenance_argv,
    classify,
    compose_prompt,
    default_branch_name,
    extract_embedded_findings,
    is_major_bump,
    load_findings_file,
    parse_findings,
    plan_summary,
    promote,
    worktree_path_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_LIB = REPO_ROOT / ".github" / "scripts" / "lib_dependency_scan.sh"
SCAN_SCRIPT = REPO_ROOT / ".github" / "scripts" / "dependency-scan.sh"


def _document(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema": FINDINGS_SCHEMA,
        "generated_at": "2026-10-01T06:12:00Z",
        "scan_complete": True,
        "has_drift": True,
        "incomplete_reasons": [],
        "surfaces": [
            {
                "surface": "Python Packages",
                "urgency": "routine",
                "count": 2,
                "skipped": None,
                "findings": [
                    {"name": "urllib3", "current": "2.7.0", "latest": "2.8.0"},
                    {"name": "aws-cdk-lib", "current": "2.272.0", "latest": "3.0.1"},
                ],
            },
            {
                "surface": "EKS Add-ons",
                "urgency": "routine",
                "count": 0,
                "skipped": "No AWS credentials",
                "findings": [],
            },
            {
                "surface": "EKS Kubernetes Version",
                "urgency": "act soon",
                "count": 1,
                "skipped": None,
                "findings": [
                    {
                        "pin": "kubernetes_version",
                        "current": "1.36",
                        "latest": "1.37",
                        "standard_support_ends": "2027-01-15",
                    }
                ],
            },
            {
                "surface": "EMR Serverless",
                "urgency": "routine",
                "count": 1,
                "skipped": None,
                "findings": [
                    {
                        "release": "emr-serverless",
                        "current": "emr-7.14.0",
                        "latest": "emr-spark-8.1.0",
                    }
                ],
            },
            {
                "surface": "Accelerator Catalog and NodePools",
                "urgency": "act soon",
                "count": 2,
                "skipped": None,
                "findings": [],
                "offline_report_markdown": "### Retire p2",
            },
            {
                "surface": "Suppression Expiries",
                "urgency": "act soon",
                "count": 1,
                "skipped": None,
                "findings": [
                    {
                        "file": ".trivyignore",
                        "id": "CVE-2026-14257",
                        "expires": "2026-10-22",
                        "days_left": "19",
                    }
                ],
            },
        ],
    }
    base.update(overrides)
    return base


# ─── Lockstep with the shell scan ───────────────────────────────────────────


def _shell_constant(name: str) -> str:
    match = re.search(rf'^{name}="([^"]+)"$', SCAN_LIB.read_text(encoding="utf-8"), re.M)
    assert match is not None, f"{name} not found in {SCAN_LIB.name}"
    return match.group(1)


def test_schema_and_markers_match_the_shell_scan() -> None:
    assert _shell_constant("FINDINGS_SCHEMA") == FINDINGS_SCHEMA
    assert _shell_constant("FINDINGS_BEGIN_MARKER") == FINDINGS_BEGIN_MARKER
    assert _shell_constant("FINDINGS_END_MARKER") == FINDINGS_END_MARKER


def test_every_scan_surface_has_a_policy_and_nothing_else_does() -> None:
    """The policy table names exactly the surfaces the scan records."""
    script = SCAN_SCRIPT.read_text(encoding="utf-8")
    recorded = set(re.findall(r'^findings_surface(?:_json)? +"([^"]+)"', script, re.M))
    assert recorded, "the scan records no surfaces?"
    assert set(SURFACE_POLICIES) == recorded


def test_every_procedure_anchor_is_a_maintenance_heading() -> None:
    text = (REPO_ROOT / MAINTENANCE_DOC).read_text(encoding="utf-8")
    anchors = set()
    for line in text.splitlines():
        heading = re.match(r"^#{2,4}\s+(.*)", line)
        if heading:
            slug = re.sub(r"[^a-z0-9 -]", "", heading.group(1).lower()).replace(" ", "-")
            anchors.add("#" + slug)
    needed = {policy.procedure for policy in SURFACE_POLICIES.values()}
    needed.add(UNKNOWN_SURFACE_POLICY.procedure)
    assert needed <= anchors, sorted(needed - anchors)
    # The tiers themselves are documented where the agent is told to read.
    assert "#agent-assisted-maintenance" in anchors
    for tier in Tier:
        assert f"`{tier.value}`" in text


# ─── Tiers ──────────────────────────────────────────────────────────────────


def test_promotion_moves_one_tier_up_and_stops_at_judgment() -> None:
    assert promote(Tier.MECHANICAL) is Tier.SEMANTIC
    assert promote(Tier.SEMANTIC) is Tier.JUDGMENT
    assert promote(Tier.JUDGMENT) is Tier.JUDGMENT


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        ("2.7.0", "2.8.0", False),
        ("7.14.0", "8.1.0", True),
        ("emr-7.14.0", "emr-spark-8.1.0", True),
        ("v1.36.4", "v2.36.4", True),
        ("v1.36.4", "v1.37.0", False),
        ("PYTHON_3_14", "PYTHON_4_14", True),
        ("1.36", "1.37", False),
        ("global.anthropic.claude-opus-5", "global.anthropic.claude-opus-6", True),
        ("latest", "deprecated", False),
        ("", "1.0", False),
    ],
)
def test_a_major_bump_is_a_change_in_the_leading_number(
    current: str, latest: str, expected: bool
) -> None:
    assert is_major_bump(current, latest) is expected


def test_classification_promotes_majors_where_the_policy_allows() -> None:
    assert classify("Python Packages", {"current": "2.7.0", "latest": "2.8.0"}) == (
        Tier.MECHANICAL,
        "",
    )
    assert classify("Python Packages", {"current": "2.272.0", "latest": "3.0.1"}) == (
        Tier.SEMANTIC,
        "major version change, promoted to semantic",
    )
    assert classify("EMR Serverless", {"current": "emr-7.14.0", "latest": "emr-spark-8.1.0"}) == (
        Tier.JUDGMENT,
        "major version change, promoted to judgment",
    )
    # Surfaces whose values are not versions never promote.
    assert classify("Bedrock Default Model", {"current": "x-5", "latest": "x-6"}) == (
        Tier.SEMANTIC,
        "",
    )
    assert classify("EKS Kubernetes Version", {"current": "1.36", "latest": "2.0"}) == (
        Tier.JUDGMENT,
        "",
    )
    # A surface the table does not know is reported only.
    assert classify("Mystery Surface", {"current": "1", "latest": "1.1"}) == (
        Tier.JUDGMENT,
        "surface unknown to the maintenance policy",
    )


# ─── The findings document ──────────────────────────────────────────────────


def test_parse_findings_classifies_rows_and_carries_skips_and_report_surfaces() -> None:
    document = parse_findings(_document())
    assert document.generated_at == "2026-10-01T06:12:00Z"
    assert document.scan_complete is True
    assert document.has_drift is True
    assert document.incomplete_reasons == ()
    assert document.skipped == {"EKS Add-ons": "No AWS credentials"}
    grouped = document.by_tier()
    assert [finding.label() for finding in grouped[Tier.MECHANICAL]] == [
        "urllib3: 2.7.0 -> 2.8.0",
        "file=.trivyignore, id=CVE-2026-14257, expires=2026-10-22, days_left=19",
    ]
    assert [finding.label() for finding in grouped[Tier.SEMANTIC]] == [
        "aws-cdk-lib: 2.272.0 -> 3.0.1 [major version change, promoted to semantic]",
        "findings=2 finding(s) in the surface's Markdown report",
    ]
    assert [finding.label() for finding in grouped[Tier.JUDGMENT]] == [
        "kubernetes_version: 1.36 -> 1.37 (standard_support_ends=2027-01-15)",
        "emr-serverless: emr-7.14.0 -> emr-spark-8.1.0 "
        "[major version change, promoted to judgment]",
    ]
    accelerator = grouped[Tier.SEMANTIC][1]
    assert accelerator.surface == "Accelerator Catalog and NodePools"
    assert accelerator.procedure == "#reviewing-and-refreshing-catalog-drift"
    assert accelerator.urgency == "act soon"
    suppression = grouped[Tier.MECHANICAL][1]
    assert suppression.verify.startswith("each entry re-checked upstream")


def test_a_finding_without_a_name_field_is_labelled_by_its_surface() -> None:
    finding = Finding(
        surface="Python Packages",
        urgency="routine",
        tier=Tier.MECHANICAL,
        fields={"current": "1", "latest": "2"},
        procedure="#x",
        verify="y",
    )
    assert finding.label() == "Python Packages: 1 -> 2"


def test_parse_findings_tolerates_missing_optional_keys() -> None:
    document = parse_findings({"schema": FINDINGS_SCHEMA, "surfaces": []})
    assert document == FindingsDocument(
        generated_at="",
        scan_complete=False,
        has_drift=False,
        incomplete_reasons=(),
        skipped={},
        findings=(),
    )
    # A skipped surface with a stale count is not turned into a finding.
    document = parse_findings(
        {
            "schema": FINDINGS_SCHEMA,
            "surfaces": [{"surface": "Runner Images", "count": 2, "skipped": "no network"}],
            "incomplete_reasons": ["x failed", 7],
        }
    )
    assert document.findings == ()
    assert document.incomplete_reasons == ("x failed", "7")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "not a JSON object"),
        ({"schema": "other/9", "surfaces": []}, "is not 'gco.dependency-scan.findings/1'"),
        ({"schema": FINDINGS_SCHEMA}, "no surfaces list"),
        ({"schema": FINDINGS_SCHEMA, "surfaces": ["x"]}, "surface record is not an object"),
        ({"schema": FINDINGS_SCHEMA, "surfaces": [{"count": 1}]}, "has no name"),
        (
            {"schema": FINDINGS_SCHEMA, "surfaces": [{"surface": "Docker Images", "findings": 3}]},
            "findings is not a list",
        ),
        (
            {
                "schema": FINDINGS_SCHEMA,
                "surfaces": [{"surface": "Docker Images", "findings": [1]}],
            },
            "is not an object",
        ),
        ({"schema": FINDINGS_SCHEMA, "surfaces": [], "incomplete_reasons": "x"}, "not a list"),
    ],
)
def test_parse_findings_refuses_what_it_cannot_trust(data: object, message: str) -> None:
    with pytest.raises(FindingsError, match=re.escape(message)):
        parse_findings(data)


def test_the_embedded_document_is_lifted_from_between_the_markers() -> None:
    body = (
        "# Dependency Update Report\n\n| a | b |\n\n<details>\n<summary>Machine-readable</summary>\n\n"
        f"{FINDINGS_BEGIN_MARKER}\n```json\n"
        + json.dumps(_document(), indent=2)
        + f"\n```\n{FINDINGS_END_MARKER}\n\n</details>\n\n---\n"
    )
    assert extract_embedded_findings(body) == _document()


@pytest.mark.parametrize(
    "body",
    [
        "# Dependency Update Report\n\nno markers here\n",
        f"{FINDINGS_BEGIN_MARKER}\nno end\n",
        f"{FINDINGS_END_MARKER}\nbackwards\n{FINDINGS_BEGIN_MARKER}\n",
    ],
    ids=["no markers", "no end marker", "end before begin"],
)
def test_a_body_without_the_markers_names_the_alternatives(body: str) -> None:
    with pytest.raises(FindingsError, match=r"--findings .* or run with --scan"):
        extract_embedded_findings(body)


def test_an_embedded_document_that_is_not_json_is_refused() -> None:
    body = f"{FINDINGS_BEGIN_MARKER}\n```json\n{{not json\n```\n{FINDINGS_END_MARKER}"
    with pytest.raises(FindingsError, match="not valid JSON"):
        extract_embedded_findings(body)


def test_load_findings_file_reads_json_and_names_failures(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    assert load_findings_file(path) == _document()
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(FindingsError, match="not valid JSON"):
        load_findings_file(path)
    with pytest.raises(FindingsError, match="cannot read"):
        load_findings_file(tmp_path / "missing.json")


# ─── The launch ─────────────────────────────────────────────────────────────


def test_default_branch_and_worktree_names() -> None:
    assert default_branch_name(dt.date(2026, 10, 3)) == "maint/deps-2026-10-03"
    assert default_branch_name().startswith("maint/deps-20")
    assert worktree_path_for(Path("/repo"), "maint/deps-2026-10-03") == Path(
        "/repo/.worktrees/maint-deps-2026-10-03"
    )


def test_allowed_rules_name_the_branch_and_the_documentation_hosts() -> None:
    rules = allowed_tool_rules("maint/deps-2026-10-03")
    assert "Bash(git push -u origin maint/deps-2026-10-03)" in rules
    assert all("{branch}" not in rule for rule in rules)
    assert rules[-1] == "WebSearch"
    assert "WebFetch(domain:pypi.org)" in rules
    assert len(rules) == len(ALLOWED_BASH_RULES) + len(ALLOWED_FETCH_DOMAINS) + 1
    # Deny rules are the repository's red lines; they must stay.
    for rule in (
        "Bash(git push --force*)",
        "Bash(git merge *)",
        "Bash(gh pr merge *)",
        "Bash(aws *)",
        "Bash(kubectl *)",
        "Bash(rm -rf *)",
    ):
        assert rule in DENIED_TOOL_RULES
    assert PERMISSION_MODE == "acceptEdits"


def test_argv_puts_owned_flags_first_passthrough_next_and_the_prompt_last() -> None:
    argv = build_maintenance_argv(
        "/usr/bin/claude",
        mcp_config=Path("/s/mcp.json"),
        prompt="do the thing",
        branch="maint/deps-2026-10-03",
        print_mode=False,
        extra_args=("--max-turns", "200"),
    )
    assert argv[:7] == [
        "/usr/bin/claude",
        "--mcp-config",
        "/s/mcp.json",
        "--strict-mcp-config",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
    ]
    allowed = argv.index("--allowedTools")
    denied = argv.index("--disallowedTools")
    assert argv[allowed + 1 : denied] == list(allowed_tool_rules("maint/deps-2026-10-03"))
    assert argv[denied + 1 : denied + 1 + len(DENIED_TOOL_RULES)] == list(DENIED_TOOL_RULES)
    assert argv[-3:] == ["--max-turns", "200", "do the thing"]
    assert "-p" not in argv
    headless = build_maintenance_argv(
        "claude", mcp_config=Path("m"), prompt="p", branch="b", print_mode=True
    )
    assert headless[-2:] == ["-p", "p"]


def test_the_prompt_carries_tiers_rules_verification_and_the_deliverable() -> None:
    document = parse_findings(_document(incomplete_reasons=["PyPI lookup failed for x"]))
    prompt = compose_prompt(
        document,
        act_on=[Tier.MECHANICAL, Tier.SEMANTIC],
        branch="maint/deps-2026-10-03",
        source="https://example.invalid/issues/434",
        repo_url="https://example.invalid/repo",
    )
    # Context.
    assert "Repository: https://example.invalid/repo" in prompt
    assert "Findings source: https://example.invalid/issues/434" in prompt
    assert "Scan generated: 2026-10-01T06:12:00Z (complete)" in prompt
    assert "on the branch `maint/deps-2026-10-03`" in prompt
    # Findings, grouped by what the agent may do.
    apply_section = prompt.split("## Findings to apply")[1].split("## Findings to report only")[0]
    report_section = prompt.split("## Findings to report only")[1].split(
        "## Surfaces the scan skipped"
    )[0]
    assert "### mechanical (2)" in apply_section
    assert "### semantic (2)" in apply_section
    assert "urllib3: 2.7.0 -> 2.8.0" in apply_section
    assert f"procedure: {MAINTENANCE_DOC}#renewing-cve-suppressions" in apply_section
    assert "### judgment (2)" in report_section
    assert "emr-spark-8.1.0" in report_section
    assert "Do NOT change these" in report_section
    # Skips and incomplete lookups are visible.
    assert "- EKS Add-ons: No AWS credentials" in prompt
    assert "- PyPI lookup failed for x" in prompt
    # Rules the repository insists on.
    for rule in (
        "Regenerating the Lockfile",
        "Never edit a test expectation except",
        "Suspected scan inaccuracy",
        "Could not complete",
        "no account IDs, ARNs",
        "Never run `aws`, `cdk deploy`, `kubectl` or `helm install`",
        "never `tests/test_cdk_synthesis_matrix.py`",
    ):
        assert rule in prompt, rule
    # Verification hints for the applied surfaces only, deduplicated.
    verify_section = prompt.split("What proves each surface in CI:")[1].split("## Deliverable")[0]
    assert verify_section.count("- Python Packages:") == 1
    assert "- Suppression Expiries:" in verify_section
    assert "- EKS Kubernetes Version" not in verify_section
    # Deliverable.
    assert "`git push -u origin maint/deps-2026-10-03`" in prompt
    assert "gh pr create --draft" in prompt
    assert "chore(deps): maintenance 2026-10-01" in prompt
    assert "Next steps for the maintainer" in prompt
    assert prompt.rstrip().endswith("do not open a second pull request.")


def test_the_prompt_marks_an_incomplete_scan_and_empty_sections() -> None:
    document = parse_findings(_document(scan_complete=False, generated_at="", surfaces=[]))
    prompt = compose_prompt(document, act_on=[Tier.MECHANICAL], branch="b", source="s")
    assert "Scan generated: unknown (INCOMPLETE)" in prompt
    assert "Repository:" not in prompt
    assert "chore(deps): maintenance run`" in prompt
    assert prompt.count("(none)") == 4
    assert "- (nothing to apply)" in prompt


def test_the_prompt_reports_everything_when_no_tier_is_acted_on() -> None:
    document = parse_findings(_document())
    prompt = compose_prompt(document, act_on=[], branch="b", source="s")
    apply_section = prompt.split("## Findings to apply")[1].split("## Findings to report only")[0]
    assert "(none)" in apply_section
    assert "### mechanical (2)" in prompt
    assert "### judgment (2)" in prompt


def test_plan_summary_counts_by_tier() -> None:
    document = parse_findings(_document(incomplete_reasons=["x"]))
    assert plan_summary(document, [Tier.MECHANICAL]) == {
        "apply": {"mechanical": 2},
        "report_only": {"semantic": 2, "judgment": 2},
        "skipped_surfaces": {"EKS Add-ons": "No AWS credentials"},
        "incomplete_reasons": ["x"],
    }


def test_module_constants_are_the_documented_values() -> None:
    assert maintenance.ROLLING_ISSUE_TITLE == "[Automated] Dependency updates available"
    assert maintenance.ROLLING_ISSUE_LABELS == ("dependencies", "automated")
