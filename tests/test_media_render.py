import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from r3dcontactsheet.batch import ClipEntry, ClipOrganization, JobPlanItem
from r3dcontactsheet.frame_index import FrameResolution, MatchingMoment
from r3dcontactsheet.media_render import GenericRenderResult, render_plan_items_parallel
from r3dcontactsheet.metadata import ClipMetadata


def _plan_item(root: Path, index: int) -> JobPlanItem:
    clip_path = root / f"clip_{index:03d}.mov"
    clip_path.write_text("video")
    metadata = ClipMetadata(
        clip_path=clip_path,
        clip_fps=24.0,
        timecode_base_fps=24.0,
        start_timecode="10:00:00:00",
        total_frames=100,
        resolution="1920x1080",
        timecode_source="ffprobe",
        drop_frame=False,
        sync_basis="test",
        metadata_ok=True,
        raw_fields={},
        end_timecode="10:00:04:03",
        manufacturer="Generic",
        format_type="MOV",
        provider_name="video",
        timecode_supported=True,
        sync_eligible=True,
        render_supported=True,
    )
    return JobPlanItem(
        clip=ClipEntry(
            source_path=clip_path,
            clip_name=clip_path.stem,
            reel_name="001",
            group_name="renders",
            source_kind="video",
            provider_kind="video",
        ),
        clip_metadata=metadata,
        clip_fields=ClipOrganization(group_name="renders", camera_label=f"Cam {index}"),
        frame_resolution=FrameResolution(
            frame_index=10,
            source="test",
            verification_note="test",
            absolute_frame=10,
            resolved_timecode="10:00:00:10",
            clip_fps=24.0,
            timecode_base_fps=24.0,
            sync_basis="test",
            sync_status="exact_match",
        ),
        matching_moment=MatchingMoment(
            absolute_frame=10,
            source_timecode="10:00:00:10",
            source="test",
            sync_status="verified",
            note="test",
        ),
        output_group="renders",
        output_file=root / "frames" / f"{index:03d}_Cam_{index}.jpg",
        render_job=None,
    )


class ParallelRenderTests(unittest.TestCase):
    @patch("r3dcontactsheet.media_render.render_plan_item")
    def test_parallel_render_reports_all_progress(self, mock_render_plan_item):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "frames").mkdir()
            plan = [_plan_item(root, index) for index in range(1, 5)]
            progress = []

            def fake_render(item, *, redline_exe, min_output_bytes):
                time.sleep(0.01)
                item.output_file.write_bytes(b"x" * 4096)
                return GenericRenderResult(
                    command=["ffmpeg"],
                    output_path=item.output_file,
                    output_size=4096,
                    stdout="",
                    stderr="",
                )

            mock_render_plan_item.side_effect = fake_render
            outcomes = render_plan_items_parallel(
                plan,
                redline_exe=None,
                min_output_bytes=1024,
                max_workers=2,
                progress_callback=lambda outcome, completed, total: progress.append((outcome.index, completed, total)),
            )

            self.assertEqual(len(outcomes), 4)
            self.assertEqual([outcome.index for outcome in outcomes], [1, 2, 3, 4])
            self.assertEqual(progress[-1][1:], (4, 4))
            self.assertTrue(all(item.output_file.exists() for item in plan))


if __name__ == "__main__":
    unittest.main()
