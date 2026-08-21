from __future__ import annotations

"""Explain source roles, instruments, conflicts, and section-level arrangement plans.

The functions in this module are intentionally UI-independent.  Assisted Studio
and Creative Workbench can share the same evidence without duplicating musical
heuristics or hiding decisions inside widget state.
"""

from dataclasses import dataclass
from typing import Sequence

from core.assisted_studio import AssistedSetPlan
from core.remix_profile import MidiProfile, PairAssessment


SECTION_NAMES: tuple[str, ...] = ("Intro", "Verse", "Chorus", "Bridge", "Build", "Finale")


@dataclass(frozen=True)
class SourceIntelligence:
    source_index: int
    title: str
    suggested_role: str
    instruments: tuple[str, ...]
    arrangement_type: str
    role_confidence: int
    summary: str


@dataclass(frozen=True)
class ArrangementConflict:
    severity: str
    title: str
    summary: str
    mitigation: str


@dataclass(frozen=True)
class SectionAssignment:
    name: str
    source_index: int
    source_title: str
    purpose: str
    density: str


@dataclass(frozen=True)
class ArrangementBlueprint:
    preset_key: str
    sections: tuple[SectionAssignment, ...]

    @property
    def source_order(self) -> tuple[int, ...]:
        return tuple(section.source_index for section in self.sections)

    def decision_lines(self) -> tuple[str, ...]:
        return tuple(
            f"{section.name}: {section.source_title} leads · {section.purpose} · {section.density} density."
            for section in self.sections
        )


def _instrument_names(profile: MidiProfile, *, limit: int = 6) -> tuple[str, ...]:
    weighted: dict[str, int] = {}
    drums = False
    for track in profile.note_tracks:
        weight = max(1, int(track.note_count))
        for name in track.program_names:
            cleaned = str(name or "").strip()
            if cleaned:
                weighted[cleaned] = weighted.get(cleaned, 0) + weight
        if 9 in {int(channel) for channel in track.channels}:
            drums = True
    ordered = [name for name, _weight in sorted(weighted.items(), key=lambda item: (-item[1], item[0].casefold()))]
    if drums:
        ordered.append("Drums / percussion")
    if not ordered:
        ordered.append("General MIDI default")
    return tuple(dict.fromkeys(ordered))[: max(1, int(limit))]


def source_intelligence(profile: MidiProfile, suggested_role: str, source_index: int) -> SourceIntelligence:
    role_track = None
    if suggested_role == "lead melody" and profile.melody_track_index is not None:
        role_track = next((item for item in profile.note_tracks if item.index == profile.melody_track_index), None)
    elif suggested_role == "foundation" and profile.accompaniment_track_index is not None:
        role_track = next((item for item in profile.note_tracks if item.index == profile.accompaniment_track_index), None)
    if role_track is None and profile.note_tracks:
        role_track = max(profile.note_tracks, key=lambda item: item.note_count)
    confidence = int(role_track.role_confidence) if role_track is not None else int(profile.orchestration_readiness)
    instruments = _instrument_names(profile)
    role_detail = role_track.likely_role if role_track is not None else "whole arrangement"
    summary = (
        f"Detected as {profile.arrangement_type}; suggested {suggested_role}. "
        f"The strongest matching track is {role_detail} at {max(0, min(100, confidence))}% role confidence."
    )
    return SourceIntelligence(
        source_index=source_index,
        title=profile.title,
        suggested_role=suggested_role,
        instruments=instruments,
        arrangement_type=profile.arrangement_type,
        role_confidence=max(0, min(100, confidence)),
        summary=summary,
    )


def source_intelligence_for_plan(plan: AssistedSetPlan) -> tuple[SourceIntelligence, ...]:
    return tuple(
        source_intelligence(profile, role, index)
        for index, (profile, role) in enumerate(zip(plan.profiles, plan.source_roles))
    )


