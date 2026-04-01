import unittest
from pathlib import Path

from r3dcontactsheet.frame_index import FrameTargetRequest, clip_timecode_out, resolve_clip_frame, resolve_matching_moment
from r3dcontactsheet.metadata import ClipMetadata


class AutomaticSyncTests(unittest.TestCase):
    def test_auto_sync_defaults_to_midpoint_of_shared_overlap(self):
        clips = [
            ClipMetadata(
                clip_path=Path("/tmp/A001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:53:21",
                total_frames=120,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
            ClipMetadata(
                clip_path=Path("/tmp/B001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:54:00",
                total_frames=120,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
        ]

        moment = resolve_matching_moment(clips, FrameTargetRequest())
        first = resolve_clip_frame(clips[0], moment)
        second = resolve_clip_frame(clips[1], moment)

        self.assertEqual(moment.source_timecode, "15:06:56:10")
        self.assertEqual(first.resolved_timecode, "15:06:56:10")
        self.assertEqual(second.resolved_timecode, "15:06:56:10")
        self.assertGreater(second.frame_index, 0)
        self.assertGreater(first.frame_index, 0)

    def test_zero_based_metadata_timecode_does_not_raise(self):
        clips = [
            ClipMetadata(
                clip_path=Path("/tmp/A001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="01:00:00:00",
                total_frames=120,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
            ClipMetadata(
                clip_path=Path("/tmp/B001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="01:00:00:00",
                total_frames=120,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
        ]

        moment = resolve_matching_moment(clips, FrameTargetRequest())

        self.assertEqual(moment.source_timecode, "01:00:02:11")

    def test_tc_out_is_unavailable_when_total_frames_missing(self):
        clip = ClipMetadata(
            clip_path=Path("/tmp/A001.R3D"),
            clip_fps=24.0,
            timecode_base_fps=24.0,
            start_timecode="15:06:53:21",
            total_frames=None,
            resolution="5760x3240",
            timecode_source="edge timecode",
            drop_frame=False,
            sync_basis="REDline printMeta",
            metadata_ok=True,
            raw_fields={},
        )

        self.assertIsNone(clip_timecode_out(clip))

    def test_partial_sync_chooses_real_metadata_timecode_from_largest_subset(self):
        clips = [
            ClipMetadata(
                clip_path=Path("/tmp/A001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:53:21",
                total_frames=10,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
            ClipMetadata(
                clip_path=Path("/tmp/B001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:54:00",
                total_frames=10,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
            ClipMetadata(
                clip_path=Path("/tmp/C001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:55:00",
                total_frames=1,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
        ]

        moment = resolve_matching_moment(clips, FrameTargetRequest())
        first = resolve_clip_frame(clips[0], moment)
        third = resolve_clip_frame(clips[2], moment)

        self.assertEqual(moment.sync_mode, "partial")
        self.assertEqual(moment.source_timecode, "15:06:54:03")
        self.assertTrue(first.in_matched_subset)
        self.assertEqual(third.sync_status, "outside_overlap")

    def test_no_common_moment_keeps_real_per_clip_metadata_timecodes(self):
        clips = [
            ClipMetadata(
                clip_path=Path("/tmp/A001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:53:21",
                total_frames=1,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
            ClipMetadata(
                clip_path=Path("/tmp/B001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:54:00",
                total_frames=1,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
        ]

        moment = resolve_matching_moment(clips, FrameTargetRequest())
        first = resolve_clip_frame(clips[0], moment)
        second = resolve_clip_frame(clips[1], moment)

        self.assertEqual(moment.sync_mode, "none")
        self.assertEqual(first.resolved_timecode, "15:06:53:21")
        self.assertEqual(second.resolved_timecode, "15:06:54:00")
        self.assertEqual(first.sync_status, "outside_overlap")

    def test_largest_matching_subset_uses_overlap_start_candidate(self):
        clips = [
            ClipMetadata(
                clip_path=Path("/tmp/A001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:53:20",
                total_frames=4,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
            ClipMetadata(
                clip_path=Path("/tmp/B001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:53:22",
                total_frames=4,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
            ClipMetadata(
                clip_path=Path("/tmp/C001.R3D"),
                clip_fps=24.0,
                timecode_base_fps=24.0,
                start_timecode="15:06:53:21",
                total_frames=2,
                resolution="5760x3240",
                timecode_source="edge timecode",
                drop_frame=False,
                sync_basis="REDline printMeta",
                metadata_ok=True,
                raw_fields={},
            ),
        ]

        moment = resolve_matching_moment(clips, FrameTargetRequest())

        self.assertEqual(moment.sync_mode, "full")
        self.assertEqual(moment.source_timecode, "15:06:53:22")


if __name__ == "__main__":
    unittest.main()
