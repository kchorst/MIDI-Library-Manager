from __future__ import annotations

"""Section-level MIDI refinement and preflight checks.

This module edits only a selected section of an already-generated Take.  It is
UI-independent so Guided, Assisted, and Creative Workbench can share one
implementation and one lineage model.
"""

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import os
import tempfile
from typing import Iterable, Sequence

import mido

from core.arrangement_intelligence import SECTION_NAMES
from core.remix_engine import RemixTake
from core.resource_safety import DEFAULT_MIDI_LIMITS, validate_decoded_midi, validate_file


LEAD_WORDS = (
    "lead", "melody", "relay voice", "anchor melody", "progressive voice",
    "hook", "upper voice", "solo",
)
BACKING_WORDS = (
    "backing", "foundation", "section", "journey", "accompaniment",
    "texture", "chord", "rhythm", "changing foundation",
)
BASS_PROGRAMS = set(range(32, 40))


@dataclass(frozen=True)
class RefinementRequest:
    section_name: str
    section_index: int
    section_count: int = len(SECTION_NAMES)
    issue_code: str = "clarity"
    issue_label: str = "Make the section clearer"
    lead_gain_percent: int = 100
    backing_gain_percent: int = 100
    bass_gain_percent: int = 100
    drum_density_percent: int = 100
    pitch_shift_semitones: int = 0
    octave_shift: int = 0
    timing_shift_beats: int = 0
    transition_beats: int = 0
    duration_delta_bars: int = 0
    instrument_program: int | None = None
    instrument_target: str = "lead"
    comparison_slot: str = "B"

    def normalized(self) -> "RefinementRequest":
        count = max(1, min(32, int(self.section_count or 1)))
        index = max(0, min(count - 1, int(self.section_index or 0)))
        section_name = str(self.section_name or f"Section {index + 1}").strip()
        return replace(
            self,
            section_name=section_name,
            section_index=index,
            section_count=count,
            lead_gain_percent=max(20, min(180, int(self.lead_gain_percent))),
            backing_gain_percent=max(10, min(180, int(self.backing_gain_percent))),
            bass_gain_percent=max(0, min(180, int(self.bass_gain_percent))),
            drum_density_percent=max(0, min(150, int(self.drum_density_percent))),
            pitch_shift_semitones=max(-12, min(12, int(self.pitch_shift_semitones))),
            octave_shift=max(-2, min(2, int(self.octave_shift))),
            timing_shift_beats=max(-8, min(8, int(self.timing_shift_beats))),
            transition_beats=max(0, min(8, int(self.transition_beats))),
            duration_delta_bars=max(-4, min(8, int(self.duration_delta_bars))),
            instrument_program=(
                None if self.instrument_program is None
                else max(0, min(127, int(self.instrument_program)))
            ),
            instrument_target=(
                self.instrument_target if self.instrument_target in {"lead", "backing", "all"} else "lead"
            ),
            comparison_slot=(str(self.comparison_slot or "B").strip().upper()[:1] or "B"),
        )

    def to_dict(self) -> dict:
        return asdict(self.normalized())

    def summary_lines(self) -> tuple[str, ...]:
        item = self.normalized()
        lines = [
            f"Selective refinement {item.comparison_slot}: {item.section_name} — {item.issue_label}.",
            (
                f"Section controls: lead {item.lead_gain_percent}%; backing {item.backing_gain_percent}%; "
                f"bass {item.bass_gain_percent}%; drums {item.drum_density_percent}%."
            ),
        ]
        if item.pitch_shift_semitones:
            lines.append(f"Pitched material in the selected section shifted {item.pitch_shift_semitones:+d} semitone(s).")
        if item.octave_shift:
            lines.append(f"Lead register shifted {item.octave_shift:+d} octave(s) in the selected section.")
        if item.timing_shift_beats:
            lines.append(f"Selected-section entries shifted {item.timing_shift_beats:+d} beat(s).")
        if item.transition_beats:
            lines.append(f"Applied a {item.transition_beats}-beat boundary transition envelope.")
        if item.duration_delta_bars:
            lines.append(f"Selected-section duration changed by {item.duration_delta_bars:+d} bar(s).")
        if item.instrument_program is not None:
            lines.append(
                f"Program {item.instrument_program} substituted for {item.instrument_target} material during the selected section."
            )
        return tuple(lines)


