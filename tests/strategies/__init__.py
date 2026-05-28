"""Shared Hypothesis strategies for Mission property-based tests.

Generators in this package build well-formed Mission domain objects
(``SessionState``, ``IterationRecord``, ``Observation``, …) so test
modules can compose them without restating the strategy plumbing in
every file. The strategies favour minimal-but-realistic shapes — every
required key is populated, but optional and rarely-tested fields are
left out unless a test composes them in.
"""
