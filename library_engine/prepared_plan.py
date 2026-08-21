from __future__ import annotations

"""Stable JSON-safe prepared-plan snapshots shared by Engine and Remixer.

The Library Engine performs expensive deterministic pair validation once.  These
snapshots let the interactive Remixer reconstruct the exact validated plan from
committed results without re-running musical pair analysis.
"""

from dataclasses import asdict
from typing import Any, Mapping, Sequence

from core.assisted_studio import AssistedSetPlan
from core.guided_beginner import GuidedPairPlan
from core.remix_profile import MidiProfile, PairAssessment


def _assessment_to_dict(value: PairAssessment) -> dict[str, Any]:
    return asdict(value)


def _assessment_from_dict(data: Mapping[str, Any]) -> PairAssessment:
    values = dict(data)
    values["observations"] = list(values.get("observations") or [])
    for key in ("reference_evidence", "reference_sources", "reference_urls"):
        values[key] = tuple(values.get(key) or ())
    return PairAssessment(**values)


def guided_plan_snapshot(plan: GuidedPairPlan) -> dict[str, Any]:
    return {
        "kind": "guided_pair_v1",
        "assessment": _assessment_to_dict(plan.assessment),
        "category": plan.category,
        "headline": plan.headline,
        "summary": plan.summary,
        "reasons": list(plan.reasons),
        "transformations": list(plan.transformations),
        "likely_heard": list(plan.likely_heard),
        "recipe_keys": list(plan.recipe_keys),
        "target_bpm": int(plan.target_bpm),
        "target_key_pc": plan.target_key_pc,
        "target_mode": plan.target_mode,
        "bars": int(plan.bars),
        "section_bars": int(plan.section_bars),
        "safety_score": int(plan.safety_score),
        "start_bar_a": int(plan.start_bar_a),
        "start_bar_b": int(plan.start_bar_b),
        "window_harmony_score": int(plan.window_harmony_score),
        "window_melody_a_over_b_score": int(plan.window_melody_a_over_b_score),
        "window_melody_b_over_a_score": int(plan.window_melody_b_over_a_score),
        "window_structure_score": int(plan.window_structure_score),
        "lead_source": plan.lead_source,
        "backing_source": plan.backing_source,
        "contribution_plan": list(plan.contribution_plan),
        "signature_plan": list(plan.signature_plan),
        "signature_source": plan.signature_source,
        "signature_asset": dict(plan.signature_asset) if plan.signature_asset else None,
    }


def guided_plan_from_snapshot(data: Mapping[str, Any], a: MidiProfile, b: MidiProfile) -> GuidedPairPlan | None:
    if str(data.get("kind") or "") != "guided_pair_v1":
        return None
    assessment_data = data.get("assessment")
    if not isinstance(assessment_data, Mapping):
        return None
    try:
        assessment = _assessment_from_dict(assessment_data)
        return GuidedPairPlan(
            source_a=a, source_b=b, assessment=assessment,
            category=str(data.get("category") or "Prepared mash relationship"),
            headline=str(data.get("headline") or f"{a.title} + {b.title}"),
            summary=str(data.get("summary") or ""),
            reasons=tuple(data.get("reasons") or ()),
            transformations=tuple(data.get("transformations") or ()),
            likely_heard=tuple(data.get("likely_heard") or ()),
            recipe_keys=tuple(data.get("recipe_keys") or ()),
            target_bpm=int(data.get("target_bpm") or 120),
            target_key_pc=(int(data["target_key_pc"]) if data.get("target_key_pc") is not None else None),
            target_mode=str(data.get("target_mode") or "major"),
            bars=int(data.get("bars") or 32), section_bars=int(data.get("section_bars") or 8),
            safety_score=int(data.get("safety_score") or 0),
            start_bar_a=int(data.get("start_bar_a") or 0), start_bar_b=int(data.get("start_bar_b") or 0),
            window_harmony_score=int(data.get("window_harmony_score") or 0),
            window_melody_a_over_b_score=int(data.get("window_melody_a_over_b_score") or 0),
            window_melody_b_over_a_score=int(data.get("window_melody_b_over_a_score") or 0),
            window_structure_score=int(data.get("window_structure_score") or 0),
            lead_source=str(data.get("lead_source") or "a"), backing_source=str(data.get("backing_source") or "b"),
            contribution_plan=tuple(data.get("contribution_plan") or ()),
            signature_plan=tuple(data.get("signature_plan") or ()),
            signature_source=(str(data.get("signature_source")) if data.get("signature_source") else None),
            signature_asset=(dict(data.get("signature_asset")) if isinstance(data.get("signature_asset"), Mapping) else None),
        )
    except (KeyError, TypeError, ValueError):
        return None


