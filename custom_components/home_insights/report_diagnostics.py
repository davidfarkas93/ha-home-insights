"""Bounded, private AI rejection evidence; never log prompts or provider secrets."""

import hashlib
import json
import re

REASONS = {
    "Invalid AI output shape": "invalid_shape",
    "Invalid AI output length": "invalid_length",
    "Unknown evidence reference": "unknown_evidence",
    "Reference only the observations actually discussed": "too_many_references",
    "Operational signals require an evidence reference": "missing_evidence",
    "Choose a check bound to an actual evidence source": "unbound_check",
    "Do not cite unrelated evidence": "source_not_named",
    "Describe the observation, not an unverified physical diagnosis": "unqualified_claim",
    "AI must not invent numeric facts": "unverified_number",
    "Explanation requires evidence": "unsupported_explanation",
    "Use the household's informal Hungarian voice": "wrong_voice",
    "Invalid measured interpretation": "invalid_measured_selection",
}


def rejection(error, candidate, bundle):
    detail = str(error)
    reason = REASONS.get(detail, "unexpected_validation_error")
    if reason == "unexpected_validation_error":
        detail = "The response could not be validated; exception text omitted."
    try:
        raw = json.dumps(candidate, ensure_ascii=False, allow_nan=False)
    except ValueError, TypeError:
        raw = "<non-JSON response>"
    # These values were not supplied to AI. Still avoid retaining accidental echoes.
    raw = re.sub(r"https?://[^\s\"<>]+", "[URL omitted]", raw)
    raw = re.sub(r"\bBearer\s+[^\s\"]+|\bsk-[A-Za-z0-9_-]{12,}", "[secret omitted]", raw)
    raw = re.sub(
        r'(?i)("(?:api_key|access_token|password|secret)"\s*:\s*")[^"]*',
        r"\1[secret omitted]",
        raw,
    )
    return {
        "schema_version": 1,
        "stage": "ai_output_validation",
        "reason": reason,
        "detail": detail,
        "candidate_json": raw[:12000],
        "candidate_truncated": len(raw) > 12000,
        "candidate_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "allowed_evidence_refs": [e["ref"] for e in bundle["evidence"]][:100],
        "allowed_checks": bundle.get("permitted_checks", [])[:100],
    }
