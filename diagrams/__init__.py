"""Diagram generators (``python diagrams/generate.py``).

This marker makes ``diagrams`` a regular package so ``diagrams.generate``,
``diagrams.code_diagrams.generate`` and ``diagrams.infra_diagrams.generate``
are distinct module names for mypy; the generators already import each other
by these fully-qualified names. It is not shipped in the ``gco-cli`` wheel
(see ``[tool.setuptools.packages.find]`` in pyproject.toml).
"""
