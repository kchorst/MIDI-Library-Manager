from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from core.atomic_io import atomic_write_json
import json
import random
from typing import Sequence

import mido

import core.remix_engine as pair_engine
from core.remix_engine import RemixTake
from core.remix_profile import MidiProfile, assess_pair, key_name


@dataclass(frozen=True)
class MultiSourceRecipe:
    key: str
    label: str
    description: str
    minimum_sources: int = 2


MULTI_SOURCE_RECIPES: tuple[MultiSourceRecipe, ...] = (
    MultiSourceRecipe(
        "melody-relay",
        "Melody relay",
        "Each source takes a turn carrying the melody over a shared foundation.",
    ),
    MultiSourceRecipe(
        "rotating-sections",
        "Rotating sections",
        "Complete sections rotate through the source set while the target pulse and key stay unified.",
    ),
    MultiSourceRecipe(
        "progressive-build",
        "Progressive build",
        "Begin with the current focus source, then introduce additional sources one by one toward a denser finish.",
        3,
    ),
    MultiSourceRecipe(
        "role-collage",
        "Role collage",
        "Use designated melody, foundation, hook, and section sources as separate arrangement roles.",
        3,
    ),
    MultiSourceRecipe(
        "source-journey",
        "Source journey",
        "Move from the current focus through the selected sources and return to a hybrid conclusion.",
        3,
    ),
    MultiSourceRecipe(
        "finale-stack",
        "Combined finale",
        "Rotate sources through the body, then bring several recognizable upper parts together for the ending.",
        3,
    ),
    MultiSourceRecipe(
        "anchor-transformations",
        "Focus-source transformations",
        "Keep the current focus melody recognizable while successive sources replace its foundation.",
        2,
    ),
)

DEFAULT_MULTI_SOURCE_RECIPE_KEYS: tuple[str, ...] = (
    "melody-relay",
    "rotating-sections",
    "anchor-transformations",
)


@dataclass(frozen=True)
class SourceSpec:
    path: str
    role: str = "auto"
    start_bar: int = 0
    melody_treatment: str = "full"
    enabled: bool = True
    volume_percent: int = 100
    octave_shift: int = 0
    melody_track_index: int | None = None
    foundation_track_index: int | None = None


@dataclass(frozen=True)
class MultiSourceSettings:
    target_bpm: int = 110
    target_key_pc: int | None = None
    target_mode: str = "major"
    mode_handling: str = "Preserve source mode"
    bars: int = 64
    section_bars: int = 8
    density: str = "Balanced"
    recipes: tuple[str, ...] = DEFAULT_MULTI_SOURCE_RECIPE_KEYS
    variations_per_recipe: int = 1
    variation_strength: str = "Moderate"
    random_seed: int = 0
    auto_balance: bool = True
    preserve_signature_assets: bool = True
    signature_source: str = "auto"
    signature_placement: str = "intro-alone"
    # Assisted section planner order. Each value is a source index for the next
    # named arrangement section; empty preserves the recipe default.
    section_source_order: tuple[int, ...] = ()
    section_labels: tuple[str, ...] = ()


PITCHED_CHANNELS = tuple(channel for channel in range(16) if channel != 9)


def _role_capable(profile: MidiProfile) -> bool:
    """Return whether the engine can derive lead/foundation material.

    Distinct tracks are ideal. A piano reduction may deliberately use the same
    polyphonic track twice: top-voice extraction for lead and the complete
    texture for foundation. Files with no detected pitched role are limited to
    whole-section recipes.
    """
    return profile.melody_track_index is not None and profile.accompaniment_track_index is not None


def _channel(index: int, offset: int = 0) -> int:
    return PITCHED_CHANNELS[(index * 2 + offset) % len(PITCHED_CHANNELS)]


def _safe_role(role: str) -> str:
    value = str(role or "auto").strip().lower()
    return value if value in {"auto", "anchor", "melody", "foundation", "hook", "section"} else "auto"


