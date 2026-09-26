"""Runtime smoke for the distroless service images.

Runs as the final Dockerfile stage's only RUN — exec form, as the runtime
user — with the service entry module as its argument, plus ``--tracing`` in
the four traced API images. It proves builder-to-scratch parity
programmatically instead of via a hand-maintained module list:

- every stdlib C extension that was importable in the builder stage must
  import here too. ``build_scratch_rootfs.py`` records that set in
  ``runtime_smoke_manifest.json`` next to this file, derived by actually
  importing everything under ``lib-dynload`` in the builder. Import parity
  is a strictly stronger completeness proof than the ldd closure alone: it
  also catches libraries reached only via ``dlopen``, which ldd cannot see.
- the service entry module must import (the full third-party graph);
- ``getpass`` must resolve the synthesized passwd identity for the runtime
  uid (NSS wiring);
- OpenSSL's default trust store must load CA certificates (TLS to AWS);
- ``zoneinfo`` must resolve from the staged tzdata.

``--tracing`` adds the tracing stack. ``gco.services.tracing`` imports
OpenTelemetry, botocore's SigV4 signer and httpx2 only once tracing is
enabled, so importing the entry module never loads them: a package missing
from a traced image would surface only at runtime, as one warning and a
service that exports nothing. With the flag, every module in
``TRACING_MODULES`` must import, and httpx2's default TLS context (the public
trust the X-Ray exporter uses, backed by truststore) must load the image's CA
anchors. Neither check opens a connection. ``TRACING_MODULES`` is the one list
kept by hand here; ``tests/test_distroless_build_scripts.py`` derives the lazy
imports from the tracing module's source and fails when the two drift.

Every failure is collected and reported, then the process exits non-zero so
the image build — including CDK deploys — fails instead of the pod. The
script and its manifest live in the builder stage's ``/opt/build`` and reach
the final stage through a BuildKit bind mount scoped to the smoke RUN alone:
the deployed image ships neither of them (deleting files in a later layer
would only hide them — layers are additive — so they are never written into
the image at all). The manifest is located relative to ``__file__``, so the
pair works from any mount target.

Only ``json``, ``sys``, ``importlib``, and ``pathlib`` are imported at module
scope; everything under test (``ssl``, ``getpass``, ``zoneinfo``, the stdlib
extensions, the entry module, the tracing stack) is imported inside guarded
sections so a single breakage cannot mask the rest of the report.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

TRACING_FLAG = "--tracing"

#: Every module ``gco.services.tracing`` imports inside its functions (none of
#: them load until ``GCO_TRACING_ENABLED=true``), plus ``truststore``, which
#: provides httpx2's default trust store.
TRACING_MODULES = (
    "botocore.auth",
    "botocore.awsrequest",
    "botocore.session",
    "google.protobuf.message",
    "httpx2",
    "opentelemetry.exporter.otlp.proto.common.trace_encoder",
    "opentelemetry.instrumentation.fastapi",
    "opentelemetry.instrumentation.httpx",
    "opentelemetry.metrics",
    "opentelemetry.propagate",
    "opentelemetry.proto.collector.trace.v1.trace_service_pb2",
    "opentelemetry.sdk.resources",
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.trace.export",
    "opentelemetry.sdk.trace.sampling",
    "opentelemetry.trace",
    "opentelemetry.trace.propagation.tracecontext",
    "truststore",
)

# ``wrap_bio`` needs a server name to satisfy check_hostname. No handshake
# runs, so the name is never resolved or contacted.
_TRUST_PROBE_HOSTNAME = "localhost"


def check_tracing_stack(failures: list[str]) -> int:
    """Import the lazily loaded tracing stack and check httpx2's default trust.

    Appends every problem to ``failures`` and returns the number of CA
    certificates httpx2's default context loaded (0 when that check failed).
    """
    for name in TRACING_MODULES:
        try:
            # The names are this script's own constant, never input.
            # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
            importlib.import_module(name)
        except BaseException as exc:  # aggregate every breakage
            failures.append(f"tracing module {name}: {type(exc).__name__}: {exc}")

    try:
        import ssl

        import httpx2

        # The context the X-Ray exporter's client builds (trust_env=False) is
        # a truststore one, which loads the system trust store when a
        # connection is wrapped rather than when it is built. Wrapping two
        # in-memory BIOs does that without a socket or a handshake; the SSL
        # object's context is the OpenSSL one truststore configured.
        context = httpx2.create_ssl_context(trust_env=False)
        tls = context.wrap_bio(
            ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname=_TRUST_PROBE_HOSTNAME
        )
        anchors = tls.context.cert_store_stats()["x509_ca"]
    except BaseException as exc:
        failures.append(f"httpx2 default trust: {type(exc).__name__}: {exc}")
        return 0
    if anchors <= 0:
        failures.append("httpx2 default trust store (truststore) loaded zero CA certificates")
    return anchors


def main() -> int:
    arguments = sys.argv[1:]
    tracing = arguments[1:] == [TRACING_FLAG]
    if len(arguments) != 1 and not tracing:
        print(
            f"usage: runtime_smoke.py <service-entry-module> [{TRACING_FLAG}]",
            file=sys.stderr,
        )
        return 2
    entry_module = arguments[0]

    manifest_path = Path(__file__).with_name("runtime_smoke_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extensions: list[str] = manifest["stdlib_extensions"]
    expected_user: str = manifest["runtime_user"]

    failures: list[str] = []
    if not extensions:
        failures.append("manifest lists no stdlib extensions; the builder probe broke")

    for name in extensions:
        try:
            # Dynamic import IS the check: the names come from the manifest
            # this build's rootfs script derived (root-written, read-only at
            # runtime), never from untrusted input.
            # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
            importlib.import_module(name)
        except BaseException as exc:  # aggregate every breakage
            failures.append(f"stdlib extension {name}: {type(exc).__name__}: {exc}")

    try:
        # The entry module is a literal baked into each Dockerfile's smoke
        # RUN, not user input; importing it dynamically is this script's job.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        importlib.import_module(entry_module)
    except BaseException as exc:
        failures.append(f"entry module {entry_module}: {type(exc).__name__}: {exc}")

    actual_user = "<unresolved>"
    try:
        import getpass

        actual_user = getpass.getuser()
        if actual_user != expected_user:
            failures.append(f"runtime user: expected {expected_user!r}, got {actual_user!r}")
    except BaseException as exc:
        failures.append(f"runtime identity lookup: {type(exc).__name__}: {exc}")

    try:
        import ssl

        ca_count = ssl.create_default_context().cert_store_stats()["x509_ca"]
        if ca_count <= 0:
            failures.append("OpenSSL default trust store loaded zero CA certificates")
    except BaseException as exc:
        failures.append(f"CA trust store: {type(exc).__name__}: {exc}")

    try:
        import zoneinfo

        zoneinfo.ZoneInfo("UTC")
    except BaseException as exc:
        failures.append(f"zoneinfo/tzdata: {type(exc).__name__}: {exc}")

    trust_anchors = check_tracing_stack(failures) if tracing else 0

    if failures:
        print(f"distroless runtime smoke FAILED ({len(failures)} problem(s)):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    summary = (
        f"distroless runtime smoke OK: {len(extensions)} stdlib extensions, "
        f"entry module {entry_module}, user {actual_user}, CA trust and tzdata present"
    )
    if tracing:
        summary += (
            f"; tracing stack imports ({len(TRACING_MODULES)} modules), "
            f"httpx2 default trust loads {trust_anchors} CA certificates"
        )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
