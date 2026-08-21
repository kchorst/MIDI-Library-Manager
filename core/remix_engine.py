from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from core.atomic_io import atomic_write_json
import json
import math
import os
import re
import shutil
import random
from typing import Iterable, Sequence

import mido

from core.remix_profile import MidiProfile, NOTE_NAMES, key_name, primary_signature_asset, quarter_beats_per_bar, signed_pitch_class_shift


_AUTO_BALANCE_ENABLED = True
_MODE_HANDLING = "Preserve source mode"
_TARGET_TONIC_PC: int | None = None
_TARGET_MODE = "major"


@dataclass(frozen=True)
class TakeStrategy:
    key: str
    label: str
    description: str


TAKE_STRATEGIES: tuple[TakeStrategy, ...] = (
    TakeStrategy("melody-a-accompaniment-b", "A melody over B backing", "Source A supplies only its lead; Source B supplies backing and signature elements with its detected lead removed."),
    TakeStrategy("melody-b-accompaniment-a", "B melody over A backing", "Reverse the source roles while preserving the chosen backing source’s drums, bass, chords, riffs, fills, and protected opening."),
    TakeStrategy("section-exchange", "Alternating sections", "Alternate complete blocks from the two sources."),
    TakeStrategy("conversation", "Melodic conversation", "Let both upper parts trade prominence over an alternating foundation."),
    TakeStrategy("transformation", "Transformation arc", "Begin with A, move through a hybrid, and end with B."),
    TakeStrategy("layered-blend", "Layered blend", "Layer both melodies at different registers over one accompaniment."),
    TakeStrategy("foundation-swap", "Midpoint role reversal", "Change melody and accompaniment roles at the midpoint."),
)
DEFAULT_TAKE_STRATEGY_KEYS: tuple[str, ...] = tuple(item.key for item in TAKE_STRATEGIES[:5])


@dataclass(frozen=True)
class RemixSettings:
    target_bpm: int = 110
    target_key_pc: int | None = None
    target_mode: str = "major"
    mode_handling: str = "Preserve source mode"
    length_mode: str = "Shared full length"
    bars: int = 16
    start_bar_a: int = 0
    start_bar_b: int = 0
    section_bars: int = 8
    density: str = "Balanced"
    strategies: tuple[str, ...] = DEFAULT_TAKE_STRATEGY_KEYS
    variations_per_strategy: int = 1
    variation_strength: str = "Moderate"
    random_seed: int = 0
    melody_treatment_a: str = "full"
    melody_treatment_b: str = "full"
    auto_balance: bool = True
    # Audible user adjustments. These are separate from automatic normalization:
    # auto_balance evens out source dynamics, while these percentages deliberately
    # move the generated lead/backing relationship after normalization.
    lead_gain_percent: int = 100
    backing_gain_percent: int = 100
    backing_mode: str = "all-except-melody"
    preserve_signature_assets: bool = True
    signature_source: str = "auto"
    signature_placement: str = "intro-alone"
    # Optional source index sequence from the Assisted section planner.
    section_source_order: tuple[int, ...] = ()
    section_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemixTake:
    take_id: str
    name: str
    description: str
    path: str
    source_a: str
    source_b: str
    target_bpm: int
    target_key: str
    bars: int
    kind: str
    encoded_bpm: float = 0.0
    variation: int = 1
    source_paths: tuple[str, ...] = ()
    recipe: str = ""
    trial_id: str = ""
    trial_name: str = ""
    generation_run: int = 1
    status: str = "candidate"
    approved: bool = False
    created_at: str = ""
    decision_summary: tuple[str, ...] = ()
    exported_paths: tuple[str, ...] = ()
    # Revision lineage. Empty values identify an original generated Take.
    parent_take_id: str = ""
    parent_trial_id: str = ""
    parent_path: str = ""
    lineage_root_take_id: str = ""
    revision_generation: int = 0
    changed_sections: tuple[str, ...] = ()
    refinement_reason: str = ""
    refinement_settings: tuple[str, ...] = ()
    comparison_slot: str = "A"

    def to_dict(self) -> dict:
        return asdict(self)


def take_identity_key(take: "RemixTake") -> tuple[str, str, str]:
    """Return the exact identity of a Take across Trials and legacy sessions.

    ``take_id`` is normally project-unique, but older sessions and imported
    project histories can reuse labels such as ``take-01``.  Trial plus path
    keeps playback, approval, deletion, and Workbench handoff attached to the
    exact generated file the user clicked.
    """

    try:
        path = os.path.normcase(str(Path(take.path).resolve(strict=False)))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        path = str(getattr(take, "path", "") or "")
    return (str(take.trial_id or ""), str(take.take_id or ""), path)


def remix_take_from_dict(payload: dict) -> "RemixTake":
    """Load a Take while normalizing tuple fields and ignoring future keys.

    Project/session files from older versions omit lineage fields; future files
    may contain keys this build does not understand.  One loader keeps every
    restore path consistent and prevents a single optional field from dropping
    an otherwise valid Take.
    """

    from dataclasses import fields

    data = dict(payload or {})
    tuple_fields = {
        "source_paths", "decision_summary", "exported_paths",
        "changed_sections", "refinement_settings",
    }
    for key in tuple_fields:
        if isinstance(data.get(key), list):
            data[key] = tuple(data[key])
    allowed = {field.name for field in fields(RemixTake)}
    return RemixTake(**{key: value for key, value in data.items() if key in allowed})


@dataclass(frozen=True)
class _TimedMessage:
    tick: int
    order: int
    msg: mido.Message




def encoded_bpm_for_felt(profile: MidiProfile, target_felt_bpm: float) -> float:
    """Convert the user-facing pulse back to the MIDI quarter-note tempo.

    The pilot profiler folds half/double-time values into a practical felt-BPM
    range. A 2/2 march can therefore report 100 felt BPM while its MIDI tempo is
    quarter=200. Preserve the source's encoded/felt ratio when writing the remix
    so the output does not accidentally run at half speed.
    """
    encoded = float(profile.tempo_bpm or target_felt_bpm or 120.0)
    felt = float(profile.felt_bpm or encoded or 120.0)
    ratio = encoded / felt if felt > 0 else 1.0
    return max(20.0, min(600.0, float(target_felt_bpm) * ratio))


def _midi_text(value: str) -> str:
    """Return text that Standard MIDI latin-1 metadata can always encode."""
    return str(value).encode("latin-1", errors="replace").decode("latin-1")


def _safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    return cleaned[:72] or "remix"


