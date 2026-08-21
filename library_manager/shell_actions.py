from __future__ import annotations

"""OS shell actions for source MIDI files.

These helpers deliberately live outside the Qt browser so the Windows command
semantics can be regression-tested in headless CI.  The Library must distinguish
opening the MIDI itself from revealing its exact path in Explorer.
"""

import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Mapping, Any


PathLike = str | os.PathLike[str]


def _existing_file(path: PathLike) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"MIDI source file is not currently available: {candidate}")
    return candidate


def _windows_popen_kwargs(platform_name: str) -> dict[str, Any]:
    if platform_name != "win32":
        return {}
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    }
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is not None:
        startupinfo = startupinfo_type()
        startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
        kwargs["startupinfo"] = startupinfo
    return kwargs




def _windows_shell_select_file(source: Path) -> bool:
    """Use the Windows Shell API to reveal and select an exact source file.

    SHOpenFolderAndSelectItems with cidl=0 accepts a fully-qualified item PIDL
    and opens the parent Explorer folder with that item selected. This avoids
    depending on Explorer command-line parsing and gives us a real HRESULT.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        coinit_apartmentthreaded = 0x2
        rpc_e_changed_mode = ctypes.c_long(0x80010106).value
        hr_init = int(ole32.CoInitializeEx(None, coinit_apartmentthreaded))
        initialized_here = hr_init in (0, 1)
        if hr_init not in (0, 1, rpc_e_changed_mode):
            raise OSError(f"Windows COM initialization failed (HRESULT 0x{hr_init & 0xffffffff:08X})")
        try:
            shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
            shell32.ILCreateFromPathW.restype = ctypes.c_void_p
            pidl = shell32.ILCreateFromPathW(str(source))
            if not pidl:
                return False
            try:
                shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
                shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
                hr = int(shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0))
                if hr != 0:
                    raise OSError(f"Windows Explorer selection failed (HRESULT 0x{hr & 0xffffffff:08X})")
                return True
            finally:
                shell32.ILFree.argtypes = [ctypes.c_void_p]
                shell32.ILFree(pidl)
        finally:
            if initialized_here:
                ole32.CoUninitialize()
    except (AttributeError, OSError, TypeError, ValueError):
        return False

def open_midi_file(
    path: PathLike,
    *,
    platform_name: str | None = None,
    startfile: Callable[[str], object] | None = None,
    popen: Callable[..., object] = subprocess.Popen,
) -> Path:
    """Open the actual MIDI file with the operating system's associated app."""

    source = _existing_file(path)
    platform_name = str(platform_name or sys.platform)
    if platform_name == "win32":
        opener = startfile or getattr(os, "startfile", None)
        if opener is None:
            raise OSError("Windows file-association launcher is unavailable")
        opener(str(source))
    elif platform_name == "darwin":
        popen(["open", str(source)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        popen(["xdg-open", str(source)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return source


def show_file_in_folder(
    path: PathLike,
    *,
    platform_name: str | None = None,
    popen: Callable[..., object] = subprocess.Popen,
) -> Path:
    """Reveal a source MIDI, selecting the exact file where the OS supports it.

    Windows is the release target and uses Explorer's /select switch, so this
    must not degrade into merely opening the containing directory.
    """

    source = _existing_file(path)
    platform_name = str(platform_name or sys.platform)
    if platform_name == "win32":
        # Prefer the Shell API on real Windows so success/failure is explicit and
        # the exact file selection does not depend on command-line parsing. Keep
        # explorer.exe /select as a compatibility fallback and for headless tests.
        if os.name != "nt" or not _windows_shell_select_file(source):
            popen(["explorer.exe", f"/select,{source}"])
    elif platform_name == "darwin":
        popen(["open", "-R", str(source)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # Common Linux file managers do not expose a portable select-file
        # command.  Opening the parent is the safest cross-desktop fallback.
        popen(["xdg-open", str(source.parent)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return source


def open_folder(
    path: PathLike,
    *,
    platform_name: str | None = None,
    startfile: Callable[[str], object] | None = None,
    popen: Callable[..., object] = subprocess.Popen,
) -> Path:
    """Open an existing folder in the operating system's file manager."""

    folder = Path(path).expanduser().resolve(strict=False)
    if not folder.is_dir():
        raise FileNotFoundError(f"Folder is not currently available: {folder}")
    platform_name = str(platform_name or sys.platform)
    if platform_name == "win32":
        opener = startfile or getattr(os, "startfile", None)
        if opener is None:
            raise OSError("Windows folder launcher is unavailable")
        opener(str(folder))
    elif platform_name == "darwin":
        popen(["open", str(folder)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        popen(["xdg-open", str(folder)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return folder
