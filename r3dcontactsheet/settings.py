"""Persisted application settings for the macOS desktop app."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


def default_settings_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "R3DContactSheet" / "settings.json"


@dataclass
class AppSettings:
    redline_path: str = ""
    last_input_path: str = ""
    last_output_path: str = ""
    frame_index: int = 6
    target_timecode: str = ""
    fps: str = "23.976"
    drop_frame: bool = False
    color_sci_version: int = 3
    output_tone_map: int = 1
    roll_off: int = 2
    output_gamma_curve: int = 32
    render_res: int = 4
    resize_x: str = ""
    resize_y: str = ""
    group_mode: str = "flat"
    custom_group_name: str = ""
    alphabetize: bool = True
    metadata_mode: bool = True


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_settings_path()

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        defaults = asdict(AppSettings())
        defaults.update(data)
        return AppSettings(**defaults)

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
