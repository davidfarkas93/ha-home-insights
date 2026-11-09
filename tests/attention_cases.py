"""Installation-independent differential cases for the frozen Luma v1 contract."""

from copy import deepcopy
from datetime import UTC, datetime

NOW = 1793534400000


def cases():
    changed = (
        datetime.fromtimestamp((NOW - 600000) / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    states = {}
    registry = {}

    def add(identifier, state, platform="test", **attributes):
        states[identifier] = {
            "entity_id": identifier,
            "state": state,
            "last_changed": changed,
            "attributes": attributes,
        }
        registry[identifier] = {"platform": platform}

    add("sensor.temperature", "44", friendly_name="Gépház 🌡️")
    add("sensor.alerts", "ServerDisk, FutureAlert", "example")
    add("sensor.disk_usage", "83.95", unit_of_measurement="%")
    add("sensor.other_usage", "99")
    registry["sensor.alerts"]["device_id"] = "device-a"
    registry["sensor.disk_usage"]["device_id"] = "device-a"
    registry["sensor.other_usage"]["device_id"] = "device-b"
    add("sensor.battery", "12", device_class="battery")
    add("sensor.ignored", "unknown", device_class="battery")
    registry["sensor.ignored"]["labels"] = ["ignore"]
    add("input_boolean.plant_task", "on")
    add("sensor.guard", "off")
    add("sensor.flash", "unknown")
    add("binary_sensor.problem", "on", device_class="problem")
    add("binary_sensor.safety", "on", device_class="safety")
    add("sensor.deadline", "on")
    add("sensor.clear_alerts", "")
    add("update.example", "on", "marketplace", installed_version="1", latest_version="2")
    add("update.installing", "off", "marketplace", in_progress=0, latest_version="2")
    add(
        "event.example",
        "2026-01-01T00:00:00Z",
        title="Example event",
        subject="Observed",
        importance="alert",
    )
    add("input_text.snooze", "")
    policy = {
        "version": 1,
        "revision": "contract.1",
        "rules": [
            {
                "id": "warm",
                "entity": "sensor.temperature",
                "above": 42,
                "below": 45,
                "message": "{name}: {value}",
                "identity": "condition",
            },
            {
                "id": "hot",
                "entity": "sensor.temperature",
                "above": 44.999,
                "message": "Hot",
                "tone": "error",
            },
            {
                "id": "battery",
                "device_classes": ["battery"],
                "below": 20,
                "message": "{name}: {remaining}",
            },
            {
                "id": "alerts",
                "entity_pattern": "sensor.*alerts",
                "platform": "example",
                "state_not": ["", "none", "unknown", "unavailable"],
                "message": "{value}",
                "state_labels": {"ServerDisk": "Disk usage"},
                "context": [{"label": "Usage", "entity_pattern": "sensor.*usage", "decimals": 1}],
            },
            {
                "id": "plant",
                "entity": "input_boolean.plant_task",
                "state": "on",
                "message": "Water plants",
                "notification_ids": ["plant"],
                "completion": {
                    "label": "Watered",
                    "confirmation": "Confirm?",
                    "action": {"action": "perform-action", "perform_action": "script.example"},
                },
            },
            {
                "id": "deadline",
                "entity": "sensor.deadline",
                "state": "on",
                "for_minutes": 15,
                "message": "Long-running",
            },
            {"id": "missing", "entity": "sensor.missing", "above": 5, "message": "Unavailable"},
            {"id": "flash", "entity": "sensor.flash", "state": "on", "message": "Flash fault"},
        ],
        "ignore_labels": ["ignore"],
        "problems": {"enabled": True},
        "updates": {
            "enabled": True,
            "groups": [{"id": "system", "name": "System updates", "platforms": ["marketplace"]}],
        },
        "adapters": {
            "notifications": True,
            "repairs": True,
            "integrations": True,
            "events": ["event.example"],
            "notification_exclude": ["excluded*"],
        },
        "acknowledgements_entity": "input_text.snooze",
        "diagnostics_path": "/example/diagnostics",
    }
    external = {
        "registry": registry,
        "readiness": {k: "ready" for k in ("registry", "notifications", "repairs", "integrations")},
        "notifications": [
            {
                "notification_id": "plant",
                "title": "Plants",
                "message": "Time to water",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "notification_id": "other",
                "title": "Other",
                "message": "Unrelated",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {"notification_id": "excluded-test", "title": "Excluded", "message": "Hidden"},
        ],
        "repairs": {
            "issues": [
                {
                    "domain": "example",
                    "issue_id": "repair",
                    "translation_key": "repair",
                    "translation_placeholders": {"server": "Example server"},
                }
            ]
        },
        "repairTranslations": {
            "component.example.issues.repair.title": "{server}: repair",
            "component.example.issues.repair.description": "Check {server}",
        },
        "integrations": [
            {"entry_id": "example", "domain": "example", "title": "Example", "state": "setup_retry"}
        ],
    }
    base = {
        "states": states,
        "registry": registry,
        "policy": policy,
        "external": external,
        "now": NOW,
        "language": "hu",
    }
    result = {"base": deepcopy(base)}
    for state in ("46", "42", "45", "unknown", "unavailable", "", "0x10", "1_000", "NaN"):
        case = deepcopy(base)
        case["states"]["sensor.temperature"]["state"] = state
        result["temperature_" + (state or "empty")] = case
    for name in ("loading", "error"):
        case = deepcopy(base)
        case["external"].pop("registry")
        case["external"]["readiness"]["registry"] = name
        case["registry"] = {}
        result["registry_" + name] = case
    case = deepcopy(base)
    case["external"]["readiness"]["registry"] = "error"
    result["cached_registry_error"] = case
    case = deepcopy(base)
    case["language"] = "en"
    result["english"] = case
    case = deepcopy(base)
    case["now"] += 6 * 60000
    result["grace_elapsed"] = case
    case = deepcopy(base)
    case["registry"]["update.example"]["disabled_by"] = "user"
    result["disabled"] = case
    case = deepcopy(base)
    case["states"]["update.example"]["attributes"]["restored"] = True
    result["restored"] = case
    case = deepcopy(base)
    case["states"]["input_boolean.plant_task"]["state"] = "off"
    result["unlinked_plant_channel"] = case
    case = deepcopy(base)
    case["policy"]["rules"].append(
        {
            "id": "offline",
            "entity": "sensor.flash",
            "state": "unknown",
            "message": "Offline",
            "for_minutes": 5,
        }
    )
    result["explicit_missing"] = case
    case = deepcopy(base)
    case["policy"]["rules"][-1]["unavailable_after_minutes"] = 5
    result["missing_escalation"] = case
    for lang in ("hu", "en"):
        for value in ("1000", "10000", "-0", "12345.67"):
            case = deepcopy(base)
            case["language"] = lang
            case["states"]["sensor.disk_usage"]["state"] = value
            result[f"format_{lang}_{value}"] = case
    for readiness in ("loading", "error"):
        case = deepcopy(base)
        case["external"]["readiness"]["notifications"] = readiness
        case["external"].pop("notifications")
        if readiness == "error":
            case["external"]["errors"] = {"notifications": "Read failed"}
        result[f"adapter_{readiness}"] = case
    household = deepcopy(base)
    source = {
        "entity_id": "sensor.household",
        "state": "2",
        "last_changed": changed,
        "attributes": {
            "schema_version": 1,
            "revision": 2,
            "rules": [
                {
                    "id": "flash",
                    "title": "No SMART",
                    "explanation": "Unsupported capability",
                    "effect": "expected_unavailable",
                    "targets": ["sensor.flash"],
                    "target_states": ["unknown"],
                    "when": [],
                    "enabled": True,
                    "status": "approved",
                }
            ],
        },
    }
    household["states"]["sensor.household"] = source
    household["policy"]["household"] = {
        "entity": "sensor.household",
        "evaluate_script": "script.household_read",
        "path": "/example/context",
    }
    household["external"]["household"] = {
        "schema_version": 1,
        "revision": 2,
        "status": "ready",
        "evaluated_at": NOW / 1000,
        "expected_unavailable": ["sensor.flash"],
        "evaluations": [
            {
                "id": "flash",
                "title": "No SMART",
                "explanation": "Unsupported capability",
                "effect": "expected_unavailable",
                "status": "applies",
                "matched_entities": ["sensor.flash"],
                "observations": [
                    {
                        "entity": "sensor.flash",
                        "state": "unknown",
                        "changed_at": (NOW - 600000) / 1000,
                    }
                ],
                "valid_until": NOW / 1000 + 90,
            }
        ],
    }
    household["external"]["householdStatus"] = "ready"
    result["household_expected"] = deepcopy(household)
    case = deepcopy(household)
    case["states"]["sensor.flash"]["state"] = "on"
    result["fault_wins"] = case
    case = deepcopy(household)
    case["now"] += 91000
    result["household_expired"] = case
    case = deepcopy(household)
    case["external"].pop("household")
    case["external"]["householdStatus"] = "loading"
    result["household_loading"] = case
    return result


if __name__ == "__main__":
    import json

    print(json.dumps(cases(), ensure_ascii=False))
