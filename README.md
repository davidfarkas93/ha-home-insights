# Home Insights

Persistent incident-source tracking and daily facts inside Home Assistant.
The project is building toward household context, AI-assisted morning summaries
and Luma dashboard integration.

**v0.1 is the native observation journal foundation.** It does not yet replace
Luma's custom rules, household-context evaluator or the existing daily report.
Installing it does not enable an AI provider or change any dashboard or automation.

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

Open source episodes survive integration reloads and process restarts. Repeated
observations do not create repeated discoveries. Unknown states, collection failures,
queue overflow, storage failures and observation gaps are not successful recoveries.
Deleting a HA notification is recorded as channel removal, never proof that its
underlying problem was fixed, seen or delivered to a phone.

## Installation

Requires **Home Assistant Core 2026.11.0 or newer**; currently tested on 2026.11.0.
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
| `home_insights/daily_facts` | optional `date` (`YYYY-MM-DD`) | Completed-day facts |

In v0.1, these private journal APIs require an administrator. The action also allows
native HA automations with no user context. Broader frontend access needs a deliberate
permission model before Luma adopts these APIs; a normal user's entity visibility
must not be bypassed through a household-wide journal endpoint.

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

The lockfile pins the test environment to actual HA 2026.11.0. Tests exercise Core's
configuration flow, sensor creation, reload/unload, native events, disk failure,
bounded-queue loss, source recovery, permissions, persistence, pagination and DST.
They do not connect to a production HA instance or operate real devices.

Architecture and the staged Luma migration: [docs/architecture.md](docs/architecture.md).
