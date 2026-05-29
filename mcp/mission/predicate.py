"""Restricted AST evaluator for ``Criterion(kind="predicate")`` expressions.

A Mission criterion of kind ``predicate`` carries a small Python expression
that runs against an ``Observation`` dict. Operator-supplied source must be
treated as untrusted: the same JSON that carries it travels across MCP, the
CLI, and disk. We parse the expression once at session start (so the
operator sees errors immediately, not on iteration N), validate it against
a tight allowlist, and cache the AST on the criterion so every later
evaluation reuses it without reparsing.

The sandbox has two layers:

1. **Parse-time validation.** :func:`parse_predicate` parses the source in
   ``eval`` mode and walks the tree with :class:`_PredicateValidator`. The
   first disallowed construct raises :class:`PredicateRejected` and the
   evaluator is never reached.
2. **Eval-time isolation.** :func:`evaluate_predicate` compiles the
   already-validated AST and calls :func:`eval` with an empty
   ``__builtins__`` plus an explicit safe-callable namespace. With
   ``__builtins__`` cleared, even a tree that smuggled past the validator
   could not look up ``__import__``, ``open``, ``compile``, etc.

Allowed surface
---------------
Names: ``obs`` (the dict argument), and the read-only callables ``len``,
``min``, ``max``, ``sum``, ``abs``, ``any``, ``all``, ``sorted``, plus
the four type coercions ``str``, ``int``, ``float``, ``bool``.

Operators: arithmetic (``+ - * / // % ** @``), unary (``+ - not ~``),
comparisons (``< <= > >= == != is is_not in not_in``), boolean
(``and or``), and the ternary ``a if b else c``.

Containers and collections: ``List``, ``Tuple``, ``Dict``, ``Set``, plus
``ListComp``, ``SetComp``, ``DictComp``, ``GeneratorExp`` (their iteration
targets must not shadow a name from the allowlist).

Calls: bare-name calls to one of the twelve stdlib callables above, OR
read-only method calls — ``.get(key[, default])``, ``.keys()``,
``.values()``, ``.items()``, ``.lower()``, ``.upper()``, ``.strip()``.
Method calls accept any receiver the predicate could otherwise
produce: ``Name`` (``obs``), ``Subscript``
(``obs['tool_results']``), or comprehension-bound names
(``r.get('_status')`` inside ``for r in obs['tool_results']``). Method
calls outside the allowlist (``.update``, ``.pop``, ``.count``,
``.append``, ``.startswith``, ...) are rejected with
``call_target_method_not_allowed``.

Attribute access: only ``obs.<attr>`` (one level), and the attribute
name itself must not start with ``__``. Anything more elaborate
(chained attribute walks, attributes on calls or subscripts) is
rejected — predicates that need nested data should use subscripting.

Subscripts: any ``value[...]`` chain whose ultimate base is an allowlisted
name. Rejection happens automatically because every nested ``Name`` lookup
is validated.

f-strings: ``JoinedStr`` and ``FormattedValue`` recurse normally so any
embedded name lookup re-enters this same allowlist check.

Rejected outright
-----------------
``Import`` / ``ImportFrom`` (also unreachable in ``eval`` mode), ``Lambda``
(it would let a predicate ship hidden code), the walrus ``NamedExpr``,
``Yield`` / ``YieldFrom`` / ``Await`` and other async constructs, any
identifier or string constant that starts with ``__``, and every
``Name``/``Attribute``/``Call`` whose target is not on the allowlist.
"""

from __future__ import annotations

import ast
from typing import Any, Final, NoReturn

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

_ALLOWED_CALLABLES: Final[frozenset[str]] = frozenset(
    {"len", "min", "max", "sum", "abs", "any", "all", "sorted", "str", "int", "float", "bool"}
)
"""Builtin callables a predicate may invoke. Pure, side-effect-free.

The eight stdlib aggregate / comparison helpers (``len``, ``min``,
``max``, ``sum``, ``abs``, ``any``, ``all``, ``sorted``) plus four
type coercions (``str``, ``int``, ``float``, ``bool``). The
coercions are useful for normalising values before comparison —
``str(r.get('count')) == '0'`` and ``bool(obs['errors'])`` are
common idioms — and none of them can escape the eval-time sandbox
(empty ``__builtins__``, no ``__import__`` / ``open`` / ``getattr``
in scope) regardless of input.
"""