def _absolute_messages(track: mido.MidiTrack) -> list[_TimedMessage]:
    result: list[_TimedMessage] = []
    tick = 0
    for order, msg in enumerate(track):
        tick += int(msg.time)
        result.append(_TimedMessage(tick=tick, order=order, msg=msg.copy(time=0)))
    return result


def _delta_track(events: Iterable[_TimedMessage], name: str, end_tick: int | None = None) -> mido.MidiTrack:
    """Convert absolute events to a safe delta-time track.

    Generated arrangements can trim a source while a note or sustain pedal is
    still active.  A raw end-of-track in that state can create stuck notes in
    hardware synths and in some FluidSynth/device combinations.  The remixer
    now closes every active note and releases sustain at the requested ending,
    then emits exactly one terminal end-of-track event.
    """

    sorted_events = sorted(events, key=lambda item: (max(0, int(item.tick)), int(item.order)))
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=_midi_text(name), time=0))
    last_tick = 0
    active_notes: dict[tuple[int, int], int] = {}
    used_channels: set[int] = set()

    for event in sorted_events:
        # Source tracks may carry their own EOT.  Keeping it would place a
        # terminal marker before the generated safety releases below.
        if event.msg.is_meta and event.msg.type == "end_of_track":
            continue
        tick = max(last_tick, int(event.tick))
        msg = event.msg.copy(time=tick - last_tick)
        track.append(msg)
        last_tick = tick

        if msg.is_meta or not hasattr(msg, "channel"):
            continue
        channel = int(msg.channel)
        used_channels.add(channel)
        if msg.type not in {"note_on", "note_off"} or not hasattr(msg, "note"):
            continue
        key = (channel, int(msg.note))
        velocity = int(getattr(msg, "velocity", 0) or 0)
        if msg.type == "note_on" and velocity > 0:
            active_notes[key] = active_notes.get(key, 0) + 1
        else:
            remaining = active_notes.get(key, 0) - 1
            if remaining > 0:
                active_notes[key] = remaining
            else:
                active_notes.pop(key, None)

    tail_tick = max(last_tick, int(end_tick or 0))
    first_tail_event = True

    # Release sustain before individual note-offs so the tail cannot be held by
    # a pedal message copied from the source section.
    for channel in sorted(used_channels):
        track.append(
            mido.Message(
                "control_change", channel=channel, control=64, value=0,
                time=(tail_tick - last_tick) if first_tail_event else 0,
            )
        )
        last_tick = tail_tick
        first_tail_event = False

    for (channel, note), count in sorted(active_notes.items()):
        for _ in range(max(1, int(count))):
            track.append(
                mido.Message(
                    "note_off", channel=channel, note=note, velocity=0,
                    time=(tail_tick - last_tick) if first_tail_event else 0,
                )
            )
            last_tick = tail_tick
            first_tail_event = False

    # Controller 123 is a final device-level guard.  It does not alter the
    # arrangement and is safe for melodic and percussion channels.
    for channel in sorted(used_channels):
        track.append(
            mido.Message(
                "control_change", channel=channel, control=123, value=0,
                time=(tail_tick - last_tick) if first_tail_event else 0,
            )
        )
        last_tick = tail_tick
        first_tail_event = False

    track.append(mido.MetaMessage("end_of_track", time=max(0, tail_tick - last_tick)))
    return track


def _remap_channel(msg: mido.Message, channel: int | None) -> mido.Message:
    if channel is None or msg.is_meta or not hasattr(msg, "channel"):
        return msg.copy()
    if int(getattr(msg, "channel", 0)) == 9:
        return msg.copy()
    return msg.copy(channel=max(0, min(15, int(channel))))


def _transpose_message(msg: mido.Message, semitones: int) -> mido.Message:
    if semitones == 0 or msg.is_meta or msg.type not in {"note_on", "note_off", "polytouch"} or not hasattr(msg, "note"):
        return msg.copy()
    if hasattr(msg, "channel") and int(msg.channel) == 9:
        return msg.copy()
    note = max(0, min(127, int(msg.note) + int(semitones)))
    return msg.copy(note=note)


def _mode_adjusted_note(
    note: int,
    *,
    transpose: int,
    source_tonic_pc: int | None,
    source_mode: str,
    target_tonic_pc: int | None,
    target_mode: str,
    mode_handling: str,
) -> int:
    transposed = int(note) + int(transpose)
    if (
        str(mode_handling).lower() != "adapt diatonic notes to target mode"
        or source_tonic_pc is None
        or target_tonic_pc is None
        or source_mode == target_mode
    ):
        return max(0, min(127, transposed))
    source_scale = (0, 2, 3, 5, 7, 8, 10) if source_mode == "minor" else (0, 2, 4, 5, 7, 9, 11)
    target_scale = (0, 2, 3, 5, 7, 8, 10) if target_mode == "minor" else (0, 2, 4, 5, 7, 9, 11)
    relative_pc = (int(note) - int(source_tonic_pc)) % 12
    if relative_pc not in source_scale:
        return max(0, min(127, transposed))
    degree = source_scale.index(relative_pc)
    desired_pc = (int(target_tonic_pc) + target_scale[degree]) % 12
    current_pc = transposed % 12
    delta = (desired_pc - current_pc + 6) % 12 - 6
    return max(0, min(127, transposed + delta))


def _transform_pitch_message(
    msg: mido.Message,
    semitones: int,
    *,
    source_tonic_pc: int | None = None,
    source_mode: str = "major",
) -> mido.Message:
    if msg.is_meta or msg.type not in {"note_on", "note_off", "polytouch"} or not hasattr(msg, "note"):
        return msg.copy()
    if hasattr(msg, "channel") and int(msg.channel) == 9:
        return msg.copy()
    note = _mode_adjusted_note(
        int(msg.note), transpose=semitones, source_tonic_pc=source_tonic_pc,
        source_mode=source_mode, target_tonic_pc=_TARGET_TONIC_PC,
        target_mode=_TARGET_MODE, mode_handling=_MODE_HANDLING,
    )
    return msg.copy(note=note)


def _scale_velocity(msg: mido.Message, factor: float) -> mido.Message:
    if msg.type != "note_on" or int(getattr(msg, "velocity", 0) or 0) <= 0:
        return msg.copy()
    velocity = max(1, min(127, int(round(int(msg.velocity) * float(factor)))))
    return msg.copy(velocity=velocity)


