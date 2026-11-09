# Architecture and migration

## Target ownership

Home Insights owns backend observations, persistent source/incident episodes,
coverage, approved household context, deterministic measurements and report data.
Luma owns presentation and the associated metadata workflows. Native HA automations
own the morning time trigger. The configured AI Task receives measured facts and
approved context to write short Hungarian interpretations, without any control tools.

The morning report is a retrospective of the previous local calendar day. The main
hero continues to handle immediate attention. Reports belong on the relevant
Energy/Heating/Homelab pages, with expandable operational detail. No new main-page
daily report is part of this project.

## v0.1 boundary

The implemented integration records native notifications, Repairs and integration
setup errors. It optionally records selected state facts, without interpreting them
as custom threshold alerts. It exposes health, paginated history and completed-day
facts. Native channel/source identity is stable; each observed episode has a UUID.
Source timestamps and observation timestamps are distinct. Complete source snapshots
and failures are distinct inputs. Missing evidence never implies a resolution.

It does not yet import or reinterpret Luma's existing runtime policy. Those custom
rules remain on their existing path until contract tests establish parity and the
frontend can explicitly consume backend results. There is no automatic cutover and
no claim that the v0.1 open-source count equals the current Luma hero count.

The discarded external Node/Docker observer is not a runtime dependency or release
artifact. Tests and data semantics informed the native design; the external runtime
must not be installed alongside it.

## Journal lifecycle

1. Open the entry-owned SQLite journal on its dedicated worker thread.
2. Wait for HA to finish startup before establishing live source baselines.
3. Capture immutable source snapshots at event callbacks; persist them in order.
4. Reconcile sources and checkpoint observation coverage every 15 seconds.
5. On queue overflow or storage failure, record a gap and re-establish baselines;
   do not infer any transitions inside the gap.
6. On unload/stop, remove callbacks, drain work, checkpoint/close the database and
   shut down the executor. HA clears entry runtime data after unloading.

SQLite transactions group source observation, episode and event changes. WAL and
`synchronous=FULL` protect committed writes. Coverage is conservative to the last
checkpoint after a crash; crash/power-loss durability is not claimed as tested on
the production host. A restart never creates a second copy of an already-open
episode. An actual observed clearance followed by recurrence creates a new episode.

`first_observed` means the source existed when observation began/resumed.
`source_appeared` means it appeared after a successful baseline. Neither is guaranteed
to be the exact underlying physical fault start. `notification_removed` is channel
removal; `repair_no_longer_reported` is disappearance from the visible registry;
`source_cleared` is an explicit clear state. Superseded content and excluded sources
have separate events. Unknown integration states keep an existing fault episode open.
No delivery receipt, seen flag, user-claimed fix or approved normality is fabricated.

## Next stages

1. Validate this foundation in HA with a manual journal read, reload and controlled
   metadata-only tests. Observe a full local calendar day before evaluating daily coverage.
2. Migrate the Luma policy using synthetic contract fixtures and a local private
   live-data comparison. Preserve explicit plant task/channel correlation, selector
   semantics, grace periods, metadata readiness, exceptions and stable identities.
   Move authority to the Python backend; do not maintain two permanent evaluators.
3. Adopt the shared backend in Luma list/details/history. Design seen, snooze,
   user-claimed fixed, observed clearance and approved household rules as distinct
   metadata operations with auditable actor/time/revision information.
4. Migrate existing household knowledge through a validated explicit import. Retain
   drafts, approval, source/guard evidence, rule history and safety protections.
5. Wire the native morning trigger to prior-day measured facts and an AI Task with
   no tools. Persist result and attempt time separately so an old success cannot
   mask a missing or failed run. Keep unknown data distinct from a clean result.
6. Extend topic-specific measurements to Energy, Heating and NAS once the first
   report's collection, persistence and presentation work end to end.

## Engineering sources

- [HA config entry lifecycle](https://contributors.home-assistant.io/core/integrations/config-entries/)
- [HA event subscriptions](https://contributors.home-assistant.io/core/integrations/events/)
- [Keep blocking I/O out of HA's event loop](https://contributors.home-assistant.io/core/internals/blocking-operations/)
- Pinned HA 2026.11.0 source is authoritative for the native adapter compatibility tests.