_ALLOWED_METHOD_CALLS: Final[frozenset[str]] = frozenset(
    {"get", "keys", "values", "items", "lower", "upper", "strip"}
)
"""Read-only methods a predicate may invoke on any value.

Models trained on Python idioms gravitate toward ``r.get('_status')``,
``r.items()``, and ``str(...).lower()`` for case-insensitive substring
search. The seven methods listed here are all pure read-only
accessors / transformations:

* ``dict.get(key[, default])`` returns the value at ``key`` (or
  ``default``); identical to subscript except it tolerates missing
  keys without raising.
* ``dict.keys()`` / ``dict.values()`` / ``dict.items()`` return views
  that the comprehension protocol then iterates.
* ``str.lower()`` / ``str.upper()`` return a new string with
  case-folded contents; common in case-insensitive substring
  search like ``'foo' in str(x).lower()``.
* ``str.strip()`` returns a new string with leading and trailing
  whitespace removed; common in normalising values before
  comparison.

None of the seven can mutate state, escape ``__builtins__``, or reach a
callable that we did not already opt into through the eval-time
sandbox (``__builtins__`` is empty; ``eval`` / ``compile`` /
``__import__`` / ``getattr`` / ``setattr`` / ``open`` are all
unreachable). Allowing them lets the model write the natural
expression ``any('inference' in str(r).lower() for r in obs['tool_results'])``
instead of being forced into the more verbose subscript-only equivalent
that the model rarely produces unprompted.

Method-call gating still applies in two places:

1. The attribute *name* must be in this set. ``r.update(...)``,
   ``r.pop(...)``, ``r.setdefault(...)``, etc. raise
   ``call_target_method_not_allowed`` even though they would otherwise
   parse as ``Attribute -> Call``.
2. Method calls are only permitted on values produced by the
   predicate's allowed surface — ``Name``, ``Subscript``, comprehension
   targets. A method call on a literal expression (``[1, 2].count(1)``)
   parses but the call goes through ``visit_Call`` → still rejected
   because the receiver is not on the data namespace. See
   ``visit_Call`` for the full set of acceptable receivers.
"""

_ALLOWED_DATA_NAMES: Final[frozenset[str]] = frozenset({"obs"})
"""Top-level data names the predicate may read."""

_ALLOWED_NAMES: Final[frozenset[str]] = _ALLOWED_DATA_NAMES | _ALLOWED_CALLABLES
"""Every globally-allowed identifier the predicate may reference."""

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


