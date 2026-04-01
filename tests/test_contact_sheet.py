import tempfile
import unittest
from pathlib import Path

from PIL import Image

from r3dcontactsheet.contact_sheet import ContactSheetItem, build_contact_sheet_pdf


class ContactSheetPdfTests(unittest.TestCase):
    def test_build_contact_sheet_pdf_writes_multi_item_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            items = []
            for index in range(13):
                image_path = root / f"clip_{index}.jpg"
                Image.new("RGB", (320, 180), color=(index * 10 % 255, 50, 90)).save(image_path, "JPEG")
                items.append(
                    ContactSheetItem(
                        image_path=image_path,
                        clip_label=f"Clip {index}",
                        group_label="A_CAM",
                        frame_label=f"Abs Frame {index}",
                        timecode_label="12:11:12:01",
                        fps_label="FPS 23.976",
                        resolution_label="6144x3160",
                        sync_label="Sync verified",
                    )
                )

            destination = root / "sheet.pdf"
            result = build_contact_sheet_pdf(items, destination, "Synchronized Contact Sheet", header_lines=["Camera count: 13"])

            self.assertEqual(result, destination.resolve())
            self.assertTrue(destination.exists())
            self.assertGreater(destination.stat().st_size, 1024)


if __name__ == "__main__":
    unittest.main()
