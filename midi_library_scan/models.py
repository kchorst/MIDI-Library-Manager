from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class DeepAnalysisPolicy(str, Enum):
    BACKGROUND = "background"
    SELECTED_ONLY = "selected_only"
    LATER = "later"


class DuplicateKind(str, Enum):
    NONE = "none"
    EXACT = "exact"
    LIKELY_SAME_EXPORT = "likely_same_export"
    SAME_COMPOSITION = "same_composition"


@dataclass(slots=True)
class CatalogMatch:
    composition_id: str | None = None
    artist: str | None = None
    title: str | None = None
    genres: str | None = None
    primary_genre: str | None = None
    confidence: float = 0.0
    status: str = "unmatched"  # exact, high, probable, unmatched
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QuickScanResult:
    path: str
    filename: str
    size_bytes: int
    modified_ns: int
    sha256: str
    midi_type: int | None
    ticks_per_beat: int | None
    track_count: int
    active_channel_count: int
    note_count: int
    duration_seconds: float | None
    bpm: float | None
    bpm_min: float | None
    bpm_max: float | None
    tempo_change_count: int
    key: str | None
    mode: str | None
    key_confidence: float
    key_source: str
    meter: str | None
    arrangement_type: str
    program_families: list[str]
    embedded_text: list[str]
    catalog_match: CatalogMatch = field(default_factory=CatalogMatch)
    family_id: str | None = None
    duplicate_group: str | None = None
    composition_group: str | None = None
    content_duplicate_group: str | None = None
    duplicate_kind: DuplicateKind = DuplicateKind.NONE
    musical_fingerprint: str | None = None
    status: str = "catalogued"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["duplicate_kind"] = self.duplicate_kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuickScanResult":
        payload = dict(data)
        match_raw = payload.pop("catalog_match", {}) or {}
        duplicate_raw = payload.pop("duplicate_kind", DuplicateKind.NONE.value)
        payload["catalog_match"] = CatalogMatch(**dict(match_raw))
        try:
            payload["duplicate_kind"] = DuplicateKind(str(duplicate_raw))
        except ValueError:
            payload["duplicate_kind"] = DuplicateKind.NONE
        return cls(**payload)


@dataclass(slots=True)
class ScanProgress:
    phase: str
    current: int
    total: int
    current_file: str = ""
    message: str = ""
    rate_per_second: float | None = None
    eta_seconds: float | None = None
    current_folder: str = ""


@dataclass(slots=True)
class ScanSummary:
    root: str
    discovered: int = 0
    catalogued: int = 0
    identified: int = 0
    probable_identity: int = 0
    unidentified: int = 0
    exact_duplicates: int = 0
    likely_duplicates: int = 0
    failed: int = 0
    resource_limited: int = 0
    reused: int = 0
    new_files: int = 0
    changed_files: int = 0
    removed_files: int = 0
    cancelled: bool = False
    active_paths: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)
    # Only records that were parsed or otherwise changed during this pass.
    # Keeping this bounded is important for incremental refreshes of very large libraries.
    results: list[QuickScanResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "discovered": self.discovered,
            "catalogued": self.catalogued,
            "identified": self.identified,
            "probable_identity": self.probable_identity,
            "unidentified": self.unidentified,
            "exact_duplicates": self.exact_duplicates,
            "likely_duplicates": self.likely_duplicates,
            "failed": self.failed,
            "resource_limited": self.resource_limited,
            "reused": self.reused,
            "new_files": self.new_files,
            "changed_files": self.changed_files,
            "removed_files": self.removed_files,
            "cancelled": self.cancelled,
            "active_paths": list(self.active_paths),
            "removed_paths": list(self.removed_paths),
            "results": [r.as_dict() for r in self.results],
        }


@dataclass(order=True, slots=True)
class DeepAnalysisJob:
    priority: int
    sequence: int
    path: Path = field(compare=False)
