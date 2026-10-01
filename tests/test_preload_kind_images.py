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
PYTHON_PINNED = f"public.ecr.aws/docker/library/python:3.14.7-slim@{DIGEST}"
LOCAL = "gco-ci-tls-proxy:local"

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


def test_a_pinned_docker_official_image_comes_from_docker_hub_by_its_digest() -> None:
    run = _Recorder()
    preload.stage_pinned_image(PYTHON_PINNED, LOCAL, run=run, sleep=pytest.fail)
    source = f"docker.io/library/python@{DIGEST}"
    assert run.calls == [["docker", "pull", source], ["docker", "tag", source, LOCAL]]


def test_a_pinned_digest_docker_hub_lacks_comes_from_ecr_public(
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = f"docker.io/library/python@{DIGEST}"
    fallback = f"public.ecr.aws/docker/library/python@{DIGEST}"
    run = _Recorder(
        {f"docker pull {source}": [1] * preload.PULL_ATTEMPTS, f"docker pull {fallback}": [1, 0]}
    )
    waits, sleep = _sleeps()
    preload.stage_pinned_image(PYTHON_PINNED, LOCAL, run=run, sleep=sleep)
    assert run.calls[-3:] == [
        ["docker", "pull", fallback],
        ["docker", "pull", fallback],
        ["docker", "tag", fallback, LOCAL],
    ]
    assert waits == [preload.PULL_DELAY_SECONDS] * (preload.PULL_ATTEMPTS - 1) + [
        preload.ECR_PUBLIC_PULL_DELAY_SECONDS
    ]
    assert f"::warning::could not pull {source}" in capsys.readouterr().out


def test_a_pinned_digest_neither_registry_serves_fails_the_preload() -> None:
    run = _Recorder(
        {"docker pull": [1] * (preload.PULL_ATTEMPTS + preload.ECR_PUBLIC_PULL_ATTEMPTS)}
    )
    sources = f"docker.io/library/python@{DIGEST} or public.ecr.aws/docker/library/python@{DIGEST}"
    with pytest.raises(
        preload.PreloadError, match=re.escape(f"could not pull {PYTHON_PINNED} from {sources}")
    ):
        preload.stage_pinned_image(PYTHON_PINNED, LOCAL, run=run, sleep=lambda _s: None)
    assert not [call for call in run.calls if call[:2] == ["docker", "tag"]]


def test_a_pinned_image_from_any_other_registry_is_pulled_by_its_own_digest() -> None:
    run = _Recorder()
    preload.stage_pinned_image(f"{ARGOCD}@{DIGEST}", LOCAL, run=run, sleep=pytest.fail)
    source = f"quay.io/argoproj/argocd@{DIGEST}"
    assert run.calls == [["docker", "pull", source], ["docker", "tag", source, LOCAL]]


@pytest.mark.parametrize(
    ("image", "local", "message"),
    [
        (REDIS, LOCAL, "is not pinned by digest"),
        (PYTHON_PINNED, f"gco-ci-tls-proxy@{DIGEST}", "must be a tag, not a digest"),
    ],
)
def test_pinning_needs_a_digest_and_a_local_tag(image: str, local: str, message: str) -> None:
    run = _Recorder()
    with pytest.raises(preload.PreloadError, match=message):
        preload.stage_pinned_image(image, local, run=run, sleep=pytest.fail)
    assert run.calls == []


def test_a_failed_local_tag_fails_the_pinned_stage() -> None:
    run = _Recorder({"docker tag": [1]})
    with pytest.raises(preload.PreloadError, match="docker tag"):
        preload.stage_pinned_image(PYTHON_PINNED, LOCAL, run=run, sleep=pytest.fail)


def _capture(platform: str = "linux/amd64\n") -> Callable[[Sequence[str]], str]:
    def capture(argv: Sequence[str]) -> str:
        assert list(argv) == list(preload.DAEMON_PLATFORM_ARGV)
        return platform

    return capture


def _loads(calls: list[list[str]], cluster: str) -> list[tuple[str, str, str]]:
    """``(image, platform, archive)`` for each save/load pair, checking the pairing."""
    pairs: list[tuple[str, str, str]] = []
    for save, load in zip(calls[0::2], calls[1::2], strict=True):
        assert save[:3] == ["docker", "save", "--platform"] and save[4] == "--output"
        assert load == ["kind", "load", "image-archive", "--name", cluster, save[5]]
        assert save[5].endswith("/image.tar") and "kind-image-" in save[5]
        pairs.append((save[6], save[3], save[5]))
    return pairs


def test_each_staged_image_is_saved_for_the_daemon_platform_and_loaded_as_an_archive() -> None:
    run = _Recorder()
    preload.load_into_kind([], "gco", run=run, capture=pytest.fail)
    assert run.calls == []
    preload.load_into_kind([ARGOCD, REDIS], "gco", run=run, capture=_capture("linux/arm64\n"))
    loads = _loads(run.calls, "gco")
    assert [(image, platform) for image, platform, _ in loads] == [
        (ARGOCD, "linux/arm64"),
        (REDIS, "linux/arm64"),
    ]
    # One archive at a time: each is gone before the next is written.
    archives = [Path(archive) for _, _, archive in loads]
    assert len({archive.parent for archive in archives}) == 2
    assert not any(archive.parent.exists() for archive in archives)


def test_the_archive_is_written_under_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # tempfile caches the directory it picked; make it look at TMPDIR again.
    monkeypatch.setattr(preload.tempfile, "tempdir", None)
    run = _Recorder()
    preload.load_into_kind([ARGOCD], "gco", run=run, capture=_capture())
    ((_image, _platform, archive),) = _loads(run.calls, "gco")
    assert Path(archive).parent.parent == tmp_path.resolve()


def test_a_failed_save_or_load_names_the_image() -> None:
    with pytest.raises(
        preload.PreloadError, match=re.escape(f"save --platform linux/amd64 {ARGOCD}")
    ):
        preload.load_into_kind(
            [ARGOCD], "gco", run=_Recorder({"docker save": [1]}), capture=_capture()
        )
    run = _Recorder({"kind load": [1]})
    with pytest.raises(preload.PreloadError, match=re.escape(f"of {ARGOCD} into gco failed")):
        preload.load_into_kind([ARGOCD], "gco", run=run, capture=_capture())
    assert len(run.calls) == 2


@pytest.mark.parametrize("reported", ["", "linux\n", "windows/amd64/v8", "error: no daemon"])
def test_an_unusable_daemon_platform_fails_before_any_save(reported: str) -> None:
    run = _Recorder()
    with pytest.raises(preload.PreloadError, match="no usable daemon platform"):
        preload.load_into_kind([ARGOCD], "gco", run=run, capture=lambda _argv: reported)
    assert run.calls == []


def test_the_default_capturer_returns_stdout_only_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([(0, "linux/amd64\n"), (1, "garbage\n")])

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        code, out = next(answers)
        return subprocess.CompletedProcess(argv, code, stdout=out)

    monkeypatch.setattr(preload.subprocess, "run", fake_run)
    assert preload._capture(preload.DAEMON_PLATFORM_ARGV) == "linux/amd64\n"
    assert preload._capture(preload.DAEMON_PLATFORM_ARGV) == ""
    assert preload.daemon_platform(capture=lambda _argv: " linux/amd64 \n") == "linux/amd64"


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
        capture=_capture(),
        sleep=pytest.fail,
        resolve=lambda _r: DIGEST,
    )
    assert code == 0
    assert [image for image, _, _ in _loads(run.calls[-4:], "gco-platform-addons")] == [
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
        ["--cluster", "c", render],
        run=run,
        capture=_capture(),
        sleep=pytest.fail,
        resolve=lambda _r: DIGEST,
    )
    assert code == 1
    assert f"::error::kind load image-archive of {ARGOCD} into c failed" in capsys.readouterr().out


