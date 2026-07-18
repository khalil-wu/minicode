from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from typing import Any, Iterable, Literal

ClaimVerdict = Literal["accepted", "unverified", "rejected"]


@dataclass(frozen=True)
class EvidenceSource:
    url: str
    title: str = ""
    retrieved_at: str = ""


@dataclass(frozen=True)
class EvidenceClaim:
    subject: str
    field: str
    value: str | int | float
    unit: str = ""
    valid_at: str = ""
    confidence: float = 0.5
    sources: tuple[EvidenceSource, ...] = field(default_factory=tuple)
    verdict: ClaimVerdict = "unverified"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceClaim":
        sources = tuple(
            EvidenceSource(
                url=str(item.get("url") or "").strip(),
                title=str(item.get("title") or "").strip(),
                retrieved_at=str(item.get("retrieved_at") or item.get("retrievedAt") or "").strip(),
            )
            for item in data.get("sources", [])
            if isinstance(item, dict) and str(item.get("url") or "").strip()
        )
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        verdict = str(data.get("verdict") or "unverified")
        if verdict not in {"accepted", "unverified", "rejected"}:
            verdict = "unverified"
        if verdict == "accepted" and not sources:
            verdict = "unverified"
        return cls(
            subject=str(data.get("subject") or "").strip(),
            field=str(data.get("field") or "").strip(),
            value=data.get("value", ""),
            unit=str(data.get("unit") or "").strip(),
            valid_at=str(data.get("valid_at") or data.get("validAt") or "").strip(),
            confidence=confidence,
            sources=sources,
            verdict=verdict,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ClaimConflict:
    subject: str
    field: str
    valid_at: str
    claims: tuple[EvidenceClaim, ...]


def normalize_claim_value(claim: EvidenceClaim) -> str:
    value = claim.value
    if isinstance(value, float):
        value_text = f"{value:.6f}".rstrip("0").rstrip(".")
    else:
        value_text = str(value).strip().casefold()
    unit = claim.unit.strip().casefold().replace("℃", "c").replace("°c", "c")
    return f"{value_text}|{unit}"


def detect_claim_conflicts(claims: Iterable[EvidenceClaim]) -> list[ClaimConflict]:
    groups: dict[tuple[str, str, str], list[EvidenceClaim]] = {}
    for claim in claims:
        if claim.verdict == "rejected" or not claim.subject or not claim.field:
            continue
        key = (claim.subject.casefold(), claim.field.casefold(), _date_key(claim.valid_at))
        groups.setdefault(key, []).append(claim)

    conflicts: list[ClaimConflict] = []
    for (_, _, valid_at), group in groups.items():
        accepted = [item for item in group if item.verdict == "accepted"]
        compared = accepted or group
        if len({normalize_claim_value(item) for item in compared}) <= 1:
            continue
        conflicts.append(ClaimConflict(compared[0].subject, compared[0].field, valid_at, tuple(compared)))
    return conflicts


def build_targeted_retry_prompt(conflict: ClaimConflict) -> str:
    values = ", ".join(f"{claim.value}{claim.unit}" for claim in conflict.claims)
    return (
        f"Verify only the conflicting field '{conflict.field}' for {conflict.subject}"
        f" at {conflict.valid_at or 'the requested date'}. Existing values: {values}. "
        "Use a primary source, return one value with source URL and validity date, and do not redo unrelated fields."
    )


_EVIDENCE_CLAIMS_SECTION_RE = re.compile(
    r"^\s{0,3}#{1,4}\s+Evidence\s+claims\s*$([\s\S]*?)(?=^\s{0,3}#{1,4}\s+|\Z)",
    re.IGNORECASE | re.MULTILINE,
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_evidence_claims(content: str) -> list[EvidenceClaim]:
    """Parse the report contract's structured claims section."""
    section = _EVIDENCE_CLAIMS_SECTION_RE.search(str(content or ""))
    if section is None:
        return []
    body = section.group(1).strip()
    fenced = _JSON_FENCE_RE.search(body)
    payload = fenced.group(1).strip() if fenced else body
    try:
        data = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    claims: list[EvidenceClaim] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            claim = EvidenceClaim.from_dict(item)
        except (TypeError, ValueError):
            continue
        if claim.subject and claim.field:
            claims.append(claim)
    return claims


def evidence_conflict_feedback(contents: Iterable[str]) -> str:
    claims = [claim for content in contents for claim in extract_evidence_claims(content)]
    conflicts = detect_claim_conflicts(claims)
    if not conflicts:
        return ""
    retry_prompts = "\n".join(f"- {build_targeted_retry_prompt(conflict)}" for conflict in conflicts)
    return (
        "Evidence conflict detected. Do not silently choose a value or redo unrelated research. "
        "Delegate only these targeted verification tasks:\n"
        f"{retry_prompts}"
    )


def _date_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text.casefold()
