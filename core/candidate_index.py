from __future__ import annotations

"""Bounded, versioned candidate retrieval for large MIDI libraries.

The musical pair scorer remains authoritative.  This module only narrows the
number of pairs that need full assessment by using cheap profile metadata.
Candidate lists can be persisted so opening the same unchanged library does not
repeat discovery work on every launch.
"""

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import heapq
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterable, Mapping, Sequence

from core.app_paths import AppPaths
from core.remix_profile import MidiProfile


CANDIDATE_INDEX_VERSION = "2"
_TEMPO_BIN_WIDTH = 4.0


@dataclass(frozen=True)
class CandidateIndexStats:
    profile_count: int
    pair_count: int
    cache_hit: bool
    signature: str


def _text(value: object) -> str:
    return str(value or "").strip().casefold()


def _profile_signature_payload(profile: MidiProfile) -> tuple[object, ...]:
    return (
        str(profile.path),
        str(profile.fingerprint or profile.content_fingerprint or profile.musical_fingerprint),
        round(float(profile.felt_bpm or 0.0), 2),
        int(profile.tonic_pc) if profile.tonic_pc is not None else None,
        _text(profile.mode),
        _text(profile.meter_family or profile.meter_display),
        _text(profile.progression_family),
        _text(profile.style),
        int(profile.melody_track_index is not None),
        int(profile.accompaniment_track_index is not None),
    )


