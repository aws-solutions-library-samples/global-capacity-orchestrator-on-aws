"""Tests for the kind image preload (``.github/scripts/preload_kind_images.py``).

``integration:kind:platform-addons`` kept failing on one image: Argo CD's
Redis, which the chart runs from Amazon ECR Public. ECR Public allows anonymous
clients one pull request a second, a pull by tag sends its manifest requests
back to back, and the kubelet's backoff ran out of the install's wait before a
pull got through. The script pulls every image a rendered chart runs on the
runner, with retry, and loads it into the node. A Docker Official Image on ECR
Public is fetched by its ECR Public digest from Docker Hub, which publishes the
identical index. These tests cover every branch: which images a render runs,
how references normalize, the paced registry requests and what counts as an
answer, and each pull path, all without a network or a container runtime.
"""

from __future__ import annotations

import email.message
import importlib.util
import io
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "preload_kind_images.py"
_spec = importlib.util.spec_from_file_location("preload_kind_images", _SCRIPT)
assert _spec is not None and _spec.loader is not None
preload = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("preload_kind_images", preload)
_spec.loader.exec_module(preload)

REDIS = "ecr-public.aws.com/docker/library/redis:8.6.4-alpine"
ARGOCD = "quay.io/argoproj/argocd:v3.5.3"
DIGEST = "sha256:" + "2c" * 32
OTHER_DIGEST = "sha256:" + "ab" * 32

ARGOCD_RENDER = """\
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: argocd-server
spec:
  template:
    spec:
      initContainers:
        - name: copyutil
          image: quay.io/argoproj/argocd:v3.5.3
      containers:
        - name: server
          image: quay.io/argoproj/argocd:v3.5.3
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: argocd-redis
spec:
  template:
    spec:
      containers:
        - name: redis
          image: ecr-public.aws.com/docker/library/redis:8.6.4-alpine
---
apiVersion: batch/v1
kind: Job
metadata:
  name: argocd-redis-secret-init
  annotations:
    helm.sh/hook: pre-install,pre-upgrade
spec:
  template:
    spec:
      containers:
        - name: secret-init
          image: quay.io/argoproj/argocd:v3.5.3
---
apiVersion: v1
kind: Pod
metadata:
  name: argocd-test-connection
  annotations:
    helm.sh/hook: test
spec:
  containers:
    - name: wget
      image: busybox
"""


