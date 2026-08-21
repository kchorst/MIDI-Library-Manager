from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations
from collections import Counter
from pathlib import Path
import sqlite3
from typing import Callable, Mapping, Sequence

from core.app_paths import AppPaths
from core.candidate_index import CandidateIndex, indexed_candidate_pairs
from core.discovery_graph import DiscoveryGraph
from core.remix_guidance import RecipeAdvice, recipe_advice, recommend_frame
from core.reference_catalog import ReferenceCatalog
from core.remix_profile import MidiProfile, PairAssessment, assess_pair, key_name, primary_signature_asset, windowed_pair_scores
from core.song_identity import composition_groups, distinct_representatives, probable_same_composition


@dataclass(frozen=True)
class GuidedSourceGate:
    eligible: bool
    level: str
    summary: str
    restrictions: tuple[str, ...]
    allowed_recipes: tuple[str, ...]
    confidence: int


@dataclass(frozen=True)
class GuidedPairPlan:
    source_a: MidiProfile
    source_b: MidiProfile
    assessment: PairAssessment
    category: str
    headline: str
    summary: str
    reasons: tuple[str, ...]
    transformations: tuple[str, ...]
    likely_heard: tuple[str, ...]
    recipe_keys: tuple[str, ...]
    target_bpm: int
    target_key_pc: int | None
    target_mode: str
    bars: int
    section_bars: int
    safety_score: int
    start_bar_a: int = 0
    start_bar_b: int = 0
    window_harmony_score: int = 0
    window_melody_a_over_b_score: int = 0
    window_melody_b_over_a_score: int = 0
    window_structure_score: int = 0
    lead_source: str = "a"
    backing_source: str = "b"
    contribution_plan: tuple[str, ...] = ()
    signature_plan: tuple[str, ...] = ()
    signature_source: str | None = None
    signature_asset: dict | None = None


@dataclass(frozen=True)
class GuidedNearMatch:
    source_a: MidiProfile
    source_b: MidiProfile
    assessment: PairAssessment
    label: str
    blockers: tuple[str, ...]
    promise_score: int


_SCORE_BANDS = (
    (80, "Strong", "little preparation is expected"),
    (65, "Good", "the engine can handle the main adjustments automatically"),
    (50, "Workable", "audible adjustment or selective section use is expected"),
    (0, "Experimental", "manual review is likely"),
)


def score_band(value: int) -> tuple[str, str]:
    value = max(0, min(100, int(value)))
    for threshold, label, meaning in _SCORE_BANDS:
        if value >= threshold:
            return label, meaning
    return "Experimental", "manual review is likely"


def score_help(metric: str, value: int) -> str:
    label, meaning = score_band(value)
    descriptions = {
        "preparation": "How much key, tempo, meter, role, length, and harmonic preparation the engine expects.",
        "harmony": "How often the detected chord functions and melody notes cooperate after proposed key alignment.",
        "direction": "How well one named source's melody fits over the other source's detected harmony.",
        "tempo": "How easily felt pulses align, including half-time and double-time interpretations.",
        "meter": "How easily bars and phrase grids line up.",
        "orchestration": "How confidently melody, accompaniment, bass, drums, and other roles are separated.",
    }
    explanation = descriptions.get(metric.casefold(), "A musical preparation estimate used by MIDI Remixer.")
    return f"{label} ({value}%): {explanation} In this range, {meaning}. This is not an artistic-quality score."


