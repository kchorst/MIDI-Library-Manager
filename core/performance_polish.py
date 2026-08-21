from __future__ import annotations

"""Deterministic, bounded whole-Take performance finishing.

The performance layer changes only generated Take copies. It never edits source
songs. Timing movement is intentionally small, paired note-offs follow their
note-ons, percussion can remain quantized, and output is written atomically.
"""

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import os
import random
import tempfile
from typing import Iterable

import mido


@dataclass(frozen=True)
class PerformancePolishSettings:
    profile: str = "Natural"
    timing_ms: int = 8
    dynamics_percent: int = 6
    articulation_percent: int = 96
    swing_percent: int = 0
    preserve_percussion_timing: bool = True
    seed: int = 0

    def normalized(self) -> "PerformancePolishSettings":
        profile = str(self.profile or "Natural").strip() or "Natural"
        return replace(
            self,
            profile=profile,
            timing_ms=max(0, min(35, int(self.timing_ms or 0))),
            dynamics_percent=max(0, min(30, int(self.dynamics_percent or 0))),
            articulation_percent=max(65, min(115, int(self.articulation_percent or 100))),
            swing_percent=max(0, min(35, int(self.swing_percent or 0))),
            seed=max(0, min(2_147_483_647, int(self.seed or 0))),
            preserve_percussion_timing=bool(self.preserve_percussion_timing),
        )

    def to_dict(self) -> dict:
        return asdict(self.normalized())

    def summary_lines(self) -> tuple[str, ...]:
        item = self.normalized()
        percussion = "kept tight" if item.preserve_percussion_timing else "included"
        return (
            f"Performance profile: {item.profile}",
            f"Timing variation: ±{item.timing_ms} ms; swing: {item.swing_percent}%",
            f"Dynamics variation: ±{item.dynamics_percent}%; articulation: {item.articulation_percent}%",
            f"Percussion timing: {percussion}; deterministic seed: {item.seed}",
        )


PERFORMANCE_PRESETS: dict[str, PerformancePolishSettings] = {
    "Tight": PerformancePolishSettings(profile="Tight", timing_ms=2, dynamics_percent=3, articulation_percent=98, swing_percent=0),
    "Natural": PerformancePolishSettings(profile="Natural", timing_ms=8, dynamics_percent=6, articulation_percent=96, swing_percent=0),
    "Live": PerformancePolishSettings(profile="Live", timing_ms=15, dynamics_percent=10, articulation_percent=93, swing_percent=4),
    "Punchy": PerformancePolishSettings(profile="Punchy", timing_ms=5, dynamics_percent=13, articulation_percent=88, swing_percent=0),
    "Laid Back": PerformancePolishSettings(profile="Laid Back", timing_ms=11, dynamics_percent=7, articulation_percent=101, swing_percent=12),
}


@dataclass(frozen=True)
class _AbsoluteMessage:
    tick: int
    order: int
    message: mido.Message | mido.MetaMessage


def preset_settings(name: str) -> PerformancePolishSettings:
    return PERFORMANCE_PRESETS.get(str(name or "").strip(), PERFORMANCE_PRESETS["Natural"])


def _absolute_track(track: mido.MidiTrack) -> list[_AbsoluteMessage]:
    result: list[_AbsoluteMessage] = []
    tick = 0
    for order, message in enumerate(track):
        tick += max(0, int(message.time or 0))
        result.append(_AbsoluteMessage(tick, order, message.copy(time=0)))
    return result


def _delta_track(events: Iterable[_AbsoluteMessage]) -> mido.MidiTrack:
    track = mido.MidiTrack()
    last_tick = 0
    # Note-offs precede note-ons at an identical tick to prevent hanging overlaps.
    def priority(item: _AbsoluteMessage) -> tuple[int, int, int]:
        message = item.message
        is_off = (
            not message.is_meta
            and message.type in {"note_off", "note_on"}
            and (message.type == "note_off" or int(getattr(message, "velocity", 0) or 0) == 0)
        )
        return (max(0, int(item.tick)), 0 if is_off else 1, int(item.order))

    for event in sorted(events, key=priority):
        tick = max(last_tick, int(event.tick))
        track.append(event.message.copy(time=tick - last_tick))
        last_tick = tick
    if not track or track[-1].type != "end_of_track":
        track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def _timing_ticks(ticks_per_beat: int, timing_ms: int) -> int:
    # At 120 BPM one quarter note is 500 ms. This bounded conversion avoids
    # rebuilding the tempo map merely to apply a few milliseconds of feel.
    return max(0, int(round(max(1, ticks_per_beat) * int(timing_ms) / 500.0)))


