import subprocess
import unittest
from unittest.mock import patch

from r3dcontactsheet.app import BUILD_MARKER, WINDOW_TITLE, _choose_directory_macos


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


if __name__ == "__main__":
    unittest.main()
