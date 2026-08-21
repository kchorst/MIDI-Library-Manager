from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path
from typing import Iterable

from core.app_paths import AppPaths
from core.candidate_index import indexed_anchor_candidates, indexed_candidate_pairs
from core.reference_catalog import ReferenceCatalog, normalize_title
from core.remix_profile import MidiProfile, PairAssessment, assess_pair
from core.staged_library import is_provisional_profile


@dataclass(frozen=True)
class LibraryFilter:
    search: str = ""
    style: str = "All styles"
    composer: str = "All composers"
    meter: str = "All meters"
    mode: str = "All modes"
    bpm_min: int = 0
    bpm_max: int = 0
    roles_ready_only: bool = False
    arrangement_type: str = "All arrangements"
    progression_family: str = "All progression families"
    minimum_orchestration_readiness: int = 0


def profile_matches(profile: MidiProfile, criteria: LibraryFilter) -> bool:
    # Cached or third-party MIDI metadata may contain null/non-string values.
    # Library browsing must treat them as blank labels rather than crash the UI.
    text = lambda value: "" if value is None else str(value)
    needle = text(criteria.search).strip().casefold()
    if needle:
        haystack = " ".join((
            text(profile.title), text(profile.composer), text(profile.style), text(profile.path),
            text(profile.key_display), text(profile.analyzed_key_display), text(profile.meter_display),
            text(profile.arrangement_type), text(profile.progression_family),
        )).casefold()
        if needle not in haystack:
            return False
    if text(criteria.style) != "All styles" and text(profile.style) != text(criteria.style):
        return False
    if text(criteria.composer) != "All composers" and text(profile.composer) != text(criteria.composer):
        return False
    if text(criteria.meter) != "All meters" and text(profile.meter_display) != text(criteria.meter):
        return False
    if text(criteria.mode) != "All modes" and text(profile.mode) != text(criteria.mode):
        return False
    if text(criteria.arrangement_type) != "All arrangements" and text(profile.arrangement_type) != text(criteria.arrangement_type):
        return False
    if text(criteria.progression_family) != "All progression families" and text(profile.progression_family) != text(criteria.progression_family):
        return False
    bpm = int(round(profile.felt_bpm or 0))
    if criteria.bpm_min and bpm < criteria.bpm_min:
        return False
    if criteria.bpm_max and bpm > criteria.bpm_max:
        return False
    if criteria.roles_ready_only and (profile.melody_track_index is None or profile.accompaniment_track_index is None):
        return False
    if profile.orchestration_readiness < int(criteria.minimum_orchestration_readiness or 0):
        return False
    return True


def filter_profiles(profiles: Iterable[MidiProfile], criteria: LibraryFilter) -> list[MidiProfile]:
    return [profile for profile in profiles if profile_matches(profile, criteria)]


def _same_composition(a: MidiProfile, b: MidiProfile) -> bool:
    return bool(a.song_identity and b.song_identity and a.song_identity == b.song_identity)


def _preliminary_pairs(
    profiles: list[MidiProfile], *, app_root: str | Path | None = None
) -> set[tuple[str, str]]:
    """Create an indexed, bounded candidate set without N² pairing.

    Full musical scoring still decides quality.  The index only retrieves a
    broad set of tempo/key/meter/progression/role neighbors and is persisted for
    unchanged libraries when an application root is available.
    """
    return indexed_candidate_pairs(profiles, app_root=app_root, per_anchor=36)


def _catalog_for_root(app_root: str | Path | None) -> ReferenceCatalog | None:
    if not app_root:
        return None
    path = Path(app_root) / "data" / "known_pairings.json"
    return ReferenceCatalog(
        path,
        import_directories=(AppPaths.for_app(app_root).ensure().reference_imports,),
    ) if path.is_file() else None


def _enrich(
    assessment: PairAssessment,
    catalog: ReferenceCatalog | None,
    source_a: MidiProfile | None = None,
    source_b: MidiProfile | None = None,
) -> PairAssessment:
    """Attach transparent reference evidence without altering musical scores."""
    if catalog is None:
        return assessment
    evidence = catalog.evidence_for_pair(
        assessment.title_a,
        assessment.title_b,
        source_a.composer if source_a else "",
        source_b.composer if source_b else "",
    )
    if not evidence.has_evidence:
        return assessment
    return replace(
        assessment,
        documented_reference=evidence.documented_reference,
        pairing_category=evidence.pairing_category or assessment.pairing_category,
        shared_progression_family=evidence.progression_family or assessment.shared_progression_family,
        reference_score=evidence.score,
        familiarity_score=evidence.familiarity_score,
        reference_evidence=evidence.reasons,
        reference_sources=evidence.providers,
        reference_urls=evidence.source_urls,
    )


