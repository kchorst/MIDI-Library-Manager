from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable, Iterable, Sequence

from core.remix_profile import MidiProfile, NoteTrackProfile, analyze_midi, is_generated_remix

ANALYSIS_SCHEMA_VERSION = 5
_DECODED_PROFILE_CACHE = 512


@dataclass(frozen=True, slots=True)
class IndexStats:
    discovered: int = 0
    analyzed: int = 0
    reused: int = 0
    skipped_generated: int = 0
    failed: int = 0
    cancelled: bool = False


def profile_from_dict(data: dict) -> MidiProfile:
    payload = dict(data)
    payload["note_tracks"] = [
        item if isinstance(item, NoteTrackProfile) else NoteTrackProfile(**item)
        for item in payload.get("note_tracks", [])
    ]
    return MidiProfile(**payload)


def discover_midi_paths(
    paths: Iterable[str | Path], *, include_generated: bool = False, recursive: bool = True,
) -> tuple[list[Path], int]:
    raw_paths = [Path(raw) for raw in paths]
    explicitly_added = {str(path.resolve()).casefold() for path in raw_paths if path.is_file()}
    discovered: list[Path] = []
    seen: set[str] = set()
    skipped_generated = 0
    for path in raw_paths:
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            finder = path.rglob if recursive else path.glob
            candidates = sorted([*finder("*.mid"), *finder("*.midi"), *finder("*.kar")])
        else:
            candidates = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            key = str(resolved).casefold()
            if key in seen:
                continue
            seen.add(key)
            if not include_generated and key not in explicitly_added and is_generated_remix(resolved):
                skipped_generated += 1
                continue
            discovered.append(resolved)
    return discovered, skipped_generated


