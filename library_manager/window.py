from __future__ import annotations

from pathlib import Path
import os
import json
import sys
import threading
import time
from datetime import datetime
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from shared_ui.theme import load_stylesheet
from core.app_paths import AppPaths
from core.library_backup import create_library_intelligence_backup, restore_library_intelligence_backup, verify_library_intelligence_backup
from core.python_runtime import launch_gui_python_script
from core.progress_display import format_eta, format_file_rate
from library_engine.client import LibraryEngineClient, LibraryEngineUnavailable
from library_engine.protocol import ENGINE_PRODUCT_VERSION
from library_engine.timing import (
    CLIENT_SHUTDOWN_WAIT_SECONDS, ENGINE_STOP_FEEDBACK_SECONDS, MANAGER_BUSY_FEEDBACK_SECONDS,
    MANAGER_FULL_EXIT_FORCE_SECONDS, SHUTDOWN_REQUEST_SECONDS, STATUS_POLL_SECONDS,
)
from library_manager.version import MANAGER_PRODUCT_VERSION
from library_manager.browser import LibraryBrowserDialog
from library_manager.catalogue_reader import CatalogueReader
from library_manager.shell_actions import open_folder



def _soft_wrap_runtime_text(value: object) -> str:
    """Allow Qt labels to wrap long paths/exception tokens without widening HOME.

    QLabel word-wrap only breaks at normal word boundaries. Windows paths and
    exception payloads can be one very long token, which previously forced the
    scroll area's content width far beyond the viewport and pushed Deep Analysis
    off-screen. Zero-width break opportunities are display-only; the raw detail
    remains available in the tooltip.
    """
    text = str(value or "")
    for token in ("\\", "/", ";", ",", ":"):
        text = text.replace(token, token + "\u200b")
    return text


