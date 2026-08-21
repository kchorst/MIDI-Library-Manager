from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess
from typing import Callable, Sequence

from core.app_paths import AppPaths


@dataclass(frozen=True)
class ToolCheck:
    name: str
    found: bool
    path: str | None = None
    confirmed: bool = False
    version: str = ""
    detail: str = ""
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    corrective_action: str = ""


def _windows_program_roots() -> list[Path]:
    roots: list[Path] = []
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramData"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    return roots


MAX_PRIOR_RELEASE_ROOTS = 24


def _looks_like_remixer_release(path: Path) -> bool:
    name = path.name.casefold().replace("_", "-").replace(" ", "-")
    return "midi-remixer" in name or "midi-performer" in name


def _prior_release_roots(app_root: Path) -> list[Path]:
    """Return a small, bounded set of nearby earlier extracted releases.

    Users commonly extract each ZIP into its own sibling directory. v0.19.2
    moved mutable dependencies to the per-user data directory, but it also
    stopped seeing a previously downloaded FluidSynth/SoundFont. We inspect
    only two immediate ancestor containers and one nested release directory;
    there is no unbounded filesystem crawl.
    """

    containers: list[Path] = []
    for candidate in (app_root.parent, app_root.parent.parent):
        if candidate not in containers:
            containers.append(candidate)

    roots: list[tuple[float, Path]] = []
    seen: set[str] = {str(app_root.resolve()).casefold()}
    for container in containers:
        try:
            entries = list(container.iterdir())
        except OSError:
            continue
        for entry in entries[:256]:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
            except OSError:
                continue
            candidates = [entry] if _looks_like_remixer_release(entry) else []
            if not candidates:
                continue
            # "Extract All" may create an outer folder containing a single
            # identically named release folder. Include only one nested level.
            try:
                nested = [
                    child for child in list(entry.iterdir())[:32]
                    if child.is_dir() and not child.is_symlink() and _looks_like_remixer_release(child)
                ]
            except OSError:
                nested = []
            candidates.extend(nested)
            for candidate in candidates:
                try:
                    resolved = candidate.resolve()
                    key = str(resolved).casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    roots.append((resolved.stat().st_mtime, resolved))
                except OSError:
                    continue
    roots.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in roots[:MAX_PRIOR_RELEASE_ROOTS]]


def _fluidsynth_candidates(base: Path) -> list[Path]:
    candidates = [
        base / "bin" / "fluidsynth.exe",
        base / "fluidsynth.exe",
        base / "bin" / "fluidsynth",
        base / "fluidsynth",
    ]
    if not base.is_dir():
        return candidates
    try:
        children = [child for child in list(base.iterdir())[:32] if child.is_dir() and not child.is_symlink()]
    except OSError:
        children = []
    for child in children:
        candidates.extend(
            (
                child / "bin" / "fluidsynth.exe",
                child / "fluidsynth.exe",
                child / "bin" / "fluidsynth",
                child / "fluidsynth",
            )
        )
    return candidates


