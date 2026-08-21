from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import mido

from .key_detection import estimate_key, parse_declared_key
from .models import QuickScanResult
from core.resource_safety import (
    DEFAULT_MIDI_LIMITS, MidiResourceLimitError, ProcessingBudget,
    validate_decoded_midi, validate_file,
)

PROGRAM_FAMILIES = [
    "Piano", "Chromatic Percussion", "Organ", "Guitar", "Bass", "Strings",
    "Ensemble", "Brass", "Reed", "Pipe", "Synth Lead", "Synth Pad",
    "Synth Effects", "Ethnic", "Percussive", "Sound Effects",
]


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _program_family(program: int) -> str:
    return PROGRAM_FAMILIES[min(15, max(0, program) // 8)]


def _arrangement_type(note_channels: set[int], programs: dict[int, int], note_count: int) -> str:
    melodic = {ch for ch in note_channels if ch != 9}
    has_drums = 9 in note_channels
    if note_count == 0:
        return "Empty / no note events"
    if not melodic and has_drums:
        return "Drum-only"
    families = {_program_family(programs.get(ch, 0)) for ch in melodic}
    if melodic and families <= {"Piano"} and not has_drums:
        return "Piano-only"
    if len(melodic) == 1 and not has_drums:
        return f"Single instrument ({next(iter(families), 'Unknown')})"
    if len(families) >= 2 or has_drums or len(melodic) >= 2:
        return "Multi-instrument"
    return "Unknown arrangement"


def _dominant_bpm(tempo_events: list[tuple[int, int]], end_tick: int, ticks_per_beat: int) -> tuple[float | None, float | None, float | None, int]:
    if not tempo_events:
        tempo_events = [(0, 500000)]
    by_tick: dict[int, int] = {}
    for tick, tempo in tempo_events:
        by_tick[tick] = tempo
    ordered = sorted(by_tick.items())
    weighted: Counter[float] = Counter()
    bpms: list[float] = []
    for idx, (tick, tempo) in enumerate(ordered):
        next_tick = ordered[idx + 1][0] if idx + 1 < len(ordered) else max(end_tick, tick + ticks_per_beat)
        bpm = round(mido.tempo2bpm(tempo), 3)
        bpms.append(bpm)
        weighted[bpm] += max(1, next_tick - tick)
    primary = weighted.most_common(1)[0][0]
    return primary, min(bpms), max(bpms), max(0, len(ordered) - 1)


def _duration_from_tempo_map(tempo_events: list[tuple[int, int]], end_tick: int, ticks_per_beat: int) -> float:
    """Calculate song duration without merging every track/event.

    Quick catalogue needs an accurate-enough tempo map, but `mido.merge_tracks`
    performs a global merge of every note/controller event and is unnecessarily
    expensive across a six-figure library. Tempo events are tiny, so merge only
    that bounded metadata here after each track has been walked once.
    """
    tempo = 500000
    previous_tick = 0
    seconds = 0.0
    by_tick: dict[int, int] = {}
    for tick, value in tempo_events:
        by_tick[int(tick)] = int(value)
    for tick, value in sorted(by_tick.items()):
        if tick < previous_tick:
            continue
        seconds += mido.tick2second(tick - previous_tick, ticks_per_beat, tempo)
        previous_tick = tick
        tempo = value
    if end_tick > previous_tick:
        seconds += mido.tick2second(end_tick - previous_tick, ticks_per_beat, tempo)
    return seconds


def failed_quick_scan_result(
    path: str | Path, error: BaseException | str, *, digest: str = "", stat_result=None
) -> QuickScanResult:
    """Create a stable failed catalogue row without reparsing a dangerous file."""
    source = Path(path)
    stat = stat_result
    if stat is None:
        try:
            stat = source.stat()
        except OSError:
            stat = None
    detail = str(error)
    error_prefix = "Safety limit" if isinstance(error, MidiResourceLimitError) else type(error).__name__ if isinstance(error, BaseException) else "WorkerError"
    try:
        resolved = str(source.resolve(strict=False))
    except OSError:
        resolved = str(source)
    return QuickScanResult(
        path=resolved, filename=source.name, size_bytes=int(getattr(stat, "st_size", 0) or 0),
        modified_ns=int(getattr(stat, "st_mtime_ns", 0) or 0), sha256=digest, midi_type=None,
        ticks_per_beat=None, track_count=0, active_channel_count=0, note_count=0,
        duration_seconds=None, bpm=None, bpm_min=None, bpm_max=None, tempo_change_count=0,
        key=None, mode=None, key_confidence=0.0, key_source="unavailable", meter=None,
        arrangement_type="Unreadable", program_families=[], embedded_text=[], status="failed",
        error=f"{error_prefix}: {detail}",
    )


def scan_midi(path: str | Path, max_key_notes: int = 12000) -> QuickScanResult:
    """Perform a bounded, single-pass catalogue scan.

    The merged-track iterator is deliberately not converted to a list.  This
    keeps memory proportional to the decoded MIDI tracks instead of creating a
    second unbounded event collection.
    """
    path = Path(path)
    stat = path.stat()
    digest = ""
    try:
        validate_file(path, DEFAULT_MIDI_LIMITS)
        digest = sha256_file(path)
        midi = mido.MidiFile(path)
        validate_decoded_midi(midi, DEFAULT_MIDI_LIMITS)
        budget = ProcessingBudget("quick", DEFAULT_MIDI_LIMITS)
        end_tick = 0
        tempo_events: list[tuple[int, int]] = []
        meter_events: list[tuple[int, str]] = []
        declared_keys: list[tuple[int, str]] = []
        embedded: list[str] = []
        programs: dict[int, int] = {}
        note_channels: set[int] = set()
        note_count = 0
        weighted_pitches: list[tuple[int, float]] = []
        fp_events: list[tuple[int, int, int, int]] = []

        # Walk decoded tracks directly instead of globally merging all events.
        # For the quick catalogue we need absolute event positions and a tiny
        # global tempo map; sorting every controller/note message across tracks
        # is pure overhead. This is especially material at ~100K MIDI files.
        for track_index, track in enumerate(midi.tracks):
            abs_tick = 0
            active: dict[tuple[int, int], list[int]] = defaultdict(list)
            for msg in track:
                abs_tick += int(msg.time)
                end_tick = max(end_tick, abs_tick)
                is_note_message = msg.type in {"note_on", "note_off"}
                budget.add_event(note=is_note_message, total_ticks=abs_tick)
                if msg.is_meta:
                    if msg.type == "set_tempo":
                        tempo_events.append((abs_tick, msg.tempo))
                    elif msg.type == "time_signature":
                        meter_events.append((abs_tick, f"{msg.numerator}/{msg.denominator}"))
                    elif msg.type == "key_signature":
                        declared_keys.append((abs_tick, msg.key))
                    elif msg.type in {"track_name", "instrument_name", "text", "copyright", "marker", "lyrics"}:
                        text = str(getattr(msg, "name", "") or getattr(msg, "text", "")).strip()
                        if text and text not in embedded and len(embedded) < 24:
                            embedded.append(text[:160])
                    continue

                channel = getattr(msg, "channel", None)
                if msg.type == "program_change" and channel is not None:
                    programs[channel] = msg.program
                elif msg.type == "note_on" and msg.velocity > 0 and channel is not None:
                    note_count += 1
                    note_channels.add(channel)
                    active[(channel, msg.note)].append(abs_tick)
                    if channel != 9 and len(weighted_pitches) < max_key_notes:
                        weighted_pitches.append((msg.note, 1.0 + msg.velocity / 127.0))
                    if len(fp_events) < 4000:
                        fp_events.append((abs_tick, channel, msg.note, msg.velocity))
                elif msg.type in {"note_off", "note_on"} and channel is not None:
                    starts = active.get((channel, msg.note))
                    if starts:
                        start = starts.pop()
                        if channel != 9 and len(weighted_pitches) < max_key_notes:
                            duration_beats = max(0.125, (abs_tick - start) / max(1, midi.ticks_per_beat))
                            weighted_pitches.append((msg.note, min(8.0, duration_beats)))

        elapsed_seconds = _duration_from_tempo_map(tempo_events, end_tick, midi.ticks_per_beat)
        budget.validate_ticks(end_tick)
        budget.validate_duration(elapsed_seconds)
        bpm, bpm_min, bpm_max, tempo_changes = _dominant_bpm(tempo_events, end_tick, midi.ticks_per_beat)
        meter = meter_events[0][1] if meter_events else "4/4"

        key = mode = None
        key_confidence = 0.0
        key_source = "unavailable"
        if declared_keys:
            key, mode = parse_declared_key(declared_keys[0][1])
            if key:
                key_confidence = 0.92
                key_source = "MIDI key-signature event"
        if not key:
            key, mode, key_confidence = estimate_key(weighted_pitches)
            key_source = "note-distribution estimate" if key else "unavailable"

        families = sorted({_program_family(programs.get(ch, 0)) for ch in note_channels if ch != 9})
        if 9 in note_channels:
            families.append("Drums")

        # A transposition-tolerant quick fingerprint: relative onset, channel class, intervals.
        fp_events.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        first_pitch = next((note for _, ch, note, _ in fp_events if ch != 9), 60)
        normalized_events = []
        for tick, channel, note, velocity in fp_events:
            pitch_value = note if channel == 9 else note - first_pitch
            quant_tick = round(tick / max(1, midi.ticks_per_beat / 8))
            normalized_events.append(f"{quant_tick}:{9 if channel == 9 else 0}:{pitch_value}:{velocity // 16}")
        musical_fp = hashlib.sha1("|".join(normalized_events).encode("utf-8")).hexdigest() if normalized_events else None

        return QuickScanResult(
            path=str(path.resolve()),
            filename=path.name,
            size_bytes=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            sha256=digest,
            midi_type=midi.type,
            ticks_per_beat=midi.ticks_per_beat,
            track_count=len(midi.tracks),
            active_channel_count=len(note_channels),
            note_count=note_count,
            duration_seconds=round(elapsed_seconds, 3) if math.isfinite(elapsed_seconds) else None,
            bpm=round(bpm, 3) if bpm else None,
            bpm_min=round(bpm_min, 3) if bpm_min else None,
            bpm_max=round(bpm_max, 3) if bpm_max else None,
            tempo_change_count=tempo_changes,
            key=key,
            mode=mode,
            key_confidence=key_confidence,
            key_source=key_source,
            meter=meter,
            arrangement_type=("Type 2 sequence collection" if midi.type == 2 else _arrangement_type(note_channels, programs, note_count)),
            program_families=families,
            embedded_text=embedded,
            musical_fingerprint=musical_fp,
        )
    except Exception as exc:
        return failed_quick_scan_result(path, exc, digest=digest, stat_result=stat)

