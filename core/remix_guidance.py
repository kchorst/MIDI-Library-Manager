from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from core.multi_source import MULTI_SOURCE_RECIPES
from core.remix_engine import TAKE_STRATEGIES
from core.remix_profile import MidiProfile, PairAssessment, assess_pair, key_name


GUIDANCE_MODES: dict[str, tuple[str, str]] = {
    "guided": (
        "Guided Remix",
        "MIDI Remixer completes most musical and technical decisions, explains them, and creates a small set of contrasting Trials and Takes.",
    ),
    "assisted": (
        "Assisted Studio",
        "MIDI Remixer pre-fills its recommendations while you choose the major musical direction and may override any decision.",
    ),
    "workbench": (
        "Creative Workbench",
        "Every detailed source, structure, role, transformation, arrangement, instrumentation, and mixer control remains available.",
    ),
}


@dataclass(frozen=True)
class SourceReadiness:
    status: str
    summary: str
    suitable_uses: tuple[str, ...]
    preparation: tuple[str, ...]
    confidence: int


@dataclass(frozen=True)
class RecipeAdvice:
    key: str
    label: str
    eligible: bool
    recommendation: str
    reason: str
    expected_result: str


@dataclass(frozen=True)
class EngineDecision:
    title: str
    chosen: tuple[str, ...]
    why: tuple[str, ...]
    uncertainty: tuple[str, ...] = ()
    audible_result: tuple[str, ...] = ()

    def as_text(self) -> str:
        lines = [self.title]
        if self.chosen:
            lines.append("\nWhat MIDI Remixer chose")
            lines.extend(f"• {item}" for item in self.chosen)
        if self.why:
            lines.append("\nWhy")
            lines.extend(f"• {item}" for item in self.why)
        if self.audible_result:
            lines.append("\nWhat you may hear")
            lines.extend(f"• {item}" for item in self.audible_result)
        if self.uncertainty:
            lines.append("\nUncertainty / review")
            lines.extend(f"• {item}" for item in self.uncertainty)
        return "\n".join(lines)


def source_readiness(profile: MidiProfile) -> SourceReadiness:
    has_melody = profile.melody_track_index is not None
    has_foundation = profile.accompaniment_track_index is not None
    distinct_roles = has_melody and has_foundation and profile.melody_track_index != profile.accompaniment_track_index
    polyphonic_reduction = profile.arrangement_type in {"piano reduction", "keyboard reduction", "single-track performance"}
    confidence = int(round((profile.orchestration_readiness + profile.melody_extraction_confidence) / 2.0))

    if distinct_roles and profile.orchestration_readiness >= 62:
        return SourceReadiness(
            "Remix-ready",
            "Distinct melody/foundation material is available and most arrangement recipes can be attempted directly.",
            ("melody exchange", "foundation exchange", "alternating sections", "conversation", "layering"),
            (),
            max(60, confidence),
        )
    if distinct_roles:
        prep = []
        if polyphonic_reduction:
            prep.append("Use top-voice extraction before assigning the upper track to a solo instrument.")
        if profile.orchestration_readiness < 50:
            prep.append("Review track roles and instrumentation after generation.")
        return SourceReadiness(
            "Automatically adaptable",
            "The file can be remixed after automatic voice extraction, register handling, or dynamics preparation.",
            ("melody exchange", "alternating sections", "transformation", "whole-section use"),
            tuple(prep) or ("Automatic role preparation is recommended.",),
            max(35, confidence),
        )
    if profile.note_count > 0:
        return SourceReadiness(
            "Whole-section source",
            "The engine cannot confidently separate melody and backing, but the complete MIDI remains useful as a section, transition, intro, ending, or texture.",
            ("alternating sections", "source journey", "transition", "intro/ending", "whole-texture transformation"),
            (
                "Direct melody-over-backing recipes are disabled until tracks or voices are assigned.",
                "Try highest-voice melody extraction, lowest-voice foundation extraction, or manual track mapping.",
            ),
            max(20, confidence),
        )
    return SourceReadiness(
        "Manual preparation recommended",
        "No usable pitched-note material was identified confidently.",
        (),
        ("Inspect the MIDI file, choose usable tracks manually, or use another source.",),
        0,
    )


