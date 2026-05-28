"""Restricted AST validator for Mission ``Strategy.script`` source.

Where a ``Criterion(kind="predicate")`` carries a single expression, a
``Strategy.script`` carries a multi-statement Python module that runs
inside the Mission sandbox to drive an iteration. Both surfaces accept
untrusted operator input, so both go through a parse-time AST allowlist
before any execution. This module owns the script side: it parses
scripts in ``exec`` mode, walks the tree with an explicit list of
allowed nodes, and rejects everything else with :class:`ScriptRejected`.

The script surface is wider than the predicate surface — multi-statement
control flow, helper function definitions, named-exception ``try`` /
``except`` / ``finally`` blocks, plus calls to the operator-supplied
tool allowlist — so this module is its own validator rather than a
shared base class. The structural decisions (an :class:`ast.NodeVisitor`
that defines a ``visit_*`` for every accepted node and rejects in
``generic_visit``, an exception type carrying ``reason`` /
``failing_node`` / ``lineno`` / ``col_offset``, dunder filtering on
strings and identifiers, comprehension-target shadowing checks) mirror
:mod:`mcp.mission.predicate` so the two layers reject the same shapes
the same way.

Two layers, same as the predicate sandbox:

1. **Parse-time validation.** :func:`validate_script_ast` parses the
   source in ``exec`` mode and walks the tree with
   :class:`_ScriptValidator`. The first disallowed construct raises
   :class:`ScriptRejected`; the script never runs.
2. **Run-time isolation.** The runtime layer (the
   :class:`MissionSandbox` wrapper around ``MontySandboxProvider``)
   executes a validated script under shared duration / memory limits
   with an explicit namespace that withholds dangerous builtins like
   ``open`` / ``getattr`` / ``__import__``. Even a tree that smuggled
   past this validator would fail at lookup.

Allowed surface
---------------
**Statements:** ``Module``, ``Expr``, ``Assign``, ``AugAssign``,
``AnnAssign``, ``If``, ``While``, ``For``, ``Pass``, ``Break``,
``Continue``, ``Return``, ``FunctionDef`` (no decorators), ``Try``
(named-exception handlers only), ``Raise``.

**Expressions:** constants, names from the allowlist, container
literals (``List`` / ``Tuple`` / ``Set`` / ``Dict``), comprehensions
(``ListComp`` / ``SetComp`` / ``DictComp`` / ``GeneratorExp``),
``BinOp`` / ``UnaryOp`` / ``BoolOp`` / ``Compare`` / ``IfExp``,
subscript and slice access, f-strings, lambdas, the walrus operator,
plus calls.

**Names visible to a script (the *base scope*):**

- ``mission`` — the per-iteration namespace; the only allowed
  attribute access is ``mission.observe`` and ``mission.event``.
- The pure stdlib callables ``len``, ``min``, ``max``, ``sum``,
  ``abs``, ``any``, ``all``, ``sorted``, ``range``, ``enumerate``,
  ``zip``, ``list``, ``dict``, ``tuple``, ``set``, ``str``, ``int``,
  ``float``, ``bool``.
- A small set of built-in exception classes so ``raise ValueError(...)``
  and ``except KeyError as e:`` both work without importing.
- Every tool name the operator placed on the per-session allowlist.

**Calls** may target a bare name from the base scope, a name a script
introduced (a function it defined or a value it bound), or one of the
two attribute calls ``mission.observe(...)`` / ``mission.event(...)``.
``exec``, ``eval``, ``compile``, and ``__import__`` are rejected by
name even if a script binds those identifiers locally.

Rejected outright
-----------------
``Import`` / ``ImportFrom``, ``ClassDef``, ``AsyncFunctionDef`` /
``AsyncFor`` / ``AsyncWith``, ``Yield`` / ``YieldFrom``, ``Global`` /
``Nonlocal``, ``Match``, ``With``, ``Assert``, ``Delete``, decorators
(the allowlist is currently empty), bare ``except:`` clauses,
attribute access on anything other than ``mission``, calls on
attributes / subscripts / other calls, dunder strings and identifiers,
and any binding (``Assign``, ``AnnAssign``, ``AugAssign``, walrus,
function parameter, function name, comprehension target, ``for``
target, ``except as`` name) whose name shadows a base-scope identifier.

``Await`` carries a single, narrow exception: ``await <tool>(...)``
where ``<tool>`` is a bare name on the per-session tool allowlist.
The runtime layer below exposes every allowlisted tool through the
underlying Monty ``external_functions`` channel as a coroutine
factory, so the script must ``await`` the call to receive the
dispatcher's return value rather than a coroutine object. Every
other ``Await`` shape — ``await name`` on a non-call, ``await
mission.observe(...)``, ``await some_other_tool()`` for a tool not on
the allowlist, ``await (lambda: ...)()`` — stays rejected with
reason ``await_not_allowed``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from typing import Final, NoReturn

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``validate_script_ast`` -> ``diagrams/code_diagrams/mcp/mission/sandbox.validate_script_ast.html``
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

_SAFE_BUILTINS: Final[frozenset[str]] = frozenset(
    {
        "len",
        "min",
        "max",
        "sum",
        "abs",
        "any",
        "all",
        "sorted",
        "range",
        "enumerate",
        "zip",
        "list",
        "dict",
        "tuple",
        "set",
        "str",
        "int",
        "float",
        "bool",
    }
)
"""Pure stdlib callables a script may look up by bare name."""

_ALLOWED_EXCEPTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "LookupError",
        "RuntimeError",
        "ArithmeticError",
        "ZeroDivisionError",
        "OverflowError",
        "OSError",
        "FileNotFoundError",
        "TimeoutError",
        "ConnectionError",
        "StopIteration",
        "AssertionError",
    }
)
"""Built-in exception classes a script may name in ``raise`` and ``except``.

Including these in the base scope is what lets a script say
``except ValueError as e:`` or ``raise RuntimeError("msg")`` without an
``import``. Constructing an exception instance is side-effect-free, so
exposing the class is no broader than exposing the safe builtins.
"""

_MISSION_NAMESPACE_NAME: Final[str] = "mission"
"""Top-level identifier reserved for the per-iteration helper namespace."""

_MISSION_HELPER_ATTRIBUTES: Final[frozenset[str]] = frozenset({"observe", "event"})
"""Only attributes the validator accepts on the ``mission`` namespace."""

_FORBIDDEN_CALL_TARGETS: Final[frozenset[str]] = frozenset(
    {"exec", "eval", "compile", "__import__"}
)
"""Names whose call form is rejected by name even if a script shadows them.

A script could in principle write ``def exec(): ...`` and then call its
own local. Rejecting these names at the call site as well as via the
dunder filter (for ``__import__``) closes the gap.
"""

_ALLOWED_DECORATORS: Final[frozenset[str]] = frozenset()
"""Decorator names a function definition may carry.

