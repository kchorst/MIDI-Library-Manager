from __future__ import annotations

"""Backup/restore persistent MIDI Library intelligence safely.

Library Intelligence is intentionally independent of source MIDI files and of an
extracted application build.  Backups contain only persistent, reusable library
state: quick catalogue, detailed analysis, prepared relationships, discovery
graph, Engine-owned settings, and imported reference evidence.

Restore is designed to run only after the Library Engine has stopped and released
its SQLite handles.  The Manager enforces that lifecycle before calling restore.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile

from core.app_paths import AppPaths
from core.remix_version import APP_VERSION


@dataclass(frozen=True, slots=True)
class LibraryBackupResult:
    path: str
    files: tuple[str, ...]
    bytes: int


@dataclass(frozen=True, slots=True)
class LibraryRestoreResult:
    source: str
    restored_files: tuple[str, ...]
    manifest: dict[str, object]


_SQLITE_FILES = (
    "library_scan.sqlite3",
    "library_index.sqlite3",
    "library_relationships.sqlite3",
    "discovery_graph.sqlite3",
)
_ENGINE_SETTINGS = "library_engine_settings.json"
_FORMAT_V1 = "midi-remixer-library-intelligence-backup-v1"
_FORMAT_V2 = "midi-remixer-library-intelligence-backup-v2"
_SUPPORTED_FORMATS = {_FORMAT_V1, _FORMAT_V2}


def _sqlite_integrity(path: Path) -> tuple[bool, int]:
    if not path.is_file():
        return False, 0
    with sqlite3.connect(path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).casefold() != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path.name}: {integrity!r}")
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return True, user_version


def _sqlite_integrity_image(image: bytes, *, label: str) -> int:
    """Validate one SQLite image without reopening it through the filesystem.

    Target-Windows qualification proved that a valid backup member could be
    written successfully and then fail when SQLite reopened a second on-disk
    verification copy with WinError 267.  Python 3.12's SQLite binding exposes
    ``Connection.deserialize``; loading the exact ZIP member into an in-memory
    SQLite connection removes that unrelated filesystem/temporary-path boundary
    while still running SQLite's own full ``PRAGMA integrity_check`` against the
    archived bytes.
    """
    if not image:
        raise RuntimeError(f"SQLite backup member is empty: {label}")
    if not hasattr(sqlite3.Connection, "deserialize"):
        raise RuntimeError(
            "This Python SQLite runtime does not support in-memory backup verification "
            f"for {label}; Python 3.12 with sqlite3.Connection.deserialize is required."
        )
    # Online backup images can legitimately retain WAL read/write format bytes
    # (header offsets 18/19 == 2) even after their WAL has been fully checkpointed
    # into the self-contained main database. SQLite cannot deserialize a WAL-mode
    # header into an in-memory database because there is no WAL sidecar to open.
    # For verification only, normalize those two *journal-mode* header bytes to
    # rollback format. The archived bytes themselves are never modified.
    verify_image = image
    if image.startswith(b"SQLite format 3\x00") and len(image) >= 20 and (image[18] == 2 or image[19] == 2):
        normalized = bytearray(image)
        normalized[18] = 1
        normalized[19] = 1
        verify_image = bytes(normalized)
    try:
        with sqlite3.connect(":memory:", timeout=30) as conn:
            conn.deserialize(verify_image)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                raise RuntimeError(f"SQLite integrity check failed for {label}: {integrity!r}")
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Invalid SQLite database image in Library backup: {label}") from exc


def _backup_sqlite_image(source: Path) -> tuple[bool, int, bytes]:
    """Create one self-contained SQLite snapshot entirely in memory.

    Target-Windows qualification REG-101 proved that even a correctly created
    temporary *backup staging* SQLite file can fail with ``ERROR_DIRECTORY``
    when SQLite opens it under the process temp directory.  The backup producer
    therefore has no temporary SQLite filesystem boundary at all: the live
    database is copied with SQLite's online-backup API into an in-memory
    connection, integrity-checked there, then serialized directly into ZIP bytes.
    """
    if not source.is_file():
        return False, 0, b""
    if not hasattr(sqlite3.Connection, "serialize"):
        raise RuntimeError(
            "This Python SQLite runtime does not support in-memory backup creation; "
            f"Python 3.12 with sqlite3.Connection.serialize is required for {source.name}."
        )
    try:
        with sqlite3.connect(source, timeout=30) as src, sqlite3.connect(":memory:", timeout=30) as dst:
            src.execute("PRAGMA busy_timeout=30000")
            src.backup(dst, pages=2048, sleep=0.01)
            integrity = dst.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                raise RuntimeError(f"SQLite integrity check failed while backing up {source.name}: {integrity!r}")
            user_version = int(dst.execute("PRAGMA user_version").fetchone()[0])
            image = bytes(dst.serialize())
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Could not create a consistent SQLite backup image for {source.name}.") from exc
    # Independently verify the serialized bytes before they are admitted to the
    # archive.  This also proves that the producer and verifier agree on the
    # exact image that restore will later consume.
    verified_version = _sqlite_integrity_image(image, label=f"library/{source.name}")
    if verified_version != user_version:
        raise RuntimeError(
            f"SQLite backup schema changed while serializing {source.name}: "
            f"snapshot {user_version}, serialized {verified_version}."
        )
    return True, user_version, image


def _backup_sqlite(source: Path, destination: Path) -> tuple[bool, int]:
    """Compatibility helper for tests/tools that explicitly request a file.

    Production backup creation does not call this function.  It intentionally
    serializes an in-memory snapshot via :func:`_backup_sqlite_image` and writes
    the bytes directly to the ZIP.
    """
    copied, version, image = _backup_sqlite_image(source)
    if not copied:
        return False, 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(image)
    return True, version


def create_library_intelligence_backup(paths: AppPaths, destination: str | Path) -> LibraryBackupResult:
    """Create one verified ZIP containing persistent Library Intelligence.

    SQLite members never transit through a temporary SQLite database on disk.
    Each live database is copied into one in-memory SQLite snapshot, verified,
    serialized directly into the ZIP, then released before the next database is
    processed.  This removes the Windows temp-SQLite failure boundary without
    retaining the full multi-database Library in RAM at once.
    """
    target = Path(destination).expanduser()
    if target.suffix.casefold() != ".zip":
        target = target.with_suffix(".zip")
    target.parent.mkdir(parents=True, exist_ok=True)

    included: list[str] = []
    sqlite_versions: dict[str, int] = {}
    engine_settings = paths.config / _ENGINE_SETTINGS
    legacy_app_settings = paths.config / "app_settings.json"
    reference_files: list[tuple[Path, str]] = []
    if paths.reference_imports.is_dir():
        for source in sorted(paths.reference_imports.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(paths.reference_imports)
            reference_files.append((source, (Path("reference_imports") / relative).as_posix()))

    partial = target.with_name(f".{target.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            # Process one SQLite image at a time.  Do not accumulate all Library
            # databases in memory; a 96K-file Library can make those images large.
            for filename in _SQLITE_FILES:
                copied, version, image = _backup_sqlite_image(paths.library / filename)
                if not copied:
                    continue
                archive_name = f"library/{filename}"
                archive.writestr(archive_name, image)
                del image
                included.append(archive_name)
                sqlite_versions[archive_name] = int(version)

            if engine_settings.is_file():
                archive_name = f"config/{engine_settings.name}"
                archive.write(engine_settings, archive_name)
                included.append(archive_name)
            if legacy_app_settings.is_file():
                archive_name = f"config/{legacy_app_settings.name}"
                archive.write(legacy_app_settings, archive_name)
                included.append(archive_name)
            for source, archive_name in reference_files:
                archive.write(source, archive_name)
                included.append(archive_name)

            manifest = {
                "format": _FORMAT_V2,
                "app_version": APP_VERSION,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "source_midi_files_included": False,
                "engine_settings_included": f"config/{_ENGINE_SETTINGS}" in included,
                "sqlite_user_versions": sqlite_versions,
                "included": sorted(included),
            }
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            included.append("manifest.json")

        verify_library_intelligence_backup(partial)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)

    return LibraryBackupResult(str(target), tuple(sorted(included)), target.stat().st_size)


def _safe_archive_name(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise RuntimeError(f"Unsafe path in Library backup: {name!r}")
    return value


def _validated_archive_members(archive: zipfile.ZipFile) -> tuple[dict[str, object], tuple[zipfile.ZipInfo, ...]]:
    """Validate archive structure before any restore/verification extraction.

    Windows is deliberately treated as the strict path model even when this
    code is running elsewhere: case-insensitive duplicate names and a file that
    shadows a directory prefix are rejected because either can make restore
    results platform-dependent.
    """
    bad = archive.testzip()
    if bad:
        raise RuntimeError(f"CRC failure in {bad}")
    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Not a valid MIDI Library Intelligence backup.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") not in _SUPPORTED_FORMATS:
        raise RuntimeError("Unsupported Library backup format.")

    infos: list[zipfile.ZipInfo] = []
    files: dict[str, PurePosixPath] = {}
    directories: set[str] = set()
    for info in archive.infolist():
        relative = _safe_archive_name(info.filename)
        normalized = relative.as_posix().rstrip("/")
        key = normalized.casefold()
        if not normalized:
            raise RuntimeError(f"Unsafe empty path in Library backup: {info.filename!r}")
        if key in files:
            raise RuntimeError(f"Duplicate/colliding path in Library backup: {info.filename!r}")
        if not info.is_dir() and key in directories:
            raise RuntimeError(f"File/directory path collision in Library backup: {info.filename!r}")
        parent_parts = relative.parts[:-1]
        for depth in range(1, len(parent_parts) + 1):
            parent_key = PurePosixPath(*parent_parts[:depth]).as_posix().casefold()
            if parent_key in files:
                raise RuntimeError(
                    f"File/directory path collision in Library backup: {info.filename!r}"
                )
            directories.add(parent_key)
        if info.is_dir():
            directories.add(key)
            continue
        # A file may not replace a directory namespace already required by a
        # previously seen nested member (for example ``library`` versus
        # ``library/discovery_graph.sqlite3``).
        if key in directories:
            raise RuntimeError(f"File/directory path collision in Library backup: {info.filename!r}")
        files[key] = relative
        infos.append(info)
    return manifest, tuple(infos)


def _verify_open_archive(archive: zipfile.ZipFile) -> tuple[dict[str, object], tuple[zipfile.ZipInfo, ...]]:
    """Verify manifest/CRC and SQLite images without a verification filesystem.

    REG-098 removed nested archive-path extraction, but target Windows then
    proved that even a flat temporary SQLite verification file could fail on
    reopen with ``ERROR_DIRECTORY`` after the exact archived bytes had already
    been written successfully.  Verification therefore has no on-disk SQLite
    stage at all: each ZIP member is loaded directly into an in-memory SQLite
    connection and receives the same ``PRAGMA integrity_check``.  Restore still
    extracts only *after* this verification succeeds.
    """
    manifest, infos = _validated_archive_members(archive)
    info_by_name = {_safe_archive_name(info.filename).as_posix().casefold(): info for info in infos}
    actual_versions: dict[str, int] = {}
    actual_members = set(info_by_name)
    # A manifest-only ZIP (or a settings-only ZIP) is not a recoverable
    # intelligence backup. Do not report success for a no-op restore.
    sqlite_members = {f"library/{name}" for name in _SQLITE_FILES}
    if not actual_members.intersection(sqlite_members):
        raise RuntimeError("Library backup contains no Library SQLite databases.")
    if manifest.get("format") == _FORMAT_V2:
        included = manifest.get("included")
        if not isinstance(included, list) or any(not isinstance(name, str) for name in included):
            raise RuntimeError("Library backup manifest has no valid included-file inventory.")
        declared_members = {name.casefold() for name in included}
        if len(declared_members) != len(included) or declared_members != actual_members - {"manifest.json"}:
            raise RuntimeError("Library backup manifest does not match archive contents.")
    for filename in _SQLITE_FILES:
        archive_name = f"library/{filename}"
        info = info_by_name.get(archive_name.casefold())
        if info is None:
            continue
        image = archive.read(info)
        actual_versions[archive_name] = _sqlite_integrity_image(image, label=archive_name)

    declared = manifest.get("sqlite_user_versions")
    if manifest.get("format") == _FORMAT_V2:
        if not isinstance(declared, dict) or set(declared) != set(actual_versions):
            raise RuntimeError("Library backup SQLite schema inventory is incomplete or inconsistent.")
    elif declared is None:
        declared = {}
    if not isinstance(declared, dict):
        raise RuntimeError("Invalid SQLite schema inventory in Library backup manifest.")
    for name, version in declared.items():
        if name not in actual_versions:
            raise RuntimeError(f"Library backup declares a missing or unknown SQLite member: {name}")
        try:
            matches = int(version) == actual_versions[name]
        except (ValueError, TypeError, OverflowError) as exc:
            raise RuntimeError(f"Invalid SQLite schema version for {name}") from exc
        if not matches:
            raise RuntimeError(
                f"Library backup schema metadata mismatch for {name}: "
                f"manifest {version}, database {actual_versions[name]}."
            )
    return manifest, infos


def _extract_verified_archive(source: Path, destination: Path) -> dict[str, object]:
    """Verify the archive first, then extract the exact verified members."""
    with zipfile.ZipFile(source, "r") as archive:
        manifest, infos = _verify_open_archive(archive)
        for info in infos:
            relative = _safe_archive_name(info.filename)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    return manifest


def verify_library_intelligence_backup(path: str | Path) -> dict[str, object]:
    """Validate ZIP, manifest, safe paths, and SQLite integrity without restoring."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise RuntimeError(f"Library backup does not exist: {source}")
    with zipfile.ZipFile(source, "r") as archive:
        manifest, _infos = _verify_open_archive(archive)
        return manifest


