from __future__ import annotations

import json
from pathlib import Path
import shutil
import time
from typing import Any, Iterable

from core.app_paths import AppPaths
from core.atomic_io import atomic_write_json


ENGINE_SETTINGS_VERSION = 1
DEFAULT_ENGINE_SETTINGS: dict[str, Any] = {
    "settings_version": ENGINE_SETTINGS_VERSION,
    "library_paths": [],
    "library_recursive_scan": True,
    "library_skip_generated": True,
    "library_watch_enabled": True,
    "library_use_analysis_cache": True,
    "deep_analysis_policy": "later",
    "throughput_mode": "maximum",
    # Library Manager is intentionally an analysis dashboard: opening it means
    # "keep preparing my library" unless the user explicitly turns this off.
    "manager_auto_start_analysis": True,
}


class LibraryEngineSettings:
    """Engine-owned settings stored separately from Remixer UI preferences.

    Only the Library Engine process writes this file. Library Manager changes
    settings through the localhost Engine API and MIDI Remixer treats them as
    read-only status. Separating this file prevents last-writer-wins races
    between two independently running Qt applications.
    """

    def __init__(self, app_root: str | Path) -> None:
        self.app_root = Path(app_root).resolve()
        self.paths = AppPaths.for_app(self.app_root).ensure()
        self.path = self.paths.config / "library_engine_settings.json"
        self.data: dict[str, Any] = dict(DEFAULT_ENGINE_SETTINGS)
        self.recovery_notice = ""
        settings_existed = self.path.exists()
        if not self.load():
            if settings_existed:
                backup = self._preserve_unreadable_settings()
                self.recovery_notice = (
                    "Engine settings were unreadable and were reset to safe defaults. "
                    f"The unreadable file was preserved at {backup}."
                )
            self._migrate_from_legacy_app_settings()
            self.save()

    def load(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        self.data.update(payload)
        self.data["settings_version"] = ENGINE_SETTINGS_VERSION
        return True

    def save(self) -> None:
        self.data["settings_version"] = ENGINE_SETTINGS_VERSION
        atomic_write_json(self.path, self.data)

    def _preserve_unreadable_settings(self) -> Path:
        """Preserve a corrupt settings file before replacing it with defaults.

        Source-root configuration is user state.  A parse failure must never
        silently destroy the only copy of that state merely so Engine startup
        can continue.  If preservation itself fails, abort startup rather than
        overwrite the unreadable file.
        """
        backup = self.path.with_name(
            f"library_engine_settings.corrupt-{time.time_ns()}.json"
        )
        try:
            shutil.copy2(self.path, backup)
        except OSError as exc:
            raise RuntimeError(
                "MIDI Library Engine settings are unreadable and could not be "
                "preserved safely; the original settings file was not replaced."
            ) from exc
        return backup

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self.data.get(key) == value:
            return
        self.data[key] = value
        self.save()

    def update_many(self, values: dict[str, Any]) -> None:
        changed = False
        for key, value in values.items():
            if self.data.get(key) != value:
                self.data[key] = value
                changed = True
        if changed:
            self.save()

    def roots(self) -> list[str]:
        values = self.get("library_paths", []) or []
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            try:
                value = str(Path(raw).expanduser().resolve(strict=False))
            except (OSError, TypeError, ValueError):
                continue
            key = value.casefold()
            if not value or key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    def set_roots(self, roots: Iterable[str | Path]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in roots:
            try:
                value = str(Path(raw).expanduser().resolve(strict=False))
            except (OSError, TypeError, ValueError):
                continue
            key = value.casefold()
            if not value or key in seen:
                continue
            seen.add(key)
            normalized.append(value)
        self.set("library_paths", normalized)
        return normalized

    def _migrate_from_legacy_app_settings(self) -> None:
        """Import only Engine-owned keys from the pre-split settings file once."""
        legacy = self.paths.config / "app_settings.json"
        if not legacy.is_file():
            return
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        mapping = {
            "library_paths": "library_paths",
            "library_recursive_scan": "library_recursive_scan",
            "library_skip_generated": "library_skip_generated",
            "library_watch_while_open": "library_watch_enabled",
            "library_use_analysis_cache": "library_use_analysis_cache",
            "library_deep_analysis_policy": "deep_analysis_policy",
            "library_engine_throughput": "throughput_mode",
            "library_manager_auto_start": "manager_auto_start_analysis",
        }
        for old_key, new_key in mapping.items():
            if old_key in payload:
                self.data[new_key] = payload[old_key]
