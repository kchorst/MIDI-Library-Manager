from __future__ import annotations

"""Defensive resource limits for untrusted or pathological MIDI files.

MIDI files are user-provided input.  A small file can still contain a very large
number of events or extreme tick values, so callers validate both the file and
its decoded structure before performing expensive musical analysis.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Iterable


class MidiResourceLimitError(RuntimeError):
    """Raised when a MIDI file exceeds a documented processing budget."""

    def __init__(self, code: str, detail: str, *, actual: int | float | None = None, limit: int | float | None = None) -> None:
        self.code = code
        self.actual = actual
        self.limit = limit
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class MidiResourceLimits:
    """Conservative limits that accept normal song MIDI while bounding abuse.

    Environment overrides are intentionally available for unusually large,
    trusted collections without requiring source edits.  Values are read once
    when :func:`from_environment` is called.
    """

    max_file_bytes: int = 64 * 1024 * 1024
    max_tracks: int = 256
    max_events: int = 1_500_000
    max_note_events: int = 900_000
    max_total_ticks: int = 2_147_483_647
    max_bars: int = 8192
    max_duration_seconds: float = 6 * 60 * 60
    quick_processing_seconds: float = 25.0
    deep_processing_seconds: float = 120.0

    @classmethod
    def from_environment(cls) -> "MidiResourceLimits":
        base = cls()

        def integer(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if not raw:
                return default
            try:
                return max(1, int(raw))
            except ValueError:
                return default

        def number(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if not raw:
                return default
            try:
                return max(0.25, float(raw))
            except ValueError:
                return default

        return cls(
            max_file_bytes=integer("MIDI_REMIXER_MAX_FILE_BYTES", base.max_file_bytes),
            max_tracks=integer("MIDI_REMIXER_MAX_TRACKS", base.max_tracks),
            max_events=integer("MIDI_REMIXER_MAX_EVENTS", base.max_events),
            max_note_events=integer("MIDI_REMIXER_MAX_NOTE_EVENTS", base.max_note_events),
            max_total_ticks=integer("MIDI_REMIXER_MAX_TOTAL_TICKS", base.max_total_ticks),
            max_bars=integer("MIDI_REMIXER_MAX_BARS", base.max_bars),
            max_duration_seconds=number("MIDI_REMIXER_MAX_DURATION_SECONDS", base.max_duration_seconds),
            quick_processing_seconds=number("MIDI_REMIXER_QUICK_TIMEOUT_SECONDS", base.quick_processing_seconds),
            deep_processing_seconds=number("MIDI_REMIXER_DEEP_TIMEOUT_SECONDS", base.deep_processing_seconds),
        )

    def describe(self) -> str:
        return (
            f"file ≤ {self.max_file_bytes // (1024 * 1024)} MiB; tracks ≤ {self.max_tracks}; "
            f"events ≤ {self.max_events:,}; note events ≤ {self.max_note_events:,}; bars ≤ {self.max_bars:,}; "
            f"duration ≤ {self.max_duration_seconds / 3600:g} h"
        )


DEFAULT_MIDI_LIMITS = MidiResourceLimits.from_environment()


def validate_file(path: str | Path, limits: MidiResourceLimits = DEFAULT_MIDI_LIMITS) -> int:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise MidiResourceLimitError("file-unavailable", f"Cannot read MIDI file metadata: {exc}") from exc
    if size > limits.max_file_bytes:
        raise MidiResourceLimitError(
            "file-too-large",
            f"MIDI file is {size / (1024 * 1024):.1f} MiB; the safety limit is {limits.max_file_bytes / (1024 * 1024):.1f} MiB.",
            actual=size,
            limit=limits.max_file_bytes,
        )
    return size


def validate_decoded_midi(midi, limits: MidiResourceLimits = DEFAULT_MIDI_LIMITS) -> int:
    track_count = len(getattr(midi, "tracks", ()) or ())
    if track_count > limits.max_tracks:
        raise MidiResourceLimitError(
            "too-many-tracks",
            f"MIDI contains {track_count:,} tracks; the safety limit is {limits.max_tracks:,}.",
            actual=track_count,
            limit=limits.max_tracks,
        )
    event_count = sum(len(track) for track in midi.tracks)
    if event_count > limits.max_events:
        raise MidiResourceLimitError(
            "too-many-events",
            f"MIDI contains {event_count:,} events; the safety limit is {limits.max_events:,}.",
            actual=event_count,
            limit=limits.max_events,
        )
    return event_count


class ProcessingBudget:
    """Cheap cooperative time/event/tick checks for analysis loops."""

    __slots__ = ("limits", "stage", "started", "events", "note_events")

    def __init__(self, stage: str, limits: MidiResourceLimits = DEFAULT_MIDI_LIMITS) -> None:
        if stage not in {"quick", "deep"}:
            raise ValueError(f"Unknown MIDI processing stage: {stage}")
        self.limits = limits
        self.stage = stage
        self.started = time.monotonic()
        self.events = 0
        self.note_events = 0

    @property
    def time_limit(self) -> float:
        return self.limits.quick_processing_seconds if self.stage == "quick" else self.limits.deep_processing_seconds

    def add_event(self, *, note: bool = False, total_ticks: int | None = None, force_check: bool = False) -> None:
        self.events += 1
        if note:
            self.note_events += 1
            if self.note_events > self.limits.max_note_events:
                raise MidiResourceLimitError(
                    "too-many-note-events",
                    f"MIDI contains more than {self.limits.max_note_events:,} note events; analysis was stopped safely.",
                    actual=self.note_events,
                    limit=self.limits.max_note_events,
                )
        if self.events > self.limits.max_events:
            raise MidiResourceLimitError(
                "too-many-events",
                f"MIDI contains more than {self.limits.max_events:,} events; analysis was stopped safely.",
                actual=self.events,
                limit=self.limits.max_events,
            )
        if total_ticks is not None and total_ticks > self.limits.max_total_ticks:
            raise MidiResourceLimitError(
                "tick-range-too-large",
                f"MIDI tick range exceeds {self.limits.max_total_ticks:,}; analysis was stopped safely.",
                actual=total_ticks,
                limit=self.limits.max_total_ticks,
            )
        if force_check or self.events % 2048 == 0:
            self._validate_time()

    def _validate_time(self) -> None:
        elapsed = time.monotonic() - self.started
        if elapsed > self.time_limit:
            raise MidiResourceLimitError(
                "processing-time-exceeded",
                f"{self.stage.title()} MIDI analysis exceeded {self.time_limit:g} seconds and was stopped safely.",
                actual=elapsed,
                limit=self.time_limit,
            )

    def validate_ticks(self, total_ticks: int) -> None:
        if total_ticks > self.limits.max_total_ticks:
            raise MidiResourceLimitError(
                "tick-range-too-large",
                f"MIDI tick range exceeds {self.limits.max_total_ticks:,}; analysis was stopped safely.",
                actual=total_ticks,
                limit=self.limits.max_total_ticks,
            )
        self._validate_time()

    def validate_duration(self, duration_seconds: float) -> None:
        if duration_seconds > self.limits.max_duration_seconds:
            raise MidiResourceLimitError(
                "duration-too-long",
                f"MIDI duration is {duration_seconds / 3600:.1f} hours; the safety limit is {self.limits.max_duration_seconds / 3600:g} hours.",
                actual=duration_seconds,
                limit=self.limits.max_duration_seconds,
            )
        self._validate_time()

    def validate_bars(self, bars: float) -> None:
        if bars > self.limits.max_bars:
            raise MidiResourceLimitError(
                "too-many-bars",
                f"MIDI spans approximately {bars:,.0f} bars; the safety limit is {self.limits.max_bars:,} bars.",
                actual=bars,
                limit=self.limits.max_bars,
            )
        self._validate_time()


def bounded_count(items: Iterable[object], limit: int, label: str) -> int:
    """Count an iterable while enforcing a hard maximum."""
    count = 0
    for _item in items:
        count += 1
        if count > limit:
            raise MidiResourceLimitError(
                f"too-many-{label}",
                f"MIDI contains more than {limit:,} {label}; analysis was stopped safely.",
                actual=count,
                limit=limit,
            )
    return count
