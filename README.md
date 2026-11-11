# Home Insights

Persistent incident-source tracking and daily facts inside Home Assistant.
The project is building toward household context, AI-assisted morning summaries
and Luma dashboard integration.

**v0.2 adds the authoritative Luma attention policy evaluator.** Import a policy
explicitly, then bind a compatible Luma frontend to its ID. The integration observes
its source entities without a browser and journals stable policy episodes separately
from native source evidence. Installing alone changes no dashboard or automation.
It does not automatically configure an AI provider or replace a daily-report automation.

## Morning reports (v0.4)

The `home_insights.generate_report` action takes `policy_id`, `ai_task_entity`,
optional completed local `date` (yesterday by default), and `force` (default false).
Call it from a native HA time-triggered automation or its existing script. Successful
results for the same policy/day are reused unless explicitly forced. Overlapping
runs are refused. No scheduler or device/configuration action is added inside the integration.

The integration persists the attempt before collecting canonical facts. It stores
the fact bundle separately from the AI narrative and keeps failed/missing/interrupted
attempts distinct from the last successful result. Attempts survive reload/restart;
an interrupted run becomes failed, not successful. AI calls time out after two minutes.
When the observer did not run, the report records that fact without asking AI to guess.

The existing native OpenAI **AI Task** is the currently verified provider. Each call
checks the loaded entity type and rejects provider web-search, code-interpreter or
HA API tool options. The native AI Task receives `llm_api=None`, no attachments and
a bounded fact bundle. No MCP tools, device controls, shell, configuration or firmware
capability is passed to it. Other providers require an explicit capability review.
No new OpenAI credential or direct OpenAI SDK client is created.

AI returns a short Hungarian summary, optional hypothesis/check and evidence references.
Unknown references and numeric claims are rejected; measured counts/coverage are
rendered separately. Partial coverage receives a deterministic prefix; summaries
must attribute claims to observations, reference at most two relevant sources and
use informal Hungarian. This first alert-log scope has no validated causal measurements,
so the hypothesis stays empty. The suggested check must be selected from known
source-bound checks; it cannot invent a sensor, service or arbitrary action. Successful
reports expose the selected supporting observations while retaining full measured
facts separately. URLs are removed from the bounded input projection. Source
messages and household notes are data, not executable instructions or automatic exceptions.

`home_insights/report` is an administrator-only read endpoint: `policy_id`, optional
`due_time` (`08:00` by default) and `grace_minutes` (default 10). Its state distinguishes
waiting, running, missed, stalled, failed, no observation, insufficient data, findings
and no anomaly. The expected period and schedule use HA's timezone. A prior successful
result cannot mask a missing current run. The schedule supplied by the dashboard must
match the native automation. Luma 0.50 provides `custom:luma-daily-report-card`.

Report facts and attempts stay in the integration's private SQLite journal and are
not placed in giant Recorder sensor attributes or MQTT messages. The existing raw
facts endpoint remains available; installing alone does not rewrite an automation.

## Energy morning reports (v0.5)

Energy reports are an independent scope; they never combine energy measurements
with attention episode counts. An administrator explicitly imports a profile with
`home_insights.set_energy_profile` (`profile_id`, `profile`, optional `expected_hash`).
Existing profiles require their current hash from the administrator-only
`home_insights/energy_profile` WebSocket command. The profile uses native HA Store;
household mappings belong in runtime configuration, not the integration source.

Profile v1 fields:

| Field | Meaning |
| --- | --- |
| `version`, `revision` | `1` and an explicit revision string |
| `metrics` | Four distinct cumulative sensor statistic IDs under `solar`, `grid_import`, `grid_export`, `ev` |
| `baseline_days` | Prior completed local days to examine, 7–28; default 14 |
| `minimum_baseline_days` | Required complete statistical days, at least 7; default 7 |
| `deviation_percent`, `deviation_kwh` | Relative and absolute differences that must **both** be met; defaults 50% and 3 kWh |

`home_insights.get_energy_facts` reads a profile and optional completed local `date`
without AI or journal writes. `home_insights.generate_energy_report` takes
`profile_id`, `ai_task_entity`, optional `date` and `force`. It uses the same bounded,
tool-free native AI Task and persisted attempt lifecycle as attention reports.
Native HA automations own the time trigger. Luma 0.51's existing daily report card
accepts `energy_profile` **instead of** `policy`; `home_insights/report` accepts the
same mutually exclusive scope selector. Attention reports retain their original API.

