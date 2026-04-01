"""Persisted application settings for the macOS desktop app."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def default_settings_path() -> Path:
    config_root = os.getenv("XDG_CONFIG_HOME")
    if config_root:
        return Path(config_root).expanduser() / "R3DContactSheet" / "settings.json"
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
        self.last_status = f"Using defaults. Settings path: {self.path}"

    def load(self) -> AppSettings:
        if not self.path.exists():
            self.last_status = f"Settings file not found. Using defaults at {self.path}."
            return AppSettings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            defaults = asdict(AppSettings())
            if not isinstance(data, dict):
                raise ValueError("Settings file did not contain a JSON object.")
            defaults.update(data)
            self.last_status = f"Loaded settings from {self.path}."
            return AppSettings(**defaults)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            self.last_status = f"Settings were unreadable at {self.path}. Using defaults. ({exc})"
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), indent=2)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(self.path)
        self.last_status = f"Saved settings to {self.path}."