def library_signature(profiles: Iterable[MidiProfile]) -> str:
    digest = hashlib.sha256(CANDIDATE_INDEX_VERSION.encode("ascii"))
    for payload in sorted((_profile_signature_payload(profile) for profile in profiles), key=lambda item: str(item[0]).casefold()):
        digest.update(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()[:32]


def _tempo_bin(bpm: float | None) -> int | None:
    if not bpm or bpm <= 0:
        return None
    return int(round(float(bpm) / _TEMPO_BIN_WIDTH))


def _circle_distance(left: int, right: int) -> int:
    # Circle-of-fifths distance: adjacent values tend to share many pitches.
    a = (int(left) * 7) % 12
    b = (int(right) * 7) % 12
    direct = abs(a - b)
    return min(direct, 12 - direct)


def _relative_key(profile: MidiProfile) -> tuple[int, str] | None:
    if profile.tonic_pc is None:
        return None
    mode = _text(profile.mode)
    if mode == "major":
        return ((int(profile.tonic_pc) + 9) % 12, "minor")
    if mode == "minor":
        return ((int(profile.tonic_pc) + 3) % 12, "major")
    return None


class CandidateIndex:
    """In-memory metadata index that avoids all-pairs candidate discovery."""

    def __init__(self, profiles: Sequence[MidiProfile]) -> None:
        self._lock = threading.RLock()
        self.profiles: list[MidiProfile] = []
        self.by_path: dict[str, MidiProfile] = {}
        self._tempo: dict[int, set[str]] = defaultdict(set)
        self._key: dict[tuple[int, str], set[str]] = defaultdict(set)
        self._meter: dict[str, set[str]] = defaultdict(set)
        self._progression: dict[str, set[str]] = defaultdict(set)
        self._style: dict[str, set[str]] = defaultdict(set)
        self._role_ready: set[str] = set()
        for index, profile in enumerate(profiles, 1):
            # Six-figure startup builds run on a worker thread, but this loop is
            # Python-heavy. Brief periodic GIL yields keep the Qt GUI thread
            # repainting and processing input during catalogue hydration.
            if index % 2048 == 0:
                time.sleep(0.001)
            self._append_profile(profile)

    def _append_profile(self, profile: MidiProfile) -> None:
        path = profile.path
        self.profiles.append(profile)
        self.by_path[path] = profile
        tempo = _tempo_bin(profile.felt_bpm)
        if tempo is not None:
            self._tempo[tempo].add(path)
        if profile.tonic_pc is not None:
            self._key[(int(profile.tonic_pc), _text(profile.mode))].add(path)
        self._meter[_text(profile.meter_family or profile.meter_display)].add(path)
        self._progression[_text(profile.progression_family)].add(path)
        self._style[_text(profile.style)].add(path)
        if profile.melody_track_index is not None and profile.accompaniment_track_index is not None:
            self._role_ready.add(path)

    def _unindex_profile(self, profile: MidiProfile) -> None:
        path = profile.path
        tempo = _tempo_bin(profile.felt_bpm)
        if tempo is not None:
            self._tempo.get(tempo, set()).discard(path)
        if profile.tonic_pc is not None:
            self._key.get((int(profile.tonic_pc), _text(profile.mode)), set()).discard(path)
        self._meter.get(_text(profile.meter_family or profile.meter_display), set()).discard(path)
        self._progression.get(_text(profile.progression_family), set()).discard(path)
        self._style.get(_text(profile.style), set()).discard(path)
        self._role_ready.discard(path)

    def upsert_profile(self, profile: MidiProfile) -> None:
        """Incrementally add/replace one deeply-ready candidate in O(1) indexing work.

        Deep Analysis completes continuously in a large Library. Rebuilding the
        full 96K candidate index every few completions is both slow and creates a
        stale-snapshot window. This mutation is protected by the index lock so
        the relationship builder may query candidates concurrently.
        """
        with self._lock:
            old = self.by_path.get(profile.path)
            if old is None:
                self._append_profile(profile)
                return
            self._unindex_profile(old)
            self.by_path[profile.path] = profile
            for index, current in enumerate(self.profiles):
                if current.path == profile.path:
                    self.profiles[index] = profile
                    break
            self._index_replacement(profile)

    def _index_replacement(self, profile: MidiProfile) -> None:
        path = profile.path
        tempo = _tempo_bin(profile.felt_bpm)
        if tempo is not None:
            self._tempo[tempo].add(path)
        if profile.tonic_pc is not None:
            self._key[(int(profile.tonic_pc), _text(profile.mode))].add(path)
        self._meter[_text(profile.meter_family or profile.meter_display)].add(path)
        self._progression[_text(profile.progression_family)].add(path)
        self._style[_text(profile.style)].add(path)
        if profile.melody_track_index is not None and profile.accompaniment_track_index is not None:
            self._role_ready.add(path)

    def remove_path(self, path: str) -> bool:
        value = str(path)
        with self._lock:
            old = self.by_path.pop(value, None)
            if old is None:
                return False
            self._unindex_profile(old)
            self.profiles = [profile for profile in self.profiles if profile.path != value]
            return True

    def candidate_paths(self, anchor_path: str, *, limit: int = 96) -> tuple[str, ...]:
        with self._lock:
            anchor = self.by_path.get(anchor_path)
            if anchor is None:
                return ()

            candidates: set[str] = set()
            tempo = _tempo_bin(anchor.felt_bpm)
            if tempo is not None:
                # About +/-24 BPM at ordinary tempos. Half/double-time candidates
                # are included as a smaller secondary set for Assisted/Workbench.
                for bucket in range(tempo - 6, tempo + 7):
                    candidates.update(self._tempo.get(bucket, ()))
                for relationship in (tempo * 2, int(round(tempo / 2))):
                    for bucket in range(relationship - 2, relationship + 3):
                        candidates.update(self._tempo.get(bucket, ()))

            meter = _text(anchor.meter_family or anchor.meter_display)
            candidates.update(self._meter.get(meter, ()))
            progression = _text(anchor.progression_family)
            if progression and progression != "unclassified":
                candidates.update(self._progression.get(progression, ()))
            if anchor.tonic_pc is not None:
                tonic = int(anchor.tonic_pc)
                mode = _text(anchor.mode)
                candidates.update(self._key.get((tonic, mode), ()))
                relative = _relative_key(anchor)
                if relative:
                    candidates.update(self._key.get(relative, ()))
                for other_pc in range(12):
                    if _circle_distance(tonic, other_pc) <= 1:
                        candidates.update(self._key.get((other_pc, mode), ()))
            candidates.discard(anchor_path)

            def rank(path: str) -> tuple[float, float, float, float, float, str]:
                other = self.by_path[path]
                score = 0.0
                a_bpm = float(anchor.felt_bpm or 0.0)
                b_bpm = float(other.felt_bpm or 0.0)
                if a_bpm and b_bpm:
                    direct = abs(a_bpm - b_bpm) / max(a_bpm, b_bpm)
                    half = min(abs(a_bpm - b_bpm * 2), abs(a_bpm * 2 - b_bpm)) / max(a_bpm, b_bpm)
                    score += max(0.0, 6.0 - min(direct, half * 0.8) * 30.0)
                if _text(anchor.meter_family or anchor.meter_display) == _text(other.meter_family or other.meter_display):
                    score += 4.0
                if anchor.tonic_pc is not None and other.tonic_pc is not None:
                    distance = _circle_distance(int(anchor.tonic_pc), int(other.tonic_pc))
                    score += max(0.0, 4.0 - distance)
                    if _relative_key(anchor) == (int(other.tonic_pc), _text(other.mode)):
                        score += 2.0
                if progression and progression == _text(other.progression_family) and progression != "unclassified":
                    score += 3.5
                if anchor.path in self._role_ready and other.path in self._role_ready:
                    score += 2.0
                style_same = _text(anchor.style) == _text(other.style)
                score += 1.25 if style_same else 0.5
                # Prefer dependable analysis and sufficient shared musical extent.
                score += min(int(anchor.orchestration_readiness), int(other.orchestration_readiness)) / 100.0
                score += min(float(anchor.bars or 0), float(other.bars or 0), 64.0) / 128.0
                return (
                    score,
                    min(int(anchor.key_confidence), int(other.key_confidence)),
                    min(int(anchor.orchestration_readiness), int(other.orchestration_readiness)),
                    min(float(anchor.bars or 0), float(other.bars or 0)),
                    -abs(a_bpm - b_bpm) if a_bpm and b_bpm else -9999.0,
                    path.casefold(),
                )

            requested = max(1, int(limit))
            available = (path for path in candidates if path in self.by_path)
            # Six-figure libraries can put tens of thousands of paths in the cheap
            # metadata neighborhood. Sorting that whole neighborhood for a request
            # for 10-200 candidates is unnecessary O(N log N) work. Keep only the
            # requested best paths with a bounded heap.
            if len(candidates) > requested * 4:
                ordered = heapq.nlargest(requested, available, key=rank)
            else:
                ordered = sorted(available, key=rank, reverse=True)[:requested]
            return tuple(ordered)
    def neighbor_map(self, *, per_anchor: int = 48) -> dict[str, tuple[str, ...]]:
        with self._lock:
            profiles = tuple(self.profiles)
        return {profile.path: self.candidate_paths(profile.path, limit=per_anchor) for profile in profiles}

    def pair_paths(self, *, per_anchor: int = 32, maximum_pairs: int | None = None) -> set[tuple[str, str]]:
        maximum = maximum_pairs or max(500, len(self.profiles) * 40)
        pairs: set[tuple[str, str]] = set()
        with self._lock:
            profiles = tuple(self.profiles)
        for profile in profiles:
            for other_path in self.candidate_paths(profile.path, limit=per_anchor):
                if profile.path == other_path:
                    continue
                pairs.add(tuple(sorted((profile.path, other_path))))
                if len(pairs) >= maximum:
                    return pairs
        return pairs


class CandidateIndexCache:
    """SQLite persistence for deterministic per-library neighbor lists."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS candidate_indexes (
                signature TEXT NOT NULL,
                version TEXT NOT NULL,
                per_anchor INTEGER NOT NULL,
                profile_count INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(signature, version, per_anchor)
            )
            """
        )
        self._connection.commit()
        self.hits = 0
        self.misses = 0

    def neighbor_map(self, profiles: Sequence[MidiProfile], *, per_anchor: int = 48) -> tuple[dict[str, tuple[str, ...]], CandidateIndexStats]:
        signature = library_signature(profiles)
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM candidate_indexes WHERE signature=? AND version=? AND per_anchor=?",
                (signature, CANDIDATE_INDEX_VERSION, int(per_anchor)),
            ).fetchone()
        if row:
            try:
                raw = json.loads(row[0])
                allowed = {profile.path for profile in profiles}
                mapping = {
                    str(path): tuple(str(value) for value in values if str(value) in allowed and str(value) != str(path))
                    for path, values in raw.items() if str(path) in allowed
                }
                if len(mapping) == len(allowed):
                    self.hits += 1
                    pair_count = len({tuple(sorted((path, other))) for path, values in mapping.items() for other in values})
                    return mapping, CandidateIndexStats(len(profiles), pair_count, True, signature)
            except (TypeError, ValueError, json.JSONDecodeError):
                with self._lock:
                    self._connection.execute(
                        "DELETE FROM candidate_indexes WHERE signature=? AND version=? AND per_anchor=?",
                        (signature, CANDIDATE_INDEX_VERSION, int(per_anchor)),
                    )
                    self._connection.commit()

        self.misses += 1
        mapping = CandidateIndex(profiles).neighbor_map(per_anchor=per_anchor)
        payload = json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO candidate_indexes(signature, version, per_anchor, profile_count, payload) VALUES(?,?,?,?,?)",
                (signature, CANDIDATE_INDEX_VERSION, int(per_anchor), len(profiles), payload),
            )
            # Retain only a few current library signatures to avoid unbounded growth.
            self._connection.execute(
                "DELETE FROM candidate_indexes WHERE rowid NOT IN (SELECT rowid FROM candidate_indexes ORDER BY created_at DESC, rowid DESC LIMIT 12)"
            )
            self._connection.commit()
        pair_count = len({tuple(sorted((path, other))) for path, values in mapping.items() for other in values})
        return mapping, CandidateIndexStats(len(profiles), pair_count, False, signature)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


