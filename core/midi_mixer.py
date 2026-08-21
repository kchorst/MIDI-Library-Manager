from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import statistics
from typing import Iterable

import mido

from core.remix_profile import GM_PROGRAM_NAMES, gm_program_name


@dataclass(frozen=True)
class MixTrackInfo:
    track_index: int
    name: str
    channels: tuple[int, ...]
    program: int
    program_name: str
    note_count: int
    median_velocity: float
    min_note: int | None
    max_note: int | None
    max_polyphony: int
    polyphonic: bool
    likely_role: str
    source_hint: str
    instrumentation_warning: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TrackMix:
    track_index: int
    program: int | None = None
    volume: int = 100
    expression: int = 100
    pan: int = 64
    velocity_percent: int = 100
    octave_shift: int = 0
    mute: bool = False
    solo: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _track_name(track: mido.MidiTrack, fallback: str) -> str:
    for message in track:
        if message.type == "track_name":
            return str(message.name)
    return fallback


def _role_from_name(name: str) -> str:
    low = name.casefold()
    if "melody" in low or "voice" in low or "primary" in low:
        return "melody"
    if "accompaniment" in low or "foundation" in low or "lower" in low:
        return "accompaniment"
    if "bass" in low:
        return "bass"
    if "drum" in low or "percussion" in low:
        return "percussion"
    if "section" in low:
        return "section"
    return "part"


def _source_hint(name: str) -> str:
    if " - " in name:
        return name.split(" - ", 1)[-1].strip()
    return "Generated arrangement"


def _polyphony(track: mido.MidiTrack) -> int:
    tick = 0
    active: set[tuple[int, int]] = set()
    maximum = 0
    for message in track:
        tick += int(message.time)
        if message.type == "note_on" and int(message.velocity) > 0:
            active.add((int(message.channel), int(message.note)))
            maximum = max(maximum, len(active))
        elif message.type in {"note_off", "note_on"} and hasattr(message, "note"):
            active.discard((int(message.channel), int(message.note)))
    return maximum


def inspect_take_tracks(path: str | Path) -> list[MixTrackInfo]:
    midi = mido.MidiFile(path)
    result: list[MixTrackInfo] = []
    for index, track in enumerate(midi.tracks):
        notes: list[int] = []
        velocities: list[int] = []
        channels: set[int] = set()
        programs: list[int] = []
        for message in track:
            if hasattr(message, "channel"):
                channels.add(int(message.channel))
            if message.type == "program_change":
                programs.append(int(message.program))
            elif message.type == "note_on" and int(message.velocity) > 0:
                notes.append(int(message.note)); velocities.append(int(message.velocity))
        if not notes:
            continue
        name = _track_name(track, f"Track {index + 1}")
        program = programs[-1] if programs else 0
        maximum = _polyphony(track)
        role = _role_from_name(name)
        if 9 in channels:
            warning = "Percussion channel: instrument program and octave transposition are intentionally restricted."
        elif maximum > 1 and program in range(56, 80):
            warning = "Polyphonic part assigned to a nominal solo brass/reed/pipe patch; consider a section patch or top-voice extraction."
        elif maximum > 1:
            warning = "Polyphonic part. A solo-instrument patch may sound artificial unless voices are separated."
        else:
            warning = "Monophonic part suitable for a solo-instrument patch, subject to range and phrasing."
        result.append(MixTrackInfo(
            track_index=index,
            name=name,
            channels=tuple(sorted(channels)),
            program=program,
            program_name=gm_program_name(program),
            note_count=len(notes),
            median_velocity=round(statistics.median(velocities), 1),
            min_note=min(notes),
            max_note=max(notes),
            max_polyphony=maximum,
            polyphonic=maximum > 1,
            likely_role=role,
            source_hint=_source_hint(name),
            instrumentation_warning=warning,
        ))
    return result


def recommended_mix(tracks: Iterable[MixTrackInfo]) -> list[TrackMix]:
    result: list[TrackMix] = []
    for item in tracks:
        if item.likely_role == "melody":
            volume, expression, velocity = 104, 105, max(70, min(140, int(round(88 / max(1.0, item.median_velocity) * 100))))
        elif item.likely_role in {"accompaniment", "bass"}:
            volume, expression, velocity = 86, 88, max(60, min(125, int(round(70 / max(1.0, item.median_velocity) * 100))))
        else:
            volume, expression, velocity = 92, 94, max(65, min(130, int(round(76 / max(1.0, item.median_velocity) * 100))))
        result.append(TrackMix(
            track_index=item.track_index,
            program=item.program,
            volume=volume,
            expression=expression,
            pan=64,
            velocity_percent=velocity,
            octave_shift=0,
        ))
    return result


def _convert_track(track: mido.MidiTrack, mix: TrackMix, *, audible: bool) -> mido.MidiTrack:
    out = mido.MidiTrack()
    channel_state_written: set[int] = set()
    octave = int(mix.octave_shift) * 12
    for message in track:
        msg = message.copy()
        if hasattr(msg, "channel"):
            channel = int(msg.channel)
            is_percussion = channel == 9
            if channel not in channel_state_written:
                # Place mix controls at the first event without disturbing absolute time.
                original_time = int(msg.time)
                out.append(mido.Message("control_change", channel=channel, control=7, value=max(0, min(127, int(round(mix.volume * 1.27)))), time=original_time))
                out.append(mido.Message("control_change", channel=channel, control=11, value=max(0, min(127, int(round(mix.expression * 1.27)))), time=0))
                out.append(mido.Message("control_change", channel=channel, control=10, value=max(0, min(127, int(mix.pan))), time=0))
                if mix.program is not None and not is_percussion:
                    out.append(mido.Message("program_change", channel=channel, program=max(0, min(127, int(mix.program))), time=0))
                msg = msg.copy(time=0)
                channel_state_written.add(channel)
            if msg.type == "program_change":
                if mix.program is not None and not is_percussion:
                    msg = msg.copy(program=max(0, min(127, int(mix.program))))
            elif msg.type in {"note_on", "note_off", "polytouch"} and hasattr(msg, "note") and not is_percussion:
                msg = msg.copy(note=max(0, min(127, int(msg.note) + octave)))
            if msg.type == "note_on" and int(getattr(msg, "velocity", 0) or 0) > 0:
                velocity = int(round(int(msg.velocity) * int(mix.velocity_percent) / 100.0)) if audible else 0
                msg = msg.copy(velocity=max(0, min(127, velocity)))
        out.append(msg)
    return out


def apply_take_mix(source_path: str | Path, target_path: str | Path, mixes: Iterable[TrackMix]) -> Path:
    source = Path(source_path)
    target = Path(target_path)
    if target.suffix.lower() not in {".mid", ".midi"}:
        target = target.with_suffix(".mid")
    midi = mido.MidiFile(source)
    by_index = {int(item.track_index): item for item in mixes}
    soloed = {index for index, item in by_index.items() if item.solo}
    output = mido.MidiFile(type=midi.type, ticks_per_beat=midi.ticks_per_beat)
    for index, track in enumerate(midi.tracks):
        mix = by_index.get(index)
        if mix is None:
            output.tracks.append(mido.MidiTrack([message.copy() for message in track]))
            continue
        audible = not mix.mute and (not soloed or index in soloed)
        output.tracks.append(_convert_track(track, mix, audible=audible))
    target.parent.mkdir(parents=True, exist_ok=True)
    output.save(target)
    return target


def general_midi_programs() -> list[tuple[int, str]]:
    return list(enumerate(GM_PROGRAM_NAMES))
