from __future__ import annotations

"""Bounded, low-volume timing traces for six-figure library operations."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from core.logging_config import append_bounded


def record_performance(log_path: str | Path, event: str, **fields: Any) -> None:
    """Append one compact JSON event without exposing source MIDI paths.

    Performance traces exist to diagnose a long phase without asking the user to
    reproduce it repeatedly.  Callers should pass counts/timings/status labels,
    never raw library file paths.
    """
    payload: dict[str, Any] = {
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": str(event),
    }
    for key, value in fields.items():
        if any(token in str(key).casefold() for token in ("path", "file", "folder")):
            continue
        if isinstance(value, float):
            payload[str(key)] = round(value, 4)
        elif isinstance(value, (str, int, bool)) or value is None:
            payload[str(key)] = value
        else:
            payload[str(key)] = str(value)
    append_bounded(log_path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", max_bytes=512 * 1024, backups=2)
