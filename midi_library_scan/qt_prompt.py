"""Optional PySide6 dialog. Import only in the Qt application process."""
from __future__ import annotations

from .models import DeepAnalysisPolicy, ScanSummary


def ask_deep_analysis_policy(parent, summary: ScanSummary) -> DeepAnalysisPolicy:
    try:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PySide6 is required for the policy dialog") from exc

    dialog = QDialog(parent)
    dialog.setWindowTitle("MIDI library catalogued")
    dialog.setMinimumWidth(520)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(
        f"Initial cataloguing is complete.\n\n"
        f"{summary.catalogued} files catalogued · {summary.identified} identified · "
        f"{summary.exact_duplicates + summary.likely_duplicates} duplicate/version entries grouped.\n\n"
        "Continue detailed musical analysis? You can pause or change this later."
    ))

    choice = {"value": DeepAnalysisPolicy.LATER}

    def choose(value: DeepAnalysisPolicy) -> None:
        choice["value"] = value
        dialog.accept()

    background = QPushButton("Continue in background")
    background.setDefault(True)
    background.clicked.connect(lambda: choose(DeepAnalysisPolicy.BACKGROUND))
    selected = QPushButton("Analyze songs when selected")
    selected.clicked.connect(lambda: choose(DeepAnalysisPolicy.SELECTED_ONLY))
    later = QPushButton("Later")
    later.clicked.connect(lambda: choose(DeepAnalysisPolicy.LATER))
    layout.addWidget(background)
    layout.addWidget(selected)
    layout.addWidget(later)
    dialog.exec()
    return choice["value"]
