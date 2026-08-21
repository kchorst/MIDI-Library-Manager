from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterable

from core.assisted_studio import assisted_plan_for_pair
from core.discovery_graph import DiscoveryEvidence
from core.guided_beginner import _make_plan, _make_section_switch_plan, _pair_safe, _section_switch_candidate
from core.remix_profile import MidiProfile, PairAssessment, assess_pair
from library_engine.prepared_plan import assisted_plan_snapshot, guided_plan_snapshot

# Increment whenever stored scoring/candidate semantics change.  Existing rows
# remain as rollback evidence but are ignored until rebuilt by the Engine.
RELATIONSHIP_ALGORITHM_VERSION = 4
RELATIONSHIP_MODES = {"guided", "assisted"}


@dataclass(frozen=True, slots=True)
class StoredRelationship:
    mode: str
    path_a: str
    path_b: str
    family_a: str
    family_b: str
    score: int
    category: str
    preparation_score: int
    harmonic_score: int
    tempo_score: int
    meter_score: int
    evidence_score: int = 0
    evidence_feedback_score: int = 0
    evidence_labels: tuple[str, ...] = ()
    evidence_reasons: tuple[str, ...] = ()
    evidence_providers: tuple[str, ...] = ()
    prepared_plan: dict[str, object] = field(default_factory=dict)
    algorithm_version: int = RELATIONSHIP_ALGORITHM_VERSION


def relationship_key(mode: str, path_a: str, path_b: str) -> str:
    left, right = sorted((str(path_a), str(path_b)), key=str.casefold)
    material = str(mode).casefold() + "\0" + left + "\0" + right
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def family_relationship_key(family_a: str, family_b: str) -> str:
    """Stable unordered composition-pair key for the global DJ reservoir."""
    left, right = sorted((str(family_a), str(family_b)), key=str.casefold)
    material = left + "\0" + right
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def with_discovery_evidence(
    row: StoredRelationship, evidence: DiscoveryEvidence | None, *, maximum_bonus: int
) -> StoredRelationship:
    """Attach transparent discovery evidence without bypassing MIDI validity.

    `row` exists only after the actual MIDI-derived Guided/Assisted gate passed.
    Evidence can modestly rank a valid relationship higher and explain why it
    entered the shortlist, but it can never create a relationship by itself.
    """
    if evidence is None or not evidence.has_evidence:
        return row
    source_score = max(0, min(100, int(evidence.score)))
    feedback = max(-100, min(60, int(evidence.feedback_score)))
    evidence_bonus = min(max(0, int(maximum_bonus)), int(round(source_score / 20.0)))
    feedback_bonus = max(-4, min(4, int(round(feedback / 15.0))))
    return replace(
        row,
        score=max(0, min(100, int(row.score) + evidence_bonus + feedback_bonus)),
        evidence_score=source_score,
        evidence_feedback_score=feedback,
        evidence_labels=tuple(evidence.labels),
        evidence_reasons=tuple(evidence.reasons),
        evidence_providers=tuple(evidence.providers),
    )


