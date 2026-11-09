"""Validated, integration-owned JSON policies. No executable templates or actions."""

import math
import re
from copy import deepcopy

from .models import canonical

RULE_FIELDS = set(
    """id entity entity_pattern entity_patterns exclude related_suffix device_classes label
    platform platforms exclude_platforms domain unit message tone navigation_path for_minutes
    aggregate ack_scope dismissible scope category identity unavailable_after_minutes
    notification_ids completion state_labels context above below state state_not""".split()
)
POLICY_FIELDS = set(
    """version revision acknowledgements_entity diagnostics_path rules household ignored
    ignore_labels platforms problems updates adapters""".split()
)


def validate_policy(value):
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("rules"), list)
    ):
        raise ValueError("Unsupported attention policy")
    if set(value) - POLICY_FIELDS or not isinstance(value.get("revision"), str):
        raise ValueError("Unsupported policy fields or revision")
    if len(canonical(value).encode()) > 1048576 or len(value["rules"]) > 2000:
        raise ValueError("Policy too large")
    identifiers, channels = set(), set()
    for key in ("ignored", "platforms", "problems", "updates", "adapters"):
        if key in value and not isinstance(value[key], dict):
            raise ValueError("Invalid policy object")
    for key in ("ignore_labels",):
        if key in value and (
            not isinstance(value[key], list) or any(not isinstance(v, str) for v in value[key])
        ):
            raise ValueError("Invalid label list")
    for key in ("events", "notification_exclude"):
        items = value.get("adapters", {}).get(key, [])
        if not isinstance(items, list) or any(not isinstance(v, str) for v in items):
            raise ValueError("Invalid adapter sources")
    for rule in value["rules"]:
        if not isinstance(rule, dict) or set(rule) - RULE_FIELDS:
            raise ValueError("Unsupported rule fields")
        identifier = rule.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("Missing or duplicate rule ID")
        identifiers.add(identifier)
        for key in (
            "entity",
            "entity_pattern",
            "platform",
            "domain",
            "label",
            "unit",
            "navigation_path",
        ):
            if key in rule and not isinstance(rule[key], str):
                raise ValueError("Invalid rule selector")
        for key in ("state", "state_not"):
            if key in rule and not (
                isinstance(rule[key], str)
                or isinstance(rule[key], list)
                and all(isinstance(v, str) for v in rule[key])
            ):
                raise ValueError("Invalid state condition")
        if "state_labels" in rule and (
            not isinstance(rule["state_labels"], dict)
            or any(not isinstance(v, str) for v in rule["state_labels"].values())
        ):
            raise ValueError("Invalid state labels")
        if "context" in rule and not isinstance(rule["context"], list):
            raise ValueError("Invalid context list")
        if not isinstance(rule.get("message"), str) or not any(
            rule.get(k)
            for k in (
                "entity",
                "entity_pattern",
                "entity_patterns",
                "device_classes",
                "platform",
                "platforms",
                "domain",
                "label",
            )
        ):
            raise ValueError("Rule needs a selector and message")
        for key in ("above", "below", "for_minutes", "unavailable_after_minutes"):
            if key in rule and (
                type(rule[key]) not in (int, float)
                or not math.isfinite(rule[key])
                or key.endswith("minutes")
                and rule[key] < 0
            ):
                raise ValueError("Invalid numeric condition")
        if "above" in rule and "below" in rule and rule["above"] >= rule["below"]:
            raise ValueError("Invalid numeric range")
        if rule.get("identity", "state") not in ("state", "condition", "version"):
            raise ValueError("Invalid identity")
        for key in (
            "entity_patterns",
            "exclude",
            "device_classes",
            "platforms",
            "exclude_platforms",
            "notification_ids",
        ):
            if key in rule and (
                not isinstance(rule[key], list)
                or any(not isinstance(v, str) or not v.strip() for v in rule[key])
            ):
                raise ValueError("Invalid selector list")
        for channel in rule.get("notification_ids", []):
            if not rule.get("entity") or channel in channels:
                raise ValueError("Ambiguous notification channel")
            channels.add(channel)
        if "completion" in rule:
            completion = rule["completion"]
            if (
                not rule.get("notification_ids")
                or not isinstance(completion, dict)
                or not completion.get("label")
                or not completion.get("confirmation")
                or completion.get("action", {}).get("action")
                not in ("perform-action", "call-service")
            ):
                raise ValueError("Invalid UI completion binding")
        for item in rule.get("context", []):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("label"), str)
                or sum(k in item for k in ("entity", "entity_pattern", "value")) != 1
                or "decimals" in item
                and (type(item["decimals"]) is not int or not 0 <= item["decimals"] <= 6)
            ):
                raise ValueError("Invalid context")
    config = value.get("household")
    if config and (
        not isinstance(config, dict)
        or not re.fullmatch(r"script\.[a-z0-9_]+", config.get("evaluate_script", ""))
        or not re.fullmatch(r"sensor\.[a-z0-9_]+", config.get("entity", ""))
    ):
        raise ValueError("Invalid household read binding")
    return deepcopy(value)


def presentation(policy):
    return {
        **{
            k: v
            for k, v in policy.items()
            if k not in ("rules", "ignored", "ignore_labels", "platforms", "problems", "adapters")
        },
        "rules": [
            {k: v for k, v in rule.items() if k in ("id", "aggregate", "message")}
            for rule in policy["rules"]
        ],
    }