def _pair_recipe_advice(assessment: PairAssessment, profiles: Sequence[MidiProfile]) -> list[RecipeAdvice]:
    a_ready = source_readiness(profiles[0])
    b_ready = source_readiness(profiles[1])
    roles_available = all(
        profile.melody_track_index is not None
        and profile.accompaniment_track_index is not None
        and profile.melody_track_index != profile.accompaniment_track_index
        for profile in profiles
    )
    lookup = {item.key: item for item in TAKE_STRATEGIES}
    advice: list[RecipeAdvice] = []

    def add(key: str, eligible: bool, recommendation: str, reason: str, expected: str) -> None:
        item = lookup[key]
        advice.append(RecipeAdvice(key, item.label, eligible, recommendation, reason, expected))

    add(
        "melody-a-accompaniment-b",
        roles_available,
        "Recommended" if assessment.melody_a_over_b_score >= 68 else "Workable with correction" if assessment.melody_a_over_b_score >= 48 else "Use cautiously",
        (
            f"A melody over B harmony tests at {assessment.melody_a_over_b_score}%. "
            + ("Both source roles are separable." if roles_available else "At least one source needs voice or track preparation first.")
        ),
        f"Source A remains recognizable as the lead while Source B supplies the harmonic foundation; B is shifted {assessment.key_shift_b_to_a:+d} semitone(s) toward A's opening tonic.",
    )
    add(
        "melody-b-accompaniment-a",
        roles_available,
        "Recommended" if assessment.melody_b_over_a_score >= 68 else "Workable with correction" if assessment.melody_b_over_a_score >= 48 else "Use cautiously",
        f"B melody over A harmony tests at {assessment.melody_b_over_a_score}%. " + ("Both source roles are separable." if roles_available else "Voice/track preparation is required."),
        "Source B becomes the lead over Source A's foundation; the audible result may differ substantially from the opposite direction.",
    )
    add(
        "section-exchange",
        True,
        "Strong fallback" if not roles_available else "Recommended",
        "Complete sections preserve each source's internal harmony and work even when role separation is weak.",
        "The listener hears the sources trade complete passages instead of being forced into continuous simultaneous harmony.",
    )
    add(
        "conversation",
        roles_available,
        "Recommended" if min(assessment.melody_a_over_b_score, assessment.melody_b_over_a_score) >= 60 else "Experimental",
        "Call-and-response needs two identifiable melodic voices and benefits from both directional harmony checks.",
        "Recognizable phrases answer one another over a changing foundation.",
    )
    add(
        "transformation",
        True,
        "Recommended",
        "A gradual handoff needs less constant harmonic agreement and makes the relationship understandable to the listener.",
        "The Take begins near one source, becomes hybrid, and resolves toward the other.",
    )
    add(
        "layered-blend",
        roles_available and assessment.harmonic_score >= 48,
        "Recommended" if assessment.harmonic_score >= 68 else "High-density experiment",
        f"Simultaneous layering uses harmonic similarity {assessment.harmonic_score}% and requires separable voices.",
        "Both melodies may be audible together; register and dynamics balancing are critical.",
    )
    add(
        "foundation-swap",
        roles_available,
        "Workable" if roles_available else "Unavailable until roles are prepared",
        "The midpoint swap deliberately exposes both melody/harmony directions.",
        "The musical hierarchy reverses halfway through the Take.",
    )
    return advice


