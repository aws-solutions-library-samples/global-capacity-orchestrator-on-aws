#!/usr/bin/env python3
"""Single source for the GitHub Actions SHA-pinning contract.

Every third-party ``uses:`` in this repository names a 40-character commit SHA
and records the tag it came from in a trailing comment::

    - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1

Three things have to be true for that shape to be worth anything, and this
module owns all three so the PR-time pytest contract
(``tests/test_workflow_security_contract.py``) and the CI job that additionally
calls GitHub cannot drift apart about what "pinned" means:

1. **Format** — the ref is a commit SHA and the comment is an exact ``vX.Y.Z``
   semantic version. A bare ``# v7`` would reintroduce the ambiguity SHA
   pinning exists to remove: nobody reading the diff could tell which release
   the hash is supposed to be, so nobody could catch a wrong one.
2. **Agreement** — every occurrence of an action resolves to the same SHA and
   claims the same version. Subpath actions (``github/codeql-action/init`` and
   ``…/analyze``) are checked per *repository*, because they are one repo and
   therefore one commit.
3. **Truth** (``--verify-upstream``, needs network) — the tag in the comment
   really does point at the pinned SHA on GitHub. Without this the comment is
   an unverified claim, and a typo'd or copy-pasted version silently misleads
   every future reviewer and every Dependabot bump.

Exit status is 1 only for a *definitive* problem. A lookup that could not be
completed (rate limit, timeout, deleted tag) is reported and tolerated, because
failing every pull request on an api.github.com blip would train people to
ignore this check.

Usage::

    python .github/scripts/verify_action_pins.py
    python .github/scripts/verify_action_pins.py --verify-upstream
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
ACTION_DIR = ROOT / ".github" / "actions"

GITHUB_API = "https://api.github.com"

#: ``uses:`` as a step key: optional list dash, then the ref, then whatever
#: trails it. The ref stops at whitespace or ``#`` so the comment is parsed
#: separately. A line that starts with ``#`` cannot match, so prose that
#: mentions the unsafe form is not mistaken for a real ref.
USES_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:-[ \t]+)?uses:[ \t]+(?P<ref>[^\s#]+)(?P<trailer>.*)$"
)

#: Anything after the ref must be exactly one comment, nothing else.
TRAILER_RE = re.compile(r"^[ \t]*#[ \t]*(?P<body>.*?)[ \t]*$")

#: Deliberately strict: three numeric components, no pre-release or build
#: metadata, no bare major. Actions publish ``vX.Y.Z`` releases; accepting
#: less makes the comment unfalsifiable.
SEMVER_TAG_RE = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: ``owner/repo`` in GitHub's own character set. Both halves are interpolated
#: into an API path, and the strings come from workflow files — which, on a pull
#: request from a fork, are attacker-authored. Anchoring the shape here means a
#: crafted ``uses:`` cannot smuggle ``../``, a query string, a credential, or a
#: second scheme into the request URL.
REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)


@dataclass(frozen=True)
class Pin:
    """One ``uses:`` reference as it appears on disk."""

    path: Path
    line: int
    ref: str
    action: str
    sha: str
    version: str
    trailer: str

    @property
    def repository(self) -> str:
        """``owner/repo``, dropping any subpath.

        ``github/codeql-action/init`` and ``github/codeql-action/analyze`` are
        two entry points into one repository at one commit, so agreement and
        upstream lookups are keyed here rather than on the full action path.
        """
        return "/".join(self.action.split("/")[:2])

    @property
    def local(self) -> bool:
        """A same-repo composite ref, which travels with the commit itself."""
        return self.action.startswith("./")

    @property
    def location(self) -> str:
        try:
            relative: Path | str = self.path.relative_to(ROOT)
        except ValueError:  # pragma: no cover - only when called out of tree
            relative = self.path
        return f"{relative}:{self.line}"


@dataclass(frozen=True)
class TagResolution:
    """Outcome of asking GitHub what commit a tag points at."""

    sha: str | None = None
    error: str | None = None


TagResolver = Callable[[str, str], TagResolution]


def reference_files(root: Path | None = None) -> list[Path]:
    """Every file in which this repository may name an action to run."""
    base = root or ROOT
    workflows = sorted((base / ".github" / "workflows").glob("*.yml"))
    actions = sorted((base / ".github" / "actions").glob("*/action.yml"))
    return workflows + actions


def collect_pins(path: Path) -> list[Pin]:
    """Parse every ``uses:`` line in one workflow or composite action.

    Line-based on purpose: YAML parsing discards comments, and the comment is
    half of the contract. ``tests/test_workflow_security_contract.py``
    cross-checks the count found here against a structural walk, so a ref
    cannot hide from this parser behind unusual formatting.
    """
    pins: list[Pin] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = USES_LINE_RE.match(line)
        if not match:
            continue
        ref = match.group("ref").strip("\"'")
        trailer = match.group("trailer")
        comment = TRAILER_RE.match(trailer)
        action, _, revision = ref.partition("@")
        pins.append(
            Pin(
                path=path,
                line=number,
                ref=ref,
                action=action,
                sha=revision if SHA_RE.match(revision) else "",
                version=comment.group("body") if comment else "",
                trailer=trailer,
            )
        )
    return pins


def collect_all_pins(root: Path | None = None) -> list[Pin]:
    return [pin for path in reference_files(root) for pin in collect_pins(path)]


def third_party(pins: Iterable[Pin]) -> list[Pin]:
    return [pin for pin in pins if not pin.local]


def format_problems(pins: Iterable[Pin]) -> list[str]:
    """Each third-party ref must be a commit SHA plus an exact ``vX.Y.Z``."""
    problems: list[str] = []
    for pin in third_party(pins):
        if not pin.sha:
            problems.append(f"{pin.location}: {pin.ref} is not pinned to a 40-character commit SHA")
            continue
        if not pin.version:
            problems.append(
                f"{pin.location}: {pin.action} is pinned to {pin.sha[:12]}… with no "
                f"trailing '# vX.Y.Z' comment"
            )
            continue
        if not SEMVER_TAG_RE.match(pin.version):
            problems.append(
                f"{pin.location}: {pin.action} version comment {pin.version!r} is not an "
                f"exact vX.Y.Z semantic version"
            )
    return problems


def consistency_problems(pins: Iterable[Pin]) -> list[str]:
    """Every reference to one repository must agree on SHA and version.

    Two SHAs for one repository means two different builds of the same action
    run in the same pipeline — the drift `test_repeated_workflow_pins_agree`
    forbids for tool versions, applied to actions. Two *versions* for one SHA
    (or vice versa) means at least one comment is lying, which is the failure
    this check exists to make loud.
    """
    by_repository: dict[str, set[str]] = defaultdict(set)
    by_version: dict[str, set[str]] = defaultdict(set)
    locations: dict[tuple[str, str, str], list[str]] = defaultdict(list)

    for pin in third_party(pins):
        if not pin.sha or not pin.version:
            continue  # format_problems already reports these
        by_repository[pin.repository].add(pin.sha)
        by_version[pin.repository].add(pin.version)
        locations[(pin.repository, pin.sha, pin.version)].append(pin.location)

    problems: list[str] = []
    for repository in sorted(by_repository):
        shas = sorted(by_repository[repository])
        versions = sorted(by_version[repository])
        if len(shas) > 1:
            detail = ", ".join(
                f"{sha[:12]}… at {', '.join(sorted(sites))}"
                for sha in shas
                for version in versions
                for sites in [locations.get((repository, sha, version), [])]
                if sites
            )
            problems.append(
                f"{repository} is pinned to {len(shas)} different commits ({detail}); "
                f"every reference to one action must resolve to one commit"
            )
        if len(versions) > 1:
            problems.append(
                f"{repository} claims {len(versions)} different versions "
                f"({', '.join(versions)}); the comment must match the pinned commit"
            )
    return problems


#: HTTP codes that mean "this credential is not welcome here" rather than
#: "this tag is wrong". An org can block the GitHub Actions app, in which case a
#: workflow's GITHUB_TOKEN is refused for that org's public repositories even
#: though anonymous reads of them succeed.
_CREDENTIAL_REJECTED = {401, 403}


def _fetch_commit(
    repository: str,
    version: str,
    token: str | None,
    timeout: float,
) -> tuple[TagResolution, int | None]:
    """One attempt. Returns the outcome and the HTTP status, when there was one."""
    url = f"{GITHUB_API}/repos/{repository}/commits/{version}"
    # Both path components were shape-checked by resolve_tag, so this holds by
    # construction; it is asserted anyway because it is the property that makes
    # the urlopen below safe, and a future caller reaching _fetch_commit
    # directly should fail loudly rather than issue an unconstrained request.
    if not url.startswith(f"{GITHUB_API}/repos/"):  # pragma: no cover - defensive
        return TagResolution(error="refusing to request a non-GitHub URL"), None
    request = urllib.request.Request(  # noqa: S310 - constant https host, validated path
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gco-verify-action-pins",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    # dynamic-urllib-use-detected below is suppressed because its premise does
    # not hold here. The rule guards against a dynamic value choosing the scheme
    # (``file://`` and friends): the scheme and host are the GITHUB_API constant,
    # and the only interpolated values are an ``owner/repo`` matching
    # REPOSITORY_RE and a tag matching SEMVER_TAG_RE, both rejected by
    # resolve_tag before this runs, with the prefix re-asserted above. Switching
    # to ``requests`` (the rule's own suggestion) would put a third-party import
    # in a script that has to run with no dependency install.
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return TagResolution(error=f"tag {version} not found upstream (HTTP 404)"), 404
        if error.code in _CREDENTIAL_REJECTED or error.code == 429:
            return (
                TagResolution(error=f"rate limited or forbidden (HTTP {error.code})"),
                error.code,
            )
        return TagResolution(error=f"HTTP {error.code}"), error.code
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
        return TagResolution(error=f"lookup failed ({error.__class__.__name__})"), None

    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        return TagResolution(error="response carried no commit sha"), None
    return TagResolution(sha=sha), None


def resolve_tag(
    repository: str,
    version: str,
    *,
    token: str | None = None,
    timeout: float = 15.0,
) -> TagResolution:
    """Ask GitHub which commit ``version`` points at in ``repository``.

    ``/commits/{ref}`` dereferences annotated tags to the commit, which is the
    same thing ``gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`` returns
    and therefore the same thing the pins were produced from.

    A token that is *refused* (401/403) falls back to an anonymous read. Some
    organizations block the GitHub Actions app, so a workflow's ``GITHUB_TOKEN``
    gets 403 on their public repositories while an unauthenticated request to
    the same URL returns 200 — observed with ``aquasecurity/setup-trivy``.
    Without the fallback that pin would be permanently unverified while CI still
    reported success, which is the worst outcome available: a check that looks
    green precisely where it has stopped looking.

    Both arguments are shape-checked before any request is made. They originate
    in workflow files, which on a fork pull request are attacker-authored, so a
    crafted ``uses:`` must not be able to steer the URL.
    """
    if not REPOSITORY_RE.match(repository):
        return TagResolution(error=f"refusing to look up malformed repository {repository!r}")
    if not SEMVER_TAG_RE.match(version):
        return TagResolution(error=f"refusing to look up malformed version {version!r}")

    resolution, status = _fetch_commit(repository, version, token, timeout)
    if resolution.sha is None and token and status in _CREDENTIAL_REJECTED:
        anonymous, _ = _fetch_commit(repository, version, None, timeout)
        if anonymous.sha is not None:
            return anonymous
        return TagResolution(
            error=f"{resolution.error}; anonymous retry also failed ({anonymous.error})"
        )
    return resolution


def upstream_problems(
    pins: Iterable[Pin],
    resolver: TagResolver,
) -> tuple[list[str], list[str]]:
    """Confirm each version comment names the commit it is pinned to.

    Returns ``(mismatches, unresolved)``. A mismatch is definitive: GitHub
    answered, and the tag points somewhere other than the pinned SHA — either
    the comment is wrong or the tag was moved, and both need a human. An
    unresolved lookup is reported but not fatal.
    """
    wanted: dict[tuple[str, str], set[str]] = defaultdict(set)
    sites: dict[tuple[str, str], list[str]] = defaultdict(list)
    for pin in third_party(pins):
        if not pin.sha or not SEMVER_TAG_RE.match(pin.version):
            continue
        wanted[(pin.repository, pin.version)].add(pin.sha)
        sites[(pin.repository, pin.version)].append(pin.location)

    mismatches: list[str] = []
    unresolved: list[str] = []
    for repository, version in sorted(wanted):
        resolution = resolver(repository, version)
        if resolution.sha is None:
            unresolved.append(f"{repository}@{version}: {resolution.error}")
            continue
        for sha in sorted(wanted[(repository, version)]):
            if sha != resolution.sha:
                where = ", ".join(sorted(sites[(repository, version)]))
                mismatches.append(
                    f"{repository} is pinned to {sha} but its comment says {version}, "
                    f"which upstream resolves to {resolution.sha} ({where})"
                )
    return mismatches, unresolved


def _report(title: str, entries: Sequence[str]) -> None:
    print(f"\n{title}")
    for entry in entries:
        print(f"  - {entry}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--verify-upstream",
        action="store_true",
        help="also resolve each version comment against GitHub (requires network)",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="treat an unresolved upstream lookup as a failure instead of a warning",
    )
    args = parser.parse_args(argv)

    pins = collect_all_pins()
    external = third_party(pins)
    print(
        f"verify-action-pins: {len(external)} third-party ref(s) across "
        f"{len(reference_files())} file(s); "
        f"{len(pins) - len(external)} local composite ref(s) exempt"
    )

    problems = format_problems(pins)
    problems += consistency_problems(pins)
    if problems:
        _report("Pinning problems:", problems)

    unresolved: list[str] = []
    if args.verify_upstream:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print(
                "verify-action-pins: no GH_TOKEN/GITHUB_TOKEN; using unauthenticated "
                "API (60 requests/hour)"
            )
        mismatches, unresolved = upstream_problems(
            pins, lambda repository, version: resolve_tag(repository, version, token=token)
        )
        repositories = {pin.repository for pin in external}
        print(f"verify-action-pins: resolved {len(repositories)} action repositor(y|ies) upstream")
        if mismatches:
            _report("Version comments that do not match the pinned commit:", mismatches)
            problems += mismatches
        if unresolved:
            _report("Incomplete lookups (not treated as failures):", unresolved)

    if problems or (args.require_complete and unresolved):
        print(f"\nverify-action-pins: FAILED ({len(problems)} problem(s))")
        return 1
    print("\nverify-action-pins: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