Currently empty: any ``@decorator`` on a ``FunctionDef`` is rejected.
The hook is here so a future iteration can vet a small set of operator-
facing helpers (e.g. a retry decorator) by editing only this constant.
"""

_ALLOWED_BIN_OPS: Final[tuple[type[ast.operator], ...]] = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.MatMult,
)

_ALLOWED_UNARY_OPS: Final[tuple[type[ast.unaryop], ...]] = (
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.Invert,
)

_ALLOWED_COMPARE_OPS: Final[tuple[type[ast.cmpop], ...]] = (
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)

_ALLOWED_BOOL_OPS: Final[tuple[type[ast.boolop], ...]] = (ast.And, ast.Or)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ScriptRejected(Exception):
    """Raised when a script source contains a disallowed construct.

    Mirror of :class:`mcp.mission.predicate.PredicateRejected` so callers
    can render uniform structured errors regardless of which sandbox
    layer rejected the input. ``reason`` is a short stable token (e.g.
    ``"forbidden_node"``, ``"shadows_protected_name"``) suitable for
    machine-readable error envelopes; ``failing_node`` is the
    :class:`ast.AST` that triggered rejection (``None`` only when the
    source failed to parse at all).
    """

    def __init__(
        self,
        reason: str,
        *,
        failing_node: ast.AST | None = None,
        message: str | None = None,
    ) -> None:
        self.reason: str = reason
        self.failing_node: ast.AST | None = failing_node
        self.lineno: int | None = (
            getattr(failing_node, "lineno", None) if failing_node is not None else None
        )
        self.col_offset: int | None = (
            getattr(failing_node, "col_offset", None) if failing_node is not None else None
        )
        rendered = message if message is not None else reason
        if self.lineno is not None:
            rendered = f"{rendered} (line {self.lineno}, col {self.col_offset})"
        super().__init__(rendered)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class _ScriptValidator(ast.NodeVisitor):
    """Walk a script AST and reject any construct outside the allowlist.

    The validator tracks two things across the walk:

    * **The base scope** — the union of the operator-supplied tool
      allowlist, the safe builtins, the allowed exception names, and
      the ``mission`` namespace. These names are *protected*: a script
      may read them but may not bind, rebind, or shadow them with a
      local of any kind (assignment, walrus, function parameter,
      function name, comprehension target, ``for`` target,
      ``except as`` name). Protecting them keeps the security model
      one-line-tall: if you see a Name in the source whose ``id`` is
      ``submit_job_sqs``, you can be sure it resolves to the registered
      tool.
    * **A scope stack** — entries onto the stack carry the names a
      script has bound at module level plus the names introduced by
      function parameters, comprehension targets, ``for`` loops, and
      ``except as`` clauses. The stack is what makes a helper function
      that defines a parameter ``i`` validate cleanly without ``i``
      leaking into the module-level scope.
    """

    def __init__(self, allowlist: Iterable[str]) -> None:
        # Order does not matter; keep as a frozenset for fast membership.
        self._tool_allowlist: frozenset[str] = frozenset(allowlist)

        # Names that are visible from the start of the script and that
        # script-introduced bindings may NOT shadow. The mission
        # namespace counts as protected: rebinding it would defeat the
        # one-allowed-attribute-base rule in :meth:`visit_Attribute`.
        # The forbidden call targets (``eval``, ``exec``, ``compile``,
        # ``__import__``) are folded into the protected set so that a
        # script trying to shadow them — ``(eval := 1)``, ``def exec():
        # ...``, ``for compile in xs:`` — is rejected at the binding
        # site with ``shadows_protected_name``, in addition to the
        # call-site rejection in :meth:`visit_Call`. Two layers of
        # defense for the same risk: a reader does not have to chase
        # every later use to know whether the shadow is harmful.
        self._base_scope: frozenset[str] = (
            self._tool_allowlist
            | _SAFE_BUILTINS
            | _ALLOWED_EXCEPTION_NAMES
            | _FORBIDDEN_CALL_TARGETS
            | {_MISSION_NAMESPACE_NAME}
        )

        # Stack of frozensets of script-bound names (function params,
        # for-loop targets, comprehension targets, assignment targets,
        # function definitions). The base frame is empty; each scope
        # push appends a new frame whose contents accumulate from the
        # parent frame so a nested lookup can see outer locals.
        self._scopes: list[frozenset[str]] = [frozenset()]

    # ---- helpers -------------------------------------------------------

    def _current_locals(self) -> frozenset[str]:
        return self._scopes[-1]

    def _name_is_visible(self, name: str) -> bool:
        return name in self._base_scope or name in self._current_locals()

    @staticmethod
    def _is_dunder(name: str) -> bool:
        return name.startswith("__")

    @staticmethod
    def _reject(reason: str, node: ast.AST, message: str | None = None) -> NoReturn:
        raise ScriptRejected(reason, failing_node=node, message=message)

    def _push_scope(self, locals_: frozenset[str]) -> None:
        self._scopes.append(self._current_locals() | locals_)

    def _pop_scope(self) -> None:
        self._scopes.pop()

    def _bind_local(self, name: str, node: ast.AST) -> None:
        """Add ``name`` to the current frame, rejecting protected shadows.

        Used by every binding form (assignment, walrus, function name,
        function parameter, ``for`` target, comprehension target,
        ``except as`` name). The shadow check is what prevents a
        script from rebinding ``submit_job_sqs`` or ``mission`` and
        thereby sneaking past later name-based validation.
        """
        if self._is_dunder(name):
            self._reject(
                "dunder_binding",
                node,
                f"binding to '{name}' is not allowed (starts with '__')",
            )
        if name in self._base_scope:
            self._reject(
                "shadows_protected_name",
                node,
                f"binding to '{name}' shadows a protected name",
            )
        # The accumulated-frame model means we replace the top frame
        # rather than mutate it in place: every ``_push_scope`` already
        # captured the parent, and append-adds at the leaf are local to
        # this frame.
        self._scopes[-1] = self._scopes[-1] | {name}

    def _collect_target_names(self, target: ast.AST) -> list[ast.Name]:
        """Flatten an assignment / for / comprehension target.

        Tuples and lists nest (``for (a, b) in pairs``). ``Starred``
        wraps (``a, *rest = xs``). Anything else under a target —
        ``Subscript``, ``Attribute`` — would be a write into a
        non-local namespace and is rejected by the caller via the
        ``invalid_target`` reason.
        """
        if isinstance(target, ast.Name):
            return [target]
        if isinstance(target, (ast.Tuple, ast.List)):
            collected: list[ast.Name] = []
            for elt in target.elts:
                collected.extend(self._collect_target_names(elt))
            return collected
        if isinstance(target, ast.Starred):
            return self._collect_target_names(target.value)
        self._reject(
            "invalid_target",
            target,
            "assignment / loop target must be a plain identifier",
        )
        return []  # unreachable; _reject raises

    def _bind_targets(self, target: ast.AST) -> None:
        for name_node in self._collect_target_names(target):
            self._bind_local(name_node.id, name_node)

    # ---- top-level entry ----------------------------------------------

    def visit_Module(self, node: ast.Module) -> None:
        # ``ast.parse(..., mode="exec")`` produces a Module whose body
        # is a list of statements. Walk each in order so any forward
        # binding (e.g. a function definition followed by a call)
        # validates with the binding visible in the same module scope.
        for stmt in node.body:
            self.visit(stmt)

    # ---- catch-all -----------------------------------------------------

    def generic_visit(self, node: ast.AST) -> None:
        # Default rejection: the validator opts in to every supported
        # node via a dedicated ``visit_*`` method. Anything reaching
        # ``generic_visit`` is something the operator wrote that the
        # script surface deliberately does not support — ``Import``,
        # ``ClassDef``, ``Global``, ``Nonlocal``, ``Match``, ``With``,
        # ``Assert``, ``Delete``, ``Yield``, ``AsyncFunctionDef`` /
        # ``AsyncFor`` / ``AsyncWith`` (``Await`` is handled by its
        # own narrow visitor), etc.
        self._reject(
            "forbidden_node",
            node,
            f"{type(node).__name__} is not allowed in a script",
        )

    # ---- statements ----------------------------------------------------

    def visit_Expr(self, node: ast.Expr) -> None:
        self.visit(node.value)

    def visit_Pass(self, node: ast.Pass) -> None:
        # No children; the visitor still has to opt in to keep
        # generic_visit from rejecting it.
        pass

    def visit_Break(self, node: ast.Break) -> None:
        pass

    def visit_Continue(self, node: ast.Continue) -> None:
        pass

    def visit_Assign(self, node: ast.Assign) -> None:
        # Validate the RHS *first* under the current scope, then bind
        # the LHS targets. This ordering matters for ``x = x + 1``: the
        # right-hand ``x`` must already exist as a local; if it does
        # not, the ``visit_Name`` lookup fails. Conversely, ``x = 1``
        # introduces ``x`` only after the literal validates.
        self.visit(node.value)
        for target in node.targets:
            self._bind_targets(target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if not isinstance(node.op, _ALLOWED_BIN_OPS):
            self._reject(
                "binop_not_allowed",
                node,
                f"augmented operator {type(node.op).__name__} is not allowed",
            )
        # ``x += 1`` reads ``x`` then writes ``x``. The target Name
        # must be visible already (no defining via aug-assign), and
        # the target itself must not be a protected name. We re-use
        # ``_bind_local`` for the shadow check; if ``x`` is already
        # local the bind is a no-op.
        if not isinstance(node.target, ast.Name):
            self._reject(
                "invalid_target",
                node.target,
                "augmented assignment target must be a plain identifier",
            )
        # Read-side check: target must already be in scope.
        self.visit(node.target)
        self.visit(node.value)
        # Bind defensively — protects against aug-assign on a
        # protected name even though the read-side visit above would
        # already accept it (protected names ARE visible). The
        # shadow check fires here.
        self._bind_local(node.target.id, node.target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # ``x: int = 1`` and ``x: int`` are accepted; ``obj.attr: int``
        # is not (target must be a plain identifier).
        if node.value is not None:
            self.visit(node.value)
        if node.annotation is not None:
            self.visit(node.annotation)
        if not isinstance(node.target, ast.Name):
            self._reject(
                "invalid_target",
                node.target,
                "annotated assignment target must be a plain identifier",
            )
        self._bind_local(node.target.id, node.target)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_For(self, node: ast.For) -> None:
        # Validate the iterable in the *outer* scope, then bind the
        # loop targets in the same scope as the body. ``for x in xs:``
        # leaks ``x`` after the loop, matching Python semantics.
        self.visit(node.iter)
        self._bind_targets(node.target)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            self.visit(node.exc)
        if node.cause is not None:
            self.visit(node.cause)

    def visit_Try(self, node: ast.Try) -> None:
        # Body of the try block runs in the current scope.
        for stmt in node.body:
            self.visit(stmt)
        for handler in node.handlers:
            # Bare ``except:`` is rejected — operators must name the
            # exception class so an unrelated bug is not silently
            # swallowed by the same handler that catches a tool
            # timeout.
            if handler.type is None:
                self._reject(
                    "bare_except",
                    handler,
                    "bare 'except:' is not allowed; name the exception class",
                )
            self.visit(handler.type)
            # ``except Exc as name:`` introduces ``name`` only inside
            # the handler block, mirroring Python semantics. Push a
            # new scope so the binding does not leak to siblings.
            self._push_scope(frozenset())
            try:
                if handler.name is not None:
                    # ``handler`` is the canonical AST node for the
                    # binding location; reuse it as the failing-node
                    # context for shadow rejections.
                    self._bind_local(handler.name, handler)
                for stmt in handler.body:
                    self.visit(stmt)
            finally:
                self._pop_scope()
        for stmt in node.orelse:
            self.visit(stmt)
        for stmt in node.finalbody:
            self.visit(stmt)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Decorators are gated by a dedicated allowlist so the security
        # surface stays small. The list is currently empty.
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Name) and deco.id in _ALLOWED_DECORATORS):
                self._reject(
                    "decorator_not_allowed",
                    deco,
                    "decorators are not allowed on script functions",
                )
        # Bind the function name in the *current* scope so the rest of
        # the module can call it. The body opens a new scope under
        # which arguments live.
        self._bind_local(node.name, node)
        self._validate_function_signature_and_body(node.args, node.body, node)

    def _validate_function_signature_and_body(
        self,
        args: ast.arguments,
        body: list[ast.stmt],
        owner: ast.AST,
    ) -> None:
        # No defaults that touch the outer scope are forbidden, but
        # the default expressions still validate under the *outer*
        # scope (Python evaluates them once at def time, not per call).
        for default in args.defaults:
            self.visit(default)
        for kw_default in args.kw_defaults:
            if kw_default is not None:
                self.visit(kw_default)

        # Collect parameter names. Reject duplicates and protected
        # shadows up front so the body sees a coherent local frame.
        param_names: list[tuple[str, ast.AST]] = []

        def _collect_arg(arg: ast.arg) -> None:
            param_names.append((arg.arg, arg))
            if arg.annotation is not None:
                self.visit(arg.annotation)

        for arg in args.posonlyargs:
            _collect_arg(arg)
        for arg in args.args:
            _collect_arg(arg)
        if args.vararg is not None:
            _collect_arg(args.vararg)
        for arg in args.kwonlyargs:
            _collect_arg(arg)
        if args.kwarg is not None:
            _collect_arg(args.kwarg)

        # Push a fresh frame; bindings inside the function do not
        # leak to the module-level scope.
        self._push_scope(frozenset())
        try:
            seen: set[str] = set()
            for name, owning_node in param_names:
                if name in seen:
                    self._reject(
                        "duplicate_parameter",
                        owning_node,
                        f"duplicate parameter '{name}'",
                    )
                seen.add(name)
                self._bind_local(name, owning_node)
            for stmt in body:
                self.visit(stmt)
        finally:
            self._pop_scope()

    # ---- expressions ---------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        # Reject dunder strings even when used as plain data. The same
        # rationale as in the predicate sandbox: a string like
        # ``"__class__"`` only ever appears in source code as part of
        # an introspection escape pattern (``getattr(x, "__class__")``,
        # ``locals()["__import__"]``). Forbidding them at the constant
        # level closes those off even if a future change widened the
        # call or attribute allowlist.
        if isinstance(node.value, str) and self._is_dunder(node.value):
            self._reject(
                "dunder_string",
                node,
                "string constants starting with '__' are not allowed",
            )

    def visit_Name(self, node: ast.Name) -> None:
        if self._is_dunder(node.id):
            self._reject(
                "dunder_name",
                node,
                f"identifier '{node.id}' starts with '__'",
            )
        if not self._name_is_visible(node.id):
            self._reject(
                "name_not_allowed",
                node,
                f"name '{node.id}' is not in the script allowlist",
            )

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        # ``(x := expr)`` — the walrus binds ``x`` in the enclosing
        # scope. Validate the value first, then route through the
        # standard binding helper so the protected-name shadow check
        # fires for ``(mission := ...)`` etc.
        self.visit(node.value)
        if not isinstance(node.target, ast.Name):
            self._reject(
                "invalid_target",
                node.target,
                "walrus target must be a plain identifier",
            )
        self._bind_local(node.target.id, node.target)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Lambdas are scoped expressions: validate parameters + body
        # under a fresh frame, exactly like a ``FunctionDef`` minus
        # the decorator list and statement body. The lambda itself
        # produces no binding in the enclosing scope.
        self._validate_function_signature_and_body(node.args, [ast.Expr(value=node.body)], node)

    # ---- containers ----------------------------------------------------

    def visit_List(self, node: ast.List) -> None:
        for elt in node.elts:
            self.visit(elt)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        for elt in node.elts:
            self.visit(elt)

    def visit_Set(self, node: ast.Set) -> None:
        for elt in node.elts:
            self.visit(elt)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            if key is not None:
                self.visit(key)
            else:
                # ``{**other}`` would let a script splat arbitrary
                # mappings into a dict literal; reject for the same
                # reason as in the predicate sandbox.
                self._reject(
                    "dict_unpacking",
                    node,
                    "dict unpacking is not allowed in a script",
                )
        for value in node.values:
            self.visit(value)

    def visit_Starred(self, node: ast.Starred) -> None:
        # ``[*xs]``, ``f(*xs)``, ``a, *rest = xs`` — recurse into the
        # inner expression so the nested Name still hits the
        # allowlist check.
        self.visit(node.value)

    # ---- operators -----------------------------------------------------

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not isinstance(node.op, _ALLOWED_BIN_OPS):
            self._reject(
                "binop_not_allowed",
                node,
                f"binary operator {type(node.op).__name__} is not allowed",
            )
        self.visit(node.left)
        self.visit(node.right)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if not isinstance(node.op, _ALLOWED_UNARY_OPS):
            self._reject(
                "unaryop_not_allowed",
                node,
                f"unary operator {type(node.op).__name__} is not allowed",
            )
        self.visit(node.operand)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if not isinstance(node.op, _ALLOWED_BOOL_OPS):
            self._reject(
                "boolop_not_allowed",
                node,
                f"bool operator {type(node.op).__name__} is not allowed",
            )
        for value in node.values:
            self.visit(value)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op in node.ops:
            if not isinstance(op, _ALLOWED_COMPARE_OPS):
                self._reject(
                    "compareop_not_allowed",
                    node,
                    f"comparison operator {type(op).__name__} is not allowed",
                )
        self.visit(node.left)
        for comparator in node.comparators:
            self.visit(comparator)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        self.visit(node.body)
        self.visit(node.orelse)

    # ---- attribute and subscript --------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # The script surface allows attribute access on exactly one
        # name — the ``mission`` namespace — and only for the two
        # helper attributes ``observe`` and ``event``. Every other
        # ``foo.bar`` reads raise ``ScriptRejected``: tool results are
        # opaque values, not deep object graphs, so a script that
        # needs nested data should use subscripting on a return value.
        if self._is_dunder(node.attr):
            self._reject(
                "dunder_attribute",
                node,
                f"attribute '{node.attr}' starts with '__'",
            )
        if not isinstance(node.value, ast.Name):
            self._reject(
                "attribute_target_not_name",
                node,
                "attribute access is only allowed on the 'mission' namespace",
            )
        if node.value.id != _MISSION_NAMESPACE_NAME:
            self._reject(
                "attribute_target_not_allowed",
                node,
                "attribute access is only allowed on the 'mission' namespace",
            )
        if node.attr not in _MISSION_HELPER_ATTRIBUTES:
            self._reject(
                "attribute_not_allowed",
                node,
                f"'mission.{node.attr}' is not an allowed helper",
            )
        # ``mission`` itself is a base-scope name; visit it for
        # regularity so any future Name-side check still fires here.
        self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # Recurse into both the value and the slice. The base of the
        # chain falls out as a ``Name`` lookup that hits the
        # allowlist; slices may themselves contain Names and Calls
        # that go through the same validation path.
        self.visit(node.value)
        self.visit(node.slice)

    def visit_Slice(self, node: ast.Slice) -> None:
        if node.lower is not None:
            self.visit(node.lower)
        if node.upper is not None:
            self.visit(node.upper)
        if node.step is not None:
            self.visit(node.step)

    # ---- calls ---------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        # The callee form decides which rule applies. Three shapes are
        # allowed:
        #
        # * ``name(...)`` — bare name call. The name must already be
        #   visible (base-scope or script-bound local).
        # * ``mission.observe(...)`` / ``mission.event(...)`` — the
        #   only attribute-call shape supported.
        #
        # ``foo()()`` (call returning a callable, then call), ``a[0]()``
        # (subscript-then-call), and ``x.y()`` for any ``y`` not on the
        # mission helper list are all rejected outright.
        func = node.func
        if isinstance(func, ast.Name):
            # ``__import__``, ``exec``, ``eval``, ``compile`` are
            # rejected by name even if a script defined a local with
            # one of those names. The dunder filter in
            # :meth:`visit_Name` already rejects ``__import__`` for
            # plain reads; the explicit list is what blocks the
            # ``def exec(): ...; exec()`` shadow attempt.
            if func.id in _FORBIDDEN_CALL_TARGETS:
                self._reject(
                    "forbidden_call_target",
                    node,
                    f"call to '{func.id}' is not allowed",
                )
            # Visit the Name so the visibility / dunder check fires.
            self.visit(func)
        elif isinstance(func, ast.Attribute):
            # Only ``mission.observe`` / ``mission.event``. The
            # attribute visit raises with a structured reason for
            # every other shape (non-Name base, non-mission base,
            # disallowed attribute), so we just recurse here.
            self.visit(func)
        else:
            # ``f()()``, ``xs[0]()``, ``(lambda: ...)()`` — the
            # callee is neither a Name nor a single ``mission.<x>``
            # attribute access. Reject without descending; the
            # blanket ``call_target_shape`` reason captures all three.
            self._reject(
                "call_target_shape",
                node,
                "script calls must target a bare name or 'mission.<helper>'",
            )
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            # ``**kwargs`` shows up as a keyword with arg=None; allow
            # the value but recurse so its content is still validated
            # against the same name and call rules.
            self.visit(kw.value)

    def visit_Await(self, node: ast.Await) -> None:
        # The runtime layer (:class:`MissionSandbox`) exposes every
        # allowlisted tool through Monty's ``external_functions``
        # channel, where a registered async callable surfaces inside
        # the script as a coroutine factory. Calling
        # ``find_examples(query="gpu")`` from inside a script returns
        # a coroutine object, not the dispatcher's return value;
        # consuming the value requires writing ``await
        # find_examples(query="gpu")``. The two ``mission`` helpers
        # ride the same channel — the runtime layer prepends a small
        # source-level shim that makes ``mission.observe`` /
        # ``mission.event`` route into host-side closures via the same
        # coroutine-factory channel, so awaiting them is required for
        # the side effect (an observation row, an event row) to land
        # on the iteration's audit log. The validator therefore opens
        # ``Await`` for exactly two shapes:
        #
        # * ``await <name>(...)`` where ``<name>`` is on the per-
        #   session tool allowlist.
        # * ``await mission.observe(...)`` / ``await mission.event(...)``
        #   — attribute calls on the ``mission`` namespace whose
        #   attribute is one of the two helper names that
        #   :meth:`visit_Attribute` already accepts.
        #
        # Both forms route the wrapped Call back through
        # :meth:`visit_Call` so kwargs, positional args, and the
        # forbidden-call-target rules apply unchanged.
        #
        # Rejected (folded into ``await_not_allowed``):
        #
        # * ``await x`` — bare name (no Call inside).
        # * ``await some_other_tool()`` — call on a Name that is not
        #   on the per-session tool allowlist (a safe builtin, an
        #   exception class, ``mission`` itself, a script-bound local,
        #   or simply unknown).
        # * ``await mission.foo(...)`` for any ``foo`` outside the
        #   helper set — :meth:`visit_Attribute` would already reject
        #   the inner call, but the early reject here keeps the reason
        #   token stable as ``await_not_allowed``.
        # * ``await x.observe(...)`` for any ``x`` other than
        #   ``mission`` — same rationale.
        # * ``await (lambda: ...)()`` / ``await xs[0]()`` —
        #   subscript-then-call / call-of-call shapes; the underlying
        #   Call would already fail :meth:`visit_Call`'s
        #   ``call_target_shape`` check, but reject at the await
        #   level too so the reason token stays ``await_not_allowed``.
        #
        # ``AsyncFunctionDef`` / ``AsyncFor`` / ``AsyncWith`` continue
        # to fall through to :meth:`generic_visit` and stay rejected
        # with ``forbidden_node`` — the relaxation here covers only
        # the bare ``Await`` expression on the two accepted call
        # shapes.
        inner = node.value
        if not isinstance(inner, ast.Call):
            self._reject(
                "await_not_allowed",
                node,
                "'await' may only be used on a call to an allowlisted "
                "tool or a 'mission.<helper>' call",
            )
        func = inner.func
        if isinstance(func, ast.Name):
            if func.id not in self._tool_allowlist:
                self._reject(
                    "await_not_allowed",
                    node,
                    "'await' may only be used on a call to an allowlisted "
                    "tool or a 'mission.<helper>' call",
                )
        elif isinstance(func, ast.Attribute):
            # Only ``mission.observe(...)`` / ``mission.event(...)``.
            if not (
                isinstance(func.value, ast.Name)
                and func.value.id == _MISSION_NAMESPACE_NAME
                and func.attr in _MISSION_HELPER_ATTRIBUTES
            ):
                self._reject(
                    "await_not_allowed",
                    node,
                    "'await' may only be used on a call to an allowlisted "
                    "tool or a 'mission.<helper>' call",
                )
        else:
            self._reject(
                "await_not_allowed",
                node,
                "'await' may only be used on a call to an allowlisted "
                "tool or a 'mission.<helper>' call",
            )
        # Hand the Call node back to the existing call-validation
        # machinery so kwargs, positional args, and the
        # forbidden-call-target check all fire exactly as they would
        # for the non-awaited form.
        self.visit(inner)

    # ---- f-strings -----------------------------------------------------

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        for value in node.values:
            self.visit(value)

    def visit_FormattedValue(self, node: ast.FormattedValue) -> None:
        self.visit(node.value)
        if node.format_spec is not None:
            self.visit(node.format_spec)

    # ---- comprehensions -----------------------------------------------

    def _validate_comprehensions(self, generators: list[ast.comprehension]) -> frozenset[str]:
        """Walk comprehension generators and return their target names.

        Each generator's ``iter`` is validated against the *outer*
        scope (it cannot reference targets of its own generator), then
        the targets are added to the local set so the next generator's
        ``ifs`` and any later ``iter`` can see them. Async generators
        (``async for``) are rejected; the script body is sync.
        """
        accumulated: set[str] = set()
        for gen in generators:
            if gen.is_async:
                self._reject(
                    "async_comprehension",
                    gen.iter,
                    "async comprehensions are not allowed",
                )
            self.visit(gen.iter)
            target_names = self._collect_target_names(gen.target)
            for name_node in target_names:
                if self._is_dunder(name_node.id):
                    self._reject(
                        "dunder_comprehension_target",
                        name_node,
                        f"comprehension target '{name_node.id}' starts with '__'",
                    )
                if name_node.id in self._base_scope:
                    self._reject(
                        "shadows_protected_name",
                        name_node,
                        f"comprehension target '{name_node.id}' shadows a protected name",
                    )
                accumulated.add(name_node.id)
            self._push_scope(frozenset(accumulated))
            try:
                for if_clause in gen.ifs:
                    self.visit(if_clause)
            finally:
                self._pop_scope()
        return frozenset(accumulated)

    def _visit_comprehension_like(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp,
    ) -> None:
        locals_ = self._validate_comprehensions(node.generators)
        self._push_scope(locals_)
        try:
            self.visit(node.elt)
        finally:
            self._pop_scope()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension_like(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension_like(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension_like(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        locals_ = self._validate_comprehensions(node.generators)
        self._push_scope(locals_)
        try:
            self.visit(node.key)
            self.visit(node.value)
        finally:
            self._pop_scope()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_script_ast(script: str, allowlist: list[str]) -> None:
    """Parse and validate a Mission script source string.

    On success, the function returns ``None`` and the caller may pass
    ``script`` to the sandbox runtime layer. On any disallowed
    construct, raises :class:`ScriptRejected` carrying ``reason``,
    ``failing_node``, ``lineno``, and ``col_offset``. The script is
    *never* executed by this function; it only walks the AST.

    ``allowlist`` is the per-session list of MCP tool names the script
    may call. Each name becomes a visible bare-Name and a permitted
    call target. Names not in the allowlist (and not in the safe
    builtin / exception / mission set) are rejected at every Name
    lookup.
    """
    if not isinstance(script, str):
        raise ScriptRejected(
            "not_a_string",
            message="script source must be a str",
        )
    try:
        parsed = ast.parse(script, mode="exec")
    except SyntaxError as exc:
        rejection = ScriptRejected(
            "syntax_error",
            message=f"could not parse script: {exc.msg}",
        )
        rejection.lineno = exc.lineno
        rejection.col_offset = exc.offset
        raise rejection from exc
    _ScriptValidator(allowlist).visit(parsed)


# ===========================================================================
# Runtime layer — MissionSandbox wrapper around MontySandboxProvider
# ===========================================================================
#
# Where ``validate_script_ast`` above is the parse-time gate, the wrapper
# below is the run-time isolation. A validated script is handed to the
# Monty sandbox under shared duration / memory limits, with two extras
# layered on top:
#
# * The operator-supplied tool allowlist is exposed as a set of async
#   callables in the script's namespace. Each callable forwards into the
#   engine's tool dispatcher so the existing ``@audit_logged`` /
#   feature-flag / allowlist semantics still fire — running inside a
#   script is *not* a way to bypass any of those.
# * A ``mission`` namespace object exposes the iteration's read-only
#   metadata (deep-copied snapshot of the session's directive, criteria,
#   budget, and prior-iteration summaries) plus the two streaming
#   helpers ``mission.observe(...)`` / ``mission.event(...)``. The
#   helpers append into closure-captured lists that ``MissionSandbox.run``
#   merges into the resulting Observation.
#
# On any limit violation (duration, memory, runtime / typing / syntax
# from inside the script) the ``MontyError`` family bubbles out of the
# provider; the wrapper re-raises it as :class:`SandboxTerminated`
# carrying whatever the script collected before it was killed so the
# engine's ``_decide_phase`` can produce a deterministic ``terminate``
# verdict with the partial observation attached.

import copy  # noqa: E402 — runtime layer below; keep imports near their consumers
import os  # noqa: E402
import time  # noqa: E402
from collections.abc import Awaitable, Callable  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from types import MappingProxyType  # noqa: E402
from typing import Any  # noqa: E402

from . import audit as _audit  # noqa: E402

# ---------------------------------------------------------------------------
# Env helpers — module-level so the constants below are read once at import
# time. Tests pin the constants by monkey-patching the module attributes; a
# per-call read of os.environ would defeat that.
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    """Parse an integer env var; fall back to default on missing/empty/non-numeric.

    Mirrors the helper in :mod:`mcp.server` so the two code-mode entry
    points read the same caps with the same parsing semantics. Empty,
    whitespace-only, and non-numeric values all collapse to ``default``
    rather than raising — an operator who fat-fingers the env should
    still get a working sandbox.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    """Parse a float env var; fall back to default on missing/empty/non-numeric.

    Same fall-back semantics as :func:`_int_env`. The duration cap is a
    float so fractional seconds remain expressible.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Read the resource caps once at import time. Tests pin behaviour by
# monkey-patching these module-level constants before constructing a
# MissionSandbox. The defaults match the existing precedent in
# ``mcp/server.py`` where the same env names are wired into the
# Code Mode discovery transform's sandbox.
_DURATION_LIMIT_SECS: float = _float_env("GCO_MCP_CODE_MODE_MAX_DURATION_SECS", 30.0)
_MEMORY_LIMIT_BYTES: int = _int_env("GCO_MCP_CODE_MODE_MAX_MEMORY", 200_000_000)


# ---------------------------------------------------------------------------
# Lazy import of the runtime dependencies
# ---------------------------------------------------------------------------
#
# The AST validator above must remain importable on a host where
# ``fastmcp`` and ``pydantic_monty`` are not installed (for example a
# CLI-only environment that runs ``gco mission validate`` against a
# stored session JSON without ever wiring an engine). The provider class
# and the error class are pulled in lazily by ``_import_provider`` and
# cached at module level so repeated MissionSandbox constructions in the
# same process pay the import cost exactly once.

_MONTY_PROVIDER_CLASS: Any = None
_MONTY_ERROR_CLASS: Any = None


def _import_provider() -> tuple[Any, Any]:
    """Lazy-import ``MontySandboxProvider`` and ``MontyError`` and cache them.

    Returns the ``(provider_cls, error_cls)`` pair. The provider class
    is the value the wrapper instantiates with a ``ResourceLimits``
    dict; the error class is the *base* ``pydantic_monty.MontyError``
    that covers the whole limit / runtime / typing / syntax family
    raised from inside a script. We catch the base class rather than
    the leaves so a future Monty release that adds a new error type
    still routes through ``SandboxTerminated`` rather than escaping as
    an opaque ``Exception``.
    """
    global _MONTY_PROVIDER_CLASS, _MONTY_ERROR_CLASS
    if _MONTY_PROVIDER_CLASS is None:
        from fastmcp.experimental.transforms.code_mode import MontySandboxProvider
        from pydantic_monty import MontyError

        _MONTY_PROVIDER_CLASS = MontySandboxProvider
        _MONTY_ERROR_CLASS = MontyError
    return _MONTY_PROVIDER_CLASS, _MONTY_ERROR_CLASS


# ---------------------------------------------------------------------------
# Termination signal
# ---------------------------------------------------------------------------


class SandboxTerminated(Exception):
    """Raised when the Monty sandbox killed the script for exceeding a limit.

    The Mission engine catches this exception in its decide-phase and
    produces a ``terminate`` verdict for the iteration. Whatever the
    script collected via ``mission.observe(...)`` / ``mission.event(...)``
    before being killed is carried on the exception so the engine can
    surface the partial Observation in the iteration's audit record —
    a script that ran for 29 seconds and observed five intermediate
    states should not lose those five states just because the 30-second
    cap fired before the script returned.

    ``cause`` is the underlying Monty exception's class name (e.g.
    ``"MontyRuntimeError"``, ``"MontyTypingError"``) so callers can render
    a stable structured-error envelope without holding a reference to
    the original Monty exception object.
    """

    def __init__(
        self,
        cause: str,
        *,
        partial_observations: list[dict[str, Any]] | None = None,
        partial_events: list[dict[str, Any]] | None = None,
        partial_script_call_log: list[dict[str, Any]] | None = None,
    ) -> None:
        self.cause: str = cause
        # Defensive copies: callers occasionally inspect these lists
        # after the exception has propagated several frames up. A
        # shared reference would let a later mutation in the original
        # closure corrupt the audit record.
        self.partial_observations: list[dict[str, Any]] = list(partial_observations or [])
        self.partial_events: list[dict[str, Any]] = list(partial_events or [])
        # Partial in-script tool-call log captured by the per-tool
        # wrappers up to the moment Monty killed the script. Carrying
        # this onto the exception lets the engine's
        # ``_execute_script`` stash the partial calls on the iteration
        # record so a script that fired ten ``submit_job_sqs(...)``
        # calls before tripping the duration cap still records all ten
        # in the audit log. Defensive copy for the same reason as the
        # observe / event lists above.
        self.partial_script_call_log: list[dict[str, Any]] = list(partial_script_call_log or [])
        super().__init__(f"sandbox terminated: {cause}")


# ---------------------------------------------------------------------------
# Script rewrite — mission.observe/event → _mission_observe/_mission_event
# ---------------------------------------------------------------------------
#
# The AST gate above accepts ``mission.observe(...)`` and
# ``mission.event(...)`` as the only two attribute calls a script may
# write on the ``mission`` namespace. The runtime needs those calls to
# land on host-side closures so the iteration's ``observe_log`` /
# ``event_log`` lists actually receive the appends — passing the
# helpers in through ``inputs={"mission": <object>}`` would not work,
# because :class:`MontySandboxProvider` round-trips ``inputs`` values
# into the Monty VM by value (any in-script mutation lands on the VM
# copy, not the host's). Wrapping the helpers in a small host-side
# class and prepending it to the script as a preamble would not work
# either: Monty's parser does not support ``class`` definitions.
#
# Instead, after validation, the host re-parses the script and
# rewrites every accepted ``mission.<helper>(...)`` Call so its
# callee becomes a bare-Name lookup of the corresponding reserved
# external-function name. The rewritten source is then handed to
# Monty, where ``_mission_observe`` / ``_mission_event`` resolve to
# the host-side closures registered via ``external_functions``.
# Operator scripts cannot reference these names directly: the AST
# validator rejects them under ``name_not_allowed`` (neither is on
# the per-session tool allowlist nor in any safe-builtin / exception
# / mission base set), so the only path that produces those Name
# nodes is the rewrite below.

_MISSION_HELPER_RUNTIME_NAMES: Final[dict[str, str]] = {
    "observe": "_mission_observe",
    "event": "_mission_event",
}
# The keys must mirror ``_MISSION_HELPER_ATTRIBUTES`` exactly:
# the validator opens up ``mission.<attr>`` for those two attributes,
# and the rewriter below has to translate the same two and only the
# same two. A future widening of the helper set has to add an entry
# here too, or the rewriter would leave the new attribute as an
# ``Attribute`` callee and Monty's parser would reject it.
assert set(_MISSION_HELPER_RUNTIME_NAMES) == set(_MISSION_HELPER_ATTRIBUTES)


class _MissionAttributeCallRewriter(ast.NodeTransformer):
    """Rewrite ``mission.observe(...)`` / ``mission.event(...)`` callees.

    The transformer replaces the ``Attribute`` callee on accepted
    ``mission.<helper>`` Call nodes with a ``Name`` referencing the
    corresponding external-function key (``_mission_observe`` /
    ``_mission_event``). Args and kwargs ride through unchanged: the
    AST validator already vetted them, and the rewrite preserves
    source positions so any subsequent error in those subtrees still
    points at the operator's original column.

    The validator's :meth:`_ScriptValidator.visit_Attribute` already
    rejects every other ``mission.<x>`` shape, so the transformer
    only ever encounters the two helper attributes; defensive
    fallthrough leaves any other ``Attribute`` callee untouched, but
    in practice such a node would not have passed the gate.
    """

    def visit_Call(self, node: ast.Call) -> ast.AST:
        # Recurse into args / kwargs first so a nested
        # ``mission.<helper>(...)`` (e.g. inside an f-string used as
        # an argument) is rewritten too. ``self.generic_visit``
        # walks children and updates them in place.
        self.generic_visit(node)
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == _MISSION_NAMESPACE_NAME
            and func.attr in _MISSION_HELPER_RUNTIME_NAMES
        ):
            replacement = ast.Name(
                id=_MISSION_HELPER_RUNTIME_NAMES[func.attr],
                ctx=ast.Load(),
            )
            ast.copy_location(replacement, func)
            node.func = replacement
        return node


def _rewrite_mission_helpers(script: str) -> str:
    """Re-parse ``script``, rewrite mission helper calls, and unparse.

    Called after :func:`validate_script_ast` has already accepted the
    source — so ``ast.parse`` cannot fail here on syntax that was
    valid moments ago. Returns a fresh source string suitable for
    handing to ``MontySandboxProvider.run``.
    """
    tree = ast.parse(script, mode="exec")
    rewritten = _MissionAttributeCallRewriter().visit(tree)
    ast.fix_missing_locations(rewritten)
    return ast.unparse(rewritten)


# ---------------------------------------------------------------------------
# Tool callable wrapper
# ---------------------------------------------------------------------------


def _make_tool_wrapper(
    tool_name: str,
    ctx: Any | None,
    tool_dispatcher: Callable[[str, dict[str, Any], Any], Awaitable[Any]],
    script_call_log: list[dict[str, Any]],
    session_id: str,
    iteration_index: int,
    cost_estimators: dict[str, Callable[[dict[str, Any]], float]] | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Build the per-tool async wrapper inserted into ``external_functions``.

    The wrapper is keyword-only by design — the Mission script grammar
    passes tool args as kwargs (``submit_job_sqs(manifest_path=...,
    region=...)``) and rejecting positionals at call time keeps the
    wrapper's record shape aligned with the engine's
    :class:`ToolCallRecord`. A script that calls
    ``submit_job_sqs("examples/x.yaml")`` with a positional argument
    fails immediately with a ``TypeError`` from Python's call
    machinery; that error surfaces through Monty as a
    ``MontyRuntimeError`` and is caught by the wrapper layer in
    :meth:`MissionSandbox.run`.

    The wrapper appends one record to ``script_call_log`` per call,
    whether the call succeeded or raised. A raised exception still
    propagates out of the wrapper (so Monty surfaces it to the script
    as a Python exception the script can catch with
    ``try``/``except``), but the record carries ``status="failed"``
    plus a truncated error message so the engine's audit path sees
    every invocation.

    On both success and failure the wrapper also emits a
    ``mission_script_call_event`` audit row tagged
    ``via_script=True``. The dispatch into ``tool_dispatcher`` runs
    the registered tool function, so the standard ``@audit_logged``
    entry has already fired by the time the wrapper reaches its emit
    site — the script-call event is a *second*, distinct row that
    lets consumers distinguish in-script invocations from direct
    ``tool_calls`` strategy invocations without having to walk
    timestamps.

    ``cost_estimators`` is consulted on every successful call so the
    resulting :class:`ToolCallRecord` carries an accurate ``cost_usd``
    field. The engine's ``_execute_script`` walks the returned
    ``script_call_log`` after the sandbox returns and folds those
    per-call costs onto the engine's loaded
    ``session["accumulated_cost_usd"]`` so the Decide_Phase's
    ``_cost_exceeded`` check observes the same running total it would
    on the direct ``_dispatch_one_call`` path. Defaults to ``None``
    (an empty estimator map) so older callers that don't thread cost
    estimators through stay working unchanged; without estimators the
    ``cost_usd`` field is omitted from the record (matching the
    direct-dispatch convention).
    """
    estimators = cost_estimators or {}

    async def wrapper(**kwargs: Any) -> Any:
        # Snapshot the kwargs into a fresh dict before dispatch so the
        # log entry preserves exactly what the script passed even if
        # the dispatcher mutates the dict downstream.
        args = dict(kwargs)
        started = time.monotonic()
        try:
            result = await tool_dispatcher(tool_name, args, ctx)
        except Exception as exc:
            duration_ms = max(int((time.monotonic() - started) * 1000), 0)
            error_message = f"{type(exc).__name__}: {exc}"[:200]
            script_call_log.append(
                {
                    "tool_name": tool_name,
                    "args": args,
                    "status": "failed",
                    "result_summary": None,
                    "duration_ms": duration_ms,
                    # Truncated to 200 chars to match the audit
                    # module's existing convention for error_message
                    # fields elsewhere in the engine.
                    "error_message": error_message,
                }
            )
            # Emit the via_script audit row before re-raising so the
            # event is recorded even when the script catches the
            # exception and continues executing.
            _audit.emit_script_call_event(
                session_id,
                iteration_index,
                tool_name,
                "failed",
                duration_ms,
                error_message=error_message,
            )
            raise
        duration_ms = max(int((time.monotonic() - started) * 1000), 0)
        # Record the per-call cost on the call record (matching the
        # direct-dispatch shape on :class:`ToolCallRecord`). The
        # engine's ``_execute_script`` walks the returned call log
        # after the sandbox returns and folds these into the live
        # ``session["accumulated_cost_usd"]`` so the Decide_Phase's
        # ``_cost_exceeded`` check sees the same running total it
        # would on the direct dispatch path. Cost accumulation lives
        # on the engine side rather than here so the sandbox can hold
        # the session as an immutable construction-time snapshot
        # without risking that script-side mutations land on a stale
        # copy of the session record.
        cost = 0.0
        estimator = estimators.get(tool_name)
        if estimator is not None:
            try:
                raw_cost = estimator(args)
            except Exception:
                raw_cost = 0.0
            if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
                cost = float(raw_cost)
        record: dict[str, Any] = {
            "tool_name": tool_name,
            "args": args,
            "status": "ok",
            "result_summary": result,
            "duration_ms": duration_ms,
        }
        if cost:
            record["cost_usd"] = cost
        script_call_log.append(record)
        _audit.emit_script_call_event(
            session_id,
            iteration_index,
            tool_name,
            "ok",
            duration_ms,
        )
        return result

    # Setting ``__name__`` makes Monty's traceback render the
    # operator's tool name rather than ``wrapper`` when a call goes
    # wrong inside the sandboxed script. The script_call_log remains
    # the canonical record of what fired.
    wrapper.__name__ = tool_name
    return wrapper