def recommended_multi_source_recipes(profiles: Sequence[MidiProfile]) -> dict[str, str]:
    """Return measurable reasons for suggested recipes.

    These are workflow recommendations, not artistic scores.
    """
    count = len(profiles)
    if count < 2:
        return {}
    meters = {profile.meter_family for profile in profiles}
    harmonic = []
    for index in range(1, count):
        assessment = assess_pair(profiles[0], profiles[index])
        harmonic.append(assessment.harmonic_score)
    average_harmony = sum(harmonic) / max(1, len(harmonic))
    reasons: dict[str, str] = {
        "melody-relay": "Audible source identity with only one lead at a time; useful when upper parts are clear.",
        "rotating-sections": "Preserves complete source textures and requires less simultaneous harmonic agreement.",
        "anchor-transformations": "Tests the current focus melody against several foundations instead of committing to one pairing.",
    }
    if count >= 3:
        reasons["source-journey"] = "Uses several sources sequentially, so contrasting files can contribute without constant layering."
        reasons["role-collage"] = "Useful when the source tray includes distinct melody, foundation, hook, or section roles."
        if len(meters) <= 2:
            reasons["progressive-build"] = "The selected sources have manageable meter relationships for staged entrances."
        if average_harmony >= 48:
            reasons["finale-stack"] = "Several focus-source relationships have enough harmonic overlap to test a short combined ending."
    return reasons


def _resolve_specs(profiles: Sequence[MidiProfile], specs: Sequence[SourceSpec] | None) -> list[tuple[MidiProfile, SourceSpec]]:
    by_path = {profile.path: profile for profile in profiles}
    if specs:
        result = [(by_path[spec.path], replace(spec, role=_safe_role(spec.role))) for spec in specs if spec.enabled and spec.path in by_path]
    else:
        result = [(profile, SourceSpec(path=profile.path, role="anchor" if index == 0 else "auto")) for index, profile in enumerate(profiles)]
    if len(result) < 2:
        raise ValueError("Choose at least two enabled MIDI sources.")
    anchor_positions = [index for index, (_, spec) in enumerate(result) if spec.role == "anchor"]
    if anchor_positions and anchor_positions[0] != 0:
        anchor_item = result.pop(anchor_positions[0])
        result.insert(0, anchor_item)
    return result


def _variation(settings: MultiSourceSettings, profiles: Sequence[MidiProfile], variation: int) -> MultiSourceSettings:
    if variation <= 1:
        return replace(settings, variations_per_recipe=1)
    seed = int(settings.random_seed or 0) + variation * 3571 + sum(sum(ord(ch) for ch in profile.title) for profile in profiles)
    rng = random.Random(seed)
    strength = str(settings.variation_strength or "Moderate").lower()
    spread = 1 if strength == "low" else 2 if strength == "moderate" else 4
    section = max(2, min(int(settings.bars), int(settings.section_bars) + rng.choice([-2, 0, 2]) * spread))
    density = str(settings.density) if strength == "low" else rng.choice(["Lean", "Balanced", "Full"])
    return replace(settings, section_bars=section, density=density, variations_per_recipe=1)


def generate_multi_source_takes(
    profiles: Sequence[MidiProfile],
    output_directory: str | Path,
    settings: MultiSourceSettings,
    source_specs: Sequence[SourceSpec] | None = None,
) -> list[RemixTake]:
    resolved = _resolve_specs(profiles, source_specs)
    profiles_ordered = [item[0] for item in resolved]
    specs_ordered = [item[1] for item in resolved]
    selected = [recipe for recipe in settings.recipes if recipe in {item.key for item in MULTI_SOURCE_RECIPES}]
    if not selected:
        raise ValueError("Choose at least one arrangement recipe.")
    count = max(1, min(12, int(settings.variations_per_recipe or 1)))
    results: list[RemixTake] = []

    role_dependent = {
        "melody-relay", "anchor-transformations", "progressive-build",
        "role-collage", "finale-stack",
    }
    unavailable = [recipe for recipe in selected if recipe in role_dependent and not all(_role_capable(profile) for profile in profiles_ordered)]
    if unavailable:
        labels = ", ".join(recipe.replace("-", " ") for recipe in unavailable)
        problem_sources = ", ".join(profile.title for profile in profiles_ordered if not _role_capable(profile))
        raise ValueError(
            f"{labels} requires clearly separated melody and foundation roles. "
            f"Use rotating sections or source journey for {problem_sources}, or assign tracks/voice extraction manually."
        )
    for variation_number in range(1, count + 1):
        varied = _variation(settings, profiles_ordered, variation_number)
        results.extend(
            _generate_once(
                profiles_ordered,
                specs_ordered,
                Path(output_directory),
                varied,
                selected,
                take_offset=len(results),
                variation_number=variation_number,
            )
        )
    manifest = {
        "format": "MIDI Remixer multi-source take manifest",
        "version": 4,
        "settings": asdict(settings),
        "sources": [profile.to_dict() for profile in profiles_ordered],
        "source_specs": [asdict(spec) for spec in specs_ordered],
        "takes": [take.to_dict() for take in results],
    }
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    prefix = pair_engine._safe_slug("-".join(profile.title for profile in profiles_ordered[:4]))
    atomic_write_json(output / f"{prefix}-multi-source-manifest.json", manifest)
    return results


