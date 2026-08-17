"""PySide6 desktop app for batch REDline still renders."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QRect, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QImage, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .batch import (
    BatchAssignment,
    BatchOptions,
    BatchGroup,
    BatchScanResult,
    ClipOrganization,
    PreviewContext,
    build_batch_groups,
    build_batch_scan_result,
    build_batch_output_path,
    build_job_plan_from_context,
    build_preview_context,
    describe_source_selection,
    discover_media_clips,
    regroup_batch_groups,
    suggest_clip_family,
    suggest_subgroup,
)
from .contact_sheet import ContactSheetItem, build_contact_sheet_pdf
from .frame_index import FrameTargetRequest, MatchSelectionState, OverlapSubset, _timecode_for_absolute_frame
from .media_render import build_replay_command, render_plan_item
from .media_render import render_plan_items_parallel
from .redline import RedlinePaths, RenderSettings, probe_redline, shell_join
from .settings import AppSettings, SettingsStore

APP_TITLE = "R3D Contact Sheet"
MIN_OUTPUT_BYTES = 2048
REPLAY_SCRIPT_NAME = "r3dcontactsheet_last_batch.sh"
CONTACT_SHEET_NAME = "r3dcontactsheet_contact_sheet.pdf"
BUILD_MARKER = "VERIFIED UI BUILD 2026-04-01-RDCFIX"
WINDOW_TITLE = f"{APP_TITLE} - {BUILD_MARKER}"
LOGO_NAME = "r3dcontactsheet_logo.png"


def _format_preview_progress(processed: int, total: int) -> str:
    if total <= 0:
        return "Analyzing clips..."
    percent = int((processed / total) * 100) if processed else 0
    return f"Analyzing clips: {processed} / {total} ({percent}%)"


def _format_batch_progress(label: str, processed: int, total: int) -> str:
    if total <= 0:
        return f"{label}: waiting..."
    percent = int((processed / total) * 100) if processed else 0
    return f"{label}: {processed} / {total} ({percent}%)"


def _format_eta(elapsed: float, processed: int, total: int) -> str:
    if processed <= 0 or total <= 0 or processed >= total:
        return "ETA: calculating..."
    remaining = max(0.0, (elapsed / processed) * (total - processed))
    minutes, seconds = divmod(int(round(remaining)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"ETA: {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"ETA: {minutes:02d}:{seconds:02d}"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1380, 980)
        self.setMinimumSize(1220, 860)

        self.store = SettingsStore()
        self.settings = self.store.load()
        self.plan = []
        self.worker: threading.Thread | None = None
        self.preview_worker: threading.Thread | None = None
        self.batch_scan_worker: threading.Thread | None = None
        self.batch_render_worker: threading.Thread | None = None
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.run_started_at = 0.0
        self.batch_scan_started_at = 0.0
        self.batch_render_started_at = 0.0
        self.last_replay_script: Path | None = None
        self.last_contact_sheet_pdf: Path | None = None
        self._preview_idle_text = "Preview Working Set"
        self._batch_scan_idle_text = "Scan Batches"
        self._batch_render_idle_text = "Render Selected Batches"
        self.preview_context: PreviewContext | None = None
        self.active_subset: OverlapSubset | None = None
        self.current_selection: MatchSelectionState = MatchSelectionState(None, None, "auto")
        self.batch_groups: list[BatchGroup] = []
        self.batch_scan_result: BatchScanResult | None = None
        self.batch_assignments: list[BatchAssignment] = []
        self.redline_ready = False
        self.preview_table_updating = False
        self.batch_table_updating = False
        self.batch_detail_table_updating = False
        self.unassigned_table_updating = False
        self._last_probe_log_message: str | None = None
        self.metadata_cache: dict[tuple[str, str, int, int, str], object] = {}

        self._build_ui()
        QTimer.singleShot(0, self._initialize_batch_splitters)
        self._apply_settings_to_ui()
        self._connect_signals()
        self._refresh_redline_probe()
        self._refresh_source_state()
        self._refresh_batch_state()
        self._refresh_frame_mode_summary()
        self._log_startup_banner()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_events)
        self.poll_timer.start(150)

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        root.addWidget(self._build_header())

        tabs = QTabWidget()
        self.tabs = tabs
        setup_tab = QWidget()
        setup_layout = QVBoxLayout(setup_tab)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(12)
        setup_layout.addWidget(self._build_settings_section())

        controls_row = QHBoxLayout()
        controls_row.setSpacing(12)
        controls_row.addWidget(self._build_render_section(), 1)
        controls_row.addStretch(1)
        setup_layout.addLayout(controls_row)
        setup_layout.addStretch(1)

        preview_tab = QWidget()
        preview_layout = QVBoxLayout(preview_tab)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(12)
        preview_layout.addWidget(self._build_preview_source_section())
        preview_layout.addWidget(self._build_preview_section(), 1)

        batching_tab = QWidget()
        batching_layout = QVBoxLayout(batching_tab)
        batching_layout.setContentsMargins(0, 0, 0, 0)
        batching_layout.setSpacing(12)
        batching_layout.addWidget(self._build_batching_section(), 1)

        render_tab = QWidget()
        render_layout = QVBoxLayout(render_tab)
        render_layout.setContentsMargins(0, 0, 0, 0)
        render_layout.setSpacing(12)
        render_layout.addWidget(self._build_render_status_section(), 1)
        render_layout.addWidget(self._build_footer())

        tabs.addTab(setup_tab, "Settings")
        tabs.addTab(preview_tab, "Preview")
        tabs.addTab(batching_tab, "Batching")
        tabs.addTab(render_tab, "Render")
        root.addWidget(tabs, 1)

        save_action = QAction("Remember Settings", self)
        save_action.triggered.connect(self._save_settings)
        self.addAction(save_action)

    def _build_header(self) -> QWidget:
        box = QFrame()
        box.setObjectName("compactHeader")
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        box.setMaximumHeight(56)

        layout = QHBoxLayout(box)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)

        title_label = QLabel(APP_TITLE)
        title_label.setObjectName("headerTitle")
        layout.addWidget(title_label, 0, Qt.AlignVCenter)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setFrameShadow(QFrame.Plain)
        divider.setObjectName("headerDivider")
        layout.addWidget(divider, 0, Qt.AlignVCenter)

        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        layout.addWidget(self.status_dot, 0, Qt.AlignVCenter)

        self.status_label = QLabel("Checking REDline…")
        self.status_label.setWordWrap(False)
        self.status_label.setObjectName("summaryBright")
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.status_label, 1, Qt.AlignVCenter)
        return box

    def _initialize_batch_splitters(self) -> None:
        main_splitter = getattr(self, "batch_main_splitter", None)
        if main_splitter is not None:
            main_splitter.setSizes([940])

    def _build_settings_section(self) -> QGroupBox:
        box = self._section_box("1. Application Settings")
        outer = QHBoxLayout(box)
        outer.setSpacing(12)

        left = QWidget(box)
        left_layout = QGridLayout(left)
        left_layout.setColumnStretch(1, 1)
        left_layout.setHorizontalSpacing(10)
        left_layout.setVerticalSpacing(8)

        self.redline_edit = QLineEdit()
        self.redline_edit.setClearButtonEnabled(True)
        self.redline_button = QPushButton("Choose REDline")
        left_layout.addWidget(QLabel("REDline executable"), 0, 0)
        left_layout.addWidget(self.redline_edit, 0, 1)
        left_layout.addWidget(self.redline_button, 0, 2)

        self.output_edit = QLineEdit()
        self.output_edit.setClearButtonEnabled(True)
        self.output_button = QPushButton("Choose Output")
        left_layout.addWidget(QLabel("Default output folder"), 1, 0)
        left_layout.addWidget(self.output_edit, 1, 1)
        left_layout.addWidget(self.output_button, 1, 2)

        left_layout.addWidget(QLabel("Theme"), 2, 0)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.setMaximumWidth(180)
        left_layout.addWidget(self.theme_combo, 2, 1, alignment=Qt.AlignLeft)

        settings_help = QLabel(
            "Use this page for environment and persistent defaults only. Preview and Batching own the active workflow controls."
        )
        settings_help.setWordWrap(True)
        settings_help.setObjectName("muted")
        left_layout.addWidget(settings_help, 3, 0, 1, 3)

        organization_box = self._section_box("Source Organization Defaults")
        organization_layout = QGridLayout(organization_box)
        organization_layout.setColumnStretch(3, 1)
        organization_layout.setHorizontalSpacing(10)
        organization_layout.setVerticalSpacing(6)
        organization_layout.addWidget(QLabel("Preview grouping default"), 0, 0)
        self.group_mode_combo = QComboBox()
        self.group_mode_combo.addItems(["flat", "parent_folder", "reel_prefix"])
        organization_layout.addWidget(self.group_mode_combo, 0, 1)
        self.alphabetize_check = QCheckBox("Alphabetize preview/source discovery lists")
        organization_layout.addWidget(self.alphabetize_check, 0, 2, 1, 2)
        organization_layout.addWidget(QLabel("Optional custom group"), 1, 0)
        self.custom_group_edit = QLineEdit()
        self.custom_group_edit.setPlaceholderText("Optional mini-array label")
        organization_layout.addWidget(self.custom_group_edit, 1, 1, 1, 3)
        left_layout.addWidget(organization_box, 4, 0, 1, 3)

        right_column = QVBoxLayout()
        right_column.setSpacing(12)

        summary = self._section_box("2. Output And Defaults")
        summary_layout = QVBoxLayout(summary)
        self.settings_output_label = QLabel("Default output folder will be used for preview and render unless you change it.")
        self.settings_output_label.setWordWrap(True)
        self.settings_output_label.setObjectName("summaryBright")
        self.settings_render_label = QLabel("Render defaults are configured below on this page and applied across workflows.")
        self.settings_render_label.setWordWrap(True)
        self.settings_render_label.setObjectName("mutedBlue")
        summary_layout.addWidget(self.settings_output_label)
        summary_layout.addWidget(self.settings_render_label)
        summary.setMinimumWidth(400)

        system_box = self._section_box("System Status")
        system_layout = QVBoxLayout(system_box)
        system_layout.setSpacing(6)
        self.redline_state_label = QLabel("REDline status will appear here after startup.")
        self.redline_state_label.setWordWrap(True)
        self.redline_state_label.setObjectName("summaryBright")
        self.redline_path_label = QLabel("Configured REDline path will appear here.")
        self.redline_path_label.setWordWrap(True)
        self.redline_path_label.setObjectName("muted")
        self.config_status_label = QLabel(self.store.last_status)
        self.config_status_label.setWordWrap(True)
        self.config_status_label.setObjectName("mutedBlue")
        system_layout.addWidget(self.redline_state_label)
        system_layout.addWidget(self.redline_path_label)
        system_layout.addWidget(self.config_status_label)
        right_column.addWidget(summary)
        right_column.addWidget(system_box)

        outer.addWidget(left, 3)
        outer.addLayout(right_column, 2)
        return box

    def _build_preview_source_section(self) -> QGroupBox:
        box = self._section_box("2. Preview Source")
        outer = QHBoxLayout(box)
        outer.setSpacing(12)

        left = QWidget(box)
        left_layout = QGridLayout(left)
        left_layout.setColumnStretch(1, 1)
        left_layout.setHorizontalSpacing(10)
        left_layout.setVerticalSpacing(8)

        self.source_edit = QLineEdit()
        self.source_edit.setClearButtonEnabled(True)
        left_layout.addWidget(QLabel("Preview source"), 0, 0)
        left_layout.addWidget(self.source_edit, 0, 1, 1, 2)

        source_button_row = QHBoxLayout()
        self.choose_source_button = QPushButton("Choose Preview Source...")
        source_button_row.addWidget(self.choose_source_button)
        source_button_row.addStretch(1)
        left_layout.addLayout(source_button_row, 1, 0, 1, 3)

        self.sync_mode_combo = QComboBox()
        self.sync_mode_combo.addItems(["Sync On", "Sync Off"])
        self.sync_mode_combo.setMaximumWidth(180)
        left_layout.addWidget(QLabel("Preview sync mode"), 2, 0)
        left_layout.addWidget(self.sync_mode_combo, 2, 1, alignment=Qt.AlignLeft)

        source_help = QLabel(
            "Use Preview to inspect one working set at a time. Choose a single clip, an RDC package, or a source folder you want to validate before rendering."
        )
        source_help.setWordWrap(True)
        source_help.setObjectName("muted")
        left_layout.addWidget(source_help, 3, 0, 1, 3)

        right_column = QVBoxLayout()
        right_column.setSpacing(12)

        summary = self._section_box("Preview Source Summary")
        summary_layout = QVBoxLayout(summary)
        self.source_type_label = QLabel("No preview source selected.")
        self.source_type_label.setWordWrap(True)
        self.source_count_label = QLabel("Resolved clip count will appear here after you choose a preview source.")
        self.source_count_label.setWordWrap(True)
        self.source_count_label.setObjectName("mutedBlue")
        self.source_detail_label = QLabel("Detected grouping and provider details will appear here after selection.")
        self.source_detail_label.setWordWrap(True)
        self.source_detail_label.setObjectName("muted")
        summary_layout.addWidget(self.source_type_label)
        summary_layout.addWidget(self.source_count_label)
        summary_layout.addWidget(self.source_detail_label)
        summary.setMinimumWidth(400)

        right_column.addWidget(summary)

        outer.addWidget(left, 3)
        outer.addLayout(right_column, 2)
        return box

    def _build_frame_section(self) -> QGroupBox:
        box = self._section_box("Preview Match Controls")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        mode_row = QGridLayout()
        mode_row.setHorizontalSpacing(10)
        mode_row.addWidget(QLabel("Preview mode"), 0, 0)
        mode_row.addWidget(self.sync_mode_combo, 0, 1)
        mode_row.setColumnStretch(3, 1)
        layout.addLayout(mode_row)

        intro = QLabel("Use Preview mode to inspect one array or working set. Sync On keeps overlap diagnostics visible. Sync Off still chooses coherent frames quietly for presentation-oriented review.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        layout.addWidget(intro)
        self.target_timecode_edit = QLineEdit()
        self.fps_edit = QLineEdit()
        self.target_timecode_edit.setMaximumWidth(240)
        self.fps_edit.setMaximumWidth(140)
        self.target_timecode_edit.setPlaceholderText("Optional")
        self.fps_edit.setPlaceholderText("Only if supplied")

        self.advanced_timecode_check = QCheckBox("Advanced: target a specific metadata timecode")
        layout.addWidget(self.advanced_timecode_check)

        self.advanced_timecode_panel = QFrame()
        advanced_layout = QGridLayout(self.advanced_timecode_panel)
        advanced_layout.setContentsMargins(12, 8, 12, 8)
        advanced_layout.setHorizontalSpacing(10)
        advanced_layout.setVerticalSpacing(8)
        advanced_layout.setColumnStretch(1, 1)
        advanced_layout.addWidget(QLabel("Target timecode"), 0, 0)
        advanced_layout.addWidget(self.target_timecode_edit, 0, 1)
        advanced_layout.addWidget(QLabel("FPS (only if you were given one)"), 1, 0)
        advanced_layout.addWidget(self.fps_edit, 1, 1, alignment=Qt.AlignLeft)
        self.drop_frame_check = QCheckBox("Drop-frame timecode")
        advanced_layout.addWidget(self.drop_frame_check, 2, 0, 1, 2)
        self.advanced_timecode_panel.setVisible(False)
        layout.addWidget(self.advanced_timecode_panel)

        self.metadata_mode_check = QCheckBox("Use RED metadata look (recommended)")
        layout.addWidget(self.metadata_mode_check)

        guidance = QLabel(
            "Recommended workflow: choose the preview source, inspect the overlap analysis and chosen match frame, then render once the per-clip status looks correct."
        )
        guidance.setWordWrap(True)
        guidance.setObjectName("muted")
        layout.addWidget(guidance)
        self.frame_mode_label = QLabel("Per-clip match frame and match timecode will be resolved automatically from clip metadata.")
        self.frame_mode_label.setWordWrap(True)
        self.frame_mode_label.setObjectName("summaryBright")
        layout.addWidget(self.frame_mode_label)

        workflow = QLabel("Workflow: select source, preview the resolved metadata sync, confirm the matching moment or subset status, then render.")
        workflow.setWordWrap(True)
        workflow.setObjectName("mutedBlue")
        layout.addWidget(workflow)
        layout.addStretch(1)
        return box

    def _build_render_section(self) -> QGroupBox:
        box = self._section_box("4. Render Settings")
        layout = QGridLayout(box)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.color_sci_edit = QLineEdit()
        self.output_tone_map_edit = QLineEdit()
        self.roll_off_edit = QLineEdit()
        self.output_gamma_edit = QLineEdit()
        self.render_res_edit = QLineEdit()
        self.resize_x_edit = QLineEdit()
        self.resize_y_edit = QLineEdit()

        self._add_grid_entry(layout, 0, 0, "Color science", self.color_sci_edit)
        self._add_grid_entry(layout, 0, 2, "Output tone map", self.output_tone_map_edit)
        self._add_grid_entry(layout, 1, 0, "Roll off", self.roll_off_edit)
        self._add_grid_entry(layout, 1, 2, "Output gamma", self.output_gamma_edit)
        self._add_grid_entry(layout, 2, 0, "Render res", self.render_res_edit)
        self._add_grid_entry(layout, 2, 2, "Resize X", self.resize_x_edit)
        self._add_grid_entry(layout, 3, 0, "Resize Y", self.resize_y_edit)

        note = QLabel(
            "Supported REDline values in this build: color science 0/1/2/3, tone map 0/1/2/3, roll off 0/1/2/3/4, render res 1/2/3/4/8, gamma 32. The verified baseline remains 3 / 1 / 2 / 32."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note, 4, 0, 1, 4)
        legend = QLabel(
            "Legend: 3 = IPP2 color science, tone map 1 = medium, roll off 2 = default, render res 4 = quarter. Extra REDline arguments are not exposed here yet."
        )
        legend.setWordWrap(True)
        legend.setObjectName("mutedBlue")
        layout.addWidget(legend, 5, 0, 1, 4)
        return box

    def _build_preview_section(self) -> QGroupBox:
        box = self._section_box("3. Preview Validation")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setAlignment(Qt.AlignTop)
        top_row.addWidget(self._build_frame_section(), 1)
        preview_action_column = QVBoxLayout()
        preview_action_column.setSpacing(4)
        self.preview_button = QPushButton(self._preview_idle_text)
        preview_action_column.addWidget(self.preview_button, 0, Qt.AlignRight)
        self.preview_help_label = QLabel("Click once — scanning media may take a moment.")
        self.preview_help_label.setObjectName("muted")
        self.preview_help_label.setWordWrap(True)
        self.preview_help_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.preview_help_label.setMaximumWidth(260)
        preview_action_column.addWidget(self.preview_help_label, 0, Qt.AlignRight)
        self.preview_progress_label = QLabel("")
        self.preview_progress_label.setObjectName("mutedBlue")
        self.preview_progress_label.setWordWrap(True)
        self.preview_progress_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.preview_progress_label.setMaximumWidth(260)
        preview_action_column.addWidget(self.preview_progress_label, 0, Qt.AlignRight)
        top_row.addLayout(preview_action_column)
        layout.addLayout(top_row)

        analysis_row = QHBoxLayout()
        analysis_row.setSpacing(12)

        overlap_box = self._section_box("Overlap Analysis")
        overlap_layout = QVBoxLayout(overlap_box)
        overlap_layout.setSpacing(6)
        self.overlap_subset_label = QLabel("No overlap subset selected yet.")
        self.overlap_subset_label.setObjectName("summaryBright")
        self.overlap_subset_label.setWordWrap(True)
        self.overlap_range_label = QLabel("Overlap range will appear after preview.")
        self.overlap_range_label.setObjectName("muted")
        self.overlap_range_label.setWordWrap(True)
        self.overlap_counts_label = QLabel("Subset counts and earlier/later diagnostics will appear after preview.")
        self.overlap_counts_label.setObjectName("mutedBlue")
        self.overlap_counts_label.setWordWrap(True)
        self.overlap_alternates_label = QLabel("Alternate overlap subsets will appear here when available.")
        self.overlap_alternates_label.setObjectName("muted")
        self.overlap_alternates_label.setWordWrap(True)
        overlap_layout.addWidget(self.overlap_subset_label)
        overlap_layout.addWidget(self.overlap_range_label)
        overlap_layout.addWidget(self.overlap_counts_label)
        overlap_layout.addWidget(self.overlap_alternates_label)
        analysis_row.addWidget(overlap_box, 1)

        selection_box = self._section_box("Match Frame Selection")
        selection_layout = QGridLayout(selection_box)
        selection_layout.setHorizontalSpacing(10)
        selection_layout.setVerticalSpacing(8)
        selection_layout.setColumnStretch(1, 1)
        selection_layout.setColumnStretch(3, 1)

        self.preview_subset_combo = QComboBox()
        self.preview_subset_combo.setEnabled(False)
        self.selection_mode_combo = QComboBox()
        self.selection_mode_combo.addItems(["Auto", "Start of Range", "Middle of Range", "End of Range", "Custom"])
        self.selection_mode_combo.setEnabled(False)
        self.selected_match_label = QLabel("Current selected match will appear after preview.")
        self.selected_match_label.setObjectName("summaryBright")
        self.selected_match_label.setWordWrap(True)
        self.recommended_match_label = QLabel("Recommended match will appear after preview.")
        self.recommended_match_label.setObjectName("muted")
        self.recommended_match_label.setWordWrap(True)
        self.match_slider = QSlider(Qt.Horizontal)
        self.match_slider.setEnabled(False)
        self.match_slider.setMinimum(0)
        self.match_slider.setMaximum(0)
        self.match_slider_value_label = QLabel("No valid overlap range yet.")
        self.match_slider_value_label.setObjectName("mutedBlue")
        self.match_slider_value_label.setWordWrap(True)
        self.match_step_back_10 = QPushButton("-10")
        self.match_step_back_1 = QPushButton("-1")
        self.match_step_forward_1 = QPushButton("+1")
        self.match_step_forward_10 = QPushButton("+10")
        for button in (self.match_step_back_10, self.match_step_back_1, self.match_step_forward_1, self.match_step_forward_10):
            button.setEnabled(False)

        selection_layout.addWidget(QLabel("Overlap subset"), 0, 0)
        selection_layout.addWidget(self.preview_subset_combo, 0, 1)
        selection_layout.addWidget(QLabel("Selection mode"), 0, 2)
        selection_layout.addWidget(self.selection_mode_combo, 0, 3)
        selection_layout.addWidget(self.selected_match_label, 1, 0, 1, 4)
        selection_layout.addWidget(self.recommended_match_label, 2, 0, 1, 4)
        selection_layout.addWidget(self.match_slider, 3, 0, 1, 4)
        step_row = QHBoxLayout()
        step_row.setSpacing(8)
        step_row.addWidget(self.match_step_back_10)
        step_row.addWidget(self.match_step_back_1)
        step_row.addWidget(self.match_step_forward_1)
        step_row.addWidget(self.match_step_forward_10)
        step_row.addStretch(1)
        selection_layout.addLayout(step_row, 4, 0, 1, 4)
        selection_layout.addWidget(self.match_slider_value_label, 5, 0, 1, 4)
        analysis_row.addWidget(selection_box, 1)

        layout.addLayout(analysis_row)

        self.preview_table = QTableWidget(0, 14)
        self.preview_table.setHorizontalHeaderLabels(
            [
                "Camera Label",
                "Group",
                "Subgroup",
                "Manufacturer",
                "Format",
                "Clip",
                "FPS",
                "Source TC In",
                "Source TC Out",
                "Clip Frame",
                "Match Timecode",
                "Match Status",
                "Range Relation",
                "Sync Basis",
            ]
        )
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked | QAbstractItemView.EditKeyPressed
        )
        self.preview_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.preview_table.setAlternatingRowColors(True)
        layout.addWidget(self.preview_table, 1)
        return box

    def _build_batching_section(self) -> QGroupBox:
        box = self._section_box("4. Batching")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        picker_box = self._section_box("Batch Media Root And Output")
        picker_layout = QGridLayout(picker_box)
        picker_layout.setColumnStretch(1, 1)
        picker_layout.setHorizontalSpacing(10)
        picker_layout.setVerticalSpacing(6)

        self.batch_source_edit = QLineEdit()
        self.batch_source_edit.setClearButtonEnabled(True)
        self.batch_source_button = QPushButton("Choose Batch Source")
        picker_layout.addWidget(QLabel("Batch media root"), 0, 0)
        picker_layout.addWidget(self.batch_source_edit, 0, 1)
        picker_layout.addWidget(self.batch_source_button, 0, 2)

        self.batch_output_edit = QLineEdit()
        self.batch_output_edit.setClearButtonEnabled(True)
        self.batch_output_button = QPushButton("Choose Batch Output")
        picker_layout.addWidget(QLabel("Output folder"), 1, 0)
        picker_layout.addWidget(self.batch_output_edit, 1, 1)
        picker_layout.addWidget(self.batch_output_button, 1, 2)

        picker_help = QLabel(
            "Use Batching for large-folder discovery and reel/clip planning. Clips are grouped by reel number and numeric clip number from the filename, and unresolved mixed-media clips stay available for operator assignment."
        )
        picker_help.setObjectName("muted")
        picker_help.setWordWrap(True)
        picker_layout.addWidget(picker_help, 2, 0, 1, 3)
        layout.addWidget(picker_box)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)

        scan_controls = self._section_box("Batch Planning")
        scan_layout = QGridLayout(scan_controls)
        scan_layout.setColumnStretch(3, 1)
        scan_layout.setHorizontalSpacing(10)
        scan_layout.setVerticalSpacing(6)

        scan_layout.addWidget(QLabel("Sync mode"), 0, 0)
        self.batch_sync_mode_combo = QComboBox()
        self.batch_sync_mode_combo.addItems(["Sync On", "Sync Off"])
        scan_layout.addWidget(self.batch_sync_mode_combo, 0, 1)
        scan_layout.addWidget(QLabel("Batch auto-frame strategy"), 0, 2)
        self.batch_selection_mode_combo = QComboBox()
        self.batch_selection_mode_combo.addItems(["Auto", "Start of Range", "Middle of Range", "End of Range"])
        scan_layout.addWidget(self.batch_selection_mode_combo, 0, 3)

        self.batch_scan_button = QPushButton(self._batch_scan_idle_text)
        scan_layout.addWidget(self.batch_scan_button, 1, 0, 1, 2)
        self.batch_scan_help_label = QLabel("Click once — scanning large media trees may take a moment.")
        self.batch_scan_help_label.setObjectName("muted")
        self.batch_scan_help_label.setWordWrap(True)
        scan_layout.addWidget(self.batch_scan_help_label, 1, 2, 1, 2)

        self.batch_phase_label = QLabel("Phase: idle")
        self.batch_phase_label.setObjectName("summaryBright")
        self.batch_phase_label.setWordWrap(True)
        self.batch_counter_label = QLabel("Scanned clips: 0 • Built batches: 0")
        self.batch_counter_label.setObjectName("mutedBlue")
        self.batch_counter_label.setWordWrap(True)
        self.batch_eta_label = QLabel("ETA: --:--")
        self.batch_eta_label.setObjectName("muted")
        self.batch_current_label = QLabel("Current batch: none")
        self.batch_current_label.setObjectName("muted")
        self.batch_current_label.setWordWrap(True)
        scan_layout.addWidget(self.batch_phase_label, 2, 0, 1, 4)
        scan_layout.addWidget(self.batch_counter_label, 3, 0, 1, 4)
        scan_layout.addWidget(self.batch_eta_label, 4, 0, 1, 2)
        scan_layout.addWidget(self.batch_current_label, 4, 2, 1, 2)
        controls_row.addWidget(scan_controls, 3)

        progress_box = self._section_box("Batch Progress")
        progress_layout = QVBoxLayout(progress_box)
        progress_layout.setSpacing(8)
        self.batch_progress_bar = QProgressBar()
        self.batch_progress_bar.setMinimum(0)
        self.batch_progress_bar.setMaximum(1)
        self.batch_progress_bar.setValue(0)
        progress_layout.addWidget(self.batch_progress_bar)
        self.batch_render_button = QPushButton(self._batch_render_idle_text)
        self.batch_render_button.setEnabled(False)
        progress_layout.addWidget(self.batch_render_button)
        self.batch_render_note_label = QLabel("Scan a top-level media folder to preview reel/clip contact sheets before rendering.")
        self.batch_render_note_label.setObjectName("muted")
        self.batch_render_note_label.setWordWrap(True)
        progress_layout.addWidget(self.batch_render_note_label)
        progress_layout.addStretch(1)
        controls_row.addWidget(progress_box, 2)

        layout.addLayout(controls_row)

        self.batch_table = QTableWidget(0, 9)
        self.batch_table.setHorizontalHeaderLabels(
            [
                "Include",
                "Reel",
                "Clip",
                "Source Clips",
                "Cameras Found",
                "Clip Families",
                "Subgroups",
                "Sync Summary",
                "Output PDF",
            ]
        )
        self.batch_table.horizontalHeader().setStretchLastSection(True)
        self.batch_table.verticalHeader().setVisible(False)
        self.batch_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.batch_table.setAlternatingRowColors(True)
        self.batch_table.setMinimumHeight(440)

        batch_table_box = self._section_box("Batch Groups")
        batch_table_layout = QVBoxLayout(batch_table_box)
        batch_table_layout.setContentsMargins(8, 10, 8, 8)
        batch_table_layout.setSpacing(8)
        batch_table_layout.addWidget(self.batch_table)

        inline_actions = QHBoxLayout()
        inline_actions.setSpacing(8)
        self.batch_selected_summary_label = QLabel("Select a batch row to inspect its contributing source clips.")
        self.batch_selected_summary_label.setObjectName("mutedBlue")
        self.batch_selected_summary_label.setWordWrap(True)
        inline_actions.addWidget(self.batch_selected_summary_label, 1)

        self.batch_details_button = QPushButton("Batch Sources")
        self.batch_details_button.setEnabled(False)
        inline_actions.addWidget(self.batch_details_button)

        self.batch_needs_assignment_button = QPushButton("Needs Assignment (0)")
        self.batch_needs_assignment_button.setEnabled(False)
        inline_actions.addWidget(self.batch_needs_assignment_button)

        self.batch_log_toggle_button = QPushButton("Batch Log")
        self.batch_log_toggle_button.setEnabled(False)
        inline_actions.addWidget(self.batch_log_toggle_button)

        self.batch_hide_details_button = QPushButton("Hide Details")
        self.batch_hide_details_button.setVisible(False)
        inline_actions.addWidget(self.batch_hide_details_button)
        batch_table_layout.addLayout(inline_actions)

        detail_box = self._section_box("Batch Source Labels")
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(8, 10, 8, 8)
        self.batch_detail_help_label = QLabel(
            "Suggested Clip Family and Subgroup values are editable per source clip. These labels affect final contact sheet organization, not sync truth."
        )
        self.batch_detail_help_label.setObjectName("muted")
        self.batch_detail_help_label.setWordWrap(True)
        detail_layout.addWidget(self.batch_detail_help_label)
        self.batch_detail_table = QTableWidget(0, 6)
        self.batch_detail_table.setHorizontalHeaderLabels(
            [
                "Source Clip",
                "Camera Label",
                "Clip Family",
                "Subgroup",
                "Manufacturer",
                "Format",
            ]
        )
        self.batch_detail_table.horizontalHeader().setStretchLastSection(True)
        self.batch_detail_table.verticalHeader().setVisible(False)
        self.batch_detail_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.batch_detail_table.setAlternatingRowColors(True)
        self.batch_detail_table.setMinimumHeight(120)
        detail_layout.addWidget(self.batch_detail_table)

        unassigned_box = self._section_box("Needs Batch Assignment")
        unassigned_layout = QVBoxLayout(unassigned_box)
        unassigned_layout.setContentsMargins(8, 10, 8, 8)
        self.unassigned_help_label = QLabel(
            "Unparsed clips stay visible here. Enter Reel and Clip manually, or assign the selected rows to the batch highlighted above."
        )
        self.unassigned_help_label.setObjectName("muted")
        self.unassigned_help_label.setWordWrap(True)
        unassigned_layout.addWidget(self.unassigned_help_label)
        assign_button_row = QHBoxLayout()
        assign_button_row.setSpacing(8)
        self.assign_to_selected_batch_button = QPushButton("Assign To Selected Batch")
        self.clear_assignment_button = QPushButton("Clear Assignment")
        assign_button_row.addWidget(self.assign_to_selected_batch_button)
        assign_button_row.addWidget(self.clear_assignment_button)
        assign_button_row.addStretch(1)
        unassigned_layout.addLayout(assign_button_row)
        self.unassigned_table = QTableWidget(0, 10)
        self.unassigned_table.setHorizontalHeaderLabels(
            [
                "Include",
                "Source Clip",
                "Assignment",
                "Reel",
                "Clip",
                "Clip Family",
                "Subgroup",
                "Metadata Source",
                "Confidence",
                "Reason",
            ]
        )
        self.unassigned_table.horizontalHeader().setStretchLastSection(True)
        self.unassigned_table.verticalHeader().setVisible(False)
        self.unassigned_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.unassigned_table.setAlternatingRowColors(True)
        self.unassigned_table.setMinimumHeight(150)
        unassigned_layout.addWidget(self.unassigned_table)

        log_box = self._section_box("Batch Log")
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(8, 10, 8, 8)
        self.batch_log_text = QPlainTextEdit()
        self.batch_log_text.setReadOnly(True)
        self.batch_log_text.setMinimumHeight(72)
        log_layout.addWidget(self.batch_log_text)

        self.batch_inline_summary_label = QLabel(
            "This embedded area stays hidden until you inspect a batch, review unresolved clips, or open the batch log."
        )
        self.batch_inline_summary_label.setObjectName("muted")
        self.batch_inline_summary_label.setWordWrap(True)

        self.batch_detail_tabs = QTabWidget()
        self.batch_detail_tabs.addTab(detail_box, "Batch Sources")
        self.batch_detail_tabs.addTab(unassigned_box, "Needs Assignment")
        self.batch_detail_tabs.addTab(log_box, "Batch Log")

        self.batch_inline_panel = QFrame()
        self.batch_inline_panel.setObjectName("embeddedBatchPanel")
        inline_panel_layout = QVBoxLayout(self.batch_inline_panel)
        inline_panel_layout.setContentsMargins(0, 0, 0, 0)
        inline_panel_layout.setSpacing(6)
        inline_panel_layout.addWidget(self.batch_inline_summary_label)
        inline_panel_layout.addWidget(self.batch_detail_tabs, 1)
        self.batch_inline_panel.setVisible(False)
        batch_table_layout.addWidget(self.batch_inline_panel)

        self.batch_main_splitter = QSplitter(Qt.Vertical)
        self.batch_main_splitter.setChildrenCollapsible(False)
        self.batch_main_splitter.addWidget(batch_table_box)
        layout.addWidget(self.batch_main_splitter, 1)
        return box

    def _build_render_status_section(self) -> QGroupBox:
        box = self._section_box("5. Render Queue And Results")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        self.summary_label = QLabel("No preview jobs or batch renders are active yet.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("summaryBright")
        layout.addWidget(self.summary_label)

        self.replay_label = QLabel("Replay script and output summary will appear here after preview or render.")
        self.replay_label.setWordWrap(True)
        self.replay_label.setObjectName("muted")
        layout.addWidget(self.replay_label)

        self.preview_note_label = QLabel("Use this page to run the current queue, watch progress, and review render-specific output events.")
        self.preview_note_label.setWordWrap(True)
        self.preview_note_label.setObjectName("mutedBlue")
        layout.addWidget(self.preview_note_label)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(320)
        layout.addWidget(self.log_text, 1)
        return box

    def _build_footer(self) -> QGroupBox:
        box = self._section_box("Render Controls")
        layout = QHBoxLayout(box)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar, 1)
        self.run_button = QPushButton("Build Contact Sheet PDF")
        layout.addWidget(self.run_button)
        return box

    def _section_box(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setObjectName("sectionBox")
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        return box

    def _add_grid_entry(self, layout: QGridLayout, row: int, column: int, label: str, widget: QLineEdit) -> None:
        layout.addWidget(QLabel(label), row, column)
        layout.addWidget(widget, row, column + 1)

    def _connect_signals(self) -> None:
        self.redline_button.clicked.connect(self._choose_redline)
        self.choose_source_button.clicked.connect(self._choose_input_folder)
        self.output_button.clicked.connect(self._choose_output)
        self.batch_source_button.clicked.connect(self._choose_batch_input_folder)
        self.batch_output_button.clicked.connect(self._choose_batch_output)
        self.batch_scan_button.clicked.connect(self.scan_batches)
        self.batch_render_button.clicked.connect(self.run_batch_jobs)
        self.preview_button.clicked.connect(self.preview_jobs)
        self.run_button.clicked.connect(self.run_jobs)
        self.preview_subset_combo.currentIndexChanged.connect(self._apply_preview_selection)
        self.selection_mode_combo.currentTextChanged.connect(self._apply_preview_selection)
        self.match_slider.valueChanged.connect(self._on_match_slider_changed)
        self.match_step_back_10.clicked.connect(lambda: self._step_match_slider(-10))
        self.match_step_back_1.clicked.connect(lambda: self._step_match_slider(-1))
        self.match_step_forward_1.clicked.connect(lambda: self._step_match_slider(1))
        self.match_step_forward_10.clicked.connect(lambda: self._step_match_slider(10))

        self.redline_edit.editingFinished.connect(self._refresh_redline_probe)
        self.source_edit.editingFinished.connect(self._refresh_source_state)
        self.output_edit.editingFinished.connect(self._refresh_source_state)
        self.batch_source_edit.editingFinished.connect(self._refresh_batch_state)
        self.batch_output_edit.editingFinished.connect(self._refresh_batch_state)
        self.group_mode_combo.currentTextChanged.connect(self._refresh_source_state)
        self.custom_group_edit.textChanged.connect(lambda *_: self._refresh_source_state())
        self.alphabetize_check.stateChanged.connect(lambda *_: self._refresh_source_state())
        self.sync_mode_combo.currentTextChanged.connect(lambda *_: self._refresh_frame_mode_summary())
        self.theme_combo.currentTextChanged.connect(lambda *_: self._refresh_frame_mode_summary())
        self.batch_sync_mode_combo.currentTextChanged.connect(lambda *_: self._refresh_batch_state())
        self.batch_selection_mode_combo.currentTextChanged.connect(lambda *_: self._refresh_batch_state())
        self.preview_table.itemChanged.connect(self._on_preview_table_item_changed)
        self.batch_table.itemChanged.connect(self._on_batch_table_item_changed)
        self.batch_table.itemSelectionChanged.connect(self._on_batch_selection_changed)
        self.batch_detail_table.itemChanged.connect(self._on_batch_detail_item_changed)
        self.unassigned_table.itemChanged.connect(self._on_unassigned_table_item_changed)
        self.assign_to_selected_batch_button.clicked.connect(self._assign_unparsed_to_selected_batch)
        self.clear_assignment_button.clicked.connect(self._clear_selected_unassigned_assignments)
        self.batch_details_button.clicked.connect(self._show_selected_batch_inspector)
        self.batch_needs_assignment_button.clicked.connect(self._show_needs_assignment_inspector)
        self.batch_log_toggle_button.clicked.connect(self._show_batch_log_inspector)
        self.batch_hide_details_button.clicked.connect(lambda: self._set_batch_detail_visible(False))

        for widget in (self.target_timecode_edit, self.fps_edit):
            widget.textChanged.connect(self._refresh_frame_mode_summary)
        self.drop_frame_check.stateChanged.connect(lambda *_: self._refresh_frame_mode_summary())
        self.advanced_timecode_check.toggled.connect(self._toggle_advanced_timecode)

    def _apply_settings_to_ui(self) -> None:
        self.redline_edit.setText(self.settings.redline_path)
        self._set_path_field(self.redline_edit, self.settings.redline_path)
        self._set_path_field(self.source_edit, self.settings.last_input_path)
        self._set_path_field(self.output_edit, self.settings.last_output_path)
        self._set_path_field(self.batch_source_edit, getattr(self.settings, "batch_input_path", self.settings.last_input_path))
        self._set_path_field(self.batch_output_edit, getattr(self.settings, "batch_output_path", self.settings.last_output_path))
        self.target_timecode_edit.setText(self.settings.target_timecode)
        self.fps_edit.setText("" if self.settings.fps == "23.976" and not self.settings.target_timecode else self.settings.fps)
        self.drop_frame_check.setChecked(self.settings.drop_frame)
        advanced_on = bool(self.settings.target_timecode.strip())
        self.advanced_timecode_check.setChecked(advanced_on)
        self.advanced_timecode_panel.setVisible(advanced_on)
        self.color_sci_edit.setText(str(self.settings.color_sci_version))
        self.output_tone_map_edit.setText(str(self.settings.output_tone_map))
        self.roll_off_edit.setText(str(self.settings.roll_off))
        self.output_gamma_edit.setText(str(self.settings.output_gamma_curve))
        self.render_res_edit.setText(str(self.settings.render_res))
        self.resize_x_edit.setText(self.settings.resize_x)
        self.resize_y_edit.setText(self.settings.resize_y)
        safe_group_mode = self.settings.group_mode if self.settings.group_mode in {"flat", "parent_folder", "reel_prefix"} else "flat"
        self.group_mode_combo.setCurrentText(safe_group_mode)
        self.custom_group_edit.setText(getattr(self.settings, "custom_group_name", ""))
        self.alphabetize_check.setChecked(self.settings.alphabetize)
        self.metadata_mode_check.setChecked(self.settings.metadata_mode)
        self.sync_mode_combo.setCurrentText("Sync Off" if getattr(self.settings, "sync_mode", "sync_on") == "sync_off" else "Sync On")
        self.theme_combo.setCurrentText("Light" if getattr(self.settings, "theme_name", "dark") == "light" else "Dark")
        self.batch_sync_mode_combo.setCurrentText("Sync Off" if getattr(self.settings, "batch_sync_mode", getattr(self.settings, "sync_mode", "sync_on")) == "sync_off" else "Sync On")
        batch_selection = getattr(self.settings, "batch_selection_mode", "auto")
        selection_map = {
            "auto": "Auto",
            "start": "Start of Range",
            "middle": "Middle of Range",
            "end": "End of Range",
        }
        self.batch_selection_mode_combo.setCurrentText(selection_map.get(batch_selection, "Auto"))

    def _choose_redline(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose REDline executable")
        if path:
            self._set_path_field(self.redline_edit, path)
            self._save_settings()
            self._refresh_redline_probe()

    def _choose_input_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose R3D Clip", filter="RED Clips (*.R3D);;All Files (*)")
        if path:
            self._set_input_path(path)

    def _choose_rdc_package(self) -> None:
        self._append_log("RDC chooser: using native RDC chooser path.")
        selection = ""
        if sys_platform_is_macos():
            status, selection = _choose_directory_macos(
                "Choose a RED clip package (.RDC). You do not need to show package contents."
            )
            if status == "cancelled":
                self._append_log("RDC chooser cancelled.")
                return
            if status == "failed":
                self._append_log("RDC chooser failed.")
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    "The native RDC package chooser failed.\n\nPlease try again or relaunch the app.",
                )
                return
        else:
            selection = QFileDialog.getExistingDirectory(self, "Choose RDC Package")

        if selection:
            path = Path(selection).expanduser().resolve()
            if path.suffix.lower() != ".rdc":
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    f"That selection is not an RDC package.\n\nChoose the folder ending in .RDC, not a regular folder.\n\n{path}",
                )
                return
            self._set_input_path(str(path))

    def _choose_input_folder(self) -> None:
        selection = QFileDialog.getExistingDirectory(self, "Choose Folder / Reel")
        if selection:
            self._set_input_path(selection)

    def _choose_output(self) -> None:
        selection = QFileDialog.getExistingDirectory(self, "Choose Output Folder")
        if selection:
            path = Path(selection).expanduser().resolve()
            self._set_path_field(self.output_edit, str(path))
            self._append_log(f"Output folder: {path}")
            self._save_settings()

    def _choose_batch_input_folder(self) -> None:
        selection = QFileDialog.getExistingDirectory(self, "Choose Top-Level Media Folder")
        if selection:
            path = Path(selection).expanduser().resolve()
            self._set_path_field(self.batch_source_edit, str(path))
            self._append_log(f"Batch source folder: {path}")
            self._refresh_batch_state()
            self._save_settings()

    def _choose_batch_output(self) -> None:
        selection = QFileDialog.getExistingDirectory(self, "Choose Batch Output Folder")
        if selection:
            path = Path(selection).expanduser().resolve()
            self._set_path_field(self.batch_output_edit, str(path))
            self._append_log(f"Batch output folder: {path}")
            self._refresh_batch_state()
            self._save_settings()

    def _set_input_path(self, value: str) -> None:
        resolved = Path(value).expanduser().resolve()
        self._set_path_field(self.source_edit, str(resolved))
        self._refresh_source_state()
        self._append_log(describe_source_selection(resolved))
        self._save_settings()

    def _refresh_source_state(self) -> None:
        value = self.source_edit.text().strip()
        if not value:
            self.source_type_label.setText("No source selected.")
            self.source_count_label.setText("Resolved clip count will appear here after selection.")
            self.source_detail_label.setText("Detected reel and grouping details will appear here after selection.")
            return

        path = Path(value).expanduser().resolve()
        self.source_type_label.setText(describe_source_selection(path))
        if not path.exists():
            self.source_count_label.setText("The selected source does not exist.")
            self.source_detail_label.setText("")
            return
        try:
            clips = discover_media_clips(path, group_mode=self.group_mode_combo.currentText(), alphabetize=self.alphabetize_check.isChecked())  # type: ignore[arg-type]
            if path.is_file():
                mode_text = "Single clip"
            elif path.suffix.lower() == ".rdc":
                mode_text = "RDC package"
            else:
                mode_text = "Folder / Reel"
            self.source_count_label.setText(
                f"Resolved {len(clips)} clip(s) from {mode_text}. Grouping mode: {self.group_mode_combo.currentText()}."
            )
            reel_group = self._display_group_value(self._logical_common_value([clip.clip_name.split("_")[0] for clip in clips]))
            clip_group = self._display_group_value(self._logical_common_value([clip.clip_name.split("_")[1] for clip in clips if "_" in clip.clip_name]))
            providers = sorted({clip.provider_kind for clip in clips})
            self.source_detail_label.setText(
                f"Selected path: {path}. Reel group: {reel_group}. Clip group: {clip_group}. Providers: {', '.join(providers)}. Alphabetized: {'Yes' if self.alphabetize_check.isChecked() else 'No'}."
            )
        except Exception as exc:
            self.source_count_label.setText(str(exc))
            self.source_detail_label.setText("")

    def _refresh_batch_state(self) -> None:
        source_text = self.batch_source_edit.text().strip()
        output_text = self.batch_output_edit.text().strip()
        if not source_text:
            self.batch_phase_label.setText("Phase: idle")
            self.batch_counter_label.setText("Scanned clips: 0 • Built batches: 0")
            self.batch_current_label.setText("Current batch: choose a top-level media folder.")
            self.batch_detail_help_label.setText("Suggested Clip Family and Subgroup values are editable per source clip. These labels affect final contact sheet organization, not sync truth.")
            self.batch_render_button.setEnabled(False)
            self._update_batch_secondary_actions()
            return
        source_path = Path(source_text).expanduser().resolve()
        if not source_path.exists():
            self.batch_phase_label.setText("Phase: source missing")
            self.batch_current_label.setText(f"Current batch: source does not exist at {source_path}")
            self.batch_render_button.setEnabled(False)
            self._update_batch_secondary_actions()
            return
        if output_text:
            output_path = Path(output_text).expanduser().resolve()
            if output_path == source_path or source_path in output_path.parents:
                self.batch_phase_label.setText("Phase: output must be separate")
                self.batch_current_label.setText("Current batch: choose an output folder outside the media source tree.")
                self.batch_render_button.setEnabled(False)
                self._update_batch_secondary_actions()
                return
        self.batch_phase_label.setText("Phase: ready to scan")
        self.batch_current_label.setText(
            f"Current batch mode: {'Sync Off' if self.batch_sync_mode_combo.currentText() == 'Sync Off' else 'Sync On'} • {self.batch_selection_mode_combo.currentText()}."
        )
        self._update_batch_secondary_actions()

    def _on_batch_table_item_changed(self, _item: QTableWidgetItem) -> None:
        if self.batch_table_updating:
            return
        self._refresh_batch_render_button_state()

    def _on_batch_selection_changed(self) -> None:
        self._render_selected_batch_detail()
        self._update_assignment_help()
        self._update_batch_secondary_actions()
        if self._selected_batch_id():
            self._show_selected_batch_inspector()

    def _on_batch_detail_item_changed(self, item: QTableWidgetItem) -> None:
        if self.batch_detail_table_updating:
            return
        path_text = item.data(Qt.UserRole)
        if not path_text:
            return
        clip_path = Path(path_text)
        assignment = self._assignment_for_path(clip_path)
        if assignment is None:
            return
        value = item.text().strip()
        if item.column() == 1:
            assignment.camera_label = value
        elif item.column() == 2:
            assignment.clip_family = value
        elif item.column() == 3:
            assignment.subgroup_name = value
        else:
            return
        selected_batch_id = self._selected_batch_id()
        self._regroup_batch_assignments()
        if selected_batch_id:
            self._restore_batch_selection(selected_batch_id)
        self._render_selected_batch_detail()
        self._render_unassigned_table()

    def _on_unassigned_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self.unassigned_table_updating:
            return
        path_text = item.data(Qt.UserRole)
        if not path_text:
            return
        assignment = self._assignment_for_path(Path(path_text))
        if assignment is None:
            return
        if item.column() == 0:
            assignment.assignment_state = "needs_assignment" if item.checkState() == Qt.Checked else "excluded_by_operator"
        elif item.column() == 3:
            assignment.reel_number = item.text().strip() or None
        elif item.column() == 4:
            assignment.clip_number = item.text().strip() or None
        elif item.column() == 5:
            assignment.clip_family = item.text().strip()
        elif item.column() == 6:
            assignment.subgroup_name = item.text().strip()
        else:
            return
        self._normalize_assignment_state(assignment)
        self._regroup_batch_assignments()
        self._render_unassigned_table()
        self._update_assignment_help()

    def _assign_unparsed_to_selected_batch(self) -> None:
        batch_id = self._selected_batch_id()
        if not batch_id:
            return
        group = next((item for item in self.batch_groups if item.batch_id == batch_id), None)
        if group is None:
            return
        rows = self.unassigned_table.selectionModel().selectedRows() if self.unassigned_table.selectionModel() else []
        if not rows:
            return
        for model_index in rows:
            item = self.unassigned_table.item(model_index.row(), 1)
            if item is None:
                continue
            assignment = self._assignment_for_path(Path(item.data(Qt.UserRole)))
            if assignment is None:
                continue
            assignment.reel_number = group.reel_number
            assignment.clip_number = group.clip_number
            assignment.assignment_state = "auto_assigned"
            self._normalize_assignment_state(assignment)
        self._regroup_batch_assignments()
        self._restore_batch_selection(batch_id)
        self._render_unassigned_table()
        self._update_assignment_help()
        self._show_selected_batch_inspector()

    def _clear_selected_unassigned_assignments(self) -> None:
        rows = self.unassigned_table.selectionModel().selectedRows() if self.unassigned_table.selectionModel() else []
        for model_index in rows:
            item = self.unassigned_table.item(model_index.row(), 1)
            if item is None:
                continue
            assignment = self._assignment_for_path(Path(item.data(Qt.UserRole)))
            if assignment is None:
                continue
            assignment.reel_number = None
            assignment.clip_number = None
            assignment.assignment_state = "needs_assignment"
        self._regroup_batch_assignments()
        self._render_unassigned_table()
        self._update_assignment_help()

    def _assignment_for_path(self, clip_path: Path) -> BatchAssignment | None:
        resolved = clip_path.resolve()
        for assignment in self.batch_assignments:
            if assignment.clip.source_path.resolve() == resolved:
                return assignment
        return None

    def _normalize_assignment_state(self, assignment: BatchAssignment) -> None:
        if assignment.assignment_state == "excluded_by_operator":
            return
        if assignment.reel_number and assignment.clip_number:
            assignment.assignment_state = "auto_assigned"
            assignment.reel_number = assignment.reel_number.zfill(3)
            assignment.clip_number = assignment.clip_number.zfill(3)
            return
        assignment.assignment_state = "needs_assignment"
        if not assignment.reel_number:
            assignment.reel_number = None
        if not assignment.clip_number:
            assignment.clip_number = None

    def _regroup_batch_assignments(self) -> None:
        selected_batch_id = self._selected_batch_id()
        self.batch_groups = regroup_batch_groups(self.batch_assignments)
        self._render_batch_groups()
        if selected_batch_id:
            self._restore_batch_selection(selected_batch_id)
        self._render_selected_batch_detail()
        self._render_unassigned_table()
        self._update_assignment_help()
        self._refresh_batch_state()

    def _render_unassigned_table(self) -> None:
        rows = [
            assignment for assignment in self.batch_assignments
            if assignment.assignment_state in {"needs_assignment", "excluded_by_operator"}
        ]
        rows.sort(key=lambda item: self._display_clip_label(item.clip.clip_name).lower())
        self.unassigned_table_updating = True
        self.unassigned_table.setRowCount(len(rows))
        for row, assignment in enumerate(rows):
            include_item = QTableWidgetItem()
            include_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            include_item.setCheckState(Qt.Unchecked if assignment.assignment_state == "excluded_by_operator" else Qt.Checked)
            include_item.setData(Qt.UserRole, str(assignment.clip.source_path))
            self.unassigned_table.setItem(row, 0, include_item)

            state_label = {
                "auto_assigned": "Auto-assigned",
                "needs_assignment": "Needs batch assignment",
                "excluded_by_operator": "Excluded by operator",
            }[assignment.assignment_state]
            values = (
                assignment.clip.clip_name,
                state_label,
                assignment.reel_number or "",
                assignment.clip_number or "",
                assignment.clip_family,
                assignment.subgroup_name,
                assignment.metadata_source or "unresolved",
                assignment.metadata_confidence or "unresolved",
                assignment.assignment_reason or "manual assignment required",
            )
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, str(assignment.clip.source_path))
                if column in {3, 4, 5, 6}:
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.unassigned_table.setItem(row, column, item)
        self.unassigned_table.resizeColumnsToContents()
        self.unassigned_table_updating = False

    def _selected_unassigned_assignments(self) -> list[BatchAssignment]:
        rows = self.unassigned_table.selectionModel().selectedRows() if self.unassigned_table.selectionModel() else []
        assignments: list[BatchAssignment] = []
        for model_index in rows:
            item = self.unassigned_table.item(model_index.row(), 1)
            if item is None:
                continue
            assignment = self._assignment_for_path(Path(item.data(Qt.UserRole)))
            if assignment is not None:
                assignments.append(assignment)
        return assignments

    def _needs_assignment_count(self) -> int:
        return sum(1 for assignment in self.batch_assignments if assignment.assignment_state == "needs_assignment")

    def _update_assignment_help(self) -> None:
        selected_batch_id = self._selected_batch_id()
        selected_batch = next((group for group in self.batch_groups if group.batch_id == selected_batch_id), None)
        selected_unassigned = self._selected_unassigned_assignments()
        if selected_batch and selected_unassigned:
            self.unassigned_help_label.setText(
                f"Assigning {len(selected_unassigned)} clip(s) into Reel {selected_batch.reel_number} / Clip {selected_batch.clip_number}. You can also type Reel and Clip manually for new batches."
            )
        elif self._needs_assignment_count():
            self.unassigned_help_label.setText(
                f"{self._needs_assignment_count()} clip(s) still need manual batch assignment. Enter Reel and Clip or assign them to the selected batch."
            )
        else:
            self.unassigned_help_label.setText(
                "All discovered clips are currently assigned to a batch. Select any row below to exclude or reassign it if needed."
            )

    def _set_batch_detail_visible(self, visible: bool, tab_index: int | None = None) -> None:
        self.batch_inline_panel.setVisible(visible)
        self.batch_hide_details_button.setVisible(visible)
        if visible and tab_index is not None:
            self.batch_detail_tabs.setCurrentIndex(tab_index)
            if hasattr(self, "batch_main_splitter"):
                self.batch_main_splitter.setSizes([760])
        self._update_batch_secondary_actions()

    def _show_selected_batch_inspector(self) -> None:
        if not self._selected_batch_id():
            return
        self._set_batch_detail_visible(True, 0)

    def _show_needs_assignment_inspector(self) -> None:
        self._set_batch_detail_visible(True, 1)

    def _show_batch_log_inspector(self) -> None:
        self._set_batch_detail_visible(True, 2)

    def _update_batch_secondary_actions(self) -> None:
        selected_batch = self._selected_batch_id()
        needs_assignment = self._needs_assignment_count()
        self.batch_details_button.setEnabled(bool(selected_batch))
        self.batch_needs_assignment_button.setEnabled(bool(self.batch_assignments))
        self.batch_needs_assignment_button.setText(f"Needs Assignment ({needs_assignment})")
        self.batch_log_toggle_button.setEnabled(True)
        if selected_batch:
            group = next((item for item in self.batch_groups if item.batch_id == selected_batch), None)
            if group is not None:
                self.batch_selected_summary_label.setText(
                    f"Selected: Reel {group.reel_number} / Clip {group.clip_number} • {group.source_clip_count} source clip(s) • {group.output_pdf_name}"
                )
                self.batch_inline_summary_label.setText(
                    f"Inspecting Reel {group.reel_number} / Clip {group.clip_number}. Source labels, subgroup organization, and manual batch assignment are edited here on demand."
                )
            else:
                self.batch_selected_summary_label.setText("Select a batch row to inspect its contributing source clips.")
        else:
            self.batch_selected_summary_label.setText("Select a batch row to inspect its contributing source clips.")
            if needs_assignment:
                self.batch_inline_summary_label.setText(
                    f"{needs_assignment} clip(s) still need manual batch assignment. Open the assignment view when you want to place them into a reel/clip batch."
                )
            else:
                self.batch_inline_summary_label.setText(
                    "Details stay out of the way until you need them. Select a batch row or open Needs Assignment to inspect sources."
                )

    def _refresh_frame_mode_summary(self) -> None:
        tc_text = self.target_timecode_edit.text().strip()
        fps_text = self.fps_edit.text().strip()
        mode_text = self.sync_mode_combo.currentText()
        if self.advanced_timecode_check.isChecked() and tc_text:
            drop_text = "drop-frame" if self.drop_frame_check.isChecked() else "non-drop-frame"
            fps_summary = fps_text if fps_text else "clip metadata FPS auto-detect"
            self.frame_mode_label.setText(
                f"{mode_text}: requested matching moment {tc_text}. Timecode basis: {fps_summary} ({drop_text})."
            )
            return
        if mode_text == "Sync Off":
            self.frame_mode_label.setText(
                f"Sync Off: the app will still choose coherent frames quietly, but the contact sheet suppresses written sync diagnostics. Theme: {self.theme_combo.currentText()}."
            )
        else:
            self.frame_mode_label.setText(
                f"Sync On: matching moment will be resolved automatically from truthful clip metadata across sync-eligible clips. Theme: {self.theme_combo.currentText()}."
            )

    def _toggle_advanced_timecode(self, enabled: bool) -> None:
        self.advanced_timecode_panel.setVisible(enabled)
        if not enabled:
            self.target_timecode_edit.clear()
            self.fps_edit.clear()
            self.drop_frame_check.setChecked(False)
        self._refresh_frame_mode_summary()

    def _refresh_redline_probe(self) -> None:
        explicit = Path(self.redline_edit.text()).expanduser() if self.redline_edit.text().strip() else None
        probe = probe_redline(paths=None if explicit is None else RedlinePaths(explicit_path=explicit))
        if probe.available and probe.executable:
            self._set_path_field(self.redline_edit, str(probe.executable))
        self.redline_ready = bool(probe.available and probe.compatible and probe.executable)
        self.preview_button.setEnabled(True)
        self.run_button.setEnabled(True)
        if probe.available and probe.executable:
            self.redline_state_label.setText("REDline found and executable." if probe.compatible else "REDline found, but this build needs a newer REDCINE-X / REDline.")
            self.redline_path_label.setText(f"REDline path: {probe.executable}")
        elif probe.bundle_selected and probe.bundle_path is not None:
            self.redline_state_label.setText("REDCINE-X application bundle selected, but the REDline CLI binary was not resolved.")
            self.redline_path_label.setText(
                f"Bundle: {probe.bundle_path}. Choose the REDline executable inside Contents/MacOS instead of the .app bundle itself."
            )
        elif explicit is not None:
            self.redline_state_label.setText("Selected REDline path is not a valid executable.")
            self.redline_path_label.setText(f"Path: {explicit}")
        else:
            self.redline_state_label.setText("REDline not found. RED clips require REDCINE-X PRO, but generic video preview can still work if metadata and rendering tools are available.")
            self.redline_path_label.setText("Path: Set REDline manually with Choose REDline after installing REDCINE-X PRO.")
        self.config_status_label.setText(self.store.last_status)
        if probe.available and probe.compatible:
            self._set_health_state("yellow", "REDline ready. Preview a source to verify metadata and matched-frame sync.")
        elif probe.bundle_selected:
            self._set_health_state("red", "A REDCINE-X app bundle was selected, but the REDline CLI binary was not resolved.")
        else:
            self._set_health_state("yellow", "REDline unavailable for RED media. Generic video support remains best-effort.")
        self._log_probe_message_once(probe.message)

    def _build_preview_context(self, *, progress_callback=None) -> PreviewContext:
        input_path = self._validate_input_path()
        output_path = self._validate_output_path()
        frame_request = self._build_frame_request()
        settings = self._build_render_settings()
        clips = discover_media_clips(
            input_path,
            group_mode=self.group_mode_combo.currentText(),  # type: ignore[arg-type]
            alphabetize=self.alphabetize_check.isChecked(),
        )
        redline_path = self._validate_redline_path() if any(clip.provider_kind == "red" for clip in clips) else None
        options = BatchOptions(
            output_dir=output_path,
            frame_request=frame_request,
            settings=settings,
            group_mode=self.group_mode_combo.currentText(),  # type: ignore[arg-type]
            alphabetize=self.alphabetize_check.isChecked(),
            custom_group_name=self.custom_group_edit.text().strip() or None,
            redline_exe=str(redline_path) if redline_path else None,
            sync_mode="sync_off" if self.sync_mode_combo.currentText() == "Sync Off" else "sync_on",
        )
        if redline_path:
            self._emit_log(f"Using REDline: {redline_path}")
        self._emit_log(f"Resolved {len(clips)} clip(s) from {input_path}")
        self.last_contact_sheet_pdf = None
        self.run_button.setText("Build Contact Sheet PDF")
        context = build_preview_context(
            clips,
            options,
            metadata_cache=self.metadata_cache,
            progress_callback=progress_callback,
        )
        for metadata in context.metadata_by_clip.values():
            if metadata.metadata_error:
                self._emit_log(metadata.metadata_error)
        return context

    def _apply_preview_context(self, context: PreviewContext) -> None:
        self.preview_context = context
        self.current_selection = context.selection

        self.preview_subset_combo.blockSignals(True)
        self.preview_subset_combo.clear()
        for subset in context.overlap_subsets:
            label = f"{len(subset.clip_paths)} clip(s) • {subset.start_timecode} → {subset.end_timecode}"
            self.preview_subset_combo.addItem(label, subset.subset_id)
        self.preview_subset_combo.setEnabled(bool(context.overlap_subsets))
        active_index = 0
        for index in range(self.preview_subset_combo.count()):
            if self.preview_subset_combo.itemData(index) == context.selection.active_subset_id:
                active_index = index
                break
        if self.preview_subset_combo.count():
            self.preview_subset_combo.setCurrentIndex(active_index)
        self.preview_subset_combo.blockSignals(False)

        self.selection_mode_combo.blockSignals(True)
        mode_map = {
            "auto": "Auto",
            "start": "Start of Range",
            "middle": "Middle of Range",
            "end": "End of Range",
            "custom": "Custom",
        }
        self.selection_mode_combo.setCurrentText(mode_map.get(context.selection.selection_mode, "Auto"))
        self.selection_mode_combo.setEnabled(bool(context.overlap_subsets))
        self.selection_mode_combo.blockSignals(False)
        self._sync_selection_widgets()
        self._refresh_plan_from_selection(write_replay=True)

    def _current_active_subset(self) -> OverlapSubset | None:
        if not self.preview_context:
            return None
        subset_id = self.preview_subset_combo.currentData()
        for subset in self.preview_context.overlap_subsets:
            if subset.subset_id == subset_id:
                return subset
        return self.preview_context.overlap_subsets[0] if self.preview_context.overlap_subsets else None

    def _selection_state_from_widgets(self) -> MatchSelectionState:
        subset = self._current_active_subset()
        if subset is None:
            return MatchSelectionState(None, None, "auto")
        mode_text = self.selection_mode_combo.currentText()
        mode_map = {
            "Auto": "auto",
            "Start of Range": "start",
            "Middle of Range": "middle",
            "End of Range": "end",
            "Custom": "custom",
        }
        mode = mode_map.get(mode_text, "auto")
        if mode == "start":
            selected_abs = subset.start_abs_frame
        elif mode == "middle":
            selected_abs = subset.recommended_abs_frame
        elif mode == "end":
            selected_abs = subset.end_abs_frame
        elif mode == "custom":
            selected_abs = subset.start_abs_frame + self.match_slider.value()
        else:
            selected_abs = subset.recommended_abs_frame
        return MatchSelectionState(
            active_subset_id=subset.subset_id,
            selected_abs_frame=selected_abs,
            selection_mode=mode,
        )

    def _sync_selection_widgets(self) -> None:
        subset = self._current_active_subset()
        has_subset = subset is not None
        self.match_slider.setEnabled(bool(has_subset and self.selection_mode_combo.currentText() == "Custom"))
        for button in (self.match_step_back_10, self.match_step_back_1, self.match_step_forward_1, self.match_step_forward_10):
            button.setEnabled(bool(has_subset and self.selection_mode_combo.currentText() == "Custom"))
        if not has_subset:
            self.selected_match_label.setText("No valid overlap subset is available for shared-frame selection.")
            self.recommended_match_label.setText("Per-clip metadata timecodes will remain authoritative.")
            self.match_slider_value_label.setText("No valid overlap range yet.")
            return
        slider_max = max(0, subset.shared_frame_count - 1)
        self.match_slider.blockSignals(True)
        self.match_slider.setMinimum(0)
        self.match_slider.setMaximum(slider_max)
        selection = self._selection_state_from_widgets()
        selected_abs = selection.selected_abs_frame or subset.recommended_abs_frame
        self.match_slider.setValue(max(0, min(slider_max, selected_abs - subset.start_abs_frame)))
        self.match_slider.blockSignals(False)
        self.current_selection = selection
        selected_tc = self._timecode_for_subset_frame(subset, selected_abs)
        self.selected_match_label.setText(
            f"Current selected match: {selected_tc or 'Unavailable'} (absolute frame {selected_abs})."
        )
        self.recommended_match_label.setText(
            f"Recommended match: {subset.start_timecode if subset.shared_frame_count == 1 else self._timecode_for_subset_frame(subset, subset.recommended_abs_frame) or subset.start_timecode}. "
            f"Valid shared frames: {subset.shared_frame_count}."
        )
        self.match_slider_value_label.setText(
            f"Overlap start {subset.start_timecode} • overlap end {subset.end_timecode} • current offset {selected_abs - subset.start_abs_frame} frame(s)."
        )

    def _timecode_for_subset_frame(self, subset: OverlapSubset, absolute_frame: int) -> str | None:
        if not self.preview_context:
            return None
        for clip_path in subset.clip_paths:
            for metadata in self.preview_context.metadata_by_clip.values():
                if str(metadata.clip_path.resolve()) == clip_path:
                    return _timecode_for_absolute_frame(metadata, absolute_frame)
        return None

    def _apply_preview_selection(self) -> None:
        if not self.preview_context:
            return
        self._sync_selection_widgets()
        self._refresh_plan_from_selection(write_replay=True)

    def _on_match_slider_changed(self, _value: int) -> None:
        if self.selection_mode_combo.currentText() != "Custom":
            return
        self._apply_preview_selection()

    def _step_match_slider(self, delta: int) -> None:
        if not self.match_slider.isEnabled():
            return
        value = self.match_slider.value()
        self.match_slider.setValue(max(self.match_slider.minimum(), min(self.match_slider.maximum(), value + delta)))

    def _refresh_plan_from_selection(self, *, write_replay: bool) -> None:
        if not self.preview_context:
            return
        selection = self._selection_state_from_widgets()
        self.current_selection = selection
        self.plan = build_job_plan_from_context(self.preview_context, selection)
        if write_replay:
            self.last_replay_script = self._write_replay_script(self.plan)
            self.replay_label.setText(f"Replay shell script: {self.last_replay_script}")
        self._render_plan()
        self.summary_label.setText(f"Preview ready: {len(self.plan)} JPEG render job(s) queued for contact sheet assembly.")
        self._update_overlap_analysis()
        self.frame_mode_label.setText(self._frame_mode_summary_from_plan(self.plan))
        self._update_health_from_plan(self.plan)

    def _update_overlap_analysis(self) -> None:
        subset = self._current_active_subset()
        if not self.preview_context or subset is None:
            self.overlap_subset_label.setText("No overlap subset selected yet.")
            self.overlap_range_label.setText("Overlap range will appear after preview.")
            self.overlap_counts_label.setText("Subset counts and earlier/later diagnostics will appear after preview.")
            self.overlap_alternates_label.setText("Alternate overlap subsets will appear here when available.")
            return
        exact = sum(1 for item in self.plan if item.frame_resolution.sync_status == "exact_match")
        earlier = sum(1 for item in self.plan if item.frame_resolution.range_relation == "Earlier Only")
        later = sum(1 for item in self.plan if item.frame_resolution.range_relation == "Later Only")
        outside = max(0, len(self.plan) - len(subset.clip_paths))
        self.overlap_subset_label.setText(
            f"Main subset: {len(subset.clip_paths)} clip(s) • selected frame {self.current_selection.selected_abs_frame or subset.recommended_abs_frame}."
        )
        self.overlap_range_label.setText(
            f"Overlap range: {subset.start_timecode} → {subset.end_timecode}. Valid shared frames: {subset.shared_frame_count}."
        )
        self.overlap_counts_label.setText(
            f"Exact matches: {exact}. Outside active subset: {outside}. Earlier than selected frame: {earlier}. Later than selected frame: {later}."
        )
        alternates = [s for s in self.preview_context.overlap_subsets if s.subset_id != subset.subset_id]
        if alternates:
            preview = " | ".join(
                f"{len(item.clip_paths)} clips • {item.start_timecode} → {item.end_timecode}"
                for item in alternates[:3]
            )
            self.overlap_alternates_label.setText(f"Alternate subsets: {preview}")
        else:
            self.overlap_alternates_label.setText("No alternate overlap subsets were found.")

    def preview_jobs(self) -> None:
        if self.preview_worker and self.preview_worker.is_alive():
            return
        self.last_contact_sheet_pdf = None
        self.run_button.setText("Build Contact Sheet PDF")
        self._set_preview_busy(True, "Building Preview...")
        self.preview_button.repaint()
        self.preview_help_label.repaint()
        self.preview_progress_label.setText(_format_preview_progress(0, 0))
        self.preview_progress_label.repaint()
        QCoreApplication.processEvents()
        time.sleep(0.05)
        QCoreApplication.processEvents()
        self.preview_worker = threading.Thread(target=self._run_preview_worker, daemon=True)
        self.preview_worker.start()

    def run_jobs(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.last_contact_sheet_pdf and self.last_contact_sheet_pdf.exists() and self.run_button.text() == "Open Contact Sheet":
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_contact_sheet_pdf)))
            return
        try:
            if not self.plan:
                self._apply_preview_context(self._build_preview_context())
        except Exception as exc:
            self._append_log(str(exc))
            self._set_health_state("red", "Render setup failed. Check preview, REDline, and output settings.")
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return

        self._save_settings()
        self.run_button.setText("Build Contact Sheet PDF")
        self.run_button.setEnabled(False)
        self.progress_bar.setMaximum(max(len(self.plan), 1))
        self.progress_bar.setValue(0)
        self.run_started_at = time.time()
        self.summary_label.setText(f"Rendering {len(self.plan)} JPEG still job(s)...")
        self.preview_note_label.setText("Rendering matched JPEG stills now. PDF assembly will follow automatically when rendering completes.")
        self._append_log(f"Starting render for {len(self.plan)} JPEG still job(s).")
        if self.last_replay_script is not None:
            self.replay_label.setText(f"Replay shell script: {self.last_replay_script}")
            self._append_log(f"Replay script: {self.last_replay_script}")
        self.worker = threading.Thread(target=self._run_jobs_worker, daemon=True)
        self.worker.start()

    def _run_jobs_worker(self) -> None:
        successes = 0
        failures = 0
        outcomes = render_plan_items_parallel(
            self.plan,
            redline_exe=self.redline_edit.text().strip() or None,
            min_output_bytes=MIN_OUTPUT_BYTES,
            progress_callback=lambda outcome, completed, total: self.event_queue.put(
                ("parallel-progress", {"outcome": outcome, "completed": completed, "total": total})
            ),
        )
        for outcome in outcomes:
            if outcome.error is None:
                successes += 1
            else:
                failures += 1
        elapsed = time.time() - self.run_started_at
        self.event_queue.put(("done", {"successes": successes, "failures": failures, "elapsed": elapsed}))

    def _run_preview_worker(self) -> None:
        try:
            context = self._build_preview_context(
                progress_callback=lambda processed, total, clip, metadata: self.event_queue.put(
                    (
                        "preview-progress",
                        {
                            "processed": processed,
                            "total": total,
                            "clip_name": clip.clip_name,
                        },
                    )
                )
            )
            self.event_queue.put(("preview-ready", context))
        except Exception as exc:
            self.event_queue.put(
                (
                    "preview-failure",
                    {
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
            )

    def _poll_events(self) -> None:
        while True:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if event == "job-success":
                self._on_job_success(payload)
            elif event == "job-failure":
                self._on_job_failure(payload)
            elif event == "progress":
                self._on_progress(payload)
            elif event == "parallel-progress":
                self._on_parallel_progress(payload)
            elif event == "done":
                self._on_done(payload)
            elif event == "preview-progress":
                self._on_preview_progress(payload)
            elif event == "preview-ready":
                self._on_preview_ready(payload)
            elif event == "preview-failure":
                self._on_preview_failure(payload)
            elif event == "batch-scan-progress":
                self._on_batch_scan_progress(payload)
            elif event == "batch-scan-ready":
                self._on_batch_scan_ready(payload)
            elif event == "batch-scan-failure":
                self._on_batch_scan_failure(payload)
            elif event == "batch-render-state":
                self._on_batch_render_state(payload)
            elif event == "batch-render-metadata-progress":
                self._on_batch_render_metadata_progress(payload)
            elif event == "batch-render-job-progress":
                self._on_batch_render_job_progress(payload)
            elif event == "batch-render-done":
                self._on_batch_render_done(payload)
            elif event == "batch-log":
                self._append_batch_log(str(payload))
            elif event == "log":
                self._append_log(str(payload))

    def _on_job_success(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self._append_log(f"Job {data['index']} succeeded in {data['duration']:.1f}s, {data['size']} bytes.")
        self._append_log(f"Command: {data['command']}")

    def _on_job_failure(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self._append_log(f"Job {data['index']} failed.")
        self._append_log(str(data["error"]))

    def _on_parallel_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        outcome = data["outcome"]
        completed = data["completed"]
        total = data["total"]
        if outcome.error is None:
            result = outcome.result
            command_text = shell_join(result.command)
            output_path = str(getattr(result, "job", None).output_file if getattr(result, "job", None) else result.output_path)
            self._append_log(
                f"Job {outcome.index} succeeded in {outcome.duration:.1f}s, {result.output_size} bytes."
            )
            self._append_log(f"Command: {command_text}")
            self._append_log(f"Output: {output_path}")
        else:
            self._append_log(f"Job {outcome.index} failed.")
            self._append_log(str(outcome.error))
        self._on_progress({"completed": completed, "total": total})

    def _on_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        completed = data["completed"]
        total = data["total"]
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(completed)
        elapsed = time.time() - self.run_started_at if self.run_started_at else 0.0
        eta = (elapsed / completed) * (total - completed) if completed else 0.0
        self.summary_label.setText(f"Rendering {completed}/{total} job(s). Estimated time remaining: {eta:.1f}s.")

    def _on_preview_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        processed = data["processed"]
        total = data["total"]
        clip_name = data["clip_name"]
        self.preview_progress_label.setText(_format_preview_progress(processed, total))
        self.preview_help_label.setText(f"Click once — scanning media may take a moment. Current clip: {clip_name}")

    def _on_preview_ready(self, payload: object) -> None:
        context = payload  # type: ignore[assignment]
        self._apply_preview_context(context)
        self._append_log(f"Prepared {len(self.plan)} jobs.")
        self._append_log(f"Inferred camera count: {len(context.clips)}")
        self._append_log(f"Sync mode: {self._plan_sync_mode(self.plan)}")
        if self.last_replay_script is not None:
            self._append_log(f"Saved replay script: {self.last_replay_script}")
        self._set_preview_busy(False)
        self.preview_progress_label.setText("Preview ready.")
        self.preview_worker = None

    def _on_preview_failure(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self._append_log(str(data["error"]))
        self._set_health_state("red", "Preview failed. Check REDline, source metadata, and output folder settings.")
        self._set_preview_busy(False)
        self.preview_progress_label.setText("Preview failed.")
        self.preview_worker = None
        QMessageBox.critical(self, APP_TITLE, str(data["error"]))

    def _on_batch_scan_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        phase = data["phase"]
        processed = data["processed"]
        total = data["total"]
        if phase == "build":
            self.batch_phase_label.setText("Phase: building batches")
            self.batch_counter_label.setText(f"Scanned clips: complete • Built batches: {processed} / {total}")
        else:
            self.batch_phase_label.setText("Phase: scanning clips")
            self.batch_counter_label.setText(f"Scanned clips: {processed} / {total} • Built batches: {len(self.batch_groups)}")
        self.batch_progress_bar.setMaximum(max(total, 1))
        self.batch_progress_bar.setValue(processed)
        elapsed = time.time() - self.batch_scan_started_at if self.batch_scan_started_at else 0.0
        self.batch_eta_label.setText(_format_eta(elapsed, processed, total))
        self.batch_current_label.setText("Current batch: building reel/clip groups" if phase == "build" else "Current batch: scanning top-level media folder")

    def _on_batch_scan_ready(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        result: BatchScanResult = data["result"]
        self.batch_scan_result = result
        self.batch_assignments = list(result.assignments)
        self.batch_groups = list(result.groups)
        self._render_batch_groups()
        self._render_unassigned_table()
        self._update_batch_secondary_actions()
        self.batch_render_button.setText(self._batch_render_idle_text)
        self.batch_phase_label.setText("Phase: scan complete")
        self.batch_counter_label.setText(
            f"Scanned clips: {len(self.batch_assignments)} • Built batches: {len(self.batch_groups)} • Needs assignment: {self._needs_assignment_count()}"
        )
        self.batch_eta_label.setText("ETA: complete")
        self.batch_current_label.setText(f"Current batch: ready to render from {data['source']}")
        self.batch_render_note_label.setText(
            f"Batch preview ready. {len(self.batch_groups)} reel/clip contact sheet(s) are ready, with {self._needs_assignment_count()} source clip(s) still awaiting operator assignment."
        )
        self._append_batch_log(
            f"Built {len(self.batch_groups)} batch group(s) from {data['source']}. {self._needs_assignment_count()} clip(s) need manual batch assignment."
        )
        self._set_batch_scan_busy(False)
        self.batch_scan_worker = None

    def _on_batch_scan_failure(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self._append_batch_log(data["error"])
        self.batch_render_button.setText(self._batch_render_idle_text)
        self.batch_phase_label.setText("Phase: scan failed")
        self.batch_eta_label.setText("ETA: --:--")
        self._update_batch_secondary_actions()
        self._set_batch_scan_busy(False)
        self.batch_scan_worker = None
        QMessageBox.critical(self, APP_TITLE, str(data["error"]))

    def _on_batch_render_state(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self.batch_phase_label.setText(f"Phase: {data['phase']}")
        self.batch_current_label.setText(f"Current batch: {data['current_batch']}")
        self.batch_counter_label.setText(
            f"Rendered batches: {data['batches_done']} / {data['batches_total']} • Processed clips: {data['rendered_jobs']} / {data['render_total']}"
        )

    def _on_batch_render_metadata_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        processed = data["processed"]
        total = data["total"]
        self.batch_phase_label.setText("Phase: resolving metadata")
        self.batch_counter_label.setText(
            f"Metadata clips: {processed} / {total} • Current batch: {data['batch_label']}"
        )
        self.batch_progress_bar.setMaximum(max(total, 1))
        self.batch_progress_bar.setValue(processed)
        elapsed = time.time() - self.batch_render_started_at if self.batch_render_started_at else 0.0
        self.batch_eta_label.setText(_format_eta(elapsed, processed, total))
        self._append_batch_log(f"Metadata: {data['clip_name']} ({processed}/{total})")

    def _on_batch_render_job_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        processed = data["processed"]
        total = data["total"]
        self.batch_phase_label.setText("Phase: rendering stills")
        self.batch_counter_label.setText(
            f"Rendered stills: {processed} / {total} • {data['batch_label']} ({data['batch_completed']} / {data['batch_total']})"
        )
        self.batch_progress_bar.setMaximum(max(total, 1))
        self.batch_progress_bar.setValue(processed)
        elapsed = time.time() - self.batch_render_started_at if self.batch_render_started_at else 0.0
        self.batch_eta_label.setText(_format_eta(elapsed, processed, total))
        outcome = data["outcome"]
        if outcome.error is None:
            self._append_batch_log(f"{data['batch_label']}: job {outcome.index} succeeded.")
        else:
            self._append_batch_log(f"{data['batch_label']}: job {outcome.index} failed: {outcome.error}")

    def _on_batch_render_done(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self.batch_render_worker = None
        self.batch_phase_label.setText("Phase: complete")
        self.batch_counter_label.setText(
            f"Rendered batches: {data['batches_done']} / {data['batches_total']} • Failures: {data['failures']}"
        )
        self.batch_eta_label.setText("ETA: complete")
        self.batch_current_label.setText("Current batch: finished")
        self.batch_render_note_label.setText(
            f"Batch render complete. Wrote {len(data['pdfs'])} contact sheet PDF(s) into {data['output_root']}."
        )
        self.batch_render_button.setEnabled(True)
        self.batch_render_button.setText("Open Batch Output")
        self.batch_progress_bar.setValue(self.batch_progress_bar.maximum())
        self._append_batch_log(
            f"Finished batch render in {data['elapsed']:.1f}s with {data['failures']} failed still render(s)."
        )

    def _on_done(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self.run_button.setEnabled(True)
        self.summary_label.setText(
            f"Finished in {data['elapsed']:.1f}s. {data['successes']} succeeded, {data['failures']} failed."
        )
        pdf_path = None
        if data["successes"]:
            try:
                pdf_path = self._build_contact_sheet_pdf()
                self.last_contact_sheet_pdf = pdf_path
                self._append_log(f"Contact sheet PDF: {pdf_path}")
            except Exception as exc:
                self._append_log(f"Contact sheet PDF failed: {exc}")
                if not data["failures"]:
                    return
        if data["failures"]:
            self._set_health_state("red", "One or more renders failed. Review the log and replay script.")
            self.preview_note_label.setText(
                f"Run finished with failures. {'PDF written to: ' + str(pdf_path) if pdf_path else 'No PDF written.'}"
            )
            self.run_button.setText("Build Contact Sheet PDF")
        else:
            self._set_health_state("green", "Contact sheet PDF written successfully from verified clip metadata.")
            self.preview_note_label.setText(
                f"Job complete. PDF written to: {pdf_path}. JPEG intermediates remain in the output folder."
            )
            if pdf_path:
                self.run_button.setText("Open Contact Sheet")

    def scan_batches(self) -> None:
        if self.batch_scan_worker and self.batch_scan_worker.is_alive():
            return
        try:
            self._validate_batch_source_path()
            if self.batch_output_edit.text().strip():
                self._validate_batch_output_path()
        except Exception as exc:
            self._append_batch_log(str(exc))
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return
        self.batch_scan_started_at = time.time()
        self.batch_scan_result = None
        self.batch_assignments = []
        self.batch_groups = []
        self._render_batch_groups()
        self._render_unassigned_table()
        self._set_batch_detail_visible(False)
        self._set_batch_scan_busy(True)
        self.batch_phase_label.setText("Phase: scanning clips")
        self.batch_counter_label.setText("Scanned clips: 0 • Built batches: 0")
        self.batch_eta_label.setText("ETA: calculating...")
        self.batch_current_label.setText("Current batch: scanning source tree")
        self.batch_render_note_label.setText("Scanning clips and building reel/clip batch groups.")
        QCoreApplication.processEvents()
        time.sleep(0.05)
        QCoreApplication.processEvents()
        self.batch_scan_worker = threading.Thread(target=self._run_batch_scan_worker, daemon=True)
        self.batch_scan_worker.start()

    def run_batch_jobs(self) -> None:
        if self.batch_render_worker and self.batch_render_worker.is_alive():
            return
        if self.batch_groups and self.batch_render_button.text() == "Open Batch Output":
            output_dir = self._validate_batch_output_path()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_dir)))
            return
        selected = self._selected_batch_groups()
        if not selected:
            QMessageBox.warning(self, APP_TITLE, "Select at least one batch to render.")
            return
        try:
            output_dir = self._validate_batch_output_path()
        except Exception as exc:
            self._append_batch_log(str(exc))
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return
        self._save_settings()
        self.batch_render_started_at = time.time()
        self.batch_progress_bar.setMaximum(max(sum(len(group.clips) for group in selected), 1))
        self.batch_progress_bar.setValue(0)
        self.batch_render_note_label.setText("Resolving metadata, rendering stills, and assembling per-batch contact sheets.")
        self.batch_render_button.setEnabled(False)
        self.batch_render_button.setText("Rendering Batches...")
        self.batch_phase_label.setText("Phase: resolving metadata")
        self.batch_counter_label.setText(
            f"Rendered batches: 0 / {len(selected)} • Processed clips: 0 / {sum(len(group.clips) for group in selected)}"
        )
        self.batch_eta_label.setText("ETA: calculating...")
        self.batch_current_label.setText("Current batch: preparing first reel/clip set")
        self._append_batch_log(f"Starting batch render for {len(selected)} reel/clip group(s) into {output_dir}")
        self.batch_render_worker = threading.Thread(target=self._run_batch_render_worker, args=(selected,), daemon=True)
        self.batch_render_worker.start()

    def _run_batch_scan_worker(self) -> None:
        try:
            source_path = self._validate_batch_source_path()
            redline_path = None
            try:
                redline_path = str(self._validate_redline_path())
            except Exception:
                redline_path = None
            result = build_batch_scan_result(
                source_path,
                alphabetize=self.alphabetize_check.isChecked(),
                redline_exe=redline_path,
                progress_callback=lambda phase, processed, total: self.event_queue.put(
                    ("batch-scan-progress", {"phase": phase, "processed": processed, "total": total})
                ),
                log_callback=lambda message: self.event_queue.put(("batch-log", message)),
            )
            self.event_queue.put(("batch-scan-ready", {"result": result, "source": str(source_path)}))
        except Exception as exc:
            self.event_queue.put(("batch-scan-failure", {"error": str(exc)}))

    def _run_batch_render_worker(self, selected_groups: list[BatchGroup]) -> None:
        output_root = self._validate_batch_output_path()
        redline_path = None
        if any(any(clip.provider_kind == "red" for clip in group.clips) for group in selected_groups):
            redline_path = self._validate_redline_path()
            self.event_queue.put(("batch-log", f"Using REDline for batch render: {redline_path}"))

        total_metadata_clips = sum(len(group.clips) for group in selected_groups)
        total_render_jobs = total_metadata_clips
        processed_metadata = 0
        completed_render_jobs = 0
        completed_batches = 0
        failures = 0
        written_pdfs: list[str] = []

        for batch_index, group in enumerate(selected_groups, start=1):
            self.event_queue.put(
                (
                    "batch-render-state",
                    {
                        "phase": "resolving metadata",
                        "current_batch": f"Reel {group.reel_number} / Clip {group.clip_number} ({batch_index} of {len(selected_groups)})",
                        "batches_done": completed_batches,
                        "batches_total": len(selected_groups),
                        "rendered_jobs": completed_render_jobs,
                        "render_total": total_render_jobs,
                    },
                )
            )

            batch_pdf_path = build_batch_output_path(output_root, group)
            options = BatchOptions(
                output_dir=batch_pdf_path.parent,
                frame_request=self._build_frame_request(),
                settings=self._build_render_settings(),
                group_mode="flat",
                alphabetize=self.alphabetize_check.isChecked(),
                custom_group_name=None,
                redline_exe=str(redline_path) if redline_path else None,
                sync_mode="sync_off" if self.batch_sync_mode_combo.currentText() == "Sync Off" else "sync_on",
            )
            context = build_preview_context(
                list(group.clips),
                options,
                metadata_cache=self.metadata_cache,
                progress_callback=lambda processed, total, clip, metadata, base=processed_metadata: self.event_queue.put(
                    (
                        "batch-render-metadata-progress",
                        {
                            "processed": base + processed,
                            "total": total_metadata_clips,
                            "clip_name": clip.clip_name,
                            "batch_label": f"Reel {group.reel_number} / Clip {group.clip_number}",
                        },
                    )
                ),
            )
            for clip_path, fields in context.clip_fields.items():
                override = self._assignment_for_path(clip_path)
                if override is None:
                    continue
                fields.camera_label = override.camera_label.strip() or fields.camera_label
                fields.clip_family = override.clip_family.strip() or fields.clip_family
                fields.subgroup_name = ""
                fields.group_name = override.subgroup_name.strip() or fields.group_name or "Uncategorized"
                fields.manufacturer = override.manufacturer.strip() or fields.manufacturer
                fields.format_type = override.format_type.strip() or fields.format_type
            processed_metadata += len(group.clips)
            selection = self._batch_selection_for_context(context)
            plan = build_job_plan_from_context(context, selection)

            self.event_queue.put(
                (
                    "batch-render-state",
                    {
                        "phase": "rendering stills",
                        "current_batch": f"Reel {group.reel_number} / Clip {group.clip_number} ({batch_index} of {len(selected_groups)})",
                        "batches_done": completed_batches,
                        "batches_total": len(selected_groups),
                        "rendered_jobs": completed_render_jobs,
                        "render_total": total_render_jobs,
                    },
                )
            )

            outcomes = render_plan_items_parallel(
                plan,
                redline_exe=str(redline_path) if redline_path else None,
                min_output_bytes=MIN_OUTPUT_BYTES,
                progress_callback=lambda outcome, completed, total, base=completed_render_jobs, group_label=f"Reel {group.reel_number} / Clip {group.clip_number}": self.event_queue.put(
                    (
                        "batch-render-job-progress",
                        {
                            "outcome": outcome,
                            "processed": base + completed,
                            "total": total_render_jobs,
                            "batch_completed": completed,
                            "batch_total": total,
                            "batch_label": group_label,
                        },
                    )
                ),
            )
            completed_render_jobs += len(plan)
            batch_failures = sum(1 for outcome in outcomes if outcome.error is not None)
            failures += batch_failures

            self.event_queue.put(
                (
                    "batch-render-state",
                    {
                        "phase": "assembling pdf",
                        "current_batch": f"Reel {group.reel_number} / Clip {group.clip_number} ({batch_index} of {len(selected_groups)})",
                        "batches_done": completed_batches,
                        "batches_total": len(selected_groups),
                        "rendered_jobs": completed_render_jobs,
                        "render_total": total_render_jobs,
                    },
                )
            )

            pdf_path = self._build_contact_sheet_pdf_for_plan(
                plan,
                output_dir=options.output_dir,
                destination_name=batch_pdf_path.name,
                sync_mode_override=options.sync_mode,
            )
            written_pdfs.append(str(pdf_path))
            completed_batches += 1
            self.event_queue.put(("batch-log", f"Built {pdf_path}"))

        elapsed = time.time() - self.batch_render_started_at
        self.event_queue.put(
            (
                "batch-render-done",
                {
                    "elapsed": elapsed,
                    "batches_done": completed_batches,
                    "batches_total": len(selected_groups),
                    "failures": failures,
                    "pdfs": written_pdfs,
                    "output_root": str(output_root),
                },
            )
        )

    def _render_plan(self) -> None:
        self.preview_table_updating = True
        self.preview_table.setRowCount(len(self.plan))
        sync_mode = self._plan_sync_mode(self.plan)
        for row, item in enumerate(self.plan):
            sync_status = self._sync_status_text(item, sync_mode)
            fields = item.clip_fields
            values = (
                fields.camera_label or self._display_clip_label(item.clip.clip_name),
                fields.group_name or item.output_group,
                fields.subgroup_name or "",
                fields.manufacturer or item.clip_metadata.manufacturer or item.clip.manufacturer or "Unknown",
                fields.format_type or item.clip_metadata.format_type or item.clip.format_type or "Unknown",
                item.clip.clip_name,
                self._preview_fps_text(item),
                item.frame_resolution.source_timecode_in or "Unavailable",
                item.frame_resolution.source_timecode_out or "Unavailable",
                str(item.frame_resolution.frame_index),
                item.frame_resolution.match_timecode or "Unavailable",
                sync_status,
                item.frame_resolution.range_relation or "Unavailable",
                self._sync_basis_text(item),
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setData(Qt.UserRole, str(item.clip.source_path))
                if column in {0, 1, 2, 3, 4}:
                    table_item.setFlags(table_item.flags() | Qt.ItemIsEditable)
                else:
                    table_item.setFlags(table_item.flags() & ~Qt.ItemIsEditable)
                if column == 11:
                    self._style_status_item(table_item, sync_status)
                self.preview_table.setItem(row, column, table_item)
        self.preview_table.resizeColumnsToContents()
        self.summary_label.setText(self._sync_summary_text(sync_mode))
        self.preview_table_updating = False

    def _render_batch_groups(self) -> None:
        self.batch_table_updating = True
        self.batch_table.setRowCount(len(self.batch_groups))
        for row, group in enumerate(self.batch_groups):
            include_item = QTableWidgetItem()
            include_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            include_item.setCheckState(Qt.Checked)
            include_item.setData(Qt.UserRole, group.batch_id)
            self.batch_table.setItem(row, 0, include_item)

            values = (
                group.reel_number,
                group.clip_number,
                str(group.source_clip_count),
                self._batch_camera_summary(group),
                self._batch_family_summary(group),
                self._batch_subgroup_summary(group),
                self._batch_sync_summary(group),
                group.output_pdf_name,
            )
            for column, value in enumerate(values, start=1):
                table_item = QTableWidgetItem(value)
                table_item.setFlags(table_item.flags() & ~Qt.ItemIsEditable)
                table_item.setData(Qt.UserRole, group.batch_id)
                self.batch_table.setItem(row, column, table_item)
        self.batch_table.resizeColumnsToContents()
        self.batch_table_updating = False
        self._refresh_batch_render_button_state()
        if self.batch_groups and not self._selected_batch_id():
            self.batch_table.selectRow(0)
        self._render_selected_batch_detail()

    def _batch_sync_summary(self, group: BatchGroup) -> str:
        providers = sorted({clip.provider_kind for clip in group.clips})
        if providers == ["red"]:
            return "RED metadata resolves on render"
        if set(providers).issubset({"red", "video", "braw"}):
            return "Mixed providers, sync resolves on render"
        return "Metadata resolves on render"

    def _batch_camera_summary(self, group: BatchGroup) -> str:
        labels = [
            (self._assignment_for_path(clip.source_path).camera_label if self._assignment_for_path(clip.source_path) else "")
            or self._display_clip_label(clip.clip_name)
            for clip in group.clips
        ]
        preview = ", ".join(labels[:4])
        if len(labels) <= 4:
            return f"{group.camera_count} cameras • {preview}"
        return f"{group.camera_count} cameras • {preview}..."

    def _batch_family_summary(self, group: BatchGroup) -> str:
        families = sorted(
            {
                (self._assignment_for_path(clip.source_path).clip_family if self._assignment_for_path(clip.source_path) else suggest_clip_family(clip))
                for clip in group.clips
            }
        )
        return ", ".join(families[:3]) + ("…" if len(families) > 3 else "")

    def _batch_subgroup_summary(self, group: BatchGroup) -> str:
        subgroups = sorted(
            {
                (self._assignment_for_path(clip.source_path).subgroup_name if self._assignment_for_path(clip.source_path) else suggest_subgroup(clip))
                for clip in group.clips
            }
        )
        return ", ".join(subgroups[:3]) + ("…" if len(subgroups) > 3 else "")

    def _selected_batch_groups(self) -> list[BatchGroup]:
        selected: list[BatchGroup] = []
        by_id = {group.batch_id: group for group in self.batch_groups}
        for row in range(self.batch_table.rowCount()):
            item = self.batch_table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            batch_id = item.data(Qt.UserRole)
            group = by_id.get(batch_id)
            if group is not None:
                selected.append(group)
        return selected

    def _selected_batch_id(self) -> str | None:
        rows = self.batch_table.selectionModel().selectedRows() if self.batch_table.selectionModel() else []
        if not rows:
            item = self.batch_table.item(0, 0) if self.batch_table.rowCount() else None
            return item.data(Qt.UserRole) if item is not None else None
        row = rows[0].row()
        item = self.batch_table.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else None

    def _restore_batch_selection(self, batch_id: str) -> None:
        for row in range(self.batch_table.rowCount()):
            item = self.batch_table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == batch_id:
                self.batch_table.selectRow(row)
                return

    def _render_selected_batch_detail(self) -> None:
        batch_id = self._selected_batch_id()
        self.batch_detail_table_updating = True
        self.batch_detail_table.setRowCount(0)
        if not batch_id:
            if self.batch_assignments:
                self.batch_detail_help_label.setText(
                    "Select a batch to review its source clips. Unparsed media can be assigned from the panel below into an existing or new batch."
                )
            else:
                self.batch_detail_help_label.setText(
                    "Suggested Clip Family and Subgroup values are editable per source clip. Select a batch to review its source labels."
                )
            self.batch_detail_table_updating = False
            return
        group = next((item for item in self.batch_groups if item.batch_id == batch_id), None)
        if group is None:
            self.batch_detail_table_updating = False
            return
        self.batch_detail_help_label.setText(
            f"Editing source labels for Reel {group.reel_number} / Clip {group.clip_number}. Subgroup drives section organization on the final contact sheet."
        )
        self.batch_detail_table.setRowCount(len(group.clips))
        for row, clip in enumerate(group.clips):
            assignment = self._assignment_for_path(clip.source_path)
            if assignment is None:
                continue
            values = (
                clip.clip_name,
                assignment.camera_label or self._display_clip_label(clip.clip_name),
                assignment.clip_family or suggest_clip_family(clip),
                assignment.subgroup_name or suggest_subgroup(clip, assignment.clip_family),
                assignment.manufacturer or clip.manufacturer or "Unknown",
                assignment.format_type or clip.format_type or "Unknown",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, str(clip.source_path))
                item.setData(Qt.UserRole + 1, batch_id)
                if column in {1, 2, 3}:
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.batch_detail_table.setItem(row, column, item)
        self.batch_detail_table.resizeColumnsToContents()
        self.batch_detail_table_updating = False

    def _refresh_batch_render_button_state(self) -> None:
        enabled = bool(self.batch_groups and self._selected_batch_groups() and self.batch_output_edit.text().strip())
        if self.batch_render_worker and self.batch_render_worker.is_alive():
            enabled = False
        self.batch_render_button.setEnabled(enabled)

    def _build_plan(self):
        if self.preview_context:
            return build_job_plan_from_context(self.preview_context, self._selection_state_from_widgets())
        context = self._build_preview_context()
        self.preview_context = context
        return build_job_plan_from_context(context, context.selection)

    def _on_preview_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self.preview_table_updating or not self.preview_context:
            return
        path_text = item.data(Qt.UserRole)
        if not path_text:
            return
        clip_path = Path(path_text)
        fields = self.preview_context.clip_fields.get(clip_path)
        if fields is None:
            return
        value = item.text().strip()
        if item.column() == 0:
            fields.camera_label = value
        elif item.column() == 1:
            fields.group_name = value
        elif item.column() == 2:
            fields.subgroup_name = value
        elif item.column() == 3:
            fields.manufacturer = value
        elif item.column() == 4:
            fields.format_type = value
        else:
            return
        self._refresh_plan_from_selection(write_replay=False)

    def _build_frame_request(self) -> FrameTargetRequest:
        tc_text = self.target_timecode_edit.text().strip() if self.advanced_timecode_check.isChecked() else ""
        fps_text = self.fps_edit.text().strip()
        try:
            fps_value = float(fps_text) if fps_text else None
        except ValueError as exc:
            raise ValueError(f"FPS must be numeric, got {fps_text!r}.") from exc
        return FrameTargetRequest(
            target_timecode=tc_text or None,
            fps=fps_value,
            drop_frame=self.drop_frame_check.isChecked(),
        )

    def _build_render_settings(self) -> RenderSettings:
        try:
            resize_x = int(self.resize_x_edit.text()) if self.resize_x_edit.text().strip() else None
            resize_y = int(self.resize_y_edit.text()) if self.resize_y_edit.text().strip() else None
            return RenderSettings(
                render_res=int(self.render_res_edit.text()),
                resize_x=resize_x,
                resize_y=resize_y,
                use_meta=self.metadata_mode_check.isChecked(),
                color_sci_version=int(self.color_sci_edit.text()),
                output_tone_map=int(self.output_tone_map_edit.text()),
                roll_off=int(self.roll_off_edit.text()),
                output_gamma_curve=int(self.output_gamma_edit.text()),
            )
        except ValueError as exc:
            raise ValueError(f"Render settings must be numeric where applicable: {exc}") from exc

    def _batch_selection_for_context(self, context: PreviewContext) -> MatchSelectionState:
        selection = context.selection
        subset = next((item for item in context.overlap_subsets if item.subset_id == selection.active_subset_id), None)
        if subset is None:
            return selection
        mode_text = self.batch_selection_mode_combo.currentText()
        if mode_text == "Start of Range":
            return MatchSelectionState(subset.subset_id, subset.start_abs_frame, "start")
        if mode_text == "Middle of Range":
            return MatchSelectionState(subset.subset_id, subset.recommended_abs_frame, "middle")
        if mode_text == "End of Range":
            return MatchSelectionState(subset.subset_id, subset.end_abs_frame, "end")
        return selection

    def _validate_input_path(self) -> Path:
        value = self.source_edit.text().strip()
        if not value:
            raise ValueError("Choose a source folder, RDC package, or single R3D clip.")
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Selected source does not exist: {path}")
        return path

    def _validate_output_path(self) -> Path:
        value = self.output_edit.text().strip()
        if not value:
            raise ValueError("Choose an output folder.")
        path = Path(value).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError(f"Output path is not a folder: {path}")
        try:
            with tempfile.NamedTemporaryFile(prefix="r3dcontactsheet_", dir=path, delete=True):
                pass
        except OSError as exc:
            raise ValueError(f"Output folder is not writable: {path}") from exc
        return path

    def _validate_batch_source_path(self) -> Path:
        value = self.batch_source_edit.text().strip()
        if not value:
            raise ValueError("Choose a top-level media folder for batching.")
        path = Path(value).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise ValueError(f"Batch source folder does not exist: {path}")
        return path

    def _validate_batch_output_path(self) -> Path:
        value = self.batch_output_edit.text().strip()
        if not value:
            raise ValueError("Choose a batch output folder.")
        path = Path(value).expanduser().resolve()
        source_path = self._validate_batch_source_path()
        if path == source_path or source_path in path.parents:
            raise ValueError("Batch output folder must be outside the media source tree.")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _validate_redline_path(self) -> Path:
        value = self.redline_edit.text().strip()
        if not value:
            raise ValueError("Choose a REDline executable.")
        path = Path(value).expanduser().resolve()
        probe = probe_redline(RedlinePaths(explicit_path=path))
        if not probe.available:
            raise ValueError(probe.message)
        if not probe.compatible:
            raise ValueError(probe.message)
        if not probe.executable:
            raise ValueError("REDline validation succeeded without a resolved executable path, which should not happen.")
        return probe.executable

    def _write_replay_script(self, plan) -> Path:
        output_dir = self._validate_output_path()
        replay_path = output_dir / REPLAY_SCRIPT_NAME
        commands = [build_replay_command(item, redline_exe=self.redline_edit.text().strip() or None) for item in plan]
        replay_path.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n" + "\n".join(shell_join(command) for command in commands) + "\n",
            encoding="utf-8",
        )
        replay_path.chmod(replay_path.stat().st_mode | 0o111)
        return replay_path

    def _preview_fps_text(self, item) -> str:
        fps = item.clip_metadata.clip_fps or item.frame_resolution.clip_fps
        if fps is None:
            return "Unknown"
        return f"{fps:g}"

    def _sync_basis_text(self, item) -> str:
        tc_source = item.clip_metadata.timecode_source or "unknown source"
        fps = item.clip_metadata.clip_fps
        tc_base = item.clip_metadata.timecode_base_fps
        bits = [item.clip_metadata.provider_name.upper(), tc_source]
        if fps is not None:
            bits.append(f"clip {fps:g} fps")
        if tc_base is not None:
            bits.append(f"TC {tc_base:g}")
        subset = self._current_active_subset()
        if subset is not None:
            bits.append(f"subset {subset.start_timecode} → {subset.end_timecode}")
        return " | ".join(bits)

    def _sync_status_text(self, item, sync_mode: str) -> str:
        if not item.clip_metadata.sync_eligible:
            if item.clip_metadata.metadata_error:
                return item.clip_metadata.metadata_error
            return "Metadata incomplete: clip timing is not sync-eligible"
        if item.frame_resolution.match_frame is None or not item.frame_resolution.match_timecode:
            return "Metadata incomplete"
        if item.frame_resolution.sync_status == "exact_match":
            return "Exact match"
        if item.frame_resolution.sync_status == "nearest_available":
            return "Nearest available"
        if item.frame_resolution.sync_status == "outside_overlap":
            return "Out Of Frame Sync"
        return "Metadata incomplete"

    def _sync_summary_text(self, sync_mode: str) -> str:
        if not self.plan:
            return "No jobs planned yet."
        subset = self._current_active_subset()
        if sync_mode == "full":
            reference = self._current_match_timecode_label()
            return f"Full sync verified across {len(self.plan)} clip(s). Shared frame: {reference}."
        if sync_mode == "partial":
            matched = sum(1 for item in self.plan if item.frame_resolution.sync_status == "exact_match")
            reference = self._current_match_timecode_label()
            if subset is not None:
                return f"Partial sync. {matched} of {len(self.plan)} clips contain the selected shared frame at {reference} inside {subset.start_timecode} → {subset.end_timecode}."
            return f"Partial sync. {matched} of {len(self.plan)} clips contain the selected shared frame at {reference}."
        return "No common moment across all clips. The contact sheet will still be built using each clip's own metadata timecode."

    def _frame_mode_summary_from_plan(self, plan) -> str:
        if not plan:
            return "Current frame mode: no preview plan yet."
        subset = self._current_active_subset()
        current_tc = self._current_match_timecode_label()
        sync_mode = self._plan_sync_mode(plan)
        if sync_mode == "full":
            return f"Shared match frame: {current_tc}. Full sync verified from clip metadata across all selected clips."
        if sync_mode == "partial":
            if subset is not None:
                return f"Partial sync. Shared frame comes from the active overlap subset {subset.start_timecode} → {subset.end_timecode}, currently set to {current_tc}."
            return f"Partial sync. Shared frame is currently set to {current_tc}."
        return "No common moment across all clips. Each clip will use its own real metadata timecode and nearest available frame."

    def _build_contact_sheet_pdf(self) -> Path:
        output_dir = self._validate_output_path()
        return self._build_contact_sheet_pdf_for_plan(self.plan, output_dir=output_dir, destination_name=CONTACT_SHEET_NAME)

    def _build_contact_sheet_pdf_for_plan(
        self,
        plan,
        *,
        output_dir: Path,
        destination_name: str,
        sync_mode_override: str | None = None,
    ) -> Path:
        sync_mode = self._plan_sync_mode(plan)
        if sync_mode_override is not None:
            presentation_mode = sync_mode_override == "sync_off"
        else:
            presentation_mode = self.sync_mode_combo.currentText() == "Sync Off"
        items = []
        sorted_plan = sorted(
            plan,
            key=lambda item: (
                (item.clip_fields.group_name or item.output_group or "Uncategorized").lower(),
                (item.clip_fields.subgroup_name or "").lower(),
                (item.clip_fields.camera_label or item.clip.clip_name).lower(),
            ),
        )
        for item in sorted_plan:
            image_path = item.output_file
            if not image_path.exists() or image_path.stat().st_size < MIN_OUTPUT_BYTES:
                continue
            display_group = item.clip_fields.group_name.strip() or item.output_group or "Uncategorized"
            display_subgroup = item.clip_fields.subgroup_name.strip()
            items.append(
                ContactSheetItem(
                    image_path=image_path,
                    clip_label=item.clip_fields.camera_label.strip() or self._display_clip_label(item.clip.clip_name),
                    group_label=display_group,
                    subgroup_label=display_subgroup,
                    frame_label=(
                        f"Frame {item.frame_resolution.frame_index}"
                        if item.frame_resolution.frame_index is not None
                        else "Frame unavailable"
                    ),
                    timecode_label=item.frame_resolution.match_timecode or "Unavailable",
                    fps_label=(f"{item.clip_metadata.clip_fps:g} fps" if item.clip_metadata.clip_fps is not None else "FPS unknown"),
                    resolution_label=((item.clip_metadata.resolution or "Resolution unknown").replace("x", " × ")),
                    sync_label="" if presentation_mode else self._display_sync_caption(self._sync_status_text(item, sync_mode)),
                )
            )
        if not items:
            raise ValueError("No valid rendered stills were available for contact sheet PDF generation.")
        subset = self._current_active_subset() if plan is self.plan else None
        common_fps = self._common_value([f"{item.clip_metadata.clip_fps:g}" for item in plan if item.clip_metadata.clip_fps is not None])
        common_resolution = self._common_value([item.clip_metadata.resolution for item in plan if item.clip_metadata.resolution])
        grouping_summary = self._sheet_grouping_summary(plan)
        header_lines = [
            grouping_summary,
            f"Mode: {'Presentation' if presentation_mode else 'Technical Sync'}",
            (
                "Frames selected automatically from the strongest usable overlap or each clip's middle region."
                if presentation_mode
                else (
                    f"Shared match: {self._plan_matching_label(plan)} • {subset.start_timecode} → {subset.end_timecode}"
                    if subset is not None and sync_mode != "none"
                    else (
                        f"Shared match: {self._plan_matching_label(plan)}"
                        if sync_mode != "none"
                        else "No universal common frame across all clips"
                    )
                )
            ),
            f"{common_fps or 'mixed'} fps • {(common_resolution or 'mixed or unavailable').replace('x', '×')} • {len(plan)} cameras",
        ]
        destination = output_dir / destination_name
        return build_contact_sheet_pdf(
            items,
            destination,
            "R3DContactSheet",
            header_lines=header_lines,
            theme_name=self.theme_combo.currentText().strip().lower(),
        )

    def _update_health_from_plan(self, plan) -> None:
        if not plan:
            self._set_health_state("yellow", "No preview plan yet.")
            return
        sync_mode = self._plan_sync_mode(plan)
        statuses = [self._sync_status_text(item, sync_mode) for item in plan]
        if sync_mode == "full" and all(status == "Exact match" for status in statuses):
            self._set_health_state("green", f"Full sync verified for {len(plan)} clip(s). Metadata is complete.")
            return
        if any(status.startswith("Metadata incomplete") for status in statuses):
            self._set_health_state("yellow", "Metadata is incomplete for one or more clips. Review preview before rendering.")
            return
        if sync_mode == "partial":
            self._set_health_state("yellow", "Partial sync only. Review exact matches, nearest frames, and outside-overlap clips.")
            return
        self._set_health_state("yellow", "No common moment across all clips. The contact sheet will use per-clip metadata timecode and nearest available frames.")

    def _set_health_state(self, level: str, text: str) -> None:
        colors = {
            "green": "#67d36f",
            "yellow": "#e5b94b",
            "red": "#e46666",
        }
        self.status_dot.setStyleSheet(f"color: {colors.get(level, '#e5b94b')}; font-size: 18px;")
        self.status_label.setText(text)

    def _common_value(self, values):
        unique = [value for value in values if value]
        if not unique:
            return None
        return unique[0] if len(set(unique)) == 1 else None

    def _display_clip_label(self, clip_name: str) -> str:
        parts = clip_name.split("_")
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"
        return clip_name.replace("_", " ")

    def _display_sync_caption(self, status: str) -> str:
        if status == "Exact match":
            return "Exact match"
        if status == "Out Of Frame Sync":
            return "Out Of Frame Sync"
        return status

    def _plan_sync_mode(self, plan) -> str:
        if not plan:
            return "none"
        statuses = {item.frame_resolution.sync_status for item in plan}
        if statuses and statuses == {"exact_match"}:
            return "full"
        if "exact_match" in statuses or "nearest_available" in statuses:
            return "partial"
        return "none"

    def _plan_matching_label(self, plan) -> str:
        if not plan:
            return "Unavailable"
        for item in plan:
            if item.frame_resolution.match_timecode:
                return item.frame_resolution.match_timecode
        return "Unavailable"

    def _logical_common_value(self, values):
        unique = {self._logical_token(value) for value in values if value}
        if len(unique) == 1:
            return next(iter(unique))
        return None

    def _logical_token(self, value: str) -> str:
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits or value

    def _display_group_value(self, value):
        return value or "Mixed"

    def _display_overlap_group(self, plan) -> str:
        overlap_items = [
            item for item in plan
            if item.frame_resolution.sync_status in {"exact_match", "nearest_available"}
        ]
        if not overlap_items:
            return "No overlap"
        reel = self._display_group_value(self._logical_common_value([item.clip.clip_name.split("_")[0] for item in overlap_items]))
        clip = self._display_group_value(self._logical_common_value([item.clip.clip_name.split("_")[1] for item in overlap_items if "_" in item.clip.clip_name]))
        if reel != "Mixed" and clip != "Mixed":
            return f"Reel {reel} • Clip {clip}"
        return "Mixed"

    def _sheet_grouping_summary(self, plan) -> str:
        groups = sorted({(item.clip_fields.group_name or item.output_group or "Uncategorized") for item in plan})
        if len(groups) == 1:
            group_text = groups[0]
        else:
            group_text = ", ".join(groups[:3]) + ("…" if len(groups) > 3 else "")
        subgroups = sorted({item.clip_fields.subgroup_name for item in plan if item.clip_fields.subgroup_name})
        if len(subgroups) == 1:
            return f"Group: {group_text} • Subgroup: {subgroups[0]}"
        if len(subgroups) > 1:
            subgroup_text = ", ".join(subgroups[:3]) + ("…" if len(subgroups) > 3 else "")
            return f"Group: {group_text} • Subgroups: {subgroup_text}"
        return f"Group: {group_text}"

    def _set_preview_busy(self, busy: bool, text: str | None = None) -> None:
        self.preview_button.setDisabled(busy)
        self.preview_button.setText((text or self._preview_idle_text) if busy else self._preview_idle_text)
        if busy:
            self.preview_button.setStyleSheet(
                "background: #d6a63f; color: #111318; border: none; border-radius: 6px; padding: 8px 12px; font-weight: 700;"
            )
            self.preview_help_label.setText("Click once — scanning media may take a moment.")
        else:
            self.preview_button.setStyleSheet("")
            self.preview_help_label.setText("Click once — scanning media may take a moment.")
            if not self.preview_progress_label.text():
                self.preview_progress_label.setText("")

    def _set_batch_scan_busy(self, busy: bool) -> None:
        self.batch_scan_button.setDisabled(busy)
        self.batch_scan_button.setText("Scanning..." if busy else self._batch_scan_idle_text)
        if busy:
            self.batch_scan_button.setStyleSheet(
                "background: #d6a63f; color: #111318; border: none; border-radius: 6px; padding: 8px 12px; font-weight: 700;"
            )
        else:
            self.batch_scan_button.setStyleSheet("")
        self._refresh_batch_render_button_state()

    def _append_batch_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        self.batch_log_text.appendPlainText(line)
        print(line, flush=True)

    def _current_match_timecode_label(self) -> str:
        subset = self._current_active_subset()
        selection = self._selection_state_from_widgets()
        if subset is None or selection.selected_abs_frame is None:
            return "Unavailable"
        return self._timecode_for_subset_frame(subset, selection.selected_abs_frame) or "Unavailable"

    def _style_status_item(self, item: QTableWidgetItem, status: str) -> None:
        font = item.font()
        font.setBold(True)
        if status == "Exact match":
            font.setPointSize(font.pointSize() + 1)
            item.setForeground(QColor("#67d36f"))
        elif status == "Out Of Frame Sync":
            font.setPointSize(font.pointSize() + 1)
            item.setForeground(QColor("#e46666"))
        elif status == "Nearest available":
            item.setForeground(QColor("#e5b94b"))
        item.setFont(font)

    def _save_settings(self) -> None:
        try:
            color_sci = int(self.color_sci_edit.text().strip() or "3")
            tone_map = int(self.output_tone_map_edit.text().strip() or "1")
            roll_off = int(self.roll_off_edit.text().strip() or "2")
            gamma = int(self.output_gamma_edit.text().strip() or "32")
            render_res = int(self.render_res_edit.text().strip() or "4")
        except ValueError:
            return
        settings = AppSettings(
            redline_path=self.redline_edit.text().strip(),
            last_input_path=self.source_edit.text().strip(),
            last_output_path=self.output_edit.text().strip(),
            batch_input_path=self.batch_source_edit.text().strip(),
            batch_output_path=self.batch_output_edit.text().strip(),
            frame_index=6,
            target_timecode=self.target_timecode_edit.text().strip() if self.advanced_timecode_check.isChecked() else "",
            fps=self.fps_edit.text().strip() if self.advanced_timecode_check.isChecked() else "23.976",
            drop_frame=self.drop_frame_check.isChecked(),
            color_sci_version=color_sci,
            output_tone_map=tone_map,
            roll_off=roll_off,
            output_gamma_curve=gamma,
            render_res=render_res,
            resize_x=self.resize_x_edit.text().strip(),
            resize_y=self.resize_y_edit.text().strip(),
            group_mode=self.group_mode_combo.currentText().strip() or "flat",
            custom_group_name=self.custom_group_edit.text().strip(),
            alphabetize=self.alphabetize_check.isChecked(),
            metadata_mode=self.metadata_mode_check.isChecked(),
            sync_mode="sync_off" if self.sync_mode_combo.currentText() == "Sync Off" else "sync_on",
            batch_sync_mode="sync_off" if self.batch_sync_mode_combo.currentText() == "Sync Off" else "sync_on",
            batch_selection_mode={
                "Auto": "auto",
                "Start of Range": "start",
                "Middle of Range": "middle",
                "End of Range": "end",
            }.get(self.batch_selection_mode_combo.currentText(), "auto"),
            theme_name=self.theme_combo.currentText().strip().lower(),
        )
        self.store.save(settings)
        self.config_status_label.setText(self.store.last_status)
        self.summary_label.setText("Settings remembered for the next launch.")

    def _append_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        self.log_text.appendPlainText(line)
        print(line, flush=True)

    def _emit_log(self, text: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self._append_log(text)
            return
        self.event_queue.put(("log", text))

    def _set_path_field(self, widget: QLineEdit, value: str) -> None:
        widget.setText(value)
        widget.setCursorPosition(0)
        widget.setToolTip(value)

    def _load_header_logo(self, width: int, height: int) -> QPixmap:
        logo_path = _resource_path(LOGO_NAME)
        pixmap = QPixmap(str(logo_path))
        if pixmap.isNull():
            return QPixmap()
        trimmed = self._trim_pixmap_transparency(pixmap)
        return trimmed.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _trim_pixmap_transparency(self, pixmap: QPixmap) -> QPixmap:
        image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
        width = image.width()
        height = image.height()
        left = width
        right = -1
        top = height
        bottom = -1
        for y in range(height):
            for x in range(width):
                if image.pixelColor(x, y).alpha() > 10:
                    left = min(left, x)
                    right = max(right, x)
                    top = min(top, y)
                    bottom = max(bottom, y)
        if right < left or bottom < top:
            return pixmap
        rect = QRect(left, top, right - left + 1, bottom - top + 1)
        return pixmap.copy(rect)

    def _log_probe_message_once(self, message: str) -> None:
        if not message or message == self._last_probe_log_message:
            return
        self._last_probe_log_message = message
        self._append_log(message)

    def _log_startup_banner(self) -> None:
        for text in (f"STARTUP: {BUILD_MARKER}", self.store.last_status):
            timestamp = time.strftime("%H:%M:%S")
            print(f"[{timestamp}] {text}", flush=True)


def sys_platform_is_macos() -> bool:
    return sys.platform == "darwin"


def _choose_directory_macos(prompt: str) -> tuple[str, str]:
    script_lines = [
        "set chosenFolder to choose folder with prompt " + _applescript_quote(prompt),
        "POSIX path of chosenFolder",
    ]
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", *[arg for line in script_lines for arg in ("-e", line)]],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return ("failed", "")
    if result.returncode == 0:
        return ("selected", result.stdout.strip())
    stderr = (result.stderr or "").lower()
    if "user canceled" in stderr or "cancelled" in stderr:
        return ("cancelled", "")
    return ("failed", "")


def _applescript_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _resource_path(name: str) -> Path:
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / name)
    candidates.append(Path(__file__).resolve().parent.parent / name)
    executable_dir = Path(sys.executable).resolve().parent
    candidates.append(executable_dir / name)
    candidates.append(executable_dir.parent / "Resources" / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[1]


def _apply_app_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#23262b"))
    palette.setColor(QPalette.WindowText, QColor("#f2f2f2"))
    palette.setColor(QPalette.Base, QColor("#171a1f"))
    palette.setColor(QPalette.AlternateBase, QColor("#262b31"))
    palette.setColor(QPalette.Text, QColor("#f2f2f2"))
    palette.setColor(QPalette.Button, QColor("#323740"))
    palette.setColor(QPalette.ButtonText, QColor("#f2f2f2"))
    palette.setColor(QPalette.Highlight, QColor("#4a77c9"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipBase, QColor("#2a2e36"))
    palette.setColor(QPalette.ToolTipText, QColor("#f2f2f2"))
    app.setPalette(palette)
    app.setStyleSheet(
        """
        QMainWindow { background: #23262b; }
        QFrame#compactHeader {
            background: #2b3037;
            border: 1px solid #4d525b;
            border-radius: 10px;
        }
        QLabel#headerTitle {
            font-size: 22px;
            font-weight: 700;
            color: #ffffff;
        }
        QFrame#headerDivider {
            color: #4d525b;
            background: #4d525b;
            min-width: 1px;
            max-width: 1px;
        }
        QGroupBox#sectionBox {
            border: 2px solid #4d525b;
            border-radius: 10px;
            margin-top: 8px;
            padding-top: 8px;
            font-weight: 700;
            color: #f2f2f2;
            background: #2b3037;
        }
        QGroupBox#sectionBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px 0 6px;
        }
        QLabel#heroTitle {
            font-size: 24px;
            font-weight: 700;
            color: #ffffff;
        }
        QLabel#buildBanner {
            color: #f8b26a;
            font-weight: 700;
            font-size: 13px;
        }
        QLabel#muted {
            color: #c2c7ce;
        }
        QLabel#mutedBlue {
            color: #d8dfe8;
            font-weight: 600;
        }
        QLabel#summaryBright {
            color: #f2f8ff;
            font-weight: 700;
        }
        QLineEdit, QComboBox, QPlainTextEdit, QTableWidget {
            background: #171a1f;
            color: #f2f2f2;
            border: 1px solid #5b616b;
            border-radius: 6px;
            padding: 6px;
        }
        QPushButton {
            background: #4a77c9;
            color: white;
            border: none;
            border-radius: 6px;
            padding: 8px 12px;
            font-weight: 600;
        }
        QPushButton:hover {
            background: #5e87d1;
        }
        QCheckBox {
            color: #f2f2f2;
        }
        QHeaderView::section {
            background: #323740;
            color: #f2f2f2;
            padding: 6px;
            border: 1px solid #4b515c;
        }
        """
    )


def main() -> None:
    app = QApplication(sys.argv)
    _apply_app_palette(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
