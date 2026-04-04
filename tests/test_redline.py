import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r3dcontactsheet.redline import (
    RedlinePaths,
    RenderJob,
    RenderSettings,
    _default_macos_redline_candidates,
    _resolve_redline_candidate,
    probe_redline,
    render_frame,
)


class RenderOutputDetectionTests(unittest.TestCase):
    @patch("r3dcontactsheet.redline.subprocess.run")
    def test_render_frame_accepts_numbered_redline_jpeg_output(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            expected_output = root / "clip.jpg"
            emitted_output = root / "clip.jpg.000000.jpg"

            def fake_run(*args, **kwargs):
                emitted_output.write_bytes(b"x" * 4096)
                return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="warning")

            mock_run.side_effect = fake_run
            job = RenderJob(
                input_file=root / "clip.R3D",
                frame_index=6,
                output_file=expected_output,
                settings=RenderSettings(),
            )

            result = render_frame(job, redline_exe="/Applications/REDline", min_output_bytes=1024)

            self.assertTrue(expected_output.exists())
            self.assertFalse(emitted_output.exists())
            self.assertEqual(result.output_exists, True)
            self.assertGreaterEqual(result.output_size, 4096)

    def test_default_macos_candidates_include_common_redline_names(self):
        candidates = _default_macos_redline_candidates()
        candidate_strings = {str(path) for path in candidates}

        self.assertIn("/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDline", candidate_strings)
        self.assertIn("/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDLine", candidate_strings)

    def test_resolve_redline_candidate_rejects_app_bundle_without_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "REDCINE-X PRO.app"
            (bundle / "Contents" / "MacOS").mkdir(parents=True)

            resolved, bundle_path, message = _resolve_redline_candidate(bundle)

            self.assertIsNone(resolved)
            self.assertEqual(bundle_path, bundle.resolve())
            self.assertIn("application bundle", message)

    @patch("r3dcontactsheet.redline.subprocess.run")
    def test_probe_redline_reports_bundle_selected_when_cli_missing(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "REDCINE-X PRO.app"
            (bundle / "Contents" / "MacOS").mkdir(parents=True)

            probe = probe_redline(paths=RedlinePaths(explicit_path=bundle))

            self.assertFalse(probe.available)
            self.assertTrue(probe.bundle_selected)
            self.assertIn("not the REDline CLI binary", probe.message)
            mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
