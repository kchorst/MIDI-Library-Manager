from __future__ import annotations

"""Lifecycle-owned MIDI audition transport for Library Manager views.

The Library browser is intentionally a read-only catalogue view, but users need
an audible way to decide whether an unusual or failed MIDI is worth keeping.
This controller reuses the Remixer's live PlaybackEngine so Pause/Resume/Stop
have real transport semantics and every child synth/process remains owned by the
existing runtime supervisor.
"""

from pathlib import Path
import os
import threading
import time

from core.app_paths import AppPaths
from core.app_settings import AppSettings
from core.dependency_setup import find_soundfont
from core.fluidsynth_player import FluidSynthPlayer
from core.logging_config import append_bounded
from core.playback_engine import PlaybackEngine, PlaybackSnapshot, PlaybackStartCancelled, PlaybackState


class LibraryAuditionError(RuntimeError):
    pass


class LibraryAuditionCancelled(LibraryAuditionError):
    """A pending Browser play request was superseded by Stop/Close/new Play."""


class LibraryAuditionController:
    def __init__(self, app_root: str | Path) -> None:
        self.app_root = Path(app_root).resolve()
        self.settings = AppSettings(self.app_root)
        self.log_path = AppPaths.for_app(self.app_root).ensure().logs / "library_audition.log"
        executable = self.settings.get("fluidsynth_path")
        self.player = FluidSynthPlayer(self.app_root, executable_path=executable)
        # The Browser may reuse the route while playing/paused, but explicit
        # Stop is a hard lifecycle boundary: it silences immediately, closes the
        # synth process, and proves no scheduler thread survives before returning.
        self.engine = PlaybackEngine(
            self.player, self._prepare_source, restart_audio_on_play=False
        )
        driver = str(self.settings.get("audio_driver") or "default")
        if driver and driver != "default":
            self.engine.set_audio_driver(driver)
        self._path = ""
        self._label = ""
        self._paused_tick = 0
        # Never hold this lock across MIDI parsing, FluidSynth startup, or scheduler
        # joins.  Those operations can legitimately take time and Browser UI
        # polling/Stop must not queue behind them.
        self._state_lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._start_condition = threading.Condition(self._state_lock)
        self._active_starts = 0
        self._request_generation = 0
        self._closed = False

    def _log(self, action: str, detail: str = "") -> None:
        try:
            snapshot = self.engine.audio_diagnostics()
        except Exception:
            snapshot = {}
        pid = snapshot.get("pid") if isinstance(snapshot, dict) else None
        text = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pid={os.getpid()} action={action} "
            f"synth_pid={pid or '-'} path={self._path or '-'} {detail}\n"
        )
        append_bounded(self.log_path, text)

    @staticmethod
    def _prepare_source(source: str, _label: str, _version_key: str) -> str:
        # Library audition never transforms a source MIDI. The engine receives
        # the original file path and owns only playback state, not source data.
        return str(Path(source))

    def _soundfont(self) -> str:
        configured = self.settings.get("last_soundfont_path")
        configured_dir = self.settings.get("last_soundfont_directory")
        soundfont = find_soundfont(self.app_root, configured, configured_dir)
        if not soundfont:
            raise LibraryAuditionError(
                "No SoundFont is configured or discoverable. Use Audio Setup… in MIDI Library Browser to choose "
                "FluidSynth and a General MIDI .sf2/.sf3 SoundFont."
            )
        return str(soundfont)

    def _gain(self) -> float:
        try:
            return max(0.0, min(1.5, float(self.settings.get("library_audition_volume", 100) or 100) / 100.0))
        except (TypeError, ValueError, OverflowError):
            return 1.0

    def _next_generation(self) -> int:
        with self._state_lock:
            self._request_generation += 1
            return self._request_generation

    def _cancelled(self, generation: int) -> bool:
        with self._state_lock:
            return self._closed or generation != self._request_generation

    def cancel_pending_start(self) -> None:
        """Invalidate any slow Play preparation without waiting for it.

        Browser Stop calls this synchronously on the Qt thread before dispatching
        the potentially blocking scheduler join to a worker thread.
        """
        self._next_generation()

    def play(self, path: str | Path, label: str | None = None, *, start_tick: int = 0) -> PlaybackSnapshot:
        with self._state_lock:
            if self._closed:
                raise LibraryAuditionError("Library audition is already closed.")
            self._request_generation += 1
            generation = self._request_generation
        source = Path(path).resolve(strict=False)
        if not source.is_file():
            raise LibraryAuditionError(f"MIDI file not found: {source}")
        requested_label = str(label or source.name)
        self._log("play-request", f"label={requested_label} generation={generation}")
        with self._start_condition:
            self._active_starts += 1
        try:
            snapshot = self.engine.play(
                str(source),
                requested_label,
                str(source),
                self._soundfont(),
                self._gain(),
                100,
                start_tick=max(0, int(start_tick)),
                cancel_check=lambda: self._cancelled(generation),
            )
        except PlaybackStartCancelled as exc:
            self._log("play-cancelled", f"generation={generation}")
            # If cancellation happened after audio startup, guarantee the late
            # route is torn down before this worker finishes.
            self._close_audio_route_best_effort()
            raise LibraryAuditionCancelled("Playback start was cancelled.") from exc
        except Exception as exc:
            self._log("play-error", f"{type(exc).__name__}: {exc}")
            self._close_audio_route_best_effort()
            raise LibraryAuditionError(f"Could not play {source.name}: {type(exc).__name__}: {exc}") from exc
        finally:
            with self._start_condition:
                self._active_starts = max(0, self._active_starts - 1)
                self._start_condition.notify_all()

        if self._cancelled(generation):
            # A Stop/new Play/Close arrived while FluidSynth was starting.  Do
            # not let a late completion resurrect playback after the user acted.
            self._close_audio_route_best_effort()
            raise LibraryAuditionCancelled("Playback start was superseded by another transport action.")
        with self._state_lock:
            self._path = str(source)
            self._label = requested_label
            self._paused_tick = 0
        self._log("play-started", f"generation={snapshot.generation}")
        return snapshot

    def _close_audio_route_best_effort(self) -> None:
        try:
            with self._cleanup_lock:
                self.engine.close()
        except (OSError, RuntimeError) as exc:
            # Expected audio/process cleanup failures remain retryable by the
            # controller/runtime supervisor. Record them instead of silently
            # swallowing a failure that could otherwise hide a survivor.
            self._log("cleanup-warning", f"{type(exc).__name__}: {exc}")

    def _wait_for_pending_starts(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + max(0.05, float(timeout))
        with self._start_condition:
            while self._active_starts > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._start_condition.wait(timeout=min(0.05, remaining))
            return True

    def pause(self) -> PlaybackSnapshot:
        try:
            snapshot = self.engine.pause()
        except Exception as exc:
            raise LibraryAuditionError(f"Could not pause playback: {type(exc).__name__}: {exc}") from exc
        with self._state_lock:
            self._paused_tick = int(snapshot.tick or 0)
        return snapshot

    def resume(self) -> PlaybackSnapshot:
        with self._state_lock:
            path = self._path
            label = self._label
            paused_tick = self._paused_tick
        if not path:
            raise LibraryAuditionError("Nothing is paused. Select a MIDI and press Play first.")
        try:
            snapshot = self.engine.snapshot()
            start_tick = int(snapshot.tick or paused_tick or 0)
            return self.play(path, label, start_tick=start_tick)
        except LibraryAuditionError:
            raise
        except Exception as exc:
            raise LibraryAuditionError(f"Could not resume playback: {type(exc).__name__}: {exc}") from exc

    def _scheduler_alive(self) -> bool:
        probe = getattr(self.engine, "scheduler_alive", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:
                return True
        return False

    def stop(self) -> PlaybackSnapshot:
        """Immediate silence plus verified zero Browser playback process/thread."""
        self.cancel_pending_start()
        self._log("stop-request")
        # Silence first; do not wait for the scheduler join before removing
        # audible notes from the device.
        try:
            self.engine.backend.all_notes_off()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass
        try:
            with self._start_condition:
                had_pending_start = self._active_starts > 0
            with self._cleanup_lock:
                self.engine.close()
            # A Play worker that was still decoding may observe cancellation a
            # few milliseconds later. Wait for it, then close once more so a
            # late audio startup can never survive the completed Stop action.
            if not self._wait_for_pending_starts(timeout=3.0):
                raise RuntimeError("Playback preparation did not cancel within 3 seconds.")
            if had_pending_start:
                with self._cleanup_lock:
                    self.engine.close()
            snapshot = self.engine.snapshot()
            diagnostics = self.engine.audio_diagnostics()
            synth_pid = int((diagnostics or {}).get("pid") or 0) if isinstance(diagnostics, dict) else 0
            scheduler_alive = self._scheduler_alive()
            if scheduler_alive or synth_pid > 0:
                raise RuntimeError(
                    f"Playback cleanup verification failed (scheduler={scheduler_alive}, synth_pid={synth_pid or '-'})."
                )
        except Exception as exc:
            self._log("stop-error", f"{type(exc).__name__}: {exc}")
            raise LibraryAuditionError(f"Could not fully stop playback: {type(exc).__name__}: {exc}") from exc
        with self._state_lock:
            self._path = ""
            self._label = ""
            self._paused_tick = 0
        self._log("stop-complete")
        return snapshot

    @property
    def volume_percent(self) -> int:
        try:
            return max(0, min(150, int(round(float(self.settings.get("library_audition_volume", 100) or 100)))))
        except (TypeError, ValueError, OverflowError):
            return 100

    def set_volume(self, percent: int) -> PlaybackSnapshot:
        value = max(0, min(150, int(percent)))
        self.settings.set("library_audition_volume", value)
        try:
            if self.engine.snapshot().state == PlaybackState.STOPPED:
                return self.engine.snapshot()
            return self.engine.change_gain(value / 100.0)
        except Exception as exc:
            raise LibraryAuditionError(f"Could not change audition volume: {type(exc).__name__}: {exc}") from exc

    def snapshot(self) -> PlaybackSnapshot:
        # PlaybackEngine owns its own short critical section.  Do not serialize
        # UI polling behind slow Play preparation or FluidSynth startup.
        return self.engine.poll()

    def close(self) -> None:
        """Close audition only after the owned audio route is proven terminal."""
        with self._state_lock:
            if self._closed:
                return
            self._request_generation += 1
        self._log("close-request")
        try:
            try:
                self.engine.backend.all_notes_off()
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                pass
            with self._start_condition:
                had_pending_start = self._active_starts > 0
            with self._cleanup_lock:
                self.engine.close()
            if not self._wait_for_pending_starts(timeout=3.0):
                raise RuntimeError("Playback preparation did not cancel within 3 seconds during Browser close.")
            if had_pending_start:
                with self._cleanup_lock:
                    self.engine.close()
            diagnostics = self.engine.audio_diagnostics()
            synth_pid = int((diagnostics or {}).get("pid") or 0) if isinstance(diagnostics, dict) else 0
            scheduler_alive = self._scheduler_alive()
            if scheduler_alive or synth_pid > 0:
                raise RuntimeError(
                    f"Audition cleanup verification failed (scheduler={scheduler_alive}, synth_pid={synth_pid or '-'})."
                )
        except Exception as exc:
            self._log("close-error", f"{type(exc).__name__}: {exc}")
            raise LibraryAuditionError(
                f"Could not fully stop Library audition audio: {type(exc).__name__}: {exc}"
            ) from exc
        self._log("close-complete")
        with self._state_lock:
            self._closed = True
            self._path = ""
            self._label = ""
            self._paused_tick = 0

    @property
    def current_path(self) -> str:
        with self._state_lock:
            return str(self._path or "")

    @property
    def state(self) -> PlaybackState:
        return self.engine.snapshot().state
