"""A small CEL interpreter for the ValidatingAdmissionPolicy tests.

No CEL evaluator ships with the repository, so the admission policies' own
expressions (``08-internal-ca-issuance.yaml``, ``09-tenant-write-fence.yaml``)
run through this interpreter in ``tests/test_internal_tls_manifests.py`` and
``tests/test_tenant_write_fence.py``. It knows exactly the CEL those policies
use (string, int, bool and null literals, lists, maps, field and index
selection, ``has()``, ``all()``, the string methods ``endsWith()`` and
``startsWith()``, ``!``, ``&&``, ``||``, ``==``, ``!=``, ``in``, ``+`` and
``?:``), with CEL's semantics where they decide an admission: selecting an
absent field or key is an error, ``&&`` and ``||`` absorb an error when the
other side settles the result, and an error would deny the request
(``failurePolicy: Fail``), so any error fails a test. Anything else is a
parse error, so a construct the interpreter does not know fails the tests
instead of passing unevaluated. The kind jobs run the same policies in a real
API server.
"""

from __future__ import annotations

import re
from typing import Any

CelNode = tuple[Any, ...]


class CelError(Exception):
    """A CEL evaluation error; under failurePolicy Fail it denies the request."""


_CEL_TOKEN = re.compile(
    r"""\s*(?:
        (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
      | (?P<int>\d+)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op>&&|\|\||==|!=|[!?:.,()\[\]{}+])
    )""",
    re.VERBOSE,
)
_CEL_ESCAPES = {"\\": "\\", "'": "'", '"': '"'}
_CEL_LITERALS = {"true": True, "false": False, "null": None}
#: The string methods the policies call, each taking one string argument.
_CEL_STRING_METHODS = {"endsWith": str.endswith, "startsWith": str.startswith}


def _cel_unquote(literal: str) -> str:
    def escape(match: re.Match[str]) -> str:
        if match.group(1) not in _CEL_ESCAPES:
            raise ValueError(f"unsupported CEL escape {match.group(0)!r}")
        return _CEL_ESCAPES[match.group(1)]

    return re.sub(r"\\(.)", escape, literal[1:-1])


class _CelParser:
    """Recursive descent over CEL's precedence levels, loosest first."""

    def __init__(self, source: str) -> None:
        self._tokens: list[tuple[str, str]] = []
        position = 0
        while source[position:].strip():
            match = _CEL_TOKEN.match(source, position)
            if match is None or match.lastgroup is None:
                raise ValueError(f"unsupported CEL at {source[position : position + 30]!r}")
            self._tokens.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
        self._position = 0

    def parse(self) -> CelNode:
        node = self._conditional()
        if self._position != len(self._tokens):
            raise ValueError(f"unexpected CEL token {self._tokens[self._position][1]!r}")
        return node

    def _peek(self) -> str | None:
        return self._tokens[self._position][1] if self._position < len(self._tokens) else None

    def _accept(self, text: str) -> bool:
        if self._peek() != text:
            return False
        self._position += 1
        return True

    def _expect(self, text: str) -> None:
        if not self._accept(text):
            raise ValueError(f"expected {text!r} in CEL, found {self._peek()!r}")

    def _next(self) -> tuple[str, str]:
        if self._position == len(self._tokens):
            raise ValueError("unexpected end of CEL")
        self._position += 1
        return self._tokens[self._position - 1]

    def _identifier(self) -> str:
        kind, text = self._next()
        if kind != "ident":
            raise ValueError(f"expected an identifier in CEL, found {text!r}")
        return text

    def _conditional(self) -> CelNode:
        condition = self._or()
        if not self._accept("?"):
            return condition
        then = self._or()
        self._expect(":")
        return ("?:", condition, then, self._conditional())

    def _or(self) -> CelNode:
        node = self._and()
        while self._accept("||"):
            node = ("||", node, self._and())
        return node

    def _and(self) -> CelNode:
        node = self._relation()
        while self._accept("&&"):
            node = ("&&", node, self._relation())
        return node

    def _relation(self) -> CelNode:
        node = self._addition()
        while (operator := self._peek()) in ("==", "!=", "in"):
            self._position += 1
            node = (operator, node, self._addition())
        return node

    def _addition(self) -> CelNode:
        node = self._unary()
        while self._accept("+"):
            node = ("+", node, self._unary())
        return node

    def _unary(self) -> CelNode:
        if self._accept("!"):
            return ("!", self._unary())
        return self._member()

    def _member(self) -> CelNode:
        node = self._primary()
        while True:
            if self._accept("."):
                field = self._identifier()
                if not self._accept("("):
                    node = ("select", node, field)
                    continue
                if field in _CEL_STRING_METHODS:
                    argument = self._conditional()
                    self._expect(")")
                    node = ("method", field, node, argument)
                    continue
                if field != "all":
                    raise ValueError(f"unsupported CEL macro .{field}()")
                variable = self._identifier()
                self._expect(",")
                predicate = self._conditional()
                self._expect(")")
                node = ("all", node, variable, predicate)
            elif self._accept("["):
                node = ("index", node, self._conditional())
                self._expect("]")
            else:
                return node

    def _primary(self) -> CelNode:
        kind, text = self._next()
        if kind == "string":
            return ("literal", _cel_unquote(text))
        if kind == "int":
            return ("literal", int(text))
        if kind == "ident" and text in _CEL_LITERALS:
            return ("literal", _CEL_LITERALS[text])
        if kind == "ident" and self._accept("("):
            argument = self._conditional()
            self._expect(")")
            if text != "has" or argument[0] != "select":
                raise ValueError(f"unsupported CEL call {text}()")
            return ("has", argument[1], argument[2])
        if kind == "ident":
            return ("ident", text)
        if text == "(":
            node = self._conditional()
            self._expect(")")
            return node
        if text == "[":
            items: list[CelNode] = []
            while not self._accept("]"):
                if items:
                    self._expect(",")
                items.append(self._conditional())
            return ("list", tuple(items))
        if text == "{":
            entries: list[tuple[CelNode, CelNode]] = []
            while not self._accept("}"):
                if entries:
                    self._expect(",")
                key = self._conditional()
                self._expect(":")
                entries.append((key, self._conditional()))
            return ("map", tuple(entries))
        raise ValueError(f"unsupported CEL token {text!r}")


