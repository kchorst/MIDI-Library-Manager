from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import time
from typing import Any, Iterable

from core.app_paths import AppPaths
from core.atomic_io import atomic_write_json


DEFAULT_SETTINGS: dict[str, Any] = {
    # Audio and paths
    "last_soundfont_path": None,
    "last_midi_path": None,
    "last_midi_directory": None,
    "last_output_directory": None,
    "last_export_directory": None,
    "last_project_directory": None,
    "last_soundfont_directory": None,
    "fluidsynth_path": None,
    "audio_driver": "default",
    "audio_driver_verified": False,
    "master_volume": 100,
    "library_audition_volume": 100,
    "export_last_formats": ["midi"],
    "export_mp3_bitrate": 192,
    "export_bake_master_volume": False,
    "export_open_folder_after": True,
    "recent_midi_paths": [],
    "recent_soundfont_paths": [],
    "recent_project_paths": [],
    # Library
    "library_recursive_scan": True,
    "library_skip_generated": True,
    "library_watch_while_open": True,
    "library_use_analysis_cache": True,
    "library_deep_analysis_policy": "ask",
    "library_pair_scope": "selected",
    "library_max_candidate_pairs": 250,
    "remix_candidate_limit": 50,
    "library_min_preparation": 0,
    "library_search": "",
    "library_style_filter": "All styles",
    "library_composer_filter": "All composers",
    "library_meter_filter": "All meters",
    "library_mode_filter": "All modes",
    "library_arrangement_filter": "All arrangements",
    "library_progression_filter": "All progression families",
    "library_min_orchestration": 0,
    "library_min_harmony": 0,
    "library_pair_category": "All categories",
    "library_bpm_min": 0,
    "library_bpm_max": 0,
    "library_roles_ready_only": False,
    "library_relationship_filter": "All relationships",
    "library_paths": [],
    "library_setup_completed": False,
    "library_setup_prompt_dismissed": False,
    "library_visible_columns": [
        "Title", "Composer", "Style", "MIDI Type", "Felt BPM", "Meter", "Declared key",
        "Analyzed key", "Arrangement", "Status", "Orchestration", "Progression family", "Roles",
    ],
    # Remix defaults
    "remix_default_bpm": 110,
    "remix_target_mode": None,
    "remix_mode_handling": "Preserve source mode",
    "remix_length_mode": "Shared full length",
    "remix_multisource_length_mode": "Anchor full length",
    "remix_default_bars": 64,
    "remix_default_section_bars": 8,
    "remix_default_density": "Balanced",
    "remix_default_strategies": [
        "melody-a-accompaniment-b",
        "melody-b-accompaniment-a",
        "section-exchange",
        "conversation",
        "transformation",
    ],
    "remix_default_multisource_recipes": [
        "melody-relay",
        "rotating-sections",
        "anchor-transformations",
    ],
    "remix_default_melody_treatment": "full",
    "remix_auto_evaluate_pair": True,
    "remix_open_folder_after_generation": False,
    "remix_variations_per_strategy": 1,
    "remix_variation_strength": "Moderate",
    "remix_random_seed": 0,
    "remix_melody_treatment_a": "full",
    "remix_melody_treatment_b": "full",
    "remix_auto_balance": True,
    "guidance_mode": "guided",
    "onboarding_mode_chosen": False,
    "guided_initial_take_count": 3,
    "guided_correct_severe_conflicts": True,
    "guided_preserve_tensions": True,
    "session_autosave": True,
    "session_prompt_on_close": True,
    "session_keep_recoverable": True,
    # Interface/layout
    "ui_compact_header": True,
    "ui_adaptive_header": True,
    "ui_restore_layout": True,
    "ui_row_density": "Comfortable",
    "ui_takes_show_description_column": False,
    "ui_show_full_paths": False,
    "ui_active_tab": 0,
    "ui_window_geometry": None,
    "ui_window_state": None,
    "ui_library_splitter_state": None,
    "ui_discovery_splitter_state": None,
    "ui_analysis_splitter_state": None,
    "ui_builder_splitter_state": None,
    "ui_takes_splitter_state": None,
    "ui_workbench_splitter_state": None,
    "ui_library_browser_geometry": None,
    "ui_library_header_state": None,
    "ui_pair_header_state": None,
    "ui_takes_header_state": None,
    "migrated_from": None,
}


