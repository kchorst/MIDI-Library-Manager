from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import shutil
import uuid
import time
from typing import Any, Iterable

from core.atomic_io import atomic_write_json
from core.listening_review import ListeningReview

_LOG = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value)).strip("-")
    return cleaned[:80] or "trial"


def take_storage_key(trial_id: str, take_id: str) -> str:
    """Manifest key for an exact Take, even when display IDs repeat."""
    return f"{str(trial_id or '').strip()}::{str(take_id or '').strip()}"


@dataclass
class TrialRecord:
    trial_id: str
    name: str
    created_at: str
    guidance_mode: str
    decision_summary: list[str] = field(default_factory=list)
    take_ids: list[str] = field(default_factory=list)
    status: str = "active"
    source_paths: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionManifest:
    session_id: str
    created_at: str
    updated_at: str
    status: str = "active"
    guidance_mode: str = "guided"
    project_path: str | None = None
    source_paths: list[str] = field(default_factory=list)
    trials: list[dict[str, Any]] = field(default_factory=list)
    take_paths: dict[str, str] = field(default_factory=dict)
    exported_paths: list[str] = field(default_factory=list)
    undo_stack: list[dict[str, Any]] = field(default_factory=list)
    listening_reviews: list[dict[str, Any]] = field(default_factory=list)
    saved_discovery_ideas: list[dict[str, Any]] = field(default_factory=list)


