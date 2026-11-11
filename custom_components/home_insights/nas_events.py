"""Bounded Unraid notification chronology; notification removal is not recovery."""

import re
from datetime import datetime

from .models import iso
from .recorder_history import stamp


def classify(subject, importance):
    text = subject.casefold()
    if "parity disk in error state" in text:
        return "parity_disk_error", "Parity-lemez hibajelzése", True
    if "array has errors" in text:
        return "array_errors", "Array olvasási hibajelzése", True
    if "array health report [fail]" in text:
        return "array_health_fail", "Array állapotjelentés: hibajelzés", True
    if "array health report [pass]" in text:
        return "array_health_pass", "Array állapotjelentés: rendben", False
    if "array turned good" in text:
        return "array_recovered", "Array: rendben visszajelzés", False
    if "parity-sync started" in text or "parity-sync in progress" in text:
        return "parity_sync", "Parity-szinkronizálás", False
    if "parity" in text and "check" in text and "start" in text:
        return "parity_check", "Parity-ellenőrzés indult", False
    if importance in ("ALERT", "WARNING"):
        return "notification_warning", "NAS-figyelmeztetés", True
    return "notification_info", "NAS tájékoztató esemény", False


def event_time(row):
    try:
        value = row.attributes.get("timestamp")
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo is not None else None
    except ValueError, TypeError, AttributeError:
        return None


def collect_events(histories, sources, start, end):
    events, seen, missing_time = [], set(), 0
    for source in sources:
        rows = histories.get(source, [])
        if len(rows) > 20000:
            raise ValueError("NAS notification history exceeds query budget")
        for row in sorted(rows, key=lambda r: stamp(r.last_updated)):
            attrs = row.attributes
            if attrs.get("event_type") != "notification_created" or attrs.get("restored"):
                continue
            at = event_time(row)
            observed = stamp(row.last_updated)
            if at is None:
                missing_time += start <= observed < end
                continue
            # Replayed/held events must retain their creation day, not become new events.
            if not start <= at < end or at > observed + 60:
                continue
            identity = attrs.get("notification_id")
            if not isinstance(identity, str) or not identity:
                missing_time += 1
                continue
            key = (source, identity)
            if key in seen:
                continue
            seen.add(key)
            importance = str(attrs.get("importance", "UNKNOWN")).upper()
            subject = re.sub(
                r"https?://\S+", "[hivatkozás elhagyva]", str(attrs.get("subject", ""))
            )[:500]
            code, label, alert = classify(subject, importance)
            events.append(
                {
                    "source": source,
                    "id": identity[:512],
                    "at": iso(at),
                    "observed_at": iso(observed),
                    "code": code,
                    "label": label,
                    "subject": subject,
                    "importance": importance,
                    "attention": alert,
                }
            )
    events.sort(key=lambda e: (e["at"], e["source"], e["id"]))
    # Preserve alerts and context near them before routine informational floods.
    relevant = [e for e in events if e["code"] != "notification_info"]
    selected = sorted(
        sorted(relevant, key=lambda e: (not e["attention"], e["at"]))[:200], key=lambda e: e["at"]
    )
    return {
        "start": iso(start),
        "end": iso(end),
        "sources": sources,
        "events": selected,
        "attention": any(e["attention"] for e in events),
        "truncated": len(relevant) > 200,
        "invalid_events": missing_time,
        "limitations": [
            "recorded_events_only_not_a_complete_notification_delivery_log",
            "clearance_or_entity_removal_does_not_prove_repaired_disk_or_valid_parity",
        ],
    }


def chronology_statement(timeline):
    events = timeline["events"]
    errors = [e for e in events if e["code"] == "parity_disk_error"]
    if errors:
        later = [e for e in events if e["at"] > errors[-1]["at"]]
        if any(e["code"] == "parity_sync" for e in later):
            return (
                "Parity-lemez hibajelzése érkezett, később parity-szinkronizálás indult; "
                "a helyreállt paritásvédelem ebből még nem igazolt."
            )
        return (
            "Parity-lemez hibajelzése érkezett; a későbbi elérhetőség "
            "önmagában nem igazolja a helyreállást."
        )
    if timeline["attention"]:
        return (
            "A NAS eseménynaplójában figyelmet kérő jelzés volt; "
            "a részletekben visszanézhető az eseménysor."
        )
    if any(e["code"] == "parity_sync" for e in events):
        return "Parity-szinkronizálás indult; ez különbözik a parity-ellenőrzéstől."
    if any(e["code"] == "array_recovered" for e in events):
        return (
            "Az array rendben állapotot jelzett; ebből önmagában "
            "nem igazolható a teljes paritásvédelem."
        )
    return "Nem találtam figyelmet kérő eseményt a megőrzött NAS-értesítésekben."
