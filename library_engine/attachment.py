from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Iterable
import logging

from library_engine.client import LibraryEngineClient, LibraryEngineUnavailable
from midi_library_scan.database import LibraryDatabase
from midi_library_scan.lifecycle import EligibilityState
from midi_library_scan.models import DeepAnalysisPolicy


_LOG = logging.getLogger(__name__)


class _RemoteDeepQueue:
    def __init__(self, attachment: "LibraryEngineAttachment") -> None:
        self.attachment = attachment

    def stop(self, timeout: float = 0.0) -> bool:
        self.attachment.pause_deep_analysis()
        return True

    def is_alive(self) -> bool:
        try:
            return bool(self.attachment.client.status(start_if_needed=False).get("deep", {}).get("queue_alive"))
        except (LibraryEngineUnavailable, RuntimeError):
            return False


class EngineDatabaseFacade:
    """Read local committed rows; route all mutations to the Library Engine."""

    def __init__(self, path: str | Path, client: LibraryEngineClient) -> None:
        self.path = Path(path)
        self.client = client
        self._reader = LibraryDatabase(self.path, recover_interrupted=False, read_only=True)

    def close(self) -> None:
        self._reader.close()

    def backfill_family_ids(self, *args, **kwargs) -> int:
        # Schema enrichment is owned by Library Engine before it publishes rows.
        return 0

    def load_result(self, *args, **kwargs):
        return self._reader.load_result(*args, **kwargs)

    def load_results(self, *args, **kwargs):
        return self._reader.load_results(*args, **kwargs)

    def deep_status_counts(self, *args, **kwargs):
        return self._reader.deep_status_counts(*args, **kwargs)

    def deep_status(self, *args, **kwargs):
        return self._reader.deep_status(*args, **kwargs)

    def catalogue_counts(self, *args, **kwargs):
        return self._reader.catalogue_counts(*args, **kwargs)

    def lifecycle_counts(self):
        return self._reader.lifecycle_counts()

    def lifecycle_for_path(self, *args, **kwargs):
        return self._reader.lifecycle_for_path(*args, **kwargs)

    def blessed_paths(self, *args, **kwargs):
        return self._reader.blessed_paths(*args, **kwargs)

    def deep_health_stats(self, *args, **kwargs):
        return self._reader.deep_health_stats(*args, **kwargs)

    def family_index(self, *args, **kwargs):
        return self._reader.family_index(*args, **kwargs)

    def family_id_for_path(self, *args, **kwargs):
        return self._reader.family_id_for_path(*args, **kwargs)

    def family_paths(self, *args, **kwargs):
        return self._reader.family_paths(*args, **kwargs)

    def ignored_pairs(self):
        return self._reader.ignored_pairs()

    def is_pair_ignored(self, *args, **kwargs):
        return self._reader.is_pair_ignored(*args, **kwargs)

    def get_policy(self):
        return self._reader.get_policy()

    def ignore_pair(self, identity_a: str, identity_b: str, label_a: str = "Song A", label_b: str = "Song B") -> None:
        self.client.command(
            "ignore_pair", identity_a=identity_a, identity_b=identity_b, label_a=label_a, label_b=label_b
        )

    def restore_pair(self, identity_a: str, identity_b: str) -> None:
        self.client.command("restore_pair", identity_a=identity_a, identity_b=identity_b)

    def prioritize_paths(self, paths: Iterable[str | Path], priority: int) -> int:
        response = self.client.command("prioritize", paths=[str(path) for path in paths], priority=int(priority))
        return int(response.get("prioritized") or 0)

    def delete_paths(self, paths: Iterable[str | Path]) -> int:
        response = self.client.command("remove_paths", paths=[str(path) for path in paths], permanent=False, timeout=15.0)
        return int(response.get("removed") or 0)

    def clear(self) -> None:
        raise RuntimeError("Library rebuild is managed by MIDI Library Manager, not MIDI Remixer.")

    def checkpoint(self, *args, **kwargs) -> None:
        return None


