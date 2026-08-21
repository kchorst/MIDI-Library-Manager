from __future__ import annotations

import hashlib
from collections import defaultdict

from .models import DuplicateKind, QuickScanResult
from .normalize import filename_family_key


def _group_id(prefix: str, members: list[QuickScanResult]) -> str:
    seed = "|".join(sorted(m.path for m in members))
    return f"{prefix}-{hashlib.sha1(seed.encode()).hexdigest()[:10]}"


def assign_duplicate_groups(results: list[QuickScanResult]) -> None:
    exact: dict[str, list[QuickScanResult]] = defaultdict(list)
    by_composition: dict[str, list[QuickScanResult]] = defaultdict(list)
    by_name_and_fp: dict[tuple[str, str | None], list[QuickScanResult]] = defaultdict(list)

    for result in results:
        if result.status == "failed":
            continue
        exact[result.sha256].append(result)
        if result.catalog_match.composition_id:
            by_composition[result.catalog_match.composition_id].append(result)
            result.composition_group = result.catalog_match.composition_id
            result.duplicate_group = result.catalog_match.composition_id
        by_name_and_fp[(filename_family_key(result.filename), result.musical_fingerprint)].append(result)

    # Exact byte copies are marked explicitly, even when they also belong to a larger
    # canonical composition/version family.
    for members in exact.values():
        if len(members) < 2:
            continue
        group_id = _group_id("EXACT", members)
        for member in members:
            member.content_duplicate_group = group_id
            member.duplicate_kind = DuplicateKind.EXACT
            if not member.duplicate_group:
                member.duplicate_group = group_id

    # Unmatched repeated exports can still be grouped by normalized filename plus
    # transposition-tolerant event fingerprint.
    for members in by_name_and_fp.values():
        if len(members) < 2:
            continue
        group_id = _group_id("EXPORT", members)
        for member in members:
            if member.content_duplicate_group:
                continue
            member.content_duplicate_group = group_id
            if member.duplicate_kind == DuplicateKind.NONE:
                member.duplicate_kind = DuplicateKind.LIKELY_SAME_EXPORT
            if not member.duplicate_group:
                member.duplicate_group = group_id

    # Multiple different files identified as the same composition are versions, not
    # separate beginner recommendations. Exact copies retain their stronger exact label.
    for composition_id, members in by_composition.items():
        if len(members) < 2:
            continue
        for member in members:
            member.composition_group = composition_id
            member.duplicate_group = composition_id
            if member.duplicate_kind == DuplicateKind.NONE:
                member.duplicate_kind = DuplicateKind.SAME_COMPOSITION