_CACHE_BY_PATH: dict[str, CandidateIndexCache] = {}
_CACHE_LOCK = threading.RLock()


def _cache_for_root(app_root: str | Path | None) -> CandidateIndexCache | None:
    if not app_root:
        return None
    path = str((AppPaths.for_app(app_root).ensure().library / "candidate_index.sqlite3").resolve())
    with _CACHE_LOCK:
        cache = _CACHE_BY_PATH.get(path)
        if cache is None:
            cache = CandidateIndexCache(path)
            _CACHE_BY_PATH[path] = cache
        return cache


def candidate_neighbor_map(
    profiles: Sequence[MidiProfile], *, app_root: str | Path | None = None, per_anchor: int = 48
) -> tuple[dict[str, tuple[str, ...]], CandidateIndexStats]:
    cache = _cache_for_root(app_root)
    if cache is not None:
        return cache.neighbor_map(profiles, per_anchor=per_anchor)
    signature = library_signature(profiles)
    mapping = CandidateIndex(profiles).neighbor_map(per_anchor=per_anchor)
    pair_count = len({tuple(sorted((path, other))) for path, values in mapping.items() for other in values})
    return mapping, CandidateIndexStats(len(profiles), pair_count, False, signature)


def indexed_candidate_pairs(
    profiles: Sequence[MidiProfile], *, app_root: str | Path | None = None, per_anchor: int = 32
) -> set[tuple[str, str]]:
    mapping, _stats = candidate_neighbor_map(profiles, app_root=app_root, per_anchor=per_anchor)
    maximum = max(500, len(profiles) * 40)
    pairs: set[tuple[str, str]] = set()
    for path, neighbors in mapping.items():
        for other in neighbors:
            if path != other:
                pairs.add(tuple(sorted((path, other))))
                if len(pairs) >= maximum:
                    return pairs
    return pairs


def indexed_anchor_candidates(
    profiles: Sequence[MidiProfile], anchor_path: str, *, app_root: str | Path | None = None, limit: int = 96
) -> tuple[str, ...]:
    mapping, _stats = candidate_neighbor_map(profiles, app_root=app_root, per_anchor=max(1, int(limit)))
    return tuple(mapping.get(anchor_path, ()))[: max(1, int(limit))]


def close_candidate_index_caches() -> None:
    """Close every process-local persistent candidate index connection."""
    with _CACHE_LOCK:
        caches = list(_CACHE_BY_PATH.values())
        _CACHE_BY_PATH.clear()
    for cache in caches:
        try:
            cache.close()
        except sqlite3.Error:
            continue
