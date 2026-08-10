"""Live validation of every shipped example manifest.

``gco examples validate`` (this package's CLI face) stands up the configured
GCO topology, runs every example under ``examples/`` through its DOCUMENTED
submission path, verifies each example's success criteria, tears everything
down, and writes a per-example report — reusing the
``scripts/live_release_validation`` machinery (preflight, baseline, deploy,
destroy, final-inventory, checkpointing, reporting) with one new ``examples``
action.

Selection is supported (``--examples``, ``--skip``, ``--category``) so an
author who changed one example can validate just that one against existing
infrastructure. The offline half (``--static-only``) parses every example,
checks it against the API/SQS transport gates it is documented to use, and
enforces spec/catalog symmetry — it also runs in CI via
``tests/test_example_job_validation.py``.
"""
