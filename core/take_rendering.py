from __future__ import annotations

from pathlib import Path
import shutil

import mido


AUDIO_RENDER_BASE_GAIN = 0.5


def normalize_volume_percent(volume_percent: int | float | str | None, default: int = 100) -> int:
    """Return a safe 0..200 app master-volume percentage while preserving 0%.

    Several UI paths use 100% as the neutral playback reference.  This helper
    deliberately treats 0 as a real value, not as a fallback to 100.
    """
    if volume_percent is None:
        percent = default
    else:
        try:
            percent = int(round(float(volume_percent)))
        except (TypeError, ValueError):
            percent = int(default)
    return max(0, min(200, int(percent)))


def audio_gain_for_volume_percent(volume_percent: int | float | None) -> float:
    """Return the single gain scale used for both live playback and audio export.

    FluidSynth's practical 100% app volume has historically rendered at -g 0.5.
    The renderer keeps that reference level and maps the app's 0..200% volume slider
    linearly around it, so a saved take sounds the same in live playback and
    exported WAV/MP3. MIDI export still has to approximate volume by scaling
    note velocities because .mid files have no universal player-gain field.
    """
    percent = float(normalize_volume_percent(volume_percent))
    return max(0.0, min(1.0, AUDIO_RENDER_BASE_GAIN * (percent / 100.0)))


def apply_master_volume_percent(source_midi: str | Path, output_midi: str | Path, volume_percent: int) -> Path:
    """Write a MIDI copy with master playback volume approximated in note velocities.

    MIDI files do not have a universal "player gain" field. For exported takes,
    we preserve the user's take-level volume by scaling note-on velocities while
    keeping existing musical dynamics intact proportionally.
    """
    source = Path(source_midi)
    target = Path(output_midi)
    target.parent.mkdir(parents=True, exist_ok=True)
    volume_percent = normalize_volume_percent(volume_percent)
    if volume_percent == 100:
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return target

    factor = volume_percent / 100.0
    midi = mido.MidiFile(source)
    out = mido.MidiFile(type=midi.type, ticks_per_beat=midi.ticks_per_beat, charset=getattr(midi, "charset", "latin1"))
    for track in midi.tracks:
        new_track = mido.MidiTrack()
        for msg in track:
            if msg.type == "note_on" and int(getattr(msg, "velocity", 0) or 0) > 0:
                new_velocity = int(round(int(msg.velocity) * factor))
                if volume_percent > 0:
                    new_velocity = max(1, new_velocity)
                new_velocity = max(0, min(127, new_velocity))
                new_track.append(msg.copy(velocity=new_velocity))
            else:
                new_track.append(msg.copy())
        out.tracks.append(new_track)
    out.save(target)
    return target


def volume_chain_summary(master_volume_percent: int | float | str | None, mixer_volume_overrides: dict | None = None) -> str:
    """Human-readable audit text for the app's volume model.

    Master volume is the global listener/output level used by live playback and
    WAV/MP3 rendering. Track mixer volume is a separate MIDI CC7 musical-balance
    override. Keeping this wording centralized prevents UI/status text from
    drifting and keeps the volume model testable.
    """
    percent = normalize_volume_percent(master_volume_percent)
    gain = audio_gain_for_volume_percent(percent)
    overrides = mixer_volume_overrides or {}
    count = len([value for value in overrides.values() if value is not None and int(value) != 100])
    neutral = "neutral" if percent == 100 else ("muted" if percent == 0 else ("reduced" if percent < 100 else "boosted"))
    mix = "no track-volume overrides" if count == 0 else f"{count} track-volume override(s)"
    return f"Master {percent}% ({neutral}) → live/export gain {gain:.3f}; mixer: {mix}."
