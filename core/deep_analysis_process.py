from __future__ import annotations

"""Owned persistent subprocess boundary for CPU-heavy detailed MIDI analysis."""

from collections import deque
import json
from pathlib import Path
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

from core.resource_safety import DEFAULT_MIDI_LIMITS
from core.python_runtime import console_python_executable
from core.runtime_cleanup import runtime_supervisor
from midi_library_scan.models import QuickScanResult


class DeepAnalysisProcessError(RuntimeError):
    pass


class DeepAnalysisProcessCancelled(DeepAnalysisProcessError):
    """An Engine-owned analysis request was intentionally interrupted for shutdown.

    This is not a MIDI failure. Callers use the marker to return persistent
    queue/catalogue state to a retryable paused/cancelled boundary instead of
    consuming one of the user's failure-recovery attempts.
    """

    analysis_cancelled = True


class DeepAnalysisProcessRunner:
    """Reuse one supervised worker process; restart it after a timeout/crash."""

    def __init__(
        self, app_root: str | Path, library_index_path: str | Path, quick_database_path: str | Path,
        *, process_priority: str = "background",
    ) -> None:
        self.app_root = Path(app_root).resolve()
        self.library_index_path = Path(library_index_path)
        self.quick_database_path = Path(quick_database_path)
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, object]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._request_lock = threading.Lock()
        # Engine shutdown must be able to interrupt a worker request that is
        # blocked waiting for a pathological MIDI.  A plain request lock alone
        # made stop() wait behind the full analysis safety timeout.
        self._cancel_request = threading.Event()
        # One-way Engine-lifetime shutdown barrier. Unlike _cancel_request this
        # is never cleared by a later borrowed request, so a runner borrowed
        # just before Engine Close cannot create a fresh analyzer after shutdown
        # ownership has already begun.
        self._shutdown_requested = threading.Event()
        self.process_priority = str(process_priority or "background").casefold()

    @property
    def active_process(self) -> subprocess.Popen[str] | None:
        with self._lock:
            return self._process

    @property
    def request_active(self) -> bool:
        """Whether this runner is currently owned by a Fast/Deep request."""
        return self._request_lock.locked()

    def _worker_command(self) -> list[str]:
        return [
            console_python_executable(), "-u", "-m", "midi_library_scan.deep_worker", "--serve",
            "--library-index", str(self.library_index_path),
            "--quick-database", str(self.quick_database_path),
        ]

    def _reader(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    self._responses.put(payload)
        except (OSError, ValueError):
            # Lifecycle cleanup may close the local pipe after a child has died.
            # That is terminal cleanup, not a second worker failure.
            return

    def _stderr_reader(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        try:
            for line in process.stderr:
                self._stderr_tail.append(line.rstrip())
        except (OSError, ValueError):
            return

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self._shutdown_requested.is_set():
            raise DeepAnalysisProcessCancelled("MIDI analysis worker creation was blocked by Library Engine shutdown.")
        with self._lock:
            if self._shutdown_requested.is_set():
                raise DeepAnalysisProcessCancelled("MIDI analysis worker creation was blocked by Library Engine shutdown.")
            process = self._process
            if process is not None and process.poll() is None:
                return process
            if process is not None:
                runtime_supervisor.unregister_process(process)
                self._process = None
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if self.process_priority != "normal":
                    creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            process = subprocess.Popen(
                self._worker_command(), cwd=str(self.app_root), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1, creationflags=creationflags,
            )
            self._process = process
            self._stderr_tail.clear()
            while not self._responses.empty():
                try:
                    self._responses.get_nowait()
                except queue.Empty:
                    break
            runtime_supervisor.register_process(process, "Persistent detailed MIDI analysis worker")
            self._reader_thread = threading.Thread(
                target=self._reader, args=(process,), name="MidiDeepWorkerStdout", daemon=True
            )
            self._stderr_thread = threading.Thread(
                target=self._stderr_reader, args=(process,), name="MidiDeepWorkerStderr", daemon=True
            )
            self._reader_thread.start()
            self._stderr_thread.start()
            return process

    def _join_io_threads(self, timeout: float = 2.0) -> tuple[str, ...]:
        """Join both pipe readers inside one caller-owned total budget."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        current = threading.current_thread()
        threads = (self._reader_thread, self._stderr_thread)
        for thread in threads:
            if thread is None or thread is current or not thread.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        alive = tuple(
            thread.name for thread in threads
            if thread is not None and thread is not current and thread.is_alive()
        )
        if self._reader_thread is not None and not self._reader_thread.is_alive():
            self._reader_thread = None
        if self._stderr_thread is not None and not self._stderr_thread.is_alive():
            self._stderr_thread = None
        return alive

    @staticmethod
    def _close_terminal_stdin(process: subprocess.Popen[str]) -> None:
        """Close only the request-owned stdin wrapper after child termination.

        stdout/stderr belong to daemon reader threads and are deliberately not
        closed cross-thread. stdin has no reader thread; leaving it for GC can
        make TextIOWrapper.__del__ flush against a dead child and emit an
        unraisable BrokenPipeError during otherwise clean lifecycle tests.
        """
        if process.poll() is None or process.stdin is None:
            return
        close = getattr(process.stdin, "close", None)
        if not callable(close):
            return
        try:
            close()
        except (BrokenPipeError, OSError, ValueError):
            return

    def _discard_process(self, process: subprocess.Popen[str], *, terminate: bool) -> None:
        if terminate and process.poll() is None:
            runtime_supervisor.terminate_process(process, timeout=1.5)
        if process.poll() is not None:
            runtime_supervisor.unregister_process(process)
            self._close_terminal_stdin(process)
        # A dead worker must be reported promptly. The old default two-second
        # pipe-thread join could turn an immediate child crash into a >2s
        # apparent hang under load. Give normal EOF a short chance first, then
        # close the local pipe handles to wake any reader still blocked on I/O.
        alive = self._join_io_threads(timeout=0.35)
        if alive and process.poll() is not None:
            # Never close a TextIOWrapper from a different thread while a pipe
            # reader may be blocked inside it. CPython's buffered/text stream
            # close can wait on the reader's internal I/O lock and turn an
            # already-dead analyzer into an unbounded parent-thread hang. The
            # readers are daemon lifecycle observers and the child is already
            # terminal, so abandoning a late EOF reader is safer than blocking
            # the Engine control plane. It will naturally retire when EOF is
            # delivered or when the process object/OS tears the pipe down.
            pass
        with self._lock:
            if self._process is process:
                self._process = None

    def quick_scan(self, path: str | Path) -> QuickScanResult:
        """Run fast MIDI parsing in the same supervised persistent worker.

        Initial six-figure catalogues are CPU-heavy enough to benefit from a few
        isolated worker processes. Reusing the deep-worker process pool avoids
        creating an unmanaged second process tree just for quick scanning.
        """
        source = Path(path).resolve()
        timeout = DEFAULT_MIDI_LIMITS.quick_processing_seconds + 10.0
        with self._request_lock:
            if self._shutdown_requested.is_set():
                raise DeepAnalysisProcessCancelled(
                    f"Quick scan of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                )
            self._cancel_request.clear()
            if self._shutdown_requested.is_set():
                self._cancel_request.set()
                raise DeepAnalysisProcessCancelled(
                    f"Quick scan of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                )
            process = self._ensure_process()
            if process.stdin is None:
                self._discard_process(process, terminate=True)
                raise DeepAnalysisProcessError("MIDI analysis worker stdin is unavailable.")
            request_id = uuid.uuid4().hex
            request = {"id": request_id, "command": "quick_scan", "path": str(source)}
            try:
                process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                shutting_down = self._cancel_request.is_set()
                self._discard_process(process, terminate=True)
                if shutting_down:
                    raise DeepAnalysisProcessCancelled(
                        f"Quick scan of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                    ) from exc
                raise DeepAnalysisProcessError(f"MIDI analysis worker stopped before {source.name} could be scanned.") from exc

            deadline = time.monotonic() + timeout
            while True:
                if self._cancel_request.is_set():
                    self._discard_process(process, terminate=process.poll() is None)
                    raise DeepAnalysisProcessCancelled(
                        f"Quick scan of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                    )
                if process.poll() is not None:
                    detail = "\n".join(self._stderr_tail).strip()
                    code = process.returncode
                    self._discard_process(process, terminate=False)
                    raise DeepAnalysisProcessError(
                        f"MIDI analysis worker exited unexpectedly during Fast scan of {source.name} "
                        f"(exit {code}). {detail[-1000:]}".strip()
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._discard_process(process, terminate=True)
                    raise DeepAnalysisProcessError(
                        f"Quick scan of {source.name} exceeded the "
                        f"{DEFAULT_MIDI_LIMITS.quick_processing_seconds:g}-second safety budget and was stopped."
                    )
                try:
                    # Poll in short slices so an Engine shutdown can preempt a
                    # pathological worker instead of waiting for the full MIDI
                    # processing timeout.
                    response = self._responses.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if str(response.get("id") or "") != request_id:
                    continue
                break
            if not bool(response.get("ok")):
                detail = str(response.get("error") or "MIDI analysis worker reported an unknown error.")
                raise DeepAnalysisProcessError(f"Quick scan failed for {source.name}: {detail}")
            payload = response.get("result")
            if not isinstance(payload, dict):
                raise DeepAnalysisProcessError(f"Quick scan worker returned an invalid result for {source.name}.")
            return QuickScanResult.from_dict(payload)

    def analyze(self, path: str | Path, *, force: bool = False) -> None:
        source = Path(path).resolve()
        timeout = DEFAULT_MIDI_LIMITS.deep_processing_seconds + 15.0
        try:
            qa_timeout = float(os.environ.get("MIDI_LIBRARY_QA_ANALYZER_TIMEOUT_SECONDS", "0") or 0)
        except ValueError:
            qa_timeout = 0.0
        if qa_timeout > 0.0:
            timeout = max(0.5, min(timeout, qa_timeout))
        # The DeepAnalysisQueue calls this serially. The lock also makes direct
        # selected-song requests safe if they race a queue transition.
        with self._request_lock:
            if self._shutdown_requested.is_set():
                raise DeepAnalysisProcessCancelled(
                    f"Detailed analysis of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                )
            self._cancel_request.clear()
            if self._shutdown_requested.is_set():
                self._cancel_request.set()
                raise DeepAnalysisProcessCancelled(
                    f"Detailed analysis of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                )
            process = self._ensure_process()
            if process.stdin is None:
                self._discard_process(process, terminate=True)
                raise DeepAnalysisProcessError("Detailed-analysis worker stdin is unavailable.")
            request_id = uuid.uuid4().hex
            request = {"id": request_id, "path": str(source), "force": bool(force)}
            try:
                process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                shutting_down = self._cancel_request.is_set()
                self._discard_process(process, terminate=True)
                if shutting_down:
                    raise DeepAnalysisProcessCancelled(
                        f"Detailed analysis of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                    ) from exc
                raise DeepAnalysisProcessError(f"Detailed-analysis worker stopped before {source.name} could start.") from exc

            deadline = time.monotonic() + timeout
            while True:
                if self._cancel_request.is_set():
                    self._discard_process(process, terminate=process.poll() is None)
                    raise DeepAnalysisProcessCancelled(
                        f"Detailed analysis of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                    )
                if process.poll() is not None:
                    detail = "\n".join(self._stderr_tail).strip()
                    code = process.returncode
                    self._discard_process(process, terminate=False)
                    raise DeepAnalysisProcessError(
                        f"Detailed-analysis worker exited unexpectedly during {source.name} "
                        f"(exit {code}). {detail[-1000:]}".strip()
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    marker = self.library_index_path.parent / "qa-analyzer-timeout-active.txt"
                    try:
                        if str(os.environ.get("MIDI_LIBRARY_QA_HOLD_ANALYZER_TIMEOUT_SECONDS", "")).strip():
                            marker.parent.mkdir(parents=True, exist_ok=True)
                            marker.write_text(f"pid={os.getpid()} path={source}", encoding="utf-8")
                            try:
                                qa_hold = max(0.0, min(4.0, float(
                                    os.environ.get("MIDI_LIBRARY_QA_HOLD_ANALYZER_TIMEOUT_SECONDS", "0") or 0
                                )))
                            except ValueError:
                                qa_hold = 0.0
                            if qa_hold > 0.0:
                                # QA-only state visibility must never pin shutdown
                                # behind an artificial sleep. Full Engine close
                                # sets this request-cancel event.
                                self._cancel_request.wait(qa_hold)
                    except OSError:
                        pass
                    self._discard_process(process, terminate=True)
                    raise DeepAnalysisProcessError(
                        f"Detailed analysis of {source.name} exceeded the "
                        f"{DEFAULT_MIDI_LIMITS.deep_processing_seconds:g}-second safety budget and was stopped."
                    )
                try:
                    response = self._responses.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if str(response.get("id") or "") != request_id:
                    # A stale terminal message from a worker restart is harmless,
                    # but it must not reset this request's absolute deadline.
                    continue
                break
            if self._cancel_request.is_set():
                self._discard_process(process, terminate=process.poll() is None)
                raise DeepAnalysisProcessCancelled(
                    f"Detailed analysis of {source.name} was interrupted by Library Engine shutdown/lifecycle control."
                )
            if not bool(response.get("ok")):
                detail = str(response.get("error") or "Detailed analysis worker reported an unknown error.")
                raise DeepAnalysisProcessError(f"Detailed analysis failed for {source.name}: {detail}")
            if process.poll() is not None:
                detail = "\n".join(self._stderr_tail)
                self._discard_process(process, terminate=False)
                raise DeepAnalysisProcessError(
                    f"Detailed-analysis worker exited unexpectedly after {source.name}. {detail[-1000:]}"
                )

    def stop(self, timeout: float = 2.5) -> None:
        """Stop one worker inside one strict caller-owned deadline.

        MIDI parse safety limits are deliberately unrelated to lifecycle
        controls.  Pause/Cancel/shutdown may preempt the child, and no local
        minimum wait is allowed to extend the caller's deadline.
        """
        budget = max(0.0, float(timeout))
        if budget <= 0:
            raise DeepAnalysisProcessError("No lifecycle-control budget remained for the MIDI analysis worker.")
        deadline = time.monotonic() + budget
        self._cancel_request.set()

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        acquired = self._request_lock.acquire(blocking=False)
        if not acquired:
            with self._lock:
                active = self._process
            rem = remaining()
            if active is not None and active.poll() is None and rem > 0:
                runtime_supervisor.terminate_process(active, timeout=min(1.25, rem))
            rem = remaining()
            if rem <= 0 or not self._request_lock.acquire(timeout=rem):
                raise DeepAnalysisProcessError(
                    "Detailed-analysis request did not release its ownership lock during Engine lifecycle control."
                )
            acquired = True

        try:
            with self._lock:
                process = self._process
            if process is None:
                return
            if process.poll() is None and process.stdin is not None:
                rem = remaining()
                if rem <= 0:
                    raise DeepAnalysisProcessError("MIDI analysis worker exceeded its lifecycle-control deadline.")
                try:
                    process.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
                    process.stdin.flush()
                    # Keep most of the remaining budget available for hard kill.
                    process.wait(timeout=min(0.35, rem))
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    rem = remaining()
                    if rem <= 0 or not runtime_supervisor.terminate_process(process, timeout=rem):
                        raise DeepAnalysisProcessError(
                            f"Detailed-analysis worker PID {process.pid} did not terminate before its lifecycle deadline."
                        )
            if process.poll() is not None:
                runtime_supervisor.unregister_process(process)
                self._close_terminal_stdin(process)
            rem = remaining()
            alive = self._join_io_threads(timeout=rem) if rem > 0 else tuple(
                thread.name for thread in (self._reader_thread, self._stderr_thread)
                if thread is not None and thread.is_alive()
            )
            if alive:
                raise DeepAnalysisProcessError("MIDI analysis pipe reader(s) did not stop: " + ", ".join(alive))
            with self._lock:
                if self._process is process:
                    self._process = None
        finally:
            if acquired:
                self._request_lock.release()

    def request_shutdown(self) -> None:
        """Publish a one-way no-new-process barrier for Engine lifetime shutdown."""
        self._shutdown_requested.set()
        self._cancel_request.set()


    def snapshot(self) -> dict[str, object]:
        process = self.active_process
        return {
            "active": bool(process is not None and process.poll() is None),
            "pid": process.pid if process is not None and process.poll() is None else None,
            "persistent_worker": True,
            "timeout_seconds": DEFAULT_MIDI_LIMITS.deep_processing_seconds,
        }


class DeepAnalysisProcessPool:
    """Small pool of isolated persistent analyzers for Library Engine throughput.

    Each runner owns one child Python process and serializes its own requests.
    The pool permits independent MIDI files to be analyzed on separate CPU cores
    while SQLite WAL serializes the short commit phases safely.
    """

    def __init__(
        self,
        app_root: str | Path,
        library_index_path: str | Path,
        quick_database_path: str | Path,
        *,
        workers: int = 1,
        process_priority: str = "background",
    ) -> None:
        self.worker_total = max(1, min(8, int(workers)))
        self._available: queue.Queue[DeepAnalysisProcessRunner] = queue.Queue()
        self._runners: list[DeepAnalysisProcessRunner] = []
        for _index in range(self.worker_total):
            runner = DeepAnalysisProcessRunner(
                app_root, library_index_path, quick_database_path, process_priority=process_priority
            )
            self._runners.append(runner)
            self._available.put(runner)
        self._closed = False
        self._stopping = False
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()

    def _borrow_runner(self) -> DeepAnalysisProcessRunner:
        """Borrow a runner without allowing shutdown to strand a waiter."""
        while True:
            with self._lock:
                if self._closed or self._stopping:
                    raise DeepAnalysisProcessCancelled("MIDI analysis process pool is shutting down.")
            try:
                return self._available.get(timeout=0.10)
            except queue.Empty:
                continue

    def analyze(self, path: str | Path, *, force: bool = False) -> None:
        runner = self._borrow_runner()
        try:
            runner.analyze(path, force=force)
        finally:
            with self._lock:
                if not self._closed:
                    self._available.put(runner)

    def quick_scan(self, path: str | Path) -> QuickScanResult:
        runner = self._borrow_runner()
        try:
            return runner.quick_scan(path)
        finally:
            with self._lock:
                if not self._closed:
                    self._available.put(runner)

    def retire_idle_processes(self, timeout: float = 1.0) -> int:
        """Stop currently idle worker subprocesses without closing the pool.

        Runners remain reusable and will lazily start a fresh child on the next
        Fast/Deep request. Only runners presently in the availability queue are
        touched, so a worker borrowed by an active analysis can never be killed
        by the idle reaper.
        """
        with self._lock:
            if self._closed or self._stopping:
                return 0
        available: list[DeepAnalysisProcessRunner] = []
        while True:
            try:
                available.append(self._available.get_nowait())
            except queue.Empty:
                break
        retired = 0
        try:
            for runner in available:
                process = runner.active_process
                if process is None or process.poll() is not None:
                    continue
                runner.stop(timeout=max(0.1, float(timeout)))
                retired += 1
        finally:
            with self._lock:
                if not self._closed:
                    for runner in available:
                        self._available.put(runner)
        return retired

    def preempt_active_requests(self, timeout: float = 3.0) -> int:
        """Interrupt active requests concurrently under one absolute deadline."""
        with self._stop_lock:
            with self._lock:
                if self._closed or self._stopping:
                    return 0
                runners = [runner for runner in self._runners if runner.request_active]
            active_before = len(runners)
            if not runners:
                return 0
            budget = max(0.0, float(timeout))
            if budget <= 0:
                raise DeepAnalysisProcessError("No lifecycle-control budget remained to preempt MIDI workers.")
            deadline = time.monotonic() + budget
            errors: list[str] = []
            error_lock = threading.Lock()

            def interrupt(runner: DeepAnalysisProcessRunner) -> None:
                rem = max(0.0, deadline - time.monotonic())
                try:
                    runner.stop(timeout=rem)
                except (DeepAnalysisProcessError, OSError, RuntimeError) as exc:
                    with error_lock:
                        errors.append(f"{type(exc).__name__}: {exc}")

            control_threads = [
                threading.Thread(
                    target=interrupt, args=(runner,), name=f"MidiAnalyzerPreempt-{index + 1}", daemon=True
                )
                for index, runner in enumerate(runners)
            ]
            for thread in control_threads:
                thread.start()
            for thread in control_threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            pending = [thread for thread in control_threads if thread.is_alive()]
            if pending:
                errors.append(f"{len(pending)} MIDI analysis worker control task(s) exceeded the shared deadline")
            if errors:
                raise DeepAnalysisProcessError(
                    "One or more MIDI analysis workers did not yield cleanly: " + "; ".join(errors)
                )
            return active_before

    def request_shutdown(self) -> None:
        """Block new borrows/spawns immediately, before bounded stop waits begin."""
        with self._lock:
            if self._closed:
                return
            self._stopping = True
            runners = list(self._runners)
        for runner in runners:
            runner.request_shutdown()


    def stop(self, timeout: float = 3.0) -> None:
        """Stop all analyzers concurrently inside one absolute deadline."""
        with self._stop_lock:
            self.request_shutdown()
            with self._lock:
                if self._closed:
                    return
                runners = list(self._runners)
            budget = max(0.0, float(timeout))
            if budget <= 0:
                with self._lock:
                    self._stopping = False
                raise DeepAnalysisProcessError("No shutdown budget remained for the MIDI analysis process pool.")
            deadline = time.monotonic() + budget
            errors: list[str] = []
            error_lock = threading.Lock()

            def stop_runner(runner: DeepAnalysisProcessRunner) -> None:
                rem = max(0.0, deadline - time.monotonic())
                try:
                    runner.stop(timeout=rem)
                except (DeepAnalysisProcessError, OSError, RuntimeError) as exc:
                    with error_lock:
                        errors.append(f"{type(exc).__name__}: {exc}")

            stop_threads = [
                threading.Thread(
                    target=stop_runner, args=(runner,), name=f"MidiAnalyzerStop-{index + 1}", daemon=True
                )
                for index, runner in enumerate(runners)
            ]
            for thread in stop_threads:
                thread.start()
            for thread in stop_threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            pending = [thread for thread in stop_threads if thread.is_alive()]
            if pending:
                errors.append(f"{len(pending)} MIDI analysis worker stop task(s) exceeded the shared deadline")

            # REG-103: pool shutdown may report success only after every exact
            # subprocess handle owned by every runner is terminal.  Qualification
            # also checks OS process identity, but production lifecycle ownership
            # must not depend on an external PID sweep.  If a runner stop task
            # returned while its Popen handle is still live, use the remaining
            # shared budget for one final owned-process escalation and verify the
            # same handle has become terminal before clearing pool ownership.
            for runner in runners:
                process = runner.active_process
                if process is None or process.poll() is not None:
                    continue
                rem = max(0.0, deadline - time.monotonic())
                if rem <= 0 or not runtime_supervisor.terminate_process(process, timeout=rem):
                    errors.append(
                        f"Detailed-analysis worker PID {process.pid} remained live after pool shutdown"
                    )
                elif process.poll() is None:
                    errors.append(
                        f"Detailed-analysis worker PID {process.pid} did not reach a terminal handle state"
                    )

            if errors:
                with self._lock:
                    self._stopping = False
                raise DeepAnalysisProcessError(
                    "One or more MIDI analysis workers did not stop cleanly: " + "; ".join(errors)
                )
            with self._lock:
                self._closed = True
                self._stopping = False
                self._runners.clear()
                while not self._available.empty():
                    try:
                        self._available.get_nowait()
                    except queue.Empty:
                        break


    def snapshot(self) -> dict[str, object]:
        runners = [runner.snapshot() for runner in self._runners]
        return {
            "workers": self.worker_total,
            "active_processes": sum(1 for item in runners if item.get("active")),
            "persistent_worker_pool": True,
            "runners": runners,
        }