def _chunks(values: Sequence[str], size: int = 700):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class LibraryIndex:
    """Persistent detailed-profile store with exact SQL lookups and a bounded LRU.

    v0.27 never decodes all completed detailed profiles merely because the app
    starts.  Heavy profiles are loaded only for selected/shortlisted songs and
    are evicted from memory when the LRU limit is reached.
    """

    def __init__(self, database_path: str | Path, *, read_only: bool = False) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, MidiProfile] = OrderedDict()
        if self.read_only:
            uri = self.database_path.resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(self.database_path, timeout=30, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        if not self.read_only:
            self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS midi_profiles (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    profile_json TEXT NOT NULL,
                    analyzed_at REAL NOT NULL,
                    error TEXT,
                    analysis_version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(midi_profiles)").fetchall()}
            if "analysis_version" not in columns:
                self._conn.execute("ALTER TABLE midi_profiles ADD COLUMN analysis_version INTEGER NOT NULL DEFAULT 1")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_midi_profiles_mtime ON midi_profiles(mtime_ns)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_midi_profiles_version_error ON midi_profiles(analysis_version,error)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS midi_profile_readiness (
                    path TEXT PRIMARY KEY,
                    analysis_version INTEGER NOT NULL,
                    guided_eligible INTEGER NOT NULL,
                    guided_confidence INTEGER NOT NULL,
                    guided_level TEXT NOT NULL,
                    assisted_eligible INTEGER NOT NULL,
                    assisted_confidence INTEGER NOT NULL,
                    assisted_level TEXT NOT NULL,
                    title TEXT NOT NULL,
                    composer TEXT NOT NULL,
                    style TEXT NOT NULL,
                    felt_bpm REAL,
                    tonic_pc INTEGER,
                    mode TEXT,
                    meter_family TEXT,
                    bars REAL NOT NULL,
                    note_count INTEGER NOT NULL,
                    track_count INTEGER NOT NULL,
                    arrangement_type TEXT NOT NULL,
                    orchestration_readiness INTEGER NOT NULL,
                    melody_extraction_confidence INTEGER NOT NULL,
                    analyzed_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_profile_readiness_guided "
                "ON midi_profile_readiness(analysis_version,guided_eligible,guided_confidence)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_profile_readiness_assisted "
                "ON midi_profile_readiness(analysis_version,assisted_eligible,assisted_confidence)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_profile_readiness_title "
                "ON midi_profile_readiness(analysis_version,title)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_profile_readiness_artist_form "
                "ON midi_profile_readiness(analysis_version,composer,meter_family,bars)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS midi_profile_identity_keys (
                    path TEXT NOT NULL,
                    identity_key TEXT NOT NULL,
                    analysis_version INTEGER NOT NULL,
                    PRIMARY KEY(path,identity_key)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_profile_identity_key_lookup "
                "ON midi_profile_identity_keys(analysis_version,identity_key,path)"
            )
            self._conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """Compatibility hook retained for regression instrumentation.

        v0.27 uses one persistent connection; callers should not open per-query
        connections.
        """
        return self._conn

    def _remember(self, profile: MidiProfile) -> MidiProfile:
        self._cache[profile.path] = profile
        self._cache.move_to_end(profile.path)
        while len(self._cache) > _DECODED_PROFILE_CACHE:
            self._cache.popitem(last=False)
        return profile

    def invalidate(self, path: str | Path | None = None) -> None:
        with self._lock:
            if path is None:
                self._cache.clear()
                return
            try:
                resolved = str(Path(path).resolve(strict=False))
            except OSError:
                resolved = str(path)
            self._cache.pop(resolved, None)

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM midi_profiles")
            self._conn.execute("DELETE FROM midi_profile_readiness")
            self._conn.execute("DELETE FROM midi_profile_identity_keys")
            self._conn.commit()
            self._cache.clear()

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            if not self.read_only:
                self._conn.commit()
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
                except sqlite3.Error as exc:
                    _checkpoint_error = exc
            self._conn.close()
            self._conn = None
            self._cache.clear()

    def _rows_for_paths(self, paths: Iterable[str | Path] | None) -> list[sqlite3.Row]:
        if paths is None:
            with self._lock:
                return self._conn.execute(
                    "SELECT path,profile_json FROM midi_profiles WHERE error IS NULL AND analysis_version=? ORDER BY path",
                    (ANALYSIS_SCHEMA_VERSION,),
                ).fetchall()
        values = list(paths)
        if not values:
            return []
        exact: list[str] = []
        prefixes: list[str] = []
        for raw in values:
            candidate = Path(raw).expanduser()
            try:
                resolved = candidate.resolve(strict=False)
            except OSError:
                continue
            if resolved.suffix.casefold() in {".mid", ".midi", ".kar"}:
                exact.append(str(resolved))
            else:
                prefixes.append(_escape_like(str(resolved).rstrip("\\/") + os.sep) + "%")
        found: dict[str, sqlite3.Row] = {}
        with self._lock:
            for chunk in _chunks(exact):
                placeholders = ",".join("?" for _ in chunk)
                query = (
                    f"SELECT path,profile_json FROM midi_profiles WHERE error IS NULL AND analysis_version=? "
                    f"AND path IN ({placeholders})"
                )
                for row in self._conn.execute(query, (ANALYSIS_SCHEMA_VERSION, *chunk)).fetchall():
                    found[str(row["path"])] = row
            for pattern in prefixes:
                for row in self._conn.execute(
                    "SELECT path,profile_json FROM midi_profiles WHERE error IS NULL AND analysis_version=? "
                    "AND path LIKE ? ESCAPE '\\'",
                    (ANALYSIS_SCHEMA_VERSION, pattern),
                ).fetchall():
                    found[str(row["path"])] = row
        return [found[path] for path in sorted(found, key=str.casefold)]

    def load_cached(self, paths: Iterable[str | Path] | None = None, *, validate_sources: bool = True) -> list[MidiProfile]:
        path_values = None if paths is None else list(paths)
        if path_values == []:
            return []
        rows = self._rows_for_paths(path_values)
        result: list[MidiProfile] = []
        for row in rows:
            path = str(row["path"])
            if validate_sources and not Path(path).exists():
                continue
            with self._lock:
                cached = self._cache.get(path)
                if cached is not None:
                    self._cache.move_to_end(path)
                    result.append(cached)
                    continue
            try:
                profile = profile_from_dict(json.loads(row["profile_json"]))
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                continue
            with self._lock:
                self._remember(profile)
            result.append(profile)
        result.sort(key=lambda item: (item.composer.casefold(), item.title.casefold(), item.path.casefold()))
        return result

    def load_one(self, path: str | Path, *, validate_source: bool = False) -> MidiProfile | None:
        loaded = self.load_cached([path], validate_sources=validate_source)
        return loaded[0] if loaded else None

    def load_many(self, paths: Iterable[str | Path], *, validate_sources: bool = False) -> dict[str, MidiProfile]:
        return {profile.path: profile for profile in self.load_cached(paths, validate_sources=validate_sources)}

    def error_for(self, path: str | Path) -> str | None:
        try:
            source = str(Path(path).resolve(strict=False))
        except OSError:
            source = str(path)
        with self._lock:
            row = self._conn.execute("SELECT error FROM midi_profiles WHERE path=?", (source,)).fetchone()
        return str(row["error"]) if row is not None and row["error"] else None

    def delete_paths(self, paths: list[str] | tuple[str, ...] | set[str]) -> int:
        values = sorted({str(Path(path).resolve(strict=False)) for path in paths if path})
        if not values:
            return 0
        with self._lock:
            self._conn.executemany("DELETE FROM midi_profiles WHERE path=?", [(path,) for path in values])
            self._conn.executemany("DELETE FROM midi_profile_readiness WHERE path=?", [(path,) for path in values])
            self._conn.executemany("DELETE FROM midi_profile_identity_keys WHERE path=?", [(path,) for path in values])
            self._conn.commit()
            for path in values:
                self._cache.pop(path, None)
        return len(values)

    def remove_missing(self) -> int:
        with self._lock:
            rows = self._conn.execute("SELECT path FROM midi_profiles").fetchall()
        missing = [str(row["path"]) for row in rows if not Path(row["path"]).exists()]
        if not missing:
            return 0
        with self._lock:
            self._conn.executemany("DELETE FROM midi_profiles WHERE path=?", [(path,) for path in missing])
            self._conn.executemany("DELETE FROM midi_profile_readiness WHERE path=?", [(path,) for path in missing])
            self._conn.executemany("DELETE FROM midi_profile_identity_keys WHERE path=?", [(path,) for path in missing])
            self._conn.commit()
            for path in missing:
                self._cache.pop(path, None)
        return len(missing)

    @staticmethod
    def _readiness_values(profile: MidiProfile, analyzed_at: float | None = None) -> tuple[object, ...]:
        # Local imports avoid a module cycle while keeping the readiness table
        # authoritative with the same gates used by the actual workflows.
        from core.assisted_studio import assisted_source_gate
        from core.guided_beginner import guided_source_gate

        guided = guided_source_gate(profile)
        assisted = assisted_source_gate(profile)
        return (
            profile.path, ANALYSIS_SCHEMA_VERSION, int(bool(guided.eligible)), int(guided.confidence), str(guided.level),
            int(bool(assisted.eligible)), int(assisted.confidence), str(assisted.level),
            str(profile.title or ""), str(profile.composer or ""), str(profile.style or ""),
            float(profile.felt_bpm) if profile.felt_bpm is not None else None,
            int(profile.tonic_pc) if profile.tonic_pc is not None else None, str(profile.mode or ""),
            str(profile.meter_family or profile.meter_display or ""), float(profile.bars or 0.0),
            int(profile.note_count or 0), int(profile.track_count or 0), str(profile.arrangement_type or ""),
            int(profile.orchestration_readiness or 0), int(profile.melody_extraction_confidence or 0),
            float(analyzed_at if analyzed_at is not None else time.time()),
        )

    def _upsert_readiness_locked(self, profile: MidiProfile, analyzed_at: float | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO midi_profile_readiness(
                path,analysis_version,guided_eligible,guided_confidence,guided_level,
                assisted_eligible,assisted_confidence,assisted_level,title,composer,style,
                felt_bpm,tonic_pc,mode,meter_family,bars,note_count,track_count,
                arrangement_type,orchestration_readiness,melody_extraction_confidence,analyzed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                analysis_version=excluded.analysis_version,guided_eligible=excluded.guided_eligible,
                guided_confidence=excluded.guided_confidence,guided_level=excluded.guided_level,
                assisted_eligible=excluded.assisted_eligible,assisted_confidence=excluded.assisted_confidence,
                assisted_level=excluded.assisted_level,title=excluded.title,composer=excluded.composer,style=excluded.style,
                felt_bpm=excluded.felt_bpm,tonic_pc=excluded.tonic_pc,mode=excluded.mode,meter_family=excluded.meter_family,
                bars=excluded.bars,note_count=excluded.note_count,track_count=excluded.track_count,
                arrangement_type=excluded.arrangement_type,orchestration_readiness=excluded.orchestration_readiness,
                melody_extraction_confidence=excluded.melody_extraction_confidence,analyzed_at=excluded.analyzed_at
            """,
            self._readiness_values(profile, analyzed_at),
        )
        self._replace_identity_keys_locked(profile)

    @staticmethod
    def _identity_key_rows(profile: MidiProfile) -> tuple[tuple[str, str, int], ...]:
        from core.song_identity import identity_index_keys
        return tuple(
            (str(profile.path), str(key), ANALYSIS_SCHEMA_VERSION)
            for key in identity_index_keys(profile)
        )

    def replace_identity_keys_batch(self, profiles: Iterable[MidiProfile]) -> int:
        """Persist bounded derived identity lookup keys for Deep profiles.

        The table is a rebuildable index, not new authoritative source data.  It
        lets incremental family reconciliation find opaque aliases at 96K scale
        without scanning every Deep JSON row.
        """
        items = tuple(profiles)
        if not items:
            return 0
        paths = [(str(profile.path),) for profile in items]
        rows = [row for profile in items for row in self._identity_key_rows(profile)]
        with self._lock:
            self._conn.executemany("DELETE FROM midi_profile_identity_keys WHERE path=?", paths)
            if rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO midi_profile_identity_keys(path,identity_key,analysis_version) VALUES(?,?,?)",
                    rows,
                )
            self._conn.commit()
        return len(items)

    def _replace_identity_keys_locked(self, profile: MidiProfile) -> None:
        self._conn.execute("DELETE FROM midi_profile_identity_keys WHERE path=?", (str(profile.path),))
        rows = self._identity_key_rows(profile)
        if rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO midi_profile_identity_keys(path,identity_key,analysis_version) VALUES(?,?,?)",
                rows,
            )

    def backfill_readiness_summaries(self, *, batch_size: int = 256, cancel_callback=None) -> int:
        """Create compact readiness rows for legacy deep profiles once, off the UI thread.

        Existing v0.26 detailed JSON remains untouched and reusable.  Each row is
        decoded at most once for this migration; future startups query compact
        indexed readiness columns instead of materializing thousands of profiles.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.path,p.profile_json,p.analyzed_at FROM midi_profiles p "
                "LEFT JOIN midi_profile_readiness r ON r.path=p.path AND r.analysis_version=? "
                "WHERE p.error IS NULL AND p.analysis_version=? AND r.path IS NULL ORDER BY p.path",
                (ANALYSIS_SCHEMA_VERSION, ANALYSIS_SCHEMA_VERSION),
            ).fetchall()
        updated = 0
        size = max(1, int(batch_size))
        for start in range(0, len(rows), size):
            if cancel_callback is not None and cancel_callback():
                break
            prepared: list[tuple[object, ...]] = []
            for row in rows[start:start + size]:
                try:
                    profile = profile_from_dict(json.loads(row["profile_json"]))
                    prepared.append(self._readiness_values(profile, float(row["analyzed_at"] or time.time())))
                except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                    continue
            if not prepared:
                continue
            with self._lock:
                self._conn.executemany(
                    """
                    INSERT INTO midi_profile_readiness(
                        path,analysis_version,guided_eligible,guided_confidence,guided_level,
                        assisted_eligible,assisted_confidence,assisted_level,title,composer,style,
                        felt_bpm,tonic_pc,mode,meter_family,bars,note_count,track_count,
                        arrangement_type,orchestration_readiness,melody_extraction_confidence,analyzed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        analysis_version=excluded.analysis_version,guided_eligible=excluded.guided_eligible,
                        guided_confidence=excluded.guided_confidence,guided_level=excluded.guided_level,
                        assisted_eligible=excluded.assisted_eligible,assisted_confidence=excluded.assisted_confidence,
                        assisted_level=excluded.assisted_level,title=excluded.title,composer=excluded.composer,style=excluded.style,
                        felt_bpm=excluded.felt_bpm,tonic_pc=excluded.tonic_pc,mode=excluded.mode,meter_family=excluded.meter_family,
                        bars=excluded.bars,note_count=excluded.note_count,track_count=excluded.track_count,
                        arrangement_type=excluded.arrangement_type,orchestration_readiness=excluded.orchestration_readiness,
                        melody_extraction_confidence=excluded.melody_extraction_confidence,analyzed_at=excluded.analyzed_at
                    """, prepared
                )
                self._conn.commit()
            updated += len(prepared)
            # This legacy migration runs on a worker thread, but decoding large
            # JSON profiles is Python/GIL-heavy. Yield briefly between batches so
            # the Qt GUI thread can repaint its visible startup activity banner.
            time.sleep(0.001)
        return updated


    def iter_profile_batches(self, *, batch_size: int = 256, cancel_callback=None):
        """Yield decoded deep profiles in bounded batches without hydrating the full Library.

        Family reconciliation in Library v0.39.2 derives identity from already-
        persisted Deep Analysis.  A dedicated read-only connection keeps the
        long sequential read away from the Engine's normal point-query handle.
        No source MIDI files are opened.
        """
        size = max(1, int(batch_size))
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT path,profile_json FROM midi_profiles "
                "WHERE error IS NULL AND analysis_version=? ORDER BY path",
                (ANALYSIS_SCHEMA_VERSION,),
            )
            while True:
                if cancel_callback is not None and cancel_callback():
                    return
                rows = cursor.fetchmany(size)
                if not rows:
                    return
                profiles: list[MidiProfile] = []
                for row in rows:
                    try:
                        profiles.append(profile_from_dict(json.loads(row["profile_json"])))
                    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                        continue
                if profiles:
                    yield tuple(profiles)
        finally:
            conn.close()

    def duplicate_identity_key_groups(self) -> list[tuple[str, int]]:
        """Return only derived identity keys shared by two or more Deep profiles."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT identity_key,COUNT(*) AS n FROM midi_profile_identity_keys "
                "WHERE analysis_version=? GROUP BY identity_key HAVING COUNT(*)>1",
                (ANALYSIS_SCHEMA_VERSION,),
            ).fetchall()
        return [(str(row["identity_key"]), int(row["n"] or 0)) for row in rows]

    def identity_paths_for_key(self, identity_key: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM midi_profile_identity_keys WHERE analysis_version=? AND identity_key=? ORDER BY path",
                (ANALYSIS_SCHEMA_VERSION, str(identity_key)),
            ).fetchall()
        return [str(row["path"]) for row in rows]

    def identity_neighbor_paths(self, profile: MidiProfile, *, limit: int = 160) -> list[str]:
        """Return a bounded deep-profile shortlist for one newly analyzed MIDI.

        The durable family predicate performs the final comparison.  This SQL
        shortlist only avoids comparing a new completion against all ~96K rows.
        """
        cap = max(16, min(512, int(limit)))
        found: list[str] = []
        seen: set[str] = {str(profile.path)}

        def add(rows) -> None:
            for row in rows:
                value = str(row[0])
                if value not in seen:
                    seen.add(value)
                    found.append(value)
                    if len(found) >= cap:
                        return

        with self._lock:
            from core.song_identity import identity_index_keys
            keys = tuple(identity_index_keys(profile))
            if keys:
                placeholders = ",".join("?" for _ in keys)
                add(self._conn.execute(
                    f"SELECT path FROM midi_profile_identity_keys WHERE analysis_version=? "
                    f"AND identity_key IN ({placeholders}) AND path<>? "
                    "GROUP BY path ORDER BY COUNT(*) DESC,path LIMIT ?",
                    (ANALYSIS_SCHEMA_VERSION, *keys, str(profile.path), cap),
                ).fetchall())
            title = str(profile.title or "").strip()
            composer = str(profile.composer or "").strip()
            if title:
                add(self._conn.execute(
                    "SELECT path FROM midi_profile_readiness WHERE analysis_version=? AND title=? "
                    "ORDER BY assisted_confidence DESC,path LIMIT ?",
                    (ANALYSIS_SCHEMA_VERSION, title, cap),
                ).fetchall())
            if len(found) < cap and composer and profile.bars:
                bars = float(profile.bars)
                low, high = max(0.0, bars * 0.80), bars * 1.20
                meter = str(profile.meter_family or profile.meter_display or "")
                add(self._conn.execute(
                    "SELECT path FROM midi_profile_readiness WHERE analysis_version=? AND composer=? "
                    "AND meter_family=? AND bars BETWEEN ? AND ? "
                    "ORDER BY ABS(bars-?),assisted_confidence DESC,path LIMIT ?",
                    (ANALYSIS_SCHEMA_VERSION, composer, meter, low, high, bars, cap),
                ).fetchall())
            if len(found) < cap and profile.bars:
                # Metadata-free aliases still deserve a small structural
                # neighborhood. Tight form/meter bounds keep this bounded; the
                # deep identity predicate rejects merely compatible songs.
                bars = float(profile.bars)
                low, high = max(0.0, bars * 0.94), bars * 1.06
                meter = str(profile.meter_family or profile.meter_display or "")
                add(self._conn.execute(
                    "SELECT path FROM midi_profile_readiness WHERE analysis_version=? AND meter_family=? "
                    "AND bars BETWEEN ? AND ? ORDER BY ABS(bars-?),path LIMIT ?",
                    (ANALYSIS_SCHEMA_VERSION, meter, low, high, bars, min(cap, 96)),
                ).fetchall())
        return found[:cap]

    def readiness_summary(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) total,"
                "SUM(CASE WHEN guided_eligible=1 THEN 1 ELSE 0 END) guided_ready,"
                "SUM(CASE WHEN assisted_eligible=1 THEN 1 ELSE 0 END) assisted_ready "
                "FROM midi_profile_readiness WHERE analysis_version=?",
                (ANALYSIS_SCHEMA_VERSION,),
            ).fetchone()
        return {
            "total": int(row["total"] or 0) if row else 0,
            "guided_ready": int(row["guided_ready"] or 0) if row else 0,
            "assisted_ready": int(row["assisted_ready"] or 0) if row else 0,
        }

    def readiness_map(self) -> dict[str, dict[str, object]]:
        """Return the current persisted readiness contract in one bounded query.

        Lifecycle migration uses this once at Engine startup so a pre-schema-15
        Library can be blessed without issuing one SQLite lookup per MIDI.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,guided_eligible,guided_confidence,guided_level,assisted_eligible,"
                "assisted_confidence,assisted_level FROM midi_profile_readiness WHERE analysis_version=?",
                (ANALYSIS_SCHEMA_VERSION,),
            ).fetchall()
        return {
            str(row["path"]): {
                "guided_eligible": bool(row["guided_eligible"]),
                "guided_confidence": int(row["guided_confidence"] or 0),
                "guided_level": str(row["guided_level"] or ""),
                "assisted_eligible": bool(row["assisted_eligible"]),
                "assisted_confidence": int(row["assisted_confidence"] or 0),
                "assisted_level": str(row["assisted_level"] or ""),
            }
            for row in rows
        }

    def readiness_for_path(self, path: str | Path) -> dict[str, object] | None:
        try:
            resolved = str(Path(path).resolve(strict=False))
        except OSError:
            resolved = str(path)
        with self._lock:
            row = self._conn.execute(
                "SELECT guided_eligible,guided_confidence,guided_level,assisted_eligible,"
                "assisted_confidence,assisted_level FROM midi_profile_readiness "
                "WHERE path=? AND analysis_version=?",
                (resolved, ANALYSIS_SCHEMA_VERSION),
            ).fetchone()
        if row is None:
            return None
        return {
            "guided_eligible": bool(row["guided_eligible"]),
            "guided_confidence": int(row["guided_confidence"] or 0),
            "guided_level": str(row["guided_level"] or ""),
            "assisted_eligible": bool(row["assisted_eligible"]),
            "assisted_confidence": int(row["assisted_confidence"] or 0),
            "assisted_level": str(row["assisted_level"] or ""),
        }

    def guided_ready_paths(self, *, limit: int = 1200, offset: int = 0) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM midi_profile_readiness WHERE analysis_version=? AND guided_eligible=1 "
                "ORDER BY guided_confidence DESC,orchestration_readiness DESC,note_count DESC,path LIMIT ? OFFSET ?",
                (ANALYSIS_SCHEMA_VERSION, max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def assisted_ready_paths(self, *, limit: int = 512) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM midi_profile_readiness WHERE analysis_version=? AND assisted_eligible=1 "
                "ORDER BY assisted_confidence DESC,orchestration_readiness DESC,note_count DESC,path LIMIT ?",
                (ANALYSIS_SCHEMA_VERSION, max(1, int(limit))),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def classify(
        self,
        paths: Iterable[str | Path],
        *,
        include_generated: bool = False,
        recursive: bool = True,
        force: bool = False,
        progress_callback: Callable[[int, int, str], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
        profile_transform: Callable[[MidiProfile], MidiProfile] | None = None,
    ) -> tuple[list[MidiProfile], IndexStats]:
        discovered, skipped_generated = discover_midi_paths(paths, include_generated=include_generated, recursive=recursive)
        profiles: list[MidiProfile] = []
        analyzed = reused = failed = 0
        cancelled = False
        total = len(discovered)
        if progress_callback:
            progress_callback(0, total, "Preparing classification queue…")
        for position, path in enumerate(discovered, 1):
            if cancel_callback and cancel_callback():
                cancelled = True
                break
            if progress_callback:
                progress_callback(position - 1, total, path.name)
            try:
                stat = path.stat()
            except OSError:
                failed += 1
                if progress_callback:
                    progress_callback(position, total, path.name)
                continue
            source = str(path)
            with self._lock:
                row = self._conn.execute(
                    "SELECT size,mtime_ns,profile_json,analysis_version FROM midi_profiles WHERE path=?", (source,)
                ).fetchone()
            if (
                not force and row is not None and int(row["size"]) == int(stat.st_size)
                and int(row["mtime_ns"]) == int(stat.st_mtime_ns)
                and int(row["analysis_version"]) == ANALYSIS_SCHEMA_VERSION
            ):
                try:
                    profile = profile_from_dict(json.loads(row["profile_json"]))
                    if profile_transform is not None:
                        profile = profile_transform(profile)
                        with self._lock:
                            self._conn.execute(
                                "UPDATE midi_profiles SET profile_json=? WHERE path=?",
                                (json.dumps(profile.to_dict(), ensure_ascii=False), source),
                            )
                            self._conn.commit()
                    profiles.append(profile)
                    with self._lock:
                        self._remember(profile)
                        self._upsert_readiness_locked(profile, time.time())
                        self._conn.commit()
                    reused += 1
                    if progress_callback:
                        progress_callback(position, total, path.name)
                    continue
                except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                    with self._lock:
                        self._conn.execute("DELETE FROM midi_profiles WHERE path=?", (source,))
                        self._conn.commit()
                        self._cache.pop(source, None)
            try:
                profile = analyze_midi(path)
                if profile_transform is not None:
                    profile = profile_transform(profile)
                profile_json = json.dumps(profile.to_dict(), ensure_ascii=False)
                with self._lock:
                    self._conn.execute(
                        """
                        INSERT INTO midi_profiles(path,size,mtime_ns,profile_json,analyzed_at,error,analysis_version)
                        VALUES (?,?,?,?,?,NULL,?)
                        ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                        profile_json=excluded.profile_json,analyzed_at=excluded.analyzed_at,error=NULL,
                        analysis_version=excluded.analysis_version
                        """,
                        (source, int(stat.st_size), int(stat.st_mtime_ns), profile_json, time.time(), ANALYSIS_SCHEMA_VERSION),
                    )
                    self._upsert_readiness_locked(profile, time.time())
                    self._conn.commit()
                    self._remember(profile)
                profiles.append(profile)
                analyzed += 1
            except (OSError, ValueError, TypeError, RuntimeError, KeyError) as exc:
                failed += 1
                with self._lock:
                    self._conn.execute(
                        """
                        INSERT INTO midi_profiles(path,size,mtime_ns,profile_json,analyzed_at,error,analysis_version)
                        VALUES (?, ?, ?, '{}', ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                        analyzed_at=excluded.analyzed_at,error=excluded.error,analysis_version=excluded.analysis_version
                        """,
                        (source, int(stat.st_size), int(stat.st_mtime_ns), time.time(), f"{type(exc).__name__}: {exc}", ANALYSIS_SCHEMA_VERSION),
                    )
                    self._conn.execute("DELETE FROM midi_profile_readiness WHERE path=?", (source,))
                    self._conn.commit()
                    self._cache.pop(source, None)
            if progress_callback:
                progress_callback(position, total, path.name)
        profiles.sort(key=lambda item: (item.composer.casefold(), item.title.casefold(), item.path.casefold()))
        return profiles, IndexStats(
            discovered=len(discovered), analyzed=analyzed, reused=reused, skipped_generated=skipped_generated,
            failed=failed, cancelled=cancelled,
        )
