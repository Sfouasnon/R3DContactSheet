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
    QVBoxLayout,
    QWidget,
)

from .batch import (
    BatchOptions,
    ClipOrganization,
    PreviewContext,
    build_job_plan_from_context,
    build_preview_context,
    describe_source_selection,
    discover_media_clips,
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
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.run_started_at = 0.0
        self.last_replay_script: Path | None = None
        self.last_contact_sheet_pdf: Path | None = None
        self._preview_idle_text = "Preview Batch"
        self.preview_context: PreviewContext | None = None
        self.active_subset: OverlapSubset | None = None
        self.current_selection: MatchSelectionState = MatchSelectionState(None, None, "auto")
        self.redline_ready = False
        self.preview_table_updating = False
        self._last_probe_log_message: str | None = None
        self.metadata_cache: dict[tuple[str, str, int, int, str], object] = {}

        self._build_ui()
        self._apply_settings_to_ui()
        self._connect_signals()
        self._refresh_redline_probe()
        self._refresh_source_state()
        self._refresh_frame_mode_summary()
        self._log_startup_banner()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_events)
        self.poll_timer.start(150)

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        root.addWidget(self._build_header())

        tabs = QTabWidget()
        setup_tab = QWidget()
        setup_layout = QVBoxLayout(setup_tab)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(12)
        setup_layout.addWidget(self._build_source_section())

        controls_row = QHBoxLayout()
        controls_row.setSpacing(12)
        controls_row.addWidget(self._build_frame_section(), 1)
        controls_row.addWidget(self._build_render_section(), 1)
        setup_layout.addLayout(controls_row)
        setup_layout.addStretch(1)

        preview_tab = QWidget()
        preview_layout = QVBoxLayout(preview_tab)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(12)
        preview_layout.addWidget(self._build_preview_section(), 1)

        render_tab = QWidget()
        render_layout = QVBoxLayout(render_tab)
        render_layout.setContentsMargins(0, 0, 0, 0)
        render_layout.setSpacing(12)
        render_layout.addWidget(self._build_render_status_section(), 1)
        render_layout.addWidget(self._build_footer())

        tabs.addTab(setup_tab, "Setup")
        tabs.addTab(preview_tab, "Preview")
        tabs.addTab(render_tab, "Render")
        root.addWidget(tabs, 1)

        save_action = QAction("Remember Settings", self)
        save_action.triggered.connect(self._save_settings)
        self.addAction(save_action)

    def _build_header(self) -> QGroupBox:
        box = self._section_box("Build And Status")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        logo_label = QLabel()
        logo_label.setFixedSize(520, 112)
        logo_label.setAlignment(Qt.AlignCenter)
        logo_pixmap = self._load_header_logo(logo_label.size().width(), logo_label.size().height())
        if not logo_pixmap.isNull():
            logo_label.setPixmap(logo_pixmap)
        header_row.addWidget(logo_label, 0, Qt.AlignVCenter)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        header_row.addWidget(self.status_dot, 0, Qt.AlignVCenter)
        self.status_label = QLabel("Checking REDline…")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("summaryBright")
        header_row.addWidget(self.status_label, 1, Qt.AlignVCenter)
        header_row.addStretch(1)
        layout.addLayout(header_row)
        return box

    def _build_source_section(self) -> QGroupBox:
        box = self._section_box("1. Choose Source And Output")
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

        self.source_edit = QLineEdit()
        self.source_edit.setClearButtonEnabled(True)
        left_layout.addWidget(QLabel("Source path"), 1, 0)
        left_layout.addWidget(self.source_edit, 1, 1, 1, 2)

        source_button_row = QHBoxLayout()
        self.choose_source_button = QPushButton("Choose Source...")
        self.choose_source_button.setText("Choose Source...")
        source_button_row.addWidget(self.choose_source_button)
        source_button_row.addStretch(1)
        left_layout.addLayout(source_button_row, 2, 0, 1, 3)

        source_help = QLabel(
            "Point the app at the media parent folder. The app scans RED clips plus common video files, then normalizes what each provider can truthfully support."
        )
        source_help.setWordWrap(True)
        source_help.setObjectName("muted")
        left_layout.addWidget(source_help, 3, 0, 1, 3)

        self.output_edit = QLineEdit()
        self.output_edit.setClearButtonEnabled(True)
        self.output_button = QPushButton("Choose Output")
        left_layout.addWidget(QLabel("Output folder"), 4, 0)
        left_layout.addWidget(self.output_edit, 4, 1)
        left_layout.addWidget(self.output_button, 4, 2)

        right_column = QVBoxLayout()
        right_column.setSpacing(12)

        summary = self._section_box("2. Source Summary")
        summary_layout = QVBoxLayout(summary)
        self.source_type_label = QLabel("No source selected.")
        self.source_type_label.setWordWrap(True)
        self.source_count_label = QLabel("Resolved clip count will appear here after selection.")
        self.source_count_label.setWordWrap(True)
        self.source_count_label.setObjectName("mutedBlue")
        self.source_detail_label = QLabel("Detected reel and grouping details will appear here after selection.")
        self.source_detail_label.setWordWrap(True)
        self.source_detail_label.setObjectName("muted")
        summary_layout.addWidget(self.source_type_label)
        summary_layout.addWidget(self.source_count_label)
        summary_layout.addWidget(self.source_detail_label)
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

    def _build_frame_section(self) -> QGroupBox:
        box = self._section_box("3. Sync Resolution")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        mode_row = QGridLayout()
        mode_row.setHorizontalSpacing(10)
        mode_row.addWidget(QLabel("Mode"), 0, 0)
        self.sync_mode_combo = QComboBox()
        self.sync_mode_combo.addItems(["Sync On", "Sync Off"])
        mode_row.addWidget(self.sync_mode_combo, 0, 1)
        mode_row.addWidget(QLabel("Theme"), 0, 2)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        mode_row.addWidget(self.theme_combo, 0, 3)
        mode_row.setColumnStretch(5, 1)
        layout.addLayout(mode_row)

        intro = QLabel("Sync On keeps the technical overlap diagnostics visible. Sync Off still chooses coherent frames quietly, but the contact sheet suppresses written sync warnings for presentation use.")
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
            "Recommended workflow: choose the source folder, preview the matching metadata timecode, and let the app auto-align the array."
        )
        guidance.setWordWrap(True)
        guidance.setObjectName("muted")
        layout.addWidget(guidance)

        summary = self._section_box("Frame Selection Summary")
        summary.setMinimumHeight(96)
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(12, 18, 12, 12)
        summary_layout.setSpacing(6)
        self.frame_mode_label = QLabel("Per-clip match frame and match timecode will be resolved automatically from clip metadata.")
        self.frame_mode_label.setWordWrap(True)
        self.frame_mode_label.setObjectName("summaryBright")
        self.frame_mode_label.setMinimumHeight(24)
        summary_layout.addWidget(self.frame_mode_label)
        after_preview = QLabel("Leave advanced timecode off for normal operator use. Preview should prove each clip's chosen match frame, match timecode, and overlap status before you render.")
        after_preview.setWordWrap(True)
        after_preview.setObjectName("muted")
        summary_layout.addWidget(after_preview)
        layout.addWidget(summary)

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
        box = self._section_box("5. Batch Preview")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setAlignment(Qt.AlignTop)
        top_row.addWidget(QLabel("Grouping mode"))
        self.group_mode_combo = QComboBox()
        self.group_mode_combo.addItems(["flat", "parent_folder", "reel_prefix"])
        top_row.addWidget(self.group_mode_combo)
        self.custom_group_edit = QLineEdit()
        self.custom_group_edit.setPlaceholderText("Optional, for mini-array naming")
        self.custom_group_edit.setMaximumWidth(220)
        self.custom_group_edit.hide()
        self.alphabetize_check = QCheckBox("Alphabetize clips")
        top_row.addWidget(self.alphabetize_check)
        top_row.addSpacing(12)
        prompt = QLabel("Preview is the sync check. Edit camera labels and grouping here, then review LTC in/out, per-clip match frame, match timecode, and status before building the contact sheet PDF.")
        prompt.setObjectName("muted")
        top_row.addWidget(prompt, 1)
        preview_action_column = QVBoxLayout()
        preview_action_column.setSpacing(4)
        self.preview_button = QPushButton("Preview Batch")
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

    def _build_render_status_section(self) -> QGroupBox:
        box = self._section_box("6. Render Status")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        self.summary_label = QLabel("No jobs planned yet.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("summaryBright")
        layout.addWidget(self.summary_label)

        self.replay_label = QLabel("Replay script will be created in the output folder when you preview.")
        self.replay_label.setWordWrap(True)
        self.replay_label.setObjectName("muted")
        layout.addWidget(self.replay_label)

        self.preview_note_label = QLabel("This build previews overlap groups, renders per-clip chosen JPEG stills, and assembles a contact sheet PDF in the chosen output folder.")
        self.preview_note_label.setWordWrap(True)
        self.preview_note_label.setObjectName("mutedBlue")
        layout.addWidget(self.preview_note_label)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(320)
        layout.addWidget(self.log_text, 1)
        self._append_log("Ready. Choose a source, preview the batch, then render.")
        return box

    def _build_footer(self) -> QGroupBox:
        box = self._section_box("7. Render Progress And Controls")
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
        self.group_mode_combo.currentTextChanged.connect(self._refresh_source_state)
        self.custom_group_edit.textChanged.connect(lambda *_: self._refresh_source_state())
        self.alphabetize_check.stateChanged.connect(lambda *_: self._refresh_source_state())
        self.sync_mode_combo.currentTextChanged.connect(lambda *_: self._refresh_frame_mode_summary())
        self.theme_combo.currentTextChanged.connect(lambda *_: self._refresh_frame_mode_summary())
        self.preview_table.itemChanged.connect(self._on_preview_table_item_changed)

        for widget in (self.target_timecode_edit, self.fps_edit):
            widget.textChanged.connect(self._refresh_frame_mode_summary)
        self.drop_frame_check.stateChanged.connect(lambda *_: self._refresh_frame_mode_summary())
        self.advanced_timecode_check.toggled.connect(self._toggle_advanced_timecode)

    def _apply_settings_to_ui(self) -> None:
        self.redline_edit.setText(self.settings.redline_path)
        self._set_path_field(self.redline_edit, self.settings.redline_path)
        self._set_path_field(self.source_edit, self.settings.last_input_path)
        self._set_path_field(self.output_edit, self.settings.last_output_path)
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
        sync_mode = self._plan_sync_mode(self.plan)
        presentation_mode = self.sync_mode_combo.currentText() == "Sync Off"
        items = []
        sorted_plan = sorted(
            self.plan,
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
        subset = self._current_active_subset()
        common_fps = self._common_value([f"{item.clip_metadata.clip_fps:g}" for item in self.plan if item.clip_metadata.clip_fps is not None])
        common_resolution = self._common_value([item.clip_metadata.resolution for item in self.plan if item.clip_metadata.resolution])
        grouping_summary = self._sheet_grouping_summary(self.plan)
        header_lines = [
            grouping_summary,
            f"Mode: {'Presentation' if presentation_mode else 'Technical Sync'}",
            (
                "Frames selected automatically from the strongest usable overlap or each clip's middle region."
                if presentation_mode
                else (
                    f"Shared match: {self._current_match_timecode_label()} • {subset.start_timecode} → {subset.end_timecode}"
                    if subset is not None and sync_mode != "none"
                    else "No universal common frame across all clips"
                )
            ),
            f"{common_fps or 'mixed'} fps • {(common_resolution or 'mixed or unavailable').replace('x', '×')} • {len(self.plan)} cameras",
        ]
        destination = output_dir / CONTACT_SHEET_NAME
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
        return self._current_match_timecode_label()

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
        self._append_log(f"STARTUP: {BUILD_MARKER}")
        self._append_log(self.store.last_status)


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
        QGroupBox#sectionBox {
            border: 2px solid #4d525b;
            border-radius: 10px;
            margin-top: 10px;
            padding-top: 10px;
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
