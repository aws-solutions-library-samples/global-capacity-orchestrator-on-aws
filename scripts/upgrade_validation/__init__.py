"""Live validation of ``gco upgrade`` from the previous release to this checkout.

``gco release validate-upgrade`` (this package's CLI face) deploys the
previous tagged release from a private clone with that release's own ``gco``,
upgrades the deployment to the checked-out commit with that same ``gco
upgrade`` (the path an operator takes), proves the upgraded deployment is
healthy and that the state the upgrade promises to keep survived, then tears
everything down and verifies the account is back to its baseline.

It reuses the ``scripts/live_release_validation`` machinery (preflight,
baseline, topology, destroy, final-inventory, checkpointing, reporting). Its
stacks are deployed by a ``gco`` subprocess, not by change sets the harness
prepares, so it is the one harness that owns stacks by run-tag adoption (see
``ownership/stacks.py`` in that package and docs/UPGRADE_VALIDATION.md).
"""