def assisted_plan_snapshot(plan: AssistedSetPlan) -> dict[str, Any]:
    return {
        "kind": "assisted_pair_v1",
        "headline": plan.headline,
        "category": plan.category,
        "summary": plan.summary,
        "reasons": list(plan.reasons),
        "warnings": list(plan.warnings),
        "source_roles": list(plan.source_roles),
        "recipe_keys": list(plan.recipe_keys),
        "target_bpm": int(plan.target_bpm),
        "target_key_pc": plan.target_key_pc,
        "target_mode": plan.target_mode,
        "bars": int(plan.bars),
        "section_bars": int(plan.section_bars),
        "confidence": int(plan.confidence),
        "pair_assessments": [_assessment_to_dict(item) for item in plan.pair_assessments],
        "contribution_plan": list(plan.contribution_plan),
        "signature_plan": list(plan.signature_plan),
        "origin": plan.origin,
        "discovery_score": int(plan.discovery_score),
        "discovery_feedback_score": int(plan.discovery_feedback_score),
        "discovery_labels": list(plan.discovery_labels),
        "discovery_providers": list(plan.discovery_providers),
    }


def assisted_plan_from_snapshot(
    data: Mapping[str, Any], profiles: Sequence[MidiProfile]
) -> AssistedSetPlan | None:
    if str(data.get("kind") or "") != "assisted_pair_v1" or len(profiles) < 2:
        return None
    assessments_raw = data.get("pair_assessments") or ()
    try:
        assessments = tuple(
            _assessment_from_dict(item) for item in assessments_raw if isinstance(item, Mapping)
        )
        if not assessments:
            return None
        return AssistedSetPlan(
            profiles=tuple(profiles),
            headline=str(data.get("headline") or " + ".join(profile.title for profile in profiles)),
            category=str(data.get("category") or "Prepared mash relationship"),
            summary=str(data.get("summary") or ""), reasons=tuple(data.get("reasons") or ()),
            warnings=tuple(data.get("warnings") or ()), source_roles=tuple(data.get("source_roles") or ()),
            recipe_keys=tuple(data.get("recipe_keys") or ()), target_bpm=int(data.get("target_bpm") or 120),
            target_key_pc=(int(data["target_key_pc"]) if data.get("target_key_pc") is not None else None),
            target_mode=str(data.get("target_mode") or "major"), bars=int(data.get("bars") or 32),
            section_bars=int(data.get("section_bars") or 8), confidence=int(data.get("confidence") or 0),
            pair_assessments=assessments, contribution_plan=tuple(data.get("contribution_plan") or ()),
            signature_plan=tuple(data.get("signature_plan") or ()), origin=str(data.get("origin") or "assisted"),
            discovery_score=int(data.get("discovery_score") or 0),
            discovery_feedback_score=int(data.get("discovery_feedback_score") or 0),
            discovery_labels=tuple(data.get("discovery_labels") or ()),
            discovery_providers=tuple(data.get("discovery_providers") or ()),
        )
    except (KeyError, TypeError, ValueError):
        return None
