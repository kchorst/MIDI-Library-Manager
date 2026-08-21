from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import signal
import threading
import time
from typing import Any

from core.app_paths import AppPaths
from core.python_runtime import console_python_executable
from library_engine.protocol import (
    ENGINE_API_VERSION, ENGINE_PRODUCT_VERSION, ENGINE_PROTOCOL_VERSION, LIBRARY_CONTRACT_VERSION, endpoint_path, read_endpoint,
)
from library_engine.timing import CLIENT_SHUTDOWN_WAIT_SECONDS, ENGINE_SHUTDOWN_HARD_SECONDS, SHUTDOWN_REQUEST_SECONDS


class LibraryEngineUnavailable(RuntimeError):
    pass


def _read_tail(path: Path, *, limit: int = 5000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max(256, int(limit)):].strip()


def _engine_startup_diagnostic(paths: AppPaths) -> str:
    """Return the most useful persisted Engine-startup diagnostic available."""
    structured = paths.logs / "library-engine-startup-error.json"
    try:
        payload = json.loads(structured.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        payload = {}
    if isinstance(payload, dict):
        error = str(payload.get("error") or "").strip()
        if error:
            return error
    launch_log = _read_tail(paths.logs / "library-engine-launch.log")
    if launch_log:
        return launch_log
    return ""




def _pid_alive(pid: int) -> bool:
    target = int(pid or 0)
    if target <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, target)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return int(code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    # A detached Engine launched by this Python process can briefly remain as a
    # zombie after it has closed every database handle and removed its lifetime
    # lock. kill(pid, 0) reports zombies as alive, so reap/detect them first.
    try:
        waited, _status = os.waitpid(target, os.WNOHANG)
        if waited == target:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    proc_stat = Path(f"/proc/{target}/stat")
    try:
        fields = proc_stat.read_text(encoding="ascii").split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except (OSError, UnicodeError):
        pass
    try:
        os.kill(target, 0)
    except OSError:
        return False
    return True


def _process_birth_token(pid: int) -> str:
    """Return a best-effort process creation identity for PID-reuse safety.

    Full-exit escalation may need to occur after the Engine has already removed
    its endpoint file. A bare PID is not enough at that point because Windows
    can eventually recycle process identifiers. Capturing the process creation
    timestamp before authenticated shutdown lets the client prove that the PID
    still names the same Engine before any last-resort taskkill.
    """
    target = int(pid or 0)
    if target <= 0:
        return ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, target)
            if not handle:
                return ""
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)
                ):
                    return ""
                value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
                return f"win:{value}"
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return ""
    proc_stat = Path(f"/proc/{target}/stat")
    try:
        raw = proc_stat.read_text(encoding="ascii", errors="strict")
        close = raw.rfind(")")
        if close < 0:
            return ""
        fields = raw[close + 2:].split()
        # /proc/<pid>/stat field 22 is process starttime. The remainder starts
        # at field 3 (state), so starttime is index 19 here.
        if len(fields) > 19:
            return f"proc:{fields[19]}"
    except (OSError, UnicodeError, ValueError):
        return ""
    return ""


