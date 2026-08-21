from __future__ import annotations

"""Persistent discovery evidence and user feedback for mash recommendations.

The graph is deliberately metadata-only.  It can widen candidate discovery with
curated human mashups, authorized imports, similar-song/key/BPM lists, and the
user's own interaction history, but it never overrides the MIDI-derived safety
and musical gates in Guided/Assisted/Workbench scoring.
"""

from dataclasses import dataclass
from hashlib import sha256
from itertools import combinations
import json
from pathlib import Path
import sqlite3
import time
from typing import Iterable, Mapping, Sequence

from core.app_paths import AppPaths
from core.mash_preferences import pair_identities
from core.reference_catalog import ReferenceCatalog, normalize_title
from core.remix_profile import MidiProfile

GRAPH_SCHEMA_VERSION = 1
MAX_RESOLVED_GRAPH_PATHS = 4096


def _artist_key(value: str) -> str:
    return normalize_title(value)


def _ordered_text_pair(left: str, right: str) -> tuple[str, str]:
    a, b = str(left or "").strip(), str(right or "").strip()
    return (a, b) if a <= b else (b, a)


def _edge_id(*parts: object) -> str:
    material = "\x1f".join(str(part or "").strip().casefold() for part in parts)
    return sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class DiscoveryEvidence:
    score: int = 0
    labels: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    feedback_score: int = 0

    @property
    def has_evidence(self) -> bool:
        return bool(self.score or self.labels or self.reasons or self.feedback_score)


@dataclass(frozen=True)
class DiscoveryGraphSummary:
    edges: int = 0
    providers: int = 0
    feedback_pairs: int = 0


