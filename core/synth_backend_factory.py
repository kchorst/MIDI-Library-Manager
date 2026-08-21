from __future__ import annotations

from pathlib import Path

from core.live_fluidsynth_backend import LiveFluidSynthBackend


def create_synth_backend(app_root: str | Path | None = None, executable_path: str | Path | None = None) -> LiveFluidSynthBackend:
    """Return the single known-good live playback backend.

    Automatic switching between shell and in-process FluidSynth is disabled.
    A backend that starts but is silent is worse than a clear startup failure,
    so Windows uses the same persistent DirectSound shell path that previously
    produced audio on the target system.
    """

    return LiveFluidSynthBackend(app_root, executable_path=executable_path)
