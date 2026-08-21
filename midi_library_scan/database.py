from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Callable, Iterable, Iterator, Sequence

from .family_identity import family_id_for_result, family_id_from_fields
from .models import CatalogMatch, DuplicateKind, QuickScanResult
from .lifecycle import (EligibilityState, LifecycleState, RejectionCode, VALIDATION_RULESET_VERSION, deep_failure_reason,
                        quick_failure_reason, quick_health_reasons)

SCHEMA_VERSION = 15
SQL_ROOT = Path(__file__).with_name("sql")


def _load_sql(name: str) -> str:
    return (SQL_ROOT / name).read_text(encoding="utf-8")


SCHEMA = _load_sql("schema_v15.sql")
UPSERT_SQL = _load_sql("upsert_v15.sql")


def _iter_sql_statements(script: str) -> Iterator[str]:
    buffer: list[str] = []
    for line in script.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()
    remainder = "\n".join(buffer).strip()
    if remainder:
        raise ValueError("Incomplete SQL schema resource")


def _chunks(values: Sequence[str], size: int = 700) -> Iterator[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _json_list(raw: str | None) -> list:
    """Decode the catalogue's small JSON-list fields without paying JSON cost for the common empty case."""
    if raw is None or raw == "" or raw == "[]":
        return []
    value = json.loads(raw)
    if not isinstance(value, list):
        raise TypeError("Catalogue JSON list field does not contain a list")
    return value


class LibraryDatabase:
    """Persistent quick catalogue optimized for six-figure MIDI libraries."""

    def __init__(
        self, path: str | Path, *, recover_interrupted: bool = True, read_only: bool = False,
        busy_timeout_seconds: float = 30.0,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.read_only = bool(read_only)
        self._lock = RLock()
        busy_timeout = max(0.05, float(busy_timeout_seconds))
        if self.read_only:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(self.path, timeout=busy_timeout, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={max(1, int(busy_timeout * 1000))}")
        if not self.read_only:
            self._migrate_schema()
            # Only the owning application coordinator performs crash recovery. A
            # supervised child worker must not reset the parent's currently active row.
            if recover_interrupted:
                self.recover_interrupted_deep_analysis()

    def _migrate_schema(self) -> None:
        with self._lock:
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Library database schema {current} is newer than supported schema {SCHEMA_VERSION}."
                )
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.execute("BEGIN IMMEDIATE")
                # Target-Windows QA can hold an actual populated schema migration
                # inside its SQLite transaction. This is inert unless the dedicated
                # qualification environment variable is set. It lets Manager Close
                # prove bounded process-tree termination while SQLite has a live
                # migration transaction, followed by rollback/retry verification.
                if current < SCHEMA_VERSION:
                    try:
                        qa_hold = max(0.0, min(30.0, float(
                            os.environ.get("MIDI_LIBRARY_QA_HOLD_MIGRATION_SECONDS", "0") or 0
                        )))
                    except ValueError:
                        qa_hold = 0.0
                    if qa_hold > 0.0:
                        marker = self.path.parent / "qa-migration-active.txt"
                        try:
                            marker.write_text(
                                f"pid={os.getpid()} from={current} to={SCHEMA_VERSION}", encoding="utf-8"
                            )
                        except OSError:
                            pass
                        time.sleep(qa_hold)
                for statement in _iter_sql_statements(SCHEMA):
                    self._conn.execute(statement)
                columns = {
                    str(row[1])
                    for row in self._conn.execute("PRAGMA table_info(library_catalog_files_v11)").fetchall()
                }
                if "family_id" not in columns:
                    self._conn.execute("ALTER TABLE library_catalog_files_v11 ADD COLUMN family_id TEXT")
                if "deep_priority" not in columns:
                    self._conn.execute(
                        "ALTER TABLE library_catalog_files_v11 ADD COLUMN deep_priority INTEGER NOT NULL DEFAULT 70"
                    )
                if "deep_updated_utc" not in columns:
                    self._conn.execute("ALTER TABLE library_catalog_files_v11 ADD COLUMN deep_updated_utc TEXT")
                if "deep_attempt_count" not in columns:
                    self._conn.execute(
                        "ALTER TABLE library_catalog_files_v11 ADD COLUMN deep_attempt_count INTEGER NOT NULL DEFAULT 0"
                    )
                if "deep_last_error" not in columns:
                    self._conn.execute("ALTER TABLE library_catalog_files_v11 ADD COLUMN deep_last_error TEXT")
                if "deep_last_duration_seconds" not in columns:
                    self._conn.execute("ALTER TABLE library_catalog_files_v11 ADD COLUMN deep_last_duration_seconds REAL")
                lifecycle_columns = {
                    "lifecycle_state": "TEXT NOT NULL DEFAULT 'discovered'",
                    "eligibility_state": "TEXT NOT NULL DEFAULT 'unresolved'",
                    "rejection_code": "TEXT",
                    "rejection_detail": "TEXT",
                    "lifecycle_validation_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "lifecycle_validation_ruleset": "INTEGER NOT NULL DEFAULT 0",
                    "last_validation_utc": "TEXT",
                    "lifecycle_updated_utc": "TEXT NOT NULL DEFAULT ''",
                    "resolved_utc": "TEXT",
                    "original_source_path": "TEXT",
                    "quarantined_utc": "TEXT",
                }
                for column_name, definition in lifecycle_columns.items():
                    if column_name not in columns:
                        # SQLite rejects ALTER TABLE ... ADD COLUMN when the
                        # default is non-constant on a populated table.  Fresh
                        # schema-15 databases may use CURRENT_TIMESTAMP, but an
                        # in-place upgrade of a real v14 catalogue must add a
                        # constant-default column and backfill it explicitly.
                        self._conn.execute(
                            f"ALTER TABLE library_catalog_files_v11 ADD COLUMN {column_name} {definition}"
                        )
                # Migrated catalogues receive a real lifecycle timestamp after
                # the constant-default ADD COLUMN above.  All lifecycle write
                # paths also maintain this value with CURRENT_TIMESTAMP.
                self._conn.execute(
                    "UPDATE library_catalog_files_v11 SET lifecycle_updated_utc=CURRENT_TIMESTAMP "
                    "WHERE lifecycle_updated_utc IS NULL OR lifecycle_updated_utc=''"
                )
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_library_family_id ON library_catalog_files_v11(family_id)")
                # Deep completion resolves one composition family at a time. On a
                # 96K catalogue, SQLite otherwise preferred the broad Deep-queue
                # index and scanned most catalogued rows to find the current Mash
                # representative. Keep this query at the family/index boundary.
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_library_family_mash_v15 "
                    "ON library_catalog_files_v11(family_id,quick_status,lifecycle_state,eligibility_state,path)"
                )
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_library_lifecycle_state_v15 ON library_catalog_files_v11(lifecycle_state)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_library_eligibility_state_v15 ON library_catalog_files_v11(eligibility_state)")
                # A validation-rules change is a legitimate reason to give an
                # unchanged review item one fresh pass.  Otherwise the same bytes
                # are never queued indefinitely.
                self._conn.execute(
                    "UPDATE library_catalog_files_v11 SET lifecycle_validation_attempts=0,"
                    "lifecycle_validation_ruleset=? WHERE eligibility_state='needs_review' "
                    "AND lifecycle_validation_ruleset<?",
                    (VALIDATION_RULESET_VERSION, VALIDATION_RULESET_VERSION),
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_library_deep_queue_v13 "
                    "ON library_catalog_files_v11(quick_status,deep_status,deep_priority,family_id)"
                )
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                self._conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(?)", (SCHEMA_VERSION,))
                # Schema-15 turns scan/deep facts into an explicit lifecycle.
                # Preserve completed analysis as ANALYZED/UNRESOLVED until the
                # Engine can join it to persisted readiness and bless it. Fast
                # or Deep failures enter Review rather than being silently usable.
                if current < 15:
                    self._conn.execute(
                        "UPDATE library_catalog_files_v11 SET "
                        "lifecycle_state=CASE "
                        " WHEN quick_status='failed' OR deep_status='failed' THEN 'review_required' "
                        " WHEN quick_status='catalogued' AND deep_status='complete' THEN 'analyzed' "
                        " WHEN quick_status='catalogued' THEN 'validated' ELSE 'discovered' END,"
                        "eligibility_state=CASE "
                        " WHEN quick_status='failed' OR deep_status='failed' THEN 'needs_review' ELSE 'unresolved' END,"
                        "last_validation_utc=COALESCE(last_validation_utc,last_scanned_utc),"
                        "lifecycle_updated_utc=CURRENT_TIMESTAMP "
                        "WHERE lifecycle_state='discovered' AND eligibility_state='unresolved'"
                    )
                tables = [
                    str(row[0])
                    for row in self._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND (name LIKE 'library_catalog_files_v%' OR name LIKE 'library_scan_settings_v%')"
                    ).fetchall()
                ]
                keep = {"library_catalog_files_v11", "library_scan_settings_v11"}
                for name in tables:
                    if name in keep:
                        continue
                    suffix = name.rsplit("_v", 1)[-1]
                    if suffix.isdigit() and int(suffix) < SCHEMA_VERSION:
                        self._conn.execute(f'DROP TABLE IF EXISTS "{name}"')
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise RuntimeError("Library database is read-only in this process")
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Library database is closed")
            try:
                self._conn.execute("BEGIN")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            if not self.read_only:
                self._conn.commit()
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                except sqlite3.Error:
                    self._conn.commit()
            self._conn.close()
            self._conn = None

    def upsert_many(self, results: list[QuickScanResult]) -> None:
        rows = []
        existing: dict[str, tuple[str, int, int, str, int]] = {}
        if results:
            with self._lock:
                for chunk in _chunks([str(result.path) for result in results]):
                    placeholders = ",".join("?" for _ in chunk)
                    for row in self._conn.execute(
                        f"SELECT path,sha256,size_bytes,modified_ns,lifecycle_state,lifecycle_validation_attempts "
                        f"FROM library_catalog_files_v11 WHERE path IN ({placeholders})",
                        tuple(chunk),
                    ).fetchall():
                        existing[str(row[0])] = (
                            str(row[1] or ""), int(row[2] or 0), int(row[3] or 0),
                            str(row[4] or LifecycleState.DISCOVERED.value), int(row[5] or 0),
                        )
        for result in results:
            match = result.catalog_match
            family_id = result.family_id or family_id_for_result(result)
            result.family_id = family_id
            base_priority = 48 if match.status in {"exact", "high"} else 58 if match.status == "probable" else 70
            rows.append((
                result.path, result.filename, result.size_bytes, result.modified_ns, result.sha256,
                result.midi_type, result.ticks_per_beat, result.track_count, result.active_channel_count,
                result.note_count, result.duration_seconds, result.bpm, result.bpm_min, result.bpm_max,
                result.tempo_change_count, result.key, result.mode, result.key_confidence, result.key_source,
                result.meter, result.arrangement_type, json.dumps(result.program_families),
                json.dumps(result.embedded_text), match.composition_id, match.artist, match.title, match.genres,
                match.primary_genre, match.confidence, match.status, json.dumps(match.evidence),
                result.duplicate_group, result.composition_group, result.content_duplicate_group,
                result.duplicate_kind.value, result.musical_fingerprint, result.status, result.error,
                family_id, base_priority,
            ))
        if not rows:
            return
        with self.transaction() as conn:
            conn.executemany(UPSERT_SQL, rows)
            for result in results:
                previous = existing.get(str(result.path))
                changed = bool(previous and (
                    previous[0] != str(result.sha256 or "")
                    or previous[1] != int(result.size_bytes or 0)
                    or previous[2] != int(result.modified_ns or 0)
                ))
                previous_state = previous[3] if previous else LifecycleState.DISCOVERED.value
                validation_attempts = 0 if changed else (previous[4] if previous else 0)
                if result.status == "failed":
                    reason = quick_failure_reason(result.error)
                    # Rejected is a persistent curation decision for unchanged
                    # source content. A forced re-scan (including the automatic
                    # post-quarantine restore verification) must not silently
                    # reopen the Review queue just because the parser observed
                    # the same deterministic failure again. Only a genuine
                    # source identity/signature change resets attempts and may
                    # reopen lifecycle.
                    preserve_rejected = bool(
                        previous and not changed and previous_state == LifecycleState.REJECTED.value
                    )
                    if preserve_rejected:
                        conn.execute(
                            "UPDATE library_catalog_files_v11 SET last_validation_utc=CURRENT_TIMESTAMP,"
                            "lifecycle_updated_utc=CURRENT_TIMESTAMP,original_source_path=NULL,quarantined_utc=NULL "
                            "WHERE path=?",
                            (str(result.path),),
                        )
                        continue
                    deliberate_revalidation = validation_attempts > 0 and previous_state in {
                        LifecycleState.REVIEW_REQUIRED.value, LifecycleState.REVALIDATED.value,
                    }
                    reject = bool(deliberate_revalidation and reason.deterministic)
                    lifecycle_state = LifecycleState.REJECTED.value if reject else LifecycleState.REVIEW_REQUIRED.value
                    eligibility_state = EligibilityState.REJECTED.value if reject else EligibilityState.NEEDS_REVIEW.value
                    rejection_code = reason.code if reject else None
                    resolved_sql = "CURRENT_TIMESTAMP" if reject else "NULL"
                    conn.execute(
                        f"UPDATE library_catalog_files_v11 SET lifecycle_state=?,eligibility_state=?,"
                        f"rejection_code=?,rejection_detail=?,lifecycle_validation_attempts=?,lifecycle_validation_ruleset=?,"
                        f"last_validation_utc=CURRENT_TIMESTAMP,lifecycle_updated_utc=CURRENT_TIMESTAMP,"
                        f"resolved_utc={resolved_sql},original_source_path=NULL,quarantined_utc=NULL WHERE path=?",
                        (lifecycle_state, eligibility_state, rejection_code, str(result.error or reason.label),
                         validation_attempts, VALIDATION_RULESET_VERSION, str(result.path)),
                    )
                else:
                    # An unchanged normal incremental scan must not revoke a
                    # previously resolved eligibility decision. Reclassification
                    # is required only for new/changed content or a deliberate
                    # revalidation path. This preserves the curated contract
                    # across routine 96K inventory checks.
                    preserve_resolved = bool(
                        previous and not changed and previous_state in {
                            LifecycleState.BLESSED.value, LifecycleState.REJECTED.value,
                        }
                    )
                    if preserve_resolved:
                        conn.execute(
                            "UPDATE library_catalog_files_v11 SET last_validation_utc=CURRENT_TIMESTAMP,"
                            "lifecycle_updated_utc=CURRENT_TIMESTAMP WHERE path=?",
                            (str(result.path),),
                        )
                    else:
                        health_reasons = quick_health_reasons(
                            track_count=result.track_count, note_count=result.note_count,
                            duration_seconds=result.duration_seconds,
                        )
                        revalidated = bool(
                            not changed and validation_attempts > 0
                            and previous_state in {LifecycleState.REVIEW_REQUIRED.value, LifecycleState.REVALIDATED.value}
                        )
                        if health_reasons:
                            lifecycle_state = LifecycleState.REVALIDATED.value if revalidated else LifecycleState.REVIEW_REQUIRED.value
                            eligibility_state = EligibilityState.NEEDS_REVIEW.value
                            detail = "; ".join(health_reasons)
                        else:
                            lifecycle_state = LifecycleState.REVALIDATED.value if revalidated else LifecycleState.VALIDATED.value
                            eligibility_state = EligibilityState.UNRESOLVED.value
                            detail = None
                        conn.execute(
                            "UPDATE library_catalog_files_v11 SET lifecycle_state=?,eligibility_state=?,"
                            "rejection_code=NULL,rejection_detail=?,lifecycle_validation_attempts=?,lifecycle_validation_ruleset=?,"
                            "last_validation_utc=CURRENT_TIMESTAMP,lifecycle_updated_utc=CURRENT_TIMESTAMP,"
                            "resolved_utc=NULL,original_source_path=NULL,quarantined_utc=NULL WHERE path=?",
                            (lifecycle_state, eligibility_state, detail, validation_attempts, VALIDATION_RULESET_VERSION, str(result.path)),
                        )

    def revalidation_allowed_paths(self, paths: Iterable[str | Path]) -> list[str]:
        """Return Needs Review paths eligible for one pass in this ruleset."""
        values = sorted({str(Path(path).expanduser().resolve(strict=False)) for path in paths if str(path).strip()})
        if not values:
            return []
        allowed: list[str] = []
        with self._lock:
            for chunk in _chunks(values):
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT path FROM library_catalog_files_v11 WHERE eligibility_state='needs_review' "
                    f"AND lifecycle_validation_attempts<1 AND path IN ({placeholders})", tuple(chunk),
                ).fetchall()
                allowed.extend(str(row[0]) for row in rows)
        return allowed

    def mark_revalidation_started(self, paths: Iterable[str | Path]) -> int:
        values = self.revalidation_allowed_paths(paths)
        if not values:
            return 0
        changed = 0
        with self.transaction() as conn:
            for chunk in _chunks(values):
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"UPDATE library_catalog_files_v11 SET lifecycle_state='review_required',"
                    f"eligibility_state='needs_review',lifecycle_validation_attempts=lifecycle_validation_attempts+1,"
                    f"lifecycle_validation_ruleset=?,last_validation_utc=CURRENT_TIMESTAMP,"
                    f"lifecycle_updated_utc=CURRENT_TIMESTAMP,resolved_utc=NULL "
                    f"WHERE eligibility_state='needs_review' AND lifecycle_validation_attempts<1 "
                    f"AND path IN ({placeholders})",
                    (VALIDATION_RULESET_VERSION, *tuple(chunk)),
                )
                changed += int(cursor.rowcount or 0)
        return changed

    def mark_deep_failure_lifecycle(self, path: str | Path, error: str | None = None) -> str:
        value = str(Path(path).expanduser().resolve(strict=False))
        reason = deep_failure_reason(error)
        with self._lock:
            row = self._conn.execute(
                "SELECT deep_attempt_count FROM library_catalog_files_v11 WHERE path=?", (value,)
            ).fetchone()
            attempts = int(row[0] or 0) if row else 0
            reject = bool(reason.deterministic and attempts >= 2)
            lifecycle = LifecycleState.REJECTED.value if reject else LifecycleState.REVIEW_REQUIRED.value
            eligibility = EligibilityState.REJECTED.value if reject else EligibilityState.NEEDS_REVIEW.value
            self._conn.execute(
                "UPDATE library_catalog_files_v11 SET lifecycle_state=?,eligibility_state=?,"
                "rejection_code=?,rejection_detail=?,last_validation_utc=CURRENT_TIMESTAMP,"
                "lifecycle_updated_utc=CURRENT_TIMESTAMP,resolved_utc=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END "
                "WHERE path=?",
                (lifecycle, eligibility, reason.code if reject else None, str(error or reason.label), int(reject), value),
            )
            self._conn.commit()
        return lifecycle

    def mark_analyzed(self, path: str | Path) -> bool:
        value = str(Path(path).expanduser().resolve(strict=False))
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE library_catalog_files_v11 SET lifecycle_state='analyzed',eligibility_state='unresolved',"
                "last_validation_utc=COALESCE(deep_updated_utc,CURRENT_TIMESTAMP),lifecycle_updated_utc=CURRENT_TIMESTAMP,"
                "resolved_utc=NULL WHERE path=? AND quick_status='catalogued' AND deep_status IN ('complete','finalizing') "
                "AND lifecycle_state<>'quarantined'",
                (value,),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    def set_rejected(self, path: str | Path, rejection_code: str, detail: str) -> bool:
        value = str(Path(path).expanduser().resolve(strict=False))
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE library_catalog_files_v11 SET lifecycle_state='rejected',eligibility_state='rejected',"
                "rejection_code=?,rejection_detail=?,last_validation_utc=CURRENT_TIMESTAMP,"
                "lifecycle_updated_utc=CURRENT_TIMESTAMP,resolved_utc=CURRENT_TIMESTAMP WHERE path=? "
                "AND lifecycle_state<>'quarantined'",
                (str(rejection_code), str(detail), value),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    def set_rejected_many(self, states: Iterable[tuple[str, str, str]]) -> int:
        prepared = [(str(code), str(detail), str(Path(path).expanduser().resolve(strict=False))) for path, code, detail in states]
        if not prepared:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "UPDATE library_catalog_files_v11 SET lifecycle_state='rejected',eligibility_state='rejected',"
                "rejection_code=?,rejection_detail=?,last_validation_utc=COALESCE(deep_updated_utc,last_scanned_utc,CURRENT_TIMESTAMP),"
                "lifecycle_updated_utc=CURRENT_TIMESTAMP,resolved_utc=CURRENT_TIMESTAMP WHERE path=? "
                "AND lifecycle_state<>'quarantined'", prepared,
            )
            changed = self._conn.total_changes - before
            self._conn.commit()
        return int(changed)

    def set_blessed_eligibility(self, path: str | Path, eligibility_state: str) -> bool:
        value = str(Path(path).expanduser().resolve(strict=False))
        allowed = {
            EligibilityState.MASH_READY.value, EligibilityState.FUSION_CANDIDATE.value,
            EligibilityState.USABLE_NOT_MASH.value,
        }
        state = str(eligibility_state)
        if state not in allowed:
            raise ValueError(f"Invalid blessed eligibility state: {state}")
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE library_catalog_files_v11 SET lifecycle_state='blessed',eligibility_state=?,"
                "rejection_code=NULL,rejection_detail=NULL,last_validation_utc=CURRENT_TIMESTAMP,"
                "lifecycle_updated_utc=CURRENT_TIMESTAMP,resolved_utc=CURRENT_TIMESTAMP "
                "WHERE path=? AND quick_status='catalogued' AND deep_status IN ('complete','finalizing')",
                (state, value),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    def keep_reviewed_not_mash(self, paths: Iterable[str | Path]) -> tuple[list[str], list[str]]:
        """Resolve human-reviewed readable files as Library-usable / Not Mash.

        This is an explicit curation decision, not analyzer inference.  It is
        intended for valid MIDI files that Fast Scan can read but current Deep
        Analysis cannot fully profile (for example Type-2 or safety-limit cases).
        Malformed/unreadable rows are never blessed through this path.
        """
        values = sorted({str(Path(path).expanduser().resolve(strict=False)) for path in paths if str(path).strip()})
        if not values:
            return [], []
        safe_rejected_codes = {
            RejectionCode.UNSUPPORTED_STRUCTURE.value,
            RejectionCode.SAFETY_RESOURCE_LIMIT.value,
            RejectionCode.REMIXER_INCOMPATIBLE.value,
        }
        changed: list[str] = []
        refused: list[str] = []
        with self.transaction() as conn:
            for value in values:
                row = conn.execute(
                    "SELECT quick_status,eligibility_state,rejection_code,rejection_detail "
                    "FROM library_catalog_files_v11 WHERE path=?",
                    (value,),
                ).fetchone()
                if row is None:
                    refused.append(value)
                    continue
                quick_status = str(row[0] or "")
                eligibility = str(row[1] or "")
                rejection_code = str(row[2] or "")
                detail = " ".join(str(row[3] or "").split())
                allowed = (
                    quick_status == "catalogued"
                    and (
                        eligibility == EligibilityState.NEEDS_REVIEW.value
                        or (
                            eligibility == EligibilityState.REJECTED.value
                            and rejection_code in safe_rejected_codes
                        )
                    )
                )
                if not allowed:
                    refused.append(value)
                    continue
                curated_detail = "User kept after review"
                if detail:
                    curated_detail += f" · prior review: {detail}"
                cursor = conn.execute(
                    "UPDATE library_catalog_files_v11 SET lifecycle_state='blessed',"
                    "eligibility_state='usable_not_mash',rejection_code=NULL,rejection_detail=?,"
                    "last_validation_utc=CURRENT_TIMESTAMP,lifecycle_updated_utc=CURRENT_TIMESTAMP,"
                    "resolved_utc=CURRENT_TIMESTAMP WHERE path=?",
                    (curated_detail, value),
                )
                if cursor.rowcount:
                    changed.append(value)
                else:
                    refused.append(value)
        return changed, refused

    def lifecycle_for_path(self, path: str | Path) -> dict[str, object] | None:
        value = str(Path(path).expanduser().resolve(strict=False))
        with self._lock:
            row = self._conn.execute(
                "SELECT lifecycle_state,eligibility_state,rejection_code,rejection_detail,"
                "lifecycle_validation_attempts,last_validation_utc,resolved_utc,original_source_path,quarantined_utc "
                "FROM library_catalog_files_v11 WHERE path=?", (value,),
            ).fetchone()
        return dict(row) if row is not None else None

    def blessed_paths(self, eligibility: str | None = None) -> set[str]:
        params: tuple[object, ...] = ()
        query = "SELECT path FROM library_catalog_files_v11 WHERE lifecycle_state='blessed'"
        if eligibility is not None:
            query += " AND eligibility_state=?"
            params = (str(eligibility),)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return {str(row[0]) for row in rows}

    def lifecycle_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT eligibility_state,COUNT(*) FROM library_catalog_files_v11 GROUP BY eligibility_state"
            ).fetchall()
            total_row = self._conn.execute("SELECT COUNT(*) FROM library_catalog_files_v11").fetchone()
        counts = {str(row[0] or EligibilityState.UNRESOLVED.value): int(row[1] or 0) for row in rows}
        result = {
            "discovered": int(total_row[0] or 0) if total_row else 0,
            "mash_ready": counts.get(EligibilityState.MASH_READY.value, 0),
            "fusion_candidate": counts.get(EligibilityState.FUSION_CANDIDATE.value, 0),
            "usable_not_mash": counts.get(EligibilityState.USABLE_NOT_MASH.value, 0),
            "needs_review": counts.get(EligibilityState.NEEDS_REVIEW.value, 0),
            "rejected": counts.get(EligibilityState.REJECTED.value, 0),
            "quarantined": counts.get(EligibilityState.QUARANTINED.value, 0),
            "unresolved": counts.get(EligibilityState.UNRESOLVED.value, 0),
        }
        result["usable"] = result["mash_ready"] + result["fusion_candidate"] + result["usable_not_mash"]
        return result

    def mark_quarantined(self, path: str | Path, quarantine_path: str | Path) -> bool:
        source = str(Path(path).expanduser().resolve(strict=False))
        target = str(Path(quarantine_path).expanduser().resolve(strict=False))
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM library_catalog_files_v11 WHERE path=?", (source,)).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE library_catalog_files_v11 SET path=?,filename=?,original_source_path=?,"
                "lifecycle_state='quarantined',eligibility_state='quarantined',quarantined_utc=CURRENT_TIMESTAMP,"
                "lifecycle_updated_utc=CURRENT_TIMESTAMP,resolved_utc=CURRENT_TIMESTAMP WHERE path=?",
                (target, Path(target).name, source, source),
            )
            self._conn.commit()
        return True

    def restore_quarantined(self, quarantine_path: str | Path) -> tuple[str, str] | None:
        current = str(Path(quarantine_path).expanduser().resolve(strict=False))
        with self._lock:
            row = self._conn.execute(
                "SELECT original_source_path FROM library_catalog_files_v11 "
                "WHERE path=? AND lifecycle_state='quarantined'", (current,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        return current, str(row[0])

    def complete_quarantine_restore(self, quarantine_path: str | Path, restored_path: str | Path) -> bool:
        current = str(Path(quarantine_path).expanduser().resolve(strict=False))
        target = str(Path(restored_path).expanduser().resolve(strict=False))
        with self._lock:
            # A quarantined Rejected MIDI remains Rejected when restored unless
            # a subsequent Fast parse proves that the source content genuinely
            # changed.  Reopening unchanged rejected content as Needs Review
            # creates a dead queue: its bounded revalidation attempt was already
            # consumed before quarantine.  Rows quarantined from Needs Review
            # have no rejection_code and correctly return to Needs Review.
            cursor = self._conn.execute(
                "UPDATE library_catalog_files_v11 SET path=?,filename=?,original_source_path=NULL,quarantined_utc=NULL,"
                "lifecycle_state=CASE WHEN rejection_code IS NOT NULL THEN 'rejected' ELSE 'review_required' END,"
                "eligibility_state=CASE WHEN rejection_code IS NOT NULL THEN 'rejected' ELSE 'needs_review' END,"
                "lifecycle_validation_attempts=lifecycle_validation_attempts,last_validation_utc=CURRENT_TIMESTAMP,"
                "lifecycle_updated_utc=CURRENT_TIMESTAMP,"
                "resolved_utc=CASE WHEN rejection_code IS NOT NULL THEN COALESCE(resolved_utc,CURRENT_TIMESTAMP) ELSE NULL END,"
                "deep_status='pending',deep_attempt_count=0,deep_last_error=NULL,deep_last_duration_seconds=NULL "
                "WHERE path=? AND lifecycle_state='quarantined'",
                (target, Path(target).name, current),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    def deep_error(self, path: str | Path) -> str:
        value = str(Path(path).expanduser().resolve(strict=False))
        with self._lock:
            row = self._conn.execute(
                "SELECT deep_last_error FROM library_catalog_files_v11 WHERE path=?", (value,)
            ).fetchone()
        return str(row[0] or "") if row is not None else ""

    def lifecycle_backfill_rows(self) -> list[dict[str, object]]:
        """Return unresolved analyzed rows needed for one-time schema-15 blessing."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,family_id,duplicate_kind,deep_status,track_count,note_count,duration_seconds FROM library_catalog_files_v11 "
                "WHERE quick_status='catalogued' AND lifecycle_state IN ('analyzed','validated','discovered') "
                "AND eligibility_state='unresolved'"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_blessed_eligibilities(self, states: Iterable[tuple[str, str]]) -> int:
        """Bless many completed paths in one transaction during lifecycle backfill."""
        prepared: list[tuple[str, str]] = []
        allowed = {
            EligibilityState.MASH_READY.value, EligibilityState.FUSION_CANDIDATE.value,
            EligibilityState.USABLE_NOT_MASH.value,
        }
        for path, eligibility in states:
            state = str(eligibility)
            if state in allowed:
                prepared.append((state, str(Path(path).expanduser().resolve(strict=False))))
        if not prepared:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "UPDATE library_catalog_files_v11 SET lifecycle_state='blessed',eligibility_state=?,"
                "rejection_code=NULL,rejection_detail=NULL,last_validation_utc=COALESCE(last_validation_utc,deep_updated_utc,last_scanned_utc),"
                "lifecycle_updated_utc=CURRENT_TIMESTAMP,resolved_utc=COALESCE(resolved_utc,CURRENT_TIMESTAMP) "
                "WHERE path=? AND quick_status='catalogued' AND deep_status IN ('complete','finalizing')",
                prepared,
            )
            changed = self._conn.total_changes - before
            self._conn.commit()
        return int(changed)

    def inventory_index(self) -> dict[str, tuple[int, int, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,size_bytes,modified_ns,quick_status FROM library_catalog_files_v11 "
                "WHERE lifecycle_state<>'quarantined'"
            ).fetchall()
        return {str(row[0]): (int(row[1]), int(row[2]), str(row[3] or "")) for row in rows}

    def quick_failed_paths(self, *, limit: int | None = None) -> list[str]:
        """Return every persisted Fast-scan failure without the review UI's old 5K cap."""
        query = "SELECT path FROM library_catalog_files_v11 WHERE quick_status='failed' ORDER BY path"
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (max(1, int(limit)),)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [str(row[0]) for row in rows]

    def reset_deep_analysis(self, paths: Iterable[str | Path]) -> int:
        """Invalidate selected derived analysis so it can be deliberately rebuilt.

        This is a user-directed operation for successfully catalogued files. It is
        intentionally separate from failure recovery and resets attempt/error state.
        """
        values = sorted({str(Path(path).resolve(strict=False)) for path in paths if str(path).strip()})
        if not values:
            return 0
        changed = 0
        with self.transaction() as conn:
            for chunk in _chunks(values):
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"UPDATE library_catalog_files_v11 SET deep_status='pending',deep_priority=0,"
                    f"deep_attempt_count=0,deep_last_error=NULL,deep_last_duration_seconds=NULL,"
                    f"deep_updated_utc=CURRENT_TIMESTAMP,lifecycle_state='validated',eligibility_state='unresolved',"
                    f"rejection_code=NULL,rejection_detail=NULL,resolved_utc=NULL,lifecycle_updated_utc=CURRENT_TIMESTAMP "
                    f"WHERE quick_status='catalogued' AND path IN ({placeholders})",
                    tuple(chunk),
                )
                changed += int(cursor.rowcount or 0)
        return changed

    def metadata_for_paths(self, paths: Iterable[str]) -> dict[str, tuple[str | None, str | None]]:
        values = sorted({str(path) for path in paths})
        result: dict[str, tuple[str | None, str | None]] = {}
        with self._lock:
            for chunk in _chunks(values):
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT path,family_id,sha256 FROM library_catalog_files_v11 WHERE path IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                result.update({str(row[0]): (row[1], row[2]) for row in rows})
        return result

    def delete_paths(self, paths: list[str] | tuple[str, ...] | set[str]) -> int:
        unique = sorted({str(Path(path).resolve()) for path in paths})
        if not unique:
            return 0
        with self.transaction() as conn:
            conn.executemany("DELETE FROM library_catalog_files_v11 WHERE path=?", [(path,) for path in unique])
        return len(unique)

    def checkpoint(self, *, truncate: bool = True) -> None:
        with self._lock:
            if self._conn is None:
                return
            mode = "TRUNCATE" if truncate else "PASSIVE"
            self._conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchall()

    @staticmethod
    def _ordered_pair(identity_a: str, identity_b: str) -> tuple[str, str]:
        a, b = str(identity_a).strip(), str(identity_b).strip()
        if not a or not b or a == b:
            raise ValueError("Ignored mash pairs require two distinct song identities")
        return (a, b) if a < b else (b, a)

    def ignore_pair(self, identity_a: str, identity_b: str, label_a: str = "Song A", label_b: str = "Song B") -> None:
        first, second = self._ordered_pair(identity_a, identity_b)
        labels = {str(identity_a).strip(): str(label_a), str(identity_b).strip(): str(label_b)}
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO ignored_mash_pairs_v12(identity_a,identity_b,label_a,label_b) VALUES(?,?,?,?) "
                "ON CONFLICT(identity_a,identity_b) DO UPDATE SET label_a=excluded.label_a,label_b=excluded.label_b",
                (first, second, labels.get(first, "Song A"), labels.get(second, "Song B")),
            )

    def restore_pair(self, identity_a: str, identity_b: str) -> None:
        first, second = self._ordered_pair(identity_a, identity_b)
        with self.transaction() as conn:
            conn.execute("DELETE FROM ignored_mash_pairs_v12 WHERE identity_a=? AND identity_b=?", (first, second))

    def is_pair_ignored(self, identity_a: str, identity_b: str) -> bool:
        try:
            first, second = self._ordered_pair(identity_a, identity_b)
        except ValueError:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ignored_mash_pairs_v12 WHERE identity_a=? AND identity_b=?", (first, second)
            ).fetchone()
        return row is not None

    def ignored_pairs(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT identity_a,identity_b,label_a,label_b,created_utc "
                "FROM ignored_mash_pairs_v12 ORDER BY created_utc DESC,label_a,label_b"
            ).fetchall()
        return [
            {"identity_a": str(row[0]), "identity_b": str(row[1]), "label_a": str(row[2]),
             "label_b": str(row[3]), "created_utc": str(row[4])}
            for row in rows
        ]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM library_catalog_files_v11")
            self._conn.commit()

    def _row_to_result(self, row: sqlite3.Row) -> QuickScanResult:
        match = CatalogMatch(
            composition_id=row["composition_id"], artist=row["catalog_artist"], title=row["catalog_title"],
            genres=row["catalog_genres"], primary_genre=row["catalog_primary_genre"],
            confidence=float(row["catalog_confidence"] or 0.0), status=row["catalog_status"] or "unmatched",
            evidence=_json_list(row["catalog_evidence_json"]),
        )
        try:
            duplicate_kind = DuplicateKind(row["duplicate_kind"] or "none")
        except ValueError:
            duplicate_kind = DuplicateKind.NONE
        return QuickScanResult(
            path=row["path"], filename=row["filename"], size_bytes=int(row["size_bytes"]),
            modified_ns=int(row["modified_ns"]), sha256=row["sha256"], midi_type=row["midi_type"],
            ticks_per_beat=row["ticks_per_beat"], track_count=int(row["track_count"] or 0),
            active_channel_count=int(row["active_channel_count"] or 0), note_count=int(row["note_count"] or 0),
            duration_seconds=row["duration_seconds"], bpm=row["bpm"], bpm_min=row["bpm_min"], bpm_max=row["bpm_max"],
            tempo_change_count=int(row["tempo_change_count"] or 0), key=row["detected_key"], mode=row["mode"],
            key_confidence=float(row["key_confidence"] or 0.0), key_source=row["key_source"] or "unavailable",
            meter=row["meter"], arrangement_type=row["arrangement_type"] or "Unknown arrangement",
            program_families=_json_list(row["program_families_json"]),
            embedded_text=_json_list(row["embedded_text_json"]), catalog_match=match,
            family_id=row["family_id"], duplicate_group=row["duplicate_group"], composition_group=row["composition_group"],
            content_duplicate_group=row["content_duplicate_group"], duplicate_kind=duplicate_kind,
            musical_fingerprint=row["musical_fingerprint"], status=row["quick_status"] or "catalogued", error=row["error"],
        )

    def _select_rows(self, roots: list[str | Path] | None) -> list[sqlite3.Row]:
        if roots is None:
            with self._lock:
                return self._conn.execute("SELECT * FROM library_catalog_files_v11 ORDER BY path").fetchall()
        if roots == []:
            return []
        exact: list[str] = []
        prefixes: list[str] = []
        for raw in roots:
            candidate = Path(raw).expanduser()
            try:
                resolved = candidate.resolve(strict=False)
            except OSError:
                continue
            # A .mid/.midi/.kar argument is an exact lookup even if it vanished.
            if resolved.suffix.casefold() in {".mid", ".midi", ".kar"}:
                exact.append(str(resolved))
            else:
                prefix = str(resolved).rstrip("\\/") + os.sep
                prefixes.append(_escape_like(prefix) + "%")
        # The overwhelmingly common Library hydration path is one configured
        # directory root.  Avoid materializing a second 96k-entry Python dict
        # and sorting it when SQLite can stream the already indexed rows in
        # deterministic path order.  Mixed exact/prefix lookups still use the
        # de-duplicating path below.
        if not exact and len(prefixes) == 1:
            with self._lock:
                return self._conn.execute(
                    "SELECT * FROM library_catalog_files_v11 "
                    "WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
                    (prefixes[0],),
                ).fetchall()

        found: dict[str, sqlite3.Row] = {}
        with self._lock:
            for chunk in _chunks(exact):
                placeholders = ",".join("?" for _ in chunk)
                for row in self._conn.execute(
                    f"SELECT * FROM library_catalog_files_v11 WHERE path IN ({placeholders})", tuple(chunk)
                ).fetchall():
                    found[str(row["path"])] = row
            for pattern in prefixes:
                for row in self._conn.execute(
                    "SELECT * FROM library_catalog_files_v11 WHERE path LIKE ? ESCAPE '\\'", (pattern,)
                ).fetchall():
                    found[str(row["path"])] = row
        return [found[path] for path in sorted(found, key=str.casefold)]

    def load_result(self, path: str | Path, *, validate_source: bool = False) -> QuickScanResult | None:
        try:
            resolved = str(Path(path).expanduser().resolve(strict=False))
        except OSError:
            resolved = str(path)
        with self._lock:
            row = self._conn.execute("SELECT * FROM library_catalog_files_v11 WHERE path=?", (resolved,)).fetchone()
        if row is None or (validate_source and not Path(resolved).exists()):
            return None
        return self._row_to_result(row)

    def load_results(self, roots: list[str | Path] | None = None, *, validate_sources: bool = True) -> list[QuickScanResult]:
        rows = self._select_rows(roots)
        results: list[QuickScanResult] = []
        for row in rows:
            if validate_sources and not Path(row["path"]).exists():
                continue
            try:
                results.append(self._row_to_result(row))
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                continue
        return results

    def family_index(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT path,family_id FROM library_catalog_files_v11 WHERE quick_status<>'failed'").fetchall()
        return {str(row[0]): str(row[1] or f"path:{row[0]}") for row in rows}

    def family_id_for_path(self, path: str | Path) -> str | None:
        try:
            resolved = str(Path(path).resolve(strict=False))
        except OSError:
            resolved = str(path)
        with self._lock:
            row = self._conn.execute("SELECT family_id FROM library_catalog_files_v11 WHERE path=?", (resolved,)).fetchone()
        return str(row[0]) if row and row[0] else None

    def family_paths(self, family_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM library_catalog_files_v11 WHERE family_id=? AND quick_status<>'failed' "
                "ORDER BY catalog_confidence DESC,note_count DESC,track_count DESC,path", (family_id,)
            ).fetchall()
        return [str(row[0]) for row in rows]

    def family_mash_ready_rows(self, family_id: str) -> list[dict[str, object]]:
        """Return only the currently authoritative Mash representatives for one family.

        Detailed Analysis completes one MIDI at a time. Expanding every member of
        a huge duplicate/version family on every completion is O(N^2) and makes
        Pause/Close depend on family cardinality. Persisted Mash representatives
        are the only prior rows needed for the incremental decision.
        """
        if not str(family_id or "").strip():
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,family_id,duplicate_kind,deep_status,track_count,note_count,duration_seconds "
                "FROM library_catalog_files_v11 WHERE family_id=? AND quick_status='catalogued' "
                "AND lifecycle_state='blessed' AND eligibility_state='mash_ready' ORDER BY path",
                (str(family_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def family_has_same_composition_versions(self, family_id: str) -> bool:
        """Bounded family context used by incremental lifecycle classification."""
        if not str(family_id or "").strip():
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM library_catalog_files_v11 WHERE family_id=? AND quick_status<>'failed' "
                "AND duplicate_kind='same_composition' LIMIT 1",
                (str(family_id),),
            ).fetchone()
        return row is not None


    def set_family_ids(self, assignments: Iterable[tuple[str, str]]) -> int:
        """Persist authoritative family IDs without reopening source MIDI files."""
        prepared = [
            (str(family_id), str(Path(path).expanduser().resolve(strict=False)))
            for path, family_id in assignments
            if str(path).strip() and str(family_id).strip()
        ]
        if not prepared:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "UPDATE library_catalog_files_v11 SET family_id=? WHERE path=? AND quick_status<>'failed'",
                prepared,
            )
            changed = self._conn.total_changes - before
            self._conn.commit()
        return int(changed)

    def duplicate_completed_family_groups(self) -> list[tuple[str, int]]:
        """Return provisional family IDs shared by 2+ Deep-complete catalogue rows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT family_id,COUNT(*) AS n FROM library_catalog_files_v11 "
                "WHERE deep_status='complete' AND family_id IS NOT NULL AND family_id<>'' "
                "GROUP BY family_id HAVING COUNT(*)>1"
            ).fetchall()
        return [(str(row[0]), int(row[1] or 0)) for row in rows]

    def family_rows(self, family_id: str, *, completed_only: bool = False) -> list[dict[str, object]]:
        if not str(family_id or "").strip():
            return []
        query = (
            "SELECT path,family_id,duplicate_kind,deep_status,track_count,note_count,duration_seconds,"
            "lifecycle_state,eligibility_state,catalog_confidence FROM library_catalog_files_v11 "
            "WHERE family_id=? AND quick_status='catalogued'"
        )
        if completed_only:
            query += " AND deep_status IN ('complete','finalizing')"
        query += " ORDER BY catalog_confidence DESC,note_count DESC,track_count DESC,path"
        with self._lock:
            rows = self._conn.execute(query, (str(family_id),)).fetchall()
        return [dict(row) for row in rows]

    def paths_with_quick_musical_fingerprint(self, fingerprint: str, *, limit: int = 160) -> list[str]:
        value = str(fingerprint or "").strip()
        if not value:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM library_catalog_files_v11 WHERE quick_status='catalogued' "
                "AND musical_fingerprint=? ORDER BY path LIMIT ?",
                (value, max(1, min(512, int(limit)))),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def get_scan_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM library_scan_settings_v11 WHERE key=?", (str(key),)
            ).fetchone()
        return str(row[0]) if row is not None else default

    def set_scan_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO library_scan_settings_v11(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )
            self._conn.commit()

    def backfill_family_ids(self, *, batch_size: int = 1000) -> int:
        """One-time v13 migration.  No source MIDI files are opened."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,filename,composition_id,catalog_artist,catalog_title FROM library_catalog_files_v11 "
                "WHERE family_id IS NULL OR family_id=''"
            ).fetchall()
        updated = 0
        for start in range(0, len(rows), max(1, int(batch_size))):
            batch = rows[start:start + batch_size]
            values = [
                (
                    family_id_from_fields(
                        filename=str(row["filename"] or Path(row["path"]).name),
                        composition_id=row["composition_id"], artist=row["catalog_artist"], title=row["catalog_title"],
                    ),
                    str(row["path"]),
                )
                for row in batch
            ]
            with self.transaction() as conn:
                conn.executemany("UPDATE library_catalog_files_v11 SET family_id=? WHERE path=?", values)
            updated += len(values)
        if updated:
            self.reconcile_all_duplicate_metadata()
        return updated

    def reconcile_all_duplicate_metadata(self) -> None:
        """SQL-only duplicate/version grouping; scales linearly without decoding 96k rows.

        Clear legacy non-exact grouping first.  Older releases used path-derived and
        fuzzy duplicate ids; leaving those values behind would make the v0.27 family
        index look inconsistent after migration.  Exact content groups remain valid.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE library_catalog_files_v11 SET composition_group=NULL,"
                "duplicate_group=CASE WHEN duplicate_kind='exact' THEN duplicate_group ELSE NULL END,"
                "duplicate_kind=CASE WHEN duplicate_kind='exact' THEN 'exact' ELSE 'none' END "
                "WHERE quick_status<>'failed'"
            )
            conn.execute(
                "UPDATE library_catalog_files_v11 SET composition_group=family_id, duplicate_group=family_id "
                "WHERE family_id IS NOT NULL AND family_id<>'' AND family_id IN ("
                "SELECT family_id FROM library_catalog_files_v11 WHERE quick_status<>'failed' "
                "GROUP BY family_id HAVING COUNT(*)>1)"
            )
            conn.execute(
                "UPDATE library_catalog_files_v11 SET duplicate_kind='same_composition' "
                "WHERE family_id IS NOT NULL AND family_id<>'' AND family_id IN ("
                "SELECT family_id FROM library_catalog_files_v11 WHERE quick_status<>'failed' "
                "GROUP BY family_id HAVING COUNT(*)>1) AND duplicate_kind='none'"
            )
            conn.execute(
                "UPDATE library_catalog_files_v11 SET content_duplicate_group='EXACT-'||substr(sha256,1,16), "
                "duplicate_group=COALESCE(duplicate_group,'EXACT-'||substr(sha256,1,16)), duplicate_kind='exact' "
                "WHERE sha256<>'' AND sha256 IN (SELECT sha256 FROM library_catalog_files_v11 "
                "WHERE quick_status<>'failed' GROUP BY sha256 HAVING COUNT(*)>1)"
            )

    def reconcile_duplicate_keys(self, family_ids: Iterable[str | None], hashes: Iterable[str | None]) -> None:
        families = sorted({str(value) for value in family_ids if value})
        sha_values = sorted({str(value) for value in hashes if value})
        with self.transaction() as conn:
            for family in families:
                count = int(conn.execute(
                    "SELECT COUNT(*) FROM library_catalog_files_v11 WHERE family_id=? AND quick_status<>'failed'", (family,)
                ).fetchone()[0])
                if count > 1:
                    conn.execute(
                        "UPDATE library_catalog_files_v11 SET composition_group=?,duplicate_group=?,"
                        "duplicate_kind=CASE WHEN duplicate_kind='exact' THEN 'exact' ELSE 'same_composition' END "
                        "WHERE family_id=?", (family, family, family)
                    )
                else:
                    conn.execute(
                        "UPDATE library_catalog_files_v11 SET composition_group=NULL,"
                        "duplicate_group=CASE WHEN duplicate_kind='exact' THEN duplicate_group ELSE NULL END,"
                        "duplicate_kind=CASE WHEN duplicate_kind='exact' THEN 'exact' ELSE 'none' END WHERE family_id=?",
                        (family,),
                    )
            for sha in sha_values:
                rows = conn.execute("SELECT path FROM library_catalog_files_v11 WHERE sha256=?", (sha,)).fetchall()
                group = f"EXACT-{sha[:16]}"
                if len(rows) > 1:
                    conn.execute(
                        "UPDATE library_catalog_files_v11 SET content_duplicate_group=?,duplicate_kind='exact',"
                        "duplicate_group=COALESCE(duplicate_group,?) WHERE sha256=?", (group, group, sha)
                    )
                else:
                    conn.execute(
                        "UPDATE library_catalog_files_v11 SET content_duplicate_group=NULL,"
                        "duplicate_kind=CASE WHEN composition_group IS NOT NULL THEN 'same_composition' ELSE 'none' END "
                        "WHERE sha256=?", (sha,)
                    )

    def deep_complete_paths(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT path FROM library_catalog_files_v11 WHERE deep_status='complete'").fetchall()
        return {str(row[0]) for row in rows}

    def deep_status(self, path: str | Path) -> str | None:
        try:
            resolved = str(Path(path).resolve(strict=False))
        except OSError:
            resolved = str(path)
        with self._lock:
            row = self._conn.execute("SELECT deep_status FROM library_catalog_files_v11 WHERE path=?", (resolved,)).fetchone()
        return str(row[0]) if row else None

    def recover_interrupted_deep_analysis(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE library_catalog_files_v11 SET deep_status='pending',deep_updated_utc=CURRENT_TIMESTAMP,"
                "error=CASE WHEN error IS NULL OR error='' THEN 'Interrupted before completion; safely requeued.' ELSE error END "
                "WHERE deep_status IN ('analyzing','finalizing')"
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    def remove_missing(self) -> int:
        # Explicit maintenance only.  Startup never calls this for a 96k library.
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM library_catalog_files_v11 WHERE lifecycle_state<>'quarantined'"
            ).fetchall()
        missing = [str(row[0]) for row in rows if not Path(row[0]).exists()]
        return self.delete_paths(missing) if missing else 0

    def set_policy(self, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO library_scan_settings_v11(key,value) VALUES('deep_analysis_policy',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,)
            )
            self._conn.commit()

    def get_policy(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM library_scan_settings_v11 WHERE key='deep_analysis_policy'"
            ).fetchone()
        return str(row[0]) if row else None

    def mark_deep_status(
        self, path: str, status: str, error: str | None = None, *, duration_seconds: float | None = None
    ) -> None:
        """Persist one detailed-analysis transition without conflating quick-scan errors.

        Failed detailed analyses are terminal for automatic background scheduling.
        They can be explicitly retried, or a changed source file automatically
        resets them to pending through the quick-catalogue UPSERT.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE library_catalog_files_v11 SET deep_status=?,deep_updated_utc=CURRENT_TIMESTAMP,"
                "deep_attempt_count=deep_attempt_count + CASE WHEN ?='analyzing' THEN 1 ELSE 0 END,"
                "deep_last_error=CASE WHEN ?='failed' THEN ? WHEN ?='complete' THEN NULL ELSE deep_last_error END,"
                "deep_last_duration_seconds=CASE WHEN ? IS NOT NULL THEN ? ELSE deep_last_duration_seconds END "
                "WHERE path=?",
                (status, status, status, error, status, duration_seconds, duration_seconds, path),
            )
            self._conn.commit()

    def mark_deep_interrupted(self, path: str) -> None:
        """Return a shutdown-interrupted analysis to a retryable paused state.

        Entering ``analyzing`` increments ``deep_attempt_count``.  An Engine
        shutdown is not a failed MIDI analysis, so undo that increment and do
        not preserve a misleading failure reason.  The next Engine start can
        resume the row normally.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE library_catalog_files_v11 SET deep_status='paused',"
                "deep_updated_utc=CURRENT_TIMESTAMP,"
                "deep_attempt_count=MAX(0,deep_attempt_count-1),"
                "deep_last_error=NULL WHERE path=?",
                (path,),
            )
            self._conn.commit()

    def retry_failed_deep_analysis(self, paths: Iterable[str | Path] | None = None) -> int:
        """Explicitly requeue previously failed deep analyses.

        Background refill deliberately excludes failed rows so corrupt or
        pathological MIDIs do not consume the overnight queue forever.
        """
        if paths is None:
            with self._lock:
                cursor = self._conn.execute(
                    "UPDATE library_catalog_files_v11 SET deep_status='pending',deep_last_error=NULL,"
                    "deep_updated_utc=CURRENT_TIMESTAMP "
                    "WHERE quick_status='catalogued' AND deep_status='failed' AND deep_attempt_count<2"
                )
                self._conn.commit()
                return int(cursor.rowcount or 0)
        values = sorted({str(Path(path).resolve(strict=False)) for path in paths})
        if not values:
            return 0
        changed = 0
        with self.transaction() as conn:
            for chunk in _chunks(values):
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"UPDATE library_catalog_files_v11 SET deep_status='pending',deep_last_error=NULL,"
                    f"deep_updated_utc=CURRENT_TIMESTAMP "
                    f"WHERE deep_status='failed' AND deep_attempt_count<2 AND path IN ({placeholders})",
                    tuple(chunk),
                )
                changed += int(cursor.rowcount or 0)
        return changed

    def deep_health_stats(self) -> dict[str, float | int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) total, "
                "SUM(CASE WHEN deep_status='failed' THEN 1 ELSE 0 END) failed, "
                "SUM(deep_attempt_count) attempts, "
                "AVG(CASE WHEN deep_status='complete' THEN deep_last_duration_seconds END) avg_seconds "
                "FROM library_catalog_files_v11 WHERE quick_status='catalogued'"
            ).fetchone()
        return {
            "total": int(row[0] or 0) if row else 0,
            "failed": int(row[1] or 0) if row else 0,
            "attempts": int(row[2] or 0) if row else 0,
            "average_seconds": float(row[3] or 0.0) if row else 0.0,
        }

    def prioritize_paths(self, paths: Iterable[str | Path], priority: int) -> int:
        values = sorted({str(Path(path).resolve(strict=False)) for path in paths})
        if not values:
            return 0
        priority = max(0, min(99, int(priority)))
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE library_catalog_files_v11 SET deep_priority=MIN(deep_priority,?) WHERE path=?",
                [(priority, path) for path in values],
            )
        return len(values)

    def pending_paths(
        self, only_paths: list[str] | tuple[str, ...] | set[str] | None = None, *,
        limit: int | None = None, include_failed: bool = False,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[str]:
        """Return best family representatives first, entirely ordered by SQLite.

        Failed rows are excluded from normal background scheduling. They require
        explicit retry so one corrupt file cannot be reprocessed indefinitely.
        """
        allowed = sorted({str(Path(path).resolve(strict=False)) for path in only_paths}) if only_paths is not None else None
        base = (
            "SELECT path,COALESCE(NULLIF(family_id,''),path) family_id,deep_priority,catalog_confidence,note_count,track_count "
            "FROM library_catalog_files_v11 WHERE quick_status='catalogued' "
            + ("AND deep_status IN ('pending','paused','failed')" if include_failed else "AND deep_status IN ('pending','paused')")
        )
        params: list[object] = []
        if allowed is not None:
            if not allowed:
                return []
            # selected-only queues are small; chunk and combine before ranking.
            rows: list[sqlite3.Row] = []
            with self._lock:
                for chunk in _chunks(allowed):
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(self._conn.execute(base + f" AND path IN ({placeholders})", tuple(chunk)).fetchall())
            rows.sort(key=lambda row: (int(row["deep_priority"] or 70), -float(row["catalog_confidence"] or 0), -int(row["note_count"] or 0)))
            paths = [str(row["path"]) for row in rows]
            return paths[: max(1, int(limit))] if limit is not None else paths
        query = (
            "WITH pending AS (" + base + "), ranked AS ("
            "SELECT *,ROW_NUMBER() OVER (PARTITION BY family_id ORDER BY deep_priority ASC,catalog_confidence DESC,"
            "note_count DESC,track_count DESC,path) family_rank FROM pending) "
            "SELECT path FROM ranked ORDER BY family_rank ASC,deep_priority ASC,catalog_confidence DESC,note_count DESC,track_count DESC,path"
        )
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._lock:
            if cancel_check is not None:
                self._conn.set_progress_handler(lambda: 1 if cancel_check() else 0, 2000)
            try:
                rows = self._conn.execute(query, tuple(params)).fetchall()
            except sqlite3.OperationalError as exc:
                if cancel_check is not None and cancel_check() and "interrupt" in str(exc).casefold():
                    return []
                raise
            finally:
                if cancel_check is not None:
                    self._conn.set_progress_handler(None, 0)
        return [str(row[0]) for row in rows]

    def deep_status_counts(self, only_paths: list[str] | tuple[str, ...] | set[str] | None = None) -> dict[str, int]:
        """Return detailed-analysis state with explicit one-recovery semantics.

        ``deep_attempt_count`` is incremented whenever a row enters ``analyzing``.
        A failed row with one attempt is therefore eligible for one deliberate
        recovery pass; a failed row with two or more attempts is a persistent
        exception and must be reviewed rather than endlessly requeued.
        """
        counts: dict[str, int] = {}
        retryable_failed = 0
        persistent_failed = 0
        if only_paths is None:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT COALESCE(deep_status,'pending'),COUNT(*) FROM library_catalog_files_v11 "
                    "WHERE quick_status='catalogued' GROUP BY COALESCE(deep_status,'pending')"
                ).fetchall()
                failure_row = self._conn.execute(
                    "SELECT "
                    "SUM(CASE WHEN deep_status='failed' AND deep_attempt_count<2 THEN 1 ELSE 0 END),"
                    "SUM(CASE WHEN deep_status='failed' AND deep_attempt_count>=2 THEN 1 ELSE 0 END) "
                    "FROM library_catalog_files_v11 WHERE quick_status='catalogued'"
                ).fetchone()
            counts.update({str(row[0]): int(row[1]) for row in rows})
            retryable_failed = int((failure_row[0] if failure_row else 0) or 0)
            persistent_failed = int((failure_row[1] if failure_row else 0) or 0)
        else:
            allowed = sorted({str(Path(path).resolve(strict=False)) for path in only_paths})
            with self._lock:
                for chunk in _chunks(allowed):
                    placeholders = ",".join("?" for _ in chunk)
                    rows = self._conn.execute(
                        f"SELECT COALESCE(deep_status,'pending'),COUNT(*) FROM library_catalog_files_v11 "
                        f"WHERE quick_status='catalogued' AND path IN ({placeholders}) GROUP BY COALESCE(deep_status,'pending')",
                        tuple(chunk),
                    ).fetchall()
                    for row in rows:
                        key = str(row[0])
                        counts[key] = counts.get(key, 0) + int(row[1])
                    failure_row = self._conn.execute(
                        f"SELECT "
                        f"SUM(CASE WHEN deep_status='failed' AND deep_attempt_count<2 THEN 1 ELSE 0 END),"
                        f"SUM(CASE WHEN deep_status='failed' AND deep_attempt_count>=2 THEN 1 ELSE 0 END) "
                        f"FROM library_catalog_files_v11 WHERE quick_status='catalogued' AND path IN ({placeholders})",
                        tuple(chunk),
                    ).fetchone()
                    retryable_failed += int((failure_row[0] if failure_row else 0) or 0)
                    persistent_failed += int((failure_row[1] if failure_row else 0) or 0)
        for key in ("pending", "paused", "analyzing", "finalizing", "complete", "failed"):
            counts.setdefault(key, 0)
        counts["failed_retryable"] = retryable_failed
        counts["failed_persistent"] = persistent_failed
        counts["total"] = sum(counts[key] for key in ("pending", "paused", "analyzing", "finalizing", "complete", "failed"))
        counts["remaining"] = counts["pending"] + counts["paused"] + counts["analyzing"] + counts["finalizing"]
        counts["finished"] = counts["complete"] + counts["failed"]
        return counts


    def suspicious_results(
        self, *, short_seconds: float = 12.0, long_seconds: float = 30.0 * 60.0,
        tiny_note_count: int = 8, limit: int = 5000,
    ) -> list[QuickScanResult]:
        """Return review candidates with indexed SQL filtering, never a 96K Python walk."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM library_catalog_files_v11 WHERE "
                "quick_status='failed' OR track_count<=0 OR note_count<=? OR duration_seconds<=0.25 "
                "OR (duration_seconds IS NOT NULL AND duration_seconds<?) "
                "OR (duration_seconds IS NOT NULL AND duration_seconds>?) "
                "ORDER BY CASE WHEN quick_status='failed' THEN 0 WHEN note_count<=0 OR track_count<=0 THEN 1 "
                "WHEN duration_seconds<=0.25 THEN 2 WHEN note_count<=? THEN 3 ELSE 4 END, path LIMIT ?",
                (max(0, int(tiny_note_count)), max(0.1, float(short_seconds)), max(1.0, float(long_seconds)),
                 max(0, int(tiny_note_count)), max(1, int(limit))),
            ).fetchall()
        return [self._row_to_result(row) for row in rows]

    def catalogue_counts(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*),SUM(CASE WHEN quick_status='failed' THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN quick_status<>'failed' AND catalog_status IN ('exact','high') THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN quick_status<>'failed' AND catalog_status='probable' THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN error LIKE 'Safety limit:%' THEN 1 ELSE 0 END),"
                "COUNT(DISTINCT CASE WHEN quick_status<>'failed' THEN COALESCE(NULLIF(family_id,''),path) END) "
                "FROM library_catalog_files_v11"
            ).fetchone()
            exact = self._conn.execute(
                "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM library_catalog_files_v11 "
                "WHERE content_duplicate_group IS NOT NULL AND content_duplicate_group<>'' "
                "GROUP BY content_duplicate_group HAVING COUNT(*)>1)"
            ).fetchone()[0]
            versions = self._conn.execute(
                "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM library_catalog_files_v11 "
                "WHERE family_id IS NOT NULL AND family_id<>'' GROUP BY family_id HAVING COUNT(*)>1)"
            ).fetchone()[0]
            suspicious = self._conn.execute(
                "SELECT COUNT(*) FROM library_catalog_files_v11 WHERE "
                "quick_status='failed' OR track_count<=0 OR note_count<=8 OR duration_seconds<=0.25 "
                "OR (duration_seconds IS NOT NULL AND duration_seconds<12.0) "
                "OR (duration_seconds IS NOT NULL AND duration_seconds>1800.0)"
            ).fetchone()[0]
        total = int(row[0] or 0)
        failed = int(row[1] or 0)
        catalogued = max(0, total - failed)
        identified = int(row[2] or 0)
        probable = int(row[3] or 0)
        return {
            "total": total, "catalogued": catalogued, "failed": failed, "identified": identified,
            "probable_identity": probable, "unidentified": max(0, catalogued - identified - probable),
            "resource_limited": int(row[4] or 0), "distinct_families": int(row[5] or 0),
            "exact_duplicates": int(exact or 0), "likely_duplicates": int(versions or 0),
            "suspicious": int(suspicious or 0),
            "no_basic_health_flag": max(0, total - int(suspicious or 0)),
        }
