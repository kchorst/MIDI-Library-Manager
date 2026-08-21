from __future__ import annotations

"""Versioned cache for repeated MIDI pair assessments.

v0.27 keeps one SQLite connection open for the lifetime of the cache, uses a
bounded in-memory LRU, and batches recency touches.  The old implementation
opened SQLite and wrote ``last_used_at`` for every disk hit; at 96k-library
scale that I/O cost could exceed recomputing inexpensive metadata scores.

The cache stores only derived metadata. Source MIDI files are never copied.
Keys are ordered because some assessment fields describe B relative to A.
"""

from collections import OrderedDict
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

PAIR_SCORING_VERSION = "pair-score-v0.27.0-1"
_MAX_MEMORY_ENTRIES = 4096
_MAX_DISK_ENTRIES = 50000
_TOUCH_FLUSH_BATCH = 256
_LOG = logging.getLogger(__name__)


def profile_cache_key(profile: Any) -> str:
    fingerprint = str(getattr(profile, "fingerprint", "") or "")
    content = str(getattr(profile, "content_fingerprint", "") or "")
    musical = str(getattr(profile, "musical_fingerprint", "") or "")
    if any((content, musical, fingerprint)):
        return ":".join((content, musical, fingerprint))

    fallback = {
        "path": str(getattr(profile, "path", "") or ""),
        "title": str(getattr(profile, "title", "") or ""),
        "felt_bpm": getattr(profile, "felt_bpm", None),
        "meter": str(getattr(profile, "meter", "") or ""),
        "tonic_pc": getattr(profile, "tonic_pc", None),
        "mode": str(getattr(profile, "mode", "") or ""),
        "bars": getattr(profile, "bars", None),
        "note_count": getattr(profile, "note_count", None),
    }
    encoded = json.dumps(fallback, sort_keys=True, ensure_ascii=False, default=str)
    return "fallback:" + hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()


def assessment_key(a: Any, b: Any) -> str:
    material = f"{PAIR_SCORING_VERSION}|{profile_cache_key(a)}|{profile_cache_key(b)}"
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


class PairAssessmentCache:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path else None
        self._memory: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._touched: set[str] = set()
        self.hits = 0
        self.misses = 0
        self.writes = 0
        if self.database_path:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        assert self.database_path is not None
        connection = sqlite3.connect(self.database_path, timeout=5.0, check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _initialize(self) -> None:
        with self._lock:
            db = self._db()
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS pair_assessments (
                    cache_key TEXT PRIMARY KEY,
                    scoring_version TEXT NOT NULL,
                    profile_a_key TEXT NOT NULL,
                    profile_b_key TEXT NOT NULL,
                    assessment_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_pair_assessments_used ON pair_assessments(last_used_at)")
            db.execute("DELETE FROM pair_assessments WHERE scoring_version <> ?", (PAIR_SCORING_VERSION,))
            db.commit()

    def _remember(self, key: str, payload: dict[str, Any]) -> None:
        self._memory[key] = payload
        self._memory.move_to_end(key)
        while len(self._memory) > _MAX_MEMORY_ENTRIES:
            self._memory.popitem(last=False)

    def _flush_touches_locked(self) -> None:
        if not self.database_path or not self._touched:
            return
        keys = tuple(self._touched)
        self._touched.clear()
        now = time.time()
        try:
            self._db().executemany(
                "UPDATE pair_assessments SET last_used_at=? WHERE cache_key=?",
                ((now, key) for key in keys),
            )
            self._db().commit()
        except (OSError, sqlite3.Error) as exc:
            _LOG.debug("Pair cache recency flush failed: %s", exc, exc_info=True)

    def get(self, a: Any, b: Any) -> dict[str, Any] | None:
        key = assessment_key(a, b)
        with self._lock:
            payload = self._memory.get(key)
            if payload is not None:
                self._memory.move_to_end(key)
                self.hits += 1
                return dict(payload)
            if self.database_path:
                try:
                    row = self._db().execute(
                        "SELECT assessment_json FROM pair_assessments WHERE cache_key=? AND scoring_version=?",
                        (key, PAIR_SCORING_VERSION),
                    ).fetchone()
                    if row:
                        payload = json.loads(row[0])
                        if isinstance(payload, dict):
                            self._remember(key, payload)
                            self._touched.add(key)
                            if len(self._touched) >= _TOUCH_FLUSH_BATCH:
                                self._flush_touches_locked()
                            self.hits += 1
                            return dict(payload)
                except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    _LOG.debug("Pair cache read failed: %s", exc, exc_info=True)
            self.misses += 1
            return None

    def put(self, a: Any, b: Any, payload: dict[str, Any]) -> None:
        key = assessment_key(a, b)
        clean = dict(payload)
        with self._lock:
            self._remember(key, clean)
            self.writes += 1
            if not self.database_path:
                return
            now = time.time()
            try:
                db = self._db()
                db.execute(
                    """
                    INSERT INTO pair_assessments(
                        cache_key, scoring_version, profile_a_key, profile_b_key,
                        assessment_json, created_at, last_used_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        scoring_version=excluded.scoring_version,
                        profile_a_key=excluded.profile_a_key,
                        profile_b_key=excluded.profile_b_key,
                        assessment_json=excluded.assessment_json,
                        last_used_at=excluded.last_used_at
                    """,
                    (
                        key,
                        PAIR_SCORING_VERSION,
                        profile_cache_key(a),
                        profile_cache_key(b),
                        json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
                # Cache data is disposable, but commits keep cross-process reuse safe.
                # One persistent connection removes nearly all of the old open/close cost.
                db.commit()
                if self.writes % 500 == 0:
                    db.execute(
                        "DELETE FROM pair_assessments WHERE cache_key IN ("
                        "SELECT cache_key FROM pair_assessments ORDER BY last_used_at DESC LIMIT -1 OFFSET ?)" ,
                        (_MAX_DISK_ENTRIES,),
                    )
                    db.commit()
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                _LOG.debug("Pair cache write failed: %s", exc, exc_info=True)

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()
            self._touched.clear()
            if self.database_path:
                try:
                    self._db().execute("DELETE FROM pair_assessments")
                    self._db().commit()
                except (OSError, sqlite3.Error) as exc:
                    _LOG.debug("Pair cache clear failed: %s", exc, exc_info=True)

    def close(self) -> None:
        with self._lock:
            self._flush_touches_locked()
            connection = self._connection
            self._connection = None
            if connection is not None:
                try:
                    connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    _LOG.debug("Pair cache WAL checkpoint failed", exc_info=True)
                try:
                    connection.close()
                except sqlite3.Error:
                    _LOG.debug("Pair cache close failed", exc_info=True)

    def snapshot(self) -> dict[str, int | str | None]:
        with self._lock:
            return {
                "scoring_version": PAIR_SCORING_VERSION,
                "memory_entries": len(self._memory),
                "pending_recency_touches": len(self._touched),
                "hits": self.hits,
                "misses": self.misses,
                "writes": self.writes,
                "database_path": str(self.database_path) if self.database_path else None,
            }


_default_cache = PairAssessmentCache()
_default_cache_lock = threading.RLock()


def configure_pair_assessment_cache(database_path: str | Path | None) -> PairAssessmentCache:
    global _default_cache
    with _default_cache_lock:
        previous = _default_cache
        _default_cache = PairAssessmentCache(database_path)
        previous.close()
        return _default_cache


def get_pair_assessment_cache() -> PairAssessmentCache:
    return _default_cache


def close_pair_assessment_cache() -> None:
    with _default_cache_lock:
        _default_cache.close()
