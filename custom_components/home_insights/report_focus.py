"""Source grouping and factual report focus before the bounded AI projection."""

from collections import defaultdict


def group_sources(episodes):
    groups = defaultdict(list)
    for item in episodes:
        groups[(item.get("category"), item.get("source") or item.get("id"))].append(item)
    result = []
    for items in groups.values():
        # Canonical episodes remain untouched; a representative and grouping context
        # cannot invent downtime by summing overlapping or unobserved intervals.
        open_items = [e for e in items if not e.get("closed_at")]
        representative = max(open_items or items, key=lambda e: e.get("first_observed_at") or "")
        maximum = max((e.get("confirmed_active_seconds") or 0 for e in items), default=0)
        resolved = not open_items and all(
            e.get("close_reason") == "condition_cleared" for e in items
        )
        latest_cleared = (
            not open_items and representative.get("close_reason") == "condition_cleared"
        )

        def is_snoozed(e):
            handling = e.get("handling_at_period_end", {})
            until = (
                handling.get("snoozed_until")
                if handling.get("snooze_override")
                else e.get("source_snoozed_until")
            )
            return bool(until and until >= e.get("period_end", 0))

        snoozed = bool(open_items) and all(is_snoozed(e) for e in open_items)
        event_record = representative.get("category") == "events"
        notification = representative.get("kind") == "notification"
        delivery = representative.get("delivery_handling", {}).get("status", "")
        removed_notice = (
            notification
            and not open_items
            and representative.get("close_reason") in ("notification_removed", "superseded")
        )
        retired_repair = representative.get("kind") == "repair" and not open_items
        delivery = delivery or ("removed" if removed_notice else "")
        handled = delivery in ("dismissed_by_user", "redelivery_suppressed")
        previously_observed = any(
            e.get("history", {}).get("previously_observed") or e.get("present_at_period_start")
            for e in items
        )
        end_state_known = all(
            e.get("evidence_status") not in ("unavailable", "loading", "pending")
            for e in open_items
        )
        requires_check = (
            representative.get("category") == "issues"
            and bool(open_items)
            and not latest_cleared
            and not event_record
            and not handled
            and not removed_notice
            and not retired_repair
            and not (snoozed and representative.get("tone") != "error")
        )
        tone = (
            representative.get("tone")
            if event_record
            else (
                "error"
                if any(e.get("tone") == "error" for e in items)
                else representative.get("tone")
            )
        )
        score = (100 if tone == "error" else 40) + (30 if open_items else 0)
        score += 30 if maximum >= 300 else 10 if maximum >= 120 else 0
        score += (
            15
            if any(not e.get("initial") and not e.get("present_at_period_start") for e in items)
            else 0
        )
        score -= 50 if resolved and maximum < 120 else 0
        score -= 70 if snoozed else 0
        score -= 200 if representative.get("category") == "updates" else 0
        score -= 80 if event_record and tone != "error" else 0
        score += 200 if requires_check else 0
        score -= 160 if handled or removed_notice or retired_repair else 0
        result.append(
            {
                **representative,
                "tone": tone,
                "group": {
                    "observations": len(items),
                    "open_at_end": bool(open_items),
                    "observed_clearance": resolved,
                    "latest_observed_clearance": latest_cleared,
                    "snoozed_at_end": snoozed,
                    "event_record_only": event_record,
                    "notification_only": notification,
                    "retired_repair": retired_repair,
                    "delivery_status": delivery,
                    "previously_observed": bool(previously_observed),
                    "end_state_known": end_state_known,
                    "claimed_fixed": any(
                        e.get("handling_at_period_end", {}).get("claimed_fixed_at")
                        for e in open_items
                    ),
                    "requires_check": requires_check,
                    "maximum_confirmed_active_seconds": maximum,
                },
                "focus_score": score,
            }
        )
    return sorted(result, key=lambda e: (-e["focus_score"], e.get("source") or ""))


def headline(evidence):
    item = next((e for e in evidence if e.get("group")), None)
    if not item:
        return None
    name, group = item["name"], item["group"]
    if group.get("event_record_only"):
        return f"{name}: az utolsó rögzített értesítés részletei visszanézhetők."
    if group.get("delivery_status") == "redelivery_suppressed":
        return (
            f"{name}: a korábban elvetett értesítés változatlan újraküldését elrejtette a rendszer."
        )
    if group.get("delivery_status") == "dismissed_by_user":
        return f"{name}: az értesítést elvetetted. Ez nem igazolja a mögöttes állapot rendeződését."
    if group.get("notification_only"):
        if group["open_at_end"]:
            return (
                f"{name}: az értesítés nap végén még elérhető volt; "
                "önmagában nem igazol fennálló hibát."
            )
        return f"{name}: értesítés érkezett; nap végén már nem szerepelt az értesítési listában."
    if group.get("retired_repair"):
        return f"{name}: a Home Assistant nap végén már nem jelentette ezt a teendőt."
    if group["open_at_end"] and not group.get("end_state_known", True):
        return f"{name}: korábban aktív jelzést láttam; a nap végi állapot nem volt ellenőrizhető."
    if group["snoozed_at_end"]:
        return f"{name}: a jelzés nap végén is fennállt; a megjelenítését szüneteltetted."
    if group["open_at_end"]:
        if group.get("claimed_fixed"):
            return f"{name}: javítottnak jelölted, de a forrás jelzése nap végén még aktív volt."
        return (
            f"{name}: a korábbról ismert jelzés nap végén is aktív volt."
            if group.get("previously_observed")
            else f"{name}: a jelzés nap végén is fennállt."
        )
    if group["observed_clearance"]:
        return f"{name}: a felügyeleti jelzés a megfigyelt időszakban rendeződött."
    if group.get("latest_observed_clearance"):
        return (
            f"{name}: a legutóbbi megfigyelt jelzés rendeződött; "
            "a korábbi szakaszok egy része nem követhető végig."
        )
    return f"{name}: a megfigyelt jelzés részletei visszanézhetők; rendeződése nem igazolt."