def guided_source_gate(profile: MidiProfile) -> GuidedSourceGate:
    """Strict first-mash gate.

    Guided Remix intentionally hides risky files. It permits a multitrack source
    with clear roles, or a two-hand keyboard reduction when the hands are
    actually separated. Single isolated parts and merged polyphonic files stay
    available in Assisted Studio / Creative Workbench.
    """
    if profile.note_count < 80 or profile.duration_seconds < 12 or profile.bars < 8:
        return GuidedSourceGate(
            False,
            "Not recommended for a first mash",
            "The file is too short or contains too little pitched material for a dependable beginner arrangement.",
            ("Use it later as a hook, fill, transition, or manually prepared source.",),
            (),
            0,
        )

    if profile.felt_bpm is None or not 40 <= float(profile.felt_bpm) <= 220:
        return GuidedSourceGate(
            False,
            "Not recommended for a first mash",
            "The engine cannot establish a dependable felt pulse in the beginner-safe range.",
            ("Tempo can still be assigned manually in Assisted Studio or Creative Workbench.",),
            (),
            max(0, profile.key_confidence // 3),
        )

    distinct_roles = (
        profile.melody_track_index is not None
        and profile.accompaniment_track_index is not None
        and profile.melody_track_index != profile.accompaniment_track_index
    )
    keyboard = profile.arrangement_type in {"piano reduction", "keyboard reduction"}
    single_or_merged = profile.arrangement_type in {"single-track", "single-track performance", "solo line", "unclassified"}

    if profile.tempo_change_count > 32 or len(profile.key_changes) > 12:
        return GuidedSourceGate(
            False,
            "Assisted Studio recommended",
            "The source contains many tempo or key-map changes, so a first-time automatic mash would hide too much uncertainty.",
            ("Assisted Studio can preserve, simplify, or manually choose the changing sections.",),
            (),
            max(20, min(59, profile.key_confidence)),
        )

    if not distinct_roles:
        return GuidedSourceGate(
            False,
            "Assisted Studio recommended",
            "The MIDI does not provide a dependable separate melody and foundation for an automatic first mash.",
            (
                "It may be one solo part, a merged piano performance, or a texture that needs voice extraction.",
                "Whole-section use remains possible outside Guided Remix.",
            ),
            (),
            max(10, min(49, profile.melody_extraction_confidence)),
        )

    if single_or_merged and not keyboard:
        return GuidedSourceGate(
            False,
            "Assisted Studio recommended",
            "The file's arrangement type is too uncertain for a restricted beginner workflow.",
            ("Review tracks and musical roles manually before remixing it.",),
            (),
            max(10, min(49, profile.orchestration_readiness)),
        )

    base_confidence = int(round(
        profile.key_confidence * 0.35
        + min(100, profile.orchestration_readiness + (42 if keyboard else 0)) * 0.35
        + min(100, profile.melody_extraction_confidence + (45 if keyboard else 0)) * 0.30
    ))

    if keyboard:
        melody_track = next((track for track in profile.note_tracks if track.index == profile.melody_track_index), None)
        foundation_track = next((track for track in profile.note_tracks if track.index == profile.accompaniment_track_index), None)
        if not melody_track or not foundation_track or min(melody_track.note_count, foundation_track.note_count) < 64:
            return GuidedSourceGate(
                False,
                "Assisted Studio recommended",
                "The keyboard reduction does not contain enough dependable material in both detected hands for a restricted first mash.",
                ("Review the tracks or use the file as a whole-section source outside Guided Remix.",),
                (),
                max(20, min(54, base_confidence)),
            )
        return GuidedSourceGate(
            True,
            "Guided-ready keyboard arrangement",
            "Separate upper and lower parts make this keyboard reduction usable for a restricted first mash.",
            (
                "The wizard keeps the result primarily as keyboard material.",
                "Solo-instrument reassignment is postponed until the user enters Instrumentation.",
                "Dense simultaneous layering is avoided unless the directional fit is strong.",
            ),
            (
                "melody-a-accompaniment-b",
                "melody-b-accompaniment-a",
                "section-exchange",
                "transformation",
            ),
            max(55, min(88, base_confidence)),
        )

    if profile.orchestration_readiness < 48 or profile.key_confidence < 35:
        return GuidedSourceGate(
            False,
            "Assisted Studio recommended",
            "The source has separate tracks, but its role or key analysis is not dependable enough for a first-time automatic mash.",
            ("The engine can still assist after the user reviews the track roles.",),
            (),
            max(20, min(59, base_confidence)),
        )

    return GuidedSourceGate(
        True,
        "Guided-ready multitrack arrangement",
        "The source has dependable timing, key information, and separable musical roles.",
        (),
        (
            "melody-a-accompaniment-b",
            "melody-b-accompaniment-a",
            "section-exchange",
            "conversation",
            "transformation",
            "layered-blend",
            "foundation-swap",
        ),
        max(65, min(98, base_confidence)),
    )


def _safe_recipe_keys(
    a: MidiProfile,
    b: MidiProfile,
    assessment: PairAssessment,
    *,
    start_bar_a: int = 0,
    start_bar_b: int = 0,
    bars: int = 32,
) -> tuple[str, ...]:
    gates = (guided_source_gate(a), guided_source_gate(b))
    common = set(gates[0].allowed_recipes).intersection(gates[1].allowed_recipes)
    advice: dict[str, RecipeAdvice] = {item.key: item for item in recipe_advice((a, b))}
    harmony, a_over_b, b_over_a, structure = windowed_pair_scores(
        a, b, start_bar_a=start_bar_a, start_bar_b=start_bar_b, bars=bars
    )
    safe: list[str] = []

    directional = [
        ("melody-a-accompaniment-b", a_over_b),
        ("melody-b-accompaniment-a", b_over_a),
    ]
    directional.sort(key=lambda item: item[1], reverse=True)
    for key, score in directional:
        if key in common and advice.get(key) and advice[key].eligible and score >= 72 and harmony >= 58:
            safe.append(key)
            break

    # Section-safe recipes are allowed only when the activity grids align after
    # intro trimming. This prevents one source's first theme from beginning
    # against another source's unrelated introduction.
    if structure >= 84:
        for key in ("section-exchange", "transformation"):
            if key in common and advice.get(key) and advice[key].eligible and key not in safe:
                safe.append(key)

    if (
        len(safe) < 3
        and "conversation" in common
        and advice.get("conversation")
        and advice["conversation"].eligible
        and min(a_over_b, b_over_a) >= 74
        and harmony >= 64
        and structure >= 84
    ):
        safe.append("conversation")

    return tuple(safe[:3])


def _guided_start_bar(profile: MidiProfile, role: str = "melody") -> int:
    """Return the role-specific main entry while leaving protected intros separate."""
    if str(role).casefold() == "backing":
        asset = primary_signature_asset(profile)
        protected_end = int(asset.get("end_bar", 0)) if asset and asset.get("protect_default") else 0
        return max(protected_end, int(round(float(profile.main_entry_bar or profile.accompaniment_start_bar or 0))))
    return max(0, int(round(float(profile.melody_start_bar))))


def song_family_key(profile: MidiProfile) -> str:
    """Backward-compatible diagnostic key; pairwise grouping is authoritative."""
    from core.song_identity import composition_family_key

    return composition_family_key(profile)


def _same_song_family(a: MidiProfile, b: MidiProfile) -> bool:
    return probable_same_composition(a, b)

def _pair_safe(a: MidiProfile, b: MidiProfile, assessment: PairAssessment) -> bool:
    if _same_song_family(a, b):
        return False
    if not guided_source_gate(a).eligible or not guided_source_gate(b).eligible:
        return False
    # Guided Remix is deliberately conservative: exact meter and a direct pulse
    # relationship are required for a dependable first mash. Half/double-time
    # experiments remain available in Assisted Studio.
    if assessment.meter_score < 100 or assessment.tempo_relationship != "direct":
        return False
    target = float(assessment.target_felt_bpm or 0)
    for profile in (a, b):
        source = float(profile.felt_bpm or 0)
        if not source or abs(target / source - 1.0) > 0.20:
            return False
    if assessment.tempo_score < 55 or assessment.preparation_score < 62 or assessment.key_score < 50:
        return False
    directions: list[tuple[int, int, int, int, int, int]] = []
    # A melody over B backing.
    a_lead, b_back = _guided_start_bar(a, "melody"), _guided_start_bar(b, "backing")
    remaining_ab = min(int(a.bars) - a_lead, int(b.bars) - b_back)
    if remaining_ab >= 32 and max(a_lead, b_back) <= 12:
        h, aob, _boa, st = windowed_pair_scores(a, b, start_bar_a=a_lead, start_bar_b=b_back, bars=min(48, remaining_ab))
        directions.append((aob, h, st, a_lead, b_back, remaining_ab))
    # B melody over A backing.
    a_back, b_lead = _guided_start_bar(a, "backing"), _guided_start_bar(b, "melody")
    remaining_ba = min(int(a.bars) - a_back, int(b.bars) - b_lead)
    if remaining_ba >= 32 and max(a_back, b_lead) <= 12:
        h, _aob, boa, st = windowed_pair_scores(a, b, start_bar_a=a_back, start_bar_b=b_lead, bars=min(48, remaining_ba))
        directions.append((boa, h, st, a_back, b_lead, remaining_ba))
    if not directions:
        return False
    direction, harmony, structure, start_a, start_b, remaining = max(directions, key=lambda item: (item[0], item[1], item[2]))
    simultaneous_ok = harmony >= 58 and direction >= 72 and structure >= 70
    section_safe_ok = harmony >= 45 and structure >= 84 and assessment.key_score >= 70
    if not (simultaneous_ok or section_safe_ok):
        return False
    return len(_safe_recipe_keys(
        a, b, assessment, start_bar_a=start_a, start_bar_b=start_b, bars=min(48, remaining)
    )) >= 2


def _safety_score(a: MidiProfile, b: MidiProfile, assessment: PairAssessment) -> int:
    gates = (guided_source_gate(a), guided_source_gate(b))
    direction = max(assessment.melody_a_over_b_score, assessment.melody_b_over_a_score)
    return int(round(
        assessment.preparation_score * 0.22
        + assessment.harmonic_score * 0.16
        + direction * 0.20
        + assessment.tempo_score * 0.12
        + assessment.meter_score * 0.08
        + assessment.structural_alignment_score * 0.07
        + assessment.entry_alignment_score * 0.05
        + min(gates[0].confidence, gates[1].confidence) * 0.20
    ))


def _category(a: MidiProfile, b: MidiProfile, assessment: PairAssessment) -> str:
    # Reference metadata can explain a verified result, but it never changes the
    # strict MIDI safety gate that allowed the result into Guided Remix.
    if assessment.reference_score >= 80:
        return "Documented precedent — verified against these MIDI files"
    if assessment.reference_score >= 40:
        return "External discovery lead — verified against these MIDI files"
    same_style = a.style == b.style and a.style != "Unclassified"
    keyboard = all(profile.arrangement_type in {"piano reduction", "keyboard reduction"} for profile in (a, b))
    if same_style and keyboard:
        return "Safe same-style keyboard experiment"
    if same_style:
        return "Safe same-style experiment"
    if assessment.harmonic_score >= 45 and max(assessment.melody_a_over_b_score, assessment.melody_b_over_a_score) >= 65:
        return "Strong cross-style bridge"
    return "Supported contrast"


def _make_plan(a: MidiProfile, b: MidiProfile, assessment: PairAssessment) -> GuidedPairPlan:
    a_lead, b_back = _guided_start_bar(a, "melody"), _guided_start_bar(b, "backing")
    rem_ab = max(1, min(int(a.bars) - a_lead, int(b.bars) - b_back))
    ab_scores = windowed_pair_scores(a, b, start_bar_a=a_lead, start_bar_b=b_back, bars=min(48, rem_ab))
    a_back, b_lead = _guided_start_bar(a, "backing"), _guided_start_bar(b, "melody")
    rem_ba = max(1, min(int(a.bars) - a_back, int(b.bars) - b_lead))
    ba_scores = windowed_pair_scores(a, b, start_bar_a=a_back, start_bar_b=b_lead, bars=min(48, rem_ba))
    if ab_scores[1] >= ba_scores[2]:
        lead_source, backing_source = "a", "b"
        start_a, start_b, remaining = a_lead, b_back, rem_ab
        window_harmony, window_a_over_b, window_b_over_a, window_structure = ab_scores
    else:
        lead_source, backing_source = "b", "a"
        start_a, start_b, remaining = a_back, b_lead, rem_ba
        window_harmony, window_a_over_b, window_b_over_a, window_structure = ba_scores
    remaining = max(32, remaining)
    window_bars = min(48, remaining)
    recipes = list(_safe_recipe_keys(
        a, b, assessment, start_bar_a=start_a, start_bar_b=start_b, bars=window_bars
    ))
    preferred_direction = "melody-a-accompaniment-b" if lead_source == "a" else "melody-b-accompaniment-a"
    if preferred_direction in recipes:
        recipes.remove(preferred_direction)
        recipes.insert(0, preferred_direction)
    recipes = tuple(recipes)
    frame = recommend_frame((a, b))
    # Guided mode uses the pair assessor's pulse relationship rather than a raw
    # average, avoiding accidental compromises between half/double-time files.
    frame["target_bpm"] = int(assessment.target_felt_bpm)
    # A first-time Trial should be long enough to feel musical but short enough
    # to audition several versions without waiting through a full arrangement.
    bars = max(32, min(64, int(frame["bars"]), remaining))
    frame["bars"] = bars
    target_pc = frame.get("target_key_pc")
    target_mode = str(frame.get("target_mode") or "major")
    target_key = key_name(int(target_pc) if isinstance(target_pc, int) else a.tonic_pc, target_mode)
    score = _safety_score(a, b, assessment)
    category = _category(a, b, assessment)

    lead_profile = a if lead_source == "a" else b
    backing_profile = b if backing_source == "b" else a
    direction = (
        f"{lead_profile.title}'s melody is the safer lead over {backing_profile.title}'s complete backing "
        "(drums, bass, chords, riffs, and fills, with its lead melody removed)."
    )
    signature_asset = primary_signature_asset(backing_profile)
    signature_source = backing_source if signature_asset and signature_asset.get("protect_default") else None
    contribution_plan = (
        f"{lead_profile.title}: lead melody only.",
        f"{backing_profile.title}: backing and supporting parts, with its detected lead melody removed.",
    )
    if signature_asset and signature_source:
        signature_plan = (
            f"Preserve {backing_profile.title}'s {signature_asset.get('label', 'opening passage').casefold()} from bars {int(signature_asset.get('start_bar', 0)) + 1}-{int(signature_asset.get('end_bar', 0))}.",
            f"Play that passage alone first; {lead_profile.title}'s melody enters after it completes.",
            "Keep the signature passage's internal rhythm and percussion mapping; pitched notes follow the non-destructive target-key plan.",
        )
    else:
        signature_plan = ("No high-confidence protected opening was detected in the selected backing source.",)

    a_bpm = float(a.felt_bpm or frame["target_bpm"])
    b_bpm = float(b.felt_bpm or frame["target_bpm"])
    target_bpm = int(frame["target_bpm"])
    a_change = ((target_bpm / a_bpm) - 1.0) * 100.0 if a_bpm else 0.0
    b_change = ((target_bpm / b_bpm) - 1.0) * 100.0 if b_bpm else 0.0
    direction_score = window_a_over_b if lead_source == "a" else window_b_over_a
    direction_label = score_band(direction_score)[0]
    meter_label = score_band(assessment.meter_score)[0]
    tempo_label = score_band(assessment.tempo_score)[0]
    harmony_label = score_band(window_harmony)[0]
    gate_a = guided_source_gate(a)
    gate_b = guided_source_gate(b)

    summary = (
        f"{category}. {a.title} is {a.key_display} at {a_bpm:g} felt BPM; "
        f"{b.title} is {b.key_display} at {b_bpm:g} felt BPM. "
        f"The engine verified separate melody/backing roles, {meter_label.lower()} phrase-grid alignment, "
        f"and {len(recipes)} arrangement path(s) it can generate without manual track preparation."
    )
    reference_reason = tuple(assessment.reference_evidence)
    if assessment.familiarity_score and not reference_reason:
        reference_reason = (
            f"Imported familiarity metadata is available ({assessment.familiarity_score}%). It is only a ranking tie-breaker, not musical compatibility.",
        )
    reasons = (
        f"Source readiness: {a.title} — {gate_a.level} ({gate_a.confidence}%); {b.title} — {gate_b.level} ({gate_b.confidence}%).",
        *reference_reason,
        f"Key plan: {a.key_display} + {b.key_display} → {target_key}; Source B is shifted {assessment.key_shift_b_to_a:+d} semitone(s).",
        f"Tempo plan: {a_bpm:g} and {b_bpm:g} felt BPM → {target_bpm} BPM ({a.title} {a_change:+.0f}%, {b.title} {b_change:+.0f}%). Tempo relationship: {tempo_label} ({assessment.tempo_score}%).",
        f"Meter/phrasing: both sources use {a.meter}; window structure alignment is {score_band(window_structure)[0]} ({window_structure}%).",
        f"Contribution plan: {lead_profile.title} supplies only its lead melody; {backing_profile.title} supplies everything except its detected lead melody.",
        f"Entry alignment: the lead begins at its melody entry while the backing begins at its main texture entry, so unrelated introductions are not layered accidentally.",
        f"Melody direction: {direction} Directional fit in the chosen window is {direction_label} ({direction_score}%).",
        *(signature_plan[:2]),
        f"Harmony is {harmony_label} in the chosen window ({window_harmony}%); Guided Remix rejects pairs that need broad harmonic repair.",
        f"The first Trials use {bars} bars so the user can compare complete musical ideas without waiting through both full songs.",
    )
    transformations = (
        f"Target key: {target_key}; Source B moves {assessment.key_shift_b_to_a:+d} semitone(s) toward Source A's tonic.",
        f"Target pulse: {target_bpm} felt BPM; Source A changes {a_change:+.0f}% and Source B changes {b_change:+.0f}% inside the Trial only.",
        f"Aligned main starting points: Source A bar {start_a + 1}; Source B bar {start_b + 1}.",
        *(signature_plan),
        "Automatic dynamics normalization reduces a dense backing before the final mixer.",
        f"Only these prevalidated recipes are offered: {', '.join(key.replace('-', ' ') for key in recipes)}.",
    )
    likely_heard = (
        *( (f"The {signature_asset.get('label', 'signature opening').casefold()} plays alone before the surprise entrance." ,) if signature_source else () ),
        f"{lead_profile.title}'s melody is heard over {backing_profile.title}'s fuller backing rather than over a single chord track.",
        "A section-based Trial keeps each source's own harmony when simultaneous layering is less convincing.",
        "A transformation Trial gradually changes which source controls the arrangement.",
    )
    return GuidedPairPlan(
        source_a=a,
        source_b=b,
        assessment=assessment,
        category=category,
        headline=f"{a.title} + {b.title}",
        summary=summary,
        reasons=reasons,
        transformations=transformations,
        likely_heard=likely_heard,
        recipe_keys=recipes,
        target_bpm=int(frame["target_bpm"]),
        target_key_pc=int(target_pc) if isinstance(target_pc, int) else None,
        target_mode=target_mode,
        bars=bars,
        section_bars=int(frame["section_bars"]),
        safety_score=max(0, min(100, score)),
        start_bar_a=start_a,
        start_bar_b=start_b,
        window_harmony_score=window_harmony,
        window_melody_a_over_b_score=window_a_over_b,
        window_melody_b_over_a_score=window_b_over_a,
        window_structure_score=window_structure,
        lead_source=lead_source,
        backing_source=backing_source,
        contribution_plan=contribution_plan,
        signature_plan=signature_plan,
        signature_source=signature_source,
        signature_asset=signature_asset if signature_source else None,
    )


def _section_switch_candidate(a: MidiProfile, b: MidiProfile, assessment: PairAssessment) -> bool:
    """A controlled beginner experiment that never relies on simultaneous harmony.

    Complete sections keep each song's own chords, bass, drums, and melody together.
    This lets Guided Remix offer more genuinely different ideas without pretending
    that every pair supports melody-over-backing layering.
    """
    if _same_song_family(a, b):
        return False
    gate_a, gate_b = guided_source_gate(a), guided_source_gate(b)
    if not gate_a.eligible or not gate_b.eligible:
        return False
    if "section-exchange" not in set(gate_a.allowed_recipes).intersection(gate_b.allowed_recipes):
        return False
    if assessment.meter_score < 100 or assessment.tempo_relationship != "direct":
        return False
    target = float(assessment.target_felt_bpm or 0)
    if not target:
        return False
    for profile in (a, b):
        source = float(profile.felt_bpm or 0)
        if not source or abs(target / source - 1.0) > 0.24:
            return False
    if assessment.tempo_score < 48 or assessment.preparation_score < 48:
        return False
    if min(int(a.bars), int(b.bars)) < 32:
        return False
    # A section switch does not layer unrelated harmonies, but it still needs
    # reasonably compatible phrase grids so the handoff feels intentional.
    return assessment.structural_alignment_score >= 58 and assessment.entry_alignment_score >= 45


def _make_section_switch_plan(a: MidiProfile, b: MidiProfile, assessment: PairAssessment) -> GuidedPairPlan:
    base = _make_plan(a, b, assessment)
    a_bpm = float(a.felt_bpm or assessment.target_felt_bpm or 0)
    b_bpm = float(b.felt_bpm or assessment.target_felt_bpm or 0)
    return GuidedPairPlan(
        source_a=base.source_a,
        source_b=base.source_b,
        assessment=base.assessment,
        category="Creative section-switch",
        headline=base.headline,
        summary=(
            f"{a.title} and {b.title} take turns in complete musical sections. "
            "Their melodies and chords are not forced on top of each other."
        ),
        reasons=(
            *assessment.reference_evidence,
            f"Both songs use {a.meter} and have compatible phrase grids for clean handoffs.",
            f"Their felt tempos ({a_bpm:g} and {b_bpm:g} BPM) can meet near {base.target_bpm} BPM without a drastic speed change.",
            "This option preserves each song's own harmony and uses the style contrast as the creative feature.",
            "The pair is a controlled experiment, not a claim that every melody and chord will layer together.",
        ),
        transformations=(
            f"Align both songs near {base.target_bpm} felt BPM inside the generated Take only.",
            f"Begin near each song's main musical entry and trade {base.section_bars}-bar passages.",
            "Keep complete instrument sections together; do not overlay one song's melody on the other's chords.",
            "Leave the original MIDI files unchanged.",
        ),
        likely_heard=(
            "The first song plays a complete passage, then the second song answers with its own passage.",
            "The contrast can feel surprising while each song remains recognizable.",
            "There should be less harmonic collision than in a simultaneous blend.",
        ),
        recipe_keys=("section-exchange",),
        target_bpm=base.target_bpm,
        target_key_pc=base.target_key_pc,
        target_mode=base.target_mode,
        bars=base.bars,
        section_bars=base.section_bars,
        safety_score=max(50, min(69, base.safety_score)),
        start_bar_a=base.start_bar_a,
        start_bar_b=base.start_bar_b,
        window_harmony_score=base.window_harmony_score,
        window_melody_a_over_b_score=base.window_melody_a_over_b_score,
        window_melody_b_over_a_score=base.window_melody_b_over_a_score,
        window_structure_score=base.window_structure_score,
        lead_source=base.lead_source,
        backing_source=base.backing_source,
        contribution_plan=(
            f"{a.title}: complete sections with its own melody, harmony, bass, drums, and supporting parts.",
            f"{b.title}: complete sections with its own melody, harmony, bass, drums, and supporting parts.",
        ),
        signature_plan=base.signature_plan,
        signature_source=base.signature_source,
        signature_asset=base.signature_asset,
    )


# A documented human pairing is supporting discovery evidence, not proof; the MIDI analysis remains authoritative.
def _guided_reference_catalog(app_root: str | Path | None) -> ReferenceCatalog | None:
    if not app_root:
        return None
    path = Path(app_root) / "data" / "known_pairings.json"
    return ReferenceCatalog(
        path,
        import_directories=(AppPaths.for_app(app_root).ensure().reference_imports,),
    ) if path.is_file() else None


def _enrich_guided_reference(
    assessment: PairAssessment, catalog: ReferenceCatalog | None, a: MidiProfile, b: MidiProfile
) -> PairAssessment:
    """Attach reference explanations without changing MIDI-derived scores."""
    if catalog is None:
        return assessment
    evidence = catalog.evidence_for_pair(assessment.title_a, assessment.title_b, a.composer, b.composer)
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



def guided_plan_for_pair(
    a: MidiProfile, b: MidiProfile, *, app_root: str | Path | None = None
) -> GuidedPairPlan | None:
    """Validate one already-prepared relationship without searching the library.

    Library Engine uses the expensive broad discovery ahead of time. Beginner
    mode can therefore validate only the handful of persisted relationships it
    is about to show, keeping interaction effectively immediate.
    """
    if _same_song_family(a, b):
        return None
    catalog = _guided_reference_catalog(app_root)
    assessment = _enrich_guided_reference(assess_pair(a, b), catalog, a, b)
    if _pair_safe(a, b, assessment):
        return _make_plan(a, b, assessment)
    if _section_switch_candidate(a, b, assessment):
        return _make_section_switch_plan(a, b, assessment)
    return None

def guided_pair_recommendations(
    profiles: Sequence[MidiProfile],
    limit: int = 6,
    *,
    app_root: str | Path | None = None,
    cancel_event=None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    candidate_index: CandidateIndex | None = None,
    family_by_path: Mapping[str, str] | None = None,
) -> tuple[GuidedPairPlan, ...]:
    """Return a diverse beginner set while preserving strict duplicate protection.

    The first tier contains fully prevalidated pairings. When the library has
    fewer strict pairings than the requested limit, a second tier adds controlled
    complete-section experiments. Those experiments never depend on simultaneous
    melody-over-harmony compatibility.
    """
    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Guided recommendation search was cancelled.")

    def report(current: int, total: int, label: str) -> None:
        if progress_callback is not None:
            progress_callback(int(current), max(1, int(total)), str(label))

    check_cancelled()
    eligible = [profile for profile in profiles if guided_source_gate(profile).eligible]
    reference_catalog = _guided_reference_catalog(app_root)

    variant_words = ("karaoke", "version", "alternate", "alt", "copy", "demo", "remix", "midi")

    def representative_rank(profile: MidiProfile) -> tuple[int, int, int, int]:
        stem = profile.title.casefold() + " " + profile.path.casefold()
        variant_penalty = sum(1 for word in variant_words if word in stem)
        gate = guided_source_gate(profile)
        return (gate.confidence, int(profile.orchestration_readiness), int(profile.note_count), -variant_penalty)

    eligible.sort(key=representative_rank, reverse=True)
    family_lookup = family_by_path or {}
    if family_lookup:
        grouped: dict[str, list[MidiProfile]] = {}
        for profile in eligible:
            grouped.setdefault(family_lookup.get(profile.path) or f"path:{profile.path}", []).append(profile)
        unique_eligible = [max(group, key=representative_rank) for group in grouped.values()]
    else:
        unique_eligible = [max(group, key=representative_rank) for group in composition_groups(tuple(eligible))]
    unique_eligible.sort(key=representative_rank, reverse=True)

    # Small libraries still receive an exhaustive comparison. Large libraries
    # use the single catalogue-wide CandidateIndex that startup already built;
    # Guided never builds another 96k index merely because the user changed mode.
    pool = unique_eligible if len(unique_eligible) <= 120 else unique_eligible[:1200]
    by_path = {profile.path: profile for profile in pool}
    if len(pool) <= 120:
        candidate_pairs = {(a.path, b.path) for a, b in combinations(pool, 2)}
    elif candidate_index is not None:
        candidate_pairs: set[tuple[str, str]] = set()
        # The strongest 600 beginner-safe sources act as discovery seeds, while
        # their neighbors can come from the full deep-ready pool. Request extra
        # metadata neighbors because some 96k quick-only rows will be filtered.
        for anchor in pool[:600]:
            check_cancelled()
            for other_path in candidate_index.candidate_paths(anchor.path, limit=160):
                if other_path not in by_path or other_path == anchor.path:
                    continue
                candidate_pairs.add(tuple(sorted((anchor.path, other_path))))
                if len(candidate_pairs) >= 24000:
                    break
            if len(candidate_pairs) >= 24000:
                break
    else:
        candidate_pairs = indexed_candidate_pairs(pool[:600], app_root=app_root, per_anchor=40)
        # v0.26 lets documented/imported human knowledge reach into the entire
        # eligible library instead of being limited to the generic top-600 pool.
        if reference_catalog is not None and app_root:
            graph = None
            try:
                graph = DiscoveryGraph.for_app(app_root)
                graph.import_reference_catalog(reference_catalog)
                graph_pairs = graph.resolved_evidence_pairs(tuple(unique_eligible), limit=1024)
                all_by_path = {profile.path: profile for profile in unique_eligible}
                for pair in graph_pairs:
                    left_path, right_path = pair
                    left, right = all_by_path.get(left_path), all_by_path.get(right_path)
                    if left is None or right is None:
                        continue
                    by_path.setdefault(left_path, left)
                    by_path.setdefault(right_path, right)
                    candidate_pairs.add(tuple(sorted(pair)))
            except (OSError, sqlite3.Error, ValueError) as exc:
                _optional_discovery_error = exc
            finally:
                if graph is not None:
                    graph.close()

    strict_ranked: list[GuidedPairPlan] = []
    exploratory_ranked: list[GuidedPairPlan] = []
    ordered_candidate_pairs = tuple(candidate_pairs)
    total_pairs = max(1, len(ordered_candidate_pairs))
    for index, (a_path, b_path) in enumerate(ordered_candidate_pairs, 1):
        check_cancelled()
        if index == 1 or index == total_pairs or index % 16 == 0:
            report(index, total_pairs, "Checking Guided Remix candidate pairs…")
        a = by_path.get(a_path)
        b = by_path.get(b_path)
        if a is None or b is None or _same_song_family(a, b):
            continue
        assessment = _enrich_guided_reference(assess_pair(a, b), reference_catalog, a, b)
        if _pair_safe(a, b, assessment):
            strict_ranked.append(_make_plan(a, b, assessment))
        elif _section_switch_candidate(a, b, assessment):
            exploratory_ranked.append(_make_section_switch_plan(a, b, assessment))

    check_cancelled()
    strict_ranked.sort(
        key=lambda plan: (
            plan.safety_score,
            plan.assessment.preparation_score,
            max(plan.assessment.melody_a_over_b_score, plan.assessment.melody_b_over_a_score),
            plan.window_harmony_score,
            plan.assessment.reference_score,
            plan.assessment.familiarity_score,
        ),
        reverse=True,
    )
    exploratory_ranked.sort(
        key=lambda plan: (
            plan.assessment.structural_alignment_score,
            plan.assessment.tempo_score,
            plan.safety_score,
            plan.assessment.reference_score,
            plan.assessment.familiarity_score,
        ),
        reverse=True,
    )

    selected: list[GuidedPairPlan] = []
    used_pairs: set[frozenset[str]] = set()
    source_use: Counter[str] = Counter()
    used_categories: set[str] = set()

    def add_diverse(candidates: Sequence[GuidedPairPlan], *, allow_repeat_category: bool) -> None:
        for plan in candidates:
            if len(selected) >= max(1, limit):
                return
            pair = frozenset((plan.source_a.path, plan.source_b.path))
            if pair in used_pairs:
                continue
            # Keep one popular song from dominating every card. A song may still
            # appear twice when it is genuinely useful in different relationships.
            if source_use[plan.source_a.path] >= 2 or source_use[plan.source_b.path] >= 2:
                continue
            if not allow_repeat_category and plan.category in used_categories:
                continue
            selected.append(plan)
            used_pairs.add(pair)
            source_use[plan.source_a.path] += 1
            source_use[plan.source_b.path] += 1
            used_categories.add(plan.category)

    add_diverse(strict_ranked, allow_repeat_category=False)
    add_diverse(strict_ranked, allow_repeat_category=True)
    add_diverse(exploratory_ranked, allow_repeat_category=False)
    add_diverse(exploratory_ranked, allow_repeat_category=True)

    # If diversity limits leave open slots, fill them without ever reusing the
    # same pair or same composition family.
    for plan in (*strict_ranked, *exploratory_ranked):
        if len(selected) >= max(1, limit):
            break
        pair = frozenset((plan.source_a.path, plan.source_b.path))
        if pair in used_pairs:
            continue
        selected.append(plan)
        used_pairs.add(pair)

    check_cancelled()
    report(total_pairs, total_pairs, f"Guided search found {len(selected)} recommendation(s).")
    return tuple(selected)

def _exclusion_category(profile: MidiProfile, gate: GuidedSourceGate) -> str:
    text = (gate.summary + " " + " ".join(gate.restrictions)).casefold()
    if profile.note_count < 80 or profile.duration_seconds < 12 or profile.bars < 8:
        return "Too short or incomplete"
    if "tempo" in text or "pulse" in text:
        return "Tempo or meter needs review"
    if "merged" in text or "separate melody" in text or "voice extraction" in text:
        return "Melody and backing are not clearly separated"
    if "key" in text:
        return "Key analysis needs review"
    if "role" in text or "arrangement type" in text:
        return "Instrument roles need review"
    return "Manual preparation recommended"


def guided_library_diagnostics(profiles: Sequence[MidiProfile]) -> dict:
    gates = [(profile, guided_source_gate(profile)) for profile in profiles]
    families = composition_groups(tuple(profiles))
    ready_profiles = [profile for profile, gate in gates if gate.eligible]
    ready_families = composition_groups(tuple(ready_profiles))
    exclusions = Counter(_exclusion_category(profile, gate) for profile, gate in gates if not gate.eligible)
    return {
        "total_files": len(profiles),
        "distinct_songs": len(families),
        "duplicate_variants": max(0, len(profiles) - len(families)),
        "guided_ready_files": len(ready_profiles),
        "guided_ready_songs": len(ready_families),
        "hidden_files": sum(1 for _profile, gate in gates if not gate.eligible),
        "exclusions": dict(exclusions),
        "song_rows": tuple((profile, gate) for profile, gate in gates),
    }


def _near_match_blockers(a: MidiProfile, b: MidiProfile, assessment: PairAssessment) -> tuple[str, ...]:
    blockers: list[str] = []
    if _same_song_family(a, b):
        blockers.append("These files appear to be versions of the same song.")
    for profile in (a, b):
        gate = guided_source_gate(profile)
        if not gate.eligible:
            blockers.append(f"{profile.title}: {gate.summary}")
    if assessment.meter_score < 100:
        blockers.append("Their bar grids use different meters and need manual phrase decisions.")
    if assessment.tempo_relationship != "direct":
        blockers.append("The match depends on a half-time or double-time interpretation.")
    if assessment.preparation_score < 62:
        blockers.append("Automatic preparation is not yet dependable enough for a first mash.")
    if assessment.key_score < 50:
        blockers.append("The key relationship needs more harmonic preparation.")
    direction = max(assessment.melody_a_over_b_score, assessment.melody_b_over_a_score)
    if direction < 72:
        blockers.append("Neither detected melody is consistently clear over the other song's harmony.")
    if assessment.structural_alignment_score < 70:
        blockers.append("Their musical sections do not line up reliably at the detected entry points.")
    return tuple(dict.fromkeys(blockers)) or ("This pair is close, but it did not provide two dependable beginner arrangements.",)


def guided_near_matches(profiles: Sequence[MidiProfile], limit: int = 5) -> tuple[GuidedNearMatch, ...]:
    diagnostics = guided_library_diagnostics(profiles)
    representatives = distinct_representatives(
        tuple(profiles),
        rank=lambda profile: guided_source_gate(profile).confidence,
    )
    pool = sorted(representatives, key=lambda p: guided_source_gate(p).confidence, reverse=True)[:80]
    ranked: list[GuidedNearMatch] = []
    for a, b in combinations(pool, 2):
        if _same_song_family(a, b):
            continue
        assessment = assess_pair(a, b)
        blockers = _near_match_blockers(a, b, assessment)
        direction = max(assessment.melody_a_over_b_score, assessment.melody_b_over_a_score)
        score = int(round(assessment.preparation_score * .30 + assessment.harmonic_score * .20 + direction * .25 + assessment.structural_alignment_score * .15 + assessment.tempo_score * .10))
        label = "Close, but needs review" if score >= 60 else "Experimental outside Guided Remix"
        ranked.append(GuidedNearMatch(a, b, assessment, label, blockers, max(0, min(100, score))))
    ranked.sort(key=lambda item: item.promise_score, reverse=True)
    return tuple(ranked[:max(0, limit)])


def guided_library_summary(profiles: Sequence[MidiProfile]) -> dict[str, int]:
    diagnostics = guided_library_diagnostics(profiles)
    gates = [guided_source_gate(profile) for profile in profiles]
    return {
        "total": diagnostics["total_files"],
        "distinct_songs": diagnostics["distinct_songs"],
        "duplicate_variants": diagnostics["duplicate_variants"],
        "guided_ready": diagnostics["guided_ready_files"],
        "guided_ready_songs": diagnostics["guided_ready_songs"],
        "restricted": sum(1 for gate in gates if gate.eligible and "keyboard" in gate.level.casefold()),
        "hidden": diagnostics["hidden_files"],
    }