class _CelVariables:
    """The policy's variables, each evaluated on first use and then reused."""

    def __init__(self, definitions: list[dict[str, str]], activation: dict[str, Any]) -> None:
        self._expressions = {
            item["name"]: _CelParser(item["expression"]).parse() for item in definitions
        }
        self._activation = activation
        self._results: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        if name not in self._expressions:
            raise CelError(f"undefined variable {name!r}")
        if name not in self._results:
            try:
                self._results[name] = _cel_eval(self._expressions[name], self._activation)
            except CelError as error:
                self._results[name] = error
        result = self._results[name]
        if isinstance(result, CelError):
            raise result
        return result


def _cel_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise CelError(f"no such overload: expected a bool, got {value!r}")
    return value


def _cel_equal(left: Any, right: Any) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_cel_equal, left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_cel_equal(left[k], right[k]) for k in left)
    # Values of different types are unequal: in CEL a bool is not an int.
    return type(left) is type(right) and left == right


def _cel_eval(node: CelNode, activation: dict[str, Any]) -> Any:
    operator = node[0]
    if operator == "literal":
        return node[1]
    if operator == "ident":
        if node[1] not in activation:
            raise CelError(f"undeclared reference to {node[1]!r}")
        return activation[node[1]]
    if operator in ("select", "has"):
        target = _cel_eval(node[1], activation)
        if operator == "select" and isinstance(target, _CelVariables):
            return target.get(node[2])
        if not isinstance(target, dict):
            raise CelError(f"{operator} {node[2]!r} on {target!r}")
        if operator == "has":
            return node[2] in target
        if node[2] not in target:
            raise CelError(f"no such key: {node[2]}")
        return target[node[2]]
    if operator == "index":
        target, key = _cel_eval(node[1], activation), _cel_eval(node[2], activation)
        if not isinstance(target, dict) or not isinstance(key, str) or key not in target:
            raise CelError(f"no such key: {key!r}")
        return target[key]
    if operator == "list":
        return [_cel_eval(item, activation) for item in node[1]]
    if operator == "map":
        return {_cel_eval(key, activation): _cel_eval(value, activation) for key, value in node[1]}
    if operator == "!":
        return not _cel_bool(_cel_eval(node[1], activation))
    if operator in ("&&", "||"):
        # Commutative: a side that settles the result wins over an error.
        decisive = operator == "||"
        logic_error: CelError | None = None
        for side in node[1:]:
            try:
                if _cel_bool(_cel_eval(side, activation)) is decisive:
                    return decisive
            except CelError as error:
                logic_error = error
        if logic_error is not None:
            raise logic_error
        return not decisive
    if operator == "?:":
        branch = node[2] if _cel_bool(_cel_eval(node[1], activation)) else node[3]
        return _cel_eval(branch, activation)
    if operator == "method":
        target, argument = _cel_eval(node[2], activation), _cel_eval(node[3], activation)
        if not isinstance(target, str) or not isinstance(argument, str):
            raise CelError(f"no such overload: {target!r}.{node[1]}({argument!r})")
        return _CEL_STRING_METHODS[node[1]](target, argument)
    if operator == "all":
        target = _cel_eval(node[1], activation)
        if not isinstance(target, (list, dict)):
            raise CelError(f"all() over {target!r}")
        item_error: CelError | None = None
        for item in target:
            try:
                if not _cel_bool(_cel_eval(node[3], {**activation, node[2]: item})):
                    return False
            except CelError as error:
                item_error = error
        if item_error is not None:
            raise item_error
        return True
    left, right = _cel_eval(node[1], activation), _cel_eval(node[2], activation)
    if operator == "==":
        return _cel_equal(left, right)
    if operator == "!=":
        return not _cel_equal(left, right)
    if operator == "in":
        if not isinstance(right, (list, dict)):
            raise CelError(f"no such overload: in {right!r}")
        return any(_cel_equal(left, item) for item in right)
    if type(left) is type(right) and isinstance(left, (str, list)):
        return left + right
    raise CelError(f"no such overload: {left!r} {operator} {right!r}")


def _variable_references(source: str) -> list[str]:
    """Every ``variables.<name>`` an expression selects."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, tuple):
            if node[:2] == ("select", ("ident", "variables")):
                found.append(node[2])
            for child in node[1:]:
                walk(child)

    walk(_CelParser(source).parse())
    return found
