"""Tests for the action-pinning verifier (.github/scripts/verify_action_pins.py).

The verifier is the single source of the SHA-pinning contract: the PR-time
suite in ``test_workflow_security_contract.py`` and the
``lint:actions:pinning`` CI job both call into it, so a hole here is a hole in
both. These tests pin the failure modes it must catch — a mutable tag, a
missing or sloppy version comment, two commits claiming to be one action, and a
comment that names a version the pinned commit does not correspond to — plus
the deliberate tolerances: local composite refs are exempt, and a lookup that
could not be completed must not be reported as a mismatch.

Upstream verification is exercised through an injected resolver, so the network
path is covered without the suite ever reaching api.github.com.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "verify_action_pins.py"
_spec = importlib.util.spec_from_file_location("verify_action_pins", _SCRIPT)
assert _spec is not None and _spec.loader is not None
verifier = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_action_pins", verifier)
_spec.loader.exec_module(verifier)

SHA_A = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SHA_B = "5595ccaf912efad79be6eef63a5619ff05969be3"


def _workflow(tmp_path: Path, body: str, name: str = "w.yml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _steps(*lines: str) -> str:
    return "jobs:\n  build:\n    steps:\n" + "".join(f"      {line}\n" for line in lines)


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_parses_ref_line_number_action_sha_and_version(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps(f"- uses: actions/checkout@{SHA_A}  # v7.0.1"))
    (pin,) = verifier.collect_pins(path)
    assert pin.action == "actions/checkout"
    assert pin.repository == "actions/checkout"
    assert pin.sha == SHA_A
    assert pin.version == "v7.0.1"
    assert pin.line == 4
    assert not pin.local


def test_parses_both_the_dashed_and_bare_uses_forms(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        _steps(
            "- name: build",
            f"  uses: docker/build-push-action@{SHA_A}  # v7.3.0",
            f"- uses: actions/cache@{SHA_B}  # v6.1.0",
        ),
    )
    assert [pin.action for pin in verifier.collect_pins(path)] == [
        "docker/build-push-action",
        "actions/cache",
    ]


def test_quoted_refs_are_unwrapped(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps(f'- uses: "actions/checkout@{SHA_A}"  # v7.0.1'))
    (pin,) = verifier.collect_pins(path)
    assert pin.sha == SHA_A
    assert pin.version == "v7.0.1"


def test_a_commented_out_ref_is_not_mistaken_for_a_real_one(tmp_path: Path) -> None:
    """Prose warning about the unsafe form must not itself trip the check."""
    path = _workflow(
        tmp_path,
        _steps(
            "# never do this: uses: actions/checkout@v7",
            f"- uses: actions/checkout@{SHA_A}  # v7.0.1",
        ),
    )
    assert len(verifier.collect_pins(path)) == 1


def test_subpath_actions_key_on_their_repository(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps(f"- uses: github/codeql-action/init@{SHA_B}  # v4.37.6"))
    (pin,) = verifier.collect_pins(path)
    assert pin.action == "github/codeql-action/init"
    assert pin.repository == "github/codeql-action"


# ── Format rules ─────────────────────────────────────────────────────────────


def test_a_tag_ref_is_reported_as_unpinned(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps("- uses: actions/checkout@v7.0.1  # v7.0.1"))
    (problem,) = verifier.format_problems(verifier.collect_pins(path))
    assert "not pinned to a 40-character commit SHA" in problem


def test_a_branch_ref_is_reported_as_unpinned(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps("- uses: actions/checkout@main"))
    (problem,) = verifier.format_problems(verifier.collect_pins(path))
    assert "not pinned" in problem


def test_a_pin_without_a_comment_is_reported(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps(f"- uses: actions/checkout@{SHA_A}"))
    (problem,) = verifier.format_problems(verifier.collect_pins(path))
    assert "no trailing '# vX.Y.Z' comment" in problem


@pytest.mark.parametrize("comment", ["v7", "v7.0", "latest", "7.0.1", "v7.0.1-beta.1", "pinned"])
def test_a_comment_that_is_not_an_exact_semver_is_reported(tmp_path: Path, comment: str) -> None:
    """``# v7`` is unfalsifiable, so the upstream check could never catch a lie."""
    path = _workflow(tmp_path, _steps(f"- uses: actions/checkout@{SHA_A}  # {comment}"))
    (problem,) = verifier.format_problems(verifier.collect_pins(path))
    assert "not an exact vX.Y.Z semantic version" in problem