Totals use Recorder's reset-adjusted cumulative `sum` at local day boundaries,
converted by Core to kWh. Every UTC hour plus the preceding baseline is required;
negative changes, missing/duplicate buckets, unsupported units and unknown raw
history boundary states cannot establish a complete day. Zero is displayed only
for a verified total. DST days use 23 or 25 hours; zones with non-hour-aligned local
midnights are explicitly unsupported by this hourly implementation. Queries run on
Recorder's executor via native read APIs, never direct SQL or the event loop. The
profile snapshot/hash, values, coverage and comparison samples are persisted with
the attempt before interpretation. Historical data may be used without waiting for
Home Insights itself to observe a full day.

Recovered internal counter outages are disclosed separately: missing device
telemetry is not automatically lost energy when both day boundaries and all hourly
statistics remain known. Statistical completeness does **not** certify device uptime,
hardware accuracy or a counter's ability to accumulate while disconnected. A source
that masks missing values needs its own quality audit before selection.

The prior-day median excludes the examined day and incomplete statistical days.
Lower solar yield and higher import are observations when both configured thresholds
are met, not physical fault diagnoses. EV charging/export changes provide context;
zero charging is not an incident. Missing comparison history is insufficient data,
even if current totals are known. Comparisons are not weather/occupancy normalized;
no forecast discrepancy, tariff cost, EV solar share or total home consumption is
inferred. AI prioritizes constrained combinations of short, HA-verified statements. The
native output schema enumerates only those valid summaries; citations and a known
measurement-bound check are derived from the selected statements. This prevents
free prose from changing a median to a mean, inventing a cause, or citing unrelated
measurements. NAS measurements use the independent scope below.

## Heating morning reports (v0.7)

`home_insights.set_heating_profile` binds four distinct `binary_sensor` sources
under `sources.flame`, `heating`, `dhw` and `connectivity`. A v1 profile requires
`revision`; replacement requires its current hash from the administrator-only
`home_insights/heating_profile` WebSocket command. Profiles live in native HA Store.
No thermostat, boiler setting or device is changed.

`home_insights.get_heating_facts` reads `profile_id` and an optional completed local
`date`. `home_insights.generate_heating_report` also takes `ai_task_entity` and
optional `force`. Native HA owns the time trigger. Luma 0.53's existing report card
accepts `heating_profile` instead of `policy` or `energy_profile`. Separate `heating:`
keys isolate attempt/cache/success. An earlier successful report is reused unless
forced; force after changing a profile if the same day's interpretation must change.

Flame feedback, connected bus and trusted Recorder intervals establish observation.
Room demand is separate. Native history reads and one fixed read-only RecorderRuns
ORM query run on Recorder's executor. Gaps and run boundaries break continuity;
unknown recovery is not an ignition. Only complete burns ending during the local day
enter duration statistics. DHW, mixed and unknown burns are excluded from short CH
counts. Unknown starts and unfinished/interrupted burns remain separate. Counts and
runtime retain `≥` when observation is incomplete; no gas/heat/comfort is inferred.

**A short burn alone is not an incident.** `short_burn_seconds` defaults to 180.
A rapid restart sequence requires consecutive complete short heating burns with
known uninterrupted spacing: `rapid_pause_seconds` defaults to 120,
`rapid_start_interval_seconds` to 300, and `minimum_short_cycles` to 3. DHW/mixed,
unknown intervals and longer burns break the sequence. `mode_edge_seconds` defaults
to 2. These are explicit review heuristics, not manufacturer fault limits.

Optional `temperatures.flow`, `return` and `target` map three distinct temperature
sensors. Celsius/Fahrenheit are converted to °C; missing units, restored/nonfinite
values and gaps cannot establish warmth. Warm context requires a preceding complete
heating burn no more than 30 minutes earlier and continuously recorded return/flow
at least target minus `warm_margin_celsius` (default 5) for
`warm_window_seconds` (default 600). Unknown/DHW intervals break this context.
Held recorded values are disclosed as such, not claimed as a fresh physical sample
at each ignition; a bus/Recorder gap requires new recorded values. Full start-time
values and their recording timestamps remain in the facts.

