from __future__ import annotations

"""Runtime ownership and shutdown cleanup for MIDI Remixer.

Only subprocesses explicitly registered by this application are terminated.
On Windows they are also assigned to a kill-on-close Job Object when possible,
which provides a final safety net if Python exits unexpectedly.
"""

from dataclasses import dataclass
import atexit
import ctypes
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class CleanupReport:
    requested_processes: int
    terminated_processes: int
    remaining_processes: tuple[int, ...]
    callback_errors: tuple[str, ...]


class _WindowsKillOnCloseJob:
    """Best-effort Windows Job Object with KILL_ON_JOB_CLOSE."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            kernel32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            kernel32.SetInformationJobObject.restype = ctypes.c_int
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.AssignProcessToJobObject.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            info.BasicLimitInformation.LimitFlags = 0x00002000
            ok = kernel32.SetInformationJobObject(
                handle,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                kernel32.CloseHandle(handle)
                return
            self._kernel32 = kernel32
            self.handle = handle
        except (AttributeError, OSError, TypeError, ValueError):
            self.handle = None

    def assign(self, process: subprocess.Popen) -> bool:
        if self.handle is None or os.name != "nt":
            return False
        try:
            process_handle = ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
            return bool(self._kernel32.AssignProcessToJobObject(self.handle, process_handle))
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def close(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is not None:
            try:
                self._kernel32.CloseHandle(handle)
            except (AttributeError, OSError, TypeError, ValueError):
                # The process registry remains authoritative even if the final Job handle is already invalid.
                return


class RuntimeSupervisor:
    """Own subprocesses and cleanup callbacks created by MIDI Remixer.

    Ownership records are removed only after a resource is proven terminal. A
    partial cleanup remains retryable and its report lists exact surviving PIDs.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[int, tuple[subprocess.Popen, str]] = {}
        self._callbacks: list[tuple[str, Callable[[], None]]] = []
        self._completed_callbacks: set[str] = set()
        self._cleaning = False
        self._cleaned = False
        self._shutdown_event = threading.Event()
        self._job = _WindowsKillOnCloseJob()
        self._job_closed = False
        # Interpreter finalization is a last-resort safety net, not another
        # graceful-shutdown phase.  Never register the normal cleanup() here:
        # arbitrary callbacks can block indefinitely after the Qt loop/Engine
        # coordinator has already ended.  The atexit fallback below performs
        # only non-blocking hard cleanup of subprocesses this supervisor owns.
        atexit.register(self._atexit_cleanup)

    def _atexit_cleanup(self) -> None:
        """Final interpreter safety net with no unbounded callback waits.

        Orderly application/Engine shutdown must call :meth:`cleanup` while the
        runtime is fully alive.  At interpreter finalization it is too late to
        trust arbitrary cleanup callbacks, joins, IPC, or database work: one
        stuck callback can keep a headless Python process alive forever.

        This fallback therefore does only what can be made effectively
        non-blocking: signal shutdown, close the Windows kill-on-close Job, and
        hard-kill any still-owned subprocess handles.  It deliberately does not
        wait for verification; explicit lifecycle owners are responsible for
        proving terminality before normal exit.
        """
        try:
            self._shutdown_event.set()
        except (AttributeError, RuntimeError):
            pass
        try:
            with self._lock:
                snapshot = [process for process, _label in self._processes.values()]
                if not self._job_closed:
                    try:
                        self._job.close()
                    finally:
                        self._job_closed = True
        except (AttributeError, RuntimeError, TypeError):
            snapshot = []
        for process in snapshot:
            try:
                if process.poll() is None:
                    process.kill()
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                pass

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def register_process(self, process: subprocess.Popen, label: str) -> subprocess.Popen:
        if process.pid is None:
            return process
        late_registration = False
        with self._lock:
            # A subprocess created during shutdown must never become unowned.
            # The previous implementation tried one short termination and then
            # returned without registering the PID.  If that bounded kill lost
            # a Windows scheduling race, the child was invisible to the final
            # cleanup sweep and could outlive the Engine.  Register first, then
            # attempt the eager stop; a survivor remains in the registry until
            # terminal verification succeeds.
            late_registration = bool(
                self._cleaning or self._cleaned or self._shutdown_event.is_set()
            )
            self._processes[int(process.pid)] = (process, str(label))
            self._cleaned = False
            if not self._job_closed:
                self._job.assign(process)
        if late_registration and self._terminate_process(process, timeout=0.5):
            self.unregister_process(process)
        return process

    def unregister_process(self, process: subprocess.Popen | None) -> bool:
        if process is None or process.pid is None:
            return True
        if process.poll() is None:
            return False
        with self._lock:
            self._processes.pop(int(process.pid), None)
        return True

    def register_cleanup(self, name: str, callback: Callable[[], None]) -> None:
        with self._lock:
            if not any(existing == name for existing, _ in self._callbacks):
                self._callbacks.append((name, callback))

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    @staticmethod
    def _terminate_process(process: subprocess.Popen, timeout: float = 1.5) -> bool:
        """Terminate one process inside one absolute caller-owned budget.

        Older code gave the graceful wait ``timeout`` *and then* allowed a
        three-second ``taskkill`` plus another 1.5-second wait.  A caller that
        requested a 250 ms control-plane preemption could therefore block for
        several seconds per worker.  The supplied timeout is now the whole
        operation, including escalation and verification.
        """
        if process.poll() is not None:
            return True
        budget = max(0.0, float(timeout))
        if budget <= 0:
            # No verification time remains, but shutdown still must not abandon
            # a live owned child. Hard-kill is non-blocking; the caller can
            # report that terminal verification exceeded its deadline.
            try:
                if process.poll() is None:
                    process.kill()
            except (OSError, ProcessLookupError):
                pass
            return process.poll() is not None
        deadline = time.monotonic() + budget

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        # Preserve some of the caller's budget for hard escalation.  A process
        # that ignores terminate must not consume the entire window before kill.
        graceful = min(0.75, max(0.05, budget * 0.35))
        try:
            process.terminate()
            rem = remaining()
            if rem > 0:
                process.wait(timeout=min(graceful, rem))
        except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
            pass
        if process.poll() is not None:
            return True

        rem = remaining()
        if rem <= 0.01:
            return False
        try:
            if os.name == "nt" and process.pid and rem >= 0.20:
                # ``taskkill /T`` is useful for an owned child tree, but its own
                # timeout must fit inside the same caller deadline.  If there is
                # too little time, direct kill is the only bounded escalation.
                try:
                    taskkill_budget = remaining()
                    if taskkill_budget <= 0:
                        process.kill()
                    else:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=taskkill_budget,
                            check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        process.kill()
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            return process.poll() is not None

        rem = remaining()
        if process.poll() is None and rem > 0.01:
            try:
                process.wait(timeout=rem)
            except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
                pass
        return process.poll() is not None

    def terminate_process(self, process: subprocess.Popen, timeout: float = 1.5) -> bool:
        """Terminate one owned child; retain it in the registry on failure."""

        stopped = self._terminate_process(process, timeout=timeout)
        if stopped:
            self.unregister_process(process)
        return stopped

    def _active_processes_locked(self) -> dict[int, tuple[subprocess.Popen, str]]:
        terminal = [pid for pid, (process, _label) in self._processes.items() if process.poll() is not None]
        for pid in terminal:
            self._processes.pop(pid, None)
        return {pid: item for pid, item in self._processes.items() if item[0].poll() is None}

    def cleanup(self, timeout: float | None = None) -> CleanupReport:
        """Release registered resources, optionally under one absolute budget.

        ``timeout`` is used by the Library Engine daemon so the runtime safety net
        cannot silently outlive the Engine's advertised shutdown contract.  The
        atexit registration keeps the historical unbounded best-effort behavior by
        calling this method without a timeout.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))

        def remaining(default: float = 1.5) -> float:
            if deadline is None:
                return max(0.05, float(default))
            return max(0.0, deadline - time.monotonic())

        with self._lock:
            active = self._active_processes_locked()
            pending_callbacks = [item for item in reversed(self._callbacks) if item[0] not in self._completed_callbacks]
            if self._cleaned and not active and not pending_callbacks:
                return CleanupReport(0, 0, (), ())
            if self._cleaning:
                remaining = tuple(sorted(active))
                return CleanupReport(len(remaining), 0, remaining, ("cleanup already in progress",))
            self._cleaning = True
            self._shutdown_event.set()
            requested = len(active)

        callback_errors: dict[str, str] = {}
        failed_callbacks: list[tuple[str, Callable[[], None]]] = []
        for name, callback in pending_callbacks:
            try:
                callback()
            except Exception as exc:
                callback_errors[name] = f"{type(exc).__name__}: {exc}"
                failed_callbacks.append((name, callback))
            else:
                with self._lock:
                    self._completed_callbacks.add(name)

        process_errors: list[str] = []
        terminated = 0
        with self._lock:
            process_snapshot = list(self._active_processes_locked().values())
        for process, label in process_snapshot:
            rem = remaining()
            if deadline is not None and rem <= 0.01:
                process_errors.append(f"{label}: PID {process.pid} exceeded runtime cleanup deadline")
                continue
            if self._terminate_process(process, timeout=rem if deadline is not None else 1.5):
                terminated += 1
                self.unregister_process(process)
            else:
                process_errors.append(f"{label}: PID {process.pid} did not terminate")

        # The Windows Job Object is the final tree-wide escalation boundary.
        # Closing it is immediate and is especially important when the orderly
        # portion has consumed its deadline.  Verification still uses only the
        # remaining caller budget.
        with self._lock:
            has_remaining = bool(self._active_processes_locked())
        if has_remaining and not self._job_closed:
            self._job.close()
            self._job_closed = True
            with self._lock:
                remaining_snapshot = list(self._active_processes_locked().values())
            for process, _label in remaining_snapshot:
                rem = remaining(default=1.0)
                if deadline is not None and rem <= 0.01:
                    break
                try:
                    process.wait(timeout=min(1.0, rem))
                except (OSError, subprocess.TimeoutExpired):
                    continue
                self.unregister_process(process)

        # A callback such as a QThread shutdown barrier may initially fail only
        # because its worker is blocked in one of the owned child processes just
        # terminated above. Retry those callbacks once in the same cleanup pass.
        for name, callback in failed_callbacks:
            try:
                callback()
            except Exception as exc:
                callback_errors[name] = f"{type(exc).__name__}: {exc}"
            else:
                callback_errors.pop(name, None)
                with self._lock:
                    self._completed_callbacks.add(name)

        # A worker can be created in the narrow interval between the first
        # process snapshot and shutdown quiescence (for example, a scan thread
        # that had already borrowed an analyzer runner).  register_process()
        # now retains such a PID, so perform one final bounded sweep before the
        # cleanup result is declared terminal.  This is intentionally based on
        # the *current* registry, not the earlier snapshot.
        with self._lock:
            late_snapshot = list(self._active_processes_locked().values())
        for process, label in late_snapshot:
            rem = remaining()
            if deadline is not None and rem <= 0.01:
                process_errors.append(f"{label}: PID {process.pid} exceeded final runtime cleanup deadline")
                continue
            if self._terminate_process(process, timeout=rem if deadline is not None else 1.5):
                self.unregister_process(process)
            else:
                process_errors.append(f"{label}: PID {process.pid} survived final runtime cleanup sweep")

        with self._lock:
            remaining_active = self._active_processes_locked()
            remaining = tuple(sorted(remaining_active))
            pending_names = [name for name, _callback in self._callbacks if name not in self._completed_callbacks]
            # A transient termination miss is not itself a shutdown failure if
            # later escalation proved every owned process terminal.
            if not remaining_active:
                process_errors.clear()
            errors = [f"{name}: {detail}" for name, detail in sorted(callback_errors.items())]
            errors.extend(process_errors)
            if pending_names:
                errors.append("pending cleanup callbacks: " + ", ".join(pending_names))
            self._cleaned = not remaining and not pending_names and not errors
            self._cleaning = False
        return CleanupReport(requested, terminated, remaining, tuple(errors))

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            active = {
                pid: label for pid, (process, label) in self._active_processes_locked().items()
            }
            pending_callbacks = [name for name, _ in self._callbacks if name not in self._completed_callbacks]
        return {
            "shutdown_requested": self.shutdown_requested,
            "active_processes": active,
            "active_process_count": len(active),
            "pending_cleanup_callbacks": pending_callbacks,
            "cleanup_complete": self._cleaned,
        }


runtime_supervisor = RuntimeSupervisor()
