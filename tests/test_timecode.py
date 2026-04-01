import unittest

from r3dcontactsheet.timecode import frame_to_timecode, parse_timecode, timecode_to_frame


class TimecodeTests(unittest.TestCase):
    def test_parse_timecode(self):
        tc = parse_timecode("01:02:03:04")
        self.assertEqual(tc.hours, 1)
        self.assertEqual(tc.minutes, 2)
        self.assertEqual(tc.seconds, 3)
        self.assertEqual(tc.frames, 4)

    def test_non_drop_frame_roundtrip(self):
        frame = timecode_to_frame("00:00:10:12", 24.0)
        self.assertEqual(frame, 252)
        self.assertEqual(frame_to_timecode(frame, 24.0), "00:00:10:12")

    def test_drop_frame_roundtrip(self):
        frame = timecode_to_frame("00:01:00;02", 29.97, drop_frame=True)
        self.assertEqual(frame, 1800)
        self.assertEqual(frame_to_timecode(frame, 29.97, drop_frame=True), "00:01:00;02")


if __name__ == "__main__":
    unittest.main()
