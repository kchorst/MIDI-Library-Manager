"""Staged MIDI library scanning for MIDI Remixer."""

from .models import (
    CatalogMatch,
    DeepAnalysisPolicy,
    DuplicateKind,
    QuickScanResult,
    ScanProgress,
    ScanSummary,
)
from .pipeline import LibraryScanService

__all__ = [
    "CatalogMatch",
    "DeepAnalysisPolicy",
    "DuplicateKind",
    "QuickScanResult",
    "ScanProgress",
    "ScanSummary",
    "LibraryScanService",
]

__version__ = "0.17.0"
