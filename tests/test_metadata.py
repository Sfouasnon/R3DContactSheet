import unittest
from pathlib import Path
from unittest.mock import patch

from r3dcontactsheet.metadata import _parse_perframe_csv, load_clip_metadata, parse_redline_printmeta


class MetadataParsingTests(unittest.TestCase):
    def test_parse_redline_printmeta_reads_csv_header_and_row(self):
        text = "\n".join(
            [
                "Clip Name,Clip Frame Rate,Edge Timecode,Timecode Base,Frame Size",
                "A003_A001_0127R2_001,23.976,12:11:12:01,23.976,6144x3160",
            ]
        )

        fields = parse_redline_printmeta(text)

        self.assertEqual(fields["Clip Frame Rate"], "23.976")
        self.assertEqual(fields["Edge Timecode"], "12:11:12:01")
        self.assertEqual(fields["Timecode Base"], "23.976")
        self.assertEqual(fields["Frame Size"], "6144x3160")

    def test_parse_perframe_csv_reads_real_redline_rows(self):
        text = "\n".join(
            [
                "FrameNo,Timecode,Timestamp",
                "0,15:41:34:01,20761902289",
                "1,15:41:34:02,20761943970",
                "20,15:41:34:21,20762735616",
            ]
        )

        rows = _parse_perframe_csv(text)

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["Timecode"], "15:41:34:01")
        self.assertEqual(rows[-1]["Timecode"], "15:41:34:21")

    @patch("r3dcontactsheet.metadata.subprocess.run")
    def test_load_clip_metadata_prefers_perframe_timecodes_and_count(self, mock_run):
        summary_output = "\n".join(
            [
                "Clip Name,Clip Frame Rate,Edge Timecode,Timecode Base,Frame Size",
                "A003_A001_0127R2_001,24,01:00:00:00,24,5760x3240",
            ]
        )
        perframe_output = "\n".join(
            [
                "FrameNo,Timecode,Timestamp",
                "0,15:41:34:01,20761902289",
                "1,15:41:34:02,20761943970",
                "2,15:41:34:03,20761985623",
                "20,15:41:34:21,20762735616",
            ]
        )
        mock_run.side_effect = [
            self._completed(summary_output),
            self._completed(summary_output),
            self._completed(perframe_output),
        ]

        metadata = load_clip_metadata(Path("/tmp/A001.R3D"), "/Applications/REDline")

        self.assertEqual(metadata.start_timecode, "15:41:34:01")
        self.assertEqual(metadata.end_timecode, "15:41:34:21")
        self.assertEqual(metadata.total_frames, 4)
        self.assertTrue(metadata.metadata_ok)
        self.assertEqual(metadata.timecode_source, "per-frame CSV")
        perframe_call = mock_run.call_args_list[-1].args[0]
        self.assertIn("--printMeta", perframe_call)
        self.assertIn("5", perframe_call)
        self.assertNotIn("--silent", perframe_call)

    @patch("r3dcontactsheet.metadata.subprocess.run")
    def test_load_clip_metadata_marks_timecode_incomplete_when_perframe_missing(self, mock_run):
        summary_output = "\n".join(
            [
                "Clip Name,Clip Frame Rate,Edge Timecode,Timecode Base,Frame Size",
                "A003_A001_0127R2_001,24,01:00:00:00,24,5760x3240",
            ]
        )
        mock_run.side_effect = [
            self._completed(summary_output),
            self._completed(summary_output),
            self._completed(""),
        ]

        metadata = load_clip_metadata(Path("/tmp/A001.R3D"), "/Applications/REDline")

        self.assertIsNone(metadata.start_timecode)
        self.assertIsNone(metadata.end_timecode)
        self.assertIsNone(metadata.total_frames)
        self.assertFalse(metadata.metadata_ok)

    def _completed(self, stdout: str):
        import subprocess

        return subprocess.CompletedProcess(args=["REDline"], returncode=0, stdout=stdout, stderr="")


if __name__ == "__main__":
    unittest.main()
