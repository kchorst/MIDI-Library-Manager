from __future__ import annotations

from pathlib import Path
import os
import threading
import time
from typing import Any

from PySide6.QtCore import QEvent, QItemSelectionModel, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QProgressBar,
    QSlider,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.app_settings import AppSettings
from core.dependency_setup import find_fluidsynth, find_soundfont
from core.playback_engine import PlaybackSnapshot, PlaybackState
from library_manager.audition import LibraryAuditionCancelled, LibraryAuditionController, LibraryAuditionError
from library_manager.catalogue_reader import CataloguePage, CatalogueReader
from library_manager.browser_policy import (
    plan_browser_geometry,
    saved_row_can_attempt_audition,
    saved_row_can_attempt_shell_action,
    source_roots_ready,
)
from library_manager.shell_actions import open_midi_file, show_file_in_folder
from library_engine.timing import BROWSER_CLOSE_HARD_SECONDS


ROLE_PATH = int(Qt.ItemDataRole.UserRole.value)
ROLE_QUICK_STATUS = ROLE_PATH + 1
ROLE_DEEP_STATUS = ROLE_PATH + 2
ROLE_DEEP_ATTEMPTS = ROLE_PATH + 3
ROLE_SUSPICIOUS = ROLE_PATH + 4
ROLE_DEEP_RETRY_ALLOWED = ROLE_PATH + 5
ROLE_LIFECYCLE = ROLE_PATH + 6
ROLE_ELIGIBILITY = ROLE_PATH + 7
ROLE_ORIGINAL_PATH = ROLE_PATH + 8
ROLE_REJECTION_CODE = ROLE_PATH + 9
ROLE_RECOMMENDED_ACTION = ROLE_PATH + 10