def _windows_process_parent_snapshot() -> dict[int, int]:
    """Return PID -> parent PID using Toolhelp32 without shelling out."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (0, INVALID_HANDLE_VALUE):
        return {}
    result: dict[int, int] = {}
    entry = PROCESSENTRY32W(); entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _windows_descendant_pids(root_pid: int) -> set[int]:
    parents = _windows_process_parent_snapshot()
    descendants: set[int] = set()
    frontier = {int(root_pid)}
    while frontier:
        next_frontier = {pid for pid, parent in parents.items() if parent in frontier and pid not in descendants}
        descendants.update(next_frontier)
        frontier = next_frontier
    return descendants


def _windows_terminate_pid(pid: int) -> None:
    if os.name != "nt" or int(pid or 0) <= 0:
        return
    import ctypes
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, int(pid))
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)

def _version_key(value: object) -> tuple[int, int, int]:
    """Comparable product-version key used only to avoid Engine downgrades.

    API compatibility is still governed by ENGINE_API_VERSION/capabilities.
    Product version decides which compatible Engine implementation should own
    the detached process when independently installed components differ.
    """
    parts = str(value or "").strip().split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits or 0))
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3])


class LibraryEngineClient:
    """Small localhost client used by Library Manager and MIDI Remixer.

    The engine endpoint is per-user and bound to 127.0.0.1. A random token in
    the user's AppData directory prevents unrelated local processes from issuing
    maintenance commands by merely guessing the port.
    """

    def __init__(self, app_root: str | Path, *, required_capabilities: set[str] | None = None) -> None:
        self.app_root = Path(app_root).resolve()
        self.required_capabilities = frozenset(required_capabilities or ())
        self.paths = AppPaths.for_app(self.app_root).ensure()
        self.endpoint_file = endpoint_path(self.paths.library)
        self.start_lock_file = self.paths.library / "library_engine_start.lock"
        self.last_shutdown_forced = False
        self.last_shutdown_detail = ""
        # Exact ownership ticket for the short interval after this client has
        # launched the detached Engine but before that Engine has published its
        # authenticated endpoint.  A PID lock alone is not enough to make the
        # Manager's emergency shutdown path race-proof on Windows: process birth
        # metadata can be temporarily unavailable, while the Popen handle is an
        # exact kernel identity that cannot be confused with a recycled PID.
        self._startup_process_lock = threading.Lock()
        self._owned_startup_process: subprocess.Popen[Any] | None = None

    def _request(self, payload: dict[str, Any], *, timeout: float = 2.0) -> dict[str, Any]:
        endpoint = read_endpoint(self.endpoint_file)
        if endpoint is None:
            raise LibraryEngineUnavailable("MIDI Library Engine is not running.")
        request = dict(payload)
        request["token"] = endpoint.token
        request["protocol_version"] = ENGINE_PROTOCOL_VERSION
        wire = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with socket.create_connection((endpoint.host, endpoint.port), timeout=timeout) as connection:
                connection.settimeout(timeout)
                connection.sendall(wire)
                received = bytearray()
                while b"\n" not in received:
                    block = connection.recv(65536)
                    if not block:
                        break
                    received.extend(block)
                    if len(received) > 4 * 1024 * 1024:
                        raise LibraryEngineUnavailable("MIDI Library Engine response exceeded the safety limit.")
        except (OSError, socket.timeout) as exc:
            raise LibraryEngineUnavailable(f"MIDI Library Engine did not respond: {exc}") from exc
        if not received:
            raise LibraryEngineUnavailable("MIDI Library Engine returned no response.")
        try:
            response = json.loads(bytes(received).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LibraryEngineUnavailable("MIDI Library Engine returned an unreadable response.") from exc
        if not isinstance(response, dict):
            raise LibraryEngineUnavailable("MIDI Library Engine returned an invalid response.")
        if not response.get("ok", False):
            raise RuntimeError(str(response.get("error") or "MIDI Library Engine command failed."))
        return response

    def ping(self, *, timeout: float = 0.5) -> dict[str, Any] | None:
        try:
            return self._request({"command": "ping"}, timeout=timeout)
        except (LibraryEngineUnavailable, RuntimeError):
            return None

    def _validate_running_engine(self, response: dict[str, Any]) -> None:
        api = str(response.get("api_version") or "")
        if api.split(".", 1)[0] != ENGINE_API_VERSION.split(".", 1)[0]:
            raise LibraryEngineUnavailable(
                f"Library Engine API {api or 'unknown'} is incompatible with required {ENGINE_API_VERSION}."
            )
        contract = str(response.get("library_contract_version") or "").strip()
        if contract != LIBRARY_CONTRACT_VERSION:
            raise LibraryEngineUnavailable(
                f"Library contract {contract or 'unknown'} is incompatible with required {LIBRARY_CONTRACT_VERSION}. "
                "Stop the older detached Engine and launch the current Library/Remixer generation again."
            )
        capabilities = {str(item) for item in response.get("capabilities") or ()}
        missing = sorted(self.required_capabilities - capabilities)
        if missing:
            raise LibraryEngineUnavailable(
                "The running MIDI Library Engine is compatible at the protocol level but is missing required "
                "capabilities: " + ", ".join(missing) + ". Stop the old Engine and launch this version again."
            )

    @staticmethod
    def _running_source_is_available(response: dict[str, Any]) -> bool:
        raw = str(response.get("source_root") or "").strip()
        if not raw:
            return True  # pre-source-root compatible Engine; do not kill it merely for missing metadata.
        root = Path(raw)
        return root.is_dir() and (root / "library_engine" / "daemon.py").is_file()

    def _start_lock_pid(self) -> int:
        """Return the PID named by the startup lock even when that PID is dead.

        Full-exit cleanup needs the dead PID as an ownership key long enough to
        remove a stale target-owned lock.  ``_live_engine_pid_without_response``
        intentionally filters dead PIDs for normal startup arbitration, so the
        two questions must remain separate.
        """
        try:
            return int(self.start_lock_file.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            return 0

    def _live_engine_pid_without_response(self) -> int | None:
        endpoint = read_endpoint(self.endpoint_file)
        if endpoint is not None and _pid_alive(endpoint.pid):
            return int(endpoint.pid)
        locked_pid = self._start_lock_pid()
        return locked_pid if _pid_alive(locked_pid) else None

    def _remember_owned_startup_process(self, process: subprocess.Popen[Any]) -> None:
        """Publish the exact child handle while Engine startup has no endpoint."""
        with self._startup_process_lock:
            self._owned_startup_process = process

    def _clear_owned_startup_process(self, process: subprocess.Popen[Any] | None = None) -> None:
        """Release the pre-endpoint ownership ticket once IPC or terminality wins."""
        with self._startup_process_lock:
            current = self._owned_startup_process
            if process is None or current is process:
                self._owned_startup_process = None

    def _owned_startup_target(self) -> dict[str, Any]:
        """Return an immutable shutdown ticket backed by this client's Popen handle.

        Do not discard the exact handle merely because Windows already reports
        the child terminal.  Abrupt pre-endpoint termination can leave the
        Engine's startup-lock pathname visible for a short sharing-release
        interval.  The dead child's PID/handle is still the strongest safe key
        for removing *that child's* stale lifetime file.
        """
        with self._startup_process_lock:
            process = self._owned_startup_process
        if process is None:
            return {}
        pid = int(getattr(process, "pid", 0) or 0)
        if pid <= 0:
            return {}
        try:
            terminal = process.poll() is not None
        except (OSError, ValueError):
            terminal = False
        if terminal:
            endpoint = read_endpoint(self.endpoint_file)
            endpoint_owned = bool(endpoint is not None and int(endpoint.pid) == pid)
            lock_owned = self._start_lock_pid() == pid
            if not endpoint_owned and not lock_owned:
                self._clear_owned_startup_process(process)
                return {}
        return {
            "pid": pid,
            "birth_token": _process_birth_token(pid),
            "force_kill_guard": "",
            "source_root": str(self.app_root),
            "ownership": "owned_startup_child",
            "owned_process": process,
        }

    def _force_terminate_owned_startup_process(
        self, process: subprocess.Popen[Any], *, timeout: float
    ) -> bool:
        """Stop the exact locally spawned pre-endpoint Engine using its OS handle.

        Before endpoint publication there is no authenticated IPC command to ask
        for graceful shutdown.  The Manager nevertheless owns the exact Popen
        handle created by this client, which is stronger identity than a bare PID
        or best-effort creation timestamp on Windows.  Use that handle first, then
        perform the normal lifetime-file cleanup proof.
        """
        pid = int(getattr(process, "pid", 0) or 0)
        if pid <= 0:
            self._clear_owned_startup_process(process)
            return not (self.endpoint_file.exists() or self.start_lock_file.exists())
        deadline = time.monotonic() + max(0.5, float(timeout))
        try:
            if process.poll() is None:
                process.terminate()
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                # Extremely defensive fallback: if the exact handle did not
                # terminate promptly, use the existing bounded tree cleanup for
                # any descendants that managed to appear during the race.
                remaining = max(0.0, deadline - time.monotonic())
                if remaining > 0.0:
                    self._force_terminate_engine_tree(pid, timeout=remaining)
        except (OSError, ValueError) as exc:
            self.last_shutdown_detail = (
                f"Exact pre-endpoint Engine cleanup could not complete: {type(exc).__name__}: {exc}"
            )
        stopped = not _pid_alive(pid)
        if stopped:
            cleanup_remaining = max(0.0, deadline - time.monotonic())
            stopped = self._cleanup_stale_lifetime_files(pid, timeout=cleanup_remaining)
        if stopped:
            self._clear_owned_startup_process(process)
            self.last_shutdown_forced = True
            self.last_shutdown_detail = (
                "The pre-endpoint Library Engine startup process was stopped through the exact Manager-owned "
                "process handle before authenticated IPC became available."
            )
        return stopped

    def _runtime_preflight(self, executable: str, *, timeout: float = 12.0) -> None:
        """Verify the child interpreter can import the packaged Engine before detaching it.

        A GUI process and a detached ``python.exe`` can resolve different working
        directories/environments on Windows.  This cheap cold-start gate catches
        missing modules/dependencies with a concrete stderr message instead of a
        generic 30-second ``Engine did not start`` timeout.
        """
        command = [
            executable, "-c",
            "import library_engine.daemon, library_engine.service, midi_library_scan.database; print('engine-preflight-ok')",
        ]
        kwargs: dict[str, Any] = {
            "cwd": str(self.app_root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "close_fds": True,
            "timeout": max(2.0, float(timeout)),
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(command, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LibraryEngineUnavailable(
                f"Library Engine runtime preflight could not run {executable}: {type(exc).__name__}: {exc}"
            ) from exc
        if int(completed.returncode or 0) != 0:
            detail = str(completed.stderr or completed.stdout or "").strip()[-4000:]
            raise LibraryEngineUnavailable(
                "Library Engine child-Python preflight failed before startup"
                + (f": {detail}" if detail else f" with exit code {completed.returncode}")
            )

    def ensure_running(
        self, *, timeout: float = 12.0, require_service_ready: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Attach to or launch the detached Engine, with optional startup cancellation.

        Manager Close/Exit may arrive while child-Python preflight or detached
        Engine startup is still in progress.  A cancellation request must make
        it impossible for this call to launch an Engine *after* the Manager has
        already accepted full exit.  If cancellation races with Popen, the exact
        child PID created by this call is terminated before cancellation returns.
        """
        def startup_cancelled() -> bool:
            return bool(cancel_event is not None and cancel_event.is_set())

        def raise_if_cancelled(process: subprocess.Popen[Any] | None = None) -> None:
            if not startup_cancelled():
                return
            if process is not None and process.poll() is None:
                # This PID was launched by this exact ensure_running call, so it
                # is safe to reap even before the Engine publishes an endpoint.
                # Prefer the exact Popen/OS handle over PID-only termination so
                # Windows startup cancellation cannot lose identity in the tiny
                # pre-endpoint interval.
                self._force_terminate_owned_startup_process(process, timeout=2.0)
            raise LibraryEngineUnavailable("Library Engine startup was cancelled by full exit.")

        raise_if_cancelled()
        existing = self.ping(timeout=0.35)
        if existing is not None:
            restart_reason = ""
            try:
                self._validate_running_engine(existing)
            except LibraryEngineUnavailable as exc:
                # Same protocol but an older API/capability set: a newly installed
                # Manager or Remixer may safely replace the detached Engine with
                # the compatible Engine bundled beside it.  This is what makes
                # independently versioned components practical for normal users.
                restart_reason = str(exc)
            running_version = _version_key(existing.get("engine_version"))
            bundled_version = _version_key(ENGINE_PRODUCT_VERSION)
            if not restart_reason and running_version < bundled_version:
                restart_reason = (
                    f"A newer compatible Library Engine {ENGINE_PRODUCT_VERSION} is bundled with this component "
                    f"(running {existing.get('engine_version') or 'unknown'})."
                )
            if not restart_reason and self._running_source_is_available(existing):
                # Never downgrade a newer compatible detached Engine merely
                # because an older independently installed client launches.
                # Older compatible Engines do not publish service_ready; absence
                # therefore means command-ready for backward compatibility.
                if not require_service_ready or bool(existing.get("service_ready", True)):
                    return existing
                ready_deadline = time.monotonic() + max(1.0, float(timeout))
                last = existing
                while time.monotonic() < ready_deadline:
                    raise_if_cancelled()
                    response = self.ping(timeout=0.5)
                    if response is not None:
                        self._validate_running_engine(response)
                        last = response
                        if bool(response.get("service_ready", True)):
                            return response
                    time.sleep(0.1)
                raise LibraryEngineUnavailable(
                    f"MIDI Library Engine PID {int(last.get('pid') or 0)} is alive, but its persistent library "
                    f"service did not become command-ready within {timeout:g} seconds."
                )
            if not restart_reason:
                restart_reason = "The previous Engine code folder is no longer available."
            # The common unzip-upgrade workflow can delete the old code folder
            # while its detached Engine is still alive. Restart from this install
            # before a future deep-worker restart discovers that its modules vanished.
            try:
                self._request({"command": "shutdown"}, timeout=1.5)
            except (LibraryEngineUnavailable, RuntimeError):
                pass
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and self.ping(timeout=0.2) is not None:
                time.sleep(0.1)
        live_pid = self._live_engine_pid_without_response()
        if live_pid is not None:
            # The lifetime PID lock is authoritative. Do not delete the endpoint
            # or launch a competing writer merely because a live Engine missed a
            # ping. Give startup/transient load a short recovery window, then
            # surface the condition instead of risking library corruption.
            recovery_deadline = time.monotonic() + max(1.5, min(45.0, float(timeout)))
            while time.monotonic() < recovery_deadline:
                raise_if_cancelled()
                response = self.ping(timeout=0.35)
                if response is not None:
                    self._validate_running_engine(response)
                    if not require_service_ready or bool(response.get("service_ready", True)):
                        return response
                # Shutdown deliberately removes the endpoint before another
                # Engine is allowed to acquire the database writer lock. Re-read
                # both files instead of raising based on the stale PID captured
                # before this recovery window began.
                current_live_pid = self._live_engine_pid_without_response()
                if current_live_pid is None:
                    live_pid = None
                    break
                live_pid = current_live_pid
                time.sleep(0.1)
            if live_pid is not None:
                raise LibraryEngineUnavailable(
                    f"MIDI Library Engine PID {live_pid} is still running but is not responding. "
                    "A second Engine was not started because the library permits only one writer. "
                    "Wait briefly or stop the existing Engine before retrying."
                )
        try:
            self.endpoint_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise_if_cancelled()
        child_python = console_python_executable()
        self._runtime_preflight(child_python, timeout=min(12.0, max(4.0, float(timeout))))
        raise_if_cancelled()
        command = [child_python, "-u", "-m", "library_engine.daemon", "--app-root", str(self.app_root)]
        startup_error_file = self.paths.logs / "library-engine-startup-error.json"
        launch_log_file = self.paths.logs / "library-engine-launch.log"
        try:
            startup_error_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            launch_log_file.parent.mkdir(parents=True, exist_ok=True)
            launch_log = launch_log_file.open("a", encoding="utf-8", buffering=1)
            launch_log.write(
                f"\n--- Engine launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
                f"app_root={self.app_root}\npython={command[0]}\n"
            )
        except OSError:
            launch_log = None
        kwargs: dict[str, Any] = {
            "cwd": str(self.app_root),
            "stdin": subprocess.DEVNULL,
            "stdout": launch_log if launch_log is not None else subprocess.DEVNULL,
            "stderr": launch_log if launch_log is not None else subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            # Windows processes are not automatically terminated when their
            # parent GUI exits. CREATE_NO_WINDOW keeps the console worker
            # invisible; CREATE_NEW_PROCESS_GROUP gives it an independent
            # control group. DETACHED_PROCESS was previously combined with
            # CREATE_NO_WINDOW even though Windows documents the latter as
            # ignored for detached processes, making startup behavior harder to
            # reason about across Python/Windows versions.
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
            self._remember_owned_startup_process(process)
        except OSError as exc:
            if launch_log is not None:
                launch_log.close()
            raise LibraryEngineUnavailable(
                f"Could not launch MIDI Library Engine with {command[0]}: {exc}"
            ) from exc
        finally:
            # Popen duplicates/owns the child handles. Closing the Manager's
            # copy prevents a long-running Engine from keeping this descriptor
            # unnecessarily open in the GUI process.
            if launch_log is not None and 'process' in locals():
                launch_log.close()
        raise_if_cancelled(process)
        if os.name != "nt":
            # The Engine is intentionally detached from either GUI, but while
            # this client process remains alive a stopped child would otherwise
            # linger as a POSIX zombie until some later PID probe happens to reap
            # it. A daemon waiter owns only reaping, never Engine lifetime.
            threading.Thread(
                target=process.wait, name=f"LibraryEngineReaper-{process.pid}", daemon=True
            ).start()
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            raise_if_cancelled(process)
            response = self.ping(timeout=0.5)
            if response is not None:
                self._validate_running_engine(response)
                if not require_service_ready or bool(response.get("service_ready", True)):
                    self._clear_owned_startup_process(process)
                    return response
            return_code = process.poll()
            if return_code is not None:
                diagnostic = _engine_startup_diagnostic(self.paths)
                detail = f" Engine diagnostic: {diagnostic}" if diagnostic else ""
                raise LibraryEngineUnavailable(
                    f"MIDI Library Engine exited during startup with code {return_code}.{detail} "
                    f"Engine source: {self.app_root}. Python: {child_python}. Launch log: {launch_log_file}."
                )
            time.sleep(0.15)
        live = self.ping(timeout=0.5)
        if live is not None and require_service_ready and not bool(live.get("service_ready", True)):
            self._clear_owned_startup_process(process)
            raise LibraryEngineUnavailable(
                f"MIDI Library Engine PID {int(live.get('pid') or 0)} is alive, but its persistent library "
                f"service did not become command-ready within {timeout:g} seconds."
            )
        diagnostic = _engine_startup_diagnostic(self.paths)
        detail = f" Engine diagnostic: {diagnostic}" if diagnostic else ""
        raise LibraryEngineUnavailable(
            f"MIDI Library Engine did not start within {timeout:g} seconds.{detail} "
            f"Engine source: {self.app_root}. Python: {child_python}. Launch log: {launch_log_file}."
        )

    def command(
        self, command: str, *, timeout: float = 3.0, start_if_needed: bool = True, **payload: Any
    ) -> dict[str, Any]:
        request = {"command": command, **payload}
        # The common path is one socket round-trip.  Do not add a ping before
        # every status/recommendation request; at DJ-interaction rates that is
        # pure latency and doubles local IPC.  Start/recover the Engine only if
        # the actual command cannot reach it.
        try:
            return self._request(request, timeout=timeout)
        except LibraryEngineUnavailable:
            if not start_if_needed:
                raise
            self.ensure_running()
            return self._request(request, timeout=timeout)

    def status(self, *, start_if_needed: bool = True, timeout: float = 5.0) -> dict[str, Any]:
        # Status is designed to be lightweight/cached in the Engine.  Keep a
        # slightly wider IPC window than ping so a brief SQLite commit never
        # masquerades as a dead Engine on a large active library.
        return dict(self.command("status", timeout=max(0.5, float(timeout)), start_if_needed=start_if_needed).get("status") or {})


    def _cleanup_stale_lifetime_files(self, pid: int, *, timeout: float = 0.0) -> bool:
        """Remove target-owned lifetime files only after its process is proven dead.

        Windows may report a terminated process before the final file-handle release
        is observable by another process.  Full-exit therefore retries boundedly and
        does not translate a failed unlink into a false clean-stop result.
        
        Return True only when no remaining endpoint/start-lock file still belongs to
        ``pid``. Files owned by a different process are never removed.
        """
        target = int(pid or 0)
        if target <= 0 or _pid_alive(target):
            return False
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            target_file_remains = False

            endpoint = read_endpoint(self.endpoint_file)
            if endpoint is not None and int(endpoint.pid) == target:
                try:
                    self.endpoint_file.unlink(missing_ok=True)
                except OSError:
                    target_file_remains = True

            try:
                locked_pid = int(self.start_lock_file.read_text(encoding="ascii").strip() or "0")
            except FileNotFoundError:
                locked_pid = 0
            except (OSError, ValueError):
                # A transient sharing violation while the dead Engine's final
                # handle is being released is still an unreleased lifetime state.
                locked_pid = target if self.start_lock_file.exists() else 0
            if locked_pid == target:
                try:
                    self.start_lock_file.unlink(missing_ok=True)
                except OSError:
                    target_file_remains = True

            # Re-read after attempted cleanup. A path can disappear between the
            # failed/read branches above, so filesystem state is authoritative.
            endpoint = read_endpoint(self.endpoint_file)
            endpoint_exists = self.endpoint_file.exists()
            endpoint_target_remains = bool(endpoint is not None and int(endpoint.pid) == target)
            endpoint_other_or_unknown_remains = bool(
                endpoint_exists and (endpoint is None or int(endpoint.pid) != target)
            )
            try:
                locked_pid = int(self.start_lock_file.read_text(encoding="ascii").strip() or "0")
            except FileNotFoundError:
                locked_pid = 0
            except (OSError, ValueError):
                locked_pid = target if self.start_lock_file.exists() else 0
            lock_exists = self.start_lock_file.exists()
            lock_target_remains = locked_pid == target
            lock_other_or_unknown_remains = bool(lock_exists and locked_pid != target)

            if not (
                target_file_remains
                or endpoint_target_remains
                or endpoint_other_or_unknown_remains
                or lock_target_remains
                or lock_other_or_unknown_remains
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _force_terminate_engine_tree(self, pid: int, *, timeout: float) -> bool:
        """Last-resort full-exit escalation after graceful Engine shutdown expires.

        The Engine is deliberately detached, so Windows will not reap it when the
        Manager closes.  If orderly shutdown misses its advertised deadline, the
        application must perform the same process-tree termination the user would
        otherwise have to do in Task Manager.  This is used only after the clean
        transactional shutdown window has expired.
        """
        target = int(pid or 0)
        if target <= 0:
            return not (self.endpoint_file.exists() or self.start_lock_file.exists())
        if not _pid_alive(target):
            return self._cleanup_stale_lifetime_files(
                target, timeout=min(0.5, max(0.0, float(timeout)))
            )
        deadline = time.monotonic() + max(0.5, float(timeout))
        try:
            if os.name == "nt":
                # Do not depend on taskkill.exe for the terminal safety path.
                # A shell helper can itself stall; Win32 process handles are
                # bounded and let us terminate the captured tree directly.
                descendants = _windows_descendant_pids(target)
                _windows_terminate_pid(target)
                for child_pid in sorted(descendants, reverse=True):
                    _windows_terminate_pid(child_pid)
            else:
                # Engine is launched in a new session on POSIX, so its PID is
                # also the process-group id and includes analyzer descendants.
                try:
                    os.killpg(target, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(target, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                soft_deadline = min(deadline, time.monotonic() + 0.75)
                while _pid_alive(target) and time.monotonic() < soft_deadline:
                    time.sleep(0.05)
                if _pid_alive(target):
                    try:
                        os.killpg(target, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        try:
                            os.kill(target, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.last_shutdown_detail = f"Forced Engine cleanup could not complete: {type(exc).__name__}: {exc}"

        while _pid_alive(target) and time.monotonic() < deadline:
            time.sleep(0.05)
        stopped = not _pid_alive(target)
        if stopped:
            cleanup_remaining = max(0.0, deadline - time.monotonic())
            return self._cleanup_stale_lifetime_files(target, timeout=cleanup_remaining)
        return False

    def capture_shutdown_target(self) -> dict[str, Any]:
        """Capture immutable process identity for one explicit user shutdown.

        The Manager keeps this ticket outside the normal shutdown worker so a
        UI-level watchdog can still terminate the exact owned Engine if the
        graceful control thread itself becomes stuck in an OS/runtime call.
        A dead PID still named by the startup lock is retained as a cleanup-only
        target; ``force_stop_captured_target`` rechecks liveness before deleting
        any target-owned lifetime file, so PID reuse remains fail-closed.
        """
        endpoint = read_endpoint(self.endpoint_file)
        if endpoint is None:
            owned = self._owned_startup_target()
            if owned:
                return owned
        if endpoint is not None:
            pid = int(endpoint.pid)
            ownership = "endpoint"
        else:
            live_pid = int(self._live_engine_pid_without_response() or 0)
            locked_pid = self._start_lock_pid()
            pid = live_pid or locked_pid
            ownership = "lifetime_lock" if live_pid > 0 else ("stale_lifetime_lock" if locked_pid > 0 else "none")
        return {
            "pid": pid,
            "birth_token": _process_birth_token(pid),
            "force_kill_guard": str(endpoint.force_kill_guard or "") if endpoint is not None else "",
            "source_root": str(endpoint.source_root or "") if endpoint is not None else "",
            "ownership": ownership,
        }

    def force_stop_captured_target(self, target: dict[str, Any], *, timeout: float = 3.0) -> bool:
        """Bounded emergency cleanup for a previously captured owned Engine.

        This is intentionally independent of the graceful shutdown request. It
        exists for the failure mode where the shutdown worker itself stops
        making progress. PID reuse is rejected through the captured process
        birth token; when the endpoint still exists its per-instance guard must
        also continue to match.
        """
        pid = int((target or {}).get("pid") or 0)
        owned_process = (target or {}).get("owned_process")
        if (
            str((target or {}).get("ownership") or "") == "owned_startup_child"
            and owned_process is not None
            and int(getattr(owned_process, "pid", 0) or 0) == pid
        ):
            return self._force_terminate_owned_startup_process(
                owned_process, timeout=max(0.5, float(timeout))
            )
        if pid <= 0:
            return (
                self._live_engine_pid_without_response() is None
                and not self.endpoint_file.exists()
                and not self.start_lock_file.exists()
            )
        if not _pid_alive(pid):
            return self._cleanup_stale_lifetime_files(pid, timeout=max(0.0, float(timeout)))
        birth = str((target or {}).get("birth_token") or "")
        if not birth or _process_birth_token(pid) != birth:
            self.last_shutdown_detail = "Emergency cleanup refused because Engine process identity changed."
            return False
        captured_guard = str((target or {}).get("force_kill_guard") or "")
        endpoint = read_endpoint(self.endpoint_file)
        if endpoint is not None and int(endpoint.pid) == pid and captured_guard:
            if str(endpoint.force_kill_guard or "") != captured_guard:
                self.last_shutdown_detail = "Emergency cleanup refused because Engine ownership guard changed."
                return False
        stopped = self._force_terminate_engine_tree(pid, timeout=max(0.5, float(timeout)))
        if stopped:
            self.last_shutdown_forced = True
            self.last_shutdown_detail = (
                "The Library Engine exceeded the Manager shutdown deadline and the exact owned process tree was force-stopped. "
                "Committed Library work is preserved; the next launch will verify the databases."
            )
        return stopped

    def wait_until_stopped(self, *, timeout: float = 20.0, pid: int | None = None) -> bool:
        """Wait until the captured Engine PID *and its lifetime files* are gone.

        A dead PID alone is not a clean-stop proof on Windows. The terminated
        process can become non-running just before another process can delete its
        startup lock.  This method therefore performs bounded target-safe cleanup
        and returns success only after the target PID is terminal and no endpoint
        or startup-lock file still names that target.
        """
        target_pid = int(pid or 0)
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            process_exited = target_pid <= 0 or not _pid_alive(target_pid)
            if process_exited:
                remaining = max(0.0, deadline - time.monotonic())
                if target_pid <= 0:
                    # With no captured PID, preserve the conservative historical
                    # rule: success requires no live Engine discoverable through
                    # either lifetime surface.
                    if (
                        self._live_engine_pid_without_response() is None
                        and not self.endpoint_file.exists()
                        and not self.start_lock_file.exists()
                    ):
                        return True
                elif self._cleanup_stale_lifetime_files(
                    target_pid, timeout=min(0.25, remaining)
                ):
                    return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        if target_pid <= 0:
            return (
                self._live_engine_pid_without_response() is None
                and not self.endpoint_file.exists()
                and not self.start_lock_file.exists()
            )
        return (
            not _pid_alive(target_pid)
            and self._cleanup_stale_lifetime_files(target_pid, timeout=0.0)
        )

    def shutdown(
        self, *, request_timeout: float = SHUTDOWN_REQUEST_SECONDS,
        wait_timeout: float = CLIENT_SHUTDOWN_WAIT_SECONDS, force_after_timeout: bool = True,
    ) -> bool:
        """Request clean Engine stop under one inclusive client deadline.

        The advertised client budget includes the shutdown request itself,
        graceful observation, and any force-stop verification. Older code added
        the request timeout *before* the 15-second wait budget, so the real user
        wait could already exceed the documented contract even without a bug.
        """
        self.last_shutdown_forced = False
        self.last_shutdown_detail = ""
        total_budget = max(0.5, float(wait_timeout))
        deadline = time.monotonic() + total_budget
        captured = self.capture_shutdown_target()
        target_pid = int(captured.get("pid") or 0)
        # Pre-endpoint startup has no authenticated command channel to drain.
        # If this exact client owns the Popen handle, waiting through the normal
        # 12-15 second graceful IPC budget cannot improve safety and can make a
        # Windows Manager look hung.  Stop that exact child immediately and prove
        # its lifetime files gone instead.
        if str(captured.get("ownership") or "") == "owned_startup_child":
            return self.force_stop_captured_target(
                captured, timeout=max(0.5, min(total_budget, 3.0))
            )
        force_guard = str(captured.get("force_kill_guard") or "")
        target_birth_token = str(captured.get("birth_token") or "")
        if target_pid <= 0 and self.ping(timeout=min(0.2, max(0.05, deadline - time.monotonic()))) is None:
            return not (self.endpoint_file.exists() or self.start_lock_file.exists())

        shutdown_acknowledged = False
        remaining = max(0.05, deadline - time.monotonic())
        try:
            self.command(
                "shutdown", timeout=min(max(0.05, float(request_timeout)), remaining),
                start_if_needed=False,
            )
            shutdown_acknowledged = True
        except LibraryEngineUnavailable:
            pass

        remaining = max(0.0, deadline - time.monotonic())
        # Preserve a bounded slice of the *same* absolute deadline for force
        # verification. Otherwise wait_until_stopped can consume the entire
        # client budget and leave no time to perform the recovery the contract
        # promises. On the normal 15s path this still allows the Engine nearly
        # its full 12s orderly shutdown boundary.
        force_reserve = min(2.0, max(0.25, remaining * 0.35)) if force_after_timeout else 0.0
        graceful_budget = min(max(0.0, remaining - force_reserve), ENGINE_SHUTDOWN_HARD_SECONDS + 0.25)
        if graceful_budget > 0 and self.wait_until_stopped(timeout=graceful_budget, pid=target_pid):
            return True
        if target_pid <= 0:
            return (
                self._live_engine_pid_without_response() is None
                and not self.endpoint_file.exists()
                and not self.start_lock_file.exists()
            )
        if not _pid_alive(target_pid):
            cleanup_remaining = max(0.0, deadline - time.monotonic())
            cleaned = self._cleanup_stale_lifetime_files(target_pid, timeout=cleanup_remaining)
            if not cleaned:
                self.last_shutdown_detail = (
                    f"Library Engine PID {target_pid} exited, but its lifetime files were not released "
                    "within the shutdown deadline."
                )
            return cleaned

        current_endpoint = read_endpoint(self.endpoint_file)
        endpoint_identity_valid = bool(
            current_endpoint is not None
            and int(current_endpoint.pid) == target_pid
            and force_guard
            and str(current_endpoint.force_kill_guard or "") == force_guard
        )
        birth_identity_valid = bool(
            target_birth_token and _process_birth_token(target_pid) == target_birth_token
        )
        force_identity_valid = bool(
            force_guard and shutdown_acknowledged and (endpoint_identity_valid or birth_identity_valid)
        )
        remaining = max(0.0, deadline - time.monotonic())
        if not force_after_timeout or not force_identity_valid or remaining <= 0:
            self.last_shutdown_detail = (
                f"Library Engine PID {target_pid or 'unknown'} exceeded the shutdown deadline. "
                "Graceful cleanup did not complete within the one shared client budget."
            )
            return False

        self.last_shutdown_forced = True
        stopped = self._force_terminate_engine_tree(target_pid, timeout=max(0.25, remaining))
        if stopped:
            self.last_shutdown_detail = (
                "The Library Engine exceeded its graceful shutdown window and was force-stopped within the shared client deadline. "
                "Committed Library work is preserved; the next launch will verify the databases."
            )
        elif not self.last_shutdown_detail:
            self.last_shutdown_detail = (
                f"Library Engine PID {target_pid} survived both graceful and forced shutdown attempts."
            )
        return stopped