def _replace_file_with_rollback(staged: Path, target: Path, rollback_root: Path, changed: list[tuple[Path, Path | None]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    rollback: Path | None = None
    if target.exists():
        rollback = rollback_root / target.parent.name / target.name
        rollback.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, rollback)
    # Register the displacement BEFORE installing the replacement: if the
    # second os.replace fails, exception handling must still restore target.
    changed.append((target, rollback))
    os.replace(staged, target)


def restore_library_intelligence_backup(paths: AppPaths, source: str | Path) -> LibraryRestoreResult:
    """Restore verified Library Intelligence after the Engine has stopped.

    The caller is responsible for ensuring no Library Engine is running. Existing
    database files are moved aside until every replacement succeeds, allowing a
    local rollback if filesystem replacement itself fails. The Manager also makes
    a separate verified pre-restore ZIP before stopping the Engine.
    """
    backup = Path(source).expanduser()
    paths.ensure()
    with tempfile.TemporaryDirectory(prefix="midi-library-restore-") as temp_name:
        staged_root = Path(temp_name) / "staged"
        manifest = _extract_verified_archive(backup, staged_root)
        rollback_root = Path(temp_name) / "rollback"
        changed: list[tuple[Path, Path | None]] = []
        restored: list[str] = []
        try:
            for filename in _SQLITE_FILES:
                staged = staged_root / "library" / filename
                if not staged.is_file():
                    continue
                target = paths.library / filename
                # Stale WAL/SHM files from a previously stopped process must not
                # be allowed to replay against a restored database image.
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(target) + suffix)
                    if sidecar.exists():
                        # Preserve sidecars too: WAL may contain uncheckpointed
                        # transactions in the original database state.
                        displaced = rollback_root / "sidecars" / (filename + suffix)
                        displaced.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(sidecar, displaced)
                        changed.append((sidecar, displaced))
                _replace_file_with_rollback(staged, target, rollback_root, changed)
                restored.append(f"library/{filename}")

            staged_settings = staged_root / "config" / _ENGINE_SETTINGS
            if staged_settings.is_file():
                # Validate JSON before replacing the Engine-owned settings file.
                payload = json.loads(staged_settings.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError("Backed-up Library Engine settings are not a JSON object.")
                _replace_file_with_rollback(
                    staged_settings, paths.config / _ENGINE_SETTINGS, rollback_root, changed
                )
                restored.append(f"config/{_ENGINE_SETTINGS}")

            staged_refs = staged_root / "reference_imports"
            if staged_refs.is_dir():
                current_refs = paths.reference_imports
                old_refs = rollback_root / "reference_imports-current"
                if current_refs.exists():
                    os.replace(current_refs, old_refs)
                try:
                    os.replace(staged_refs, current_refs)
                except OSError:
                    if old_refs.exists() and not current_refs.exists():
                        os.replace(old_refs, current_refs)
                    raise
                restored.extend(
                    (Path("reference_imports") / item.relative_to(current_refs)).as_posix()
                    for item in current_refs.rglob("*") if item.is_file()
                )
        except (OSError, RuntimeError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
            # Restore in reverse order. Never delete an original file if it was
            # not actually displaced (for example when os.replace failed).
            rollback_failures: list[str] = []
            for target, rollback in reversed(changed):
                try:
                    if rollback is None:
                        target.unlink(missing_ok=True)
                    elif rollback.exists():
                        target.unlink(missing_ok=True)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(rollback, target)
                    else:
                        # Original not moved (or already restored); do not erase.
                        if not target.exists():
                            rollback_failures.append(str(target))
                except OSError:
                    rollback_failures.append(str(target))
            if rollback_failures:
                # Retain a durable copy if rollback could not complete: a
                # TemporaryDirectory would otherwise erase its only copy.
                rescue = paths.library / "restore-recovery"
                rescue.mkdir(parents=True, exist_ok=True)
                for _target, displaced in changed:
                    if displaced is not None and displaced.exists():
                        recovery_target = rescue / displaced.relative_to(rollback_root)
                        recovery_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(displaced), str(recovery_target))
                raise RuntimeError(
                    f"Restore failed and rollback is incomplete for {rollback_failures}. "
                    f"Original files retained under {rescue}."
                ) from exc
            raise

    return LibraryRestoreResult(str(backup), tuple(sorted(set(restored))), manifest)
