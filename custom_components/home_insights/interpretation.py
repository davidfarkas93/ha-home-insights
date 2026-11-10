"""A native output schema limits AI to prioritizing verified measurement statements."""

from itertools import permutations

import probatio as vol


def summary_options(evidence, priority):
    options = []
    for length in (1, 2):
        for selected in permutations(evidence, length):
            refs = [e["ref"] for e in selected]
            if priority and (
                refs[0] not in priority or len(set(refs) & priority) < min(2, len(priority))
            ):
                continue
            options.append({"text": " ".join(e["statement"] for e in selected), "refs": refs})
    return options


def output_schema(bundle):
    return vol.Schema(
        {vol.Required("summary"): vol.In([o["text"] for o in bundle["summary_options"]])}
    )


def validate_selection(value, bundle, partial_prefix):
    try:
        result = output_schema(bundle)(value)
    except (vol.Invalid, TypeError) as error:
        raise ValueError("Invalid measured interpretation") from error
    selected = next(o for o in bundle["summary_options"] if o["text"] == result["summary"])
    check = next(
        (c["text"] for c in bundle["permitted_checks"] if c["ref"] in selected["refs"]), ""
    )
    if bundle["data_quality"] != "complete":
        result["summary"] = partial_prefix + result["summary"]
    return {**result, "hypothesis": "", "suggested_check": check, "evidence_refs": selected["refs"]}
