from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import os
import shutil
import sqlite3
import stat
import threading
import time
from typing import Any, Iterable

from core.app_paths import AppPaths
from core.library_engine_settings import LibraryEngineSettings
from core.candidate_index import CandidateIndex
from core.deep_analysis_process import DeepAnalysisProcessPool
from core.discovery_graph import DiscoveryGraph
from core.library_backup import create_library_intelligence_backup
from core.library_index import ANALYSIS_SCHEMA_VERSION, LibraryIndex
from core.reference_catalog import ReferenceCatalog
from core.remix_profile import assess_pair
from core.recursive_directory_watcher import RecursiveDirectoryWatcher
from core.staged_library import quick_result_to_profile
from core.song_identity import authoritative_same_composition, materially_different_arrangement
from core.suspicious_midi import suspicious_midi_candidates
from library_engine.candidate_summary import summary_from_quick
from library_engine.family_reconciliation import reconcile_completed_path_family, reconcile_persisted_families
from library_engine.relationships import (
    RELATIONSHIP_ALGORITHM_VERSION,
    RelationshipStore, assisted_relationship_for_profiles, guided_relationship_for_profiles, with_discovery_evidence,
)
from library_engine.timing import (
    ANALYZER_PREEMPT_SECONDS, DEEP_PAUSE_TRANSITION_SECONDS, ENGINE_SHUTDOWN_HARD_SECONDS,
    FAST_CANCEL_TRANSITION_SECONDS, FAST_PAUSE_TRANSITION_SECONDS, FAST_PRIORITY_TRANSITION_SECONDS,
    MAINTENANCE_QUIESCE_SECONDS,
    RELATIONSHIP_DEEP_ANCHOR_BUDGET, RELATIONSHIP_DEEP_COMPLETION_BATCH,
    RELATIONSHIP_YIELD_SECONDS, STATUS_METRICS_MAX_AGE_SECONDS,
)
from midi_library_scan import DeepAnalysisPolicy, LibraryScanService
from midi_library_scan.database import SCHEMA_VERSION as QUICK_SCHEMA_VERSION
from midi_library_scan.models import ScanProgress
from midi_library_scan.lifecycle import EligibilityState, RejectionCode, clearly_no_usable_content, quick_health_reasons


THROUGHPUT_MODES = {"maximum", "balanced", "low_impact"}


def recommended_worker_count(mode: str = "maximum") -> int:
    cpu = max(1, int(os.cpu_count() or 1))
    key = str(mode or "maximum").casefold()
    if key == "low_impact":
        return 1
    if key == "balanced":
        return max(1, min(2, cpu - 1 if cpu > 1 else 1))
    # "Maximum" means maximum *safe sustained* throughput, not reckless CPU
    # saturation. Keep capacity for the Engine relationship builder, OS/Qt, and
    # SQLite commit bursts; use up to eight isolated persistent analyzers on
    # higher-core machines.
    reserve = 2 if cpu >= 6 else 1
    return max(1, min(8, cpu - reserve))


