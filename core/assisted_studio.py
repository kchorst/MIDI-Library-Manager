from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
import json
import sqlite3
from typing import Callable, Mapping, Sequence

from core.candidate_index import CandidateIndex
from core.discovery_graph import DiscoveryEvidence, DiscoveryGraph
from core.app_paths import AppPaths
from core.reference_catalog import ReferenceCatalog
from core.multi_source import MULTI_SOURCE_RECIPES, recommended_multi_source_recipes
from core.remix_guidance import recipe_advice, recommend_frame, source_readiness
from core.remix_profile import MidiProfile, PairAssessment, assess_pair, key_name, primary_signature_asset, signed_pitch_class_shift
from core.song_identity import (
    arrangement_distance, composition_groups, materially_different_arrangement,
    probable_same_composition, same_composition,
)


@dataclass(frozen=True)
class AssistedSourceGate:
    eligible: bool
    level: str
    summary: str
    likely_uses: tuple[str, ...]
    decisions_needed: tuple[str, ...]
    confidence: int


@dataclass(frozen=True)
class AssistedSetPlan:
    profiles: tuple[MidiProfile, ...]
    headline: str
    category: str
    summary: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    source_roles: tuple[str, ...]
    recipe_keys: tuple[str, ...]
    target_bpm: int
    target_key_pc: int | None
    target_mode: str
    bars: int
    section_bars: int
    confidence: int
    pair_assessments: tuple[PairAssessment, ...]
    contribution_plan: tuple[str, ...] = ()
    signature_plan: tuple[str, ...] = ()
    origin: str = "assisted"
    discovery_score: int = 0
    discovery_feedback_score: int = 0
    discovery_labels: tuple[str, ...] = ()
    discovery_providers: tuple[str, ...] = ()

    @property
    def source_count(self) -> int:
        return len(self.profiles)

    @property
    def source_titles(self) -> tuple[str, ...]:
        return tuple(profile.title for profile in self.profiles)


@dataclass(frozen=True)
class AssistedDiscoveryResult:
    """Bounded recommendation result for one optional discovery focus."""

    anchor: MidiProfile | None
    standard: tuple[AssistedSetPlan, ...]
    variants: tuple[AssistedSetPlan, ...]
    evaluated_candidates: int = 0

    @property
    def plans(self) -> tuple[AssistedSetPlan, ...]:
        return (*self.standard, *self.variants)


_SCORE_LABELS = (
    (78, "Strong"),
    (62, "Good"),
    (46, "Workable"),
    (0, "Exploratory"),
)


def assisted_score_label(value: int) -> str:
    value = max(0, min(100, int(value)))
    for threshold, label in _SCORE_LABELS:
        if value >= threshold:
            return label
    return "Exploratory"


ASSISTED_GROUP_ADAPTABLE = "adaptable"
ASSISTED_GROUP_SECTION = "section"
ASSISTED_GROUP_MULTI = "multi"
ASSISTED_GROUP_BROADER = "broader"
ASSISTED_GROUP_GUIDED = "guided"
ASSISTED_GROUP_VARIANT = "variant"


def assisted_plan_group(plan: AssistedSetPlan) -> str:
    """Classify why a recommendation belongs in Assisted Studio.

    This is intentionally based on musical preparation and arrangement scope,
    not merely score order, so the UI can show what Assisted adds beyond Guided.
    """
    if plan.origin == "variant":
        return ASSISTED_GROUP_VARIANT
    if plan.origin == "guided":
        return ASSISTED_GROUP_GUIDED
    if plan.source_count >= 3:
        return ASSISTED_GROUP_MULTI
    gates = tuple(assisted_source_gate(profile) for profile in plan.profiles)
    if any(gate.level != "Studio-ready" for gate in gates):
        return ASSISTED_GROUP_ADAPTABLE
    section_recipes = {
        "section-exchange", "transformation", "rotating-sections",
        "source-journey", "anchor-transformations", "progressive-build",
    }
    direct_recipes = {
        "melody-a-accompaniment-b", "melody-b-accompaniment-a",
        "layered-blend", "foundation-swap",
    }
    recipe_set = set(plan.recipe_keys)
    if recipe_set & section_recipes and not recipe_set & direct_recipes:
        return ASSISTED_GROUP_SECTION
    if recipe_set & section_recipes and plan.warnings:
        return ASSISTED_GROUP_SECTION
    return ASSISTED_GROUP_BROADER


def assisted_source_gate(profile: MidiProfile) -> AssistedSourceGate:
    if profile.note_count <= 0:
        return AssistedSourceGate(
            False,
            "Unavailable",
            "No pitched note material was detected.",
            (),
            ("Inspect or repair the MIDI before using it.",),
            0,
        )
    if profile.note_count < 24 or profile.duration_seconds < 5 or profile.bars < 4:
        return AssistedSourceGate(
            False,
            "Fragment only",
            "The MIDI is too short for a main source set, although it may later work as a hook or fill.",
            ("hook", "fill", "transition"),
            ("Use Creative Workbench to place the fragment manually.",),
            max(5, min(35, profile.melody_extraction_confidence)),
        )

    ready = source_readiness(profile)
    distinct_roles = (
        profile.melody_track_index is not None
        and profile.accompaniment_track_index is not None
        and profile.melody_track_index != profile.accompaniment_track_index
    )
    has_roles = profile.melody_track_index is not None and profile.accompaniment_track_index is not None
    confidence = int(round(
        profile.key_confidence * 0.30
        + profile.orchestration_readiness * 0.35
        + profile.melody_extraction_confidence * 0.35
    ))

    if distinct_roles:
        decisions: list[str] = []
        if profile.key_agreement == "differs":
            decisions.append("Review the declared and analyzed key before detailed layering.")
        if profile.tempo_change_count > 20:
            decisions.append("Choose whether to preserve or simplify the tempo map.")
        return AssistedSourceGate(
            True,
            "Studio-ready",
            "Separate melody and foundation material is available; the engine can recommend direct role exchanges and section-based ideas.",
            ("lead melody", "foundation", "section exchange", "transformation"),
            tuple(decisions),
            max(50, min(98, confidence)),
        )

    if has_roles or profile.arrangement_type in {"piano reduction", "keyboard reduction", "single-track performance"}:
        return AssistedSourceGate(
            True,
            "Adaptable",
            "The source can participate after automatic top-voice, bottom-voice, register, or whole-section preparation.",
            ("top-voice melody", "whole-section source", "transition", "source journey"),
            (
                "Direct melody/foundation recipes may be limited until the suggested extraction is accepted.",
                "Instrumentation changes should respect polyphony and practical range.",
            ),
            max(32, min(80, confidence)),
        )

    return AssistedSourceGate(
        True,
        "Whole-section source",
        "The engine cannot confidently separate roles, but the complete arrangement can still contribute sections, texture, transitions, or endings.",
        ("rotating sections", "source journey", "intro", "ending", "texture"),
        ("Use a whole-section recipe or open More Control for manual track mapping.",),
        max(20, min(64, confidence)),
    )


def _pair_eligible_recipes(a: MidiProfile, b: MidiProfile) -> tuple[str, ...]:
    advice = recipe_advice((a, b))
    priority = (
        "melody-a-accompaniment-b",
        "melody-b-accompaniment-a",
        "section-exchange",
        "transformation",
        "conversation",
        "layered-blend",
        "foundation-swap",
    )
    by_key = {item.key: item for item in advice}
    selected = [key for key in priority if key in by_key and by_key[key].eligible]
    # Assisted Studio accepts at least one dependable whole-section path, then
    # exposes other eligible approaches for the DJ to choose.
    if "section-exchange" in selected:
        selected.remove("section-exchange")
        selected.insert(0, "section-exchange")
    if "transformation" in selected:
        selected.remove("transformation")
        selected.insert(1 if selected else 0, "transformation")
    return tuple(selected[:5])


