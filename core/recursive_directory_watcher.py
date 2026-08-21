from __future__ import annotations

"""Low-overhead recursive library change watcher for Windows.

QFileSystemWatcher does not watch a directory tree recursively.  A six-figure
MIDI library should not register tens of thousands of Qt watcher paths, so on
Windows we use one ReadDirectoryChangesW handle per configured root with
``bWatchSubtree=True``.  The callback only marks the catalogue dirty; the normal
incremental scan reconciles paths later.
"""

from pathlib import Path
import os
import threading
from typing import Callable, Iterable


class RecursiveDirectoryWatcher:
    def __init__(self, callback: Callable[[str], None]) -> None:
        self.callback = callback
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._handles: list[object] = []
        self._roots: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return os.name == "nt"

    def start(self, roots: Iterable[str | Path]) -> bool:
        self.stop()
        normalized = tuple(str(Path(root).resolve(strict=False)) for root in roots if Path(root).is_dir())
        if not normalized or not self.supported:
            return False
        self._stop_event.clear()
        self._roots = normalized
        for root in normalized:
            thread = threading.Thread(
                target=self._watch_windows_root,
                args=(root,),
                name=f"MidiRecursiveWatch-{Path(root).name[:24]}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        return True

    def stop(self, timeout: float = 4.0) -> None:
        self._stop_event.set()
        handles: list[object]
        with self._lock:
            handles = list(self._handles)
            self._handles.clear()
        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                for handle in handles:
                    try:
                        kernel32.CancelIoEx(handle, None)
                        kernel32.CloseHandle(handle)
                    except (OSError, ValueError):
                        continue
            except (ImportError, OSError):
                handles = []
        current = threading.current_thread()
        for thread in list(self._threads):
            if thread is not current and thread.is_alive():
                thread.join(timeout=timeout)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        self._roots = ()

    def _watch_windows_root(self, root: str) -> None:
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        FILE_LIST_DIRECTORY = 0x0001
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        notify_filter = (
            0x00000001  # FILE_NOTIFY_CHANGE_FILE_NAME
            | 0x00000002  # DIR_NAME
            | 0x00000008  # SIZE
            | 0x00000010  # LAST_WRITE
            | 0x00000040  # CREATION
        )
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        handle = kernel32.CreateFileW(
            root,
            FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return
        with self._lock:
            self._handles.append(handle)
        buffer = ctypes.create_string_buffer(64 * 1024)
        returned = wintypes.DWORD()
        try:
            while not self._stop_event.is_set():
                ok = kernel32.ReadDirectoryChangesW(
                    handle,
                    ctypes.byref(buffer),
                    len(buffer),
                    True,
                    notify_filter,
                    ctypes.byref(returned),
                    None,
                    None,
                )
                if not ok:
                    break
                if returned.value and not self._stop_event.is_set():
                    self.callback(root)
        finally:
            with self._lock:
                if handle in self._handles:
                    self._handles.remove(handle)
            try:
                kernel32.CloseHandle(handle)
            except (OSError, ValueError):
                return
