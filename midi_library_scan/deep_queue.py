from __future__ import annotations

import heapq
import itertools
import threading
import time
from pathlib import Path
from typing import Callable

from .database import LibraryDatabase
from .models import DeepAnalysisJob


class DeepAnalysisQueue:
    """Cleanly pausable queue. The app supplies its existing deep-analysis callable."""

    def __init__(
        self,
        database: LibraryDatabase,
        analyzer: Callable[[Path], None],
        on_state: Callable[[str, str], None] | None = None,
        on_queue_low: Callable[[], None] | None = None,
        worker_count: int = 1,
    ):
        self.database = database
        self.analyzer = analyzer
        self.on_state = on_state or (lambda _path, _state: None)
        self.on_queue_low = on_queue_low or (lambda: None)
        self._jobs: list[DeepAnalysisJob] = []
        self._known: set[str] = set()
        self._seq = itertools.count()
        self._lock = threading.Condition()
        self._stop = False
        self._paused = True
        self._worker_count = max(1, min(8, int(worker_count)))
        self._active_limit = self._worker_count
        self._threads: list[threading.Thread] = []
        self._active_paths: set[str] = set()

    def start(self) -> None:
        with self._lock:
            alive = [thread for thread in self._threads if thread.is_alive()]
            self._threads = alive
            self._stop = False
            for index in range(len(alive), self._worker_count):
                thread = threading.Thread(
                    target=self._run, name=f"MidiDeepAnalysis-{index + 1}", daemon=True
                )
                self._threads.append(thread)
                thread.start()

    def enqueue(self, path: str | Path, priority: int = 50) -> None:
        path = str(Path(path).resolve())
        with self._lock:
            if path in self._known or path in self._active_paths:
                return
            self._known.add(path)
            heapq.heappush(self._jobs, DeepAnalysisJob(priority, next(self._seq), Path(path)))
            self._lock.notify_all()

    def prioritize_selected(self, path: str | Path) -> None:
        self.enqueue(path, priority=0)

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._lock.notify_all()

    def clear_pending(self) -> None:
        """Drop queued jobs while allowing an already-running analyzer to finish safely."""
        with self._lock:
            self._jobs.clear()
            self._known.clear()
            self._lock.notify_all()

    def resume(self) -> None:
        self.start()
        with self._lock:
            self._paused = False
            self._lock.notify_all()

    def stop(self, timeout: float = 30.0) -> bool:
        with self._lock:
            self._stop = True
            self._paused = False
            self._jobs.clear()
            self._known.clear()
            self._lock.notify_all()
        deadline = time.monotonic() + max(0.0, float(timeout))
        current = threading.current_thread()
        for thread in list(self._threads):
            if thread is current or not thread.is_alive():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        return not self._threads

    def is_alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def worker_count(self) -> int:
        return self._worker_count

    def set_active_limit(self, limit: int | None = None) -> int:
        """Temporarily cap concurrent deep jobs without rebuilding worker threads.

        Fast catalogue and Detailed Analysis share the supervised process pool.
        During a first huge Fast scan the Engine can reserve most workers for
        quick catalogue work while still deeply analyzing a small bootstrap set
        that makes the first remix ideas available sooner.
        """
        value = self._worker_count if limit is None else max(1, min(self._worker_count, int(limit)))
        with self._lock:
            self._active_limit = value
            self._lock.notify_all()
        return value

    def active_limit(self) -> int:
        with self._lock:
            return int(self._active_limit)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_paths)

    def active_paths(self) -> tuple[str, ...]:
        """Snapshot paths currently owned by Detailed Analysis workers."""
        with self._lock:
            return tuple(sorted(self._active_paths))

    def depth(self) -> int:
        with self._lock:
            return len(self._jobs)


    def _notify_state(self, path: str, state: str) -> None:
        """Publish observational state without letting a dead UI/pipe kill a worker.

        Database state is authoritative. A callback may target an IPC/logging
        sink that disappears during shutdown/preemption; that must not convert a
        successfully interrupted MIDI into an unraisable worker-thread failure.
        """
        try:
            self.on_state(path, state)
        except (BrokenPipeError, ConnectionError, OSError, RuntimeError):
            return

    def _notify_queue_low(self) -> None:
        """Publish the low-queue callback without letting an observer kill a worker."""
        try:
            self.on_queue_low()
        except (BrokenPipeError, ConnectionError, OSError, RuntimeError):
            return

    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    while not self._stop and (
                        self._paused or not self._jobs or len(self._active_paths) >= self._active_limit
                    ):
                        self._lock.wait(timeout=0.5)
                    if self._stop:
                        return
                    job = heapq.heappop(self._jobs)
                    path = str(job.path)
                    self._active_paths.add(path)
                started = time.monotonic()
                self.database.mark_deep_status(path, "analyzing")
                self._notify_state(path, "analyzing")
                try:
                    self.analyzer(job.path)
                except Exception as exc:
                    if bool(getattr(exc, "analysis_cancelled", False)):
                        # Engine/user shutdown is an interrupted operation, not
                        # evidence that the source MIDI is bad. Return the row
                        # to paused and do not consume a recovery attempt.
                        self.database.mark_deep_interrupted(path)
                        self._notify_state(path, "paused")
                    else:
                        elapsed = max(0.0, time.monotonic() - started)
                        self.database.mark_deep_status(
                            path, "failed", f"{type(exc).__name__}: {exc}", duration_seconds=elapsed
                        )
                        self._notify_state(path, "failed")
                else:
                    elapsed = max(0.0, time.monotonic() - started)
                    # Detailed analysis is not externally complete until the
                    # authoritative lifecycle/eligibility callback has committed.
                    # Publishing ``complete`` first created a race where Browser/
                    # Remixer readers could observe Deep Complete + Analyzed/
                    # Unresolved for the same MIDI. Keep a recoverable internal
                    # finalizing state until lifecycle classification returns, then
                    # publish the terminal Deep status.
                    self.database.mark_deep_status(path, "finalizing", duration_seconds=elapsed)
                    self._notify_state(path, "complete")
                    self.database.mark_deep_status(path, "complete", duration_seconds=elapsed)
                finally:
                    with self._lock:
                        self._known.discard(path)
                        self._active_paths.discard(path)
                        self._lock.notify_all()
                if self.depth() < 64 and not self._stop:
                    self._notify_queue_low()
        finally:
            with self._lock:
                current = threading.current_thread()
                self._threads = [thread for thread in self._threads if thread is not current]
                self._lock.notify_all()

