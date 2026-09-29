"""CEL for the ValidatingAdmissionPolicy tests, compiled and run by cel-expr-python.

The admission policies' own expressions (``08-internal-ca-issuance.yaml``,
``09-tenant-write-fence.yaml``) run here for
``tests/test_internal_tls_manifests.py`` and ``tests/test_tenant_write_fence.py``
through cel-expr-python, the CEL project's Python binding of cel-cpp, pinned in
the ``test`` extra. The environment is CEL's standard library with one
dynamically typed variable per name a test binds (``object``, ``oldObject`` and
``request`` for a policy). The API server runs cel-go with Kubernetes' own
libraries on top. A construct outside the standard library, or a root these
tests do not bind (``params``, ``namespaceObject``, ``authorizer``), does not
compile here, so it fails the tests instead of passing unevaluated. The kind
jobs run the same policies in a real API server.

``variables`` is composed the way the API server composes it. Each variable
compiles against the ones defined before it, declared as the qualified names
``variables.<name>``, so a forward or unknown reference does not compile. Its
value, or its error, is fixed once per request. A variable that errors stays
unbound: selecting it is an error again where it is used, which ``&&``, ``||``
and ``?:`` settle exactly as they would the original, and the error reported
is the variable's own. Every error fails a test, because under
``failurePolicy: Fail`` it would deny the request.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from cel_expr_python import cel

#: The request roots the tests bind for a policy, as the API server does.
ADMISSION_ROOTS = ("object", "oldObject", "request")


class CelError(Exception):
    """A CEL compile or evaluation error; under failurePolicy Fail it denies the request."""


def _declared(roots: Iterable[str], variables: Iterable[str]) -> frozenset[str]:
    return frozenset(roots) | {f"variables.{name}" for name in variables}


@functools.cache
def _environment(names: frozenset[str]) -> cel.Env:
    # Cached, so every compiled expression's environment stays alive with it.
    return cel.NewEnv(variables=dict.fromkeys(names, cel.Type.DYN))


@functools.cache
def _compiled(names: frozenset[str], expression: str) -> cel.Expression:
    try:
        return _environment(names).compile(expression)
    except RuntimeError as error:
        raise CelError(f"does not compile: {error}") from None


class Variables:
    """A policy's ``variables`` for one request, composed as the API server composes them."""

    def __init__(
        self, definitions: Sequence[Mapping[str, str]], activation: Mapping[str, Any]
    ) -> None:
        self.defined: list[str] = []
        self.values: dict[str, Any] = {}
        self.errors: dict[str, CelError] = {}
        roots = {name: value for name, value in activation.items() if name != "variables"}
        for definition in definitions:
            name = definition["name"]
            try:
                self.values[name] = _evaluate(definition["expression"], roots, self)
            except CelError as error:
                self.errors[name] = error
            self.defined.append(name)

    def get(self, name: str) -> Any:
        """The variable's value; its error, raised again, when it has one."""
        if name in self.errors:
            raise self.errors[name]
        if name not in self.values:
            raise CelError(f"undefined variable {name!r}")
        return self.values[name]


def evaluate(expression: str, activation: Mapping[str, Any]) -> Any:
    """Compile and run one expression; its value as plain Python.

    ``activation`` binds each root by name, and a :class:`Variables` under
    ``variables`` binds the policy's variables.

    Raises:
        CelError: when the expression does not compile or its value is an error.
    """
    variables = activation.get("variables")
    roots = {name: value for name, value in activation.items() if name != "variables"}
    return _evaluate(expression, roots, variables)


def evaluate_bool(expression: str, activation: Mapping[str, Any]) -> bool:
    """:func:`evaluate` for a validation, whose value must be a bool."""
    value = evaluate(expression, activation)
    if not isinstance(value, bool):
        raise CelError(f"expected a bool, got {value!r}")
    return value


def check(expression: str, variables: Iterable[str] = ()) -> None:
    """Compile an admission expression against the request roots and the named variables.

    Raises:
        CelError: when it does not compile, a reference to any other variable included.
    """
    _compiled(_declared(ADMISSION_ROOTS, variables), expression)


def _evaluate(expression: str, roots: Mapping[str, Any], variables: Variables | None) -> Any:
    data = dict(roots)
    defined: Sequence[str] = ()
    if variables is not None:
        defined = variables.defined
        data.update({f"variables.{name}": value for name, value in variables.values.items()})
    result = _compiled(_declared(roots, defined), expression).eval(data=data)
    if result.type() == cel.Type.ERROR:
        raise CelError(_cause(str(result.plain_value()), variables))
    return result.plain_value()


def _cause(message: str, variables: Variables | None) -> str:
    """The variable's own error, for the lookup error an unbound variable leaves."""
    if variables is not None:
        for name, error in variables.errors.items():
            if f'"variables.{name}"' in message:
                return f"variables.{name}: {error}"
    return message
