import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r3dcontactsheet.redline import RenderJob, RenderSettings, render_frame


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


if __name__ == "__main__":
    unittest.main()
