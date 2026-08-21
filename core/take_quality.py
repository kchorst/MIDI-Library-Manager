from __future__ import annotations

"""Fast structural quality gate for generated MIDI Takes.

This module does not claim that a mash is artistically good. It catches broken,
silent, percussion-only, severely hanging, or obviously overcrowded output
before the user is asked to audition it, then preserves softer observations for
human listening review.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

import mido

from core.resource_safety import DEFAULT_MIDI_LIMITS, ProcessingBudget, validate_decoded_midi, validate_file


@dataclass(frozen=True)
class TakeQualityReport:
    path: str
    passed: bool
    note_on_count: int
    melodic_note_count: int
    drum_note_count: int
    audible_track_count: int
    total_ticks: int
    first_note_tick: int
    max_melodic_polyphony: int
    hanging_note_count: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def summary(self) -> str:
        base = (
            f"{self.note_on_count} note-ons across {self.audible_track_count} audible tracks; "
            f"max melodic polyphony {self.max_melodic_polyphony}"
        )
        if self.warnings:
            return base + "; review: " + "; ".join(self.warnings)
        return base


def inspect_generated_take(path: str | Path) -> TakeQualityReport:
    source = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        validate_file(source, DEFAULT_MIDI_LIMITS)
        midi = mido.MidiFile(source)
        validate_decoded_midi(midi, DEFAULT_MIDI_LIMITS)
    except Exception as exc:
        return TakeQualityReport(
            str(source), False, 0, 0, 0, 0, 0, 0, 0, 0,
            errors=(f"Unreadable generated MIDI: {type(exc).__name__}: {exc}",),
        )

    budget = ProcessingBudget("quick", DEFAULT_MIDI_LIMITS)
    note_on_count = 0
    melodic_note_count = 0
    drum_note_count = 0
    audible_tracks = 0
    total_ticks = 0
    first_note_tick: int | None = None
    hanging_note_count = 0
    max_polyphony = 0
    high_velocity = 0

    try:
        for track in midi.tracks:
            tick = 0
            track_note_ons = 0
            active: dict[tuple[int, int], int] = {}
            melodic_active = 0
            for msg in track:
                tick += int(msg.time)
                budget.add_event(
                    note=msg.type in {"note_on", "note_off"},
                    total_ticks=tick,
                )
                if msg.is_meta or not hasattr(msg, "channel"):
                    continue
                channel = int(msg.channel)
                note = int(getattr(msg, "note", 0))
                key = (channel, note)
                is_on = msg.type == "note_on" and int(getattr(msg, "velocity", 0) or 0) > 0
                is_off = msg.type == "note_off" or (msg.type == "note_on" and int(getattr(msg, "velocity", 0) or 0) == 0)
                if is_on:
                    note_on_count += 1
                    track_note_ons += 1
                    first_note_tick = tick if first_note_tick is None else min(first_note_tick, tick)
                    active[key] = active.get(key, 0) + 1
                    if channel == 9:
                        drum_note_count += 1
                    else:
                        melodic_note_count += 1
                        melodic_active += 1
                        max_polyphony = max(max_polyphony, melodic_active)
                    if int(getattr(msg, "velocity", 0)) >= 124:
                        high_velocity += 1
                elif is_off and key in active:
                    active[key] -= 1
                    if channel != 9:
                        melodic_active = max(0, melodic_active - 1)
                    if active[key] <= 0:
                        active.pop(key, None)
            total_ticks = max(total_ticks, tick)
            if track_note_ons:
                audible_tracks += 1
            hanging_note_count += sum(active.values())

        budget.validate_ticks(total_ticks)
    except Exception as exc:
        return TakeQualityReport(
            str(source), False, note_on_count, melodic_note_count, drum_note_count,
            audible_tracks, total_ticks, int(first_note_tick or 0), max_polyphony,
            hanging_note_count,
            errors=(f"Generated MIDI quality scan stopped safely: {type(exc).__name__}: {exc}",),
        )

    first_note = int(first_note_tick or 0)
    tpb = max(1, int(midi.ticks_per_beat or 480))

    if note_on_count < 4:
        errors.append("The generated Take contains too few audible note events.")
    if melodic_note_count == 0:
        if drum_note_count:
            errors.append("The generated Take contains percussion but no pitched musical material.")
        else:
            errors.append("The generated Take contains no pitched musical material.")
    if total_ticks < tpb:
        errors.append("The generated Take is shorter than one beat.")
    if audible_tracks == 0:
        errors.append("The generated Take has no audible tracks.")
    if hanging_note_count > max(12, int(note_on_count * 0.08)):
        errors.append(f"The generated Take leaves {hanging_note_count} notes hanging at the end.")
    elif hanging_note_count:
        warnings.append(f"{hanging_note_count} note(s) remain active at end-of-track")

    if first_note > tpb * 4:
        warnings.append("long silent lead-in before the first note")
    if max_polyphony > 28:
        warnings.append("very dense simultaneous pitched material; listen for masking")
    if note_on_count and high_velocity / note_on_count > 0.35:
        warnings.append("many notes are near maximum velocity; listen for harsh balance")
    if melodic_note_count and drum_note_count == 0:
        warnings.append("no percussion is present; confirm that this is intentional")

    return TakeQualityReport(
        path=str(source.resolve()),
        passed=not errors,
        note_on_count=note_on_count,
        melodic_note_count=melodic_note_count,
        drum_note_count=drum_note_count,
        audible_track_count=audible_tracks,
        total_ticks=total_ticks,
        first_note_tick=first_note,
        max_melodic_polyphony=max_polyphony,
        hanging_note_count=hanging_note_count,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
