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
        snoozed = bool(open_items) and all(
            e.get("handling_at_period_end", {}).get("snooze_override") for e in open_items
        )
        event_record = representative.get("category") == "events"
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
        result.append(
            {
                **representative,
                "tone": tone,
                "group": {
                    "observations": len(items),
                    "open_at_end": bool(open_items),
                    "observed_clearance": resolved,
                    "snoozed_at_end": snoozed,
                    "event_record_only": event_record,
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
    if group["snoozed_at_end"]:
        return f"{name}: a jelzés nap végén is fennállt; a megjelenítését szüneteltetted."
    if group["open_at_end"]:
        return f"{name}: a jelzés nap végén is fennállt."
    if group["observed_clearance"]:
        return f"{name}: a felügyeleti jelzés a megfigyelt időszakban rendeződött."
    return f"{name}: a megfigyelt jelzés részletei visszanézhetők; rendeződése nem igazolt."
