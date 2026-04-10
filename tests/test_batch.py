import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r3dcontactsheet.batch import (
    BatchOptions,
    ClipEntry,
    build_job_plan,
    build_preview_context,
    describe_source_selection,
    discover_r3d_clips,
)
from r3dcontactsheet.frame_index import FrameTargetRequest
from r3dcontactsheet.metadata import ClipMetadata
from r3dcontactsheet.redline import RenderSettings, RenderJob, build_redline_command, write_batch_file


class BatchDiscoveryTests(unittest.TestCase):
    def test_rdc_selection_resolves_primary_segment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            package = Path(tmpdir) / "A001_C001_000001.RDC"
            package.mkdir()
            (package / "A001_C001_000001_002.R3D").write_text("segment2")
            (package / "A001_C001_000001_001.R3D").write_text("segment1")

            clips = discover_r3d_clips(package)

            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].source_path.name, "A001_C001_000001_001.R3D")
            self.assertEqual(clips[0].package_path, package.resolve())
            self.assertEqual(clips[0].segment_count, 2)

    def test_folder_scan_finds_rdc_and_standalone_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package = root / "camA" / "B001_C001_000001.RDC"
            package.mkdir(parents=True)
            (package / "B001_C001_000001_001.R3D").write_text("segment1")
            (package / "B001_C001_000001_002.R3D").write_text("segment2")
            standalone = root / "camB" / "B002_C001_000001.R3D"
            standalone.parent.mkdir(parents=True)
            standalone.write_text("clip")

            clips = discover_r3d_clips(root, group_mode="parent_folder")

            self.assertEqual(len(clips), 2)
            self.assertEqual([clip.clip_name for clip in clips], ["B001_C001_000001_001", "B002_C001_000001"])
            self.assertEqual(clips[0].group_name, "B001_C001_000001")
            self.assertEqual(clips[1].group_name, "camB")
            self.assertEqual(clips[0].reel_name, "001")

    def test_describe_source_selection_reports_rdc_segments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            package = Path(tmpdir) / "clip.RDC"
            package.mkdir()
            (package / "clip_001.R3D").write_text("segment1")
            (package / "clip_002.R3D").write_text("segment2")

            description = describe_source_selection(package)

            self.assertIn("Selected RDC package", description)
            self.assertIn("2 segments", description)

    def test_folder_scan_includes_generic_video_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "Witness_A" / "cam01.mov"
            video.parent.mkdir(parents=True)
            video.write_text("video")

            clips = discover_r3d_clips(root, group_mode="parent_folder")

            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].provider_kind, "video")
            self.assertEqual(clips[0].source_kind, "video")
            self.assertEqual(clips[0].group_name, "Witness_A")


class ReplayScriptTests(unittest.TestCase):
    def test_replay_script_uses_verified_render_flags_with_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "out"
            output_dir.mkdir()
            job = RenderJob(
                input_file=Path(tmpdir) / "clip.R3D",
                frame_index=6,
                output_file=output_dir / "clip.jpg",
                settings=RenderSettings(),
            )
            command = build_redline_command("/Applications/REDline", job)
            self.assertIn("--colorSciVersion", command)
            self.assertIn("3", command)
            self.assertIn("--outputToneMap", command)
            self.assertIn("--rollOff", command)
            self.assertIn("--gammaCurve", command)
            self.assertIn("--useMeta", command)

            script = write_batch_file([job], output_dir / "replay.sh", redline_exe="/Applications/REDline")
            contents = script.read_text(encoding="utf-8")
            self.assertTrue(script.exists())
            self.assertIn("--colorSciVersion 3", contents)
            self.assertIn("--outputToneMap 1", contents)
            self.assertIn("--rollOff 2", contents)
            self.assertIn("--gammaCurve 32", contents)
            self.assertIn("--useMeta", contents)

    @patch("r3dcontactsheet.batch.load_provider_metadata")
    def test_custom_group_name_overrides_clip_group(self, mock_load_provider_metadata):
        self._configure_mock_metadata(mock_load_provider_metadata)
        with tempfile.TemporaryDirectory() as tmpdir:
            clip = Path(tmpdir) / "A003_A001_0127R2_001.R3D"
            clip.write_text("clip")
            clips = discover_r3d_clips(clip, group_mode="flat")
            plan = build_job_plan(
                clips,
                BatchOptions(
                    output_dir=Path(tmpdir) / "out",
                    frame_request=FrameTargetRequest(),
                    settings=RenderSettings(),
                    group_mode="custom",
                    custom_group_name="A_CAM_ARRAY",
                    redline_exe="/Applications/REDline",
                ),
            )
            self.assertEqual(plan[0].output_group, "A_CAM_ARRAY")
            self.assertIn("/frames/", str(plan[0].output_file))
            self.assertTrue(plan[0].output_file.name.startswith("001_A003_A001"))

    @patch("r3dcontactsheet.batch.load_provider_metadata")
    def test_build_preview_context_uses_metadata_cache_and_progress_callback(self, mock_load_provider_metadata):
        self._configure_mock_metadata(mock_load_provider_metadata)
        with tempfile.TemporaryDirectory() as tmpdir:
            clip_path = Path(tmpdir) / "A003_A001_0127R2_001.R3D"
            clip_path.write_text("clip")
            clip = ClipEntry(
                source_path=clip_path.resolve(),
                clip_name=clip_path.stem,
                reel_name="003",
                group_name="renders",
                source_kind="r3d",
                provider_kind="red",
            )
            progress = []
            cache = {}
            options = BatchOptions(
                output_dir=Path(tmpdir) / "out",
                frame_request=FrameTargetRequest(),
                settings=RenderSettings(),
                redline_exe="/Applications/REDline",
            )

            build_preview_context([clip, clip], options, metadata_cache=cache, progress_callback=lambda processed, total, _clip, _metadata: progress.append((processed, total)))

            self.assertEqual(mock_load_provider_metadata.call_count, 1)
            self.assertEqual(progress, [(1, 1)])

    def _configure_mock_metadata(self, mock_load_provider_metadata):
        mock_load_provider_metadata.return_value = ClipMetadata(
            clip_path=Path("/tmp/mock.R3D"),
            clip_fps=23.976,
            timecode_base_fps=23.976,
            start_timecode="15:06:53:21",
            total_frames=240,
            resolution="6144x3160",
            timecode_source="edge timecode",
            drop_frame=False,
            sync_basis="REDline printMeta",
            metadata_ok=True,
            raw_fields={},
            end_timecode="15:07:03:20",
            manufacturer="RED",
            format_type="R3D",
            provider_name="red",
            timecode_supported=True,
            sync_eligible=True,
            render_supported=True,
        )


if __name__ == "__main__":
    unittest.main()
