from __future__ import annotations

from dataclasses import replace

from core.remix_profile import KEY_TO_PC, MidiProfile
from midi_library_scan.models import QuickScanResult

_PENDING_WARNING = "Deep musical analysis pending"


def _filename_stem(filename: str) -> str:
    # Quick catalogue already stores only the basename. Avoid constructing a
    # pathlib object for every row when hydrating a six-figure catalogue.
    text = str(filename or "")
    dot = text.rfind(".")
    return text[:dot] if dot > 0 else text


def _meter_parts(value: str | None) -> tuple[int, int]:
    try:
        numerator, denominator = str(value or "4/4").split("/", 1)
        return max(1, int(numerator)), max(1, int(denominator))
    except (TypeError, ValueError):
        return 4, 4


def _tonic_pc(key: str | None) -> int | None:
    if not key:
        return None
    text = str(key).strip()
    if text.endswith("m"):
        text = text[:-1]
    return KEY_TO_PC.get(text)


def quick_result_to_profile(result: QuickScanResult) -> MidiProfile:
    """Create a safe, visibly provisional library row from the fast scan.

    The profile deliberately leaves track-role and harmonic-structure fields empty.
    Those values must come from the existing deep analyzer before remix generation.
    """
    numerator, denominator = _meter_parts(result.meter)
    ticks_per_beat = int(result.ticks_per_beat or 480)
    bpm = float(result.bpm) if result.bpm else None
    duration = float(result.duration_seconds or 0.0)
    total_ticks = int(round(duration * (bpm or 120.0) * ticks_per_beat / 60.0)) if duration else 0
    quarter_beats_per_bar = numerator * (4.0 / denominator)
    bars = total_ticks / max(1.0, ticks_per_beat * quarter_beats_per_bar)
    match = result.catalog_match
    title = match.title or _filename_stem(result.filename).replace("_", " ").replace("-", " ").strip()
    composer = match.artist or "Unknown"
    style = match.primary_genre or "Unclassified"
    tonic = _tonic_pc(result.key)
    mode = result.mode or "major"
    status_note = f"Catalog identity: {match.status} ({int(round(match.confidence * 100))}%)"
    warnings = [_PENDING_WARNING, status_note]
    if result.duplicate_group:
        warnings.append(f"Duplicate/version group: {result.duplicate_group}")
    if result.error:
        warnings.append(result.error)

    readiness = 35 if result.arrangement_type == "Multi-instrument" else 18 if result.note_count else 0
    return MidiProfile(
        path=result.path,
        fingerprint=(result.sha256 or "")[:24],
        title=title or result.filename,
        composer=composer,
        style=style,
        midi_type=int(result.midi_type or 0),
        ticks_per_beat=ticks_per_beat,
        track_count=int(result.track_count or 0),
        note_count=int(result.note_count or 0),
        duration_seconds=duration,
        total_ticks=total_ticks,
        tempo_bpm=bpm,
        felt_bpm=bpm,
        tempo_change_count=int(result.tempo_change_count or 0),
        numerator=numerator,
        denominator=denominator,
        meter=result.meter or f"{numerator}/{denominator}",
        meter_family="catalogued",
        bars=bars,
        initial_key_signature=(f"{result.key}m" if result.key and mode == "minor" and not str(result.key).endswith("m") else result.key),
        key_changes=[],
        tonic_pc=tonic,
        mode=mode,
        note_tracks=[],
        melody_track_index=None,
        accompaniment_track_index=None,
        warnings=warnings,
        declared_tonic_pc=tonic,
        declared_mode=mode,
        analyzed_tonic_pc=tonic,
        analyzed_mode=mode,
        key_confidence=int(round(float(result.key_confidence or 0.0) * 100)),
        key_agreement="quick estimate",
        arrangement_type=result.arrangement_type,
        orchestration_readiness=readiness,
        melody_extraction_confidence=0,
        progression_family="Pending deep analysis",
        reference_tags=[tag for tag in (match.primary_genre, match.genres) if tag],
        content_fingerprint=result.sha256,
        song_identity=match.composition_id or result.composition_group or "",
        musical_fingerprint=result.musical_fingerprint or "",
    )


def is_provisional_profile(profile: MidiProfile) -> bool:
    return _PENDING_WARNING in profile.warnings


def profile_analysis_status(profile: MidiProfile) -> str:
    if profile.warnings and any(
        "failed" in warning.casefold() or "unreadable" in warning.casefold()
        for warning in profile.warnings
    ):
        return "Needs attention"
    if is_provisional_profile(profile):
        if any("Duplicate/version group:" in warning for warning in profile.warnings):
            return "Catalogued · version grouped"
        if profile.composer != "Unknown":
            return "Identified · analysis pending"
        return "Catalogued · analysis pending"
    return "Ready"


def enrich_deep_profile(profile: MidiProfile, result: QuickScanResult | None) -> MidiProfile:
    """Retain catalog identity after the authoritative deep MIDI analysis."""
    if result is None:
        return profile
    match = result.catalog_match
    title = match.title or profile.title
    composer = match.artist or profile.composer
    style = match.primary_genre or profile.style
    tags = list(profile.reference_tags)
    for tag in (match.primary_genre, match.genres):
        if tag and tag not in tags:
            tags.append(tag)
    warnings = [
        warning for warning in profile.warnings
        if warning != _PENDING_WARNING
        and not warning.startswith("Catalog identity:")
        and not warning.startswith("Duplicate/version group:")
    ]
    if match.status != "unmatched":
        warnings.append(f"Catalog identity: {match.status} ({int(round(match.confidence * 100))}%)")
    if result.duplicate_group:
        warnings.append(f"Duplicate/version group: {result.duplicate_group}")
    warnings = list(dict.fromkeys(warnings))
    return replace(
        profile,
        title=title,
        composer=composer,
        style=style,
        warnings=warnings,
        reference_tags=tags,
        song_identity=match.composition_id or profile.song_identity,
        content_fingerprint=result.sha256 or profile.content_fingerprint,
        musical_fingerprint=profile.musical_fingerprint or result.musical_fingerprint or "",
    )