def test_an_exact_semver_comment_passes(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps(f"- uses: actions/checkout@{SHA_A}  # v7.0.1"))
    assert verifier.format_problems(verifier.collect_pins(path)) == []


def test_multi_digit_components_pass(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps(f"- uses: github/codeql-action/init@{SHA_A}  # v4.37.6"))
    assert verifier.format_problems(verifier.collect_pins(path)) == []


def test_local_composite_refs_are_exempt(tmp_path: Path) -> None:
    """A ``./`` ref resolves inside the commit under review; there is no tag."""
    path = _workflow(tmp_path, _steps("- uses: ./.github/actions/install-trivy"))
    parsed = verifier.collect_pins(path)
    assert parsed[0].local
    assert verifier.format_problems(parsed) == []
    assert verifier.third_party(parsed) == []


# ── Agreement rules ──────────────────────────────────────────────────────────


def test_two_commits_for_one_action_is_reported(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        _steps(
            f"- uses: actions/checkout@{SHA_A}  # v7.0.1",
            f"- uses: actions/checkout@{SHA_B}  # v7.0.1",
        ),
    )
    problems = verifier.consistency_problems(verifier.collect_pins(path))
    assert any("2 different commits" in problem for problem in problems)


def test_two_versions_for_one_action_is_reported(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        _steps(
            f"- uses: actions/checkout@{SHA_A}  # v7.0.1",
            f"- uses: actions/checkout@{SHA_A}  # v6.0.0",
        ),
    )
    problems = verifier.consistency_problems(verifier.collect_pins(path))
    assert any("2 different versions" in problem for problem in problems)


def test_subpaths_of_one_repository_must_share_a_commit(tmp_path: Path) -> None:
    """init and analyze are one repo; two SHAs means two builds of one action."""
    path = _workflow(
        tmp_path,
        _steps(
            f"- uses: github/codeql-action/init@{SHA_A}  # v4.37.6",
            f"- uses: github/codeql-action/analyze@{SHA_B}  # v4.37.6",
        ),
    )
    problems = verifier.consistency_problems(verifier.collect_pins(path))
    assert any("2 different commits" in problem for problem in problems)


def test_subpaths_sharing_one_commit_are_accepted(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        _steps(
            f"- uses: github/codeql-action/init@{SHA_B}  # v4.37.6",
            f"- uses: github/codeql-action/analyze@{SHA_B}  # v4.37.6",
        ),
    )
    assert verifier.consistency_problems(verifier.collect_pins(path)) == []


def test_repeated_identical_pins_are_accepted(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        _steps(*[f"- uses: actions/checkout@{SHA_A}  # v7.0.1"] * 5),
    )
    assert verifier.consistency_problems(verifier.collect_pins(path)) == []


def test_malformed_pins_are_left_to_the_format_check(tmp_path: Path) -> None:
    """Agreement must not double-report what the format rules already flagged."""
    path = _workflow(tmp_path, _steps("- uses: actions/checkout@v7"))
    assert verifier.consistency_problems(verifier.collect_pins(path)) == []


# ── Upstream verification ────────────────────────────────────────────────────


def _resolver(mapping: dict[tuple[str, str], object]):
    def resolve(repository: str, version: str):
        outcome = mapping[(repository, version)]
        if isinstance(outcome, str):
            return verifier.TagResolution(sha=outcome)
        return outcome

    return resolve


def test_a_comment_matching_upstream_reports_nothing(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps(f"- uses: actions/checkout@{SHA_A}  # v7.0.1"))
    mismatches, unresolved = verifier.upstream_problems(
        verifier.collect_pins(path), _resolver({("actions/checkout", "v7.0.1"): SHA_A})
    )
    assert (mismatches, unresolved) == ([], [])


def test_a_comment_naming_the_wrong_version_is_reported(tmp_path: Path) -> None:
    """The lie this whole check exists to catch: hash and tag disagree."""
    path = _workflow(tmp_path, _steps(f"- uses: actions/checkout@{SHA_A}  # v7.0.1"))
    mismatches, unresolved = verifier.upstream_problems(
        verifier.collect_pins(path), _resolver({("actions/checkout", "v7.0.1"): SHA_B})
    )
    assert unresolved == []
    assert len(mismatches) == 1
    assert SHA_A in mismatches[0]
    assert SHA_B in mismatches[0]
    assert "w.yml:4" in mismatches[0]


