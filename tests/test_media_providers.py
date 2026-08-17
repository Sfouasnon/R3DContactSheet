import json
import os
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

    @patch("r3dcontactsheet.media_providers.resolve_ltcdump")
    @patch("r3dcontactsheet.media_providers.resolve_ffmpeg")
    @patch("r3dcontactsheet.media_providers.resolve_mediainfo")
    @patch("r3dcontactsheet.media_providers.resolve_ffprobe")
    @patch("r3dcontactsheet.media_providers.subprocess.run")
    def test_generic_video_metadata_uses_ffprobe_when_available(
        self,
        mock_run,
        mock_resolve_ffprobe,
        mock_resolve_mediainfo,
        mock_resolve_ffmpeg,
        mock_resolve_ltcdump,
    ):
        mock_resolve_ffprobe.return_value = "/usr/local/bin/ffprobe"
        mock_resolve_mediainfo.return_value = None
        mock_resolve_ffmpeg.return_value = None
        mock_resolve_ltcdump.return_value = None
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
        self.assertEqual(metadata.metadata_provenance, "ffprobe")
        self.assertEqual(metadata.metadata_confidence, "high")

    @patch("r3dcontactsheet.media_providers.resolve_ltcdump")
    @patch("r3dcontactsheet.media_providers.resolve_ffmpeg")
    @patch("r3dcontactsheet.media_providers.resolve_mediainfo")
    @patch("r3dcontactsheet.media_providers.resolve_ffprobe")
    def test_generic_video_metadata_is_incomplete_when_all_fallbacks_missing(
        self,
        mock_resolve_ffprobe,
        mock_resolve_mediainfo,
        mock_resolve_ffmpeg,
        mock_resolve_ltcdump,
    ):
        mock_resolve_ffprobe.return_value = None
        mock_resolve_mediainfo.return_value = None
        mock_resolve_ffmpeg.return_value = None
        mock_resolve_ltcdump.return_value = None

        metadata = load_provider_metadata(Path("/tmp/clip.mov"), provider_kind="video")

        self.assertFalse(metadata.metadata_ok)
        self.assertFalse(metadata.sync_eligible)
        self.assertIsNone(metadata.start_timecode)
        self.assertEqual(metadata.metadata_provenance, "unresolved")
        self.assertEqual(metadata.metadata_confidence, "unresolved")

    @patch("r3dcontactsheet.media_providers.resolve_ltcdump")
    @patch("r3dcontactsheet.media_providers.resolve_ffmpeg")
    @patch("r3dcontactsheet.media_providers.resolve_mediainfo")
    @patch("r3dcontactsheet.media_providers.resolve_ffprobe")
    @patch("r3dcontactsheet.media_providers.subprocess.run")
    def test_generic_video_metadata_falls_back_to_mediainfo(
        self,
        mock_run,
        mock_resolve_ffprobe,
        mock_resolve_mediainfo,
        mock_resolve_ffmpeg,
        mock_resolve_ltcdump,
    ):
        mock_resolve_ffprobe.return_value = "/usr/local/bin/ffprobe"
        mock_resolve_mediainfo.return_value = "/usr/local/bin/mediainfo"
        mock_resolve_ffmpeg.return_value = None
        mock_resolve_ltcdump.return_value = None
        ffprobe_payload = {"streams": [{"codec_type": "video", "avg_frame_rate": "24/1", "nb_frames": "21"}], "format": {}}
        mediainfo_payload = {
            "media": {
                "track": [
                    {"@type": "General", "TimeCode_FirstFrame": "15:41:34:01", "TimeCode_LastFrame": "15:41:34:21"},
                    {"@type": "Video", "FrameRate": "24", "FrameCount": "21", "Width": "1920", "Height": "1080"},
                ]
            }
        }
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout=json.dumps(ffprobe_payload), stderr=""),
            subprocess.CompletedProcess(args=["mediainfo"], returncode=0, stdout=json.dumps(mediainfo_payload), stderr=""),
        ]

        metadata = load_provider_metadata(Path("/tmp/clip.mov"), provider_kind="video")

        self.assertTrue(metadata.metadata_ok)
        self.assertEqual(metadata.start_timecode, "15:41:34:01")
        self.assertEqual(metadata.metadata_provenance, "mediainfo")
        self.assertEqual(metadata.metadata_confidence, "medium")

    @patch.dict(os.environ, {"R3DCONTACTSHEET_ENABLE_LTC_AUDIO": "1"}, clear=False)
    @patch("r3dcontactsheet.media_providers.resolve_ltcdump")
    @patch("r3dcontactsheet.media_providers.resolve_ffmpeg")
    @patch("r3dcontactsheet.media_providers.resolve_mediainfo")
    @patch("r3dcontactsheet.media_providers.resolve_ffprobe")
    @patch("r3dcontactsheet.media_providers.subprocess.run")
    def test_generic_video_metadata_falls_back_to_ltc_audio(
        self,
        mock_run,
        mock_resolve_ffprobe,
        mock_resolve_mediainfo,
        mock_resolve_ffmpeg,
        mock_resolve_ltcdump,
    ):
        mock_resolve_ffprobe.return_value = "/usr/local/bin/ffprobe"
        mock_resolve_mediainfo.return_value = None
        mock_resolve_ffmpeg.return_value = "/usr/local/bin/ffmpeg"
        mock_resolve_ltcdump.return_value = "/usr/local/bin/ltcdump"
        ffprobe_payload = {
            "streams": [{"codec_type": "video", "avg_frame_rate": "24/1", "nb_frames": "21", "width": 1920, "height": 1080}],
            "format": {},
        }
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout=json.dumps(ffprobe_payload), stderr=""),
            subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ltcdump"], returncode=0, stdout="LTC 15:41:34:01\n", stderr=""),
        ]

        metadata = load_provider_metadata(Path("/tmp/clip.mov"), provider_kind="video")

        self.assertTrue(metadata.metadata_ok)
        self.assertEqual(metadata.start_timecode, "15:41:34:01")
        self.assertEqual(metadata.end_timecode, "15:41:34:21")
        self.assertEqual(metadata.metadata_provenance, "ltc_audio")
        self.assertEqual(metadata.metadata_confidence, "low")


if __name__ == "__main__":
    unittest.main()
