from __future__ import annotations

"""Small, dependency-free helpers for durable bounded file writes."""

from pathlib import Path
import json
import os
import tempfile
from typing import Any


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync on platforms that support it."""

    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, payload: bytes, *, fsync: bool = True) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(temp, target)
        if fsync:
            _fsync_directory(target.parent)
        return target
    except BaseException:
        safe_unlink(temp)
        raise


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
) -> Path:
    return atomic_write_bytes(path, text.encode(encoding), fsync=fsync)


def atomic_write_json(path: str | Path, payload: Any, *, indent: int = 2, fsync: bool = True) -> Path:
    text = json.dumps(payload, indent=indent, ensure_ascii=False)
    return atomic_write_text(path, text, fsync=fsync)


def safe_unlink(path: str | Path) -> bool:
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False
