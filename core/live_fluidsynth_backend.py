from __future__ import annotations

from pathlib import Path
import os
import subprocess
import threading
import time
from typing import Optional

from core.app_paths import AppPaths
from core.fluidsynth_player import FluidSynthPlayer, FluidSynthError
from core.runtime_cleanup import runtime_supervisor
from core.logging_config import append_bounded, rotate_if_needed


class LiveFluidSynthBackend:
    """Persistent FluidSynth shell backend with explicit Windows diagnostics.

    The backend keeps MIDI Performer's real scheduler/transport architecture, but
    stops treating a living FluidSynth process as proof that Windows audio is
    working.  It records the selected driver, captures FluidSynth output, can
    play a direct diagnostic chord, and can be restarted cleanly before live
    playback.
    """

    backend_name = "FluidSynth shell"
    WINDOWS_DRIVER_CANDIDATES = ("default", "dsound", "wasapi", "waveout")

    def __init__(self, app_root: str | Path | None = None, executable_path: str | Path | None = None) -> None:
        self.finder = FluidSynthPlayer(app_root, executable_path=executable_path)
        self.app_root = Path(app_root) if app_root else Path(__file__).resolve().parents[1]
        self.paths = AppPaths.for_app(self.app_root).ensure()
        self.process: Optional[subprocess.Popen] = None
        self.soundfont_path: str | None = None
        self.gain: float = 0.5
        self._lock = threading.RLock()
        self.active_driver: str | None = None
        self.preferred_driver: str = "default"
        self.active_backend_name = "not started"
        self.command_count = 0
        self.note_on_count = 0
        self.last_start_command: list[str] = []
        self.last_start_error: str | None = None
        self._log_handle = None
        self.log_path = self.paths.logs / "audio_diagnostics.log"

    def set_preferred_driver(self, driver: str | None) -> None:
        value = str(driver or "default").strip().lower()
        if value not in self.WINDOWS_DRIVER_CANDIDATES:
            value = "default"
        self.preferred_driver = value

    def available_driver_candidates(self) -> list[str]:
        if os.name != "nt":
            return ["default"]
        ordered = [self.preferred_driver, *self.WINDOWS_DRIVER_CANDIDATES]
        result: list[str] = []
        for driver in ordered:
            if driver not in result:
                result.append(driver)
        return result

    def audio_ready_delay_seconds(self) -> float:
        return 0.0

    def prime_audio(self) -> None:
        return

    def start_keepalive(self) -> None:
        return

    def stop_keepalive(self) -> None:
        return

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @staticmethod
    def _clamp_gain(value: float | int | None) -> float:
        try:
            gain = float(value) if value is not None else 0.5
        except (TypeError, ValueError):
            gain = 0.5
        return max(0.0, min(1.5, gain))

    def _open_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        rotate_if_needed(self.log_path)
        self._log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)

    def _write_log(self, text: str) -> None:
        append_bounded(
            self.log_path,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n",
        )

    def _popen_kwargs(self) -> dict:
        if self._log_handle is None:
            self._open_log()
        kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": self._log_handle,
            "stderr": self._log_handle,
            "text": True,
            "bufsize": 1,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = startupinfo
        return kwargs

    def _start_command(
        self,
        exe: Path | str,
        soundfont: str,
        gain: float,
        driver: str | None = None,
    ) -> list[str]:
        selected_driver = str(driver or ("dsound" if os.name == "nt" else "default"))
        cmd = [str(exe), "-n", "-g", f"{gain:.3f}"]
        if os.name == "nt" and selected_driver != "default":
            cmd.extend(["-a", selected_driver])
        cmd.append(soundfont)
        return cmd

    def start(
        self,
        soundfont_path: str | Path,
        gain: float = 0.5,
        *,
        driver: str | None = None,
        force_restart: bool = False,
    ) -> None:
        sf = Path(soundfont_path)
        if not sf.exists():
            raise FluidSynthError(f"SoundFont file not found: {sf}")
        exe = self.finder.find_executable()
        if not exe:
            raise FluidSynthError(
                "FluidSynth was not found. Expected it at "
                "the FluidSynth location selected in Settings, vendor\\fluidsynth\\bin\\fluidsynth.exe, or Windows PATH."
            )

        selected_driver = str(driver or self.preferred_driver or "default").strip().lower()
        if selected_driver not in self.WINDOWS_DRIVER_CANDIDATES:
            selected_driver = "default"
        sf_str = str(sf)
        gain = self._clamp_gain(gain)

        with self._lock:
            reusable = (
                not force_restart
                and self.is_running()
                and self.soundfont_path == sf_str
                and self.active_driver == selected_driver
            )
            if reusable:
                self.set_gain(gain)
                return

            self.close()
            self.last_start_error = None
            self.command_count = 0
            self.note_on_count = 0
            cmd = self._start_command(exe, sf_str, gain, selected_driver)
            self.last_start_command = list(cmd)
            self._write_log("START " + " ".join(f'"{part}"' if " " in part else part for part in cmd))
            try:
                self.process = runtime_supervisor.register_process(
                    subprocess.Popen(cmd, **self._popen_kwargs()),
                    "FluidSynth live audio",
                )
            except Exception as exc:
                self.last_start_error = f"{type(exc).__name__}: {exc}"
                self._write_log("START FAILED " + self.last_start_error)
                raise FluidSynthError(f"FluidSynth could not start: {exc}") from exc

            self.soundfont_path = sf_str
            self.gain = gain
            self.active_driver = selected_driver
            self.active_backend_name = f"{self.backend_name} · {selected_driver}"

            # A longer check than the prior 150 ms gives Windows drivers time to
            # reject an unavailable endpoint and write the reason to the log.
            time.sleep(0.35)
            if self.process.poll() is not None:
                code = self.process.returncode
                self.last_start_error = f"FluidSynth exited during audio startup (code {code})."
                self._write_log(self.last_start_error)
                runtime_supervisor.unregister_process(self.process)
                self.process = None
                self.soundfont_path = None
                self.active_driver = None
                self.active_backend_name = "not started"
                raise FluidSynthError(
                    f"FluidSynth could not open the Windows audio route '{selected_driver}'. "
                    f"See {self.log_path}."
                )
            self._write_log(f"RUNNING pid={self.process.pid} driver={selected_driver} gain={gain:.3f}")

    def _send(self, command: str) -> None:
        with self._lock:
            if not self.is_running() or self.process is None or self.process.stdin is None:
                raise FluidSynthError("FluidSynth live backend is not running.")
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
                self.command_count += 1
                if command.startswith("noteon "):
                    self.note_on_count += 1
            except (BrokenPipeError, OSError) as exc:
                self.last_start_error = f"FluidSynth command pipe failed: {exc}"
                self._write_log(self.last_start_error)
                raise FluidSynthError("FluidSynth live backend stopped unexpectedly.") from exc

    def set_gain(self, gain: float) -> None:
        gain = self._clamp_gain(gain)
        self.gain = gain
        if self.is_running():
            self._send(f"gain {gain:.3f}")

    def program_change(self, channel: int, program: int) -> None:
        self._send(f"prog {int(channel)} {max(0, min(127, int(program)))}")

    def control_change(self, channel: int, control: int, value: int) -> None:
        self._send(
            f"cc {int(channel)} {max(0, min(127, int(control)))} "
            f"{max(0, min(127, int(value)))}"
        )

    def note_on(self, channel: int, note: int, velocity: int) -> None:
        self._send(
            f"noteon {int(channel)} {max(0, min(127, int(note)))} "
            f"{max(0, min(127, int(velocity)))}"
        )

    def note_off(self, channel: int, note: int) -> None:
        self._send(f"noteoff {int(channel)} {max(0, min(127, int(note)))}")

    def pitch_bend(self, channel: int, value: int) -> None:
        raw = max(0, min(16383, int(value) + 8192))
        self._send(f"pitch_bend {int(channel)} {raw}")

    def test_tone(
        self,
        soundfont_path: str | Path,
        gain: float = 0.7,
        *,
        driver: str | None = None,
        duration_seconds: float = 0.75,
    ) -> dict[str, object]:
        """Play a direct chord without MIDI parsing or the scheduler.

        Hearing this chord proves the executable, SoundFont, selected audio
        driver, command pipe, and Windows output route are working.  It does not
        depend on a loaded MIDI file.
        """
        selected_driver = str(driver or self.preferred_driver or "default").lower()
        self.start(soundfont_path, gain=gain, driver=selected_driver, force_restart=True)
        self.all_notes_off()
        self.reset_controllers()
        self.control_change(0, 0, 0)
        self.control_change(0, 32, 0)
        self.program_change(0, 0)
        self.control_change(0, 7, 115)
        self.control_change(0, 11, 127)
        self.control_change(0, 10, 64)
        chord = (60, 64, 67)
        for note in chord:
            self.note_on(0, note, 112)
        time.sleep(max(0.25, min(1.5, float(duration_seconds))))
        for note in chord:
            self.note_off(0, note)
        self._write_log(
            f"TEST TONE driver={selected_driver} commands={self.command_count} note_on={self.note_on_count}"
        )
        return self.diagnostic_snapshot()

    def diagnostic_snapshot(self) -> dict[str, object]:
        return {
            "running": self.is_running(),
            "driver": self.active_driver or self.preferred_driver,
            "backend": self.active_backend_name,
            "pid": self.process.pid if self.is_running() and self.process else None,
            "commands": int(self.command_count),
            "note_on": int(self.note_on_count),
            "log_path": str(self.log_path),
            "last_error": self.last_start_error,
            "command": list(self.last_start_command),
        }

    def all_notes_off(self) -> None:
        if not self.is_running():
            return
        for ch in range(16):
            try:
                self.control_change(ch, 123, 0)
                self.control_change(ch, 120, 0)
                self.control_change(ch, 64, 0)
            except FluidSynthError:
                break

    def reset_controllers(self) -> None:
        if not self.is_running():
            return
        for ch in range(16):
            try:
                self.control_change(ch, 121, 0)
            except FluidSynthError:
                break

    def close(self) -> None:
        """Stop the owned FluidSynth process and verify terminal state.

        The process reference is intentionally retained when termination cannot
        be confirmed so the runtime supervisor can report and retry the survivor.
        """
        with self._lock:
            proc = self.process
            if proc is not None and proc.poll() is None:
                try:
                    if proc.stdin:
                        proc.stdin.write("quit\n")
                        proc.stdin.flush()
                    proc.wait(timeout=1.0)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    if not runtime_supervisor.terminate_process(proc, timeout=1.5):
                        raise FluidSynthError(
                            f"FluidSynth PID {proc.pid} did not terminate; ownership was retained for retry."
                        )
            if proc is not None:
                if proc.poll() is None:
                    raise FluidSynthError(
                        f"FluidSynth PID {proc.pid} is still running; ownership was retained for retry."
                    )
                runtime_supervisor.unregister_process(proc)
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except (BrokenPipeError, OSError) as exc:
                    self.last_start_error = f"FluidSynth stdin close warning: {type(exc).__name__}: {exc}"
                self.process = None
            self.soundfont_path = None
            self.active_driver = None
            self.active_backend_name = "not started"
            if self._log_handle is not None:
                try:
                    self._log_handle.flush()
                    self._log_handle.close()
                except OSError as exc:
                    self.last_start_error = f"Audio diagnostic log close warning: {type(exc).__name__}: {exc}"
                self._log_handle = None
