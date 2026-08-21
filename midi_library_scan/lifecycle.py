from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# Increment only when validation/rejection semantics materially change.
# Needs Review rows receive at most one deliberate revalidation per ruleset.
VALIDATION_RULESET_VERSION = 1


class LifecycleState(str, Enum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    ANALYZED = "analyzed"
    BLESSED = "blessed"
    REVIEW_REQUIRED = "review_required"
    REVALIDATED = "revalidated"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


class EligibilityState(str, Enum):
    UNRESOLVED = "unresolved"
    MASH_READY = "mash_ready"
    FUSION_CANDIDATE = "fusion_candidate"
    USABLE_NOT_MASH = "usable_not_mash"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


class RejectionCode(str, Enum):
    MALFORMED_TRUNCATED = "malformed_truncated_midi"
    UNSUPPORTED_STRUCTURE = "unsupported_midi_structure"
    NO_USABLE_CONTENT = "no_usable_musical_content"
    IRRECOVERABLE_PARSE = "irrecoverable_parsing_failure"
    SAFETY_RESOURCE_LIMIT = "safety_resource_limit"
    REMIXER_INCOMPATIBLE = "remixer_incompatible"


@dataclass(frozen=True, slots=True)
class FailureReason:
    code: str
    label: str
    deterministic: bool


def _clean(error: str | None) -> str:
    return " ".join(str(error or "").split()).casefold()


def quick_failure_reason(error: str | None) -> FailureReason:
    text = _clean(error)
    if any(token in text for token in ("unexpected end of file", "eoferror", "truncated", "invalid mthd", "invalid midi")):
        return FailureReason(RejectionCode.MALFORMED_TRUNCATED.value, "Malformed/truncated MIDI", True)
    if "type 2" in text and ("asyn" in text or "compute length" in text):
        return FailureReason(RejectionCode.UNSUPPORTED_STRUCTURE.value, "Unsupported MIDI structure", True)
    if any(token in text for token in ("safety limit", "resource limit", "midiresourcelimiterror")):
        return FailureReason(RejectionCode.SAFETY_RESOURCE_LIMIT.value, "Safety/resource limit", True)
    if any(token in text for token in ("no note events", "no usable musical", "empty midi", "no midi tracks")):
        return FailureReason(RejectionCode.NO_USABLE_CONTENT.value, "Empty/no usable musical content", True)
    if any(token in text for token in ("permissionerror", "access is denied", "file not found", "filenotfounderror", "no such file")):
        return FailureReason("source_access_problem", "Source/access problem", False)
    if any(token in text for token in ("attributeerror", "assertionerror", "nameerror", "notimplementederror")):
        return FailureReason("scanner_internal_error", "Scanner/internal error", False)
    return FailureReason(RejectionCode.IRRECOVERABLE_PARSE.value, "Parsing failure", True)


def deep_failure_reason(error: str | None) -> FailureReason:
    text = _clean(error)
    if not text:
        return FailureReason("deep_analysis_unknown", "Needs review", False)
    if "type 2" in text and ("asyn" in text or "compute length" in text):
        return FailureReason(RejectionCode.UNSUPPORTED_STRUCTURE.value, "Unsupported MIDI structure", True)
    if any(token in text for token in ("midiresourcelimiterror", "safety limit", "resource limit")):
        return FailureReason(RejectionCode.SAFETY_RESOURCE_LIMIT.value, "Safety/resource limit", True)
    if any(token in text for token in ("unexpected end of file", "eoferror", "invalid midi", "invalid mthd", "running status", "truncated")):
        return FailureReason(RejectionCode.MALFORMED_TRUNCATED.value, "Malformed/truncated MIDI", True)
    if any(token in text for token in ("no note events", "no usable musical", "empty midi", "no midi tracks")):
        return FailureReason(RejectionCode.NO_USABLE_CONTENT.value, "Empty/no usable musical content", True)
    if any(token in text for token in ("permissionerror", "access is denied", "file not found", "filenotfounderror")):
        return FailureReason("source_access_problem", "Source/access problem", False)
    if any(token in text for token in ("attributeerror", "assertionerror", "nameerror", "notimplementederror")):
        return FailureReason("analyzer_internal_error", "Analyzer/internal error", False)
    return FailureReason("deep_analysis_unknown", "Needs review", False)



def quick_health_reasons(*, track_count: int, note_count: int, duration_seconds: float | None) -> tuple[str, ...]:
    """Structural/shape observations that require resolution before Remixer use."""
    reasons: list[str] = []
    tracks = int(track_count or 0)
    notes = int(note_count or 0)
    duration = float(duration_seconds) if duration_seconds is not None else None
    if tracks <= 0:
        reasons.append("no MIDI tracks")
    if notes <= 0:
        reasons.append("no usable note events")
    elif notes <= 8:
        reasons.append(f"only {notes} note events")
    if duration is not None:
        if duration <= 0.25:
            reasons.append("near-zero duration")
        elif duration < 12.0:
            reasons.append(f"very short ({duration:.1f}s)")
        elif duration > 1800.0:
            reasons.append(f"very long ({duration / 60.0:.1f} min)")
    return tuple(reasons)


def clearly_no_usable_content(*, track_count: int, note_count: int, duration_seconds: float | None) -> bool:
    duration = float(duration_seconds) if duration_seconds is not None else None
    return int(track_count or 0) <= 0 or int(note_count or 0) <= 0 or (duration is not None and duration <= 0.25)

def eligibility_label(value: str | None) -> str:
    labels = {
        EligibilityState.UNRESOLVED.value: "Unresolved",
        EligibilityState.MASH_READY.value: "Mash Ready",
        EligibilityState.FUSION_CANDIDATE.value: "Fusion Candidate",
        EligibilityState.USABLE_NOT_MASH.value: "Not Mash Candidate",
        EligibilityState.NEEDS_REVIEW.value: "Needs Review",
        EligibilityState.REJECTED.value: "Rejected",
        EligibilityState.QUARANTINED.value: "Quarantined",
    }
    return labels.get(str(value or ""), str(value or "Unresolved").replace("_", " ").title())


def recommended_action(*, lifecycle_state: str, eligibility_state: str, rejection_code: str = "") -> str:
    lifecycle = str(lifecycle_state or "")
    eligibility = str(eligibility_state or "")
    reason = str(rejection_code or "")
    if lifecycle == LifecycleState.QUARANTINED.value or eligibility == EligibilityState.QUARANTINED.value:
        return "Restore to the original Library location, inspect it in place, or permanently delete it."
    if lifecycle == LifecycleState.REJECTED.value or eligibility == EligibilityState.REJECTED.value:
        if reason == RejectionCode.UNSUPPORTED_STRUCTURE.value:
            return "Keep outside mash recommendations. Quarantine, inspect externally, or delete if you no longer want the file."
        if reason == RejectionCode.SAFETY_RESOURCE_LIMIT.value:
            return "Keep outside Remixer under current limits. Quarantine or delete only if you intentionally want to clean it up."
        return "Inspect externally if desired, then Quarantine or permanently Delete. A changed/repaired file may be revalidated."
    if eligibility == EligibilityState.NEEDS_REVIEW.value:
        return "Run Revalidate. If the deterministic problem remains, Library will reject it instead of retrying forever."
    if eligibility == EligibilityState.MASH_READY.value:
        return "Blessed for Guided/Assisted recommendations."
    if eligibility == EligibilityState.FUSION_CANDIDATE.value:
        return "Blessed for the Library and routed toward Version Fusion rather than normal mash recommendations."
    if eligibility == EligibilityState.USABLE_NOT_MASH.value:
        return "Keep in the Library; exclude from normal Guided/Assisted mash recommendations."
    return "Continue Fast/Deep analysis until this MIDI reaches a resolved Library state."