class _Recorder:
    """A command runner: records every argv and answers from a queue of exit codes."""

    def __init__(self, codes: dict[str, list[int]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._codes = codes or {}

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        key = " ".join(argv[:2])
        queue = self._codes.get(" ".join(argv), self._codes.get(key, []))
        return queue.pop(0) if queue else 0


def _sleeps() -> tuple[list[float], Callable[[float], None]]:
    recorded: list[float] = []
    return recorded, recorded.append


class _Response:
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, retry_after: str | None = None, *, headers: bool = True) -> Exception:
    message = email.message.Message()
    if retry_after is not None:
        message["Retry-After"] = retry_after
    # The stubs type an HTTPError's headers as always present; the script copes without them.
    hdrs = message if headers else cast(email.message.Message, None)
    return urllib.error.HTTPError("https://registry.invalid/", code, "error", hdrs, io.BytesIO(b""))


class _Opener:
    """Answers each request from a queue: a response or an exception to raise."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        assert timeout == preload.REQUEST_TIMEOUT_SECONDS
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


# ─── Image references ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (ARGOCD, ("quay.io", "argoproj/argocd", "v3.5.3", None)),
        (REDIS, ("ecr-public.aws.com", "docker/library/redis", "8.6.4-alpine", None)),
        ("busybox", ("docker.io", "library/busybox", "latest", None)),
        ("redis:8", ("docker.io", "library/redis", "8", None)),
        ("bitnami/redis:7", ("docker.io", "bitnami/redis", "7", None)),
        ("localhost/tool:1", ("localhost", "tool", "1", None)),
        ("registry:5000/team/app:2", ("registry:5000", "team/app", "2", None)),
        (f"redis@{DIGEST}", ("docker.io", "library/redis", None, DIGEST)),
        (f" quay.io/a/b:1@{DIGEST} ", ("quay.io", "a/b", "1", DIGEST)),
    ],
)
def test_references_normalize_like_docker(reference: str, expected: tuple[Any, ...]) -> None:
    parsed = preload.ImageReference.parse(reference)
    assert (parsed.registry, parsed.repository, parsed.tag, parsed.digest) == expected


@pytest.mark.parametrize(
    "reference", ["", "redis:", "quay.io/", "quay.io/team//app:1", "redis@sha256:short"]
)
def test_malformed_references_are_refused(reference: str) -> None:
    with pytest.raises(preload.PreloadError, match="cannot parse"):
        preload.ImageReference.parse(reference)


@pytest.mark.parametrize(
    ("reference", "twin"),
    [
        (REDIS, "docker.io/library/redis"),
        ("public.ecr.aws/docker/library/nginx:1.29", "docker.io/library/nginx"),
        ("public.ecr.aws/eks/aws-load-balancer-controller:v2", None),
        (ARGOCD, None),
        ("redis:8", None),
    ],
)
def test_only_ecr_public_docker_official_images_have_a_docker_hub_twin(
    reference: str, twin: str | None
) -> None:
    assert preload.ImageReference.parse(reference).docker_hub_twin() == twin


# ─── Which images a render runs ────────────────────────────────────


def test_a_render_runs_its_workloads_and_hooks_but_not_its_tests() -> None:
    assert preload.collect_images([ARGOCD_RENDER]) == [ARGOCD, REDIS]


def test_images_are_collected_across_files_wherever_a_pod_template_sits() -> None:
    cron = """\
kind: CronJob
metadata: {name: nightly, annotations: {helm.sh/hook: "pre-install, test-success"}}
spec: {jobTemplate: {spec: {template: {spec: {containers: [{image: skipped:1}]}}}}}
---
kind: CronJob
metadata: []
spec:
  jobTemplate:
    spec:
      template:
        spec:
          ephemeralContainers:
            - {image: " debug:1 "}
          containers:
            - "not a container"
            - {name: no-image}
            - {image: ""}
            - {image: 7}
            - {image: app:1}
          volumes: {containers: "a key that only looks like a pod field"}
---
- a top-level list is not a manifest
---
kind: Deployment
metadata: {annotations: []}
spec: {template: {spec: {containers: [{image: app:1}, {image: sidecar:2}]}}}
---
kind: List
items:
  - kind: Pod
    spec: {containers: [{image: listed:3}]}
"""
    assert preload.collect_images([cron, "", ARGOCD_RENDER]) == [
        "debug:1",
        "app:1",
        "sidecar:2",
        "listed:3",
        ARGOCD,
        REDIS,
    ]


# ─── The paced registry requests ───────────────────────────────────


def test_a_registry_request_returns_headers_and_body() -> None:
    opener = _Opener(_Response(b"body", {"Docker-Content-Digest": DIGEST}))
    headers, body = preload._registry_request(
        "https://ecr-public.aws.com/x", headers={"A": "b"}, opener=opener, sleep=pytest.fail
    )
    assert body == b"body" and headers["Docker-Content-Digest"] == DIGEST
    assert opener.requests[0].get_method() == "GET"
    assert opener.requests[0].get_header("A") == "b"


def test_throttling_and_transport_errors_are_retried_with_growing_waits() -> None:
    opener = _Opener(
        _http_error(429),
        _http_error(429, "7"),
        _http_error(429, "Wed, 21 Oct 2026 07:28:00 GMT"),
        _http_error(429, headers=False),
        urllib.error.URLError("reset"),
        TimeoutError("slow"),
        _Response(b"ok"),
    )
    waits, sleep = _sleeps()
    _headers, body = preload._registry_request(
        "https://ecr-public.aws.com/x", method="HEAD", opener=opener, sleep=sleep, spacing=1.5
    )
    assert body == b"ok"
    assert opener.requests[0].get_method() == "HEAD"
    # 1.5 doubling, capped at 20; a numeric Retry-After lengthens a wait.
    assert waits == [1.5, 7.0, 6.0, 12.0, 20.0, 20.0]


def test_an_answer_other_than_429_fails_at_once() -> None:
    opener = _Opener(_http_error(404))
    with pytest.raises(preload.PreloadError, match="answered HTTP 404") as raised:
        preload._registry_request("https://ecr-public.aws.com/x", opener=opener, sleep=pytest.fail)
    assert not isinstance(raised.value, preload.RegistryUnreachable)


def test_running_out_of_attempts_means_the_registry_never_answered() -> None:
    opener = _Opener(_http_error(429), _http_error(429), _http_error(429))
    waits, sleep = _sleeps()
    with pytest.raises(preload.RegistryUnreachable, match=r"after 3 attempts \(HTTP 429"):
        preload._registry_request(
            "https://ecr-public.aws.com/x", opener=opener, sleep=sleep, attempts=3, spacing=2.0
        )
    assert waits == [2.0, 4.0]


def test_the_digest_lookup_is_one_token_and_one_paced_head() -> None:
    opener = _Opener(
        _Response(json.dumps({"token": "anon"}).encode()),
        _Response(b"", {"Docker-Content-Digest": DIGEST}),
    )
    waits, sleep = _sleeps()
    reference = preload.ImageReference.parse(REDIS)
    assert preload.ecr_public_digest(reference, opener=opener, sleep=sleep) == DIGEST
    token, head = opener.requests
    assert token.full_url == (
        "https://ecr-public.aws.com/token/?service=public.ecr.aws"
        "&scope=repository:docker/library/redis:pull"
    )
    assert head.get_method() == "HEAD"
    assert head.full_url == (
        "https://ecr-public.aws.com/v2/docker/library/redis/manifests/8.6.4-alpine"
    )
    assert head.get_header("Authorization") == "Bearer anon"
    assert "application/vnd.oci.image.index.v1+json" in str(head.get_header("Accept"))
    assert waits == [preload.REQUEST_SPACING_SECONDS]


def test_a_reference_pinned_by_digest_needs_no_lookup() -> None:
    reference = preload.ImageReference.parse(f"public.ecr.aws/docker/library/redis@{DIGEST}")
    assert preload.ecr_public_digest(reference, opener=_Opener(), sleep=pytest.fail) == DIGEST


@pytest.mark.parametrize("body", [b"not json", b"{}", b"[]"])
def test_a_token_endpoint_without_a_token_fails(body: bytes) -> None:
    opener = _Opener(_Response(body))
    with pytest.raises(preload.PreloadError, match="no anonymous token"):
        preload.ecr_public_digest(
            preload.ImageReference.parse(REDIS), opener=opener, sleep=pytest.fail
        )


@pytest.mark.parametrize("headers", [{}, {"Docker-Content-Digest": "sha256:short"}])
def test_a_manifest_without_a_usable_digest_fails(headers: dict[str, str]) -> None:
    opener = _Opener(_Response(json.dumps({"token": "t"}).encode()), _Response(b"", headers))
    with pytest.raises(preload.PreloadError, match="no usable Docker-Content-Digest"):
        preload.ecr_public_digest(
            preload.ImageReference.parse(REDIS), opener=opener, sleep=lambda _s: None
        )


def test_the_default_opener_and_runner_are_urllib_and_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> str:
        seen["urlopen"] = (request.full_url, timeout)
        return "response"

    def fake_run(argv: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        seen["run"] = (argv, check)
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(preload.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(preload.subprocess, "run", fake_run)
    request = urllib.request.Request("https://ecr-public.aws.com/v2/")
    assert preload._open(request, 5.0) == "response"
    assert preload._run(("docker", "pull", "x")) == 3
    assert seen == {
        "urlopen": ("https://ecr-public.aws.com/v2/", 5.0),
        "run": (["docker", "pull", "x"], False),
    }


# ─── Pulls, tags and the kind load ─────────────────────────────────


def test_a_pull_is_retried_with_a_fixed_wait() -> None:
    run = _Recorder({"docker pull": [1, 1, 0]})
    waits, sleep = _sleeps()
    assert preload.pull_with_retry("app:1", run=run, sleep=sleep, attempts=4, delay=15.0)
    assert run.calls == [["docker", "pull", "app:1"]] * 3
    assert waits == [15.0, 15.0]


def test_a_pull_that_never_succeeds_reports_false() -> None:
    run = _Recorder({"docker pull": [1, 1]})
    waits, sleep = _sleeps()
    assert not preload.pull_with_retry("app:1", run=run, sleep=sleep, attempts=2, delay=3.0)
    assert waits == [3.0]


def test_an_image_from_any_other_registry_is_pulled_as_written() -> None:
    run = _Recorder()
    assert preload.stage_image(ARGOCD, run=run, sleep=pytest.fail, resolve=pytest.fail)
    assert run.calls == [["docker", "pull", ARGOCD]]


def test_an_image_that_cannot_be_pulled_fails_the_preload() -> None:
    run = _Recorder({"docker pull": [1] * preload.PULL_ATTEMPTS})
    with pytest.raises(preload.PreloadError, match=re.escape(f"could not pull {ARGOCD}")):
        preload.stage_image(ARGOCD, run=run, sleep=lambda _s: None, resolve=pytest.fail)


def test_a_digest_pinned_image_is_left_to_the_kubelet(capsys: pytest.CaptureFixture[str]) -> None:
    run = _Recorder()
    assert not preload.stage_image(f"{REDIS}@{DIGEST}", run=run, resolve=pytest.fail)
    assert run.calls == []
    assert "kind load cannot address" in capsys.readouterr().out


def test_ecr_public_redis_comes_from_docker_hub_by_the_ecr_public_digest() -> None:
    run = _Recorder()
    resolved: list[Any] = []

    def resolve(reference: Any) -> str:
        resolved.append(reference)
        return DIGEST

    assert preload.stage_image(REDIS, run=run, sleep=pytest.fail, resolve=resolve)
    assert resolved == [preload.ImageReference.parse(REDIS)]
    source = f"docker.io/library/redis@{DIGEST}"
    assert run.calls == [["docker", "pull", source], ["docker", "tag", source, REDIS]]


def test_a_digest_docker_hub_lacks_is_pulled_from_ecr_public_by_digest() -> None:
    source = f"docker.io/library/redis@{DIGEST}"
    fallback = f"ecr-public.aws.com/docker/library/redis@{DIGEST}"
    run = _Recorder(
        {f"docker pull {source}": [1] * preload.PULL_ATTEMPTS, f"docker pull {fallback}": [1, 0]}
    )
    waits, sleep = _sleeps()
    assert preload.stage_image(REDIS, run=run, sleep=sleep, resolve=lambda _r: DIGEST)
    assert run.calls[-3:] == [
        ["docker", "pull", fallback],
        ["docker", "pull", fallback],
        ["docker", "tag", fallback, REDIS],
    ]
    assert waits == [preload.PULL_DELAY_SECONDS] * (preload.PULL_ATTEMPTS - 1) + [
        preload.ECR_PUBLIC_PULL_DELAY_SECONDS
    ]


def test_a_digest_neither_registry_serves_fails_the_preload() -> None:
    run = _Recorder(
        {"docker pull": [1] * (preload.PULL_ATTEMPTS + preload.ECR_PUBLIC_PULL_ATTEMPTS)}
    )
    with pytest.raises(preload.PreloadError, match="from Docker Hub or, after 12 attempts"):
        preload.stage_image(REDIS, run=run, sleep=lambda _s: None, resolve=lambda _r: DIGEST)


def test_when_ecr_public_never_answers_docker_hubs_copy_of_the_tag_is_used(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def resolve(_reference: Any) -> str:
        raise preload.RegistryUnreachable("HEAD ... failed after 8 attempts (HTTP 429)")

    run = _Recorder()
    assert preload.stage_image(REDIS, run=run, sleep=pytest.fail, resolve=resolve)
    source = "docker.io/library/redis:8.6.4-alpine"
    assert run.calls == [["docker", "pull", source], ["docker", "tag", source, REDIS]]
    assert "without the digest check" in capsys.readouterr().out


def test_when_ecr_public_never_answers_and_docker_hub_fails_the_preload_fails() -> None:
    def resolve(_reference: Any) -> str:
        raise preload.RegistryUnreachable("throttled")

    run = _Recorder({"docker pull": [1] * preload.PULL_ATTEMPTS})
    with pytest.raises(
        preload.PreloadError, match=re.escape("could not pull docker.io/library/redis")
    ):
        preload.stage_image(REDIS, run=run, sleep=lambda _s: None, resolve=resolve)


def test_a_definitive_ecr_public_answer_is_never_papered_over() -> None:
    def resolve(_reference: Any) -> str:
        raise preload.PreloadError("HEAD ... answered HTTP 404")

    run = _Recorder()
    with pytest.raises(preload.PreloadError, match="HTTP 404"):
        preload.stage_image(REDIS, run=run, sleep=pytest.fail, resolve=resolve)
    assert run.calls == []


def test_a_failed_tag_fails_the_preload() -> None:
    run = _Recorder({"docker tag": [1]})
    with pytest.raises(preload.PreloadError, match="docker tag"):
        preload.stage_image(REDIS, run=run, sleep=pytest.fail, resolve=lambda _r: OTHER_DIGEST)


def test_kind_loads_every_staged_image_in_one_call() -> None:
    run = _Recorder()
    preload.load_into_kind([], "gco", run=run)
    assert run.calls == []
    preload.load_into_kind([ARGOCD, REDIS], "gco", run=run)
    assert run.calls == [["kind", "load", "docker-image", "--name", "gco", ARGOCD, REDIS]]
    with pytest.raises(preload.PreloadError, match="kind load"):
        preload.load_into_kind([ARGOCD], "gco", run=_Recorder({"kind load": [1]}))


# ─── The command ───────────────────────────────────────────────────


def _manifest(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_main_stages_and_loads_what_the_render_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned = f"kind: Pod\nspec: {{containers: [{{image: 'quay.io/x/y@{DIGEST}'}}]}}\n"
    files = [_manifest(tmp_path, "a.yaml", ARGOCD_RENDER), _manifest(tmp_path, "b.yaml", pinned)]
    run = _Recorder()
    code = preload.main(
        ["--cluster", "gco-platform-addons", *files],
        run=run,
        sleep=pytest.fail,
        resolve=lambda _r: DIGEST,
    )
    assert code == 0
    assert run.calls[-1] == [
        "kind",
        "load",
        "docker-image",
        "--name",
        "gco-platform-addons",
        ARGOCD,
        REDIS,
    ]
    assert (
        "preloaded 2 of 3 image(s) into kind cluster gco-platform-addons" in capsys.readouterr().out
    )


def test_main_fails_a_render_that_runs_no_images(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = _manifest(tmp_path, "empty.yaml", "kind: ConfigMap\nmetadata: {name: x}\n")
    assert preload.main(["--cluster", "c", empty], run=_Recorder(), resolve=pytest.fail) == 1
    assert "run no container images" in capsys.readouterr().out


def test_main_reports_a_preload_failure_as_an_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    render = _manifest(tmp_path, "a.yaml", ARGOCD_RENDER)
    run = _Recorder({"kind load": [1]})
    code = preload.main(
        ["--cluster", "c", render], run=run, sleep=pytest.fail, resolve=lambda _r: DIGEST
    )
    assert code == 1
    assert "::error::kind load docker-image into c failed" in capsys.readouterr().out
