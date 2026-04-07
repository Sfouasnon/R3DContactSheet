"""Tests for tool_resolver. stdlib unittest only."""
from __future__ import annotations
import os, stat, sys, unittest, importlib.util, tempfile, shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

def _import_tool_resolver():
    spec = importlib.util.spec_from_file_location(
        "tool_resolver",
        Path(__file__).parent.parent / "tool_resolver.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_tr = _import_tool_resolver()

def _make_executable(p): p.chmod(p.stat().st_mode | 0o111)
def _make_non_executable(p): p.chmod(p.stat().st_mode & ~0o111)

class TmpMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(); self.tmp = Path(self._tmp)
    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

class TestOverride(TmpMixin):
    def test_valid_override(self):
        f = self.tmp/"ffmpeg"; f.write_text("#!/bin/sh\n"); _make_executable(f)
        self.assertEqual(_tr.resolve_tool("ffmpeg", override=str(f)), str(f))
    def test_missing_override_falls_through(self):
        self.assertIsNone(_tr.resolve_tool("__xyz__", override=str(self.tmp/"nope")))
    def test_nonexec_override_falls_through(self):
        f = self.tmp/"ffmpeg"; f.write_text("#!/bin/sh\n"); _make_non_executable(f)
        self.assertIsNone(_tr.resolve_tool("__xyz__", override=str(f)))

class TestWhich(TmpMixin):
    def test_which_exec_returned(self):
        f = self.tmp/"t"; f.write_text("#!/bin/sh\n"); _make_executable(f)
        with patch("shutil.which", return_value=str(f)):
            self.assertEqual(_tr.resolve_tool("t"), str(f))
    def test_which_nonexec_falls_through(self):
        f = self.tmp/"t"; f.write_text("#!/bin/sh\n"); _make_non_executable(f)
        with patch("shutil.which", return_value=str(f)):
            self.assertIsNone(_tr.resolve_tool("__xyz__"))

class TestFallback(TmpMixin):
    def test_homebrew_fallback(self):
        f = self.tmp/"ffmpeg"; f.write_text("#!/bin/sh\n"); _make_executable(f)
        with patch.object(_tr, "_FALLBACK_DIRS", (str(self.tmp), "/usr/local/bin")):
            with patch("shutil.which", return_value=None):
                self.assertEqual(_tr.resolve_tool("ffmpeg"), str(f))
    def test_all_missing_returns_none(self):
        with patch("shutil.which", return_value=None):
            with patch.object(_tr, "_FALLBACK_DIRS", ("/no_a", "/no_b")):
                self.assertIsNone(_tr.resolve_tool("__xyz__"))

class TestMissing(unittest.TestCase):
    def test_missing_returns_none(self):
        with patch("shutil.which", return_value=None):
            with patch.object(_tr, "_FALLBACK_DIRS", ()):
                self.assertIsNone(_tr.resolve_tool("__xyz__"))

class TestConvenience(unittest.TestCase):
    def test_ffmpeg_delegates(self):
        with patch.object(_tr, "resolve_tool", return_value="/opt/homebrew/bin/ffmpeg") as m:
            r = _tr.resolve_ffmpeg(); m.assert_called_once_with("ffmpeg", override=None)
    def test_ffprobe_delegates(self):
        with patch.object(_tr, "resolve_tool", return_value="/opt/homebrew/bin/ffprobe") as m:
            r = _tr.resolve_ffprobe(); m.assert_called_once_with("ffprobe", override=None)
    def test_ffmpeg_override_passed(self):
        with patch.object(_tr, "resolve_tool", return_value="/x") as m:
            _tr.resolve_ffmpeg(override="/x"); m.assert_called_once_with("ffmpeg", override="/x")
    def test_ffprobe_override_passed(self):
        with patch.object(_tr, "resolve_tool", return_value="/x") as m:
            _tr.resolve_ffprobe(override="/x"); m.assert_called_once_with("ffprobe", override="/x")

class TestFallbackDirs(unittest.TestCase):
    def test_homebrew_in_fallback_dirs(self):
        self.assertIn("/opt/homebrew/bin", _tr._FALLBACK_DIRS)
    def test_usr_local_in_fallback_dirs(self):
        self.assertIn("/usr/local/bin", _tr._FALLBACK_DIRS)

class TestErrorMessages(unittest.TestCase):
    def test_render_error_mentions_searched_paths(self):
        src = (Path(__file__).parent.parent / "media_render.py").read_text()
        self.assertIn("/opt/homebrew/bin", src)
        self.assertIn("/usr/local/bin", src)
        self.assertNotIn("Install ffmpeg on this machine to render non-RED sources.", src)

if __name__ == "__main__":
    unittest.main()