class LibraryEngineAttachment:
    """Compatibility surface used by Remixer while the Engine owns heavy work."""

    def __init__(self, app_root: str | Path, database_path: str | Path | None = None) -> None:
        self.app_root = Path(app_root).resolve()
        self.client = LibraryEngineClient(
            self.app_root, required_capabilities={
                "catalogue_deltas", "prepared_recommendations", "related_candidates",
                "read_committed_library", "storage_contract", "persisted_lifecycle_eligibility",
            }
        )
        self.client.ensure_running(timeout=20.0)
        status = self.client.status()
        storage = status.get("storage") if isinstance(status, dict) else None
        if not isinstance(storage, dict):
            raise LibraryEngineUnavailable(
                "The running MIDI Library Engine did not publish its database storage contract."
            )
        try:
            library_root = Path(str(storage.get("library_root") or "")).expanduser().resolve()
            quick_database = Path(str(storage.get("quick_database") or "")).expanduser().resolve()
            detail_database = Path(str(storage.get("detail_database") or "")).expanduser().resolve()
            relationship_database = Path(str(storage.get("relationship_database") or "")).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise LibraryEngineUnavailable(f"Library Engine published invalid database paths: {exc}") from exc
        if not library_root.is_dir():
            raise LibraryEngineUnavailable(f"Library Engine storage folder is unavailable: {library_root}")
        for label, path in (
            ("catalogue", quick_database), ("detailed analysis", detail_database),
            ("recommendations", relationship_database),
        ):
            if path.parent != library_root:
                raise LibraryEngineUnavailable(
                    f"Library Engine {label} database is outside its published library root: {path}"
                )
            if not path.is_file():
                raise LibraryEngineUnavailable(f"Library Engine {label} database is missing: {path}")
        if database_path is not None and Path(database_path).expanduser().resolve() != quick_database:
            raise LibraryEngineUnavailable(
                "MIDI Remixer and MIDI Library Engine resolved different catalogue databases. "
                f"Remixer expected {Path(database_path).expanduser().resolve()}, Engine owns {quick_database}."
            )
        self.library_root = library_root
        self.database_path = quick_database
        self.library_index_path = detail_database
        self.relationship_database_path = relationship_database
        self.storage_contract = dict(storage)
        self.database = EngineDatabaseFacade(self.database_path, self.client)
        self.cancel_event = Event()
        self.deep_queue = _RemoteDeepQueue(self)

    def cached_results(self, roots=None, *, validate_sources: bool = True):
        return self.database.load_results(roots, validate_sources=validate_sources)

    def version_fusion_candidates(self) -> dict[str, str]:
        """Return persisted Fusion-eligible paths keyed to their Library family IDs.

        Version Fusion must consume the authoritative Library classification, not
        rediscover same-song/version families inside Remixer.  Only rows currently
        persisted as Blessed + Fusion Candidate are exposed; lifecycle changes are
        therefore reflected on the next read.
        """
        eligible = self.database.blessed_paths(EligibilityState.FUSION_CANDIDATE.value)
        family_by_path = self.database.family_index()
        return {
            path: family_by_path.get(path, f"path:{path}")
            for path in sorted(eligible, key=str.casefold)
        }

    def current_policy(self) -> DeepAnalysisPolicy:
        raw = self.database.get_policy() or DeepAnalysisPolicy.LATER.value
        try:
            return DeepAnalysisPolicy(str(raw))
        except ValueError:
            return DeepAnalysisPolicy.LATER

    def deep_progress(self, only_paths=None):
        local = dict(self.database.deep_status_counts(only_paths))
        try:
            remote = self.client.status(start_if_needed=False).get("deep", {})
        except (LibraryEngineUnavailable, RuntimeError):
            remote = {}
        local.update({key: value for key, value in remote.items() if key.startswith("queue_")})
        local["policy"] = self.current_policy().value
        return local

    def analyze_selected(self, path: str | Path) -> None:
        self.client.command("prioritize", paths=[str(path)], priority=0)

    def retry_failed_deep_analysis(self) -> int:
        return int(self.client.command("retry_failed").get("retried") or 0)

    def pause_deep_analysis(self) -> None:
        self.client.command("pause_analysis", start_if_needed=False)

    def suspend_deep_analysis(self) -> bool:
        if self.current_policy() != DeepAnalysisPolicy.BACKGROUND:
            return False
        self.client.command("pause_analysis", start_if_needed=False)
        return True

    def resume_suspended_deep_analysis(self, only_paths=None) -> bool:
        self.client.command("resume_analysis")
        return True

    def resume_deep_analysis(self, only_paths=None) -> None:
        self.client.command("resume_analysis")

    def apply_deep_policy(self, policy: DeepAnalysisPolicy, selected_path=None, only_paths=None) -> None:
        if policy == DeepAnalysisPolicy.BACKGROUND:
            self.client.command("resume_analysis")
        elif policy == DeepAnalysisPolicy.SELECTED_ONLY and selected_path:
            try:
                self.client.command("pause_analysis", start_if_needed=False)
            except LibraryEngineUnavailable:
                pass
            self.client.command("prioritize", paths=[str(selected_path)], priority=0)
        else:
            self.client.command("pause_analysis", start_if_needed=False)

    def cancel_scan(self) -> None:
        self.cancel_event.set()
        try:
            self.client.command("cancel_scan", start_if_needed=False)
        except (LibraryEngineUnavailable, RuntimeError) as exc:
            # Cancellation is best-effort from the Remixer client, but never hide
            # a lost Engine connection.  The local cancel_event still prevents
            # this client from waiting on stale work.
            _LOG.warning("Library Engine scan cancellation request failed: %s", exc)

    def quick_catalog_paths(self, *args, **kwargs):
        raise RuntimeError("Library scanning is owned by MIDI Library Manager/Engine.")

    def close(self) -> None:
        self.database.close()