def test_main_stages_a_pinned_image_under_its_local_tag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _Recorder()
    code = preload.main(
        ["--cluster", "gco-examples-smoke", "--pinned", f"{PYTHON_PINNED}={LOCAL}"],
        run=run,
        capture=_capture(),
        sleep=pytest.fail,
        resolve=pytest.fail,
    )
    assert code == 0
    assert [image for image, _, _ in _loads(run.calls[-2:], "gco-examples-smoke")] == [LOCAL]
    out = capsys.readouterr().out
    assert "loaded 1 pinned image(s) under local tags into kind cluster gco-examples-smoke" in out
    assert "preloaded" not in out


def test_main_loads_rendered_pinned_and_local_images_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    render = _manifest(tmp_path, "a.yaml", ARGOCD_RENDER)
    run = _Recorder()
    code = preload.main(
        [
            "--cluster",
            "c",
            "--pinned",
            f" {PYTHON_PINNED} = {LOCAL} ",
            "--image",
            " cost-monitor:ci ",
            render,
            "--image",
            "busybox:1.38.0",
        ],
        run=run,
        capture=_capture(),
        sleep=pytest.fail,
        resolve=lambda _r: DIGEST,
    )
    assert code == 0
    assert [image for image, _, _ in _loads(run.calls[-10:], "c")] == [
        ARGOCD,
        REDIS,
        LOCAL,
        "cost-monitor:ci",
        "busybox:1.38.0",
    ]
    # A --image is already in Docker: nothing is pulled or tagged for it.
    assert not [call for call in run.calls if call[:2] == ["docker", "pull"] and "ci" in call[2]]
    out = capsys.readouterr().out
    assert "preloaded 2 of 2 image(s) into kind cluster c" in out
    assert "loaded 1 pinned image(s) under local tags into kind cluster c" in out
    assert "loaded 2 local image(s) into kind cluster c" in out


