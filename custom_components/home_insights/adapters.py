"""Read-only native HA adapters; private HA compatibility is isolated here."""

from dataclasses import replace
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry
from homeassistant.helpers.translation import async_get_translations

from .models import Observation, digest


def notifications(hass: HomeAssistant) -> list[Observation]:
    """Read the same collection as persistent_notification/get, without a HA token.

    HA 2026.11 has no public Python read API for this collection. Keep this one
    private accessor isolated and covered by actual HA integration tests.
    """
    rows = persistent_notification._async_get_or_create_notifications(hass).values()  # noqa: SLF001
    result = []
    for row in rows:
        created = row.get("created_at")
        created = created.isoformat() if hasattr(created, "isoformat") else created
        result.append(
            Observation(
                adapter="notifications",
                source_id=row["notification_id"],
                status="active",
                fingerprint=digest([created, row["message"]]),
                title=row.get("title") or "HA notification",
                message=row["message"],
                source_time=created,
            )
        )
    return result


def repairs(hass: HomeAssistant) -> list[Observation]:
    """Capture an immutable source snapshot before any translation I/O."""
    registry = issue_registry.async_get(hass)
    issues = [
        item
        for item in registry.issues.values()
        if item.active and not registry.async_is_hidden(item)
    ]
    result = []
    for issue in issues:
        placeholders = issue.translation_placeholders or {}
        created = issue.created.isoformat()
        result.append(
            Observation(
                adapter="repairs",
                source_id=f"{issue.domain}:{issue.issue_id}",
                status="active",
                fingerprint=digest([issue.domain, issue.issue_id, created]),
                title=f"{issue.domain}: repair needed",
                message="HA reports an open repair.",
                severity=issue.severity.value if issue.severity else "warning",
                source_time=created,
                facts={
                    "domain": issue.domain,
                    "issue_id": issue.issue_id,
                    "translation_key": issue.translation_key,
                    "placeholders": placeholders,
                },
            )
        )
    return result


async def translate_repairs(hass: HomeAssistant, rows: list[Observation]) -> list[Observation]:
    """Enrich captured facts; failed translations never remove an actual repair."""
    domains = {row.facts["domain"] for row in rows}
    if not domains:
        return rows
    try:
        translations = await async_get_translations(hass, hass.config.language, "issues", domains)
    except Exception:
        return rows
    result = []
    for row in rows:
        facts = row.facts
        prefix = (
            f"component.{facts['domain']}.issues.{facts['translation_key'] or facts['issue_id']}"
        )

        def interpolate(value: str, replacements=facts["placeholders"]) -> str:
            for key, replacement in replacements.items():
                value = value.replace("{" + key + "}", str(replacement))
            return value

        description = translations.get(f"{prefix}.description") or next(
            (
                value
                for key, value in translations.items()
                if key.startswith(f"{prefix}.fix_flow.step.") and key.endswith(".description")
            ),
            row.message,
        )
        result.append(
            replace(
                row,
                title=interpolate(translations.get(f"{prefix}.title", row.title)),
                message=interpolate(description),
            )
        )
    return result


def integrations(hass: HomeAssistant) -> list[Observation]:
    """An entry not loaded yet is unknown, rather than successfully recovered."""
    errors = {"setup_retry", "setup_error", "migration_error", "failed_unload"}
    result = []
    for entry in hass.config_entries.async_entries():
        if entry.domain == "home_insights":
            continue
        state = entry.state.value
        status = (
            "excluded"
            if entry.disabled_by
            else "active"
            if state in errors
            else "clear"
            if state == "loaded"
            else "unknown"
        )
        result.append(
            Observation(
                adapter="integrations",
                source_id=entry.entry_id,
                status=status,
                fingerprint=state,
                title=entry.title,
                message=f"{entry.domain}: {state}",
                severity="error",
                facts={"domain": entry.domain, "state": state},
            )
        )
    return result


def entity_fact(state: State | None) -> dict[str, Any]:
    """No arbitrary attributes, URLs or access tokens enter the fact journal."""
    if state is None:
        return {"available": False, "state": None, "source_time": None}
    return {
        "available": state.state not in ("unknown", "unavailable", "")
        and not state.attributes.get("restored", False),
        "state": state.state,
        "source_time": state.last_changed.isoformat(),
        "unit": state.attributes.get("unit_of_measurement"),
        "device_class": state.attributes.get("device_class"),
        "restored": bool(state.attributes.get("restored", False)),
    }
