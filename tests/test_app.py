import os
import subprocess
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from r3dcontactsheet.app import (
    BUILD_MARKER,
    MainWindow,
    WINDOW_TITLE,
    _choose_directory_macos,
    _format_batch_progress,
    _format_preview_progress,
)


class MacChooserTests(unittest.TestCase):
    @patch("r3dcontactsheet.app.subprocess.run")
    def test_choose_directory_macos_returns_selected_path(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["/usr/bin/osascript"],
            returncode=0,
            stdout="/tmp/Test.RDC\n",
            stderr="",
        )

        status, selection = _choose_directory_macos("Choose RDC Package")

        self.assertEqual(status, "selected")
        self.assertEqual(selection, "/tmp/Test.RDC")
        command = mock_run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertIn("choose folder", " ".join(command))

    @patch("r3dcontactsheet.app.subprocess.run")
    def test_choose_directory_macos_treats_cancel_as_cancelled(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["/usr/bin/osascript"],
            returncode=1,
            stdout="",
            stderr="User canceled.",
        )

        status, selection = _choose_directory_macos("Choose RDC Package")

        self.assertEqual(status, "cancelled")
        self.assertEqual(selection, "")

    def test_build_marker_constants_are_visible(self):
        self.assertIn("VERIFIED", BUILD_MARKER)
        self.assertIn(BUILD_MARKER, WINDOW_TITLE)

    def test_preview_progress_format(self):
        self.assertEqual(_format_preview_progress(12, 37), "Analyzing clips: 12 / 37 (32%)")
        self.assertEqual(_format_preview_progress(0, 0), "Analyzing clips...")
        self.assertEqual(_format_batch_progress("Scanning clips", 58, 412), "Scanning clips: 58 / 412 (14%)")


class BatchingLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_batching_layout_prioritizes_main_table_height(self):
        window = MainWindow()
        try:
            self.assertGreaterEqual(window.batch_table.minimumHeight(), 400)
            self.assertLessEqual(window.batch_log_text.minimumHeight(), 90)
            self.assertLess(window.batch_detail_table.minimumHeight(), window.batch_table.minimumHeight())
            self.assertIsNotNone(window.batch_main_splitter)
            self.assertFalse(window.batch_inline_panel.isVisible())
            self.assertEqual(window.batch_detail_tabs.count(), 3)
            self.assertEqual(window.batch_needs_assignment_button.text(), "Needs Assignment (0)")
            self.assertFalse(window.batch_hide_details_button.isVisible())
            self.assertEqual(window.tabs.tabText(0), "Settings")
            self.assertEqual(window.tabs.tabText(1), "Preview")
            self.assertEqual(window.tabs.tabText(2), "Batching")
            self.assertEqual(window.tabs.tabText(3), "Render")
            self.assertEqual(window.batch_source_button.text(), "Choose Batch Source")
            self.assertEqual(window.choose_source_button.text(), "Choose Preview Source...")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