def _pair_plan(
    a: MidiProfile,
    b: MidiProfile,
    *,
    allow_same_composition: bool = False,
    discovery_evidence: DiscoveryEvidence | None = None,
    assessment: PairAssessment | None = None,
) -> AssistedSetPlan | None:
    if probable_same_composition(a, b) and not allow_same_composition:
        return None
    gate_a = assisted_source_gate(a)
    gate_b = assisted_source_gate(b)
    if not gate_a.eligible or not gate_b.eligible:
        return None
    assessment = assessment or assess_pair(a, b)
    recipes = _pair_eligible_recipes(a, b)
    if len(recipes) < 2:
        return None
    # Broader than Guided Remix, but still avoids combinations where every
    # measurable relationship is weak.
    if assessment.preparation_score < 36 and assessment.meter_score < 55 and assessment.harmonic_score < 38:
        return None
    frame = recommend_frame((a, b))
    target_pc = frame.get("target_key_pc")
    target_mode = str(frame.get("target_mode") or "major")
    target_key = key_name(int(target_pc) if isinstance(target_pc, int) else a.tonic_pc, target_mode)
    direction_a = assessment.melody_a_over_b_score
    direction_b = assessment.melody_b_over_a_score
    if direction_a >= direction_b:
        roles = ("lead melody", "foundation")
        direction = f"{a.title}'s melody is the stronger starting direction over {b.title}'s foundation ({direction_a}% versus {direction_b}%)."
    else:
        roles = ("foundation", "lead melody")
        direction = f"{b.title}'s melody is the stronger starting direction over {a.title}'s foundation ({direction_b}% versus {direction_a}%)."
    lead_profile = a if roles[0] == "lead melody" else b
    backing_profile = b if lead_profile is a else a
    contribution_plan = (
        f"{lead_profile.title}: lead melody or top voice.",
        f"{backing_profile.title}: complete backing without its detected lead melody (drums, bass, chords, riffs, fills, and support).",
    )
    signature_asset = primary_signature_asset(backing_profile)
    if signature_asset and signature_asset.get("protect_default"):
        signature_plan = (
            f"Protect {backing_profile.title}'s {signature_asset.get('label', 'opening passage').casefold()} in bars {int(signature_asset.get('start_bar', 0)) + 1}-{int(signature_asset.get('end_bar', 0))}.",
            "Default placement: play it alone, then bring in the companion melody.",
            "Assisted Studio may instead repeat it as a transition or disable it before generation.",
        )
    else:
        signature_plan = ("No high-confidence protected opening was detected in the proposed backing source.",)

    readiness = min(gate_a.confidence, gate_b.confidence)
    score = int(round(
        assessment.preparation_score * 0.20
        + assessment.harmonic_score * 0.16
        + max(direction_a, direction_b) * 0.18
        + assessment.tempo_score * 0.12
        + assessment.meter_score * 0.10
        + readiness * 0.18
        + min(100, len(recipes) * 18) * 0.06
    ))
    same_style = a.style == b.style and a.style != "Unclassified"
    if same_style:
        category = "Same-style recombination"
    elif assessment.harmonic_score >= 52:
        category = "Cross-style harmonic bridge"
    elif assessment.meter_score >= 70:
        category = "Cross-style structural bridge"
    else:
        category = "Interpretive contrast"
    warnings: list[str] = []
    warnings.extend(gate_a.decisions_needed)
    warnings.extend(gate_b.decisions_needed)
    if assessment.harmonic_score < 48:
        warnings.append("Use section exchange or transformation before attempting dense simultaneous layering.")
    if abs(assessment.key_shift_b_to_a) >= 5:
        warnings.append("The proposed transposition is substantial; audition the normalized sources before generation.")
    discovery_evidence = discovery_evidence or DiscoveryEvidence()
    discovery_reasons = tuple(discovery_evidence.reasons)
    reasons = (
        f"Source readiness: {a.title} is {gate_a.level} ({gate_a.confidence}%); {b.title} is {gate_b.level} ({gate_b.confidence}%).",
        *discovery_reasons,
        f"Key plan: {a.key_display} and {b.key_display} can meet at {target_key}; Source B moves {assessment.key_shift_b_to_a:+d} semitone(s) toward Source A.",
        f"Tempo plan: {float(a.felt_bpm or assessment.target_felt_bpm):g} and {float(b.felt_bpm or assessment.target_felt_bpm):g} felt BPM meet near {int(frame['target_bpm'])} BPM. Tempo relationship is {assisted_score_label(assessment.tempo_score)} ({assessment.tempo_score}%).",
        f"Meter/phrase relationship is {assisted_score_label(assessment.meter_score)} ({assessment.meter_score}%): {a.meter_display} and {b.meter_display}.",
        f"{direction}",
        f"Contribution plan: {lead_profile.title} supplies the lead; {backing_profile.title} supplies its fuller backing with its detected lead removed.",
        *(signature_plan[:2]),
        f"Harmony is {assisted_score_label(assessment.harmonic_score)} ({assessment.harmonic_score}%). The engine recommends recipe-specific use rather than treating this as an artistic verdict.",
        f"Eligible starting arrangements: {', '.join(key.replace('-', ' ') for key in recipes)}.",
    )
    summary = (
        f"{category}. The engine found enough timing, key, phrase, and source-role information to prepare this pair automatically, "
        f"while leaving the major artistic choices to the DJ."
    )
    return AssistedSetPlan(
        profiles=(a, b),
        headline=f"{a.title} + {b.title}",
        category=category,
        summary=summary,
        reasons=reasons,
        warnings=tuple(dict.fromkeys(warnings)),
        source_roles=roles,
        recipe_keys=recipes,
        target_bpm=int(frame["target_bpm"]),
        target_key_pc=int(target_pc) if isinstance(target_pc, int) else None,
        target_mode=target_mode,
        bars=max(32, min(128, int(frame["bars"]))),
        section_bars=int(frame["section_bars"]),
        confidence=max(0, min(100, score)),
        pair_assessments=(assessment,),
        contribution_plan=contribution_plan,
        signature_plan=signature_plan,
        discovery_score=max(0, min(100, int(discovery_evidence.score))),
        discovery_feedback_score=max(-100, min(100, int(discovery_evidence.feedback_score))),
        discovery_labels=tuple(discovery_evidence.labels),
        discovery_providers=tuple(discovery_evidence.providers),
    )


def assisted_plan_for_pair(
    a: MidiProfile,
    b: MidiProfile,
    *,
    allow_same_composition: bool = False,
    discovery_evidence: DiscoveryEvidence | None = None,
    assessment: PairAssessment | None = None,
) -> AssistedSetPlan | None:
    """Public bounded pair validator used by the Library Engine and Remixer.

    It deliberately evaluates one already-selected pair; it never performs a
    library search. This lets expensive deterministic pair work be persisted by
    the Library Engine and reopened instantly by interactive workflows.
    """
    return _pair_plan(
        a, b, allow_same_composition=allow_same_composition, discovery_evidence=discovery_evidence,
        assessment=assessment,
    )


