# Working on Home Insights

This is a Home Assistant custom integration. It runs inside Core; do not add
a separate daemon, Docker service, Node runtime or HA access token.

Use the personal Git identity and `github.com-personal` SSH host for this repository.
Keep household entity IDs, addresses, rules, private snapshots and credentials out of Git.

Read README.md and docs/architecture.md before changing source semantics.
Consult the connected HA best-practice skill before live HA changes. Use supported
config/registry APIs; never edit HA `.storage` files directly. Native Store helpers
are permitted for integration-owned settings. Journal I/O belongs on its dedicated
executor, never on the HA event loop.

Preserve stable source identities and distinguish first observation, recurrence,
source clearance, channel removal, exclusion, unavailable observations and collector gaps.
Do not turn unknown data, a missing run or an old report into a healthy result.
AI receives explicit facts with no device/configuration/firmware tools.

Luma is the presentation layer. Do not create a permanent second implementation
of Luma's custom rule evaluator. Porting those rules requires contract tests and
an explicit authoritative-backend cutover; v0.1 does not perform that cutover.

Before committing: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run pytest`, and `uv run python scripts/package.py`.
Tests use HA 2026.11.0 directly and never contact the production HA or real devices.
Production installation and a Core restart are separate from a passing local test run.