def _filter_voice_events(events: list[_TimedMessage], mode: str) -> list[_TimedMessage]:
    """Keep a likely top or bottom voice while preserving channel state.

    Piano reductions often place melody and inner harmony in one track. This
    conservative onset-based splitter lets the user extract the highest or
    lowest simultaneous note before assigning a solo instrument.
    """
    mode = str(mode or "full").lower()
    if mode not in {"top", "bottom"}:
        return events
    state = [event for event in events if event.msg.type not in {"note_on", "note_off"}]
    note_events = [event for event in events if event.msg.type in {"note_on", "note_off"}]
    onsets: dict[int, list[_TimedMessage]] = {}
    for event in note_events:
        if event.msg.type == "note_on" and int(getattr(event.msg, "velocity", 0) or 0) > 0:
            onsets.setdefault(event.tick, []).append(event)
    selected: set[tuple[int, int, int]] = set()
    for tick, group in onsets.items():
        chosen = max(group, key=lambda e: int(e.msg.note)) if mode == "top" else min(group, key=lambda e: int(e.msg.note))
        selected.add((tick, int(chosen.msg.channel), int(chosen.msg.note)))
    active: set[tuple[int, int]] = set()
    output = list(state)
    for event in sorted(note_events, key=lambda item: (item.tick, item.order)):
        msg = event.msg
        key = (int(getattr(msg, "channel", 0)), int(getattr(msg, "note", 0)))
        if msg.type == "note_on" and int(getattr(msg, "velocity", 0) or 0) > 0:
            if (event.tick, key[0], key[1]) in selected:
                active.add(key)
                output.append(event)
        elif key in active:
            output.append(event)
            active.discard(key)
    return sorted(output, key=lambda item: (item.tick, item.order))


def _extract_segment(
    midi: mido.MidiFile,
    track_index: int,
    start_tick: int,
    end_tick: int,
    output_start_tick: int,
    output_length_ticks: int,
    transpose: int = 0,
    velocity_factor: float = 1.0,
    channel: int | None = None,
    voice_mode: str = "full",
    source_tonic_pc: int | None = None,
    source_mode: str = "major",
) -> list[_TimedMessage]:
    if track_index < 0 or track_index >= len(midi.tracks) or end_tick <= start_tick:
        return []
    events = _absolute_messages(midi.tracks[track_index])
    source_length = max(1, int(end_tick - start_tick))
    scale = float(output_length_ticks) / float(source_length)
    initial_state: dict[tuple[str, int, int], mido.Message] = {}
    active: dict[tuple[int, int], tuple[int, int]] = {}
    output: list[_TimedMessage] = []
    order = 10

    # Capture the last useful channel state before the selected segment.
    for event in events:
        if event.tick >= start_tick:
            break
        msg = event.msg
        if msg.is_meta or not hasattr(msg, "channel"):
            continue
        ch = int(msg.channel)
        if msg.type == "program_change":
            initial_state[("program", ch, 0)] = msg
        elif msg.type == "control_change" and int(getattr(msg, "control", -1)) in {0, 7, 10, 11, 32, 64, 91, 93}:
            initial_state[("cc", ch, int(msg.control))] = msg
        elif msg.type == "pitchwheel":
            initial_state[("pitch", ch, 0)] = msg
        elif msg.type == "note_on" and int(msg.velocity) > 0:
            active[(ch, int(msg.note))] = (event.tick, int(msg.velocity))
        elif msg.type in {"note_off", "note_on"}:
            active.pop((ch, int(msg.note)), None)

    for msg in initial_state.values():
        mapped = _remap_channel(_transform_pitch_message(msg, transpose, source_tonic_pc=source_tonic_pc, source_mode=source_mode), channel)
        output.append(_TimedMessage(output_start_tick, order, mapped)); order += 1
    for (ch, note), (_, velocity) in active.items():
        seed = mido.Message("note_on", channel=ch, note=note, velocity=velocity, time=0)
        seed = _scale_velocity(_remap_channel(_transform_pitch_message(seed, transpose, source_tonic_pc=source_tonic_pc, source_mode=source_mode), channel), velocity_factor)
        output.append(_TimedMessage(output_start_tick, order, seed)); order += 1

    active_output: set[tuple[int, int]] = set()
    for event in events:
        if event.tick < start_tick:
            continue
        if event.tick > end_tick:
            break
        msg = event.msg
        if msg.is_meta:
            continue
        relative = max(0, event.tick - start_tick)
        tick = output_start_tick + int(round(relative * scale))
        mapped = _transform_pitch_message(msg, transpose, source_tonic_pc=source_tonic_pc, source_mode=source_mode)
        mapped = _remap_channel(mapped, channel)
        mapped = _scale_velocity(mapped, velocity_factor)
        if mapped.type == "note_on" and int(getattr(mapped, "velocity", 0) or 0) > 0:
            active_output.add((int(mapped.channel), int(mapped.note)))
        elif mapped.type in {"note_off", "note_on"} and hasattr(mapped, "note"):
            active_output.discard((int(mapped.channel), int(mapped.note)))
        output.append(_TimedMessage(tick, order, mapped)); order += 1

    end_output_tick = output_start_tick + int(output_length_ticks)
    for ch, note in sorted(active_output):
        output.append(_TimedMessage(end_output_tick, order, mido.Message("note_off", channel=ch, note=note, velocity=0, time=0))); order += 1
    return _filter_voice_events(output, voice_mode)


def _profile_transpose(profile: MidiProfile, target_pc: int | None) -> int:
    return signed_pitch_class_shift(profile.tonic_pc, target_pc)


def _source_segment_ticks(profile: MidiProfile, start_bar: int, bars: int) -> tuple[int, int]:
    bar_ticks = max(1, int(round(profile.bar_ticks)))
    start = max(0, int(start_bar)) * bar_ticks
    end = min(int(profile.total_ticks), start + max(1, int(bars)) * bar_ticks)
    if end <= start:
        start = 0
        end = min(int(profile.total_ticks), max(1, int(bars)) * bar_ticks)
    return start, max(start + 1, end)


def _target_bar_ticks(profile_a: MidiProfile, tpb: int) -> int:
    return max(1, int(round(tpb * quarter_beats_per_bar(profile_a.numerator, profile_a.denominator))))


