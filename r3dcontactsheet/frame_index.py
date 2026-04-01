"""Frame resolution helpers and scaffolding for LTC-driven selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .timecode import timecode_to_frame


@dataclass(frozen=True)
class FrameTargetRequest:
    frame_index: Optional[int] = None
    target_timecode: Optional[str] = None
    fps: Optional[float] = None
    drop_frame: bool = False
    verify_matching_frame: bool = True


@dataclass(frozen=True)
class FrameResolution:
    frame_index: int
    source: str
    verification_note: str


def resolve_frame_target(request: FrameTargetRequest, clip_path: Optional[Path] = None) -> FrameResolution:
    if request.frame_index is not None:
        if request.frame_index < 0:
            raise ValueError("frame_index must be >= 0")
        note = "Direct frame index selected."
        if request.verify_matching_frame:
            note += " Matching-frame verification scaffold is ready for clip metadata integration."
        return FrameResolution(
            frame_index=request.frame_index,
            source="frame_index",
            verification_note=note,
        )

    if request.target_timecode:
        if request.fps is None:
            location = f" for {clip_path}" if clip_path else ""
            raise ValueError(
                f"Cannot resolve target timecode{location} without fps metadata or a user-supplied fps."
            )
        frame_index = timecode_to_frame(
            request.target_timecode,
            request.fps,
            drop_frame=request.drop_frame,
        )
        return FrameResolution(
            frame_index=frame_index,
            source="timecode",
            verification_note=(
                "Resolved from target timecode using supplied fps. "
                "Per-clip metadata verification is the next refinement."
            ),
        )

    raise ValueError("Provide either frame_index or target_timecode.")
