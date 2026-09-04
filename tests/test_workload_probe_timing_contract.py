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

The tests below pin the two properties that make that failure impossible to
reintroduce silently: every probe declares a timeout, and every startup probe
keeps a cold-start budget big enough for a slow node.
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
MINIMUM_EXEC_TIMEOUT_SECONDS = 3

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
