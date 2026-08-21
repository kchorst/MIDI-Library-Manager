from __future__ import annotations

"""Conservative review-only detection for obviously unusual MIDI files.

Nothing in this module deletes or moves source files. It merely identifies
files a human may want to review. Short jingles and long-form scores can be
legitimate, so duration thresholds are intentionally advisory rather than an
automatic quality gate.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SuspiciousMidiCandidate:
    path: str
    title: str
    duration_seconds: float
    note_count: int
    track_count: int
    reasons: tuple[str, ...]
    severity: int
    category: str = "Suspicious but loadable"
    error: str = ""


def suspicious_midi_candidates(
    profiles: Iterable[object],
    *,
    short_seconds: float = 12.0,
    long_seconds: float = 30.0 * 60.0,
    tiny_note_count: int = 8,
    limit: int = 5000,
) -> list[SuspiciousMidiCandidate]:
    """Return unusual files for explicit human review, never auto-deletion."""
    short_limit = max(0.1, float(short_seconds))
    long_limit = max(short_limit + 1.0, float(long_seconds))
    note_limit = max(0, int(tiny_note_count))
    rows: list[SuspiciousMidiCandidate] = []
    for profile in profiles:
        path = str(getattr(profile, "path", "") or "").strip()
        if not path:
            continue
        try:
            duration = max(0.0, float(getattr(profile, "duration_seconds", 0.0) or 0.0))
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
        try:
            notes = max(0, int(getattr(profile, "note_count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            notes = 0
        try:
            tracks = max(0, int(getattr(profile, "track_count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            tracks = 0

        reasons: list[str] = []
        severity = 0
        if tracks <= 0:
            reasons.append("no usable MIDI tracks reported")
            severity = max(severity, 100)
        if notes <= 0:
            reasons.append("no note events reported")
            severity = max(severity, 100)
        elif notes <= note_limit:
            reasons.append(f"only {notes} note event{'s' if notes != 1 else ''}")
            severity = max(severity, 80)
        if duration <= 0.25:
            reasons.append("zero or near-zero duration")
            severity = max(severity, 100)
        elif duration < short_limit:
            reasons.append(f"very short ({duration:.1f} seconds)")
            severity = max(severity, 60)
        if duration > long_limit:
            reasons.append(f"very long ({duration / 60.0:.1f} minutes)")
            severity = max(severity, 70)
        if not reasons:
            continue
        title = str(getattr(profile, "title", "") or Path(path).stem)
        rows.append(SuspiciousMidiCandidate(path, title, duration, notes, tracks, tuple(reasons), severity))

    rows.sort(key=lambda item: (-item.severity, item.duration_seconds, item.title.casefold(), item.path.casefold()))
    return rows[: max(1, int(limit))]