`warm_short_burns_expected: true` records explicitly approved household context:
brief burns during established warm operation can be expected in this installation.
It enables a conditional explanation, never suppresses a rapid restart finding and
is not a blanket boiler-health exception. Profile revision/hash are saved with each
attempt. Thermal inputs are optional; absent context stays unknown.

Assessment and observation quality are separate. Brief gaps are `minor_gaps` when
their total is at most 300 seconds, each is at most 120 seconds, no burn was lost or
interrupted, and mode classification/conflicts introduce no further uncertainty.
The main result can describe the observed behavior; the card separately shows gap
times, actual coverage and lower-bound totals. Larger/critical gaps remain
`insufficient_data`; a confirmed rapid finding takes precedence with the data-quality
warning retained. Zero observation is `not_run`, not a clean day.

AI selects up to two measured statements through the same constrained native schema
as energy. Measured restart findings, thermal evidence and approved context determine
the permitted explanations. Backend-derived citations/checks cannot invent causes,
values, controls or device actions. No HA/MCP, firmware, shell or provider tools are
passed to the task.

## NAS morning reports (v0.8)

`home_insights.set_nas_profile` stores an explicit v1 profile in native HA Store.
It requires `revision`, four distinct `sources` (`array_running`, `parity_problem`,
`parity_running`, `ups_status`) and 1–32 `disks`. Each disk has a stable `id`,
`name`, binary problem `health` source, `temperature` (`entity`, optional
`attribute`, explicit `unit` °C/°F) and `warn_celsius`. Optional
`problem_sources` are binary problem indicators. `minimum_hot_seconds` defaults
to 600; `parity_error_attribute` defaults to `errors`. These thresholds are
review heuristics, not manufacturer fault limits. Replacements require the current
hash from the administrator-only `home_insights/nas_profile` WebSocket command.
No household mappings are included in the integration.

`home_insights.get_nas_facts` reads `profile_id` and optional completed local `date`
without AI or journal writes. `home_insights.generate_nas_report` adds
`ai_task_entity` and optional `force`. Native HA owns the morning time trigger;
`home_insights/report` accepts mutually exclusive `nas_profile` and isolates
attempts and successes under `nas:` keys. Luma 0.55 supports the same selector.

Native Recorder history and trusted run intervals establish known observation.
Run boundaries break held values, counters and temperature continuity; recovery
is not a new UPS/parity start. DST days use actual local midnight boundaries.
Array stopped time is distinct from a physical fault; a running parity check is
activity, not a problem. The largest reported parity error counter can belong to
an earlier check and is not the number of new daily errors. Disk maximum values
retain their original recording timestamps. Only sustained observed temperature
at/above the configured threshold creates a temperature finding. Explicit standby
can explain absent temperature; unknown or standby never becomes zero degrees.

