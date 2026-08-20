"""One module per live-validation action.

Each module here owns exactly one registry entry (``actions/deploy.py`` ->
``deploy``), so the file you open matches the action name printed in the run
log and the row in the ``docs/LIVE_RELEASE_VALIDATION.md`` contract table.
Two of them pair naturally and share a module: ``api`` and ``sqs`` are the
same Job lifecycle over two transports, so both live in ``actions/jobs.py``.

Action handlers take a ``RunContext`` and return the JSON-able evidence dict
the runner stores in the checkpoint and both reports. They compose helpers
from the sibling packages rather than reaching into each other:

* ``..ownership`` — proves what this run created and may destroy
* ``..cleanup`` — deletes exactly those proven-owned resources
* ``..checks`` — reusable polling/validation helpers
* ``..protected`` / ``..context`` — ownership boundary and run identity

See ``scripts/live_release_validation/README.md`` for when to add a new
action versus extending an existing one.
"""

from .baseline import action_baseline
from .central_queue import action_central_queue_lifecycle
from .convergence import action_convergence
from .deploy import action_deploy
from .destroy import action_destroy, destroy_deployment
from .final_inventory import action_final_inventory
from .jobs import action_api_lifecycle, action_sqs_lifecycle
from .opencost import action_opencost
from .preflight import action_preflight
from .schedulers import action_schedulers
from .topology import action_topology
from .volume_inventory import action_volume_inventory

__all__ = [
    "action_api_lifecycle",
    "action_baseline",
    "action_central_queue_lifecycle",
    "action_convergence",
    "action_deploy",
    "action_destroy",
    "action_final_inventory",
    "action_opencost",
    "action_preflight",
    "action_schedulers",
    "action_sqs_lifecycle",
    "action_topology",
    "action_volume_inventory",
    "destroy_deployment",
]