class DependencyRegistry:
    """One authoritative resolver for executable and SoundFont identities.

    Search order is explicit: user selection, managed per-user dependency
    directory, immutable package assets, a bounded set of nearby prior extracted
    releases, system PATH, then conventional Windows program locations.
    """

    def __init__(self, app_root: str | Path) -> None:
        self.paths = AppPaths.for_app(app_root)
        self.app_root = self.paths.app_root

    @staticmethod
    def _first_file(candidates: Sequence[Path]) -> str | None:
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                resolved = candidate.expanduser()
            key = str(resolved).casefold()
            if key in seen:
                continue
            seen.add(key)
            if resolved.is_file():
                return str(resolved)
        return None

    def find_fluidsynth(self, configured_path: str | Path | None = None) -> str | None:
        candidates: list[Path] = []
        if configured_path:
            candidates.append(Path(configured_path))
        for base in (self.paths.dependencies / "fluidsynth", self.app_root / "vendor" / "fluidsynth"):
            candidates.extend(_fluidsynth_candidates(base))
        for release_root in _prior_release_roots(self.app_root):
            for base in (
                release_root / "vendor" / "fluidsynth",
                release_root / "dependencies" / "fluidsynth",
                release_root / "user-data" / "dependencies" / "fluidsynth",
            ):
                candidates.extend(_fluidsynth_candidates(base))
        found = shutil.which("fluidsynth") or shutil.which("fluidsynth.exe")
        if found:
            candidates.append(Path(found))
        for base in _windows_program_roots():
            for rel in (
                Path("FluidSynth") / "bin" / "fluidsynth.exe",
                Path("FluidSynth") / "fluidsynth.exe",
                Path("fluidsynth") / "bin" / "fluidsynth.exe",
                Path("Programs") / "FluidSynth" / "bin" / "fluidsynth.exe",
                Path("Programs") / "FluidSynth" / "fluidsynth.exe",
            ):
                candidates.append(base / rel)
        return self._first_file(candidates)

    def find_ffmpeg(self, configured_path: str | Path | None = None) -> str | None:
        candidates: list[Path] = []
        if configured_path:
            candidates.append(Path(configured_path))
        for base in (self.paths.dependencies / "ffmpeg", self.app_root / "vendor" / "ffmpeg"):
            candidates.extend([base / "bin" / "ffmpeg.exe", base / "ffmpeg.exe", base / "bin" / "ffmpeg", base / "ffmpeg"] )
            if base.is_dir():
                candidates.extend(sorted(base.glob("**/ffmpeg.exe")))
                candidates.extend(sorted(base.glob("**/ffmpeg")))
        found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if found:
            candidates.append(Path(found))
        return self._first_file(candidates)

    def find_soundfonts(
        self,
        configured_path: str | Path | None = None,
        configured_directory: str | Path | None = None,
    ) -> list[str]:
        candidates: list[Path] = []
        if configured_path:
            candidates.append(Path(configured_path))
        if configured_directory:
            directory = Path(configured_directory).expanduser()
            if directory.is_dir():
                candidates.extend(sorted(directory.glob("*.sf2")))
                candidates.extend(sorted(directory.glob("*.sf3")))
        directories = [
            self.paths.soundfonts,
            self.app_root / "assets" / "soundfonts",
            self.app_root / "soundfonts",
            self.app_root / "vendor" / "soundfonts",
        ]
        for release_root in _prior_release_roots(self.app_root):
            directories.extend(
                (
                    release_root / "assets" / "soundfonts",
                    release_root / "soundfonts",
                    release_root / "vendor" / "soundfonts",
                    release_root / "user-data" / "soundfonts",
                )
            )
        for directory in directories:
            if directory.is_dir():
                candidates.extend(sorted(directory.glob("*.sf2")))
                candidates.extend(sorted(directory.glob("*.sf3")))
        env_sf = os.environ.get("MIDI_REMIXER_SOUNDFONT") or os.environ.get("SOUNDFONT")
        if env_sf:
            candidates.insert(0, Path(env_sf).expanduser())
        results: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            key = str(resolved).casefold()
            if key in seen:
                continue
            if resolved.is_file() and resolved.suffix.lower() in {".sf2", ".sf3"}:
                seen.add(key)
                results.append(str(resolved))
        return results


def find_fluidsynth(app_root: str | Path, configured_path: str | Path | None = None) -> str | None:
    return DependencyRegistry(app_root).find_fluidsynth(configured_path)


def find_ffmpeg(app_root: str | Path, configured_path: str | Path | None = None) -> str | None:
    return DependencyRegistry(app_root).find_ffmpeg(configured_path)


def find_soundfonts(
    app_root: str | Path,
    configured_path: str | Path | None = None,
    configured_directory: str | Path | None = None,
) -> list[str]:
    return DependencyRegistry(app_root).find_soundfonts(configured_path, configured_directory)


def find_soundfont(
    app_root: str | Path,
    configured_path: str | Path | None = None,
    configured_directory: str | Path | None = None,
) -> str | None:
    candidates = find_soundfonts(app_root, configured_path, configured_directory)
    return candidates[0] if candidates else None


