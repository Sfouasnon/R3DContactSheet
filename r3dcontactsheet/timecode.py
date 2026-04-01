"""SMPTE-style timecode helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimecodeParts:
    hours: int
    minutes: int
    seconds: int
    frames: int
    drop_frame: bool = False

    def normalized(self) -> str:
        separator = ";" if self.drop_frame else ":"
        return (
            f"{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}{separator}{self.frames:02d}"
        )


def parse_timecode(tc: str) -> TimecodeParts:
    value = tc.strip()
    drop_frame = ";" in value
    separator = ";" if drop_frame else ":"
    parts = value.replace(";", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"Timecode must look like HH:MM:SS:FF, got {tc!r}")
    try:
        hours, minutes, seconds, frames = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Timecode contains non-numeric fields: {tc!r}") from exc
    if minutes not in range(60) or seconds not in range(60):
        raise ValueError(f"Invalid timecode field in {tc!r}")
    if hours < 0 or frames < 0:
        raise ValueError(f"Invalid timecode field in {tc!r}")
    return TimecodeParts(hours, minutes, seconds, frames, drop_frame=drop_frame or separator == ";")


def timecode_to_frame(tc: str, fps: float, drop_frame: bool = False) -> int:
    if fps <= 0:
        raise ValueError("fps must be greater than zero")
    parts = parse_timecode(tc)
    if drop_frame or parts.drop_frame:
        nominal_fps = _drop_frame_rate(fps)
        total_minutes = parts.hours * 60 + parts.minutes
        dropped = 2 * (total_minutes - total_minutes // 10)
        return (
            ((parts.hours * 3600) + (parts.minutes * 60) + parts.seconds) * nominal_fps
            + parts.frames
            - dropped
        )
    return (
        ((parts.hours * 3600) + (parts.minutes * 60) + parts.seconds) * int(round(fps))
        + parts.frames
    )


def frame_to_timecode(frame: int, fps: float, drop_frame: bool = False) -> str:
    if frame < 0:
        raise ValueError("frame must be >= 0")
    if fps <= 0:
        raise ValueError("fps must be greater than zero")
    if drop_frame:
        nominal_fps = _drop_frame_rate(fps)
        frames_per_hour = nominal_fps * 60 * 60
        frames_per_24_hours = frames_per_hour * 24
        frames_per_10_minutes = nominal_fps * 60 * 10 - 18
        frames_per_minute = nominal_fps * 60 - 2

        frame = frame % frames_per_24_hours
        d = frame // frames_per_10_minutes
        m = frame % frames_per_10_minutes
        frame += 18 * d + 2 * max(0, (m - 2) // frames_per_minute)

        hours = frame // frames_per_hour
        frame %= frames_per_hour
        minutes = frame // (nominal_fps * 60)
        frame %= nominal_fps * 60
        seconds = frame // nominal_fps
        frames = frame % nominal_fps
        return TimecodeParts(hours, minutes, seconds, frames, drop_frame=True).normalized()

    fps_int = int(round(fps))
    hours = frame // (fps_int * 3600)
    frame %= fps_int * 3600
    minutes = frame // (fps_int * 60)
    frame %= fps_int * 60
    seconds = frame // fps_int
    frames = frame % fps_int
    return TimecodeParts(hours, minutes, seconds, frames, drop_frame=False).normalized()


def _drop_frame_rate(fps: float) -> int:
    rounded = int(round(fps))
    if rounded not in {30, 60}:
        raise ValueError("Drop-frame conversion currently supports 29.97/59.94-style rates only")
    return rounded
