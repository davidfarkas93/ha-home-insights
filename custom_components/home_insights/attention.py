"""Authoritative, side-effect-free Luma attention policy evaluator.

Wire format and state semantics match Luma's v1 contract. Presentation grouping
and user-initiated HA actions remain frontend responsibilities.
"""

import math
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

UNDEFINED = "undefined"


def attention_hash(value: str) -> str:
    raw = value.encode("utf-16-le", errors="surrogatepass")
    hashed = 2166136261
    for i in range(0, len(raw), 2):
        hashed = ((hashed ^ int.from_bytes(raw[i : i + 2], "little")) * 16777619) & 0xFFFFFFFF
    result = ""
    while hashed:
        hashed, digit = divmod(hashed, 36)
        result = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + result
    return result or "0"


def number(value: Any) -> float:
    text = str(value).strip()
    if not text:
        return 0.0
    if re.fullmatch(r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+", text):
        return float(int(text, 0))
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return math.nan
    try:
        return float(text)
    except ValueError:
        return math.nan


def string_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def format_number(value, precision, hu):
    with localcontext() as context:
        context.prec = 330
        quantized = Decimal(str(value)).quantize(
            Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP
        )
    whole, _, fraction = format(quantized, "f").partition(".")
    fraction = fraction.rstrip("0")
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    if len(whole) > (4 if hu else 3):
        groups = []
        while whole:
            groups.append(whole[-3:])
            whole = whole[:-3]
        whole = ("\u00a0" if hu else ",").join(reversed(groups))
    return sign + whole + (("," if hu else ".") + fraction if fraction else "")


def timestamp(value: str | None) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except TypeError, ValueError, AttributeError, OverflowError:
        return math.nan


def glob(pattern: str, value: str) -> bool:
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), value) is not None


def ack_map(raw: str) -> dict[str, float]:
    result = {}
    for record in raw.split(","):
        key, separator, value = record.rpartition(":")
        expiry = number(value)
        if separator and key and math.isfinite(expiry):
            result[key] = expiry
    return result


def available(entity) -> bool:
    return bool(
        entity
        and entity["state"] not in ("", "unknown", "unavailable")
        and entity.get("attributes", {}).get("restored") is not True
    )


def installing(entity) -> bool:
    progress = entity.get("attributes", {}).get("in_progress")
    return progress is True or type(progress) in (int, float) and progress >= 0


def listed(expected, actual: str) -> bool:
    return expected is not None and actual.lower() in [
        str(v).lower() for v in (expected if isinstance(expected, list) else [expected])
    ]


def source_ids(states, registry, rule) -> list[str]:
    if rule.get("entity"):
        candidates = [rule["entity"]]
    elif "entity_patterns" in rule:
        candidates = [s for s in states if any(glob(p, s) for p in rule["entity_patterns"])]
    elif rule.get("entity_pattern"):
        candidates = [s for s in states if glob(rule["entity_pattern"], s)]
    else:
        candidates = list(states)
    result = []
    for source in candidates:
        entity, item = states.get(source), registry.get(source, {})
        if not entity:
            continue
        attrs = entity.get("attributes", {})
        if any(glob(p, source) for p in rule.get("exclude", [])):
            continue
        if rule.get("platform") and item.get("platform") != rule["platform"]:
            continue
        if "platforms" in rule and item.get("platform", "") not in rule["platforms"]:
            continue
        if item.get("platform", "") in rule.get("exclude_platforms", []):
            continue
        if rule.get("domain") and source.split(".")[0] != rule["domain"]:
            continue
        if rule.get("unit") and attrs.get("unit_of_measurement") != rule["unit"]:
            continue
        if rule.get("label") and rule["label"] not in item.get("labels", []):
            continue
        if (
            rule.get("device_classes")
            and attrs.get("device_class", "") not in rule["device_classes"]
        ):
            continue
        result.append(source)
    return result


def matches(entity, rule) -> bool:
    if not entity:
        return False
    if "state" in rule and not listed(rule["state"], entity["state"]):
        return False
    if "state_not" in rule and listed(rule["state_not"], entity["state"]):
        return False
    numeric = number(entity["state"])
    if "above" in rule and (not math.isfinite(numeric) or numeric <= rule["above"]):
        return False
    return not ("below" in rule and (not math.isfinite(numeric) or numeric >= rule["below"]))