def test_an_unresolvable_tag_is_reported_but_not_a_mismatch(tmp_path: Path) -> None:
    """A rate limit must never be presented as a bad pin, or be fatal."""
    path = _workflow(tmp_path, _steps(f"- uses: actions/checkout@{SHA_A}  # v7.0.1"))
    mismatches, unresolved = verifier.upstream_problems(
        verifier.collect_pins(path),
        _resolver({("actions/checkout", "v7.0.1"): verifier.TagResolution(error="rate limited")}),
    )
    assert mismatches == []
    assert unresolved == ["actions/checkout@v7.0.1: rate limited"]


def test_each_repository_version_pair_is_resolved_once(tmp_path: Path) -> None:
    """75 checkout refs must not become 75 API calls."""
    calls: list[tuple[str, str]] = []

    def resolve(repository: str, version: str):
        calls.append((repository, version))
        return verifier.TagResolution(sha=SHA_A)

    path = _workflow(
        tmp_path,
        _steps(*[f"- uses: actions/checkout@{SHA_A}  # v7.0.1"] * 12),
    )
    verifier.upstream_problems(verifier.collect_pins(path), resolve)
    assert calls == [("actions/checkout", "v7.0.1")]


def test_upstream_skips_refs_the_format_check_already_rejected(tmp_path: Path) -> None:
    path = _workflow(tmp_path, _steps("- uses: actions/checkout@v7  # v7"))
    mismatches, unresolved = verifier.upstream_problems(verifier.collect_pins(path), _resolver({}))
    assert (mismatches, unresolved) == ([], [])


# ── resolve_tag HTTP handling ────────────────────────────────────────────────


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_resolve_tag_returns_the_commit_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return _Response({"sha": SHA_A})

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)
    assert verifier.resolve_tag("actions/checkout", "v7.0.1", token="t").sha == SHA_A
    assert captured["url"] == "https://api.github.com/repos/actions/checkout/commits/v7.0.1"
    assert captured["auth"] == "Bearer t"


def test_resolve_tag_sends_no_authorization_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        captured["auth"] = request.get_header("Authorization")
        return _Response({"sha": SHA_A})

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)
    verifier.resolve_tag("actions/checkout", "v7.0.1")
    assert captured["auth"] is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [(404, "not found"), (403, "rate limited"), (429, "rate limited"), (500, "HTTP 500")],
)
def test_resolve_tag_maps_http_errors_to_a_reason(
    monkeypatch: pytest.MonkeyPatch, code: int, expected: str
) -> None:
    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        raise urllib.error.HTTPError(request.full_url, code, "boom", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)
    resolution = verifier.resolve_tag("actions/checkout", "v7.0.1")
    assert resolution.sha is None
    assert expected in (resolution.error or "")


def test_resolve_tag_survives_a_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)
    resolution = verifier.resolve_tag("actions/checkout", "v7.0.1")
    assert resolution.sha is None
    assert "lookup failed" in (resolution.error or "")


def test_resolve_tag_rejects_a_response_without_a_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verifier.urllib.request, "urlopen", lambda request, timeout=None: _Response({"x": 1})
    )
    assert verifier.resolve_tag("actions/checkout", "v7.0.1").error == (
        "response carried no commit sha"
    )


# ── This repository ──────────────────────────────────────────────────────────


def test_the_repository_passes_the_offline_checks() -> None:
    """The contract holds for the real workflows, not just synthetic fixtures."""
    assert verifier.main([]) == 0


def test_the_repository_pins_a_plausible_number_of_actions() -> None:
    """A broken enumeration must fail loudly instead of passing vacuously."""
    external = verifier.third_party(verifier.collect_all_pins())
    assert len(external) > 100, f"only discovered {len(external)} third-party refs"
    assert len({pin.repository for pin in external}) > 10


# ── Token-refused fallback ───────────────────────────────────────────────────
#
# Observed in CI: aquasecurity/setup-trivy answers 403 to a workflow's
# GITHUB_TOKEN (the org blocks the GitHub Actions app) but 200 to an anonymous
# read of the same URL. Without a fallback that pin is never verified while the
# job still reports success — a check that looks green exactly where it stopped
# looking.