def test_main_loads_only_local_images_without_a_pull(capsys: pytest.CaptureFixture[str]) -> None:
    run = _Recorder()
    code = preload.main(
        ["--cluster", "gco-ci", "--image", "health-monitor:ci"],
        run=run,
        capture=_capture(),
        sleep=pytest.fail,
        resolve=pytest.fail,
    )
    assert code == 0
    assert [image for image, _, _ in _loads(run.calls, "gco-ci")] == ["health-monitor:ci"]
    out = capsys.readouterr().out
    assert "loaded 1 local image(s) into kind cluster gco-ci" in out
    assert "preloaded" not in out and "pinned" not in out


def test_main_reports_a_pinned_failure_as_an_annotation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _Recorder()
    code = preload.main(
        ["--cluster", "c", "--pinned", f"{REDIS}={LOCAL}"],
        run=run,
        sleep=pytest.fail,
        resolve=pytest.fail,
    )
    assert code == 1
    assert f"::error::{REDIS} is not pinned by digest" in capsys.readouterr().out
    assert run.calls == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ([], "IMAGE=LOCAL, --image NAME"),
        (["--pinned", "no-separator"], "IMAGE=LOCAL"),
        (["--pinned", f"={LOCAL}"], "IMAGE=LOCAL"),
        (["--pinned", f"{PYTHON_PINNED}= "], "IMAGE=LOCAL"),
        (["--image", " "], "cannot parse the image reference"),
        (["--image", "redis:"], "cannot parse the image reference"),
        (["--image", PYTHON_PINNED], "pinned by digest, which kind cannot address"),
    ],
    ids=[
        "nothing to stage",
        "no separator",
        "no image",
        "no local tag",
        "blank image",
        "malformed image",
        "digest image",
    ],
)
def test_main_refuses_nothing_to_stage_or_a_malformed_argument(
    extra: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        preload.main(
            ["--cluster", "c", *extra], run=_Recorder(), capture=pytest.fail, resolve=pytest.fail
        )
    assert excinfo.value.code == 2
    assert message in capsys.readouterr().err