def recipe_advice(profiles: Sequence[MidiProfile]) -> list[RecipeAdvice]:
    profiles = tuple(profiles)
    if len(profiles) < 2:
        return []
    if len(profiles) == 2:
        return _pair_recipe_advice(assess_pair(profiles[0], profiles[1]), profiles)

    readies = [source_readiness(profile) for profile in profiles]
    distinct_roles = sum(1 for profile in profiles if profile.melody_track_index is not None and profile.accompaniment_track_index is not None and profile.melody_track_index != profile.accompaniment_track_index)
    lookup = {item.key: item for item in MULTI_SOURCE_RECIPES}
    advice: list[RecipeAdvice] = []
    for item in MULTI_SOURCE_RECIPES:
        eligible = len(profiles) >= item.minimum_sources
        recommendation = "Recommended"
        reason = f"Uses {len(profiles)} sources; requires at least {item.minimum_sources}."
        if item.key in {"role-collage", "anchor-transformations", "melody-relay"} and distinct_roles < 2:
            eligible = False
            recommendation = "Needs role preparation"
            reason = "Too few sources have confidently separable melody and foundation roles."
        elif item.key in {"rotating-sections", "source-journey"}:
            recommendation = "Strong fallback"
            reason = "Whole sections preserve source identity and accept sources with weak role separation."
        elif item.key == "finale-stack":
            recommendation = "Experimental"
            reason = "Several sources overlap near the finale, so harmonic, register, and dynamics review is required."
        advice.append(RecipeAdvice(item.key, item.label, eligible, recommendation, reason, item.description))
    return advice