def household_decisions(states, config, proof, now):
    source = states.get(config["entity"])
    registry = source.get("attributes", {}) if available(source) else {}
    rules = registry.get("rules", []) if registry.get("schema_version") == 1 else []
    if not proof or proof.get("schema_version") != 1 or proof.get("status") != "ready":
        return rules, [], False
    ready = (
        proof.get("revision") == registry.get("revision")
        and now / 1000 - proof.get("evaluated_at", -math.inf) <= 90
    )
    valid = ready and proof.get("evaluated_at", math.inf) <= now / 1000 + 10
    decisions = []
    for decision in proof.get("evaluations", []) if valid else []:
        rule = next((r for r in rules if r["id"] == decision["id"]), None)
        if (
            decision["status"] != "applies"
            or not rule
            or not rule.get("enabled")
            or rule.get("status") != "approved"
            or not decision.get("observations")
        ):
            continue
        if decision.get("valid_until", math.inf) <= now / 1000:
            continue
        if rule.get("expires_at") and not timestamp(rule["expires_at"]) > now:
            continue
        if all(
            (e := states.get(o["entity"]))
            and not e.get("attributes", {}).get("restored")
            and e["state"] == o["state"]
            and abs(timestamp(e.get("last_changed")) / 1000 - o["changed_at"]) < 0.01
            for o in decision["observations"]
        ):
            decisions.append(decision)
    return rules, decisions, ready


