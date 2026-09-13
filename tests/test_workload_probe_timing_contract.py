"""Every container probe must state its own timeout and cold-start budget.

The kubelet defaults ``timeoutSeconds`` to **1**. That is invisible in a
manifest — the key is simply absent — and it is far too short for the probes
this project actually uses, which shell out to ``python -c`` and therefore pay
interpreter startup plus an import before they even open a socket.

This bit us for real. Every ``startupProbe`` in the repo omitted
``timeoutSeconds`` while the liveness and readiness probes on the *same
containers* set 5s and 3s, so the probe that runs when a process is coldest and
slowest was the one given the least time. On a CPU-contended node the kubelet
reported::

    Startup probe failed: command timed out: "python -c import urllib.request;
    urllib.request.urlopen('http://127.0.0.1:9000/healthz', timeout=3).read()"
    timed out after 1s
    Killing: Container health-monitor failed startup probe, will be restarted

...and killed a container whose own log said ``Application startup complete``
one second later. Two replicas crash-looped 11 times each and failed the live
release validation's topology check, while the identical workload on a less
crowded node ran fine — so nothing about the image or the code was wrong.

It bit again with 3s and 5s. A later live run (2026-09-13) put the
health-monitor leader on a node whose kernel was burning most of both vCPUs;
the ``urllib.request`` probes timed out 190 times in 25 minutes, liveness
restarted the container five times while it answered every request that
reached it, and the ALB marked the pod unhealthy. A probe is a process that
competes with the server for the container's CPU quota, so it has to be cheap
and the liveness budget has to be wide: the exec probes now start the
interpreter lean (``-I -S``, no site-packages scan) and speak HTTP over a bare
socket, about a fifth of the work of importing ``urllib.request``, and a
liveness restart takes a minute of continuous failure rather than 45 seconds.

The tests below pin the properties that make those failures impossible to
reintroduce silently: every probe declares a timeout, every startup probe
keeps a cold-start budget big enough for a slow node, every exec probe starts
lean and gets a generous timeout, and liveness never restarts on less than a
minute of failure.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = ROOT / "lambda" / "kubectl-applier-simple" / "manifests"

PROBE_KINDS = ("startupProbe", "livenessProbe", "readinessProbe")

#: Floor for the startup window, in seconds. A cold start on a saturated node
#: was measured at ~80s; this leaves headroom without letting a genuinely dead
#: container sit unnoticed for long.
MINIMUM_STARTUP_BUDGET_SECONDS = 120

#: An exec probe pays interpreter startup before it does any work, so it needs
#: materially more than a socket check.
MINIMUM_EXEC_TIMEOUT_SECONDS = 5

#: A liveness exec probe on a contended node was measured taking more than 5s
#: while the server it probed answered everything that reached it.
MINIMUM_EXEC_LIVENESS_TIMEOUT_SECONDS = 10

#: Seconds of continuous liveness failure (``periodSeconds * failureThreshold``)
#: before the kubelet restarts a container. A minute catches a stuck process
#: and rides out a contended node.
MINIMUM_LIVENESS_RESTART_WINDOW_SECONDS = 60

#: How an exec probe must start the interpreter: isolated (no PYTHON* env, no
#: user site) and without ``site`` (no site-packages scan) — the cheap start.
LEAN_INTERPRETER_PREFIX = ("python", "-I", "-S", "-c")

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}


def _documents(path: Path) -> list[Any]:
    """Parse a manifest, stubbing the ``{{TOKEN}}`` placeholders.

    The applier substitutes these at apply time. Left as-is they are not valid
    YAML — ``{{FOO}}`` parses as a mapping used as its own key — so every
    consumer of these files has to neutralise them first.
    """
    text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "PLACEHOLDER", path.read_text(encoding="utf-8"))
    return list(yaml.safe_load_all(text))


def _probes() -> list[tuple[str, str, str, str, dict[str, Any]]]:
    """Every probe in every workload manifest, as (file, workload, container, kind, probe)."""
    found: list[tuple[str, str, str, str, dict[str, Any]]] = []
    for path in sorted(MANIFEST_DIR.glob("*.yaml")):
        for document in _documents(path):
            if not isinstance(document, dict) or document.get("kind") not in WORKLOAD_KINDS:
                continue
            workload = str((document.get("metadata") or {}).get("name"))
            pod_spec = ((document.get("spec") or {}).get("template") or {}).get("spec") or {}
            for container in pod_spec.get("containers") or []:
                if not isinstance(container, dict):
                    continue
                for kind in PROBE_KINDS:
                    probe = container.get(kind)
                    if isinstance(probe, dict):
                        found.append((path.name, workload, str(container.get("name")), kind, probe))
    assert found, "no probes discovered; did the manifest directory move?"
    return found


def _probe_id(case: tuple[str, str, str, str, dict[str, Any]]) -> str:
    return f"{case[1]}/{case[2]}:{case[3]}"


ALL_PROBES = _probes()
STARTUP_PROBES = [case for case in ALL_PROBES if case[3] == "startupProbe"]


def test_the_probe_inventory_is_non_trivial() -> None:
    """Guard the guard: a parsing regression must not silently empty the suite.

    Every assertion below is parametrized over discovered probes, so a helper
    that quietly returned nothing would turn this whole file green while
    checking not a single manifest.
    """
    assert len(ALL_PROBES) >= 20, f"only found {len(ALL_PROBES)} probes; parsing likely broke"
    assert len(STARTUP_PROBES) >= 5, (
        f"only found {len(STARTUP_PROBES)} startup probes; parsing likely broke"
    )


@pytest.mark.parametrize("case", ALL_PROBES, ids=_probe_id)
def test_every_probe_declares_its_timeout(case: tuple[str, str, str, str, dict[str, Any]]) -> None:
    """An absent ``timeoutSeconds`` silently means 1 second.

    That is the whole bug: nothing in the manifest looks wrong, and the value
    that decides whether a healthy container gets killed is invisible.
    """
    manifest, workload, container, kind, probe = case
    timeout = probe.get("timeoutSeconds")
    assert timeout is not None, (
        f"{manifest}: {workload}/{container} {kind} omits timeoutSeconds, so the "
        "kubelet applies its 1s default"
    )
    assert isinstance(timeout, int) and timeout >= 1, (
        f"{manifest}: {workload}/{container} {kind} has a non-positive timeoutSeconds {timeout!r}"
    )


@pytest.mark.parametrize("case", [c for c in ALL_PROBES if "exec" in c[4]], ids=_probe_id)
def test_exec_probes_allow_for_interpreter_startup(
    case: tuple[str, str, str, str, dict[str, Any]],
) -> None:
    """An exec probe spawning ``python`` cannot finish in about a second.

    These probes fork an interpreter and import a module before they issue a
    request, so they need materially more slack than a tcpSocket check.
    """
    manifest, workload, container, kind, probe = case
    timeout = probe["timeoutSeconds"]
    assert timeout >= MINIMUM_EXEC_TIMEOUT_SECONDS, (
        f"{manifest}: {workload}/{container} {kind} is an exec probe with "
        f"timeoutSeconds={timeout}; allow at least {MINIMUM_EXEC_TIMEOUT_SECONDS}s "
        "for interpreter startup plus imports"
    )
    if kind == "livenessProbe":
        assert timeout >= MINIMUM_EXEC_LIVENESS_TIMEOUT_SECONDS, (
            f"{manifest}: {workload}/{container} liveness is an exec probe with "
            f"timeoutSeconds={timeout}; a contended node made a healthy server's probe "
            f"take longer than 5s, so allow at least {MINIMUM_EXEC_LIVENESS_TIMEOUT_SECONDS}s"
        )


@pytest.mark.parametrize("case", [c for c in ALL_PROBES if "exec" in c[4]], ids=_probe_id)
def test_exec_probes_start_the_interpreter_lean(
    case: tuple[str, str, str, str, dict[str, Any]],
) -> None:
    """An exec probe competes with the server for the container's CPU quota.

    ``-I -S`` skips the site-packages scan and the environment, and a bare
    socket request avoids ``urllib.request`` and the ~130 modules behind it;
    together they cut the work per probe about fivefold, which is the margin
    between a probe that completes on a contended node and one that gets a
    healthy container killed. The pre-stop sleep is not a probe and may stay
    plain.
    """
    manifest, workload, container, kind, probe = case
    command = probe["exec"]["command"]
    assert tuple(command[:4]) == LEAN_INTERPRETER_PREFIX, (
        f"{manifest}: {workload}/{container} {kind} must start the interpreter lean "
        f"({' '.join(LEAN_INTERPRETER_PREFIX)}), got {command[:4]}"
    )
    source = command[-1]
    assert "urllib" not in source, (
        f"{manifest}: {workload}/{container} {kind} imports urllib; use the bare-socket request"
    )
    assert "socket.create_connection(('127.0.0.1'," in source, (
        f"{manifest}: {workload}/{container} {kind} must dial the loopback listener over a socket"
    )
    # Every network wait inside the probe is bounded below the probe's own
    # timeout, so a hung listener is reported as a failure, not as a kubelet
    # timeout with no output.
    inner = [int(value) for value in re.findall(r"(?:\),|settimeout\()(\d+)\)", source)]
    assert inner and all(value < probe["timeoutSeconds"] for value in inner), (
        f"{manifest}: {workload}/{container} {kind} socket timeouts {inner} must stay below "
        f"timeoutSeconds={probe['timeoutSeconds']}"
    )


@pytest.mark.parametrize(
    "case",
    [c for c in ALL_PROBES if c[3] == "livenessProbe" and "exec" in c[4]],
    ids=_probe_id,
)
def test_exec_liveness_restarts_only_after_a_minute_of_failure(
    case: tuple[str, str, str, str, dict[str, Any]],
) -> None:
    """A liveness probe exists to catch a stuck process, not a slow node.

    ``periodSeconds * failureThreshold`` is the floor of continuous failure
    before the kubelet restarts the container; three failures 15s apart (45s)
    was short enough for a contended node to restart a serving container five
    times in 25 minutes. Exec probes are the exposed kind: the kubelet runs
    tcpSocket and httpGet checks itself, outside the container's CPU quota.
    """
    manifest, workload, container, kind, probe = case
    period = probe.get("periodSeconds")
    threshold = probe.get("failureThreshold")
    assert isinstance(period, int) and isinstance(threshold, int), (
        f"{manifest}: {workload}/{container} {kind} must set periodSeconds and failureThreshold "
        "explicitly so its restart window is auditable"
    )
    window = period * threshold
    assert window >= MINIMUM_LIVENESS_RESTART_WINDOW_SECONDS, (
        f"{manifest}: {workload}/{container} {kind} restarts after {window}s of failure "
        f"({threshold} x {period}s); allow at least {MINIMUM_LIVENESS_RESTART_WINDOW_SECONDS}s"
    )


@pytest.mark.parametrize("case", STARTUP_PROBES, ids=_probe_id)
def test_startup_probes_budget_for_a_slow_cold_start(
    case: tuple[str, str, str, str, dict[str, Any]],
) -> None:
    """The startup window has to cover a cold start on a contended node.

    ``failureThreshold * periodSeconds`` is the floor of the window; each
    attempt can additionally burn up to ``timeoutSeconds``. Only the floor is
    asserted, so the check stays true regardless of how attempts interleave.
    """
    manifest, workload, container, kind, probe = case
    period = probe.get("periodSeconds")
    threshold = probe.get("failureThreshold")
    assert isinstance(period, int) and isinstance(threshold, int), (
        f"{manifest}: {workload}/{container} {kind} must set periodSeconds and failureThreshold "
        "explicitly so its startup budget is auditable"
    )
    budget = period * threshold
    assert budget >= MINIMUM_STARTUP_BUDGET_SECONDS, (
        f"{manifest}: {workload}/{container} startup budget is only {budget}s "
        f"({threshold} x {period}s); allow at least {MINIMUM_STARTUP_BUDGET_SECONDS}s, since a "
        "cold start on a CPU-contended node was measured at ~80s"
    )


@pytest.mark.parametrize("case", STARTUP_PROBES, ids=_probe_id)
def test_startup_probes_are_not_stricter_than_liveness(
    case: tuple[str, str, str, str, dict[str, Any]],
) -> None:
    """A startup probe must never get less time than the liveness probe beside it.

    This is the inversion that caused the incident: liveness allowed 5s while
    startup allowed 1s, even though startup runs when the process is least
    responsive. Comparing the two catches a regression that an absolute floor
    would let through.
    """
    manifest, workload, container, _kind, probe = case
    liveness = next(
        (
            other[4]
            for other in ALL_PROBES
            if other[:3] == (manifest, workload, container) and other[3] == "livenessProbe"
        ),
        None,
    )
    if liveness is None or liveness.get("timeoutSeconds") is None:
        pytest.skip("no liveness probe with an explicit timeout on this container")
    assert probe["timeoutSeconds"] >= liveness["timeoutSeconds"], (
        f"{manifest}: {workload}/{container} startup timeout "
        f"({probe['timeoutSeconds']}s) is tighter than its liveness timeout "
        f"({liveness['timeoutSeconds']}s), but startup runs when the process is slowest"
    )