_MIGRATABLE_KEYS = {
    "last_soundfont_path", "last_midi_path", "last_midi_directory",
    "last_output_directory", "last_export_directory", "last_project_directory", "last_soundfont_directory",
    "fluidsynth_path", "audio_driver", "audio_driver_verified", "master_volume",
    "recent_midi_paths", "recent_soundfont_paths", "recent_project_paths",
}


class AppSettings:
    """JSON settings stored in the per-user application data directory.

    The application package is treated as immutable. On the first run after an
    upgrade, only explicit legacy locations are inspected; neighboring release
    folders are never crawled or silently reused.
    """

    _DEBOUNCED_PREFIXES = ("ui_", "library_search", "library_bpm_")

    def __init__(self, app_root: str | Path, data_root: str | Path | None = None) -> None:
        self.app_root = Path(app_root).resolve()
        self.paths = AppPaths.for_app(self.app_root)
        settings_root = Path(data_root).resolve() if data_root is not None else self.paths.config
        self.path = settings_root / "app_settings.json"
        self.data: dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self._dirty = False
        self._last_save_monotonic = 0.0
        loaded = self.load()
        if not loaded:
            self._migrate_explicit_legacy_settings()

    def load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
                self._dirty = False
                return True
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            # Keep launch recoverable while leaving the unreadable settings file
            # untouched for diagnostics. Defaults remain in memory.
            self.data = copy.deepcopy(DEFAULT_SETTINGS)
        return False

    def save(self) -> None:
        atomic_write_json(self.path, self.data)
        self._dirty = False
        self._last_save_monotonic = time.monotonic()

    def flush(self) -> None:
        if self._dirty:
            self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, *, save: bool | None = None) -> None:
        if self.data.get(key) == value:
            return
        self.data[key] = value
        self._dirty = True
        should_debounce = key.startswith(self._DEBOUNCED_PREFIXES)
        if save is True or (save is None and not should_debounce):
            self.save()
        elif save is None and time.monotonic() - self._last_save_monotonic >= 2.0:
            self.save()

    def update_many(self, values: dict[str, Any], *, save: bool = True) -> None:
        changed = any(self.data.get(key) != value for key, value in values.items())
        if not changed:
            return
        self.data.update(values)
        self._dirty = True
        if save:
            self.save()

    def reset(self, keys: Iterable[str] | None = None) -> None:
        if keys is None:
            self.data = copy.deepcopy(DEFAULT_SETTINGS)
        else:
            for key in keys:
                if key in DEFAULT_SETTINGS:
                    self.data[key] = copy.deepcopy(DEFAULT_SETTINGS[key])
                else:
                    self.data.pop(key, None)
        self._dirty = True
        self.save()

    def _explicit_legacy_candidates(self) -> list[Path]:
        candidates = [self.app_root / "config" / "app_settings.json"]
        appdata = os.environ.get("APPDATA")
        local = os.environ.get("LOCALAPPDATA")
        for base in (appdata, local):
            if base:
                candidates.extend([
                    Path(base) / "MIDI Performer" / "app_settings.json",
                    Path(base) / "MIDI Remixer" / "app_settings.json",
                ])
        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            key = str(resolved).casefold()
            if key not in seen and resolved != self.path:
                seen.add(key)
                unique.append(resolved)
        return unique

    def _migrate_explicit_legacy_settings(self) -> bool:
        for candidate in self._explicit_legacy_candidates():
            if not candidate.is_file():
                continue
            try:
                source = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(source, dict):
                continue
            changed = False
            for key in _MIGRATABLE_KEYS:
                value = source.get(key)
                if value not in (None, "", [], {}):
                    self.data[key] = value
                    changed = True
            if changed:
                self.data["migrated_from"] = str(candidate)
                self._dirty = True
                self.save()
                return True
        return False