def _set_recipe_keys(profiles: Sequence[MidiProfile]) -> tuple[str, ...]:
    advice = [item for item in recipe_advice(profiles) if item.eligible]
    preferred = (
        "rotating-sections",
        "source-journey",
        "melody-relay",
        "anchor-transformations",
        "progressive-build",
        "role-collage",
        "finale-stack",
    )
    available = {item.key for item in advice}
    return tuple(key for key in preferred if key in available)[:5]


def _role_assignment(profiles: Sequence[MidiProfile]) -> tuple[str, ...]:
    if not profiles:
        return ()
    lead_index = max(
        range(len(profiles)),
        key=lambda i: (
            assisted_source_gate(profiles[i]).confidence,
            profiles[i].melody_extraction_confidence,
            profiles[i].orchestration_readiness,
        ),
    )
    foundation_candidates = [i for i in range(len(profiles)) if i != lead_index]
    foundation_index = max(
        foundation_candidates,
        key=lambda i: (
            profiles[i].orchestration_readiness,
            profiles[i].note_count,
        ),
    ) if foundation_candidates else lead_index
    roles: list[str] = []
    for index, profile in enumerate(profiles):
        if index == lead_index:
            roles.append("lead melody")
        elif index == foundation_index:
            roles.append("foundation")
        elif assisted_source_gate(profile).level == "Whole-section source":
            roles.append("section / texture")
        else:
            roles.append("supporting section")
    return tuple(roles)


def _multi_plan(profiles: Sequence[MidiProfile]) -> AssistedSetPlan | None:
    profiles = tuple(profiles)
    if len(profiles) < 3:
        return None
    if len(composition_groups(profiles)) != len(profiles):
        return None
    gates = tuple(assisted_source_gate(profile) for profile in profiles)
    if not all(gate.eligible for gate in gates):
        return None
    recipes = _set_recipe_keys(profiles)
    if len(recipes) < 2:
        return None
    assessments = tuple(assess_pair(profiles[0], profile) for profile in profiles[1:])
    frame = recommend_frame(profiles)
    target_pc = frame.get("target_key_pc")
    target_mode = str(frame.get("target_mode") or "major")
    target_key = key_name(int(target_pc) if isinstance(target_pc, int) else profiles[0].tonic_pc, target_mode)
    avg_prep = sum(item.preparation_score for item in assessments) / len(assessments)
    avg_harmony = sum(item.harmonic_score for item in assessments) / len(assessments)
    avg_tempo = sum(item.tempo_score for item in assessments) / len(assessments)
    avg_meter = sum(item.meter_score for item in assessments) / len(assessments)
    readiness = sum(gate.confidence for gate in gates) / len(gates)
    score = int(round(avg_prep * 0.22 + avg_harmony * 0.14 + avg_tempo * 0.12 + avg_meter * 0.10 + readiness * 0.25 + min(100, len(recipes) * 18) * 0.17))
    roles = _role_assignment(profiles)
    role_text = "; ".join(f"{profile.title}: {role}" for profile, role in zip(profiles, roles))
    styles = {profile.style for profile in profiles if profile.style != "Unclassified"}
    category = "Multi-source role collage" if "role-collage" in recipes else "Multi-source section journey"
    if len(styles) >= 2:
        category = "Cross-style " + category.casefold()
    warnings: list[str] = []
    for profile, gate in zip(profiles, gates):
        if gate.decisions_needed:
            warnings.append(f"{profile.title}: {gate.decisions_needed[0]}")
    if avg_harmony < 48:
        warnings.append("The engine favors sequential or rotating-section recipes over constant layering for this set.")
    reasons = (
        f"The set contains {len(profiles)} usable sources: " + ", ".join(profile.title for profile in profiles) + ".",
        f"Suggested roles: {role_text}.",
        f"Shared frame: {target_key} at {int(frame['target_bpm'])} felt BPM in {int(frame['section_bars'])}-bar blocks.",
        f"Average focus-source preparation is {assisted_score_label(int(avg_prep))} ({avg_prep:.0f}%); harmony {assisted_score_label(int(avg_harmony))} ({avg_harmony:.0f}%).",
        f"Average tempo relationship is {assisted_score_label(int(avg_tempo))} ({avg_tempo:.0f}%); meter/phrase alignment {assisted_score_label(int(avg_meter))} ({avg_meter:.0f}%).",
        f"Eligible starting arrangements: {', '.join(key.replace('-', ' ') for key in recipes)}.",
        "The extra sources are not all forced to play simultaneously; they can enter by role, section, or progression through the arrangement.",
    )
    summary = (
        f"{category}. This set gives Assisted Studio several ways to build beyond a two-song mash while keeping the user in control of the broad arrangement."
    )
    contribution_plan = tuple(f"{profile.title}: {role}." for profile, role in zip(profiles, roles))
    signature_lines: list[str] = []
    for profile in profiles:
        asset = primary_signature_asset(profile)
        if asset and asset.get("protect_default"):
            signature_lines.append(
                f"{profile.title}: protect {asset.get('label', 'opening passage').casefold()} in bars {int(asset.get('start_bar', 0)) + 1}-{int(asset.get('end_bar', 0))}; place it alone or as a transition rather than forcing all sources over it."
            )
    signature_plan = tuple(signature_lines or ["No high-confidence protected opening was detected in this source set."])
    return AssistedSetPlan(
        profiles=profiles,
        headline=" + ".join(profile.title for profile in profiles),
        category=category,
        summary=summary,
        reasons=reasons,
        warnings=tuple(dict.fromkeys(warnings)),
        source_roles=roles,
        recipe_keys=recipes,
        target_bpm=int(frame["target_bpm"]),
        target_key_pc=int(target_pc) if isinstance(target_pc, int) else None,
        target_mode=target_mode,
        bars=max(48, min(160, int(frame["bars"]))),
        section_bars=int(frame["section_bars"]),
        confidence=max(0, min(100, score)),
        pair_assessments=assessments,
        contribution_plan=contribution_plan,
        signature_plan=signature_plan,
    )



def _variant_blend_plan(a: MidiProfile, b: MidiProfile) -> AssistedSetPlan | None:
    """Create one explicitly labeled Version Fusion candidate when arrangements differ."""
    if not materially_different_arrangement(a, b):
        return None
    base = _pair_plan(a, b, allow_same_composition=True)
    if base is None:
        return None
    distance = arrangement_distance(a, b)
    warning = (
        "These files appear to represent the same composition. This is a Version Fusion candidate, "
        "not a normal two-song mash: combine complementary roles/sections rather than doubling the whole arrangement."
    )
    return AssistedSetPlan(
        profiles=base.profiles,
        headline=base.headline,
        category="Version Fusion candidate · same composition",
        summary=(
            "The composition identity appears to be the same, but the detected instrumentation, track roles, or arrangement "
            "structure differs enough to support Version Fusion: choose the strongest roles or sections from each MIDI."
        ),
        reasons=(
            f"Arrangement distance is {distance}%: the engine detected a material instrumentation or role difference.",
            "This candidate is separated from standard mash recommendations so alternate filenames do not masquerade as different songs.",
            "Prefer role exchange, section exchange, or transformation; the goal is to keep the best tracks from each version without doubling the same full arrangement.",
            *base.reasons[:3],
        ),
        warnings=tuple(dict.fromkeys((warning, *base.warnings))),
        source_roles=base.source_roles,
        recipe_keys=base.recipe_keys,
        target_bpm=base.target_bpm,
        target_key_pc=base.target_key_pc,
        target_mode=base.target_mode,
        bars=base.bars,
        section_bars=base.section_bars,
        confidence=min(88, max(55, int(round(base.confidence * 0.70 + distance * 0.30)))),
        pair_assessments=base.pair_assessments,
        contribution_plan=base.contribution_plan,
        signature_plan=base.signature_plan,
        origin="variant",
    )