# ---------------------------------------------------------------------------
# Observation assembly
# ---------------------------------------------------------------------------


def _build_script_observation(
    *,
    script_call_log: list[dict[str, Any]],
    observe_log: list[dict[str, Any]],
    event_log: list[dict[str, Any]],
    phase_started_at: str,
    phase_ended_at: str,
) -> dict[str, Any]:
    """Merge the closure-captured logs into an Observation dict.

    Mirrors :meth:`MissionEngine._build_observation` for the
    ``tool_calls`` strategy path so a downstream Evaluate_Phase /
    Decide_Phase consumer cannot tell, from the Observation shape
    alone, whether the iteration ran a scripted or a non-scripted
    Strategy:

    * ``tool_results`` lists every call's ``result_summary`` (including
      failures, for stable indexing against ``script_call_log``).
    * ``metrics`` lifts any top-level ``metrics`` dict from a
      successful tool result, exactly like the engine does.
    * ``events`` pools the events emitted by tool results with the
      ``mission.event(...)`` calls so the criteria evaluator only
      walks one list.
    * ``errors`` carries failed / skipped calls in the same shape the
      engine uses, so the decide-phase heuristic that triggers
      ``adjust`` on new errors keeps working unchanged.

    The ``mission.observe(...)`` rows fold into a dedicated
    ``observations`` bucket inside ``metrics`` rather than flat-merging
    so a script-collected key cannot silently overwrite a tool-derived
    metric of the same name. A criterion that wants a script-collected
    key reads ``metrics.observations.<key>``; a criterion that wants a
    tool-derived metric reads ``metrics.<key>``. The two namespaces
    stay distinct.
    """
    tool_results: list[Any] = []
    metrics: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for call in script_call_log:
        tool_results.append(call.get("result_summary"))
        if call.get("status") == "ok":
            result = call.get("result_summary")
            if isinstance(result, dict):
                result_metrics = result.get("metrics")
                if isinstance(result_metrics, dict):
                    metrics.update(result_metrics)
                result_events = result.get("events")
                if isinstance(result_events, list):
                    for event in result_events:
                        if isinstance(event, dict):
                            events.append(event)
        else:
            errors.append(
                {
                    "tool_name": call.get("tool_name"),
                    "status": call.get("status"),
                    "error_message": call.get("error_message"),
                }
            )

    # Pool the script-side ``mission.event(...)`` calls with
    # tool-derived events. ``dict(ev)`` is a defensive copy so a later
    # mutation of the closure list does not bleed into the persisted
    # Observation.
    for ev in event_log:
        events.append(dict(ev))

    # ``mission.observe(...)`` rows fold into a dedicated bucket on
    # metrics so they remain addressable without colliding with
    # tool-derived metric names.
    if observe_log:
        observations_bucket: dict[str, Any] = {}
        for entry in observe_log:
            observations_bucket[entry["key"]] = entry["value"]
        metrics["observations"] = observations_bucket

    observation: dict[str, Any] = {
        "tool_results": tool_results,
        "metrics": metrics,
        "events": events,
        "phase_started_at": phase_started_at,
        "phase_ended_at": phase_ended_at,
    }
    if errors:
        observation["errors"] = errors
    return observation