@dataclass(frozen=True)
class SectionPreflightReport:
    passed: bool
    section_name: str
    start_bar: int
    end_bar: int
    note_on_count: int
    pitched_note_count: int
    drum_note_count: int
    active_lead_tracks: int
    active_bass_tracks: int
    peak_polyphony: int
    hanging_notes: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def summary(self) -> str:
        status = "passed" if self.passed else "failed"
        base = (
            f"Section preflight {status}: {self.note_on_count} note-ons, "
            f"peak polyphony {self.peak_polyphony}, {self.active_lead_tracks} active lead track(s), "
            f"{self.active_bass_tracks} bass-bearing track(s)."
        )
        if self.warnings:
            return base + " Review: " + "; ".join(self.warnings)
        return base


@dataclass(frozen=True)
class RefinementResult:
    take: RemixTake
    request: RefinementRequest
    preflight: SectionPreflightReport


@dataclass(frozen=True)
class _AbsoluteEvent:
    tick: int
    order: int
    message: mido.Message | mido.MetaMessage


def refinement_preset(issue_code: str, *, comparison_slot: str = "B") -> dict[str, int | str | None]:
    """Return conservative, audible defaults for a listening complaint."""

    presets: dict[str, dict[str, int | str | None]] = {
        "clarity": {"lead_gain_percent": 118, "backing_gain_percent": 76, "transition_beats": 1},
        "reduce-backing": {"lead_gain_percent": 105, "backing_gain_percent": 62},
        "wrong-lead": {"lead_gain_percent": 122, "backing_gain_percent": 68, "octave_shift": 1},
        "melody-conflict": {"lead_gain_percent": 112, "backing_gain_percent": 64, "octave_shift": 1},
        "bass-conflict": {"bass_gain_percent": 48, "backing_gain_percent": 82},
        "rhythm-off": {"drum_density_percent": 72, "transition_beats": 2},
        "harmony-wrong": {"backing_gain_percent": 58, "pitch_shift_semitones": 0},
        "starts-early": {"timing_shift_beats": 1, "transition_beats": 1},
        "starts-late": {"timing_shift_beats": -1, "transition_beats": 1},
        "too-busy": {"lead_gain_percent": 108, "backing_gain_percent": 68, "bass_gain_percent": 72, "drum_density_percent": 68},
        "too-empty": {"lead_gain_percent": 112, "backing_gain_percent": 114, "bass_gain_percent": 108, "drum_density_percent": 115},
        "weak-transition": {"transition_beats": 3, "backing_gain_percent": 88},
        "instrument-fit": {"instrument_program": 48, "instrument_target": "lead"},
        "weak-ending": {"lead_gain_percent": 118, "backing_gain_percent": 92, "transition_beats": 4, "duration_delta_bars": 2},
    }
    values = dict(presets.get(str(issue_code), presets["clarity"]))
    values["comparison_slot"] = comparison_slot
    return values


def alternate_request(request: RefinementRequest) -> RefinementRequest:
    """Create a meaningfully different C option without becoming extreme."""

    item = request.normalized()
    issue = item.issue_code
    if issue in {"clarity", "reduce-backing", "melody-conflict", "too-busy", "harmony-wrong"}:
        return replace(
            item,
            comparison_slot="C",
            lead_gain_percent=min(150, item.lead_gain_percent + 8),
            backing_gain_percent=max(30, item.backing_gain_percent - 12),
            bass_gain_percent=max(30, item.bass_gain_percent - 8),
            drum_density_percent=max(35, item.drum_density_percent - 8),
        )
    if issue in {"starts-early", "starts-late", "weak-transition", "rhythm-off"}:
        direction = 1 if item.timing_shift_beats >= 0 else -1
        return replace(
            item,
            comparison_slot="C",
            timing_shift_beats=item.timing_shift_beats + direction,
            transition_beats=min(8, max(2, item.transition_beats + 1)),
            drum_density_percent=max(45, item.drum_density_percent - 8),
        )
    if issue == "bass-conflict":
        return replace(item, comparison_slot="C", bass_gain_percent=max(20, item.bass_gain_percent - 18), octave_shift=1)
    if issue == "too-empty":
        return replace(
            item,
            comparison_slot="C",
            lead_gain_percent=min(150, item.lead_gain_percent + 8),
            backing_gain_percent=min(150, item.backing_gain_percent + 10),
            drum_density_percent=min(135, item.drum_density_percent + 10),
        )
    if issue == "instrument-fit":
        alternate_program = 40 if item.instrument_program != 40 else 73
        return replace(item, comparison_slot="C", instrument_program=alternate_program)
    if issue == "weak-ending":
        return replace(item, comparison_slot="C", duration_delta_bars=min(4, item.duration_delta_bars + 2), transition_beats=6)
    return replace(item, comparison_slot="C", backing_gain_percent=max(40, item.backing_gain_percent - 10))