def _assessment_conflicts(assessments: Sequence[PairAssessment]) -> list[ArrangementConflict]:
    conflicts: list[ArrangementConflict] = []
    if not assessments:
        return conflicts
    minimum_harmony = min(item.harmonic_score for item in assessments)
    minimum_meter = min(item.meter_score for item in assessments)
    minimum_tempo = min(item.tempo_score for item in assessments)
    maximum_shift = max(abs(item.key_shift_b_to_a) for item in assessments)
    if minimum_harmony < 42:
        conflicts.append(ArrangementConflict(
            "High",
            "Harmony overlap is weak",
            f"At least one source relationship scored {minimum_harmony}% for harmonic overlap after key alignment.",
            "Prefer section exchange, source journey, or transformation before dense layering.",
        ))
    if maximum_shift >= 5:
        conflicts.append(ArrangementConflict(
            "Medium",
            "Large transposition",
            f"At least one source needs a shift of {maximum_shift} semitones toward the target key.",
            "Audition exposed melodies and bass lines; keep whole sections when the timbre becomes unnatural.",
        ))
    if minimum_meter < 55:
        conflicts.append(ArrangementConflict(
            "Medium",
            "Phrase grids may not line up cleanly",
            f"The weakest meter/phrase relationship is {minimum_meter}%.",
            "Use longer section blocks and avoid rapid call-and-response until the downbeats are confirmed.",
        ))
    if minimum_tempo < 50:
        conflicts.append(ArrangementConflict(
            "Medium",
            "Tempo adaptation is substantial",
            f"The weakest practical tempo relationship is {minimum_tempo}%.",
            "Use half-time/double-time interpretation or preserve complete sections instead of stretching exposed solos.",
        ))
    return conflicts


def detect_arrangement_conflicts(plan: AssistedSetPlan) -> tuple[ArrangementConflict, ...]:
    conflicts = _assessment_conflicts(plan.pair_assessments)
    lead_count = sum(1 for role in plan.source_roles if role == "lead melody")
    dense_sources = sum(
        1
        for profile in plan.profiles
        if profile.arrangement_type in {"full arrangement", "multi-track arrangement", "piano reduction", "keyboard reduction"}
        or sum(track.max_polyphony for track in profile.note_tracks) >= 12
    )
    if lead_count > 1 or "melody-relay" in plan.recipe_keys or "layered-blend" in plan.recipe_keys:
        conflicts.append(ArrangementConflict(
            "Medium",
            "Competing lead material",
            "Several arrangements can expose more than one recognizable upper voice.",
            "Keep one lead at a time, use melody relay, or lower the secondary voice before a layered finale.",
        ))
    if dense_sources >= 2:
        conflicts.append(ArrangementConflict(
            "Medium",
            "Dense arrangements may mask one another",
            f"{dense_sources} selected sources contain broad or highly polyphonic textures.",
            "Rotate complete sections or keep one foundation while other songs contribute short hooks.",
        ))
    bass_like = 0
    for profile in plan.profiles:
        names = {name.casefold() for item in profile.note_tracks for name in item.program_names}
        if any("bass" in name for name in names):
            bass_like += 1
    if bass_like >= 2:
        conflicts.append(ArrangementConflict(
            "Medium",
            "Multiple bass parts detected",
            f"{bass_like} sources appear to contain bass instruments.",
            "Use only one foundation at a time unless the bass lines are deliberately separated by section or register.",
        ))
    uncertain = [profile.title for profile in plan.profiles if profile.melody_extraction_confidence < 45]
    if uncertain:
        conflicts.append(ArrangementConflict(
            "Low",
            "Some melody roles are uncertain",
            "Low-confidence role extraction: " + ", ".join(uncertain[:3]) + ("…" if len(uncertain) > 3 else ""),
            "Prefer whole-section recipes or inspect track mapping in Creative Workbench.",
        ))
    if not conflicts:
        conflicts.append(ArrangementConflict(
            "Low",
            "No major automatic conflict found",
            "The measured role, tempo, meter, and harmony checks did not identify a release-blocking arrangement conflict.",
            "Human listening is still required; preparation confidence is not an artistic guarantee.",
        ))
    severity_order = {"High": 0, "Medium": 1, "Low": 2}
    unique: dict[tuple[str, str], ArrangementConflict] = {}
    for conflict in conflicts:
        unique[(conflict.title, conflict.summary)] = conflict
    return tuple(sorted(unique.values(), key=lambda item: (severity_order.get(item.severity, 3), item.title)))