def _run_command(
    command: Sequence[str],
    *,
    timeout: float = 7.0,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[bool, str, str, int | None]:
    try:
        kwargs: dict = {"capture_output": True, "text": True, "timeout": timeout, "check": False}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = startupinfo
        result = runner(list(command), **kwargs)
    except subprocess.TimeoutExpired as exc:
        return False, "", f"Command timed out after {timeout:g} seconds: {exc}", None
    except Exception as exc:  # noqa: BLE001
        return False, "", f"{type(exc).__name__}: {exc}", None
    output = "\n".join(part.strip() for part in (result.stdout or "", result.stderr or "") if part and part.strip())
    first = output.splitlines()[0].strip() if output.splitlines() else "command completed"
    return result.returncode == 0, first, output[-1200:], int(result.returncode)


def _soundfont_header_ok(path: Path) -> tuple[bool, str]:
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError as exc:
        return False, f"Cannot read the selected SoundFont: {exc}"
    if len(header) < 12:
        return False, "The selected SoundFont is truncated."
    if header[:4] != b"RIFF" or header[8:12] != b"sfbk":
        return False, "The file does not contain a valid RIFF SoundFont bank header."
    return True, "SoundFont bank header confirmed."


def check_fluidsynth(app_root: str | Path, configured_path: str | Path | None = None) -> ToolCheck:
    path = find_fluidsynth(app_root, configured_path)
    if not path:
        return ToolCheck(
            "FluidSynth", False,
            detail="FluidSynth was not found in saved settings, managed dependencies, package assets, standard install locations, or PATH.",
            corrective_action="Install FluidSynth or browse to the FluidSynth executable.",
        )
    command = (path, "--version")
    confirmed, version, detail, code = _run_command(command)
    return ToolCheck(
        "FluidSynth", True, path, confirmed, version,
        f"Confirmed: {version}" if confirmed else detail, command, code,
        "Choose a different FluidSynth executable or reinstall FluidSynth." if not confirmed else "",
    )


def check_ffmpeg(app_root: str | Path, configured_path: str | Path | None = None) -> ToolCheck:
    path = find_ffmpeg(app_root, configured_path)
    if not path:
        return ToolCheck(
            "FFmpeg", False, detail="FFmpeg was not found. MIDI and WAV export remain available, but MP3 export will be disabled.",
            corrective_action="Install FFmpeg or place it in vendor/ffmpeg/bin.",
        )
    command = (path, "-version")
    confirmed, version, detail, code = _run_command(command)
    return ToolCheck(
        "FFmpeg", True, path, confirmed, version,
        f"Confirmed: {version}" if confirmed else detail, command, code,
        "Choose a working FFmpeg executable before exporting MP3." if not confirmed else "",
    )


def check_soundfont(
    app_root: str | Path,
    configured_path: str | Path | None = None,
    configured_directory: str | Path | None = None,
    *,
    fluidsynth_path: str | Path | None = None,
    probe_load: bool = False,
) -> ToolCheck:
    path = find_soundfont(app_root, configured_path, configured_directory)
    if not path:
        return ToolCheck(
            "SoundFont", False, detail="No .sf2 or .sf3 instrument library was found.",
            corrective_action="Choose a General MIDI .sf2 or .sf3 SoundFont.",
        )
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        return ToolCheck("SoundFont", False, str(candidate), detail=str(exc), corrective_action="Choose a readable SoundFont file.")
    if size < 1024:
        return ToolCheck(
            "SoundFont", True, str(candidate), False, detail="The selected SoundFont is unusually small or truncated.",
            corrective_action="Choose another .sf2 or .sf3 file.",
        )
    header_ok, header_detail = _soundfont_header_ok(candidate)
    if not header_ok:
        return ToolCheck(
            "SoundFont", True, str(candidate), False, detail=header_detail,
            corrective_action="Choose a valid SoundFont bank rather than a renamed or incomplete file.",
        )
    if probe_load and fluidsynth_path and Path(fluidsynth_path).is_file():
        command = (str(Path(fluidsynth_path)), "-ni", "-q", str(candidate))
        confirmed, version, detail, code = _run_command(command, timeout=8.0)
        if not confirmed:
            return ToolCheck(
                "SoundFont", True, str(candidate), False, version,
                f"{header_detail} FluidSynth load test failed: {detail}", command, code,
                "Try another SoundFont or verify that this FluidSynth build supports the file format.",
            )
        return ToolCheck("SoundFont", True, str(candidate), True, version, f"{header_detail} FluidSynth load test passed.", command, code)
    return ToolCheck("SoundFont", True, str(candidate), True, detail=header_detail)

