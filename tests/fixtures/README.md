# Test Fixtures

This directory contains deterministic input and response data used by automated
validation without contacting live services.

## Table of Contents

- [Fixture Sets](#fixture-sets)
- [Maintenance Rules](#maintenance-rules)

## Fixture Sets

- [`cdk_json/`](cdk_json/README.md) contains the `cdk.json` files earlier
  releases shipped, which the current configuration validation must still
  accept.
- [`scaffold_responses/`](scaffold_responses/README.md) contains captured model
  responses replayed through the Mission scaffolder pipeline.

## Maintenance Rules

Fixtures must be stable, reviewable, free of credentials and personal data, and
paired with documentation in their nearest directory. Record provenance and
refresh procedures beside captures whose upstream format can evolve.
