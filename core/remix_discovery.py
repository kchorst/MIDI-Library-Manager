from __future__ import annotations

"""Session-owned mash-idea previews and saved discovery metadata."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from core.assisted_studio import AssistedSetPlan
from core.atomic_io import atomic_write_json
from core.multi_source import (
    MULTI_SOURCE_RECIPES,
    MultiSourceSettings,
    SourceSpec,
    generate_multi_source_takes,
)
from core.remix_engine import (
    TAKE_STRATEGIES,
    RemixSettings,
    RemixTake,
    generate_remix_takes,
    remix_take_from_dict,
)


def discovery_plan_id(plan: AssistedSetPlan) -> str:
    material = {
        "sources": [
            {
                "path": str(Path(profile.path).resolve(strict=False)),
                "fingerprint": str(profile.content_fingerprint or profile.fingerprint or ""),
            }
            for profile in plan.profiles
        ],
        "headline": plan.headline,
        "category": plan.category,
        "bpm": int(plan.target_bpm),
        "key": plan.target_key_pc,
        "mode": plan.target_mode,
        "recipes": list(plan.recipe_keys),
        "roles": list(plan.source_roles),
        "origin": plan.origin,
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def discovery_idea_payload(plan: AssistedSetPlan, *, preview_bars: int = 8) -> dict[str, object]:
    return {
        "idea_id": discovery_plan_id(plan),
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "headline": plan.headline,
        "category": plan.category,
        "summary": plan.summary,
        "source_paths": [profile.path for profile in plan.profiles],
        "source_titles": list(plan.source_titles),
        "source_roles": list(plan.source_roles),
        "recipe_keys": list(plan.recipe_keys),
        "target_bpm": int(plan.target_bpm),
        "target_key_pc": plan.target_key_pc,
        "target_mode": plan.target_mode,
        "confidence": int(plan.confidence),
        "origin": plan.origin,
        "discovery_score": int(plan.discovery_score),
        "discovery_labels": list(plan.discovery_labels),
        "discovery_providers": list(plan.discovery_providers),
        "preview_bars": 16 if int(preview_bars) >= 16 else 8,
    }


def _restore_take(payload: dict[str, object]) -> RemixTake:
    data = dict(payload)
    for key in ("source_paths", "decision_summary", "exported_paths"):
        value = data.get(key)
        if isinstance(value, list):
            data[key] = tuple(str(item) for item in value)
    return remix_take_from_dict(data)  # type: ignore[arg-type]


def _safe_cache_root(root: str | Path) -> Path:
    resolved = Path(root).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _pair_preview(plan: AssistedSetPlan, output: Path, bars: int) -> RemixTake:
    available = {item.key for item in TAKE_STRATEGIES}
    recipe = next((key for key in plan.recipe_keys if key in available), None)
    if recipe is None:
        raise ValueError("This idea has no pair recipe that can be previewed.")
    settings = RemixSettings(
        target_bpm=int(plan.target_bpm),
        target_key_pc=plan.target_key_pc,
        target_mode=plan.target_mode,
        mode_handling="Preserve source mode",
        length_mode="Custom bars",
        bars=bars,
        section_bars=max(2, min(int(plan.section_bars), bars)),
        density="Balanced",
        strategies=(recipe,),
        variations_per_strategy=1,
        variation_strength="Subtle",
        random_seed=0,
        auto_balance=True,
        preserve_signature_assets=False,
    )
    takes = generate_remix_takes(plan.profiles[0], plan.profiles[1], output, settings)
    if not takes:
        raise RuntimeError("The preview generator did not create a playable pair preview.")
    return takes[0]


def _multi_preview(plan: AssistedSetPlan, output: Path, bars: int) -> RemixTake:
    available = {item.key for item in MULTI_SOURCE_RECIPES}
    recipe = next((key for key in plan.recipe_keys if key in available), None)
    if recipe is None:
        raise ValueError("This idea has no multi-source recipe that can be previewed.")
    role_map = {
        "lead melody": "melody",
        "foundation": "foundation",
        "section / texture": "section",
        "supporting section": "section",
    }
    specs = tuple(
        SourceSpec(path=profile.path, role=role_map.get(role, "auto"), melody_treatment="full")
        for profile, role in zip(plan.profiles, plan.source_roles)
    )
    settings = MultiSourceSettings(
        target_bpm=int(plan.target_bpm),
        target_key_pc=plan.target_key_pc,
        target_mode=plan.target_mode,
        mode_handling="Preserve source mode",
        bars=bars,
        section_bars=max(2, min(int(plan.section_bars), bars)),
        density="Balanced",
        recipes=(recipe,),
        variations_per_recipe=1,
        variation_strength="Subtle",
        random_seed=0,
        auto_balance=True,
        preserve_signature_assets=False,
    )
    takes = generate_multi_source_takes(plan.profiles, output, settings, specs)
    if not takes:
        raise RuntimeError("The preview generator did not create a playable multi-source preview.")
    return takes[0]


def generate_assisted_preview(
    plan: AssistedSetPlan,
    preview_root: str | Path,
    *,
    bars: int = 8,
) -> RemixTake:
    """Generate or reuse one short, session-owned MIDI preview for an idea."""

    bars = 16 if int(bars) >= 16 else 8
    root = _safe_cache_root(preview_root)
    cache = (root / f"{discovery_plan_id(plan)}-{bars}bars").resolve()
    if root != cache.parent:
        raise ValueError("Preview cache path escaped its session-owned root.")
    metadata = cache / "preview.json"
    if metadata.is_file():
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            take = _restore_take(dict(payload.get("take") or {}))
            if Path(take.path).is_file():
                return take
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            shutil.rmtree(cache, ignore_errors=True)

    staging = (root / f".{cache.name}.{uuid.uuid4().hex[:8]}.partial").resolve()
    if root != staging.parent:
        raise ValueError("Preview staging path escaped its session-owned root.")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        take = _pair_preview(plan, staging, bars) if plan.source_count == 2 else _multi_preview(plan, staging, bars)
        if not Path(take.path).is_file():
            raise RuntimeError("The generated preview file is missing.")
        shutil.rmtree(cache, ignore_errors=True)
        staging.replace(cache)
        final_path = cache / Path(take.path).name
        for generated_manifest in cache.glob("*manifest.json"):
            generated_manifest.unlink(missing_ok=True)
        final_take = replace(
            take,
            path=str(final_path.resolve()),
            name=f"{plan.headline} — {bars}-bar preview",
            description=f"Short discovery preview. {take.description}",
            trial_id="discovery-preview",
            trial_name="Discovery previews",
            status="preview",
        )
        atomic_write_json(
            metadata,
            {
                "format": "MIDI Remixer discovery preview",
                "version": 1,
                "idea_id": discovery_plan_id(plan),
                "bars": bars,
                "take": asdict(final_take),
            },
        )
        prune_discovery_previews(root, keep=40, max_bytes=256 * 1024 * 1024, protected={cache})
        return final_take
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def prune_discovery_previews(
    preview_root: str | Path,
    *,
    keep: int = 40,
    max_bytes: int = 256 * 1024 * 1024,
    protected: set[Path] | None = None,
) -> tuple[str, ...]:
    """Bound preview cache growth without touching files outside the session."""

    root = Path(preview_root).resolve()
    if not root.exists():
        return ()
    protected_resolved = {path.resolve() for path in (protected or set())}
    directories = [path.resolve() for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")]
    directories.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)

    def directory_size(path: Path) -> int:
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    sizes = {path: directory_size(path) for path in directories}
    total = sum(sizes.values())
    removed: list[str] = []
    remaining = len(directories)
    minimum_keep = max(1, int(keep))
    byte_limit = max(1, int(max_bytes))
    for path in reversed(directories):
        if remaining <= minimum_keep and total <= byte_limit:
            break
        if path in protected_resolved:
            continue
        size = sizes.get(path, 0)
        try:
            shutil.rmtree(path)
        except OSError:
            continue
        total = max(0, total - size)
        remaining -= 1
        removed.append(str(path))
    return tuple(removed)
