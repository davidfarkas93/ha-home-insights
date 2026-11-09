"""HA lifecycle and a bounded, ordered stream of journal work."""

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.start import async_at_started

from . import adapters
from .const import DOMAIN, HEARTBEAT_SECONDS, QUEUE_LIMIT
from .journal import Journal
from .models import previous_day

_LOGGER = logging.getLogger(__name__)


class Coordinator:
    """No network client, API credential, external process or device service calls."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self.entities = list(
            dict.fromkeys(
                entry.options.get("monitored_entities", entry.data.get("monitored_entities", []))
            )
        )
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="home_insights")
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.listeners: set[Callable[[], None]] = set()
        self.removers: list[Callable[[], None]] = []
        self.entry_watchers: dict[str, Callable[[], None]] = {}
        self.data: dict[str, Any] = {"status": "starting", "open_count": None}
        self.failed = False
        self.failure_at: float | None = None
        self.started = False
        self.closed = False
        self.overflow_at: float | None = None
        self.worker: asyncio.Task | None = None
        self.journal: Journal | None = None

    async def execute(self, function, *args):
        return await self.hass.loop.run_in_executor(self.pool, partial(function, *args))

    async def async_setup(self) -> None:
        policy = {
            "version": 1,
            "revision": "native-observers.1",
            "adapters": ["notifications", "repairs", "integrations"],
            "monitored_entities": sorted(self.entities),
        }
        path = Path(self.hass.config.path(DOMAIN, f"{self.entry.entry_id}.sqlite"))

        def open_journal():
            path.parent.mkdir(parents=True, exist_ok=True)
            return Journal(path, time.time(), policy)

        try:
            self.journal = await self.execute(open_journal)
        except Exception:
            self.pool.shutdown(wait=False)
            raise
        self.worker = self.hass.async_create_background_task(self._run(), "home_insights_journal")
        self.removers.append(async_at_started(self.hass, self._async_start))
        self.removers.append(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)
        )

    @callback
    def enqueue(self, kind: str, payload: Any = None, now: float | None = None) -> None:
        if self.closed:
            return
        at = time.time() if now is None else now
        try:
            self.queue.put_nowait((kind, payload, at))
        except asyncio.QueueFull:
            if self.overflow_at is None:
                self.overflow_at = at

    async def _async_start(self, _hass: HomeAssistant) -> None:
        if self.closed or self.started:
            return
        self.started = True
        self.removers.extend(
            [
                async_dispatcher_connect(
                    self.hass,
                    adapters.persistent_notification.SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED,
                    self._notifications_changed,
                ),
                self.hass.bus.async_listen(
                    issue_registry.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, self._repairs_changed
                ),
                async_dispatcher_connect(
                    self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._entries_changed
                ),
                async_track_time_interval(
                    self.hass, self._heartbeat, timedelta(seconds=HEARTBEAT_SECONDS)
                ),
            ]
        )
        if self.entities:
            self.removers.append(
                async_track_state_change_event(self.hass, self.entities, self._entity_changed)
            )
        self._watch_entries()
        self.enqueue("resync")

    @callback
    def _watch_entries(self) -> None:
        entries = {
            e.entry_id: e for e in self.hass.config_entries.async_entries() if e.domain != DOMAIN
        }
        for entry_id in list(self.entry_watchers):
            if entry_id not in entries:
                self.entry_watchers.pop(entry_id)()
        for entry_id, entry in entries.items():
            if entry_id not in self.entry_watchers:
                self.entry_watchers[entry_id] = entry.async_on_state_change(
                    self._integrations_changed
                )

    @callback
    def _notifications_changed(self, *_args) -> None:
        try:
            self.enqueue("batch", ("notifications", adapters.notifications(self.hass)))
        except Exception:
            self.enqueue("failure", ("notifications", "adapter_read_failed"))

    @callback
    def _integrations_changed(self) -> None:
        try:
            self.enqueue("batch", ("integrations", adapters.integrations(self.hass)))
        except Exception:
            self.enqueue("failure", ("integrations", "adapter_read_failed"))

    @callback
    def _entries_changed(self, *_args) -> None:
        self._watch_entries()
        self._integrations_changed()
        self._repairs_changed(None)

    @callback
    def _repairs_changed(self, _event) -> None:
        try:
            self.enqueue("batch", ("repairs", adapters.repairs(self.hass)))
        except Exception:
            self.enqueue("failure", ("repairs", "adapter_read_failed"))

    @callback
    def _entity_changed(self, event) -> None:
        entity_id = event.data["entity_id"]
        # Also reject self-observation after a registry rename/config import.
        item = er.async_get(self.hass).async_get(entity_id)
        if item and item.platform == DOMAIN:
            return
        self.enqueue(
            "entity",
            (entity_id, adapters.entity_fact(event.data["new_state"])),
            event.time_fired.timestamp(),
        )

    @callback
    def _heartbeat(self, _now) -> None:
        self._watch_entries()
        self._notifications_changed()
        self._integrations_changed()
        self._repairs_changed(None)
        self.enqueue("checkpoint")

    async def _resync(self) -> None:
        """Re-establish baselines without guessing any missed transitions."""
        now = time.time()
        captured = []
        for adapter, function in [
            ("notifications", adapters.notifications),
            ("integrations", adapters.integrations),
            ("repairs", adapters.repairs),
        ]:
            try:
                captured.append((adapter, function(self.hass)))
            except Exception:
                captured.append((adapter, None))
        entity_facts = []
        for entity in self.entities:
            item = er.async_get(self.hass).async_get(entity)
            if item and item.platform == DOMAIN:
                continue
            entity_facts.append((entity, adapters.entity_fact(self.hass.states.get(entity))))
        for adapter, rows in captured:
            if rows is None:
                await self.execute(self.journal.source_failure, adapter, "adapter_read_failed", now)
            else:
                await self._apply_batch(adapter, rows, now)
        for entity, fact in entity_facts:
            await self.execute(self.journal.entity, entity, fact, now)
        await self.execute(self.journal.checkpoint, time.time())
        self.data = await self.execute(self.journal.snapshot)
        self._notify()

    async def _apply_batch(self, adapter, rows, now: float) -> None:
        if adapter == "repairs":
            rows = await adapters.translate_repairs(self.hass, rows)
        await self.execute(self.journal.batch, adapter, rows, now)

    async def _run(self) -> None:
        while True:
            kind, payload, at = await self.queue.get()
            try:
                if self.overflow_at is not None or self.failure_at is not None:
                    reason = (
                        "storage_write_failed" if self.failure_at is not None else "queue_overflow"
                    )
                    await self.execute(
                        self.journal.gap, reason, self.failure_at or self.overflow_at
                    )
                    self.overflow_at = None
                    while not self.queue.empty():
                        self.queue.get_nowait()
                        self.queue.task_done()
                    await self._resync()
                    self.failure_at = None
                    self.failed = False
                    self._notify()
                    continue
                if kind == "batch":
                    await self._apply_batch(*payload, at)
                elif kind == "failure":
                    await self.execute(self.journal.source_failure, *payload, at)
                elif kind == "entity":
                    await self.execute(self.journal.entity, *payload, at)
                elif kind == "resync":
                    await self._resync()
                elif kind == "checkpoint":
                    await self.execute(self.journal.checkpoint, at)
                self.failed = False
                self.data = await self.execute(self.journal.snapshot)
                self._notify()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Notification text and measured data never enter application logs.
                if not self.failed:
                    _LOGGER.error("Journal operation failed (%s)", type(error).__name__)
                self.failed = True
                if self.failure_at is None:
                    self.failure_at = at
                self._notify()
            finally:
                self.queue.task_done()

    @callback
    def _notify(self) -> None:
        for listener in self.listeners.copy():
            listener()

    @callback
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    async def async_flush(self) -> None:
        await self.queue.join()

    async def async_history(self, before=None, limit=30):
        await self.async_flush()
        return await self.execute(self.journal.history, before, limit)

    async def async_daily_facts(self, day: date | None = None):
        await self.async_flush()
        if self.failed or self.closed or not self.started:
            raise RuntimeError("Journal is not recording")
        zone = self.hass.config.time_zone
        return await self.execute(
            self.journal.daily_facts, day or previous_day(time.time(), zone), zone
        )

    async def _async_stop(self, _event) -> None:
        await self.async_close()

    async def async_close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for remover in [*self.removers, *self.entry_watchers.values()]:
            remover()
        self.removers.clear()
        self.entry_watchers.clear()
        try:
            await asyncio.wait_for(self.async_flush(), timeout=15)
        finally:
            if self.worker:
                self.worker.cancel()
                with suppress(asyncio.CancelledError):
                    await self.worker
            try:
                if self.journal:
                    await self.execute(self.journal.close, time.time())
            finally:
                await self.hass.async_add_executor_job(self.pool.shutdown)
        self.data = {**self.data, "status": "stopped"}
        self._notify()