def _meta_track(profile_a: MidiProfile, settings: RemixSettings, title: str, source_names: Sequence[str]) -> mido.MidiTrack:
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="MIDI Remixer conductor", time=0))
    track.append(mido.MetaMessage("text", text=_midi_text(f"Generated by MIDI Remixer: {title}"), time=0))
    track.append(mido.MetaMessage("text", text=_midi_text("Sources: " + " + ".join(source_names)), time=0))
    encoded_bpm = encoded_bpm_for_felt(profile_a, settings.target_bpm)
    track.append(mido.MetaMessage("text", text=_midi_text(f"Target pulse: {settings.target_bpm} felt BPM; MIDI quarter-note tempo: {encoded_bpm:.3f} BPM"), time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(encoded_bpm), time=0))
    track.append(mido.MetaMessage("time_signature", numerator=profile_a.numerator, denominator=profile_a.denominator, clocks_per_click=24, notated_32nd_notes_per_beat=8, time=0))
    target_key_pc = profile_a.tonic_pc if settings.target_key_pc is None else settings.target_key_pc
    if target_key_pc is not None:
        effective_mode = settings.target_mode if settings.mode_handling == "Adapt diatonic notes to target mode" else profile_a.mode
        key = NOTE_NAMES[target_key_pc % 12] + ("m" if effective_mode == "minor" else "")
        try:
            track.append(mido.MetaMessage("key_signature", key=key, time=0))
        except ValueError:
            track.append(mido.MetaMessage("text", text=_midi_text(f"Target key metadata unavailable: {key}"), time=0))
    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def _source_track_events(
    midi: mido.MidiFile,
    profile: MidiProfile,
    track_index: int,
    start_bar: int,
    bars: int,
    output_start_tick: int,
    output_length_ticks: int,
    transpose: int,
    velocity_factor: float,
    channel: int | None,
    voice_mode: str = "full",
) -> list[_TimedMessage]:
    start, end = _source_segment_ticks(profile, start_bar, bars)
    track_profile = next((item for item in profile.note_tracks if item.index == track_index), None)
    is_percussion = bool(track_profile and (9 in track_profile.channels or "percussion" in track_profile.likely_role.casefold() or "drum" in track_profile.likely_role.casefold()))
    if is_percussion:
        # General MIDI drums must stay on channel 10 and must never be pitched.
        channel = 9
        transpose = 0
        voice_mode = "full"
    if _AUTO_BALANCE_ENABLED and track_profile and track_profile.median_velocity:
        role_target = 88.0 if track_index == profile.melody_track_index else 70.0 if track_index == profile.accompaniment_track_index else 76.0
        normalization = max(0.55, min(1.60, role_target / max(1.0, float(track_profile.median_velocity))))
        velocity_factor *= normalization
    return _extract_segment(
        midi, track_index, start, end,
        output_start_tick=output_start_tick,
        output_length_ticks=output_length_ticks,
        transpose=transpose,
        velocity_factor=velocity_factor,
        channel=channel,
        voice_mode=voice_mode,
        source_tonic_pc=profile.tonic_pc,
        source_mode=profile.mode,
    )


def _source_all_track_events(
    midi: mido.MidiFile,
    profile: MidiProfile,
    start_bar: int,
    bars: int,
    output_start_tick: int,
    output_length_ticks: int,
    transpose: int,
    velocity_factor: float,
    channel_offset: int = 0,
    *,
    lead_gain: float = 1.0,
    backing_gain: float = 1.0,
) -> list[_TimedMessage]:
    """Extract every pitched note track for whole-section/texture recipes.

    This fallback lets single-track files and merged piano reductions contribute
    as complete sections even when melody/foundation separation is uncertain.
    """
    events: list[_TimedMessage] = []
    tracks = [item for item in profile.note_tracks if item.note_count > 0]
    for position, track in enumerate(tracks):
        is_percussion = 9 in track.channels or "percussion" in track.likely_role.casefold() or "drum" in track.likely_role.casefold()
        channel = 9 if is_percussion else (channel_offset + position) % 15
        if not is_percussion and channel >= 9:
            channel += 1
        channel = min(15, channel)
        role_gain = lead_gain if track.index == profile.melody_track_index else backing_gain
        events.extend(_source_track_events(
            midi, profile, track.index, start_bar, bars,
            output_start_tick, output_length_ticks, transpose,
            velocity_factor * role_gain, channel, "full",
        ))
    return events


def _source_backing_events(
    midi: mido.MidiFile,
    profile: MidiProfile,
    start_bar: int,
    bars: int,
    output_start_tick: int,
    output_length_ticks: int,
    transpose: int,
    velocity_factor: float,
    channel_offset: int = 1,
    *,
    mode: str = "all-except-melody",
    backing_gain: float = 1.0,
) -> list[_TimedMessage]:
    """Extract the source backing, preserving more than one chord track.

    The common mashup request is not merely "one harmony track". It is drums,
    bass, chords, riffs, fills, and supporting texture with the detected lead
    removed. Foundation-only remains available for compatibility.
    """
    if str(mode).casefold() == "foundation-only" and profile.accompaniment_track_index is not None:
        return _source_track_events(
            midi, profile, profile.accompaniment_track_index, start_bar, bars,
            output_start_tick, output_length_ticks, transpose, velocity_factor * backing_gain,
            channel_offset, "full",
        )
    events: list[_TimedMessage] = []
    tracks = [item for item in profile.note_tracks if item.note_count > 0 and item.index != profile.melody_track_index]
    for position, track in enumerate(tracks):
        is_percussion = 9 in track.channels or "percussion" in track.likely_role.casefold() or "drum" in track.likely_role.casefold()
        channel = 9 if is_percussion else (channel_offset + position) % 15
        if not is_percussion and channel >= 9:
            channel += 1
        channel = min(15, channel)
        role_factor = 1.0
        if is_percussion:
            role_factor = 0.92
        elif track.index == profile.accompaniment_track_index:
            role_factor = 1.0
        else:
            role_factor = 0.86
        events.extend(_source_track_events(
            midi, profile, track.index, start_bar, bars,
            output_start_tick, output_length_ticks, transpose,
            velocity_factor * role_factor * backing_gain, channel, "full",
        ))
    return events


def _shift_events(events: list[_TimedMessage], ticks: int) -> list[_TimedMessage]:
    if not ticks:
        return list(events)
    return [_TimedMessage(max(0, item.tick + int(ticks)), item.order, item.msg.copy()) for item in events]


def _signature_events(
    midi: mido.MidiFile,
    profile: MidiProfile,
    asset: dict,
    *,
    bar_ticks: int,
    transpose: int,
) -> tuple[list[_TimedMessage], int]:
    start = max(0, int(asset.get("start_bar", 0)))
    end = max(start + 1, int(asset.get("end_bar", start + 1)))
    length = max(1, end - start)
    length_ticks = length * bar_ticks
    events: list[_TimedMessage] = []
    for position, track_index in enumerate(asset.get("track_indices", [])):
        track_profile = next((item for item in profile.note_tracks if item.index == int(track_index)), None)
        is_percussion = bool(track_profile and (9 in track_profile.channels or "percussion" in track_profile.likely_role.casefold() or "drum" in track_profile.likely_role.casefold()))
        channel = 9 if is_percussion else (position + 6) % 15
        if not is_percussion and channel >= 9:
            channel += 1
        channel = min(15, channel)
        events.extend(_source_track_events(
            midi, profile, int(track_index), start, length, 0, length_ticks,
            transpose, 1.0, channel, "full",
        ))
    return events, length


