from __future__ import annotations

"""Helpers for launching console workers from GUI Python runtimes.

On Windows a .pyw launcher normally runs under pythonw.exe.  Background services
and workers that use stdin/stdout pipes are more reliable under the matching
python.exe.  Keep the interpreter inside the same environment so dependencies
and ABI versions remain identical.
"""

from pathlib import Path
import os
import sys


def console_python_executable(executable: str | None = None) -> str:
    current = Path(executable or sys.executable)
    if os.name == "nt" and current.name.casefold() == "pythonw.exe":
        console = current.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(current)


def gui_python_executable(executable: str | None = None) -> str:
    """Return the matching no-console Python executable when available."""
    current = Path(executable or sys.executable)
    if os.name == "nt" and current.name.casefold() == "python.exe":
        gui = current.with_name("pythonw.exe")
        if gui.is_file():
            return str(gui)
    return str(current)


def launch_gui_python_script(script: str | Path, *, cwd: str | Path | None = None):
    """Launch a sibling GUI Python entry point without exposing a console window."""
    import subprocess

    executable = gui_python_executable()
    kwargs: dict[str, object] = {
        "cwd": str(cwd) if cwd is not None else None,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([executable, str(Path(script))], **kwargs)
