from __future__ import annotations

"""Bounded command-line FluidSynth playback and rendering."""

from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from typing import Optional

import mido

from core.atomic_io import safe_unlink
from core.dependency_setup import DependencyRegistry
from core.runtime_cleanup import runtime_supervisor


MAX_RENDER_SECONDS = 30 * 60
MAX_RENDER_BYTES = 2 * 1024 * 1024 * 1024
MIN_PROCESS_TIMEOUT = 60.0
MAX_PROCESS_TIMEOUT = 45 * 60.0


class FluidSynthError(RuntimeError):
    pass


class FluidSynthPlayer:
    """Small wrapper around FluidSynth command-line playback/rendering.

    All child processes are registered with ``RuntimeSupervisor``. Render output
    is produced in a temporary file and atomically promoted only after validation.
    """

    def __init__(self, app_root: str | Path | None = None, executable_path: str | Path | None = None) -> None:
        self.process: Optional[subprocess.Popen] = None
        self.app_root = Path(app_root) if app_root else Path(__file__).resolve().parents[1]
        self.executable_path: Path | None = Path(executable_path).expanduser() if executable_path else None
        self.dependencies = DependencyRegistry(self.app_root)

    def set_executable_path(self, executable_path: str | Path | None) -> None:
        self.executable_path = Path(executable_path).expanduser() if executable_path else None

    def find_executable(self) -> Optional[Path | str]:
        return self.dependencies.find_fluidsynth(self.executable_path)

    def find_ffmpeg(self) -> Optional[Path | str]:
        return self.dependencies.find_ffmpeg()

    def is_available(self) -> bool:
        return self.find_executable() is not None

    @staticmethod
    def _popen_kwargs() -> dict:
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = startupinfo
        return kwargs

    def _run_hidden(
        self,
        cmd: list[str],
        *,
        timeout: float,
        cancel_event: threading.Event | None = None,
    ) -> subprocess.CompletedProcess:
        process = runtime_supervisor.register_process(
            subprocess.Popen(cmd, **self._popen_kwargs()),
            f"Render process: {Path(cmd[0]).name}",
        )
        deadline = time.monotonic() + max(1.0, float(timeout))
        try:
            while process.poll() is None:
                cancelled = bool(cancel_event and cancel_event.is_set()) or runtime_supervisor.shutdown_requested
                if cancelled:
                    stopped = runtime_supervisor.terminate_process(process, timeout=2.0)
                    if not stopped:
                        raise FluidSynthError(
                            "Render cancellation was requested, but the child process could not be stopped. "
                            "Restart MIDI Remixer before rendering again."
                        )
                    raise FluidSynthError("Audio render was cancelled.")
                if time.monotonic() >= deadline:
                    stopped = runtime_supervisor.terminate_process(process, timeout=2.0)
                    if not stopped:
                        raise FluidSynthError(
                            f"Render process exceeded {timeout:.0f} seconds and could not be stopped. "
                            "Restart MIDI Remixer before rendering again."
                        )
                    raise FluidSynthError(f"Render process exceeded the {timeout:.0f}-second safety timeout.")
                time.sleep(0.05)
            return subprocess.CompletedProcess(cmd, int(process.returncode or 0))
        except BaseException:
            if process.poll() is None:
                runtime_supervisor.terminate_process(process, timeout=1.0)
            raise
        finally:
            if process.poll() is not None:
                runtime_supervisor.unregister_process(process)

    @staticmethod
    def _gain_arg(volume_gain: float | int | None) -> str:
        try:
            value = float(volume_gain) if volume_gain is not None else 0.5
        except (TypeError, ValueError):
            value = 0.5
        return f"{max(0.0, min(1.5, value)):.3f}"

    @staticmethod
    def _midi_duration(path: Path) -> float:
        try:
            return max(0.0, float(mido.MidiFile(str(path), clip=True).length))
        except (OSError, EOFError, ValueError, TypeError) as exc:
            raise FluidSynthError(f"Could not determine MIDI duration: {exc}") from exc

    @classmethod
    def _validate_render_request(cls, midi: Path, soundfont: Path) -> tuple[float, float]:
        if not midi.is_file():
            raise FluidSynthError(f"MIDI file not found: {midi}")
        if not soundfont.is_file():
            raise FluidSynthError(f"SoundFont file not found: {soundfont}")
        duration = cls._midi_duration(midi)
        if duration > MAX_RENDER_SECONDS:
            raise FluidSynthError(
                f"Audio render is limited to {MAX_RENDER_SECONDS // 60} minutes per Take; "
                f"this MIDI is approximately {duration / 60:.1f} minutes."
            )
        timeout = min(MAX_PROCESS_TIMEOUT, max(MIN_PROCESS_TIMEOUT, duration * 2.5 + 45.0))
        return duration, timeout

    def play(self, midi_path: str | Path, soundfont_path: str | Path, volume_gain: float = 0.5) -> None:
        if not self.stop():
            raise FluidSynthError("Previous FluidSynth playback did not stop; restart MIDI Remixer before playing again.")
        midi = Path(midi_path)
        sf = Path(soundfont_path)
        if not midi.exists():
            raise FluidSynthError(f"MIDI file not found: {midi}")
        if not sf.exists():
            raise FluidSynthError(f"SoundFont file not found: {sf}")
        exe = self.find_executable()
        if not exe:
            raise FluidSynthError("FluidSynth was not found. Choose a verified FluidSynth executable in Audio Setup.")
        cmd = [str(exe), "-i", "-n", "-a", "dsound", "-g", self._gain_arg(volume_gain), str(sf), str(midi)]
        self.process = runtime_supervisor.register_process(
            subprocess.Popen(cmd, **self._popen_kwargs()),
            "FluidSynth one-shot playback",
        )

    @staticmethod
    def _wav_data_offset(path: Path) -> tuple[int, int]:
        with path.open("rb") as handle:
            if handle.read(4) != b"RIFF":
                raise wave.Error("Not a RIFF file")
            handle.seek(8)
            if handle.read(4) != b"WAVE":
                raise wave.Error("Not a WAVE file")
            while True:
                header = handle.read(8)
                if len(header) != 8:
                    raise wave.Error("WAV data chunk not found")
                chunk_id, size = struct.unpack("<4sI", header)
                data_start = handle.tell()
                if chunk_id == b"data":
                    return data_start, size
                handle.seek(size + (size & 1), os.SEEK_CUR)

    @staticmethod
    def _clean_wav_start(wav_path: str | Path, fade_ms: float = 28.0, silence_ms: float = 3.0) -> None:
        """Edit only the leading guard region; memory use is independent of file size."""

        path = Path(wav_path)
        if not path.exists():
            return
        try:
            with wave.open(str(path), "rb") as reader:
                params = reader.getparams()
            data_offset, data_size = FluidSynthPlayer._wav_data_offset(path)
        except (wave.Error, OSError, struct.error):
            return
        if params.sampwidth != 2 or params.nframes <= 0 or params.framerate <= 0:
            return
        channels = max(1, int(params.nchannels or 1))
        silence_frames = max(0, int(params.framerate * float(silence_ms) / 1000.0))
        fade_frames = max(1, int(params.framerate * float(fade_ms) / 1000.0))
        guard_frames = min(params.nframes, silence_frames + fade_frames)
        guard_bytes = min(data_size, guard_frames * channels * params.sampwidth)
        if guard_bytes <= 0:
            return
        try:
            with path.open("r+b") as handle:
                handle.seek(data_offset)
                samples = bytearray(handle.read(guard_bytes))
                sample_count = len(samples) // 2
                silence_samples = min(sample_count, silence_frames * channels)
                samples[: silence_samples * 2] = b"\x00" * (silence_samples * 2)
                for index in range(silence_samples, sample_count):
                    frame_index = index // channels
                    scale = min(1.0, max(0.0, (frame_index - silence_frames) / float(fade_frames)))
                    offset = index * 2
                    value = int.from_bytes(samples[offset:offset + 2], "little", signed=True)
                    samples[offset:offset + 2] = int(value * scale).to_bytes(2, "little", signed=True)
                handle.seek(data_offset)
                handle.write(samples)
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, OverflowError, ValueError):
            return

    @staticmethod
    def _validate_audio_output(path: Path) -> None:
        if not path.is_file() or path.stat().st_size <= 44:
            raise FluidSynthError("Renderer did not produce a usable audio file.")
        if path.stat().st_size > MAX_RENDER_BYTES:
            raise FluidSynthError("Rendered audio exceeded the 2 GiB safety limit.")

    def render_wav(
        self,
        midi_path: str | Path,
        soundfont_path: str | Path,
        output_wav: str | Path,
        volume_gain: float = 0.5,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        midi = Path(midi_path)
        sf = Path(soundfont_path)
        out = Path(output_wav).expanduser()
        _duration, timeout = self._validate_render_request(midi, sf)
        exe = self.find_executable()
        if not exe:
            raise FluidSynthError("FluidSynth was not found, so WAV export cannot run.")
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{out.stem}.", suffix=".partial.wav", dir=str(out.parent))
        os.close(fd)
        temp = Path(temp_name)
        safe_unlink(temp)
        cmd = [
            str(exe), "-n", "-i", "-g", self._gain_arg(volume_gain),
            "-T", "wav", "-O", "s16", "-F", str(temp), "-r", "44100", str(sf), str(midi),
        ]
        try:
            result = self._run_hidden(cmd, timeout=timeout, cancel_event=cancel_event)
            if result.returncode != 0:
                raise FluidSynthError("WAV export failed. Check that the MIDI and SoundFont can play first.")
            self._validate_audio_output(temp)
            self._clean_wav_start(temp)
            os.replace(temp, out)
            return out
        finally:
            safe_unlink(temp)

    def render_mp3(
        self,
        midi_path: str | Path,
        soundfont_path: str | Path,
        output_mp3: str | Path,
        volume_gain: float = 0.5,
        cancel_event: threading.Event | None = None,
        bitrate_kbps: int = 192,
    ) -> Path:
        midi = Path(midi_path)
        sf = Path(soundfont_path)
        mp3 = Path(output_mp3).expanduser()
        duration, timeout = self._validate_render_request(midi, sf)
        ffmpeg = self.find_ffmpeg()
        if not ffmpeg:
            raise FluidSynthError("MP3 export needs FFmpeg. WAV and MIDI export remain available.")
        mp3.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="midi-remixer-render-") as temp_name:
            temp_root = Path(temp_name)
            temp_wav = temp_root / "source.wav"
            temp_mp3 = temp_root / "output.mp3"
            self.render_wav(midi, sf, temp_wav, volume_gain=volume_gain, cancel_event=cancel_event)
            bitrate = min((128, 192, 256, 320), key=lambda value: abs(value - int(bitrate_kbps or 192)))
            cmd = [
                str(ffmpeg), "-y", "-i", str(temp_wav),
                "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k", str(temp_mp3),
            ]
            result = self._run_hidden(
                cmd,
                timeout=max(60.0, min(timeout, duration * 1.5 + 45.0)),
                cancel_event=cancel_event,
            )
            if result.returncode != 0:
                raise FluidSynthError("MP3 export failed. Try WAV export or check FFmpeg.")
            self._validate_audio_output(temp_mp3)
            fd, staging_name = tempfile.mkstemp(prefix=f".{mp3.stem}.", suffix=".partial.mp3", dir=str(mp3.parent))
            os.close(fd)
            staging = Path(staging_name)
            try:
                shutil.copyfile(temp_mp3, staging)
                os.replace(staging, mp3)
            finally:
                safe_unlink(staging)
        return mp3

    def stop(self) -> bool:
        process = self.process
        if process is None:
            return True
        stopped = runtime_supervisor.terminate_process(process, timeout=2.0)
        if stopped:
            self.process = None
        return stopped