class PredicateRejected(Exception):
    """Raised when a predicate source contains a disallowed construct.

    The :attr:`reason` field is a short stable token (e.g.
    ``"forbidden_call"``) so callers can render structured errors. The
    :attr:`failing_node` field is the ``ast`` node that triggered the
    rejection; it is ``None`` only when the source failed to parse at all.
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


class _PredicateValidator(ast.NodeVisitor):
    """Walk a predicate AST and reject any construct outside the allowlist.

    The validator tracks per-scope local names introduced by comprehensions
    so a tight expression like ``all(x > 0 for x in obs["xs"])`` works
    while the comprehension target ``x`` cannot shadow ``obs`` or any of
    the allowed callables.
    """

    def __init__(self) -> None:
        # Stack of frozensets of locally-bound names. The base scope is
        # empty; comprehensions push a frame containing their targets.
        self._scopes: list[frozenset[str]] = [frozenset()]

    # ---- helpers -------------------------------------------------------

    def _current_locals(self) -> frozenset[str]:
        return self._scopes[-1]

    def _name_is_visible(self, name: str) -> bool:
        return name in _ALLOWED_NAMES or name in self._current_locals()

    @staticmethod
    def _is_dunder(name: str) -> bool:
        return name.startswith("__")

    @staticmethod
    def _reject(reason: str, node: ast.AST, message: str | None = None) -> NoReturn:
        raise PredicateRejected(reason, failing_node=node, message=message)

    def _push_scope(self, locals_: frozenset[str]) -> None:
        self._scopes.append(self._current_locals() | locals_)

    def _pop_scope(self) -> None:
        self._scopes.pop()

    def _collect_target_names(self, target: ast.AST) -> list[ast.Name]:
        """Flatten a comprehension/assignment target into Name nodes.

        Tuples and lists nest (``for (a, b) in pairs``); Starred wraps
        (``for *xs, last in rows``). Anything else under a target is a
        validation error reported by the caller.
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
        # Anything else (Subscript, Attribute, ...) as a target is invalid.
        self._reject(
            "invalid_comprehension_target",
            target,
            "comprehension target must be a plain identifier",
        )
        return []  # unreachable; _reject raises

    # ---- top-level entry ----------------------------------------------

    def visit_Expression(self, node: ast.Expression) -> None:
        # ast.parse(..., mode="eval") guarantees a single Expression root;
        # walk its body.
        self.visit(node.body)

    # ---- catch-all -----------------------------------------------------

    def generic_visit(self, node: ast.AST) -> None:
        # Default rejection: every node type we accept has a dedicated
        # ``visit_*`` method below. If we reach generic_visit it means the
        # source contained something we did not explicitly opt into
        # (Lambda, NamedExpr, Yield, async constructs, FunctionDef, etc.).
        self._reject(
            "forbidden_node",
            node,
            f"{type(node).__name__} is not allowed in a predicate",
        )

    # ---- leaves --------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        # Reject dunder strings even when used as plain data. We never
        # need them in a numeric/boolean/string literal, and forbidding
        # them closes off the most common escape patterns
        # (``getattr(x, "__class__")``, ``obs["__import__"]``, etc.) even
        # if a future change accidentally widens the allowlist.
        if isinstance(node.value, str) and self._is_dunder(node.value):
            self._reject(
                "dunder_string",
                node,
                "string constants starting with '__' are not allowed",
            )
        # Other constants (int, float, bool, None, bytes, complex, str)
        # are inert.

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
                f"name '{node.id}' is not in the predicate allowlist",
            )

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
                # ``{**other}`` unpacking would let an attacker splat
                # arbitrary mappings; reject to keep the surface tight.
                self._reject(
                    "dict_unpacking",
                    node,
                    "dict unpacking is not allowed in a predicate",
                )
        for value in node.values:
            self.visit(value)

    def visit_Starred(self, node: ast.Starred) -> None:
        # ``[*xs]`` / ``f(*xs)`` — recurse into the inner expression so
        # the nested Name still hits the allowlist check.
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
        # Three shapes are allowed:
        #
        # 1. ``obs.<attr>`` — single-level read off the data dict.
        # 2. ``<inner>.<method>`` *only when* visited from
        #    ``visit_Call`` and ``<method>`` is in
        #    ``_ALLOWED_METHOD_CALLS``. ``visit_Call`` handles that
        #    case by validating the inner expression itself rather
        #    than recursing into ``visit_Attribute``, so by the time a
        #    bare ``Attribute`` lands here we know it is *not* the
        #    receiver of an allowed method call.
        # 3. Nothing else: chained walks (``obs.a.b``), attributes on
        #    calls, and attributes on subscripts are all rejected.
        if self._is_dunder(node.attr):
            self._reject(
                "dunder_attribute",
                node,
                f"attribute '{node.attr}' starts with '__'",
            )
        if not (isinstance(node.value, ast.Name) and node.value.id in _ALLOWED_DATA_NAMES):
            self._reject(
                "attribute_target_not_allowed",
                node,
                "attribute access is only allowed on 'obs' "
                "(or as a read-only method call on a dict/list)",
            )
        # The base Name is in _ALLOWED_DATA_NAMES, so we know it passes
        # the visit_Name check; visit it anyway to stay regular.
        self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # No special restriction beyond "the base Name must be on the
        # allowlist", which falls out of recursing into ``node.value``.
        # ``node.slice`` may itself contain Names and Calls; recurse so
        # they hit the same allowlist gate.
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
        # Two callable shapes are allowed:
        #
        # 1. Bare-name calls to one of ``_ALLOWED_CALLABLES`` —
        #    ``len(x)``, ``any(...)``, ``sorted(xs)``. The validator
        #    enforces the name appears on the allowlist.
        # 2. Method calls of the form ``<expr>.<method>(...)`` where
        #    ``<method>`` is in ``_ALLOWED_METHOD_CALLS`` (the four
        #    pure dict/list read accessors). The receiver expression
        #    is validated through the normal visit chain so a method
        #    call on something the predicate cannot otherwise see
        #    (e.g. ``getattr(x, 'y').get(...)``) is rejected at the
        #    receiver-validation step before the method allowlist is
        #    even consulted.
        #
        # Anything else — subscript-then-call (``builtins["eval"]()``),
        # call-then-call (``factory()()``), method calls to non-
        # allowlisted attribute names — is rejected.
        if isinstance(node.func, ast.Attribute):
            if self._is_dunder(node.func.attr):
                self._reject(
                    "dunder_attribute",
                    node.func,
                    f"attribute '{node.func.attr}' starts with '__'",
                )
            if node.func.attr not in _ALLOWED_METHOD_CALLS:
                self._reject(
                    "call_target_method_not_allowed",
                    node,
                    f"method '.{node.func.attr}()' is not allowed; "
                    f"the read-only method allowlist is "
                    f"{sorted(_ALLOWED_METHOD_CALLS)}",
                )
            # Validate the receiver itself. Recursing here (rather
            # than into ``visit_Attribute``) bypasses the
            # ``visit_Attribute`` rule that only ``obs.<attr>``
            # is allowed — but only because the *method name* is on
            # the explicit pure-accessor allowlist above. Any other
            # attribute name still falls through ``visit_Attribute``'s
            # tighter rules.
            self.visit(node.func.value)
        elif isinstance(node.func, ast.Name):
            if node.func.id not in _ALLOWED_CALLABLES:
                self._reject(
                    "call_target_not_allowed",
                    node,
                    f"call to '{node.func.id}' is not allowed",
                )
        else:
            # Subscript-then-call, call-then-call, etc. — reject.
            self._reject(
                "call_target_not_name",
                node,
                "predicate calls must target a bare callable name or a read-only dict/list method",
            )
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            # ``**kwargs`` shows up as a keyword with arg=None; allow the
            # value but recurse so its content is still validated.
            self.visit(kw.value)

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

        Each generator's ``iter`` is validated against the *outer* scope
        (it cannot reference the targets of its own generator), then the
        targets are added to the local set so the next generator's
        ``ifs`` and any later ``iter`` can see them.
        """
        accumulated: set[str] = set()
        for gen in generators:
            if gen.is_async:
                self._reject(
                    "async_comprehension",
                    gen.iter,
                    "async comprehensions are not allowed",
                )
            # Validate the iterable in the scope visible *before* this
            # generator's targets are bound.
            self.visit(gen.iter)
            target_names = self._collect_target_names(gen.target)
            for name_node in target_names:
                if self._is_dunder(name_node.id):
                    self._reject(
                        "dunder_comprehension_target",
                        name_node,
                        f"comprehension target '{name_node.id}' starts with '__'",
                    )
                if name_node.id in _ALLOWED_NAMES:
                    self._reject(
                        "comprehension_target_shadows_allowlist",
                        name_node,
                        f"comprehension target '{name_node.id}' shadows an allowlisted name",
                    )
                accumulated.add(name_node.id)
            # Subsequent ``ifs`` and any later generator may reference
            # these targets; push them now.
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


def parse_predicate(src: str) -> ast.Expression:
    """Parse and validate a predicate source string.

    Returns the parsed :class:`ast.Expression` so callers can cache it and
    feed it to :func:`evaluate_predicate` without reparsing. Raises
    :class:`PredicateRejected` if the source fails to parse or contains
    any disallowed construct.
    """
    if not isinstance(src, str):
        raise PredicateRejected(
            "not_a_string",
            message="predicate source must be a str",
        )
    try:
        parsed = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        rejection = PredicateRejected(
            "syntax_error",
            message=f"could not parse predicate: {exc.msg}",
        )
        rejection.lineno = exc.lineno
        rejection.col_offset = exc.offset
        raise rejection from exc
    _PredicateValidator().visit(parsed)
    return parsed


# Pre-built sandbox namespace. The double-empty ``__builtins__`` plus an
# explicit safe-callable namespace is the established sandbox pattern: it
# blocks lookup of every dangerous builtin (``__import__``, ``open``,
# ``eval``, ``compile``, ``exec``, ``getattr``, ...) even if the validator
# were ever bypassed by a future AST node we forgot about.
_SAFE_GLOBALS: Final[dict[str, Any]] = {"__builtins__": {}}
_SAFE_CALLABLES: Final[dict[str, Any]] = {
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "any": any,
    "all": all,
    "sorted": sorted,
    # Type coercions — pure, side-effect-free transforms used in
    # idioms like ``str(r.get('count')) == '0'``. None of them can
    # escape the empty-``__builtins__`` namespace regardless of input.
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
}


def evaluate_predicate(parsed: ast.Expression, obs: dict[str, Any]) -> Any:
    """Evaluate an already-validated predicate AST against ``obs``.

    The caller is responsible for passing only an :class:`ast.Expression`
    that came from :func:`parse_predicate`; the function does not
    re-validate. Compilation is per-call to keep the function pure (the
    AST itself is the cached unit of work). Returns whatever the
    expression evaluates to — typically a ``bool``, but the criterion
    layer handles other values.
    """
    code = compile(parsed, "<predicate>", "eval")
    # Names referenced from inside a comprehension or generator
    # expression resolve through the enclosing function's *globals*
    # at runtime, not the ``locals`` mapping passed to ``eval`` —
    # because each comprehension compiles to its own implicit
    # function scope. So validated free names (``obs`` plus the
    # safe callables) must live in the globals dict to remain
    # visible from inside ``any(str(r) ... for r in obs[...])``
    # idioms; an earlier "locals-only" arrangement raised
    # ``NameError: name 'str' is not defined`` at runtime even
    # though parse_predicate had accepted the source. The empty
    # ``__builtins__`` still keeps the sandbox tight: every name
    # the body can reach is one we put in the globals dict
    # ourselves.
    eval_globals: dict[str, Any] = {**_SAFE_GLOBALS, "obs": obs, **_SAFE_CALLABLES}
    return eval(  # nosemgrep: python.lang.security.audit.eval-detected.eval-detected
        code, eval_globals, {}
    )  # noqa: S307