UPS interpretation uses [NUT status tokens](https://networkupstools.org/docs/user-manual.chunked/ar01s06.html).
Known battery intervals, observed entry edges and warning flags are separate;
battery operation alone does not prove a grid outage. This is a selected-source
retrospective, not a complete SMART, hardware or power-quality audit.

Assessment and quality remain separate: no observation is `not_run`, unknown
intervals remain insufficient, brief non-critical gaps can be disclosed as
`minor_gaps`, and confirmed findings retain their quality warning. Partial durations
and edge counts are observed lower bounds. The same verified tool-free native AI
Task selects up to two measured source-bound statements; it cannot invent causes,
measurements, controls or repair operations.

NAS profiles can additionally bind up to five `unraid_notifications` (`event.*`)
sources. Recorder retains each notification's creation time, ID and bounded subject;
replays after restart are deduplicated and retain their original calendar day.
Parity-disk errors, array warnings, later clear feedback and parity-sync activity
remain in a chronological timeline even after the entity or notification disappears.
Unknown alert types remain warnings; notification removal never proves repair.
A parity sync is distinct from a routine parity check. Prior-day warning events must
be represented in the measured interpretation, even when current flags are clear.

For yesterday's report, `since_midnight` separately snapshots events between today's
local midnight and report generation. Its own AI field is constrained to a measured
chronology statement; it cannot rewrite yesterday or assert restored parity protection.
The card labels the time window and saved-at time explicitly; this is not live status.
Historical reports do not silently attach events from a later date. Raw notification
coverage is not a complete delivery log and remains explicitly limited.

## Private AI validation diagnostics (v0.8)

Rejected responses retain a stable reason code, trusted validator explanation,
bounded/redacted response preview, permitted evidence references and checks in the
private report attempt. The administrator-only detail API exposes this separately
from any published narrative. URLs and recognizable secrets are redacted; full
responses/prompts are never written to system logs. The idempotent nullable database
migration leaves old attempts without invented diagnostic reasons. A rejected current
response never replaces the last successful report or appears as a successful summary.

## Short clothing advice (v0.9)

`home_insights.generate_weather_advice` accepts `weather_entity`, `ai_task_entity`
and optional `force`. It reads the selected entity's native daily forecast and
selects exactly today's row in HA's timezone. A forecast for tomorrow cannot fill
missing data for today. Required daily low/high temperatures are converted to °C;
optional wind, precipitation and current apparent temperature retain unknown values
and explicit units. Location, entity names and arbitrary forecast attributes are
not passed to AI. Daily minimums can include the night; no morning/afternoon timing
or personal cold tolerance is inferred.

The existing verified tool-free native OpenAI AI Task writes at most two short
Hungarian sentences (15–200 characters), without numeric claims, URLs or markup.
No new API key, model client or control/MCP tools are added. The native HA automation
owns scheduling; opening the dashboard never calls AI. A valid result younger than
four hours is reused unless forced, while display validity is at most eight hours
and always ends at the next local midnight. This separates refresh cadence from
expiry so a response finishing seconds after a scheduled run cannot skip the next
six-hour refresh.

Native HA Store keeps the small result, measured input and explicit failed/running
attempt state across restart. Interrupted and rejected attempts never become ready;
storage failures invalidate the in-memory text. The new household-facing clothing
advice sensor exposes only the text and small freshness/source/status metadata,
not raw forecasts or the private incident journal. Its visibility follows normal HA
sensor permissions. This is practical forecast-based clothing guidance, not health
or severe-weather safety advice. Admin users/native automations can generate it.

Luma 0.56's optional `weather_advice_entity` renders a plain paragraph below the
existing Home Hero weather line. Source, day and expiry are checked again in the
browser; an explicit short unavailable message replaces stale/failed text. In that original presentation there
was no additional card, modal, badge or report layout; v0.10 adds optional hourly details. Household entity mappings and
schedules remain runtime configuration.

## Shared Luma policies

The response-only administrator action `home_insights.set_attention_policy` accepts
`policy_id` and a v1 JSON `policy`. Replacing an existing policy also requires its
current `expected_hash`. Policy files use HA's native Store and are integration-owned.
Configuration contains selectors and inert UI action bindings; the backend never
executes a completion/update binding. Unknown fields and incompatible policy shapes
are refused. Concurrent policy writes are serialized.

`home_insights/attention` takes `policy_id` and `language` (`hu` or `en`). It returns
individual evaluated rows, policy revision/hash, observation time and presentation
metadata. Normal authenticated users receive only results permitted by their HA
entity read access. Raw journal/history APIs remain administrator-only. The optional
`debug` projection is administrator-only and contains private household input data;
never publish it or include it in Git.

Backend-bound Luma clients group and render these rows, retaining inline update
confirmation and plant task controls. They do not evaluate a second set of conditions
or silently fall back to the browser when the backend is unavailable. Observation
freshness is explicit. The v1 Python evaluator is verified against 34 synthetic
cases from Luma's frozen TypeScript contract, including HU/EN formatting.

Existing approved household knowledge can remain in its shared HA evaluator during
migration. Before every invocation, its script must consist solely of variables and
a stop/response; device/service/event/parallel/delay actions are refused. Its proof
revision, source observations and expiry are still checked before an exclusion can
apply. A real active fault always takes priority over a missing-data exception.

**v0.3 adds canonical daily facts and audited handling.** Pass `policy_id` to
`home_insights.get_daily_facts` or `home_insights/daily_facts` for schema v2 policy
facts. Omitting it preserves the legacy native-source result. These are separate
scopes: never add native evidence counts to canonical episode counts.

Canonical facts separate issues, maintenance updates and informational events.
They distinguish carried episodes, first observation, detected recurrence,
observed clearance, channel removal and exclusion. Brief pending/inactive changes
are not faults. Coverage is clipped to policy adoption, observer runs and known
source gaps. Confirmed active seconds cover only known complete observation;
episode elapsed time is not a physical failure-duration estimate. Calendar days
follow HA's timezone, including 23/25-hour DST days.

Evidence, handling state, policy revisions and household definitions are queried
as of that day's end; tomorrow's edits cannot rewrite yesterday's interpretation.
Output is bounded with explicit truncation flags and incidents before update floods.
Per-source availability intervals and household definition snapshots start when
v0.3 observes them; earlier details are explicitly unavailable, not backfilled.

Luma 0.49 uses `home_insights/record_handling` for seen, claimed-fixed, note,
snooze and unsnooze metadata. Requests are atomic across 1–100 exact episode targets,
with per-episode revisions and actor-scoped idempotency keys. Actor identity comes
from the authenticated HA user. Records survive reload/restart and never close a
source episode. A recurrence has a fresh UUID and fresh handling state. Critical
or non-dismissible sources cannot be snoozed.

`home_insights/episode` exposes bounded, cursor-paginated, plain-text event history.
Entity sources require HA read permission; writing handling additionally requires
control permission. Non-entity native channels require an administrator.
`seen` records review time and authenticated actor; it does not hide or resolve
a source. Snoozed episodes stay in the journal but are absent from the main hero
and default active list; the explicit Snoozed filter permits restoration. Raw linked
source context is not exposed through this history projection. These APIs have no
device or configuration actions. The morning AI/report pipeline above uses this same journal.

## What runs, and where?

One custom integration runs inside **Home Assistant Core**, including while every
browser is closed. It requires no separate server, Docker container, Node runtime,
HA access token, MQTT broker or AI credentials.

It observes:

- Native persistent notifications: appearance, replacement and removal.
- Active, visible HA Repairs, preserving their source identity and translations.
- Integration setup/retry/migration/unload errors, including unknown intermediate states.
- Optionally, explicitly selected entity states as measured facts. These do not
  create additional threshold rules or alerts.

Ignored Repairs and ignored discovery entries remain excluded; they do not become
open incidents or unexplained gaps. Their exclusion is distinct from recovery.

Open source episodes survive integration reloads and process restarts. Repeated
observations do not create repeated discoveries. Unknown states, collection failures,
queue overflow, storage failures and observation gaps are not successful recoveries.
Deleting a HA notification is recorded as channel removal, never proof that its
underlying problem was fixed, seen or delivered to a phone.

## Installation

Requires **Home Assistant Core 2026.11.0 or newer**; currently tested on 2026.11.1 (also verified on 2026.11.0).
Compatibility with later versions is not assumed by the version requirement.

Add `https://github.com/davidfarkas93/ha-home-insights` as a HACS/Marketplace custom
repository, category **Integration**, and download it. Alternatively, copy
`custom_components/home_insights` to the matching directory under your HA config.
Restart Core to discover the new integration, then go to **Settings → Devices &
services → Add integration → Home Insights**. No YAML configuration is needed.

Native source observation is always enabled. Additional entity facts are optional
and can be changed using the integration's options. The integration reloads after
an options change. Its own status entities cannot be selected for observation.

Three diagnostic entities expose journal status, open native source episodes and
the last observation time. Their IDs are assigned by HA; discover them rather than
hard-coding an assumed ID. The open count is **not** the hero's complete Luma issue
count. Full private history is exposed through bounded read APIs, not giant sensor
attributes that would duplicate the journal in Recorder.

## Reading history and daily facts

The response-only action `home_insights.get_daily_facts` reads a completed local
calendar day. Omitting `date` selects yesterday in HA's configured timezone.
The action does not invoke AI, control a device or mutate the journal.

Read-only WebSocket commands:

| Command | Parameters | Result |
| --- | --- | --- |
| `home_insights/snapshot` | none | Current observation health and bounded open episodes |
| `home_insights/history` | `limit` 1–100; optional `before` sequence | Cursor-paginated events |
| `home_insights/daily_facts` | optional `date` (`YYYY-MM-DD`), `policy_id` | Completed-day facts; policy selects canonical schema v2 |
| `home_insights/episode` | `policy_id`, `episode_id`, optional `before`, `limit` | Authorized episode and handling history |

Raw snapshot/history and daily facts require an administrator. The action also allows
native HA automations with no user context. Broader frontend access needs a deliberate
permission model; a normal user's entity visibility must not be bypassed through
a household-wide journal endpoint. The filtered episode projection above has its
own entity authorization and does not grant access to raw journal endpoints.

Daily results distinguish:

- `not_run`: no observed coverage for that day.
- `insufficient_data`: partial coverage, source failures or unknown measurements.
- `no_anomaly_found`: complete coverage with no active episodes **within the stated scope**.
- `attention_needed`: complete coverage with an episode present during the day.

Each result includes actual local midnight boundaries (23/24/25-hour days), coverage,
per-event counts, ongoing episodes, bounded evidence and explicit truncation flags.
Raw selected-entity changes cannot crowd source incidents out of the evidence list.
The complete journal remains on disk. Future energy, heating or NAS analyses must
compute numeric results from HA measurements; the AI must not estimate missing counts.

## Storage, permissions and limitations

The journal uses SQLite at `<config>/home_insights/<entry_id>.sqlite`, with WAL and
transactional writes on one dedicated executor thread. It does no disk I/O in the
HA event loop. Include the whole `home_insights` directory in backups; do not copy
only a live `.sqlite` file while omitting its WAL. Stop/unload the integration or
use SQLite's online backup facility for a consistent standalone database copy.

History is retained without automatic pruning in v0.1. Selected high-frequency
sensors can therefore grow the database; select only facts you actually need.
Removing an integration entry preserves its private journal for recovery and audit.
A new entry uses a new journal; it does not silently attach another entry's history.

The database contains private household facts and notification messages. It is not
uploaded. Diagnostics contain only health/count metadata, not messages, state values
or configured entity IDs. Arbitrary entity attributes, URLs and access tokens are
not copied into state facts.

A custom integration runs with HA's internal privileges; it is **not a security
sandbox**. This implementation contains no device-control, configuration-write,
firmware, shell, generic template-evaluation or arbitrary-SQL endpoint. The future
AI task will receive a prepared fact bundle and no HA tools or journal credential.

HA downtime is also observer downtime. The journal cannot reconstruct events it
never observed. A repair created and deleted before its bus callback can read the
registry may be missed; native snapshots and 15-second reconciliation do not provide
guaranteed delivery or historical completeness. Collection gaps remain explicit.
The notification adapter has one isolated private HA read accessor because 2026.11
does not expose an equivalent public Python collection API. Its behavior is tested
against the pinned target HA version.

## Development

```sh
uv sync --frozen --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python scripts/package.py
```

The lockfile pins the test environment to actual HA 2026.11.1. Tests exercise Core's
configuration flow, sensor creation, reload/unload, native events, disk failure,
bounded-queue loss, source recovery, permissions, persistence, pagination and DST.
They do not connect to a production HA instance or operate real devices.

Architecture and the staged Luma migration: [docs/architecture.md](docs/architecture.md).


### Hourly detail for clothing advice (v0.10)

The same generation action additionally reads the native hourly forecast when the
weather entity advertises hourly support. Today's remaining points are projected
into morning (06–12), afternoon (12–18) and evening (18–24) in HA's timezone.
Unknown units, temperatures and duplicate/ambiguous hours cannot fabricate data.
Past periods remain elapsed, missing periods remain missing, and incomplete hourly
sets are explicitly partial. Unknown precipitation or wind stays unknown. There is
no historical weather claim and tomorrow's forecast never fills today's gaps.
An hourly fetch failure keeps the daily clothing tip usable with unavailable detail.

The public sensor adds only the three bounded period/sample projections and fixed
hourly availability/version fields, never raw forecast attributes, location or
private journal data. These same measured projections may inform the short AI tip.
The detailed weather prose in Luma is composed from these forecast values, including
sunshine only at explicitly sunny forecast hours. There is no second AI call when
opening the sheet. Existing v0.9 results are refreshed once rather than reused as
if they contained the new hourly detail; scheduling and tool-free access are unchanged.

In v0.10.1 the compact tip rejects time-of-day predictions, unqualified rain
claims and shopping language. Persisted tips are validated before reuse, so an
older unsupported timing claim cannot survive as a cached current result.
Hourly detail remains composed from measured forecast points.