def _time_signature(midi: mido.MidiFile) -> tuple[int, int]:
    for track in midi.tracks:
        for msg in track:
            if msg.is_meta and msg.type == "time_signature":
                return max(1, int(msg.numerator)), max(1, int(msg.denominator))
    return 4, 4


def bar_ticks_for_midi(midi: mido.MidiFile) -> int:
    numerator, denominator = _time_signature(midi)
    return max(1, int(round(int(midi.ticks_per_beat or 480) * numerator * (4.0 / denominator))))


def section_bar_bounds(total_bars: int, section_index: int, section_count: int) -> tuple[int, int]:
    bars = max(1, int(total_bars or 1))
    count = max(1, int(section_count or 1))
    index = max(0, min(count - 1, int(section_index or 0)))
    start = int(round(bars * index / count))
    end = int(round(bars * (index + 1) / count))
    end = max(start + 1, min(bars, end))
    start = max(0, min(end - 1, start))
    return start, end


def section_start_percent(request: RefinementRequest) -> int:
    item = request.normalized()
    return max(0, min(99, int(round(item.section_index * 100 / item.section_count))))


def _track_name(track: mido.MidiTrack, fallback: str) -> str:
    for msg in track:
        if msg.is_meta and msg.type == "track_name":
            return str(getattr(msg, "name", fallback) or fallback)
    return fallback


def _track_role(track_name: str) -> str:
    lowered = str(track_name).casefold()
    # Generated names such as "Backing without lead" contain the word lead;
    # backing/foundation markers therefore take precedence.
    if any(word in lowered for word in BACKING_WORDS):
        return "backing"
    if any(word in lowered for word in LEAD_WORDS):
        return "lead"
    return "unknown"


def _absolute(track: mido.MidiTrack) -> list[_AbsoluteEvent]:
    tick = 0
    events: list[_AbsoluteEvent] = []
    for order, msg in enumerate(track):
        tick += int(msg.time)
        events.append(_AbsoluteEvent(tick, order, msg.copy(time=0)))
    return events


def _velocity(value: int, multiplier: float) -> int:
    if value <= 0:
        return 0
    return max(1, min(127, int(round(value * multiplier))))


