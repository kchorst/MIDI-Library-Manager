from __future__ import annotations

"""Collision-safe export planning shared by the UI and tests."""

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import tempfile

from core.take_rendering import apply_master_volume_percent, normalize_volume_percent


VALID_FORMATS = ("midi", "wav", "mp3")
FORMAT_SUFFIX = {"midi": ".mid", "wav": ".wav", "mp3": ".mp3"}


def safe_export_name(value: str, fallback: str = "remix-take") -> str:
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .-")
    text = re.sub(r"\s", "-", text)
    text = re.sub(r"-+", "-", text)
    result = text[:96].strip(".-") or fallback
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if result.split(".", 1)[0].upper() in reserved:
        result = f"remix-{result}"
    return result


def unique_target(directory: str | Path, basename: str, suffix: str, reserved: set[str] | None = None) -> Path:
    root = Path(directory).expanduser()
    stem = safe_export_name(basename)
    suffix = suffix if str(suffix).startswith(".") else f".{suffix}"
    reserved_keys = reserved if reserved is not None else set()
    candidate = root / f"{stem}{suffix}"
    counter = 2
    while candidate.exists() or str(candidate).casefold() in reserved_keys:
        candidate = root / f"{stem}-{counter}{suffix}"
        counter += 1
    reserved_keys.add(str(candidate).casefold())
    return candidate


@dataclass(frozen=True)
class ExportSelection:
    output_directory: str
    basename: str
    formats: tuple[str, ...] = ("midi",)
    mp3_bitrate_kbps: int = 192
    bake_master_volume_into_midi: bool = False
    open_folder_after: bool = True

    def normalized(self) -> "ExportSelection":
        formats = tuple(dict.fromkeys(
            item for item in (str(value).casefold() for value in self.formats) if item in VALID_FORMATS
        ))
        if not formats:
            raise ValueError("Choose at least one export format.")
        directory = Path(self.output_directory).expanduser()
        if not str(directory).strip():
            raise ValueError("Choose an export folder.")
        bitrate = min((128, 192, 256, 320), key=lambda value: abs(value - int(self.mp3_bitrate_kbps or 192)))
        return ExportSelection(
            output_directory=str(directory),
            basename=safe_export_name(self.basename),
            formats=formats,
            mp3_bitrate_kbps=bitrate,
            bake_master_volume_into_midi=bool(self.bake_master_volume_into_midi),
            open_folder_after=bool(self.open_folder_after),
        )

    def targets(self) -> dict[str, Path]:
        item = self.normalized()
        reserved: set[str] = set()
        return {
            kind: unique_target(item.output_directory, item.basename, FORMAT_SUFFIX[kind], reserved)
            for kind in item.formats
        }


def export_midi(
    source_midi: str | Path,
    target_midi: str | Path,
    *,
    bake_master_volume: bool = False,
    master_volume_percent: int = 100,
) -> Path:
    source = Path(source_midi)
    target = Path(target_midi)
    if not source.is_file():
        raise FileNotFoundError(f"Take MIDI not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Export target already exists: {target}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".partial.mid", dir=str(target.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        if bake_master_volume:
            apply_master_volume_percent(source, temp, normalize_volume_percent(master_volume_percent))
        else:
            shutil.copy2(source, temp)
        if target.exists():
            raise FileExistsError(f"Export target appeared while exporting: {target}")
        os.replace(temp, target)
        return target
    finally:
        temp.unlink(missing_ok=True)
