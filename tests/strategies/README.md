# Property-Test Strategies

This package provides reusable Hypothesis strategies for structured GCO domain
objects.

## Table of Contents

- [Purpose](#purpose)
- [Available Strategies](#available-strategies)
- [Design Constraints](#design-constraints)
- [Source Files](#source-files)

## Purpose

Shared strategies keep generated test objects internally consistent while
allowing properties to explore combinations that hand-authored examples often
miss.

## Available Strategies

[`mission.py`](mission.py) exports constrained generators for Mission cadence,
budgets, criteria, observations, phase and iteration records, session state,
and complete `decide_verdict` inputs.

## Design Constraints

Generated values remain JSON-serializable, timestamps stay timezone-aware and
inside a bounded range, criterion results align with criterion IDs, and session
records preserve the invariants expected by the Mission verdict cascade.

## Source Files

- [`__init__.py`](__init__.py) marks the strategy package.
- [`mission.py`](mission.py) defines and exports Mission strategies.