def attention_snapshot(states, registry, policy, external=None, now=0, language="en"):
    external = external or {}
    readiness = external.get("readiness", {})
    registry_ready = (
        "registry" in external or readiness.get("registry") == "ready"
        if readiness.get("registry")
        else bool(registry) or "registry" in external
    )
    if "registry" in external:
        registry = external["registry"]
    rows = []
    ack = ack_map(states.get(policy.get("acknowledgements_entity"), {}).get("state", ""))
    expected = set()
    hu = language.split("-")[0] == "hu"

    def tr(en, hungarian):
        return hungarian if hu else en

    def base(source, rule, scope="home", category="home"):
        return {
            "id": f"{rule}:{source}",
            "key": "",
            "rule": rule,
            "source": source,
            "name": str(
                states.get(source, {}).get("attributes", {}).get("friendly_name") or source
            ),
            "message": "",
            "scope": scope,
            "category": category,
            "kind": "incident",
            "status": "inactive",
            "reason": "condition_not_met",
            "tone": "warning",
            "dismissible": True,
        }

    def add(row, fingerprint):
        row["key"] = "a" + attention_hash(f"{row['rule']}|{row['source']}|{fingerprint}")
        if row["dismissible"] and row["tone"] != "error" and ack.get(row["key"], 0) > now / 1000:
            row["snoozedUntil"] = ack[row["key"]]
        rows.append(row)

    def ignored(source):
        item = registry.get(source, {})
        if policy.get("ignored", {}).get(source):
            return policy["ignored"][source]
        if item.get("disabled") or item.get("disabled_by"):
            return "disabled"
        if states.get(source, {}).get("attributes", {}).get("restored") is True:
            return "restored"
        if any(label in item.get("labels", []) for label in policy.get("ignore_labels", [])):
            return "ignored_label"
        return ""

    if not registry_ready or readiness.get("registry") == "error":
        row = base("registry", "metadata", "system")
        error = readiness.get("registry") == "error"
        row.update(
            status="unavailable" if error else "loading",
            reason="metadata_unavailable" if error else "metadata_loading",
            name=tr("Monitoring sources", "Felügyeleti források"),
            dismissible=False,
            message=tr(
                "Entity information could not be read. Monitoring coverage cannot be confirmed.",
                "Az entitásadatokat nem sikerült beolvasni. "
                "A felügyelet teljessége nem igazolható.",
            )
            if error
            else tr(
                "Entity information is being checked. Monitoring is not yet complete.",
                "Az entitásadatok ellenőrzése folyamatban. A felügyelet még nem teljes.",
            ),
        )
        add(row, "metadata")
        if not registry_ready:
            return rows
    for rule in policy["rules"]:
        sources = [rule["entity"]] if rule.get("entity") else source_ids(states, registry, rule)
        for source in sources:
            expected.add(source)
            suffix = rule.get("related_suffix")
            evaluated = source.replace(suffix["from"], suffix["to"], 1) if suffix else source
            entity = states.get(evaluated)
            row = base(source, rule["id"], rule.get("scope", "home"), rule.get("category", "home"))
            if entity:
                row["sourceState"] = entity["state"]
            codes = [
                s.strip()
                for s in re.split(r"[,\n]+", entity["state"] if entity else "")
                if s.strip()
            ]
            value = (
                " · ".join(rule["state_labels"].get(c, c) for c in codes)
                if rule.get("state_labels") is not None
                else entity["state"]
                if entity
                else ""
            )
            row.update(
                tone=rule.get("tone", "warning"), dismissible=rule.get("dismissible") is not False
            )
            if "navigation_path" in rule:
                row["path"] = rule["navigation_path"]
            numeric = number(entity["state"]) if available(entity) else math.nan
            remaining = string_number(max(0.0, 100 - numeric)) if math.isfinite(numeric) else "—"
            row["message"] = (
                rule["message"]
                .replace("{name}", row["name"])
                .replace("{count}", "1")
                .replace("{value}", value or "—")
                .replace("{remaining}", remaining)
            )
            context = []
            for item in rule.get("context", []):
                if item.get("source") and item["source"] != source:
                    continue
                if "states" in item and not any(c in codes for c in item["states"]):
                    continue
                if "value" in item:
                    context.append({"label": item["label"], "value": item["value"]})
                    continue
                device = registry.get(source, {}).get("device_id")
                identifier = item.get("entity") or (
                    next(
                        (
                            s
                            for s in states
                            if registry.get(s, {}).get("device_id") == device
                            and glob(item["entity_pattern"], s)
                        ),
                        None,
                    )
                    if device and item.get("entity_pattern")
                    else None
                )
                metric = states.get(identifier)
                n = number(metric["state"]) if available(metric) else math.nan
                if math.isfinite(n):
                    precision = item.get("decimals", 2)
                    formatted = format_number(n, precision, hu)
                else:
                    formatted = (
                        metric["state"] if available(metric) else tr("No data", "Nincs adat")
                    )
                unit = (
                    str(metric.get("attributes", {}).get("unit_of_measurement") or "")
                    if available(metric)
                    else ""
                )
                context.append(
                    {
                        "label": item["label"],
                        "value": formatted + (" " + unit if unit else ""),
                        **({"entity": identifier} if identifier else {}),
                    }
                )
            row["context"] = context
            exclusion = ignored(source)
            if exclusion:
                row.update(status="excluded", reason=exclusion)
            else:
                blank_allowed = listed(rule.get("state"), "") or listed(rule.get("state_not"), "")
                no_data = (
                    not entity
                    or entity["state"] in ("unknown", "unavailable")
                    or entity.get("attributes", {}).get("restored") is True
                    or entity["state"] == ""
                    and not blank_allowed
                )
                explicit_missing = (
                    no_data
                    and "state" in rule
                    and listed(rule["state"], entity["state"] if entity else "")
                )
                if no_data and not explicit_missing:
                    row.update(status="unavailable", reason="source_unavailable")
                    age = now - timestamp(entity.get("last_changed")) if entity else 0
                    if (
                        "unavailable_after_minutes" in rule
                        and age >= rule["unavailable_after_minutes"] * 60000
                    ):
                        row.update(
                            status="active",
                            message=tr(
                                f"{row['name']}: no fresh state",
                                f"{row['name']}: nincs friss állapotadat",
                            ),
                        )
                elif matches(entity, rule):
                    age = now - timestamp(entity.get("last_changed"))
                    pending = rule.get("for_minutes") and age < rule["for_minutes"] * 60000
                    row.update(
                        status="pending" if pending else "active",
                        reason="grace_period" if pending else "condition_met",
                    )
            identity = rule.get("identity") or (
                "condition" if "above" in rule or "below" in rule else "state"
            )
            fingerprint = (
                str(entity.get("attributes", {}).get("latest_version") or "")
                if identity == "version" and entity
                else f"{entity.get('state', UNDEFINED)}|{entity.get('last_changed', UNDEFINED)}"
                if identity == "state" and entity
                else "undefined|undefined"
                if identity == "state"
                else ""
                if identity == "version"
                else "threshold"
            )
            add(row, fingerprint)
    for source, entity in states.items():
        platform = registry.get(source, {}).get("platform") or "unknown"
        classification = policy.get("platforms", {}).get(platform, {})
        exclusion = ignored(source)
        updates = policy.get("updates", {})
        if updates.get("enabled") and source.startswith("update."):
            group = next((g for g in updates.get("groups", []) if platform in g["platforms"]), {})
            row = base(
                source,
                "updates",
                classification.get("scope", "homelab"),
                group.get("id", "other_updates"),
            )
            row.update(
                kind="update", message=group.get("name", tr("Other updates", "Egyéb frissítések"))
            )
            if "path" in updates:
                row["path"] = updates["path"]
            status = (
                "excluded"
                if exclusion
                else "unavailable"
                if not available(entity)
                else "installing"
                if installing(entity)
                else "active"
                if entity["state"] == "on"
                else "inactive"
            )
            row.update(
                status=status,
                reason=exclusion
                or {
                    "unavailable": "source_unavailable",
                    "active": "update_available",
                    "installing": "installing",
                }.get(status, "up_to_date"),
            )
            add(row, str(entity.get("attributes", {}).get("latest_version") or "unknown-version"))
        elif (
            policy.get("problems", {}).get("enabled")
            and source.startswith("binary_sensor.")
            and entity.get("attributes", {}).get("device_class") in ("problem", "safety", "tamper")
            and source not in expected
        ):
            row = base(source, "problems", classification.get("scope", "home"), platform)
            if "path" in classification:
                row["path"] = classification["path"]
            exclusion = exclusion or (
                "policy_exception"
                if any(glob(p, source) for p in policy["problems"].get("exclude", []))
                else ""
            )
            status = (
                "excluded"
                if exclusion
                else "unavailable"
                if not available(entity)
                else "active"
                if entity["state"] == "on"
                else "inactive"
            )
            row.update(
                message=row["name"],
                tone="error",
                dismissible=False,
                status=status,
                reason=exclusion
                or ("source_unavailable" if status == "unavailable" else "problem_signal"),
            )
            add(row, f"{entity['state']}|{entity.get('last_changed', UNDEFINED)}")
    adapters = policy.get("adapters", {})
    linked = set()
    for rule in policy["rules"]:
        if not adapters.get("notifications") or not rule.get("notification_ids"):
            continue
        row = next(
            (r for r in rows if r["rule"] == rule["id"] and r["source"] == rule["entity"]), None
        )
        if not row or row["status"] not in ("active", "pending"):
            continue
        if "completion" in rule:
            row["completion"] = rule["completion"]
        row["relatedNotifications"] = [
            {"id": n["notification_id"], "title": n["title"], "message": n["message"]}
            for n in external.get("notifications", [])
            if n["notification_id"] in rule["notification_ids"]
        ]
        for item in row["relatedNotifications"]:
            linked.add(item["id"])
            row.setdefault("context", []).append({"label": item["title"], "value": item["message"]})
    for notification in external.get("notifications", []) if adapters.get("notifications") else []:
        identifier = notification["notification_id"]
        if identifier in linked or any(
            glob(p, identifier) for p in adapters.get("notification_exclude", [])
        ):
            continue
        row = base("notification:" + identifier, "persistent_notifications")
        row.update(
            kind="notification",
            name=notification.get("title")
            or tr("Home Assistant notification", "Home Assistant értesítés"),
            message=notification["message"],
            status="active",
            reason="persistent_notification",
            notificationId=identifier,
            dismissible=False,
        )
        if policy.get("diagnostics_path"):
            row["path"] = policy["diagnostics_path"]
        add(row, f"{notification.get('created_at') or ''}|{notification['message']}")
    for issue in external.get("repairs", {}).get("issues", []) if adapters.get("repairs") else []:
        if issue.get("dismissed_version"):
            continue
        row = base(f"repair:{issue['domain']}:{issue['issue_id']}", "repairs", "homelab")
        prefix = (
            f"component.{issue['domain']}.issues."
            f"{issue.get('translation_key') or issue['issue_id']}"
        )
        translations = external.get("repairTranslations", {})

        replacements = issue.get("translation_placeholders") or {}

        def interpolate(text, values=replacements):
            return re.sub(r"\{([^{}]+)\}", lambda m: str(values.get(m[1], m[0])), text)

        description = translations.get(prefix + ".description") or next(
            (
                v
                for k, v in translations.items()
                if k.startswith(prefix + ".fix_flow.step.") and k.endswith(".description")
            ),
            tr(
                "Home Assistant reports an open repair. "
                "Open Repairs for the integration's explanation.",
                "A Home Assistant nyitott javítási feladatot jelez. "
                "Az integráció magyarázata a Javítások oldalon érhető el.",
            ),
        )
        row.update(
            kind="repair",
            name=interpolate(
                translations.get(prefix + ".title")
                or tr(f"{issue['domain']}: repair needed", f"{issue['domain']}: javítási feladat")
            ),
            message=interpolate(description),
            sourceState=issue.get("translation_key") or issue["issue_id"],
            context=[
                {"label": k, "value": v}
                for k, v in (issue.get("translation_placeholders") or {}).items()
            ],
            status="active",
            reason="repair_issue",
            path="/config/repairs",
            tone="error" if issue.get("severity") in ("error", "critical") else "warning",
        )
        add(row, issue["issue_id"])
    for integration in external.get("integrations", []) if adapters.get("integrations") else []:
        if integration.get("disabled_by") or integration["state"] not in (
            "setup_retry",
            "setup_error",
            "migration_error",
            "failed_unload",
        ):
            continue
        row = base(
            "integration:" + integration["entry_id"],
            "integrations",
            "homelab",
            integration["domain"],
        )
        row.update(
            kind="integration",
            name=integration.get("title") or integration["domain"],
            status="active",
            reason="integration_error",
            path="/config/integrations",
            dismissible=False,
        )
        row["message"] = f"{row['name']}: {integration['state']}"
        add(row, integration["state"])
    for source in adapters.get("events", []):
        entity = states.get(source)
        attrs = entity.get("attributes", {}) if entity else {}
        row = base(source, "events", "homelab")
        row.update(
            kind="event",
            status="event" if available(entity) else "unavailable",
            reason="latest_event_only",
            name=str(attrs.get("title") or row["name"]),
            message=str(attrs.get("subject") or attrs.get("description") or "—"),
            tone="error"
            if str(attrs.get("importance", "")).strip().lower() == "alert"
            else "warning",
        )
        add(row, entity["state"] if entity else "")
    for adapter in ("notifications", "repairs", "integrations"):
        if not adapters.get(adapter):
            continue
        name = {
            "notifications": tr("Home Assistant notifications", "Home Assistant értesítések"),
            "repairs": tr("Home Assistant Repairs", "Home Assistant javítási feladatok"),
            "integrations": tr("Integrations", "Integrációk"),
        }[adapter]
        if readiness.get(adapter) == "loading":
            row = base("adapter:" + adapter, "adapter_health", "system")
            row.update(
                name=name,
                status="loading",
                reason="adapter_loading",
                dismissible=False,
                message=tr(
                    "Checking Home Assistant data sources…",
                    "Home Assistant adatforrások ellenőrzése…",
                ),
            )
            add(row, adapter)
        if adapter in external.get("errors", {}):
            row = base("adapter:" + adapter, "adapter_health", "homelab")
            row.update(
                name=name,
                message=external["errors"][adapter],
                status="unavailable",
                reason="adapter_unavailable",
                dismissible=False,
            )
            add(row, adapter)
    if policy.get("household"):
        config, proof = policy["household"], external.get("household")
        rules, decisions, ready = household_decisions(states, config, proof, now)
        if not ready:
            row = base(config["entity"], "household_health", "system")
            error = (
                external.get("householdStatus") == "error"
                or proof
                and proof.get("status") != "ready"
            )
            row.update(
                name=tr("Household knowledge", "Háztartási háttértudás"),
                dismissible=False,
                status="unavailable" if error else "loading",
                reason="household_unavailable" if error else "household_loading",
                message=tr(
                    "Household rules could not yet be verified.",
                    "A háztartási szabályok érvényessége még nem ellenőrizhető.",
                ),
            )
            if config.get("path"):
                row["path"] = config["path"]
            add(row, "household")
        for row in rows:
            matching = [d for d in decisions if row["source"] in d["matched_entities"]]
            if matching:
                row.setdefault("context", []).extend(
                    {
                        "label": d["title"],
                        "value": d["explanation"]
                        + (" · " + d["limitation"] if d.get("limitation") else ""),
                    }
                    for d in matching
                )
            expected_missing = next(
                (d for d in matching if d["effect"] == "expected_unavailable"), None
            )
            if (
                row["status"] == "unavailable"
                and expected_missing
                and row["source"] in proof.get("expected_unavailable", [])
                and states.get(row["source"], {}).get("state") in ("unknown", "unavailable")
            ):
                row.update(
                    status="excluded",
                    reason="household_expected",
                    message=expected_missing["explanation"],
                )
                if config.get("path"):
                    row["path"] = config["path"]
            elif (
                row["status"] == "unavailable"
                and not ready
                and external.get("householdStatus") != "error"
                and any(
                    r.get("status") == "approved"
                    and r.get("enabled")
                    and r.get("effect") == "expected_unavailable"
                    and row["source"] in r["targets"]
                    for r in rules
                )
            ):
                row.update(status="loading", reason="household_loading")
    winners = {}
    for row in rows:
        if row["status"] == "active":
            key = (row["kind"], row["source"])
            old = winners.get(key)
            if old is None or row["tone"] == "error" and old["tone"] != "error":
                winners[key] = row
    return [
        {**r, "status": "excluded", "reason": "covered_by_stronger_rule"}
        if r["status"] == "active" and winners[(r["kind"], r["source"])] is not r
        else r
        for r in rows
    ]
