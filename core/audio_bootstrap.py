from __future__ import annotations

"""Bounded installation of optional audio dependencies.

A release-pinned artifact remains the preferred path. On Windows, MIDI Remixer
can also resolve the official FluidSynth GitHub release at install time. The
resolver accepts only the FluidSynth project's release API and download path,
uses the release-asset SHA-256 digest when GitHub provides one, bounds every
network/archive operation, validates the extracted executable, and installs to
the per-user dependency directory.
"""

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from typing import Callable

from core.app_paths import AppPaths
from core.atomic_io import atomic_write_text, safe_unlink
from core.remix_version import APP_VERSION

FLUIDSYNTH_RELEASE_API = "https://api.github.com/repos/FluidSynth/fluidsynth/releases/latest"
FLUIDSYNTH_DOWNLOAD_PREFIX = "https://github.com/FluidSynth/fluidsynth/releases/download/"
MAX_API_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
MAX_ARCHIVE_FILES = 4000
MAX_ARCHIVE_EXPANDED_BYTES = 700 * 1024 * 1024


@dataclass(frozen=True)
class AudioSetupResult:
    ok: bool
    fluidsynth_path: str | None = None
    soundfont_path: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class ApprovedArtifact:
    name: str
    version: str
    url: str
    sha256: str
    max_bytes: int
    license_url: str = ""
    source: str = "release manifest"


def _request(url: str):
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": f"MIDI-Remixer/{APP_VERSION} setup",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _artifact_manifest(app_root: Path) -> dict[str, object]:
    path = app_root / "data" / "approved_audio_artifacts.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


