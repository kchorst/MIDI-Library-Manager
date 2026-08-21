from __future__ import annotations

from collections import deque
from pathlib import Path
import os
import time
from queue import Empty, Queue
from core.atomic_io import atomic_write_json
from threading import Event, Lock, Thread, current_thread
from typing import Callable, Iterable

from .catalog import SongCatalog
from .database import LibraryDatabase
from .deep_queue import DeepAnalysisQueue
from .family_identity import family_id_for_result
from .midi_quick import failed_quick_scan_result, scan_midi
from .models import DeepAnalysisPolicy, QuickScanResult, ScanProgress, ScanSummary

MIDI_EXTENSIONS = {".mid", ".midi", ".kar"}
GENERATED_FOLDER_NAMES = {"takes", "trials", "renders", "previews", "exports", "generated", ".midi-remixer", "working_sessions"}
QUICK_COMMIT_BATCH = 128
QUICK_FIRST_COMMIT_BATCH = 16
QUICK_EARLY_COMMIT_WINDOW = 64


class LibraryScanService:
    """Persistent, incremental MIDI catalogue and detailed-analysis coordinator.

    v0.25 deliberately separates the cheap filesystem inventory from MIDI parsing.
    A normal refresh opens only new or changed MIDI files; unchanged records are
    reused from SQLite.  Parsed rows are committed in bounded batches so cancel,
    shutdown, or a single bad file cannot throw away hours of completed work.
    """

    def __init__(
        self,
        database_path: str | Path,
        catalog_path: str | Path | None = None,
        deep_analyzer: Callable[[Path], None] | None = None,
        on_progress: Callable[[ScanProgress], None] | None = None,
        on_deep_state: Callable[[str, str], None] | None = None,
        on_quick_commit: Callable[[tuple[str, ...]], None] | None = None,
        deep_worker_count: int = 1,
        quick_scanner: Callable[[Path], QuickScanResult] | None = None,
        quick_worker_count: int = 1,
    ):
        self.database = LibraryDatabase(database_path)
        self.catalog = SongCatalog.from_resource(catalog_path)
        self.on_progress = on_progress or (lambda _p: None)
        self.on_quick_commit = on_quick_commit or (lambda _paths: None)
        self.cancel_event = Event()
        self.pause_scan_event = Event()
        self._scan_pause_ack = Event()
        # Keep None as the default so tests/diagnostics can replace the module-level
        # scanner after construction; Engine supplies its supervised process-pool
        # scanner explicitly.
        self.quick_scanner = quick_scanner
        self.quick_worker_count = max(1, min(8, int(quick_worker_count)))
        self._closed = False
        self._deep_seed_cancel = Event()
        self._deep_seed_lock = Lock()
        self._deep_seed_thread: Thread | None = None
        self._deep_auto_refill_enabled = True
        # Status/heartbeat reads must never wait behind a SQLite write transaction.
        # Keep the user policy in memory after one startup read; every policy
        # mutation goes through ``set_persistent_policy`` so the cache and DB stay
        # synchronized.
        self._policy_lock = Lock()
        raw_policy = self.database.get_policy() or DeepAnalysisPolicy.LATER.value
        try:
            self._deep_policy = DeepAnalysisPolicy(raw_policy)
        except ValueError:
            self._deep_policy = DeepAnalysisPolicy.LATER
        self.deep_queue = DeepAnalysisQueue(
            self.database,
            deep_analyzer or (lambda _path: None),
            on_state=on_deep_state,
            on_queue_low=self._request_deep_refill,
            worker_count=deep_worker_count,
        )

    @staticmethod
    def _inside_generated_folder(path: Path, root: Path) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        return bool({part.casefold() for part in relative.parts[:-1]} & GENERATED_FOLDER_NAMES)

    def _wait_while_scan_paused(self) -> bool:
        """Acknowledge and hold a persistent Fast-scan pause boundary."""
        self._scan_pause_ack.set()
        try:
            while self.pause_scan_event.is_set() and not self.cancel_event.is_set():
                time.sleep(0.05)
            return not self.cancel_event.is_set()
        finally:
            self._scan_pause_ack.clear()

    def discover_many(
        self,
        inputs: Iterable[str | Path],
        *,
        include_subfolders: bool = True,
        include_generated: bool = False,
        on_discovery: Callable[[int, Path], None] | None = None,
        inventory_out: dict[str, tuple[int, int]] | None = None,
    ) -> list[Path]:
        """Discover MIDI paths with low filesystem overhead.

        `Path.rglob()` + `is_file()` + `resolve()` for every entry caused several
        redundant filesystem lookups on six-figure libraries. `os.scandir()`
        reuses directory-entry metadata supplied by the OS, does not recurse
        through symlinked directories, and skips generated subtrees before
        walking them. Only symlinked files pay the extra resolve cost.
        """
        discovered: list[Path] = []
        seen: set[str] = set()
        last_progress_emit = 0.0

        def publish(path: Path, stat_result: os.stat_result | None = None) -> None:
            nonlocal last_progress_emit
            key = str(path).casefold()
            if key in seen:
                return
            seen.add(key)
            discovered.append(path)
            if inventory_out is not None and stat_result is not None:
                inventory_out[str(path)] = (int(stat_result.st_size), int(stat_result.st_mtime_ns))
            if on_discovery is None:
                return
            now = time.monotonic()
            count = len(discovered)
            if count == 1 or count % 256 == 0 or now - last_progress_emit >= 0.30:
                on_discovery(count, path)
                last_progress_emit = now

        for raw in inputs:
            if self.pause_scan_event.is_set() and not self._wait_while_scan_paused():
                break
            if self.cancel_event.is_set():
                break
            source = Path(raw).expanduser()
            try:
                source = source.resolve()
            except OSError:
                continue
            if source.is_file():
                if source.suffix.casefold() in MIDI_EXTENSIONS:
                    try:
                        source_stat = source.stat()
                    except OSError:
                        source_stat = None
                    publish(source, source_stat)
                continue
            if not source.is_dir():
                continue

            pending_dirs = [source]
            while pending_dirs and not self.cancel_event.is_set():
                if self.pause_scan_event.is_set() and not self._wait_while_scan_paused():
                    break
                directory = pending_dirs.pop()
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if self.pause_scan_event.is_set() and not self._wait_while_scan_paused():
                                break
                            if self.cancel_event.is_set():
                                break
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    if not include_subfolders:
                                        continue
                                    if not include_generated and entry.name.casefold() in GENERATED_FOLDER_NAMES:
                                        continue
                                    pending_dirs.append(Path(entry.path))
                                    continue
                                if not entry.is_file(follow_symlinks=True):
                                    continue
                            except OSError:
                                continue
                            suffix = os.path.splitext(entry.name)[1].casefold()
                            if suffix not in MIDI_EXTENSIONS:
                                continue
                            candidate = Path(entry.path)
                            if entry.is_symlink():
                                try:
                                    candidate = candidate.resolve()
                                except OSError:
                                    continue
                            try:
                                entry_stat = entry.stat(follow_symlinks=True)
                            except OSError:
                                entry_stat = None
                            publish(candidate, entry_stat)
                except OSError:
                    continue

        if on_discovery is not None and discovered:
            on_discovery(len(discovered), discovered[-1])
        return sorted(discovered, key=lambda p: str(p).casefold())

    def discover(self, root: str | Path, include_subfolders: bool = True) -> list[Path]:
        return self.discover_many([root], include_subfolders=include_subfolders)

    @staticmethod
    def _recount(summary: ScanSummary, results: list[QuickScanResult]) -> None:
        summary.catalogued = 0
        summary.identified = 0
        summary.probable_identity = 0
        summary.unidentified = 0
        summary.failed = 0
        summary.exact_duplicates = 0
        summary.likely_duplicates = 0
        exact_groups: dict[str, int] = {}
        version_groups: dict[str, int] = {}
        for result in results:
            if result.status == "failed":
                summary.failed += 1
            else:
                summary.catalogued += 1
                status = result.catalog_match.status
                if status in {"exact", "high"}:
                    summary.identified += 1
                elif status == "probable":
                    summary.probable_identity += 1
                else:
                    summary.unidentified += 1
            if result.content_duplicate_group:
                exact_groups[result.content_duplicate_group] = exact_groups.get(result.content_duplicate_group, 0) + 1
            if result.composition_group:
                version_groups[result.composition_group] = version_groups.get(result.composition_group, 0) + 1
            elif result.duplicate_kind.value == "likely_same_export" and result.duplicate_group:
                version_groups[result.duplicate_group] = version_groups.get(result.duplicate_group, 0) + 1
        summary.exact_duplicates = sum(max(0, count - 1) for count in exact_groups.values())
        summary.likely_duplicates = sum(max(0, count - 1) for count in version_groups.values())

    def quick_catalog_paths(
        self,
        inputs: Iterable[str | Path],
        *,
        include_subfolders: bool = True,
        include_generated: bool = False,
        replace_existing: bool = False,
        force_paths: Iterable[str | Path] | None = None,
        known_files_only: bool = False,
        quick_worker_limit: int | None = None,
        preserve_control_state: bool = False,
    ) -> ScanSummary:
        if not preserve_control_state:
            self.cancel_event.clear()
            self.pause_scan_event.clear()
            self._scan_pause_ack.clear()
        input_list = [str(Path(item).expanduser()) for item in inputs]
        forced = {str(Path(item).expanduser().resolve(strict=False)) for item in (force_paths or ())}
        summary = ScanSummary(root=";".join(input_list))

        existing_total = int(self.database.catalogue_counts().get("total") or 0)
        discovery_started = time.monotonic()

        def discovery_progress(count: int, current_path: Path) -> None:
            elapsed = max(0.001, time.monotonic() - discovery_started)
            rate = count / elapsed if count > 0 else None
            # On an incremental refresh, the previous committed catalogue is a
            # useful estimate of discovery size.  On a first scan there is no
            # honest total yet, so the UI explicitly says it is estimating
            # rather than inventing a completion time.
            estimated_total = existing_total if existing_total >= count and existing_total > 0 else 0
            eta = ((estimated_total - count) / rate) if rate and estimated_total > count else None
            self.on_progress(ScanProgress(
                "discovery", count, estimated_total, current_path.name,
                f"Discovering MIDI files · {count:,} found"
                + (" · estimating total" if estimated_total <= 0 else ""),
                rate_per_second=rate, eta_seconds=eta, current_folder=str(current_path.parent),
            ))

        discovered_inventory: dict[str, tuple[int, int]] = {}
        if known_files_only:
            # A human-requested failed-file retry already owns an exact path list.
            # Do not walk directories or hydrate the entire 96K inventory merely
            # to reparse those known failures.  This keeps the Engine responsive
            # and dramatically reduces filesystem/SQLite pressure while the
            # Library Browser remains open.
            paths: list[Path] = []
            self.on_progress(ScanProgress(
                "recovery_prepare", 0, max(1, len(input_list)),
                message=f"Preparing failed-file retry · 0/{len(input_list):,}",
            ))
            for checked, raw in enumerate(input_list, start=1):
                if self.cancel_event.is_set():
                    summary.cancelled = True
                    break
                if self.pause_scan_event.is_set() and not self._wait_while_scan_paused():
                    summary.cancelled = True
                    break
                path = Path(raw).expanduser()
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file() or path.suffix.casefold() not in MIDI_EXTENSIONS:
                    continue
                try:
                    resolved = path.resolve(strict=False)
                except OSError:
                    resolved = path.absolute()
                paths.append(resolved)
                discovered_inventory[str(resolved)] = (int(stat.st_size), int(stat.st_mtime_ns))
                if checked == 1 or checked % 128 == 0 or checked == len(input_list):
                    self.on_progress(ScanProgress(
                        "recovery_prepare", checked, max(1, len(input_list)), path.name,
                        f"Preparing failed-file retry · {checked:,}/{len(input_list):,}",
                        current_folder=str(path.parent),
                    ))
        else:
            self.on_progress(ScanProgress("discovery", 0, existing_total, message="Discovering MIDI files…"))
            paths = self.discover_many(
                input_list,
                include_subfolders=include_subfolders,
                include_generated=include_generated,
                on_discovery=discovery_progress,
                inventory_out=discovered_inventory,
            )
        summary.discovered = len(paths)
        summary.active_paths = [str(path) for path in paths]
        if self.cancel_event.is_set() or summary.cancelled:
            summary.cancelled = True
            return summary

        inventory = {} if known_files_only else self.database.inventory_index()
        candidates: list[Path] = []
        inventory_started = time.monotonic()
        for checked, path in enumerate(paths, start=1):
            if self.pause_scan_event.is_set() and not self._wait_while_scan_paused():
                summary.cancelled = True
                break
            if self.cancel_event.is_set():
                summary.cancelled = True
                break
            stat_meta = discovered_inventory.get(str(path))
            if stat_meta is None:
                try:
                    stat = path.stat()
                    stat_meta = (int(stat.st_size), int(stat.st_mtime_ns))
                except OSError:
                    continue
            size_bytes, modified_ns = stat_meta
            resolved_path = str(path.resolve(strict=False))
            old = inventory.get(str(path)) or inventory.get(resolved_path)
            if known_files_only or resolved_path in forced:
                # A human-requested retry deliberately bypasses incremental reuse.
                # This is critical after parser/Engine upgrades: an unchanged file
                # may deserve a fresh parse even though its filesystem metadata did
                # not change.  The existing row stays committed until the new result
                # is safely upserted.
                candidates.append(path)
                summary.changed_files += 1
            elif old and old[0] == size_bytes and old[1] == modified_ns and old[2]:
                summary.reused += 1
            else:
                candidates.append(path)
                if old is None:
                    summary.new_files += 1
                else:
                    summary.changed_files += 1
            if checked == 1 or checked % 256 == 0 or checked == summary.discovered:
                elapsed = max(0.001, time.monotonic() - inventory_started)
                rate = checked / elapsed
                eta = max(0.0, (summary.discovered - checked) / rate) if rate > 0 else None
                self.on_progress(ScanProgress(
                    "inventory", checked, max(1, summary.discovered), path.name,
                    f"Comparing file changes · {checked:,}/{summary.discovered:,} checked · "
                    f"{summary.reused:,} unchanged · {len(candidates):,} new/changed",
                    rate_per_second=rate, eta_seconds=eta, current_folder=str(path.parent),
                ))

        self.on_progress(
            ScanProgress(
                "inventory",
                summary.discovered if not summary.cancelled else summary.reused + len(candidates),
                max(1, summary.discovered),
                message=(
                    f"{summary.discovered:,} MIDI files found · {summary.reused:,} unchanged · "
                    f"{len(candidates):,} new/changed"
                ),
                eta_seconds=0.0 if not summary.cancelled else None,
            )
        )

        batch: list[QuickScanResult] = []
        early_committed = 0
        total_work = len(candidates)
        catalogue_started = time.monotonic()
        if total_work:
            self.on_progress(ScanProgress(
                "cataloguing", 0, total_work, message=f"Fast musical catalogue · 0/{total_work:,} new/changed files",
            ))

        def flush_batch() -> None:
            nonlocal early_committed
            if not batch:
                return
            committed_paths = tuple(result.path for result in batch)
            self.database.upsert_many(batch)
            batch.clear()
            early_committed += len(committed_paths)
            # Publish committed rows immediately. Remixer can consume the usable
            # fast catalogue while the remainder of a six-figure first scan is
            # still running instead of waiting for the final 96K file.
            self.on_quick_commit(committed_paths)

        def accept_result(path: Path, result: QuickScanResult, completed: int) -> None:
            elapsed = max(0.001, time.monotonic() - catalogue_started)
            rate = completed / elapsed if completed > 0 else None
            eta = max(0.0, (total_work - completed) / rate) if rate and total_work >= completed else None
            self.on_progress(
                ScanProgress(
                    "cataloguing",
                    completed,
                    max(1, total_work),
                    path.name,
                    f"Fast musical catalogue · {completed:,}/{total_work:,} new/changed files",
                    rate_per_second=rate, eta_seconds=eta, current_folder=str(path.parent),
                )
            )
            if result.status != "failed":
                result.catalog_match = self.catalog.match(result.filename, result.embedded_text)
                result.family_id = family_id_for_result(result)
            elif str(result.error or "").startswith("Safety limit:"):
                summary.resource_limited += 1
            summary.results.append(result)
            batch.append(result)
            commit_target = (
                QUICK_FIRST_COMMIT_BATCH if early_committed < QUICK_EARLY_COMMIT_WINDOW else QUICK_COMMIT_BATCH
            )
            if len(batch) >= commit_target:
                flush_batch()

        # The first six-figure catalogue is CPU-heavy.  In Maximum Throughput,
        # use a few already-supervised persistent Engine workers concurrently;
        # tiny incremental updates stay serial so process coordination costs do
        # not overwhelm the useful work.  Submission is bounded to keep a 96K
        # scan from allocating 96K Future objects at once.
        active_quick_workers = self.quick_worker_count
        if quick_worker_limit is not None:
            active_quick_workers = max(1, min(active_quick_workers, int(quick_worker_limit)))
        parallel = active_quick_workers > 1 and total_work >= 64
        completed = 0
        if parallel:
            # Quick parsing dispatch uses daemon coordination threads rather than
            # ThreadPoolExecutor.  CPython registers executor threads for an
            # unconditional interpreter-exit join; one pathological request can
            # therefore keep a detached Engine alive even after its supervised
            # analyzer subprocess has been killed.  These dispatch threads never
            # touch SQLite or persistent state: they only call the supervised
            # scanner and return a result to this coordinator, so abandoning a
            # still-stuck dispatch thread during Engine teardown is safe.
            task_queue: Queue[tuple[int, Path] | None] = Queue()
            result_queue: Queue[tuple[int, Path, QuickScanResult | None, BaseException | None]] = Queue()
            dispatch_stop = Event()
            dispatch_threads: list[Thread] = []
            in_flight: dict[int, tuple[Path, float]] = {}
            next_index = 0
            next_token = 0
            retry_paths: deque[Path] = deque()
            last_active_heartbeat = 0.0
            def dispatch_worker() -> None:
                while not dispatch_stop.is_set():
                    try:
                        item = task_queue.get(timeout=0.10)
                    except Empty:
                        continue
                    if item is None:
                        return
                    token, path = item
                    try:
                        active_scanner = self.quick_scanner or scan_midi
                        result_queue.put((token, path, active_scanner(path), None))
                    except BaseException as exc:  # worker boundary: report, never strand coordinator
                        result_queue.put((token, path, None, exc))
                    finally:
                        task_queue.task_done()

            for index in range(active_quick_workers):
                thread = Thread(
                    target=dispatch_worker, name=f"MidiQuickDispatch-{index + 1}", daemon=True
                )
                thread.start()
                dispatch_threads.append(thread)

            def fill_workers() -> None:
                nonlocal next_index, next_token
                # Exactly one outstanding item per dispatcher.  Pause therefore
                # has a strict, small preemption surface and no hidden work queue.
                while (
                    (retry_paths or next_index < total_work)
                    and len(in_flight) < active_quick_workers
                    and not self.pause_scan_event.is_set()
                    and not self.cancel_event.is_set()
                ):
                    if retry_paths:
                        path = retry_paths.popleft()
                    else:
                        path = candidates[next_index]
                        next_index += 1
                    next_token += 1
                    in_flight[next_token] = (path, time.monotonic())
                    task_queue.put((next_token, path))

            try:
                fill_workers()
                while in_flight or retry_paths or next_index < total_work:
                    if self.cancel_event.is_set():
                        summary.cancelled = True
                        break
                    if self.pause_scan_event.is_set() and not in_flight:
                        if not self._wait_while_scan_paused():
                            summary.cancelled = True
                            break
                        fill_workers()
                        continue
                    if not in_flight:
                        fill_workers()
                        continue

                    try:
                        token, path, result, error = result_queue.get(timeout=0.10)
                    except Empty:
                        now = time.monotonic()
                        if in_flight and now - last_active_heartbeat >= 1.0:
                            oldest_token = min(in_flight, key=lambda item: in_flight[item][1])
                            oldest_path, oldest_started = in_flight[oldest_token]
                            oldest_age = max(0.0, now - oldest_started)
                            elapsed = max(0.001, now - catalogue_started)
                            rate = completed / elapsed if completed > 0 else None
                            eta = max(0.0, (total_work - completed) / rate) if rate else None
                            self.on_progress(ScanProgress(
                                "cataloguing", completed, max(1, total_work), oldest_path.name,
                                f"Fast musical catalogue · {completed:,}/{total_work:,} · "
                                f"{len(in_flight)} active · oldest {oldest_age:.1f}s",
                                rate_per_second=rate, eta_seconds=eta, current_folder=str(oldest_path.parent),
                            ))
                            last_active_heartbeat = now
                        continue

                    if token not in in_flight:
                        # Result from a dispatcher that was deliberately abandoned
                        # after a lifecycle transition; it owns no persistent state.
                        continue
                    in_flight.pop(token, None)
                    if error is not None:
                        if isinstance(error, (OSError, RuntimeError, ValueError, TypeError)):
                            exc = error
                        else:
                            exc = RuntimeError(f"Quick scanner worker failed: {type(error).__name__}: {error}")
                        interrupted = bool(getattr(exc, "analysis_cancelled", False))
                        if interrupted and self.cancel_event.is_set():
                            summary.cancelled = True
                            continue
                        if interrupted and self.pause_scan_event.is_set():
                            retry_paths.append(path)
                            continue
                        result = failed_quick_scan_result(path, exc)
                    if result is None:
                        result = failed_quick_scan_result(path, RuntimeError("Quick scanner returned no result."))
                    completed += 1
                    accept_result(path, result, completed)
                    if not self.pause_scan_event.is_set():
                        fill_workers()
            finally:
                dispatch_stop.set()
                for _thread in dispatch_threads:
                    task_queue.put(None)
                # Never turn dispatcher cleanup back into an unbounded shutdown
                # join. Normal runs exit immediately; Engine teardown may safely
                # leave a daemon dispatcher whose supervised child was preempted.
                join_deadline = time.monotonic() + 0.30
                for thread in dispatch_threads:
                    thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
        else:
            next_serial = 0
            while next_serial < total_work:
                path = candidates[next_serial]
                if self.pause_scan_event.is_set() and not self._wait_while_scan_paused():
                    summary.cancelled = True
                    break
                if self.cancel_event.is_set():
                    summary.cancelled = True
                    break
                try:
                    result = (self.quick_scanner or scan_midi)(path)
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    interrupted = bool(getattr(exc, "analysis_cancelled", False))
                    if interrupted and self.cancel_event.is_set():
                        summary.cancelled = True
                        break
                    if interrupted and self.pause_scan_event.is_set():
                        # Resume retries the same path; a lifecycle interruption
                        # is never converted into a persisted MIDI failure.
                        continue
                    result = failed_quick_scan_result(path, exc)
                completed += 1
                next_serial += 1
                accept_result(path, result, completed)

        if batch:
            flush_batch()

        # Scope replacement happens only after a complete inventory. Cancellation
        # never deletes older records; the next incremental pass can reconcile them.
        # Capture affected duplicate/family keys before deleting so a family that
        # shrinks from two files to one can be corrected without regrouping 96k rows.
        affected_families: set[str] = set()
        affected_hashes: set[str] = set()
        if summary.results:
            changed_meta = self.database.metadata_for_paths(result.path for result in summary.results)
            for family_id, sha256 in changed_meta.values():
                if family_id:
                    affected_families.add(str(family_id))
                if sha256:
                    affected_hashes.add(str(sha256))
        if replace_existing and not summary.cancelled:
            active = set(summary.active_paths)
            existing = set(inventory)
            removed = sorted(existing - active)
            if removed:
                removed_meta = self.database.metadata_for_paths(removed)
                for family_id, sha256 in removed_meta.values():
                    if family_id:
                        affected_families.add(str(family_id))
                    if sha256:
                        affected_hashes.add(str(sha256))
                self.database.delete_paths(removed)
                summary.removed_paths = removed
                summary.removed_files = len(removed)

        # v0.27 maintains duplicate/version metadata only for families touched by
        # this incremental pass. A one-file change must never decode/regroup the
        # complete six-figure catalogue.
        if (affected_families or affected_hashes) and not summary.cancelled:
            self.database.reconcile_duplicate_keys(affected_families, affected_hashes)

        counts = self.database.catalogue_counts()
        summary.catalogued = int(counts["catalogued"])
        summary.failed = int(counts["failed"])
        summary.identified = int(counts["identified"])
        summary.probable_identity = int(counts["probable_identity"])
        summary.unidentified = int(counts["unidentified"])
        summary.exact_duplicates = int(counts["exact_duplicates"])
        summary.likely_duplicates = int(counts["likely_duplicates"])
        summary.resource_limited = int(counts["resource_limited"])
        summary.cancelled = summary.cancelled or self.cancel_event.is_set()
        action = "stopped safely" if summary.cancelled else "up to date"
        self.on_progress(
            ScanProgress(
                "complete",
                summary.catalogued + summary.failed,
                max(1, summary.discovered),
                message=(
                    f"Library {action} · {summary.reused} reused · "
                    f"{summary.new_files + summary.changed_files} parsed"
                ),
            )
        )
        return summary

    def quick_catalog(self, root: str | Path, include_subfolders: bool = True) -> ScanSummary:
        return self.quick_catalog_paths([root], include_subfolders=include_subfolders)

    def cached_results(
        self, roots: list[str | Path] | None = None, *, validate_sources: bool = True
    ) -> list[QuickScanResult]:
        return self.database.load_results(roots, validate_sources=validate_sources)

    def _request_deep_refill(self) -> None:
        if (
            self._closed or not self._deep_auto_refill_enabled
            or self.current_policy() != DeepAnalysisPolicy.BACKGROUND
        ):
            return
        self._start_deep_seed()

    def set_deep_auto_refill_enabled(self, enabled: bool) -> None:
        self._deep_auto_refill_enabled = bool(enabled)

    def _start_deep_seed(self, only_paths: Iterable[str | Path] | None = None) -> None:
        """Populate the Deep queue on an isolated cancellable read connection.

        The 96K-family ranking query must never hold the primary LibraryDatabase
        lock while foreground Fast work is trying to acquire ownership.
        """
        with self._deep_seed_lock:
            existing = self._deep_seed_thread
        if existing is not None and existing.is_alive():
            if not self._deep_seed_cancel.is_set():
                return
            # Cancellation uses SQLite's progress handler, so a stale seed should
            # relinquish quickly.  Never clear its cancel flag while it is live.
            existing.join(timeout=1.25)
            if existing.is_alive():
                raise RuntimeError("Detailed-analysis queue seed did not yield before restart.")

        with self._deep_seed_lock:
            self._deep_seed_cancel.clear()
            allowed_source = only_paths

            def seed() -> None:
                reader: LibraryDatabase | None = None
                try:
                    allowed = None if allowed_source is None else [str(path) for path in allowed_source]
                    reader = LibraryDatabase(
                        self.database.path, recover_interrupted=False, read_only=True, busy_timeout_seconds=1.0
                    )
                    pending = reader.pending_paths(
                        allowed, limit=4096, cancel_check=lambda: self._deep_seed_cancel.is_set() or self._closed
                    )
                    for priority, path in enumerate(pending, start=20):
                        if self._deep_seed_cancel.is_set() or self._closed:
                            return
                        self.deep_queue.enqueue(path, priority=min(priority, 90))
                finally:
                    if reader is not None:
                        reader.close()
                    with self._deep_seed_lock:
                        if self._deep_seed_thread is current_thread():
                            self._deep_seed_thread = None

            thread = Thread(target=seed, name="MidiDeepQueueSeed", daemon=True)
            self._deep_seed_thread = thread
            thread.start()

    def wait_deep_seed_idle(self, timeout: float) -> bool:
        """Wait for the current seed worker using one bounded caller budget."""
        with self._deep_seed_lock:
            thread = self._deep_seed_thread
        if thread is None or not thread.is_alive() or thread is current_thread():
            return True
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()

    def deep_seed_active(self) -> bool:
        with self._deep_seed_lock:
            return bool(self._deep_seed_thread and self._deep_seed_thread.is_alive())

    def set_persistent_policy(self, policy: DeepAnalysisPolicy | str) -> DeepAnalysisPolicy:
        """Persist and cache the Detailed Analysis policy.

        The cache is authoritative for runtime/status reads so a heartbeat can
        remain responsive even while the Fast catalogue owns SQLite's writer
        transaction. The database remains authoritative across process restart.
        """
        try:
            value = policy if isinstance(policy, DeepAnalysisPolicy) else DeepAnalysisPolicy(str(policy))
        except ValueError:
            value = DeepAnalysisPolicy.LATER
        with self._policy_lock:
            previous = self._deep_policy
            self._deep_policy = value
        try:
            self.database.set_policy(value.value)
        except Exception:
            with self._policy_lock:
                self._deep_policy = previous
            raise
        return value

    def apply_deep_policy(
        self,
        policy: DeepAnalysisPolicy,
        selected_path: str | Path | None = None,
        only_paths: Iterable[str | Path] | None = None,
    ) -> None:
        self.set_persistent_policy(policy)
        if policy == DeepAnalysisPolicy.LATER:
            self._deep_seed_cancel.set()
            self.deep_queue.pause()
            self.deep_queue.clear_pending()
            return
        if policy == DeepAnalysisPolicy.SELECTED_ONLY:
            self._deep_seed_cancel.set()
            self.deep_queue.pause()
            self.deep_queue.clear_pending()
            if selected_path:
                self.deep_queue.prioritize_selected(selected_path)
                self.deep_queue.resume()
            return
        self.deep_queue.resume()
        self._start_deep_seed(only_paths)
        if selected_path:
            self.deep_queue.prioritize_selected(selected_path)

    def current_policy(self) -> DeepAnalysisPolicy:
        # Deliberately database-free: Engine status/heartbeat must remain live
        # while another worker owns a long catalogue transaction.
        with self._policy_lock:
            return self._deep_policy

    def deep_runtime_state(self) -> dict[str, int | str]:
        """Return queue/policy state without querying the 96K catalogue table.

        The Engine heartbeat/status endpoint uses this lightweight snapshot and
        refreshes persistent counts separately.  This prevents routine UI polls
        from blocking behind large catalogue transactions.
        """
        return {
            "policy": self.current_policy().value,
            "queue_alive": int(self.deep_queue.is_alive()),
            "queue_workers": int(self.deep_queue.worker_count()),
            "queue_active": int(self.deep_queue.active_count()),
            "queue_active_limit": int(self.deep_queue.active_limit()),
            "queue_depth": int(self.deep_queue.depth()),
            "queue_seeding": int(bool(self._deep_seed_thread and self._deep_seed_thread.is_alive())),
        }

    def deep_progress(self, only_paths: Iterable[str | Path] | None = None) -> dict[str, int | str]:
        # Whole-library UI progress is intentionally unscoped; the persistent DB
        # is reconciled to the active library. Avoid resolving 96k paths here.
        allowed = None if only_paths is None else [str(path) for path in only_paths]
        counts: dict[str, int | str] = dict(self.database.deep_status_counts(allowed))
        counts.update(self.deep_runtime_state())
        return counts

    def analyze_selected(self, path: str | Path) -> None:
        status = self.database.deep_status(path)
        if status == "complete":
            return
        if status == "failed":
            self.database.retry_failed_deep_analysis([path])
        self.database.prioritize_paths([path], 0)
        self.deep_queue.prioritize_selected(path)
        self.deep_queue.resume()

    def retry_failed_deep_analysis(self, paths: Iterable[str | Path] | None = None) -> int:
        """Requeue failed analyses and resume background work.

        ``paths is None`` is the dashboard one-time recovery pass and therefore
        only requeues first failures. Explicit paths are a deliberate human
        retry after the source/analyzer/limits have changed and may requeue a
        persistent exception.
        """
        changed = self.database.retry_failed_deep_analysis(paths)
        if changed:
            self.set_persistent_policy(DeepAnalysisPolicy.BACKGROUND)
            self.deep_queue.resume()
            self._start_deep_seed()
        return changed

    def pause_deep_analysis(self) -> None:
        self._deep_seed_cancel.set()
        self.deep_queue.pause()
        self.deep_queue.clear_pending()
        self.set_persistent_policy(DeepAnalysisPolicy.LATER)

    def suspend_deep_analysis(self) -> bool:
        """Temporarily yield background analysis without changing user policy.

        This is a control-plane operation and therefore deliberately performs no
        aggregate SQLite query.  The caller may subsequently preempt an active
        supervised analyzer so Fast/maintenance work does not wait behind the
        135-second pathological-MIDI hard limit.
        """
        if self.current_policy() != DeepAnalysisPolicy.BACKGROUND:
            return False
        self._deep_seed_cancel.set()
        self.deep_queue.pause()
        self.deep_queue.clear_pending()
        return True

    def resume_suspended_deep_analysis(self, only_paths: Iterable[str | Path] | None = None) -> bool:
        """Resume a temporary yield only if the user policy is still background."""
        if self.current_policy() != DeepAnalysisPolicy.BACKGROUND:
            return False
        self.deep_queue.resume()
        self._start_deep_seed(only_paths)
        return True

    def resume_deep_analysis(self, only_paths: Iterable[str | Path] | None = None) -> None:
        self.set_persistent_policy(DeepAnalysisPolicy.BACKGROUND)
        self.deep_queue.resume()
        if only_paths is not None:
            # Explicit committed batches are small. Queue them directly instead
            # of starting the whole-library seed worker; this is how a first
            # 96K scan can prepare a few genuinely usable songs without letting
            # Detailed Analysis compete with Fast catalogue for every file.
            allowed = tuple(str(path) for path in only_paths)
            pending = self.database.pending_paths(allowed, limit=max(1, len(allowed))) if allowed else []
            for priority, path in enumerate(pending, start=10):
                self.deep_queue.enqueue(path, priority=min(priority, 40))
            return
        self._start_deep_seed()

    def set_deep_active_limit(self, limit: int | None = None) -> int:
        return self.deep_queue.set_active_limit(limit)

    def pause_scan(self) -> None:
        self._scan_pause_ack.clear()
        self.pause_scan_event.set()

    def resume_scan(self) -> None:
        self.pause_scan_event.clear()
        self._scan_pause_ack.clear()

    def scan_pause_requested(self) -> bool:
        return self.pause_scan_event.is_set()

    def scan_pause_acknowledged(self) -> bool:
        return self._scan_pause_ack.is_set()

    def wait_until_scan_paused(self, timeout: float) -> bool:
        if not self.pause_scan_event.is_set():
            return False
        return self._scan_pause_ack.wait(timeout=max(0.0, float(timeout)))

    def scan_paused(self) -> bool:
        return self.pause_scan_event.is_set() and self._scan_pause_ack.is_set()

    def cancel_scan(self) -> None:
        # Clear pause first so a paused coordinator can observe cancellation and
        # reach its persistent safe boundary promptly.
        self.pause_scan_event.clear()
        self._scan_pause_ack.clear()
        self.cancel_event.set()

    def export_report(self, summary: ScanSummary, path: str | Path) -> None:
        atomic_write_json(Path(path), summary.as_dict())

    def close(self, timeout: float = 8.0) -> None:
        if self._closed:
            return
        deadline = time.monotonic() + max(0.25, float(timeout))
        self.pause_scan_event.clear()
        self.cancel_event.set()
        self._deep_seed_cancel.set()
        seed_thread = self._deep_seed_thread
        if seed_thread and seed_thread.is_alive():
            seed_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if seed_thread and seed_thread.is_alive():
            raise RuntimeError("Detailed-analysis queue seeding worker did not stop within the shared shutdown deadline")
        self._deep_seed_thread = None
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError("No shutdown budget remained for Detailed-analysis workers")
        stopped = self.deep_queue.stop(timeout=remaining)
        if not stopped:
            raise RuntimeError(
                "Detailed-analysis worker did not stop within the shared shutdown deadline; cleanup remains retryable."
            )
        self.database.checkpoint(truncate=True)
        self.database.close()
        self._closed = True

    def __enter__(self) -> "LibraryScanService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