def _generate_once(
    profiles: Sequence[MidiProfile],
    specs: Sequence[SourceSpec],
    output_directory: Path,
    settings: MultiSourceSettings,
    selected_recipes: Sequence[str],
    *,
    take_offset: int,
    variation_number: int,
) -> list[RemixTake]:
    pair_engine._AUTO_BALANCE_ENABLED = bool(settings.auto_balance)
    anchor = profiles[0]
    pair_engine._MODE_HANDLING = str(settings.mode_handling)
    pair_engine._TARGET_TONIC_PC = anchor.tonic_pc if settings.target_key_pc is None else settings.target_key_pc
    pair_engine._TARGET_MODE = str(settings.target_mode)
    midis = [mido.MidiFile(profile.path) for profile in profiles]
    target_pc = anchor.tonic_pc if settings.target_key_pc is None else settings.target_key_pc
    transposes = [pair_engine._profile_transpose(profile, target_pc) + int(spec.octave_shift) * 12 for profile, spec in zip(profiles, specs)]
    tpb = 480
    bar_ticks = pair_engine._target_bar_ticks(anchor, tpb)
    bars = max(4, int(settings.bars))
    total_ticks = bars * bar_ticks
    section_bars = max(2, min(int(settings.section_bars), bars))
    density = str(settings.density or "Balanced")
    effective_mode = settings.target_mode if settings.mode_handling == "Adapt diatonic notes to target mode" else anchor.mode
    target_key = key_name(target_pc, effective_mode)
    encoded_target_bpm = pair_engine.encoded_bpm_for_felt(anchor, settings.target_bpm)
    source_paths = tuple(profile.path for profile in profiles)
    source_names = [profile.title for profile in profiles]
    requested_order = tuple(
        index for index in settings.section_source_order
        if isinstance(index, int) and 0 <= int(index) < len(profiles)
    )
    section_order = requested_order or tuple(range(len(profiles)))
    prefix = pair_engine._safe_slug("-".join(source_names[:4]))
    results: list[RemixTake] = []

    signature_index: int | None = None
    signature_asset: dict | None = None
    signature_events: list[pair_engine._TimedMessage] = []
    signature_bars = 0
    if settings.preserve_signature_assets and str(settings.signature_placement).casefold() != "none":
        requested = str(settings.signature_source or "auto").casefold()
        candidates: list[tuple[int, dict]] = []
        for index, profile in enumerate(profiles):
            asset = pair_engine.primary_signature_asset(profile)
            if asset and asset.get("protect_default"):
                candidates.append((index, asset))
        if requested.isdigit():
            wanted = int(requested)
            candidates = [item for item in candidates if item[0] == wanted]
        elif requested == "foundation":
            foundation_indices = {i for i, spec in enumerate(specs) if spec.role == "foundation"}
            filtered = [item for item in candidates if item[0] in foundation_indices]
            candidates = filtered or candidates
        if candidates:
            signature_index, signature_asset = max(candidates, key=lambda item: int(item[1].get("confidence", 0)))
            signature_events, signature_bars = pair_engine._signature_events(
                midis[signature_index], profiles[signature_index], signature_asset,
                bar_ticks=bar_ticks, transpose=transposes[signature_index],
            )

    def _effective_source_start(index: int, requested_start: int) -> int:
        if signature_index == index and signature_asset:
            return max(int(requested_start), int(signature_asset.get("end_bar", requested_start)))
        return int(requested_start)

    def source_events(index: int, role: str, output_bar: int, length_bars: int, *, source_bar: int | None = None, velocity: float = 1.0, octave: int = 0, voice_mode: str | None = None) -> list[pair_engine._TimedMessage]:
        profile = profiles[index]
        spec = specs[index]
        track_index = profile.melody_track_index if role == "melody" else profile.accompaniment_track_index
        assert track_index is not None
        start = _effective_source_start(index, int(spec.start_bar if source_bar is None else source_bar))
        selected_voice_mode = voice_mode or (spec.melody_treatment if role == "melody" else "full")
        if role == "melody" and profile.melody_track_index == profile.accompaniment_track_index and selected_voice_mode == "full":
            selected_voice_mode = "top"
        return pair_engine._source_track_events(
            midis[index], profile, track_index, start, max(1, length_bars),
            output_bar * bar_ticks, max(1, length_bars) * bar_ticks,
            transposes[index] + octave * 12,
            velocity * max(0.2, min(2.0, int(spec.volume_percent) / 100.0)),
            _channel(index, 0 if role == "melody" else 1),
            selected_voice_mode,
        )

    def whole_source_events(index: int, output_bar: int, length_bars: int, *, source_bar: int | None = None, velocity: float = 1.0, octave: int = 0) -> list[pair_engine._TimedMessage]:
        """Preserve a complete source texture when role separation is uncertain."""
        profile = profiles[index]
        spec = specs[index]
        start = _effective_source_start(index, int(spec.start_bar if source_bar is None else source_bar))
        return pair_engine._source_all_track_events(
            midis[index], profile, start, max(1, length_bars),
            output_bar * bar_ticks, max(1, length_bars) * bar_ticks,
            transposes[index] + octave * 12,
            velocity * max(0.2, min(2.0, int(spec.volume_percent) / 100.0)),
            _channel(index),
        )

    def add_take(label: str, description: str, recipe: str, tracks: Sequence[tuple[str, list[pair_engine._TimedMessage]]], end_tick: int = total_ticks) -> None:
        number = take_offset + len(results) + 1
        display = label if variation_number == 1 else f"{label} — Variation {variation_number}"
        detail = description if variation_number == 1 else f"{description} Variation {variation_number} changes block size, entry position, or density within the chosen recipe."
        final_tracks = list(tracks)
        final_end_tick = end_tick
        signature_summary: tuple[str, ...] = ()
        if signature_events and signature_asset is not None and signature_index is not None:
            pre_roll_ticks = signature_bars * bar_ticks
            final_tracks = [(f"{signature_asset.get('label', 'Protected signature opening')} — {profiles[signature_index].title}", signature_events)] + [
                (name, pair_engine._shift_events(events, pre_roll_ticks)) for name, events in final_tracks
            ]
            final_end_tick += pre_roll_ticks
            detail += (
                f" {profiles[signature_index].title}'s {str(signature_asset.get('label', 'signature opening')).casefold()} "
                f"plays alone for {signature_bars} bar(s) before the multi-source arrangement enters."
            )
            summary_items = [
                f"Protected signature asset: {profiles[signature_index].title} bars {int(signature_asset.get('start_bar', 0)) + 1}-{int(signature_asset.get('end_bar', 0))}.",
                "Other sources enter only after the protected opening completes.",
            ]
            if str(settings.signature_placement).casefold() in {"intro-and-outro", "opening-and-outro"}:
                final_tracks.append((
                    f"{signature_asset.get('label', 'Protected signature return')} return — {profiles[signature_index].title}",
                    pair_engine._shift_events(signature_events, final_end_tick),
                ))
                final_end_tick += pre_roll_ticks
                detail += " The protected passage returns once as an outro after the multi-source arrangement."
                summary_items.append("Signature return: repeated once as an outro; it is not looped continuously.")
            signature_summary = tuple(summary_items)
        path = pair_engine._take_path(output_directory, prefix, number, display)
        pair_engine._write_take(path, anchor, pair_engine.RemixSettings(target_bpm=settings.target_bpm, target_key_pc=settings.target_key_pc, target_mode=settings.target_mode, mode_handling=settings.mode_handling, bars=bars, section_bars=section_bars, density=density, auto_balance=settings.auto_balance, preserve_signature_assets=False), display, source_names, final_tracks, final_end_tick)
        results.append(RemixTake(
            take_id=f"take-{number:02d}", name=display, description=detail, path=str(path.resolve()),
            source_a=profiles[0].path, source_b=profiles[1].path,
            target_bpm=int(settings.target_bpm), target_key=target_key,
            bars=max(1, int(round(final_end_tick / bar_ticks))), kind=recipe,
            encoded_bpm=round(encoded_target_bpm, 3), variation=variation_number,
            source_paths=source_paths, recipe=recipe, decision_summary=(
                f"Arrangement recipe: {recipe.replace('-', ' ')}.",
                *(
                    ("Section leads: " + " → ".join(source_names[index] for index in section_order) + ".",)
                    if requested_order else ()
                ),
                *signature_summary,
            ),
        ))

    if "melody-relay" in selected_recipes:
        foundation_index = next((index for index, spec in enumerate(specs) if spec.role == "foundation"), 0)
        foundation = source_events(foundation_index, "foundation", 0, bars, velocity=0.86)
        relay_by_source: list[list[pair_engine._TimedMessage]] = [[] for _ in profiles]
        cursor = 0
        source_index = 0
        while cursor < bars:
            length = min(section_bars, bars - cursor)
            index = section_order[source_index % len(section_order)]
            relay_by_source[index] += source_events(index, "melody", cursor, length, source_bar=specs[index].start_bar + cursor, velocity=1.02)
            cursor += length
            source_index += 1
        tracks: list[tuple[str, list[pair_engine._TimedMessage]]] = [(f"Shared foundation — {profiles[foundation_index].title}", foundation)]
        tracks.extend((f"Relay voice — {profile.title}", events) for profile, events in zip(profiles, relay_by_source) if events)
        add_take("Melody relay", "Each selected source carries one melodic block over a shared foundation, making source changes easy to hear and compare.", "melody-relay", tracks)

    if "rotating-sections" in selected_recipes:
        tracks_by_source: list[list[pair_engine._TimedMessage]] = [[] for _ in profiles]
        cursor = 0
        source_index = 0
        while cursor < bars:
            length = min(section_bars, bars - cursor)
            index = section_order[source_index % len(section_order)]
            if _role_capable(profiles[index]):
                tracks_by_source[index] += source_events(index, "melody", cursor, length, source_bar=specs[index].start_bar + cursor, velocity=1.0)
                tracks_by_source[index] += source_events(index, "foundation", cursor, length, source_bar=specs[index].start_bar + cursor, velocity=0.92)
            else:
                tracks_by_source[index] += whole_source_events(index, cursor, length, source_bar=specs[index].start_bar + cursor, velocity=0.92)
            cursor += length
            source_index += 1
        add_take("Rotating sections", "Complete source textures rotate in blocks, preserving each source before the next one enters.", "rotating-sections", [(f"Sections — {profile.title}", events) for profile, events in zip(profiles, tracks_by_source) if events])

    if "anchor-transformations" in selected_recipes:
        tracks: list[tuple[str, list[pair_engine._TimedMessage]]] = [(f"Anchor melody — {anchor.title}", source_events(0, "melody", 0, bars, velocity=1.03))]
        foundations: list[list[pair_engine._TimedMessage]] = [[] for _ in profiles]
        cursor = 0
        foundation_indices = list(range(1, len(profiles))) or [0]
        block = 0
        while cursor < bars:
            length = min(section_bars, bars - cursor)
            index = foundation_indices[block % len(foundation_indices)]
            foundations[index] += source_events(index, "foundation", cursor, length, source_bar=specs[index].start_bar + cursor, velocity=0.88)
            cursor += length
            block += 1
        tracks.extend((f"Changing foundation — {profiles[index].title}", events) for index, events in enumerate(foundations) if events)
        add_take("Focus-source transformations", "The current focus melody remains recognizable while foundations from the other selected sources replace one another.", "anchor-transformations", tracks)

    if "progressive-build" in selected_recipes and len(profiles) >= 3:
        tracks: list[tuple[str, list[pair_engine._TimedMessage]]] = [(f"Anchor foundation — {anchor.title}", source_events(0, "foundation", 0, bars, velocity=0.84))]
        entrance_gap = max(2, bars // max(2, len(profiles) + 1))
        progressive_order = tuple(dict.fromkeys(section_order)) + tuple(
            index for index in range(len(profiles)) if index not in section_order
        )
        for position, index in enumerate(progressive_order):
            profile = profiles[index]
            start = min(bars - 1, position * entrance_gap)
            length = max(1, bars - start)
            octave = -1 if position % 3 == 2 else 1 if position % 3 == 1 and density == "Full" else 0
            velocity = 1.04 if position == 0 else 0.70 if density == "Lean" else 0.78
            tracks.append((f"Progressive voice — {profile.title}", source_events(index, "melody", start, length, velocity=velocity, octave=octave)))
        add_take("Progressive build", "The current focus begins alone and additional voices enter in stages. Later sources are balanced below the principal lead unless Full density is selected.", "progressive-build", tracks)

    if "role-collage" in selected_recipes and len(profiles) >= 3:
        melody_index = next((i for i, spec in enumerate(specs) if spec.role in {"anchor", "melody"}), 0)
        foundation_index = next((i for i, spec in enumerate(specs) if spec.role == "foundation" and i != melody_index), 1 if len(profiles) > 1 else 0)
        tracks: list[tuple[str, list[pair_engine._TimedMessage]]] = [
            (f"Lead — {profiles[melody_index].title}", source_events(melody_index, "melody", 0, bars, velocity=1.04)),
            (f"Foundation — {profiles[foundation_index].title}", source_events(foundation_index, "foundation", 0, bars, velocity=0.86)),
        ]
        extras = [i for i in range(len(profiles)) if i not in {melody_index, foundation_index}]
        for position, index in enumerate(extras):
            hook_events: list[pair_engine._TimedMessage] = []
            block = max(2, min(4, section_bars))
            cursor = (position + 1) * block
            while cursor < bars:
                length = min(block, bars - cursor)
                role = "foundation" if specs[index].role == "foundation" else "melody"
                hook_events += source_events(index, role, cursor, length, source_bar=specs[index].start_bar + cursor, velocity=0.58 if role == "melody" else 0.68, octave=-1 if position % 2 else 0, voice_mode="top" if role == "melody" else "full")
                cursor += block * max(2, len(extras))
            tracks.append((f"Hook/colour — {profiles[index].title}", hook_events))
        add_take("Role collage", "One source leads, another supplies the foundation, and additional sources appear as short hooks or colour rather than playing continuously.", "role-collage", tracks)

    if "source-journey" in selected_recipes and len(profiles) >= 3:
        tracks_by_source: list[list[pair_engine._TimedMessage]] = [[] for _ in profiles]
        segment = max(2, bars // (len(profiles) + 1))
        cursor = 0
        order = list(section_order) + [section_order[0]]
        for position, index in enumerate(order):
            if cursor >= bars:
                break
            length = min(segment if position < len(order) - 1 else bars - cursor, bars - cursor)
            if _role_capable(profiles[index]):
                tracks_by_source[index] += source_events(index, "melody", cursor, length, source_bar=specs[index].start_bar + cursor, velocity=1.0)
                tracks_by_source[index] += source_events(index, "foundation", cursor, length, source_bar=specs[index].start_bar + cursor, velocity=0.91)
            else:
                tracks_by_source[index] += whole_source_events(index, cursor, length, source_bar=specs[index].start_bar + cursor, velocity=0.92)
            cursor += length
        add_take("Source journey", "The arrangement travels through the selected source set and returns to the current focus for a clear large-scale path.", "source-journey", [(f"Journey — {profile.title}", events) for profile, events in zip(profiles, tracks_by_source) if events])

    if "finale-stack" in selected_recipes and len(profiles) >= 3:
        finale_bars = max(section_bars, min(bars // 3, 16))
        body_bars = max(1, bars - finale_bars)
        tracks_by_source: list[list[pair_engine._TimedMessage]] = [[] for _ in profiles]
        cursor = 0
        index = 0
        while cursor < body_bars:
            length = min(section_bars, body_bars - cursor)
            source_index = section_order[index % len(section_order)]
            tracks_by_source[source_index] += source_events(source_index, "melody", cursor, length, source_bar=specs[source_index].start_bar + cursor, velocity=1.0)
            tracks_by_source[source_index] += source_events(source_index, "foundation", cursor, length, source_bar=specs[source_index].start_bar + cursor, velocity=0.90)
            cursor += length
            index += 1
        finale_start = body_bars
        finale_lead = section_order[-1]
        foundation_index = next((i for i, spec in enumerate(specs) if spec.role == "foundation"), section_order[0])
        tracks_by_source[foundation_index] += source_events(foundation_index, "foundation", finale_start, finale_bars, source_bar=specs[foundation_index].start_bar + finale_start, velocity=0.82)
        finale_order = (finale_lead,) + tuple(index for index in dict.fromkeys(reversed(section_order)) if index != finale_lead)
        max_finale_voices = min(5, len(finale_order))
        for position, source_index in enumerate(finale_order[:max_finale_voices]):
            octave = (-1 if position % 3 == 2 else 1 if position % 3 == 1 else 0)
            velocity = 1.0 if position == 0 else 0.54 + (0.05 if density == "Full" else 0.0)
            tracks_by_source[source_index] += source_events(source_index, "melody", finale_start, finale_bars, source_bar=specs[source_index].start_bar + finale_start, velocity=velocity, octave=octave, voice_mode="top")
        add_take("Combined finale", "The body rotates through sources; the final section layers a limited number of extracted upper voices over the current focus foundation to avoid an uncontrolled wall of notes.", "finale-stack", [(f"Finale source — {profile.title}", events) for profile, events in zip(profiles, tracks_by_source) if events])

    return results