class DiscoveryGraph:
    """Small persistent SQLite graph stored beside the user's library index."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.database_path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discovery_edges_v1 (
                edge_id TEXT PRIMARY KEY,
                title_a_key TEXT NOT NULL,
                title_b_key TEXT NOT NULL,
                artist_a_key TEXT NOT NULL DEFAULT '',
                artist_b_key TEXT NOT NULL DEFAULT '',
                title_a TEXT NOT NULL,
                title_b TEXT NOT NULL,
                artist_a TEXT NOT NULL DEFAULT '',
                artist_b TEXT NOT NULL DEFAULT '',
                evidence_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                confidence INTEGER NOT NULL DEFAULT 0,
                relationship_tags_json TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                updated_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_edges_a_v1 ON discovery_edges_v1(title_a_key);
            CREATE INDEX IF NOT EXISTS idx_discovery_edges_b_v1 ON discovery_edges_v1(title_b_key);
            CREATE TABLE IF NOT EXISTS discovery_feedback_v1 (
                identity_a TEXT NOT NULL,
                identity_b TEXT NOT NULL,
                auditions INTEGER NOT NULL DEFAULT 0,
                saves INTEGER NOT NULL DEFAULT 0,
                choices INTEGER NOT NULL DEFAULT 0,
                rejects INTEGER NOT NULL DEFAULT 0,
                ignores INTEGER NOT NULL DEFAULT 0,
                last_action TEXT NOT NULL DEFAULT '',
                updated_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(identity_a, identity_b)
            );
            CREATE TABLE IF NOT EXISTS discovery_graph_meta_v1 (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO discovery_graph_meta_v1(key,value) VALUES('schema_version',?)",
            (str(GRAPH_SCHEMA_VERSION),),
        )
        self._conn.commit()

    @classmethod
    def for_app(cls, app_root: str | Path) -> "DiscoveryGraph":
        paths = AppPaths.for_app(app_root).ensure()
        return cls(paths.library / "discovery_graph.sqlite3")

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        except sqlite3.Error as exc:
            # Closing remains authoritative; SQLite recovers WAL state on next open.
            _checkpoint_error = exc
        self._conn.close()
        self._conn = None

    def __enter__(self) -> "DiscoveryGraph":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _upsert_edge(
        self,
        *,
        title_a: str,
        title_b: str,
        artist_a: str = "",
        artist_b: str = "",
        evidence_type: str,
        provider: str,
        confidence: int,
        relationship_tags: Sequence[str] = (),
        note: str = "",
        source_url: str = "",
    ) -> bool:
        left_key, right_key = normalize_title(title_a), normalize_title(title_b)
        if not left_key or not right_key or left_key == right_key:
            return False
        # Store a canonical undirected orientation so duplicate imports collapse.
        left_tuple = (left_key, _artist_key(artist_a), str(title_a).strip(), str(artist_a).strip())
        right_tuple = (right_key, _artist_key(artist_b), str(title_b).strip(), str(artist_b).strip())
        if right_tuple[:2] < left_tuple[:2]:
            left_tuple, right_tuple = right_tuple, left_tuple
        edge = _edge_id(
            left_tuple[0], right_tuple[0], left_tuple[1], right_tuple[1],
            evidence_type, provider, source_url, note,
        )
        self._conn.execute(
            """
            INSERT INTO discovery_edges_v1(
                edge_id,title_a_key,title_b_key,artist_a_key,artist_b_key,title_a,title_b,artist_a,artist_b,
                evidence_type,provider,confidence,relationship_tags_json,note,source_url,updated_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(edge_id) DO UPDATE SET
                confidence=MAX(discovery_edges_v1.confidence,excluded.confidence),
                relationship_tags_json=excluded.relationship_tags_json,
                note=excluded.note,
                source_url=excluded.source_url,
                updated_utc=CURRENT_TIMESTAMP
            """,
            (
                edge, left_tuple[0], right_tuple[0], left_tuple[1], right_tuple[1],
                left_tuple[2], right_tuple[2], left_tuple[3], right_tuple[3],
                str(evidence_type or "reference evidence"), str(provider or "Imported reference"),
                max(0, min(100, int(confidence))),
                json.dumps(tuple(dict.fromkeys(str(tag).strip() for tag in relationship_tags if str(tag).strip()))),
                str(note or ""), str(source_url or ""),
            ),
        )
        return True

    def import_reference_catalog(self, catalog: ReferenceCatalog) -> int:
        """Persist reference metadata once per actual catalogue revision.

        Discovery searches may run repeatedly against 96k songs; re-upserting the
        same bundled/imported relationship files on every search is needless I/O.
        """
        signature_material = repr((
            tuple(catalog.pairings), tuple(catalog.jazz_relationships),
            tuple(catalog.external_candidates), tuple(catalog.popularity),
        ))
        signature = sha256(signature_material.encode("utf-8", errors="replace")).hexdigest()
        row = self._conn.execute(
            "SELECT value FROM discovery_graph_meta_v1 WHERE key='reference_catalog_signature'"
        ).fetchone()
        if row is not None and str(row[0]) == signature:
            return 0
        written = 0
        for item in catalog.pairings:
            sources = [
                (title, item.source_artists[index] if index < len(item.source_artists) else "")
                for index, title in enumerate(item.source_titles) if str(title).strip()
            ]
            for (title_a, artist_a), (title_b, artist_b) in combinations(sources, 2):
                written += int(self._upsert_edge(
                    title_a=title_a, title_b=title_b, artist_a=artist_a, artist_b=artist_b,
                    evidence_type=item.evidence_type, provider=item.provider, confidence=item.confidence,
                    relationship_tags=item.relationship_tags,
                    note=f"Human-created mashup: {item.mashup_artist} — {item.mashup_title}",
                    source_url=item.source_url,
                ))
        for item in catalog.jazz_relationships:
            written += int(self._upsert_edge(
                title_a=item.source_title, title_b=item.related_title,
                evidence_type=f"jazz {item.relationship}", provider=item.provider, confidence=item.confidence,
                relationship_tags=(item.relationship, item.progression_family),
                note=f"Shared progression family: {item.progression_family}", source_url=item.source_url,
            ))
        for item in catalog.external_candidates:
            written += int(self._upsert_edge(
                title_a=item.anchor_title, title_b=item.candidate_title,
                artist_a=item.anchor_artist, artist_b=item.candidate_artist,
                evidence_type=item.evidence_type, provider=item.provider, confidence=item.confidence,
                relationship_tags=item.relationship_tags, note=item.note, source_url=item.source_url,
            ))
        self._conn.execute(
            "INSERT OR REPLACE INTO discovery_graph_meta_v1(key,value) VALUES('reference_catalog_signature',?)",
            (signature,),
        )
        self._conn.commit()
        return written

    @staticmethod
    def profile_title_map(profiles: Iterable[MidiProfile]) -> dict[str, tuple[MidiProfile, ...]]:
        """Build the normalized-title resolver once per catalogue revision.

        A six-figure library should not rebuild this 96k mapping for every
        Assisted/Guided search.  Startup builds it off the GUI thread and search
        workers reuse the immutable tuples.
        """
        mutable: dict[str, list[MidiProfile]] = {}
        for index, profile in enumerate(profiles, 1):
            if index % 2048 == 0:
                time.sleep(0.001)
            keys = {normalize_title(profile.title), normalize_title(Path(profile.path).stem)}
            if profile.song_identity:
                keys.add(normalize_title(profile.song_identity))
            for key in keys:
                if key:
                    mutable.setdefault(key, []).append(profile)
        return {key: tuple(values) for key, values in mutable.items()}

    @staticmethod
    def _profile_title_map(profiles: Iterable[MidiProfile]) -> dict[str, tuple[MidiProfile, ...]]:
        # Compatibility alias for tests/callers predating v0.27.
        return DiscoveryGraph.profile_title_map(profiles)

    @staticmethod
    def _artist_matches(profile: MidiProfile, expected_key: str) -> bool:
        if not expected_key:
            return True
        actual = _artist_key(profile.composer)
        return not actual or actual in {"unknown", "various", "anonymous"} or actual == expected_key

    def evidence_candidates(
        self,
        anchor: MidiProfile,
        profiles: Sequence[MidiProfile],
        *,
        limit: int = 512,
        title_map: Mapping[str, Sequence[MidiProfile]] | None = None,
    ) -> dict[str, DiscoveryEvidence]:
        """Resolve persistent title-level edges into paths in the current library."""
        anchor_keys = {normalize_title(anchor.title)}
        if anchor.song_identity:
            anchor_keys.add(normalize_title(anchor.song_identity))
        anchor_keys.discard("")
        if not anchor_keys:
            return {}
        placeholders = ",".join("?" for _ in anchor_keys)
        rows = self._conn.execute(
            f"""
            SELECT title_a_key,title_b_key,artist_a_key,artist_b_key,evidence_type,provider,confidence,
                   relationship_tags_json,note,source_url,title_a,title_b
            FROM discovery_edges_v1
            WHERE title_a_key IN ({placeholders}) OR title_b_key IN ({placeholders})
            ORDER BY confidence DESC, provider, evidence_type
            LIMIT ?
            """,
            (*anchor_keys, *anchor_keys, max(1, int(limit)) * 8),
        ).fetchall()
        resolved_titles = title_map or self.profile_title_map(profiles)
        candidate_profiles: dict[str, MidiProfile] = {}
        aggregated: dict[str, dict[str, object]] = {}
        for row in rows:
            left_key, right_key = str(row[0]), str(row[1])
            if left_key in anchor_keys:
                candidate_key, expected_artist = right_key, str(row[3] or "")
                candidate_label = str(row[11] or right_key)
            elif right_key in anchor_keys:
                candidate_key, expected_artist = left_key, str(row[2] or "")
                candidate_label = str(row[10] or left_key)
            else:
                continue
            try:
                tags = tuple(str(tag) for tag in json.loads(row[7] or "[]") if str(tag).strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                tags = ()
            for profile in resolved_titles.get(candidate_key, ()):
                if profile.path == anchor.path or not self._artist_matches(profile, expected_artist):
                    continue
                candidate_profiles[profile.path] = profile
                entry = aggregated.setdefault(profile.path, {
                    "score": 0, "labels": [], "reasons": [], "providers": [], "urls": [],
                })
                confidence = max(0, min(100, int(row[6] or 0)))
                entry["score"] = max(int(entry["score"]), confidence)
                evidence_type = str(row[4] or "reference evidence").replace("_", " ")
                provider = str(row[5] or "Reference evidence")
                label = evidence_type.title()
                if label not in entry["labels"]:
                    entry["labels"].append(label)
                tag_text = f" [{', '.join(tags)}]" if tags else ""
                note_text = f" {str(row[8]).strip()}" if str(row[8] or "").strip() else ""
                reason = (
                    f"Discovery graph: {provider} links {anchor.title} with {candidate_label} as {evidence_type} "
                    f"({confidence}% source confidence).{tag_text}{note_text}"
                )
                if reason not in entry["reasons"]:
                    entry["reasons"].append(reason)
                if provider not in entry["providers"]:
                    entry["providers"].append(provider)
                url = str(row[9] or "").strip()
                if url and url not in entry["urls"]:
                    entry["urls"].append(url)
                if len(aggregated) >= MAX_RESOLVED_GRAPH_PATHS:
                    break
        result: dict[str, DiscoveryEvidence] = {}
        for path, entry in aggregated.items():
            result[path] = DiscoveryEvidence(
                score=int(entry["score"]),
                labels=tuple(entry["labels"]),
                reasons=tuple(entry["reasons"]),
                providers=tuple(entry["providers"]),
                source_urls=tuple(entry["urls"]),
                feedback_score=self.feedback_score(anchor, candidate_profiles.get(path, anchor)),
            )
        ordered = sorted(
            result.items(), key=lambda item: (item[1].score, item[1].feedback_score, item[0].casefold()), reverse=True
        )
        return dict(ordered[: max(1, int(limit))])


    def resolved_evidence_pairs(
        self,
        profiles: Sequence[MidiProfile],
        *,
        limit: int = 2048,
        title_map: Mapping[str, Sequence[MidiProfile]] | None = None,
    ) -> dict[tuple[str, str], DiscoveryEvidence]:
        """Resolve the strongest persistent evidence edges to current-library paths.

        One dependable representative per normalized title is used so alternate
        filenames do not explode a single human relationship into dozens of
        duplicate candidate pairs.
        """
        resolved_titles = title_map or self.profile_title_map(profiles)

        def quality(profile: MidiProfile) -> tuple[int, int, int, str]:
            return (
                int(profile.orchestration_readiness or 0),
                int(profile.key_confidence or 0),
                int(profile.note_count or 0),
                profile.path.casefold(),
            )

        representative = {
            key: max(items, key=quality)
            for key, items in resolved_titles.items() if items
        }
        rows = self._conn.execute(
            """
            SELECT title_a_key,title_b_key,evidence_type,provider,confidence,relationship_tags_json,note,source_url,title_a,title_b
            FROM discovery_edges_v1
            ORDER BY confidence DESC, provider, evidence_type
            LIMIT ?
            """,
            (max(1, int(limit)) * 6,),
        ).fetchall()
        result: dict[tuple[str, str], DiscoveryEvidence] = {}
        for row in rows:
            left = representative.get(str(row[0]))
            right = representative.get(str(row[1]))
            if left is None or right is None or left.path == right.path:
                continue
            pair = tuple(sorted((left.path, right.path)))
            try:
                tags = tuple(str(tag) for tag in json.loads(row[5] or "[]") if str(tag).strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                tags = ()
            confidence = max(0, min(100, int(row[4] or 0)))
            evidence_type = str(row[2] or "reference evidence").replace("_", " ")
            provider = str(row[3] or "Reference evidence")
            tag_text = f" [{', '.join(tags)}]" if tags else ""
            note_text = f" {str(row[6]).strip()}" if str(row[6] or "").strip() else ""
            reason = (
                f"Discovery graph: {provider} links {str(row[8])} with {str(row[9])} as {evidence_type} "
                f"({confidence}% source confidence).{tag_text}{note_text}"
            )
            previous = result.get(pair, DiscoveryEvidence())
            result[pair] = DiscoveryEvidence(
                score=max(previous.score, confidence),
                labels=tuple(dict.fromkeys((*previous.labels, evidence_type.title()))),
                reasons=tuple(dict.fromkeys((*previous.reasons, reason))),
                providers=tuple(dict.fromkeys((*previous.providers, provider))),
                source_urls=tuple(dict.fromkeys((*previous.source_urls, str(row[7] or "")))) if row[7] else previous.source_urls,
                feedback_score=self.feedback_score(left, right),
            )
            if len(result) >= max(1, int(limit)):
                break
        return result

    def record_feedback(self, a: MidiProfile, b: MidiProfile, action: str) -> None:
        first, second = pair_identities(a, b)
        self.record_feedback_identities(first, second, action)

    def record_feedback_identities(self, identity_a: str, identity_b: str, action: str) -> None:
        first, second = _ordered_text_pair(identity_a, identity_b)
        if not first or not second or first == second:
            return
        action_key = str(action or "").strip().casefold()
        column = {
            "audition": "auditions",
            "preview": "auditions",
            "save": "saves",
            "saved": "saves",
            "choose": "choices",
            "chosen": "choices",
            "accept": "choices",
            "reject": "rejects",
            "ignored": "ignores",
            "ignore": "ignores",
        }.get(action_key)
        if column is None:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO discovery_feedback_v1(identity_a,identity_b) VALUES(?,?)",
            (first, second),
        )
        self._conn.execute(
            f"UPDATE discovery_feedback_v1 SET {column}={column}+1,last_action=?,updated_utc=CURRENT_TIMESTAMP "
            "WHERE identity_a=? AND identity_b=?",
            (action_key, first, second),
        )
        self._conn.commit()

    def restore_ignored_feedback(self, identity_a: str, identity_b: str) -> None:
        first, second = _ordered_text_pair(identity_a, identity_b)
        self._conn.execute(
            "UPDATE discovery_feedback_v1 SET ignores=0,last_action='restore',updated_utc=CURRENT_TIMESTAMP "
            "WHERE identity_a=? AND identity_b=?",
            (first, second),
        )
        self._conn.commit()

    def feedback_score(self, a: MidiProfile, b: MidiProfile) -> int:
        first, second = pair_identities(a, b)
        if first == second:
            return 0
        row = self._conn.execute(
            "SELECT auditions,saves,choices,rejects,ignores FROM discovery_feedback_v1 WHERE identity_a=? AND identity_b=?",
            (first, second),
        ).fetchone()
        if not row:
            return 0
        auditions, saves, choices, rejects, ignores = (int(value or 0) for value in row)
        score = min(10, auditions) + min(18, saves * 4) + min(30, choices * 8) - min(25, rejects * 8) - min(100, ignores * 100)
        return max(-100, min(60, score))

    def summary(self) -> DiscoveryGraphSummary:
        edge_count = int(self._conn.execute("SELECT COUNT(*) FROM discovery_edges_v1").fetchone()[0])
        provider_count = int(self._conn.execute("SELECT COUNT(DISTINCT provider) FROM discovery_edges_v1").fetchone()[0])
        feedback_count = int(self._conn.execute("SELECT COUNT(*) FROM discovery_feedback_v1").fetchone()[0])
        return DiscoveryGraphSummary(edge_count, provider_count, feedback_count)


def build_discovery_evidence(
    app_root: str | Path | None,
    anchor: MidiProfile,
    profiles: Sequence[MidiProfile],
    *,
    reference_catalog: ReferenceCatalog | None = None,
    limit: int = 512,
) -> dict[str, DiscoveryEvidence]:
    if not app_root:
        return {}
    catalog = reference_catalog or ReferenceCatalog(
        Path(app_root) / "data" / "known_pairings.json",
        import_directories=(AppPaths.for_app(app_root).ensure().reference_imports,),
    )
    with DiscoveryGraph.for_app(app_root) as graph:
        graph.import_reference_catalog(catalog)
        return graph.evidence_candidates(anchor, profiles, limit=limit)


def record_discovery_feedback(app_root: str | Path | None, a: MidiProfile, b: MidiProfile, action: str) -> None:
    if not app_root:
        return
    with DiscoveryGraph.for_app(app_root) as graph:
        graph.record_feedback(a, b, action)


def restore_discovery_feedback(app_root: str | Path | None, identity_a: str, identity_b: str) -> None:
    if not app_root:
        return
    with DiscoveryGraph.for_app(app_root) as graph:
        graph.restore_ignored_feedback(identity_a, identity_b)
