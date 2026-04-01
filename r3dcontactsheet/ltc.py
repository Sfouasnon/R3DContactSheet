"""LTC ingestion scaffolding for future frame targeting workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .timecode import parse_timecode


@dataclass(frozen=True)
class LTCInput:
    source: str
    start_timecode: str
    fps: float
    drop_frame: bool = False
    notes: str = ""


@dataclass(frozen=True)
class LTCParseResult:
    input: LTCInput
    source_path: Optional[Path] = None


def load_ltc_text(source: str, fps: float, drop_frame: bool = False) -> LTCParseResult:
    value = source.strip()
    if not value:
        raise ValueError("LTC input cannot be empty")
    parse_timecode(value)
    return LTCParseResult(
        input=LTCInput(
            source="manual",
            start_timecode=value,
            fps=fps,
            drop_frame=drop_frame,
            notes="Manual placeholder entry until waveform/audio LTC ingest is added.",
        )
    )
