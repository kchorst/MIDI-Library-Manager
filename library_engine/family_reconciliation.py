from __future__ import annotations

"""Deep-analysis composition-family reconciliation for Library Contract 4.

v0.39.1 persisted family IDs primarily from catalog/title/filename evidence.
That is fast enough for first discovery but can split opaque aliases of the same
composition.  v0.39.2 performs one bounded, source-file-free reconciliation over
already persisted Deep profiles and then incrementally reconciles each new Deep
completion.
"""

from collections import defaultdict
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Callable, Iterable

from core.library_index import LibraryIndex
from core.song_identity import (
    IDENTITY_RULESET_VERSION,
    authoritative_same_composition_reason,
    identity_index_keys,
)
from midi_library_scan.database import LibraryDatabase


@dataclass(frozen=True, slots=True)
class FamilyReconciliationResult:
    ruleset: int
    deep_profiles: int
    candidate_groups: int
    compared_pairs: int
    changed_paths: int
    merged_family_count: int
    split_family_count: int
    family_ids_to_reclassify: tuple[str, ...]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.rank: dict[str, int] = {}

    def add(self, value: str) -> None:
        if value not in self.parent:
            self.parent[value] = value
            self.rank[value] = 0

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def _deep_family_id(seed_path: str) -> str:
    digest = hashlib.sha1(str(seed_path).casefold().encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"deep2:{digest}"


def _family_priority(value: str) -> tuple[int, str]:
    text = str(value or "")
    if text.startswith("catalog:"):
        return (4, text)
    if text.startswith("title:"):
        return (3, text)
    if text.startswith("filename:"):
        return (2, text)
    if text.startswith("deep2:"):
        return (1, text)
    return (0, text)


def _compare_group(
    paths: Iterable[str],
    *,
    index: LibraryIndex,
    union_find: _UnionFind,
    stop: Callable[[], bool],
) -> int:
    """Compare one candidate bucket without allowing a pathological O(N²) group."""
    values = sorted(set(str(path) for path in paths if str(path).strip()), key=str.casefold)
    if len(values) < 2:
        return 0
    compared = 0

    if len(values) <= 48:
        profiles = index.load_many(values, validate_sources=False)
        ordered = [profiles[path] for path in values if path in profiles]
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                if stop():
                    return compared
                if union_find.find(left.path) == union_find.find(right.path):
                    continue
                compared += 1
                if authoritative_same_composition_reason(left, right):
                    union_find.union(left.path, right.path)
        return compared

    # Large buckets are usually a broad legacy filename family or a prolific
    # artist/form bucket.  Compare each item against a bounded set of structural
    # representatives.  Same-song version piles collapse immediately against the
    # first representative; unrelated songs do not create quadratic work.
    representatives = []
    for start in range(0, len(values), 96):
        if stop():
            break
        batch_values = values[start:start + 96]
        profiles = index.load_many(batch_values, validate_sources=False)
        for path in batch_values:
            profile = profiles.get(path)
            if profile is None:
                continue
            matched = False
            for representative in representatives:
                if union_find.find(profile.path) == union_find.find(representative.path):
                    matched = True
                    break
                compared += 1
                if authoritative_same_composition_reason(profile, representative):
                    union_find.union(profile.path, representative.path)
                    matched = True
                    break
            if not matched:
                if len(representatives) < 64:
                    representatives.append(profile)
                else:
                    # Once a broad bucket has 64 distinct representatives, other
                    # identity keys (melody/name/structure) remain responsible for
                    # finer clustering. This bounds worst-case work at 96K scale.
                    continue
    return compared


def reconcile_persisted_families(
    database: LibraryDatabase,
    index: LibraryIndex,
    *,
    stop: Callable[[], bool] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> FamilyReconciliationResult:
    """Reconcile all persisted Deep profiles without rescanning source MIDI files.

    Candidate identity keys are persisted in a rebuildable SQLite index while the
    profiles are already being decoded.  Only keys/families with 2+ members are
    materialized for comparison, so a 96K collection of mostly-singleton songs does
    not create hundreds of thousands of Python list/dict buckets.
    """
    cancelled = stop or (lambda: False)
    old_family = database.family_index()
    uf = _UnionFind()
    deep_profiles = 0

    for batch in index.iter_profile_batches(batch_size=192, cancel_callback=cancelled):
        if cancelled():
            break
        index.replace_identity_keys_batch(batch)
        for profile in batch:
            path = str(profile.path)
            if path not in old_family:
                continue
            uf.add(path)
            deep_profiles += 1
        if progress:
            progress(deep_profiles, 0, "Indexing persisted Deep identity")

    if cancelled():
        return FamilyReconciliationResult(
            IDENTITY_RULESET_VERSION, deep_profiles, 0, 0, 0, 0, 0, (),
        )

    descriptors: list[tuple[int, int, str, str]] = []
    for key, count in index.duplicate_identity_key_groups():
        priority = 0 if key.startswith(("content:", "melody:", "deep-melody:", "name:", "harmony-activity:", "structure:")) else 2
        descriptors.append((priority, int(count), "identity", key))
    for family_id, count in database.duplicate_completed_family_groups():
        descriptors.append((1, int(count), "legacy", family_id))
    descriptors.sort(key=lambda item: (item[0], item[1], item[3]))

    compared = 0
    candidate_groups = 0
    total_groups = len(descriptors)
    for position, (_priority, _count, kind, value) in enumerate(descriptors, 1):
        if cancelled():
            break
        if kind == "identity":
            paths = index.identity_paths_for_key(value)
        else:
            paths = [str(row.get("path") or "") for row in database.family_rows(value, completed_only=True)]
        compared += _compare_group(paths, index=index, union_find=uf, stop=cancelled)
        candidate_groups += 1
        if progress and (position == total_groups or position % 32 == 0):
            progress(position, total_groups, "Reconciling composition/version families")

    components: dict[str, list[str]] = defaultdict(list)
    for path in sorted(uf.parent, key=str.casefold):
        components[uf.find(path)].append(path)

    roots_by_old: dict[str, set[str]] = defaultdict(set)
    for root, paths in components.items():
        for path in paths:
            roots_by_old[str(old_family.get(path) or f"path:{path}")].add(root)
    split_old = {family for family, roots in roots_by_old.items() if len(roots) > 1}
    split_family_count = len(split_old)

    keeper: dict[str, str] = {}
    for family in split_old:
        roots = roots_by_old[family]
        keeper[family] = max(
            roots,
            key=lambda root: (len(components[root]), tuple(sorted(components[root], key=str.casefold))[0].casefold()),
        )

    assignments: list[tuple[str, str]] = []
    family_ids_to_reclassify: set[str] = set()
    merged_family_count = 0

    for root, paths in components.items():
        old_ids = sorted({str(old_family.get(path) or f"path:{path}") for path in paths})
        allowed_old = [
            family for family in old_ids
            if family not in split_old or keeper.get(family) == root
        ]
        if allowed_old:
            chosen = max(allowed_old, key=_family_priority)
        else:
            chosen = _deep_family_id(min(paths, key=str.casefold))

        if len(old_ids) > 1:
            merged_family_count += 1
        affected = len(paths) > 1 or any(family in split_old for family in old_ids) or len(old_ids) > 1
        if affected:
            family_ids_to_reclassify.add(chosen)
        for path in paths:
            if str(old_family.get(path) or f"path:{path}") != chosen:
                assignments.append((path, chosen))

    changed_paths = database.set_family_ids(assignments)
    if changed_paths:
        database.reconcile_all_duplicate_metadata()

    return FamilyReconciliationResult(
        ruleset=IDENTITY_RULESET_VERSION,
        deep_profiles=deep_profiles,
        candidate_groups=candidate_groups,
        compared_pairs=compared,
        changed_paths=changed_paths,
        merged_family_count=merged_family_count,
        split_family_count=split_family_count,
        family_ids_to_reclassify=tuple(sorted(family_ids_to_reclassify)),
    )


def reconcile_completed_path_family(
    database: LibraryDatabase,
    index: LibraryIndex,
    path: str | Path,
    *,
    stop: Callable[[], bool] | None = None,
) -> str | None:
    """Incrementally reconcile one newly completed Deep profile.

    Fast/catalogue identity remains provisional until Deep evidence exists.  A new
    completion may therefore *merge* into an established family or *split itself*
    out of a filename-derived family whose completed members are musically
    different.  Pending siblings are never force-split: they wait for their own
    Deep completion.
    """
    cancelled = stop or (lambda: False)
    value = str(Path(path).expanduser().resolve(strict=False))
    profile = index.load_one(value, validate_source=False)
    quick = database.load_result(value, validate_source=False)
    if profile is None or quick is None or cancelled():
        return database.family_id_for_path(value)

    current_family = str(quick.family_id or "")
    current_family_paths = set(database.family_paths(current_family)) if current_family else set()
    candidates: set[str] = set(index.identity_neighbor_paths(profile, limit=192))
    candidates.update(current_family_paths)
    if quick.musical_fingerprint:
        candidates.update(database.paths_with_quick_musical_fingerprint(quick.musical_fingerprint, limit=192))
    candidates.discard(value)
    if not candidates:
        return current_family or database.family_id_for_path(value)

    profiles = index.load_many(sorted(candidates), validate_sources=False)
    matched_family_ids: set[str] = set()
    current_completed_seen = False
    current_family_matched = False
    for candidate_path, candidate in profiles.items():
        if cancelled():
            return database.family_id_for_path(value)
        family_id = str(database.family_id_for_path(candidate_path) or "")
        if current_family and family_id == current_family:
            current_completed_seen = True
        if authoritative_same_composition_reason(profile, candidate):
            if family_id:
                matched_family_ids.add(family_id)
                if family_id == current_family:
                    current_family_matched = True

    # Deep evidence disproved the provisional current family.  Split only this
    # completed row; pending siblings retain their hint until they are analyzed.
    if current_family and current_completed_seen and not current_family_matched and not matched_family_ids:
        chosen = _deep_family_id(value)
        database.set_family_ids(((value, chosen),))
        return chosen

    if not matched_family_ids:
        return current_family or database.family_id_for_path(value)

    if matched_family_ids == {current_family}:
        return current_family

    chosen = max(matched_family_ids, key=_family_priority)
    all_paths: set[str] = {value}
    for family_id in matched_family_ids:
        all_paths.update(database.family_paths(family_id))
    # If the current provisional family was not musically matched, do not drag its
    # pending/unverified siblings into the newly proven family.
    database.set_family_ids((member, chosen) for member in sorted(all_paths))
    return chosen