def _reference_profile_pairs(profiles: list[MidiProfile], catalog: ReferenceCatalog | None) -> set[tuple[str, str]]:
    """Resolve imported title-pair evidence to paths already present in the library."""
    if catalog is None:
        return set()
    by_title: dict[str, list[MidiProfile]] = {}
    for profile in profiles:
        by_title.setdefault(normalize_title(profile.title), []).append(profile)
    result: set[tuple[str, str]] = set()
    for left_title, right_title in catalog.candidate_title_pairs():
        for left in by_title.get(left_title, ()):
            for right in by_title.get(right_title, ()):
                if left.path != right.path and not _same_composition(left, right):
                    result.add(tuple(sorted((left.path, right.path))))
    return result


def candidate_assessments(
    profiles: Iterable[MidiProfile],
    *,
    anchor_path: str | None = None,
    limit: int = 250,
    app_root: str | Path | None = None,
) -> list[PairAssessment]:
    items = [profile for profile in profiles if getattr(profile, "path", None)]
    if len(items) < 2:
        return []
    by_path = {item.path: item for item in items}
    catalog = _catalog_for_root(app_root)
    if anchor_path and anchor_path in by_path:
        anchor = by_path[anchor_path]
        if len(items) <= 150:
            anchor_candidates = [other for other in items if other.path != anchor.path]
        else:
            candidate_paths = set(indexed_anchor_candidates(items, anchor.path, app_root=app_root, limit=max(96, int(limit) * 3)))
            # Documented/imported relationships remain retrievable even when
            # cheap metadata buckets would not normally place them nearby.
            candidate_paths.update(
                other for pair in _reference_profile_pairs(items, catalog)
                if anchor.path in pair
                for other in pair if other != anchor.path
            )
            anchor_candidates = [by_path[path] for path in candidate_paths if path in by_path]
        assessed = [
            _enrich(assess_pair(anchor, other), catalog, anchor, other)
            for other in anchor_candidates
            if not _same_composition(anchor, other)
        ]
    elif len(items) <= 150:
        assessed = [_enrich(assess_pair(a, b), catalog, a, b) for a, b in combinations(items, 2) if not _same_composition(a, b)]
    else:
        assessed = []
        candidate_paths = _preliminary_pairs(items, app_root=app_root) | _reference_profile_pairs(items, catalog)
        for a_path, b_path in candidate_paths:
            a = by_path.get(a_path); b = by_path.get(b_path)
            if a and b and not _same_composition(a, b):
                assessed.append(_enrich(assess_pair(a, b), catalog, a, b))
    # Harmonic and key columns now influence ranking; style similarity does not dominate.
    assessed.sort(
        key=lambda item: (
            # Actual MIDI evidence stays first. Reference/familiarity metadata is
            # intentionally a bounded tie-breaker and cannot rescue a weak pair.
            item.harmonic_score,
            max(item.melody_a_over_b_score, item.melody_b_over_a_score),
            item.key_score,
            item.preparation_score,
            item.role_score,
            item.tempo_score,
            item.reference_score,
            item.familiarity_score,
        ),
        reverse=True,
    )
    return assessed[: max(1, int(limit))]


def filter_assessments(
    assessments: Iterable[PairAssessment],
    *,
    search: str = "",
    relationship: str = "All relationships",
    minimum_preparation: int = 0,
    minimum_harmonic: int = 0,
    pairing_category: str = "All categories",
) -> list[PairAssessment]:
    needle = search.strip().casefold()
    result: list[PairAssessment] = []
    for item in assessments:
        if item.preparation_score < minimum_preparation:
            continue
        if item.harmonic_score < minimum_harmonic:
            continue
        if relationship != "All relationships" and item.relationship != relationship:
            continue
        if pairing_category != "All categories" and item.pairing_category != pairing_category:
            continue
        haystack = (
            f"{item.title_a} {item.title_b} {item.relationship} {item.pairing_category} "
            f"{item.documented_reference or ''} {' '.join(item.reference_sources)} {' '.join(item.reference_evidence)}"
        ).casefold()
        if needle and needle not in haystack:
            continue
        result.append(item)
    return result