class MidiLibraryManagerWindow(QMainWindow):
    """Administrative UI for the independent MIDI Library Engine."""

    statusFetched = Signal(object, str)
    commandFinished = Signal(str, object, str)
    longActionFinished = Signal(str, object, str)
    engineStopFinished = Signal(bool, str)
    engineForceStopFinished = Signal(bool, str)
    offlineSummaryLoaded = Signal(object)
    shellActionFinished = Signal(str, str)  # action, error

    def __init__(self) -> None:
        super().__init__()
        self.root = Path(__file__).resolve().parents[1]
        self.paths = AppPaths.for_app(self.root).ensure()
        self.catalogue_reader = CatalogueReader(self.paths.library / "library_scan.sqlite3")
        self._library_browser: LibraryBrowserDialog | None = None
        self._last_deep_status: dict[str, Any] = {}
        self._last_scan_running = False
        self._last_scan_operation_kind = "idle"
        self._fast_recovery_start_failure_count: int | None = None
        self._scan_cancel_requested = False
        self._scan_terminal_notice = ""
        self._configured_source_roots: list[str] = []
        self._accessible_source_roots: list[str] = []
        self._offline_summary_cache: dict[str, Any] = {}
        self._offline_summary_at = 0.0
        self._offline_summary_inflight = False
        self._offline_summary_groups_requested = False
        self.client = LibraryEngineClient(
            self.root, required_capabilities={
                "maintenance", "background_analysis", "library_health", "library_review_retry", "library_backup_restore", "review_disposition"
            }
        )
        self.setWindowTitle(f"MIDI Library Manager {MANAGER_PRODUCT_VERSION} · Engine {ENGINE_PRODUCT_VERSION}")
        self.setStyleSheet(load_stylesheet("base.qss") + """
            QWidget#LibraryManagerPage { background: #f5f7fa; }
            QFrame#LibrarySection, QFrame#LibraryHeaderSection { background: #ffffff; border: 1px solid #d9e0e7; border-radius: 8px; }
            QLabel#LibrarySectionTitle { font-size: 14px; font-weight: 700; color: #17202a; }
            QLabel#LibrarySectionHint { color: #66717d; font-size: 11px; }
            QLabel#LibraryStateValue { font-size: 13px; font-weight: 700; color: #23364a; }
            QProgressBar#LibraryPrimaryProgress {
                min-height: 30px; max-height: 30px; text-align: center; font-size: 12px; font-weight: 700;
                color: #ffffff; background: #718399; border: 2px solid #5f748d; border-radius: 7px;
            }
            QProgressBar#LibraryPrimaryProgress::chunk { background: #2563eb; border-radius: 5px; }
            QPushButton { min-height: 28px; }
            QPushButton#PlayButton { background:#2f8f4e; color:#ffffff; border:1px solid #267640; font-weight:700; }
            QPushButton#PlayButton:hover { background:#277d43; border-color:#1f6536; }
            QPushButton#PlayButton[transportState="pause"] { background:#f4a340; color:#17202a; border:1px solid #c57b13; }
            QPushButton#PlayButton[transportState="pause"]:hover { background:#ffb64d; border-color:#b56d0c; }
            QPushButton#PlayButton:disabled { background:#dbe2df; color:#7c8781; border:1px solid #c4cec8; }
            QPushButton#PauseButton { background:#f4a340; color:#17202a; border:1px solid #c57b13; font-weight:700; }
            QPushButton#PauseButton:hover { background:#ffb64d; }
            QPushButton#PauseButton:disabled { background:#e6ebf0; color:#9aa5b1; border:1px solid #cbd3dc; }
            QPushButton#StopButton { background:#e4e7eb; color:#69717b; border:1px solid #c6cbd2; font-weight:700; }
            QPushButton#StopButton[transportState="active"] { background:#b93636; color:#ffffff; border:1px solid #9f2c2c; }
            QPushButton#StopButton[transportState="active"]:hover { background:#a52f2f; border-color:#842424; }
            QPushButton#StopButton:disabled { background:#e4e7eb; color:#9aa1a9; border:1px solid #c6cbd2; }
            QPushButton#DangerButton:disabled { background: #e6ebf0; color: #9aa5b1; border: 1px solid #cbd3dc; }
            QPushButton#PrimaryButton:disabled { background: #dfe5eb; color: #8b97a4; border: 1px solid #c5ced7; }
            QPushButton:disabled { color: #9aa5b1; }
            QListWidget#LibraryRootList { min-height: 28px; max-height: 30px; }
        """)
        self.resize(1360, 760)
        self.setMinimumSize(1000, 620)
        self._qa_layout_only = bool(str(os.environ.get("MIDI_LIBRARY_QA_LAYOUT_ONLY", "")).strip())
        self._qa_close_on_active_scan_triggered = False
        self._qa_close_on_state_triggered = False
        self._qa_maintenance_started = False
        self.offlineSummaryLoaded.connect(self._apply_offline_summary)
        self.shellActionFinished.connect(self._finish_shell_action)
        self._build_ui()
        self._manager_busy_reasons: dict[str, int] = {}
        self._manager_busy_delay = QTimer(self)
        self._manager_busy_delay.setSingleShot(True)
        self._manager_busy_delay.setInterval(int(MANAGER_BUSY_FEEDBACK_SECONDS * 1000))
        self._manager_busy_delay.timeout.connect(self._show_manager_busy)
        self._engine_ready = False
        self._engine_connecting = True
        self._engine_connect_error = ""
        self._engine_connect_started = time.monotonic()
        self._engine_connect_cancel = threading.Event()
        self._engine_connect_thread: threading.Thread | None = None
        self._engine_stopped_by_user = False
        self._engine_initial_status: dict[str, Any] | None = None
        self._engine_preferences_applied = False
        self._status_poll_inflight = False
        self._status_failure_count = 0
        self._long_action_inflight = ""
        self._long_action_context: dict[str, Any] = {}
        self._command_inflight: set[str] = set()
        self._command_contexts: dict[str, dict[str, Any]] = {}
        self._close_in_progress = False
        self._close_after_browser = False
        self._exit_after_maintenance = False
        self._exit_after_engine_stop = False
        self._full_exit_verified = False
        self._engine_stop_started_monotonic = 0.0
        self._engine_stop_full_exit = False
        self._engine_stop_target: dict[str, Any] = {}
        self._engine_stop_escalation_started = False
        self._engine_stop_feedback_timer = QTimer(self)
        self._engine_stop_feedback_timer.setInterval(int(ENGINE_STOP_FEEDBACK_SECONDS * 1000))
        self._engine_stop_feedback_timer.timeout.connect(self._refresh_engine_stop_feedback)
        self._latest_engine_status: dict[str, Any] = {}
        self._set_engine_controls_enabled(False)
        self.statusFetched.connect(self._apply_polled_status)
        self.commandFinished.connect(self._finish_command)
        self.longActionFinished.connect(self._finish_long_action)
        self.engineStopFinished.connect(self._finish_engine_stop)
        self.engineForceStopFinished.connect(self._finish_engine_force_stop)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(int(STATUS_POLL_SECONDS * 1000))
        self._poll_timer.timeout.connect(self._poll_status)
        # Target-Windows QA can prove that the real Qt event loop continues to
        # advance while Engine work, cancellation, and Manager maintenance are
        # active. This timer is completely inert in normal product use.
        self._qa_event_loop_tick = 0
        self._qa_browser_opened = False
        self._qa_event_loop_timer = QTimer(self)
        self._qa_event_loop_timer.setInterval(100)
        self._qa_event_loop_timer.timeout.connect(self._record_qa_event_loop_heartbeat)
        if str(os.environ.get("MIDI_LIBRARY_QA_EVENT_LOOP_HEARTBEAT", "")).strip():
            self._qa_event_loop_timer.start()
        # Target-Windows qualification can request a Close at the exact
        # pre-endpoint startup boundary.  The timer cannot fire until Qt's event
        # loop is running, which means the real Manager window has already been
        # shown.  This avoids external HWND discovery races while still invoking
        # QWidget.close() -> the production closeEvent path.
        self._qa_startup_lock_close_timer = QTimer(self)
        self._qa_startup_lock_close_timer.setInterval(25)
        self._qa_startup_lock_close_timer.timeout.connect(self._maybe_trigger_qa_close_on_startup_lock)
        if str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_ON_STARTUP_LOCK", "")).strip():
            self._qa_startup_lock_close_timer.start()
        if self._qa_layout_only:
            # Target-machine geometry QA must be able to render the exact Qt
            # dashboard without launching or attaching the detached Engine.
            self._engine_connecting = False
            self._engine_ready = False
            self.engine_state.setText("State · Layout self-check")
            self.library_folder_status.setText("MIDI folders: C:\\Users\\Example\\Music")
            self.storage_status.setText("Saved catalogue: 96,362 MIDI(s)")
            self.scan_label.setText("Fast catalogue: Comparing file changes · 91,648/96,362 · 8,378.9 files/s")
            self.scan_progress.setRange(0, 96362)
            self.scan_progress.setValue(91648)
            self.scan_progress.setFormat("95% · 91,648/96,362")
            self.deep_label.setText("Deep: complete · 91,105 analyzed · 101 review items")
            self.deep_progress.setRange(0, 1)
            self.deep_progress.setValue(1)
            self.deep_progress.setFormat("Done · 91,105")
            # Geometry QA must exercise the longest normal state-aware Deep
            # action, not merely the shorter default Start / Resume label.
            self.run_deep_button.setText("Review (101)…")
            self.run_deep_button.setEnabled(True)
            self.relationship_label.setText("4,122,987 relationships · 45,403 families ready")
            self.relationship_progress.setText("Paused — Fast Scan has priority")
            self.roots_list.clear()
            self.roots_list.addItem(r"C:\Users\Example\Music")
            self.fast_failures_button.setText("Unreadable (5,156)…")
            self.suspicious_button.setText("Unusual (2,491)…")
            self.status_detail.setText("Normal operation. Completed work is saved continuously.")
        else:
            self._begin_manager_busy("engine_start")
            # Engine startup/migration belongs off the Qt thread. The Manager
            # appears immediately and remains repaintable during migration.
            self._engine_connect_cancel.clear()
            self._engine_connect_thread = threading.Thread(
                target=self._connect_engine_background, name="MidiLibraryManagerEngineConnect", daemon=True
            )
            self._engine_connect_thread.start()
            self._poll_timer.start()
            self._poll_status()

    def _connect_engine_background(self) -> None:
        """Attach without confusing a slow status snapshot with a dead Engine."""
        cancelled_for_full_exit = False
        try:
            self.client.ensure_running(
                timeout=30.0, require_service_ready=False, cancel_event=self._engine_connect_cancel
            )
            # A successful ping is the lifetime/IPC boundary.  Status is richer
            # but may briefly lag while a large existing catalogue opens or an
            # aggregate refresh is underway; that must not turn the whole UI
            # into a false "Engine unavailable" state.
            self._engine_ready = True
            status: dict[str, Any] = {}
            try:
                status = self.client.status(start_if_needed=False, timeout=8.0)
            except (LibraryEngineUnavailable, RuntimeError) as exc:
                if self.client.ping(timeout=1.0) is None:
                    raise
                self._engine_connect_error = (
                    f"Library Engine heartbeat is live; detailed status is still warming: {type(exc).__name__}: {exc}"
                )
            if status:
                self._engine_initial_status = dict(status)
        except (LibraryEngineUnavailable, RuntimeError) as exc:
            self._engine_ready = False
            cancelled_for_full_exit = bool(
                self._engine_connect_cancel.is_set() and self._exit_after_engine_stop
            )
            if not cancelled_for_full_exit:
                self._engine_connect_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._engine_connecting = False
            if self._engine_connect_cancel.is_set() and self._exit_after_engine_stop:
                # Startup may have been cancelled before endpoint publication or
                # immediately after Popen.  Perform one final authoritative
                # shutdown check before telling Qt that full exit is safe.
                try:
                    stopped = self.client.shutdown(
                        request_timeout=SHUTDOWN_REQUEST_SECONDS,
                        wait_timeout=CLIENT_SHUTDOWN_WAIT_SECONDS,
                    )
                    detail = str(self.client.last_shutdown_detail or "")
                    if not stopped and not detail:
                        detail = "Engine startup cancellation did not prove the Library process tree terminal."
                except (LibraryEngineUnavailable, OSError, RuntimeError, ValueError) as exc:
                    stopped = False
                    detail = f"{type(exc).__name__}: {exc}"
                self.engineStopFinished.emit(stopped, detail)

    def _build_ui(self) -> None:
        """Build a task-oriented one-screen operator dashboard.

        The Manager is organized by user workflow, not by subsystem ownership:
        the header identifies the Library and its persisted curation/readiness state,
        Fast/Deep work shares the primary row, and all source/review/maintenance
        controls live in one full-width operations card.

        The supported release target is 1280x720 logical pixels or larger. A
        scroll area remains only as an accessibility/smaller-window fallback;
        routine operation at the release target must not require scrolling.
        """
        self._statistics_values: dict[str, int] = {
            "catalogued": 0, "distinct": 0, "health_clear": 0, "suspicious": 0, "fast_failed": 0,
            "deep_complete": 0, "deep_remaining": 0, "failed": 0,
            "guided": 0, "relationships": 0,
            "discovered": 0, "usable": 0, "mash_ready": 0, "fusion_candidate": 0,
            "usable_not_mash": 0, "needs_review": 0, "rejected": 0, "quarantined": 0, "unresolved": 0,
        }

        scroll = QScrollArea()
        scroll.setObjectName("LibraryManagerScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.dashboard_scroll = scroll

        central = QWidget()
        central.setObjectName("LibraryManagerPage")
        central.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        central.setMinimumWidth(0)
        self.dashboard_content = central
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        self.dashboard_outer_layout = outer
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)

        def section(title_text: str, hint_text: str = "") -> tuple[QFrame, QVBoxLayout]:
            frame = QFrame()
            frame.setObjectName("LibrarySection")
            frame.setFrameShape(QFrame.Shape.StyledPanel)
            frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(8, 4, 8, 5)
            layout.setSpacing(3)
            heading = QHBoxLayout()
            heading.setSpacing(7)
            title = QLabel(title_text)
            title.setObjectName("LibrarySectionTitle")
            heading.addWidget(title)
            if hint_text:
                hint = QLabel(hint_text)
                hint.setObjectName("LibrarySectionHint")
                hint.setWordWrap(False)
                hint.setToolTip(hint_text)
                heading.addWidget(hint)
            heading.addStretch(1)
            layout.addLayout(heading)
            return frame, layout

        # Compact identity/status header. No child is allowed to expand it
        # vertically; surplus window height belongs below the dashboard.
        header = QFrame()
        header.setObjectName("LibraryHeaderSection")
        header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header_frame = header
        header_layout = QGridLayout(header)
        header_layout.setContentsMargins(10, 5, 10, 5)
        header_layout.setHorizontalSpacing(8)
        header_layout.setVerticalSpacing(2)

        identity_actions = QHBoxLayout()
        identity_actions.setSpacing(6)
        title = QLabel(f"MIDI Library Manager {MANAGER_PRODUCT_VERSION}")
        title.setObjectName("AppTitle")
        identity_actions.addWidget(title)
        self.browse_library_button = QPushButton("Browse Library")
        self.browse_library_button.setObjectName("PrimaryButton")
        self.browse_library_button.setToolTip("View/search the saved MIDI catalogue. Works even when the Library Engine is stopped.")
        self.browse_library_button.clicked.connect(self._open_library_browser)
        identity_actions.addWidget(self.browse_library_button)
        self.statistics_button = QPushButton("Statistics")
        self.statistics_button.clicked.connect(self._show_statistics)
        identity_actions.addWidget(self.statistics_button)
        self.engine_action_button = QPushButton("Retry Engine Start")
        self.engine_action_button.setVisible(False)
        self.engine_action_button.clicked.connect(self._restart_engine)
        identity_actions.addWidget(self.engine_action_button)
        identity_actions.addStretch(1)
        header_layout.addLayout(identity_actions, 0, 0)

        self.engine_state = QLabel("Starting Library Engine…")
        self.engine_state.setObjectName("LibraryStateValue")
        self.engine_state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.engine_state.setWordWrap(True)
        self.engine_state.setMaximumHeight(34)
        self.engine_state.setMinimumWidth(240)
        header_layout.addWidget(self.engine_state, 0, 1)

        self.library_folder_status = QLabel("MIDI folders: checking Engine configuration…")
        self.library_folder_status.setObjectName("Subtitle")
        self.library_folder_status.setWordWrap(False)
        self.library_folder_status.setMinimumWidth(0)
        self.library_folder_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header_layout.addWidget(self.library_folder_status, 1, 0)
        self.storage_status = QLabel("Saved catalogue: checking connection…")
        self.storage_status.setObjectName("Subtitle")
        self.storage_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.storage_status.setWordWrap(False)
        self.storage_status.setMinimumWidth(0)
        self.storage_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header_layout.addWidget(self.storage_status, 1, 1)
        self.curation_status = QLabel("Curated Library: loading persisted lifecycle…")
        self.curation_status.setObjectName("Subtitle")
        self.curation_status.setWordWrap(False)
        self.curation_status.setMaximumHeight(24)
        self.curation_status.setMinimumWidth(0)
        self.curation_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.curation_status.setToolTip(
            "These are persisted lifecycle counts. Only Blessed · Mash Ready MIDI enters Guided/Assisted recommendations."
        )
        header_layout.addWidget(self.curation_status, 2, 0, 1, 2)
        header_layout.setColumnStretch(0, 3)
        header_layout.setColumnStretch(1, 2)
        outer.addWidget(header)

        self.engine_error_banner = QLabel("")
        self.engine_error_banner.setObjectName("EngineErrorBanner")
        self.engine_error_banner.setWordWrap(True)
        self.engine_error_banner.setMinimumWidth(0)
        self.engine_error_banner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.engine_error_banner.setVisible(False)
        self.engine_error_banner.setStyleSheet(
            "QLabel#EngineErrorBanner { background: #fff4f4; border: 1px solid #d66; "
            "border-radius: 6px; padding: 6px; color: #7a1111; font-weight: 600; }"
        )
        outer.addWidget(self.engine_error_banner)

        # Primary work row: Fast and Deep have equal visual weight and use the
        # same progress/action pattern.
        fast_frame, fast = section("Fast Scan", "Normal update · new/changed MIDI")
        self.fast_frame = fast_frame
        self.fast_frame.setMinimumHeight(116)
        self.scan_label = QLabel("Fast catalogue: waiting")
        self.scan_label.setWordWrap(True)
        self.scan_label.setMaximumHeight(38)
        self.scan_label.setMinimumWidth(0)
        self.scan_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        fast.addWidget(self.scan_label)
        fast_row = QHBoxLayout()
        fast_row.setSpacing(6)
        self.scan_progress = QProgressBar()
        self.scan_progress.setObjectName("LibraryPrimaryProgress")
        self.scan_progress.setFormat("%p% · %v/%m")
        self.scan_progress.setMinimumWidth(220)
        self.scan_progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        fast_row.addWidget(self.scan_progress, 1)
        self.run_fast_button = QPushButton("Start")
        self.run_fast_button.setObjectName("PrimaryButton")
        self.run_fast_button.setToolTip("Start a Fast Scan of the configured MIDI folders.")
        self.run_fast_button.clicked.connect(lambda: self._dispatch_command("scan", "scan", message="Starting Fast Scan…"))
        self.pause_scan_button = QPushButton("Pause")
        self.pause_scan_button.setObjectName("PauseButton")
        self.pause_scan_button.setToolTip("Pause or resume the active Fast Scan.")
        self.pause_scan_button.clicked.connect(self._toggle_fast_scan)
        self.cancel_scan_button = QPushButton("Cancel")
        self.cancel_scan_button.setObjectName("DangerButton")
        self.cancel_scan_button.setToolTip("Cancel the active Fast Scan. Completed batches remain saved.")
        self.cancel_scan_button.clicked.connect(self._cancel_fast_scan)
        for button in (self.run_fast_button, self.pause_scan_button, self.cancel_scan_button):
            button.setMinimumWidth(68)
            button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            fast_row.addWidget(button)
        fast.addLayout(fast_row)
        self.scan_location = QLabel("")
        self.scan_location.setVisible(False)

        deep_frame, deep_layout = section("Deep Analysis", "Musical profiling · saved incrementally")
        self.deep_frame = deep_frame
        self.deep_frame.setMinimumHeight(116)
        self.deep_label = QLabel("Deep Analysis — WAITING")
        self.deep_label.setWordWrap(True)
        self.deep_label.setMaximumHeight(38)
        self.deep_label.setMinimumWidth(0)
        self.deep_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        deep_layout.addWidget(self.deep_label)
        deep_row = QHBoxLayout()
        deep_row.setSpacing(6)
        self.deep_progress = QProgressBar()
        self.deep_progress.setObjectName("LibraryPrimaryProgress")
        self.deep_progress.setFormat("%p% · %v/%m")
        self.deep_progress.setMinimumWidth(220)
        self.deep_progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        deep_row.addWidget(self.deep_progress, 1)
        self.run_deep_button = QPushButton("Start / Resume")
        self.run_deep_button.setObjectName("PrimaryButton")
        self.run_deep_button.setToolTip("This state-aware button only performs the action named on it: start/resume Deep Analysis, run the one-time first-failure retry, or open Deep Analysis Review.")
        self.run_deep_button.clicked.connect(self._start_or_resume_deep_analysis)
        self.pause_analysis_button = QPushButton("Pause")
        self.pause_analysis_button.setObjectName("PauseButton")
        self.pause_analysis_button.setToolTip("Pause Deep Analysis. Completed files remain saved.")
        self.pause_analysis_button.clicked.connect(self._toggle_deep_analysis)
        self.run_deep_button.setMinimumWidth(108)
        self.run_deep_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.pause_analysis_button.setMinimumWidth(84)
        self.pause_analysis_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.cancel_analysis_button = QPushButton("Cancel")
        self.cancel_analysis_button.setObjectName("DangerButton")
        self.cancel_analysis_button.setToolTip("Cancel the active Deep pass. Completed files remain saved; Start / Resume continues unfinished work later.")
        self.cancel_analysis_button.clicked.connect(self._cancel_deep_analysis)
        self.cancel_analysis_button.setMinimumWidth(84)
        self.cancel_analysis_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        deep_row.addWidget(self.run_deep_button)
        deep_row.addWidget(self.pause_analysis_button)
        deep_row.addWidget(self.cancel_analysis_button)
        deep_layout.addLayout(deep_row)

        primary_grid = QGridLayout()
        primary_grid.setHorizontalSpacing(6)
        primary_grid.setVerticalSpacing(0)
        primary_grid.addWidget(fast_frame, 0, 0)
        primary_grid.addWidget(deep_frame, 0, 1)
        primary_grid.setColumnStretch(0, 1)
        primary_grid.setColumnStretch(1, 1)
        outer.addLayout(primary_grid)

        # Recommendation readiness remains visible, but it is part of the existing
        # Library summary rather than a standalone decorative/status band.  The
        # banded design was explicitly retired and is a permanent regression gate.
        self.relationship_label = QLabel("Recommendation data: loading persisted readiness…")
        self.relationship_label.setObjectName("LibraryStateValue")
        self.relationship_label.setWordWrap(False)
        self.relationship_label.setMinimumWidth(0)
        self.relationship_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.relationship_label.setToolTip(
            "Persisted relationship/family readiness used by Remixer. Guided/Assisted eligibility still requires Blessed + Mash Ready Library state."
        )
        self.relationship_progress = QLabel("Loading saved recommendation readiness…")
        self.relationship_progress.setObjectName("LibrarySectionHint")
        self.relationship_progress.setWordWrap(False)
        self.relationship_progress.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.relationship_progress.setMinimumWidth(0)
        self.relationship_progress.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.relationship_progress.setToolTip(
            "Recommendation readiness is a state indicator, not a percentage. The Engine does not have a trustworthy fixed denominator while families and relationships are rebuilt incrementally."
        )
        readiness_row = QHBoxLayout()
        readiness_row.setContentsMargins(0, 0, 0, 0)
        readiness_row.setSpacing(8)
        readiness_row.addWidget(self.relationship_label, 4)
        readiness_row.addWidget(self.relationship_progress, 1)
        header_layout.addLayout(readiness_row, 3, 0, 1, 2)

        # One full-width operations workspace. This avoids the v0.37.7/0.37.8
        # split-card imbalance while remaining materially shorter than the old
        # pre-0.37.7 vertically stacked control screen.
        ops_frame, ops = section("Library & Controls", "Sources · review · maintenance")
        self.controls_frame = ops_frame

        # One full-width workspace, internally balanced into two functional
        # columns. Source/performance controls live together on the left; review
        # and maintenance actions live together on the right. This uses wide
        # screens without recreating the old disconnected-card layout.
        ops_body = QGridLayout()
        ops_body.setHorizontalSpacing(12)
        ops_body.setVerticalSpacing(4)

        source_col = QVBoxLayout()
        source_col.setSpacing(4)
        source_col.setAlignment(Qt.AlignmentFlag.AlignTop)
        performance_row = QHBoxLayout()
        performance_row.setSpacing(6)
        performance_row.addWidget(QLabel("Performance:"))
        self.throughput = QComboBox()
        self.throughput.addItem("Maximum throughput", "maximum")
        self.throughput.addItem("Balanced", "balanced")
        self.throughput.addItem("Low impact", "low_impact")
        self.throughput.currentIndexChanged.connect(self._change_throughput)
        performance_row.addWidget(self.throughput)
        self.auto_start_analysis = QCheckBox("Auto-resume Deep")
        self.auto_start_analysis.setChecked(True)
        self.auto_start_analysis.setToolTip(
            "Recommended. Controls whether Deep Analysis automatically resumes after the Engine checks configured source folders for changes. Fast Scan may still run at Engine startup to keep the saved catalogue current."
        )
        self.auto_start_analysis.toggled.connect(self._change_auto_start)
        performance_row.addWidget(self.auto_start_analysis)
        performance_row.addStretch(1)
        source_col.addLayout(performance_row)

        source_col.addWidget(QLabel("MIDI source folders:"))
        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        self.roots_list = QListWidget()
        self.roots_list.setObjectName("LibraryRootList")
        # Source folders are a compact configuration list, not a primary work
        # surface. Show a few rows and scroll when users have many roots rather
        # than stretching one configured folder into a large empty rectangle.
        self.roots_list.setMinimumHeight(52)
        self.roots_list.setMaximumHeight(82)
        folder_row.addWidget(self.roots_list, 1)
        self.add_folder_button = QPushButton("Add Folder…")
        self.add_folder_button.setToolTip("Add a folder containing MIDI files to Library sources. After selection, the Library starts the appropriate incremental Fast Scan; background Deep Analysis/readiness also resumes when that preference is enabled.")
        self.add_folder_button.clicked.connect(self._add_folder)
        folder_row.addWidget(self.add_folder_button)
        self.remove_folder_button = QPushButton("Remove…")
        self.remove_folder_button.setToolTip("Remove the highlighted folder from active Library sources after confirmation. Source MIDI files are never deleted. If other sources remain, Fast Scan reconciles rows from the removed source; if this is the last source, the saved Library is preserved as an offline snapshot until a source is added again.")
        self.remove_folder_button.clicked.connect(self._remove_folder)
        folder_row.addWidget(self.remove_folder_button)
        source_col.addLayout(folder_row)
        source_col.addStretch(1)
        ops_body.addLayout(source_col, 0, 0, Qt.AlignmentFlag.AlignTop)

        action_col = QGridLayout()
        action_col.setHorizontalSpacing(6)
        action_col.setVerticalSpacing(4)
        action_col.addWidget(QLabel("Review & maintenance:"), 0, 0, 1, 2)
        self.fast_failures_button = QPushButton("Unreadable…")
        self.fast_failures_button.setToolTip("Review MIDI files that Fast Scan could not read. Retry only after a file or Library scanner change; MIDI Library does not repair source files.")
        self.fast_failures_button.clicked.connect(lambda: self._open_library_browser("quick_failed"))
        action_col.addWidget(self.fast_failures_button, 1, 0)
        self.suspicious_button = QPushButton("Unusual…")
        self.suspicious_button.setToolTip("Review MIDI files that loaded successfully but look unusually short, long, sparse, or empty.")
        self.suspicious_button.clicked.connect(self._review_suspicious)
        action_col.addWidget(self.suspicious_button, 1, 1)
        self.quarantine_folder_button = QPushButton("Open Quarantine…")
        self.quarantine_folder_button.setToolTip(
            "Open the MIDI Library quarantine folder in the operating system file manager. Quarantined source files are preserved there outside the active scanned roots."
        )
        self.quarantine_folder_button.clicked.connect(self._open_quarantine_folder)
        action_col.addWidget(self.quarantine_folder_button, 2, 0, 1, 2)
        self.backup_button = QPushButton("Backup…")
        self.backup_button.setToolTip("Create a verified backup of persistent Library catalogue, analysis, relationship, settings, and reference data.")
        self.backup_button.clicked.connect(self._backup)
        action_col.addWidget(self.backup_button, 3, 0)
        self.restore_button = QPushButton("Restore…")
        self.restore_button.setToolTip("Restore persistent Library data from a verified backup. Source MIDI files are not replaced or deleted.")
        self.restore_button.clicked.connect(self._restore)
        action_col.addWidget(self.restore_button, 3, 1)
        self.rebuild_button = QPushButton("Rebuild Library…")
        self.rebuild_button.setToolTip(
            "Creates a verified safety backup, clears the persistent catalogue/Deep Analysis/recommendation intelligence, then performs a full rebuild from the configured source MIDI folders. Source MIDI files are not deleted."
        )
        self.rebuild_button.clicked.connect(self._rebuild_library)
        action_col.addWidget(self.rebuild_button, 4, 0)
        self.open_remixer_button = QPushButton("Open MIDI Remixer")
        self.open_remixer_button.clicked.connect(self._open_remixer)
        self.open_remixer_button.setVisible(self._remixer_available())
        action_col.addWidget(self.open_remixer_button, 4, 1)
        action_col.setColumnStretch(0, 1)
        action_col.setColumnStretch(1, 1)
        ops_body.addLayout(action_col, 0, 1, Qt.AlignmentFlag.AlignTop)
        ops_body.setColumnStretch(0, 5)
        ops_body.setColumnStretch(1, 4)
        ops.addLayout(ops_body)

        stop_row = QHBoxLayout()
        stop_row.setSpacing(6)
        self.status_detail = QLabel("Completed work is saved continuously.")
        self.status_detail.setObjectName("LibrarySectionHint")
        self.status_detail.setWordWrap(True)
        self.status_detail.setMinimumWidth(0)
        self.status_detail.setMaximumHeight(42)
        self.status_detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status_detail.setToolTip("Completed work is saved continuously.")
        stop_row.addWidget(self.status_detail, 1)
        self.stop_engine_button = QPushButton("Stop Engine")
        self.stop_engine_button.setObjectName("DangerButton")
        self.stop_engine_button.setToolTip("Stop the background Library Engine and all Library-owned analyzer processes. The Manager window stays open so the Engine can be restarted.")
        self.stop_engine_button.clicked.connect(self._stop_engine)
        stop_row.addWidget(self.stop_engine_button)
        ops.addLayout(stop_row)

        self.manager_busy_indicator = QProgressBar()
        self.manager_busy_indicator.setRange(0, 0)
        self.manager_busy_indicator.setTextVisible(False)
        self.manager_busy_indicator.setFixedHeight(5)
        self.manager_busy_indicator.setVisible(False)
        ops.addWidget(self.manager_busy_indicator)
        outer.addWidget(ops_frame)

        # A small flexible footer absorbs larger-window surplus without
        # distorting any functional panel. The actual dashboard remains a
        # coherent full-width stack rather than splitting into isolated islands.
        outer.addStretch(1)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)

        # Catalogue inspection is independent of Engine availability.
        if self._qa_layout_only:
            offline = {"available": False, "quick_failed": 5156}
        else:
            offline = self._saved_catalogue_summary(refresh=True)
        self.browse_library_button.setEnabled(bool(offline.get("available")))
        self.statistics_button.setEnabled(bool(offline.get("available")))
        self.fast_failures_button.setEnabled(bool(offline.get("available")))
        self.fast_failures_button.setText(f"Unreadable ({int(offline.get('quick_failed') or 0):,})…")

    def _set_engine_controls_enabled(self, enabled: bool) -> None:
        """Enable only controls that require the detached Library Engine.

        Browse Library and Statistics deliberately remain independent: committed
        catalogue data is useful even while the Engine is stopped or recovering.
        """
        for widget in (
            self.throughput,
            self.auto_start_analysis,
            self.run_fast_button,
            self.pause_scan_button,
            self.cancel_scan_button,
            self.run_deep_button,
            self.pause_analysis_button,
            self.cancel_analysis_button,
            self.backup_button,
            self.restore_button,
            self.rebuild_button,
            self.add_folder_button,
            self.remove_folder_button,
            self.stop_engine_button,
            self.roots_list,
        ):
            widget.setEnabled(bool(enabled))
        # An empty first-run SQLite schema is not yet a browseable Library.
        # Use the asynchronous saved-summary cache rather than mere file
        # existence; returning users are enabled as soon as that summary lands.
        saved_available = bool(self._offline_summary_cache.get("available"))
        self.browse_library_button.setEnabled(saved_available)
        self.statistics_button.setEnabled(saved_available)
        self.fast_failures_button.setEnabled(saved_available and int(self._offline_summary_cache.get("quick_failed") or 0) > 0)
        self.suspicious_button.setEnabled(saved_available and int(self._offline_summary_cache.get("suspicious_loadable") or 0) > 0)
        # Restore is a recovery function and must remain reachable when the
        # Engine itself cannot start. The restore workflow proves the writer
        # process stopped before touching persistent files.
        self.restore_button.setEnabled(not self._long_action_inflight and not self._close_in_progress)

    def _saved_catalogue_summary(self, *, refresh: bool = False, include_failure_groups: bool = False) -> dict[str, Any]:
        """Return cached offline metadata and refresh SQLite strictly off Qt.

        The saved catalogue is intentionally usable when the Engine is offline,
        but a two-second SQLite read must never become a two-second GUI freeze.
        """
        now = time.monotonic()
        cache_has_groups = "failure_groups" in self._offline_summary_cache
        fresh = bool(
            self._offline_summary_cache
            and now - self._offline_summary_at < 5.0
            and (not include_failure_groups or cache_has_groups)
        )
        if refresh or not fresh:
            self._queue_offline_summary(include_failure_groups=include_failure_groups)
        if self._offline_summary_cache:
            return dict(self._offline_summary_cache)
        return {"available": False, "database_available": bool(self.catalogue_reader.exists())}

    def _queue_offline_summary(self, *, include_failure_groups: bool = False) -> None:
        self._offline_summary_groups_requested = bool(
            self._offline_summary_groups_requested or include_failure_groups
        )
        if self._offline_summary_inflight:
            return
        self._offline_summary_inflight = True
        include_groups = bool(self._offline_summary_groups_requested)
        self._offline_summary_groups_requested = False

        def run() -> None:
            try:
                summary = self.catalogue_reader.summary(include_failure_groups=include_groups)
            except Exception as exc:
                summary = {"available": bool(self.catalogue_reader.exists()), "_error": f"{type(exc).__name__}: {exc}"}
            summary["_included_failure_groups"] = include_groups
            self.offlineSummaryLoaded.emit(summary)

        threading.Thread(target=run, name="MidiLibraryOfflineSummary", daemon=True).start()

    def _apply_offline_summary(self, payload: object) -> None:
        summary = dict(payload) if isinstance(payload, dict) else {}
        included_groups = bool(summary.pop("_included_failure_groups", False))
        summary.pop("_error", None)
        self._offline_summary_inflight = False
        if summary:
            if included_groups or "failure_groups" not in self._offline_summary_cache:
                self._offline_summary_cache.update(summary)
            else:
                existing_groups = self._offline_summary_cache.get("failure_groups")
                self._offline_summary_cache.update(summary)
                if existing_groups is not None:
                    self._offline_summary_cache["failure_groups"] = existing_groups
            self._offline_summary_at = time.monotonic()
        if self._offline_summary_groups_requested:
            self._queue_offline_summary(include_failure_groups=True)
        available = bool(self._offline_summary_cache.get("available"))
        self.browse_library_button.setEnabled(available)
        self.statistics_button.setEnabled(available)
        self.fast_failures_button.setEnabled(available and int(self._offline_summary_cache.get("quick_failed") or 0) > 0)
        self.suspicious_button.setEnabled(available and int(self._offline_summary_cache.get("suspicious_loadable") or 0) > 0)
        self.fast_failures_button.setText(
            f"Unreadable ({int(self._offline_summary_cache.get('quick_failed') or 0):,})…"
        )
        if hasattr(self, "_engine_ready") and not self._engine_ready and not self._engine_connecting:
            self._render_offline_catalogue(self._offline_summary_cache)

    def _show_engine_unavailable(self, detail: str) -> None:
        message = str(detail or "The Library Engine did not start.").strip()
        self._set_engine_controls_enabled(False)
        summary = self._saved_catalogue_summary(refresh=False)
        saved = int(summary.get("catalogued") or 0)
        banner_text = (
            "Library Engine is unavailable. Saved Library data has NOT been deleted"
            + (f" and {saved:,} catalogued MIDI(s) can still be browsed." if saved else ".")
            + "\n" + message
        )
        self.engine_error_banner.setText(_soft_wrap_runtime_text(banner_text))
        self.engine_error_banner.setToolTip(banner_text)
        self.status_detail.setText(_soft_wrap_runtime_text(message))
        self.status_detail.setToolTip(message)
        self.engine_error_banner.setVisible(True)
        self._render_offline_catalogue(summary)

    def _render_offline_catalogue(self, summary: dict[str, Any] | None = None, *, state_label: str = "ENGINE OFFLINE") -> None:
        summary = dict(summary or self._saved_catalogue_summary())
        available = bool(summary.get("available"))
        database_available = bool(summary.get("database_available"))
        total = int(summary.get("total") or 0)
        catalogued = int(summary.get("catalogued") or 0)
        quick_failed = int(summary.get("quick_failed") or 0)
        suspicious_loadable = int(summary.get("suspicious_loadable") or 0)
        pending = int(summary.get("pending") or 0)
        paused = int(summary.get("paused") or 0)
        analyzing = int(summary.get("analyzing") or 0)
        complete = int(summary.get("complete") or 0)
        failed = int(summary.get("failed") or 0)
        deep_total = pending + paused + analyzing + complete + failed
        discovered = int(summary.get("discovered") or total)
        usable = int(summary.get("usable") or 0)
        mash_ready = int(summary.get("mash_ready") or 0)
        fusion_candidate = int(summary.get("fusion_candidate") or 0)
        usable_not_mash = int(summary.get("usable_not_mash") or 0)
        needs_review = int(summary.get("needs_review") or 0)
        rejected = int(summary.get("rejected") or 0)
        quarantined = int(summary.get("quarantined") or 0)
        unresolved = int(summary.get("unresolved") or 0)

        if available:
            self.storage_status.setText(f"Saved catalogue: {catalogued:,} MIDI(s) · Engine offline")
            self.storage_status.setToolTip(str(self.catalogue_reader.database_path))
            self.curation_status.setText(
                f"Curated Library · {discovered:,} discovered · {usable:,} usable · {mash_ready:,} Mash Ready · "
                f"{fusion_candidate:,} Fusion · {usable_not_mash:,} Not Mash · {needs_review:,} review · "
                f"{rejected:,} rejected · {quarantined:,} quarantined · {unresolved:,} unresolved"
            )
            self.scan_progress.setRange(0, max(1, total))
            self.scan_progress.setValue(total if total else 0)
            self.scan_progress.setFormat(
                f"Saved · {catalogued:,}"
            )
            self.scan_label.setText(
                f"Fast catalogue — {state_label} · {total:,} found · {catalogued:,} usable · {quick_failed:,} need review"
            )
            self.deep_progress.setRange(0, max(1, deep_total))
            interrupted = pending + paused + analyzing
            retryable_failed = int(summary.get("failed_retryable") or 0)
            persistent_failed = int(summary.get("failed_persistent") or 0)
            if interrupted <= 0 and failed > 0:
                self.deep_progress.setValue(max(1, deep_total))
                self.deep_progress.setFormat(
                    f"Done · {complete:,}"
                )
            else:
                self.deep_progress.setValue(min(complete, max(1, deep_total)))
                self.deep_progress.setFormat("Saved · %v/%m")
            deep_parts = [
                f"Deep Analysis — {state_label}",
                f"{complete:,} complete",
                f"{interrupted:,} waiting/interrupted",
                f"{retryable_failed:,} Deep retry available",
                f"{persistent_failed:,} Deep Analysis review item(s)",
            ]
            self.deep_label.setText(" · ".join(deep_parts))
            self.relationship_progress.setText("Engine offline · saved recommendation data remains on disk")
            self.relationship_label.setText(f"Mash recommendation readiness — {state_label}")
        else:
            if database_available:
                self.storage_status.setText("Saved catalogue: none yet")
                self.storage_status.setToolTip(str(self.catalogue_reader.database_path))
                self.curation_status.setText("Curated Library · no persisted MIDI lifecycle rows yet")
                self.scan_progress.setRange(0, 1)
                self.scan_progress.setValue(0)
                self.scan_progress.setFormat("Waiting")
                self.deep_progress.setRange(0, 1)
                self.deep_progress.setValue(0)
                self.deep_progress.setFormat("Waiting")
                self.relationship_progress.setText("Waiting for catalogued MIDI")
                self.scan_label.setText(f"Fast catalogue — {state_label} · no source MIDI catalogued yet")
                self.deep_label.setText(f"Deep Analysis — {state_label} · not started")
                self.relationship_label.setText(f"Mash recommendation readiness — {state_label} · not started")
            else:
                self.storage_status.setText("Saved catalogue: not available")
                self.storage_status.setToolTip("")
                self.curation_status.setText("Curated Library · saved lifecycle unavailable")
                for progress in (self.scan_progress, self.deep_progress):
                    progress.setRange(0, 1)
                    progress.setValue(0)
                    progress.setFormat("Offline")
                self.relationship_progress.setText("Engine unavailable")
                self.scan_label.setText(f"Fast catalogue — {state_label} · no readable saved catalogue")
                self.deep_label.setText(f"Deep Analysis — {state_label} · no readable saved catalogue")
                self.relationship_label.setText(f"Mash recommendation readiness — {state_label}")
        self.scan_location.setVisible(False)
        self._statistics_values.update({
            "catalogued": catalogued,
            "deep_complete": complete,
            "deep_remaining": pending + paused + analyzing,
            "fast_failed": quick_failed,
            "failed": quick_failed + failed,
            "discovered": discovered, "usable": usable, "mash_ready": mash_ready,
            "fusion_candidate": fusion_candidate, "usable_not_mash": usable_not_mash,
            "needs_review": needs_review, "rejected": rejected, "quarantined": quarantined,
            "unresolved": unresolved,
        })
        self.browse_library_button.setEnabled(available)
        self.statistics_button.setEnabled(available)
        self.fast_failures_button.setEnabled(available and quick_failed > 0)
        self.fast_failures_button.setText(f"Unreadable ({quick_failed:,})…")
        self.suspicious_button.setEnabled(available and suspicious_loadable > 0)
        self.suspicious_button.setText(f"Unusual ({suspicious_loadable:,})…")

    def _dispatch_command(
        self, action: str, command: str, *, timeout: float = 10.0, message: str = "",
        context: dict[str, Any] | None = None, **payload: Any
    ) -> bool:
        """Dispatch potentially blocking Engine IPC away from the Qt thread."""
        if not self._engine_ready:
            QMessageBox.information(
                self, "MIDI Library Engine",
                "The Library Engine is still starting. The Manager remains responsive; try this action again when the Engine status says Running.",
            )
            return False
        key = str(action)
        if key in self._command_inflight:
            return False
        self._command_inflight.add(key)
        self._command_contexts[key] = dict(context or {})
        self._begin_manager_busy(f"command:{key}")
        if message:
            self.status_detail.setText(message)

        def run() -> None:
            result: dict[str, Any] | None = None
            error = ""
            try:
                result = self.client.command(command, timeout=timeout, **payload)
            except (LibraryEngineUnavailable, RuntimeError, OSError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            self.commandFinished.emit(key, result or {}, error)

        threading.Thread(target=run, name=f"MidiLibraryManagerCommand-{key}", daemon=True).start()
        return True

    def _finish_command(self, action: str, result_obj: object, error: str) -> None:
        result = dict(result_obj) if isinstance(result_obj, dict) else {}
        context = self._command_contexts.pop(str(action), {})
        self._command_inflight.discard(str(action))
        self._end_manager_busy(f"command:{action}")
        if error:
            QMessageBox.warning(self, "Library Engine", error)
            self._poll_status()
            return
        if action in {"configure_roots_add", "configure_roots_remove"}:
            self._refresh_roots(result.get("roots"))
            followup = str(context.get("followup") or "")
            if followup:
                self._dispatch_command(
                    f"root_followup_{followup}", followup,
                    message="Starting the incremental Library update…", timeout=10.0,
                )
            elif action == "configure_roots_remove" and bool(context.get("last_source_removed")):
                self.status_detail.setText(
                    "No active MIDI source folders remain. The saved Library catalogue and completed analysis are preserved as an "
                    "offline snapshot. Add a source folder when you want filesystem reconciliation to resume."
                )
        elif action == "set_throughput" and result.get("restart_required"):
            self.status_detail.setText(
                f"{self.throughput.currentText()} is saved. Restart the Library Engine to change from the current "
                f"worker count to {result.get('workers')} analysis workers."
            )
        elif action == "retry_failed_bulk":
            requeued = int(result.get("retried") or 0)
            QMessageBox.information(
                self, "Deep Analysis",
                f"Requeued {requeued:,} first-attempt item(s). Files that do not complete again will remain in Deep Analysis Review."
            )
        self._poll_status()

    def _begin_manager_busy(self, reason: str) -> None:
        key = str(reason)
        self._manager_busy_reasons[key] = int(self._manager_busy_reasons.get(key, 0)) + 1
        if not self.manager_busy_indicator.isVisible():
            self._manager_busy_delay.start()

    def _end_manager_busy(self, reason: str) -> None:
        key = str(reason)
        count = int(self._manager_busy_reasons.get(key, 0)) - 1
        if count > 0:
            self._manager_busy_reasons[key] = count
        else:
            self._manager_busy_reasons.pop(key, None)
        if not self._manager_busy_reasons:
            self._manager_busy_delay.stop()
            self.manager_busy_indicator.setVisible(False)

    def _show_manager_busy(self) -> None:
        if self._manager_busy_reasons and not self._close_in_progress:
            self.manager_busy_indicator.setVisible(True)

    def _set_long_action_busy(self, busy: bool, message: str = "") -> None:
        for button in (self.suspicious_button, self.backup_button, self.restore_button, self.rebuild_button):
            button.setEnabled(not busy)
        if busy:
            self._begin_manager_busy("maintenance")
            if message:
                self.status_detail.setText(message)
        else:
            self._end_manager_busy("maintenance")

    def _start_long_action(
        self, kind: str, command: str, *, timeout: float, context: dict[str, Any] | None = None, **payload: Any
    ) -> bool:
        """Run maintenance IPC away from the Qt thread.

        Backup/rebuild/suspicious review may legitimately spend seconds in the
        Engine.  The Manager is a monitoring dashboard and must keep repainting
        while those operations run.
        """
        if not self._engine_ready:
            QMessageBox.information(
                self, "MIDI Library Engine",
                "The Library Engine is still starting. Try this action again when the Engine status says Running.",
            )
            return False
        if self._long_action_inflight:
            QMessageBox.information(
                self, "Library work in progress",
                "Another Library Manager maintenance action is still running. The Library Engine's normal background analysis continues independently.",
            )
            return False
        self._long_action_inflight = kind
        self._long_action_context = dict(context or {})
        labels = {
            "backup": "Creating and verifying a Library Intelligence backup…",
            "restore": "Verifying the selected backup, protecting the current Library, and preparing restore…",
            "rebuild": "Creating the safety backup and starting a derived-library rebuild…",
            "remove_suspicious": "Updating the active library and persistent indexes…",
            "restore_quarantine": "Restoring quarantined MIDI and verifying current Library state…",
            "retry_selected_deep": "Queueing selected Deep Analysis retry…",
            "rescan_selected_fast": "Starting forced Fast re-scan for selected MIDI files…",
            "rescan_all_fast_failures": "Preparing a retry of unreadable MIDI files…",
            "reanalyze_selected": "Queueing selected Deep Analysis rebuild…",
            "keep_reviewed_not_mash": "Resolving reviewed MIDI as Library-usable · Not Mash…",
        }
        busy_message = labels.get(kind, "Library maintenance is running…")
        self._set_long_action_busy(True, busy_message)
        browser = self._library_browser
        if browser is not None:
            try:
                if browser.isVisible():
                    browser.set_maintenance_busy(True, busy_message)
            except RuntimeError:
                self._library_browser = None

        def run() -> None:
            result: dict[str, Any] | None = None
            error = ""
            try:
                # QA-only deterministic deferred-Close boundary. `_long_action_inflight`
                # is set synchronously above, so holding this worker briefly proves
                # that closeEvent remembers an Exit request while a real Manager-owned
                # maintenance action is outstanding instead of racing a fast backup.
                try:
                    qa_hold = max(0.0, min(3.0, float(
                        os.environ.get("MIDI_LIBRARY_QA_HOLD_MAINTENANCE_SECONDS", "0") or 0
                    )))
                except ValueError:
                    qa_hold = 0.0
                if qa_hold > 0.0:
                    qa_deadline = time.monotonic() + qa_hold
                    while time.monotonic() < qa_deadline and not self._exit_after_maintenance:
                        time.sleep(min(0.02, max(0.0, qa_deadline - time.monotonic())))
                result = self.client.command(command, timeout=timeout, **payload)
            except (LibraryEngineUnavailable, RuntimeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            self.longActionFinished.emit(kind, result or {}, error)

        threading.Thread(target=run, name=f"MidiLibraryManager-{kind}", daemon=True).start()
        return True

    def _finish_long_action(self, kind: str, result: object, error: str) -> None:
        payload = dict(result) if isinstance(result, dict) else {}
        context = dict(self._long_action_context)
        self._long_action_inflight = ""
        self._long_action_context = {}
        close_after_maintenance = bool(self._exit_after_maintenance)
        qa_maintenance_close = bool(str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_DURING_MAINTENANCE", "")).strip())
        self._exit_after_maintenance = False
        self._set_long_action_busy(False)
        browser = self._library_browser
        if browser is not None:
            try:
                if browser.isVisible():
                    browser.set_maintenance_busy(False)
            except RuntimeError:
                self._library_browser = None
        if error:
            self._write_qa_maintenance_event("boundary-error", error)
            title = {
                "backup": "Library Backup",
                "restore": "Library Restore",
                "rebuild": "Rebuild Not Started",
                "remove_suspicious": "MIDI Library Cleanup",
                "restore_quarantine": "Restore Quarantined MIDI",
                "retry_selected_deep": "Deep Analysis",
                "rescan_selected_fast": "Fast Re-scan",
                "rescan_all_fast_failures": "Unreadable File Retry",
                "reanalyze_selected": "Deep Analysis Rebuild",
                "keep_reviewed_not_mash": "Keep Reviewed MIDI",
            }.get(kind, "Library Engine")
            if kind == "rebuild":
                error = "Nothing was intentionally cleared unless a verified backup was created first.\n\n" + error
            # In normal use the operator must see the maintenance error. During
            # unattended target-Windows deferred-Close QA, a modal error dialog
            # would itself block the qualification process and hide the actual
            # Engine error behind a generic Manager timeout.  Persist the exact
            # error above and resume Close; the harness then fails with that
            # evidence.
            if not (close_after_maintenance and qa_maintenance_close):
                QMessageBox.warning(self, title, error)
            self._poll_status()
            if close_after_maintenance:
                self._write_qa_maintenance_event("close-resumed", "error")
                QTimer.singleShot(0, self.close)
            return
        if close_after_maintenance and qa_maintenance_close:
            self._write_qa_maintenance_event("boundary-ok", str(payload.get("path") or ""))
            self.status_detail.setText("Maintenance reached its persistent boundary. Continuing the requested full exit…")
            self._poll_status()
            self._write_qa_maintenance_event("close-resumed", "success")
            QTimer.singleShot(0, self.close)
            return
        if kind == "backup":
            QMessageBox.information(self, "Library Backup", f"Backup verified:\n{payload.get('path')}")
        elif kind == "restore":
            QMessageBox.information(
                self, "Library Restore Complete",
                f"Restored and verified Library data from:\n{payload.get('source')}\n\n"
                f"Safety backup of the pre-restore Library:\n{payload.get('safety_backup')}\n\n"
                f"Restored {int(payload.get('restored_count') or 0):,} persistent item(s). The Library Engine has restarted."
            )
        elif kind == "rebuild":
            QMessageBox.information(
                self, "Library Rebuild Started",
                f"Safety backup: {payload.get('backup')}\n\nThe Library Engine is rebuilding from the configured source folders in the background.",
            )
        elif kind == "remove_suspicious":
            errors = payload.get("errors") or []
            message = f"Removed {int(payload.get('removed') or 0):,} file(s) from the active library."
            if context.get("permanent"):
                message += " Permanently selected source files were deleted only after confirmation."
            if errors:
                message += "\n\nSome files could not be changed:\n" + "\n".join(map(str, errors[:10]))
            QMessageBox.information(self, "MIDI Library Cleanup", message)
        elif kind == "restore_quarantine":
            errors = payload.get("errors") or []
            restored = int(payload.get("restored") or 0)
            message = f"Restored {restored:,} quarantined MIDI file(s) to their original location and verified current Library state. Unchanged Rejected files remain Rejected; genuinely changed files can reopen Review Required."
            if errors:
                message += "\n\nSome files could not be restored:\n" + "\n".join(map(str, errors[:10]))
            QMessageBox.information(self, "Restore Quarantined MIDI", message)
        elif kind == "retry_selected_deep":
            requeued = int(payload.get("retried") or 0)
            QMessageBox.information(
                self, "Deep Analysis",
                f"Requeued {requeued:,} selected failure(s) for a deliberate retry. Completed analyses remain saved.",
            )
        elif kind == "rescan_selected_fast":
            requested = int(payload.get("eligible") or payload.get("requested") or 0)
            if bool(payload.get("started")):
                QMessageBox.information(
                    self, "Selected MIDI Retry Started",
                    f"Retry started for {requested:,} selected MIDI file(s).\n\n"
                    "Use this after a file was repaired/replaced or after a scanner update. The Browser and Manager remain usable while the Engine processes them.",
                )
            else:
                QMessageBox.information(
                    self, "Selected MIDI Retry",
                    "The selected-file retry did not start. A Fast Scan may already be active, or the selected files are unavailable.",
                )
        elif kind == "rescan_all_fast_failures":
            requested = int(payload.get("eligible") or payload.get("requested") or 0)
            if bool(payload.get("started")):
                # Do not put another modal dialog over the review Browser after
                # the user already confirmed the operation. Progress is mirrored
                # in that Browser and on the Manager dashboard.
                self.status_detail.setText(
                    f"Retrying {requested:,} unreadable MIDI file(s). This does not rescan the rest of the Library…"
                )
            elif requested <= 0:
                self._fast_recovery_start_failure_count = None
                QMessageBox.information(self, "Unreadable File Retry", "There are no unreadable MIDI files to retry.")
            else:
                self._fast_recovery_start_failure_count = None
                QMessageBox.information(
                    self, "Unreadable File Retry",
                    "The unreadable-file retry could not start because another Fast Scan is active.",
                )
        elif kind == "reanalyze_selected":
            requeued = int(payload.get("requeued") or 0)
            if requeued > 0:
                QMessageBox.information(
                    self, "Deep Analysis Rebuild Queued",
                    f"Requeued Deep Analysis for {requeued:,} selected MIDI file(s). Source files and Fast catalogue rows were not changed.",
                )
            else:
                QMessageBox.information(
                    self, "Deep Analysis Rebuild",
                    "No selected MIDI could be requeued. It may already be active or no longer be catalogued.",
                )
        elif kind == "keep_reviewed_not_mash":
            kept = int(payload.get("kept") or 0)
            errors = list(payload.get("errors") or [])
            message = (
                f"Kept {kept:,} reviewed MIDI file(s) in the Library as Not Mash Candidate. "
                "Their source files were not changed and they are excluded from normal Guided/Assisted recommendations."
            )
            if errors:
                message += "\n\nSome selections were not changed:\n" + "\n".join(map(str, errors[:10]))
            QMessageBox.information(self, "Keep Reviewed MIDI", message)
        # A forced Fast re-scan has only just started when its command returns.
        # Refreshing the Browser immediately competes with the first hot write
        # batches and creates needless table churn. The regular status transition
        # refreshes the Browser once the scan reaches a safe completed boundary.
        if self._library_browser is not None and kind in {
            "remove_suspicious", "restore_quarantine", "retry_selected_deep", "reanalyze_selected", "keep_reviewed_not_mash"
        }:
            try:
                self._library_browser.refresh_view()
            except RuntimeError:
                self._library_browser = None
        self._poll_status()
        if close_after_maintenance:
            QTimer.singleShot(0, self.close)

    def _refresh_roots(self, roots: object | None = None) -> None:
        if not self._engine_ready:
            return
        values = roots if roots is not None else self._latest_engine_status.get("roots", [])
        if not isinstance(values, (list, tuple)):
            values = []
        self.roots_list.clear()
        self.roots_list.addItems([str(path) for path in values])

    def _apply_engine_preferences_from_status(self, supplied_status: dict[str, Any] | None = None) -> None:
        status = dict(supplied_status or self._latest_engine_status or {})
        if not status:
            return
        mode = str(status.get("throughput_mode") or "maximum")
        index = self.throughput.findData(mode)
        self.throughput.blockSignals(True)
        self.throughput.setCurrentIndex(max(0, index))
        self.throughput.blockSignals(False)
        self.auto_start_analysis.blockSignals(True)
        self.auto_start_analysis.setChecked(bool(status.get("manager_auto_start_analysis", True)))
        self.auto_start_analysis.blockSignals(False)

    def _remixer_available(self) -> bool:
        return any((self.root / name).exists() for name in ("MIDI Remixer.pyw", "Launch MIDI Remixer.bat"))

    def _restart_engine(self) -> None:
        if self._engine_connecting:
            return
        self._engine_stopped_by_user = False
        self._engine_ready = False
        self._engine_connect_error = ""
        self._engine_connecting = True
        self._engine_connect_started = time.monotonic()
        self._engine_connect_cancel.clear()
        self._set_engine_controls_enabled(False)
        self.engine_error_banner.setVisible(False)
        self.engine_action_button.setVisible(False)
        self.engine_state.setText("Starting Library Engine…")
        self.status_detail.setText("Starting the Library Engine in the background. This window remains responsive.")
        self._begin_manager_busy("engine_start")
        self._engine_connect_thread = threading.Thread(
            target=self._connect_engine_background, name="MidiLibraryManagerEngineReconnect", daemon=True
        )
        self._engine_connect_thread.start()

    def _cancel_fast_scan(self) -> None:
        self._scan_cancel_requested = True
        self._dispatch_command(
            "cancel_scan", "cancel_scan",
            message="Canceling Fast Scan… completed batches remain saved. Waiting for a safe stop boundary.",
        )

    def _toggle_fast_scan(self) -> None:
        if self.pause_scan_button.text().startswith("Resume"):
            self._dispatch_command("resume_scan", "resume_scan", message="Resuming Fast Scan…")
        else:
            self._dispatch_command("pause_scan", "pause_scan", message="Pausing Fast Scan…")

    def _toggle_deep_analysis(self) -> None:
        if self.pause_analysis_button.text().startswith("Resume"):
            self._dispatch_command("resume_analysis", "resume_analysis", message="Resuming Deep Analysis…")
        else:
            self._dispatch_command("pause_analysis", "pause_analysis", message="Pausing Deep Analysis…")

    def _cancel_deep_analysis(self) -> None:
        self._dispatch_command(
            "cancel_analysis", "cancel_analysis",
            message="Canceling Deep Analysis… completed files remain saved. Waiting for a safe stop boundary.",
        )

    def _add_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Add MIDI Library Folder", str(Path.home()))
        if not selected:
            return
        roots = [self.roots_list.item(i).text() for i in range(self.roots_list.count())]
        saved_catalogued = int(self._statistics_values.get("catalogued") or 0)
        if not roots and saved_catalogued > 0:
            if QMessageBox.warning(
                self,
                "Reconnect Saved Library Source",
                f"This Library currently has {saved_catalogued:,} saved catalogued MIDI(s) but no active source root.\n\n"
                f"Add this source?\n{selected}\n\n"
                "The next Fast Scan will reconcile the saved Library against the newly configured source. "
                "Rows that are not under the configured source can leave the active Library; source MIDI files are never deleted. "
                "If you are reconnecting an existing collection, select the same highest useful source root used previously.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            ) != QMessageBox.StandardButton.Yes:
                return
        roots.append(selected)
        # Adding a source is a Fast-catalogue operation. The Engine owns whether
        # unfinished Deep Analysis auto-resumes after that Fast boundary; the
        # Manager must not duplicate/override the Engine startup/session policy.
        followup = "scan"
        self._dispatch_command(
            "configure_roots_add", "configure_roots", roots=roots,
            context={"followup": followup}, message="Updating Library source folders…",
        )

    def _remove_folder(self) -> None:
        row = self.roots_list.currentRow()
        if row < 0:
            QMessageBox.information(self, "Remove Source Folder", "Select a MIDI source folder first.")
            return
        selected = self.roots_list.item(row).text()
        roots = [self.roots_list.item(i).text() for i in range(self.roots_list.count()) if i != row]
        if roots:
            consequence = (
                "The MIDI files on disk will NOT be deleted. After the source is removed, Fast Scan will reconcile the saved "
                "Library against the remaining configured sources when all of them are accessible, and remove Library rows that no longer belong to any source."
            )
            followup = "scan"
            message = "Removing the source folder from Library configuration and reconciling the catalogue…"
        else:
            consequence = (
                "The MIDI files on disk will NOT be deleted. Because this is the last active source, the saved Library catalogue "
                "and completed analysis will be preserved as an offline snapshot. Add a source folder later to resume filesystem "
                "reconciliation; do not rebuild merely because no source is currently configured."
            )
            followup = ""
            message = "Removing the last active source while preserving the saved Library snapshot…"
        if QMessageBox.warning(
            self,
            "Remove Source Folder",
            f"Remove this folder from MIDI Library sources?\n\n{selected}\n\n{consequence}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._dispatch_command(
            "configure_roots_remove", "configure_roots", roots=roots,
            context={"followup": followup, "last_source_removed": not bool(roots)}, message=message,
        )

    def _change_throughput(self) -> None:
        mode = str(self.throughput.currentData() or "maximum")
        self._dispatch_command(
            "set_throughput", "set_throughput", mode=mode,
            message="Applying Library performance mode…",
        )

    def _change_auto_start(self, enabled: bool) -> None:
        self._dispatch_command(
            "set_auto_start", "set_auto_start", enabled=bool(enabled),
            message="Saving Library startup preference…",
        )

    def _start_or_resume_deep_analysis(self) -> None:
        deep = dict(self._last_deep_status or {})
        remaining = int(deep.get("remaining") or 0)
        failed = int(deep.get("failed") or 0)
        retryable_failed = int(deep.get("failed_retryable") or 0)
        persistent_failed = int(deep.get("failed_persistent") or 0)
        state = str(deep.get("state") or "idle").casefold()
        if state in {"running", "starting"}:
            return
        sources_ready = bool(self._configured_source_roots) and (
            len(self._configured_source_roots) == len(self._accessible_source_roots)
        )
        if (remaining > 0 or retryable_failed > 0) and not sources_ready:
            QMessageBox.information(
                self, "Deep Analysis",
                "Deep Analysis needs the configured MIDI source files. Reconnect every configured source folder first. "
                "The saved Library and completed analysis remain available for review while a source is offline.",
            )
            return
        if remaining > 0:
            self._dispatch_command("resume_analysis", "resume_analysis", message="Resuming Deep Analysis…")
            return
        if retryable_failed > 0:
            self._retry_failed()
            return
        if persistent_failed > 0 or failed > 0:
            self._open_library_browser("failed_persistent" if persistent_failed > 0 else "failed")
            return
        QMessageBox.information(
            self, "Deep Analysis", "There is no unfinished Deep Analysis work in the current Library."
        )

    def _failure_recovery_text(self, failed: int) -> str:
        summary = self._saved_catalogue_summary(refresh=True, include_failure_groups=True)
        groups = list(summary.get("failure_groups") or ())
        lines = [
            f"{failed:,} MIDI file(s) failed their first Deep Analysis attempt.",
            "",
            "This retry will requeue each of these files ONE time. Completed analysis remains saved and will not be redone.",
            "If a file does not complete again it becomes a Deep Analysis review item rather than being offered for bulk retry again.",
        ]
        if failed >= 1000:
            lines.extend(["", "This is a large retry pass and may take a long time."])
        if groups:
            lines.extend(["", "Most common saved failure reason(s):"])
            for error, count in groups[:4]:
                compact = " ".join(str(error).split())
                if len(compact) > 180:
                    compact = compact[:177] + "…"
                lines.append(f"• {int(count):,} · {compact}")
        lines.extend(["", "Run the one-time retry now?"])
        return "\n".join(lines)

    def _retry_failed(self) -> None:
        deep = dict(self._last_deep_status or {})
        failed = int(deep.get("failed_retryable") or 0)
        if failed <= 0:
            persistent = int(deep.get("failed_persistent") or 0)
            if persistent > 0:
                self._open_library_browser("failed_persistent")
            return
        if QMessageBox.warning(
            self,
            "Retry First Deep Analysis Failures",
            self._failure_recovery_text(failed),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        # Prevent double-click/double-queue while the status poll catches up.
        self.run_deep_button.setEnabled(False)
        self.run_deep_button.setText("Retry queued…")
        self._dispatch_command(
            "retry_failed_bulk", "retry_failed", timeout=12.0,
            message="Queueing one-time Deep Analysis retry…",
        )

    def _open_library_browser(self, status: str = "all") -> None:
        if not isinstance(status, str):
            status = "all"
        if not self.catalogue_reader.exists():
            QMessageBox.information(self, "Browse MIDI Library", "No saved MIDI catalogue is available yet.")
            return
        if self._library_browser is not None:
            try:
                if self._library_browser.isVisible():
                    if status != "all":
                        self._library_browser.show_status(status)
                    self._library_browser.raise_()
                    self._library_browser.activateWindow()
                    return
            except RuntimeError:
                self._library_browser = None
        dialog = LibraryBrowserDialog(self.catalogue_reader.database_path, self.root, self)
        dialog.set_source_state(self._configured_source_roots, self._accessible_source_roots)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.removeRequested.connect(self._remove_suspicious)
        dialog.restoreRequested.connect(self._restore_quarantined)
        dialog.retryRequested.connect(self._retry_selected_deep)
        dialog.rescanRequested.connect(self._rescan_selected_fast)
        dialog.rescanAllFastFailedRequested.connect(self._rescan_all_fast_failures)
        dialog.reanalyzeRequested.connect(self._reanalyze_selected)
        dialog.keepNotMashRequested.connect(self._keep_reviewed_not_mash)
        dialog.pauseFastScanRequested.connect(self._toggle_fast_scan)
        dialog.cancelFastScanRequested.connect(self._cancel_fast_scan)
        dialog.destroyed.connect(self._on_library_browser_destroyed)
        self._library_browser = dialog
        if status != "all":
            dialog.show_status(status)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_library_browser_destroyed(self, _obj=None) -> None:
        self._library_browser = None
        self._end_manager_busy("browser_close")
        if self._close_after_browser:
            self._close_after_browser = False
            QTimer.singleShot(0, self.close)

    def _review_suspicious(self) -> None:
        # The old modal materialized at most 5,000 candidates and could therefore
        # hide the tail of a large health-review population. The paged catalogue
        # browser has no such UI cap and cleanly separates Fast failures from
        # successfully loaded-but-unusual MIDIs.
        self._open_library_browser("suspicious_loadable")

    def _remove_suspicious(self, paths: list[str], permanent: bool) -> None:
        self._start_long_action(
            "remove_suspicious", "remove_paths", timeout=30.0,
            context={"permanent": bool(permanent)}, paths=paths, permanent=permanent,
        )

    def _restore_quarantined(self, paths: list[str]) -> None:
        selected = [str(path) for path in paths if str(path)]
        if not selected:
            return
        self._start_long_action(
            "restore_quarantine", "restore_quarantine", timeout=30.0,
            context={"count": len(selected)}, paths=selected,
        )

    def _retry_selected_deep(self, paths: list[str]) -> None:
        selected = [str(path) for path in paths if str(path)]
        if not selected:
            return
        self._start_long_action(
            "retry_selected_deep", "retry_failed", timeout=12.0,
            context={"count": len(selected)}, paths=selected,
        )

    def _rescan_selected_fast(self, paths: list[str]) -> None:
        selected = [str(path) for path in paths if str(path)]
        if not selected:
            return
        self._start_long_action(
            "rescan_selected_fast", "rescan_paths", timeout=20.0,
            context={"count": len(selected)}, paths=selected,
        )

    def _rescan_all_fast_failures(self) -> None:
        # Capture the pre-retry population before the asynchronous Engine command
        # returns. A one-second status poll may otherwise observe already-recovered
        # rows and under-report what this retry actually fixed.
        self._fast_recovery_start_failure_count = int(self._statistics_values.get("fast_failed") or 0)
        # Collecting thousands of persisted failures can legitimately take time
        # on a busy 96K catalogue. Never run this IPC request on the Qt thread.
        self._start_long_action(
            "rescan_all_fast_failures", "rescan_failed_fast", timeout=45.0,
        )

    def _reanalyze_selected(self, paths: list[str]) -> None:
        selected = [str(path) for path in paths if str(path)]
        if not selected:
            return
        self._start_long_action(
            "reanalyze_selected", "reanalyze_paths", timeout=20.0,
            context={"count": len(selected)}, paths=selected,
        )

    def _keep_reviewed_not_mash(self, paths: list[str]) -> None:
        selected = [str(path) for path in paths if str(path)]
        if not selected:
            return
        self._start_long_action(
            "keep_reviewed_not_mash", "keep_reviewed_not_mash", timeout=12.0,
            context={"count": len(selected)}, paths=selected,
        )

    def _show_statistics(self) -> None:
        if not self._engine_ready:
            self._render_offline_catalogue()
        labels = (
            ("Discovered MIDI", "discovered"),
            ("Usable / Blessed", "usable"),
            ("Mash Ready", "mash_ready"),
            ("Fusion Candidates", "fusion_candidate"),
            ("Usable — Not Mash Candidate", "usable_not_mash"),
            ("Needs Review", "needs_review"),
            ("Rejected", "rejected"),
            ("Quarantined", "quarantined"),
            ("Unresolved", "unresolved"),
            ("Catalogued MIDIs", "catalogued"),
            ("Distinct song/version families", "distinct"),
            ("No basic health flag", "health_clear"),
            ("Unreadable by Fast Scan", "fast_failed"),
            ("Unusual but loadable", "suspicious"),
            ("Deep analyzed", "deep_complete"),
            ("Waiting/interrupted deep analysis", "deep_remaining"),
            ("Items needing scan/analysis review", "failed"),
            ("Guided-ready", "guided"),
            ("Prepared recommendation relationships", "relationships"),
        )
        lines = [f"{label}: {int(self._statistics_values.get(key, 0)):,}" for label, key in labels]
        if not self._engine_ready:
            lines.extend(["", "Engine is offline; Engine-derived family/readiness/relationship totals may reflect the last live status."])
        QMessageBox.information(self, "Library Statistics", "\n".join(lines))

    def _open_quarantine_folder(self) -> None:
        """Open the explicit Library quarantine without blocking the Qt thread."""
        quarantine = self.paths.data_root / "quarantine"
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Open Quarantine Folder", f"Could not create/open the quarantine folder:\n{quarantine}\n\n{exc}")
            return
        self.quarantine_folder_button.setEnabled(False)
        self.status_detail.setText("Opening the MIDI Library quarantine folder…")
        self._begin_manager_busy("shell:quarantine")

        def run() -> None:
            error = ""
            try:
                open_folder(quarantine)
            except (OSError, RuntimeError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            self.shellActionFinished.emit("quarantine", error)

        threading.Thread(target=run, name="MidiLibraryManagerQuarantineFolder", daemon=True).start()

    def _finish_shell_action(self, action: str, error: str) -> None:
        if action != "quarantine":
            return
        self._end_manager_busy("shell:quarantine")
        self.quarantine_folder_button.setEnabled(True)
        if error:
            QMessageBox.warning(
                self, "Open Quarantine Folder",
                "The operating system could not open the MIDI Library quarantine folder.\n\n" + error.split(": ", 1)[-1],
            )
            return
        self.status_detail.setText("Quarantine folder opened. Quarantined MIDIs remain outside the active Library sources.")

    def _backup(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default = self.paths.backups / f"MIDI-Library-Data-Backup-{stamp}.zip"
        selected, _ = QFileDialog.getSaveFileName(self, "Backup Library Data", str(default), "ZIP archives (*.zip)")
        if not selected:
            return
        self._start_long_action("backup", "backup", timeout=180.0, target=selected)

    def _restore(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Restore Library Data", str(self.paths.backups), "ZIP archives (*.zip)"
        )
        if not selected:
            return
        if QMessageBox.warning(
            self, "Restore Library Data",
            "This will replace the current persistent Library catalogue, Deep Analysis, prepared relationships, "
            "Engine settings, and reference evidence with the selected backup. Source MIDI files are never replaced or deleted.\n\n"
            "A verified safety backup of the current Library will be created first. The Library Engine will stop, the restore will be verified, and then the Engine will restart.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        if self._long_action_inflight:
            QMessageBox.information(self, "Library Restore", "Wait for the current maintenance action to finish first.")
            return

        engine_was_ready = bool(self._engine_ready)
        self._long_action_inflight = "restore"
        self._long_action_context = {}
        self._set_long_action_busy(True, "Verifying backup and creating a pre-restore safety backup…")

        def run_restore() -> None:
            payload: dict[str, Any] = {}
            error = ""
            engine_stopped = False
            try:
                verify_library_intelligence_backup(selected)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                safety = self.paths.backups / f"MIDI-Library-Data-Pre-Restore-{stamp}.zip"
                if engine_was_ready:
                    # Online safety backup must be Engine-owned while writers are
                    # live; then stop the Engine before replacing persistent files.
                    backup_response = self.client.command("backup", timeout=120.0, target=str(safety))
                    safety_path = str(backup_response.get("path") or safety)
                    if not self.client.shutdown(request_timeout=SHUTDOWN_REQUEST_SECONDS, wait_timeout=CLIENT_SHUTDOWN_WAIT_SECONDS):
                        raise RuntimeError("Library Engine did not release its database handles; restore was not started.")
                    engine_stopped = True
                else:
                    # Restore is also a recovery path when Engine startup failed.
                    # First prove no detached writer remains, then create the same
                    # verified safety backup locally from the quiescent files.
                    if not self.client.shutdown(request_timeout=SHUTDOWN_REQUEST_SECONDS, wait_timeout=CLIENT_SHUTDOWN_WAIT_SECONDS):
                        raise RuntimeError("A Library Engine process is still using the databases; restore was not started.")
                    engine_stopped = True
                    safety_path = str(create_library_intelligence_backup(self.paths, safety).path)
                restored = restore_library_intelligence_backup(self.paths, selected)
                self.client.ensure_running(timeout=30.0)
                self.client.status()
                engine_stopped = False
                payload = {
                    "source": restored.source,
                    "safety_backup": safety_path,
                    "restored_count": len(restored.restored_files),
                }
            except (LibraryEngineUnavailable, RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                if engine_stopped:
                    try:
                        self.client.ensure_running(timeout=30.0)
                    except (LibraryEngineUnavailable, RuntimeError, OSError):
                        pass
            self.longActionFinished.emit("restore", payload, error)

        # Restore performs local filesystem replacement after the Engine has
        # released database handles. It must not be abandoned by interpreter
        # exit between those persistent boundaries.
        threading.Thread(target=run_restore, name="MidiLibraryManager-Restore", daemon=False).start()

    def _rebuild_library(self) -> None:
        if QMessageBox.warning(
            self, "Rebuild Library Index & Analysis",
            "This discards the derived catalogue, Deep Analysis results, and prepared recommendation relationships, "
            "then rebuilds them from the configured source MIDI folders.\n\n"
            "MIDI source files are not deleted. MIDI Library Engine will create and verify a rollback backup before anything is cleared.\n\n"
            "Use this only when you intentionally want a full re-analysis.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start_long_action("rebuild", "rebuild_library", timeout=90.0)

    def _open_remixer(self) -> None:
        candidates = [
            self.root / "MIDI Remixer.pyw",
            self.root / "Launch MIDI Remixer.bat",
        ]
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                if candidate.suffix.casefold() == ".bat" and os.name == "nt":
                    os.startfile(str(candidate))
                else:
                    launch_gui_python_script(candidate, cwd=self.root)
                return
            except OSError:
                continue
        QMessageBox.information(self, "MIDI Remixer", "MIDI Remixer is not installed in this component package.")

    def _begin_engine_stop_feedback(self, *, full_exit: bool) -> None:
        """Show shutdown feedback and arm an independent UI-level deadline."""
        self._engine_stop_started_monotonic = time.monotonic()
        self._engine_stop_full_exit = bool(full_exit)
        self._engine_stop_escalation_started = False
        try:
            self._engine_stop_target = self.client.capture_shutdown_target()
        except (OSError, RuntimeError, ValueError, TypeError):
            self._engine_stop_target = {}
        self._engine_stop_feedback_timer.start()
        self._refresh_engine_stop_feedback()

    def _refresh_engine_stop_feedback(self) -> None:
        if self._engine_stop_started_monotonic <= 0.0:
            return
        elapsed = max(0.0, time.monotonic() - self._engine_stop_started_monotonic)
        suffix = " for full exit" if self._engine_stop_full_exit else ""
        self.engine_state.setText(f"State · Stopping Library Engine{suffix}… {elapsed:.1f}s")
        if self._engine_stop_full_exit:
            instruction = "Please leave this window open; it will close automatically when every Library process is verified stopped."
        else:
            instruction = "Please leave this window open until State changes to ‘Library Engine stopped’. You may then close or restart the Engine."
        self.status_detail.setText(
            "Stopping Library work and verifying analyzer/Engine processes are terminal. "
            "Completed work is already saved. " + instruction
        )
        # This timer belongs to Qt, not the shutdown worker thread. If that
        # worker itself becomes stuck in a Windows/runtime call, the Manager
        # still owns a hard user-facing deadline and can terminate the exact
        # captured Engine process tree instead of waiting indefinitely.
        if (
            self._engine_stop_full_exit
            and elapsed >= CLIENT_SHUTDOWN_WAIT_SECONDS
            and not self._engine_stop_escalation_started
        ):
            self._start_engine_force_stop(
                "Graceful shutdown exceeded the 15-second full-exit deadline; forcing the verified Library process tree…"
            )

    def _start_engine_force_stop(self, message: str = "") -> None:
        if self._engine_stop_escalation_started:
            return
        self._engine_stop_escalation_started = True
        if message:
            self.status_detail.setText(message)
        target = dict(self._engine_stop_target or {})

        def force_stop() -> None:
            try:
                startup_thread = self._engine_connect_thread
                startup_cancel = bool(self._engine_connect_cancel.is_set() and self._exit_after_engine_stop)
                # RC8 captured the watchdog ticket at the instant Close was
                # accepted. On Windows the Engine can publish its startup lock
                # a few scheduler ticks before the launching thread stores its
                # exact Popen handle. A lifetime-lock-only ticket captured in
                # that gap is intentionally too weak to kill a live PID. Refresh
                # non-endpoint startup tickets here, after cancellation has had
                # time to publish/terminate the exact child, rather than turning
                # a safe transient race into an indefinitely open Manager.
                if startup_cancel and str(target.get("ownership") or "") != "endpoint":
                    refreshed = self.client.capture_shutdown_target()
                    if refreshed:
                        target.clear()
                        target.update(refreshed)

                if int(target.get("pid") or 0) <= 0 and startup_thread is not None and startup_thread.is_alive():
                    # No Engine identity is visible yet. Give the already
                    # cancelled launcher only the same bounded force window to
                    # cross its next cancellation check; never claim terminality
                    # while it could still return from Popen afterwards.
                    deadline = time.monotonic() + MANAGER_FULL_EXIT_FORCE_SECONDS
                    while startup_thread.is_alive() and time.monotonic() < deadline:
                        startup_thread.join(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
                        refreshed = self.client.capture_shutdown_target()
                        if int(refreshed.get("pid") or 0) > 0:
                            target.clear()
                            target.update(refreshed)
                            break
                if int(target.get("pid") or 0) <= 0 and startup_thread is not None and startup_thread.is_alive():
                    stopped = False
                    detail = (
                        "Engine startup cancellation has not yet reached a terminal boundary; "
                        "the Manager will not claim full exit while that startup worker can still create a detached process."
                    )
                else:
                    # Re-capture once more after a cancelled startup worker exits.
                    # This preserves a dead startup-lock PID long enough for
                    # target-safe stale-file cleanup, while endpoint-owned normal
                    # shutdown keeps its original authenticated guard.
                    if startup_cancel and str(target.get("ownership") or "") != "endpoint":
                        refreshed = self.client.capture_shutdown_target()
                        if refreshed:
                            target.clear()
                            target.update(refreshed)
                    stopped = self.client.force_stop_captured_target(target, timeout=MANAGER_FULL_EXIT_FORCE_SECONDS)
                    detail = str(self.client.last_shutdown_detail or "")
                if not stopped and not detail:
                    detail = "Emergency process-tree cleanup did not prove the Library Engine terminal."
            except (LibraryEngineUnavailable, OSError, RuntimeError, ValueError, TypeError) as exc:
                stopped = False
                detail = f"{type(exc).__name__}: {exc}"
            self.engineForceStopFinished.emit(stopped, detail)

        threading.Thread(target=force_stop, name="MidiLibraryManagerForceStop", daemon=True).start()

    def _finish_engine_force_stop(self, stopped: bool, detail: str) -> None:
        if stopped:
            self._finish_engine_stop(True, detail)
            return
        # Force cleanup is the final bounded lifecycle boundary. Stop the
        # feedback timer and keep the Manager visible with a concrete failure
        # instead of displaying an endlessly increasing shutdown counter.
        self._engine_stop_feedback_timer.stop()
        self._engine_stop_started_monotonic = 0.0
        self._engine_stop_full_exit = False
        self._end_manager_busy("engine_stop")
        self._exit_after_engine_stop = False
        self._engine_stopped_by_user = False
        self.engine_state.setText("State · Library Engine stop failed")
        self.status_detail.setText(detail or "The Library Engine could not be stopped within the bounded full-exit deadline.")
        QMessageBox.warning(
            self, "Library Engine Could Not Stop",
            "The application did not prove the Library process tree terminal within its bounded shutdown window. "
            "The Manager has stopped waiting and remains open for diagnosis.\n\n" +
            (detail or "No additional diagnostic was returned."),
        )

    def _stop_engine(self) -> None:
        if self._long_action_inflight:
            QMessageBox.information(
                self, "Library Maintenance In Progress",
                "Finish the current Library maintenance operation before stopping the Engine. "
                "This prevents backup/restore/rebuild state from being interrupted between persistent boundaries.",
            )
            return
        if QMessageBox.question(
            self, "Stop Library Engine",
            "Stop all background library scanning, detailed analysis, and recommendation preparation?\n\n"
            "Completed work is already saved. You can restart the engine later by reopening Library Manager"
            + (" or MIDI Remixer." if self._remixer_available() else "."),
        ) != QMessageBox.StandardButton.Yes:
            return
        self._engine_stopped_by_user = True
        self._engine_ready = False
        self._set_engine_controls_enabled(False)
        self._begin_engine_stop_feedback(full_exit=False)
        self._begin_manager_busy("engine_stop")

        def stop() -> None:
            try:
                stopped = self.client.shutdown(request_timeout=SHUTDOWN_REQUEST_SECONDS, wait_timeout=CLIENT_SHUTDOWN_WAIT_SECONDS)
                detail = str(self.client.last_shutdown_detail or "")
                if not stopped and not detail:
                    detail = "The Engine did not release its writer lock within the shutdown window."
            except (LibraryEngineUnavailable, RuntimeError, OSError) as exc:
                stopped = False
                detail = f"{type(exc).__name__}: {exc}"
            self.engineStopFinished.emit(stopped, detail)

        threading.Thread(target=stop, name="MidiLibraryManagerEngineStop", daemon=True).start()

    def _finish_engine_stop(self, stopped: bool, detail: str) -> None:
        exit_after_stop = bool(self._exit_after_engine_stop)
        # A graceful worker failure during full exit is not the terminal result.
        # Escalate through the independently captured process identity first.
        if not stopped and exit_after_stop:
            if not self._engine_stop_escalation_started:
                self._start_engine_force_stop(
                    "Graceful shutdown did not complete; forcing the verified Library process tree…"
                )
            # If the Qt watchdog already launched emergency cleanup, a late
            # False result from the graceful worker must not cancel that newer
            # terminal attempt. Wait for engineForceStopFinished instead.
            return
        self._engine_stop_feedback_timer.stop()
        self._engine_stop_started_monotonic = 0.0
        self._engine_stop_full_exit = False
        self._end_manager_busy("engine_stop")
        self._exit_after_engine_stop = False
        if stopped:
            self.engine_state.setText("State · Library Engine stopped · SAFE TO CLOSE")
            self.engine_action_button.setText("Start Library Engine")
            self.engine_action_button.setVisible(True)
            forced_note = (
                " The Engine required forced process-tree cleanup after missing its graceful shutdown deadline; "
                "the next launch will verify the persistent databases."
                if "force-stopped" in str(detail or "").casefold() else ""
            )
            self.status_detail.setText(
                "Background scanning, detailed analysis, and recommendation preparation are stopped. "
                "Committed library work remains saved." + forced_note + " Reopen Library Manager"
                + (" or MIDI Remixer" if self._remixer_available() else "")
                + " to start the Engine again."
            )
            if exit_after_stop:
                # The actual process-tree shutdown ran off the Qt thread. Once
                # terminality is proven, do not re-enter closeEvent and re-run
                # lifetime decisions. End the Qt event loop directly; Manager
                # process finalization below performs one last Python-thread audit.
                self._full_exit_verified = True
                QTimer.singleShot(0, self._complete_verified_full_exit)
        else:
            self._engine_stopped_by_user = False
            self.engine_state.setText("State · Library Engine stop not confirmed")
            self.engine_action_button.setText("Retry Engine Start")
            self.engine_action_button.setVisible(True)
            self.status_detail.setText(detail or "The Engine is still finishing shutdown work.")
            if exit_after_stop:
                QMessageBox.warning(
                    self,
                    "Library Engine Still Running",
                    "Full exit was cancelled because the Library-owned process tree was not proven terminal. "
                    "The Manager remains open so cleanup can be retried.\n\n"
                    + (detail or "No additional diagnostic was returned."),
                )


    def _complete_verified_full_exit(self) -> None:
        """Terminate the Manager UI after Engine/process-tree terminality.

        This path is deliberately separate from closeEvent. Re-entering the
        close handler after a successful full shutdown used to make final exit
        depend on stale endpoint/UI state. At this point Browser cleanup and
        Engine cleanup have both already been proven terminal.
        """
        self._close_in_progress = True
        try:
            self._poll_timer.stop()
            self._engine_stop_feedback_timer.stop()
            self._manager_busy_delay.stop()
        except RuntimeError:
            pass
        self.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()
        else:
            self.close()

    def _poll_status(self) -> None:
        # Target-Windows qualification markers can represent a real Engine state
        # that exists independently of the Manager's heartbeat/service-ready
        # flags.  Observe them before branching on `_engine_ready`; otherwise a
        # fast early-IPC attach can race a still-active SQLite migration and make
        # the exact same product state visible or invisible depending on thread
        # scheduling.  This hook is inert in normal product use.
        self._maybe_trigger_qa_marker_backed_close_state()
        if not self._engine_ready:
            if self._engine_stopped_by_user:
                return
            if self._engine_connecting:
                elapsed = max(0, int(time.monotonic() - self._engine_connect_started))
                self.engine_state.setText(f"Starting Library Engine… {elapsed}s")
                self.status_detail.setText(
                    "Starting the independent Library Engine in the background and attaching this Manager. "
                    "Committed Library data remains available through Browse Library while startup completes."
                )
                self._render_offline_catalogue(state_label="ENGINE STARTING")
            else:
                self.engine_state.setText("Library Engine unavailable")
                detail = self._engine_connect_error or "The Library Engine did not start."
                self.status_detail.setText(detail)
                self._show_engine_unavailable(detail)
                self.engine_action_button.setText("Retry Engine Start")
                self.engine_action_button.setVisible(True)
                self._maybe_trigger_qa_close_on_state("engine:offline", "engine:error")
            return
        if self._status_poll_inflight:
            return
        self._status_poll_inflight = True

        def fetch() -> None:
            status: dict[str, Any] = {}
            error = ""
            try:
                status = self._engine_initial_status or self.client.status(start_if_needed=False, timeout=5.0)
                self._engine_initial_status = None
            except (LibraryEngineUnavailable, RuntimeError) as exc:
                heartbeat = self.client.ping(timeout=0.9)
                if heartbeat is not None:
                    status = {"_heartbeat_only": True, "_status_delay": f"{type(exc).__name__}: {exc}"}
                else:
                    error = f"{type(exc).__name__}: {exc}"
            self.statusFetched.emit(status, error)

        threading.Thread(target=fetch, name="MidiLibraryManagerStatusPoll", daemon=True).start()

    def _maybe_trigger_qa_close_on_startup_lock(self) -> None:
        """Close through the real Qt path once Engine owns startup but no endpoint exists."""
        if not str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_ON_STARTUP_LOCK", "")).strip():
            self._qa_startup_lock_close_timer.stop()
            return
        try:
            if self.client.endpoint_file.exists() or not self.client.start_lock_file.exists():
                return
            engine_pid = int(self.client.start_lock_file.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            return
        if engine_pid <= 0:
            return
        self._qa_startup_lock_close_timer.stop()
        try:
            marker = self.paths.logs / "qa-manager-startup-lock-close.txt"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                f"engine_pid={engine_pid} endpoint_present=0 tick={time.monotonic():.6f}",
                encoding="utf-8",
            )
        except OSError:
            # The lifecycle action is the qualification target; the harness will
            # report a missing marker separately without changing product Close.
            pass
        self.close()


    def _record_qa_event_loop_heartbeat(self) -> None:
        """Write a monotonic QA tick only when the Windows runtime gate enables it."""
        if not str(os.environ.get("MIDI_LIBRARY_QA_EVENT_LOOP_HEARTBEAT", "")).strip():
            return
        self._qa_event_loop_tick += 1
        try:
            marker = self.paths.logs / "qa-manager-event-loop-heartbeat.txt"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                f"{self._qa_event_loop_tick},{time.monotonic():.6f}", encoding="utf-8"
            )
        except OSError:
            # Instrumentation failure must not perturb product shutdown; the
            # Windows harness will fail explicitly if the marker cannot advance.
            return

    def _maybe_trigger_qa_marker_backed_close_state(self) -> None:
        """Observe QA-only Engine markers independently of connection timing.

        Some qualification boundaries occur before the full Engine service is
        attached (schema migration) or in a supervised child timeout path.
        Those states are authoritatively represented by Engine-owned marker
        files.  Reading them from the Qt poll keeps the close trigger tied to the
        real state without coupling it to `_engine_ready` or a status-RPC race.
        """
        target = str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_ON_STATE", "")).strip().casefold()
        if not target or self._qa_close_on_state_triggered:
            return
        marker_states = {
            "migration": self.paths.library / "qa-migration-active.txt",
            "analyzer:timeout": self.paths.library / "qa-analyzer-timeout-active.txt",
        }
        marker = marker_states.get(target)
        if marker is not None and marker.exists():
            self._maybe_trigger_qa_close_on_state(target)


    def _maybe_trigger_qa_close_on_state(self, *states: str) -> None:
        """Target-Windows qualification hook for deterministic Close boundaries.

        This is inert in normal product use. The Windows lifecycle self-check can
        request a specific published runtime state (for example
        ``deep:cancelling``) and the real Qt Manager closes only after its own
        status renderer has observed that state.
        """
        if self._qa_close_on_state_triggered:
            return
        target = str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_ON_STATE", "")).strip().casefold()
        if not target:
            return
        normalized = {str(state or "").strip().casefold() for state in states if str(state or "").strip()}
        if target not in normalized:
            return
        self._qa_close_on_state_triggered = True
        try:
            marker = self.paths.logs / "qa-manager-close-state.txt"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(target, encoding="utf-8")
        except OSError:
            pass
        try:
            settle_ms = max(0, int(os.environ.get("MIDI_LIBRARY_QA_CLOSE_STATE_SETTLE_MS", "0") or 0))
        except ValueError:
            settle_ms = 0
        QTimer.singleShot(settle_ms, self.close)

    def _write_qa_maintenance_event(self, event: str, detail: str = "") -> None:
        """Persist QA-only deferred-maintenance lifecycle evidence.

        Normal product behavior is unchanged.  Target-Windows qualification
        uses this append-only marker to distinguish time spent reaching the
        maintenance persistence boundary from the separate full-exit budget.
        That prevents a legitimate deferred Close from being misclassified as
        a 19-second shutdown hang merely because the backup itself was still
        reaching its committed boundary.
        """
        if not str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_DURING_MAINTENANCE", "")).strip():
            return
        try:
            marker = self.paths.logs / "qa-manager-maintenance-events.txt"
            marker.parent.mkdir(parents=True, exist_ok=True)
            safe_detail = str(detail or "").replace("\r", " ").replace("\n", " ")
            with marker.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.monotonic():.6f}\t{event}\t{safe_detail}\n")
        except OSError:
            # QA evidence failure must not perturb the product path; the harness
            # will fail explicitly if the expected lifecycle evidence is absent.
            return

    def _maybe_start_qa_maintenance(self) -> None:
        """Start a real Manager-owned maintenance action for Windows Close QA.

        The operation is deliberately initiated through the same asynchronous
        Manager path as the Backup button so closeEvent must exercise deferred
        exit rather than bypassing the UI ownership contract.
        """
        if self._qa_maintenance_started or not self._engine_ready or self._long_action_inflight:
            return
        kind = str(os.environ.get("MIDI_LIBRARY_QA_START_MAINTENANCE", "")).strip().casefold()
        if kind != "backup":
            return
        target = str(os.environ.get("MIDI_LIBRARY_QA_MAINTENANCE_TARGET", "")).strip()
        if not target:
            return
        self._qa_maintenance_started = True
        if self._start_long_action("backup", "backup", timeout=180.0, target=target):
            self._write_qa_maintenance_event("action-started", target)
            if str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_DURING_MAINTENANCE", "")).strip():
                QTimer.singleShot(0, self.close)

    def _apply_polled_status(self, status_obj: object, error: str) -> None:
        self._status_poll_inflight = False
        if error:
            self._status_failure_count += 1
            self._engine_connect_error = error
            if self._status_failure_count < 3:
                # A single missed localhost heartbeat can occur while Windows is
                # scheduling a CPU-heavy worker burst. Do not flash a frightening
                # pink startup failure for a transient miss; keep polling and only
                # declare the Engine unavailable after repeated independent misses.
                self.engine_state.setText(
                    f"State · Library Engine connection delayed · retrying ({self._status_failure_count}/3)"
                )
                self.status_detail.setText(
                    "A Library Engine heartbeat was briefly delayed. Saved catalogue data is safe; "
                    "the Manager is retrying automatically. " + error
                )
                self.engine_error_banner.setVisible(False)
                self.engine_action_button.setVisible(False)
                return
            self._engine_ready = False
            self._engine_connecting = False
            self.engine_state.setText("Library Engine unavailable")
            self.status_detail.setText(error)
            self._show_engine_unavailable(error)
            self.engine_action_button.setText("Retry Engine Start")
            self.engine_action_button.setVisible(True)
            self._maybe_trigger_qa_close_on_state("engine:offline", "engine:error")
            return
        self._status_failure_count = 0
        status = dict(status_obj) if isinstance(status_obj, dict) else {}
        if status.get("_heartbeat_only"):
            self._engine_ready = True
            self.engine_state.setText("State · Library Engine running · status delayed")
            self.engine_error_banner.setVisible(False)
            self.engine_action_button.setVisible(False)
            self.status_detail.setText(
                "Library Engine heartbeat is healthy. A detailed status snapshot is temporarily delayed; "
                "the Manager will retry automatically. " + str(status.get("_status_delay") or "")
            )
            return
        self._render_status(status)

    def _render_status(self, status: dict[str, Any]) -> None:
        self._latest_engine_status = dict(status)
        if str(status.get("engine") or "").casefold() != "starting":
            self._end_manager_busy("engine_start")
        self.engine_error_banner.setVisible(False)
        self.engine_action_button.setVisible(False)
        if str(status.get("engine") or "") == "starting":
            self._set_engine_controls_enabled(False)
            startup = status.get("startup") or {}
            phase = str(startup.get("state") or "opening persistent library")
            self.engine_state.setText(f"State · Library Engine starting · {phase}")
            self.status_detail.setText(
                "The Engine heartbeat is available while persistent databases finish opening. "
                "Saved catalogue browsing remains available; processing controls will enable when the service is ready."
            )
            self._render_offline_catalogue(state_label="ENGINE STARTING")
            self._maybe_trigger_qa_close_on_state("startup", f"startup:{phase.casefold()}")
            return
        self._set_engine_controls_enabled(True)
        if not self._engine_preferences_applied:
            self._apply_engine_preferences_from_status(status)
            self._engine_preferences_applied = True

        status_roots = [str(path) for path in status.get("roots") or []]
        # Source availability is probed by the Engine's background coordinator.
        # Never stat a potentially offline/removable/network root on the Qt
        # thread merely to render HOME; that can make the Manager appear hung.
        accessible_roots = [str(path) for path in status.get("accessible_roots") or []]
        unavailable_roots = [str(path) for path in status.get("unavailable_roots") or []]
        self._configured_source_roots = list(status_roots)
        self._accessible_source_roots = list(accessible_roots)
        if status_roots:
            primary_root = status_roots[0]
            display_root = primary_root if len(primary_root) <= 78 else "…" + primary_root[-77:]
            extra = f" (+{len(status_roots) - 1} more)" if len(status_roots) > 1 else ""
            unavailable_note = f" · {len(unavailable_roots)} unavailable" if unavailable_roots else ""
            self.library_folder_status.setText(f"MIDI folders: {display_root}{extra}{unavailable_note}")
            tooltip_lines = [
                *(f"Connected: {root}" for root in accessible_roots),
                *(f"Unavailable: {root}" for root in unavailable_roots),
            ]
            self.library_folder_status.setToolTip("\n".join(tooltip_lines))
        else:
            self.library_folder_status.setText("MIDI folders: none connected")
            self.library_folder_status.setToolTip("")
        displayed_roots = [self.roots_list.item(index).text() for index in range(self.roots_list.count())]
        if displayed_roots != status_roots:
            self.roots_list.clear()
            self.roots_list.addItems(status_roots)

        maintenance = str(status.get("maintenance") or "")
        startup = status.get("startup") or {}
        catalogue = status.get("catalogue") or {}
        deep = status.get("deep") or {}
        readiness = status.get("readiness") or {}
        lifecycle = status.get("lifecycle") or {}
        relations = status.get("relationships") or {}
        scan = status.get("scan") or {}
        scan_operation_kind = str(scan.get("operation_kind") or "fast_scan")
        raw_metrics_age = status.get("status_metrics_age_seconds")
        try:
            status_metrics_ready = raw_metrics_age is not None and float(raw_metrics_age) >= 0.0
        except (TypeError, ValueError):
            status_metrics_ready = False
        self._last_deep_status = dict(deep)

        deep_state = str(deep.get("state") or "idle").casefold()
        deep_failed = int(deep.get("failed") or 0)
        startup_running = bool(startup.get("running"))
        startup_state = str(startup.get("state") or "").strip()
        if maintenance:
            current_state = f"Maintenance · {maintenance}"
        elif bool(scan.get("running")):
            operation_name = "Unreadable File Retry" if scan_operation_kind == "fast_recovery" else "Fast library scan"
            if bool(scan.get("paused")):
                current_state = f"{operation_name} · PAUSED"
            else:
                phase = str(scan.get("phase") or "cataloguing").replace("_", " ").title()
                if scan_operation_kind == "fast_recovery" and str(scan.get("phase") or "") == "cataloguing":
                    phase = "Retrying unreadable files"
                current_state = f"{operation_name} · {phase}"
        elif deep_state in {"running", "starting"}:
            current_state = f"Deep Analysis · {deep_state.upper()}"
        elif deep_state == "paused":
            current_state = "Deep Analysis · PAUSED"
        elif deep_state in {"failed", "attention"}:
            current_state = f"Deep Analysis · ATTENTION · {deep_failed:,} review item(s)"
        elif bool(relations.get("building")):
            current_state = "Preparing mash recommendations"
        elif startup_running and startup_state and startup_state != "ready":
            current_state = f"Engine starting · {startup_state}"
        elif not status_metrics_ready:
            # Aggregate catalogue/lifecycle counts are refreshed asynchronously
            # so a large returning Library never blocks the Engine heartbeat.
            # Until that first committed snapshot arrives, do not misrepresent
            # the customer as a first-time/empty-Library user.
            current_state = "Loading persisted Library status…"
        elif int(catalogue.get("catalogued") or 0) > 0 and not status_roots:
            current_state = "Ready / saved catalogue · no active source"
        elif int(catalogue.get("catalogued") or 0) > 0 and not accessible_roots:
            current_state = "Ready / saved catalogue · configured source unavailable"
        elif int(catalogue.get("catalogued") or 0) > 0 and unavailable_roots:
            current_state = f"Ready / library · {len(unavailable_roots)} source(s) unavailable"
        elif int(catalogue.get("catalogued") or 0) > 0:
            current_state = "Ready / library up to date"
        elif status_roots and not accessible_roots:
            current_state = "Source unavailable · reconnect or remove source"
        else:
            current_state = "First run · add source or restore backup"
        self.engine_state.setText(f"State · {current_state}")

        storage = status.get("storage") or {}
        quick_database = str(storage.get("quick_database") or "").strip()
        catalogued_count = int(catalogue.get("catalogued") or 0)
        first_run_empty = bool(status_metrics_ready and not status_roots and int(catalogue.get("total") or 0) <= 0)
        if quick_database:
            if catalogued_count > 0 and not status_roots:
                suffix = " · offline snapshot · add a source to reconcile"
            elif catalogued_count > 0 and status_roots and not accessible_roots:
                suffix = " · configured source unavailable"
            elif catalogued_count > 0 and unavailable_roots:
                suffix = f" · {len(unavailable_roots)} source(s) unavailable"
            else:
                suffix = ""
            if not status_metrics_ready:
                self.storage_status.setText("Saved catalogue: loading persisted counts…")
            elif first_run_empty:
                self.storage_status.setText("Saved catalogue: none yet")
            else:
                self.storage_status.setText(f"Saved catalogue: {catalogued_count:,} MIDI(s){suffix}")
            self.storage_status.setToolTip(quick_database)
        else:
            self.storage_status.setText("Saved catalogue: storage path unavailable")
            self.storage_status.setToolTip("")
        if status_metrics_ready:
            self.curation_status.setText(
                f"Curated Library · {int(lifecycle.get('discovered') or 0):,} discovered · "
                f"{int(lifecycle.get('usable') or 0):,} usable · {int(lifecycle.get('mash_ready') or 0):,} Mash Ready · "
                f"{int(lifecycle.get('fusion_candidate') or 0):,} Fusion · {int(lifecycle.get('usable_not_mash') or 0):,} Not Mash · "
                f"{int(lifecycle.get('needs_review') or 0):,} review · {int(lifecycle.get('rejected') or 0):,} rejected · "
                f"{int(lifecycle.get('quarantined') or 0):,} quarantined · {int(lifecycle.get('unresolved') or 0):,} unresolved"
            )
        else:
            self.curation_status.setText("Curated Library · loading persisted lifecycle counts…")
        fast_failure_count = int(catalogue.get("failed") or 0)
        suspicious_loadable_count = max(0, int(catalogue.get("suspicious") or 0) - fast_failure_count)
        if status_metrics_ready:
            saved_rows_available = int(catalogue.get("total") or 0) > 0
            self.browse_library_button.setEnabled(saved_rows_available)
            self.statistics_button.setEnabled(saved_rows_available)
            if (
                saved_rows_available
                and not self._qa_browser_opened
                and str(os.environ.get("MIDI_LIBRARY_QA_OPEN_BROWSER_ON_READY", "")).strip()
            ):
                self._qa_browser_opened = True
                QTimer.singleShot(0, self._open_library_browser)
            self.fast_failures_button.setEnabled(fast_failure_count > 0)
            self.suspicious_button.setEnabled(suspicious_loadable_count > 0)
            self.backup_button.setEnabled(bool(status_roots) or saved_rows_available)
            self.rebuild_button.setEnabled(bool(status_roots) and not unavailable_roots and saved_rows_available)
            self.rebuild_button.setToolTip(
                "Create a verified safety backup, then rebuild Fast/Deep/recommendation intelligence from all configured source folders."
                if status_roots and not unavailable_roots else
                "Reconnect every configured MIDI source before rebuilding. A partial rebuild could otherwise mistake an offline source for deleted files."
            )
            self.remove_folder_button.setEnabled(bool(status_roots))
        self.fast_failures_button.setText(f"Unreadable ({fast_failure_count:,})…")
        self.suspicious_button.setText(f"Unusual ({suspicious_loadable_count:,})…")
        self.suspicious_button.setToolTip(
            "Review MIDI files that loaded successfully but look unusually short, long, sparse, or empty. These are not Fast Scan failures and do not need a retry unless the source file changes."
        )

        deep_waiting = int(deep.get("remaining") or 0)
        self._statistics_values.update({
            "catalogued": catalogued_count,
            "distinct": int(catalogue.get("distinct_families") or 0),
            "health_clear": int(catalogue.get("no_basic_health_flag") or 0),
            "suspicious": max(0, int(catalogue.get("suspicious") or 0) - int(catalogue.get("failed") or 0)),
            "fast_failed": int(catalogue.get("failed") or 0),
            "deep_complete": int(deep.get("complete") or 0),
            "deep_remaining": deep_waiting,
            "failed": int(catalogue.get("failed") or 0) + deep_failed,
            "guided": int(readiness.get("guided_ready") or 0),
            "relationships": int(relations.get("count") or 0),
            "discovered": int(lifecycle.get("discovered") or 0),
            "usable": int(lifecycle.get("usable") or 0),
            "mash_ready": int(lifecycle.get("mash_ready") or 0),
            "fusion_candidate": int(lifecycle.get("fusion_candidate") or 0),
            "usable_not_mash": int(lifecycle.get("usable_not_mash") or 0),
            "needs_review": int(lifecycle.get("needs_review") or 0),
            "rejected": int(lifecycle.get("rejected") or 0),
            "quarantined": int(lifecycle.get("quarantined") or 0),
            "unresolved": int(lifecycle.get("unresolved") or 0),
        })

        scan_total = int(scan.get("total") or 0)
        scan_current = int(scan.get("current") or 0)
        scan_running = bool(scan.get("running"))
        scan_worker_processes = int(scan.get("worker_processes") or 0)
        scan_worker_limit = max(1, int(scan.get("worker_limit") or 1))
        # Source topology changes are deliberately unavailable while Fast work is
        # active.  Changing roots mid-inventory makes the visible source list and
        # the scan's filesystem snapshot describe different worlds.  Wait for a
        # committed Fast boundary, then mutate sources and reconcile once.
        self.add_folder_button.setEnabled(not scan_running)
        self.remove_folder_button.setEnabled(bool(status_roots) and not scan_running)
        source_busy_tip = (
            "Wait for the active Fast Scan/Retry to finish or cancel it before changing Library source folders."
        )
        self.add_folder_button.setToolTip(
            source_busy_tip if scan_running else
            "Add a folder containing MIDI files to Library sources. After selection, the Library starts the appropriate incremental Fast Scan; background Deep Analysis/readiness also resumes when that preference is enabled."
        )
        self.remove_folder_button.setToolTip(
            source_busy_tip if scan_running else
            "Remove the highlighted folder from active Library sources after confirmation. Source MIDI files are never deleted. If other sources remain, Fast Scan reconciles rows from the removed source; if this is the last source, the saved Library is preserved as an offline snapshot until a source is added again."
        )
        scan_phase = str(scan.get("phase") or "").strip().casefold()
        fast_found = int(catalogue.get("total") or 0)
        fast_failed = int(catalogue.get("failed") or 0)
        # Some Engine bootstrap states use a sentinel total of 1 before the
        # inventory population is known. Never present that sentinel as a real
        # 0/1 user denominator; Preparing/Discovery is indeterminate until the
        # first authoritative population arrives.
        preparing_population = (
            scan_running
            and scan_current <= 0
            and scan_total <= 1
            and scan_phase in {"", "preparing", "prepare", "discovery", "inventory"}
        )
        if preparing_population:
            self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat("Preparing…")
        elif scan_running and scan_total > 0:
            self.scan_progress.setRange(0, scan_total)
            self.scan_progress.setValue(min(scan_current, scan_total))
            percent = (100.0 * min(scan_current, scan_total) / scan_total) if scan_total else 0.0
            percent_text = f"{percent:.1f}%" if percent < 10.0 else f"{percent:.0f}%"
            self.scan_progress.setFormat(
                f"{percent_text} · {scan_current:,}/{scan_total:,}"
            )
        elif scan_running:
            self.scan_progress.setRange(0, 0)
            self.scan_progress.setFormat("Discovering…")
        else:
            # A completed scan can be 100% processed without being 100% usable.
            # Report the outcome directly instead of visually implying every
            # discovered file entered the usable catalogue.
            self.scan_progress.setRange(0, max(1, fast_found))
            self.scan_progress.setValue(fast_found if fast_found else 0)
            if fast_found:
                self.scan_progress.setFormat(
                    f"Done · {fast_found:,}"
                )
            elif not status_roots:
                self.scan_progress.setFormat("Waiting")
            else:
                self.scan_progress.setFormat("No MIDI")
        phase_key = str(scan.get("phase") or "")
        scan_rate = format_file_rate(scan.get("rate_per_second"), decimals=1)
        scan_eta = format_eta(scan.get("eta_seconds"))
        if scan_running:
            if scan_operation_kind == "fast_recovery":
                phase_labels = {
                    "recovery_prepare": "Preparing retry",
                    "cataloguing": "Retrying unreadable MIDI files",
                    "complete": "Finalizing retry results",
                }
                scan_parts = [f"Unreadable File Retry: {phase_labels.get(phase_key, phase_key.replace('_', ' ').title() or 'Working')}"]
            else:
                phase_labels = {
                    "discovery": "Discovering MIDI files",
                    "inventory": "Comparing file changes",
                    "cataloguing": "Cataloguing new/changed MIDI files",
                    "complete": "Finalizing saved catalogue",
                }
                scan_parts = [f"Fast catalogue: {phase_labels.get(phase_key, phase_key.replace('_', ' ').title() or 'Working')}"]
            if scan_total > 0:
                scan_parts.append(f"{scan_current:,}/{scan_total:,}")
        else:
            if not status_roots and fast_found <= 0:
                scan_parts = ["Fast catalogue: waiting for a MIDI source folder"]
            else:
                scan_parts = [
                    f"Fast catalogue: {fast_found:,} found · {catalogued_count:,} usable · {fast_failed:,} unreadable"
                ]
        if scan_rate:
            scan_parts.append(scan_rate)
        if scan_eta:
            if scan_operation_kind == "fast_recovery":
                eta_label = "Retry ETA"
            else:
                eta_label = "Fast-ready ETA" if phase_key == "cataloguing" else "Phase ETA"
            scan_parts.append(f"{eta_label} {scan_eta}")
        elif bool(scan.get("running")) and phase_key == "discovery":
            scan_parts.append("ETA estimating…")
        if bool(scan.get("paused")):
            scan_parts.append("PAUSED")
        elif bool(scan.get("pause_requested")):
            scan_parts.append("PAUSING…")
        current_file = str(scan.get("current_file") or "").strip()
        current_folder = str(scan.get("current_folder") or "").strip()
        if current_file:
            source_name = Path(current_file).name
            if len(source_name) > 34:
                source_name = source_name[:15] + "…" + source_name[-15:]
            scan_parts.append(source_name)
        self.scan_label.setToolTip("\n".join(part for part in (current_file, current_folder) if part))
        self.scan_label.setText(" · ".join(scan_parts))
        self.scan_location.setVisible(False)
        scan_paused = bool(scan.get("paused"))
        scan_operation_state = str(scan.get("operation_state") or ("paused" if scan_paused else "running" if scan_running else "idle"))
        if scan_running and scan_operation_state == "finalizing":
            label = "Unreadable File Retry" if scan_operation_kind == "fast_recovery" else "Fast Scan"
            self.scan_label.setText(f"{label} — FINALIZING · all files parsed · saving/reconciling results")
            self.scan_progress.setRange(0, max(1, int(scan.get("total") or 1)))
            self.scan_progress.setValue(max(1, int(scan.get("total") or 1)))
            self.scan_progress.setFormat("100% · finalizing…")
        has_roots = bool(status_roots) and not unavailable_roots
        previous_scan_running = bool(self._last_scan_running)
        previous_scan_operation_kind = str(self._last_scan_operation_kind or "idle")
        if scan_running and not previous_scan_running:
            self._scan_terminal_notice = ""
        if (
            scan_running and not previous_scan_running and scan_operation_kind == "fast_recovery"
            and self._fast_recovery_start_failure_count is None
        ):
            self._fast_recovery_start_failure_count = int(fast_failure_count)
        if self._library_browser is not None:
            try:
                self._library_browser.set_source_state(status_roots, accessible_roots)
                self._library_browser.set_engine_work_state(
                    fast_scan_running=scan_running, deep_running=deep_state in {"running", "starting"}, scan=scan
                )
                if previous_scan_running and not scan_running:
                    self._library_browser.refresh_view()
            except RuntimeError:
                self._library_browser = None
        self.run_fast_button.setEnabled(has_roots and not scan_running)
        self.pause_scan_button.setEnabled(scan_running and scan_operation_state in {"running", "paused"})
        self.pause_scan_button.setText(
            "Resume" if scan_operation_state == "paused"
            else "Pausing…" if scan_operation_state == "pausing"
            else "Pause"
        )
        self.cancel_scan_button.setEnabled(
            scan_running and scan_operation_state in {"preparing", "running", "pausing", "paused"}
        )
        self.cancel_scan_button.setText("Cancelling…" if scan_operation_state == "cancelling" else "Cancel")

        complete = int(deep.get("complete") or 0)
        remaining = int(deep.get("remaining") or 0)
        active = int(deep.get("queue_active") or 0)
        retryable_failed = int(deep.get("failed_retryable") or 0)
        persistent_failed = int(deep.get("failed_persistent") or 0)
        if retryable_failed > 0 or persistent_failed > 0:
            self._saved_catalogue_summary(include_failure_groups=True)
        deep_total = int(deep.get("total") or (complete + remaining + deep_failed))
        self.deep_progress.setRange(0, max(1, deep_total))
        if remaining <= 0 and active <= 0 and deep_failed > 0:
            # The processing pass is finished. Do not leave a 99% bar implying
            # that the Engine is secretly still working on terminal exceptions.
            self.deep_progress.setValue(max(1, deep_total))
            if retryable_failed > 0:
                self.deep_progress.setFormat(
                    f"Done · {complete:,}"
                )
            else:
                self.deep_progress.setFormat(
                    f"Done · {complete:,}"
                )
        else:
            self.deep_progress.setValue(min(complete, max(1, deep_total)))
            self.deep_progress.setFormat("%p% · %v/%m")
        last = status.get("last_deep") or {}
        last_name = Path(str(last.get("path") or "")).name
        deep_rate = format_file_rate(deep.get("rate_per_second"), decimals=2)
        deep_eta = format_eta(deep.get("eta_seconds"))
        deep_workers = int(deep.get("queue_workers") or status.get("deep_workers") or 1)
        deep_active_limit = int(deep.get("queue_active_limit") or deep_workers)
        if deep_state == "attention":
            state_label = "COMPLETE · REVIEW ITEMS"
        elif deep_state == "failed" and retryable_failed > 0:
            state_label = "ATTENTION — RETRY AVAILABLE"
        else:
            state_label = deep_state.upper()
        if deep_state == "attention" and remaining <= 0 and active <= 0:
            deep_parts = [f"Deep: complete", f"{complete:,} analyzed", f"{deep_failed:,} review item(s)"]
        else:
            deep_parts = [f"Deep: {state_label.lower()}", f"{complete:,} complete"]
            if remaining:
                deep_parts.append(f"{remaining:,} waiting")
            if active:
                deep_parts.append(f"{active:,} active")
            if deep_rate:
                deep_parts.append(deep_rate)
            if deep_eta:
                deep_parts.append(f"ETA {deep_eta}")
            if retryable_failed:
                deep_parts.append(f"{retryable_failed:,} retry available")
            elif persistent_failed:
                deep_parts.append(f"{persistent_failed:,} review item(s)")
        deep_tooltip = [f"Workers: {deep_workers}"]
        if bool(scan.get("running")) and 0 < deep_active_limit < deep_workers:
            deep_tooltip.append(f"Fast foreground ownership currently limits Deep work to {deep_active_limit} job(s).")
        if last_name:
            deep_tooltip.append(str(last.get("path") or ""))
        self.deep_label.setToolTip("\n".join(deep_tooltip))
        self.deep_label.setText(" · ".join(deep_parts))
        deep_running = deep_state in {"running", "starting"}
        deep_transitioning = deep_state in {"pausing", "cancelling"}
        if deep_transitioning:
            transition_label = "Cancelling…" if deep_state == "cancelling" else "Pausing…"
            self.run_deep_button.setText(transition_label)
            self.run_deep_button.setEnabled(False)
            self.run_deep_button.setToolTip("Deep Analysis is reaching a persistent safe boundary. Completed work remains saved.")
        elif deep_running:
            self.run_deep_button.setText("Start / Resume")
            self.run_deep_button.setEnabled(False)
            self.run_deep_button.setToolTip("Deep Analysis is already running. Use Pause to stop current-session Deep work at a safe boundary.")
        elif remaining > 0:
            self.run_deep_button.setText("Start / Resume")
            self.run_deep_button.setEnabled(has_roots)
            self.run_deep_button.setToolTip(
                "Resume unfinished Deep Analysis." if has_roots else
                "Reconnect every configured MIDI source before resuming Deep Analysis. Saved analysis remains available while a source is offline."
            )
        elif retryable_failed > 0:
            self.run_deep_button.setText(f"Retry {retryable_failed:,} Failed…")
            self.run_deep_button.setEnabled(has_roots)
            self.run_deep_button.setToolTip(
                "Review and deliberately retry eligible first-attempt Deep failures." if has_roots else
                "Reconnect every configured MIDI source before retrying Deep Analysis failures."
            )
        elif persistent_failed > 0 or deep_failed > 0:
            review_count = persistent_failed or deep_failed
            self.run_deep_button.setText(f"Review ({review_count:,})…")
            self.run_deep_button.setEnabled(True)
            self.run_deep_button.setToolTip("Open Deep Analysis Review. Reviewing saved results remains available even when a source is offline.")
        else:
            self.run_deep_button.setText("Complete")
            self.run_deep_button.setEnabled(False)
            self.run_deep_button.setToolTip("No unfinished Deep Analysis work is waiting.")
        self.pause_analysis_button.setEnabled(deep_running)
        self.pause_analysis_button.setText("Pausing…" if deep_state == "pausing" else "Pause")
        self.cancel_analysis_button.setEnabled(deep_running)
        self.cancel_analysis_button.setText("Cancelling…" if deep_state == "cancelling" else "Cancel")

        built = int(relations.get("anchors_built") or 0)
        guided_families = int(relations.get("guided_ready_families") or 0)
        relation_count = int(relations.get("count") or 0)
        relationship_building = bool(relations.get("building"))
        relationship_suspended = bool(relations.get("suspended_for_fast") or relations.get("suspended_for_recovery"))
        relationship_suspend_reason = str(relations.get("suspend_reason") or "").strip().casefold()
        if relationship_suspended and not relationship_suspend_reason:
            relationship_suspend_reason = "unreadable_retry" if scan_operation_kind == "fast_recovery" else "fast_scan"
        # anchors_built is a historical persistent count and can legitimately
        # exceed today's current guided-ready family population after rescans or
        # identity changes. It is therefore NOT a progress numerator.
        if relationship_suspended:
            if relationship_suspend_reason == "unreadable_retry":
                priority_text = "Unreadable File Retry has priority"
            else:
                priority_text = "Fast Scan has priority"
            self.relationship_progress.setText(f"Paused — {priority_text}")
            prep_text = (
                f"paused while {priority_text} · {guided_families:,} families remain ready"
                if guided_families else f"paused while {priority_text}"
            )
        elif relationship_building:
            self.relationship_progress.setText("Building recommendation matches in the background…")
            prep_text = (
                f"working · {guided_families:,} families currently ready"
                if guided_families else "working · readiness population being established"
            )
        else:
            self.relationship_progress.setText(
                f"Ready · {guided_families:,} Guided-ready families" if guided_families else "Ready"
            )
            prep_text = f"ready · {guided_families:,} Guided-ready families"
        self.relationship_label.setText(
            f"Recommendation data · {guided_families:,} families · {relation_count:,} links"
        )
        self.relationship_label.setToolTip(
            f"{guided_families:,} song families ready · {relation_count:,} prepared match links · {prep_text}"
        )
        self.relationship_label.setToolTip(
            f"Prepared recommendation match links: {relation_count:,}. Historical family anchors stored: {built:,}."
        )

        if first_run_empty:
            self.deep_label.setText("Deep: not started · waiting for the first Fast Scan")
            self.deep_progress.setRange(0, 1)
            self.deep_progress.setValue(0)
            self.deep_progress.setFormat("Waiting")
            self.run_deep_button.setText("Waiting for Fast Scan")
            self.run_deep_button.setEnabled(False)
            self.pause_analysis_button.setEnabled(False)
            self.cancel_analysis_button.setEnabled(False)
            self.relationship_label.setText("No saved recommendation readiness yet")
            self.relationship_progress.setText("Waiting for catalogued MIDI")

        # The Engine intentionally publishes an immediately responsive status
        # snapshot before slower aggregate SQLite counts are ready. Never render
        # those bootstrap zero placeholders as authoritative saved-library data.
        # A fast 96K inventory can advance thousands of paths before the first
        # aggregate snapshot completes, which previously made a healthy existing
        # Library look erased for a short interval on Windows.
        if not status_metrics_ready:
            if quick_database:
                self.storage_status.setText("Saved catalogue: loading…")
                self.storage_status.setToolTip(quick_database)
            self.fast_failures_button.setText("Unreadable (loading…)")
            self.suspicious_button.setText("Unusual (loading…)")
            self._statistics_values.update({
                "catalogued": 0, "distinct": 0, "health_clear": 0, "suspicious": 0,
                "fast_failed": 0, "deep_complete": 0, "deep_remaining": 0,
                "failed": 0, "guided": 0, "relationships": 0,
                "discovered": 0, "usable": 0, "mash_ready": 0, "fusion_candidate": 0,
                "usable_not_mash": 0, "needs_review": 0, "rejected": 0, "quarantined": 0, "unresolved": 0,
            })
            self.deep_label.setText("Deep: loading saved analysis…")
            self.deep_progress.setRange(0, 0)
            self.deep_progress.setFormat("Loading…")
            self.run_deep_button.setText("Loading…")
            self.run_deep_button.setEnabled(False)
            self.pause_analysis_button.setEnabled(False)
            self.cancel_analysis_button.setEnabled(False)
            self.relationship_label.setText("Loading saved recommendation readiness…")
            if relationship_suspended:
                priority_text = (
                    "Unreadable File Retry has priority"
                    if relationship_suspend_reason == "unreadable_retry"
                    else "Fast Scan has priority"
                )
                self.relationship_progress.setText(f"Paused — {priority_text}")
            else:
                self.relationship_progress.setText("Loading saved readiness…")

        errors = status.get("recent_errors") or []
        startup_error = str(startup.get("error") or "").strip()
        if deep_state in {"failed", "attention"} and deep_failed > 0:
            retryable_failed = int(deep.get("failed_retryable") or 0)
            persistent_failed = int(deep.get("failed_persistent") or 0)
            if retryable_failed > 0:
                self.status_detail.setText(
                    f"Deep Analysis needs attention: {retryable_failed:,} first-failure item(s) may be retried once; "
                    f"{persistent_failed:,} Deep Analysis review item(s) require attention."
                )
            else:
                self.status_detail.setText(
                    f"Deep Analysis is complete with {deep_failed:,} review item(s). "
                    "Review them in Browse Library; bulk retry is finished."
                )
        elif startup_error:
            self.status_detail.setText(f"Engine startup is degraded: {startup_error}")
        elif errors:
            self.status_detail.setText("Recent engine note: " + str(errors[-1]))
        elif self._long_action_inflight:
            return
        else:
            throughput = str(status.get("throughput_mode") or "maximum").replace("_", " ").title()
            workers = int(status.get("deep_workers") or 1)
            active_worker_processes = int(scan.get("worker_processes") or 0)
            if first_run_empty:
                self.status_detail.setText(
                    "First run: choose Add Source Folder… to build a new Library, or Restore Library Data… if you already have a Library backup. "
                    "A new Library runs Fast Scan first; restored source paths remain protected if they are not accessible on this PC."
                )
            elif bool(scan.get("running")):
                readiness_note = (
                    f"{catalogued_count:,} committed Fast-catalogue MIDI(s) are already saved and usable while this scan continues. "
                    if catalogued_count else "Fast catalogue is building its first committed batch. "
                )
                operation_name = "Unreadable File Retry" if scan_operation_kind == "fast_recovery" else "Fast Scan"
                budget = int(scan.get("worker_limit") or workers)
                self.status_detail.setText(
                    readiness_note + f"{operation_name} owns up to {budget} analyzer worker(s); "
                    f"{active_worker_processes} worker process(es) are active now. "
                    "Completed batches are persistent; cancel or restart will not throw away finished work."
                )
            else:
                self.status_detail.setText(
                    f"{throughput} · {workers} Deep Analysis worker(s). Closing this window offers a clean full exit by default; "
                    "background Engine continuation remains available only when you explicitly choose it."
                )

        self._show_scan_terminal_guidance(
            previous_kind=previous_scan_operation_kind,
            previous_running=previous_scan_running,
            scan_running=scan_running,
            fast_failure_count=fast_failure_count,
        )
        if not scan_running and self._scan_terminal_notice and not self._long_action_inflight:
            # Completion/cancel guidance is an operator decision point, not a
            # transient telemetry message. Keep it visible across subsequent
            # one-second status polls until another Fast operation begins.
            self.status_detail.setText(self._scan_terminal_notice)
        self._last_scan_running = scan_running
        self._last_scan_operation_kind = scan_operation_kind if scan_running else "idle"

        active_worker_required = min(2, scan_worker_limit)
        active_worker_boundary = bool(
            scan_worker_processes >= active_worker_required
            and (scan_running or deep_state in {"running", "starting", "pausing", "cancelling"})
        )
        self._maybe_trigger_qa_close_on_state(
            f"fast:{scan_operation_state.casefold()}",
            f"{scan_operation_kind.casefold()}:{scan_operation_state.casefold()}",
            f"deep:{deep_state}",
            "active:workers" if active_worker_boundary else "",
            "fast_recovery:workers" if active_worker_boundary and scan_operation_kind == "fast_recovery" else "",
            "maintenance" if maintenance else "",
            f"maintenance:{maintenance.casefold()}" if maintenance else "",
            "engine:ready",
        )
        self._maybe_start_qa_maintenance()

        # Target-Windows lifecycle QA hook: close the real Manager only after a
        # requested Fast operation is demonstrably active. This lets the RC
        # self-check prove shutdown from inside normal Fast Scan or unreadable
        # recovery instead of relying on a fragile wall-clock autoclose guess.
        qa_close_kind = str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_ON_ACTIVE_SCAN", "")).strip().casefold()
        if (
            qa_close_kind
            and not self._qa_close_on_active_scan_triggered
            and scan_running
            and qa_close_kind in {"any", scan_operation_kind.casefold()}
        ):
            self._qa_close_on_active_scan_triggered = True
            QTimer.singleShot(0, self.close)


    def _show_scan_terminal_guidance(
        self, *, previous_kind: str, previous_running: bool, scan_running: bool, fast_failure_count: int
    ) -> None:
        if not previous_running or scan_running:
            return
        message = ""
        if self._scan_cancel_requested:
            if previous_kind == "fast_recovery":
                message = (
                    f"Unreadable-file retry canceled. {max(0, int(fast_failure_count)):,} file(s) are currently still unreadable. "
                    "Completed retry results remain saved. You can continue reviewing files or close the Manager normally."
                )
                self._fast_recovery_start_failure_count = None
            else:
                message = (
                    "Fast Scan canceled. No Fast Scan is running now, and completed batches remain saved. "
                    "You can continue using the Library or close the Manager normally."
                )
        elif previous_kind == "fast_recovery":
            before = self._fast_recovery_start_failure_count
            after = max(0, int(fast_failure_count))
            recovered = max(0, int(before or 0) - after) if before is not None else 0
            if before is not None and recovered > 0:
                message = (
                    f"Unreadable-file retry finished: {recovered:,} file(s) are now readable; {after:,} remain unreadable. "
                    "Those files return to normal Library use. For files still unreadable, repeated retry without changing the file or scanner is unlikely to help. "
                    "Use Quarantine to preserve them outside the active Library, or Delete only if you intentionally want the source files removed from disk."
                )
            else:
                message = (
                    f"Unreadable-file retry finished: {after:,} file(s) are still unreadable. "
                    "If neither the source files nor the scanner changed, another retry is unlikely to help. "
                    "Use Show in Folder to inspect/replace a file, Quarantine to preserve it outside the active Library, or Delete only if you intentionally want it removed from disk."
                )
            self._fast_recovery_start_failure_count = None
        self._scan_cancel_requested = False
        if not message:
            return
        self._scan_terminal_notice = message
        self.status_detail.setText(message)
        browser = self._library_browser
        if browser is not None:
            try:
                if browser.isVisible():
                    browser.show_scan_completion(message)
            except RuntimeError:
                self._library_browser = None

    def closeEvent(self, event: QCloseEvent) -> None:
        """Close Manager only after its owned UI/audio resources are terminal.

        The detached Library Engine has an explicit lifetime choice.  Browser
        audition, however, is always Manager-owned and must be proven closed
        before either "keep Engine" or full-exit is allowed to complete.
        """
        if self._close_in_progress:
            event.accept()
            return

        if self._engine_stop_started_monotonic > 0.0:
            self.status_detail.setText(
                "Library shutdown is still in progress. Please leave this window open until process cleanup is verified; "
                "a full exit will close automatically when safe."
            )
            event.ignore()
            return

        if self._long_action_inflight:
            self._write_qa_maintenance_event("close-deferred", self._long_action_inflight)
            # A close request during a persistent maintenance transition is an
            # owned deferred-exit request, not a discarded click.  The current
            # operation must reach its safe terminal boundary before shutdown,
            # but the Manager remembers the request and resumes Close
            # automatically as soon as that boundary is reported.
            self._exit_after_maintenance = True
            self.status_detail.setText(
                "Exit requested. Finishing the current Library maintenance boundary safely; "
                "the close sequence will continue automatically when it completes…"
            )
            if not str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_DURING_MAINTENANCE", "")).strip():
                QMessageBox.information(
                    self, "Library Maintenance In Progress",
                    "Exit has been requested. MIDI Library Manager will remain responsive while the current backup, restore, rebuild, or retry operation reaches its terminal persistent boundary. "
                    "The close sequence will continue automatically afterward so the multi-database transition is not abandoned halfway through.",
                )
            event.ignore()
            return

        # Engine launch is itself an owned lifecycle state.  Before an endpoint
        # exists, old builds could accept Manager close while the daemon startup
        # thread was still capable of spawning a detached Engine afterwards.
        # Full exit now cancels startup first and waits asynchronously for that
        # exact startup worker to prove no Engine process remains.
        if self._engine_connecting and not self._engine_ready:
            qa_exit_mode = str(os.environ.get("MIDI_LIBRARY_QA_EXIT_MODE", "")).strip().casefold()
            force_full_exit = qa_exit_mode in {"stop", "stop-engine", "full", "full-exit"}
            stop_startup = force_full_exit
            if not force_full_exit:
                stop_startup = QMessageBox.question(
                    self,
                    "Library Engine Is Starting",
                    "The Library Engine is still starting. Stop Engine startup and exit MIDI Library Manager?\n\n"
                    "The window will remain responsive while startup cancellation verifies that no detached Library process survives.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                ) == QMessageBox.StandardButton.Yes
            if not stop_startup:
                event.ignore()
                return
            self._engine_stopped_by_user = True
            self._engine_ready = False
            self._exit_after_engine_stop = True
            self._engine_connect_cancel.set()
            self._set_engine_controls_enabled(False)
            self._begin_engine_stop_feedback(full_exit=True)
            self._begin_manager_busy("engine_stop")
            self.status_detail.setText(
                "Canceling Library Engine startup and verifying that no detached Engine/analyzer process remains…"
            )
            event.ignore()
            return

        # Close Browser audition first. QWidget.close() returns False when the
        # Browser deliberately ignores close because FluidSynth cleanup could
        # not be verified; in that case the Manager must remain open as well.
        browser = self._library_browser
        if browser is not None:
            try:
                browser_closed = bool(browser.close())
            except (RuntimeError, OSError) as exc:
                browser_closed = False
                browser_detail = f"{type(exc).__name__}: {exc}"
            else:
                browser_detail = ""
            if not browser_closed:
                if bool(getattr(browser, "close_in_progress", False)):
                    self._close_after_browser = True
                    self.status_detail.setText(
                        "Closing Library Browser audio and background reads before continuing Manager exit…"
                    )
                    self._begin_manager_busy("browser_close")
                    event.ignore()
                    return
                QMessageBox.warning(
                    self,
                    "Library Browser Still Active",
                    "MIDI Library Manager will remain open because Library Browser audio cleanup "
                    "was not verified. Stop Browser playback and close it again before exiting."
                    + (f"\n\n{browser_detail}" if browser_detail else ""),
                )
                event.ignore()
                return

        qa_autoclose = bool(str(os.environ.get("MIDI_REMIXER_QA_AUTOCLOSE_MS", "")).strip())
        qa_exit_mode = str(os.environ.get("MIDI_LIBRARY_QA_EXIT_MODE", "")).strip().casefold()
        force_full_exit = qa_exit_mode in {"stop", "stop-engine", "full", "full-exit"}
        force_keep_engine = qa_exit_mode in {"keep", "keep-engine", "manager-only"}
        # Do not perform localhost IPC synchronously inside closeEvent. Even a
        # 350 ms heartbeat delay is visible as a dead close button on Windows
        # under CPU-heavy scanning. Cached Engine state plus the tiny lifetime
        # files are enough to decide whether to offer the lifetime choice; the
        # asynchronous shutdown worker performs authoritative verification.  The
        # startup lock matters because an Engine can be alive before/without an
        # endpoint if service initialization fails.
        engine_lifetime_present = False
        if not (force_keep_engine or (qa_autoclose and not force_full_exit)):
            try:
                engine_lifetime_present = bool(
                    self._engine_ready
                    or self.client.endpoint_file.exists()
                    or self.client.start_lock_file.exists()
                )
            except OSError:
                engine_lifetime_present = bool(self._engine_ready)

        if engine_lifetime_present and not self._engine_stopped_by_user:
            stop_engine = force_full_exit
            if not force_full_exit:
                dialog = QMessageBox(self)
                dialog.setWindowTitle("Close MIDI Library Manager")
                dialog.setIcon(QMessageBox.Icon.Question)
                dialog.setText("The Library Engine is still running in the background.")
                latest = dict(self._latest_engine_status or {})
                scan_now = latest.get("scan") or {}
                deep_now = latest.get("deep") or {}
                relations_now = latest.get("relationships") or {}
                active_background_work = bool(
                    scan_now.get("running")
                    or str(deep_now.get("state") or "").casefold() in {"running", "starting", "pausing", "cancelling"}
                    or relations_now.get("building")
                    or str(latest.get("maintenance") or "").strip()
                )
                dialog.setInformativeText(
                    "Close only this Manager window and keep the independent Engine working, or perform a full exit. "
                    "Full exit does not complete until the Engine and every Library-owned analyzer Python process are verified stopped."
                    + ("\n\nLibrary work is active. Choose the keep-running option only when you intentionally want background Python Engine/analyzer processes to remain after this window closes." if active_background_work else "\n\nNo active scan/analysis is reported. Full exit is the normal clean shutdown.")
                )
                keep_button = dialog.addButton("Close Window Only · Keep Background Engine Running", QMessageBox.ButtonRole.AcceptRole)
                stop_button = dialog.addButton("Stop Engine and Exit", QMessageBox.ButtonRole.DestructiveRole)
                cancel_button = dialog.addButton(QMessageBox.StandardButton.Cancel)
                # Full exit is deliberately the default even during active work.
                # The user previously observed many Python processes after closing
                # the app because keep-Engine was the default while analysis was
                # active. Background continuation remains available, but it must
                # now be an explicit opt-in rather than the accidental close path.
                dialog.setDefaultButton(stop_button)
                dialog.exec()
                clicked = dialog.clickedButton()
                if clicked is cancel_button or clicked is None:
                    event.ignore()
                    return
                stop_engine = clicked is stop_button

            if stop_engine:
                # Never block the Qt message loop for a process-tree shutdown.
                # On Windows an active 4-8 worker pool can legitimately need a
                # moment to cancel analysis and reap descendants; doing that in
                # closeEvent made the Manager appear hung and encouraged forced
                # termination, which is exactly how orphan processes become hard
                # to distinguish from the intentional detached Engine.
                self._engine_stopped_by_user = True
                self._engine_ready = False
                self._set_engine_controls_enabled(False)
                self._exit_after_engine_stop = True
                self._begin_engine_stop_feedback(full_exit=True)
                self._begin_manager_busy("engine_stop")

                def stop_for_exit() -> None:
                    try:
                        if str(os.environ.get("MIDI_LIBRARY_QA_HANG_SHUTDOWN_WORKER", "")).strip():
                            # Windows RC gate: prove the Qt-level watchdog can
                            # finish full exit even if this control worker itself
                            # never returns from its graceful path.
                            time.sleep(CLIENT_SHUTDOWN_WAIT_SECONDS * 4.0)
                        stopped = self.client.shutdown(request_timeout=SHUTDOWN_REQUEST_SECONDS, wait_timeout=CLIENT_SHUTDOWN_WAIT_SECONDS)
                        detail = str(self.client.last_shutdown_detail or "")
                        if not stopped and not detail:
                            detail = "The Engine process tree did not become terminal within the shutdown window."
                    except (LibraryEngineUnavailable, OSError, RuntimeError, ValueError) as exc:
                        stopped = False
                        detail = f"{type(exc).__name__}: {exc}"
                    self.engineStopFinished.emit(stopped, detail)

                threading.Thread(
                    target=stop_for_exit, name="MidiLibraryManagerFullExit", daemon=True
                ).start()
                event.ignore()
                return

        self._close_in_progress = True
        self._poll_timer.stop()
        event.accept()



def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("MIDI Library Manager")
    app.setOrganizationName("MIDI Remixer")
    app.setStyleSheet(load_stylesheet("base.qss"))
    window = MidiLibraryManagerWindow()
    window.show()
    try:
        qa_autoclose_ms = max(0, int(os.environ.get("MIDI_REMIXER_QA_AUTOCLOSE_MS", "0") or 0))
    except ValueError:
        qa_autoclose_ms = 0
    if qa_autoclose_ms:
        QTimer.singleShot(qa_autoclose_ms, window.close)
    exit_code = int(app.exec())

    # QApplication has ended, so no Manager-owned background operation should
    # still require interpreter lifetime. Join unexpected non-daemon Python
    # threads for one short shared budget; if one survives, force this Manager
    # process itself out rather than recreating a Task Manager-only close. The
    # detached Library Engine is a separate process and is unaffected here.
    deadline = time.monotonic() + 1.5
    current = threading.current_thread()
    survivors: list[threading.Thread] = []
    for thread in list(threading.enumerate()):
        if thread is current or thread.daemon or not thread.is_alive():
            continue
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            survivors.append(thread)
    if survivors:
        try:
            diagnostic = window.paths.logs / "library-manager-exit-thread-warning.json"
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(json.dumps({
                "threads": [thread.name for thread in survivors],
                "time": time.time(),
                "full_exit_verified": bool(getattr(window, "_full_exit_verified", False)),
            }, indent=2), encoding="utf-8")
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        os._exit(exit_code)
    return exit_code
