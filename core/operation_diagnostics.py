from __future__ import annotations

"""Consistent operation-level error summaries and diagnostic logging."""

from datetime import datetime, timezone
from pathlib import Path
import traceback
from typing import Iterable

from core.logging_config import append_bounded, redact_context


def concise_exception(exc: BaseException, *, max_chars: int = 900) -> str:
    message = str(exc).strip() or "No additional message was provided."
    text = f"{type(exc).__name__}: {message}"
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def traceback_location(exc: BaseException) -> str:
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if not tb:
        return ""
    last = tb[-1]
    return f"{Path(last.filename).name}:{last.lineno} in {last.name}"


def operation_failure(
    operation: str,
    exc: BaseException,
    *,
    path: str | Path | None = None,
    command: Iterable[str] | None = None,
    corrective_action: str = "Try the action again. If it repeats, open Runtime Diagnostics and review logs/ui-errors.log.",
) -> str:
    lines = [f"Operation: {operation}", f"Error: {concise_exception(exc)}"]
    location = traceback_location(exc)
    if location:
        lines.append(f"Location: {location}")
    if path:
        lines.append(f"File: {path}")
    if command:
        lines.append("Command: " + " ".join(str(part) for part in command))
    if corrective_action:
        lines.append(f"Next step: {corrective_action}")
    return "\n".join(lines)


def write_diagnostic(log_path: str | Path, operation: str, exc: BaseException, **context: object) -> Path | None:
    target = Path(log_path)
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        full = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        context_lines = "\n".join(f"{key}: {value}" for key, value in redact_context(context.items()))
        block = f"\n[{timestamp}] {operation}\n"
        if context_lines:
            block += context_lines + "\n"
        block += full + "\n"
        append_bounded(target, block)
        return target
    except (OSError, UnicodeError):
        return None
