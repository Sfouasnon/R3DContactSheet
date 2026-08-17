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
from .tool_resolver import resolve_ffmpeg, resolve_ffprobe, resolve_mediainfo

APP_TITLE = "R3D Contact Sheet"
MIN_OUTPUT_BYTES = 2048
REPLAY_SCRIPT_NAME = "r3dcontactsheet_last_batch.sh"
CONTACT_SHEET_NAME = "r3dcontactsheet_contact_sheet.pdf"
BUILD_MARKER = "VERIFIED UI BUILD 2026-04-01-RDCFIX"
WINDOW_TITLE = f"{APP_TITLE} - {BUILD_MARKER}"
LOGO_NAME = "r3dcontactsheet_logo.png"

# ── Palette tokens ────────────────────────────────────────────────────────────
# Warm charcoal base with amber/gold accents and teal confirmation states.
_C = {
    "bg0":        "#18191d",   # deepest background
    "bg1":        "#1f2127",   # panel background
    "bg2":        "#272b33",   # card / group box
    "bg3":        "#2e3340",   # input fields, table
    "border":     "#3d4455",   # subtle borders
    "border_hi":  "#515a6e",   # active borders
    "text0":      "#eceef2",   # primary text
    "text1":      "#b0b8c8",   # secondary text
    "text2":      "#727d90",   # muted / placeholder
    "amber":      "#d4983a",   # primary accent (buttons, active)
    "amber_hi":   "#e6ad4e",   # hover
    "amber_dim":  "#8a6220",   # disabled / pressed
    "teal":       "#3fa89a",   # success / exact match
    "teal_dim":   "#2a6e65",
    "red":        "#c05252",   # error
    "red_dim":    "#7a3333",
    "gold":       "#c9b44a",   # warning / nearest
    "dot_green":  "#4ecba6",
    "dot_yellow": "#d4983a",
    "dot_red":    "#c05252",
    "header_bg":  "#14151a",
}


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
        self.resize(1420, 1000)
        self.setMinimumSize(1240, 860)

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
        self._batch_scan_idle_text = "Scan Source"
        self._batch_render_idle_text = "Render Contact Sheets"
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
        # Sync persistence: keyed by batch_id or source hash
        self.sync_decisions: dict[str, dict] = {}
        self._pending_sync_batch_id: str | None = None

        self._build_ui()
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

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        root.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_settings_tab(),      "  Settings  ")
        self.tabs.addTab(self._build_sync_tab(),           "  Targeted Sync  ")
        self.tabs.addTab(self._build_create_tab(),         "  Create Contact Sheet  ")
        self.tabs.addTab(self._build_debug_tab(),          "  Debug  ")
        root.addWidget(self.tabs, 1)

        save_action = QAction("Remember Settings", self)
        save_action.triggered.connect(self._save_settings)
        self.addAction(save_action)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        box = QFrame()
        box.setObjectName("appHeader")
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        box.setFixedHeight(52)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.setSpacing(12)

        title = QLabel(APP_TITLE)
        title.setObjectName("headerTitle")
        layout.addWidget(title, 0, Qt.AlignVCenter)

        div = QFrame()
        div.setFrameShape(QFrame.VLine)
        div.setObjectName("headerDivider")
        layout.addWidget(div, 0, Qt.AlignVCenter)

        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        layout.addWidget(self.status_dot, 0, Qt.AlignVCenter)

        self.status_label = QLabel("Checking tools…")
        self.status_label.setWordWrap(False)
        self.status_label.setObjectName("headerStatus")
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.status_label, 1, Qt.AlignVCenter)
        return box

    # ── Tab 1: Settings ───────────────────────────────────────────────────────

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        # Left: environment
        env_box = self._section("Environment")
        env_layout = QGridLayout(env_box)
        env_layout.setColumnStretch(1, 1)
        env_layout.setHorizontalSpacing(10)
        env_layout.setVerticalSpacing(8)

        self.redline_edit = QLineEdit()
        self.redline_edit.setClearButtonEnabled(True)
        self.redline_button = QPushButton("Choose…")
        env_layout.addWidget(QLabel("REDline executable"), 0, 0)
        env_layout.addWidget(self.redline_edit, 0, 1)
        env_layout.addWidget(self.redline_button, 0, 2)

        env_layout.addWidget(QLabel("Theme"), 1, 0)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.setMaximumWidth(160)
        env_layout.addWidget(self.theme_combo, 1, 1, alignment=Qt.AlignLeft)

        # System status inside env box
        self.redline_state_label = QLabel("REDline status will appear after startup.")
        self.redline_state_label.setWordWrap(True)
        self.redline_state_label.setObjectName("labelBright")
        self.redline_path_label = QLabel("")
        self.redline_path_label.setWordWrap(True)
        self.redline_path_label.setObjectName("labelMuted")
        self.config_status_label = QLabel(self.store.last_status)
        self.config_status_label.setWordWrap(True)
        self.config_status_label.setObjectName("labelDim")

        self.tool_ffmpeg_label = QLabel("FFmpeg: checking…")
        self.tool_ffmpeg_label.setObjectName("labelMuted")
        self.tool_ffprobe_label = QLabel("FFprobe: checking…")
        self.tool_ffprobe_label.setObjectName("labelMuted")
        self.tool_mediainfo_label = QLabel("MediaInfo: checking…")
        self.tool_mediainfo_label.setObjectName("labelMuted")

        env_layout.addWidget(self._hsep(), 2, 0, 1, 3)
        env_layout.addWidget(QLabel("System status"), 3, 0)
        env_layout.addWidget(self.redline_state_label, 4, 0, 1, 3)
        env_layout.addWidget(self.redline_path_label, 5, 0, 1, 3)
        env_layout.addWidget(self.tool_ffmpeg_label, 6, 0, 1, 3)
        env_layout.addWidget(self.tool_ffprobe_label, 7, 0, 1, 3)
        env_layout.addWidget(self.tool_mediainfo_label, 8, 0, 1, 3)
        env_layout.addWidget(self.config_status_label, 9, 0, 1, 3)

        top_row.addWidget(env_box, 2)

        # Middle: RED render defaults
        red_box = self._section("RED Render Defaults")
        red_layout = QGridLayout(red_box)
        red_layout.setColumnStretch(1, 1)
        red_layout.setColumnStretch(3, 1)
        red_layout.setHorizontalSpacing(10)
        red_layout.setVerticalSpacing(8)

        self.color_sci_edit = QLineEdit()
        self.output_tone_map_edit = QLineEdit()
        self.roll_off_edit = QLineEdit()
        self.output_gamma_edit = QLineEdit()
        self.render_res_edit = QLineEdit()
        self.resize_x_edit = QLineEdit()
        self.resize_y_edit = QLineEdit()

        self._add_grid_row(red_layout, 0, "Color science",    self.color_sci_edit,
                                         "Tone map",          self.output_tone_map_edit)
        self._add_grid_row(red_layout, 1, "Roll off",         self.roll_off_edit,
                                         "Output gamma",      self.output_gamma_edit)
        self._add_grid_row(red_layout, 2, "Render res",       self.render_res_edit,
                                         "Resize X",          self.resize_x_edit)
        red_layout.addWidget(QLabel("Resize Y"), 3, 0)
        red_layout.addWidget(self.resize_y_edit, 3, 1)

        self.metadata_mode_check = QCheckBox("Use RED metadata look (recommended)")
        red_layout.addWidget(self.metadata_mode_check, 4, 0, 1, 4)

        note = QLabel(
            "Verified baseline: color science 3 · tone map 1 · roll off 2 · gamma 32 · render res 4."
        )
        note.setWordWrap(True)
        note.setObjectName("labelDim")
        red_layout.addWidget(note, 5, 0, 1, 4)
        top_row.addWidget(red_box, 2)

        # Right: generic video + organization
        right_col = QVBoxLayout()
        right_col.setSpacing(12)

        gen_box = self._section("Generic Video Defaults (FFmpeg)")
        gen_layout = QGridLayout(gen_box)
        gen_layout.setColumnStretch(1, 1)
        gen_layout.setHorizontalSpacing(10)
        gen_layout.setVerticalSpacing(8)

        self.ffmpeg_colorspace_combo = QComboBox()
        self.ffmpeg_colorspace_combo.addItems(["Rec709 (default)", "Rec2020", "P3-D65", "sRGB"])
        gen_layout.addWidget(QLabel("Default colorspace"), 0, 0)
        gen_layout.addWidget(self.ffmpeg_colorspace_combo, 0, 1)

        gen_note = QLabel(
            "Applied to generic video (MOV, MP4, MXF, BRAW, etc.) when colorspace metadata is absent."
        )
        gen_note.setWordWrap(True)
        gen_note.setObjectName("labelDim")
        gen_layout.addWidget(gen_note, 1, 0, 1, 2)
        right_col.addWidget(gen_box)

        org_box = self._section("Organization Defaults")
        org_layout = QGridLayout(org_box)
        org_layout.setColumnStretch(1, 1)
        org_layout.setColumnStretch(3, 1)
        org_layout.setHorizontalSpacing(10)
        org_layout.setVerticalSpacing(8)

        self.group_mode_combo = QComboBox()
        self.group_mode_combo.addItems(["flat", "parent_folder", "reel_prefix"])
        org_layout.addWidget(QLabel("Default grouping"), 0, 0)
        org_layout.addWidget(self.group_mode_combo, 0, 1)

        self.alphabetize_check = QCheckBox("Alphabetize clip lists")
        org_layout.addWidget(self.alphabetize_check, 0, 2, 1, 2)

        self.custom_group_edit = QLineEdit()
        self.custom_group_edit.setPlaceholderText("Optional label")
        org_layout.addWidget(QLabel("Custom group label"), 1, 0)
        org_layout.addWidget(self.custom_group_edit, 1, 1, 1, 3)
        right_col.addWidget(org_box)
        right_col.addStretch(1)

        top_row.addLayout(right_col, 2)
        layout.addLayout(top_row)
        layout.addStretch(1)
        return tab

    # ── Tab 2: Targeted Sync Selection ───────────────────────────────────────

    def _build_sync_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        # Top row: source selector + preview action
        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        source_box = self._section("Working Set")
        source_layout = QGridLayout(source_box)
        source_layout.setColumnStretch(1, 1)
        source_layout.setHorizontalSpacing(10)
        source_layout.setVerticalSpacing(6)

        self.source_edit = QLineEdit()
        self.source_edit.setClearButtonEnabled(True)
        self.choose_source_button = QPushButton("Choose Source…")
        source_layout.addWidget(QLabel("Source"), 0, 0)
        source_layout.addWidget(self.source_edit, 0, 1)
        source_layout.addWidget(self.choose_source_button, 0, 2)

        self.source_type_label = QLabel("No source selected.")
        self.source_type_label.setObjectName("labelBright")
        self.source_type_label.setWordWrap(True)
        self.source_count_label = QLabel("")
        self.source_count_label.setObjectName("labelMuted")
        self.source_count_label.setWordWrap(True)
        source_layout.addWidget(self.source_type_label, 1, 0, 1, 3)
        source_layout.addWidget(self.source_count_label, 2, 0, 1, 3)
        top_row.addWidget(source_box, 3)

        action_box = self._section("Sync Mode")
        action_layout = QVBoxLayout(action_box)
        action_layout.setSpacing(8)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode"))
        self.sync_mode_combo = QComboBox()
        self.sync_mode_combo.addItems(["Sync On", "Sync Off"])
        self.sync_mode_combo.setMaximumWidth(160)
        mode_row.addWidget(self.sync_mode_combo)
        mode_row.addStretch(1)
        action_layout.addLayout(mode_row)

        self.preview_button = QPushButton(self._preview_idle_text)
        self.preview_button.setObjectName("primaryButton")
        action_layout.addWidget(self.preview_button)

        self.preview_progress_label = QLabel("")
        self.preview_progress_label.setObjectName("labelDim")
        self.preview_progress_label.setWordWrap(True)
        action_layout.addWidget(self.preview_progress_label)
        action_layout.addStretch(1)
        top_row.addWidget(action_box, 1)
        layout.addLayout(top_row)

        # Middle row: overlap + match controls
        mid_row = QHBoxLayout()
        mid_row.setSpacing(12)

        overlap_box = self._section("Overlap Analysis")
        overlap_layout = QVBoxLayout(overlap_box)
        overlap_layout.setSpacing(4)
        self.overlap_subset_label = QLabel("No overlap subset selected yet.")
        self.overlap_subset_label.setObjectName("labelBright")
        self.overlap_subset_label.setWordWrap(True)
        self.overlap_range_label = QLabel("Overlap range will appear after preview.")
        self.overlap_range_label.setObjectName("labelMuted")
        self.overlap_range_label.setWordWrap(True)
        self.overlap_counts_label = QLabel("Subset counts will appear after preview.")
        self.overlap_counts_label.setObjectName("labelDim")
        self.overlap_counts_label.setWordWrap(True)
        self.overlap_alternates_label = QLabel("Alternate subsets will appear when available.")
        self.overlap_alternates_label.setObjectName("labelDim")
        self.overlap_alternates_label.setWordWrap(True)
        overlap_layout.addWidget(self.overlap_subset_label)
        overlap_layout.addWidget(self.overlap_range_label)
        overlap_layout.addWidget(self.overlap_counts_label)
        overlap_layout.addWidget(self.overlap_alternates_label)
        mid_row.addWidget(overlap_box, 1)

        match_box = self._section("Match Frame Selection")
        match_layout = QGridLayout(match_box)
        match_layout.setHorizontalSpacing(10)
        match_layout.setVerticalSpacing(6)
        match_layout.setColumnStretch(1, 1)
        match_layout.setColumnStretch(3, 1)

        self.preview_subset_combo = QComboBox()
        self.preview_subset_combo.setEnabled(False)
        self.selection_mode_combo = QComboBox()
        self.selection_mode_combo.addItems(["Auto", "Start of Range", "Middle of Range", "End of Range", "Custom"])
        self.selection_mode_combo.setEnabled(False)
        match_layout.addWidget(QLabel("Subset"), 0, 0)
        match_layout.addWidget(self.preview_subset_combo, 0, 1)
        match_layout.addWidget(QLabel("Mode"), 0, 2)
        match_layout.addWidget(self.selection_mode_combo, 0, 3)

        self.selected_match_label = QLabel("Match frame will appear after preview.")
        self.selected_match_label.setObjectName("labelBright")
        self.selected_match_label.setWordWrap(True)
        match_layout.addWidget(self.selected_match_label, 1, 0, 1, 4)

        self.recommended_match_label = QLabel("")
        self.recommended_match_label.setObjectName("labelMuted")
        self.recommended_match_label.setWordWrap(True)
        match_layout.addWidget(self.recommended_match_label, 2, 0, 1, 4)

        self.match_slider = QSlider(Qt.Horizontal)
        self.match_slider.setEnabled(False)
        self.match_slider.setMinimum(0)
        self.match_slider.setMaximum(0)
        match_layout.addWidget(self.match_slider, 3, 0, 1, 4)

        step_row = QHBoxLayout()
        self.match_step_back_10 = QPushButton("-10")
        self.match_step_back_1 = QPushButton("-1")
        self.match_step_forward_1 = QPushButton("+1")
        self.match_step_forward_10 = QPushButton("+10")
        for btn in (self.match_step_back_10, self.match_step_back_1,
                    self.match_step_forward_1, self.match_step_forward_10):
            btn.setEnabled(False)
            btn.setFixedWidth(52)
            step_row.addWidget(btn)
        step_row.addStretch(1)
        match_layout.addLayout(step_row, 4, 0, 1, 4)

        self.match_slider_value_label = QLabel("No valid overlap range yet.")
        self.match_slider_value_label.setObjectName("labelDim")
        self.match_slider_value_label.setWordWrap(True)
        match_layout.addWidget(self.match_slider_value_label, 5, 0, 1, 4)

        mid_row.addWidget(match_box, 2)
        layout.addLayout(mid_row)

        # Advanced timecode
        adv_row = QHBoxLayout()
        self.advanced_timecode_check = QCheckBox("Target a specific metadata timecode")
        adv_row.addWidget(self.advanced_timecode_check)
        adv_row.addStretch(1)
        layout.addLayout(adv_row)

        self.advanced_timecode_panel = QFrame()
        adv_layout = QGridLayout(self.advanced_timecode_panel)
        adv_layout.setContentsMargins(12, 6, 12, 6)
        adv_layout.setHorizontalSpacing(10)
        adv_layout.setVerticalSpacing(6)
        adv_layout.setColumnStretch(1, 1)

        self.target_timecode_edit = QLineEdit()
        self.target_timecode_edit.setPlaceholderText("HH:MM:SS:FF")
        self.target_timecode_edit.setMaximumWidth(220)
        self.fps_edit = QLineEdit()
        self.fps_edit.setPlaceholderText("Auto-detect")
        self.fps_edit.setMaximumWidth(120)
        self.drop_frame_check = QCheckBox("Drop-frame")
        adv_layout.addWidget(QLabel("Target TC"), 0, 0)
        adv_layout.addWidget(self.target_timecode_edit, 0, 1)
        adv_layout.addWidget(QLabel("FPS"), 0, 2)
        adv_layout.addWidget(self.fps_edit, 0, 3)
        adv_layout.addWidget(self.drop_frame_check, 0, 4)
        self.advanced_timecode_panel.setVisible(False)
        layout.addWidget(self.advanced_timecode_panel)

        self.frame_mode_label = QLabel("")
        self.frame_mode_label.setObjectName("labelMuted")
        self.frame_mode_label.setWordWrap(True)
        layout.addWidget(self.frame_mode_label)

        # Apply sync button
        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        self.apply_sync_button = QPushButton("Apply Sync To Contact Sheet")
        self.apply_sync_button.setObjectName("primaryButton")
        self.apply_sync_button.setEnabled(False)
        apply_row.addWidget(self.apply_sync_button)
        layout.addLayout(apply_row)

        # Bottom: clip table (dominant)
        self.preview_table = QTableWidget(0, 14)
        self.preview_table.setHorizontalHeaderLabels([
            "Camera Label", "Group", "Subgroup", "Manufacturer", "Format",
            "Clip", "FPS", "Source TC In", "Source TC Out",
            "Clip Frame", "Match Timecode", "Match Status", "Range Relation", "Sync Basis",
        ])
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked | QAbstractItemView.EditKeyPressed
        )
        self.preview_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.preview_table.setAlternatingRowColors(True)
        layout.addWidget(self.preview_table, 1)
        return tab

    # ── Tab 3: Create Contact Sheet ───────────────────────────────────────────

    def _build_create_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        # Source + output
        picker_box = self._section("Source and Output")
        picker_layout = QGridLayout(picker_box)
        picker_layout.setColumnStretch(1, 1)
        picker_layout.setHorizontalSpacing(10)
        picker_layout.setVerticalSpacing(6)

        self.batch_source_edit = QLineEdit()
        self.batch_source_edit.setClearButtonEnabled(True)
        self.batch_source_button = QPushButton("Choose Source…")
        picker_layout.addWidget(QLabel("Source folder"), 0, 0)
        picker_layout.addWidget(self.batch_source_edit, 0, 1)
        picker_layout.addWidget(self.batch_source_button, 0, 2)

        self.batch_output_edit = QLineEdit()
        self.batch_output_edit.setClearButtonEnabled(True)
        self.batch_output_button = QPushButton("Choose Output…")
        picker_layout.addWidget(QLabel("Output folder"), 1, 0)
        picker_layout.addWidget(self.batch_output_edit, 1, 1)
        picker_layout.addWidget(self.batch_output_button, 1, 2)

        hint = QLabel("Select a single Reel/Clip for one contact sheet, or a parent folder for multiple.")
        hint.setWordWrap(True)
        hint.setObjectName("labelDim")
        picker_layout.addWidget(hint, 2, 0, 1, 3)
        layout.addWidget(picker_box)

        # Planning + progress row
        plan_row = QHBoxLayout()
        plan_row.setSpacing(12)

        plan_box = self._section("Batch Planning")
        plan_layout = QGridLayout(plan_box)
        plan_layout.setColumnStretch(3, 1)
        plan_layout.setHorizontalSpacing(10)
        plan_layout.setVerticalSpacing(6)

        plan_layout.addWidget(QLabel("Sync mode"), 0, 0)
        self.batch_sync_mode_combo = QComboBox()
        self.batch_sync_mode_combo.addItems(["Sync On", "Sync Off"])
        plan_layout.addWidget(self.batch_sync_mode_combo, 0, 1)

        plan_layout.addWidget(QLabel("Auto-frame strategy"), 0, 2)
        self.batch_selection_mode_combo = QComboBox()
        self.batch_selection_mode_combo.addItems(["Auto", "Start of Range", "Middle of Range", "End of Range"])
        plan_layout.addWidget(self.batch_selection_mode_combo, 0, 3)

        self.batch_scan_button = QPushButton(self._batch_scan_idle_text)
        self.batch_scan_button.setObjectName("primaryButton")
        plan_layout.addWidget(self.batch_scan_button, 1, 0, 1, 2)

        self.batch_phase_label = QLabel("Phase: idle")
        self.batch_phase_label.setObjectName("labelBright")
        self.batch_phase_label.setWordWrap(True)
        plan_layout.addWidget(self.batch_phase_label, 1, 2, 1, 2)

        self.batch_counter_label = QLabel("Scanned clips: 0 · Built batches: 0")
        self.batch_counter_label.setObjectName("labelDim")
        self.batch_counter_label.setWordWrap(True)
        plan_layout.addWidget(self.batch_counter_label, 2, 0, 1, 4)
        plan_row.addWidget(plan_box, 3)

        progress_box = self._section("Progress")
        progress_layout = QVBoxLayout(progress_box)
        progress_layout.setSpacing(6)

        scan_label = QLabel("Scan")
        scan_label.setObjectName("labelDim")
        progress_layout.addWidget(scan_label)
        self.scan_progress_bar = QProgressBar()
        self.scan_progress_bar.setMinimum(0)
        self.scan_progress_bar.setMaximum(1)
        self.scan_progress_bar.setValue(0)
        self.scan_progress_bar.setFixedHeight(12)
        progress_layout.addWidget(self.scan_progress_bar)

        render_label = QLabel("Render")
        render_label.setObjectName("labelDim")
        progress_layout.addWidget(render_label)
        self.batch_progress_bar = QProgressBar()
        self.batch_progress_bar.setMinimum(0)
        self.batch_progress_bar.setMaximum(1)
        self.batch_progress_bar.setValue(0)
        self.batch_progress_bar.setFixedHeight(12)
        progress_layout.addWidget(self.batch_progress_bar)

        self.batch_eta_label = QLabel("ETA: --:--")
        self.batch_eta_label.setObjectName("labelDim")
        progress_layout.addWidget(self.batch_eta_label)

        self.batch_render_button = QPushButton(self._batch_render_idle_text)
        self.batch_render_button.setObjectName("primaryButton")
        self.batch_render_button.setEnabled(False)
        progress_layout.addWidget(self.batch_render_button)

        self.batch_render_note_label = QLabel("Scan a source folder to plan contact sheets before rendering.")
        self.batch_render_note_label.setObjectName("labelDim")
        self.batch_render_note_label.setWordWrap(True)
        progress_layout.addWidget(self.batch_render_note_label)
        plan_row.addWidget(progress_box, 2)
        layout.addLayout(plan_row)

        # Flat 4-tab working pane
        # Tab 0: Contact Sheet Groups
        groups_widget = QWidget()
        groups_layout = QVBoxLayout(groups_widget)
        groups_layout.setContentsMargins(8, 8, 8, 8)
        groups_layout.setSpacing(6)

        self.batch_table = QTableWidget(0, 9)
        self.batch_table.setHorizontalHeaderLabels([
            "Include", "Reel", "Clip", "Source Clips",
            "Cameras", "Clip Families", "Subgroups", "Sync Summary", "Output PDF",
        ])
        self.batch_table.horizontalHeader().setStretchLastSection(True)
        self.batch_table.verticalHeader().setVisible(False)
        self.batch_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.batch_table.setAlternatingRowColors(True)
        groups_layout.addWidget(self.batch_table, 1)

        groups_toolbar = QHBoxLayout()
        groups_toolbar.setSpacing(8)
        self.batch_selected_summary_label = QLabel("Select a row to inspect.")
        self.batch_selected_summary_label.setObjectName("labelDim")
        self.batch_selected_summary_label.setWordWrap(True)
        groups_toolbar.addWidget(self.batch_selected_summary_label, 1)
        self.batch_inspect_sync_button = QPushButton("Inspect Sync")
        self.batch_inspect_sync_button.setEnabled(False)
        groups_toolbar.addWidget(self.batch_inspect_sync_button)
        groups_layout.addLayout(groups_toolbar)

        # Tab 1: Sources
        sources_widget = QWidget()
        sources_layout = QVBoxLayout(sources_widget)
        sources_layout.setContentsMargins(8, 8, 8, 8)
        sources_layout.setSpacing(6)
        self.batch_detail_help_label = QLabel(
            "Camera Label, Clip Family, and Subgroup are editable. They affect contact sheet organization, not sync."
        )
        self.batch_detail_help_label.setObjectName("labelDim")
        self.batch_detail_help_label.setWordWrap(True)
        sources_layout.addWidget(self.batch_detail_help_label)
        self.batch_detail_table = QTableWidget(0, 6)
        self.batch_detail_table.setHorizontalHeaderLabels([
            "Source Clip", "Camera Label", "Clip Family", "Subgroup", "Manufacturer", "Format",
        ])
        self.batch_detail_table.horizontalHeader().setStretchLastSection(True)
        self.batch_detail_table.verticalHeader().setVisible(False)
        self.batch_detail_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.batch_detail_table.setAlternatingRowColors(True)
        sources_layout.addWidget(self.batch_detail_table, 1)

        # Tab 2: Needs Assignment
        unassigned_widget = QWidget()
        unassigned_layout = QVBoxLayout(unassigned_widget)
        unassigned_layout.setContentsMargins(8, 8, 8, 8)
        unassigned_layout.setSpacing(6)
        assign_toolbar = QHBoxLayout()
        assign_toolbar.setSpacing(8)
        self.unassigned_help_label = QLabel(
            "Unparsed clips appear here. Enter Reel and Clip manually or assign to a selected batch."
        )
        self.unassigned_help_label.setObjectName("labelDim")
        self.unassigned_help_label.setWordWrap(True)
        assign_toolbar.addWidget(self.unassigned_help_label, 1)
        self.assign_to_selected_batch_button = QPushButton("Assign To Selected Batch")
        self.clear_assignment_button = QPushButton("Clear Assignment")
        assign_toolbar.addWidget(self.assign_to_selected_batch_button)
        assign_toolbar.addWidget(self.clear_assignment_button)
        unassigned_layout.addLayout(assign_toolbar)
        self.unassigned_table = QTableWidget(0, 10)
        self.unassigned_table.setHorizontalHeaderLabels([
            "Include", "Source Clip", "Assignment", "Reel", "Clip",
            "Clip Family", "Subgroup", "Metadata Source", "Confidence", "Reason",
        ])
        self.unassigned_table.horizontalHeader().setStretchLastSection(True)
        self.unassigned_table.verticalHeader().setVisible(False)
        self.unassigned_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.unassigned_table.setAlternatingRowColors(True)
        unassigned_layout.addWidget(self.unassigned_table, 1)

        # Tab 3: Log
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        log_layout.setContentsMargins(8, 8, 8, 8)
        self.batch_log_text = QPlainTextEdit()
        self.batch_log_text.setReadOnly(True)
        log_layout.addWidget(self.batch_log_text, 1)

        # Assemble flat tab widget
        self.batch_detail_tabs = QTabWidget()
        self.batch_detail_tabs.setObjectName("workPane")
        self.batch_detail_tabs.addTab(groups_widget,     "  Contact Sheet Groups  ")
        self.batch_detail_tabs.addTab(sources_widget,    "  Sources  ")
        self.batch_detail_tabs.addTab(unassigned_widget, "  Needs Assignment  ")
        self.batch_detail_tabs.addTab(log_widget,        "  Log  ")

        # Stub widgets kept for API compatibility
        self.batch_needs_assignment_button = QPushButton()
        self.batch_needs_assignment_button.setVisible(False)
        self.batch_details_button = QPushButton()
        self.batch_details_button.setVisible(False)
        self.batch_log_toggle_button = QPushButton()
        self.batch_log_toggle_button.setVisible(False)
        self.batch_hide_details_button = QPushButton()
        self.batch_hide_details_button.setVisible(False)
        self.batch_inline_summary_label = QLabel()
        self.batch_inline_summary_label.setVisible(False)
        self.batch_inline_panel = QFrame()
        self.batch_inline_panel.setVisible(False)

        layout.addWidget(self.batch_detail_tabs, 1)
        return tab

    # ── Tab 4: Debug ──────────────────────────────────────────────────────────

    def _build_debug_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        self.summary_label = QLabel("No renders active.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("labelBright")
        layout.addWidget(self.summary_label)

        self.replay_label = QLabel("Replay script will appear here after rendering.")
        self.replay_label.setWordWrap(True)
        self.replay_label.setObjectName("labelMuted")
        layout.addWidget(self.replay_label)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text, 1)
        return tab

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _section(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setObjectName("sectionBox")
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        return box

    def _hsep(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("hSep")
        return line

    def _add_grid_row(self, layout: QGridLayout, row: int,
                      label1: str, widget1: QLineEdit,
                      label2: str, widget2: QLineEdit) -> None:
        layout.addWidget(QLabel(label1), row, 0)
        layout.addWidget(widget1, row, 1)
        layout.addWidget(QLabel(label2), row, 2)
        layout.addWidget(widget2, row, 3)

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        self.redline_button.clicked.connect(self._choose_redline)
        self.choose_source_button.clicked.connect(self._choose_input_folder)
        self.batch_source_button.clicked.connect(self._choose_batch_input_folder)
        self.batch_output_button.clicked.connect(self._choose_batch_output)
        self.batch_scan_button.clicked.connect(self.scan_batches)
        self.batch_render_button.clicked.connect(self.run_batch_jobs)
        self.preview_button.clicked.connect(self.preview_jobs)
        self.apply_sync_button.clicked.connect(self._apply_sync_to_contact_sheet)
        self.batch_inspect_sync_button.clicked.connect(self._inspect_sync_for_selected_batch)

        self.preview_subset_combo.currentIndexChanged.connect(self._apply_preview_selection)
        self.selection_mode_combo.currentTextChanged.connect(self._apply_preview_selection)
        self.match_slider.valueChanged.connect(self._on_match_slider_changed)
        self.match_step_back_10.clicked.connect(lambda: self._step_match_slider(-10))
        self.match_step_back_1.clicked.connect(lambda: self._step_match_slider(-1))
        self.match_step_forward_1.clicked.connect(lambda: self._step_match_slider(1))
        self.match_step_forward_10.clicked.connect(lambda: self._step_match_slider(10))

        self.redline_edit.editingFinished.connect(self._refresh_redline_probe)
        self.source_edit.editingFinished.connect(self._refresh_source_state)
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

    # ── Settings apply / save ─────────────────────────────────────────────────

    def _apply_settings_to_ui(self) -> None:
        self._set_path_field(self.redline_edit, self.settings.redline_path)
        self._set_path_field(self.source_edit, self.settings.last_input_path)
        self._set_path_field(self.batch_source_edit, getattr(self.settings, "batch_input_path", ""))
        self._set_path_field(self.batch_output_edit, getattr(self.settings, "batch_output_path", ""))
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
        safe_group = self.settings.group_mode if self.settings.group_mode in {"flat", "parent_folder", "reel_prefix"} else "flat"
        self.group_mode_combo.setCurrentText(safe_group)
        self.custom_group_edit.setText(getattr(self.settings, "custom_group_name", ""))
        self.alphabetize_check.setChecked(self.settings.alphabetize)
        self.metadata_mode_check.setChecked(self.settings.metadata_mode)
        self.sync_mode_combo.setCurrentText(
            "Sync Off" if getattr(self.settings, "sync_mode", "sync_on") == "sync_off" else "Sync On"
        )
        self.theme_combo.setCurrentText(
            "Light" if getattr(self.settings, "theme_name", "dark") == "light" else "Dark"
        )
        self.batch_sync_mode_combo.setCurrentText(
            "Sync Off" if getattr(self.settings, "batch_sync_mode", "sync_on") == "sync_off" else "Sync On"
        )
        selection_map = {"auto": "Auto", "start": "Start of Range", "middle": "Middle of Range", "end": "End of Range"}
        self.batch_selection_mode_combo.setCurrentText(
            selection_map.get(getattr(self.settings, "batch_selection_mode", "auto"), "Auto")
        )

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
            last_output_path=self.batch_output_edit.text().strip(),
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
                "Auto": "auto", "Start of Range": "start",
                "Middle of Range": "middle", "End of Range": "end",
            }.get(self.batch_selection_mode_combo.currentText(), "auto"),
            theme_name=self.theme_combo.currentText().strip().lower(),
        )
        self.store.save(settings)
        self.config_status_label.setText(self.store.last_status)

    # ── File choosers ─────────────────────────────────────────────────────────

    def _choose_redline(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose REDline executable")
        if path:
            self._set_path_field(self.redline_edit, path)
            self._save_settings()
            self._refresh_redline_probe()

    def _choose_input_folder(self) -> None:
        selection = QFileDialog.getExistingDirectory(self, "Choose Source Folder / Reel")
        if selection:
            self._set_input_path(selection)

    def _choose_batch_input_folder(self) -> None:
        selection = QFileDialog.getExistingDirectory(self, "Choose Source Folder")
        if selection:
            path = Path(selection).expanduser().resolve()
            self._set_path_field(self.batch_source_edit, str(path))
            self._append_log(f"Batch source: {path}")
            self._refresh_batch_state()
            self._save_settings()

    def _choose_batch_output(self) -> None:
        selection = QFileDialog.getExistingDirectory(self, "Choose Output Folder")
        if selection:
            path = Path(selection).expanduser().resolve()
            self._set_path_field(self.batch_output_edit, str(path))
            self._append_log(f"Output folder: {path}")
            self._refresh_batch_state()
            self._save_settings()

    def _set_input_path(self, value: str) -> None:
        resolved = Path(value).expanduser().resolve()
        self._set_path_field(self.source_edit, str(resolved))
        self._refresh_source_state()
        self._append_log(describe_source_selection(resolved))
        self._save_settings()

    # ── State refresh ─────────────────────────────────────────────────────────

    def _refresh_source_state(self) -> None:
        value = self.source_edit.text().strip()
        if not value:
            self.source_type_label.setText("No source selected.")
            self.source_count_label.setText("")
            return
        path = Path(value).expanduser().resolve()
        self.source_type_label.setText(describe_source_selection(path))
        if not path.exists():
            self.source_count_label.setText("Source path does not exist.")
            return
        try:
            clips = discover_media_clips(
                path,
                group_mode=self.group_mode_combo.currentText(),  # type: ignore[arg-type]
                alphabetize=self.alphabetize_check.isChecked(),
            )
            providers = sorted({clip.provider_kind for clip in clips})
            self.source_count_label.setText(
                f"{len(clips)} clip(s) · providers: {', '.join(providers)}"
            )
        except Exception as exc:
            self.source_count_label.setText(str(exc))

    def _refresh_batch_state(self) -> None:
        source_text = self.batch_source_edit.text().strip()
        output_text = self.batch_output_edit.text().strip()
        if not source_text:
            self.batch_phase_label.setText("Phase: idle")
            self.batch_counter_label.setText("Scanned clips: 0 · Built batches: 0")
            self.batch_render_button.setEnabled(False)
            self._update_batch_secondary_actions()
            return
        source_path = Path(source_text).expanduser().resolve()
        if not source_path.exists():
            self.batch_phase_label.setText("Phase: source missing")
            self.batch_render_button.setEnabled(False)
            self._update_batch_secondary_actions()
            return
        if output_text:
            output_path = Path(output_text).expanduser().resolve()
            if output_path == source_path or source_path in output_path.parents:
                self.batch_phase_label.setText("Phase: output must be outside source tree")
                self.batch_render_button.setEnabled(False)
                self._update_batch_secondary_actions()
                return
        self.batch_phase_label.setText("Phase: ready to scan")
        self._update_batch_secondary_actions()

    def _refresh_redline_probe(self) -> None:
        explicit = Path(self.redline_edit.text()).expanduser() if self.redline_edit.text().strip() else None
        probe = probe_redline(paths=None if explicit is None else RedlinePaths(explicit_path=explicit))
        if probe.available and probe.executable:
            self._set_path_field(self.redline_edit, str(probe.executable))
        self.redline_ready = bool(probe.available and probe.compatible and probe.executable)

        # Tool availability
        ffmpeg_path = resolve_ffmpeg()
        ffprobe_path = resolve_ffprobe()
        mediainfo_path = resolve_mediainfo()

        self.tool_ffmpeg_label.setText(
            f"FFmpeg: {'✓ ' + ffmpeg_path if ffmpeg_path else '✗ not found'}"
        )
        self.tool_ffprobe_label.setText(
            f"FFprobe: {'✓ ' + ffprobe_path if ffprobe_path else '✗ not found'}"
        )
        self.tool_mediainfo_label.setText(
            f"MediaInfo: {'✓ ' + mediainfo_path if mediainfo_path else '○ not found (optional)'}"
        )

        if probe.available and probe.executable:
            self.redline_state_label.setText(
                "REDline found and executable." if probe.compatible
                else "REDline found but incompatible — update REDCINE-X PRO."
            )
            self.redline_path_label.setText(str(probe.executable))
        elif probe.bundle_selected and probe.bundle_path is not None:
            self.redline_state_label.setText(
                "REDCINE-X bundle selected — choose the REDline binary inside Contents/MacOS."
            )
            self.redline_path_label.setText(str(probe.bundle_path))
        elif explicit is not None:
            self.redline_state_label.setText("Selected path is not a valid REDline executable.")
            self.redline_path_label.setText(str(explicit))
        else:
            self.redline_state_label.setText(
                "REDline not found. RED clips require REDCINE-X PRO. Generic video still works if FFmpeg is available."
            )
            self.redline_path_label.setText("")

        self.config_status_label.setText(self.store.last_status)

        # Health state: green only when truly ready
        redline_ok = probe.available and probe.compatible
        ffmpeg_ok = bool(ffmpeg_path and ffprobe_path)
        if redline_ok and ffmpeg_ok:
            self._set_health_state("green", "All tools ready. REDline and FFmpeg confirmed.")
        elif redline_ok:
            self._set_health_state("yellow", "REDline ready. FFmpeg/FFprobe not found — generic video unavailable.")
        elif ffmpeg_ok:
            self._set_health_state("yellow", "FFmpeg ready. REDline unavailable — RED clips will not render.")
        else:
            self._set_health_state("red", "No render tools found. Configure REDline and/or install FFmpeg.")

        self._log_probe_message_once(probe.message)

    def _refresh_frame_mode_summary(self) -> None:
        tc_text = self.target_timecode_edit.text().strip()
        fps_text = self.fps_edit.text().strip()
        mode_text = self.sync_mode_combo.currentText()
        if self.advanced_timecode_check.isChecked() and tc_text:
            drop_text = "drop-frame" if self.drop_frame_check.isChecked() else "non-drop-frame"
            fps_summary = fps_text if fps_text else "auto-detect"
            self.frame_mode_label.setText(
                f"{mode_text} · targeting {tc_text} ({fps_summary}, {drop_text})"
            )
            return
        if mode_text == "Sync Off":
            self.frame_mode_label.setText(
                "Sync Off — coherent frames chosen quietly; sync diagnostics suppressed on the contact sheet."
            )
        else:
            self.frame_mode_label.setText(
                "Sync On — matching moment resolved automatically from clip metadata across sync-eligible clips."
            )

    def _toggle_advanced_timecode(self, enabled: bool) -> None:
        self.advanced_timecode_panel.setVisible(enabled)
        if not enabled:
            self.target_timecode_edit.clear()
            self.fps_edit.clear()
            self.drop_frame_check.setChecked(False)
        self._refresh_frame_mode_summary()

    # ── Sync tab: persistence + navigation ───────────────────────────────────

    def _apply_sync_to_contact_sheet(self) -> None:
        if not self.preview_context:
            return
        selection = self._selection_state_from_widgets()
        key = self._pending_sync_batch_id or self._source_hash()
        self.sync_decisions[key] = {
            "sync_mode": self.sync_mode_combo.currentText(),
            "active_subset_id": selection.active_subset_id,
            "selected_abs_frame": selection.selected_abs_frame,
            "selection_mode": selection.selection_mode,
        }
        self._append_log(f"Sync applied for key '{key}'.")
        self._pending_sync_batch_id = None
        self.tabs.setCurrentIndex(2)  # return to Create Contact Sheet

    def _inspect_sync_for_selected_batch(self) -> None:
        batch_id = self._selected_batch_id()
        if not batch_id:
            return
        group = next((g for g in self.batch_groups if g.batch_id == batch_id), None)
        if group is None:
            return
        # Pre-load source field from batch group into sync tab
        self._pending_sync_batch_id = batch_id
        # If we have a stored source path from the batch, pass it to the sync tab source
        # (the sync tab will use whatever is in source_edit; for batch-driven inspection,
        # we set it to the batch source folder and let the user re-preview)
        batch_src = self.batch_source_edit.text().strip()
        if batch_src:
            self._set_path_field(self.source_edit, batch_src)
            self._refresh_source_state()
        self._append_log(
            f"Inspecting sync for Reel {group.reel_number} / Clip {group.clip_number}. "
            "Preview the working set, adjust the match frame, then click 'Apply Sync To Contact Sheet'."
        )
        self.tabs.setCurrentIndex(1)  # go to Targeted Sync

    def _source_hash(self) -> str:
        """Stable key for non-batch sync decisions."""
        return self.source_edit.text().strip() or "unknown"

    # ── Preview / sync tab workers ────────────────────────────────────────────

    def _build_preview_context(self, *, progress_callback=None) -> PreviewContext:
        input_path = self._validate_input_path()
        output_path = self._validate_batch_output_path_for_preview()
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
        return build_preview_context(
            clips,
            options,
            metadata_cache=self.metadata_cache,
            progress_callback=progress_callback,
        )

    def _apply_preview_context(self, context: PreviewContext) -> None:
        self.preview_context = context
        self.current_selection = context.selection
        self.preview_subset_combo.blockSignals(True)
        self.preview_subset_combo.clear()
        for subset in context.overlap_subsets:
            label = f"{len(subset.clip_paths)} clip(s) · {subset.start_timecode} → {subset.end_timecode}"
            self.preview_subset_combo.addItem(label, subset.subset_id)
        self.preview_subset_combo.setEnabled(bool(context.overlap_subsets))
        active_index = 0
        for i in range(self.preview_subset_combo.count()):
            if self.preview_subset_combo.itemData(i) == context.selection.active_subset_id:
                active_index = i
                break
        if self.preview_subset_combo.count():
            self.preview_subset_combo.setCurrentIndex(active_index)
        self.preview_subset_combo.blockSignals(False)
        self.selection_mode_combo.blockSignals(True)
        mode_map = {"auto": "Auto", "start": "Start of Range", "middle": "Middle of Range",
                    "end": "End of Range", "custom": "Custom"}
        self.selection_mode_combo.setCurrentText(mode_map.get(context.selection.selection_mode, "Auto"))
        self.selection_mode_combo.setEnabled(bool(context.overlap_subsets))
        self.selection_mode_combo.blockSignals(False)
        self._sync_selection_widgets()
        self._refresh_plan_from_selection(write_replay=False)
        self.apply_sync_button.setEnabled(True)

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
        mode_map = {"Auto": "auto", "Start of Range": "start", "Middle of Range": "middle",
                    "End of Range": "end", "Custom": "custom"}
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
        return MatchSelectionState(active_subset_id=subset.subset_id,
                                   selected_abs_frame=selected_abs, selection_mode=mode)

    def _sync_selection_widgets(self) -> None:
        subset = self._current_active_subset()
        has_subset = subset is not None
        is_custom = bool(has_subset and self.selection_mode_combo.currentText() == "Custom")
        self.match_slider.setEnabled(is_custom)
        for btn in (self.match_step_back_10, self.match_step_back_1,
                    self.match_step_forward_1, self.match_step_forward_10):
            btn.setEnabled(is_custom)
        if not has_subset:
            self.selected_match_label.setText("No valid overlap subset available.")
            self.recommended_match_label.setText("Per-clip metadata timecodes remain authoritative.")
            self.match_slider_value_label.setText("")
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
            f"Selected: {selected_tc or 'Unavailable'} (abs frame {selected_abs})"
        )
        self.recommended_match_label.setText(
            f"Recommended: {self._timecode_for_subset_frame(subset, subset.recommended_abs_frame) or subset.start_timecode} · "
            f"{subset.shared_frame_count} shared frame(s)"
        )
        self.match_slider_value_label.setText(
            f"{subset.start_timecode} → {subset.end_timecode} · offset {selected_abs - subset.start_abs_frame}"
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
        self._refresh_plan_from_selection(write_replay=False)

    def _on_match_slider_changed(self, _value: int) -> None:
        if self.selection_mode_combo.currentText() != "Custom":
            return
        self._apply_preview_selection()

    def _step_match_slider(self, delta: int) -> None:
        if not self.match_slider.isEnabled():
            return
        v = self.match_slider.value()
        self.match_slider.setValue(max(self.match_slider.minimum(), min(self.match_slider.maximum(), v + delta)))

    def _refresh_plan_from_selection(self, *, write_replay: bool) -> None:
        if not self.preview_context:
            return
        selection = self._selection_state_from_widgets()
        self.current_selection = selection
        self.plan = build_job_plan_from_context(self.preview_context, selection)
        self._render_plan()
        self.summary_label.setText(
            f"Preview ready: {len(self.plan)} render job(s) queued."
        )
        self._update_overlap_analysis()
        self.frame_mode_label.setText(self._frame_mode_summary_from_plan(self.plan))
        self._update_health_from_plan(self.plan)

    def _update_overlap_analysis(self) -> None:
        subset = self._current_active_subset()
        if not self.preview_context or subset is None:
            self.overlap_subset_label.setText("No overlap subset selected yet.")
            self.overlap_range_label.setText("Overlap range will appear after preview.")
            self.overlap_counts_label.setText("")
            self.overlap_alternates_label.setText("")
            return
        sync_mode = self._plan_sync_mode(self.plan)
        exact = sum(1 for item in self.plan if item.frame_resolution.sync_status == "exact_match")
        earlier = sum(1 for item in self.plan if item.frame_resolution.range_relation == "Earlier Only")
        later = sum(1 for item in self.plan if item.frame_resolution.range_relation == "Later Only")
        outside = max(0, len(self.plan) - len(subset.clip_paths))
        self.overlap_subset_label.setText(
            f"Subset: {len(subset.clip_paths)} clip(s) · frame {self.current_selection.selected_abs_frame or subset.recommended_abs_frame}"
        )
        self.overlap_range_label.setText(
            f"Range: {subset.start_timecode} → {subset.end_timecode} · {subset.shared_frame_count} shared frames"
        )
        self.overlap_counts_label.setText(
            f"Exact: {exact} · outside subset: {outside} · earlier: {earlier} · later: {later}"
        )
        alternates = [s for s in self.preview_context.overlap_subsets if s.subset_id != subset.subset_id]
        if alternates:
            preview = " | ".join(
                f"{len(s.clip_paths)} clips · {s.start_timecode} → {s.end_timecode}" for s in alternates[:3]
            )
            self.overlap_alternates_label.setText(f"Alternates: {preview}")
        else:
            self.overlap_alternates_label.setText("")

    def preview_jobs(self) -> None:
        if self.preview_worker and self.preview_worker.is_alive():
            return
        self.last_contact_sheet_pdf = None
        self._set_preview_busy(True, "Analyzing…")
        self.preview_progress_label.setText(_format_preview_progress(0, 0))
        QCoreApplication.processEvents()
        time.sleep(0.05)
        QCoreApplication.processEvents()
        self.preview_worker = threading.Thread(target=self._run_preview_worker, daemon=True)
        self.preview_worker.start()

    def _run_preview_worker(self) -> None:
        try:
            context = self._build_preview_context(
                progress_callback=lambda processed, total, clip, metadata: self.event_queue.put((
                    "preview-progress",
                    {"processed": processed, "total": total, "clip_name": clip.clip_name},
                ))
            )
            self.event_queue.put(("preview-ready", context))
        except Exception as exc:
            self.event_queue.put(("preview-failure", {"error": str(exc), "traceback": traceback.format_exc()}))

    # ── Batch scan / render workers ───────────────────────────────────────────

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
        self.batch_counter_label.setText("Scanned clips: 0 · Built batches: 0")
        self.batch_eta_label.setText("ETA: calculating…")
        self.scan_progress_bar.setMaximum(1)
        self.scan_progress_bar.setValue(0)
        self.batch_progress_bar.setValue(0)
        QCoreApplication.processEvents()
        time.sleep(0.05)
        QCoreApplication.processEvents()
        self.batch_scan_worker = threading.Thread(target=self._run_batch_scan_worker, daemon=True)
        self.batch_scan_worker.start()

    def run_batch_jobs(self) -> None:
        if self.batch_render_worker and self.batch_render_worker.is_alive():
            return
        if self.batch_groups and self.batch_render_button.text() == "Open Output Folder":
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
        total_clips = sum(len(g.clips) for g in selected)
        self.batch_progress_bar.setMaximum(max(total_clips, 1))
        self.batch_progress_bar.setValue(0)
        self.batch_render_note_label.setText("Resolving metadata, rendering stills, assembling PDFs…")
        self.batch_render_button.setEnabled(False)
        self.batch_render_button.setText("Rendering…")
        self.batch_phase_label.setText("Phase: resolving metadata")
        self.batch_counter_label.setText(f"0 / {len(selected)} batches · 0 / {total_clips} clips")
        self.batch_eta_label.setText("ETA: calculating…")
        self.summary_label.setText(f"Rendering {len(selected)} contact sheet(s) — {total_clips} clip(s) total.")
        self.replay_label.setText(f"Output: {output_dir}")
        self._append_batch_log(f"Starting render for {len(selected)} group(s) into {output_dir}")
        self._append_log(f"Batch render started: {len(selected)} group(s), {total_clips} clip(s) → {output_dir}")
        self.batch_render_worker = threading.Thread(
            target=self._run_batch_render_worker, args=(selected,), daemon=True
        )
        self.batch_render_worker.start()

    def _run_batch_scan_worker(self) -> None:
        try:
            source_path = self._validate_batch_source_path()
            redline_path = None
            try:
                redline_path = str(self._validate_redline_path())
            except Exception:
                pass
            result = build_batch_scan_result(
                source_path,
                alphabetize=self.alphabetize_check.isChecked(),
                redline_exe=redline_path,
                progress_callback=lambda phase, processed, total: self.event_queue.put(
                    ("batch-scan-progress", {"phase": phase, "processed": processed, "total": total})
                ),
                log_callback=lambda msg: self.event_queue.put(("batch-log", msg)),
            )
            self.event_queue.put(("batch-scan-ready", {"result": result, "source": str(source_path)}))
        except Exception as exc:
            self.event_queue.put(("batch-scan-failure", {"error": str(exc)}))

    def _run_batch_render_worker(self, selected_groups: list[BatchGroup]) -> None:
        # Create output directories now, at render time
        try:
            output_root = self._validate_batch_output_path()
            output_root.mkdir(parents=True, exist_ok=True)
            # Verify writeability
            with tempfile.NamedTemporaryFile(prefix="r3dcs_", dir=output_root, delete=True):
                pass
        except Exception as exc:
            self.event_queue.put(("batch-render-done", {
                "elapsed": 0.0, "batches_done": 0,
                "batches_total": len(selected_groups), "failures": len(selected_groups),
                "pdfs": [], "output_root": "",
                "error": str(exc),
            }))
            return

        redline_path = None
        if any(any(clip.provider_kind == "red" for clip in g.clips) for g in selected_groups):
            try:
                redline_path = self._validate_redline_path()
                self.event_queue.put(("batch-log", f"REDline: {redline_path}"))
            except Exception as exc:
                self.event_queue.put(("batch-log", f"REDline unavailable: {exc}"))

        total_metadata_clips = sum(len(g.clips) for g in selected_groups)
        total_render_jobs = total_metadata_clips
        processed_metadata = 0
        completed_render_jobs = 0
        completed_batches = 0
        failures = 0
        written_pdfs: list[str] = []

        for batch_index, group in enumerate(selected_groups, start=1):
            batch_pdf_path = build_batch_output_path(output_root, group)
            batch_pdf_path.parent.mkdir(parents=True, exist_ok=True)

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

            # Apply stored sync decision if present
            stored_sync = self.sync_decisions.get(group.batch_id)

            context = build_preview_context(
                list(group.clips),
                options,
                metadata_cache=self.metadata_cache,
                progress_callback=lambda processed, total, clip, metadata, base=processed_metadata: self.event_queue.put((
                    "batch-render-metadata-progress",
                    {
                        "processed": base + processed,
                        "total": total_metadata_clips,
                        "clip_name": clip.clip_name,
                        "batch_label": f"Reel {group.reel_number} / Clip {group.clip_number}",
                    },
                )),
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

            if stored_sync:
                selection = MatchSelectionState(
                    active_subset_id=stored_sync.get("active_subset_id"),
                    selected_abs_frame=stored_sync.get("selected_abs_frame"),
                    selection_mode=stored_sync.get("selection_mode", "auto"),
                )
            else:
                selection = self._batch_selection_for_context(context)

            plan = build_job_plan_from_context(context, selection)

            # Create frames subdir for this batch
            frames_dir = batch_pdf_path.parent / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)

            self.event_queue.put(("batch-render-state", {
                "phase": "rendering stills",
                "current_batch": f"Reel {group.reel_number} / Clip {group.clip_number} ({batch_index}/{len(selected_groups)})",
                "batches_done": completed_batches,
                "batches_total": len(selected_groups),
                "rendered_jobs": completed_render_jobs,
                "render_total": total_render_jobs,
            }))

            outcomes = render_plan_items_parallel(
                plan,
                redline_exe=str(redline_path) if redline_path else None,
                min_output_bytes=MIN_OUTPUT_BYTES,
                progress_callback=lambda outcome, completed, total,
                    base=completed_render_jobs,
                    group_label=f"Reel {group.reel_number} / Clip {group.clip_number}":
                    self.event_queue.put(("batch-render-job-progress", {
                        "outcome": outcome,
                        "processed": base + completed,
                        "total": total_render_jobs,
                        "batch_completed": completed,
                        "batch_total": total,
                        "batch_label": group_label,
                    })),
            )
            completed_render_jobs += len(plan)
            batch_failures = sum(1 for o in outcomes if o.error is not None)
            failures += batch_failures

            # PDF assembly in worker thread (not main thread)
            self.event_queue.put(("batch-render-state", {
                "phase": "assembling PDF",
                "current_batch": f"Reel {group.reel_number} / Clip {group.clip_number} ({batch_index}/{len(selected_groups)})",
                "batches_done": completed_batches,
                "batches_total": len(selected_groups),
                "rendered_jobs": completed_render_jobs,
                "render_total": total_render_jobs,
            }))
            try:
                pdf_path = self._build_contact_sheet_pdf_for_plan(
                    plan,
                    output_dir=options.output_dir,
                    destination_name=batch_pdf_path.name,
                    sync_mode_override=options.sync_mode,
                )
                written_pdfs.append(str(pdf_path))
                self.event_queue.put(("batch-log", f"PDF: {pdf_path}"))
            except Exception as exc:
                self.event_queue.put(("batch-log", f"PDF failed for {group.batch_id}: {exc}"))
                failures += 1

            completed_batches += 1

        elapsed = time.time() - self.batch_render_started_at
        self.event_queue.put(("batch-render-done", {
            "elapsed": elapsed,
            "batches_done": completed_batches,
            "batches_total": len(selected_groups),
            "failures": failures,
            "pdfs": written_pdfs,
            "output_root": str(output_root),
        }))

    # ── Event polling ─────────────────────────────────────────────────────────

    def _poll_events(self) -> None:
        while True:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if event == "preview-progress":
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

    def _on_preview_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self.preview_progress_label.setText(_format_preview_progress(data["processed"], data["total"]))

    def _on_preview_ready(self, payload: object) -> None:
        context = payload  # type: ignore[assignment]
        self._apply_preview_context(context)
        self._append_log(f"Preview ready: {len(self.plan)} job(s), {len(context.clips)} clip(s).")
        self._set_preview_busy(False)
        self.preview_progress_label.setText("Preview ready.")
        self.preview_worker = None

    def _on_preview_failure(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self._append_log(str(data["error"]))
        self._set_health_state("red", "Preview failed — check REDline, source metadata, and output settings.")
        self._set_preview_busy(False)
        self.preview_progress_label.setText("Preview failed.")
        self.preview_worker = None
        QMessageBox.critical(self, APP_TITLE, str(data["error"]))

    def _on_batch_scan_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        phase, processed, total = data["phase"], data["processed"], data["total"]
        self.scan_progress_bar.setMaximum(max(total, 1))
        self.scan_progress_bar.setValue(processed)
        elapsed = time.time() - self.batch_scan_started_at if self.batch_scan_started_at else 0.0
        self.batch_eta_label.setText(_format_eta(elapsed, processed, total))
        if phase == "build":
            self.batch_phase_label.setText("Phase: building batches")
            self.batch_counter_label.setText(f"Clips: scanned · Batches: {processed} / {total}")
        else:
            self.batch_phase_label.setText("Phase: scanning clips")
            self.batch_counter_label.setText(f"Clips: {processed} / {total}")

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
            f"Clips: {len(self.batch_assignments)} · Batches: {len(self.batch_groups)} · "
            f"Needs assignment: {self._needs_assignment_count()}"
        )
        self.batch_eta_label.setText("ETA: complete")
        self.scan_progress_bar.setValue(self.scan_progress_bar.maximum())
        self.batch_render_note_label.setText(
            f"{len(self.batch_groups)} contact sheet(s) ready. "
            f"{self._needs_assignment_count()} clip(s) awaiting assignment."
        )
        self._append_batch_log(
            f"Scan complete: {len(self.batch_groups)} batch(es), {self._needs_assignment_count()} need assignment."
        )
        self._set_batch_scan_busy(False)
        self.batch_scan_worker = None

    def _on_batch_scan_failure(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self._append_batch_log(data["error"])
        self.batch_phase_label.setText("Phase: scan failed")
        self.batch_eta_label.setText("ETA: --:--")
        self._update_batch_secondary_actions()
        self._set_batch_scan_busy(False)
        self.batch_scan_worker = None
        QMessageBox.critical(self, APP_TITLE, str(data["error"]))

    def _on_batch_render_state(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self.batch_phase_label.setText(f"Phase: {data['phase']}")
        self.batch_counter_label.setText(
            f"Batches: {data['batches_done']} / {data['batches_total']} · "
            f"Clips: {data['rendered_jobs']} / {data['render_total']}"
        )

    def _on_batch_render_metadata_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        processed, total = data["processed"], data["total"]
        self.batch_phase_label.setText("Phase: resolving metadata")
        self.batch_counter_label.setText(
            f"Metadata: {processed} / {total} · {data['batch_label']}"
        )
        self.batch_progress_bar.setMaximum(max(total, 1))
        self.batch_progress_bar.setValue(processed)
        elapsed = time.time() - self.batch_render_started_at if self.batch_render_started_at else 0.0
        self.batch_eta_label.setText(_format_eta(elapsed, processed, total))

    def _on_batch_render_job_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        processed, total = data["processed"], data["total"]
        self.batch_phase_label.setText("Phase: rendering stills")
        self.batch_counter_label.setText(
            f"Stills: {processed} / {total} · {data['batch_label']} ({data['batch_completed']}/{data['batch_total']})"
        )
        self.batch_progress_bar.setMaximum(max(total, 1))
        self.batch_progress_bar.setValue(processed)
        elapsed = time.time() - self.batch_render_started_at if self.batch_render_started_at else 0.0
        self.batch_eta_label.setText(_format_eta(elapsed, processed, total))
        outcome = data["outcome"]
        if outcome.error is None:
            self._append_batch_log(f"{data['batch_label']}: job {outcome.index} ✓")
        else:
            self._append_batch_log(f"{data['batch_label']}: job {outcome.index} ✗ {outcome.error}")
            self._append_log(f"RENDER FAILURE — {data['batch_label']} job {outcome.index}: {outcome.error}")

    def _on_batch_render_done(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self.batch_render_worker = None
        self.batch_phase_label.setText("Phase: complete")
        self.batch_counter_label.setText(
            f"Batches: {data['batches_done']} / {data['batches_total']} · Failures: {data['failures']}"
        )
        self.batch_eta_label.setText("ETA: complete")
        pdfs = data.get("pdfs", [])
        self.batch_render_note_label.setText(
            f"Render complete. {len(pdfs)} PDF(s) written to {data.get('output_root', '')}."
        )
        self.batch_render_button.setEnabled(True)
        self.batch_render_button.setText("Open Output Folder")
        self.batch_progress_bar.setValue(self.batch_progress_bar.maximum())
        done_msg = f"Done in {data['elapsed']:.1f}s. {data['failures']} failure(s). {len(pdfs)} PDF(s) written."
        self._append_batch_log(done_msg)
        self._append_log(done_msg)
        self.summary_label.setText(
            f"Render complete — {len(pdfs)} PDF(s) written in {data['elapsed']:.1f}s."
            if not data["failures"]
            else f"Render finished with {data['failures']} failure(s). {len(pdfs)} PDF(s) written. Check Debug log."
        )
        self.replay_label.setText(f"Output folder: {data.get('output_root', '')}")
        if data["failures"]:
            self._set_health_state("red", f"{data['failures']} render failure(s). Review the Debug log.")
        else:
            self._set_health_state("green", f"{len(pdfs)} contact sheet PDF(s) written successfully.")

    # ── Table rendering ───────────────────────────────────────────────────────

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
                item.frame_resolution.source_timecode_in or "—",
                item.frame_resolution.source_timecode_out or "—",
                str(item.frame_resolution.frame_index),
                item.frame_resolution.match_timecode or "—",
                sync_status,
                item.frame_resolution.range_relation or "—",
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
                group.reel_number, group.clip_number, str(group.source_clip_count),
                self._batch_camera_summary(group), self._batch_family_summary(group),
                self._batch_subgroup_summary(group), self._batch_sync_summary(group),
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

    def _render_unassigned_table(self) -> None:
        rows = [
            a for a in self.batch_assignments
            if a.assignment_state in {"needs_assignment", "excluded_by_operator"}
        ]
        rows.sort(key=lambda a: self._display_clip_label(a.clip.clip_name).lower())
        self.unassigned_table_updating = True
        self.unassigned_table.setRowCount(len(rows))
        for row, assignment in enumerate(rows):
            include_item = QTableWidgetItem()
            include_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            include_item.setCheckState(
                Qt.Unchecked if assignment.assignment_state == "excluded_by_operator" else Qt.Checked
            )
            include_item.setData(Qt.UserRole, str(assignment.clip.source_path))
            self.unassigned_table.setItem(row, 0, include_item)
            state_label = {
                "auto_assigned": "Auto-assigned",
                "needs_assignment": "Needs assignment",
                "excluded_by_operator": "Excluded",
            }[assignment.assignment_state]
            values = (
                assignment.clip.clip_name, state_label,
                assignment.reel_number or "", assignment.clip_number or "",
                assignment.clip_family, assignment.subgroup_name,
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

    def _render_selected_batch_detail(self) -> None:
        batch_id = self._selected_batch_id()
        self.batch_detail_table_updating = True
        self.batch_detail_table.setRowCount(0)
        if not batch_id:
            self.batch_detail_help_label.setText(
                "Select a batch row to review its source clips."
            )
            self.batch_detail_table_updating = False
            return
        group = next((g for g in self.batch_groups if g.batch_id == batch_id), None)
        if group is None:
            self.batch_detail_table_updating = False
            return
        self.batch_detail_help_label.setText(
            f"Reel {group.reel_number} / Clip {group.clip_number} — Subgroup drives section organization on the final sheet."
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

    # ── Batch table selection / actions ───────────────────────────────────────

    def _on_batch_table_item_changed(self, _item: QTableWidgetItem) -> None:
        if self.batch_table_updating:
            return
        self._refresh_batch_render_button_state()

    def _on_batch_selection_changed(self) -> None:
        self._render_selected_batch_detail()
        self._update_assignment_help()
        self._update_batch_secondary_actions()

    def _on_batch_detail_item_changed(self, item: QTableWidgetItem) -> None:
        if self.batch_detail_table_updating:
            return
        path_text = item.data(Qt.UserRole)
        if not path_text:
            return
        assignment = self._assignment_for_path(Path(path_text))
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
        group = next((g for g in self.batch_groups if g.batch_id == batch_id), None)
        if group is None:
            return
        rows = self.unassigned_table.selectionModel().selectedRows() if self.unassigned_table.selectionModel() else []
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
        for a in self.batch_assignments:
            if a.clip.source_path.resolve() == resolved:
                return a
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
        assignment.reel_number = assignment.reel_number or None
        assignment.clip_number = assignment.clip_number or None

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

    def _needs_assignment_count(self) -> int:
        return sum(1 for a in self.batch_assignments if a.assignment_state == "needs_assignment")

    def _update_assignment_help(self) -> None:
        selected_batch_id = self._selected_batch_id()
        selected_batch = next((g for g in self.batch_groups if g.batch_id == selected_batch_id), None)
        selected_unassigned = self._selected_unassigned_assignments()
        if selected_batch and selected_unassigned:
            self.unassigned_help_label.setText(
                f"Assigning {len(selected_unassigned)} clip(s) → Reel {selected_batch.reel_number} / Clip {selected_batch.clip_number}."
            )
        elif self._needs_assignment_count():
            self.unassigned_help_label.setText(
                f"{self._needs_assignment_count()} clip(s) need manual batch assignment."
            )
        else:
            self.unassigned_help_label.setText("All clips assigned. Select a row to exclude or reassign.")

    def _set_batch_detail_visible(self, visible: bool, tab_index: int | None = None) -> None:
        # Flat tab model: just switch to the requested tab (tab_index offset by 1 for groups tab)
        if tab_index is not None:
            self.batch_detail_tabs.setCurrentIndex(tab_index + 1)
        self._update_batch_secondary_actions()

    def _show_selected_batch_inspector(self) -> None:
        if not self._selected_batch_id():
            return
        self.batch_detail_tabs.setCurrentIndex(1)  # Sources

    def _show_needs_assignment_inspector(self) -> None:
        self.batch_detail_tabs.setCurrentIndex(2)  # Needs Assignment

    def _show_batch_log_inspector(self) -> None:
        self.batch_detail_tabs.setCurrentIndex(3)  # Log

    def _update_batch_secondary_actions(self) -> None:
        selected_batch = self._selected_batch_id()
        needs_count = self._needs_assignment_count()
        self.batch_details_button.setEnabled(bool(selected_batch))
        self.batch_inspect_sync_button.setEnabled(bool(selected_batch))
        self.batch_needs_assignment_button.setEnabled(bool(self.batch_assignments))
        self.batch_needs_assignment_button.setText(f"Needs Assignment ({needs_count})")
        self.batch_log_toggle_button.setEnabled(True)
        if selected_batch:
            group = next((g for g in self.batch_groups if g.batch_id == selected_batch), None)
            if group:
                sync_tag = " [sync applied]" if selected_batch in self.sync_decisions else ""
                self.batch_selected_summary_label.setText(
                    f"Reel {group.reel_number} / Clip {group.clip_number} · "
                    f"{group.source_clip_count} clip(s) · {group.output_pdf_name}{sync_tag}"
                )
            else:
                self.batch_selected_summary_label.setText("Select a row to inspect.")
        else:
            self.batch_selected_summary_label.setText("Select a row to inspect source clips.")

    def _selected_batch_groups(self) -> list[BatchGroup]:
        selected: list[BatchGroup] = []
        by_id = {g.batch_id: g for g in self.batch_groups}
        for row in range(self.batch_table.rowCount()):
            item = self.batch_table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            group = by_id.get(item.data(Qt.UserRole))
            if group is not None:
                selected.append(group)
        return selected

    def _selected_batch_id(self) -> str | None:
        rows = self.batch_table.selectionModel().selectedRows() if self.batch_table.selectionModel() else []
        if not rows:
            item = self.batch_table.item(0, 0) if self.batch_table.rowCount() else None
            return item.data(Qt.UserRole) if item is not None else None
        item = self.batch_table.item(rows[0].row(), 0)
        return item.data(Qt.UserRole) if item is not None else None

    def _restore_batch_selection(self, batch_id: str) -> None:
        for row in range(self.batch_table.rowCount()):
            item = self.batch_table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == batch_id:
                self.batch_table.selectRow(row)
                return

    def _refresh_batch_render_button_state(self) -> None:
        enabled = bool(
            self.batch_groups
            and self._selected_batch_groups()
            and self.batch_output_edit.text().strip()
        )
        if self.batch_render_worker and self.batch_render_worker.is_alive():
            enabled = False
        self.batch_render_button.setEnabled(enabled)

    def _selected_unassigned_assignments(self) -> list[BatchAssignment]:
        rows = self.unassigned_table.selectionModel().selectedRows() if self.unassigned_table.selectionModel() else []
        assignments: list[BatchAssignment] = []
        for model_index in rows:
            item = self.unassigned_table.item(model_index.row(), 1)
            if item is None:
                continue
            a = self._assignment_for_path(Path(item.data(Qt.UserRole)))
            if a is not None:
                assignments.append(a)
        return assignments

    # ── Preview table item edits ──────────────────────────────────────────────

    def _on_preview_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self.preview_table_updating or not self.preview_context:
            return
        path_text = item.data(Qt.UserRole)
        if not path_text:
            return
        fields = self.preview_context.clip_fields.get(Path(path_text))
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

    # ── Build helpers ─────────────────────────────────────────────────────────

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
            raise ValueError(f"Render settings must be numeric: {exc}") from exc

    def _batch_selection_for_context(self, context: PreviewContext) -> MatchSelectionState:
        selection = context.selection
        subset = next((s for s in context.overlap_subsets if s.subset_id == selection.active_subset_id), None)
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

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_input_path(self) -> Path:
        value = self.source_edit.text().strip()
        if not value:
            raise ValueError("Choose a source folder, RDC package, or single clip.")
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Source does not exist: {path}")
        return path

    def _validate_batch_output_path_for_preview(self) -> Path:
        """Return the batch output path without creating directories (preview-safe)."""
        value = self.batch_output_edit.text().strip()
        if not value:
            raise ValueError("Choose an output folder on the Create Contact Sheet tab.")
        return Path(value).expanduser().resolve()

    def _validate_batch_source_path(self) -> Path:
        value = self.batch_source_edit.text().strip()
        if not value:
            raise ValueError("Choose a source folder.")
        path = Path(value).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise ValueError(f"Source folder does not exist: {path}")
        return path

    def _validate_batch_output_path(self) -> Path:
        value = self.batch_output_edit.text().strip()
        if not value:
            raise ValueError("Choose an output folder.")
        path = Path(value).expanduser().resolve()
        source_path = self._validate_batch_source_path()
        if path == source_path or source_path in path.parents:
            raise ValueError("Output folder must be outside the source tree.")
        return path

    def _validate_redline_path(self) -> Path:
        value = self.redline_edit.text().strip()
        if not value:
            raise ValueError("Configure a REDline executable in Settings.")
        path = Path(value).expanduser().resolve()
        probe = probe_redline(RedlinePaths(explicit_path=path))
        if not probe.available:
            raise ValueError(probe.message)
        if not probe.compatible:
            raise ValueError(probe.message)
        if not probe.executable:
            raise ValueError("REDline validated without a resolved executable path.")
        return probe.executable

    # ── PDF assembly (called from worker thread) ──────────────────────────────

    def _build_contact_sheet_pdf_for_plan(
        self,
        plan,
        *,
        output_dir: Path,
        destination_name: str,
        sync_mode_override: str | None = None,
    ) -> Path:
        sync_mode = self._plan_sync_mode(plan)
        presentation_mode = (
            sync_mode_override == "sync_off"
            if sync_mode_override is not None
            else self.sync_mode_combo.currentText() == "Sync Off"
        )
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
            items.append(ContactSheetItem(
                image_path=image_path,
                clip_label=item.clip_fields.camera_label.strip() or self._display_clip_label(item.clip.clip_name),
                group_label=display_group,
                subgroup_label=display_subgroup,
                frame_label=(
                    f"Frame {item.frame_resolution.frame_index}"
                    if item.frame_resolution.frame_index is not None else "Frame unavailable"
                ),
                timecode_label=item.frame_resolution.match_timecode or "Unavailable",
                fps_label=f"{item.clip_metadata.clip_fps:g} fps" if item.clip_metadata.clip_fps is not None else "FPS unknown",
                resolution_label=(item.clip_metadata.resolution or "Resolution unknown").replace("x", " × "),
                sync_label="" if presentation_mode else self._display_sync_caption(self._sync_status_text(item, sync_mode)),
            ))
        if not items:
            raise ValueError("No valid rendered stills available for PDF generation.")
        subset = self._current_active_subset() if plan is self.plan else None
        common_fps = self._common_value([f"{i.clip_metadata.clip_fps:g}" for i in plan if i.clip_metadata.clip_fps is not None])
        common_resolution = self._common_value([i.clip_metadata.resolution for i in plan if i.clip_metadata.resolution])
        grouping_summary = self._sheet_grouping_summary(plan)
        header_lines = [
            grouping_summary,
            f"Mode: {'Presentation' if presentation_mode else 'Technical Sync'}",
            (
                "Frames selected from strongest overlap or each clip's middle region."
                if presentation_mode
                else (
                    f"Shared match: {self._plan_matching_label(plan)} · {subset.start_timecode} → {subset.end_timecode}"
                    if subset is not None and sync_mode != "none"
                    else (
                        f"Shared match: {self._plan_matching_label(plan)}"
                        if sync_mode != "none"
                        else "No universal common frame — per-clip metadata used."
                    )
                )
            ),
            f"{common_fps or 'mixed'} fps · {(common_resolution or 'mixed').replace('x', '×')} · {len(plan)} cameras",
        ]
        destination = output_dir / destination_name
        return build_contact_sheet_pdf(
            items,
            destination,
            "R3DContactSheet",
            header_lines=header_lines,
            theme_name=self.theme_combo.currentText().strip().lower(),
        )

    # ── Summary / display helpers ─────────────────────────────────────────────

    def _batch_sync_summary(self, group: BatchGroup) -> str:
        providers = sorted({clip.provider_kind for clip in group.clips})
        if providers == ["red"]:
            return "RED — resolves on render"
        if set(providers).issubset({"red", "video", "braw"}):
            return "Mixed — resolves on render"
        return "Resolves on render"

    def _batch_camera_summary(self, group: BatchGroup) -> str:
        labels = [
            (self._assignment_for_path(clip.source_path).camera_label
             if self._assignment_for_path(clip.source_path) else "")
            or self._display_clip_label(clip.clip_name)
            for clip in group.clips
        ]
        preview = ", ".join(labels[:4])
        return f"{group.camera_count} · {preview}" + ("…" if len(labels) > 4 else "")

    def _batch_family_summary(self, group: BatchGroup) -> str:
        families = sorted({
            (self._assignment_for_path(clip.source_path).clip_family
             if self._assignment_for_path(clip.source_path) else suggest_clip_family(clip))
            for clip in group.clips
        })
        return ", ".join(families[:3]) + ("…" if len(families) > 3 else "")

    def _batch_subgroup_summary(self, group: BatchGroup) -> str:
        subgroups = sorted({
            (self._assignment_for_path(clip.source_path).subgroup_name
             if self._assignment_for_path(clip.source_path) else suggest_subgroup(clip))
            for clip in group.clips
        })
        return ", ".join(subgroups[:3]) + ("…" if len(subgroups) > 3 else "")

    def _preview_fps_text(self, item) -> str:
        fps = item.clip_metadata.clip_fps or item.frame_resolution.clip_fps
        return f"{fps:g}" if fps is not None else "Unknown"

    def _sync_basis_text(self, item) -> str:
        tc_source = item.clip_metadata.timecode_source or "unknown"
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
        return " · ".join(bits)

    def _sync_status_text(self, item, sync_mode: str) -> str:
        if not item.clip_metadata.sync_eligible:
            return item.clip_metadata.metadata_error or "Metadata incomplete"
        if item.frame_resolution.match_frame is None or not item.frame_resolution.match_timecode:
            return "Metadata incomplete"
        if item.frame_resolution.sync_status == "exact_match":
            return "Exact match"
        if item.frame_resolution.sync_status == "nearest_available":
            return "Nearest available"
        if item.frame_resolution.sync_status == "outside_overlap":
            return "Out Of Frame Sync"
        return "Metadata incomplete"

    def _plan_sync_mode(self, plan) -> str:
        if not plan:
            return "none"
        statuses = {item.frame_resolution.sync_status for item in plan}
        if statuses == {"exact_match"}:
            return "full"
        if "exact_match" in statuses or "nearest_available" in statuses:
            return "partial"
        return "none"

    def _plan_matching_label(self, plan) -> str:
        for item in plan:
            if item.frame_resolution.match_timecode:
                return item.frame_resolution.match_timecode
        return "Unavailable"

    def _frame_mode_summary_from_plan(self, plan) -> str:
        if not plan:
            return ""
        subset = self._current_active_subset()
        current_tc = self._current_match_timecode_label()
        sync_mode = self._plan_sync_mode(plan)
        if sync_mode == "full":
            return f"Shared match: {current_tc} — full sync verified across all clips."
        if sync_mode == "partial":
            if subset:
                return f"Partial sync. Subset {subset.start_timecode} → {subset.end_timecode}, match at {current_tc}."
            return f"Partial sync. Match at {current_tc}."
        return "No common moment — each clip uses its own metadata timecode."

    def _update_health_from_plan(self, plan) -> None:
        if not plan:
            return
        sync_mode = self._plan_sync_mode(plan)
        statuses = [self._sync_status_text(item, sync_mode) for item in plan]
        if sync_mode == "full" and all(s == "Exact match" for s in statuses):
            self._set_health_state("green", f"Full sync verified — {len(plan)} clip(s), all exact matches.")
        elif any(s.startswith("Metadata incomplete") for s in statuses):
            self._set_health_state("yellow", "Metadata incomplete for one or more clips. Review before rendering.")
        elif sync_mode == "partial":
            self._set_health_state("yellow", "Partial sync — review exact matches and out-of-range clips.")
        else:
            self._set_health_state("yellow", "No common moment — per-clip metadata timecodes in use.")

    def _display_sync_caption(self, status: str) -> str:
        if status == "Exact match":
            return "Exact match"
        if status == "Out Of Frame Sync":
            return "Out Of Frame Sync"
        return status

    def _sheet_grouping_summary(self, plan) -> str:
        groups = sorted({(item.clip_fields.group_name or item.output_group or "Uncategorized") for item in plan})
        group_text = groups[0] if len(groups) == 1 else ", ".join(groups[:3]) + ("…" if len(groups) > 3 else "")
        subgroups = sorted({item.clip_fields.subgroup_name for item in plan if item.clip_fields.subgroup_name})
        if len(subgroups) == 1:
            return f"Group: {group_text} · Subgroup: {subgroups[0]}"
        if len(subgroups) > 1:
            sg_text = ", ".join(subgroups[:3]) + ("…" if len(subgroups) > 3 else "")
            return f"Group: {group_text} · Subgroups: {sg_text}"
        return f"Group: {group_text}"

    def _current_match_timecode_label(self) -> str:
        subset = self._current_active_subset()
        selection = self._selection_state_from_widgets()
        if subset is None or selection.selected_abs_frame is None:
            return "Unavailable"
        return self._timecode_for_subset_frame(subset, selection.selected_abs_frame) or "Unavailable"

    def _common_value(self, values):
        unique = [v for v in values if v]
        if not unique:
            return None
        return unique[0] if len(set(unique)) == 1 else None

    def _display_clip_label(self, clip_name: str) -> str:
        parts = clip_name.split("_")
        return f"{parts[0]} {parts[1]}" if len(parts) >= 2 else clip_name.replace("_", " ")

    def _logical_common_value(self, values):
        unique = {self._logical_token(v) for v in values if v}
        return next(iter(unique)) if len(unique) == 1 else None

    def _logical_token(self, value: str) -> str:
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits or value

    # ── UI state helpers ──────────────────────────────────────────────────────

    def _set_health_state(self, level: str, text: str) -> None:
        colors = {"green": _C["dot_green"], "yellow": _C["dot_yellow"], "red": _C["dot_red"]}
        self.status_dot.setStyleSheet(f"color: {colors.get(level, _C['dot_yellow'])}; font-size: 16px;")
        self.status_label.setText(text)

    def _set_preview_busy(self, busy: bool, text: str | None = None) -> None:
        self.preview_button.setDisabled(busy)
        self.preview_button.setText((text or self._preview_idle_text) if busy else self._preview_idle_text)

    def _set_batch_scan_busy(self, busy: bool) -> None:
        self.batch_scan_button.setDisabled(busy)
        self.batch_scan_button.setText("Scanning…" if busy else self._batch_scan_idle_text)
        self._refresh_batch_render_button_state()

    def _set_path_field(self, widget: QLineEdit, value: str) -> None:
        widget.setText(value)
        widget.setCursorPosition(0)
        widget.setToolTip(value)

    def _style_status_item(self, item: QTableWidgetItem, status: str) -> None:
        font = item.font()
        font.setBold(True)
        if status == "Exact match":
            item.setForeground(QColor(_C["teal"]))
        elif status == "Out Of Frame Sync":
            item.setForeground(QColor(_C["red"]))
        elif status == "Nearest available":
            item.setForeground(QColor(_C["gold"]))
        item.setFont(font)

    def _append_batch_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        self.batch_log_text.appendPlainText(line)
        print(line, flush=True)

    def _append_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        self.log_text.appendPlainText(line)
        print(line, flush=True)

    def _emit_log(self, text: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self._append_log(text)
        else:
            self.event_queue.put(("log", text))

    def _log_probe_message_once(self, message: str) -> None:
        if not message or message == self._last_probe_log_message:
            return
        self._last_probe_log_message = message
        self._append_log(message)

    def _log_startup_banner(self) -> None:
        for text in (f"STARTUP: {BUILD_MARKER}", self.store.last_status):
            print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def _load_header_logo(self, width: int, height: int) -> QPixmap:
        logo_path = _resource_path(LOGO_NAME)
        pixmap = QPixmap(str(logo_path))
        if pixmap.isNull():
            return QPixmap()
        return pixmap.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)


# ── Standalone helpers ────────────────────────────────────────────────────────

def sys_platform_is_macos() -> bool:
    return sys.platform == "darwin"


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
    palette.setColor(QPalette.Window,          QColor(_C["bg1"]))
    palette.setColor(QPalette.WindowText,      QColor(_C["text0"]))
    palette.setColor(QPalette.Base,            QColor(_C["bg0"]))
    palette.setColor(QPalette.AlternateBase,   QColor(_C["bg2"]))
    palette.setColor(QPalette.Text,            QColor(_C["text0"]))
    palette.setColor(QPalette.Button,          QColor(_C["bg2"]))
    palette.setColor(QPalette.ButtonText,      QColor(_C["text0"]))
    palette.setColor(QPalette.Highlight,       QColor(_C["amber"]))
    palette.setColor(QPalette.HighlightedText, QColor(_C["bg0"]))
    palette.setColor(QPalette.ToolTipBase,     QColor(_C["bg2"]))
    palette.setColor(QPalette.ToolTipText,     QColor(_C["text0"]))
    app.setPalette(palette)

    app.setStyleSheet(f"""
        QMainWindow, QWidget {{
            background: {_C["bg1"]};
            color: {_C["text0"]};
            font-size: 13px;
        }}

        /* ── Header ── */
        QFrame#appHeader {{
            background: {_C["header_bg"]};
            border-bottom: 1px solid {_C["border"]};
            border-radius: 0px;
        }}
        QLabel#headerTitle {{
            font-size: 20px;
            font-weight: 700;
            color: {_C["amber"]};
            letter-spacing: 0.5px;
        }}
        QFrame#headerDivider {{
            background: {_C["border"]};
            min-width: 1px; max-width: 1px;
            min-height: 24px; max-height: 24px;
        }}
        QLabel#statusDot {{
            font-size: 16px;
        }}
        QLabel#headerStatus {{
            color: {_C["text1"]};
            font-size: 13px;
        }}

        /* ── Tabs ── */
        QTabWidget::pane {{
            border: 1px solid {_C["border"]};
            border-radius: 8px;
            background: {_C["bg1"]};
            top: -1px;
        }}
        QTabBar::tab {{
            background: {_C["bg2"]};
            color: {_C["text2"]};
            border: 1px solid {_C["border"]};
            border-bottom: none;
            border-radius: 6px 6px 0 0;
            padding: 8px 16px;
            margin-right: 3px;
            font-weight: 600;
            font-size: 13px;
        }}
        QTabBar::tab:selected {{
            background: {_C["bg1"]};
            color: {_C["amber"]};
            border-color: {_C["border_hi"]};
        }}
        QTabBar::tab:hover:!selected {{
            background: {_C["bg3"]};
            color: {_C["text1"]};
        }}

        /* ── Section boxes ── */
        QGroupBox#sectionBox {{
            border: 1px solid {_C["border"]};
            border-radius: 8px;
            margin-top: 10px;
            padding-top: 6px;
            background: {_C["bg2"]};
            font-weight: 700;
            font-size: 12px;
            color: {_C["text2"]};
        }}
        QGroupBox#sectionBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 6px;
            text-transform: uppercase;
            letter-spacing: 0.8px;
        }}

        /* ── Labels ── */
        QLabel#labelBright {{
            color: {_C["text0"]};
            font-weight: 600;
        }}
        QLabel#labelMuted {{
            color: {_C["text1"]};
        }}
        QLabel#labelDim {{
            color: {_C["text2"]};
            font-size: 12px;
        }}

        /* ── Inputs ── */
        QLineEdit, QComboBox, QPlainTextEdit {{
            background: {_C["bg3"]};
            color: {_C["text0"]};
            border: 1px solid {_C["border"]};
            border-radius: 5px;
            padding: 5px 8px;
            selection-background-color: {_C["amber_dim"]};
        }}
        QLineEdit:focus, QComboBox:focus {{
            border-color: {_C["amber_dim"]};
        }}
        QComboBox::drop-down {{
            border: none;
            width: 22px;
        }}
        QComboBox QAbstractItemView {{
            background: {_C["bg3"]};
            color: {_C["text0"]};
            border: 1px solid {_C["border_hi"]};
            selection-background-color: {_C["amber_dim"]};
        }}

        /* ── Buttons ── */
        QPushButton {{
            background: {_C["bg3"]};
            color: {_C["text1"]};
            border: 1px solid {_C["border"]};
            border-radius: 5px;
            padding: 6px 14px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background: {_C["border"]};
            color: {_C["text0"]};
            border-color: {_C["border_hi"]};
        }}
        QPushButton:disabled {{
            color: {_C["text2"]};
            border-color: {_C["border"]};
            background: {_C["bg2"]};
        }}
        QPushButton#primaryButton {{
            background: {_C["amber"]};
            color: {_C["bg0"]};
            border: none;
            font-weight: 700;
        }}
        QPushButton#primaryButton:hover {{
            background: {_C["amber_hi"]};
        }}
        QPushButton#primaryButton:disabled {{
            background: {_C["amber_dim"]};
            color: {_C["bg2"]};
        }}

        /* ── Tables ── */
        QTableWidget {{
            background: {_C["bg0"]};
            color: {_C["text0"]};
            border: 1px solid {_C["border"]};
            border-radius: 6px;
            gridline-color: {_C["bg2"]};
        }}
        QTableWidget::item:selected {{
            background: {_C["amber_dim"]};
            color: {_C["text0"]};
        }}
        QTableWidget::item:alternate {{
            background: {_C["bg1"]};
        }}
        QHeaderView::section {{
            background: {_C["bg2"]};
            color: {_C["text2"]};
            border: none;
            border-right: 1px solid {_C["border"]};
            border-bottom: 1px solid {_C["border"]};
            padding: 5px 8px;
            font-weight: 700;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        /* ── Progress bars ── */
        QProgressBar {{
            background: {_C["bg0"]};
            border: 1px solid {_C["border"]};
            border-radius: 4px;
            text-align: center;
            color: {_C["text0"]};
            font-size: 11px;
        }}
        QProgressBar::chunk {{
            background: {_C["amber"]};
            border-radius: 3px;
        }}

        /* ── Slider ── */
        QSlider::groove:horizontal {{
            background: {_C["bg0"]};
            border: 1px solid {_C["border"]};
            height: 4px;
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {_C["amber"]};
            border: none;
            width: 14px;
            height: 14px;
            margin: -5px 0;
            border-radius: 7px;
        }}
        QSlider::sub-page:horizontal {{
            background: {_C["amber_dim"]};
            border-radius: 2px;
        }}

        /* ── Check box ── */
        QCheckBox {{
            color: {_C["text1"]};
            spacing: 6px;
        }}
        QCheckBox::indicator {{
            width: 14px;
            height: 14px;
            border: 1px solid {_C["border_hi"]};
            border-radius: 3px;
            background: {_C["bg3"]};
        }}
        QCheckBox::indicator:checked {{
            background: {_C["amber"]};
            border-color: {_C["amber"]};
        }}

        /* ── Separators ── */
        QFrame#hSep {{
            background: {_C["border"]};
            min-height: 1px;
            max-height: 1px;
        }}

        /* ── Inline panel ── */
        QFrame#inlinePanel {{
            border-top: 1px solid {_C["border"]};
            background: transparent;
        }}

        /* ── Scrollbars ── */
        QScrollBar:vertical {{
            background: {_C["bg1"]};
            width: 8px;
            border-radius: 4px;
        }}
        QScrollBar::handle:vertical {{
            background: {_C["border_hi"]};
            border-radius: 4px;
            min-height: 24px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
        QScrollBar:horizontal {{
            background: {_C["bg1"]};
            height: 8px;
            border-radius: 4px;
        }}
        QScrollBar::handle:horizontal {{
            background: {_C["border_hi"]};
            border-radius: 4px;
            min-width: 24px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
    """)


def main() -> None:
    app = QApplication(sys.argv)
    _apply_app_palette(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
