from __future__ import annotations

"""Structured human listening evidence for generated Takes.

Automated MIDI checks can reject broken output, but they cannot prove artistic
quality. These records keep the exact Take identity, a stable issue code, and a
useful regeneration direction without pretending that feedback is an automatic
score.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ListeningIssue:
    code: str
    label: str
    category: str
    recommended_action: str


LISTENING_ISSUES: tuple[ListeningIssue, ...] = (
    ListeningIssue("timing-fast", "Too fast", "timing", "Lower the target pulse and regenerate the affected branch."),
    ListeningIssue("timing-slow", "Too slow", "timing", "Raise the target pulse and regenerate the affected branch."),
    ListeningIssue("timing-alignment", "Entrances or downbeats do not line up", "timing", "Review source start bars, phrase grid, and section length."),
    ListeningIssue("harmony-clash", "Melody clashes with the harmony", "harmony", "Review direction/key treatment or use a section-safe arrangement."),
    ListeningIssue("melody-missing", "The main melody is missing or unclear", "source-roles", "Review melody detection and source-role mapping."),
    ListeningIssue("balance-backing-loud", "The backing is too loud", "balance", "Enable automatic balance or lower foundation/support levels."),
    ListeningIssue("balance-lead-loud", "The lead is too loud", "balance", "Lower the lead level or restore more supporting texture."),
    ListeningIssue("crowded", "Too crowded or masked", "balance", "Use Lean/Balanced density or reduce simultaneous lead material."),
    ListeningIssue("repetitive", "Too repetitive", "structure", "Change section length, entry points, or arrangement recipe."),
    ListeningIssue("source-roles", "The songs have the wrong roles", "source-roles", "Review melody/foundation/section assignments."),
    ListeningIssue("instrumentation", "Wrong instrumentation or timbre", "instrumentation", "Open instrumentation controls for the exact Take."),
    ListeningIssue("transition", "A transition or ending is weak", "structure", "Review section boundaries, handoffs, and ending treatment."),
    ListeningIssue("identity", "One song loses its identity", "structure", "Preserve a signature passage or use longer complete sections."),
    ListeningIssue("different-pairing", "Try a different song combination", "pairing", "Return to recommendations while preserving the current Trial."),
)

ISSUE_BY_CODE = {issue.code: issue for issue in LISTENING_ISSUES}
ISSUE_CODE_BY_LABEL = {issue.label: issue.code for issue in LISTENING_ISSUES}


def issue_for(value: str) -> ListeningIssue:
    key = str(value or "").strip()
    code = ISSUE_CODE_BY_LABEL.get(key, key)
    return ISSUE_BY_CODE.get(code, ListeningIssue(code or "other", key or "Other listening issue", "other", "Review the relevant decisions and regenerate a new Trial."))


def listening_issue_labels(*, include_pairing: bool = True) -> tuple[str, ...]:
    return tuple(issue.label for issue in LISTENING_ISSUES if include_pairing or issue.code != "different-pairing")


@dataclass(frozen=True)
class ListeningReview:
    take_id: str
    trial_id: str = ""
    take_path: str = ""
    verdict: str = "needs-revision"
    issues: tuple[str, ...] = ()
    category: str = ""
    recommended_action: str = ""
    branch_signature: str = ""
    notes: str = ""
    created_at: str = ""

    @classmethod
    def create(
        cls,
        take_id: str,
        *,
        trial_id: str = "",
        take_path: str = "",
        verdict: str = "needs-revision",
        issues: Iterable[str] = (),
        category: str = "",
        recommended_action: str = "",
        branch_signature: str = "",
        notes: str = "",
    ) -> "ListeningReview":
        normalized = tuple(issue_for(str(value)).code for value in issues if str(value).strip())
        primary = issue_for(normalized[0]) if normalized else None
        return cls(
            take_id=str(take_id),
            trial_id=str(trial_id or ""),
            take_path=str(take_path or ""),
            verdict=str(verdict or "needs-revision"),
            issues=normalized,
            category=str(category or (primary.category if primary else "")),
            recommended_action=str(recommended_action or (primary.recommended_action if primary else "")),
            branch_signature=str(branch_signature or ""),
            notes=str(notes or "").strip(),
            created_at=_now(),
        )

    @property
    def exact_identity(self) -> tuple[str, str, str]:
        return (self.trial_id, self.take_id, self.take_path)

    @property
    def issue_labels(self) -> tuple[str, ...]:
        return tuple(issue_for(code).label for code in self.issues)

    def to_dict(self) -> dict:
        return asdict(self)
