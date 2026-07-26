"""Architecture guards for the live-release-validation harness.

The harness used to be one 6,900-line ``actions.py``. These tests keep it from
drifting back and keep its moving parts honest with each other, so the failure
arrives on the pull request instead of during a two-hour live run against a
real AWS account:

* every registry action has exactly one owning module, and every action module
  is registered and exported;
* the registry agrees with the operator-facing contract table in
  ``docs/LIVE_RELEASE_VALIDATION.md`` on action names, order, and dependencies;
* dependencies are declared in an order the runner can actually execute;
* imports flow one way through the layers, so the graph cannot go cyclic;
* no module regrows past a review-sized ceiling; and
* every module and public handler carries a docstring, and the developer README
  documents each action module.

Each failure message names the exact file and the one-line fix.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts.live_release_validation.registry import build_action_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "scripts" / "live_release_validation"
RUNBOOK = REPO_ROOT / "docs" / "LIVE_RELEASE_VALIDATION.md"
DEVELOPER_README = PACKAGE / "README.md"

#: Actions that intentionally share one module, with the reason. ``api`` and
#: ``sqs`` are the same Job lifecycle over two transports; separating them
#: would duplicate the lifecycle rather than clarify it.
SHARED_ACTION_MODULES = {"api": "jobs", "sqs": "jobs"}

#: Largest reviewable module. Generous enough for the harness's genuinely
#: intricate ownership logic, small enough that a 6,900-line module cannot
#: return. Raising this needs a reason in the pull request, not a reflex.
MAX_MODULE_LINES = 900

#: Which layers each layer may import from. Anything not listed is forbidden.
ALLOWED_LAYER_IMPORTS = {
    # Actions compose everything below them.
    "actions": {"actions", "checks", "cleanup", "ownership", "root"},
    # Deleting a workload reuses the same Job primitives the checks use, so
    # cleanup may import checks. Nothing in checks imports cleanup, which is
    # what keeps this direction acyclic.
    "cleanup": {"cleanup", "checks", "ownership", "root"},
    "checks": {"checks", "ownership", "root"},
    "ownership": {"ownership", "root"},
    # inventory/ is read-only: it may only use its own submodules.
    "inventory": {"inventory", "root"},
}

#: One action row of the runbook contract table, e.g.
#: ``| `preflight` | None | Verify the clean Git checkout, ... |``. Capture 1 is
#: the action name, capture 2 the dependency cell. Compiled at module level
#: rather than calling ``re.match`` inline, which Python 3.15 soft-deprecates
#: (see ``tests/test_no_python_315_deprecation_surface.py``).
_RUNBOOK_ACTION_ROW = re.compile(r"^\|\s*`([a-z-]+)`\s*\|([^|]*)\|")

#: Root modules that carry no layer of their own.
ROOT_LAYER_MODULES = {
    "_shared",
    "constants",
    "context",
    "inventory",
    "models",
    "protected",
}


def _package_modules() -> list[Path]:
    return sorted(path for path in PACKAGE.rglob("*.py") if path.name != "__init__.py")


def _layer_of(path: Path) -> str:
    relative = path.relative_to(PACKAGE)
    return relative.parts[0] if len(relative.parts) > 1 else "root"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _action_modules() -> dict[str, Path]:
    return {
        path.stem: path
        for path in sorted((PACKAGE / "actions").glob("*.py"))
        if path.stem != "__init__"
    }


def _runbook_action_rows() -> list[tuple[str, str]]:
    """Return ``(action, dependency-cell)`` rows from the runbook table."""
    rows = []
    for line in RUNBOOK.read_text(encoding="utf-8").splitlines():
        match = _RUNBOOK_ACTION_ROW.match(line.strip())
        if match:
            rows.append((match.group(1), match.group(2).strip()))
    return rows


def test_the_monolithic_actions_module_is_not_reintroduced() -> None:
    """A package-root ``actions.py`` would undo the split."""
    assert not (PACKAGE / "actions.py").exists(), (
        "scripts/live_release_validation/actions.py is back. Actions live in "
        "the actions/ package, one module per registry entry."
    )
    assert (PACKAGE / "actions" / "__init__.py").is_file(), (
        "actions/ must stay a package exporting the action handlers."
    )


def test_every_action_has_exactly_one_owning_module() -> None:
    registry = build_action_registry()
    modules = _action_modules()
    missing = []
    for name in registry:
        expected = SHARED_ACTION_MODULES.get(name, name.replace("-", "_"))
        if expected not in modules:
            missing.append(f"{name} -> actions/{expected}.py")
    assert not missing, (
        "Registered actions without an owning module (create the module, or add "
        "it to SHARED_ACTION_MODULES with a reason):\n  " + "\n  ".join(missing)
    )

    owned = {SHARED_ACTION_MODULES.get(name, name.replace("-", "_")) for name in registry}
    orphans = sorted(set(modules) - owned)
    assert not orphans, (
        "Modules under actions/ that no registry entry owns (register them in "
        "registry.py or move the helper out of actions/):\n  "
        + "\n  ".join(f"actions/{name}.py" for name in orphans)
    )


def test_every_action_handler_is_exported_from_the_actions_package() -> None:
    """A handler missing from ``actions/__init__.py`` breaks the documented flow."""
    exported = set(
        ast.literal_eval(
            next(
                node.value
                for node in _parse(PACKAGE / "actions" / "__init__.py").body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "__all__"
                    for target in node.targets
                )
            )
        )
    )
    handlers = {definition.handler.__name__ for definition in build_action_registry().values()}
    missing = sorted(handlers - exported)
    assert not missing, (
        "Action handlers missing from actions/__init__.py __all__:\n  " + "\n  ".join(missing)
    )


def test_registry_matches_the_runbook_contract_table() -> None:
    """Operators read the runbook table; the runner reads the registry."""
    registry = build_action_registry()
    rows = _runbook_action_rows()
    assert rows, f"Found no action rows in {RUNBOOK}; has the table format changed?"

    assert [name for name, _ in rows] == list(registry), (
        "The runbook action table and the registry disagree on action names or "
        f"order.\n  runbook:  {[name for name, _ in rows]}\n  registry: {list(registry)}"
    )

    mismatched = []
    for name, dependency_cell in rows:
        documented = set(re.findall(r"`([a-z-]+)`", dependency_cell))
        if not documented and dependency_cell.casefold() in {"none", "-", ""}:
            documented = set()
        declared = set(registry[name].dependencies)
        if documented != declared:
            mismatched.append(
                f"{name}: runbook {sorted(documented)} != registry {sorted(declared)}"
            )
    assert not mismatched, "Documented dependencies disagree with the registry:\n  " + "\n  ".join(
        mismatched
    )


def test_dependencies_are_declared_before_their_dependents() -> None:
    """The runner executes registry order, so dependencies must come first."""
    seen: set[str] = set()
    problems = []
    for name, definition in build_action_registry().items():
        late = sorted(set(definition.dependencies) - seen)
        if late:
            problems.append(f"{name} depends on later action(s) {late}")
        unknown = sorted(
            dependency
            for dependency in definition.dependencies
            if dependency not in build_action_registry()
        )
        if unknown:
            problems.append(f"{name} depends on unknown action(s) {unknown}")
        seen.add(name)
    assert not problems, "Registry ordering is not executable:\n  " + "\n  ".join(problems)


def test_layer_imports_flow_one_way() -> None:
    """Keep the import graph acyclic by construction."""
    violations = []
    registry_importers_allowed = {"runner.py", "__main__.py"}
    for path in _package_modules():
        layer = _layer_of(path)
        allowed = ALLOWED_LAYER_IMPORTS.get(layer)
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            # A single-dot import inside a layer names a sibling in that same
            # layer, which is always allowed; two dots reach up out of it.
            if node.level == 1 and layer != "root":
                continue
            head = (node.module or "").split(".")[0]
            if not head:
                continue
            target = "root" if head in ROOT_LAYER_MODULES else head
            if target == "registry" and path.name not in registry_importers_allowed:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)} imports registry "
                    "(only runner.py and __main__.py may)"
                )
            elif allowed is not None and target not in allowed and target != "root":
                violations.append(
                    f"{path.relative_to(REPO_ROOT)} ({layer}) imports {target}; "
                    f"{layer} may only import {sorted(allowed)}"
                )
    assert not violations, (
        "Layer import violations — see the layering rules in "
        "scripts/live_release_validation/README.md:\n  " + "\n  ".join(violations)
    )


def test_no_module_grows_past_the_review_ceiling() -> None:
    oversized = []
    for path in _package_modules():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > MAX_MODULE_LINES:
            oversized.append(f"{path.relative_to(REPO_ROOT)}: {lines} lines")
    assert not oversized, (
        f"Modules above the {MAX_MODULE_LINES}-line ceiling. Split them along a "
        "real seam rather than raising the limit:\n  " + "\n  ".join(oversized)
    )


@pytest.mark.parametrize(
    "path",
    _package_modules(),
    ids=lambda path: str(path.relative_to(PACKAGE)),
)
def test_every_module_documents_itself(path: Path) -> None:
    docstring = ast.get_docstring(_parse(path))
    assert docstring and docstring.strip(), (
        f"{path.relative_to(REPO_ROOT)} needs a module docstring saying what it owns."
    )


def test_every_action_handler_documents_its_contract() -> None:
    undocumented = []
    for name, definition in build_action_registry().items():
        docstring = (definition.handler.__doc__ or "").strip()
        if not docstring:
            undocumented.append(f"{name} ({definition.handler.__name__})")
    assert not undocumented, (
        "Action handlers without a docstring describing the contract they "
        "enforce:\n  " + "\n  ".join(undocumented)
    )


def test_developer_readme_documents_every_action_module_and_layer() -> None:
    readme = DEVELOPER_README.read_text(encoding="utf-8")
    missing = [
        f"actions/{module}.py"
        for module in sorted(_action_modules())
        if f"actions/{module}.py" not in readme and "actions/" not in readme
    ]
    for layer in sorted(ALLOWED_LAYER_IMPORTS):
        if f"`{layer}/`" not in readme:
            missing.append(f"{layer}/ layer")
    assert not missing, (
        f"{DEVELOPER_README.relative_to(REPO_ROOT)} does not document:\n  " + "\n  ".join(missing)
    )


def test_developer_readme_explains_when_to_add_an_action() -> None:
    """The README is the answer to 'where does my new check go?'."""
    readme = DEVELOPER_README.read_text(encoding="utf-8")
    required_sections = [
        "## Adding a check: decide the scope first",
        "## Adding a new action",
        "## Adding a new owned resource type",
        "## Layering rules",
        "## Testing your change",
    ]
    missing = [section for section in required_sections if section not in readme]
    assert not missing, (
        "The developer README lost the guidance sections a contributor needs:\n  "
        + "\n  ".join(missing)
    )