class RelationshipStore:
    """Persistent sparse recommendation graph prepared by the Library Engine.

    Guided and Assisted are separate lanes over the same pair universe. The
    database stores only evaluated, distinct-composition relationships; Remixer
    reads pages/cursors instead of re-running broad searches.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        if self.read_only:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        if not self.read_only:
            self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            try:
                self._conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS relationships_v2(
                        relationship_key TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        path_a TEXT NOT NULL,
                        path_b TEXT NOT NULL,
                        family_a TEXT NOT NULL,
                        family_b TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        category TEXT NOT NULL,
                        preparation_score INTEGER NOT NULL,
                        harmonic_score INTEGER NOT NULL,
                        tempo_score INTEGER NOT NULL,
                        meter_score INTEGER NOT NULL,
                        evidence_score INTEGER NOT NULL DEFAULT 0,
                        evidence_feedback_score INTEGER NOT NULL DEFAULT 0,
                        evidence_labels_json TEXT NOT NULL DEFAULT '[]',
                        evidence_reasons_json TEXT NOT NULL DEFAULT '[]',
                        evidence_providers_json TEXT NOT NULL DEFAULT '[]',
                        prepared_plan_json TEXT NOT NULL DEFAULT '{}',
                        algorithm_version INTEGER NOT NULL,
                        updated_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CHECK(mode IN ('guided','assisted')),
                        CHECK(family_a <> family_b)
                    );
                    CREATE TABLE IF NOT EXISTS anchor_builds_v2(
                        path TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        source_fingerprint TEXT NOT NULL,
                        algorithm_version INTEGER NOT NULL,
                        candidate_count INTEGER NOT NULL,
                        accepted_count INTEGER NOT NULL,
                        built_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(path,mode),
                        CHECK(mode IN ('guided','assisted'))
                    );
                    CREATE TABLE IF NOT EXISTS family_best_v1(
                        mode TEXT NOT NULL,
                        family_key TEXT NOT NULL,
                        path_a TEXT NOT NULL,
                        path_b TEXT NOT NULL,
                        family_a TEXT NOT NULL,
                        family_b TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        category TEXT NOT NULL,
                        preparation_score INTEGER NOT NULL,
                        harmonic_score INTEGER NOT NULL,
                        tempo_score INTEGER NOT NULL,
                        meter_score INTEGER NOT NULL,
                        evidence_score INTEGER NOT NULL DEFAULT 0,
                        evidence_feedback_score INTEGER NOT NULL DEFAULT 0,
                        evidence_labels_json TEXT NOT NULL DEFAULT '[]',
                        evidence_reasons_json TEXT NOT NULL DEFAULT '[]',
                        evidence_providers_json TEXT NOT NULL DEFAULT '[]',
                        prepared_plan_json TEXT NOT NULL DEFAULT '{}',
                        algorithm_version INTEGER NOT NULL,
                        updated_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(mode,family_key),
                        CHECK(mode IN ('guided','assisted')),
                        CHECK(family_a <> family_b)
                    );
                    CREATE TABLE IF NOT EXISTS builder_state_v2(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                # Upgrade an early split-development database in-place.  The public
                # release has not shipped this schema yet, but keeping migration here
                # makes repeated internal runs safe and idempotent.
                additions = {
                    "evidence_score": "INTEGER NOT NULL DEFAULT 0",
                    "evidence_feedback_score": "INTEGER NOT NULL DEFAULT 0",
                    "evidence_labels_json": "TEXT NOT NULL DEFAULT '[]'",
                    "evidence_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
                    "evidence_providers_json": "TEXT NOT NULL DEFAULT '[]'",
                    "prepared_plan_json": "TEXT NOT NULL DEFAULT '{}'",
                }
                # Both persisted recommendation tables evolved together. Earlier
                # split-development builds migrated relationships_v2 but forgot
                # family_best_v1; a populated database therefore opened and then
                # failed on the first upsert that referenced evidence_score. Keep the
                # migration idempotent and symmetrical so existing recommendation
                # reservoirs survive an upgrade intact.
                for table in ("relationships_v2", "family_best_v1"):
                    columns = {
                        str(row[1])
                        for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                    for name, ddl in additions.items():
                        if name not in columns:
                            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_relationships_v2_mode_score "
                    "ON relationships_v2(mode,algorithm_version,score DESC)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_relationships_v2_a "
                    "ON relationships_v2(mode,path_a,algorithm_version,score DESC)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_relationships_v2_b "
                    "ON relationships_v2(mode,path_b,algorithm_version,score DESC)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_family_best_v1_mode_score "
                    "ON family_best_v1(mode,algorithm_version,score DESC)"
                )
                self._conn.commit()

            except sqlite3.Error:
                # Schema upgrades are one atomic boundary: an interrupted or
                # failed migration must leave the previously committed populated
                # relationship database intact so the next Engine start can retry.
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    @staticmethod
    def _mode(mode: str) -> str:
        value = str(mode or "guided").casefold()
        if value not in RELATIONSHIP_MODES:
            raise ValueError(f"Unknown recommendation relationship mode: {mode}")
        return value

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            if not self.read_only:
                self._conn.commit()
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
                except sqlite3.Error:
                    # WAL recovery is authoritative on the next open.
                    self._conn.commit()
            self._conn.close()
            self._conn = None

    def anchor_is_current(self, path: str, fingerprint: str, *, mode: str) -> bool:
        mode = self._mode(mode)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM anchor_builds_v2 WHERE path=? AND mode=? AND source_fingerprint=? AND algorithm_version=?",
                (str(path), mode, str(fingerprint), RELATIONSHIP_ALGORITHM_VERSION),
            ).fetchone()
        return row is not None

    def mark_anchor_built(
        self, path: str, fingerprint: str, candidate_count: int, accepted_count: int, *, mode: str
    ) -> None:
        if self.read_only:
            raise RuntimeError("Relationship store is read-only")
        mode = self._mode(mode)
        with self._lock:
            self._conn.execute(
                """INSERT INTO anchor_builds_v2(path,mode,source_fingerprint,algorithm_version,candidate_count,accepted_count)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(path,mode) DO UPDATE SET
                   source_fingerprint=excluded.source_fingerprint,algorithm_version=excluded.algorithm_version,
                   candidate_count=excluded.candidate_count,accepted_count=excluded.accepted_count,
                   built_utc=CURRENT_TIMESTAMP""",
                (
                    str(path), mode, str(fingerprint), RELATIONSHIP_ALGORITHM_VERSION,
                    int(candidate_count), int(accepted_count),
                ),
            )
            self._conn.commit()

    def upsert_many(self, rows: Iterable[StoredRelationship]) -> int:
        if self.read_only:
            raise RuntimeError("Relationship store is read-only")
        values = []
        family_values = []
        for row in rows:
            mode = self._mode(row.mode)
            if row.family_a == row.family_b:
                continue
            a, b = sorted((row.path_a, row.path_b), key=str.casefold)
            fa, fb = (row.family_a, row.family_b) if a == row.path_a else (row.family_b, row.family_a)
            common = (
                a, b, fa, fb, int(row.score), row.category, int(row.preparation_score),
                int(row.harmonic_score), int(row.tempo_score), int(row.meter_score), int(row.evidence_score),
                int(row.evidence_feedback_score), json.dumps(tuple(row.evidence_labels), ensure_ascii=False),
                json.dumps(tuple(row.evidence_reasons), ensure_ascii=False),
                json.dumps(tuple(row.evidence_providers), ensure_ascii=False),
                json.dumps(dict(row.prepared_plan), ensure_ascii=False), RELATIONSHIP_ALGORITHM_VERSION,
            )
            values.append((relationship_key(mode, a, b), mode, *common))
            family_values.append((mode, family_relationship_key(fa, fb), *common))
        if not values:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO relationships_v2(
                    relationship_key,mode,path_a,path_b,family_a,family_b,score,category,preparation_score,
                    harmonic_score,tempo_score,meter_score,evidence_score,evidence_feedback_score,
                    evidence_labels_json,evidence_reasons_json,evidence_providers_json,prepared_plan_json,algorithm_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(relationship_key) DO UPDATE SET
                    mode=excluded.mode,path_a=excluded.path_a,path_b=excluded.path_b,
                    family_a=excluded.family_a,family_b=excluded.family_b,score=excluded.score,
                    category=excluded.category,preparation_score=excluded.preparation_score,
                    harmonic_score=excluded.harmonic_score,tempo_score=excluded.tempo_score,
                    meter_score=excluded.meter_score,evidence_score=excluded.evidence_score,
                    evidence_feedback_score=excluded.evidence_feedback_score,
                    evidence_labels_json=excluded.evidence_labels_json,evidence_reasons_json=excluded.evidence_reasons_json,
                    evidence_providers_json=excluded.evidence_providers_json,prepared_plan_json=excluded.prepared_plan_json,
                    algorithm_version=excluded.algorithm_version,updated_utc=CURRENT_TIMESTAMP""",
                values,
            )
            # Beginner and no-anchor Assisted consume this composition-level
            # reservoir.  Multiple filenames/arrangements for the same two songs
            # compete here; only the strongest validated representative remains.
            self._conn.executemany(
                """INSERT INTO family_best_v1(
                    mode,family_key,path_a,path_b,family_a,family_b,score,category,preparation_score,
                    harmonic_score,tempo_score,meter_score,evidence_score,evidence_feedback_score,
                    evidence_labels_json,evidence_reasons_json,evidence_providers_json,prepared_plan_json,algorithm_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(mode,family_key) DO UPDATE SET
                    path_a=excluded.path_a,path_b=excluded.path_b,family_a=excluded.family_a,family_b=excluded.family_b,
                    score=excluded.score,category=excluded.category,preparation_score=excluded.preparation_score,
                    harmonic_score=excluded.harmonic_score,tempo_score=excluded.tempo_score,meter_score=excluded.meter_score,
                    evidence_score=excluded.evidence_score,evidence_feedback_score=excluded.evidence_feedback_score,
                    evidence_labels_json=excluded.evidence_labels_json,evidence_reasons_json=excluded.evidence_reasons_json,
                    evidence_providers_json=excluded.evidence_providers_json,prepared_plan_json=excluded.prepared_plan_json,
                    algorithm_version=excluded.algorithm_version,updated_utc=CURRENT_TIMESTAMP
                    WHERE excluded.algorithm_version > family_best_v1.algorithm_version
                       OR (excluded.algorithm_version = family_best_v1.algorithm_version AND excluded.score >= family_best_v1.score)""",
                family_values,
            )
            self._conn.commit()
        return len(values)

    @staticmethod
    def _tuple_json(value: object) -> tuple[str, ...]:
        try:
            parsed = json.loads(str(value or "[]"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return ()
        if not isinstance(parsed, list):
            return ()
        return tuple(str(item) for item in parsed if str(item).strip())

    @staticmethod
    def _dict_json(value: object) -> dict[str, object]:
        try:
            parsed = json.loads(str(value or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    @classmethod
    def _row(cls, row: sqlite3.Row) -> StoredRelationship:
        return StoredRelationship(
            mode=str(row["mode"]), path_a=str(row["path_a"]), path_b=str(row["path_b"]),
            family_a=str(row["family_a"]), family_b=str(row["family_b"]), score=int(row["score"]),
            category=str(row["category"]), preparation_score=int(row["preparation_score"]),
            harmonic_score=int(row["harmonic_score"]), tempo_score=int(row["tempo_score"]),
            meter_score=int(row["meter_score"]), evidence_score=int(row["evidence_score"] or 0),
            evidence_feedback_score=int(row["evidence_feedback_score"] or 0),
            evidence_labels=cls._tuple_json(row["evidence_labels_json"]),
            evidence_reasons=cls._tuple_json(row["evidence_reasons_json"]),
            evidence_providers=cls._tuple_json(row["evidence_providers_json"]),
            prepared_plan=cls._dict_json(row["prepared_plan_json"]),
        )

    @staticmethod
    def _select_columns() -> str:
        return (
            "mode,path_a,path_b,family_a,family_b,score,category,preparation_score,harmonic_score,tempo_score,meter_score,"
            "evidence_score,evidence_feedback_score,evidence_labels_json,evidence_reasons_json,evidence_providers_json,"
            "prepared_plan_json"
        )

    def recommended_pairs(self, *, mode: str = "guided", limit: int = 64, offset: int = 0) -> list[StoredRelationship]:
        mode = self._mode(mode)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._select_columns()} FROM family_best_v1 "
                "WHERE mode=? AND algorithm_version=? "
                "ORDER BY score DESC,updated_utc DESC,family_key LIMIT ? OFFSET ?",
                (mode, RELATIONSHIP_ALGORITHM_VERSION, max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [self._row(row) for row in rows]

    def recommended_for_anchor(
        self, anchor_path: str, *, mode: str = "assisted", limit: int = 64, offset: int = 0
    ) -> list[StoredRelationship]:
        mode = self._mode(mode)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._select_columns()} FROM relationships_v2 "
                "WHERE mode=? AND algorithm_version=? AND (path_a=? OR path_b=?) "
                "ORDER BY score DESC,updated_utc DESC,relationship_key LIMIT ? OFFSET ?",
                (
                    mode, RELATIONSHIP_ALGORITHM_VERSION, str(anchor_path), str(anchor_path),
                    max(1, int(limit)), max(0, int(offset)),
                ),
            ).fetchall()
        return [self._row(row) for row in rows]

    def count(self, *, mode: str | None = None) -> int:
        """Count distinct composition-pair recommendations, not filename variants."""
        with self._lock:
            if mode is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM family_best_v1 WHERE algorithm_version=?",
                    (RELATIONSHIP_ALGORITHM_VERSION,),
                ).fetchone()
            else:
                value = self._mode(mode)
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM family_best_v1 WHERE mode=? AND algorithm_version=?",
                    (value, RELATIONSHIP_ALGORITHM_VERSION),
                ).fetchone()
        return int(row[0] or 0) if row else 0

    def raw_count(self, *, mode: str | None = None) -> int:
        with self._lock:
            if mode is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM relationships_v2 WHERE algorithm_version=?",
                    (RELATIONSHIP_ALGORITHM_VERSION,),
                ).fetchone()
            else:
                value = self._mode(mode)
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM relationships_v2 WHERE mode=? AND algorithm_version=?",
                    (value, RELATIONSHIP_ALGORITHM_VERSION),
                ).fetchone()
        return int(row[0] or 0) if row else 0

    def built_anchor_count(self, *, mode: str | None = None) -> int:
        with self._lock:
            if mode is None:
                row = self._conn.execute(
                    "SELECT COUNT(DISTINCT path) FROM anchor_builds_v2 WHERE algorithm_version=?",
                    (RELATIONSHIP_ALGORITHM_VERSION,),
                ).fetchone()
            else:
                value = self._mode(mode)
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM anchor_builds_v2 WHERE mode=? AND algorithm_version=?",
                    (value, RELATIONSHIP_ALGORITHM_VERSION),
                ).fetchone()
        return int(row[0] or 0) if row else 0

    def delete_paths(self, paths: Iterable[str]) -> int:
        if self.read_only:
            raise RuntimeError("Relationship store is read-only")
        values = sorted({str(path) for path in paths if path})
        if not values:
            return 0
        with self._lock:
            for path in values:
                self._conn.execute("DELETE FROM relationships_v2 WHERE path_a=? OR path_b=?", (path, path))
                self._conn.execute("DELETE FROM family_best_v1 WHERE path_a=? OR path_b=?", (path, path))
                self._conn.execute("DELETE FROM anchor_builds_v2 WHERE path=?", (path,))
            self._conn.commit()
        return len(values)

    def invalidate_all_builds(self) -> None:
        """Keep relationships available while forcing background re-evaluation."""
        if self.read_only:
            raise RuntimeError("Relationship store is read-only")
        with self._lock:
            self._conn.execute("DELETE FROM anchor_builds_v2")
            self._conn.commit()

    def clear_derived(self) -> None:
        """Delete prepared relationship products while preserving the database itself."""
        if self.read_only:
            raise RuntimeError("Relationship store is read-only")
        with self._lock:
            self._conn.execute("DELETE FROM relationships_v2")
            self._conn.execute("DELETE FROM family_best_v1")
            self._conn.execute("DELETE FROM anchor_builds_v2")
            self._conn.execute("DELETE FROM builder_state_v2")
            self._conn.commit()