def _refuse_token_then_allow_anonymous(
    monkeypatch: pytest.MonkeyPatch, code: int, anonymous_sha: str | None
) -> list[str | None]:
    attempts: list[str | None] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        auth = request.get_header("Authorization")
        attempts.append(auth)
        if auth is not None:
            raise urllib.error.HTTPError(request.full_url, code, "no", {}, None)  # type: ignore[arg-type]
        if anonymous_sha is None:
            raise urllib.error.HTTPError(request.full_url, 500, "no", {}, None)  # type: ignore[arg-type]
        return _Response({"sha": anonymous_sha})

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)
    return attempts


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_token_falls_back_to_an_anonymous_read(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    attempts = _refuse_token_then_allow_anonymous(monkeypatch, code, SHA_A)
    assert verifier.resolve_tag("aquasecurity/setup-trivy", "v0.2.6", token="t").sha == SHA_A
    assert attempts == ["Bearer t", None]


def test_the_fallback_reports_both_failures_when_anonymous_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_token_then_allow_anonymous(monkeypatch, 403, None)
    error = verifier.resolve_tag("aquasecurity/setup-trivy", "v0.2.6", token="t").error or ""
    assert "HTTP 403" in error
    assert "anonymous retry also failed" in error


def test_a_404_is_not_retried_anonymously(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing tag is an answer, not a credential problem; one call only."""
    attempts: list[str | None] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        attempts.append(request.get_header("Authorization"))
        raise urllib.error.HTTPError(request.full_url, 404, "no", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)
    assert verifier.resolve_tag("actions/checkout", "v9.9.9", token="t").sha is None
    assert attempts == ["Bearer t"]


def test_no_fallback_is_attempted_when_there_was_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying an already-anonymous request would just burn the rate limit."""
    attempts: list[str | None] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        attempts.append(request.get_header("Authorization"))
        raise urllib.error.HTTPError(request.full_url, 403, "no", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)
    assert verifier.resolve_tag("actions/checkout", "v7.0.1").sha is None
    assert attempts == [None]


# ── Request-URL safety ───────────────────────────────────────────────────────
#
# Both path components come from workflow files, which on a fork pull request
# are attacker-authored. resolve_tag shape-checks them before any request, so a
# crafted `uses:` cannot steer the URL scheme, host, or path.


@pytest.mark.parametrize(
    "repository",
    [
        "../../etc/passwd",
        "owner/repo/../../other",
        "owner",
        "owner/repo?x=1",
        "owner/repo#frag",
        "owner//repo",
        "own er/repo",
        "user:pass@host/repo",
        "",
    ],
)
def test_a_malformed_repository_is_refused_without_a_request(
    monkeypatch: pytest.MonkeyPatch, repository: str
) -> None:
    def explode(request, timeout=None):  # noqa: ANN001, ANN202
        raise AssertionError("no request may be issued for a malformed repository")

    monkeypatch.setattr(verifier.urllib.request, "urlopen", explode)
    resolution = verifier.resolve_tag(repository, "v1.0.0")
    assert resolution.sha is None
    assert "malformed repository" in (resolution.error or "")


@pytest.mark.parametrize("version", ["v1", "main", "../../v1.0.0", "v1.0.0 v2.0.0", ""])
def test_a_malformed_version_is_refused_without_a_request(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    def explode(request, timeout=None):  # noqa: ANN001, ANN202
        raise AssertionError("no request may be issued for a malformed version")

    monkeypatch.setattr(verifier.urllib.request, "urlopen", explode)
    resolution = verifier.resolve_tag("actions/checkout", version)
    assert resolution.sha is None
    assert "malformed version" in (resolution.error or "")


def test_the_request_url_is_always_the_github_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        seen.append(request.full_url)
        return _Response({"sha": SHA_A})

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fake_urlopen)
    for repository in ("actions/checkout", "github/codeql-action", "astral-sh/ruff-action"):
        verifier.resolve_tag(repository, "v1.2.3")
    assert all(url.startswith("https://api.github.com/repos/") for url in seen)
    assert len(seen) == 3


def test_every_repository_this_repo_pins_passes_the_shape_check() -> None:
    """The guard must not reject the real action names it has to look up."""
    repositories = {pin.repository for pin in verifier.third_party(verifier.collect_all_pins())}
    rejected = sorted(r for r in repositories if not verifier.REPOSITORY_RE.match(r))
    assert rejected == [], f"shape check rejects action repositories in use: {rejected}"