class LibraryBrowserDialog(QDialog):
    """Paged catalogue browser with lifecycle-owned audition and failed-file cleanup.

    SQLite is opened query-only so the detached Engine remains the sole writer.
    The browser can therefore remain useful while scanning continues or even
    while the Engine is stopped. File removal is not performed here; confirmed
    requests are emitted to the Manager/Engine maintenance path.
    """

    pageLoaded = Signal(int, object, str)
    summaryLoaded = Signal(int, object, str)
    transportFinished = Signal(int, str, object, str)
    closeCleanupFinished = Signal(bool, str)
    shellActionFinished = Signal(str, str, str)  # action, path, error
    removeRequested = Signal(list, bool)  # paths, permanent
    restoreRequested = Signal(list)  # quarantined paths to restore
    retryRequested = Signal(list)  # explicit deep-analysis retry after a fix/change
    rescanRequested = Signal(list)  # force Fast parse for selected unchanged/failed/suspicious files
    rescanAllFastFailedRequested = Signal()  # force Fast parse for all persisted Fast failures
    reanalyzeRequested = Signal(list)  # deliberately rebuild derived Deep Analysis
    keepNotMashRequested = Signal(list)  # human-reviewed valid MIDI retained outside Remixer
    pauseFastScanRequested = Signal()
    cancelFastScanRequested = Signal()

    COLUMNS = (
        ("Title", "title"),
        ("Artist", "artist"),
        ("Filename", "filename"),
        ("Eligibility", "eligibility"),
        ("Status", "display_status"),
        ("Rejection / Review Reason", "rejection_reason"),
        ("Last Validation", "updated"),
        ("Recommended Action", "recommended_action"),
        ("File Size", "file_size"),
        ("Duration", "duration"),
        ("BPM", "bpm"),
        ("Key", "key"),
        ("Meter", "meter"),
        ("Tracks", "tracks"),
        ("Notes", "notes"),
        ("Location", "path"),
    )

    def __init__(self, database_path: str | Path, app_root: str | Path, parent=None) -> None:
        super().__init__(parent)
        self.reader = CatalogueReader(database_path)
        self.app_root = Path(app_root).resolve()
        self.audition = LibraryAuditionController(self.app_root)
        self.setWindowTitle("MIDI Library")
        # Keep catalogue inspection inside the *available* Windows workspace.
        # A fixed 1180x760 client area can extend below a 1920x1080 display at
        # 150% scaling (logical workspace ~1280x720), hiding the footer. Size
        # against Qt's availableGeometry so the first real window is fully
        # reachable without requiring the user to resize it.
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            plan = plan_browser_geometry(available.width(), available.height())
        else:
            plan = plan_browser_geometry(1100, 650)
        self.resize(plan.width, plan.height)
        self.setMinimumSize(plan.minimum_width, plan.minimum_height)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # Defense in depth for this top-level dialog: human-critical selection
        # and transport states remain visible even if parent/global styling
        # changes later.
        self.setStyleSheet("""
            QTableWidget::item:selected { background:#9fc9ef; color:#17202a; }
            QPushButton#PlayButton { background:#2f8f4e; color:#ffffff; border:1px solid #267640; font-weight:700; }
            QPushButton#PlayButton[transportState="pause"] { background:#f4a340; color:#17202a; border:1px solid #c57b13; }
            QPushButton#PlayButton:disabled { background:#dbe2df; color:#7c8781; border:1px solid #c4cec8; }
            QPushButton#StopButton[transportState="active"] { background:#b93636; color:#ffffff; border:1px solid #9f2c2c; font-weight:700; }
        """)
        self._offset = 0
        self._limit = 120
        self._total_matches = 0
        self._sort_key = "title"
        self._descending = False
        self._request_generation = 0
        self._request_inflight = False
        self._queued_refresh = False
        self._closing = False
        self._close_cleanup_inflight = False
        self._close_cleanup_complete = False
        self._query_threads: set[threading.Thread] = set()
        self._query_lock = threading.Lock()
        self._summary_generation = 0
        self._summary_inflight = False
        self._transport_generation = 0
        self._pending_remove: tuple[list[str], bool] | None = None
        self._transport_threads: set[threading.Thread] = set()
        self._transport_actions: set[str] = set()
        self._transport_lock = threading.Lock()
        self._shell_threads: set[threading.Thread] = set()
        self._shell_lock = threading.Lock()
        self._paused_tick = 0
        self._fast_scan_running = False
        self._deep_running = False
        self._quick_failed_count = 0
        self._configured_source_roots: tuple[str, ...] = ()
        self._accessible_source_roots: tuple[str, ...] = ()
        self._all_sources_ready = False
        self._busy_reasons: dict[str, int] = {}
        self._busy_message = ""
        self._maintenance_busy = False
        self._qa_manager_close_triggered = False
        self._qa_playback_mode = str(os.environ.get("MIDI_LIBRARY_QA_PLAYBACK_CLOSE_STATE", "")).strip().casefold()
        self._qa_playback_started = False
        self._qa_playback_pause_requested = False
        self._qa_playback_close_requested = False
        self._context_menu_active = False
        self._last_context_menu_at = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        heading_row = QHBoxLayout()
        title = QLabel("MIDI Library")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        heading_row.addWidget(title)
        heading_row.addStretch(1)
        layout.addLayout(heading_row)

        self.summary_label = QLabel("Reading saved catalogue…")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color: #34495e; font-weight: 600;")
        layout.addWidget(self.summary_label)

        explanation = QLabel(
            "Curated Library. Only Blessed · Mash Ready files enter Guided/Assisted; review, rejection, quarantine, and cleanup remain explicit."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: #66717d;")
        layout.addWidget(explanation)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(QLabel("Search:"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Title, artist, filename, or folder…")
        self.search.textChanged.connect(self._queue_first_page)
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(QLabel("Filter:"))
        self.status_filter = QComboBox()
        self.status_filter.addItem("All", "all")
        self.status_filter.addItem("Mash Ready", "mash_ready")
        self.status_filter.addItem("Fusion Candidate", "fusion_candidate")
        self.status_filter.addItem("Not Mash Candidate", "usable_not_mash")
        self.status_filter.addItem("Needs Review", "needs_review")
        self.status_filter.addItem("Rejected", "rejected")
        self.status_filter.addItem("Quarantined", "quarantined")
        self.status_filter.addItem("Unresolved", "unresolved")
        self.status_filter.addItem("Deep Complete", "complete")
        self.status_filter.addItem("Deep Unfinished", "unfinished")
        self.status_filter.addItem("Deep Pending", "pending")
        self.status_filter.addItem("Deep Paused", "paused")
        self.status_filter.addItem("Deep Analyzing Now", "analyzing")
        self.status_filter.addItem("All Deep Analysis Issues", "failed")
        self.status_filter.addItem("Deep Retry Available", "failed_retryable")
        self.status_filter.addItem("Deep Analysis Review", "failed_persistent")
        self.status_filter.addItem("Unreadable Files", "quick_failed")
        self.status_filter.addItem("Unusual but Loadable", "suspicious_loadable")
        self.status_filter.addItem("All File Review Items", "health_review")
        self.status_filter.currentIndexChanged.connect(self._first_page)
        filter_row.addWidget(self.status_filter)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh)
        filter_row.addWidget(self.refresh_button)
        layout.addLayout(filter_row)

        self.review_guidance = QLabel("")
        self.review_guidance.setWordWrap(True)
        self.review_guidance.setMaximumHeight(58)
        self.review_guidance.setVisible(False)
        self.review_guidance.setStyleSheet(
            "background: #fffdf2; border: 1px solid #d9c67d; border-radius: 5px; padding: 6px; color: #4e4523;"
        )
        layout.addWidget(self.review_guidance)
        self.status_filter.currentIndexChanged.connect(self._update_review_guidance)
        self._update_review_guidance()

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([name for name, _key in self.COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        # REG-120: QAbstractScrollArea context menus are delivered to the
        # viewport. Do not suppress context menus at the outer QTableWidget; on
        # real Windows that can prevent the viewport signal from ever becoming a
        # usable menu. Bind both the standard custom-context signal and an event
        # filter fallback so an actual right-click event, not merely a direct
        # method call in QA, is qualified.
        self.table.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.viewport().customContextMenuRequested.connect(self._show_table_context_menu)
        self.table.viewport().installEventFilter(self)
        self.table.setToolTip("Select a MIDI row. Right-click the row for all available actions.")
        # REG-117/118/120: the catalogue is the product, but the footer must also
        # remain reachable on 720-logical-pixel workspaces. Protect a true
        # multi-row catalogue and let the table absorb/release the remaining
        # vertical space instead of forcing the dialog below the screen.
        self.table.setMinimumHeight(240)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._sort_by_column)
        # ResizeToContents repeatedly measures every visible cell and can stall
        # Qt for seconds while a hot Engine is updating the same 96K catalogue.
        # Use stable interactive widths; the user can still resize columns and
        # horizontally scroll to Folder/Failure details.
        for column in range(len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        # Keep the health-review information visible before the user ever has
        # to horizontally scroll. Musical metadata remains available to the
        # right for normal catalogue inspection.
        widths = (155, 85, 140, 120, 105, 190, 125, 250, 82, 68, 58, 72, 56, 56, 62, 220)
        for column, width in enumerate(widths):
            self.table.setColumnWidth(column, width)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        layout.addWidget(self.table, 20)

        transport = QHBoxLayout()
        transport.setSpacing(6)
        transport.addWidget(QLabel("Audition selected MIDI:"))
        # Product-wide transport contract: exactly two audio buttons on every
        # MIDI-list view.  Play toggles to Pause while audio is running; when
        # paused the same button becomes Play again and resumes the highlighted
        # row (or starts a newly highlighted row from the beginning).
        self.play_pause_button = QPushButton("Play")
        self.play_pause_button.setObjectName("PlayButton")
        self.play_pause_button.clicked.connect(self._toggle_play_pause)
        transport.addWidget(self.play_pause_button)
        # Backward-compatible internal alias only; there is still one visible
        # Play/Pause widget.
        self.play_button = self.play_pause_button
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("StopButton")
        self.stop_button.clicked.connect(self._stop)
        transport.addWidget(self.stop_button)
        self.playback_status = QLabel("Stopped")
        self.playback_status.setStyleSheet("color: #66717d;")
        transport.addWidget(self.playback_status)
        transport.addStretch(1)
        transport.addWidget(QLabel("Volume:"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 150)
        self.volume_slider.setSingleStep(5)
        self.volume_slider.setPageStep(10)
        self.volume_slider.setFixedWidth(150)
        self.volume_slider.setToolTip("Audition volume. Changes apply immediately and are remembered.")
        self.volume_slider.setValue(self.audition.volume_percent)
        self.volume_slider.valueChanged.connect(self._set_volume)
        transport.addWidget(self.volume_slider)
        self.volume_label = QLabel(f"{self.volume_slider.value()}%")
        self.volume_label.setMinimumWidth(42)
        self.volume_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        transport.addWidget(self.volume_label)
        self.audio_setup_button = QPushButton("Audio Setup…")
        self.audio_setup_button.setToolTip(
            "Configure FluidSynth and a SoundFont for MIDI Library audition. MIDI Library can be installed and audition MIDI independently of MIDI Remixer."
        )
        self.audio_setup_button.clicked.connect(self._configure_audio)
        transport.addWidget(self.audio_setup_button)
        layout.addLayout(transport)

        # Catalogue-first action surface. Selected-row commands live behind a
        # single visible Actions… menu and the identical right-click menu. The
        # underlying proxy buttons retain one authoritative enabled-state source
        # for keyboard/menu/legacy contract tests without consuming catalogue
        # height. Only the global unreadable retry remains directly visible.
        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        self.open_file_button = QPushButton("Open File")
        self.open_file_button.setToolTip("Open the selected source MIDI in your Windows default MIDI application. This leaves MIDI Library; MIDI Library does not repair the file.")
        self.open_file_button.clicked.connect(self._open_selected_file)
        self.show_in_folder_button = QPushButton("Show in Folder")
        self.show_in_folder_button.setToolTip("Open Windows Explorer with the exact selected MIDI file highlighted.")
        self.show_in_folder_button.clicked.connect(self._show_selected_in_folder)
        self.keep_not_mash_button = QPushButton("Keep · Not Mash…")
        self.keep_not_mash_button.setToolTip(
            "Resolve selected readable Needs Review MIDI files as Library-usable but excluded from Guided/Assisted mash recommendations. "
            "Use this for valid files you intentionally want to keep when current Deep Analysis cannot fully profile it."
        )
        self.keep_not_mash_button.clicked.connect(self._request_keep_not_mash)
        self.rescan_selected_button = QPushButton("Re-scan Selected…")
        self.rescan_selected_button.setToolTip(
            "Force a fresh Fast Scan of the selected source MIDI file(s), even if unchanged. This re-runs scanning; it does not repair the source file."
        )
        self.rescan_selected_button.clicked.connect(self._request_rescan_selected)
        self.reanalyze_button = QPushButton("Rebuild Analysis…")
        self.reanalyze_button.setToolTip(
            "For successfully analyzed/nonfailed MIDI files, discard only the selected derived Deep Analysis/readiness data and rebuild it. Source MIDI and Fast Scan data are preserved."
        )
        self.reanalyze_button.clicked.connect(self._request_reanalyze_selected)
        self.retry_deep_button = QPushButton("Retry Analysis…")
        self.retry_deep_button.setToolTip(
            "Requeue a selected Deep Analysis failure. This retries analysis; it does not repair the source MIDI. Use it only after a relevant file, analyzer, or safety-limit change."
        )
        self.retry_deep_button.clicked.connect(self._request_retry_deep)
        for proxy in (
            self.open_file_button, self.show_in_folder_button, self.keep_not_mash_button,
            self.rescan_selected_button, self.reanalyze_button, self.retry_deep_button,
        ):
            proxy.setVisible(False)

        self.actions_button = QPushButton("Actions…")
        self.actions_button.setToolTip("All actions for the selected MIDI. The same menu is available by right-clicking a row.")
        self.actions_button.clicked.connect(self._show_selected_actions_menu)
        action_row.addWidget(self.actions_button)

        self.rescan_all_failed_button = QPushButton("Retry Unreadable…")
        self.rescan_all_failed_button.setToolTip(
            "Force a fresh Fast Scan of every file currently marked unreadable. Repeating this without a file or Library scanner change is unlikely to help."
        )
        self.rescan_all_failed_button.clicked.connect(self._request_rescan_all_failed)
        action_row.addWidget(self.rescan_all_failed_button)

        self.selection_guidance = QLabel("Use Actions… or right-click the selected MIDI row.")
        self.selection_guidance.setWordWrap(False)
        self.selection_guidance.setStyleSheet("color: #66717d; font-size: 11px;")
        action_row.addWidget(self.selection_guidance, 1)
        layout.addLayout(action_row)

        scan_row = QHBoxLayout()
        scan_row.setSpacing(6)
        self.scan_activity_label = QLabel("Fast Scan — IDLE")
        self.scan_activity_label.setStyleSheet("color: #66717d;")
        scan_row.addWidget(self.scan_activity_label)
        self.scan_activity_progress = QProgressBar()
        self.scan_activity_progress.setMinimumWidth(260)
        self.scan_activity_progress.setMaximumHeight(18)
        self.scan_activity_progress.setRange(0, 1)
        self.scan_activity_progress.setValue(0)
        self.scan_activity_progress.setFormat("Idle")
        scan_row.addWidget(self.scan_activity_progress, 1)
        self.pause_fast_scan_button = QPushButton("Pause Fast Scan")
        self.pause_fast_scan_button.clicked.connect(self.pauseFastScanRequested.emit)
        self.pause_fast_scan_button.setEnabled(False)
        scan_row.addWidget(self.pause_fast_scan_button)
        self.cancel_fast_scan_button = QPushButton("Cancel Fast Scan")
        self.cancel_fast_scan_button.setObjectName("DangerButton")
        self.cancel_fast_scan_button.clicked.connect(self.cancelFastScanRequested.emit)
        self.cancel_fast_scan_button.setEnabled(False)
        scan_row.addWidget(self.cancel_fast_scan_button)
        layout.addLayout(scan_row)

        self.scan_guidance = QLabel("")
        self.scan_guidance.setWordWrap(True)
        self.scan_guidance.setMaximumHeight(58)
        self.scan_guidance.setVisible(False)
        self.scan_guidance.setStyleSheet(
            "background: #eef6ff; border: 1px solid #a9c7e8; border-radius: 5px; padding: 6px; color: #27445f;"
        )
        layout.addWidget(self.scan_guidance)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.page_status = QLabel("Page —")
        nav.addWidget(self.page_status)
        self.operation_status = QLabel("")
        self.operation_status.setStyleSheet("color: #66717d;")
        nav.addWidget(self.operation_status)
        self.busy_indicator = QProgressBar()
        self.busy_indicator.setRange(0, 0)
        self.busy_indicator.setTextVisible(False)
        self.busy_indicator.setFixedWidth(120)
        self.busy_indicator.setFixedHeight(8)
        self.busy_indicator.setVisible(False)
        nav.addWidget(self.busy_indicator)
        nav.addStretch(1)
        self.restore_button = QPushButton("Restore…")
        self.restore_button.setToolTip("Restore selected quarantined MIDI file(s) to their original Library location and verify current Library truth. Unchanged Rejected files remain Rejected; genuinely changed files can reopen Review Required.")
        self.restore_button.clicked.connect(self._request_restore)
        self.restore_button.setVisible(False)
        self.quarantine_button = QPushButton("Quarantine…")
        self.quarantine_button.clicked.connect(lambda: self._request_remove(False))
        self.quarantine_button.setVisible(False)
        self.delete_button = QPushButton("Delete…")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.clicked.connect(lambda: self._request_remove(True))
        self.delete_button.setVisible(False)
        self.previous_button = QPushButton("Previous")
        self.previous_button.clicked.connect(self._previous_page)
        nav.addWidget(self.previous_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._next_page)
        nav.addWidget(self.next_button)
        layout.addLayout(nav)

        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(self._play_double_clicked)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._first_page)
        self._playback_timer = QTimer(self)
        self._playback_timer.setInterval(250)
        self._playback_timer.timeout.connect(self._poll_playback)
        self._playback_timer.start()
        # Avoid flicker for sub-second operations, but never leave the user
        # staring at an apparently frozen dialog when work exceeds ~1 second.
        self._busy_delay_timer = QTimer(self)
        self._busy_delay_timer.setSingleShot(True)
        self._busy_delay_timer.setInterval(900)
        self._busy_delay_timer.timeout.connect(self._show_busy_indicator)
        self._volume_timer = QTimer(self)
        self._volume_timer.setSingleShot(True)
        self._volume_timer.setInterval(90)
        self._volume_timer.timeout.connect(self._apply_volume_async)
        self.pageLoaded.connect(self._apply_page)
        self.summaryLoaded.connect(self._apply_summary)
        self.transportFinished.connect(self._apply_transport_result)
        self.closeCleanupFinished.connect(self._finish_close_cleanup)
        self.shellActionFinished.connect(self._apply_shell_action_result)
        self._update_selection_controls()
        self._update_transport(PlaybackState.STOPPED)
        self._refresh()

    def _begin_busy(self, reason: str, message: str = "") -> None:
        key = str(reason)
        self._busy_reasons[key] = int(self._busy_reasons.get(key, 0)) + 1
        if message:
            self._busy_message = str(message)
            self.operation_status.setText(self._busy_message)
        if not self.busy_indicator.isVisible():
            self._busy_delay_timer.start()

    def _end_busy(self, reason: str) -> None:
        key = str(reason)
        count = int(self._busy_reasons.get(key, 0)) - 1
        if count > 0:
            self._busy_reasons[key] = count
        else:
            self._busy_reasons.pop(key, None)
        if not self._busy_reasons:
            self._busy_delay_timer.stop()
            self.busy_indicator.setVisible(False)
            self._busy_message = ""
            self.operation_status.setText("")

    def _show_busy_indicator(self) -> None:
        if self._busy_reasons and not self._closing:
            self.busy_indicator.setVisible(True)
            if not self.operation_status.text():
                self.operation_status.setText("Working…")

    def set_maintenance_busy(self, busy: bool, message: str = "") -> None:
        """Gate new Browser work while Manager-owned maintenance is active.

        Existing audio remains controllable through Pause/Stop, but starting new
        playback, shell/file actions, or lifecycle mutations is blocked until
        the maintenance boundary completes.
        """
        self._maintenance_busy = bool(busy)
        if busy:
            self._begin_busy("maintenance", message or "Library maintenance is running…")
        else:
            self._end_busy("maintenance")
        self._update_selection_controls()
        try:
            state = self.audition.state
        except Exception:
            state = PlaybackState.STOPPED
        self._update_transport(state)

    @staticmethod
    def _path_under_roots(path: str, roots: tuple[str, ...]) -> bool:
        if not path or not roots:
            return False
        try:
            candidate = os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
        except (OSError, ValueError, TypeError):
            return False
        for raw_root in roots:
            try:
                root = os.path.normcase(str(Path(raw_root).expanduser().resolve(strict=False)))
                if os.path.commonpath([candidate, root]) == root:
                    return True
            except (OSError, ValueError, TypeError):
                continue
        return False

    def set_source_state(self, configured_roots: list[str] | tuple[str, ...], accessible_roots: list[str] | tuple[str, ...]) -> None:
        """Publish Manager source availability into the Browser control contract.

        Saved rows remain browseable when a source is offline, but operations
        that re-read, re-analyze, quarantine, or delete a source file must never
        advertise availability for a path outside an accessible configured root.
        The global unreadable retry is stricter because it spans the whole saved
        population: every configured root must be available first.
        """
        self._configured_source_roots = tuple(str(root) for root in configured_roots if str(root))
        self._accessible_source_roots = tuple(str(root) for root in accessible_roots if str(root))
        self._all_sources_ready = source_roots_ready(
            self._configured_source_roots, self._accessible_source_roots
        )
        self._update_selection_controls()
        self.rescan_all_failed_button.setEnabled(
            self._quick_failed_count > 0 and not self._fast_scan_running and self._all_sources_ready and not self._maintenance_busy
        )
        if self._quick_failed_count > 0 and not self._all_sources_ready:
            self.rescan_all_failed_button.setToolTip(
                "Reconnect every configured MIDI source before retrying all unreadable files. "
                "The saved Library is preserved while a source is offline."
            )
        else:
            self.rescan_all_failed_button.setToolTip(
                "Retry the entire saved unreadable-file population. Use this after source files were repaired/replaced or after a scanner update."
            )

    def _selection_changed(self) -> None:
        self._update_selection_controls()
        try:
            state = self.audition.state
        except Exception:
            state = PlaybackState.STOPPED
        self._update_transport(state)

    def _update_review_guidance(self) -> None:
        state = str(self.status_filter.currentData() or "all")
        if state == "needs_review":
            text = (
                "Needs Review is an unresolved lifecycle state. Audition the MIDI and read Recommended Action first. "
                "For a valid file that current Deep Analysis cannot fully profile, choose Keep in Library · Not Mash to resolve it without changing the source. "
                "Use Revalidate only after a meaningful file/Library change; Quarantine or Delete remain deliberate cleanup choices."
            )
        elif state == "rejected":
            text = (
                "Rejected MIDI files are excluded from Remixer by persisted contract. Review the rejection reason, then Show in Folder, Open in Default App, "
                "Quarantine, or permanently Delete. If the source later changes, Fast Scan may return it to review/revalidation."
            )
        elif state == "quarantined":
            text = (
                "Quarantine is reversible and remains in the Library database. Restore returns the file to its original source location and verifies current Library truth; unchanged Rejected files remain Rejected, while genuinely changed files can reopen Review Required."
            )
        elif state == "quick_failed":
            text = (
                "Unreadable files could not be parsed by Fast Scan. Retry them once only after the source file was repaired/replaced "
                "or the Library scanner changed. If they remain unreadable, use Show in Folder to inspect them; Quarantine preserves them "
                "outside the active Library, while Delete permanently removes the source file."
            )
        elif state == "suspicious_loadable":
            text = (
                "These MIDI files loaded successfully but look unusual (for example very short, very long, sparse, or empty). "
                "They do not need another scan by default. Audition/review them; keeping a valid file requires no action. Quarantine uncertain files, "
                "or Delete only files you intentionally want removed from disk."
            )
        elif state == "health_review":
            text = (
                "This combined review shows both unreadable files and unusual-but-loadable files. Use the Status, Issue, and Recommended Action columns to distinguish them: "
                "retry only unreadable files after a relevant change; unusual-but-loadable files are primarily an audition/review decision."
            )
        elif state == "failed_retryable":
            text = (
                "These MIDI files completed Fast Scan but Deep Analysis failed on the first attempt. MIDI Library does not repair source MIDI bytes. "
                "Use the Issue and Recommended Action columns first; a retry simply runs Deep Analysis again."
            )
        elif state in {"failed_persistent", "failed"}:
            text = (
                "Deep Analysis Review does not mean damaged. Valid MIDI Type 2 and safety-limit files should normally be kept. "
                "Check Reason / Recommended Action; use Keep · Not Mash for a valid file you want retained but excluded from normal Remixer recommendations."
            )
        else:
            text = ""
        self.review_guidance.setText(text)
        self.review_guidance.setToolTip(text)
        self.review_guidance.setVisible(bool(text))

    def show_status(self, status: str) -> None:
        index = self.status_filter.findData(str(status))
        if index >= 0:
            self.status_filter.setCurrentIndex(index)
        self._update_review_guidance()
        self._first_page()

    def refresh_view(self) -> None:
        self._refresh()

    def set_engine_work_state(
        self, *, fast_scan_running: bool, deep_running: bool, scan: dict[str, Any] | None = None
    ) -> None:
        """Reflect Engine work and mirror Fast-retry progress in this Browser.

        A user reviewing failed MIDIs must not have to infer that a scan is
        happening behind another window.  The Browser remains the active control
        surface while the detached Engine does the actual work.
        """
        self._fast_scan_running = bool(fast_scan_running)
        self._deep_running = bool(deep_running)
        details = dict(scan or {})
        paused = bool(details.get("paused"))
        operation_state = str(details.get("operation_state") or ("paused" if paused else "running" if self._fast_scan_running else "idle"))
        operation_kind = str(details.get("operation_kind") or "fast_scan")
        current = int(details.get("current") or 0)
        total = int(details.get("total") or 0)
        message = str(details.get("message") or "").strip()
        current_file = str(details.get("current_file") or "").strip()
        recovery = operation_kind == "fast_recovery"
        noun = "Unreadable File Retry" if recovery else "Fast Scan"
        if self._fast_scan_running:
            self.scan_guidance.setVisible(False)
            self.scan_guidance.clear()
            if operation_state == "finalizing":
                self.scan_activity_label.setText(f"{noun} — FINALIZING")
            elif operation_state == "paused":
                self.scan_activity_label.setText(f"{noun} — PAUSED")
            elif operation_state == "pausing":
                self.scan_activity_label.setText(f"{noun} — PAUSING…")
            elif operation_state == "cancelling":
                self.scan_activity_label.setText(f"{noun} — CANCELLING…")
            else:
                self.scan_activity_label.setText(f"{noun} — RUNNING")
            if total > 0:
                self.scan_activity_progress.setRange(0, total)
                self.scan_activity_progress.setValue(min(current, total))
                suffix = f" · {Path(current_file).name}" if current_file else ""
                fmt = f"{current:,}/{total:,} · %p%{suffix}"
                if operation_state == "finalizing":
                    fmt = f"{total:,}/{total:,} parsed · saving results…"
                self.scan_activity_progress.setFormat(fmt)
            else:
                self.scan_activity_progress.setRange(0, 0)
                self.scan_activity_progress.setFormat(message or "Preparing failed-file retry…")
        else:
            self.scan_activity_label.setText("Fast scan — IDLE")
            self.scan_activity_progress.setRange(0, 1)
            self.scan_activity_progress.setValue(0)
            self.scan_activity_progress.setFormat("Idle")
        can_pause = self._fast_scan_running and operation_state in {"running", "paused"}
        can_cancel = self._fast_scan_running and operation_state in {"preparing", "running", "pausing", "paused"}
        self.pause_fast_scan_button.setEnabled(can_pause)
        if recovery:
            self.pause_fast_scan_button.setText(
                "Resume Retry" if operation_state == "paused"
                else "Pausing Retry…" if operation_state == "pausing"
                else "Pause Retry"
            )
            self.cancel_fast_scan_button.setText("Cancelling Retry…" if operation_state == "cancelling" else "Cancel Retry")
        else:
            self.pause_fast_scan_button.setText(
                "Resume Fast Scan" if operation_state == "paused"
                else "Pausing Fast Scan…" if operation_state == "pausing"
                else "Pause Fast Scan"
            )
            self.cancel_fast_scan_button.setText(
                "Cancelling Fast Scan…" if operation_state == "cancelling" else "Cancel Fast Scan"
            )
        self.cancel_fast_scan_button.setEnabled(can_cancel)
        self.rescan_all_failed_button.setText(
            f"Retry Unreadable ({self._quick_failed_count:,})…"
            if self._quick_failed_count > 0 else "Retry All Unreadable…"
        )
        self.rescan_all_failed_button.setEnabled(self._quick_failed_count > 0 and not self._fast_scan_running and self._all_sources_ready and not self._maintenance_busy)
        self._update_selection_controls()

    def show_scan_completion(self, message: str) -> None:
        text = str(message or "").strip()
        self.scan_guidance.setText(text)
        self.scan_guidance.setToolTip(text)
        self.scan_guidance.setVisible(bool(text))

    def _queue_first_page(self) -> None:
        self._search_timer.start()

    def _first_page(self) -> None:
        self._offset = 0
        self._load_page()

    def _refresh(self) -> None:
        # Both aggregate summary and page reads run off the Qt thread.  A WAL
        # checkpoint, forced re-scan transaction, or large failure population
        # must never make Play/Stop or window painting wait on SQLite.
        self._load_summary_async()
        self._load_page()

    def _load_summary_async(self) -> None:
        if self._closing or self._summary_inflight:
            return
        self._summary_inflight = True
        self._begin_busy("summary", "Refreshing Library summary…")
        self._summary_generation += 1
        generation = self._summary_generation

        def run() -> None:
            summary: dict[str, Any] = {}
            error = ""
            try:
                summary = dict(self.reader.summary(include_failure_groups=False))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._query_lock:
                    self._query_threads.discard(threading.current_thread())
            if not self._closing:
                self.summaryLoaded.emit(generation, summary, error)

        thread = threading.Thread(target=run, name="MidiLibraryBrowserSummary", daemon=True)
        with self._query_lock:
            self._query_threads.add(thread)
        thread.start()

    def _apply_summary(self, generation: int, summary_obj: object, error: str) -> None:
        if generation != self._summary_generation or self._closing:
            return
        self._summary_inflight = False
        self._end_busy("summary")
        summary = dict(summary_obj) if isinstance(summary_obj, dict) else {}
        if not error and summary.get("available"):
            self._quick_failed_count = int(summary.get("quick_failed") or 0)
            self.summary_label.setText(
                f"{int(summary.get('total') or 0):,} discovered · "
                f"{int(summary.get('usable') or 0):,} usable · "
                f"{int(summary.get('mash_ready') or 0):,} Mash Ready · "
                f"{int(summary.get('fusion_candidate') or 0):,} Fusion · "
                f"{int(summary.get('usable_not_mash') or 0):,} Not Mash · "
                f"{int(summary.get('needs_review') or 0):,} review · "
                f"{int(summary.get('rejected') or 0):,} rejected · "
                f"{int(summary.get('quarantined') or 0):,} quarantined"
            )
        else:
            self._quick_failed_count = 0
            self.summary_label.setText("Saved catalogue summary is temporarily unavailable" if error else "Saved catalogue is not currently readable")
        self.rescan_all_failed_button.setText(
            f"Retry Unreadable ({self._quick_failed_count:,})…"
            if self._quick_failed_count > 0 else "Retry All Unreadable…"
        )
        self.rescan_all_failed_button.setEnabled(self._quick_failed_count > 0 and not self._fast_scan_running and self._all_sources_ready and not self._maintenance_busy)

    def _load_page(self) -> None:
        if self._closing:
            return
        if self._request_inflight:
            self._queued_refresh = True
            return
        self._request_inflight = True
        self._queued_refresh = False
        self._request_generation += 1
        generation = self._request_generation
        search = self.search.text()
        status = str(self.status_filter.currentData() or "all")
        offset = self._offset
        limit = self._limit
        sort_key = self._sort_key
        descending = self._descending
        self.page_status.setText("Loading saved catalogue…")
        self._begin_busy("catalogue", "Loading saved catalogue…")
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)

        def run() -> None:
            page: CataloguePage | None = None
            error = ""
            try:
                page = self.reader.page(
                    search=search,
                    status=status,
                    sort_key=sort_key,
                    descending=descending,
                    offset=offset,
                    limit=limit,
                )
            except Exception as exc:  # read-only SQLite/runtime errors are surfaced in the browser
                error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._query_lock:
                    self._query_threads.discard(threading.current_thread())
            if not self._closing:
                self.pageLoaded.emit(generation, page, error)

        thread = threading.Thread(target=run, name="MidiLibraryBrowserQuery", daemon=True)
        with self._query_lock:
            self._query_threads.add(thread)
        thread.start()

    def _status_display(self, row: dict[str, Any]) -> str:
        lifecycle = str(row.get("lifecycle_state") or "")
        if lifecycle == "rejected":
            return "REJECTED"
        if lifecycle == "quarantined":
            return "QUARANTINED"
        if lifecycle == "blessed":
            return "BLESSED"
        if lifecycle in {"review_required", "revalidated"}:
            return "REVIEW" if lifecycle == "review_required" else "REVALIDATED"
        if str(row.get("quick_status") or "") == "failed":
            return "UNREADABLE"
        deep = str(row.get("deep_status") or "pending")
        if deep == "failed":
            attempts = int(row.get("deep_attempt_count") or 0)
            return "DEEP REVIEW" if attempts >= 2 else "DEEP RETRY AVAILABLE"
        if bool(row.get("suspicious_loadable")):
            return f"UNUSUAL / {deep.upper()}"
        return deep.upper()

    @staticmethod
    def _friendly_failure_note(row: dict[str, Any]) -> tuple[str, str]:
        """Return concise user text plus optional technical detail for tooltip."""
        quick_status = str(row.get("quick_status") or "")
        deep_status = str(row.get("deep_status") or "")
        raw_error = " ".join(str(row.get("error") or "").split())
        health_note = "; ".join(str(item) for item in (row.get("health_reasons") or ()))
        if quick_status == "failed":
            lowered = raw_error.casefold()
            if any(token in lowered for token in ("eof", "unexpected end", "mthd", "invalid midi", "truncated")):
                display = "Unreadable MIDI — file appears incomplete or damaged"
            elif "no such file" in lowered or "filenotfound" in lowered:
                display = "Unreadable MIDI — source file is missing"
            elif "permission" in lowered or "access is denied" in lowered:
                display = "Unreadable MIDI — Windows denied file access"
            elif "safety limit" in lowered or "resource" in lowered:
                display = "Unreadable MIDI — exceeded a Library safety limit"
            else:
                display = "Unreadable MIDI — Fast Scan could not parse this file"
            return display, raw_error
        if deep_status == "failed":
            category = str(row.get("failure_category") or "Deep Analysis issue")
            return category, raw_error
        if bool(row.get("suspicious_loadable")):
            display = "Unusual but loadable" + (f" — {health_note}" if health_note else "")
            return display, raw_error
        return health_note, raw_error

    def _apply_page(self, generation: int, page_obj: object, error: str) -> None:
        if generation != self._request_generation or self._closing:
            return
        self._request_inflight = False
        self._end_busy("catalogue")
        if error or not isinstance(page_obj, CataloguePage):
            self.table.setRowCount(0)
            self.page_status.setText(error or "Saved catalogue could not be read.")
            self.previous_button.setEnabled(False)
            self.next_button.setEnabled(False)
            if self._queued_refresh:
                self._load_page()
            return
        page = page_obj
        self._total_matches = page.total_matches
        if self._offset >= self._total_matches and self._offset > 0:
            self._offset = max(0, ((max(0, self._total_matches - 1)) // self._limit) * self._limit)
            self._load_page()
            return
        selected_paths_before = {path for path, _quick, _deep, _attempts, _suspicious in self._selected_rows()}
        current_item_before = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        current_path_before = str(current_item_before.data(ROLE_PATH) or "") if current_item_before is not None else ""
        vertical_scroll_before = self.table.verticalScrollBar().value()
        horizontal_scroll_before = self.table.horizontalScrollBar().value()
        restored_current_row = -1

        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(page.rows))
            for row_index, row in enumerate(page.rows):
                duration = row.get("duration_seconds")
                minutes = ""
                if duration is not None:
                    total_seconds = max(0, int(round(float(duration))))
                    minutes = f"{total_seconds // 60}:{total_seconds % 60:02d}"
                bpm = row.get("bpm")
                key = str(row.get("key") or "")
                mode = str(row.get("mode") or "")
                key_display = key + (f" {mode}" if key and mode and mode.casefold() not in key.casefold() else "")
                failure_note, technical_note = self._friendly_failure_note(row)
                rejection_note = str(row.get("rejection_detail") or "").strip()
                if not rejection_note:
                    rejection_note = failure_note
                size_bytes = int(row.get("file_size_bytes") or 0)
                if size_bytes >= 1024 * 1024:
                    size_text = f"{size_bytes / (1024 * 1024):.1f} MB"
                elif size_bytes >= 1024:
                    size_text = f"{size_bytes / 1024:.1f} KB"
                else:
                    size_text = f"{size_bytes} B"
                values = (
                    str(row.get("title") or ""),
                    str(row.get("artist") or ""),
                    str(row.get("filename") or ""),
                    str(row.get("eligibility_label") or ""),
                    self._status_display(row),
                    rejection_note,
                    str(row.get("last_validation") or ""),
                    str(row.get("recommended_action") or ""),
                    size_text,
                    minutes,
                    f"{float(bpm):.1f}" if bpm is not None else "",
                    key_display,
                    str(row.get("meter") or ""),
                    f"{int(row.get('tracks') or 0):,}",
                    f"{int(row.get('notes') or 0):,}",
                    str(row.get("folder") or ""),
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 5 and technical_note:
                        item.setToolTip(f"{value}\n\nTechnical detail:\n{technical_note}")
                    else:
                        item.setToolTip(value)
                    if column in {8, 13, 14}:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    if column == 0:
                        item.setData(ROLE_PATH, str(row.get("path") or ""))
                        item.setData(ROLE_QUICK_STATUS, str(row.get("quick_status") or ""))
                        item.setData(ROLE_DEEP_STATUS, str(row.get("deep_status") or ""))
                        item.setData(ROLE_DEEP_ATTEMPTS, int(row.get("deep_attempt_count") or 0))
                        item.setData(ROLE_SUSPICIOUS, bool(row.get("suspicious_loadable")))
                        item.setData(ROLE_DEEP_RETRY_ALLOWED, bool(row.get("deep_retry_allowed")))
                        item.setData(ROLE_LIFECYCLE, str(row.get("lifecycle_state") or ""))
                        item.setData(ROLE_ELIGIBILITY, str(row.get("eligibility_state") or ""))
                        item.setData(ROLE_ORIGINAL_PATH, str(row.get("original_source_path") or ""))
                        item.setData(ROLE_REJECTION_CODE, str(row.get("rejection_code") or ""))
                        item.setData(ROLE_RECOMMENDED_ACTION, str(row.get("recommended_action") or ""))
                    self.table.setItem(row_index, column, item)
                row_path = str(row.get("path") or "")
                if row_path in selected_paths_before:
                    for column in range(len(self.COLUMNS)):
                        selected_item = self.table.item(row_index, column)
                        if selected_item is not None:
                            selected_item.setSelected(True)
                if current_path_before and row_path == current_path_before:
                    restored_current_row = row_index
            if restored_current_row >= 0:
                self.table.setCurrentCell(
                    restored_current_row, 0, QItemSelectionModel.SelectionFlag.NoUpdate
                )
        finally:
            self.table.setUpdatesEnabled(True)
        self.table.verticalScrollBar().setValue(
            min(vertical_scroll_before, self.table.verticalScrollBar().maximum())
        )
        self.table.horizontalScrollBar().setValue(
            min(horizontal_scroll_before, self.table.horizontalScrollBar().maximum())
        )
        first = page.offset + 1 if page.total_matches and page.rows else 0
        last = page.offset + len(page.rows)
        self.page_status.setText(f"Showing {first:,}–{last:,} of {page.total_matches:,} matching MIDI(s)")
        self.previous_button.setEnabled(page.offset > 0)
        self.next_button.setEnabled(page.offset + len(page.rows) < page.total_matches)
        self._update_selection_controls()
        if self._qa_playback_mode in {"playing", "paused"} and not self._qa_playback_started:
            for row_index, row in enumerate(page.rows):
                path = str(row.get("path") or "")
                if (
                    str(row.get("quick_status") or "") != "failed"
                    and path
                    and Path(path).is_file()
                    and self._path_under_roots(path, self._accessible_source_roots)
                ):
                    self._qa_playback_started = True
                    self.table.setCurrentCell(row_index, 0)
                    self.table.selectRow(row_index)
                    QTimer.singleShot(0, self._play_selected)
                    break
        if (
            not self._qa_manager_close_triggered
            and str(os.environ.get("MIDI_LIBRARY_QA_CLOSE_MANAGER_AFTER_BROWSER_PAGE", "")).strip()
        ):
            self._qa_manager_close_triggered = True
            try:
                marker = self.app_root / "user-data" / "logs" / "qa-browser-page-ready.txt"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(f"rows={len(page.rows)} total={page.total_matches}", encoding="utf-8")
            except OSError:
                pass
            try:
                settle_ms = max(0, int(os.environ.get("MIDI_LIBRARY_QA_BROWSER_CLOSE_SETTLE_MS", "1200") or 1200))
            except ValueError:
                settle_ms = 1200
            parent = self.parentWidget()
            if parent is not None:
                QTimer.singleShot(settle_ms, parent.close)
        if self._queued_refresh:
            self._load_page()

    def _previous_page(self) -> None:
        self._offset = max(0, self._offset - self._limit)
        self._load_page()

    def _next_page(self) -> None:
        if self._offset + self._limit < self._total_matches:
            self._offset += self._limit
            self._load_page()

    def _sort_by_column(self, column: int) -> None:
        if not 0 <= column < len(self.COLUMNS):
            return
        _label, key = self.COLUMNS[column]
        if key == self._sort_key:
            self._descending = not self._descending
        else:
            self._sort_key = key
            self._descending = False
        self.table.horizontalHeader().setSortIndicator(
            column, Qt.SortOrder.DescendingOrder if self._descending else Qt.SortOrder.AscendingOrder
        )
        self._offset = 0
        self._load_page()

    def _selected_rows(self) -> list[tuple[str, str, str, int, bool]]:
        result: list[tuple[str, str, str, int, bool]] = []
        seen: set[str] = set()
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item is None:
                continue
            path = str(item.data(ROLE_PATH) or "")
            if not path or path in seen:
                continue
            seen.add(path)
            result.append((
                path,
                str(item.data(ROLE_QUICK_STATUS) or ""),
                str(item.data(ROLE_DEEP_STATUS) or ""),
                int(item.data(ROLE_DEEP_ATTEMPTS) or 0),
                bool(item.data(ROLE_SUSPICIOUS)),
            ))
        return result

    def _selected_path(self) -> str:
        rows = self._selected_rows()
        return rows[0][0] if rows else ""

    def _selected_title(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return str(item.text()) if item is not None else Path(self._selected_path()).name

    def _title_for_path(self, path: str) -> str:
        """Return the visible title bound to *path*, never to a later selection.

        Audition starts on a worker thread.  The user may change the highlighted
        row after pressing Play but before that worker executes, so playback
        metadata must be captured from the same path identity as the audio target.
        """
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and str(item.data(ROLE_PATH) or "") == str(path):
                return str(item.text())
        return Path(path).name

    def _selected_deep_retry_allowed(self) -> bool:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return False
        for index in indexes:
            item = self.table.item(index.row(), 0)
            if item is None or not bool(item.data(ROLE_DEEP_RETRY_ALLOWED)):
                return False
        return True

    def _selected_lifecycle_rows(self) -> list[tuple[str, str, str]]:
        result: list[tuple[str, str, str]] = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item is None:
                continue
            result.append((
                str(item.data(ROLE_PATH) or ""),
                str(item.data(ROLE_LIFECYCLE) or ""),
                str(item.data(ROLE_ELIGIBILITY) or ""),
            ))
        return result

    def _selected_review_disposition_rows(self) -> list[tuple[str, str, str, str]]:
        result: list[tuple[str, str, str, str]] = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            if item is None:
                continue
            result.append((
                str(item.data(ROLE_PATH) or ""),
                str(item.data(ROLE_QUICK_STATUS) or ""),
                str(item.data(ROLE_ELIGIBILITY) or ""),
                str(item.data(ROLE_REJECTION_CODE) or ""),
            ))
        return result

    def _update_selection_controls(self) -> None:
        rows = self._selected_rows()
        lifecycle_rows = self._selected_lifecycle_rows()
        have = bool(rows)
        one_selected = len(rows) == 1
        selected_paths = [path for path, _quick, _deep, _attempts, _suspicious in rows]
        eligibility_by_path = {path: eligibility for path, _life, eligibility in lifecycle_rows}
        selected_shell_allowed = have and all(
            saved_row_can_attempt_shell_action(eligibility=eligibility_by_path.get(path, ""))
            for path, _quick, _deep, _attempts, _suspicious in rows
        )
        selected_audition_allowed = have and all(
            saved_row_can_attempt_audition(
                quick_status=quick, eligibility=eligibility_by_path.get(path, "")
            )
            for path, quick, _deep, _attempts, _suspicious in rows
        )
        selected_in_active_sources = have and all(
            self._path_under_roots(path, self._accessible_source_roots) for path in selected_paths
        )
        eligibilities = [elig for _path, _life, elig in lifecycle_rows]
        all_review = have and all(elig == "needs_review" for elig in eligibilities)
        all_rejected = have and all(elig == "rejected" for elig in eligibilities)
        all_quarantined = have and all(elig == "quarantined" for elig in eligibilities)
        all_deep_failed = have and all(quick != "failed" and deep == "failed" for _path, quick, deep, _attempts, _suspicious in rows)
        all_deep_complete = have and all(quick != "failed" and deep == "complete" for _path, quick, deep, _attempts, _suspicious in rows)
        all_deep_retry_allowed = all_deep_failed and self._selected_deep_retry_allowed()
        with self._transport_lock:
            transport_busy = any(action != "volume" for action in self._transport_actions)
        with self._shell_lock:
            shell_busy = any(thread.is_alive() for thread in self._shell_threads)
        maintenance_free = not self._maintenance_busy
        one_playable = (
            one_selected and selected_audition_allowed and not all_quarantined
            and maintenance_free and not transport_busy
        )
        review_dispositions = self._selected_review_disposition_rows()
        safe_rejected_codes = {"unsupported_midi_structure", "safety_resource_limit", "remixer_incompatible"}
        can_keep_not_mash = bool(review_dispositions) and all(
            quick != "failed"
            and (
                eligibility == "needs_review"
                or (eligibility == "rejected" and rejection_code in safe_rejected_codes)
            )
            for _path, quick, eligibility, rejection_code in review_dispositions
        )

        one_file_available = one_selected and selected_shell_allowed
        self.actions_button.setEnabled(have)
        self.open_file_button.setEnabled(one_file_available and maintenance_free and not shell_busy)
        self.show_in_folder_button.setEnabled(one_file_available and maintenance_free and not shell_busy)
        self.play_button.setEnabled(one_playable)
        self.rescan_selected_button.setEnabled(maintenance_free and selected_in_active_sources and all_review and not self._fast_scan_running)
        self.rescan_selected_button.setText("Revalidate Selected…" if all_review else "Re-scan Selected…")
        self.reanalyze_button.setEnabled(maintenance_free and selected_in_active_sources and all_deep_complete and not all_review and not all_rejected)
        self.retry_deep_button.setEnabled(maintenance_free and selected_in_active_sources and all_deep_retry_allowed and all_review)
        self.keep_not_mash_button.setEnabled(maintenance_free and can_keep_not_mash)
        self.quarantine_button.setEnabled(maintenance_free and selected_in_active_sources and (all_review or all_rejected))
        self.delete_button.setEnabled(maintenance_free and have and (all_rejected or all_quarantined) and (selected_in_active_sources or all_quarantined))
        self.restore_button.setEnabled(maintenance_free and all_quarantined)

        self.quarantine_button.setToolTip("Move selected Needs Review/Rejected MIDI file(s) into reversible Library quarantine.")
        self.delete_button.setToolTip("Permanently delete selected Rejected or Quarantined MIDI file(s) from disk. Confirmation is required.")
        self.restore_button.setToolTip("Restore selected quarantined MIDI file(s) to their original Library location and verify current Library truth. Unchanged Rejected files remain Rejected; genuinely changed files can reopen Review Required.")
        if have and not selected_audition_allowed:
            self.selection_guidance.setText("This row cannot be auditioned, but Actions… can still expose available inspection/review choices.")
        elif have and not selected_in_active_sources:
            self.selection_guidance.setText("Play/Open/Show remain available from saved Library state. Reconnect the configured source before file-mutating actions. Use Actions… or right-click.")
        else:
            self.selection_guidance.setText("Use Actions… or right-click the selected MIDI row.")

    def _start_shell_action(self, action: str, path: str, func) -> None:
        """Run user-visible OS shell integration off the Qt thread.

        Explorer/file-association startup can block transiently on Windows while
        the shell is busy.  The Browser must stay repaintable and tell the user
        that the request is in progress rather than looking like a dead button.
        Shell workers are daemon fallbacks: they never own Browser/app lifetime.
        """
        if self._closing or self._maintenance_busy:
            return
        with self._shell_lock:
            if any(thread.is_alive() for thread in self._shell_threads):
                self.operation_status.setText("A Windows file action is already starting…")
                return
        label = "Opening Windows Explorer…" if action == "show" else "Opening MIDI in the default app…"
        self._begin_busy("shell", label)
        self.open_file_button.setEnabled(False)
        self.show_in_folder_button.setEnabled(False)

        def run() -> None:
            error = ""
            try:
                func(path)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._shell_lock:
                    self._shell_threads.discard(threading.current_thread())
            if not self._closing:
                self.shellActionFinished.emit(str(action), str(path), str(error))

        thread = threading.Thread(target=run, name=f"MidiLibraryShell-{action}", daemon=True)
        with self._shell_lock:
            self._shell_threads.add(thread)
        thread.start()

    def _apply_shell_action_result(self, action: str, path: str, error: str) -> None:
        self._end_busy("shell")
        self._update_selection_controls()
        if error:
            if error.startswith("FileNotFoundError:"):
                QMessageBox.information(self, "MIDI Library", f"The source MIDI file is not currently available:\n{path}")
                return
            if action == "show":
                QMessageBox.warning(
                    self,
                    "Show in Folder",
                    f"Windows Explorer could not show and select this MIDI file:\n{path}\n\n{error.split(': ', 1)[-1]}",
                )
            else:
                QMessageBox.warning(
                    self,
                    "Open in Default App",
                    f"Windows could not open this MIDI in its default application:\n{path}\n\n{error.split(': ', 1)[-1]}",
                )
            return
        if action == "show":
            self.operation_status.setText(f"Explorer opened · {Path(path).name} selected")
        else:
            self.operation_status.setText(f"Opened in default app · {Path(path).name}")

    def _open_selected_file(self) -> None:
        if not self.open_file_button.isEnabled() or len(self._selected_rows()) != 1:
            return
        path = self._selected_path()
        if path:
            self._start_shell_action("open", path, open_midi_file)

    def _show_selected_in_folder(self) -> None:
        if not self.show_in_folder_button.isEnabled() or len(self._selected_rows()) != 1:
            return
        path = self._selected_path()
        if path:
            self._start_shell_action("show", path, show_file_in_folder)

    def eventFilter(self, watched, event) -> bool:
        """Make physical Windows right-click a first-class row action path.

        Some QAbstractScrollArea/viewport combinations fail to emit the custom
        context-menu signal reliably. Handle the actual right-button release
        directly, with QEvent.ContextMenu retained as keyboard/platform fallback.
        The consumed mouse event prevents a second menu from the later synthetic
        context event; a timestamp guard covers platform variations.
        """
        if hasattr(self, "table") and watched is self.table.viewport():
            event_type = event.type()
            if event_type == QEvent.Type.MouseButtonRelease:
                try:
                    is_right = event.button() == Qt.MouseButton.RightButton
                except (AttributeError, TypeError):
                    is_right = False
                if is_right:
                    position_obj = event.position() if hasattr(event, "position") else event.pos()
                    position = position_obj.toPoint() if hasattr(position_obj, "toPoint") else position_obj
                    self._last_context_menu_at = time.monotonic()
                    self._show_table_context_menu(position)
                    event.accept()
                    return True
            if event_type == QEvent.Type.ContextMenu:
                # A physical right-click already handled above may still be
                # followed by a platform-generated ContextMenu event. Do not
                # show the same menu twice. Keyboard context-menu requests do
                # not follow a recent right-button release and remain supported.
                if time.monotonic() - self._last_context_menu_at < 0.35:
                    event.accept()
                    return True
                position = event.pos()
                item = self.table.itemAt(position)
                if item is None:
                    current = self.table.currentItem()
                    if current is None:
                        event.accept()
                        return True
                    position = self.table.visualItemRect(current).center()
                self._last_context_menu_at = time.monotonic()
                self._show_table_context_menu(position)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _show_table_context_menu(self, position) -> None:
        if self._context_menu_active or self._closing:
            return
        item = self.table.itemAt(position)
        if item is None:
            return
        self._context_menu_active = True
        self._last_context_menu_at = time.monotonic()
        try:
            if not item.isSelected():
                self.table.clearSelection()
                self.table.selectRow(item.row())
            self.table.setCurrentCell(item.row(), max(0, item.column()))
            self.table.setFocus(Qt.FocusReason.MouseFocusReason)
            self._update_selection_controls()

            menu = QMenu(self)
            play_action = menu.addAction(self.play_pause_button.text())
            play_action.setEnabled(self.play_pause_button.isEnabled())
            stop_action = menu.addAction("Stop Audition")
            stop_action.setEnabled(self.stop_button.isEnabled())
            menu.addSeparator()
            keep_action = menu.addAction("Keep Selected in Library · Not Mash…")
            keep_action.setEnabled(self.keep_not_mash_button.isEnabled())
            menu.addSeparator()
            open_action = menu.addAction("Open in Default App")
            open_action.setEnabled(self.open_file_button.isEnabled())
            show_action = menu.addAction("Show in Folder")
            show_action.setEnabled(self.show_in_folder_button.isEnabled())
            menu.addSeparator()
            rescan_action = menu.addAction(self.rescan_selected_button.text())
            rescan_action.setEnabled(self.rescan_selected_button.isEnabled())
            rebuild_action = menu.addAction("Rebuild Selected Analysis…")
            rebuild_action.setEnabled(self.reanalyze_button.isEnabled())
            retry_deep_action = menu.addAction("Retry Failed Analysis…")
            retry_deep_action.setEnabled(self.retry_deep_button.isEnabled())
            menu.addSeparator()
            restore_action = menu.addAction("Restore Selected…")
            restore_action.setEnabled(self.restore_button.isEnabled())
            quarantine_action = menu.addAction("Quarantine Selected…")
            quarantine_action.setEnabled(self.quarantine_button.isEnabled())
            delete_action = menu.addAction("Delete Selected Files…")
            delete_action.setEnabled(self.delete_button.isEnabled())

            chosen = menu.exec(self.table.viewport().mapToGlobal(position))
            if chosen is play_action:
                self._toggle_play_pause()
            elif chosen is stop_action:
                self._stop()
            elif chosen is keep_action:
                self._request_keep_not_mash()
            elif chosen is open_action:
                self._open_selected_file()
            elif chosen is show_action:
                self._show_selected_in_folder()
            elif chosen is rescan_action:
                self._request_rescan_selected()
            elif chosen is rebuild_action:
                self._request_reanalyze_selected()
            elif chosen is retry_deep_action:
                self._request_retry_deep()
            elif chosen is restore_action:
                self._request_restore()
            elif chosen is quarantine_action:
                self._request_remove(False)
            elif chosen is delete_action:
                self._request_remove(True)
        finally:
            self._context_menu_active = False
            self._last_context_menu_at = time.monotonic()
    def _request_keep_not_mash(self) -> None:
        rows = self._selected_review_disposition_rows()
        paths = [path for path, _quick, _eligibility, _reason in rows if path]
        if not paths or not self.keep_not_mash_button.isEnabled():
            QMessageBox.information(
                self,
                "MIDI Library",
                "Keep in Library · Not Mash applies to readable Needs Review files and to supported manual-review rejection categories only.",
            )
            return
        if QMessageBox.question(
            self,
            "Keep MIDI Outside Mash Recommendations",
            f"Keep {len(paths):,} selected MIDI file(s) in the Library but exclude them from normal Guided/Assisted mash recommendations?\n\n"
            "This is appropriate for valid MIDI you want to retain even though current Deep Analysis cannot fully profile it. "
            "The source files will not be changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.keepNotMashRequested.emit(paths)

    def _request_remove(self, permanent: bool) -> None:
        if self._pending_remove is not None:
            return
        if permanent:
            if not self.delete_button.isEnabled():
                return
        elif not self.quarantine_button.isEnabled():
            return
        rows = self._selected_lifecycle_rows()
        if permanent:
            paths = [path for path, _life, eligibility in rows if eligibility in {"rejected", "quarantined"}]
            required = {"rejected", "quarantined"}
        else:
            paths = [path for path, _life, eligibility in rows if eligibility in {"needs_review", "rejected"}]
            required = {"needs_review", "rejected"}
        if not paths or len(paths) != len(rows):
            QMessageBox.information(self, "MIDI Library", f"This cleanup action applies only to: {', '.join(sorted(required))}.")
            return
        if permanent:
            question = (
                f"Permanently DELETE {len(paths):,} selected MIDI file(s) from disk?\n\n"
                "This removes the physical source file and cannot be undone by MIDI Library."
            )
            title = "Confirm Permanent MIDI Deletion"
        else:
            question = (
                f"Move {len(paths):,} selected MIDI file(s) out of the active Library into Quarantine?\n\n"
                "Quarantine is the safer option and preserves the files outside the scanned source tree."
            )
            title = "Confirm MIDI Quarantine"
        if QMessageBox.warning(
            self,
            title,
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        # Windows may refuse to move/delete a MIDI while the audition synth or
        # scheduler still owns it.  File mutation is sequenced strictly after a
        # verified asynchronous Stop result, never merely after issuing Stop.
        self._pending_remove = (list(paths), bool(permanent))
        self._stop(silent=True)

    def _request_restore(self) -> None:
        if not self.restore_button.isEnabled():
            return
        rows = self._selected_lifecycle_rows()
        paths = [path for path, _life, eligibility in rows if eligibility == "quarantined"]
        if not paths or len(paths) != len(rows):
            return
        if QMessageBox.question(
            self, "Restore Quarantined MIDI",
            f"Restore {len(paths):,} quarantined MIDI file(s) to their original Library location and verify their current Library state? Unchanged Rejected files remain Rejected; genuinely changed files can reopen Review Required.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.restore_button.setEnabled(False)
        self.restoreRequested.emit(paths)

    def _request_rescan_selected(self) -> None:
        if not self.rescan_selected_button.isEnabled():
            return
        paths = [path for path, _quick, _deep, _attempts, _suspicious in self._selected_rows()]
        if not paths:
            return
        if QMessageBox.question(
            self,
            "Re-scan Selected with Fast Scan",
            f"Force a fresh Fast Scan of {len(paths):,} selected MIDI file(s), even if unchanged?\n\n"
            "This re-runs Fast Scan; it does not repair source MIDI bytes. Existing saved results remain committed until each re-scan finishes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.rescan_selected_button.setEnabled(False)
        self.rescanRequested.emit(paths)

    def _request_rescan_all_failed(self) -> None:
        if not self.rescan_all_failed_button.isEnabled():
            return
        # Use the asynchronously maintained summary count. Never perform an
        # aggregate SQLite query on the Qt thread just because the user clicks.
        count = int(self._quick_failed_count or 0)
        if count <= 0:
            QMessageBox.information(self, "Unreadable Files", "There are no unreadable Fast Scan failures to retry.")
            return
        if QMessageBox.warning(
            self,
            "Retry All Unreadable Files",
            f"Force a fresh Fast Scan of all {count:,} MIDI file(s) currently marked unreadable?\n\n"
            "Do this once after a Library scanner update or after repairing source files. If the same files remain unreadable afterward, repeated retries without another change are unlikely to help.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.rescan_all_failed_button.setEnabled(False)
        self.rescanAllFastFailedRequested.emit()

    def _request_reanalyze_selected(self) -> None:
        if not self.reanalyze_button.isEnabled():
            return
        rows = self._selected_rows()
        paths = [path for path, quick, deep, _attempts, _suspicious in rows if quick != "failed" and deep == "complete"]
        if not paths or len(paths) != len(rows):
            QMessageBox.information(
                self, "Deep Analysis",
                "Rebuild Selected Analysis applies only to MIDI files whose Deep Analysis is already complete. "
                "Pending/paused work should be resumed normally; eligible failed rows use Retry Failed Analysis instead."
            )
            return
        if QMessageBox.warning(
            self,
            "Rebuild Selected Deep Analysis",
            f"Rebuild Deep Analysis for {len(paths):,} selected MIDI file(s)?\n\n"
            "Only derived Deep Analysis/readiness data for these files is invalidated. Source MIDI files and Fast catalogue entries remain untouched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.reanalyze_button.setEnabled(False)
        self.reanalyzeRequested.emit(paths)

    def _request_retry_deep(self) -> None:
        if not self.retry_deep_button.isEnabled():
            return
        rows = self._selected_rows()
        paths = [path for path, quick, deep, _attempts, _suspicious in rows if quick != "failed" and deep == "failed"]
        if not paths or len(paths) != len(rows) or not self._selected_deep_retry_allowed():
            QMessageBox.information(
                self, "Deep Analysis",
                "Retry Failed Analysis is available only for failure types that can plausibly change after a source, access, or analyzer update. "
                "Valid unsupported MIDI Type 2 and current resource/safety-limit review items should normally be kept; repeating the same analysis will not fix them."
            )
            return
        question = (
            f"Retry Deep Analysis for {len(paths):,} selected failed MIDI file(s)?\n\n"
            "Use this deliberate retry only after something relevant has changed — for example the analyzer was updated, "
            "the source MIDI was repaired, or a safety/resource limit was changed.\n\n"
            "This action requeues selected Deep Analysis failures; it does not repair the source MIDI and is separate from the one-time first-failure retry pass."
        )
        if QMessageBox.warning(
            self,
            "Retry Failed Deep Analysis",
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.retry_deep_button.setEnabled(False)
        self.retryRequested.emit(paths)

    def _configure_audio(self) -> None:
        """Configure Library audition without requiring MIDI Remixer.

        File selection stays on the Qt thread because it is a native UI action.
        No FluidSynth subprocess is launched here; the next Play request remains
        the authoritative runtime validation. This keeps Audio Setup responsive
        even when a broken executable was selected previously.
        """
        try:
            state = self.audition.state
        except Exception:
            state = PlaybackState.STOPPED
        with self._transport_lock:
            transport_busy = any(thread.is_alive() for thread in self._transport_threads)
        if state != PlaybackState.STOPPED or transport_busy:
            QMessageBox.information(
                self, "Audio Setup",
                "Stop Library audition before changing the FluidSynth or SoundFont configuration."
            )
            return

        settings = AppSettings(self.app_root)
        configured_fluid = str(settings.get("fluidsynth_path") or "").strip() or None
        configured_soundfont = str(settings.get("last_soundfont_path") or "").strip() or None
        configured_soundfont_dir = str(settings.get("last_soundfont_directory") or "").strip() or None
        detected_fluid = find_fluidsynth(self.app_root, configured_fluid)
        detected_soundfont = find_soundfont(self.app_root, configured_soundfont, configured_soundfont_dir)

        box = QMessageBox(self)
        box.setWindowTitle("MIDI Library Audio Setup")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "Library audition uses FluidSynth plus a General MIDI SoundFont. MIDI Remixer is not required.\n\n"
            f"FluidSynth: {detected_fluid or 'not found'}\n"
            f"SoundFont: {detected_soundfont or 'not found'}"
        )
        choose_button = box.addButton("Choose Files…", QMessageBox.ButtonRole.ActionRole)
        use_button = None
        if detected_fluid and detected_soundfont:
            use_button = box.addButton("Use Detected", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None or clicked == box.button(QMessageBox.StandardButton.Cancel):
            return

        fluid = detected_fluid
        soundfont = detected_soundfont
        if clicked == choose_button:
            fluid_start = str(Path(fluid).parent) if fluid else str(Path.home())
            selected_fluid, _ = QFileDialog.getOpenFileName(
                self, "Choose FluidSynth executable", fluid_start,
                "FluidSynth executable (fluidsynth.exe fluidsynth);;All files (*)",
            )
            if not selected_fluid:
                return
            fluid_path = Path(selected_fluid).expanduser().resolve(strict=False)
            if not fluid_path.is_file():
                QMessageBox.warning(self, "Audio Setup", "The selected FluidSynth executable does not exist.")
                return

            sf_start = str(Path(soundfont).parent) if soundfont else str(Path.home())
            selected_soundfont, _ = QFileDialog.getOpenFileName(
                self, "Choose SoundFont", sf_start,
                "SoundFont (*.sf2 *.sf3);;All files (*)",
            )
            if not selected_soundfont:
                return
            soundfont_path = Path(selected_soundfont).expanduser().resolve(strict=False)
            if not soundfont_path.is_file() or soundfont_path.suffix.casefold() not in {".sf2", ".sf3"}:
                QMessageBox.warning(self, "Audio Setup", "Choose an existing .sf2 or .sf3 SoundFont file.")
                return
            fluid = str(fluid_path)
            soundfont = str(soundfont_path)
        elif use_button is None or clicked != use_button:
            return

        if not fluid or not soundfont:
            QMessageBox.warning(
                self, "Audio Setup",
                "Library audition needs both FluidSynth and a SoundFont. Choose both files before playing MIDI."
            )
            return

        settings.set("fluidsynth_path", fluid)
        settings.set("last_soundfont_path", soundfont)
        settings.set("last_soundfont_directory", str(Path(soundfont).parent))

        # The current controller captured the FluidSynth executable at Browser
        # construction. Recreate it while stopped so Play immediately uses the
        # newly saved independent-Library audio configuration.
        try:
            self.audition.close()
            self.audition = LibraryAuditionController(self.app_root)
            self.volume_slider.setValue(self.audition.volume_percent)
        except Exception as exc:
            QMessageBox.warning(
                self, "Audio Setup",
                f"Audio settings were saved, but the audition controller could not be refreshed: {type(exc).__name__}: {exc}\n\n"
                "Close and reopen the Library Browser before playing MIDI."
            )
            return
        QMessageBox.information(
            self, "Audio Setup",
            "Library audition audio is configured. The next Play request will verify FluidSynth and the selected SoundFont at runtime."
        )

    @staticmethod
    def _friendly_audition_error(error: str) -> str:
        raw = " ".join(str(error or "").split())
        lower = raw.casefold()
        if any(token in lower for token in ("eoferror", "unexpected end of file", "invalid midi", "invalid mthd", "running status")):
            return (
                "This MIDI appears incomplete or damaged, so Library audition could not play it. "
                "Use Show in Folder to inspect or replace the source. If you want to keep it but remove it from the active Library, choose Quarantine; "
                "choose Delete only if you intentionally want the source file removed from disk."
            )
        if "no soundfont" in lower:
            return raw.split(": ", 1)[-1]
        if "fluidsynth" in lower:
            return (
                "Library audition could not start FluidSynth. Use Audio Setup… beside the Volume control to choose a working "
                "FluidSynth executable and SoundFont, then try Play again."
            )
        # Hide internal wrapper class names from the normal user message while
        # retaining the useful operation detail.
        for prefix in ("LibraryAuditionError: ", "RuntimeError: "):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        return raw or "MIDI audition could not start."

    def _start_transport_task(self, action: str, func, *, generation: int | None = None) -> int:
        if action != "volume":
            self._begin_busy("transport", self.playback_status.text() or "Audio transport is working…")
            self.audio_setup_button.setEnabled(False)
        if generation is None:
            self._transport_generation += 1
            generation = self._transport_generation

        def run() -> None:
            result = None
            error = ""
            try:
                result = func()
            except LibraryAuditionCancelled:
                # A stale Play completion after Stop/new Play is expected and
                # must not produce an alarming modal.
                error = "cancelled"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._transport_lock:
                    self._transport_threads.discard(threading.current_thread())
                    self._transport_actions.discard(str(action))
            if not self._closing:
                self.transportFinished.emit(int(generation), str(action), result, error)

        thread = threading.Thread(target=run, name=f"MidiLibraryAudition-{action}", daemon=True)
        with self._transport_lock:
            self._transport_threads.add(thread)
            self._transport_actions.add(str(action))
        thread.start()
        return int(generation)

    def _play_double_clicked(self, row: int, column: int) -> None:
        """Audition the row the user actually double-clicked.

        ExtendedSelection can otherwise leave several rows selected and make a
        double-click act on an older first selection. A direct gesture must
        have one unambiguous musical target.
        """
        if not (0 <= int(row) < self.table.rowCount()):
            return
        self.table.clearSelection()
        self.table.selectRow(int(row))
        self.table.setCurrentCell(int(row), max(0, int(column)))
        self._update_selection_controls()
        self._play_selected()

    def _show_selected_actions_menu(self) -> None:
        item = self.table.currentItem()
        if item is None:
            indexes = self.table.selectionModel().selectedRows()
            if indexes:
                item = self.table.item(indexes[0].row(), 0)
        if item is None:
            return
        position = self.table.visualItemRect(item).center()
        self._show_table_context_menu(position)

    def _toggle_play_pause(self) -> None:
        """Single Play/Pause control bound to the highlighted MIDI row."""
        try:
            state = self.audition.state
        except Exception:
            state = PlaybackState.STOPPED
        if state == PlaybackState.PLAYING:
            self._pause()
            return
        if state == PlaybackState.PAUSED:
            selected = self._selected_path()
            if selected and selected == self.audition.current_path:
                self._resume()
                return
        self._play_selected()

    def _play_selected(self) -> None:
        rows = self._selected_rows()
        if len(rows) != 1:
            if rows:
                QMessageBox.information(self, "Library Audition", "Select exactly one MIDI row to audition.")
            return
        path, quick_status, _deep_status, _attempts, _suspicious = rows[0]
        lifecycle = {p: eligibility for p, _life, eligibility in self._selected_lifecycle_rows()}
        if not saved_row_can_attempt_audition(quick_status=quick_status, eligibility=lifecycle.get(path, "")):
            QMessageBox.information(
                self, "Library Audition",
                "This MIDI is not currently eligible for direct Library audition. Use Actions… to review its saved state."
            )
            return
        if quick_status == "failed":
            QMessageBox.information(
                self, "Library Audition",
                "MIDI Library cannot audition this file because Fast Scan could not read it. "
                "Use Open in Default App to try another MIDI player, or Show in Folder to inspect/replace the source file."
            )
            return
        self._transport_generation += 1
        generation = self._transport_generation
        title = self._title_for_path(path)
        self.playback_status.setText(f"Preparing · {Path(path).name}")
        # Stop remains available while a large MIDI is being prepared. Capture
        # the exact selected path/title now; never resolve selection again from
        # the asynchronous worker after the user may have highlighted another row.
        self.play_pause_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._start_transport_task(
            "play",
            lambda target_path=path, target_title=title: self.audition.play(target_path, target_title),
            generation=generation,
        )

    def _pause(self) -> None:
        generation = self._transport_generation
        self.playback_status.setText("Pausing…")
        self.play_pause_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._start_transport_task("pause", self.audition.pause, generation=generation)

    def _resume(self) -> None:
        self._transport_generation += 1
        generation = self._transport_generation
        self.playback_status.setText("Resuming…")
        self.play_pause_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._start_transport_task("resume", self.audition.resume, generation=generation)

    def _stop(self, *, silent: bool = False) -> None:
        # Invalidate slow pending Play preparation immediately on the UI thread.
        # The worker then silences notes, proves the scheduler is gone, and fully
        # closes the Browser-owned synth process before reporting Stopped.
        self._transport_generation += 1
        generation = self._transport_generation
        self.audition.cancel_pending_start()
        self._paused_tick = 0
        self.playback_status.setText("Stopping audio…")
        self.play_pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self._start_transport_task("stop-silent" if silent else "stop", self.audition.stop, generation=generation)

    def _set_volume(self, value: int) -> None:
        self.volume_label.setText(f"{int(value)}%")
        # Coalesce slider motion and perform the synth command away from Qt.
        self._volume_timer.start()

    def _apply_volume_async(self) -> None:
        value = int(self.volume_slider.value())
        generation = self._transport_generation
        self._start_transport_task("volume", lambda: self.audition.set_volume(value), generation=generation)

    def _apply_transport_result(self, generation: int, action: str, result: object, error: str) -> None:
        if self._closing:
            return
        if action != "volume":
            self._end_busy("transport")
        if action == "volume":
            if error and error != "cancelled":
                self.playback_status.setText("Volume update failed")
            return
        # A Stop/new Play makes older Play/Pause/Resume completions stale.
        if generation != self._transport_generation:
            return
        if error and error != "cancelled":
            pending_remove = self._pending_remove
            self._pending_remove = None
            self.playback_status.setText("Playback error")
            self._update_transport(PlaybackState.STOPPED)
            if pending_remove is not None:
                QMessageBox.warning(
                    self, "MIDI File Still In Use",
                    "The selected file was not changed because audio Stop could not be verified.\n\n" + error,
                )
            else:
                QMessageBox.warning(self, "MIDI Audition", self._friendly_audition_error(error))
            return
        if action.startswith("stop"):
            self.playback_status.setText("Stopped")
            self._update_transport(PlaybackState.STOPPED)
            pending_remove = self._pending_remove
            self._pending_remove = None
            if pending_remove is not None:
                paths, permanent = pending_remove
                self.removeRequested.emit(paths, permanent)
            return
        snapshot = result if isinstance(result, PlaybackSnapshot) else None
        state = snapshot.state if snapshot is not None else self.audition.state
        if action == "pause":
            self._paused_tick = int(snapshot.tick or 0) if snapshot is not None else self._paused_tick
            self.playback_status.setText("Paused")
            self._update_transport(PlaybackState.PAUSED)
        elif action in {"play", "resume"}:
            if state == PlaybackState.PLAYING:
                self.playback_status.setText("Playing")
                self._update_transport(PlaybackState.PLAYING)
            else:
                self.playback_status.setText("Stopped")
                self._update_transport(PlaybackState.STOPPED)

    def _qa_close_manager_from_playback(self, state: str) -> None:
        """Target-Windows-only Close trigger after real Browser audio reaches state."""
        if self._qa_playback_close_requested:
            return
        self._qa_playback_close_requested = True
        diagnostics: dict[str, object] = {}
        try:
            diagnostics = dict(self.audition.engine.audio_diagnostics() or {})
        except Exception:
            diagnostics = {}
        synth_pid = int(diagnostics.get("pid") or 0)
        try:
            marker = self.app_root / "user-data" / "logs" / "qa-browser-playback-state.txt"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                " ".join((
                    f"state={state}",
                    f"synth_pid={synth_pid}",
                    f"play_text={self.play_pause_button.text()}",
                    f"play_state={self.play_pause_button.property('transportState') or ''}",
                    f"play_enabled={int(self.play_pause_button.isEnabled())}",
                    f"stop_state={self.stop_button.property('transportState') or ''}",
                    f"stop_enabled={int(self.stop_button.isEnabled())}",
                )),
                encoding="utf-8",
            )
        except OSError:
            pass
        try:
            settle_ms = max(0, int(os.environ.get("MIDI_LIBRARY_QA_PLAYBACK_CLOSE_SETTLE_MS", "1000") or 1000))
        except ValueError:
            settle_ms = 1000
        parent = self.parentWidget()
        if parent is not None:
            QTimer.singleShot(settle_ms, parent.close)

    def _poll_playback(self) -> None:
        try:
            snapshot = self.audition.snapshot()
        except Exception:
            return
        if snapshot.finished:
            self.playback_status.setText("Finished · releasing audio…")
            # Natural completion obeys the same zero-playback-process rule as
            # explicit Stop.  Do not leave an idle FluidSynth child behind.
            self._stop(silent=True)
        elif snapshot.state == PlaybackState.PLAYING:
            self.playback_status.setText(f"Playing · {snapshot.percent}%")
            if self._qa_playback_mode == "paused" and not self._qa_playback_pause_requested:
                self._qa_playback_pause_requested = True
                QTimer.singleShot(0, self._pause)
            elif self._qa_playback_mode == "playing":
                self._qa_close_manager_from_playback("playing")
        elif snapshot.state == PlaybackState.PAUSED and self._qa_playback_mode == "paused":
            self._qa_close_manager_from_playback("paused")

    def _update_transport(self, state: PlaybackState) -> None:
        rows = self._selected_rows()
        lifecycle = {path: eligibility for path, _life, eligibility in self._selected_lifecycle_rows()}
        one_playable = (
            len(rows) == 1
            and saved_row_can_attempt_audition(
                quick_status=rows[0][1], eligibility=lifecycle.get(rows[0][0], "")
            )
            and not self._maintenance_busy
        )
        with self._transport_lock:
            active_actions = set(self._transport_actions)
        control_busy = any(action != "volume" for action in active_actions)
        stopping = any(action.startswith("stop") for action in active_actions)

        if state == PlaybackState.PLAYING:
            self.play_pause_button.setText("Pause")
            self.play_pause_button.setObjectName("PlayButton")
            self.play_pause_button.setProperty("transportState", "pause")
            self.stop_button.setProperty("transportState", "active")
            self.play_pause_button.setToolTip("Pause the currently playing MIDI.")
            self.play_pause_button.setEnabled(not control_busy)
            # Stop is always the emergency escape while audio is playing, even
            # if a Pause request is still crossing the worker boundary.
            self.stop_button.setEnabled(not stopping)
        elif state == PlaybackState.PAUSED:
            self.play_pause_button.setText("Play")
            self.play_pause_button.setObjectName("PlayButton")
            self.play_pause_button.setProperty("transportState", "resume")
            self.stop_button.setProperty("transportState", "active")
            self.play_pause_button.setToolTip("Resume the paused MIDI, or play the newly highlighted MIDI from the beginning.")
            self.play_pause_button.setEnabled(one_playable and not control_busy)
            self.stop_button.setEnabled(not stopping)
        else:
            self.play_pause_button.setText("Play")
            self.play_pause_button.setObjectName("PlayButton")
            self.play_pause_button.setProperty("transportState", "play")
            self.stop_button.setProperty("transportState", "active" if control_busy and not stopping else "idle")
            if control_busy:
                self.play_pause_button.setToolTip("Audio transport is preparing or stopping. Use Stop while playback is preparing.")
            else:
                self.play_pause_button.setToolTip(
                    "Play the highlighted MIDI row." if one_playable
                    else "Select exactly one readable MIDI row to audition it in MIDI Library."
                )
            self.play_pause_button.setEnabled(one_playable and not control_busy)
            # During a slow Play preparation, Stop must remain available so the
            # user can cancel immediately. During an active Stop it stays off.
            self.stop_button.setEnabled(control_busy and not stopping)

        self.audio_setup_button.setEnabled(
            state == PlaybackState.STOPPED and not control_busy and not self._closing and not self._maintenance_busy
        )
        # Re-polish because dynamic properties drive active transport styling.
        self.play_pause_button.style().unpolish(self.play_pause_button)
        self.play_pause_button.style().polish(self.play_pause_button)
        self.stop_button.style().unpolish(self.stop_button)
        self.stop_button.style().polish(self.stop_button)

    @property
    def close_in_progress(self) -> bool:
        return bool(self._close_cleanup_inflight)

    def _run_close_cleanup(self) -> None:
        error = ""
        deadline = time.monotonic() + BROWSER_CLOSE_HARD_SECONDS

        def join_owned(threads: tuple[threading.Thread, ...], label: str) -> None:
            current = threading.current_thread()
            for thread in threads:
                if thread is current or not thread.is_alive():
                    continue
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    break
                thread.join(timeout=remaining)
            alive = [thread.name for thread in threads if thread is not current and thread.is_alive()]
            if alive:
                raise RuntimeError(f"{label} did not finish within the shared Browser close deadline: " + ", ".join(alive))

        try:
            self._pending_remove = None
            self.audition.close()
            with self._transport_lock:
                transport_threads = tuple(self._transport_threads)
            join_owned(transport_threads, "Audition worker(s)")
            with self._query_lock:
                query_threads = tuple(self._query_threads)
            # Catalogue readers own live SQLite connections for the duration of
            # each query. On Windows those handles prevent database rename/delete
            # and therefore must be terminal before the Browser is allowed to
            # close. Cleanup runs off the Qt thread, so using the remaining shared
            # close budget here preserves UI responsiveness without leaking a read
            # worker past dialog lifetime.
            join_owned(query_threads, "Catalogue read worker(s)")
            with self._shell_lock:
                shell_threads = tuple(self._shell_threads)
            # Shell integration workers do not own the Library database, but they
            # are still Browser-owned threads. Require the same bounded terminal
            # ownership contract rather than silently abandoning them on close.
            join_owned(shell_threads, "Windows shell worker(s)")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        self.closeCleanupFinished.emit(not bool(error), error)

    def _finish_close_cleanup(self, success: bool, error: str) -> None:
        self._close_cleanup_inflight = False
        if success:
            self._close_cleanup_complete = True
            self.operation_status.setText("Audio stopped · closing Library Browser…")
            self.busy_indicator.setVisible(False)
            QTimer.singleShot(0, self.close)
            return
        self._closing = False
        self._playback_timer.start()
        self.busy_indicator.setVisible(False)
        self.operation_status.setText("")
        self._update_transport(self.audition.state)
        QMessageBox.warning(
            self,
            "Library Browser Cleanup Still Running",
            (error or "Library Browser cleanup could not be verified.")
            + "\n\nThe Library Browser will remain open so cleanup can be retried. "
              "Use Stop if audio is active, then close the Browser again.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._close_cleanup_complete:
            self._closing = True
            self._search_timer.stop()
            self._playback_timer.stop()
            self._volume_timer.stop()
            self._busy_delay_timer.stop()
            self.busy_indicator.setVisible(False)
            event.accept()
            return
        if self._close_cleanup_inflight:
            event.ignore()
            return

        # Never block Qt while FluidSynth/scheduler/query ownership is being
        # released.  The dialog remains repaintable with explicit status until
        # the background cleanup proves every owned resource terminal.
        self._closing = True
        self._close_cleanup_inflight = True
        self._search_timer.stop()
        self._playback_timer.stop()
        self._volume_timer.stop()
        self._busy_delay_timer.stop()
        self.audition.cancel_pending_start()
        self.playback_status.setText("Stopping audio…")
        self.operation_status.setText("Closing Library Browser · stopping audio and background reads…")
        self.busy_indicator.setVisible(True)
        self.play_pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        threading.Thread(
            target=self._run_close_cleanup,
            name="MidiLibraryBrowserCloseCleanup",
            daemon=True,
        ).start()
        event.ignore()