def guided_relationship_for_profiles(
    a: MidiProfile, b: MidiProfile, family_a: str, family_b: str,
    *, assessment: PairAssessment | None = None,
) -> StoredRelationship | None:
    if not family_a or not family_b or family_a == family_b:
        return None
    assessment = assessment or assess_pair(a, b)
    if _pair_safe(a, b, assessment):
        plan = _make_plan(a, b, assessment)
    elif _section_switch_candidate(a, b, assessment):
        plan = _make_section_switch_plan(a, b, assessment)
    else:
        return None
    return StoredRelationship(
        mode="guided", path_a=a.path, path_b=b.path, family_a=family_a, family_b=family_b,
        score=int(plan.safety_score), category=str(plan.category),
        preparation_score=int(assessment.preparation_score), harmonic_score=int(assessment.harmonic_score),
        tempo_score=int(assessment.tempo_score), meter_score=int(assessment.meter_score),
        prepared_plan=guided_plan_snapshot(plan),
    )


def assisted_relationship_for_profiles(
    a: MidiProfile, b: MidiProfile, family_a: str, family_b: str,
    *, assessment: PairAssessment | None = None, discovery_evidence: DiscoveryEvidence | None = None,
) -> StoredRelationship | None:
    if not family_a or not family_b or family_a == family_b:
        return None
    assessment = assessment or assess_pair(a, b)
    plan = assisted_plan_for_pair(a, b, assessment=assessment, discovery_evidence=discovery_evidence)
    if plan is None or not plan.pair_assessments:
        return None
    assessment = plan.pair_assessments[0]
    return StoredRelationship(
        mode="assisted", path_a=a.path, path_b=b.path, family_a=family_a, family_b=family_b,
        score=int(plan.confidence), category=str(plan.category),
        preparation_score=int(assessment.preparation_score), harmonic_score=int(assessment.harmonic_score),
        tempo_score=int(assessment.tempo_score), meter_score=int(assessment.meter_score),
        prepared_plan=assisted_plan_snapshot(plan),
    )


# Compatibility alias for callers that still expect the older Guided helper.
def relationship_for_profiles(a: MidiProfile, b: MidiProfile, family_a: str, family_b: str) -> StoredRelationship | None:
    return guided_relationship_for_profiles(a, b, family_a, family_b)
