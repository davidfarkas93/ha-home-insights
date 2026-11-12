"""Remember explicit user dismissals for configured, observable conditions only."""

import time

from homeassistant.components import persistent_notification
from homeassistant.core import callback

from . import adapters
from .models import digest


def setup_notification_memory(db):
    db.execute("""CREATE TABLE IF NOT EXISTS notification_acknowledgements(
      notification_id TEXT PRIMARY KEY, entity TEXT NOT NULL, state TEXT NOT NULL,
      content_hash TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL)""")


def content_hash(row):
    return digest([row.title, row.message])


def context(hass, binding, states=None):
    entity = hass.states.get(binding["entity"]) if states is None else states.get(binding["entity"])
    value = entity.state if states is None and entity is not None else (entity or {}).get("state")
    attributes = (
        entity.attributes
        if states is None and entity is not None
        else (entity or {}).get("attributes", {})
    )
    if (
        entity is None
        or value in ("unknown", "unavailable", "", None)
        or attributes.get("restored")
    ):
        return None
    return value


def record_suppression(journal, row, binding, now):
    with journal.db:
        journal._event(
            "notification_redelivery_suppressed",
            now,
            {
                "notification_id": row.source_id,
                "entity": binding["entity"],
            },
        )


def acknowledge(journal, notices, actor, now):
    with journal.db:
        for row, binding in notices:
            journal.db.execute(
                "INSERT OR REPLACE INTO notification_acknowledgements VALUES(?,?,?,?,?,?)",
                (row.source_id, binding["entity"], binding["state"], content_hash(row), actor, now),
            )
            journal._event(
                "notification_dismissal_remembered",
                now,
                {
                    "notification_id": row.source_id,
                    "entity": binding["entity"],
                    "state": binding["state"],
                    "actor": actor,
                },
            )


def match_and_reset(journal, rows, bindings, states, now, settled):
    current = {r.source_id: r for r in rows}
    matches = []
    with journal.db:
        for saved in journal.db.execute("SELECT * FROM notification_acknowledgements").fetchall():
            identifier = saved["notification_id"]
            binding, state, row = (
                bindings.get(identifier),
                states.get(identifier),
                current.get(identifier),
            )
            same_binding = binding and (binding["entity"], binding["state"]) == (
                saved["entity"],
                saved["state"],
            )
            changed = (
                not same_binding or row is not None and content_hash(row) != saved["content_hash"]
            )
            changed = changed or settled and state is not None and state != saved["state"]
            if changed:
                journal.db.execute(
                    "DELETE FROM notification_acknowledgements WHERE notification_id=?",
                    (identifier,),
                )
                journal._event(
                    "notification_acknowledgement_reset", now, {"notification_id": identifier}
                )
            elif row is not None and state == saved["state"]:
                matches.append(row)
    return matches


class NotificationMemory:
    """Uses native dismiss, never device controls or configuration writes."""

    def __init__(self, owner):
        self.owner = owner
        self.hass = owner.hass
        self.intents = {}
        self.settle_until = 0

    @property
    def bindings(self):
        result = {}
        for policy in self.owner.attention.policies.values():
            if policy.get("adapters", {}).get("notifications"):
                for binding in policy["adapters"].get("notification_acknowledgements", []):
                    result[binding["notification_id"]] = binding
        return result

    @callback
    def service_requested(self, event):
        if (
            event.data.get("domain") != "persistent_notification"
            or not event.context.user_id
            or event.context.parent_id is not None
        ):
            return
        service = event.data.get("service")
        if service not in ("dismiss", "dismiss_all"):
            return
        identifier = event.data.get("service_data", {}).get("notification_id")
        bindings = self.bindings
        for row in adapters.notifications(self.hass):
            binding = bindings.get(row.source_id)
            if (
                binding
                and (service == "dismiss_all" or identifier == row.source_id)
                and context(self.hass, binding) == binding["state"]
            ):
                intent = (row, binding, event.context.user_id)
                self.intents[row.source_id] = intent
                self.hass.loop.call_soon(self.expire_intent, row.source_id, intent)

    @callback
    def expire_intent(self, identifier, intent):
        if self.intents.get(identifier) is intent:
            self.intents.pop(identifier)

    @callback
    def removed(self, update_type, notices):
        if update_type != persistent_notification.UpdateType.REMOVED:
            return
        for identifier, notice in notices.items():
            intent = self.intents.pop(identifier, None)
            if intent:
                row, binding, actor = intent
                # The actual removal must match the explicit user request.
                if digest(
                    [notice.get("title") or "HA notification", notice["message"]]
                ) == content_hash(row):
                    self.owner.enqueue("notification_ack", ([(row, binding)], actor))

    async def reconcile(self, captured_states=None):
        bindings = self.bindings
        rows = adapters.notifications(self.hass)
        states = {
            identifier: context(self.hass, b, captured_states) for identifier, b in bindings.items()
        }
        matches = await self.owner.execute(
            match_and_reset,
            self.owner.journal,
            rows,
            bindings,
            states,
            time.time(),
            time.time() >= self.settle_until,
        )
        # Check again after executor I/O: a changed notice or condition must survive.
        current = {r.source_id: r for r in adapters.notifications(self.hass)}
        for row in matches:
            actual, binding = current.get(row.source_id), self.bindings.get(row.source_id)
            if (
                actual
                and binding
                and content_hash(actual) == content_hash(row)
                and context(self.hass, binding) == binding["state"]
            ):
                persistent_notification.async_dismiss(self.hass, row.source_id)
                await self.owner.execute(
                    record_suppression,
                    self.owner.journal,
                    row,
                    binding,
                    time.time(),
                )
