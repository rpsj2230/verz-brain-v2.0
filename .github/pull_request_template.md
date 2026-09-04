# What this closes

<!-- Task ids from the tracker, one per line, e.g. M0.2.4. The status page is generated
     from these, so a task with no id here will not show as done. -->

- M

## What changed

<!-- One paragraph. What a reviewer needs to know that the diff does not say. -->

## Evidence

- [ ] `uv run pytest tests/invariants` green
- [ ] `uv run ruff check src tests` clean
- [ ] `uv run mypy` clean
- [ ] New behaviour has a test; the test fails without the change

## Permission impact

<!-- Delete this block only if the change cannot affect what an answer contains. -->

- [ ] No new capability string, or the new one is in the registry with a validator test
- [ ] No scope composed anywhere except through `Scope.intersect`
- [ ] Nothing widens an entitlement; anything that could has a canary asserting a refusal
- [ ] No `Denied` reaches a person without passing through `to_public`

## Anything a reviewer should push back on

<!-- Assumptions made, shortcuts taken, things deferred. Say them here rather than
     letting them be discovered later. -->