def _density_keep(tick: int, note: int, percent: int, ticks_per_beat: int) -> bool:
    if percent >= 100:
        return True
    if percent <= 0:
        return False
    bucket = ((int(tick) // max(1, ticks_per_beat // 4)) * 37 + int(note) * 17) % 100
    return bucket < percent


def _program_context(events: Sequence[_AbsoluteEvent], section_start: int) -> dict[int, int]:
    programs: dict[int, int] = {}
    for event in events:
        if event.tick > section_start:
            break
        msg = event.message
        if not msg.is_meta and msg.type == "program_change" and hasattr(msg, "channel"):
            programs[int(msg.channel)] = int(msg.program)
    return programs


def _target_track(role: str, target: str) -> bool:
    return target == "all" or role == target or (target == "backing" and role == "unknown")


def _transform_track(
    track: mido.MidiTrack,
    *,
    track_index: int,
    request: RefinementRequest,
    section_start_tick: int,
    section_end_tick: int,
    new_section_end_tick: int,
    ticks_per_beat: int,
) -> tuple[str, list[_AbsoluteEvent]]:
    name = _track_name(track, f"Track {track_index + 1}")
    role = _track_role(name)
    events = _absolute(track)
    duration_scale = (
        (new_section_end_tick - section_start_tick) / max(1, section_end_tick - section_start_tick)
    )
    downstream_shift = new_section_end_tick - section_end_tick
    timing_shift = int(request.timing_shift_beats) * int(ticks_per_beat)
    transition_ticks = int(request.transition_beats) * int(ticks_per_beat)
    programs = _program_context(events, section_start_tick)
    suppressed_notes: dict[tuple[int, int], int] = {}
    transformed: list[_AbsoluteEvent] = []
    channels_used_in_section: set[int] = set()

    for event in events:
        msg = event.message
        if msg.is_meta and msg.type in {"end_of_track", "track_name"}:
            continue
        tick = int(event.tick)
        in_section = section_start_tick <= tick < section_end_tick
        if tick < section_start_tick:
            new_tick = tick
        elif in_section:
            relative = tick - section_start_tick
            new_tick = section_start_tick + int(round(relative * duration_scale)) + timing_shift
            new_tick = max(section_start_tick, min(max(section_start_tick, new_section_end_tick - 1), new_tick))
        else:
            new_tick = tick + downstream_shift

        if not in_section or msg.is_meta or not hasattr(msg, "channel"):
            transformed.append(_AbsoluteEvent(max(0, new_tick), event.order, msg.copy(time=0)))
            continue

        channel = int(msg.channel)
        channels_used_in_section.add(channel)
        note = int(getattr(msg, "note", 0))
        is_on = msg.type == "note_on" and int(getattr(msg, "velocity", 0) or 0) > 0
        is_off = msg.type == "note_off" or (msg.type == "note_on" and int(getattr(msg, "velocity", 0) or 0) == 0)
        note_key = (channel, note)

        if channel == 9 and (is_on or is_off):
            if is_on and not _density_keep(new_tick, note, request.drum_density_percent, ticks_per_beat):
                suppressed_notes[note_key] = suppressed_notes.get(note_key, 0) + 1
                continue
            if is_off and suppressed_notes.get(note_key, 0) > 0:
                suppressed_notes[note_key] -= 1
                if suppressed_notes[note_key] <= 0:
                    suppressed_notes.pop(note_key, None)
                continue

        if is_on:
            multiplier = 1.0
            if role == "lead":
                multiplier *= request.lead_gain_percent / 100.0
            elif role == "backing":
                multiplier *= request.backing_gain_percent / 100.0
            else:
                multiplier *= min(request.lead_gain_percent, request.backing_gain_percent) / 100.0
            program = programs.get(channel)
            is_bass = channel != 9 and (note < 48 or program in BASS_PROGRAMS or "bass" in name.casefold())
            if is_bass:
                multiplier *= request.bass_gain_percent / 100.0
            if channel == 9 and request.drum_density_percent > 100:
                multiplier *= min(1.35, request.drum_density_percent / 100.0)
            if transition_ticks > 0:
                distance_from_start = max(0, new_tick - section_start_tick)
                distance_to_end = max(0, new_section_end_tick - new_tick)
                edge = min(distance_from_start, distance_to_end)
                if edge < transition_ticks:
                    multiplier *= 0.45 + 0.55 * (edge / max(1, transition_ticks))
            msg = msg.copy(velocity=_velocity(int(msg.velocity), multiplier), time=0)

        if channel != 9 and msg.type in {"note_on", "note_off", "polytouch"} and hasattr(msg, "note"):
            shift = int(request.pitch_shift_semitones)
            if role == "lead":
                shift += int(request.octave_shift) * 12
            msg = msg.copy(note=max(0, min(127, int(msg.note) + shift)), time=0)

        transformed.append(_AbsoluteEvent(max(0, new_tick), event.order, msg.copy(time=0)))

    if request.instrument_program is not None and _target_track(role, request.instrument_target):
        target_channels = sorted(channel for channel in channels_used_in_section if channel != 9)
        for position, channel in enumerate(target_channels):
            transformed.append(
                _AbsoluteEvent(
                    section_start_tick,
                    -200 + position,
                    mido.Message(
                        "program_change", channel=channel,
                        program=int(request.instrument_program), time=0,
                    ),
                )
            )
            if channel in programs:
                transformed.append(
                    _AbsoluteEvent(
                        new_section_end_tick,
                        20_000 + position,
                        mido.Message("program_change", channel=channel, program=programs[channel], time=0),
                    )
                )
    return name, transformed


def _delta_track(name: str, events: Iterable[_AbsoluteEvent], end_tick: int) -> mido.MidiTrack:
    sorted_events = sorted(events, key=lambda item: (max(0, int(item.tick)), int(item.order)))
    track = mido.MidiTrack()
    safe_name = str(name).encode("latin-1", errors="replace").decode("latin-1")[:127]
    track.append(mido.MetaMessage("track_name", name=safe_name, time=0))
    last_tick = 0
    active: dict[tuple[int, int], int] = {}
    channels: set[int] = set()
    for event in sorted_events:
        tick = max(last_tick, int(event.tick))
        msg = event.message.copy(time=tick - last_tick)
        track.append(msg)
        last_tick = tick
        if msg.is_meta or not hasattr(msg, "channel"):
            continue
        channel = int(msg.channel)
        channels.add(channel)
        if msg.type not in {"note_on", "note_off"} or not hasattr(msg, "note"):
            continue
        key = (channel, int(msg.note))
        velocity = int(getattr(msg, "velocity", 0) or 0)
        if msg.type == "note_on" and velocity > 0:
            active[key] = active.get(key, 0) + 1
        else:
            remaining = active.get(key, 0) - 1
            if remaining > 0:
                active[key] = remaining
            else:
                active.pop(key, None)

    tail = max(last_tick, int(end_tick))
    first = True
    for channel in sorted(channels):
        track.append(mido.Message(
            "control_change", channel=channel, control=64, value=0,
            time=(tail - last_tick) if first else 0,
        ))
        last_tick = tail
        first = False
    for (channel, note), count in sorted(active.items()):
        for _ in range(max(1, int(count))):
            track.append(mido.Message(
                "note_off", channel=channel, note=note, velocity=0,
                time=(tail - last_tick) if first else 0,
            ))
            last_tick = tail
            first = False
    for channel in sorted(channels):
        track.append(mido.Message(
            "control_change", channel=channel, control=123, value=0,
            time=(tail - last_tick) if first else 0,
        ))
        last_tick = tail
        first = False
    track.append(mido.MetaMessage("end_of_track", time=max(0, tail - last_tick)))
    return track


def _safe_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-_" else "-" for character in value)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:80] or "refined-take"


def _write_atomic(midi: mido.MidiFile, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".partial.mid", dir=str(target.parent))
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        midi.save(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def preflight_section(
    path: str | Path,
    request: RefinementRequest,
    *,
    total_bars: int,
) -> SectionPreflightReport:
    item = request.normalized()
    source = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        validate_file(source, DEFAULT_MIDI_LIMITS)
        midi = mido.MidiFile(source)
        validate_decoded_midi(midi, DEFAULT_MIDI_LIMITS)
    except (OSError, EOFError, ValueError, TypeError, RuntimeError) as exc:
        return SectionPreflightReport(
            False, item.section_name, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            errors=(f"Unreadable refined MIDI: {type(exc).__name__}: {exc}",),
        )

    bar_ticks = bar_ticks_for_midi(midi)
    start_bar, end_bar = section_bar_bounds(total_bars, item.section_index, item.section_count)
    start_tick = start_bar * bar_ticks
    end_tick = end_bar * bar_ticks
    notes = 0
    pitched = 0
    drums = 0
    lead_tracks = 0
    bass_tracks = 0
    peak_polyphony = 0
    hanging = 0
    first_beat_notes = 0

    for track_index, track in enumerate(midi.tracks):
        name = _track_name(track, f"Track {track_index + 1}")
        role = _track_role(name)
        tick = 0
        active: dict[tuple[int, int], int] = {}
        active_pitched = 0
        track_notes = 0
        track_bass = False
        programs: dict[int, int] = {}
        for msg in track:
            tick += int(msg.time)
            if msg.is_meta or not hasattr(msg, "channel"):
                continue
            channel = int(msg.channel)
            if msg.type == "program_change":
                programs[channel] = int(msg.program)
                continue
            if not (start_tick <= tick < end_tick):
                continue
            note = int(getattr(msg, "note", 0))
            key = (channel, note)
            velocity = int(getattr(msg, "velocity", 0) or 0)
            is_on = msg.type == "note_on" and velocity > 0
            is_off = msg.type == "note_off" or (msg.type == "note_on" and velocity == 0)
            if is_on:
                notes += 1
                track_notes += 1
                if tick < start_tick + int(midi.ticks_per_beat or 480):
                    first_beat_notes += 1
                active[key] = active.get(key, 0) + 1
                if channel == 9:
                    drums += 1
                else:
                    pitched += 1
                    active_pitched += 1
                    peak_polyphony = max(peak_polyphony, active_pitched)
                    if note < 48 or programs.get(channel) in BASS_PROGRAMS or "bass" in name.casefold():
                        track_bass = True
            elif is_off and key in active:
                active[key] -= 1
                if channel != 9:
                    active_pitched = max(0, active_pitched - 1)
                if active[key] <= 0:
                    active.pop(key, None)
        if track_notes and role == "lead":
            lead_tracks += 1
        if track_notes and track_bass:
            bass_tracks += 1
        hanging += sum(active.values())

    section_beats = max(1.0, (end_tick - start_tick) / max(1, int(midi.ticks_per_beat or 480)))
    density = pitched / section_beats
    if notes < 4:
        errors.append("The refined section contains too few audible note events.")
    if pitched == 0:
        errors.append("The refined section contains no pitched musical material.")
    if hanging:
        warnings.append(
            f"{hanging} note(s) cross the section boundary; the whole-Take safety gate confirms they close by the ending"
        )
    if lead_tracks >= 2:
        warnings.append("multiple lead-bearing tracks remain active; listen for melody conflict")
    if bass_tracks >= 2:
        warnings.append("multiple bass-bearing tracks remain active; listen for low-frequency conflict")
    if density > 18:
        warnings.append("very high pitched-note density; listen for masking")
    if peak_polyphony > 28:
        warnings.append("very high simultaneous polyphony")
    if first_beat_notes > 18:
        warnings.append("abrupt section entrance with many notes on the first beat")
    if drums == 0:
        warnings.append("no percussion is active in this section")

    return SectionPreflightReport(
        passed=not errors,
        section_name=item.section_name,
        start_bar=start_bar,
        end_bar=end_bar,
        note_on_count=notes,
        pitched_note_count=pitched,
        drum_note_count=drums,
        active_lead_tracks=lead_tracks,
        active_bass_tracks=bass_tracks,
        peak_polyphony=peak_polyphony,
        hanging_notes=hanging,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def refine_take(
    parent: RemixTake,
    output_directory: str | Path,
    request: RefinementRequest,
) -> RefinementResult:
    item = request.normalized()
    source = Path(parent.path)
    validate_file(source, DEFAULT_MIDI_LIMITS)
    midi = mido.MidiFile(source)
    validate_decoded_midi(midi, DEFAULT_MIDI_LIMITS)

    bar_ticks = bar_ticks_for_midi(midi)
    start_bar, end_bar = section_bar_bounds(parent.bars, item.section_index, item.section_count)
    section_start = start_bar * bar_ticks
    section_end = end_bar * bar_ticks
    new_section_end = max(section_start + bar_ticks, section_end + item.duration_delta_bars * bar_ticks)
    downstream_shift = new_section_end - section_end
    total_ticks = max(
        (sum(int(msg.time) for msg in track) for track in midi.tracks),
        default=max(1, int(parent.bars)) * bar_ticks,
    )
    new_total_ticks = max(new_section_end, total_ticks + downstream_shift)

    output_midi = mido.MidiFile(type=midi.type, ticks_per_beat=midi.ticks_per_beat)
    for track_index, track in enumerate(midi.tracks):
        name, events = _transform_track(
            track,
            track_index=track_index,
            request=item,
            section_start_tick=section_start,
            section_end_tick=section_end,
            new_section_end_tick=new_section_end,
            ticks_per_beat=int(midi.ticks_per_beat or 480),
        )
        output_midi.tracks.append(_delta_track(name, events, new_total_ticks))

    revision_number = max(1, int(parent.revision_generation or 0) + 1)
    slot = item.comparison_slot
    filename = _safe_name(f"{parent.take_id}-{item.section_name}-{slot}-r{revision_number}") + ".mid"
    target = Path(output_directory) / filename
    _write_atomic(output_midi, target)

    new_bars = max(1, int(parent.bars) + int(item.duration_delta_bars))
    preflight = preflight_section(target, item, total_bars=new_bars)
    lineage_root = parent.lineage_root_take_id or parent.parent_take_id or parent.take_id
    summary = tuple(dict.fromkeys((*parent.decision_summary, *item.summary_lines(), preflight.summary)))
    take = replace(
        parent,
        take_id=f"pending-{slot.lower()}-{revision_number}",
        name=f"{parent.name} — {item.section_name} revision {slot}",
        description=(
            f"Selective regeneration of {item.section_name} from parent Take {parent.take_id}. "
            f"Only the chosen section and its boundary timing were changed. {item.issue_label}."
        ),
        path=str(target.resolve()),
        bars=new_bars,
        variation=max(1, int(parent.variation)),
        status="candidate",
        approved=False,
        exported_paths=(),
        decision_summary=summary,
        parent_take_id=parent.take_id,
        parent_trial_id=parent.trial_id,
        parent_path=parent.path,
        lineage_root_take_id=lineage_root,
        revision_generation=revision_number,
        changed_sections=(item.section_name,),
        refinement_reason=item.issue_label,
        refinement_settings=tuple(
            f"{key}={value}" for key, value in item.to_dict().items()
            if key not in {"section_name", "issue_label"}
        ),
        comparison_slot=slot,
    )
    return RefinementResult(take=take, request=item, preflight=preflight)
