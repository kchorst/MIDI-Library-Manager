from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.remix_profile import KEY_TO_PC
from midi_library_scan.models import QuickScanResult


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    """Compact metadata shape consumed by CandidateIndex.

    Keeping this separate from MidiProfile avoids materializing tens of thousands
    of heavyweight deep-analysis objects in the Library Engine merely to narrow
    search neighborhoods.
    """

    path: str
    title: str
    composer: str
    song_identity: str
    fingerprint: str
    content_fingerprint: str
    musical_fingerprint: str
    felt_bpm: float | None
    tonic_pc: int | None
    mode: str
    meter_family: str
    meter_display: str
    progression_family: str
    style: str
    melody_track_index: int | None
    accompaniment_track_index: int | None
    orchestration_readiness: int
    key_confidence: int
    note_count: int
    track_count: int
    bars: float


def _tonic_pc(key: str | None) -> int | None:
    if not key:
        return None
    text = str(key).strip()
    if text.endswith("m"):
        text = text[:-1]
    return KEY_TO_PC.get(text)


def summary_from_quick(result: QuickScanResult) -> CandidateSummary:
    bpm = float(result.bpm) if result.bpm else None
    duration = max(0.0, float(result.duration_seconds or 0.0))
    meter = str(result.meter or "4/4")
    try:
        numerator, denominator = [max(1, int(part)) for part in meter.split("/", 1)]
    except (TypeError, ValueError):
        numerator, denominator = 4, 4
    beats_per_bar = numerator * (4.0 / denominator)
    bars = duration * (bpm or 120.0) / 60.0 / max(1.0, beats_per_bar) if duration else 0.0
    arrangement = str(result.arrangement_type or "").casefold()
    readiness = 45 if "multi" in arrangement else 28 if "keyboard" in arrangement or "piano" in arrangement else 10
    match = result.catalog_match
    title = str(match.title or "").strip() or Path(result.path).stem
    composer = str(match.artist or "").strip()
    song_identity = str(match.composition_id or result.composition_group or "").strip()
    return CandidateSummary(
        path=result.path,
        title=title,
        composer=composer,
        song_identity=song_identity,
        fingerprint=(result.sha256 or result.musical_fingerprint or "")[:32],
        content_fingerprint=result.sha256 or "",
        musical_fingerprint=result.musical_fingerprint or "",
        felt_bpm=bpm,
        tonic_pc=_tonic_pc(result.key),
        mode=str(result.mode or "major"),
        meter_family=meter,
        meter_display=meter,
        progression_family="unclassified",
        style=str(match.primary_genre or "Unclassified"),
        melody_track_index=None,
        accompaniment_track_index=None,
        orchestration_readiness=readiness,
        key_confidence=max(0, min(100, int(round(float(result.key_confidence or 0.0) * 100)))),
        note_count=max(0, int(result.note_count or 0)),
        track_count=max(0, int(result.track_count or 0)),
        bars=bars,
    )
