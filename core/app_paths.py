from __future__ import annotations

"""Resolve immutable application files separately from mutable user data."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

from core.atomic_io import atomic_write_json

APP_DIRECTORY_NAME = "MIDI Remixer"
PORTABLE_ENV = "MIDI_REMIXER_PORTABLE"
PORTABLE_MARKER = ".midi-remixer-portable"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _qt_standard_path(kind_name: str) -> Path | None:
    try:
        from PySide6.QtCore import QStandardPaths

        kind = getattr(QStandardPaths.StandardLocation, kind_name)
        value = QStandardPaths.writableLocation(kind)
        return Path(value).expanduser() if value else None
    except (ImportError, AttributeError, RuntimeError):
        return None


def _platform_data_root() -> Path:
    # This is intentionally suite-specific rather than Qt-application-specific.
    # v0.30 has two independently installable UIs (Library Manager and Remixer)
    # that must attach to one persistent catalogue. QStandardPaths.AppDataLocation
    # incorporates the current QApplication name, which would otherwise let the
    # two executables silently create different libraries. These fixed platform
    # roots also preserve the historical MIDI Remixer Windows AppData location.
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        return Path(base) / APP_DIRECTORY_NAME if base else Path.home() / "AppData" / "Roaming" / APP_DIRECTORY_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIRECTORY_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "midi-remixer"


def _platform_cache_root() -> Path:
    # Cache is shared for the same reason as persistent data: component identity
    # must never alter the location of analysis artifacts or dependency caches.
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / APP_DIRECTORY_NAME / "Cache" if base else Path.home() / "AppData" / "Local" / APP_DIRECTORY_NAME / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_DIRECTORY_NAME
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "midi-remixer"


def _platform_documents_root() -> Path:
    qt = _qt_standard_path("DocumentsLocation")
    if qt:
        return qt / APP_DIRECTORY_NAME
    return Path.home() / "Documents" / APP_DIRECTORY_NAME


@dataclass(frozen=True)
class AppPaths:
    app_root: Path
    data_root: Path
    cache_root: Path
    documents_root: Path
    config: Path
    library: Path
    logs: Path
    sessions: Path
    dependencies: Path
    soundfonts: Path
    reference_imports: Path
    projects: Path
    backups: Path
    portable: bool = False

    @classmethod
    def for_app(cls, app_root: str | Path, *, portable: bool | None = None) -> "AppPaths":
        immutable_root = Path(app_root).resolve()
        portable_enabled = (
            _truthy(os.environ.get(PORTABLE_ENV))
            or (immutable_root / PORTABLE_MARKER).is_file()
            if portable is None
            else bool(portable)
        )
        if portable_enabled:
            data_root = immutable_root / "user-data"
            cache_root = data_root / "cache"
            documents_root = data_root / "projects"
        else:
            data_root = _platform_data_root().resolve()
            cache_root = _platform_cache_root().resolve()
            documents_root = _platform_documents_root().resolve()
        return cls(
            app_root=immutable_root,
            data_root=data_root,
            cache_root=cache_root,
            documents_root=documents_root,
            config=data_root / "config",
            library=data_root / "library",
            logs=data_root / "logs",
            sessions=data_root / "working_sessions",
            dependencies=data_root / "dependencies",
            soundfonts=data_root / "soundfonts",
            reference_imports=data_root / "reference_imports",
            projects=documents_root,
            backups=data_root / "backups",
            portable=portable_enabled,
        )

    def ensure(self) -> "AppPaths":
        for directory in (
            self.data_root,
            self.cache_root,
            self.config,
            self.library,
            self.logs,
            self.sessions,
            self.dependencies,
            self.soundfonts,
            self.reference_imports,
            self.projects,
            self.backups,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def cleanup_stale_partials(self, *, older_than_seconds: int = 24 * 60 * 60) -> int:
        """Remove only stale partial outputs inside application-owned roots."""

        cutoff = time.time() - max(0, int(older_than_seconds))
        removed = 0
        patterns = ("*.partial", "*.partial.*", ".*.tmp", "*.tmp_render.wav")
        for root in (self.sessions, self.cache_root, self.dependencies, self.soundfonts):
            if not root.is_dir():
                continue
            for pattern in patterns:
                for path in root.rglob(pattern):
                    try:
                        if path.is_file() and path.stat().st_mtime <= cutoff:
                            path.unlink()
                            removed += 1
                    except OSError:
                        continue
        return removed



    def migrate_persistent_library_databases(self) -> dict[str, object]:
        """Move v0.24 persistent library databases out of config without losing state.

        SQLite backup is used rather than a raw copy so committed WAL content is
        included.  Sources are intentionally retained as a rollback copy.
        """
        self.ensure()
        migrated: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        # Keep this list aligned with every persistent Library-owned SQLite
        # database. Older development builds did not always introduce these
        # stores at the same time, so migration is deliberately opportunistic:
        # copy a legacy config-resident DB only when it exists and the current
        # library location is still empty. Never overwrite current user data.
        for filename in (
            "library_scan.sqlite3",
            "library_index.sqlite3",
            "library_relationships.sqlite3",
            "discovery_graph.sqlite3",
        ):
            source = self.config / filename
            target = self.library / filename
            if target.exists():
                skipped.append(filename)
                continue
            if not source.exists():
                continue
            try:
                with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
                    src.backup(dst)
                migrated.append(filename)
            except sqlite3.Error as exc:
                errors.append(f"{filename}: {type(exc).__name__}: {exc}")
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    errors.append(f"{filename}: cleanup failed: {cleanup_exc}")
        return {"migrated": migrated, "skipped": skipped, "errors": errors}

    def cleanup_runtime_garbage(self, *, remove_empty_session_dirs: bool = True) -> dict[str, int]:
        """Clean app-owned temporary debris after workers and playback are stopped."""
        removed_files = self.cleanup_stale_partials(older_than_seconds=0)
        removed_dirs = 0
        if remove_empty_session_dirs and self.sessions.is_dir():
            for directory in sorted((p for p in self.sessions.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                    removed_dirs += 1
                except OSError:
                    continue
        return {"removed_temp_files": removed_files, "removed_empty_dirs": removed_dirs}

    def migrate_legacy_state(
        self,
        *,
        max_files: int = 20_000,
        max_total_bytes: int = 10 * 1024 * 1024 * 1024,
    ) -> dict[str, object]:
        """Copy explicit in-place legacy state once without sibling-release crawling.

        Migration never deletes the source, never follows symlinks, never
        overwrites current per-user files, and stops at fixed file/byte limits.
        Executables are intentionally excluded; users must re-validate them.
        """

        self.ensure()
        marker = self.config / "legacy-migration-v0.19.2.json"
        if marker.is_file():
            try:
                import json

                payload = json.loads(marker.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) else {"status": "already-recorded"}
            except (OSError, UnicodeError, ValueError, TypeError):
                return {"status": "marker-unreadable", "path": str(marker)}

        mappings = (
            (self.app_root / "config", self.config, None),
            (self.app_root / "working_sessions", self.sessions, None),
            (self.app_root / "assets" / "soundfonts", self.soundfonts, {".sf2", ".sf3"}),
        )
        copied = 0
        copied_bytes = 0
        skipped = 0
        errors: list[str] = []
        limit_reached = False
        for source_root, destination_root, allowed_suffixes in mappings:
            if not source_root.is_dir():
                continue
            for source in source_root.rglob("*"):
                if copied >= max(0, int(max_files)) or copied_bytes >= max(0, int(max_total_bytes)):
                    limit_reached = True
                    break
                try:
                    if source.is_symlink() or not source.is_file():
                        continue
                    if allowed_suffixes is not None and source.suffix.casefold() not in allowed_suffixes:
                        skipped += 1
                        continue
                    size = int(source.stat().st_size)
                    if size < 0 or copied_bytes + size > max_total_bytes:
                        skipped += 1
                        limit_reached = True
                        continue
                    relative = source.relative_to(source_root)
                    target = destination_root / relative
                    if target.exists():
                        skipped += 1
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target, follow_symlinks=False)
                    copied += 1
                    copied_bytes += size
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"{source}: {type(exc).__name__}: {exc}")
            if limit_reached:
                break

        report: dict[str, object] = {
            "status": "complete" if not errors and not limit_reached else "partial",
            "copied_files": copied,
            "copied_bytes": copied_bytes,
            "skipped_files": skipped,
            "limit_reached": limit_reached,
            "errors": errors[:50],
            "source_root": str(self.app_root),
        }
        # Write the marker only for a complete pass. A partial pass is safe to
        # retry because current targets are never overwritten.
        if report["status"] == "complete":
            atomic_write_json(marker, report)
        return report

    def mutable_roots(self) -> tuple[Path, ...]:
        return (
            self.data_root,
            self.cache_root,
            self.documents_root,
        )

    def legacy_migration_candidates(self) -> tuple[Path, ...]:
        """Explicit old portable locations; no sibling-tree crawling."""

        return (
            self.app_root / "config",
            self.app_root / "working_sessions",
            self.app_root / "logs",
            self.app_root / "vendor",
            self.app_root / "assets" / "soundfonts",
        )