def version_fusion_plan_for_pair(a: MidiProfile, b: MidiProfile) -> AssistedSetPlan | None:
    """Validate one same-composition alternate-arrangement fusion candidate."""
    return _variant_blend_plan(a, b)


_WHOLE_LIBRARY_FUSION_FAMILY_LIMIT = 24
_WHOLE_LIBRARY_FUSION_ALTERNATE_LIMIT = 3
_WHOLE_LIBRARY_FUSION_PLAN_LIMIT = 6
_GUIDED_REFERENCE_POOL_LIMIT = 48


def _bounded_variant_plans(
    families: Sequence[Sequence[MidiProfile]],
    *,
    source_rank: Callable[[MidiProfile], object],
    check_cancelled: Callable[[], None],
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[AssistedSetPlan, ...]:
    """Return a small, useful Version Fusion sample without N² family scans.

    Whole-library mash discovery must never compare every alternate arrangement
    against every other alternate arrangement.  A library with hundreds of
    versions of one composition would otherwise spend minutes or hours in an
    optional late phase after the cross-song recommendations were already ready.

    We examine only the strongest composition families, choose each family's
    best general-purpose representative, and compare it with a few arrangements
    that are maximally different by cheap arrangement metadata.  Version Fusion
    remains discoverable while ordinary Assisted mash search has a hard upper
    bound independent of duplicate-family size.
    """
    candidate_families = [tuple(family) for family in families if len(family) >= 2]
    candidate_families.sort(
        key=lambda family: (max(source_rank(profile) for profile in family), len(family)),
        reverse=True,
    )
    candidate_families = candidate_families[:_WHOLE_LIBRARY_FUSION_FAMILY_LIMIT]
    family_total = max(1, len(candidate_families))
    plans: list[AssistedSetPlan] = []

    for family_index, family in enumerate(candidate_families, 1):
        check_cancelled()
        primary = max(family, key=source_rank)
        alternates = [profile for profile in family if profile.path != primary.path]
        alternates.sort(
            key=lambda profile: (arrangement_distance(primary, profile), source_rank(profile)),
            reverse=True,
        )
        if progress is not None:
            progress(
                family_index,
                family_total,
                f"Checking a bounded Version Fusion family: {primary.title}",
            )
        for alternate in alternates[:_WHOLE_LIBRARY_FUSION_ALTERNATE_LIMIT]:
            check_cancelled()
            plan = _variant_blend_plan(primary, alternate)
            if plan is not None:
                plans.append(plan)
                break
        if len(plans) >= _WHOLE_LIBRARY_FUSION_PLAN_LIMIT:
            break

    plans.sort(
        key=lambda item: (item.confidence, arrangement_distance(item.profiles[0], item.profiles[1])),
        reverse=True,
    )
    return tuple(plans[:_WHOLE_LIBRARY_FUSION_PLAN_LIMIT])


def _guided_plan_as_assisted(plan) -> AssistedSetPlan:
    """Preserve dependable Guided recommendations inside Assisted Studio.

    Assisted Studio adds broader arrangements and multi-source ideas, but it
    should not hide the strongest beginner-safe pairs.  Converting the Guided
    plan keeps one shared recommendation vocabulary while still exposing the
    extra Assisted controls and recipes.
    """
    if plan.lead_source == "a":
        roles = ("lead melody", "foundation")
    else:
        roles = ("foundation", "lead melody")
    return AssistedSetPlan(
        profiles=(plan.source_a, plan.source_b),
        headline=plan.headline,
        category=f"Guided pick · {plan.category}",
        summary=(
            "Also recommended in Guided Remix. "
            "Assisted Studio keeps this dependable pairing and adds alternate plans, recipes, and technical control."
        ),
        reasons=(
            "This exact song pair also passed the stricter Guided Remix safety gate.",
            *plan.reasons,
        ),
        warnings=(),
        source_roles=roles,
        recipe_keys=tuple(plan.recipe_keys),
        target_bpm=int(plan.target_bpm),
        target_key_pc=plan.target_key_pc,
        target_mode=plan.target_mode,
        bars=int(plan.bars),
        section_bars=int(plan.section_bars),
        confidence=max(0, min(100, int(plan.safety_score))),
        pair_assessments=(plan.assessment,),
        contribution_plan=tuple(plan.contribution_plan),
        signature_plan=tuple(plan.signature_plan),
        origin="guided",
    )


def _discovery_graph_for_search(
    app_root: str | Path | None, profiles: Sequence[MidiProfile],
    discovery_title_map: Mapping[str, Sequence[MidiProfile]] | None = None,
) -> tuple[DiscoveryGraph | None, dict[tuple[str, str], DiscoveryEvidence]]:
    """Load local relationship evidence without introducing a live-network dependency."""
    if not app_root:
        return None, {}
    graph = DiscoveryGraph.for_app(app_root)
    catalog = ReferenceCatalog(
        Path(app_root) / "data" / "known_pairings.json",
        import_directories=(AppPaths.for_app(app_root).ensure().reference_imports,),
    )
    graph.import_reference_catalog(catalog)
    return graph, graph.resolved_evidence_pairs(profiles, limit=1024, title_map=discovery_title_map)


def _plan_discovery_rank(plan: AssistedSetPlan) -> float:
    """Bound metadata/human/user evidence so MIDI-derived confidence stays authoritative."""
    reference_bonus = min(8.0, max(0, plan.discovery_score) / 12.5)
    feedback_bonus = max(-6.0, min(4.0, plan.discovery_feedback_score / 10.0))
    return float(plan.confidence) + reference_bonus + feedback_bonus


def assisted_set_recommendations(
    profiles: Sequence[MidiProfile],
    limit: int = 6,
    *,
    app_root: str | Path | None = None,
    exclude_paths: Sequence[str] = (),
    search_round: int = 0,
    cancel_event=None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    candidate_index: CandidateIndex | None = None,
    family_by_path: Mapping[str, str] | None = None,
    profile_loader: Callable[[Sequence[str]], Mapping[str, MidiProfile]] | None = None,
    discovery_title_map: Mapping[str, Sequence[MidiProfile]] | None = None,
) -> tuple[AssistedSetPlan, ...]:
    """Return curated pair and multi-source ideas without building an all-library matrix.

    The pool is bounded for large collections. Pair analysis produces strong
    seeds; trio/quartet plans add sources that complement those seeds by role or
    section and expose broader creative choices than Guided Remix.
    """
    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Discovery search was cancelled.")

    def report(current: int, total: int, label: str) -> None:
        if progress_callback is not None:
            progress_callback(int(current), max(1, int(total)), str(label))

    check_cancelled()
    # A 96K search touches the source gate repeatedly during eligibility, sorting,
    # family collapse, and multi-source extension. Cache it once per profile path
    # so the full-catalogue pass remains linear instead of recomputing readiness
    # several times for every MIDI.
    gate_cache: dict[str, AssistedSourceGate] = {}

    def source_gate(profile: MidiProfile) -> AssistedSourceGate:
        cached = gate_cache.get(profile.path)
        if cached is None:
            cached = assisted_source_gate(profile)
            gate_cache[profile.path] = cached
        return cached

    excluded_path_set = {str(path) for path in exclude_paths if str(path)}
    excluded_profiles = tuple(profile for profile in profiles if profile.path in excluded_path_set)
    family_lookup = family_by_path or {}
    excluded_family_ids = {family_lookup.get(path) for path in excluded_path_set if family_lookup.get(path)}

    def excluded_family(profile: MidiProfile) -> bool:
        family_id = family_lookup.get(profile.path)
        if profile.path in excluded_path_set or (family_id and family_id in excluded_family_ids):
            return True
        return not family_lookup and any(probable_same_composition(profile, prior) for prior in excluded_profiles)

    eligible = [
        profile for profile in profiles
        if source_gate(profile).eligible and not excluded_family(profile)
    ]
    discovery_graph: DiscoveryGraph | None = None
    graph_pairs: dict[tuple[str, str], DiscoveryEvidence] = {}
    try:
        discovery_graph, graph_pairs = _discovery_graph_for_search(
            app_root, tuple(eligible), discovery_title_map=discovery_title_map
        )
        if discovery_graph is not None:
            discovery_graph.close()
            discovery_graph = None
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        if discovery_graph is not None:
            discovery_graph.close()
        discovery_graph, graph_pairs = None, {}

    def source_rank(profile: MidiProfile) -> tuple[int, int, int]:
        return (source_gate(profile).confidence, profile.orchestration_readiness, profile.note_count)

    eligible.sort(key=source_rank, reverse=True)
    if family_lookup:
        grouped: dict[str, list[MidiProfile]] = {}
        for profile in eligible:
            grouped.setdefault(family_lookup.get(profile.path) or f"path:{profile.path}", []).append(profile)
        families = list(grouped.values())
    else:
        families = list(composition_groups(tuple(eligible)))
    distinct = [max(family, key=source_rank) for family in families]
    distinct.sort(key=source_rank, reverse=True)
    by_all_path = {profile.path: profile for profile in distinct}

    # 96K-wide no-anchor discovery uses diverse seed neighborhoods rather than
    # pairing the generic top 72 files with each other. Every distinct song can
    # influence the persistent index; rotating tempo/key/style buckets provide
    # representative seeds from across the catalogue, then only bounded neighbor
    # edges receive expensive musical scoring.
    metadata_index = candidate_index or CandidateIndex(tuple(distinct))
    bucket_best: dict[tuple[object, ...], MidiProfile] = {}
    for profile in distinct:
        tempo_bucket = int(round(float(profile.felt_bpm or 0.0) / 10.0)) if profile.felt_bpm else -1
        key = (tempo_bucket, profile.tonic_pc, str(profile.mode).casefold(), str(profile.meter_family).casefold(), str(profile.style).casefold()[:24])
        current = bucket_best.get(key)
        if current is None or source_rank(profile) > source_rank(current):
            bucket_best[key] = profile
    diverse = sorted(bucket_best.values(), key=source_rank, reverse=True)
    seeds: list[MidiProfile] = []
    seen_seed_paths: set[str] = set()
    # Sixteen diverse anchors keep no-anchor discovery interactive at 96k scale.
    # Every seed still queries the complete catalogue-wide index, while imported
    # human evidence supplies an independent discovery lane. Repeated wider
    # searches rotate through different metadata buckets instead of rerunning the
    # same deterministic seed slice.
    seed_limit = 16
    round_number = max(0, int(search_round))
    if diverse:
        start = (round_number * seed_limit) % len(diverse)
        rotated = diverse[start:start + seed_limit]
        if len(rotated) < seed_limit:
            rotated = [*rotated, *diverse[: seed_limit - len(rotated)]]
    else:
        rotated = []
    for profile in (*rotated, *distinct[:8]):
        if profile.path not in seen_seed_paths:
            seeds.append(profile); seen_seed_paths.add(profile.path)
        if len(seeds) >= seed_limit:
            break
    pool_paths: set[str] = {profile.path for profile in seeds}
    pair_paths: set[tuple[str, str]] = set()
    for seed in seeds:
        for other in metadata_index.candidate_paths(seed.path, limit=10):
            if other in by_all_path and other != seed.path:
                pair_paths.add(tuple(sorted((seed.path, other))))
                pool_paths.add(other)
    pool = [by_all_path[path] for path in pool_paths if path in by_all_path]
    pool.sort(key=source_rank, reverse=True)
    pool = pool[:192]

    # Human/imported relationships are explicit additional edges and can pull a
    # song into the bounded detailed scorer even when metadata neighborhoods do
    # not surface it.
    graph_path_pairs = {
        pair: evidence for pair, evidence in graph_pairs.items()
        if all(path in by_all_path for path in pair)
    }
    pair_paths.update(graph_path_pairs)
    ordered_pair_paths = tuple(pair_paths)
    pair_total = len(ordered_pair_paths)
    # Multi-source ideas are valuable, but they must not dominate the latency of
    # a no-anchor 96K search. Build a small representative sample from the same
    # already-ranked pool; deeper multi-source exploration can happen after the
    # first useful page is visible.
    multi_total = min(max(4, min(8, max(1, int(limit)))), pair_total)
    # Version Fusion is optional enrichment during whole-library mash discovery.
    # It gets a fixed family budget rather than one step for every possible
    # combination inside duplicate/version families.
    variant_family_total = min(
        _WHOLE_LIBRARY_FUSION_FAMILY_LIMIT,
        sum(1 for family in families if len(family) >= 2),
    )
    guided_budget = 24 if len(pool) >= 2 else 0
    total_steps = max(1, pair_total + multi_total + variant_family_total + guided_budget + 1)
    completed = 0

    if profile_loader is not None:
        detailed_paths: set[str] = set()
        for a_path, b_path in ordered_pair_paths:
            detailed_paths.add(a_path); detailed_paths.add(b_path)
            if len(detailed_paths) >= 512:
                break
        loaded = profile_loader(tuple(detailed_paths))
        if loaded:
            by_all_path.update(loaded)
            pool = [loaded.get(profile.path, profile) for profile in pool]

    pair_plans: list[AssistedSetPlan] = []
    for a_path, b_path in ordered_pair_paths:
        check_cancelled()
        completed += 1
        a, b = by_all_path.get(a_path), by_all_path.get(b_path)
        if a is None or b is None:
            continue
        if completed == 1 or completed == pair_total or completed % 16 == 0:
            report(completed, total_steps, f"Testing {a.title} with {b.title}")
        evidence = graph_path_pairs.get(tuple(sorted((a.path, b.path))))
        plan = _pair_plan(a, b, discovery_evidence=evidence)
        if plan is not None:
            pair_plans.append(plan)
    check_cancelled()
    pair_plans.sort(key=lambda item: (_plan_discovery_rank(item), item.confidence, item.discovery_score, len(item.recipe_keys)), reverse=True)

    selected_pairs: list[AssistedSetPlan] = []
    used_categories: set[str] = set()
    used_pairs: set[frozenset[str]] = set()
    for plan in pair_plans:
        paths = frozenset(profile.path for profile in plan.profiles)
        if paths in used_pairs:
            continue
        if plan.category in used_categories and len(selected_pairs) >= 2:
            continue
        selected_pairs.append(plan)
        used_pairs.add(paths)
        used_categories.add(plan.category)
        if len(selected_pairs) >= 3:
            break
    if len(selected_pairs) < 3:
        for plan in pair_plans:
            paths = frozenset(profile.path for profile in plan.profiles)
            if paths not in used_pairs:
                selected_pairs.append(plan)
                used_pairs.add(paths)
            if len(selected_pairs) >= 3:
                break

    multi_plans: list[AssistedSetPlan] = []
    # The metadata seed pool is already quality-sorted. A bounded prefix is enough
    # to construct representative trio/quartet ideas without thousands of extra
    # pair assessments before the first page of mash ideas can be shown.
    multi_candidate_pool = pool[:72]
    for base in pair_plans[:multi_total]:
        check_cancelled()
        completed += 1
        report(completed, total_steps, f"Looking for additional sources around {base.headline}")
        base_profiles = list(base.profiles)
        base_paths = {item.path for item in base_profiles}
        base_styles = {item.style for item in base_profiles}
        candidates: list[tuple[float, MidiProfile]] = []
        for profile in multi_candidate_pool:
            check_cancelled()
            if profile.path in base_paths:
                continue
            if any(probable_same_composition(profile, item) for item in base_profiles):
                continue
            relations = [assess_pair(item, profile) for item in base_profiles]
            best = max(relations, key=lambda rel: (rel.preparation_score, rel.meter_score, rel.tempo_score))
            gate = source_gate(profile)
            complement = (
                best.preparation_score * 0.30
                + best.meter_score * 0.18
                + best.tempo_score * 0.14
                + best.harmonic_score * 0.12
                + gate.confidence * 0.18
                + (8 if profile.style not in base_styles else 0)
            )
            candidates.append((complement, profile))
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, third in candidates[:3]:
            check_cancelled()
            trio = _multi_plan((*base_profiles, third))
            if trio:
                multi_plans.append(trio)
                # One representative quartet is enough for the initial page.
                # Additional multi-source exploration is deliberately deferred.
                if len(multi_plans) <= 2:
                    trio_paths = {q.path for q in trio.profiles}
                    fourths = [
                        candidate for candidate in multi_candidate_pool[:48]
                        if candidate.path not in trio_paths
                        and not any(probable_same_composition(candidate, q) for q in trio.profiles)
                    ]
                    if fourths:
                        ranked_fourths: list[tuple[int, MidiProfile]] = []
                        for candidate in fourths:
                            check_cancelled()
                            score = max(
                                assess_pair(existing, candidate).preparation_score for existing in trio.profiles
                            ) + source_gate(candidate).confidence
                            ranked_fourths.append((score, candidate))
                        ranked_fourths.sort(key=lambda item: item[0], reverse=True)
                        quartet = _multi_plan((*trio.profiles, ranked_fourths[0][1])) if ranked_fourths else None
                        if quartet:
                            multi_plans.append(quartet)
                break
    check_cancelled()
    # Deduplicate identical source sets and keep the most confident plan.
    by_set: dict[frozenset[str], AssistedSetPlan] = {}
    for plan in multi_plans:
        key = frozenset(profile.path for profile in plan.profiles)
        if key not in by_set or plan.confidence > by_set[key].confidence:
            by_set[key] = plan
    multi_ranked = sorted(by_set.values(), key=lambda item: (item.confidence, item.source_count, len(item.recipe_keys)), reverse=True)

    # Same-composition files never enter standard pair or multi-song sets.
    # Search only a bounded sample for Version Fusion.  The old implementation
    # expanded every family with combinations(family, 2), which is quadratic in
    # the number of alternate files and could strand a 96k library at ~94%.
    variant_stage_start = completed

    def variant_progress(current: int, total: int, label: str) -> None:
        nonlocal completed
        if variant_family_total <= 0:
            return
        mapped = variant_stage_start + min(
            variant_family_total,
            max(0, int(round(variant_family_total * current / max(1, total)))),
        )
        completed = max(completed, mapped)
        report(completed, total_steps, label)

    variant_plans = list(
        _bounded_variant_plans(
            families,
            source_rank=source_rank,
            check_cancelled=check_cancelled,
            progress=variant_progress if variant_family_total else None,
        )
    )
    completed = variant_stage_start + variant_family_total

    # Assisted Studio must visibly broaden the search rather than leading with
    # the same list as Guided Remix.  We need only a small Guided reference set
    # for labeling/diversity here.  Do NOT run another 96k-library recommendation
    # search after Assisted has already done its expensive work.  The bounded
    # pool's pair assessments were just computed above, so this pass is normally
    # cache-heavy and quick.
    from core.guided_beginner import guided_pair_recommendations

    guided_stage_start = completed

    def guided_progress(current: int, total: int, label: str) -> None:
        if guided_budget <= 0:
            return
        mapped = guided_stage_start + min(
            guided_budget,
            max(0, int(round(guided_budget * current / max(1, total)))),
        )
        report(mapped, total_steps, f"Finalizing Assisted diversity — {label}")

    guided_source_pool = tuple(pool[:_GUIDED_REFERENCE_POOL_LIMIT])
    guided_candidates = tuple(
        _guided_plan_as_assisted(plan)
        for plan in guided_pair_recommendations(
            guided_source_pool,
            limit=min(3, max(1, limit)),
            app_root=app_root,
            cancel_event=cancel_event,
            progress_callback=guided_progress if guided_budget else None,
        )
    ) if len(guided_source_pool) >= 2 else ()
    completed = guided_stage_start + guided_budget
    guided_sets = {frozenset(profile.path for profile in plan.profiles) for plan in guided_candidates}
    check_cancelled()

    results: list[AssistedSetPlan] = []
    used_sets: set[frozenset[str]] = set()

    def add(plan: AssistedSetPlan | None) -> bool:
        if plan is None:
            return False
        if plan.origin != "variant" and len(composition_groups(plan.profiles)) != len(plan.profiles):
            return False
        key = frozenset(profile.path for profile in plan.profiles)
        if key in used_sets:
            return False
        results.append(plan)
        used_sets.add(key)
        return True

    exclusive_pairs = [
        plan for plan in pair_plans
        if frozenset(profile.path for profile in plan.profiles) not in guided_sets
    ]
    adaptable_pairs = [plan for plan in exclusive_pairs if assisted_plan_group(plan) == ASSISTED_GROUP_ADAPTABLE]
    section_pairs = [plan for plan in exclusive_pairs if assisted_plan_group(plan) == ASSISTED_GROUP_SECTION]
    broader_pairs = [plan for plan in exclusive_pairs if assisted_plan_group(plan) == ASSISTED_GROUP_BROADER]

    # Put Assisted-only possibilities first so the difference is immediately
    # visible even on a laptop-height results screen.
    add(adaptable_pairs[0] if adaptable_pairs else None)
    add(section_pairs[0] if section_pairs else None)
    add(multi_ranked[0] if multi_ranked else None)
    add(broader_pairs[0] if broader_pairs else (exclusive_pairs[0] if exclusive_pairs else None))
    if len(multi_ranked) > 1:
        first_count = multi_ranked[0].source_count
        alternate = next((item for item in multi_ranked[1:] if item.source_count != first_count), multi_ranked[1])
        add(alternate)

    # Fill from Assisted-only candidates before allowing overlapping safe pairs.
    for plan in (*exclusive_pairs, *multi_ranked):
        add(plan)
        if len(results) >= max(1, limit - 1):
            break

    # At most one dependable Guided-level reference remains, clearly labeled.
    if len(results) < max(1, limit):
        for guided in guided_candidates:
            if add(guided):
                break

    for plan in (*pair_plans, *multi_ranked):
        add(plan)
        if len(results) >= max(1, limit):
            break

    if variant_plans and limit >= 4:
        variant = variant_plans[0]
        if len(results) < max(1, limit):
            add(variant)
        elif limit >= 5:
            # Reserve no more than one final slot for a clearly labeled version
            # blend. Distinct-song recommendations remain the dominant list.
            results[-1] = variant
    check_cancelled()
    report(total_steps, total_steps, f"Found {len(results[: max(1, limit)])} remix idea(s).")
    if discovery_graph is not None:
        discovery_graph.close()
    return tuple(results[: max(1, limit)])


def assisted_library_summary(profiles: Sequence[MidiProfile]) -> dict[str, int]:
    gates = [assisted_source_gate(profile) for profile in profiles]
    return {
        "total": len(gates),
        "eligible": sum(1 for gate in gates if gate.eligible),
        "studio_ready": sum(1 for gate in gates if gate.level == "Studio-ready"),
        "adaptable": sum(1 for gate in gates if gate.level == "Adaptable"),
        "whole_section": sum(1 for gate in gates if gate.level == "Whole-section source"),
        "hidden": sum(1 for gate in gates if not gate.eligible),
    }


def assisted_anchor_recommendations(
    profiles: Sequence[MidiProfile],
    anchor_path: str,
    limit: int = 8,
    *,
    app_root: str | Path | None = None,
    exclude_paths: Sequence[str] = (),
    search_round: int = 0,
    cancel_event=None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    candidate_index: CandidateIndex | None = None,
    family_by_path: Mapping[str, str] | None = None,
    family_members: Mapping[str, Sequence[str]] | None = None,
    profile_loader: Callable[[Sequence[str]], Mapping[str, MidiProfile]] | None = None,
    discovery_title_map: Mapping[str, Sequence[MidiProfile]] | None = None,
) -> AssistedDiscoveryResult:
    """Discover several distinct ideas around a temporary focus song.

    Large libraries are handled in two stages: every eligible profile, including
    files in nested library folders, enters the cheap metadata index; only the
    strongest anchor-specific shortlist receives the more expensive pair scorer.
    This is intentionally different from taking a generic "top files" slice,
    which can repeatedly favor one directory or one kind of well-formed MIDI.
    """

    eligible = [profile for profile in profiles if assisted_source_gate(profile).eligible]
    by_path = {profile.path: profile for profile in eligible}
    anchor = by_path.get(str(anchor_path))
    if anchor is None:
        raise LookupError("The selected discovery focus is no longer available in the classified MIDI library.")
    excluded_path_set = {str(path) for path in exclude_paths if str(path) and str(path) != anchor.path}
    excluded_profiles = tuple(by_path[path] for path in excluded_path_set if path in by_path)
    family_lookup = family_by_path or {}
    excluded_family_ids = {family_lookup.get(path) for path in excluded_path_set if family_lookup.get(path)}

    def excluded_family(profile: MidiProfile) -> bool:
        family_id = family_lookup.get(profile.path)
        if profile.path in excluded_path_set or (family_id and family_id in excluded_family_ids):
            return True
        # Compatibility fallback for old databases while family IDs backfill.
        return not family_lookup and any(probable_same_composition(profile, prior) for prior in excluded_profiles)

    def cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    def report(current: int, total: int, label: str) -> None:
        if progress_callback is not None:
            progress_callback(int(current), max(1, int(total)), str(label))

    def rank(profile: MidiProfile) -> tuple[int, int, int]:
        gate = assisted_source_gate(profile)
        return (gate.confidence, profile.orchestration_readiness, profile.note_count)

    if cancelled():
        raise InterruptedError("Discovery search was cancelled.")

    # Index the complete eligible catalogue. CandidateIndex does not care which
    # directory a profile came from, so root and deeply nested MIDI files compete
    # on the same tempo/key/meter/progression/role evidence.
    report(0, 100, f"Using the full-library compatibility index around {anchor.title}")
    metadata_index = candidate_index or CandidateIndex(tuple(eligible))
    discovery_graph: DiscoveryGraph | None = None
    graph_evidence: dict[str, DiscoveryEvidence] = {}
    if app_root:
        try:
            discovery_graph = DiscoveryGraph.for_app(app_root)
            reference_catalog = ReferenceCatalog(
                Path(app_root) / "data" / "known_pairings.json",
                import_directories=(AppPaths.for_app(app_root).ensure().reference_imports,),
            )
            discovery_graph.import_reference_catalog(reference_catalog)
            graph_evidence = discovery_graph.evidence_candidates(
                anchor, tuple(eligible), limit=768, title_map=discovery_title_map
            )
            discovery_graph.close()
            discovery_graph = None
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
            if discovery_graph is not None:
                discovery_graph.close()
            discovery_graph = None
            graph_evidence = {}
    if cancelled():
        if discovery_graph is not None:
            discovery_graph.close()
        raise InterruptedError("Discovery search was cancelled.")

    # The complete 96k metadata index supplies the broad neighborhood; graph
    # edges are guaranteed a place in the detailed scorer even when key/BPM
    # metadata alone would not shortlist them.
    # Repeated "More Different Ideas" requests deliberately look farther down
    # the anchor neighborhood while excluding composition families already
    # shown in this Assisted session.  The upper bound keeps a 96k library from
    # turning an interactive request into an all-pairs search.
    round_number = max(0, int(search_round))
    shortlist_limit = max(
        384,
        min(4096, max(1, int(limit)) * 32 + len(excluded_profiles) * 6 + round_number * 256),
    )
    indexed_paths = metadata_index.candidate_paths(anchor.path, limit=shortlist_limit)
    combined_paths = list(graph_evidence)
    combined_paths.extend(path for path in indexed_paths if path not in graph_evidence)
    indexed_candidates = [metadata_index.by_path[path] for path in combined_paths if path in metadata_index.by_path]

    # Stable family IDs are computed once during catalogue ingestion/migration.
    # Alternate-version lookup is now O(k) for the selected composition instead
    # of fuzzy-comparing the anchor against all ~96,000 filenames each search.
    anchor_family_items: list[MidiProfile] = [anchor]
    anchor_family_id = family_lookup.get(anchor.path)
    if anchor_family_id and family_members is not None:
        for family_path in family_members.get(anchor_family_id, ()):
            if family_path != anchor.path and family_path in by_path:
                anchor_family_items.append(by_path[family_path])
    elif not family_lookup:
        # Migration fallback only; v0.27 normally backfills family IDs at startup.
        for profile in eligible:
            if profile.path != anchor.path and probable_same_composition(anchor, profile):
                anchor_family_items.append(profile)
    anchor_family = tuple(anchor_family_items)
    anchor_family_paths = {profile.path for profile in anchor_family}
    # Only a few strongly contrasting alternate arrangements need expensive
    # Version Fusion scoring.  Identity discovery above may find hundreds or
    # thousands of files for one composition, but Assisted returns at most one
    # fusion lane; scoring every alternate would make anchor searches scale with
    # duplicate count for no user-visible benefit.
    anchor_variant_candidates = sorted(
        (profile for profile in anchor_family if profile.path != anchor.path),
        key=lambda profile: (arrangement_distance(anchor, profile), rank(profile)),
        reverse=True,
    )[:12]

    # Collapse duplicate/version families only inside the bounded shortlist.
    # Preserve CandidateIndex order so musical proximity to the chosen anchor is
    # authoritative; source quality is used only to choose among variants.
    candidate_pool = [
        profile for profile in indexed_candidates
        if profile.path not in anchor_family_paths and not excluded_family(profile)
    ]
    path_position = {profile.path: index for index, profile in enumerate(candidate_pool)}
    if family_lookup:
        grouped: dict[str, list[MidiProfile]] = {}
        for profile in candidate_pool:
            grouped.setdefault(family_lookup.get(profile.path) or f"path:{profile.path}", []).append(profile)
        representatives = [max(family, key=rank) for family in grouped.values()]
    else:
        representatives = [max(family, key=rank) for family in composition_groups(tuple(candidate_pool))]
    representatives.sort(key=lambda profile: path_position.get(profile.path, 10**9))

    # Sparse/poorly tagged libraries may produce fewer metadata neighbors. Fill
    # the tail with dependable distinct sources from anywhere in the catalogue,
    # but never replace the anchor-specific indexed leaders.
    desired = max(160, min(384, max(1, int(limit)) * 12 + min(96, len(excluded_profiles))))
    seen_paths = {profile.path for profile in representatives}
    if len(representatives) < desired:
        fallbacks = [
            profile for profile in eligible
            if profile.path not in anchor_family_paths
            and profile.path not in seen_paths
            and not excluded_family(profile)
        ]
        fallbacks.sort(key=lambda profile: (rank(profile), profile.path.casefold()), reverse=True)
        represented_family_ids = {family_lookup.get(item.path) for item in representatives if family_lookup.get(item.path)}
        for profile in fallbacks:
            if cancelled():
                raise InterruptedError("Discovery search was cancelled.")
            family_id = family_lookup.get(profile.path)
            if family_id and family_id in represented_family_ids:
                continue
            if not family_lookup and any(probable_same_composition(profile, existing) for existing in representatives):
                continue
            representatives.append(profile)
            seen_paths.add(profile.path)
            if family_id:
                represented_family_ids.add(family_id)
            if len(representatives) >= desired:
                break

    candidates = representatives[:desired]
    if profile_loader is not None:
        requested_paths = [anchor.path, *(item.path for item in candidates), *(item.path for item in anchor_variant_candidates)]
        loaded = profile_loader(requested_paths)
        anchor = loaded.get(anchor.path, anchor)
        candidates = [loaded.get(item.path, item) for item in candidates]
        anchor_variant_candidates = [loaded.get(item.path, item) for item in anchor_variant_candidates]
    total_steps = max(1, len(candidates) + min(18, len(candidates)) + len(anchor_variant_candidates))

    pair_plans: list[AssistedSetPlan] = []
    for index, candidate in enumerate(candidates, 1):
        if cancelled():
            raise InterruptedError("Discovery search was cancelled.")
        report(index, total_steps, f"Testing {anchor.title} with {candidate.title}")
        plan = _pair_plan(anchor, candidate, discovery_evidence=graph_evidence.get(candidate.path))
        if plan is not None:
            pair_plans.append(plan)
    pair_plans.sort(
        key=lambda item: (_plan_discovery_rank(item), item.confidence, item.discovery_score, len(item.recipe_keys)),
        reverse=True,
    )

    # Preserve variety instead of returning many nearly interchangeable score
    # leaders. A category may repeat, but only after the first pass has shown
    # different relationship types.
    selected_pairs: list[AssistedSetPlan] = []
    used_categories: set[str] = set()
    pair_target = min(max(8, int(limit)), 24)
    for plan in pair_plans:
        if plan.category in used_categories and len(selected_pairs) < min(4, limit):
            continue
        selected_pairs.append(plan)
        used_categories.add(plan.category)
        if len(selected_pairs) >= pair_target:
            break
    for plan in pair_plans:
        if plan not in selected_pairs:
            selected_pairs.append(plan)
        if len(selected_pairs) >= pair_target:
            break

    multi_plans: list[AssistedSetPlan] = []
    step = len(candidates)
    for base in pair_plans[: min(18, len(pair_plans))]:
        if cancelled():
            raise InterruptedError("Discovery search was cancelled.")
        step += 1
        report(step, total_steps, f"Looking for a third source around {base.headline}")
        existing = tuple(base.profiles)
        extras: list[tuple[float, MidiProfile]] = []
        for candidate in candidates:
            if cancelled():
                raise InterruptedError("Discovery search was cancelled.")
            if any(candidate.path == item.path or probable_same_composition(candidate, item) for item in existing):
                continue
            relations = [assess_pair(item, candidate) for item in existing]
            best = max(relations, key=lambda rel: (rel.preparation_score, rel.meter_score, rel.tempo_score))
            score = (
                best.preparation_score * 0.34
                + best.meter_score * 0.18
                + best.tempo_score * 0.14
                + best.harmonic_score * 0.12
                + assisted_source_gate(candidate).confidence * 0.22
            )
            extras.append((score, candidate))
        extras.sort(key=lambda item: item[0], reverse=True)
        for _score, third in extras[:5]:
            if cancelled():
                raise InterruptedError("Discovery search was cancelled.")
            plan = _multi_plan((anchor, existing[1], third))
            if plan is not None:
                multi_plans.append(plan)
                break
        if len(multi_plans) >= 3:
            break
    multi_plans.sort(key=lambda item: (item.confidence, len(item.recipe_keys)), reverse=True)

    standard: list[AssistedSetPlan] = []
    seen_sets: set[frozenset[str]] = set()

    def add(plan: AssistedSetPlan) -> None:
        key = frozenset(profile.path for profile in plan.profiles)
        if key in seen_sets or anchor.path not in key:
            return
        if len(composition_groups(plan.profiles)) != len(plan.profiles):
            return
        standard.append(plan)
        seen_sets.add(key)

    for index in range(max(len(selected_pairs), len(multi_plans))):
        if index < len(selected_pairs):
            add(selected_pairs[index])
        if index < len(multi_plans):
            add(multi_plans[index])
        if len(standard) >= max(1, int(limit)):
            break

    variants: list[AssistedSetPlan] = []
    for other in anchor_variant_candidates:
        if cancelled():
            raise InterruptedError("Discovery search was cancelled.")
        step += 1
        report(step, total_steps, f"Checking bounded Version Fusion candidate: {other.title}")
        plan = _variant_blend_plan(anchor, other)
        if plan is not None:
            variants.append(plan)
    variants.sort(
        key=lambda item: (item.confidence, arrangement_distance(item.profiles[0], item.profiles[1])),
        reverse=True,
    )
    report(total_steps, total_steps, f"Found {len(standard)} cross-song ideas and {min(1, len(variants))} alternate arrangement blend")
    if discovery_graph is not None:
        discovery_graph.close()
    return AssistedDiscoveryResult(
        anchor=anchor,
        standard=tuple(standard[: max(1, int(limit))]),
        variants=tuple(variants[:1]),
        evaluated_candidates=len(candidates),
    )


def assisted_library_discovery(
    profiles: Sequence[MidiProfile],
    limit: int = 8,
    *,
    app_root: str | Path | None = None,
    exclude_paths: Sequence[str] = (),
    search_round: int = 0,
    cancel_event=None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    candidate_index: CandidateIndex | None = None,
    family_by_path: Mapping[str, str] | None = None,
    profile_loader: Callable[[Sequence[str]], Mapping[str, MidiProfile]] | None = None,
    discovery_title_map: Mapping[str, Sequence[MidiProfile]] | None = None,
) -> AssistedDiscoveryResult:
    """Wrap the library-wide list in the cancellable discovery result API."""

    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Discovery search was cancelled.")
    plans = assisted_set_recommendations(
        profiles,
        limit=max(1, int(limit)),
        app_root=app_root,
        exclude_paths=exclude_paths,
        search_round=search_round,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        candidate_index=candidate_index,
        family_by_path=family_by_path,
        profile_loader=profile_loader,
        discovery_title_map=discovery_title_map,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Discovery search was cancelled.")
    standard = tuple(plan for plan in plans if plan.origin != "variant")
    variants = tuple(plan for plan in plans if plan.origin == "variant")
    return AssistedDiscoveryResult(None, standard, variants, len(profiles))
