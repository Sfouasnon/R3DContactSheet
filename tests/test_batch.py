import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r3dcontactsheet.batch import (
    BatchAssignment,
    BatchOptions,
    ClipEntry,
    build_batch_groups,
    build_batch_output_path,
    build_batch_pdf_name,
    build_batch_scan_result,
    build_job_plan,
    build_preview_context,
    describe_source_selection,
    discover_r3d_clips,
    parse_batch_key,
    regroup_batch_groups,
    suggest_clip_family,
    suggest_subgroup,
)
from r3dcontactsheet.frame_index import FrameTargetRequest
from r3dcontactsheet.metadata import ClipMetadata
from r3dcontactsheet.redline import RenderSettings, RenderJob, build_redline_command, write_batch_file


class BatchDiscoveryTests(unittest.TestCase):
    def test_parse_batch_key_collapses_lettered_clip_to_numeric_clip(self):
        self.assertEqual(parse_batch_key("G007_A081"), ("007", "081"))
        self.assertEqual(parse_batch_key("H007_D083"), ("007", "083"))
        self.assertEqual(parse_batch_key("I007_C076"), ("007", "076"))
        self.assertIsNone(parse_batch_key("WitnessCam01"))

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

    def test_build_batch_groups_collapses_multiple_camera_prefixes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for name in (
                "G007_A081.R3D",
                "G007_C081.R3D",
                "H007_A081.R3D",
                "I007_D081.R3D",
                "I007_C076.R3D",
            ):
                (root / name).write_text("clip")

            groups = build_batch_groups(root)

            self.assertEqual([(group.reel_number, group.clip_number) for group in groups], [("007", "076"), ("007", "081")])
            self.assertEqual(groups[1].source_clip_count, 4)
            self.assertEqual(groups[1].output_pdf_name, "Reel_007_081_contact_sheet.pdf")

    def test_build_batch_output_path_uses_reel_subfolder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            group = build_batch_groups(_make_batch_root(tmpdir, ["G007_A081.R3D"]))[0]
            output_path = build_batch_output_path(Path(tmpdir) / "out", group)
            self.assertEqual(output_path.name, "Reel_007_081_contact_sheet.pdf")
            self.assertEqual(output_path.parent.name, "Reel_007")

    def test_build_batch_groups_logs_unparsable_clips_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "bad_name.R3D").write_text("clip")
            (root / "G007_A081.R3D").write_text("clip")
            messages = []

            groups = build_batch_groups(root, log_callback=messages.append)

            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].clip_number, "081")
            self.assertTrue(any("Needs batch assignment" in message for message in messages))

    def test_build_batch_scan_result_keeps_unparsed_clips_visible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "MVI_2058 HDR.mov").write_text("clip")
            (root / "G007_A081.R3D").write_text("clip")

            result = build_batch_scan_result(root)

            self.assertEqual(len(result.assignments), 2)
            needs_assignment = [item for item in result.assignments if item.assignment_state == "needs_assignment"]
            self.assertEqual(len(needs_assignment), 1)
            self.assertEqual(needs_assignment[0].clip.clip_name, "MVI_2058 HDR")
            self.assertEqual(len(result.groups), 1)

    def test_manual_assignment_joins_existing_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parsed = root / "G007_A081.R3D"
            parsed.write_text("clip")
            unparsed = root / "MVI_2058 HDR.mov"
            unparsed.write_text("clip")

            result = build_batch_scan_result(root)
            assignments = list(result.assignments)
            target = next(item for item in assignments if item.clip.source_path.name == "MVI_2058 HDR.mov")
            target.reel_number = "007"
            target.clip_number = "081"
            target.assignment_state = "auto_assigned"

            groups = regroup_batch_groups(assignments)

            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].batch_id, "007_081")
            self.assertEqual(groups[0].source_clip_count, 2)

    def test_operator_exclusion_omits_assignment_from_group(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "G007_A081.R3D").write_text("clip")
            (root / "H007_B081.R3D").write_text("clip")

            result = build_batch_scan_result(root)
            assignments = list(result.assignments)
            assignments[1].assignment_state = "excluded_by_operator"

            groups = regroup_batch_groups(assignments)

            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].source_clip_count, 1)

    @patch("r3dcontactsheet.batch.load_provider_metadata")
    def test_unparsed_mixed_media_auto_assigns_only_when_metadata_maps_uniquely(self, mock_load_provider_metadata):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            red_a = root / "G007_A081.R3D"
            red_b = root / "H007_B081.R3D"
            witness = root / "MVI_2058 HDR.mov"
            for path in (red_a, red_b, witness):
                path.write_text("clip")

            def fake_metadata(path, provider_kind, redline_exe=None, timeout=20.0):
                if path.suffix.lower() == ".r3d":
                    return ClipMetadata(
                        clip_path=path,
                        clip_fps=24.0,
                        timecode_base_fps=24.0,
                        start_timecode="15:41:34:01",
                        total_frames=21,
                        resolution="1920x1080",
                        timecode_source="redline",
                        drop_frame=False,
                        sync_basis="REDline printMeta",
                        metadata_ok=True,
                        raw_fields={},
                        end_timecode="15:41:34:21",
                        manufacturer="RED",
                        format_type="R3D",
                        provider_name="red",
                        timecode_supported=True,
                        sync_eligible=True,
                        render_supported=True,
                        metadata_provenance="redline",
                        metadata_confidence="high",
                    )
                return ClipMetadata(
                    clip_path=path,
                    clip_fps=24.0,
                    timecode_base_fps=24.0,
                    start_timecode="15:41:34:05",
                    total_frames=10,
                    resolution="1920x1080",
                    timecode_source="ffprobe stream/format tags",
                    drop_frame=False,
                    sync_basis="ffprobe metadata",
                    metadata_ok=True,
                    raw_fields={},
                    end_timecode="15:41:34:14",
                    manufacturer="Generic Video",
                    format_type="MOV",
                    provider_name="video",
                    timecode_supported=True,
                    sync_eligible=True,
                    render_supported=True,
                    metadata_provenance="ffprobe",
                    metadata_confidence="high",
                )

            mock_load_provider_metadata.side_effect = fake_metadata

            result = build_batch_scan_result(root, redline_exe="/Applications/REDline")

            witness_assignment = next(item for item in result.assignments if item.clip.source_path.name == "MVI_2058 HDR.mov")
            self.assertEqual(witness_assignment.assignment_state, "auto_assigned")
            self.assertEqual((witness_assignment.reel_number, witness_assignment.clip_number), ("007", "081"))
            self.assertEqual(witness_assignment.metadata_source, "ffprobe")
            self.assertEqual(witness_assignment.metadata_confidence, "high")

    @patch("r3dcontactsheet.batch.load_provider_metadata")
    def test_unparsed_clip_stays_unresolved_when_metadata_confidence_is_low(self, mock_load_provider_metadata):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parsed = root / "G007_A081.R3D"
            unparsed = root / "MVI_2058 HDR.mov"
            parsed.write_text("clip")
            unparsed.write_text("clip")

            def fake_metadata(path, provider_kind, redline_exe=None, timeout=20.0):
                if path.suffix.lower() == ".r3d":
                    return ClipMetadata(
                        clip_path=path,
                        clip_fps=24.0,
                        timecode_base_fps=24.0,
                        start_timecode="15:41:34:01",
                        total_frames=21,
                        resolution="1920x1080",
                        timecode_source="redline",
                        drop_frame=False,
                        sync_basis="REDline printMeta",
                        metadata_ok=True,
                        raw_fields={},
                        end_timecode="15:41:34:21",
                        manufacturer="RED",
                        format_type="R3D",
                        provider_name="red",
                        timecode_supported=True,
                        sync_eligible=True,
                        render_supported=True,
                        metadata_provenance="redline",
                        metadata_confidence="high",
                    )
                return ClipMetadata(
                    clip_path=path,
                    clip_fps=24.0,
                    timecode_base_fps=24.0,
                    start_timecode="15:41:34:05",
                    total_frames=10,
                    resolution="1920x1080",
                    timecode_source="ltc audio decode",
                    drop_frame=False,
                    sync_basis="ltc audio decode",
                    metadata_ok=True,
                    raw_fields={},
                    end_timecode="15:41:34:14",
                    manufacturer="Generic Video",
                    format_type="MOV",
                    provider_name="video",
                    timecode_supported=True,
                    sync_eligible=True,
                    render_supported=True,
                    metadata_provenance="ltc_audio",
                    metadata_confidence="low",
                    metadata_error="ltc audio decode: partial timing only",
                )

            mock_load_provider_metadata.side_effect = fake_metadata

            result = build_batch_scan_result(root, redline_exe="/Applications/REDline")

            assignment = next(item for item in result.assignments if item.clip.source_path.name == "MVI_2058 HDR.mov")
            self.assertEqual(assignment.assignment_state, "needs_assignment")
            self.assertEqual(assignment.metadata_source, "ltc_audio")
            self.assertEqual(assignment.metadata_confidence, "low")

    def test_suggest_clip_family_and_subgroup_defaults(self):
        red_clip = ClipEntry(
            source_path=Path("/tmp/G007_A081.R3D"),
            clip_name="G007_A081",
            reel_name="007",
            group_name="renders",
            source_kind="r3d",
            provider_kind="red",
            manufacturer="RED",
            format_type="R3D",
        )
        generic_clip = ClipEntry(
            source_path=Path("/tmp/reference/hyperdeck_A001.mov"),
            clip_name="hyperdeck_A001",
            reel_name="001",
            group_name="renders",
            source_kind="video",
            provider_kind="video",
            manufacturer="",
            format_type="MOV",
        )

        self.assertEqual(suggest_clip_family(red_clip), "RED")
        self.assertEqual(suggest_subgroup(red_clip), "Main Cameras")
        self.assertEqual(suggest_clip_family(generic_clip), "HyperDeck")
        self.assertEqual(suggest_subgroup(generic_clip), "Reference")


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


def _make_batch_root(tmpdir: str, filenames: list[str]) -> Path:
    root = Path(tmpdir) / "batch_root"
    root.mkdir()
    for name in filenames:
        (root / name).write_text("clip")
    return root


if __name__ == "__main__":
    unittest.main()
