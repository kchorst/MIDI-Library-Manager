from __future__ import annotations

"""Compact large-library mode summaries prepared away from the GUI thread."""

import heapq
from typing import Iterable, Mapping, Sequence

from core.assisted_studio import assisted_source_gate
from core.guided_beginner import guided_library_diagnostics
from core.remix_profile import MidiProfile


def build_library_mode_cache(profiles: Iterable[MidiProfile], *, anchor_limit: int = 256) -> dict[str, object]:
    """Exact small-library summaries.

    This path may use composition grouping and full gate diagnostics, so v0.27
    reserves it for modest libraries. Six-figure catalogues use
    :func:`build_large_library_mode_cache` instead.
    """
    items = tuple(profiles)

    diagnostics = guided_library_diagnostics(items)
    song_rows = tuple(diagnostics.get("song_rows", ()))
    guided_summary = {
        "total": int(diagnostics.get("total_files", len(items))),
        "distinct_songs": int(diagnostics.get("distinct_songs", len(items))),
        "duplicate_variants": int(diagnostics.get("duplicate_variants", 0)),
        "guided_ready": int(diagnostics.get("guided_ready_files", 0)),
        "guided_ready_songs": int(diagnostics.get("guided_ready_songs", 0)),
        "restricted": sum(
            1 for _profile, gate in song_rows
            if getattr(gate, "eligible", False) and "keyboard" in str(getattr(gate, "level", "")).casefold()
        ),
        "hidden": int(diagnostics.get("hidden_files", 0)),
    }
    guided_diagnostics = {
        "total_files": guided_summary["total"],
        "distinct_songs": guided_summary["distinct_songs"],
        "duplicate_variants": guided_summary["duplicate_variants"],
        "guided_ready_files": guided_summary["guided_ready"],
        "guided_ready_songs": guided_summary["guided_ready_songs"],
        "hidden_files": guided_summary["hidden"],
        "exclusions": dict(diagnostics.get("exclusions", {})),
        "sample_rows": tuple(
            (
                profile.path,
                profile.title,
                str(getattr(gate, "level", "")),
                str(getattr(gate, "summary", "")),
            )
            for profile, gate in song_rows[:20]
        ),
    }

    assisted_counts = {
        "total": len(items),
        "eligible": 0,
        "studio_ready": 0,
        "adaptable": 0,
        "whole_section": 0,
        "hidden": 0,
    }
    ranked_anchors: list[tuple[tuple[int, int, int, str], str]] = []
    for profile in items:
        gate = assisted_source_gate(profile)
        if gate.eligible:
            assisted_counts["eligible"] += 1
            if gate.level == "Studio-ready":
                assisted_counts["studio_ready"] += 1
            elif gate.level == "Adaptable":
                assisted_counts["adaptable"] += 1
            elif gate.level == "Whole-section source":
                assisted_counts["whole_section"] += 1
            ranked_anchors.append((
                (
                    int(getattr(gate, "confidence", 0) or 0),
                    int(getattr(profile, "orchestration_readiness", 0) or 0),
                    int(getattr(profile, "note_count", 0) or 0),
                    str(getattr(profile, "title", "")).casefold(),
                ),
                profile.path,
            ))
        else:
            assisted_counts["hidden"] += 1

    ranked_anchors.sort(key=lambda item: item[0], reverse=True)
    anchor_paths = tuple(path for _rank, path in ranked_anchors[: max(1, int(anchor_limit))])

    return {
        "guided_summary": guided_summary,
        "guided_diagnostics": guided_diagnostics,
        "assisted_summary": assisted_counts,
        "assisted_anchor_paths": anchor_paths,
    }


def build_large_library_mode_cache(
    profiles: Sequence[MidiProfile] | Iterable[MidiProfile],
    *,
    distinct_songs: int,
    readiness_summary: Mapping[str, int] | None = None,
    deep_anchor_paths: Sequence[str] = (),
    anchor_limit: int = 256,
) -> dict[str, object]:
    """Linear/heap-bounded mode cache for a six-figure catalogue.

    Crucially this does *not* fuzzy-group 96k filenames and does not need to
    decode detailed profile JSON. Guided readiness comes from the compact
    indexed readiness table maintained when deep analysis completes. Assisted
    can still use quick summaries, so all catalogued files participate.
    """
    items = tuple(profiles)
    total = len(items)
    distinct = max(0, min(total, int(distinct_songs or 0)))
    readiness = dict(readiness_summary or {})
    guided_ready = max(0, min(total, int(readiness.get("guided_ready", 0) or 0)))
    guided_summary = {
        "total": total,
        "distinct_songs": distinct,
        "duplicate_variants": max(0, total - distinct),
        "guided_ready": guided_ready,
        "guided_ready_songs": min(guided_ready, distinct),
        "restricted": 0,
        "hidden": max(0, total - guided_ready),
    }
    guided_diagnostics = {
        "total_files": total,
        "distinct_songs": distinct,
        "duplicate_variants": guided_summary["duplicate_variants"],
        "guided_ready_files": guided_ready,
        "guided_ready_songs": guided_summary["guided_ready_songs"],
        "hidden_files": guided_summary["hidden"],
        "exclusions": {},
        "sample_rows": (),
    }

    assisted_counts = {
        "total": total,
        "eligible": 0,
        "studio_ready": 0,
        "adaptable": 0,
        "whole_section": 0,
        "hidden": 0,
    }
    deep_set = set(deep_anchor_paths)
    ranked: list[tuple[tuple[int, int, int, int, str], str]] = []
    for profile in items:
        gate = assisted_source_gate(profile)
        if not gate.eligible:
            assisted_counts["hidden"] += 1
            continue
        assisted_counts["eligible"] += 1
        if gate.level == "Studio-ready":
            assisted_counts["studio_ready"] += 1
        elif gate.level == "Adaptable":
            assisted_counts["adaptable"] += 1
        elif gate.level == "Whole-section source":
            assisted_counts["whole_section"] += 1
        # Prefer already-deep-analyzed anchors while still allowing any usable
        # quick-catalogued song to be selected manually.
        rank = (
            1 if profile.path in deep_set else 0,
            int(gate.confidence or 0),
            int(profile.orchestration_readiness or 0),
            int(profile.note_count or 0),
            str(profile.title or "").casefold(),
        )
        ranked.append((rank, profile.path))

    maximum = max(1, int(anchor_limit))
    anchor_paths = tuple(path for _rank, path in heapq.nlargest(maximum, ranked, key=lambda item: item[0]))
    return {
        "guided_summary": guided_summary,
        "guided_diagnostics": guided_diagnostics,
        "assisted_summary": assisted_counts,
        "assisted_anchor_paths": anchor_paths,
    }