def _approved_artifact(app_root: Path, key: str) -> ApprovedArtifact | None:
    raw = _artifact_manifest(app_root).get(key)
    if not isinstance(raw, dict):
        return None
    try:
        artifact = ApprovedArtifact(
            name=str(raw["name"]),
            version=str(raw["version"]),
            url=str(raw["url"]),
            sha256=str(raw["sha256"]).casefold(),
            max_bytes=min(MAX_DOWNLOAD_BYTES, int(raw.get("max_bytes") or MAX_DOWNLOAD_BYTES)),
            license_url=str(raw.get("license_url") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not artifact.url.startswith("https://") or not _valid_sha256(artifact.sha256):
        return None
    return artifact


def automatic_install_available(app_root: str | Path, key: str = "fluidsynth_windows_x64") -> bool:
    """Return whether the application can offer one-click setup.

    A pinned manifest entry works on any supported platform. The official
    release resolver is intentionally offered only on Windows, where the app
    expects ``fluidsynth.exe`` and where the prior setup button existed.
    """

    if _approved_artifact(Path(app_root).resolve(), key) is not None:
        return True
    return os.name == "nt" and key == "fluidsynth_windows_x64"


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Audio dependency setup was cancelled.")


def _read_bounded_response(response, limit: int) -> bytes:
    declared = int(response.headers.get("Content-Length", "0") or 0)
    if declared and declared > limit:
        raise RuntimeError("The release metadata response exceeded its safety limit.")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("The release metadata response exceeded its safety limit.")
    return data


def _official_windows_artifact(cancel_event: threading.Event | None = None) -> ApprovedArtifact:
    _check_cancelled(cancel_event)
    with urllib.request.urlopen(_request(FLUIDSYNTH_RELEASE_API), timeout=30) as response:  # noqa: S310
        raw = _read_bounded_response(response, MAX_API_BYTES)
    _check_cancelled(cancel_event)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"The official FluidSynth release metadata was unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("The official FluidSynth release metadata had an unexpected format.")

    version = str(payload.get("tag_name") or payload.get("name") or "latest").strip()
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("The official FluidSynth release did not list downloadable assets.")

    machine = platform.machine().casefold()
    wants_x64 = machine in {"amd64", "x86_64", "x64", ""}
    choices: list[tuple[int, ApprovedArtifact]] = []
    for raw_asset in assets:
        if not isinstance(raw_asset, dict):
            continue
        name = str(raw_asset.get("name") or "").strip()
        low = name.casefold()
        url = str(raw_asset.get("browser_download_url") or "").strip()
        try:
            size = int(raw_asset.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if (
            not name
            or not low.endswith(".zip")
            or "win" not in low
            or any(token in low for token in ("android", "source", "debug", "symbols", "arm64", "aarch64"))
            or not url.startswith(FLUIDSYNTH_DOWNLOAD_PREFIX)
            or size <= 0
            or size > MAX_DOWNLOAD_BYTES
        ):
            continue
        is_x64 = any(token in low for token in ("x64", "win64", "amd64"))
        is_x86 = any(token in low for token in ("x86", "win32", "i686")) and not is_x64
        if wants_x64 and not is_x64:
            continue
        if not wants_x64 and is_x64:
            continue
        score = 100 if is_x64 else 50
        if "cpp11" in low:
            score += 20
        if "win10" in low or "windows" in low:
            score += 10
        if "glib" in low:
            score -= 5
        if is_x86:
            score -= 5

        digest_value = str(raw_asset.get("digest") or "").strip().casefold()
        digest = digest_value.removeprefix("sha256:")
        if not _valid_sha256(digest):
            digest = ""
        choices.append(
            (
                score,
                ApprovedArtifact(
                    name=name,
                    version=version,
                    url=url,
                    sha256=digest,
                    max_bytes=min(MAX_DOWNLOAD_BYTES, max(size + 1024 * 1024, size * 2)),
                    license_url="https://github.com/FluidSynth/fluidsynth/blob/master/LICENSE",
                    source="official FluidSynth GitHub release",
                ),
            )
        )
    if not choices:
        raise RuntimeError("The official FluidSynth release did not contain a supported Windows package.")
    choices.sort(key=lambda item: (item[0], item[1].name.casefold()), reverse=True)
    return choices[0][1]


def _download_artifact(
    artifact: ApprovedArtifact,
    target: Path,
    progress: Callable[[str, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    safe_unlink(partial)
    digest = hashlib.sha256()
    received = 0
    try:
        _check_cancelled(cancel_event)
        with urllib.request.urlopen(_request(artifact.url), timeout=60) as response:  # noqa: S310 - allowlisted HTTPS
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared and declared > artifact.max_bytes:
                raise RuntimeError(f"Download exceeds the approved {artifact.max_bytes // (1024 * 1024)} MiB limit.")
            with partial.open("wb") as out:
                while True:
                    _check_cancelled(cancel_event)
                    chunk = response.read(512 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > artifact.max_bytes:
                        raise RuntimeError("Download exceeded its approved size limit.")
                    digest.update(chunk)
                    out.write(chunk)
                    if progress:
                        pct = int(received * 100 / declared) if declared else 0
                        progress(artifact.name, max(0, min(99, pct)))
                out.flush()
                os.fsync(out.fileno())
        _check_cancelled(cancel_event)
        actual = digest.hexdigest().casefold()
        if artifact.sha256 and actual != artifact.sha256:
            raise RuntimeError(
                f"Downloaded artifact identity did not match its published SHA-256 digest "
                f"(expected {artifact.sha256}, got {actual})."
            )
        os.replace(partial, target)
        return actual
    finally:
        safe_unlink(partial)


def _download_verified(
    artifact: ApprovedArtifact,
    target: Path,
    progress: Callable[[str, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Backward-compatible name for the bounded artifact downloader."""

    return _download_artifact(artifact, target, progress, cancel_event)


def _safe_extract(
    zip_path: Path,
    destination: Path,
    cancel_event: threading.Event | None = None,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError("Downloaded archive contains too many files.")
        expanded = sum(max(0, int(member.file_size)) for member in members)
        if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
            raise RuntimeError("Downloaded archive exceeds the approved expanded-size limit.")
        for member in members:
            _check_cancelled(cancel_event)
            target = (destination / member.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError("Unsafe path found in downloaded archive.")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError("Symbolic links are not allowed in downloaded archives.")
        for member in members:
            _check_cancelled(cancel_event)
            archive.extract(member, destination)


def _select_fluidsynth_executable(root: Path) -> Path | None:
    executables = sorted(root.glob("**/fluidsynth.exe"), key=lambda path: ("bin" not in path.parts, len(path.parts)))
    return executables[0] if executables else None


def _validate_fluidsynth(executable: Path) -> str:
    if not executable.is_file():
        raise RuntimeError("FluidSynth setup did not produce fluidsynth.exe.")
    if os.name != "nt":
        return "executable present"
    kwargs: dict[str, object] = {
        "cwd": str(executable.parent),
        "capture_output": True,
        "text": True,
        "timeout": 12,
        "check": False,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    kwargs["startupinfo"] = startupinfo
    try:
        result = subprocess.run([str(executable), "--version"], **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Downloaded fluidsynth.exe could not start: {exc}") from exc
    output = "\n".join(part.strip() for part in (result.stdout or "", result.stderr or "") if part and part.strip())
    if result.returncode != 0 or "fluidsynth" not in output.casefold():
        raise RuntimeError(
            "Downloaded fluidsynth.exe did not pass its version check. "
            + (output[-600:] if output else f"Exit code: {result.returncode}")
        )
    return output.splitlines()[0] if output.splitlines() else "FluidSynth"


def install_fluidsynth(
    app_root: str | Path,
    progress: Callable[[str, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> AudioSetupResult:
    root = Path(app_root).resolve()
    paths = AppPaths.for_app(root).ensure()
    fs_root = paths.dependencies / "fluidsynth"
    existing = _select_fluidsynth_executable(fs_root) if fs_root.exists() else None
    if existing is not None:
        return AudioSetupResult(True, fluidsynth_path=str(existing), detail="Managed FluidSynth is ready.")

    artifact = _approved_artifact(root, "fluidsynth_windows_x64")
    try:
        if artifact is None:
            if os.name != "nt":
                return AudioSetupResult(
                    False,
                    detail="Automatic FluidSynth setup is currently available only on Windows. Choose an existing executable.",
                )
            if progress:
                progress("Checking the official FluidSynth release", 0)
            artifact = _official_windows_artifact(cancel_event)
        if progress:
            progress(f"Downloading {artifact.name} {artifact.version}", 1)
        with tempfile.TemporaryDirectory(prefix="midi-remixer-fluidsynth-") as temp_name:
            temp = Path(temp_name)
            archive_path = temp / artifact.name
            actual_hash = _download_artifact(artifact, archive_path, progress, cancel_event)
            unpacked = temp / "unpacked"
            _safe_extract(archive_path, unpacked, cancel_event)
            unpacked_executable = _select_fluidsynth_executable(unpacked)
            if unpacked_executable is None:
                raise RuntimeError("The FluidSynth package did not contain fluidsynth.exe.")
            version_line = _validate_fluidsynth(unpacked_executable)

            staging = paths.dependencies / ".fluidsynth-staging"
            backup = paths.dependencies / ".fluidsynth-backup"
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
            shutil.copytree(unpacked, staging)
            staged_executable = _select_fluidsynth_executable(staging)
            if staged_executable is None:
                raise RuntimeError("FluidSynth staging did not contain fluidsynth.exe.")
            _validate_fluidsynth(staged_executable)
            if fs_root.exists():
                os.replace(fs_root, backup)
            try:
                os.replace(staging, fs_root)
            except Exception:
                if backup.exists() and not fs_root.exists():
                    os.replace(backup, fs_root)
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(backup, ignore_errors=True)

        existing = _select_fluidsynth_executable(fs_root)
        if existing is None:
            raise RuntimeError("FluidSynth setup completed without a usable executable.")
        atomic_write_text(
            fs_root / "MIDI-REMIXER-INSTALL-SOURCE.txt",
            "\n".join(
                (
                    f"Source: {artifact.source}",
                    f"Version: {artifact.version}",
                    f"Asset: {artifact.name}",
                    f"URL: {artifact.url}",
                    f"SHA-256: {actual_hash}",
                    f"Published digest verified: {'yes' if artifact.sha256 else 'not supplied by API'}",
                    f"Executable check: {version_line}",
                    "",
                )
            ),
        )
        if progress:
            progress("FluidSynth ready", 100)
        return AudioSetupResult(True, fluidsynth_path=str(existing), detail=f"FluidSynth {artifact.version} is ready.")
    except (
        OSError,
        RuntimeError,
        urllib.error.URLError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as exc:
        return AudioSetupResult(False, detail=str(exc))


def install_recommended_soundfont(
    app_root: str | Path,
    progress: Callable[[str, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> AudioSetupResult:
    root = Path(app_root).resolve()
    paths = AppPaths.for_app(root).ensure()
    existing = next(iter(sorted(paths.soundfonts.glob("*.sf2"))), None)
    if existing is not None:
        return AudioSetupResult(True, soundfont_path=str(existing), detail="Managed SoundFont is ready.")
    artifact = _approved_artifact(root, "recommended_soundfont")
    if artifact is None:
        return AudioSetupResult(
            False,
            detail="Automatic SoundFont download is disabled until this release pins an immutable URL and SHA-256 digest.",
        )
    try:
        target = paths.soundfonts / artifact.name
        _download_artifact(artifact, target, progress, cancel_event)
        if target.stat().st_size < 1024:
            raise RuntimeError("Verified SoundFont is unexpectedly small.")
        atomic_write_text(
            paths.soundfonts / f"{target.stem}-SOURCE.txt",
            f"Name: {artifact.name}\nVersion: {artifact.version}\nURL: {artifact.url}\nSHA-256: {artifact.sha256}\n",
        )
        if progress:
            progress("Instrument library ready", 100)
        return AudioSetupResult(True, soundfont_path=str(target), detail=f"{artifact.name} is ready.")
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        return AudioSetupResult(False, detail=str(exc))


def install_audio_dependencies(
    app_root: str | Path,
    progress: Callable[[str, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> AudioSetupResult:
    fs = install_fluidsynth(app_root, progress, cancel_event)
    if not fs.ok:
        return fs
    sf = install_recommended_soundfont(app_root, progress, cancel_event)
    if not sf.ok:
        return AudioSetupResult(False, fluidsynth_path=fs.fluidsynth_path, detail=sf.detail)
    return AudioSetupResult(True, fs.fluidsynth_path, sf.soundfont_path, "Verified audio dependencies are ready.")
