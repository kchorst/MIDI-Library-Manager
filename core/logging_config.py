from __future__ import annotations

"""Bounded diagnostic logging with lightweight path redaction."""

from pathlib import Path
import os
from typing import Iterable

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUPS = 3


def _best_effort_unlink(target: Path) -> bool:
    try:
        target.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _best_effort_replace(source: Path, destination: Path) -> bool:
    try:
        os.replace(source, destination)
        return True
    except OSError:
        return False


def redact_path(value: object) -> str:
    text = str(value)
    home = str(Path.home())
    if home and text.casefold().startswith(home.casefold()):
        return "~" + text[len(home):]
    return text


def redact_context(items: Iterable[tuple[str, object]]) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    for key, value in items:
        if value in (None, ""):
            continue
        low = key.casefold()
        text = redact_path(value) if any(token in low for token in ("path", "file", "folder", "soundfont", "command")) else str(value)
        output.append((key, text))
    return output


def rotate_if_needed(
    path: str | Path,
    *,
    incoming_bytes: int = 0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backups: int = DEFAULT_BACKUPS,
) -> None:
    target = Path(path)
    try:
        current = target.stat().st_size
    except OSError:
        current = 0
    if current + max(0, incoming_bytes) <= max_bytes:
        return
    backup_count = max(0, int(backups))
    if backup_count == 0:
        _best_effort_unlink(target)
        return
    oldest = target.with_name(f"{target.name}.{backup_count}")
    _best_effort_unlink(oldest)
    for index in range(backup_count - 1, 0, -1):
        source = target.with_name(f"{target.name}.{index}")
        destination = target.with_name(f"{target.name}.{index + 1}")
        if source.exists():
            _best_effort_replace(source, destination)
    if target.exists():
        _best_effort_replace(target, target.with_name(f"{target.name}.1"))


def append_bounded(path: str | Path, text: str, *, max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = text.encode("utf-8", errors="replace")
    rotate_if_needed(target, incoming_bytes=len(encoded), max_bytes=max_bytes, backups=backups)
    with target.open("ab") as handle:
        handle.write(encoded)
    return target


def clear_logs(directory: str | Path) -> int:
    removed = 0
    root = Path(directory)
    if not root.is_dir():
        return 0
    for path in root.glob("*.log*"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