class SessionWorkspace:
    """Own generated working files so rejected sessions can be cleaned safely.

    Original source MIDI files and user exports are never placed under the owned
    session root. Deletion is therefore bounded to a directory created by this
    class, and every destructive operation verifies path ownership first.
    """

    def __init__(self, base_directory: str | Path, session_id: str | None = None) -> None:
        self.base_directory = Path(base_directory).resolve()
        self.session_id = session_id or f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.root = (self.base_directory / self.session_id).resolve()
        self.trials_root = self.root / "trials"
        self.manifest_path = self.root / "session.json"
        self.autosave_path = self.root / "autosave.miremix"
        self.load_warnings: list[str] = []
        self._materialized = self.manifest_path.is_file()
        self.manifest = self._load_or_create()

    @property
    def materialized(self) -> bool:
        return self._materialized and self.manifest_path.is_file()

    def _materialize(self) -> None:
        self.base_directory.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.trials_root.mkdir(parents=True, exist_ok=True)
        self._materialized = True

    def _load_or_create(self) -> SessionManifest:
        if self.manifest_path.is_file():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise TypeError("Session manifest root must be a JSON object.")
                valid = set(SessionManifest.__dataclass_fields__)
                manifest = SessionManifest(**{key: value for key, value in data.items() if key in valid})
                if self._migrate_manifest(manifest):
                    self._save_manifest(manifest)
                return manifest
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                # Keep the unreadable file for diagnostics instead of silently
                # overwriting the only evidence of what went wrong.
                backup = self.manifest_path.with_name(f"session-corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
                try:
                    self.manifest_path.replace(backup)
                    self.load_warnings.append(f"Unreadable session manifest preserved as {backup.name}: {type(exc).__name__}: {exc}")
                except OSError as backup_exc:
                    self.load_warnings.append(
                        f"Unreadable session manifest could not be preserved: {type(exc).__name__}: {exc}; "
                        f"backup failed: {type(backup_exc).__name__}: {backup_exc}"
                    )
        # New sessions remain memory-only until the first real mutation. Merely
        # launching and closing the app therefore creates no recoverable debris.
        return SessionManifest(self.session_id, _now(), _now())

    def _migrate_manifest(self, manifest: SessionManifest) -> bool:
        """Migrate legacy take-id-only path keys without guessing collisions.

        Older manifests used only ``take_id`` as a key.  A unique ID can be
        upgraded exactly.  When the same display ID appears in multiple Trials,
        the file's owned Trial directory may identify one exact owner; otherwise
        the legacy entry is left untouched rather than copied to multiple Takes.
        """

        changed = False
        owners: dict[str, list[str]] = {}
        for trial in manifest.trials:
            trial_id = str(trial.get("trial_id") or "")
            for take_id in trial.get("take_ids", []):
                owners.setdefault(str(take_id), []).append(trial_id)

        for take_id, trial_ids in owners.items():
            legacy = manifest.take_paths.get(take_id)
            if not legacy:
                continue
            chosen_trial = trial_ids[0] if len(trial_ids) == 1 else ""
            if not chosen_trial:
                try:
                    legacy_path = Path(legacy).resolve()
                    matching = [
                        trial_id for trial_id in trial_ids
                        if self.trial_directory(trial_id) == legacy_path.parent
                        or self.trial_directory(trial_id) in legacy_path.parents
                    ]
                except (OSError, RuntimeError, ValueError):
                    matching = []
                if len(matching) == 1:
                    chosen_trial = matching[0]
            if not chosen_trial:
                continue
            exact = take_storage_key(chosen_trial, take_id)
            if exact not in manifest.take_paths:
                manifest.take_paths[exact] = legacy
            manifest.take_paths.pop(take_id, None)
            changed = True
        return changed

    def _resolve_take_storage_key(self, take_id: str, trial_id: str = "") -> str | None:
        exact = take_storage_key(trial_id, take_id) if trial_id else ""
        if exact and exact in self.manifest.take_paths:
            return exact
        matching = [key for key in self.manifest.take_paths if key.endswith(f"::{take_id}")]
        if trial_id:
            # A legacy key is safe only when the display ID belongs to one Trial.
            owners = [
                str(item.get("trial_id") or "") for item in self.manifest.trials
                if take_id in [str(value) for value in item.get("take_ids", [])]
            ]
            if len(owners) == 1 and owners[0] == trial_id and take_id in self.manifest.take_paths:
                return take_id
            return None
        if len(matching) == 1:
            return matching[0]
        if not matching and take_id in self.manifest.take_paths:  # unambiguous legacy fallback
            owners = [
                item for item in self.manifest.trials
                if take_id in [str(value) for value in item.get("take_ids", [])]
            ]
            if len(owners) <= 1:
                return take_id
        return None

    def _save_manifest(self, manifest: SessionManifest | None = None) -> None:
        if manifest is not None:
            self.manifest = manifest
        self.manifest.updated_at = _now()
        self._materialize()
        atomic_write_json(self.manifest_path, asdict(self.manifest))

    def _owned(self, path: str | Path) -> Path:
        candidate = Path(path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"Refusing to modify non-session path: {candidate}")
        return candidate

    def owned_directory(self, relative_name: str) -> Path:
        """Create and return a session-owned working directory.

        Feature code must use this instead of creating children beneath
        ``session_workspace.root`` directly so the manifest is materialized and
        normal close/reject retention can account for the files.
        """
        name = str(relative_name or "").strip().replace("\\", "/")
        if not name or name.startswith("/") or ".." in Path(name).parts:
            raise ValueError("Session-owned directory name must be a safe relative path.")
        self._materialize()
        target = self._owned(self.root / name)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def set_guidance_mode(self, mode: str) -> None:
        mode_value = str(mode)
        if self.manifest.guidance_mode == mode_value:
            return
        self.manifest.guidance_mode = mode_value
        if self.materialized or self.has_meaningful_content():
            self._save_manifest()

    def update_sources(self, paths: Iterable[str]) -> None:
        self.manifest.source_paths = [str(Path(path)) for path in paths]
        self._save_manifest()

    def create_trial(self, name: str, guidance_mode: str, *, decisions: Iterable[str] = (), source_paths: Iterable[str] = (), settings: dict[str, Any] | None = None) -> TrialRecord:
        number = len(self.manifest.trials) + 1
        trial_id = f"trial-{number:03d}-{uuid.uuid4().hex[:6]}"
        record = TrialRecord(
            trial_id=trial_id,
            name=name or f"Trial {number}",
            created_at=_now(),
            guidance_mode=guidance_mode,
            decision_summary=list(decisions),
            source_paths=list(source_paths),
            settings=dict(settings or {}),
        )
        self.manifest.trials.append(asdict(record))
        self._save_manifest()
        self.trial_directory(record.trial_id).mkdir(parents=True, exist_ok=True)
        return record

    def trial_directory(self, trial_id: str) -> Path:
        return self._owned(self.trials_root / _safe_name(trial_id))

    def trials(self) -> list[TrialRecord]:
        records: list[TrialRecord] = []
        for item in self.manifest.trials:
            try:
                records.append(TrialRecord(**item))
            except TypeError:
                continue
        return records

    def trial(self, trial_id: str) -> TrialRecord | None:
        return next((item for item in self.trials() if item.trial_id == trial_id), None)

    def register_take(self, trial_id: str, take_id: str, path: str | Path) -> None:
        owned = self._owned(path)
        if not owned.is_file():
            raise FileNotFoundError(f"Generated Take does not exist: {owned}")
        trial = next((item for item in self.manifest.trials if item.get("trial_id") == trial_id), None)
        if trial is None:
            raise LookupError(f"Cannot register Take for unknown Trial: {trial_id}")
        trial.setdefault("take_ids", [])
        if take_id not in trial["take_ids"]:
            trial["take_ids"].append(take_id)
        self.manifest.take_paths[take_storage_key(trial_id, take_id)] = str(owned)
        self._save_manifest()

    def register_export(self, path: str | Path) -> None:
        resolved = Path(path).resolve()
        # A user-facing export must survive Reject Session. Generated working
        # files belong under the session root; exports deliberately may not.
        if resolved == self.root or self.root in resolved.parents:
            raise ValueError("Choose an export location outside the temporary session folder so the export survives session cleanup.")
        value = str(resolved)
        if value not in self.manifest.exported_paths:
            self.manifest.exported_paths.append(value)
            self._save_manifest()


    def record_listening_review(self, review: ListeningReview) -> None:
        """Persist one human listening result for an exact Take."""
        self.manifest.listening_reviews.append(review.to_dict())
        self.manifest.listening_reviews = self.manifest.listening_reviews[-500:]
        self._save_manifest()

    def listening_reviews(
        self, take_id: str | None = None, *, trial_id: str | None = None, take_path: str | None = None
    ) -> list[ListeningReview]:
        result: list[ListeningReview] = []
        for item in self.manifest.listening_reviews:
            payload = dict(item)
            if isinstance(payload.get("issues"), list):
                payload["issues"] = tuple(payload["issues"])
            try:
                review = ListeningReview(**payload)
            except (TypeError, ValueError):
                continue
            if take_id is not None and review.take_id != take_id:
                continue
            if trial_id is not None and review.trial_id != trial_id:
                continue
            if take_path is not None:
                try:
                    same_path = Path(review.take_path).resolve(strict=False) == Path(take_path).resolve(strict=False)
                except (OSError, RuntimeError, ValueError):
                    same_path = str(review.take_path or "") == str(take_path)
                if not review.take_path or not same_path:
                    continue
            result.append(review)
        return result

    def replace_listening_reviews(self, reviews: Iterable[ListeningReview | dict[str, Any]]) -> None:
        """Replace imported review history through the manifest's public API."""
        payloads: list[dict[str, Any]] = []
        for review in reviews:
            if isinstance(review, ListeningReview):
                payloads.append(review.to_dict())
            elif isinstance(review, dict):
                payloads.append(dict(review))
        self.manifest.listening_reviews = payloads[-500:]
        self._save_manifest()

    def saved_discovery_ideas(self) -> list[dict[str, Any]]:
        """Return defensive copies of promising mash ideas saved in this session."""
        return [dict(item) for item in self.manifest.saved_discovery_ideas]

    def save_discovery_idea(self, payload: dict[str, Any]) -> None:
        idea = dict(payload)
        idea_id = str(idea.get("idea_id") or "").strip()
        if not idea_id:
            raise ValueError("A saved discovery idea requires a stable idea_id.")
        existing = [
            dict(item) for item in self.manifest.saved_discovery_ideas
            if str(item.get("idea_id") or "") != idea_id
        ]
        existing.append(idea)
        self.manifest.saved_discovery_ideas = existing[-100:]
        self._save_manifest()

    def remove_discovery_idea(self, idea_id: str) -> bool:
        value = str(idea_id or "").strip()
        before = len(self.manifest.saved_discovery_ideas)
        self.manifest.saved_discovery_ideas = [
            dict(item) for item in self.manifest.saved_discovery_ideas
            if str(item.get("idea_id") or "") != value
        ]
        changed = len(self.manifest.saved_discovery_ideas) != before
        if changed:
            self._save_manifest()
        return changed

    def push_undo(self, action: str, payload: dict[str, Any]) -> None:
        self.manifest.undo_stack.append({"action": action, "payload": payload, "created_at": _now()})
        discarded = self.manifest.undo_stack[:-30]
        self.manifest.undo_stack = self.manifest.undo_stack[-30:]
        for record in discarded:
            self._discard_undo_record(record)
        self._save_manifest()

    def _discard_undo_record(self, record: dict[str, Any]) -> None:
        """Permanently remove trash no longer reachable by Undo."""
        action = record.get("action")
        payload = dict(record.get("payload") or {})
        candidates: list[str] = []
        if action == "restore_takes":
            for item in payload.get("items", []):
                trash = dict(item.get("file") or {}).get("trash_path")
                if trash:
                    candidates.append(str(trash))
        elif action == "restore_trial":
            trash = dict(payload.get("workspace") or {}).get("trash_directory")
            if trash:
                candidates.append(str(trash))
        for value in candidates:
            try:
                path = self._owned(value)
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            except (OSError, ValueError) as exc:
                _LOG.warning("Could not discard unreachable undo trash %s: %s", value, exc)

    def purge_unreachable_trash(self) -> list[str]:
        """Remove trash entries not referenced by the current Undo stack."""
        trash_root = self._owned(self.root / ".trash")
        if not trash_root.exists():
            return []
        referenced: set[Path] = set()
        for record in self.manifest.undo_stack:
            action = record.get("action")
            payload = dict(record.get("payload") or {})
            if action == "restore_takes":
                for item in payload.get("items", []):
                    value = dict(item.get("file") or {}).get("trash_path")
                    if value:
                        referenced.add(self._owned(value))
            elif action == "restore_trial":
                value = dict(payload.get("workspace") or {}).get("trash_directory")
                if value:
                    referenced.add(self._owned(value))
        removed: list[str] = []
        for path in sorted(trash_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path in referenced or any(parent in referenced for parent in path.parents):
                continue
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError as exc:
                _LOG.warning("Could not purge session trash %s: %s", path, exc)
        return removed

    def pop_undo(self) -> dict[str, Any] | None:
        if not self.manifest.undo_stack:
            return None
        item = self.manifest.undo_stack.pop()
        self._save_manifest()
        return item

    def delete_take(self, take_id: str, *, trial_id: str = "", allow_undo: bool = True) -> dict[str, str | None]:
        storage_key = self._resolve_take_storage_key(take_id, trial_id)
        path = self.manifest.take_paths.pop(storage_key, None) if storage_key else None
        result: dict[str, str | None] = {
            "take_id": take_id, "trial_id": trial_id or None, "storage_key": storage_key,
            "original_path": path, "trash_path": None,
        }
        if path:
            target = self._owned(path)
            if target.is_file():
                if allow_undo:
                    identity = _safe_name(f"{trial_id}-{take_id}")
                    trash = self._owned(self.root / ".trash" / f"{identity}-{target.name}")
                    trash.parent.mkdir(parents=True, exist_ok=True)
                    if trash.exists():
                        trash.unlink()
                    target.replace(trash)
                    result["trash_path"] = str(trash)
                else:
                    target.unlink()
        for item in self.manifest.trials:
            if trial_id and item.get("trial_id") != trial_id:
                continue
            ids = item.get("take_ids", [])
            if take_id in ids:
                item["take_ids"] = [value for value in ids if value != take_id]
                if trial_id:
                    break
        self._save_manifest()
        return result

    def restore_take(self, take_id: str, trial_id: str, original_path: str, trash_path: str | None) -> bool:
        original = self._owned(original_path)
        if trash_path:
            trash = self._owned(trash_path)
            if trash.is_file():
                original.parent.mkdir(parents=True, exist_ok=True)
                trash.replace(original)
        if not original.is_file():
            return False
        self.manifest.take_paths[take_storage_key(trial_id, take_id)] = str(original)
        for item in self.manifest.trials:
            if item.get("trial_id") == trial_id:
                item.setdefault("take_ids", [])
                if take_id not in item["take_ids"]:
                    item["take_ids"].append(take_id)
                break
        self._save_manifest()
        return True

    def trash_trial(self, trial_id: str) -> dict[str, Any] | None:
        record_dict = next((dict(item) for item in self.manifest.trials if item.get("trial_id") == trial_id), None)
        if not record_dict:
            return None
        take_ids = list(record_dict.get("take_ids", []))
        take_paths: dict[str, str | None] = {}
        for take_id in take_ids:
            key = self._resolve_take_storage_key(str(take_id), trial_id)
            take_paths[str(take_id)] = self.manifest.take_paths.pop(key, None) if key else None
        directory = self.trial_directory(trial_id)
        trash_directory = self._owned(self.root / ".trash" / "trials" / _safe_name(trial_id))
        if trash_directory.exists():
            shutil.rmtree(trash_directory)
        if directory.exists():
            trash_directory.parent.mkdir(parents=True, exist_ok=True)
            directory.replace(trash_directory)
        self.manifest.trials = [item for item in self.manifest.trials if item.get("trial_id") != trial_id]
        self._save_manifest()
        return {
            "trial": record_dict,
            "take_paths": take_paths,
            "original_directory": str(directory),
            "trash_directory": str(trash_directory),
        }

    def restore_trial(self, payload: dict[str, Any]) -> bool:
        record = dict(payload.get("trial") or {})
        trial_id = str(record.get("trial_id") or "")
        if not trial_id:
            return False
        original = self._owned(payload.get("original_directory") or self.trial_directory(trial_id))
        trash = self._owned(payload.get("trash_directory") or self.root / ".trash" / "trials" / _safe_name(trial_id))
        if trash.exists():
            original.parent.mkdir(parents=True, exist_ok=True)
            if original.exists():
                shutil.rmtree(original)
            trash.replace(original)
        self.manifest.trials = [item for item in self.manifest.trials if item.get("trial_id") != trial_id]
        self.manifest.trials.append(record)
        self.manifest.trials.sort(key=lambda item: str(item.get("created_at", "")))
        for take_id, path in dict(payload.get("take_paths") or {}).items():
            if path:
                restored_path = str(path)
                # Paths still point to the original trial directory after the directory move-back.
                self.manifest.take_paths[take_storage_key(trial_id, str(take_id))] = restored_path
        self._save_manifest()
        return True

    def delete_trial(self, trial_id: str) -> list[str]:
        record = self.trial(trial_id)
        if not record:
            return []
        removed: list[str] = []
        for take_id in list(record.take_ids):
            result = self.delete_take(take_id, trial_id=trial_id, allow_undo=False)
            if result.get("original_path"):
                removed.append(str(result["original_path"]))
        directory = self.trial_directory(trial_id)
        if directory.exists():
            shutil.rmtree(directory)
            removed.append(str(directory))
        self.manifest.trials = [item for item in self.manifest.trials if item.get("trial_id") != trial_id]
        self._save_manifest()
        return removed

    def autosave(self, payload: dict[str, Any]) -> Path:
        self._materialize()
        atomic_write_json(self.autosave_path, payload)
        self._save_manifest()
        return self.autosave_path

    def has_meaningful_content(self) -> bool:
        return bool(
            self.manifest.trials
            or self.manifest.take_paths
            or self.manifest.project_path
            or self.manifest.saved_discovery_ideas
            or self.autosave_path.is_file()
        )

    def close_clean(self) -> bool:
        """Close without creating a recoverable session or leaving empty debris."""

        if not self.has_meaningful_content():
            if self.root.exists():
                shutil.rmtree(self.root, ignore_errors=True)
            self._materialized = False
            return True
        if self.manifest.status == "active":
            self.manifest.status = "closed"
            self._save_manifest()
        return True

    def mark_saved(self, project_path: str | Path) -> None:
        self.manifest.project_path = str(Path(project_path).resolve())
        self.manifest.status = "saved"
        self._save_manifest()

    def mark_for_later(self) -> None:
        self.manifest.status = "recoverable"
        self._save_manifest()

    def rejection_preview(
        self,
        *,
        time_budget_seconds: float = 0.75,
        max_files: int = 50_000,
    ) -> dict[str, Any]:
        """Return a bounded cleanup estimate without unbounded UI traversal."""

        file_count = 0
        total_bytes = 0
        scan_complete = True
        deadline = time.monotonic() + max(0.05, float(time_budget_seconds))
        if self.root.exists():
            for path in self.root.rglob("*"):
                if file_count >= max(1, int(max_files)) or time.monotonic() >= deadline:
                    scan_complete = False
                    break
                try:
                    if path.is_file():
                        file_count += 1
                        total_bytes += path.stat().st_size
                except OSError:
                    continue
        export_conflicts: list[str] = []
        for value in self.manifest.exported_paths:
            try:
                exported = Path(value).resolve()
                if exported == self.root or self.root in exported.parents:
                    export_conflicts.append(str(exported))
            except (OSError, RuntimeError, ValueError):
                continue
        return {
            "session_id": self.session_id,
            "trial_count": len(self.manifest.trials),
            "take_count": len(self.manifest.take_paths),
            "file_count": file_count,
            "bytes": total_bytes,
            "scan_complete": scan_complete,
            "exports_preserved": [value for value in self.manifest.exported_paths if value not in export_conflicts],
            "source_paths_preserved": list(self.manifest.source_paths),
            "protected_export_conflicts": export_conflicts,
        }

    def reject(self) -> list[str]:
        root = self._owned(self.root)
        preview = self.rejection_preview()
        conflicts = list(preview.get("protected_export_conflicts") or [])
        if conflicts:
            raise ValueError(
                "Session cleanup was stopped because a registered exported copy is inside the temporary session folder: "
                + ", ".join(conflicts)
                + ". Move that export outside the session folder, then reject again."
            )
        removed: list[str] = []
        if root.exists():
            removed = [str(path) for path in root.rglob("*")]
            shutil.rmtree(root)
        return removed

    def disk_usage_bytes(self) -> int:
        return int(self.rejection_preview(time_budget_seconds=2.0).get("bytes") or 0)

    @classmethod
    def cleanup_retention(
        cls,
        base_directory: str | Path,
        *,
        empty_after_hours: int = 1,
        corrupt_backup_days: int = 14,
    ) -> dict[str, int]:
        root = Path(base_directory)
        removed_sessions = 0
        removed_backups = 0
        if not root.is_dir():
            return {"removed_sessions": 0, "removed_backups": 0}
        now = datetime.now(timezone.utc)
        for session_root in root.glob("session-*"):
            if not session_root.is_dir():
                continue
            manifest_path = session_root / "session.json"
            payload: dict[str, Any] = {}
            try:
                if manifest_path.is_file():
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                    payload = loaded if isinstance(loaded, dict) else {}
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                payload = {}
            meaningful = bool(
                payload.get("trials")
                or payload.get("take_paths")
                or payload.get("project_path")
                or payload.get("saved_discovery_ideas")
                or (session_root / "autosave.miremix").is_file()
            )
            try:
                modified = datetime.fromtimestamp(session_root.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            age = now - modified
            status = str(payload.get("status") or "")
            if not meaningful and age >= timedelta(hours=max(0, empty_after_hours)):
                shutil.rmtree(session_root, ignore_errors=True)
                if not session_root.exists():
                    removed_sessions += 1
                    continue
            for backup in session_root.glob("session-corrupt-*.json"):
                try:
                    backup_age = now - datetime.fromtimestamp(backup.stat().st_mtime, timezone.utc)
                    if backup_age >= timedelta(days=max(1, corrupt_backup_days)):
                        backup.unlink()
                        removed_backups += 1
                except OSError:
                    continue
        return {"removed_sessions": removed_sessions, "removed_backups": removed_backups}

    @classmethod
    def discover_recoverable(cls, base_directory: str | Path) -> list[dict[str, Any]]:
        root = Path(base_directory)
        results: list[dict[str, Any]] = []
        if not root.is_dir():
            return results
        for manifest_path in sorted(root.glob("session-*/session.json"), reverse=True):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
            meaningful = bool(
                data.get("trials")
                or data.get("take_paths")
                or data.get("project_path")
                or data.get("saved_discovery_ideas")
                or (manifest_path.parent / "autosave.miremix").is_file()
            )
            if data.get("status") in {"active", "recoverable"} and meaningful:
                data["root"] = str(manifest_path.parent)
                results.append(data)
        return results

