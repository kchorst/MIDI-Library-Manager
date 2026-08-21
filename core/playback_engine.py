from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import bisect
import os
import threading
import time
from typing import Callable, Optional

import mido

from core.fluidsynth_player import FluidSynthError
from core.synth_backend_factory import create_synth_backend
from core.resource_safety import DEFAULT_MIDI_LIMITS, ProcessingBudget, validate_decoded_midi, validate_file


class PlaybackStopError(RuntimeError):
    """Raised when the live scheduler cannot be proven stopped."""


class PlaybackStartCancelled(RuntimeError):
    """Raised when a pending playback start is superseded before it becomes audible."""


class PlaybackState(str, Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass(frozen=True)
class PlaybackSnapshot:
    state: PlaybackState
    label: str | None
    version_key: str | None
    percent: int
    finished: bool = False
    tick: int = 0
    seconds: float = 0.0
    duration_seconds: float = 0.0
    error: str | None = None
    source_path: str | None = None
    generation: int = 0


@dataclass(frozen=True)
class ScheduledEvent:
    tick: int
    order: int
    track_index: int
    msg: mido.Message | mido.MetaMessage


@dataclass
class Timeline:
    source_path: str
    ticks_per_beat: int
    total_ticks: int
    duration_seconds_100: float
    first_note_tick: int
    first_audible_tick: int
    events: list[ScheduledEvent]
    tempo_changes: list[tuple[int, int]]  # absolute tick, microseconds per beat

    def tick_to_seconds(self, tick: int, tempo_percent: int = 100) -> float:
        tick = max(0, min(int(tick), self.total_ticks))
        if self.total_ticks <= 0:
            return 0.0
        tempo_percent = max(25, min(250, int(tempo_percent or 100)))
        seconds = 0.0
        last_tick = 0
        last_tempo = 500000
        for tempo_tick, tempo in self.tempo_changes:
            if tempo_tick > tick:
                break
            if tempo_tick > last_tick:
                seconds += mido.tick2second(tempo_tick - last_tick, self.ticks_per_beat, last_tempo)
            last_tick = tempo_tick
            last_tempo = tempo
        if tick > last_tick:
            seconds += mido.tick2second(tick - last_tick, self.ticks_per_beat, last_tempo)
        return seconds / (tempo_percent / 100.0)

    def seconds_to_tick(self, seconds: float, tempo_percent: int = 100) -> int:
        if self.total_ticks <= 0:
            return 0
        tempo_percent = max(25, min(250, int(tempo_percent or 100)))
        target_100 = max(0.0, float(seconds)) * (tempo_percent / 100.0)
        lo, hi = 0, self.total_ticks
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.tick_to_seconds(mid, 100) <= target_100:
                lo = mid
            else:
                hi = mid - 1
        return max(0, min(self.total_ticks, lo))

    def percent_to_tick(self, percent: int) -> int:
        pct = max(0, min(99, int(percent or 0)))
        return int(round(self.total_ticks * pct / 100.0)) if self.total_ticks > 0 else 0

    def tick_to_percent(self, tick: int) -> int:
        if self.total_ticks <= 0:
            return 0
        return max(0, min(99, int(round((max(0, min(tick, self.total_ticks)) / self.total_ticks) * 100.0))))


PrepareMidiCallback = Callable[[str, str, str], str]
DurationCallback = Callable[[str], float]


class PlaybackEngine:
    """Persistent live MIDI playback engine.

    The engine uses a MIDI event scheduler rather than one-shot file playback.
    It prefers a low-latency in-process synth backend
    with the shell backend retained only as a compatibility fallback. The UI now asks this
    engine to play a MIDI source; this engine owns the timeline, transport state,
    pause/resume, seek/shuttle, tempo scaling, and current-position reporting.

    Export/render still uses the existing command-line renderer. Playback no longer
    trims MIDI files to fake shuttle behavior and no longer bakes tempo changes into
    temporary playback files.
    """

    def __init__(
        self,
        player,  # kept for compatibility with existing MainWindow construction; not used for live playback
        prepare_midi: PrepareMidiCallback,
        duration_seconds: DurationCallback | None = None,
        *,
        restart_audio_on_play: bool = True,
    ) -> None:
        app_root = getattr(player, "app_root", None)
        self.backend = create_synth_backend(app_root, getattr(player, "executable_path", None))
        self.prepare_midi = prepare_midi
        self.duration_seconds = duration_seconds
        # Remixer playback keeps the conservative fresh-route behavior, while
        # Library audition can safely reuse one verified synth process across
        # rapid song changes. Avoiding process teardown/relaunch between every
        # Browser selection removes a Windows race observed after Stop -> Play.
        self.restart_audio_on_play = bool(restart_audio_on_play)

        self.state = PlaybackState.STOPPED
        self.source_midi: str | None = None
        self.prepared_midi: str | None = None
        self.label: str | None = None
        self.version_key: str | None = None
        self.soundfont_path: str | None = None
        self.volume_gain: float = 0.5
        self.tempo_percent: int = 100
        self.current_tick: int = 0
        self.start_tick: int = 0
        self.timeline: Timeline | None = None
        # Must exist before any settings action can call stop()/snapshot.
        self.last_error: str | None = None

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_request = threading.Event()
        self._finished = False
        self._finished_label: str | None = None
        self._finished_key: str | None = None
        self._finished_source: str | None = None
        self._finished_generation: int = 0
        self._finished_error: str | None = None
        # Every play/stop transition advances this token. A scheduler from an
        # earlier Take may finish late on Windows, but it can no longer dispatch
        # into or overwrite the state of the newly selected Guided branch.
        self._playback_generation = 0
        self._play_started_monotonic: float | None = None
        self._play_started_tick: int = 0
        self._last_event_index: int = 0
        self.live_track_programs: dict[int, int] = {}
        self.live_track_volumes: dict[int, int] = {}
        self.live_track_pans: dict[int, int] = {}
        self.live_track_solos: dict[int, bool] = {}
        self.live_track_mutes: dict[int, bool] = {}
        self._timeline_cache_key: tuple[str, int, int] | None = None
        self._timeline_cache: Timeline | None = None
        self._audio_ready_key: tuple[str, str, int] | None = None
        self._audio_ready_at: float = 0.0
        self._scheduler_quarantined = False

    def set_fluidsynth_path(self, executable_path: str | Path | None) -> None:
        """Apply a user-selected FluidSynth executable without moving the installation."""
        self.stop()
        finder = getattr(self.backend, "finder", None)
        if finder is not None and hasattr(finder, "set_executable_path"):
            finder.set_executable_path(executable_path)

        self._timeline_cache: Timeline | None = None
        self._audio_ready_key: tuple[str, str, int] | None = None
        self._audio_ready_at: float = 0.0
        self._scheduler_quarantined = False
        self.last_error: str | None = None


    def _backend_ready_delay_seconds(self) -> float:
        getter = getattr(self.backend, "audio_ready_delay_seconds", None)
        if callable(getter):
            try:
                return max(0.0, float(getter()))
            except (TypeError, ValueError, OSError, RuntimeError):
                return 0.0
        return 0.0

    def _wait_for_backend_audio_ready(self) -> None:
        """Wait during load/preload until the audio device is genuinely awake.

        This deliberately moves Windows synth/device wake-up latency out of the
        user's first Play press. It is a readiness gate, not a musical delay.
        """
        delay = self._backend_ready_delay_seconds()
        if delay <= 0:
            return
        # Allow developers/QA to disable this for automated latency experiments.
        if os.environ.get("MIDI_PERFORMER_SKIP_AUDIO_READY_WAIT", "").strip() in {"1", "true", "yes"}:
            return
        end = time.monotonic() + delay
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))

    def _start_audio_keepalive(self) -> None:
        keepalive = getattr(self.backend, "start_keepalive", None)
        if callable(keepalive):
            try:
                keepalive()
            except (AttributeError, OSError, RuntimeError, FluidSynthError) as exc:
                self.last_error = f"Audio keepalive start warning: {type(exc).__name__}: {exc}"

    def _stop_audio_keepalive(self) -> None:
        keepalive = getattr(self.backend, "stop_keepalive", None)
        if callable(keepalive):
            try:
                keepalive()
            except (AttributeError, OSError, RuntimeError, FluidSynthError) as exc:
                self.last_error = f"Audio keepalive stop warning: {type(exc).__name__}: {exc}"

    def _mark_audio_ready(self, prepared: str, soundfont_path: str | Path, tempo_percent: int) -> None:
        self._audio_ready_key = (str(prepared), str(soundfont_path), max(25, min(250, int(tempo_percent or 100))))
        self._audio_ready_at = time.monotonic()

    def _audio_ready_matches(self, prepared: str, soundfont_path: str | Path, tempo_percent: int) -> bool:
        key = (str(prepared), str(soundfont_path), max(25, min(250, int(tempo_percent or 100))))
        return self._audio_ready_key == key

    def scheduler_alive(self) -> bool:
        """Return whether the owned playback scheduler thread is still running.

        This is the single public lifecycle probe used by Runtime Diagnostics and
        application shutdown. It deliberately reads the thread reference under
        the engine lock so diagnostics can never race a scheduler teardown.
        """

        with self._lock:
            thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def is_playing(self) -> bool:
        with self._lock:
            return self.state == PlaybackState.PLAYING and self._thread is not None and self._thread.is_alive()

    def is_paused(self) -> bool:
        with self._lock:
            return self.state == PlaybackState.PAUSED

    def current_percent(self) -> int:
        with self._lock:
            if self.timeline:
                return self.timeline.tick_to_percent(self.current_tick)
            return 0

    def _load_timeline(self, midi_path: str | Path, cancel_check: Callable[[], bool] | None = None) -> Timeline:
        source = str(midi_path)
        path_obj = Path(source)
        try:
            stat = path_obj.stat()
            cache_key = (str(path_obj.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
        except (OSError, RuntimeError, ValueError):
            cache_key = (source, 0, 0)
        if self._timeline_cache_key == cache_key and self._timeline_cache is not None:
            return self._timeline_cache

        validate_file(source, DEFAULT_MIDI_LIMITS)
        if cancel_check and cancel_check():
            raise PlaybackStartCancelled("Playback start was cancelled before MIDI decoding.")
        midi = mido.MidiFile(source)
        validate_decoded_midi(midi, DEFAULT_MIDI_LIMITS)
        if cancel_check and cancel_check():
            raise PlaybackStartCancelled("Playback start was cancelled after MIDI decoding.")
        budget = ProcessingBudget("quick", DEFAULT_MIDI_LIMITS)
        events: list[ScheduledEvent] = []
        tempo_changes: list[tuple[int, int]] = [(0, 500000)]
        total_ticks = 0
        first_note_tick: int | None = None
        active_notes: dict[tuple[int, int, int], tuple[int, int]] = {}
        order = 0
        for track_index, track in enumerate(midi.tracks):
            abs_tick = 0
            for msg in track:
                abs_tick += int(msg.time)
                total_ticks = max(total_ticks, abs_tick)
                events.append(ScheduledEvent(abs_tick, order, track_index, msg.copy(time=0)))
                if msg.type == "note_on":
                    channel = int(getattr(msg, "channel", 0))
                    note = int(getattr(msg, "note", 0))
                    velocity = int(getattr(msg, "velocity", 0) or 0)
                    key = (track_index, channel, note)
                    if velocity > 0:
                        active_notes[key] = (abs_tick, velocity)
                    else:
                        start = active_notes.pop(key, None)
                        if start is not None:
                            start_tick, start_velocity = start
                            if abs_tick > start_tick and start_velocity >= 4:
                                first_note_tick = start_tick if first_note_tick is None else min(first_note_tick, start_tick)
                elif msg.type == "note_off":
                    key = (track_index, int(getattr(msg, "channel", 0)), int(getattr(msg, "note", 0)))
                    start = active_notes.pop(key, None)
                    if start is not None:
                        start_tick, start_velocity = start
                        if abs_tick > start_tick and start_velocity >= 4:
                            first_note_tick = start_tick if first_note_tick is None else min(first_note_tick, start_tick)
                if msg.type == "set_tempo":
                    tempo_changes.append((abs_tick, int(msg.tempo)))
                budget.add_event(
                    note=msg.type in {"note_on", "note_off"},
                    total_ticks=abs_tick,
                    force_check=(order % 1024 == 0),
                )
                if cancel_check and order % 512 == 0 and cancel_check():
                    raise PlaybackStartCancelled("Playback start was cancelled while building the MIDI timeline.")
                order += 1
        for start_tick, start_velocity in active_notes.values():
            if start_velocity >= 4:
                first_note_tick = start_tick if first_note_tick is None else min(first_note_tick, start_tick)
        events.sort(key=lambda item: (item.tick, item.order))
        first_audible_tick = self._find_first_audible_tick(events, midi.ticks_per_beat)
        tempo_changes.sort(key=lambda item: item[0])
        # Collapse same-tick tempo changes so the last at a tick wins for future time conversion.
        collapsed: list[tuple[int, int]] = []
        for tick, tempo in tempo_changes:
            if collapsed and collapsed[-1][0] == tick:
                collapsed[-1] = (tick, tempo)
            else:
                collapsed.append((tick, tempo))
        timeline = Timeline(
            source_path=source,
            ticks_per_beat=midi.ticks_per_beat,
            total_ticks=max(1, total_ticks),
            duration_seconds_100=0.0,
            first_note_tick=int(first_note_tick or 0),
            first_audible_tick=int(first_audible_tick or first_note_tick or 0),
            events=events,
            tempo_changes=collapsed or [(0, 500000)],
        )
        timeline.duration_seconds_100 = timeline.tick_to_seconds(timeline.total_ticks, 100)
        budget.validate_ticks(timeline.total_ticks)
        budget.validate_duration(timeline.duration_seconds_100)
        if cancel_check and cancel_check():
            raise PlaybackStartCancelled("Playback start was cancelled after timeline preparation.")
        self._timeline_cache_key = cache_key
        self._timeline_cache = timeline
        return timeline


    def _find_first_audible_tick(self, events: list[ScheduledEvent], ticks_per_beat: int) -> int:
        """Return the first tick likely to produce real audible music.

        Some commercial/archival MIDI files contain setup notes, silent notes,
        channel-volume-zero passages, or controller-only lead-ins. Counting the
        first raw note-on as the musical start can leave users hearing silence
        until a loud entrance several seconds later. This pass follows channel
        volume/expression and ignores zero-duration or effectively silent notes.
        """
        channel_volume: dict[int, int] = {ch: 100 for ch in range(16)}
        channel_expression: dict[int, int] = {ch: 127 for ch in range(16)}
        active: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        min_effective_velocity = 10.0
        min_duration_ticks = max(1, int(ticks_per_beat) // 96)

        for event in events:
            msg = event.msg
            if msg.is_meta or not hasattr(msg, "channel"):
                continue
            channel = int(getattr(msg, "channel", 0))
            if msg.type == "control_change":
                control = int(getattr(msg, "control", -1))
                value = max(0, min(127, int(getattr(msg, "value", 0))))
                if control == 7:
                    channel_volume[channel] = value
                elif control == 11:
                    channel_expression[channel] = value
                continue
            if msg.type == "note_on":
                note = int(getattr(msg, "note", 0))
                velocity = int(getattr(msg, "velocity", 0) or 0)
                key = (channel, note)
                if velocity > 0:
                    active[key] = (int(event.tick), velocity, channel_volume.get(channel, 100), channel_expression.get(channel, 127))
                else:
                    start = active.pop(key, None)
                    if start is None:
                        continue
                    start_tick, start_velocity, volume, expression = start
                    effective = start_velocity * (volume / 127.0) * (expression / 127.0)
                    if int(event.tick) - start_tick >= min_duration_ticks and effective >= min_effective_velocity:
                        return int(start_tick)
            elif msg.type == "note_off":
                key = (channel, int(getattr(msg, "note", 0)))
                start = active.pop(key, None)
                if start is None:
                    continue
                start_tick, start_velocity, volume, expression = start
                effective = start_velocity * (volume / 127.0) * (expression / 127.0)
                if int(event.tick) - start_tick >= min_duration_ticks and effective >= min_effective_velocity:
                    return int(start_tick)
        # If notes are still active at EOF, use the earliest one that was not effectively silent.
        audible_starts = []
        for start_tick, start_velocity, volume, expression in active.values():
            effective = start_velocity * (volume / 127.0) * (expression / 127.0)
            if effective >= min_effective_velocity:
                audible_starts.append(int(start_tick))
        return min(audible_starts) if audible_starts else 0

    def _start_tick_for_percent(self, timeline: Timeline, requested_percent: int, tempo_percent: int) -> int:
        start_tick = timeline.percent_to_tick(requested_percent)
        if requested_percent != 0:
            return start_tick
        # Prefer the first audible musical event, not merely the first raw note-on.
        # This handles MIDI files with silent setup/control lead-ins.
        candidate_tick = int(timeline.first_audible_tick or timeline.first_note_tick or 0)
        if candidate_tick > 0:
            leading_seconds = timeline.tick_to_seconds(candidate_tick, max(25, min(250, int(tempo_percent or 100))))
            if leading_seconds > 0.15:
                return candidate_tick
        return start_tick

    def _event_index_at_tick(self, tick: int) -> int:
        if not self.timeline:
            return 0
        ticks = [event.tick for event in self.timeline.events]
        return bisect.bisect_left(ticks, tick)

    def _snapshot_locked(self, finished: bool = False) -> PlaybackSnapshot:
        duration = self.timeline.tick_to_seconds(self.timeline.total_ticks, self.tempo_percent) if self.timeline else 0.0
        seconds = self.timeline.tick_to_seconds(self.current_tick, self.tempo_percent) if self.timeline else 0.0
        percent = self.timeline.tick_to_percent(self.current_tick) if self.timeline else 0
        return PlaybackSnapshot(
            self.state,
            self.label,
            self.version_key,
            percent,
            finished=finished,
            tick=int(self.current_tick),
            seconds=float(seconds),
            duration_seconds=float(duration),
            error=self.last_error,
            source_path=self.source_midi,
            generation=self._playback_generation,
        )

    def snapshot(self) -> PlaybackSnapshot:
        with self._lock:
            return self._snapshot_locked(False)

    def _is_track_muted_live(self, track_index: int) -> bool:
        solos = {int(k) for k, enabled in self.live_track_solos.items() if enabled}
        muted = bool(self.live_track_mutes.get(int(track_index), False))
        if solos and int(track_index) not in solos:
            muted = True
        return muted

    def _timeline_track_channels(self) -> dict[int, set[int]]:
        channels: dict[int, set[int]] = {}
        if not self.timeline:
            return channels
        for event in self.timeline.events:
            msg = event.msg
            if msg.is_meta or not hasattr(msg, "channel"):
                continue
            channels.setdefault(int(event.track_index), set()).add(int(getattr(msg, "channel", 0)))
        return channels

    def _baseline_channel_programs(self, tick: int) -> dict[int, int]:
        """Return source-MIDI program state that should be active at ``tick``.

        FluidSynth keeps channel programs alive between plays. If a take changed
        Track 1 to Harpsichord, playing Original next must actively restore the
        source MIDI voice. Some MIDI files rely on the General MIDI default
        piano and do not contain explicit program-change messages, so a simple
        controller reset is not enough. This method seeds every used melodic
        channel with GM program 0, then overlays any real source program changes
        that occur before the requested start tick.
        """
        programs: dict[int, int] = {}
        if not self.timeline:
            return programs
        for event in self.timeline.events:
            msg = event.msg
            if msg.is_meta or not hasattr(msg, "channel"):
                continue
            channel = int(getattr(msg, "channel", 0))
            if channel == 9:
                continue
            if msg.type in {"note_on", "note_off", "program_change", "control_change", "pitchwheel"}:
                programs.setdefault(channel, 0)
            if event.tick > tick:
                continue
            if msg.type == "program_change" and int(event.track_index) not in self.live_track_programs:
                programs[channel] = int(msg.program)
        return programs

    def _send_live_channel_overrides(self) -> None:
        """Send the current dynamic mixer state into FluidSynth without restarting playback."""
        if not self.timeline:
            return
        channel_map = self._timeline_track_channels()
        for track_index, channels in sorted(channel_map.items()):
            if self._is_track_muted_live(track_index):
                continue
            program = self.live_track_programs.get(int(track_index))
            volume = self.live_track_volumes.get(int(track_index))
            pan = self.live_track_pans.get(int(track_index))
            for channel in sorted(channels):
                if program is not None and int(channel) != 9:
                    self.backend.control_change(channel, 0, 0)
                    self.backend.control_change(channel, 32, 0)
                    self.backend.program_change(channel, max(0, min(127, int(program))))
                if volume is not None:
                    self.backend.control_change(channel, 7, max(0, min(127, int(volume))))
                if pan is not None:
                    self.backend.control_change(channel, 10, max(0, min(127, int(pan))))

    def _send_context_at_tick(self, tick: int) -> None:
        """Restore source channel state at ``tick``, then apply live mixer overrides.

        Original playback must be a true baseline. After a mixer/take has changed
        a channel's program, the synth may still be holding that program even
        after all-notes-off. Therefore we explicitly restore source-MIDI programs
        for every used melodic channel, including GM default Piano when the MIDI
        omitted program-change messages. Bank select controllers are sent before
        program changes so non-default source instruments are also restored.
        """
        if not self.timeline:
            return
        programs: dict[int, int] = self._baseline_channel_programs(tick)
        controllers: dict[tuple[int, int], int] = {}
        pitch: dict[int, int] = {}
        for event in self.timeline.events:
            if event.tick > tick:
                break
            if self._is_track_muted_live(event.track_index):
                continue
            msg = event.msg
            if msg.is_meta or not hasattr(msg, "channel"):
                continue
            channel = int(getattr(msg, "channel", 0))
            if msg.type == "program_change":
                if int(event.track_index) in self.live_track_programs and channel != 9:
                    continue
                programs[channel] = int(msg.program)
            elif msg.type == "control_change":
                control = int(msg.control)
                if control == 7 and int(event.track_index) in self.live_track_volumes:
                    continue
                if control == 10 and int(event.track_index) in self.live_track_pans:
                    continue
                controllers[(channel, control)] = int(msg.value)
            elif msg.type == "pitchwheel":
                pitch[channel] = int(msg.pitch)
        self.backend.reset_controllers()
        # Bank selects must precede program changes. If the MIDI did not specify
        # a bank for a melodic channel, reset to GM bank 0 so a prior take's bank
        # cannot bleed into Original.
        melodic_channels = sorted(ch for ch in programs.keys() if int(ch) != 9)
        for channel in melodic_channels:
            self.backend.control_change(channel, 0, int(controllers.get((channel, 0), 0)))
            self.backend.control_change(channel, 32, int(controllers.get((channel, 32), 0)))
        for channel, program in sorted(programs.items()):
            if int(channel) != 9:
                self.backend.program_change(channel, program)
        for (channel, control), value in sorted(controllers.items()):
            if control in {0, 32}:
                continue
            self.backend.control_change(channel, control, value)
        for channel, value in sorted(pitch.items()):
            try:
                self.backend.pitch_bend(channel, value)
            except FluidSynthError:
                raise
            except (AttributeError, OSError, TypeError, ValueError) as exc:
                self.last_error = f"Pitch-bend restore warning: {type(exc).__name__}: {exc}"
        self._send_live_channel_overrides()

    def _dispatch_midi_message(self, event: ScheduledEvent) -> None:
        msg = event.msg
        if msg.is_meta:
            return
        track_index = int(event.track_index)
        channel = int(getattr(msg, "channel", 0))
        muted = self._is_track_muted_live(track_index)
        if muted and msg.type in {"note_on", "note_off", "program_change", "control_change", "pitchwheel"}:
            # Notes are actively silenced when mute/solo changes; suppress future events for muted tracks.
            return
        if msg.type == "note_on":
            velocity = int(msg.velocity)
            if velocity <= 0:
                self.backend.note_off(channel, int(msg.note))
            else:
                self.backend.note_on(channel, int(msg.note), velocity)
        elif msg.type == "note_off":
            self.backend.note_off(channel, int(msg.note))
        elif msg.type == "program_change":
            if track_index in self.live_track_programs and channel != 9:
                return
            self.backend.program_change(channel, int(msg.program))
        elif msg.type == "control_change":
            control = int(msg.control)
            if control == 7 and track_index in self.live_track_volumes:
                return
            if control == 10 and track_index in self.live_track_pans:
                return
            self.backend.control_change(channel, control, int(msg.value))
        elif msg.type == "pitchwheel":
            self.backend.pitch_bend(channel, int(msg.pitch))

    def _scheduler_loop(
        self,
        start_index: int,
        generation: int,
        timeline: Timeline,
        start_tick: int,
        tempo_percent: int,
        started_monotonic: float,
    ) -> None:
        events = timeline.events
        index = start_index
        try:
            while index < len(events):
                with self._lock:
                    if generation != self._playback_generation:
                        return
                    stop_requested = self._stop_event.is_set()
                    pause_requested = self._pause_request.is_set()
                if stop_requested or pause_requested:
                    break

                event = events[index]
                if event.tick < start_tick:
                    index += 1
                    continue
                target_seconds = (
                    timeline.tick_to_seconds(event.tick, tempo_percent)
                    - timeline.tick_to_seconds(start_tick, tempo_percent)
                )
                while True:
                    with self._lock:
                        if generation != self._playback_generation:
                            return
                        stop_requested = self._stop_event.is_set()
                        pause_requested = self._pause_request.is_set()
                    if stop_requested or pause_requested:
                        break
                    elapsed = time.monotonic() - started_monotonic
                    remaining = target_seconds - elapsed
                    if remaining <= 0:
                        break
                    time.sleep(min(0.01, remaining))
                    with self._lock:
                        if generation != self._playback_generation:
                            return
                        absolute_elapsed = timeline.tick_to_seconds(start_tick, tempo_percent) + (
                            time.monotonic() - started_monotonic
                        )
                        self.current_tick = timeline.seconds_to_tick(absolute_elapsed, tempo_percent)
                if stop_requested or pause_requested:
                    break
                with self._lock:
                    if generation != self._playback_generation:
                        return
                self._dispatch_midi_message(event)
                with self._lock:
                    if generation != self._playback_generation:
                        return
                    self.current_tick = event.tick
                    self._last_event_index = index
                index += 1

            with self._lock:
                if generation != self._playback_generation:
                    return
                if self._pause_request.is_set():
                    self.backend.all_notes_off()
                    self.state = PlaybackState.PAUSED
                elif self._stop_event.is_set():
                    self.backend.all_notes_off()
                else:
                    self.backend.all_notes_off()
                    self._finished_label = self.label
                    self._finished_key = self.version_key
                    self._finished_source = self.source_midi
                    self._finished_generation = generation
                    self._finished_error = None
                    self.current_tick = 0
                    self.state = PlaybackState.STOPPED
                    self._finished = True
                    self.source_midi = None
                    self.prepared_midi = None
                    self.label = None
                    self.version_key = None
        except Exception as exc:
            with self._lock:
                if generation != self._playback_generation:
                    return
                error = f"{type(exc).__name__}: {exc}"
                try:
                    self.backend.all_notes_off()
                except Exception as cleanup_exc:
                    error += f"; all-notes-off cleanup: {type(cleanup_exc).__name__}: {cleanup_exc}"
                self.last_error = error
                self._finished_error = error
                self._finished_label = self.label
                self._finished_key = self.version_key
                self._finished_source = self.source_midi
                self._finished_generation = generation
                self.state = PlaybackState.STOPPED
                self.current_tick = 0
                self._finished = True
                self.source_midi = None
                self.prepared_midi = None
                self.label = None
                self.version_key = None
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def play(
        self,
        source_midi: str | Path,
        label: str,
        version_key: str,
        soundfont_path: str | Path,
        volume_gain: float,
        tempo_percent: int,
        start_percent: int | None = None,
        start_tick: int | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> PlaybackSnapshot:
        def cancelled() -> bool:
            try:
                return bool(cancel_check and cancel_check())
            except Exception:
                return True

        if cancelled():
            raise PlaybackStartCancelled("Playback start was cancelled before preparation.")
        self.stop(reset_to_start=False)
        with self._lock:
            if self._scheduler_quarantined:
                raise PlaybackStopError(
                    "Playback is quarantined because an earlier scheduler did not terminate. Restart MIDI Remixer."
                )
        self.last_error = None
        self._finished_error = None
        source = str(Path(source_midi))
        if not Path(source).is_file():
            raise FileNotFoundError(f"Playback MIDI does not exist: {source}")
        if not str(version_key or "").strip():
            raise ValueError("Playback requires a stable target identity.")
        prepared = self.prepare_midi(source, label, version_key)
        if not Path(prepared).is_file():
            raise FileNotFoundError(f"Prepared playback MIDI does not exist: {prepared}")
        if cancelled():
            raise PlaybackStartCancelled("Playback start was cancelled during source preparation.")
        timeline = self._load_timeline(prepared, cancel_check=cancel_check)
        if cancelled():
            raise PlaybackStartCancelled("Playback start was cancelled while reading the MIDI timeline.")
        requested_percent = 0 if start_percent is None else int(start_percent)
        if start_tick is None:
            resolved_start_tick = self._start_tick_for_percent(timeline, requested_percent, int(tempo_percent or 100))
        else:
            resolved_start_tick = max(0, min(int(start_tick), int(timeline.total_ticks)))

        with self._lock:
            self._playback_generation += 1
            generation = self._playback_generation
            self.source_midi = source
            self.prepared_midi = prepared
            self.label = label
            self.version_key = version_key
            self.soundfont_path = str(soundfont_path)
            self.volume_gain = float(volume_gain)
            self.tempo_percent = max(25, min(250, int(tempo_percent or 100)))
            self.timeline = timeline
            self.start_tick = resolved_start_tick
            self.current_tick = resolved_start_tick
            self._finished = False
            self._finished_label = None
            self._finished_key = None
            self._finished_source = None
            self._finished_generation = 0
            self._stop_event.clear()
            self._pause_request.clear()

        # Audio route policy is caller-owned. Remixer normally requests a fresh
        # FluidSynth route at Play time; Library Browser audition reuses one
        # verified route across rapid song changes to avoid Windows Stop/Start
        # process races. Preload remains data-only in both cases.
        try:
            if cancelled():
                raise PlaybackStartCancelled("Playback start was cancelled before audio startup.")
            try:
                self.backend.start(
                    soundfont_path, gain=volume_gain, force_restart=self.restart_audio_on_play
                )
            except TypeError:
                # Compatibility with simple/fake backends used by tests and external
                # integrations that implement the older start(soundfont, gain) API.
                self.backend.start(soundfont_path, gain=volume_gain)
            if cancelled():
                raise PlaybackStartCancelled("Playback start was cancelled during audio startup.")
            self._stop_audio_keepalive()
            self.backend.all_notes_off()
            self._send_context_at_tick(resolved_start_tick)
            self._mark_audio_ready(prepared, soundfont_path, int(tempo_percent or 100))
            start_index = self._event_index_at_tick(resolved_start_tick)

            with self._lock:
                if generation != self._playback_generation:
                    raise RuntimeError("Playback start was superseded by another transport action.")
                self.state = PlaybackState.PLAYING
                started_monotonic = time.monotonic()
                self._play_started_monotonic = started_monotonic
                self._play_started_tick = resolved_start_tick
                self._thread = threading.Thread(
                    target=self._scheduler_loop,
                    args=(
                        start_index,
                        generation,
                        timeline,
                        resolved_start_tick,
                        self.tempo_percent,
                        started_monotonic,
                    ),
                    name="MidiPlaybackScheduler",
                    daemon=True,
                )
                self._thread.start()
                return self._snapshot_locked(False)
        except Exception:
            with self._lock:
                own_failed_start = generation == self._playback_generation
                if own_failed_start:
                    self._playback_generation += 1
                    self._stop_event.set()
            if own_failed_start:
                try:
                    self.backend.all_notes_off()
                except (FluidSynthError, OSError, RuntimeError) as cleanup_exc:
                    self.last_error = f"Playback-start cleanup warning: {type(cleanup_exc).__name__}: {cleanup_exc}"
                with self._lock:
                    self._thread = None
                    self._stop_event.clear()
                    self._pause_request.clear()
                    self._finished = False
                    self._finished_error = None
                    self.state = PlaybackState.STOPPED
                    self.current_tick = 0
                    self.start_tick = 0
                    self.source_midi = None
                    self.prepared_midi = None
                    self.label = None
                    self.version_key = None
            raise

    def _join_scheduler(self, thread: threading.Thread | None, *, timeout: float, action: str) -> None:
        if thread is None or thread is threading.current_thread() or not thread.is_alive():
            return
        thread.join(timeout=max(0.05, timeout))
        if thread.is_alive():
            with self._lock:
                self._scheduler_quarantined = True
                self.last_error = (
                    f"Playback scheduler did not terminate during {action} within {timeout:.1f} seconds."
                )
            raise PlaybackStopError(
                self.last_error + " New playback is blocked; restart MIDI Remixer before continuing."
            )

    def pause(self) -> PlaybackSnapshot:
        with self._lock:
            if self.state != PlaybackState.PLAYING:
                return self._snapshot_locked(False)
            self._pause_request.set()
            thread = self._thread
        self._join_scheduler(thread, timeout=3.0, action="pause")
        self.backend.all_notes_off()
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
            self.state = PlaybackState.PAUSED
            return self._snapshot_locked(False)

    def stop(self, reset_to_start: bool = True) -> PlaybackSnapshot:
        with self._lock:
            self._playback_generation += 1
            self._stop_event.set()
            self._pause_request.clear()
            thread = self._thread
        self._join_scheduler(thread, timeout=3.0, action="stop")
        self.backend.all_notes_off()
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
            self._stop_event.clear()
            self._pause_request.clear()
            self._scheduler_quarantined = False
            self._finished = False
            self._finished_error = None
            if reset_to_start:
                self.state = PlaybackState.STOPPED
                self._play_started_monotonic = None
                self.current_tick = 0
                self.start_tick = 0
                self.source_midi = None
                self.prepared_midi = None
                self.label = None
                self.version_key = None
            elif self.state == PlaybackState.PLAYING:
                self.state = PlaybackState.PAUSED
            return self._snapshot_locked(False)

    def seek(self, percent: int) -> PlaybackSnapshot:
        percent = max(0, min(99, int(percent)))
        with self._lock:
            was_playing = self.state == PlaybackState.PLAYING
            source = self.source_midi
            label = self.label
            key = self.version_key
            sf = self.soundfont_path
            gain = self.volume_gain
            tempo = self.tempo_percent
            if self.timeline:
                self.current_tick = self.timeline.percent_to_tick(percent)
                self.start_tick = self.current_tick
        if was_playing and source and label and key and sf:
            return self.play(source, label, key, sf, gain, tempo, start_percent=percent)
        with self._lock:
            self.state = PlaybackState.PAUSED if source and label and key and sf else PlaybackState.STOPPED
            return self._snapshot_locked(False)

    def change_tempo(self, tempo_percent: int) -> PlaybackSnapshot:
        tempo_percent = max(25, min(250, int(tempo_percent or 100)))
        with self._lock:
            percent = self.timeline.tick_to_percent(self.current_tick) if self.timeline else 0
            was_playing = self.state == PlaybackState.PLAYING
            source = self.source_midi
            label = self.label
            key = self.version_key
            sf = self.soundfont_path
            gain = self.volume_gain
            self.tempo_percent = tempo_percent
        if was_playing and source and label and key and sf:
            return self.play(source, label, key, sf, gain, tempo_percent, start_percent=percent)
        with self._lock:
            return self._snapshot_locked(False)

    def change_gain(self, volume_gain: float) -> PlaybackSnapshot:
        with self._lock:
            self.volume_gain = float(volume_gain)
        try:
            self.backend.set_gain(volume_gain)
        except FluidSynthError as exc:
            self.last_error = f"Gain update warning: {type(exc).__name__}: {exc}"
        return self.snapshot()

    def preload_midi(
        self,
        source_midi: str | Path,
        label: str,
        version_key: str,
        soundfont_path: str | Path,
        volume_gain: float,
        tempo_percent: int = 100,
    ) -> bool:
        """Prepare MIDI data without opening the Windows audio device.

        Previous builds marked playback ready after a background FluidSynth
        process merely remained alive. That did not prove audible output and could
        preserve a silent endpoint. This gate now validates paths and builds the
        exact timeline only; Play opens a fresh audio route synchronously.
        """
        self.last_error = None
        prepared = self.prepare_midi(str(source_midi), label, str(version_key))
        timeline = self._load_timeline(prepared)
        requested_percent = 0
        start_tick = self._start_tick_for_percent(timeline, requested_percent, int(tempo_percent or 100))
        with self._lock:
            self.soundfont_path = str(soundfont_path)
            self.volume_gain = float(volume_gain)
            self.tempo_percent = max(25, min(250, int(tempo_percent or 100)))
            self.timeline = timeline
            self.start_tick = start_tick
            self.current_tick = start_tick
        # Deliberately do not start FluidSynth here. A loaded timeline is data
        # ready, not proof of an audible Windows audio route.
        if not Path(soundfont_path).exists():
            raise FluidSynthError(f"SoundFont file not found: {soundfont_path}")
        with self._lock:
            self.current_tick = 0
            self.start_tick = 0
        return True

    def preload_soundfont(self, soundfont_path: str | Path, volume_gain: float = 0.5) -> bool:
        """Validate the SoundFont path without pre-opening a silent device."""
        if not Path(soundfont_path).exists():
            raise FluidSynthError(f"SoundFont file not found: {soundfont_path}")
        return True

    def set_audio_driver(self, driver: str | None) -> None:
        setter = getattr(self.backend, "set_preferred_driver", None)
        if callable(setter):
            setter(driver)

    def test_audio_tone(
        self,
        soundfont_path: str | Path,
        volume_gain: float = 0.7,
        driver: str | None = None,
    ) -> dict[str, object]:
        tester = getattr(self.backend, "test_tone", None)
        if not callable(tester):
            raise FluidSynthError("This playback backend does not provide a direct audio test.")
        self.stop(reset_to_start=True)
        return tester(soundfont_path, gain=volume_gain, driver=driver)

    def audio_diagnostics(self) -> dict[str, object]:
        getter = getattr(self.backend, "diagnostic_snapshot", None)
        return getter() if callable(getter) else {}

    def apply_live_mixer(
        self,
        track_programs: dict[str | int, int | None] | None,
        track_volumes: dict[str | int, int] | None,
        track_pans: dict[str | int, int] | None,
        track_solos: dict[str | int, bool] | None,
        track_mutes: dict[str | int, bool] | None,
    ) -> PlaybackSnapshot:
        """Apply mixer changes to the currently playing synth without rebuilding playback.

        Persisted/project mixer settings are still baked into exported MIDI. This
        live state is for immediate listening while the Track Mixer is open.
        """
        def int_map(values):
            out: dict[int, int] = {}
            for key, value in (values or {}).items():
                if value is None:
                    continue
                out[int(key)] = int(value)
            return out

        with self._lock:
            self.live_track_programs = int_map(track_programs)
            self.live_track_volumes = int_map(track_volumes)
            self.live_track_pans = int_map(track_pans)
            self.live_track_solos = {int(k): bool(v) for k, v in (track_solos or {}).items() if bool(v)}
            self.live_track_mutes = {int(k): bool(v) for k, v in (track_mutes or {}).items() if bool(v)}
            active_tick = int(self.current_tick or 0)
            active = self.state in {PlaybackState.PLAYING, PlaybackState.PAUSED}
        if active and self.timeline:
            self.backend.all_notes_off()
            self._send_context_at_tick(active_tick)
        return self.snapshot()

    def poll(self) -> PlaybackSnapshot:
        with self._lock:
            if self.state == PlaybackState.PLAYING and self.timeline and self._play_started_monotonic is not None:
                elapsed = time.monotonic() - self._play_started_monotonic
                absolute_elapsed = self.timeline.tick_to_seconds(self.start_tick, self.tempo_percent) + elapsed
                self.current_tick = self.timeline.seconds_to_tick(absolute_elapsed, self.tempo_percent)
            if self._finished:
                self._finished = False
                error = self._finished_error
                self._finished_error = None
                return PlaybackSnapshot(
                    PlaybackState.STOPPED,
                    self._finished_label,
                    self._finished_key,
                    0,
                    finished=True,
                    error=error,
                    source_path=self._finished_source,
                    generation=self._finished_generation,
                )
            return self._snapshot_locked(False)

    def close(self) -> None:
        """Stop and close only after the scheduler is proven terminal.

        A failed close remains retryable by ``RuntimeSupervisor``; ownership is
        never discarded merely because a timeout elapsed.
        """

        with self._lock:
            self._stop_event.set()
            self._pause_request.clear()
        self.stop(reset_to_start=True)
        self._stop_audio_keepalive()
        self.backend.all_notes_off()
        self.backend.close()
        with self._lock:
            self._thread = None
            self.timeline = None
            self.source_midi = None
            self.prepared_midi = None
            self.state = PlaybackState.STOPPED
            self._timeline_cache = None
            self._scheduler_quarantined = False

