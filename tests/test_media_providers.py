import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from r3dcontactsheet.media_providers import load_provider_metadata, provider_kind_for_path


class MediaProviderTests(unittest.TestCase):
    def test_provider_kind_detects_red_and_generic_extensions(self):
        self.assertEqual(provider_kind_for_path(Path("/tmp/clip.R3D")), "red")
        self.assertEqual(provider_kind_for_path(Path("/tmp/package.RDC")), "red")
        self.assertEqual(provider_kind_for_path(Path("/tmp/clip.braw")), "braw")
        self.assertEqual(provider_kind_for_path(Path("/tmp/clip.mov")), "video")

    @patch("r3dcontactsheet.media_providers.resolve_ffprobe")
    @patch("r3dcontactsheet.media_providers.subprocess.run")
    def test_generic_video_metadata_uses_ffprobe_when_available(self, mock_run, mock_resolve_ffprobe):
        mock_resolve_ffprobe.return_value = "/usr/local/bin/ffprobe"
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "avg_frame_rate": "24/1",
                    "nb_frames": "21",
                    "width": 1920,
                    "height": 1080,
                    "tags": {"timecode": "15:41:34:01"},
                }
            ],
            "format": {},
        }
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

        metadata = load_provider_metadata(Path("/tmp/clip.mov"), provider_kind="video")

        self.assertEqual(metadata.start_timecode, "15:41:34:01")
        self.assertEqual(metadata.total_frames, 21)
        self.assertEqual(metadata.end_timecode, "15:41:34:21")
        self.assertTrue(metadata.metadata_ok)
        self.assertEqual(metadata.provider_name, "video")

    @patch("r3dcontactsheet.media_providers.resolve_ffprobe")
    def test_generic_video_metadata_is_incomplete_when_ffprobe_missing(self, mock_resolve_ffprobe):
        mock_resolve_ffprobe.return_value = None

        metadata = load_provider_metadata(Path("/tmp/clip.mov"), provider_kind="video")

        self.assertFalse(metadata.metadata_ok)
        self.assertFalse(metadata.sync_eligible)
        self.assertIsNone(metadata.start_timecode)


if __name__ == "__main__":
    unittest.main()
