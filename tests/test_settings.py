import json
import tempfile
import unittest
from pathlib import Path

from r3dcontactsheet.settings import AppSettings, SettingsStore


class SettingsStoreTests(unittest.TestCase):
    def test_load_returns_defaults_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SettingsStore(Path(tmpdir) / "settings.json")

            settings = store.load()

            self.assertIsInstance(settings, AppSettings)
            self.assertEqual(settings.redline_path, "")
            self.assertIn("Using defaults", store.last_status)

    def test_load_gracefully_handles_corrupted_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.json"
            path.write_text("{not-json", encoding="utf-8")
            store = SettingsStore(path)

            settings = store.load()

            self.assertEqual(settings.redline_path, "")
            self.assertIn("unreadable", store.last_status)

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.json"
            store = SettingsStore(path)
            settings = AppSettings(
                redline_path="/Applications/REDline",
                last_output_path="/tmp/out",
                sync_mode="sync_off",
                theme_name="light",
            )

            store.save(settings)
            loaded = store.load()

            self.assertEqual(loaded.redline_path, "/Applications/REDline")
            self.assertEqual(loaded.last_output_path, "/tmp/out")
            self.assertEqual(loaded.sync_mode, "sync_off")
            self.assertEqual(loaded.theme_name, "light")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["redline_path"], "/Applications/REDline")


if __name__ == "__main__":
    unittest.main()
