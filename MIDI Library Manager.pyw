from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback


def _report(exc: BaseException) -> None:
    source_root = Path(__file__).resolve().parent
    version = "unknown"
    try:
        text = (source_root / "library_manager" / "version.py").read_text(encoding="utf-8")
        import re
        match = re.search(r"^MANAGER_PRODUCT_VERSION\s*=\s*['\"]([^'\"]+)", text, flags=re.MULTILINE)
        if match:
            version = match.group(1)
    except (OSError, UnicodeError):
        version = "unknown"
    provenance = (
        f"Version: {version}\n"
        f"Launch folder: {source_root}\n"
        f"Python: {sys.executable}\n"
        f"Working directory: {Path.cwd()}"
    )
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MIDI Remixer" / "logs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        log = base / "library-manager-startup-error.log"
        log.write_text(provenance + "\n\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")
    except OSError:
        log = None
    message = f"MIDI Library Manager could not start.\n\n{type(exc).__name__}: {exc}\n\n{provenance}"
    if log:
        message += f"\n\nDetails: {log}"
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "MIDI Library Manager Startup Error", 0x10)
            return
        except (AttributeError, OSError) as dialog_exc:
            message += f"\n\nWindows startup dialog was unavailable: {type(dialog_exc).__name__}."
    print(message)


try:
    from library_manager.window import main
    raise SystemExit(main())
except SystemExit:
    raise
except BaseException as exc:
    _report(exc)
    raise SystemExit(1)