class LibraryEngineService:
    """Long-lived owner of library scanning, deep analysis and relationship prep.

    This service deliberately contains no Qt. Library Manager is a dashboard;
    MIDI Remixer is a reader/creative client. Closing either UI does not cancel
    committed or queued library work unless the user explicitly pauses/stops it.
    """

    def __init__(self, app_root: str | Path) -> None:
        self.app_root = Path(app_root).resolve()
        self.paths = AppPaths.for_app(self.app_root).ensure()
        migration = self.paths.migrate_persistent_library_databases()
        migration_errors = [str(item) for item in migration.get("errors", []) if str(item).strip()]
        if migration_errors:
            raise RuntimeError(
                "Persistent MIDI Library migration failed; startup stopped to protect existing user data. "
                "Legacy database files were retained for retry. " + " | ".join(migration_errors[:4])
            )
        self.settings = LibraryEngineSettings(self.app_root)
        # Startup preference and current-session Deep intent are deliberately
        # separate. The checkbox controls a *new Engine lifetime*; changing it
        # must not unexpectedly pause/resume Deep work already running now.
        self._deep_session_resume_allowed = bool(self.settings.get("manager_auto_start_analysis", True))
        self._lock = threading.RLock()
        self._source_availability_lock = threading.RLock()
        self._source_accessible_roots: tuple[str, ...] = ()
        self._source_unavailable_roots: tuple[str, ...] = ()
        # Administrative/destructive commands may arrive concurrently because
        # the Engine API is intentionally multi-client/threaded.  Status reads
        # stay available, but only one maintenance mutation may run at a time.
        self._maintenance_gate = threading.Lock()
        self._maintenance_state = ""
        self._stop = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._relationship_thread: threading.Thread | None = None
        self._relationship_cancel = threading.Event()
        self._relationship_priority_paths: set[str] = set()
        self._relationship_restart_requested = False
        self._watch_dirty = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._analysis_workers_idle_since: float | None = None
        self._last_progress = ScanProgress("idle", 0, 0, "", "Library Engine ready")
        self._last_deep_path = ""
        self._last_deep_state = "idle"
        # Explicit user-control transition published through status so Qt/Windows QA
        # and the UI can distinguish a bounded pause/cancel boundary from steady
        # running/paused state. This also makes Close-during-transition testable.
        self._deep_transition_state = ""
        # Completion timestamps provide a live throughput/ETA estimate that
        # survives Manager close/reopen because it belongs to the Engine.
        self._deep_completion_times: deque[float] = deque(maxlen=512)
        self._recent_errors: deque[str] = deque(maxlen=20)
        if self.settings.recovery_notice:
            self._recent_errors.append(self.settings.recovery_notice)
        self._started_monotonic = time.monotonic()
        self._graph_summary: dict[str, int] = {"edges": 0, "providers": 0, "feedback_pairs": 0}
        self._deep_completed_since_relationship = 0
        # One authoritative Fast-operation state is published to every UI.
        # Browser/Manager must never infer RUNNING/FINALIZING independently.
        self._scan_operation_kind = "idle"
        self._scan_operation_state = "idle"
        # Explicit control-plane transition survives a very fast worker exit,
        # so Pause/Cancel and Windows qualification do not infer lifecycle from
        # thread scheduling. This mirrors `_deep_transition_state`.
        self._scan_transition_state = ""
        self._scan_worker_limit = 0
        self._scan_recovery_policy: DeepAnalysisPolicy | None = None
        mode = str(self.settings.get("throughput_mode", "maximum") or "maximum").casefold()
        if mode not in THROUGHPUT_MODES:
            mode = "maximum"
        self.throughput_mode = mode
        self.worker_count = recommended_worker_count(mode)
        self.library_index = LibraryIndex(self.paths.library / "library_index.sqlite3")
        self.deep_pool = DeepAnalysisProcessPool(
            self.app_root,
            self.library_index.database_path,
            self.paths.library / "library_scan.sqlite3",
            workers=self.worker_count,
            process_priority="normal" if self.throughput_mode == "maximum" else "background",
        )
        self.scan = LibraryScanService(
            self.paths.library / "library_scan.sqlite3",
            deep_analyzer=self._analyze_one,
            on_progress=self._on_scan_progress,
            on_deep_state=self._on_deep_state,
            on_quick_commit=self._on_quick_commit,
            deep_worker_count=self.worker_count,
            quick_scanner=self.deep_pool.quick_scan,
            quick_worker_count=self.worker_count,
        )
        self.relationships = RelationshipStore(self.paths.library / "library_relationships.sqlite3")
        self._candidate_index: CandidateIndex | None = None
        self._candidate_summaries: list[object] = []
        self._candidate_title_map: dict[str, tuple[object, ...]] = {}
        self._candidate_family: dict[str, str] = {}
        # Deep completion must find the previously indexed representative for
        # one family without scanning every candidate accumulated so far.
        self._candidate_indexed_family_paths: dict[str, set[str]] = {}
        self._candidate_fingerprint: dict[str, str] = {}
        self._guided_ready_families: set[str] = set()
        self._assisted_ready_families: set[str] = set()
        self._guided_ready_family_count = 0
        self._assisted_ready_family_count = 0
        self._index_revision = 0
        self._catalogue_revision = 0
        # Small committed change journal lets an attached Remixer update a few
        # changed rows without rehydrating the entire 96K catalogue. Large/reset
        # changes deliberately request one full refresh instead.
        self._catalogue_deltas: deque[dict[str, object]] = deque(maxlen=64)
        self._watcher = RecursiveDirectoryWatcher(self._on_filesystem_change)
        # The daemon must publish its endpoint and answer ping/status before any
        # potentially expensive 96K-library startup work begins.  Older builds
        # performed reference import / catalogue startup inside __init__, so the
        # Manager could time out and report a dead Engine even though the process
        # was simply busy initializing.
        self._startup_lock = threading.RLock()
        self._startup_thread: threading.Thread | None = None
        self._startup_state = "constructed"
        self._startup_error = ""
        self._background_started = False
        self._family_reconciliation_state = "idle"
        self._family_reconciliation_current = 0
        self._family_reconciliation_total = 0
        self._family_reconciliation_changed = 0
        self._family_reconciliation_last_result: dict[str, object] = {}
        # Expensive aggregate counts do not belong on the heartbeat/status
        # request path.  They are refreshed in the Engine background and status
        # returns the most recent committed snapshot immediately.
        self._status_metrics_lock = threading.RLock()
        self._status_metrics_updated = 0.0
        self._status_metrics_refresh_thread: threading.Thread | None = None
        self._status_metrics: dict[str, Any] = {
            "catalogue": {
                "total": 0, "catalogued": 0, "failed": 0, "identified": 0,
                "probable_identity": 0, "unidentified": 0, "resource_limited": 0,
                "distinct_families": 0, "exact_duplicates": 0,
                "likely_duplicates": 0, "suspicious": 0, "no_basic_health_flag": 0,
            },
            "deep_counts": {
                "pending": 0, "paused": 0, "analyzing": 0, "complete": 0,
                "failed": 0, "total": 0, "remaining": 0, "finished": 0,
            },
            "deep_health": {"total": 0, "failed": 0, "attempts": 0, "average_seconds": 0.0},
            "readiness": {"total": 0, "guided_ready": 0, "assisted_ready": 0},
            "relationships": {
                "count": 0, "guided_count": 0, "assisted_count": 0,
                "anchors_built": 0, "assisted_anchors_built": 0,
            },
        }

    def start_background(self) -> bool:
        """Start nonessential Engine initialization after the API is listening.

        This method is deliberately idempotent.  The local IPC endpoint can be
        written first, making heartbeat/status responsive while watcher setup,
        reference evidence, incremental inventory, Detailed Analysis, and
        relationship preparation start on an Engine-owned background thread.
        """
        with self._startup_lock:
            if self._background_started:
                return False
            self._background_started = True
            self._startup_state = "starting"
            self._startup_error = ""

            def bootstrap() -> None:
                try:
                    if self._stop.is_set():
                        return
                    self._startup_state = "migrating lifecycle"
                    # Contract-15 migration is derived entirely from persisted
                    # Fast/Deep/profile facts. It does not rescan source files.
                    # This prevents an upgraded 96K Library from temporarily
                    # exposing an empty recommendation pool simply because its
                    # newly-added lifecycle columns began as unresolved.
                    self.library_index.backfill_readiness_summaries(
                        cancel_callback=self._stop.is_set
                    )
                    if self._stop.is_set():
                        return
                    self._backfill_lifecycle_from_persisted_analysis()
                    if self._stop.is_set():
                        return
                    self._startup_state = "reconciling composition families"
                    self._reconcile_persisted_family_identity()
                    if self._stop.is_set():
                        return
                    self._startup_state = "watching"
                    self._configure_watcher()
                    self._start_watch_coordinator()
                    self._schedule_status_metrics_refresh(force=True)
                    if self._stop.is_set():
                        return
                    self._startup_state = "loading reference evidence"
                    self.refresh_reference_evidence(invalidate_relationships=False)
                    if self._stop.is_set():
                        return
                    # Engine startup always performs the cheap incremental
                    # inventory before resuming bulk Detailed Analysis. This
                    # prevents changed source MIDI from being deeply analyzed
                    # against stale quick-catalogue metadata while preserving
                    # the last committed catalogue/recommendations for readers.
                    self._startup_state = "checking library changes"
                    sources_ready = self._all_configured_roots_accessible()
                    background_requested = (
                        sources_ready
                        and self._deep_session_resume_allowed
                        and self.scan.current_policy() == DeepAnalysisPolicy.BACKGROUND
                    )
                    initial_scan_started = self.start_incremental_scan() if sources_ready else False
                    if not initial_scan_started and not self._stop.is_set():
                        if background_requested:
                            self.scan.resume_deep_analysis()
                        self._start_relationship_builder()
                    self._startup_state = "ready"
                except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as exc:
                    self._startup_error = f"{type(exc).__name__}: {exc}"
                    self._startup_state = "degraded"
                    self._record_error(f"engine startup: {self._startup_error}")
                finally:
                    with self._startup_lock:
                        if self._startup_thread is threading.current_thread():
                            self._startup_thread = None

            thread = threading.Thread(target=bootstrap, name="MidiLibraryEngineBootstrap", daemon=True)
            self._startup_thread = thread
            thread.start()
            return True

    def _schedule_status_metrics_refresh(self, *, force: bool = False) -> None:
        """Refresh slow aggregate metrics away from socket request threads."""
        if self._stop.is_set():
            return
        now = time.monotonic()
        with self._status_metrics_lock:
            if not force and self._status_metrics_updated and now - self._status_metrics_updated < STATUS_METRICS_MAX_AGE_SECONDS:
                return
            if self._status_metrics_refresh_thread is not None and self._status_metrics_refresh_thread.is_alive():
                return

            def refresh() -> None:
                updates: dict[str, Any] = {}
                errors: list[str] = []
                try:
                    updates["catalogue"] = self.scan.database.catalogue_counts()
                except (sqlite3.Error, OSError, RuntimeError) as exc:
                    errors.append(f"catalogue status: {type(exc).__name__}: {exc}")
                try:
                    updates["deep_counts"] = self.scan.database.deep_status_counts()
                except (sqlite3.Error, OSError, RuntimeError) as exc:
                    errors.append(f"deep status: {type(exc).__name__}: {exc}")
                try:
                    updates["deep_health"] = self.scan.database.deep_health_stats()
                except (sqlite3.Error, OSError, RuntimeError) as exc:
                    errors.append(f"deep health: {type(exc).__name__}: {exc}")
                try:
                    updates["readiness"] = self.library_index.readiness_summary()
                except (sqlite3.Error, OSError, RuntimeError) as exc:
                    errors.append(f"readiness status: {type(exc).__name__}: {exc}")
                try:
                    updates["lifecycle"] = self.scan.database.lifecycle_counts()
                except (sqlite3.Error, OSError, RuntimeError) as exc:
                    errors.append(f"lifecycle status: {type(exc).__name__}: {exc}")
                try:
                    updates["relationships"] = {
                        "count": self.relationships.count(),
                        "guided_count": self.relationships.count(mode="guided"),
                        "assisted_count": self.relationships.count(mode="assisted"),
                        "anchors_built": self.relationships.built_anchor_count(mode="guided"),
                        "assisted_anchors_built": self.relationships.built_anchor_count(mode="assisted"),
                    }
                except (sqlite3.Error, OSError, RuntimeError) as exc:
                    errors.append(f"relationship status: {type(exc).__name__}: {exc}")
                with self._status_metrics_lock:
                    for key, value in updates.items():
                        self._status_metrics[key] = dict(value) if isinstance(value, dict) else value
                    if updates:
                        self._status_metrics_updated = time.monotonic()
                    if self._status_metrics_refresh_thread is threading.current_thread():
                        self._status_metrics_refresh_thread = None
                for error in errors:
                    self._record_error(error)

            thread = threading.Thread(target=refresh, name="MidiLibraryStatusMetrics", daemon=True)
            self._status_metrics_refresh_thread = thread
            thread.start()

    def _invalidate_status_metrics(self) -> None:
        with self._status_metrics_lock:
            self._status_metrics_updated = 0.0

    def _status_metrics_snapshot(self) -> tuple[dict[str, Any], float]:
        self._schedule_status_metrics_refresh()
        now = time.monotonic()
        with self._status_metrics_lock:
            snapshot = {
                key: dict(value) if isinstance(value, dict) else value
                for key, value in self._status_metrics.items()
            }
            updated = float(self._status_metrics_updated or 0.0)
        age = max(0.0, now - updated) if updated else -1.0
        return snapshot, age

    def configured_roots(self) -> list[str]:
        return self.settings.roots()

    def _on_scan_progress(self, progress: ScanProgress) -> None:
        with self._lock:
            self._last_progress = progress

    def _on_quick_commit(self, paths: tuple[str, ...]) -> None:
        """Publish newly committed Fast catalogue rows to attached clients.

        The quick database remains authoritative. This journal is only a cheap
        notification path so Remixer can hydrate useful results during a long
        first scan without forcing a full 96K reload on every batch.

        Failed-file recovery is deliberately different from a normal Fast scan.
        Its input rows were persisted as Fast failures, so they cannot own valid
        prepared relationships.  Do *not* synchronously enter the relationship
        store at each recovery commit.  The relationship builder may still be
        finishing an atomic DB write after its cancellation request; waiting for
        that lock at the first 16-row commit used to pin recovery at exactly 16.
        Recovery refreshes the candidate/recommendation view once, after its Fast
        pass reaches a terminal boundary.
        """
        if not paths:
            return
        self._invalidate_status_metrics()
        self._record_catalogue_delta(paths, ())
        with self._lock:
            recovery_mode = self._scan_operation_kind == "fast_recovery"
        if recovery_mode:
            return
        # A committed changed/new Fast row invalidates relationships made from
        # its previous content immediately. Do this at the same commit boundary
        # so Remixer never consumes stale prepared rows while the rest of a long
        # scan is still running. Early bootstrap relationships built afterward
        # are therefore valid and are not thrown away again at scan completion.
        self.relationships.delete_paths(paths)

    def _on_deep_state(self, path: str, state: str) -> None:
        # Do not invalidate the expensive aggregate status cache per MIDI. At
        # 96K scale a normal Manager poll would otherwise launch near-continuous
        # COUNT/GROUP BY queries while analyzer workers are trying to commit
        # lifecycle truth. Runtime queue state and completion-rate fields remain
        # live; aggregate counts refresh on their bounded 1.5-second cadence.
        with self._lock:
            self._last_deep_path = str(path)
            self._last_deep_state = str(state)
            if state == "complete":
                self._deep_completed_since_relationship += 1
                self._deep_completion_times.append(time.monotonic())
        if state == "failed":
            error = self.scan.database.deep_error(path) or self.library_index.error_for(path) or ""
            self.scan.database.mark_deep_failure_lifecycle(path, error)
            return
        if state == "complete":
            self.scan.database.mark_analyzed(path)
            previous_family = self.scan.database.family_id_for_path(path)
            reconciled_family = reconcile_completed_path_family(
                self.scan.database, self.library_index, path, stop=self._stop.is_set
            )
            self._bless_completed_path(path)
            if reconciled_family:
                self._reclassify_families([reconciled_family])
            if previous_family and reconciled_family and previous_family != reconciled_family:
                # Family membership changed after deep identity. Prepared rows and
                # the compact candidate snapshot derived from the old family are
                # no longer authoritative. They will be rebuilt from persisted
                # lifecycle truth at the next relationship boundary.
                affected = set(self.scan.database.family_paths(reconciled_family))
                self.relationships.delete_paths(affected)
                with self._lock:
                    self._candidate_index = None
                    self._candidate_summaries = []
                    self._candidate_title_map = {}
                    self._candidate_family = {}
                    self._candidate_indexed_family_paths = {}
                    self._guided_ready_families = set()
                    self._assisted_ready_families = set()
                    self._guided_ready_family_count = 0
                    self._assisted_ready_family_count = 0
                    self._index_revision += 1
            else:
                self._sync_candidate_family_snapshot(path)
            runtime = self.scan.deep_runtime_state()
            final_deep_boundary = bool(
                int(runtime.get("queue_depth") or 0) == 0
                and int(runtime.get("queue_seeding") or 0) == 0
                # This completion callback may still count as the one active
                # worker until it returns to the queue coordinator.
                and int(runtime.get("queue_active") or 0) <= 1
            )
            with self._lock:
                priority_ready = str(path) in self._relationship_priority_paths
                batch_ready = self._deep_completed_since_relationship >= RELATIONSHIP_DEEP_COMPLETION_BATCH
                if priority_ready and self._relationship_thread is not None and self._relationship_thread.is_alive():
                    self._relationship_restart_requested = True
                if batch_ready or final_deep_boundary:
                    self._deep_completed_since_relationship = 0
            if priority_ready or batch_ready or final_deep_boundary:
                self._start_relationship_builder()

    @staticmethod
    def _lifecycle_assignments(
        rows: list[dict[str, object]], readiness: dict[str, dict[str, object]]
    ) -> list[tuple[str, str]]:
        """Resolve analyzed arrangements into deterministic persisted eligibility.

        One best-readiness arrangement represents each composition family in the
        normal mash pool. Same-composition alternatives are routed to Fusion;
        exact duplicates and musically valid but unsuitable arrangements remain
        usable without polluting Guided/Assisted. Rows without persisted
        readiness remain unresolved and are deliberately not blessed.
        """
        completed = [
            row for row in rows
            if str(row.get("deep_status") or "") == "complete"
            and not clearly_no_usable_content(
                track_count=int(row.get("track_count", 1) or 0),
                note_count=int(row.get("note_count", 64) or 0),
                duration_seconds=row.get("duration_seconds", 60.0),
            )
        ]
        groups: dict[str, list[dict[str, object]]] = {}
        for row in completed:
            path = str(row.get("path") or "")
            if not path or path not in readiness:
                continue
            family = str(row.get("family_id") or f"path:{path}")
            groups.setdefault(family, []).append(row)

        assignments: list[tuple[str, str]] = []
        for members in groups.values():
            def score(row: dict[str, object]) -> tuple[int, int, int, int, str]:
                path = str(row.get("path") or "")
                ready = readiness.get(path, {})
                return (
                    int(bool(ready.get("guided_eligible"))),
                    int(ready.get("guided_confidence") or 0),
                    int(bool(ready.get("assisted_eligible"))),
                    int(ready.get("assisted_confidence") or 0),
                    path.casefold(),
                )

            representative = max(members, key=score)
            representative_path = str(representative.get("path") or "")
            family_has_versions = any(str(row.get("duplicate_kind") or "") == "same_composition" for row in members)
            for row in members:
                path = str(row.get("path") or "")
                ready = readiness[path]
                duplicate_kind = str(row.get("duplicate_kind") or "none")
                health_reasons = quick_health_reasons(
                    track_count=int(row.get("track_count", 1) or 0),
                    note_count=int(row.get("note_count", 64) or 0),
                    duration_seconds=row.get("duration_seconds", 60.0),
                )
                if health_reasons:
                    eligibility = EligibilityState.USABLE_NOT_MASH.value
                elif path != representative_path and duplicate_kind == "exact":
                    eligibility = EligibilityState.USABLE_NOT_MASH.value
                elif path != representative_path and duplicate_kind == "same_composition":
                    eligibility = EligibilityState.FUSION_CANDIDATE.value
                elif bool(ready.get("guided_eligible")) or bool(ready.get("assisted_eligible")):
                    eligibility = EligibilityState.MASH_READY.value
                elif family_has_versions:
                    eligibility = EligibilityState.FUSION_CANDIDATE.value
                else:
                    eligibility = EligibilityState.USABLE_NOT_MASH.value
                assignments.append((path, eligibility))
        return assignments

    def _backfill_lifecycle_from_persisted_analysis(self) -> int:
        """Upgrade existing analyzed rows into the lifecycle contract without rescanning."""
        rows = self.scan.database.lifecycle_backfill_rows()
        if not rows or self._stop.is_set():
            return 0
        readiness = self.library_index.readiness_map()
        if self._stop.is_set():
            return 0
        rejected = [
            (str(row.get("path") or ""), RejectionCode.NO_USABLE_CONTENT.value, "Empty/no usable musical content")
            for row in rows
            if str(row.get("deep_status") or "") == "complete"
            and clearly_no_usable_content(
                track_count=int(row.get("track_count", 1) or 0),
                note_count=int(row.get("note_count", 64) or 0),
                duration_seconds=row.get("duration_seconds", 60.0),
            )
        ]
        rejected_count = self.scan.database.set_rejected_many(rejected)
        assignments = self._lifecycle_assignments(rows, readiness)
        changed = rejected_count + self.scan.database.set_blessed_eligibilities(assignments)
        if changed:
            self._invalidate_status_metrics()
        return changed

    @staticmethod
    def _readiness_rank(path: str, ready: dict[str, object]) -> tuple[int, int, int, int, str]:
        """The persisted representative ordering used by lifecycle assignment."""
        return (
            int(bool(ready.get("guided_eligible"))),
            int(ready.get("guided_confidence") or 0),
            int(bool(ready.get("assisted_eligible"))),
            int(ready.get("assisted_confidence") or 0),
            str(path).casefold(),
        )

    @staticmethod
    def _alternate_family_eligibility(row: dict[str, object], *, family_has_versions: bool) -> str:
        if quick_health_reasons(
            track_count=int(row.get("track_count", 1) or 0),
            note_count=int(row.get("note_count", 64) or 0),
            duration_seconds=row.get("duration_seconds", 60.0),
        ):
            return EligibilityState.USABLE_NOT_MASH.value
        duplicate_kind = str(row.get("duplicate_kind") or "none")
        if duplicate_kind == "exact":
            return EligibilityState.USABLE_NOT_MASH.value
        if duplicate_kind == "same_composition" or family_has_versions:
            return EligibilityState.FUSION_CANDIDATE.value
        return EligibilityState.USABLE_NOT_MASH.value

    def _family_eligibility_assignments(self, family_id: str) -> list[tuple[str, str]]:
        """Classify one reconciled composition family from authoritative Deep profiles."""
        rows = self.scan.database.family_rows(family_id, completed_only=True)
        if not rows:
            return []
        paths = [str(row.get("path") or "") for row in rows if str(row.get("path") or "")]
        profiles = self.library_index.load_many(paths, validate_sources=False)
        ranked: list[tuple[tuple[int, int, int, int, str], str]] = []
        readiness: dict[str, dict[str, object]] = {}
        base: dict[str, str] = {}
        for row in rows:
            path = str(row.get("path") or "")
            if not path:
                continue
            if quick_health_reasons(
                track_count=int(row.get("track_count", 1) or 0),
                note_count=int(row.get("note_count", 64) or 0),
                duration_seconds=row.get("duration_seconds", 60.0),
            ):
                base[path] = EligibilityState.USABLE_NOT_MASH.value
                continue
            ready = self.library_index.readiness_for_path(path)
            readiness[path] = ready or {}
            if ready and (ready.get("guided_eligible") or ready.get("assisted_eligible")):
                ranked.append((self._readiness_rank(path, ready), path))
            else:
                base[path] = EligibilityState.USABLE_NOT_MASH.value

        if not ranked:
            return [(path, base.get(path, EligibilityState.USABLE_NOT_MASH.value)) for path in paths]

        _rank, winner_path = max(ranked, key=lambda item: item[0])
        winner_profile = profiles.get(winner_path)
        assignments: list[tuple[str, str]] = []
        for path in paths:
            if path == winner_path:
                assignments.append((path, EligibilityState.MASH_READY.value))
                continue
            if path in base:
                assignments.append((path, base[path]))
                continue
            row = next((item for item in rows if str(item.get("path") or "") == path), {})
            if str(row.get("duplicate_kind") or "none") == "exact":
                assignments.append((path, EligibilityState.USABLE_NOT_MASH.value))
                continue
            profile = profiles.get(path)
            if winner_profile is not None and profile is not None and authoritative_same_composition(winner_profile, profile):
                assignments.append((
                    path,
                    EligibilityState.FUSION_CANDIDATE.value
                    if materially_different_arrangement(winner_profile, profile)
                    else EligibilityState.USABLE_NOT_MASH.value,
                ))
            else:
                # A reconciled family should normally satisfy the deep identity
                # predicate. Missing/corrupt profile state fails closed rather
                # than inventing a Fusion candidate.
                assignments.append((path, EligibilityState.USABLE_NOT_MASH.value))
        return assignments

    def _reclassify_families(self, family_ids: Iterable[str]) -> int:
        states: list[tuple[str, str]] = []
        for family_id in sorted({str(value) for value in family_ids if str(value).strip()}):
            if self._stop.is_set():
                break
            states.extend(self._family_eligibility_assignments(family_id))
        changed = self.scan.database.set_blessed_eligibilities(states)
        if changed:
            self._invalidate_status_metrics()
        return changed

    def _reconcile_persisted_family_identity(self) -> None:
        """Apply the current deep identity ruleset once without rescanning MIDI files."""
        from core.song_identity import IDENTITY_RULESET_VERSION

        current = int(self.scan.database.get_scan_setting("family_identity_ruleset", "0") or 0)
        if current >= IDENTITY_RULESET_VERSION or self._stop.is_set():
            self._family_reconciliation_state = "complete"
            return
        self._family_reconciliation_state = "running"
        self._family_reconciliation_current = 0
        self._family_reconciliation_total = 0

        def progress(current_value: int, total_value: int, _message: str) -> None:
            self._family_reconciliation_current = int(current_value)
            self._family_reconciliation_total = int(total_value)

        result = reconcile_persisted_families(
            self.scan.database, self.library_index, stop=self._stop.is_set, progress=progress
        )
        if self._stop.is_set():
            self._family_reconciliation_state = "cancelled"
            return
        reclassified = self._reclassify_families(result.family_ids_to_reclassify)
        # Prepared relationships are derived from family identity.  A ruleset
        # upgrade must never leave old same-song pairs addressable merely because
        # their relationship rows were created before family reconciliation.
        # Rebuild them lazily from the newly persisted Mash/Fusion truth; no source
        # MIDI files are touched.
        self.relationships.clear_derived()
        with self._lock:
            self._candidate_index = None
            self._candidate_summaries = []
            self._candidate_title_map = {}
            self._candidate_family = {}
            self._candidate_indexed_family_paths = {}
            self._candidate_fingerprint = {}
            self._guided_ready_families = set()
            self._assisted_ready_families = set()
            self._guided_ready_family_count = 0
            self._assisted_ready_family_count = 0
            self._index_revision += 1
        self.scan.database.set_scan_setting("family_identity_ruleset", str(IDENTITY_RULESET_VERSION))
        self._family_reconciliation_changed = int(result.changed_paths)
        self._family_reconciliation_last_result = {
            "ruleset": int(result.ruleset),
            "deep_profiles": int(result.deep_profiles),
            "candidate_groups": int(result.candidate_groups),
            "compared_pairs": int(result.compared_pairs),
            "changed_paths": int(result.changed_paths),
            "merged_families": int(result.merged_family_count),
            "split_families": int(result.split_family_count),
            "reclassified_rows": int(reclassified),
        }
        self._family_reconciliation_state = "complete"

    def _bless_completed_path(self, path: str | Path) -> str:
        """Resolve one completed MIDI without expanding its entire family.

        Older code loaded every member/readiness row for the family on every Deep
        completion. A 96K duplicate family therefore made one completion O(96K)
        and trapped Deep workers outside their preemptible analyzer boundary. The
        persisted Mash representative is the sufficient statistic for normal
        incremental completion: compare the new row with that representative and
        update at most those rows. Full-family reconciliation remains available in
        the one-time migration/backfill path where O(N) work is intentional.
        """
        value = str(Path(path).expanduser().resolve(strict=False))
        quick = self.scan.database.load_result(value, validate_source=False)
        if quick is None:
            return EligibilityState.NEEDS_REVIEW.value

        current_row: dict[str, object] = {
            "path": quick.path,
            "family_id": quick.family_id,
            "duplicate_kind": str(
                getattr(getattr(quick, "duplicate_kind", None), "value", getattr(quick, "duplicate_kind", "none"))
            ),
            "deep_status": self.scan.database.deep_status(quick.path),
            "track_count": quick.track_count,
            "note_count": quick.note_count,
            "duration_seconds": quick.duration_seconds,
        }
        if clearly_no_usable_content(
            track_count=int(current_row.get("track_count", 1) or 0),
            note_count=int(current_row.get("note_count", 64) or 0),
            duration_seconds=current_row.get("duration_seconds", 60.0),
        ):
            self.scan.database.set_rejected(
                value, RejectionCode.NO_USABLE_CONTENT.value, "Empty/no usable musical content"
            )
            return EligibilityState.REJECTED.value

        ready = self.library_index.readiness_for_path(value)
        if ready is None:
            return EligibilityState.UNRESOLVED.value

        family_id = str(quick.family_id or "")
        family_has_versions = bool(
            family_id and self.scan.database.family_has_same_composition_versions(family_id)
        )
        current_healthy = not quick_health_reasons(
            track_count=int(current_row.get("track_count", 1) or 0),
            note_count=int(current_row.get("note_count", 64) or 0),
            duration_seconds=current_row.get("duration_seconds", 60.0),
        )
        current_eligible = bool(
            current_healthy and (ready.get("guided_eligible") or ready.get("assisted_eligible"))
        )

        # Normally there is exactly one persisted Mash representative. Reading
        # all such rows also repairs a historical/partial state that accidentally
        # contains more than one, without touching the rest of a large family.
        existing = self.scan.database.family_mash_ready_rows(family_id) if family_id else []
        existing = [row for row in existing if str(row.get("path") or "") != value]
        assignments: list[tuple[str, str]] = []
        ranked: list[tuple[tuple[int, int, int, int, str], dict[str, object]]] = []

        for row in existing:
            existing_path = str(row.get("path") or "")
            existing_ready = self.library_index.readiness_for_path(existing_path)
            if existing_ready is None or not (
                existing_ready.get("guided_eligible") or existing_ready.get("assisted_eligible")
            ):
                # A stale/missing current-analysis readiness row can never remain
                # authoritative merely because its old lifecycle row said Mash.
                assignments.append((
                    existing_path,
                    self._alternate_family_eligibility(row, family_has_versions=family_has_versions),
                ))
                continue
            ranked.append((self._readiness_rank(existing_path, existing_ready), row))

        if current_eligible:
            ranked.append((self._readiness_rank(value, ready), current_row))

        if ranked:
            _rank, winner = max(ranked, key=lambda item: item[0])
            winner_path = str(winner.get("path") or "")
            for _candidate_rank, row in ranked:
                candidate_path = str(row.get("path") or "")
                assignments.append((
                    candidate_path,
                    EligibilityState.MASH_READY.value if candidate_path == winner_path else
                    self._alternate_family_eligibility(row, family_has_versions=family_has_versions),
                ))
            if not current_eligible:
                assignments.append((
                    value, self._alternate_family_eligibility(current_row, family_has_versions=family_has_versions)
                ))
        else:
            assignments.append((
                value, self._alternate_family_eligibility(current_row, family_has_versions=family_has_versions)
            ))

        # De-duplicate paths in case a historical multi-Mash state was repaired.
        final: dict[str, str] = {}
        for candidate_path, eligibility in assignments:
            if candidate_path:
                final[candidate_path] = eligibility
        self.scan.database.set_blessed_eligibilities(final.items())
        lifecycle = self.scan.database.lifecycle_for_path(value) or {}
        return str(lifecycle.get("eligibility_state") or EligibilityState.UNRESOLVED.value)

    def _sync_candidate_family_snapshot(self, path: str | Path) -> None:
        """Incrementally synchronize one composition family's Mash representative.

        Deep completion happens continuously in a large Library. The candidate
        snapshot built at the end of Fast Scan may contain zero deeply-ready
        rows, so relationship preparation must absorb newly Blessed material
        without rehydrating all 96K quick rows every few completions. Only the
        current persisted Mash representative(s) for this family can enter the
        normal recommendation index.
        """
        value = str(Path(path).expanduser().resolve(strict=False))
        quick = self.scan.database.load_result(value, validate_source=False)
        if quick is None:
            return
        family_id = str(quick.family_id or f"path:{value}")
        mash_rows = self.scan.database.family_mash_ready_rows(str(quick.family_id or "")) if quick.family_id else []
        winner_paths = {str(row.get("path") or "") for row in mash_rows if str(row.get("path") or "")}
        if not quick.family_id:
            lifecycle = self.scan.database.lifecycle_for_path(value) or {}
            if str(lifecycle.get("eligibility_state") or "") == EligibilityState.MASH_READY.value:
                winner_paths.add(value)

        changed = False
        removed_any = False
        with self._lock:
            indexed_family_paths = set(self._candidate_indexed_family_paths.get(family_id, set()))
            index = self._candidate_index
            for old_path in indexed_family_paths - winner_paths:
                if index is not None:
                    index.remove_path(old_path)
                self._candidate_summaries = [
                    summary for summary in self._candidate_summaries
                    if str(getattr(summary, "path", "")) != old_path
                ]
                self._candidate_family.pop(old_path, None)
                self._candidate_fingerprint.pop(old_path, None)
                family_members = self._candidate_indexed_family_paths.get(family_id)
                if family_members is not None:
                    family_members.discard(old_path)
                    if not family_members:
                        self._candidate_indexed_family_paths.pop(family_id, None)
                removed_any = True
                changed = True

            existing_index_paths = set(index.by_path) if index is not None else set()
            # New winners enter immediately.  If the just-completed path was
            # already the representative (for example after a real source-file
            # change/reanalysis), refresh its summary/fingerprint in place so
            # stale relationship rows cannot remain current by accident.
            refresh_paths = set(winner_paths - existing_index_paths)
            if value in winner_paths:
                refresh_paths.add(value)
            for winner_path in sorted(refresh_paths):
                winner = self.scan.database.load_result(winner_path, validate_source=False)
                if winner is None:
                    continue
                summary = summary_from_quick(winner)
                old_summary = next(
                    (item for item in self._candidate_summaries if str(getattr(item, "path", "")) == winner_path),
                    None,
                )
                old_family = self._candidate_family.get(winner_path)
                old_fingerprint = self._candidate_fingerprint.get(winner_path, "")
                if self._candidate_index is None:
                    self._candidate_index = CandidateIndex([summary])
                    index = self._candidate_index
                else:
                    self._candidate_index.upsert_profile(summary)
                if old_summary is None:
                    self._candidate_summaries.append(summary)
                elif old_summary != summary:
                    self._candidate_summaries = [
                        summary if str(getattr(item, "path", "")) == winner_path else item
                        for item in self._candidate_summaries
                    ]
                    for title_key in DiscoveryGraph.profile_title_map([old_summary]):
                        remaining = tuple(
                            item for item in self._candidate_title_map.get(title_key, ())
                            if str(getattr(item, "path", "")) != winner_path
                        )
                        if remaining:
                            self._candidate_title_map[title_key] = remaining
                        else:
                            self._candidate_title_map.pop(title_key, None)
                self._candidate_family[winner_path] = family_id
                new_fingerprint = winner.sha256 or winner.musical_fingerprint or ""
                self._candidate_fingerprint[winner_path] = new_fingerprint
                self._candidate_indexed_family_paths.setdefault(family_id, set()).add(winner_path)
                for title_key, profiles in DiscoveryGraph.profile_title_map([summary]).items():
                    existing = tuple(self._candidate_title_map.get(title_key, ()))
                    if not any(str(getattr(item, "path", "")) == winner_path for item in existing):
                        self._candidate_title_map[title_key] = (*existing, *profiles)
                if old_summary != summary or old_family != family_id or old_fingerprint != new_fingerprint:
                    changed = True

            if removed_any:
                # Representative switches are uncommon; rebuilding only the small
                # normalized-title resolver prevents a demoted version from
                # surviving evidence lookup without paying a 96K quick-row read.
                self._candidate_title_map = DiscoveryGraph.profile_title_map(self._candidate_summaries)

            self._guided_ready_families.discard(family_id)
            self._assisted_ready_families.discard(family_id)
            for winner_path in winner_paths:
                ready = self.library_index.readiness_for_path(winner_path) or {}
                if bool(ready.get("guided_eligible")):
                    self._guided_ready_families.add(family_id)
                if bool(ready.get("assisted_eligible")):
                    self._assisted_ready_families.add(family_id)
            self._guided_ready_family_count = len(self._guided_ready_families)
            self._assisted_ready_family_count = len(self._assisted_ready_families)
            if changed:
                self._index_revision += 1

    def _analyze_one(self, path: Path) -> None:
        force = not bool(self.settings.get("library_use_analysis_cache", True))
        self.deep_pool.analyze(path, force=force)

    def _on_filesystem_change(self, _root: str) -> None:
        self._watch_dirty.set()

    def _refresh_source_availability(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        accessible: list[str] = []
        unavailable: list[str] = []
        for root in self.configured_roots():
            try:
                available = Path(root).is_dir()
            except OSError:
                available = False
            (accessible if available else unavailable).append(root)
        with self._source_availability_lock:
            self._source_accessible_roots = tuple(accessible)
            self._source_unavailable_roots = tuple(unavailable)
        return tuple(accessible), tuple(unavailable)

    def _source_availability_snapshot(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self._source_availability_lock:
            return tuple(self._source_accessible_roots), tuple(self._source_unavailable_roots)

    def _accessible_configured_roots(self) -> list[str]:
        accessible, _unavailable = self._refresh_source_availability()
        return list(accessible)

    def _all_configured_roots_accessible(self) -> bool:
        configured = self.configured_roots()
        if not configured:
            self._refresh_source_availability()
            return False
        accessible, unavailable = self._refresh_source_availability()
        return len(accessible) == len(configured) and not unavailable

    @staticmethod
    def _path_under_roots(path: str | Path, roots: Iterable[str | Path]) -> bool:
        try:
            candidate = os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
        except (OSError, ValueError, TypeError):
            return False
        for raw_root in roots:
            try:
                root = os.path.normcase(str(Path(raw_root).expanduser().resolve(strict=False)))
                if os.path.commonpath([candidate, root]) == root:
                    return True
            except (OSError, ValueError, TypeError):
                continue
        return False

    def _path_in_accessible_source(self, path: str | Path) -> bool:
        return self._path_under_roots(path, self._accessible_configured_roots())

    def _configure_watcher(self) -> None:
        self._watcher.stop()
        roots = self._accessible_configured_roots()
        if roots and bool(self.settings.get("library_watch_enabled", True)):
            self._watcher.start(roots)

    def _retire_idle_analysis_workers_if_safe(self) -> None:
        """Release persistent analyzer subprocesses after a quiet period.

        The detached Engine itself may intentionally remain alive after the
        Manager closes, but completed Fast/Deep work should not keep extra
        Python worker processes around indefinitely. Runners are lazy and will
        restart automatically if a later rescan/reanalysis needs them.
        """
        with self._lock:
            scan_running = bool(self._scan_thread and self._scan_thread.is_alive())
        runtime = self.scan.deep_runtime_state()
        with self._status_metrics_lock:
            cached_deep = dict(self._status_metrics.get("deep_counts") or {})
        remaining = int(cached_deep.get("remaining") or 0)
        busy = bool(
            scan_running
            or int(runtime.get("queue_active") or 0)
            or int(runtime.get("queue_depth") or 0)
            or (remaining > 0 and int(runtime.get("queue_seeding") or 0))
        )
        now = time.monotonic()
        if busy:
            self._analysis_workers_idle_since = None
            return
        if self._analysis_workers_idle_since is None:
            self._analysis_workers_idle_since = now
            return
        if now - self._analysis_workers_idle_since < 4.0:
            return
        try:
            self.deep_pool.retire_idle_processes(timeout=0.8)
        except (OSError, RuntimeError) as exc:
            self._record_error(f"idle worker cleanup: {type(exc).__name__}: {exc}")
        self._analysis_workers_idle_since = now

    def _start_watch_coordinator(self) -> None:
        if self._watch_thread and self._watch_thread.is_alive():
            return

        def run() -> None:
            while not self._stop.wait(2.0):
                # Source availability is probed only on this Engine-owned
                # background coordinator, never on the Qt/UI thread. A slow or
                # disconnected Windows path therefore cannot freeze the Manager.
                before_accessible, before_unavailable = self._source_availability_snapshot()
                configured = tuple(self.configured_roots())
                after_accessible, after_unavailable = self._refresh_source_availability()
                was_all_ready = bool(configured) and len(before_accessible) == len(configured) and not before_unavailable
                all_ready = bool(configured) and len(after_accessible) == len(configured) and not after_unavailable
                if was_all_ready and not all_ready:
                    # A removable/network source disappeared. Preserve Deep
                    # state as interrupted/paused rather than allowing missing
                    # files to cascade into false analysis failures. The saved
                    # Library remains readable while sources are unavailable.
                    self.scan.set_deep_auto_refill_enabled(False)
                    self.scan.suspend_deep_analysis()
                    try:
                        self.deep_pool.preempt_active_requests(timeout=ANALYZER_PREEMPT_SECONDS)
                    except (OSError, RuntimeError) as exc:
                        self._record_error(f"source-offline Deep preemption: {type(exc).__name__}: {exc}")
                elif not was_all_ready and all_ready:
                    # Reconnect is safe to reconcile automatically because the
                    # complete configured source set is visible again. Re-arm
                    # watchers first, then let the normal Fast priority path run.
                    self._configure_watcher()
                    self._watch_dirty.set()
                self._retire_idle_analysis_workers_if_safe()
                if not self._watch_dirty.is_set():
                    continue
                # Debounce file-copy storms. One incremental inventory then
                # reconciles every change committed during the burst.
                if self._stop.wait(2.5):
                    return
                self._watch_dirty.clear()
                try:
                    started = self.start_incremental_scan()
                    if not started and self._all_configured_roots_accessible():
                        # Do not lose a filesystem/root change merely because a
                        # previous inventory was still active at this instant.
                        # A configured-but-unavailable drive is different: do not
                        # spin every debounce interval or reconcile a partial root
                        # set as though the missing source were deleted.
                        self._watch_dirty.set()
                except Exception as exc:
                    self._watch_dirty.set()
                    self._record_error(f"watch refresh: {type(exc).__name__}: {exc}")

        self._watch_thread = threading.Thread(target=run, name="MidiLibraryWatchCoordinator", daemon=True)
        self._watch_thread.start()

    def _record_error(self, text: str) -> None:
        with self._lock:
            self._recent_errors.append(str(text))

    def _maintenance_active(self) -> bool:
        with self._lock:
            return bool(self._maintenance_state)

    def _scan_active(self) -> bool:
        with self._lock:
            return bool(self._scan_thread and self._scan_thread.is_alive())

    def _wait_for_deep_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self.scan.deep_queue.active_count() > 0 and time.monotonic() < deadline:
            time.sleep(0.025)
        return self.scan.deep_queue.active_count() <= 0

    def _wait_for_relationship_yield(self, timeout: float) -> bool:
        with self._lock:
            thread = self._relationship_thread
        if thread is None or not thread.is_alive() or thread is threading.current_thread():
            return True
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()

    def _yield_background_for_fast(self) -> bool:
        """Give one foreground Fast operation exclusive lifecycle priority.

        One shared five-second transition budget covers Deep interruption *and*
        recommendation-builder yield. Returns False when the user cancels while
        this preparation is in progress. Fast parsing never begins while a
        predecessor is still live.
        """
        deadline = time.monotonic() + FAST_PRIORITY_TRANSITION_SECONDS
        self._relationship_cancel.set()
        self.scan.set_deep_auto_refill_enabled(False)
        self.scan.suspend_deep_analysis()
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeError("No Fast-priority transition budget remained for analyzer preemption.")
            self.deep_pool.preempt_active_requests(timeout=min(ANALYZER_PREEMPT_SECONDS, remaining))
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"Detailed Analysis did not yield to Fast Scan: {exc}") from exc
        while self.scan.deep_queue.active_count() > 0:
            if self._stop.is_set() or self.scan.cancel_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Detailed Analysis did not reach a paused boundary within the "
                    f"{FAST_PRIORITY_TRANSITION_SECONDS:g}-second Fast-priority budget."
                )
            self.deep_pool.preempt_active_requests(timeout=min(0.75, remaining))
            time.sleep(0.025)

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self.scan.wait_deep_seed_idle(remaining):
            raise RuntimeError(
                f"Detailed Analysis queue seeding did not yield within the "
                f"{FAST_PRIORITY_TRANSITION_SECONDS:g}-second Fast-priority budget."
            )

        while True:
            with self._lock:
                thread = self._relationship_thread
            if thread is None or not thread.is_alive() or thread is threading.current_thread():
                return True
            if self._stop.is_set() or self.scan.cancel_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Mash Relationship Preparation did not yield within the "
                    f"{FAST_PRIORITY_TRANSITION_SECONDS:g}-second Fast-priority budget."
                )
            thread.join(timeout=min(0.10, remaining))

    @contextmanager
    def _maintenance(self, name: str, *, acquire_timeout: float = 2.0):
        """Serialize administrative mutations while leaving status reads live.

        Shutdown is a one-way admission boundary.  Once ``_stop`` is set, no
        new maintenance writer may enter after the current owner releases the
        gate; otherwise a queued Quarantine/Restore/Backup request could reopen
        SQLite work while Engine teardown is already closing those stores.
        """
        if self._stop.is_set():
            raise RuntimeError("Library Engine is shutting down; new maintenance work is not accepted.")
        if not self._maintenance_gate.acquire(timeout=max(0.1, float(acquire_timeout))):
            with self._lock:
                current = self._maintenance_state or "another maintenance operation"
            raise RuntimeError(f"Library maintenance is already running: {current}.")
        if self._stop.is_set():
            self._maintenance_gate.release()
            raise RuntimeError("Library Engine shutdown began before maintenance could start.")
        with self._lock:
            self._maintenance_state = str(name)
        try:
            yield
        finally:
            with self._lock:
                self._maintenance_state = ""
            self._maintenance_gate.release()

    def _qa_hold_destructive_state(self, state: str) -> None:
        """Target-Windows QA hook for deterministic destructive-operation Close tests.

        In normal product use this is completely inert.  The Windows release
        harness can request a short hold *after* maintenance ownership is
        acquired and mutable workers are quiesced, allowing the real Qt Manager
        to observe/close during Quarantine or Restore instead of relying on a
        fragile timing race.
        """
        requested = str(os.environ.get("MIDI_LIBRARY_QA_HOLD_DESTRUCTIVE_STATE", "")).strip().casefold()
        normalized = str(state or "").strip().casefold()
        if not requested or requested != normalized:
            return
        try:
            seconds = max(0.0, min(4.0, float(
                os.environ.get("MIDI_LIBRARY_QA_HOLD_DESTRUCTIVE_SECONDS", "0") or 0
            )))
        except ValueError:
            seconds = 0.0
        marker = self.paths.library / f"qa-{normalized.replace(' ', '-').replace(':', '-')}-active.txt"
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"pid={os.getpid()} state={normalized}", encoding="utf-8")
        except OSError:
            pass
        if seconds > 0.0:
            # QA holds must never make shutdown wait for an artificial sleep.
            # The active maintenance operation continues immediately to its
            # coherent filesystem/SQLite commit-or-rollback boundary.
            self._stop.wait(seconds)

    def _quiesce_destructive_work(self, *, timeout: float = MAINTENANCE_QUIESCE_SECONDS) -> dict[str, object]:
        """Reach a short, bounded mutation-safe boundary before maintenance.

        Maintenance must never wait behind the 35/135-second MIDI safety limits.
        Active supervised requests are intentionally interrupted and remain
        retryable; no destructive mutation starts unless every mutable owner has
        acknowledged the shared control-plane deadline.
        """
        previous_policy = self.scan.current_policy()
        with self._lock:
            scan_thread = self._scan_thread
            relationship_thread = self._relationship_thread
            scan_was_active = bool(scan_thread and scan_thread.is_alive())
            relationship_was_active = bool(relationship_thread and relationship_thread.is_alive())
        self.scan.suspend_deep_analysis()
        self.scan.deep_queue.pause()
        self.scan.deep_queue.clear_pending()
        self.scan.cancel_scan()
        self._relationship_cancel.set()
        deadline = time.monotonic() + max(1.0, float(timeout))

        try:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeError("No maintenance-quiesce budget remained for analyzer preemption.")
            self.deep_pool.preempt_active_requests(timeout=min(ANALYZER_PREEMPT_SECONDS, remaining))
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"Could not preempt active MIDI workers before maintenance: {exc}") from exc

        for thread, label in ((scan_thread, "library scan"), (relationship_thread, "relationship preparation")):
            if thread is None or not thread.is_alive() or thread is threading.current_thread():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                raise RuntimeError(
                    f"Could not safely pause {label} within {float(timeout):g} seconds before maintenance."
                )
        while self.scan.deep_queue.active_count() > 0 and time.monotonic() < deadline:
            time.sleep(0.025)
        if self.scan.deep_queue.active_count() > 0:
            raise RuntimeError(
                f"Could not safely pause Detailed Analysis within {float(timeout):g} seconds before maintenance. "
                "No destructive database change was started."
            )
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0 or not self.scan.wait_deep_seed_idle(remaining):
            raise RuntimeError(
                f"Detailed-analysis queue seeding did not quiesce within {float(timeout):g} seconds before maintenance."
            )
        return {
            "policy": previous_policy,
            "scan_was_active": scan_was_active,
            "relationship_was_active": relationship_was_active,
        }

    def _resume_after_maintenance(self, state: dict[str, object], *, rescan: bool = False) -> None:
        # Shutdown is one-way.  A maintenance command that was already in flight
        # is allowed to finish/rollback coherently, but its normal finally block
        # must not restart Fast/Deep/relationship workers after close begins.
        if self._stop.is_set():
            self._relationship_cancel.set()
            return
        self._relationship_cancel.clear()
        previous_policy = state.get("policy")
        scan_started = False
        if rescan or bool(state.get("scan_was_active")) or self._watch_dirty.is_set():
            scan_started = self.start_incremental_scan()
        if previous_policy == DeepAnalysisPolicy.BACKGROUND and not scan_started:
            self.scan.resume_suspended_deep_analysis()
        self._start_relationship_builder()

    def configure_roots(self, roots: Iterable[str | Path]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in roots:
            try:
                path = str(Path(raw).expanduser().resolve(strict=False))
            except (OSError, ValueError, TypeError):
                continue
            if path.casefold() in seen:
                continue
            seen.add(path.casefold())
            normalized.append(path)
        self.settings.set_roots(normalized)
        self._refresh_source_availability()
        self._invalidate_status_metrics()
        self._configure_watcher()
        # A roots change is itself a library change.  If another incremental
        # inventory is already running, the watch coordinator retains this dirty
        # flag and retries after that scan reaches a safe boundary.
        self._watch_dirty.set()
        return normalized


    def set_manager_auto_start(self, enabled: bool) -> bool:
        value = bool(enabled)
        self.settings.set("manager_auto_start_analysis", value)
        return value

    def set_throughput_mode(self, mode: str) -> dict[str, Any]:
        value = str(mode or "maximum").casefold()
        if value not in THROUGHPUT_MODES:
            raise ValueError(f"Unknown throughput mode: {mode}")
        # Worker topology is intentionally fixed for the life of the engine.
        # Persist the request and report whether restart is needed.
        self.settings.set("throughput_mode", value)
        requested = recommended_worker_count(value)
        return {"mode": value, "workers": requested, "restart_required": requested != self.worker_count}

    def _record_catalogue_delta(
        self, updated_paths: Iterable[str] = (), removed_paths: Iterable[str] = (), *, full_refresh: bool = False
    ) -> int:
        updated = tuple(dict.fromkeys(str(path) for path in updated_paths if path))
        removed = tuple(dict.fromkeys(str(path) for path in removed_paths if path))
        if not updated and not removed and not full_refresh:
            with self._lock:
                return int(self._catalogue_revision)
        # Thousands of changes are cheaper to hydrate once as a contiguous DB
        # read than to ship/process a giant path delta through the UI client.
        force_full = bool(full_refresh or len(updated) + len(removed) > 4096)
        with self._lock:
            self._catalogue_revision += 1
            revision = int(self._catalogue_revision)
            self._catalogue_deltas.append({
                "revision": revision,
                "updated": () if force_full else updated,
                "removed": () if force_full else removed,
                "full_refresh": force_full,
            })
            return revision

    def catalogue_changes(self, since_revision: int) -> dict[str, object]:
        """Return a bounded committed delta or request one full client refresh."""
        since = int(since_revision)
        with self._lock:
            current = int(self._catalogue_revision)
            deltas = [dict(item) for item in self._catalogue_deltas if int(item.get("revision") or 0) > since]
        if since == current:
            return {"revision": current, "full_refresh": False, "updated": [], "removed": []}
        if since < 0 or since > current or not deltas:
            return {"revision": current, "full_refresh": True, "updated": [], "removed": []}
        if int(deltas[0].get("revision") or 0) != since + 1 or any(bool(item.get("full_refresh")) for item in deltas):
            return {"revision": current, "full_refresh": True, "updated": [], "removed": []}
        updated: dict[str, None] = {}
        removed: dict[str, None] = {}
        for item in deltas:
            for path in item.get("updated") or ():
                value = str(path)
                removed.pop(value, None)
                updated[value] = None
            for path in item.get("removed") or ():
                value = str(path)
                updated.pop(value, None)
                removed[value] = None
        return {
            "revision": current, "full_refresh": False,
            "updated": list(updated), "removed": list(removed),
        }

    def start_scan_paths(
        self, inputs: Iterable[str | Path], *, replace_existing: bool = False, include_subfolders: bool = True,
        force_paths: Iterable[str | Path] | None = None, known_files_only: bool = False,
        quick_worker_limit: int | None = None,
    ) -> bool:
        if self._stop.is_set() or self._maintenance_active():
            return False
        # Exact-path recovery deliberately defers filesystem resolution/stat
        # work to the Engine scan thread. The IPC command can therefore return
        # immediately and the Browser can begin showing preparation progress.
        if known_files_only:
            values = [str(Path(item).expanduser()) for item in inputs if str(item).strip()]
        else:
            values = [str(Path(item).expanduser().resolve(strict=False)) for item in inputs if str(item).strip()]
        if not values:
            return False
        recovery_mode = bool(known_files_only and quick_worker_limit is not None)

        with self._lock:
            if self._scan_thread is not None and self._scan_thread.is_alive():
                return False
            self._scan_operation_kind = "fast_recovery" if recovery_mode else "fast_scan"
            self._scan_operation_state = "preparing"
            self._scan_worker_limit = int(quick_worker_limit or self.worker_count)
            # Sequence audit: Fast is an exclusive foreground phase. Deep
            # Analysis and recommendation preparation must yield before Fast
            # parsing begins so foreground Pause/Cancel timing stays bounded.
            self._scan_recovery_policy = self.scan.current_policy() if recovery_mode else None
            # Arm a fresh operation before the worker starts so a Pause/Cancel
            # received during the preparation phase is not erased by the scan.
            self.scan.cancel_event.clear()
            self.scan.resume_scan()

            def work() -> None:
                summary = None
                try:
                    self._on_scan_progress(ScanProgress(
                        "preparing", 0, max(1, len(values)),
                        message="Preparing Fast Scan · pausing background analysis",
                    ))
                    if not self._yield_background_for_fast():
                        return
                    if self._stop.is_set() or self.scan.cancel_event.is_set():
                        return
                    with self._lock:
                        self._scan_operation_state = "running"
                    # QA-only deterministic state hold for target-Windows Close qualification.
                    # A tiny synthetic Fast source can finish inside one Manager status-poll
                    # interval, which makes the real ``fast:running`` state impossible for
                    # the Qt Manager to observe even though direct Engine IPC sees it.
                    # The hold is completely inert in normal product use and is interruptible
                    # by shutdown/cancel so it cannot extend the user's Close boundary.
                    try:
                        qa_running_hold = max(0.0, min(4.0, float(
                            os.environ.get("MIDI_LIBRARY_QA_HOLD_FAST_RUNNING_SECONDS", "0") or 0
                        )))
                    except ValueError:
                        qa_running_hold = 0.0
                    if qa_running_hold > 0.0:
                        qa_deadline = time.monotonic() + qa_running_hold
                        while (
                            time.monotonic() < qa_deadline
                            and not self._stop.is_set()
                            and not self.scan.cancel_event.is_set()
                            and not self.scan.scan_pause_requested()
                        ):
                            time.sleep(min(0.05, max(0.0, qa_deadline - time.monotonic())))
                    if self._stop.is_set() or self.scan.cancel_event.is_set():
                        return
                    summary = self.scan.quick_catalog_paths(
                        values, include_subfolders=bool(include_subfolders), include_generated=False,
                        replace_existing=bool(replace_existing), force_paths=force_paths,
                        known_files_only=bool(known_files_only), quick_worker_limit=quick_worker_limit,
                        preserve_control_state=True,
                    )
                    if not summary.cancelled:
                        with self._lock:
                            self._scan_operation_state = "finalizing"
                        if summary.removed_paths:
                            self._record_catalogue_delta((), summary.removed_paths)
                        # The relationship writer has already yielded and cannot
                        # restart while this scan thread is alive.  Clear its
                        # cancellation flag only for the local candidate snapshot
                        # rebuild; actual relationship work starts after Fast ends.
                        self._relationship_cancel.clear()
                        if not recovery_mode:
                            self._rebuild_candidate_index()
                except Exception as exc:
                    self._record_error(f"scan paths: {type(exc).__name__}: {exc}")
                finally:
                    shutting_down = self._stop.is_set()
                    self.scan.set_deep_active_limit(None)
                    self.scan.set_deep_auto_refill_enabled(not shutting_down)
                    with self._lock:
                        self._scan_recovery_policy = None
                        self._scan_operation_state = "idle"
                        self._scan_operation_kind = "idle"
                        self._scan_worker_limit = 0
                        self._scan_thread = None
                        if recovery_mode and not shutting_down:
                            # Recovered Fast rows may now be valid candidates.
                            # Rebuild lazily after the foreground phase so a 96K
                            # snapshot cannot extend the user's recovery timer.
                            self._candidate_index = None
                            self._candidate_summaries = []
                            self._candidate_title_map = {}
                            self._candidate_family = {}
                            self._candidate_indexed_family_paths = {}
                            self._candidate_fingerprint = {}
                            self._guided_ready_families = set()
                            self._assisted_ready_families = set()
                            self._guided_ready_family_count = 0
                            self._assisted_ready_family_count = 0
                            self._index_revision += 1
                    if shutting_down:
                        self._relationship_cancel.set()
                    else:
                        # Terminal ordering: Fast persistent boundary first, then
                        # Deep may resume, then recommendation preparation.  Both
                        # are background work and must yield to the next Fast scan.
                        self._relationship_cancel.clear()
                        if (
                            self._deep_session_resume_allowed
                            and self.scan.current_policy() == DeepAnalysisPolicy.BACKGROUND
                            and self._all_configured_roots_accessible()
                        ):
                            self.scan.resume_suspended_deep_analysis()
                        self._start_relationship_builder()

            self._scan_thread = threading.Thread(target=work, name="MidiLibraryEngineScan", daemon=True)
            self._scan_thread.start()
            return True

    def start_incremental_scan(self, *, force: bool = False) -> bool:
        configured = self.configured_roots()
        roots = self._accessible_configured_roots()
        # Whole-Library reconciliation is destructive with respect to rows that
        # disappear from the filesystem snapshot. Never treat an unavailable
        # configured drive/root as a deletion event. Wait until every configured
        # source is accessible, or until the user deliberately removes/reconfigures
        # the unavailable source.
        if not configured or len(roots) != len(configured):
            return False
        # A source may have reappeared since Engine startup (for example a
        # removable drive). Re-arm the directory watcher before the scan so
        # subsequent changes are observed after this reconciliation pass.
        self._configure_watcher()
        return self.start_scan_paths(configured, replace_existing=True, include_subfolders=True)

    def rescan_paths(self, paths: Iterable[str | Path]) -> dict[str, Any]:
        """Force Fast parsing for selected source files, even when unchanged."""
        if self._stop.is_set():
            return {"started": False, "requested": 0, "eligible": 0, "missing": [], "reason": "engine_shutting_down"}
        values = sorted({str(Path(path).expanduser().resolve(strict=False)) for path in paths if str(path).strip()})
        existing = [path for path in values if Path(path).is_file()]
        missing = [path for path in values if path not in existing]
        active_all = [path for path in existing if self._path_in_accessible_source(path)]
        inactive = [path for path in existing if path not in active_all]
        review = []
        ordinary = []
        for path in active_all:
            lifecycle = self.scan.database.lifecycle_for_path(path) or {}
            if str(lifecycle.get("eligibility_state") or "") == EligibilityState.NEEDS_REVIEW.value:
                review.append(path)
            else:
                ordinary.append(path)
        allowed_review = self.scan.database.revalidation_allowed_paths(review)
        retry_exhausted = sorted(set(review) - set(allowed_review))
        active = ordinary + allowed_review
        if not active:
            return {
                "started": False, "requested": len(values), "eligible": 0,
                "missing": missing, "outside_active_sources": inactive,
                "retry_exhausted": retry_exhausted,
            }
        if allowed_review:
            self.scan.database.mark_revalidation_started(allowed_review)
        started = self.start_scan_paths(
            active, replace_existing=False, include_subfolders=False, force_paths=active
        )
        return {
            "started": bool(started), "requested": len(values), "eligible": len(active),
            "missing": missing, "outside_active_sources": inactive,
            "retry_exhausted": retry_exhausted,
        }

    def rescan_failed_fast(self) -> dict[str, Any]:
        """Force a bounded fresh parse of persisted Fast failures.

        Failed files are statistically more likely to be pathological.  Recovery
        therefore uses a dedicated exact-path pipeline and at most two supervised
        workers instead of the normal maximum-throughput catalogue path.  The
        Manager/Browser stays responsive while the Engine gives old failures a
        fair retry after scanner upgrades.
        """
        if self._stop.is_set():
            return {"started": False, "requested": 0, "eligible": 0, "missing": [], "reason": "engine_shutting_down"}
        paths = self.scan.database.quick_failed_paths()
        if not paths:
            return {"started": False, "requested": 0, "eligible": 0, "missing": []}
        if not self._all_configured_roots_accessible():
            return {
                "started": False, "requested": len(paths), "eligible": 0, "missing": [],
                "reason": "configured_sources_unavailable",
            }
        values = [str(Path(path).expanduser()) for path in paths if str(path).strip()]
        allowed = self.scan.database.revalidation_allowed_paths(values)
        retry_exhausted = sorted(set(values) - set(allowed))
        if not allowed:
            return {
                "started": False, "requested": len(values), "eligible": 0, "missing": [],
                "retry_exhausted": retry_exhausted, "recovery_mode": True,
                "worker_limit": min(2, self.worker_count),
            }
        self.scan.database.mark_revalidation_started(allowed)
        started = self.start_scan_paths(
            allowed, replace_existing=False, include_subfolders=False, force_paths=allowed,
            known_files_only=True, quick_worker_limit=min(2, self.worker_count),
        )
        return {
            "started": bool(started), "requested": len(values), "eligible": len(allowed),
            "missing": [], "retry_exhausted": retry_exhausted, "recovery_mode": True,
            "worker_limit": min(2, self.worker_count),
        }

    def reanalyze_paths(self, paths: Iterable[str | Path]) -> int:
        """Deliberately rebuild Detailed Analysis for selected catalogued MIDIs.

        Derived profiles/readiness/relationships are invalidated first so the
        worker cannot satisfy this human-requested re-analysis from an old cache.
        Source MIDI files and Fast catalogue rows are never modified by this step.
        """
        if self._stop.is_set():
            return 0
        values = sorted({
            str(Path(path).expanduser().resolve(strict=False)) for path in paths
            if str(path).strip() and self._path_in_accessible_source(path)
        })
        if not values:
            return 0
        self._deep_session_resume_allowed = True
        # Refuse to mutate a row currently owned by a Deep worker. The UI can
        # retry after it reaches a safe terminal state.
        active_paths = set(self.scan.deep_queue.active_paths())
        eligible = [path for path in values if path not in active_paths]
        if not eligible:
            return 0
        self.library_index.delete_paths(eligible)
        self.relationships.delete_paths(eligible)
        changed = self.scan.database.reset_deep_analysis(eligible)
        if changed:
            self.scan.set_persistent_policy(DeepAnalysisPolicy.BACKGROUND)
            self.settings.set("deep_analysis_policy", DeepAnalysisPolicy.BACKGROUND.value)
            self.scan.resume_deep_analysis(only_paths=eligible)
            self._rebuild_candidate_index()
        return int(changed)

    def keep_reviewed_not_mash(self, paths: Iterable[str | Path]) -> dict[str, Any]:
        """Persist an explicit human curation decision for review-only MIDI.

        The source file is never modified.  The resulting row remains in the
        authoritative Library but is excluded from Guided/Assisted recommendation
        pools.  This prevents valid-but-currently-unprofileable MIDI from living
        forever in Needs Review simply because the analyzer has a known limit.
        """
        if self._stop.is_set():
            return {"kept": 0, "errors": ["Library Engine is shutting down."]}
        values = sorted({str(Path(path).expanduser().resolve(strict=False)) for path in paths if str(path).strip()})
        if not values:
            return {"kept": 0, "errors": []}
        state: dict[str, object] | None = None
        changed: list[str] = []
        refused: list[str] = []
        try:
            with self._maintenance("review disposition", acquire_timeout=5.0):
                state = self._quiesce_destructive_work()
                changed, refused = self.scan.database.keep_reviewed_not_mash(values)
                if changed:
                    # Defensive removal: these rows should not currently be recommendation
                    # candidates, but stale prepared relationships from older releases must
                    # not survive an explicit Not-Mash curation decision.
                    self.relationships.delete_paths(changed)
                    with self._lock:
                        self._candidate_index = None
                        self._candidate_summaries = []
                        self._candidate_title_map = {}
                        self._candidate_family = {}
                        self._candidate_indexed_family_paths = {}
                        self._candidate_fingerprint = {}
                        self._guided_ready_families = set()
                        self._assisted_ready_families = set()
                        self._guided_ready_family_count = 0
                        self._assisted_ready_family_count = 0
                        self._index_revision += 1
                    self._record_catalogue_delta(changed, ())
        finally:
            if state is not None:
                self._resume_after_maintenance(state, rescan=False)
        errors = [
            f"{path}: only readable Needs Review rows or safe review-only rejection categories can be kept as Not Mash"
            for path in refused
        ]
        return {"kept": len(changed), "paths": changed, "errors": errors}

    def start_maximum_analysis(self) -> bool:
        """Start foreground Fast reconciliation and allow Deep continuation.

        Whole-Library processing requires every configured source to be
        accessible. A temporarily offline root is never treated as deletion,
        and Deep work is not globally resumed against a partial source set.
        """
        if self._stop.is_set() or not self._all_configured_roots_accessible():
            return False
        self._deep_session_resume_allowed = True
        self.settings.set("deep_analysis_policy", DeepAnalysisPolicy.BACKGROUND.value)
        self.scan.set_persistent_policy(DeepAnalysisPolicy.BACKGROUND)
        started = self.start_incremental_scan()
        with self._lock:
            scan_running = bool(self._scan_thread and self._scan_thread.is_alive())
        if not started and not scan_running:
            self.scan.resume_deep_analysis()
            self._start_relationship_builder()
        return True

    def _pause_analysis_boundary(self, transition: str) -> None:
        transition_name = str(transition or "pausing").casefold()
        if transition_name not in {"pausing", "cancelling"}:
            transition_name = "pausing"
        with self._lock:
            self._deep_transition_state = transition_name
        try:
            # QA-only deterministic state hold lets the target-Windows Manager
            # poll and close while a real Deep transition is in flight. Normal
            # product runs never set this environment variable.
            try:
                qa_hold = max(0.0, min(3.0, float(
                    os.environ.get("MIDI_LIBRARY_QA_HOLD_DEEP_TRANSITION_SECONDS", "0") or 0
                )))
            except ValueError:
                qa_hold = 0.0
            if qa_hold > 0.0:
                self._stop.wait(qa_hold)
            self.settings.set("deep_analysis_policy", DeepAnalysisPolicy.LATER.value)
            self.scan.pause_deep_analysis()
            # During Fast work the shared analyzer pool already belongs to Fast and
            # Deep is paused by sequencing; never interrupt a foreground quick scan
            # merely because the user also changed the Deep policy.
            if not self._scan_active():
                deadline = time.monotonic() + DEEP_PAUSE_TRANSITION_SECONDS
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    raise RuntimeError("No Detailed Analysis pause budget remained for analyzer preemption.")
                self.deep_pool.preempt_active_requests(timeout=min(ANALYZER_PREEMPT_SECONDS, remaining))
                if not self._wait_for_deep_idle(max(0.0, deadline - time.monotonic())):
                    raise RuntimeError(
                        f"Detailed Analysis did not reach Pause within {DEEP_PAUSE_TRANSITION_SECONDS:g} seconds."
                    )
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0 or not self.scan.wait_deep_seed_idle(remaining):
                    raise RuntimeError(
                        f"Detailed Analysis queue seeding did not reach Pause within {DEEP_PAUSE_TRANSITION_SECONDS:g} seconds."
                    )
        finally:
            with self._lock:
                self._deep_transition_state = ""

    def pause_analysis(self) -> None:
        self._pause_analysis_boundary("pausing")

    def cancel_analysis(self) -> None:
        """Cancel the current Deep pass at the same persistent safe boundary as Pause.

        Completed files remain committed. Active supervised analyzers are
        preempted and returned to a resumable pending/paused state; queued work
        is discarded from memory and can be reseeded later by Start / Resume.
        Cancel is intentionally a distinct user command even though its durable
        policy after the boundary is the same as Pause (Later).
        """
        self._pause_analysis_boundary("cancelling")

    def resume_analysis(self) -> bool:
        if self._stop.is_set() or not self._all_configured_roots_accessible():
            return False
        self._deep_session_resume_allowed = True
        self.settings.set("deep_analysis_policy", DeepAnalysisPolicy.BACKGROUND.value)
        self.scan.set_persistent_policy(DeepAnalysisPolicy.BACKGROUND)
        with self._lock:
            scan_running = bool(self._scan_thread and self._scan_thread.is_alive())
        if not scan_running:
            self.scan.resume_deep_analysis()
        self._start_relationship_builder()
        return True

    def retry_failed(self, paths: Iterable[str | Path] | None = None) -> int:
        if self._stop.is_set():
            return 0
        if paths is None:
            if not self._all_configured_roots_accessible():
                return 0
            eligible = None
        else:
            eligible = [str(path) for path in paths if self._path_in_accessible_source(path)]
            if not eligible:
                return 0
        self._deep_session_resume_allowed = True
        return self.scan.retry_failed_deep_analysis(eligible)

    def prioritize_paths(self, paths: Iterable[str | Path], priority: int = 0) -> int:
        if self._stop.is_set():
            return 0
        values = [
            str(Path(path).resolve(strict=False)) for path in paths
            if self._path_in_accessible_source(path)
        ]
        if not values:
            return 0
        self._deep_session_resume_allowed = True
        changed = self.scan.database.prioritize_paths(values, int(priority))
        for path in values:
            self.scan.analyze_selected(path)
        return changed

    def prepare_anchor(self, path: str | Path) -> dict[str, Any]:
        value = str(Path(path).expanduser().resolve(strict=False))
        if self._stop.is_set():
            return {"path": value, "state": "engine_shutting_down"}
        if not self._path_in_accessible_source(value):
            return {"path": value, "state": "source_unavailable"}
        if self.scan.database.load_result(value, validate_source=False) is None:
            if Path(value).is_file():
                self.start_scan_paths([value], replace_existing=False, include_subfolders=False)
            return {"path": value, "state": "cataloguing"}
        status = self.scan.database.deep_status(value)
        with self._lock:
            self._relationship_priority_paths.add(value)
        if status != "complete":
            self.prioritize_paths([value], 0)
            return {"path": value, "state": "deep_analysis"}
        self._start_relationship_builder()
        return {"path": value, "state": "relationship_preparation"}

    def _rebuild_candidate_index(self) -> bool:
        """Rehydrate compact recommendation metadata at a shutdown-safe boundary.

        Loading 96K committed quick rows is the longest single phase. Check the
        Engine stop flag between every expensive phase and before publishing the
        replacement index so shutdown can never close SQLite underneath an
        in-flight relationship worker.
        """
        if self._stop.is_set() or self._relationship_cancel.is_set():
            return False
        quick = self.scan.cached_results(self.configured_roots() or None, validate_sources=False)
        blessed_mash_paths = self.scan.database.blessed_paths(EligibilityState.MASH_READY.value)
        if self._stop.is_set() or self._relationship_cancel.is_set():
            return False
        summaries_list: list[object] = []
        for position, result in enumerate(quick, 1):
            if result.status != "failed" and result.path in blessed_mash_paths:
                summaries_list.append(summary_from_quick(result))
            if position % 1024 == 0 and (self._stop.is_set() or self._relationship_cancel.is_set()):
                return False
        summaries = list(summaries_list)
        if self._stop.is_set() or self._relationship_cancel.is_set():
            return False
        index = CandidateIndex(summaries) if len(summaries) >= 2 else None
        if self._stop.is_set() or self._relationship_cancel.is_set():
            return False
        family = {result.path: (result.family_id or f"path:{result.path}") for result in quick}
        fingerprints = {result.path: (result.sha256 or result.musical_fingerprint or "") for result in quick}
        if self._stop.is_set() or self._relationship_cancel.is_set():
            return False
        title_map = DiscoveryGraph.profile_title_map(summaries) if summaries else {}
        if self._stop.is_set() or self._relationship_cancel.is_set():
            return False
        # Readiness is stored per MIDI arrangement, but relationship preparation
        # intentionally works at composition-family level. Keep exact family
        # totals so the Manager does not claim thousands of duplicate filenames
        # are still waiting after their song family has already been prepared.
        guided_ready_paths = [path for path in self.library_index.guided_ready_paths(limit=100_000) if path in blessed_mash_paths]
        assisted_ready_paths = [path for path in self.library_index.assisted_ready_paths(limit=100_000) if path in blessed_mash_paths]
        if self._stop.is_set() or self._relationship_cancel.is_set():
            return False
        guided_ready_families = {family.get(path, f"path:{path}") for path in guided_ready_paths}
        assisted_ready_families = {family.get(path, f"path:{path}") for path in assisted_ready_paths}
        with self._lock:
            if self._stop.is_set() or self._relationship_cancel.is_set():
                return False
            self._candidate_index = index
            self._candidate_summaries = summaries
            self._candidate_title_map = title_map
            self._candidate_family = family
            indexed_family_paths: dict[str, set[str]] = {}
            for summary in summaries:
                summary_path = str(getattr(summary, "path", ""))
                if not summary_path:
                    continue
                summary_family = family.get(summary_path, f"path:{summary_path}")
                indexed_family_paths.setdefault(summary_family, set()).add(summary_path)
            self._candidate_indexed_family_paths = indexed_family_paths
            self._candidate_fingerprint = fingerprints
            self._guided_ready_families = set(guided_ready_families)
            self._assisted_ready_families = set(assisted_ready_families)
            self._guided_ready_family_count = len(self._guided_ready_families)
            self._assisted_ready_family_count = len(self._assisted_ready_families)
            self._index_revision += 1
        if self._stop.is_set() or self._relationship_cancel.is_set():
            return False
        # Resolve imported/human evidence only after the compact title map exists.
        # This is bounded metadata work in the Engine process, never the Remixer UI.
        self.refresh_reference_evidence(invalidate_relationships=False)
        return True

    def _ensure_candidate_index(self) -> CandidateIndex | None:
        with self._lock:
            index = self._candidate_index
        if index is None:
            if not self._rebuild_candidate_index():
                return None
            with self._lock:
                index = self._candidate_index
        return index

    def _deep_bulk_analysis_active(self) -> bool:
        """Return True while Detailed Analysis owns sustained foreground throughput.

        Relationship preparation is useful during a long Deep run, but it must
        not continuously self-drain and compete with the analyzer pool for CPU
        and SQLite time.  This intentionally uses the database-free runtime
        snapshot so the check cannot block behind a large catalogue transaction.
        """
        runtime = self.scan.deep_runtime_state()
        return bool(
            str(runtime.get("policy") or "") == DeepAnalysisPolicy.BACKGROUND.value
            and (
                int(runtime.get("queue_active") or 0) > 0
                or int(runtime.get("queue_depth") or 0) > 0
                or int(runtime.get("queue_seeding") or 0) > 0
            )
        )

    def _relationship_anchor_budget(self, existing_relationships: int, priority_count: int = 0) -> int:
        """Bound relationship work so bulk Deep Analysis keeps throughput priority.

        User-prioritized anchors still run first.  During sustained Deep work a
        small batch makes fresh Remixer ideas available without turning
        recommendation preparation into a second competing bulk-analysis job.
        Once Deep is paused/finished, the normal self-draining 96/24-anchor
        budgets apply.
        """
        if self._deep_bulk_analysis_active():
            if int(priority_count or 0) > 0:
                return max(RELATIONSHIP_DEEP_ANCHOR_BUDGET, min(8, int(priority_count)))
            return RELATIONSHIP_DEEP_ANCHOR_BUDGET
        return 96 if int(existing_relationships) < 512 else 24

    def _relationship_should_self_drain(self, more_work: bool, restart_requested: bool) -> bool:
        if not (more_work or restart_requested):
            return False
        with self._lock:
            priority_pending = bool(self._relationship_priority_paths)
        if priority_pending:
            return True
        # A later Deep completion (every bounded completion batch, plus the final
        # queue boundary) will schedule the next small relationship batch.
        return not self._deep_bulk_analysis_active()

    def _start_relationship_builder(self) -> bool:
        with self._lock:
            if self._stop.is_set():
                return False
            # Every Fast operation owns foreground sequencing priority.
            # Recommendation preparation resumes only after the scan thread has
            # reached its terminal persistent boundary and released ownership.
            if self._scan_thread is not None and self._scan_thread.is_alive():
                self._relationship_restart_requested = True
                return False
            if self._maintenance_state:
                self._relationship_restart_requested = True
                return False
            if self._relationship_thread is not None and self._relationship_thread.is_alive():
                self._relationship_restart_requested = True
                return False
            self._relationship_cancel.clear()

            def work() -> None:
                more_work = False
                try:
                    more_work = self._build_relationships()
                except Exception as exc:
                    # Background-thread boundary: never let an unexpected
                    # exception disappear on stderr without reaching Engine
                    # diagnostics/status. Domain operations below still use
                    # narrower exception handling.
                    self._record_error(f"relationship builder: {type(exc).__name__}: {exc}")
                finally:
                    restart = False
                    with self._lock:
                        self._relationship_thread = None
                        restart_requested = bool(self._relationship_restart_requested)
                        self._relationship_restart_requested = False
                    restart = self._relationship_should_self_drain(more_work, restart_requested)
                    if restart and not self._stop.is_set() and not self._maintenance_active():
                        # Continuous background preparation is intentional after
                        # Deep reaches a quiet boundary.  During active bulk Deep,
                        # bounded completion callbacks schedule later small passes.
                        time.sleep(0.05)
                        self._start_relationship_builder()

            self._relationship_thread = threading.Thread(
                target=work, name="MidiLibraryRelationshipBuilder", daemon=True
            )
            self._relationship_thread.start()
            return True

    def _build_relationships(self) -> bool:
        """Prepare a bounded batch of persistent relationships.

        Returns True when the batch budget was exhausted and another background
        pass should continue.  This makes recommendation preparation self-draining
        even when Detailed Analysis is paused: a user with 10K already-analyzed
        songs must not get only the first 64 prepared anchors forever.
        """
        # A quick-only catalogue can contain 96K rows but zero deeply-ready
        # arrangements. Do not hydrate a six-figure candidate index when there
        # is literally nothing eligible to prepare yet.
        blessed_mash_paths = self.scan.database.blessed_paths(EligibilityState.MASH_READY.value)
        raw_ready_paths = [
            path for path in self.library_index.assisted_ready_paths(limit=100_000)
            if path in blessed_mash_paths
        ]
        with self._lock:
            priority_probe = bool(self._relationship_priority_paths)
        if len(raw_ready_paths) < 2 and not priority_probe:
            return False
        index = self._ensure_candidate_index()
        if index is None:
            return False
        with self._lock:
            priority_paths = list(self._relationship_priority_paths)
            families = dict(self._candidate_family)
            fingerprints = dict(self._candidate_fingerprint)
            summaries = tuple(self._candidate_summaries)
            title_map = dict(self._candidate_title_map)

        # Normal background preparation works at composition-family level.  A
        # 96K library may contain tens of thousands of alternate arrangements;
        # validating each filename as an anchor wastes CPU and produces duplicate
        # DJ ideas.  The highest-readiness path returned for each family becomes
        # its background representative.  An explicitly selected Assisted anchor
        # is always allowed through even when it is an alternate arrangement.
        priority_set = set(priority_paths)
        ordered_ready = [*priority_paths, *(path for path in raw_ready_paths if path not in priority_set)]
        ready_paths: list[str] = []
        seen_families: set[str] = set()
        for path in ordered_ready:
            family = families.get(path, f"path:{path}")
            if path in priority_set or family not in seen_families:
                ready_paths.append(path)
                seen_families.add(family)

        # The authoritative in-memory CandidateIndex now contains only current
        # persisted Mash representatives and is updated incrementally as Deep
        # lifecycle truth changes. Rebuilding a second ready-only CandidateIndex
        # on every relationship pass was redundant O(N) work at 96K scale.
        ready_index = index
        ready_set = set(ready_paths)

        existing_relationships = self.relationships.count()
        anchor_budget = self._relationship_anchor_budget(existing_relationships, len(priority_paths))
        anchors_built_this_pass = 0
        graph: DiscoveryGraph | None = None
        try:
            graph = DiscoveryGraph.for_app(self.app_root)
            graph.import_reference_catalog(self._reference_catalog())
            for position, anchor_path in enumerate(ready_paths, 1):
                if self._stop.is_set() or self._relationship_cancel.is_set():
                    return False
                fingerprint = fingerprints.get(anchor_path, "")
                assisted_current = self.relationships.anchor_is_current(
                    anchor_path, fingerprint, mode="assisted"
                )
                guided_current = self.relationships.anchor_is_current(
                    anchor_path, fingerprint, mode="guided"
                )
                if assisted_current and guided_current:
                    continue
                anchor = self.library_index.load_one(anchor_path, validate_source=False)
                if anchor is None:
                    continue

                metadata_paths = list(ready_index.candidate_paths(anchor_path, limit=128))
                evidence = graph.evidence_candidates(
                    anchor, summaries, limit=48, title_map=title_map
                ) if summaries else {}
                for evidence_path in evidence:
                    if evidence_path in ready_set and evidence_path != anchor_path and evidence_path not in metadata_paths:
                        metadata_paths.append(evidence_path)
                # The full library participates in cheap indexing/evidence. Only
                # this bounded union receives heavyweight pair validation.
                candidate_paths = tuple(metadata_paths[:176])
                deep = self.library_index.load_many(candidate_paths, validate_sources=False)
                assisted_rows = []
                guided_rows = []
                anchor_family = families.get(anchor_path, f"path:{anchor_path}")
                for candidate_path in candidate_paths:
                    if self._stop.is_set() or self._relationship_cancel.is_set():
                        return False
                    other = deep.get(candidate_path)
                    if other is None:
                        continue
                    other_family = families.get(candidate_path, f"path:{candidate_path}")
                    if other_family == anchor_family:
                        continue
                    pair_evidence = evidence.get(candidate_path)
                    # Both product lanes share the same expensive pair assessment.
                    # At six-figure-library scale, never calculate deterministic
                    # musical evidence twice for the same anchor/candidate step.
                    assessment = assess_pair(anchor, other)
                    if not assisted_current:
                        relation = assisted_relationship_for_profiles(
                            anchor, other, anchor_family, other_family, assessment=assessment,
                            discovery_evidence=pair_evidence,
                        )
                        if relation is not None:
                            assisted_rows.append(with_discovery_evidence(relation, pair_evidence, maximum_bonus=7))
                    if not guided_current:
                        relation = guided_relationship_for_profiles(
                            anchor, other, anchor_family, other_family, assessment=assessment
                        )
                        if relation is not None:
                            guided_rows.append(with_discovery_evidence(relation, pair_evidence, maximum_bonus=4))
                if not assisted_current:
                    assisted_rows.sort(
                        key=lambda item: (item.score, item.evidence_score, item.preparation_score), reverse=True
                    )
                    self.relationships.upsert_many(assisted_rows[:72])
                    self.relationships.mark_anchor_built(
                        anchor_path, fingerprint, len(candidate_paths), len(assisted_rows[:72]), mode="assisted"
                    )
                if not guided_current:
                    guided_rows.sort(
                        key=lambda item: (item.score, item.evidence_score, item.preparation_score), reverse=True
                    )
                    self.relationships.upsert_many(guided_rows[:40])
                    self.relationships.mark_anchor_built(
                        anchor_path, fingerprint, len(candidate_paths), len(guided_rows[:40]), mode="guided"
                    )
                with self._lock:
                    self._relationship_priority_paths.discard(anchor_path)
                    priority_restart = bool(self._relationship_restart_requested)
                anchors_built_this_pass += 1
                # A newly prioritized anchor (for example, the song the DJ just
                # chose in Assisted) should reach the front after the current
                # atomic anchor build instead of waiting for the remainder of a
                # background batch.  We never interrupt a pair/DB commit mid-flight.
                if priority_restart:
                    return True
                if anchors_built_this_pass >= anchor_budget:
                    return True
                if position % 2 == 0:
                    time.sleep(0.005)
            return False
        finally:
            if graph is not None:
                graph.close()

    def _reference_catalog(self) -> ReferenceCatalog:
        return ReferenceCatalog(
            self.app_root / "data" / "known_pairings.json",
            import_directories=(self.paths.reference_imports,),
        )

    def refresh_reference_evidence(self, *, invalidate_relationships: bool = True) -> dict[str, int]:
        """Import evidence and prioritize its library endpoints in the Engine.

        Human/imported relationships widen discovery but never bypass MIDI
        validity.  Their endpoints are moved ahead of the anonymous long tail so
        the expensive deep analyzer makes useful DJ evidence available sooner.
        """
        try:
            with DiscoveryGraph.for_app(self.app_root) as graph:
                written = graph.import_reference_catalog(self._reference_catalog())
                with self._lock:
                    summaries = tuple(self._candidate_summaries)
                    title_map = dict(self._candidate_title_map)
                resolved = graph.resolved_evidence_pairs(
                    summaries, limit=2048, title_map=title_map
                ) if summaries else {}
                summary = graph.summary()
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            self._record_error(f"reference evidence: {type(exc).__name__}: {exc}")
            return dict(self._graph_summary)
        priority_paths = sorted({path for pair in resolved for path in pair})
        prioritized = self.scan.database.prioritize_paths(priority_paths, 20) if priority_paths else 0
        if (
            prioritized
            and self.scan.current_policy() == DeepAnalysisPolicy.BACKGROUND
            and not self._maintenance_active()
        ):
            self.scan.resume_deep_analysis()
        self._graph_summary = {
            "edges": int(summary.edges), "providers": int(summary.providers),
            "feedback_pairs": int(summary.feedback_pairs), "imported": int(written),
            "prioritized_paths": int(prioritized),
        }
        if written and invalidate_relationships:
            self.relationships.invalidate_all_builds()
            self._start_relationship_builder()
        return dict(self._graph_summary)

    def record_feedback(self, identity_a: str, identity_b: str, action: str) -> None:
        with DiscoveryGraph.for_app(self.app_root) as graph:
            graph.record_feedback_identities(identity_a, identity_b, action)
            summary = graph.summary()
        self._graph_summary.update({
            "edges": int(summary.edges), "providers": int(summary.providers),
            "feedback_pairs": int(summary.feedback_pairs),
        })

    def restore_feedback(self, identity_a: str, identity_b: str) -> None:
        with DiscoveryGraph.for_app(self.app_root) as graph:
            graph.restore_ignored_feedback(identity_a, identity_b)

    def related_candidate_paths(self, anchor_path: str | Path, *, limit: int = 256) -> tuple[str, ...]:
        """Return cheap full-library metadata neighbors for a creative client.

        Contract 4 admission is enforced again at this read boundary.  The
        CandidateIndex is built from Mash Ready rows, but an old in-memory index
        must never become a loophole after lifecycle state changes.
        """
        value = str(Path(anchor_path).expanduser().resolve(strict=False))
        blessed = self.scan.database.blessed_paths(EligibilityState.MASH_READY.value)
        if value not in blessed:
            return ()
        index = self._ensure_candidate_index()
        if index is None:
            return ()
        values = index.candidate_paths(value, limit=max(1, min(4096, int(limit))))
        return tuple(path for path in values if path in blessed)

    def recommended_pairs(
        self, *, mode: str = "guided", limit: int = 64, offset: int = 0, anchor_path: str | None = None
    ) -> list[dict[str, Any]]:
        """Read persisted recommendations without bypassing lifecycle admission.

        Relationship rows survive upgrades and are intentionally reusable.  A
        Contract-3 row therefore cannot itself prove current eligibility.  Both
        endpoints must still be persisted Blessed / Mash Ready at read time.
        This makes the lifecycle database authoritative even before stale
        relationship rows have been rebuilt.
        """
        blessed = self.scan.database.blessed_paths(EligibilityState.MASH_READY.value)
        if anchor_path:
            anchor = str(Path(anchor_path).expanduser().resolve(strict=False))
            if anchor not in blessed:
                return []
            rows = self.relationships.recommended_for_anchor(
                anchor, mode=mode, limit=limit, offset=offset
            )
        else:
            rows = self.relationships.recommended_pairs(mode=mode, limit=limit, offset=offset)
        return [
            asdict(row) for row in rows
            if row.path_a in blessed and row.path_b in blessed
        ]

    def suspicious_candidates(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Return a review queue that never hides Fast-scan failures.

        Unloadable/Fast-failed files are a distinct cleanup class from unusual
        but successfully catalogued MIDI files. Both can be reviewed, but only
        the former means the Library could not create a usable Fast entry.
        """
        quick = self.scan.database.suspicious_results(limit=limit)
        failures = [result for result in quick if result.status == "failed"]
        loadable = [result for result in quick if result.status != "failed"]
        rows: list[dict[str, Any]] = []
        for result in failures:
            rows.append({
                "path": result.path,
                "title": result.catalog_match.title or Path(result.path).stem,
                "duration_seconds": float(result.duration_seconds or 0.0),
                "note_count": int(result.note_count or 0),
                "track_count": int(result.track_count or 0),
                "reasons": ["Fast Scan could not catalogue this MIDI"],
                "severity": 120,
                "category": "FAST SCAN FAILED / UNLOADABLE",
                "error": str(result.error or "Fast Scan failed without a saved reason"),
            })
        profiles = [quick_result_to_profile(result) for result in loadable]
        rows.extend(
            {
                "path": item.path, "title": item.title, "duration_seconds": item.duration_seconds,
                "note_count": item.note_count, "track_count": item.track_count,
                "reasons": list(item.reasons), "severity": item.severity,
                "category": item.category, "error": item.error,
            }
            for item in suspicious_midi_candidates(profiles, limit=max(1, limit - len(rows)))
        )
        return rows[: max(1, int(limit))]

    def remove_paths(self, paths: Iterable[str], *, permanent: bool = False) -> dict[str, Any]:
        requested = [str(Path(path).resolve(strict=False)) for path in paths if path]
        if not requested:
            return {"removed": 0, "quarantined": 0, "errors": []}
        # Permanent deletion may also target a file already in Library quarantine.
        # New quarantine requests must originate from an accessible configured source.
        values: list[str] = []
        rejected: list[str] = []
        for path in requested:
            lifecycle = self.scan.database.lifecycle_for_path(path) or {}
            eligibility = str(lifecycle.get("eligibility_state") or "")
            already_quarantined = eligibility == EligibilityState.QUARANTINED.value
            allowed_state = (
                eligibility in {EligibilityState.REJECTED.value, EligibilityState.QUARANTINED.value}
                if permanent else
                eligibility in {EligibilityState.NEEDS_REVIEW.value, EligibilityState.REJECTED.value}
            )
            location_ok = already_quarantined if permanent and already_quarantined else self._path_in_accessible_source(path)
            if allowed_state and location_ok:
                values.append(path)
            else:
                rejected.append(path)
        errors: list[str] = [
            f"{path}: lifecycle state is not eligible for this cleanup action; no source file was changed" for path in rejected
        ]
        if not values:
            return {"removed": 0, "quarantined": 0, "errors": errors}
        state: dict[str, object] | None = None
        deleted: list[str] = []
        retired_from_library: list[str] = []
        quarantined: list[tuple[str, str]] = []
        try:
            with self._maintenance("file quarantine/delete", acquire_timeout=5.0):
                state = self._quiesce_destructive_work()
                if not permanent:
                    self._qa_hold_destructive_state("quarantine")
                quarantine_root = self.paths.data_root / "quarantine"
                pending_delete_root = self.paths.data_root / "pending-delete"
                storage_root = pending_delete_root if permanent else quarantine_root
                try:
                    storage_root.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    action = "permanent-delete staging" if permanent else "quarantine"
                    errors.append(
                        f"Library {action} is unavailable at {storage_root}: "
                        f"{type(exc).__name__}: {exc}; no source file was changed"
                    )
                    return {"removed": 0, "quarantined": 0, "errors": errors}
                for raw in values:
                    source = Path(raw)
                    try:
                        if permanent:
                            if not source.exists():
                                errors.append(f"{source}: source file is not currently available for permanent deletion")
                                continue
                            staged = pending_delete_root / source.name
                            counter = 1
                            while staged.exists():
                                staged = pending_delete_root / f"{source.stem}-{counter}{source.suffix}"
                                counter += 1
                            # Move first so Windows sharing/permission failures happen
                            # before any authoritative Library row is removed. The
                            # staged file remains recoverable until every persistence
                            # cleanup step has committed.
                            shutil.move(str(source), str(staged))
                            try:
                                self.relationships.delete_paths([raw])
                                self.library_index.delete_paths([raw])
                                self.scan.database.delete_paths([raw])
                                retired_from_library.append(raw)
                            except (sqlite3.Error, OSError, RuntimeError) as exc:
                                try:
                                    shutil.move(str(staged), str(source))
                                except OSError as rollback_exc:
                                    raise RuntimeError(
                                        f"{source}: permanent-delete persistence failed and filesystem rollback also failed: "
                                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                                    ) from exc
                                errors.append(
                                    f"{source}: permanent-delete Library cleanup could not be committed; "
                                    "the source file was restored to its original location"
                                )
                                continue
                            except Exception:
                                # Programming errors must still preserve the user's
                                # source before surfacing. This broad boundary is
                                # deliberately not swallowed.
                                if staged.exists() and not source.exists():
                                    shutil.move(str(staged), str(source))
                                raise
                            try:
                                try:
                                    staged.unlink()
                                except PermissionError:
                                    try:
                                        staged.chmod(staged.stat().st_mode | stat.S_IWUSR | stat.S_IREAD)
                                    except OSError:
                                        pass
                                    staged.unlink()
                            except OSError as exc:
                                # Library truth is already retired. Keep the staged
                                # copy rather than silently discarding ownership; the
                                # explicit path lets support/user finish the confirmed
                                # deletion without resurrecting a stale recommendation.
                                errors.append(
                                    f"{source}: Library state was removed, but the confirmed source could not be erased "
                                    f"from pending-delete storage ({type(exc).__name__}: {exc}). Retained copy: {staged}"
                                )
                                continue
                            deleted.append(raw)
                            continue
                        if not source.exists():
                            errors.append(f"{source}: source file is not currently available for quarantine")
                            continue
                        target = quarantine_root / source.name
                        counter = 1
                        while target.exists():
                            target = quarantine_root / f"{source.stem}-{counter}{source.suffix}"
                            counter += 1
                        shutil.move(str(source), str(target))
                        target_text = str(target.resolve(strict=False))
                        try:
                            committed = self.scan.database.mark_quarantined(raw, target_text)
                        except sqlite3.Error as exc:
                            # SQLite and the filesystem cannot share one native
                            # transaction.  If persistence fails after the move,
                            # restore the source immediately so Library truth and
                            # disk truth cannot diverge.
                            try:
                                shutil.move(target_text, raw)
                            except OSError as rollback_exc:
                                raise RuntimeError(
                                    f"{source}: quarantine persistence failed and filesystem rollback also failed: "
                                    f"{type(rollback_exc).__name__}: {rollback_exc}"
                                ) from exc
                            errors.append(
                                f"{source}: Library quarantine state could not be committed; "
                                "the source file was restored to its original location"
                            )
                            continue
                        if not committed:
                            # Roll back the filesystem move if the catalogue row unexpectedly vanished.
                            shutil.move(target_text, raw)
                            errors.append(f"{source}: Library row disappeared before quarantine could be committed")
                            continue
                        quarantined.append((raw, target_text))
                    except OSError as exc:
                        errors.append(f"{source}: {type(exc).__name__}: {exc}")
                retired_paths = [*retired_from_library, *(old for old, _new in quarantined)]
                if retired_paths:
                    self.library_index.delete_paths(retired_paths)
                    self.relationships.delete_paths(retired_paths)
                    self._record_catalogue_delta((), retired_paths)
                    self._rebuild_candidate_index()
        finally:
            if state is not None:
                self._resume_after_maintenance(state, rescan=False)
        return {"removed": len(deleted), "quarantined": len(quarantined), "errors": errors}

    def restore_quarantined_paths(self, paths: Iterable[str | Path]) -> dict[str, Any]:
        requested = sorted({str(Path(path).expanduser().resolve(strict=False)) for path in paths if str(path).strip()})
        restored: list[str] = []
        errors: list[str] = []
        state: dict[str, object] | None = None
        try:
            with self._maintenance("quarantine restore", acquire_timeout=5.0):
                state = self._quiesce_destructive_work()
                self._qa_hold_destructive_state("restore")
                for current in requested:
                    pair = self.scan.database.restore_quarantined(current)
                    if pair is None:
                        errors.append(f"{current}: not a persisted quarantined MIDI")
                        continue
                    quarantine_path, original_path = pair
                    if not self._path_under_roots(original_path, self.configured_roots()):
                        errors.append(f"{current}: original Library source is no longer configured")
                        continue
                    if not self._path_under_roots(original_path, self._accessible_configured_roots()):
                        errors.append(f"{current}: original Library source is currently unavailable; reconnect it before restore")
                        continue
                    original = Path(original_path)
                    current_file = Path(quarantine_path)
                    try:
                        if not current_file.is_file():
                            errors.append(f"{current}: quarantined source file is missing")
                            continue
                        if original.exists():
                            errors.append(f"{current}: original location already contains a file; restore was not overwritten")
                            continue
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(current_file), str(original))
                        try:
                            committed = self.scan.database.complete_quarantine_restore(current, original_path)
                        except sqlite3.Error as exc:
                            try:
                                shutil.move(str(original), str(current_file))
                            except OSError as rollback_exc:
                                raise RuntimeError(
                                    f"{current}: restore persistence failed and filesystem rollback also failed: "
                                    f"{type(rollback_exc).__name__}: {rollback_exc}"
                                ) from exc
                            errors.append(
                                f"{current}: Library restore state could not be committed; "
                                "the MIDI remained in quarantine"
                            )
                            continue
                        if not committed:
                            shutil.move(str(original), str(current_file))
                            errors.append(f"{current}: Library restore state could not be committed")
                            continue
                        restored.append(original_path)
                    except OSError as exc:
                        errors.append(f"{current}: {type(exc).__name__}: {exc}")
                if restored:
                    self._record_catalogue_delta(restored, requested)
        finally:
            if state is not None:
                self._resume_after_maintenance(state, rescan=False)
        if restored and not self._stop.is_set():
            # Restored items are reparsed so current source truth is verified.
            # Unchanged previously-Rejected content remains Rejected; a genuine
            # source signature change may reopen Review Required.
            self.rescan_paths(restored)
        return {"restored": len(restored), "paths": restored, "errors": errors}

    def rebuild_library_intelligence(self, *, backup_target: str | Path | None = None) -> dict[str, Any]:
        """Quiesce writers, verify rollback backup, then rebuild derived intelligence."""
        if not self._all_configured_roots_accessible():
            raise RuntimeError(
                "Rebuild requires every configured MIDI source folder to be accessible. "
                "Reconnect unavailable sources or deliberately remove/reconfigure them first; saved Library data was not cleared."
            )
        target = Path(backup_target) if backup_target else (
            self.paths.backups / time.strftime("MIDI-Library-Pre-Rebuild-%Y%m%d-%H%M%S.zip")
        )
        state: dict[str, object] | None = None
        backup_path = ""
        cleared = False
        try:
            with self._maintenance("derived library rebuild", acquire_timeout=5.0):
                state = self._quiesce_destructive_work()
                # Backup occurs after quiescence so scan/deep/relationship DBs
                # describe one committed maintenance boundary.
                backup_path = str(create_library_intelligence_backup(self.paths, target).path)
                # Clear rebuildable/derived stores first. The authoritative Fast
                # catalogue and lifecycle truth are the final destructive commit
                # boundary so a failure while clearing Deep/relationship data
                # cannot make a populated Library suddenly appear empty. The
                # verified backup remains available for restoration of any
                # derived work already cleared before a later failure.
                try:
                    self.relationships.clear_derived()
                    self.library_index.clear()
                    self.scan.database.clear()
                except (sqlite3.Error, OSError, RuntimeError) as exc:
                    raise RuntimeError(
                        "Library rebuild stopped before the authoritative catalogue reset completed. "
                        f"Verified rollback backup retained at: {backup_path}. "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                with self._lock:
                    self._candidate_index = None
                    self._candidate_summaries = []
                    self._candidate_title_map = {}
                    self._candidate_family = {}
                    self._candidate_indexed_family_paths = {}
                    self._candidate_fingerprint = {}
                    self._guided_ready_families = set()
                    self._assisted_ready_families = set()
                    self._guided_ready_family_count = 0
                    self._assisted_ready_family_count = 0
                    self._index_revision += 1
                    self._relationship_priority_paths.clear()
                self._record_catalogue_delta(full_refresh=True)
                if state.get("policy") == DeepAnalysisPolicy.BACKGROUND:
                    self.scan.set_persistent_policy(DeepAnalysisPolicy.BACKGROUND)
                cleared = True
        finally:
            # Never launch the rebuilding scan while the destructive-maintenance
            # gate is still held. If backup failed before anything was cleared,
            # restore the previous background work instead.
            if state is not None and not cleared:
                self._resume_after_maintenance(state, rescan=False)
        if not cleared:
            return {"backup": backup_path, "scan_started": False}
        if self._stop.is_set():
            return {"backup": backup_path, "scan_started": False}
        self._relationship_cancel.clear()
        started = self.start_incremental_scan()
        if not started and state is not None:
            self._resume_after_maintenance(state, rescan=False)
        return {"backup": backup_path, "scan_started": bool(started)}

    def backup(self, target: str | Path) -> str:
        # A library-intelligence backup spans several SQLite databases. Each
        # database can be copied online safely, but rollback is much stronger if
        # all of them represent the same committed boundary. Quiesce mutable
        # scan/deep/relationship writers briefly, create/verify the backup, then
        # resume the user's prior background policy.
        state: dict[str, object] | None = None
        try:
            with self._maintenance("Library Intelligence backup", acquire_timeout=5.0):
                state = self._quiesce_destructive_work(timeout=MAINTENANCE_QUIESCE_SECONDS)
                result = create_library_intelligence_backup(self.paths, Path(target))
        finally:
            # The `with` releases the maintenance gate before this finally block
            # runs, so start_* methods can safely restore prior background work.
            if state is not None:
                self._resume_after_maintenance(state)
        return str(result.path)

    def status(self) -> dict[str, Any]:
        metrics, metrics_age = self._status_metrics_snapshot()
        catalogue = dict(metrics.get("catalogue") or {})
        health = dict(metrics.get("deep_health") or {})
        readiness = dict(metrics.get("readiness") or {})
        lifecycle = dict(metrics.get("lifecycle") or {})
        relationship_metrics = dict(metrics.get("relationships") or {})
        deep = dict(metrics.get("deep_counts") or {})
        deep.update(self.scan.deep_runtime_state())
        now = time.monotonic()
        with self._lock:
            progress = self._last_progress
            scan_running = bool(self._scan_thread and self._scan_thread.is_alive())
            relationship_running = bool(self._relationship_thread and self._relationship_thread.is_alive())
            relationship_revision = self._index_revision
            guided_ready_family_count = self._guided_ready_family_count
            assisted_ready_family_count = self._assisted_ready_family_count
            last_deep_path = self._last_deep_path
            last_deep_state = self._last_deep_state
            errors = list(self._recent_errors)
            maintenance_state = self._maintenance_state
            scan_transition_state = str(self._scan_transition_state or "")
            deep_transition_state = str(self._deep_transition_state or "")
            recent_deep = [stamp for stamp in self._deep_completion_times if now - stamp <= 120.0]
        deep_rate = 0.0
        if len(recent_deep) >= 2:
            span = recent_deep[-1] - recent_deep[0]
            if span >= 1.0:
                deep_rate = (len(recent_deep) - 1) / span
        if deep_rate <= 0.0:
            historical_seconds = float(health.get("average_seconds") or 0.0)
            if historical_seconds > 0.0:
                deep_rate = max(0.0, float(self.worker_count) / historical_seconds)
        deep_remaining = int(deep.get("remaining") or 0)
        deep_failed = int(deep.get("failed") or 0)
        deep_retryable_failed = int(deep.get("failed_retryable") or 0)
        deep_persistent_failed = int(deep.get("failed_persistent") or 0)
        deep_complete = int(deep.get("complete") or 0)
        deep_total = int(deep.get("total") or (deep_remaining + deep_failed + deep_complete))
        deep_policy = str(deep.get("policy") or "later")
        deep_active = int(deep.get("queue_active") or 0)
        deep_depth = int(deep.get("queue_depth") or 0)
        deep_seeding = bool(int(deep.get("queue_seeding") or 0))
        if deep_transition_state in {"pausing", "cancelling"}:
            deep_state = deep_transition_state
        elif deep_active > 0:
            deep_state = "running"
        elif deep_remaining > 0 and deep_policy == DeepAnalysisPolicy.BACKGROUND.value:
            deep_state = "starting" if deep_depth <= 0 and not deep_seeding else "running"
        elif deep_remaining > 0:
            deep_state = "paused"
        elif deep_retryable_failed > 0:
            deep_state = "failed"
        elif deep_persistent_failed > 0:
            deep_state = "attention"
        elif deep_failed > 0:
            # Compatibility fallback for databases created before attempt counts
            # were available. Treat unknown failures as reviewable, not a loop.
            deep_state = "attention"
        elif deep_total > 0 and deep_complete >= deep_total:
            deep_state = "complete"
        else:
            deep_state = "idle"
        deep_running = deep_state in {"running", "starting", "pausing", "cancelling"}
        deep["state"] = deep_state
        deep["transition"] = deep_transition_state
        deep["recoverable"] = deep_remaining + deep_retryable_failed
        deep["failed_retryable"] = deep_retryable_failed
        deep["failed_persistent"] = deep_persistent_failed
        deep["rate_per_second"] = deep_rate if deep_rate > 0.0 else None
        deep["eta_seconds"] = (deep_remaining / deep_rate) if deep_running and deep_remaining > 0 and deep_rate > 0.0 else None
        with self._startup_lock:
            startup_state = str(self._startup_state)
            startup_error = str(self._startup_error)
            startup_running = bool(self._startup_thread and self._startup_thread.is_alive())
        return {
            "engine": "running",
            "startup": {"state": startup_state, "running": startup_running, "error": startup_error},
            "uptime_seconds": int(time.monotonic() - self._started_monotonic),
            "status_metrics_age_seconds": metrics_age,
            "throughput_mode": self.throughput_mode,
            "deep_workers": self.worker_count,
            "roots": self.configured_roots(),
            "accessible_roots": list(self._source_availability_snapshot()[0]),
            "unavailable_roots": list(self._source_availability_snapshot()[1]),
            "manager_auto_start_analysis": bool(self.settings.get("manager_auto_start_analysis", True)),
            "maintenance": maintenance_state,
            "storage": {
                "data_root": str(self.paths.data_root),
                "library_root": str(self.paths.library),
                "quick_database": str(self.paths.library / "library_scan.sqlite3"),
                "detail_database": str(self.paths.library / "library_index.sqlite3"),
                "relationship_database": str(self.paths.library / "library_relationships.sqlite3"),
                "quick_schema_version": int(QUICK_SCHEMA_VERSION),
                "detail_schema_version": int(ANALYSIS_SCHEMA_VERSION),
                "relationship_algorithm_version": int(RELATIONSHIP_ALGORITHM_VERSION),
                "portable": bool(self.paths.portable),
            },
            "catalogue": catalogue,
            "deep": deep,
            "deep_health": health,
            "readiness": readiness,
            "lifecycle": lifecycle,
            "family_reconciliation": {
                "state": str(self._family_reconciliation_state),
                "current": int(self._family_reconciliation_current),
                "total": int(self._family_reconciliation_total),
                "changed_paths": int(self._family_reconciliation_changed),
                **dict(self._family_reconciliation_last_result),
            },
            "catalogue_revision": int(self._catalogue_revision),
            "discovery_graph": dict(self._graph_summary),
            "relationships": {
                "count": int(relationship_metrics.get("count") or 0),
                "guided_count": int(relationship_metrics.get("guided_count") or 0),
                "assisted_count": int(relationship_metrics.get("assisted_count") or 0),
                "anchors_built": int(relationship_metrics.get("anchors_built") or 0),
                "assisted_anchors_built": int(relationship_metrics.get("assisted_anchors_built") or 0),
                "guided_ready_families": int(guided_ready_family_count),
                "assisted_ready_families": int(assisted_ready_family_count),
                "building": relationship_running,
                "suspended_for_fast": bool(scan_running),
                "suspended_for_recovery": bool(scan_running and self._scan_operation_kind == "fast_recovery"),
                "suspend_reason": (
                    "unreadable_retry" if scan_running and self._scan_operation_kind == "fast_recovery"
                    else "fast_scan" if scan_running
                    else ""
                ),
                "index_revision": relationship_revision,
            },
            "scan": {
                "running": scan_running,
                "operation_kind": str(self._scan_operation_kind),
                "operation_state": (
                    # A coordinator acknowledgement is the authoritative pause
                    # boundary.  Do not keep reporting the transient ``pausing``
                    # label merely because the caller has not yet unwound its
                    # pause_scan() stack/finally block.  Target UI and clients may
                    # observe status concurrently with that final unwind.
                    "cancelling" if scan_running and self.scan.cancel_event.is_set()
                    else "paused" if scan_running and self.scan.scan_paused()
                    else scan_transition_state if scan_transition_state
                    else "pausing" if scan_running and self.scan.scan_pause_requested()
                    else str(self._scan_operation_state)
                ),
                "worker_limit": int(self._scan_worker_limit),
                "worker_processes": int(self.deep_pool.snapshot().get("active_processes") or 0),
                "phase": progress.phase,
                "current": int(progress.current),
                "total": int(progress.total),
                "current_file": progress.current_file,
                "current_folder": progress.current_folder,
                "message": progress.message,
                "rate_per_second": progress.rate_per_second,
                "eta_seconds": progress.eta_seconds,
                "paused": bool(scan_running and self.scan.scan_paused()),
                "pause_requested": bool(scan_running and self.scan.scan_pause_requested()),
            },
            "last_deep": {"path": last_deep_path, "state": last_deep_state},
            "recent_errors": errors,
        }

    def pause_scan(self) -> bool:
        with self._lock:
            running = bool(self._scan_thread and self._scan_thread.is_alive())
        if not running:
            return False
        with self._lock:
            self._scan_transition_state = "pausing"
        try:
            deadline = time.monotonic() + FAST_PAUSE_TRANSITION_SECONDS
            self.scan.pause_scan()
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeError("No Fast Scan pause budget remained for analyzer preemption.")
            self.deep_pool.preempt_active_requests(timeout=min(ANALYZER_PREEMPT_SECONDS, remaining))
            while True:
                if self.scan.scan_pause_acknowledged():
                    return True
                with self._lock:
                    thread = self._scan_thread
                if thread is None or not thread.is_alive():
                    # Scan reached its terminal boundary while Pause was in flight.
                    self.scan.resume_scan()
                    return False
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    raise RuntimeError(
                        f"Fast Scan did not acknowledge Pause within {FAST_PAUSE_TRANSITION_SECONDS:g} seconds."
                    )
                self.scan.wait_until_scan_paused(min(0.05, remaining))
        finally:
            with self._lock:
                self._scan_transition_state = ""

    def resume_scan(self) -> bool:
        with self._lock:
            running = bool(self._scan_thread and self._scan_thread.is_alive())
        if running:
            self.scan.resume_scan()
        return running

    def stop_scan(self) -> None:
        with self._lock:
            self._scan_transition_state = "cancelling"
        try:
            self.scan.cancel_scan()
            # QA-only deterministic state hold lets the target-Windows Manager poll
            # the authoritative CANCELLING state before the bounded preemption/join
            # completes. It is inert in normal product use.
            try:
                qa_hold = max(0.0, min(3.0, float(
                    os.environ.get("MIDI_LIBRARY_QA_HOLD_FAST_CANCEL_SECONDS", "0") or 0
                )))
            except ValueError:
                qa_hold = 0.0
            if qa_hold > 0.0:
                self._stop.wait(qa_hold)
            with self._lock:
                thread = self._scan_thread
            if thread is not None and thread.is_alive():
                deadline = time.monotonic() + FAST_CANCEL_TRANSITION_SECONDS
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    raise RuntimeError("No Fast Scan cancel budget remained for analyzer preemption.")
                self.deep_pool.preempt_active_requests(timeout=min(ANALYZER_PREEMPT_SECONDS, remaining))
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    raise RuntimeError(
                        f"Fast Scan did not reach its persistent cancel boundary within "
                        f"{FAST_CANCEL_TRANSITION_SECONDS:g} seconds."
                    )
        finally:
            with self._lock:
                self._scan_transition_state = ""

    def request_shutdown(self) -> None:
        """Publish the one-way Engine shutdown intent without closing databases.

        The daemon calls this *before* it drains already-admitted IPC handlers.
        In-flight maintenance can therefore finish or roll back coherently, but
        its normal completion path can no longer restart Fast/Deep/relationship
        work while shutdown is waiting for that command to return.
        """
        self._stop.set()
        self._relationship_cancel.set()
        # Publish the analyzer pool's one-way no-new-process barrier before
        # scan cancellation/drain. A worker that borrowed a runner immediately
        # before Close must not be able to spawn a fresh Python analyzer after
        # Engine shutdown ownership has begun.
        self.deep_pool.request_shutdown()
        self.scan.cancel_scan()

    def close(self, timeout: float = ENGINE_SHUTDOWN_HARD_SECONDS) -> None:
        """Reach one deterministic Engine shutdown boundary under one deadline.

        Every nested stage consumes the same absolute budget.  No sequence of
        individually reasonable joins is allowed to add up to a minute while the
        Manager has already given up waiting.
        """
        deadline = time.monotonic() + max(1.0, float(timeout))
        self.request_shutdown()
        self._watcher.stop()

        cleanup_errors: list[str] = []

        def left() -> float:
            return max(0.0, deadline - time.monotonic())

        # Maintenance commands run on daemon IPC handler threads and can own
        # filesystem + SQLite mutation state.  Shutdown must never close those
        # stores underneath an already-admitted Quarantine/Restore/Rebuild/Backup
        # transaction.  `_stop` above prevents new admission; acquiring the same
        # gate here proves the previous owner has reached its coherent
        # commit-or-rollback boundary.  One shared deadline still applies.
        maintenance_owned = False
        remaining = left()
        if remaining > 0:
            maintenance_owned = self._maintenance_gate.acquire(timeout=remaining)
        if not maintenance_owned:
            with self._lock:
                owner = self._maintenance_state or "unknown maintenance owner"
            cleanup_errors.append(
                f"maintenance boundary did not release before the shared Engine shutdown deadline ({owner})"
            )

        try:
            # Only touch worker/database owners once the maintenance mutation
            # boundary is exclusively ours. If it could not be acquired, fail
            # closed and leave process-level watchdog recovery to the daemon/client
            # rather than racing SQLite teardown against an active transaction.
            if maintenance_owned:
                try:
                    remaining = left()
                    if remaining <= 0:
                        raise RuntimeError("no shutdown budget remained for analyzer workers")
                    self.deep_pool.stop(timeout=min(ANALYZER_PREEMPT_SECONDS, remaining))
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    cleanup_errors.append(f"analysis worker pool: {type(exc).__name__}: {exc}")

                for thread, label in (
                    (self._startup_thread, "Engine startup worker"),
                    (self._scan_thread, "Fast-scan coordinator"),
                    (self._relationship_thread, "relationship builder"),
                    (self._watch_thread, "library watch coordinator"),
                    (self._status_metrics_refresh_thread, "status-metrics database reader"),
                ):
                    if thread is None or not thread.is_alive() or thread is threading.current_thread():
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        cleanup_errors.append(f"{label} exceeded the shared Engine shutdown deadline")
                        continue
                    thread.join(timeout=remaining)
                    if thread.is_alive():
                        cleanup_errors.append(f"{label} did not stop before the shared Engine shutdown deadline")

                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("no shutdown budget remained for scan/database cleanup")
                    self.scan.close(timeout=remaining)
                except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as exc:
                    cleanup_errors.append(f"scan/database service: {type(exc).__name__}: {exc}")
                try:
                    self.library_index.close()
                except (OSError, RuntimeError, sqlite3.Error) as exc:
                    cleanup_errors.append(f"library index: {type(exc).__name__}: {exc}")
                try:
                    self.relationships.close()
                except (OSError, RuntimeError, sqlite3.Error) as exc:
                    cleanup_errors.append(f"relationship store: {type(exc).__name__}: {exc}")
        finally:
            if maintenance_owned:
                self._maintenance_gate.release()

        if cleanup_errors:
            raise RuntimeError("Library Engine shutdown was incomplete: " + "; ".join(cleanup_errors))