def _swing_delay(tick: int, ticks_per_beat: int, swing_percent: int) -> int:
    if swing_percent <= 0 or ticks_per_beat <= 0:
        return 0
    eighth = max(1, ticks_per_beat // 2)
    eighth_index = int(round(tick / eighth))
    if eighth_index % 2 == 0:
        return 0
    return int(round(eighth * min(35, swing_percent) / 100.0 * 0.5))


def _velocity(value: int, dynamics_percent: int, rng: random.Random, tick: int, ticks_per_beat: int) -> int:
    if value <= 0:
        return 0
    spread = max(0, int(dynamics_percent)) / 100.0
    random_factor = 1.0 + rng.uniform(-spread, spread)
    beat_accent = 1.0
    if ticks_per_beat > 0:
        position = tick % (ticks_per_beat * 4)
        if position < max(1, ticks_per_beat // 12):
            beat_accent = 1.0 + min(0.06, spread * 0.5)
    return max(1, min(127, int(round(value * random_factor * beat_accent))))


def polish_midi(
    source_midi: str | Path,
    output_midi: str | Path,
    settings: PerformancePolishSettings,
) -> Path:
    """Create a performance-polished MIDI copy using bounded deterministic edits."""

    source = Path(source_midi)
    target = Path(output_midi)
    if not source.is_file():
        raise FileNotFoundError(f"Take MIDI not found: {source}")
    if target.suffix.casefold() not in {".mid", ".midi"}:
        target = target.with_suffix(".mid")
    target.parent.mkdir(parents=True, exist_ok=True)

    item = settings.normalized()
    midi = mido.MidiFile(str(source), clip=True)
    output = mido.MidiFile(type=midi.type, ticks_per_beat=midi.ticks_per_beat, charset=getattr(midi, "charset", "latin1"))
    max_jitter = _timing_ticks(midi.ticks_per_beat, item.timing_ms)

    for track_index, track in enumerate(midi.tracks):
        events = _absolute_track(track)
        rng = random.Random(item.seed + track_index * 1_000_003)
        active: dict[tuple[int, int], list[tuple[int, int]]] = {}
        transformed: list[_AbsoluteMessage] = []
        eot_tick = 0

        for event in events:
            message = event.message
            eot_tick = max(eot_tick, event.tick)
            if message.is_meta:
                if message.type != "end_of_track":
                    transformed.append(event)
                continue
            if message.type not in {"note_on", "note_off"} or not hasattr(message, "note"):
                transformed.append(event)
                continue

            channel = int(getattr(message, "channel", 0) or 0)
            note = int(message.note)
            velocity = int(getattr(message, "velocity", 0) or 0)
            is_on = message.type == "note_on" and velocity > 0
            key = (channel, note)

            if is_on:
                percussion_tight = channel == 9 and item.preserve_percussion_timing
                jitter = 0 if percussion_tight or max_jitter <= 0 else rng.randint(-max_jitter, max_jitter)
                swing = 0 if percussion_tight else _swing_delay(event.tick, midi.ticks_per_beat, item.swing_percent)
                new_tick = max(0, event.tick + jitter + swing)
                new_velocity = _velocity(velocity, item.dynamics_percent, rng, event.tick, midi.ticks_per_beat)
                active.setdefault(key, []).append((event.tick, new_tick))
                transformed.append(_AbsoluteMessage(new_tick, event.order, message.copy(velocity=new_velocity, time=0)))
                continue

            queue = active.get(key) or []
            if queue:
                original_start, new_start = queue.pop(0)
                if not queue:
                    active.pop(key, None)
                original_duration = max(1, event.tick - original_start)
                duration = max(1, int(round(original_duration * item.articulation_percent / 100.0)))
                new_tick = max(new_start + 1, new_start + duration)
            else:
                new_tick = event.tick
            transformed.append(_AbsoluteMessage(new_tick, event.order, message.copy(time=0)))

        # Keep the formal ending after every moved note-off.
        final_tick = max([eot_tick, *(event.tick for event in transformed)] if transformed else [eot_tick])
        transformed.append(_AbsoluteMessage(final_tick, len(events) + 1, mido.MetaMessage("end_of_track", time=0)))
        output.tracks.append(_delta_track(transformed))

    fd, temp_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".partial.mid", dir=str(target.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        output.save(str(temp))
        # Parsing the staged output catches malformed delta times before promotion.
        mido.MidiFile(str(temp), clip=True)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target
