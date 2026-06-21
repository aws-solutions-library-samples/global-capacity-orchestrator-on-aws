"""Contract tests that exercise the *actual* Mooncake vLLM image.

These are integration-style tests: they run the real upstream image GCO
defaults disaggregated/store/both deploys to — the one pinned in
``cli.images._DISAGGREGATED_DEFAULT_IMAGE`` — instead of a fake. They exist to
catch the class of regressions that only surface against the real image and
bit us during live testing, so an image-version bump is validated by CI rather
than discovered in production:

1. **The proxy starts under the image's interpreter.** The image ships
   ``python3`` but no ``python`` on ``PATH``; launching the bundled
   prefill-decode router with the wrong interpreter crash-loops the proxy
   (``StartError: exec: "python": executable file not found in $PATH``). This
   test runs the router exactly as the pod does — ``python3
   /etc/pd-proxy/mooncake_pd_proxy.py`` against a read-only mount of the
   shipped script — and asserts it serves ``/healthz``. It also implicitly
   verifies the image still bundles ``fastapi``/``uvicorn``/``httpx``.

2. **The rendered store config is accepted by the image's loader.** The
   shared KV-cache store runs embedded in each vLLM pod, and embedded mode
   rejects ``global_segment_size == 0`` (``embedded mode requires
   global_segment_size > 0``). This test feeds the *real*
   ``render_mooncake_config`` output to the image's ``MooncakeStoreConfig``
   loader and asserts it parses — and, as a guard that the check has teeth,
   that an explicit zero segment is still rejected.

3. **The connector names GCO emits are registered.** GCO's
   ``--kv-transfer-config`` names ``MooncakeConnector`` /
   ``MooncakeStoreConnector`` / ``MultiConnector``; an upstream rename would
   silently break every Mooncake deploy. This test asserts every connector
   name GCO actually emits is present in the image's ``KVConnectorFactory``
   registry.

Running these:

- The suite is gated behind ``GCO_MOONCAKE_IMAGE_TEST=1`` so it never runs in
  the normal unit job (which has no business pulling a ~9 GB image). The
  dedicated ``mooncake-image`` workflow sets it.
- The container runtime defaults to ``docker`` but can be overridden with
  ``GCO_CONTAINER_RUNTIME`` (e.g. ``finch``, ``podman``) for local runs.
- The image reference is read from ``cli.images`` at run time, so the test
  always validates whatever version GCO currently defaults to — bump the
  constant and CI validates the new image.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Gating + configuration
# --------------------------------------------------------------------------

_ENABLED = os.environ.get("GCO_MOONCAKE_IMAGE_TEST") == "1"
_RUNTIME = os.environ.get("GCO_CONTAINER_RUNTIME", "docker")

# Skip the whole module unless explicitly enabled. The module is still imported
# during collection, so everything above the test bodies must import without
# the inference-monitor / CDK extras — heavier imports (cli.images,
# gco.services.inference_monitor) are deferred into the fixtures/tests, which
# only execute when the suite is enabled (i.e. in the dedicated CI job that
# installs those extras and pulls the image).
pytestmark = [
    pytest.mark.mooncake_image,
    pytest.mark.skipif(
        not _ENABLED,
        reason=(
            "Mooncake image contract test is opt-in: set GCO_MOONCAKE_IMAGE_TEST=1 "
            "(it pulls and runs the ~9GB vLLM image). Runs in the 'mooncake-image' "
            "CI workflow."
        ),
    ),
]

# Proxy container layout, mirrored from gco.services.inference_monitor so the
# test runs the script from the exact path the pod uses. Imported lazily in a
# fixture rather than at module scope to keep collection light.
_PROXY_MOUNT_DIR = "/etc/pd-proxy"
_PROXY_SCRIPT_FILENAME = "mooncake_pd_proxy.py"
_PROXY_CONTAINER_PORT = 8000

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROXY_SCRIPT = _REPO_ROOT / "gco" / "services" / _PROXY_SCRIPT_FILENAME


# --------------------------------------------------------------------------
# Container-runtime helpers
# --------------------------------------------------------------------------


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run ``<runtime> <args...>`` capturing text output (no raise)."""
    return subprocess.run(
        [_RUNTIME, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _runtime_available() -> bool:
    try:
        return _run(["version"], timeout=30).returncode == 0
    except FileNotFoundError, subprocess.SubprocessError:
        return False


def _free_port() -> int:
    """Reserve an ephemeral localhost port for the proxy port-mapping."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def image() -> str:
    """Resolve the GCO default Mooncake image and ensure it is present locally.

    The reference comes from ``cli.images`` so the test tracks whatever version
    GCO defaults to. If the runtime can't see the image yet it is pulled (the
    dedicated CI job pulls it in a prior step; this keeps local runs
    self-contained).
    """
    if not _runtime_available():
        pytest.skip(f"container runtime {_RUNTIME!r} not available")

    from cli.images import _DISAGGREGATED_DEFAULT_IMAGE  # lazy: keep collection light

    ref = _DISAGGREGATED_DEFAULT_IMAGE
    if _run(["image", "inspect", ref], timeout=60).returncode != 0:
        # Not present — pull it (large; allow plenty of time).
        pulled = _run(["pull", ref], timeout=1800)
        if pulled.returncode != 0:
            pytest.fail(f"failed to pull {ref}:\n{pulled.stdout}\n{pulled.stderr}")
    return ref


def _python_in_image(
    image_ref: str, code: str, *, mounts: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a one-shot ``python3 -c <code>`` inside the image.

    The image's ENTRYPOINT is ``["vllm", "serve"]``, so ``--entrypoint python3``
    is required to run the interpreter directly — this mirrors how the
    Kubernetes ``command:`` field overrides the image ENTRYPOINT in production.
    """
    args = ["run", "--rm", "--entrypoint", "python3"]
    for m in mounts or []:
        args += ["-v", m]
    args += [image_ref, "-c", code]
    return _run(args, timeout=300)


# --------------------------------------------------------------------------
# 1. The proxy starts under python3 and serves /healthz
# --------------------------------------------------------------------------


def test_proxy_starts_and_serves_health(image: str) -> None:
    """The bundled PD router runs under the image's python3 and serves health.

    Reproduces the production launch (``python3 /etc/pd-proxy/<script>`` over a
    read-only mount of the shipped script) so a missing ``python3``, a dropped
    ``fastapi``/``uvicorn``/``httpx`` dependency, or a script/app-construction
    break fails here instead of crash-looping the proxy pod.
    """
    assert _PROXY_SCRIPT.is_file(), f"proxy script not found at {_PROXY_SCRIPT}"

    port = _free_port()
    name = f"gco-mooncake-proxy-ci-{os.getpid()}-{port}"
    started = _run(
        [
            "run",
            "-d",
            "--name",
            name,
            # The image ENTRYPOINT is ["vllm", "serve"]; override it with the
            # interpreter exactly as the pod's `command:` does in production.
            "--entrypoint",
            "python3",
            "-p",
            f"127.0.0.1:{port}:{_PROXY_CONTAINER_PORT}",
            "-v",
            f"{_PROXY_SCRIPT.parent}:{_PROXY_MOUNT_DIR}:ro",
            "-e",
            f"PD_PROXY_PORT={_PROXY_CONTAINER_PORT}",
            "-e",
            "PD_PROXY_PREFILL_URL=http://prefill.invalid:8000",
            "-e",
            "PD_PROXY_DECODE_URL=http://decode.invalid:8000",
            "-e",
            "ADMIN_API_KEY=ci-test",
            image,
            f"{_PROXY_MOUNT_DIR}/{_PROXY_SCRIPT_FILENAME}",
        ],
        timeout=120,
    )
    assert started.returncode == 0, f"failed to start proxy container:\n{started.stderr}"

    try:
        deadline = time.monotonic() + 90
        last_err: str = ""
        body: str | None = None
        while time.monotonic() < deadline:
            # If the container exited (e.g. StartError / import failure), fail
            # fast with its logs rather than waiting out the full deadline.
            state = _run(["inspect", "-f", "{{.State.Running}}", name], timeout=30).stdout.strip()
            if state == "false":
                logs = _run(["logs", name], timeout=30)
                pytest.fail(
                    "proxy container exited before serving /healthz "
                    f"(python3 launch failed?):\n{logs.stdout}\n{logs.stderr}"
                )
            # HTTP-only health probe (http.client, not urllib): the URL is a
            # fixed localhost path on a locally-allocated port, and http.client
            # can't be coerced into a file:// read the way urllib can.
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/healthz")
                    resp = conn.getresponse()
                    if resp.status == 200:
                        body = resp.read().decode()
                        break
                finally:
                    conn.close()
            except (http.client.HTTPException, ConnectionError, OSError) as exc:
                last_err = str(exc)
            time.sleep(2)

        if body is None:
            logs = _run(["logs", name], timeout=30)
            pytest.fail(
                f"proxy did not serve /healthz within the deadline (last error: {last_err}):"
                f"\n{logs.stdout}\n{logs.stderr}"
            )
        assert json.loads(body) == {"status": "ok"}
    finally:
        _run(["rm", "-f", name], timeout=60)


# --------------------------------------------------------------------------
# 2. The rendered store config is accepted by the image's loader
# --------------------------------------------------------------------------


def _render_store_config(overrides: dict[str, object] | None = None) -> dict[str, object]:
    """Render a store-mode mooncake.json with GCO's real renderer."""
    from gco.services.inference_monitor import render_mooncake_config  # lazy import

    store: dict[str, object] = {"enabled": True}
    if overrides:
        store.update(overrides)
    region_services = {
        "metadata_server": "http://mooncake-master:8080/metadata",
        "master_server_address": "mooncake-master:50051",
    }
    return render_mooncake_config({"store": store}, region_services)


def test_store_config_accepted_by_image(image: str, tmp_path: Path) -> None:
    """The image's ``MooncakeStoreConfig`` accepts GCO's rendered store config.

    Catches both a regression in the rendered ``global_segment_size`` default
    (the embedded store rejects ``0``) and an upstream schema change that would
    make the rendered file unparseable.
    """
    cfg = _render_store_config()
    # Sanity: the renderer must not emit a zero segment for an embedded store.
    assert cfg["global_segment_size"] not in ("0", 0)
    (tmp_path / "mooncake.json").write_text(json.dumps(cfg), encoding="utf-8")

    loader = (
        "from vllm.distributed.kv_transfer.kv_connector.v1.mooncake.store.worker "
        "import MooncakeStoreConfig; "
        "MooncakeStoreConfig.from_file('/cfg/mooncake.json'); "
        "print('STORE_CONFIG_OK')"
    )
    result = _python_in_image(image, loader, mounts=[f"{tmp_path}:/cfg:ro"])
    assert result.returncode == 0 and "STORE_CONFIG_OK" in result.stdout, (
        f"image rejected GCO-rendered store config:\n{result.stdout}\n{result.stderr}"
    )


def test_zero_global_segment_size_is_rejected_by_image(image: str, tmp_path: Path) -> None:
    """Guard: a zero ``global_segment_size`` is rejected by the image's loader.

    This is the exact failure the rendered-default fix avoids; asserting the
    image still rejects it proves the positive test above has teeth — if a
    future image silently accepted ``0`` the default wouldn't matter, and we'd
    want to know the contract changed.
    """
    cfg = _render_store_config({"global_segment_size": "0"})
    (tmp_path / "mooncake.json").write_text(json.dumps(cfg), encoding="utf-8")

    loader = (
        "from vllm.distributed.kv_transfer.kv_connector.v1.mooncake.store.worker "
        "import MooncakeStoreConfig; "
        "MooncakeStoreConfig.from_file('/cfg/mooncake.json')"
    )
    result = _python_in_image(image, loader, mounts=[f"{tmp_path}:/cfg:ro"])
    assert result.returncode != 0, (
        "expected the image to reject global_segment_size=0 for the embedded "
        f"store, but it succeeded:\n{result.stdout}\n{result.stderr}"
    )


# --------------------------------------------------------------------------
# 3. The connector names GCO emits are registered in the image
# --------------------------------------------------------------------------


def _gco_connector_names() -> set[str]:
    """Every connector name GCO emits across its supported (mode, role) pairs."""
    from gco.services.inference_monitor import build_kv_transfer_config  # lazy import

    names: set[str] = set()
    pairs = [
        ({"mode": "disaggregated"}, "prefill"),
        ({"mode": "disaggregated"}, "decode"),
        ({"mode": "store"}, "single"),
        ({"mode": "both"}, "prefill"),
        ({"mode": "both"}, "decode"),
    ]
    for mooncake, role in pairs:
        cfg = json.loads(build_kv_transfer_config(mooncake, role))
        names.add(cfg["kv_connector"])
        for sub in cfg.get("kv_connector_extra_config", {}).get("connectors", []):
            names.add(sub["kv_connector"])
    return names


def test_connector_names_registered_in_image(image: str) -> None:
    """Every connector GCO names is registered in the image's factory.

    An upstream rename/removal of ``MooncakeConnector`` /
    ``MooncakeStoreConnector`` / ``MultiConnector`` would make GCO's
    ``--kv-transfer-config`` reference a connector vLLM no longer knows,
    breaking deploys; this fails CI on the image bump instead.
    """
    expected = _gco_connector_names()
    # Guards against the helper silently returning nothing.
    assert {"MooncakeConnector", "MooncakeStoreConnector", "MultiConnector"} <= expected

    probe = (
        "from vllm.distributed.kv_transfer.kv_connector.factory "
        "import KVConnectorFactory as K; "
        "import json; "
        "print('REGISTRY=' + json.dumps(sorted(getattr(K, '_registry', {}).keys())))"
    )
    result = _python_in_image(image, probe)
    assert result.returncode == 0, f"failed to read connector registry:\n{result.stderr}"

    line = next((ln for ln in result.stdout.splitlines() if ln.startswith("REGISTRY=")), None)
    assert line is not None, f"connector registry probe produced no output:\n{result.stdout}"
    registered = set(json.loads(line[len("REGISTRY=") :]))
    assert registered, "image reported an empty connector registry"

    missing = expected - registered
    assert not missing, (
        f"connectors GCO emits are not registered in {image}: {sorted(missing)}. "
        f"Registered: {sorted(registered)}"
    )
