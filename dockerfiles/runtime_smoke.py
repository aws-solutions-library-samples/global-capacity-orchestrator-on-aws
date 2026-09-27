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
service that exports nothing. With the flag, the smoke parses that module's
source as the image ships it and performs every import its functions
contain, each the way its statement runs (``from package import name`` needs
``name``), and httpx2's default TLS context (the public trust the X-Ray
exporter uses, which httpx2 builds with truststore) must load the image's CA
anchors. Neither check opens a connection. Like the extension set, the import
list is derived rather than kept here, so it cannot drift from the module.

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
extensions, the entry module, the tracing stack and the ``ast`` parse that
finds it) is imported inside guarded sections so a single breakage cannot
mask the rest of the report.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

TRACING_FLAG = "--tracing"

#: The module whose deferred imports are the tracing stack: nothing its
#: functions import loads until ``GCO_TRACING_ENABLED=true``.
TRACING_MODULE = "gco.services.tracing"

# ``wrap_bio`` needs a server name to satisfy check_hostname. No handshake
# runs, so the name is never resolved or contacted.
_TRUST_PROBE_HOSTNAME = "localhost"


def function_scope_imports(source: str, package: str | None) -> list[tuple[str, str | None]]:
    """Every import inside a function of ``source``, as ``(module, name)`` pairs.

    ``import a.b`` gives ``("a.b", None)``; ``from a import b, c`` gives
    ``("a", "b")`` and ``("a", "c")``; relative imports resolve against
    ``package``. Imports that run at module level (``TYPE_CHECKING`` blocks
    and class bodies included) are left out: importing the module already
    proved them.
    """
    import ast
    import importlib.util

    found: set[tuple[str, str | None]] = set()
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Import):
                found.update((alias.name, None) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package
                )
                found.update((module, alias.name) for alias in node.names)
    return sorted(found, key=lambda pair: (pair[0], pair[1] or ""))


def tracing_imports(failures: list[str]) -> list[tuple[str, str | None]]:
    """What :data:`TRACING_MODULE` imports inside its functions, from its source here.

    Parsed, not imported: the module defers these imports until tracing is
    enabled, which is why importing the entry module cannot prove them.
    Appends a failure when the source cannot be found or parsed, or holds no
    function-scope import at all, so a broken derivation never passes
    vacuously.
    """
    try:
        import importlib.util

        spec = importlib.util.find_spec(TRACING_MODULE)
        if spec is None or spec.origin is None:
            raise ModuleNotFoundError(f"no source file for {TRACING_MODULE}")
        found = function_scope_imports(Path(spec.origin).read_text(encoding="utf-8"), spec.parent)
    except BaseException as exc:  # aggregate every breakage
        failures.append(f"tracing imports of {TRACING_MODULE}: {type(exc).__name__}: {exc}")
        return []
    if not found:
        failures.append(
            f"{TRACING_MODULE} has no function-scope imports; the tracing derivation broke"
        )
    return found


def check_tracing_stack(failures: list[str]) -> tuple[int, int]:
    """Perform the tracing module's deferred imports and check httpx2's default trust.

    Each import runs the way its statement does: ``from package import name``
    needs ``name``, found as an attribute or else as the submodule
    ``package.name``. truststore needs no entry of its own: httpx2 imports it
    to build the context the trust check builds. Appends every problem to
    ``failures`` and returns the number of imports checked and the number of
    CA certificates httpx2's default context loaded (0 when that check
    failed).
    """
    imports = tracing_imports(failures)
    for module_name, name in imports:
        statement = f"from {module_name} import {name}" if name else f"import {module_name}"
        try:
            # The names come from the tracing module's own source, never input.
            # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
            module = importlib.import_module(module_name)
            if name is not None and not hasattr(module, name):
                # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
                importlib.import_module(f"{module_name}.{name}")
        except BaseException as exc:  # aggregate every breakage
            failures.append(f"tracing import ({statement}): {type(exc).__name__}: {exc}")

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
        return len(imports), 0
    if anchors <= 0:
        failures.append("httpx2 default trust store (truststore) loaded zero CA certificates")
    return len(imports), anchors


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

    tracing_imported, trust_anchors = check_tracing_stack(failures) if tracing else (0, 0)

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
            f"; {tracing_imported} deferred imports of {TRACING_MODULE} present, "
            f"httpx2 default trust loads {trust_anchors} CA certificates"
        )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