def write_signature_preview(
    profile: MidiProfile,
    output_path: str | Path,
    *,
    target_bpm: int | None = None,
) -> Path:
    asset = primary_signature_asset(profile)
    if not asset:
        raise ValueError("No signature-opening candidate is available for this source.")
    midi = mido.MidiFile(profile.path)
    tpb = 480
    bar_ticks = _target_bar_ticks(profile, tpb)
    events, bars = _signature_events(midi, profile, asset, bar_ticks=bar_ticks, transpose=0)
    settings = RemixSettings(
        target_bpm=int(target_bpm or profile.felt_bpm or 120), bars=bars,
        preserve_signature_assets=False, strategies=("section-exchange",),
    )
    target = Path(output_path)
    if target.suffix.casefold() not in {".mid", ".midi"}:
        target = target.with_suffix(".mid")
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_take(
        target, profile, settings, f"{profile.title} signature opening preview",
        [profile.title], [(str(asset.get("label") or "Signature opening"), events)],
        bars * bar_ticks,
    )
    return target


def _take_path(output_dir: Path, prefix: str, number: int, name: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{prefix}-take-{number:02d}-{_safe_slug(name).lower()}.mid"


def _write_take(
    path: Path,
    profile_a: MidiProfile,
    settings: RemixSettings,
    title: str,
    source_names: Sequence[str],
    tracks: Sequence[tuple[str, list[_TimedMessage]]],
    end_tick: int,
) -> None:
    out = mido.MidiFile(type=1, ticks_per_beat=480)
    out.tracks.append(_meta_track(profile_a, settings, title, source_names))
    for track_name, events in tracks:
        out.tracks.append(_delta_track(events, track_name, end_tick=end_tick))
    out.save(path)


def _generate_remix_takes_once(
    profile_a: MidiProfile,
    profile_b: MidiProfile,
    output_directory: str | Path,
    settings: RemixSettings,
    *,
    take_number_offset: int = 0,
    variation_number: int = 1,
) -> list[RemixTake]:
    global _AUTO_BALANCE_ENABLED
    _AUTO_BALANCE_ENABLED = bool(settings.auto_balance)

    roles_a = profile_a.melody_track_index is not None and profile_a.accompaniment_track_index is not None and profile_a.melody_track_index != profile_a.accompaniment_track_index
    roles_b = profile_b.melody_track_index is not None and profile_b.accompaniment_track_index is not None and profile_b.melody_track_index != profile_b.accompaniment_track_index
    roles_available = roles_a and roles_b

    selected = [key for key in (settings.strategies or DEFAULT_TAKE_STRATEGY_KEYS) if key in {item.key for item in TAKE_STRATEGIES}]
    if not selected:
        raise ValueError("Choose at least one Take strategy.")
    selected_set = set(selected)
    requested_section_order = tuple(
        int(index) for index in settings.section_source_order if int(index) in {0, 1}
    )
    section_source_order = requested_section_order or (0, 1)

    midi_a = mido.MidiFile(profile_a.path)
    midi_b = mido.MidiFile(profile_b.path)
    target_pc = profile_a.tonic_pc if settings.target_key_pc is None else settings.target_key_pc
    transpose_a = _profile_transpose(profile_a, target_pc)
    transpose_b = _profile_transpose(profile_b, target_pc)
    tpb = 480
    bar_ticks = _target_bar_ticks(profile_a, tpb)
    bars = max(4, int(settings.bars))
    total_ticks = bars * bar_ticks
    section_bars = max(2, min(int(settings.section_bars), bars))
    density = str(settings.density or "Balanced")
    prefix = _safe_slug(f"{profile_a.title}-{profile_b.title}")
    output_dir = Path(output_directory)
    effective_mode = settings.target_mode if settings.mode_handling == "Adapt diatonic notes to target mode" else profile_a.mode
    target_key = key_name(target_pc, effective_mode)
    encoded_target_bpm = encoded_bpm_for_felt(profile_a, settings.target_bpm)
    results: list[RemixTake] = []

    start_a = int(settings.start_bar_a)
    start_b = int(settings.start_bar_b)
    lead_gain = max(0.20, min(2.00, int(settings.lead_gain_percent or 100) / 100.0))
    backing_gain = max(0.10, min(2.00, int(settings.backing_gain_percent or 100) / 100.0))
    signature_profile: MidiProfile | None = None
    signature_midi: mido.MidiFile | None = None
    signature_asset: dict | None = None
    signature_transpose = 0
    requested_signature = str(settings.signature_source or "auto").casefold()
    if settings.preserve_signature_assets and str(settings.signature_placement).casefold() != "none":
        preferred = requested_signature
        if preferred == "auto":
            if selected_set == {"melody-a-accompaniment-b"}:
                preferred = "b"
            elif selected_set == {"melody-b-accompaniment-a"}:
                preferred = "a"
            else:
                candidates = [("a", primary_signature_asset(profile_a)), ("b", primary_signature_asset(profile_b))]
                candidates = [(key, asset) for key, asset in candidates if asset and asset.get("protect_default")]
                preferred = max(candidates, key=lambda item: int(item[1].get("confidence", 0)))[0] if candidates else "none"
        if preferred == "a":
            signature_profile, signature_midi, signature_asset, signature_transpose = profile_a, midi_a, primary_signature_asset(profile_a), transpose_a
        elif preferred == "b":
            signature_profile, signature_midi, signature_asset, signature_transpose = profile_b, midi_b, primary_signature_asset(profile_b), transpose_b
        if signature_asset and not signature_asset.get("protect_default") and requested_signature == "auto":
            signature_profile = signature_midi = signature_asset = None
        if signature_asset and signature_profile is profile_a:
            start_a = max(start_a, int(signature_asset.get("end_bar", start_a)))
        elif signature_asset and signature_profile is profile_b:
            start_b = max(start_b, int(signature_asset.get("end_bar", start_b)))

    signature_events: list[_TimedMessage] = []
    signature_bars = 0
    if signature_profile and signature_midi and signature_asset:
        signature_events, signature_bars = _signature_events(
            signature_midi, signature_profile, signature_asset,
            bar_ticks=bar_ticks, transpose=signature_transpose,
        )

    def add_take(
        name: str, description: str, kind: str, tracks: Sequence[tuple[str, list[_TimedMessage]]],
        end_tick: int = total_ticks, contribution_summary: Sequence[str] = (),
    ) -> None:
        number = take_number_offset + len(results) + 1
        display_name = name if variation_number == 1 else f"{name} — Variation {variation_number}"
        final_tracks = list(tracks)
        final_end_tick = end_tick
        signature_summary: tuple[str, ...] = ()
        if signature_events and signature_asset and signature_profile:
            pre_roll_ticks = signature_bars * bar_ticks
            final_tracks = [(str(signature_asset.get("label") or "Protected signature opening") + f" - {signature_profile.title}", signature_events)] + [
                (track_name, _shift_events(events, pre_roll_ticks)) for track_name, events in final_tracks
            ]
            final_end_tick += pre_roll_ticks
            description += (
                f" {signature_profile.title}'s {str(signature_asset.get('label') or 'signature opening').casefold()} "
                f"plays alone for {signature_bars} bar(s) before the companion material enters."
            )
            summary_items = [
                f"Protected signature asset: {signature_profile.title} bars {int(signature_asset.get('start_bar', 0)) + 1}-{int(signature_asset.get('end_bar', 0))}.",
                "Companion entry: after the protected opening; no automatic overlay during the intro.",
            ]
            if str(settings.signature_placement).casefold() in {"intro-and-outro", "opening-and-outro"}:
                final_tracks.append((
                    str(signature_asset.get("label") or "Protected signature return") + f" return - {signature_profile.title}",
                    _shift_events(signature_events, final_end_tick),
                ))
                final_end_tick += pre_roll_ticks
                description += " The protected passage returns once as an outro after the mash completes."
                summary_items.append("Signature return: repeated once as an outro; it is not looped continuously.")
            signature_summary = tuple(summary_items)
        display_description = description if variation_number == 1 else f"{description} Variation {variation_number} changes source entry points, block size, or density within controlled limits."
        path = _take_path(output_dir, prefix, number, display_name)
        _write_take(path, profile_a, settings, display_name, [profile_a.title, profile_b.title], final_tracks, final_end_tick)
        results.append(RemixTake(
            take_id=f"take-{number:02d}", name=display_name, description=display_description,
            path=str(path.resolve()), source_a=profile_a.path, source_b=profile_b.path,
            target_bpm=int(settings.target_bpm), target_key=target_key,
            bars=max(1, int(round(final_end_tick / bar_ticks))), kind=kind,
            encoded_bpm=round(encoded_target_bpm, 3), variation=variation_number,
            source_paths=(profile_a.path, profile_b.path), recipe=kind,
            decision_summary=tuple(contribution_summary) + signature_summary + (
                f"User balance adjustment: lead {int(settings.lead_gain_percent)}%; backing {int(settings.backing_gain_percent)}%.",
                f"Source entry bars: {profile_a.title} bar {start_a + 1}; {profile_b.title} bar {start_b + 1}.",
                *(
                    ("Section leads: " + " → ".join(
                        (profile_a.title, profile_b.title)[index] for index in section_source_order
                    ) + ".",)
                    if requested_section_order else ()
                ),
            ),
        ))

    if "melody-a-accompaniment-b" in selected_set:
        if not roles_available:
            raise ValueError("This recipe needs separable melody and foundation roles. Try top-voice extraction, manual track mapping, Alternating sections, or Transformation arc.")
        melody_a = _source_track_events(midi_a, profile_a, profile_a.melody_track_index, start_a, bars, 0, total_ticks, transpose_a, 1.02 * lead_gain, 0, settings.melody_treatment_a)
        lower_b = _source_backing_events(
            midi_b, profile_b, start_b, bars, 0, total_ticks, transpose_b, 0.92, 1,
            mode=settings.backing_mode, backing_gain=backing_gain,
        )
        add_take(
            f"{profile_a.title} melody over {profile_b.title} backing",
            "Source A supplies only its lead melody; Source B supplies drums, bass, chords, riffs, fills, and supporting texture with its detected lead removed. Both are aligned to the same musical entry, tempo, and target key.",
            "melody-a-accompaniment-b",
            [(f"Melody - {profile_a.title}", melody_a), (f"Backing without lead - {profile_b.title}", lower_b)],
            contribution_summary=(
                f"{profile_a.title} provides its complete detected lead-melody track; voice extraction is used only when explicitly selected.",
                f"{profile_b.title} provides every detected supporting track except its lead: drums, bass, chords, riffs, fills, and texture remain.",
                "Percussion stays on General MIDI channel 10 and is never transposed.",
            ),
        )

    if "melody-b-accompaniment-a" in selected_set:
        if not roles_available:
            raise ValueError("This recipe needs separable melody and foundation roles. Try top-voice extraction, manual track mapping, Alternating sections, or Transformation arc.")
        melody_b = _source_track_events(midi_b, profile_b, profile_b.melody_track_index, start_b, bars, 0, total_ticks, transpose_b, 1.02 * lead_gain, 2, settings.melody_treatment_b)
        lower_a = _source_backing_events(
            midi_a, profile_a, start_a, bars, 0, total_ticks, transpose_a, 0.92, 1,
            mode=settings.backing_mode, backing_gain=backing_gain,
        )
        add_take(
            f"{profile_b.title} melody over {profile_a.title} backing",
            "The second source supplies only its lead melody while the first supplies its fuller backing with the detected lead removed.",
            "melody-b-accompaniment-a",
            [(f"Melody - {profile_b.title}", melody_b), (f"Backing without lead - {profile_a.title}", lower_a)],
            contribution_summary=(
                f"{profile_b.title} provides its complete detected lead-melody track; voice extraction is used only when explicitly selected.",
                f"{profile_a.title} provides every detected supporting track except its lead: drums, bass, chords, riffs, fills, and texture remain.",
                "Percussion stays on General MIDI channel 10 and is never transposed.",
            ),
        )

    if "section-exchange" in selected_set:
        section_events_a: list[_TimedMessage] = []
        section_events_b: list[_TimedMessage] = []
        block = 0
        cursor = 0
        while cursor < bars:
            this_bars = min(section_bars, bars - cursor)
            block_ticks = this_bars * bar_ticks
            source_index = section_source_order[block % len(section_source_order)]
            if source_index == 0:
                section_events_a += _source_all_track_events(
                    midi_a, profile_a, start_a + cursor, this_bars, cursor * bar_ticks, block_ticks, transpose_a, 0.94, 0,
                    lead_gain=lead_gain, backing_gain=backing_gain
                )
            else:
                section_events_b += _source_all_track_events(
                    midi_b, profile_b, start_b + cursor, this_bars, cursor * bar_ticks, block_ticks, transpose_b, 0.94, 4,
                    lead_gain=lead_gain, backing_gain=backing_gain
                )
            cursor += this_bars
            block += 1
        add_take(
            "Alternating complete sections",
            f"Complete {section_bars}-bar blocks alternate between the two songs. Every instrument track in the active block is retained, including drums, bass, harmony, countermelodies, and fills.",
            "section-exchange",
            [(f"Complete sections - {profile_a.title}", section_events_a), (f"Complete sections - {profile_b.title}", section_events_b)],
            contribution_summary=(
                f"{profile_a.title} contributes complete alternating sections with all detected instrument tracks.",
                f"{profile_b.title} contributes complete alternating sections with all detected instrument tracks.",
                "No top-note melody reduction is used in this arrangement.",
            ),
        )

    if "conversation" in selected_set:
        if not roles_available:
            raise ValueError("This recipe needs separable melody and foundation roles. Try top-voice extraction, manual track mapping, Alternating sections, or Transformation arc.")
        conversation_a: list[_TimedMessage] = []
        conversation_b: list[_TimedMessage] = []
        foundation: list[_TimedMessage] = []
        cursor = 0
        block = 0
        while cursor < bars:
            this_bars = min(section_bars, bars - cursor)
            block_ticks = this_bars * bar_ticks
            out_start = cursor * bar_ticks
            lead_index = section_source_order[block % len(section_source_order)]
            a_factor = 1.03 if lead_index == 0 else 0.72
            b_factor = 1.02 if lead_index == 1 else 0.70
            conversation_a += _source_track_events(midi_a, profile_a, profile_a.melody_track_index, start_a + cursor, this_bars, out_start, block_ticks, transpose_a, a_factor * lead_gain, 0, settings.melody_treatment_a)
            if density != "Lean" or block % 2 == 1:
                conversation_b += _source_track_events(midi_b, profile_b, profile_b.melody_track_index, start_b + cursor, this_bars, out_start, block_ticks, transpose_b - 12 if density == "Full" else transpose_b, b_factor * lead_gain, 2, settings.melody_treatment_b)
            use_b = section_source_order[block % len(section_source_order)] == 1
            foundation += _source_track_events(
                midi_b if use_b else midi_a,
                profile_b if use_b else profile_a,
                profile_b.accompaniment_track_index if use_b else profile_a.accompaniment_track_index,
                (start_b if use_b else start_a) + cursor,
                this_bars, out_start, block_ticks,
                transpose_b if use_b else transpose_a,
                (0.78 if density == "Full" else 0.88) * backing_gain,
                1,
            )
            cursor += this_bars
            block += 1
        add_take(
            "Melodic conversation",
            "The two upper parts trade prominence over an alternating lower foundation. The result tests recognizable motifs as musical responses rather than a single melody replacement.",
            "conversation",
            [(f"Voice A - {profile_a.title}", conversation_a), (f"Voice B - {profile_b.title}", conversation_b), ("Alternating foundation", foundation)],
        )

    if "transformation" in selected_set:
        intro_bars = max(2, min(section_bars, bars // 3 if bars >= 6 else 2))
        outro_bars = intro_bars
        middle_bars = max(1, bars - intro_bars - outro_bars)
        transformation_a: list[_TimedMessage] = []
        transformation_b: list[_TimedMessage] = []
        intro_ticks = intro_bars * bar_ticks
        middle_ticks = middle_bars * bar_ticks
        outro_ticks = outro_bars * bar_ticks
        transformation_a += _source_all_track_events(
            midi_a, profile_a, start_a, intro_bars, 0, intro_ticks, transpose_a, 0.95, 0,
            lead_gain=lead_gain, backing_gain=backing_gain,
        )

        if roles_a and roles_b:
            transformation_a += _source_track_events(midi_a, profile_a, profile_a.melody_track_index, start_a + intro_bars, middle_bars, intro_ticks, middle_ticks, transpose_a, 1.0 * lead_gain, 0, settings.melody_treatment_a)
            transformation_b += _source_backing_events(midi_b, profile_b, start_b + intro_bars, middle_bars, intro_ticks, middle_ticks, transpose_b, 0.88, 1, mode=settings.backing_mode, backing_gain=backing_gain)
        else:
            transformation_a += _source_all_track_events(
                midi_a, profile_a, start_a + intro_bars, middle_bars, intro_ticks, middle_ticks, transpose_a, 0.68, 0,
                lead_gain=lead_gain, backing_gain=backing_gain,
            )
            transformation_b += _source_all_track_events(
                midi_b, profile_b, start_b + intro_bars, middle_bars, intro_ticks, middle_ticks, transpose_b, 0.68, 4,
                lead_gain=lead_gain, backing_gain=backing_gain,
            )

        outro_start = intro_ticks + middle_ticks
        transformation_b += _source_all_track_events(
            midi_b, profile_b, start_b + intro_bars + middle_bars, outro_bars, outro_start, outro_ticks, transpose_b, 0.95, 4,
            lead_gain=lead_gain, backing_gain=backing_gain,
        )
        add_take(
            "Transformation arc",
            "The arrangement begins with the complete texture of the first song, becomes a deliberate hybrid, and ends with the complete texture of the second song.",
            "transformation",
            [(f"Complete opening / hybrid - {profile_a.title}", transformation_a), (f"Hybrid / complete ending - {profile_b.title}", transformation_b)],
            contribution_summary=(
                f"{profile_a.title} opens with its complete arrangement, not only its highest notes.",
                f"The hybrid uses a detected lead over {profile_b.title}'s complete backing when roles are dependable.",
                f"{profile_b.title} closes with its complete arrangement.",
            ),
        )

    if "layered-blend" in selected_set:
        if not roles_available:
            raise ValueError("This recipe needs separable melody and foundation roles. Try top-voice extraction, manual track mapping, Alternating sections, or Transformation arc.")
        melody_a = _source_track_events(midi_a, profile_a, profile_a.melody_track_index, start_a, bars, 0, total_ticks, transpose_a, 1.0 * lead_gain, 0, settings.melody_treatment_a)
        b_octave = -12 if (profile_b.note_tracks[profile_b.melody_track_index].mean_note or 72) >= (profile_a.note_tracks[profile_a.melody_track_index].mean_note or 72) else 12
        melody_b = _source_track_events(midi_b, profile_b, profile_b.melody_track_index, start_b, bars, 0, total_ticks, transpose_b + b_octave, (0.62 if density != "Full" else 0.74) * lead_gain, 2, settings.melody_treatment_b)
        lower = _source_backing_events(midi_b, profile_b, start_b, bars, 0, total_ticks, transpose_b, 0.82, 1, mode=settings.backing_mode, backing_gain=backing_gain)
        add_take(
            "Layered blend",
            "Both melodic parts are retained in separated registers over Source B's accompaniment. Lower density keeps the secondary voice subdued rather than deleting it.",
            "layered-blend",
            [(f"Primary voice - {profile_a.title}", melody_a), (f"Secondary voice - {profile_b.title}", melody_b), (f"Complete backing - {profile_b.title}", lower)],
            contribution_summary=(
                f"Both detected lead tracks are retained in separated registers.",
                f"{profile_b.title}'s drums, bass, chords, riffs, fills, and supporting texture remain under the two leads.",
            ),
        )

    if "foundation-swap" in selected_set:
        if not roles_available:
            raise ValueError("This recipe needs separable melody and foundation roles. Try top-voice extraction, manual track mapping, Alternating sections, or Transformation arc.")
        first_bars = max(2, bars // 2)
        second_bars = max(1, bars - first_bars)
        first_ticks = first_bars * bar_ticks
        second_ticks = second_bars * bar_ticks
        first_source = section_source_order[0]
        second_source = next((index for index in section_source_order[1:] if index != first_source), 1 - first_source)
        first_midi, second_midi = (midi_a, midi_b) if first_source == 0 else (midi_b, midi_a)
        first_profile, second_profile = (profile_a, profile_b) if first_source == 0 else (profile_b, profile_a)
        first_start, second_start = (start_a, start_b) if first_source == 0 else (start_b, start_a)
        first_transpose, second_transpose = (transpose_a, transpose_b) if first_source == 0 else (transpose_b, transpose_a)
        first_treatment, second_treatment = (settings.melody_treatment_a, settings.melody_treatment_b) if first_source == 0 else (settings.melody_treatment_b, settings.melody_treatment_a)
        first_melody = _source_track_events(first_midi, first_profile, first_profile.melody_track_index, first_start, first_bars, 0, first_ticks, first_transpose, 1.02 * lead_gain, 0, first_treatment)
        first_lower = _source_backing_events(second_midi, second_profile, second_start, first_bars, 0, first_ticks, second_transpose, 0.90, 1, mode=settings.backing_mode, backing_gain=backing_gain)
        second_melody = _source_track_events(second_midi, second_profile, second_profile.melody_track_index, second_start + first_bars, second_bars, first_ticks, second_ticks, second_transpose, 1.02 * lead_gain, 2, second_treatment)
        second_lower = _source_backing_events(first_midi, first_profile, first_start + first_bars, second_bars, first_ticks, second_ticks, first_transpose, 0.90, 3, mode=settings.backing_mode, backing_gain=backing_gain)
        add_take(
            "Midpoint role reversal",
            "The first half uses A's melody over B's foundation; at the midpoint both roles reverse. The handoff makes the source relationship audible as part of the form.",
            "foundation-swap",
            [("First-half melody", first_melody), ("First-half complete backing", first_lower), ("Second-half melody", second_melody), ("Second-half complete backing", second_lower)],
            contribution_summary=(
                f"First half: {first_profile.title}'s lead over {second_profile.title}'s complete backing.",
                f"Second half: {second_profile.title}'s lead over {first_profile.title}'s complete backing.",
            ),
        )

    manifest = {
        "format": "MIDI Remixer take manifest",
        "version": 4,
        "settings": asdict(settings),
        "selected_strategies": selected,
        "source_a": profile_a.to_dict(),
        "source_b": profile_b.to_dict(),
        "takes": [item.to_dict() for item in results],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / f"{prefix}-manifest.json", manifest)
    return results

def _varied_settings(settings: RemixSettings, profile_a: MidiProfile, profile_b: MidiProfile, variation: int) -> RemixSettings:
    if variation <= 1:
        return replace(settings, variations_per_strategy=1)
    seed = int(settings.random_seed or 0) + variation * 7919 + sum(ord(ch) for ch in (profile_a.title + profile_b.title))
    rng = random.Random(seed)
    strength = str(settings.variation_strength or "Moderate").lower()
    spread = 1 if strength == "low" else 2 if strength == "moderate" else 4
    max_a = max(0, int(profile_a.bars) - max(4, int(settings.bars)))
    max_b = max(0, int(profile_b.bars) - max(4, int(settings.bars)))
    start_a = max(0, min(max_a, int(settings.start_bar_a) + rng.randint(0, spread) * max(1, int(settings.section_bars))))
    start_b = max(0, min(max_b, int(settings.start_bar_b) + rng.randint(0, spread) * max(1, int(settings.section_bars))))
    section_delta = rng.choice([-2, 0, 2]) * spread
    section = max(2, min(int(settings.bars), int(settings.section_bars) + section_delta))
    density_options = [str(settings.density)] if strength == "low" else ["Lean", "Balanced", "Full"]
    density = rng.choice(density_options)
    return replace(
        settings,
        start_bar_a=start_a,
        start_bar_b=start_b,
        section_bars=section,
        density=density,
        variations_per_strategy=1,
    )


def generate_remix_takes(
    profile_a: MidiProfile,
    profile_b: MidiProfile,
    output_directory: str | Path,
    settings: RemixSettings,
) -> list[RemixTake]:
    """Generate any selected strategies with one or more controlled variations.

    The seven built-in strategies are starting recipes, not a cap. Repeated runs
    and variations_per_strategy can create as many Takes as the user wants.
    """
    global _AUTO_BALANCE_ENABLED, _MODE_HANDLING, _TARGET_TONIC_PC, _TARGET_MODE
    _AUTO_BALANCE_ENABLED = bool(settings.auto_balance)
    _MODE_HANDLING = str(settings.mode_handling)
    _TARGET_TONIC_PC = profile_a.tonic_pc if settings.target_key_pc is None else settings.target_key_pc
    _TARGET_MODE = str(settings.target_mode)
    variation_count = max(1, min(12, int(settings.variations_per_strategy or 1)))
    results: list[RemixTake] = []
    for variation in range(1, variation_count + 1):
        varied = _varied_settings(settings, profile_a, profile_b, variation)
        batch = _generate_remix_takes_once(
            profile_a, profile_b, output_directory, varied,
            take_number_offset=len(results), variation_number=variation,
        )
        results.extend(batch)
    output_dir = Path(output_directory)
    prefix = _safe_slug(f"{profile_a.title}-{profile_b.title}")
    manifest = {
        "format": "MIDI Remixer take manifest",
        "version": 4,
        "settings": asdict(settings),
        "source_a": profile_a.to_dict(),
        "source_b": profile_b.to_dict(),
        "takes": [item.to_dict() for item in results],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / f"{prefix}-manifest.json", manifest)
    return results


def export_take(take: RemixTake, target_path: str | Path) -> Path:
    source = Path(take.path)
    target = Path(target_path)
    if target.suffix.lower() not in {".mid", ".midi"}:
        target = target.with_suffix(".mid")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target
