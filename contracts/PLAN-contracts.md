# Phase 1 data contracts plan

## Scope

This work owns only the versioned domain contracts, their generated JSON Schema,
synthetic JSON fixtures, and focused contract tests.  It deliberately does not
read a real WeChat directory, decrypt media, or delete anything.

## Deliverables

1. Pydantic v2 models for scan, mapping, decode, cleanup, errors, and
   compatibility policies.
2. Versioned JSON Schema files in `contracts/jsonschema/`.
3. Synthetic, non-personal contract fixtures that exercise the public JSON boundary;
   filesystem fixtures are owned and maintained by the `fixtures` Agent.
4. Tests for validation, schema export, compatibility, and immutable cleanup
   plans.

## Acceptance checks

- `python -m unittest discover -s tests/contract -v` passes with `PYTHONPATH=src`.
- Every public top-level contract has a JSON Schema artifact.
- `CleanupPlan` rejects mutable/unsafe targets and cannot be reassigned after
  construction.