def recommend_frame(profiles: Sequence[MidiProfile], *, alternative: int = 0) -> dict[str, object]:
    profiles = tuple(profiles)
    if not profiles:
        return {"target_bpm": 110, "target_key_pc": None, "target_mode": "major", "bars": 64, "section_bars": 8}
    bpms = sorted(float(profile.felt_bpm or 110.0) for profile in profiles)
    median = bpms[len(bpms) // 2] if len(bpms) % 2 else (bpms[len(bpms)//2 - 1] + bpms[len(bpms)//2]) / 2.0
    if alternative == 1:
        target_bpm = int(round(float(profiles[0].felt_bpm or median)))
    elif alternative == 2:
        target_bpm = int(round(min(bpms)))
    else:
        target_bpm = int(round(median))
    tonic = profiles[0].tonic_pc
    mode = profiles[0].mode if profiles[0].mode in {"major", "minor"} else "major"
    lengths = [max(4, int(profile.bars)) for profile in profiles]
    bars = min(lengths) if alternative != 1 else lengths[0]
    bars = max(8, min(256, bars))
    section = 8 if bars >= 32 else 4
    return {
        "target_bpm": max(30, min(300, target_bpm)),
        "target_key_pc": tonic,
        "target_mode": mode,
        "bars": bars,
        "section_bars": section,
    }


def frame_decision(profiles: Sequence[MidiProfile], frame: dict[str, object]) -> EngineDecision:
    profiles = tuple(profiles)
    if not profiles:
        return EngineDecision("MIDI Remixer's decision", (), ("Choose at least two sources before a shared musical frame can be recommended.",))
    tonic = frame.get("target_key_pc")
    mode = str(frame.get("target_mode") or profiles[0].mode)
    key = key_name(int(tonic) if isinstance(tonic, int) else profiles[0].tonic_pc, mode)
    target_bpm = int(frame.get("target_bpm") or 110)
    bars = int(frame.get("bars") or 64)
    chosen = (
        f"Shared target: {key} at {target_bpm} felt BPM.",
        f"Working form: {bars} bars in {int(frame.get('section_bars') or 8)}-bar blocks.",
        "Automatic dynamics normalization is enabled before the final mixer.",
    )
    why = (
        "The target pulse is near the median felt pulse, reducing extreme tempo movement across the source set.",
        "The current discovery focus supplies the initial target tonic; every pitched source is transposed non-destructively in generated Takes.",
        "The shortest usable shared form avoids inventing excessive repetition in the first Trial.",
    )
    audible = (
        "Raw sources remain unchanged; normalized previews and generated Takes should share the displayed key and pulse.",
        "Dense accompaniment parts are reduced so they do not cover the lead melody.",
    )
    uncertainty = tuple(
        f"{profile.title}: declared {profile.key_display}, analyzed {profile.analyzed_key_display} ({profile.key_confidence}% confidence)."
        for profile in profiles if profile.key_confidence < 65 or profile.key_agreement == "differs"
    )
    return EngineDecision("MIDI Remixer's decision", chosen, why, uncertainty, audible)


def conflict_decision(profiles: Sequence[MidiProfile]) -> EngineDecision:
    profiles = tuple(profiles)
    if len(profiles) < 2:
        return EngineDecision("MIDI Remixer's decision", (), ("Select at least two sources to evaluate conflicts.",))
    assessments = [assess_pair(profiles[0], profile) for profile in profiles[1:]]
    weak = [item for item in assessments if min(item.melody_a_over_b_score, item.melody_b_over_a_score) < 48]
    chosen = (
        "Correct severe melody/harmony collisions only.",
        "Preserve usable tensions and source character.",
        "Normalize dynamics before generation, then leave final balance to the mixer.",
    )
    why = (
        "Removing every non-chord tone would flatten syncopation, passing tones, suspensions, and expressive dissonance.",
        "Recipe-specific eligibility prevents a weakly separated MIDI from aborting the entire generation batch.",
    )
    uncertainty = tuple(
        f"{item.title_a} / {item.title_b}: one direction tests below 48%; simultaneous melody exchange may require manual review."
        for item in weak
    )
    return EngineDecision("MIDI Remixer's decision", chosen, why, uncertainty, ("Section exchange and transformation remain available when direct layering is weak.",))


def guided_recipe_keys(profiles: Sequence[MidiProfile], limit: int = 3) -> tuple[str, ...]:
    advice = recipe_advice(profiles)
    priority = {"Recommended": 0, "Strong fallback": 1, "Workable": 2, "Workable with correction": 2, "Experimental": 4, "High-density experiment": 5}
    eligible = [item for item in advice if item.eligible]
    eligible.sort(key=lambda item: (priority.get(item.recommendation, 3), item.label.casefold()))
    # Keep the first Trial contrasting rather than selecting three near-identical melody swaps.
    chosen: list[str] = []
    families: set[str] = set()
    for item in eligible:
        family = "section" if item.key in {"section-exchange", "rotating-sections", "source-journey"} else "transform" if "transform" in item.key else "layer" if item.key in {"layered-blend", "finale-stack"} else "role"
        if family in families and len(eligible) > limit:
            continue
        chosen.append(item.key)
        families.add(family)
        if len(chosen) >= max(1, limit):
            break
    return tuple(chosen)


def compatibility_explanation(assessment: PairAssessment) -> str:
    if assessment.adaptation_required == "light":
        headline = "Most technical preparation is straightforward."
    elif assessment.adaptation_required == "moderate":
        headline = "The pair is transformable, but the engine will make audible adjustments."
    else:
        headline = "This is an exploratory pairing; choose recipes that preserve sections or allow more interpretation."
    return "\n".join((
        headline,
        f"Key: Source B moves {assessment.key_shift_b_to_a:+d} semitone(s) toward Source A's opening tonic. Generated Takes display the applied target key.",
        f"Tempo: shared target {assessment.target_felt_bpm} felt BPM using a {assessment.tempo_relationship} relationship.",
        f"Harmony: approximately {assessment.harmonic_score}% functional overlap after normalization.",
        f"Direction matters: A melody over B harmony {assessment.melody_a_over_b_score}%; B melody over A harmony {assessment.melody_b_over_a_score}%.",
        f"Preparation label: {assessment.adaptation_required}; this describes editing effort, not artistic quality.",
    ))