# ---------------------------------------------------------------------------
# MissionSandbox
# ---------------------------------------------------------------------------


class MissionSandbox:
    """Run a validated Mission script under ``MontySandboxProvider`` limits.

    One sandbox per iteration. The constructor freezes the per-iteration
    ``mission`` namespace as a :class:`types.MappingProxyType` snapshot
    (so a script cannot reach back through ``mission`` and mutate the
    session record), pins the operator's tool allowlist, and builds the
    underlying ``MontySandboxProvider`` with the duration / memory
    limits read from the module-level constants. :meth:`run` then
    drives a single script execution end to end:

    1. AST validate via :func:`validate_script_ast` — propagation of
       :class:`ScriptRejected` is the engine's signal to fail the
       Execute_Phase with reason ``script_rejected``.
    2. Build the ``external_functions`` map: one async wrapper per
       allowlisted tool, each forwarding into the engine's tool
       dispatcher so the wrapper preserves the existing
       ``@audit_logged`` / feature-flag / allowlist semantics — running
       inside a script is *not* a way to bypass any of those.
    3. Execute under Monty's caps. Any ``MontyError`` (limit /
       runtime / typing / syntax) is re-raised as
       :class:`SandboxTerminated` carrying whatever the script
       collected before being killed.
    4. Fold the closure-captured tool log, observe log, and event log
       into an Observation dict whose shape exactly matches the
       engine's tool-calls path.

    The sandbox is immutable after construction: there are no setters,
    no rebuild methods, and the underlying provider is held by
    reference rather than recreated per call. Each iteration gets its
    own MissionSandbox so a stale frozen namespace cannot leak across
    iterations.
    """

    def __init__(
        self,
        allowlist: list[str],
        session: Any,
        cost_estimators: dict[str, Callable[[dict[str, Any]], float]] | None = None,
    ) -> None:
        # Defensive copy of the allowlist: the engine pins the
        # allowlist on the session at create time, but a shared list
        # reference would let later mutations slip past the AST
        # validator's frozenset (which is constructed once per
        # validation call from ``self._allowlist``).
        self._allowlist: list[str] = list(allowlist)

        # Cost estimators flow through to the in-script tool wrapper
        # so each successful call records ``cost_usd`` on its
        # :class:`ToolCallRecord`. The engine's ``_execute_script``
        # walks the returned ``script_call_log`` after the sandbox
        # returns (or after :class:`SandboxTerminated` carries the
        # partial log out of a killed script) and folds the per-call
        # costs onto ``session["accumulated_cost_usd"]`` so the
        # Decide_Phase's existing ``_cost_exceeded`` check fires on
        # the same running total it would on the direct
        # ``_dispatch_one_call`` path. Holding the estimators on the
        # sandbox rather than threading them through ``run`` keeps
        # the per-call hot path free of lookups against a parameter
        # that never changes between calls on the same sandbox
        # instance.
        self._cost_estimators: dict[str, Callable[[dict[str, Any]], float]] = dict(
            cost_estimators or {}
        )

        # Build the per-iteration mission namespace as an immutable
        # snapshot. Each iteration summary carries only the four
        # fields a script needs to reason about prior progress —
        # full IterationRecord shapes would be both heavy and
        # tempting for a script to walk in ways the engine does not
        # support.
        iteration_summaries: list[dict[str, Any]] = []
        for it in session.get("iterations") or []:
            iteration_summaries.append(
                {
                    "iteration_index": it.get("iteration_index"),
                    "verdict": it.get("verdict"),
                    "verdict_reason": it.get("verdict_reason"),
                    "checkpoint_evaluated": it.get("checkpoint_evaluated"),
                }
            )
        # ``copy.deepcopy`` on criteria + budget so a script that
        # walks them via subscripting cannot mutate the session
        # record even if Python's MappingProxyType were ever
        # bypassed by a future change.
        ns: dict[str, Any] = {
            "session_id": session["session_id"],
            "iteration_index": len(session.get("iterations") or []),
            "directive_text": session.get("directive_text", ""),
            "criteria": copy.deepcopy(session.get("criteria") or []),
            "budget": copy.deepcopy(session.get("budget") or {}),
            "iterations": iteration_summaries,
        }
        self._frozen_mission_ns: MappingProxyType[str, Any] = MappingProxyType(ns)

        # Construct the provider once and pin it on the instance.
        # The provider holds no per-call state, so reusing it across
        # multiple ``run`` calls would be safe in principle, but the
        # one-sandbox-per-iteration lifetime keeps the failure
        # surface small and matches the rest of the per-iteration
        # state above.
        provider_cls, _ = _import_provider()
        self._provider = provider_cls(
            limits={
                "max_duration_secs": _DURATION_LIMIT_SECS,
                "max_memory": _MEMORY_LIMIT_BYTES,
            }
        )

    # ---- read-only accessors ------------------------------------------

    @property
    def frozen_mission_ns(self) -> MappingProxyType[str, Any]:
        """The iteration's frozen ``mission`` namespace snapshot."""
        return self._frozen_mission_ns

    @property
    def allowlist(self) -> list[str]:
        """Defensive copy of the per-session tool allowlist."""
        return list(self._allowlist)

    # ---- public surface -----------------------------------------------

    async def run(
        self,
        script: str,
        ctx: Any | None,
        tool_dispatcher: Callable[[str, dict[str, Any], Any], Awaitable[Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Validate, execute, and observe a Mission script.

        Returns ``(observation, script_call_log)`` matching the shape
        the engine's ``_execute_script`` expects: the observation is a
        plain dict (engine cast to :class:`Observation` at the call
        site) and the call log is a list of
        :class:`ToolCallRecord`-shaped dicts.

        On any ``MontyError`` from the provider — duration cap, memory
        cap, runtime / typing / syntax error inside the script — the
        method re-raises as :class:`SandboxTerminated` carrying the
        closure-captured partial observations and events. The engine's
        decide-phase pattern-matches on this exception and produces a
        ``terminate`` verdict for the iteration.

        ``ScriptRejected`` from the AST validator propagates upward
        unchanged: the engine's Execute_Phase treats that as a
        ``script_rejected`` failure and never reaches the runtime path
        below.
        """
        # Step 1: AST gate. Propagating ``ScriptRejected`` upward is
        # deliberate — the engine's _execute_phase wraps it as a
        # phase failure with reason ``script_rejected``; doing the
        # rejection here means the runtime path never sees a
        # disallowed source.
        validate_script_ast(script, self._allowlist)

        _, monty_error_cls = _import_provider()

        # Closure-captured collectors. Populated synchronously by the
        # host-side helper closures registered as
        # ``external_functions`` and the per-tool wrappers; observed
        # post-run (or post-termination) to build the Observation.
        # Lists rather than dicts so the order in which the script
        # called ``mission.event`` / ``mission.observe`` is preserved
        # in the final record.
        observe_log: list[dict[str, Any]] = []
        event_log: list[dict[str, Any]] = []
        script_call_log: list[dict[str, Any]] = []

        # Host-side helpers for ``mission.observe`` and
        # ``mission.event``. Routing them through the
        # ``external_functions`` channel — rather than as bound
        # methods on a dataclass shipped via ``inputs`` — is what
        # makes script-side mutations visible to the host:
        # ``MontySandboxProvider`` round-trips ``inputs`` values into
        # the underlying Monty VM by value, so a closure list
        # captured on a method body of an ``inputs`` dataclass would
        # only ever see the VM-side copy. The external-functions
        # channel runs each call back in host Python, so the lists
        # below receive the appends.
        #
        # The signatures match the original ``mission.observe`` /
        # ``mission.event`` script-facing surface: ``observe`` takes
        # ``(key, value)`` positionally, ``event`` takes ``name``
        # positionally plus arbitrary keyword arguments. The AST
        # rewrite below replaces the attribute callee with a bare
        # Name lookup but leaves args / kwargs unchanged, so the
        # call shape that lands on these helpers is exactly what an
        # operator would write at the script surface.
        async def _mission_observe(key: str, value: Any) -> None:
            observe_log.append({"key": key, "value": value})

        async def _mission_event(name: str, **kwargs: Any) -> None:
            event_row: dict[str, Any] = {"event_name": name}
            event_row.update(kwargs)
            event_log.append(event_row)

        # The frozen mission namespace remains pinned on this
        # sandbox instance (``self._frozen_mission_ns``) so a future
        # widening of the script surface can expose it without
        # rebuilding the construction-time snapshot. It does *not*
        # ride through the ``inputs`` channel today: the validator
        # never accepts attribute access on anything other than
        # ``mission`` (and the only two ``mission`` attributes are
        # the ``observe`` / ``event`` helpers handled by the
        # preamble below), so a script has no way to read the
        # snapshot through Monty's runtime. Holding it on the host
        # side is the simpler shape; routing it as a ``Mapping``
        # through ``inputs`` would require Monty to convert the
        # full dataclass + nested dicts to its own value model and
        # pay a per-iteration translation cost for data nothing
        # observes.

        # Build the external_functions mapping. Each tool name maps
        # to an async wrapper; Monty's ``external_functions`` channel
        # auto-wraps sync callables to async, but we register native
        # async functions so the dispatcher's ``await`` chain stays
        # explicit and the wrapper can do its own timing.
        external_functions: dict[str, Callable[..., Any]] = {}
        # Pull the per-iteration identifiers off the frozen namespace
        # snapshot built at construction time so the wrapper records
        # the same ``session_id`` / ``iteration_index`` the rest of
        # the iteration's audit rows carry.
        session_id = self._frozen_mission_ns["session_id"]
        iteration_index = self._frozen_mission_ns["iteration_index"]
        for tool_name in self._allowlist:
            external_functions[tool_name] = _make_tool_wrapper(
                tool_name,
                ctx,
                tool_dispatcher,
                script_call_log,
                session_id,
                iteration_index,
                cost_estimators=self._cost_estimators,
            )

        # The two helper functions ride alongside the per-tool
        # wrappers under reserved underscore-prefixed names. Operator
        # scripts cannot collide with these: the AST validator
        # rejects ``_mission_observe`` and ``_mission_event`` as
        # bare names (neither is on the per-session tool allowlist
        # nor any of the safe-builtin / exception / mission base
        # sets), so a script that wrote ``_mission_observe(...)``
        # directly would fail the gate with ``name_not_allowed``.
        # Only the AST rewrite below — applied *after* the gate —
        # ever produces those Name nodes.
        external_functions["_mission_observe"] = _mission_observe
        external_functions["_mission_event"] = _mission_event

        # The validated operator source is re-parsed and rewritten
        # so every accepted ``mission.<helper>(...)`` Call's callee
        # becomes a bare-Name lookup of the corresponding reserved
        # external-function name. Monty's parser does not accept
        # ``class`` / nested-attribute shims that would otherwise
        # let us preserve the surface attribute call, so the
        # rewrite happens on the AST itself before the source ever
        # reaches the underlying VM. Operator code keeps its
        # author-time surface (``await mission.observe(key, value)``);
        # only the run-time surface differs.
        final_source = _rewrite_mission_helpers(script)

        phase_started_at = datetime.now(UTC).isoformat()

        try:
            await self._provider.run(
                code=final_source,
                inputs={},
                external_functions=external_functions,
            )
        except monty_error_cls as exc:
            # ``MontyError`` is the base of the limit / runtime /
            # typing / syntax error family. Catching the base class
            # rather than the leaves means a future Monty release
            # adding a new error type still routes through
            # ``SandboxTerminated`` rather than escaping as an opaque
            # ``Exception``.
            raise SandboxTerminated(
                type(exc).__name__,
                partial_observations=list(observe_log),
                partial_events=list(event_log),
                partial_script_call_log=list(script_call_log),
            ) from exc

        phase_ended_at = datetime.now(UTC).isoformat()

        # The script's return value is intentionally ignored: the
        # contract documented for the script surface is "use
        # ``mission.observe(...)`` / ``mission.event(...)`` to report
        # data". A script that returned a dict would conflict with
        # the helper-driven observation list, and the engine's
        # observe-phase already accepts a pre-built Observation
        # without consulting any return value.
        observation = _build_script_observation(
            script_call_log=script_call_log,
            observe_log=observe_log,
            event_log=event_log,
            phase_started_at=phase_started_at,
            phase_ended_at=phase_ended_at,
        )
        return observation, list(script_call_log)


# ---------------------------------------------------------------------------
# Default factory
# ---------------------------------------------------------------------------


def make_default_sandbox_runner(
    allowlist: list[str],
    session: Any,
    cost_estimators: dict[str, Callable[[dict[str, Any]], float]] | None = None,
) -> Callable[
    [str, Any, Callable[[str, dict[str, Any], Any], Awaitable[Any]]],
    Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]],
]:
    """Build the default ``sandbox_runner`` callable for the engine.

    The :class:`MissionEngine` takes a callable matching the
    ``SandboxRunner`` protocol (``(script, ctx, tool_dispatcher) ->
    (observation_dict, script_call_log)``); this helper wraps a fresh
    :class:`MissionSandbox` for a given session and returns the bound
    :meth:`MissionSandbox.run` method so the engine can drive the
    sandbox without depending on the sandbox class itself.

    One sandbox per session: the constructor freezes a snapshot of the
    session's directive, criteria, budget, and prior-iteration
    summaries into the ``mission`` namespace, so reusing a runner
    across sessions would leak stale state. The engine's normal
    construction path therefore calls this factory once per
    ``mission_start`` and pins the returned callable on the engine
    instance for the session's lifetime.

    ``cost_estimators`` flows through to the in-script tool wrapper so
    a scripted strategy that calls a cost-incurring tool 1000 times in
    a loop accumulates cost onto ``session["accumulated_cost_usd"]``
    just like the engine's direct ``_dispatch_one_call`` path would.
    Defaults to ``None`` (an empty estimator map) so older callers
    that don't thread cost estimators through stay working unchanged.
    """
    sandbox = MissionSandbox(
        allowlist=allowlist,
        session=session,
        cost_estimators=cost_estimators,
    )
    return sandbox.run


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "MissionSandbox",
    "ScriptRejected",
    "SandboxTerminated",
    "make_default_sandbox_runner",
    "validate_script_ast",
]
