"""Emulator-gap shims for HARNESS SUBPROCESSES in the Floci E2E test.

The Floci E2E (tests/test_floci_live_validation_e2e.py) runs the real live
validation harness as a subprocess via ``gco release validate``. That child
interpreter builds its own boto3 sessions, out of reach of in-process test
fixtures — so the two documented Floci 1.6.0 gap shims from tests/_floci.py
(unparseable CloudFormation ``GetStackPolicy`` responses; Global Accelerator
absent) are injected here instead: the E2E prepends this directory to
``PYTHONPATH``, Python imports ``sitecustomize`` at startup, and every
botocore session created in the child auto-registers the same two
``before-send`` answers.

Triple-gated so it can never leak beyond the emulator E2E:

1. the module only ships inside ``tests/`` and only enters ``PYTHONPATH``
   when the E2E test composes the subprocess environment;
2. it hard-noops unless ``GCO_LIVE_VALIDATION_EMULATOR`` is set; and
3. the shims answer exactly two read-only operations with fixed local
   responses — they cannot mutate anything.

Delete this file (and the shims in tests/_floci.py) once a Floci release
parses GetStackPolicy and catalogs Global Accelerator.
"""

from __future__ import annotations

import os


def _install() -> None:
    if not os.environ.get("GCO_LIVE_VALIDATION_EMULATOR"):
        return

    import botocore.session

    from tests._floci_gap_shims import apply_known_floci_gap_shims

    original_init = botocore.session.Session.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        apply_known_floci_gap_shims(self.get_component("event_emitter"))

    if not getattr(botocore.session.Session, "_gco_floci_shimmed", False):
        botocore.session.Session.__init__ = patched_init  # type: ignore[method-assign]
        botocore.session.Session._gco_floci_shimmed = True  # type: ignore[attr-defined]


_install()