def _preset_order(source_count: int, preset_key: str, lead_index: int, foundation_index: int) -> tuple[int, ...]:
    indices = list(range(max(1, source_count)))
    extras = [index for index in indices if index not in {lead_index, foundation_index}]
    if preset_key == "melody":
        return (lead_index, lead_index, foundation_index, lead_index, lead_index, foundation_index)
    if preset_key == "sections":
        rotation = [lead_index, foundation_index, *extras] or [0]
        return tuple(rotation[index % len(rotation)] for index in range(len(SECTION_NAMES)))
    if preset_key == "collage":
        middle = extras[0] if extras else foundation_index
        finale = lead_index
        return (foundation_index, lead_index, middle, foundation_index, lead_index, finale)
    balanced = [lead_index, foundation_index, *extras] or [0]
    return tuple(balanced[index % len(balanced)] for index in range(len(SECTION_NAMES)))


def build_arrangement_blueprint(plan: AssistedSetPlan, preset_key: str = "balanced") -> ArrangementBlueprint:
    if not plan.profiles:
        return ArrangementBlueprint(preset_key=preset_key, sections=())
    lead_index = next((index for index, role in enumerate(plan.source_roles) if role == "lead melody"), 0)
    foundation_index = next((index for index, role in enumerate(plan.source_roles) if role == "foundation"), 1 if len(plan.profiles) > 1 else 0)
    order = _preset_order(len(plan.profiles), preset_key, lead_index, foundation_index)
    purposes = {
        "Intro": "establish a recognizable identity",
        "Verse": "set the main groove and foundation",
        "Chorus": "present the strongest hook",
        "Bridge": "create contrast without overcrowding",
        "Build": "increase energy or add a new source",
        "Finale": "resolve with a clear lead and controlled stack",
    }
    density_by_preset = {
        "balanced": ("Lean", "Balanced", "Balanced", "Lean", "Balanced", "Full"),
        "melody": ("Lean", "Balanced", "Balanced", "Lean", "Balanced", "Balanced"),
        "sections": ("Lean", "Balanced", "Balanced", "Lean", "Balanced", "Balanced"),
        "collage": ("Lean", "Balanced", "Balanced", "Balanced", "Full", "Full"),
    }
    densities = density_by_preset.get(preset_key, density_by_preset["balanced"])
    sections = tuple(
        SectionAssignment(
            name=name,
            source_index=source_index,
            source_title=plan.profiles[source_index].title,
            purpose=purposes[name],
            density=densities[index],
        )
        for index, (name, source_index) in enumerate(zip(SECTION_NAMES, order))
    )
    return ArrangementBlueprint(preset_key=preset_key, sections=sections)


def blueprint_with_source_order(
    blueprint: ArrangementBlueprint,
    plan: AssistedSetPlan,
    source_order: Sequence[int],
) -> ArrangementBlueprint:
    if not blueprint.sections or not plan.profiles:
        return blueprint
    sections: list[SectionAssignment] = []
    for index, section in enumerate(blueprint.sections):
        requested = int(source_order[index]) if index < len(source_order) else section.source_index
        source_index = max(0, min(len(plan.profiles) - 1, requested))
        sections.append(SectionAssignment(
            name=section.name,
            source_index=source_index,
            source_title=plan.profiles[source_index].title,
            purpose=section.purpose,
            density=section.density,
        ))
    return ArrangementBlueprint(preset_key=blueprint.preset_key, sections=tuple(sections))
