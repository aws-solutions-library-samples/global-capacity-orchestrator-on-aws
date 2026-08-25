"""Single source of truth for GCO version.

The authoritative value lives in the top-level ``VERSION`` file so that
non-Python tooling (shell scripts, Dockerfiles, docs, release workflows)
can read it without importing Python. ``__version__`` below mirrors that
file; ``scripts/bump_version.py`` keeps the two in sync.
"""

__version__ = "6.5.2"
