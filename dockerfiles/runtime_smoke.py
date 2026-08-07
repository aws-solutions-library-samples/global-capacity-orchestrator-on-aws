"""Runtime smoke for the distroless service images.

Runs as the final Dockerfile stage's only RUN — exec form, as the runtime
user — with one argument: the service entry module. It proves builder-to-
scratch parity programmatically instead of via a hand-maintained module
list:

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
extensions, the entry module) is imported inside guarded sections so a single
breakage cannot mask the rest of the report.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: runtime_smoke.py <service-entry-module>", file=sys.stderr)
        return 2
    entry_module = sys.argv[1]

    manifest_path = Path(__file__).with_name("runtime_smoke_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extensions: list[str] = manifest["stdlib_extensions"]
    expected_user: str = manifest["runtime_user"]

    failures: list[str] = []
    if not extensions:
        failures.append("manifest lists no stdlib extensions; the builder probe broke")

    for name in extensions:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 — aggregate every breakage
            failures.append(f"stdlib extension {name}: {type(exc).__name__}: {exc}")

    try:
        importlib.import_module(entry_module)
    except BaseException as exc:  # noqa: BLE001
        failures.append(f"entry module {entry_module}: {type(exc).__name__}: {exc}")

    actual_user = "<unresolved>"
    try:
        import getpass

        actual_user = getpass.getuser()
        if actual_user != expected_user:
            failures.append(f"runtime user: expected {expected_user!r}, got {actual_user!r}")
    except BaseException as exc:  # noqa: BLE001
        failures.append(f"runtime identity lookup: {type(exc).__name__}: {exc}")

    try:
        import ssl

        ca_count = ssl.create_default_context().cert_store_stats()["x509_ca"]
        if ca_count <= 0:
            failures.append("OpenSSL default trust store loaded zero CA certificates")
    except BaseException as exc:  # noqa: BLE001
        failures.append(f"CA trust store: {type(exc).__name__}: {exc}")

    try:
        import zoneinfo

        zoneinfo.ZoneInfo("UTC")
    except BaseException as exc:  # noqa: BLE001
        failures.append(f"zoneinfo/tzdata: {type(exc).__name__}: {exc}")

    if failures:
        print(f"distroless runtime smoke FAILED ({len(failures)} problem(s)):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"distroless runtime smoke OK: {len(extensions)} stdlib extensions, "
        f"entry module {entry_module}, user {actual_user}, CA trust and tzdata present"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
