"""Provider-backed media metadata helpers for RED and generic video clips."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Optional
import logging

from .metadata import ClipMetadata, load_clip_metadata
from .timecode import frame_to_timecode
from .tool_resolver import resolve_ffmpeg, resolve_ffprobe

logger = logging.getLogger(__name__)

GENERIC_VIDEO_EXTENSIONS = {
    ".mov",
    ".mp4",
    ".m4v",
    ".mxf",
    ".avi",
    ".mkv",
    ".webm",
    ".braw",
}


def provider_kind_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".r3d" or path.name.lower().endswith(".rdc"):
        return "red"
    if suffix == ".braw":
        return "braw"
    if suffix in GENERIC_VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def supports_generic_video_metadata() -> bool:
    return resolve_ffprobe() is not None


def supports_generic_video_rendering() -> bool:
    return resolve_ffmpeg() is not None


def load_provider_metadata(
    clip_path: Path,
    *,
    provider_kind: str,
    redline_exe: Optional[str] = None,
    timeout: float = 20.0,
) -> ClipMetadata:
    if provider_kind == "red":
        if not redline_exe:
            raise ValueError("RED clips require a REDline executable for metadata extraction.")
        return load_clip_metadata(clip_path, redline_exe, timeout=timeout)
    return _load_generic_video_metadata(clip_path, provider_kind=provider_kind, timeout=timeout)


def _load_generic_video_metadata(
    clip_path: Path,
    *,
    provider_kind: str,
    timeout: float,
) -> ClipMetadata:
    clip_path = clip_path.expanduser().resolve()
    manufacturer = _manufacturer_for_extension(clip_path.suffix.lower(), provider_kind)
    format_type = clip_path.suffix.lstrip(".").upper() or "Video"

    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return ClipMetadata(
            clip_path=clip_path,
            clip_fps=None,
            timecode_base_fps=None,
            start_timecode=None,
            total_frames=None,
            resolution=None,
            timecode_source="ffprobe unavailable",
            drop_frame=False,
            sync_basis="Generic video provider (ffprobe not found on this system).",
            metadata_ok=False,
            raw_fields={},
            end_timecode=None,
            manufacturer=manufacturer,
            format_type=format_type,
            provider_name=provider_kind,
            timecode_supported=False,
            sync_eligible=False,
            render_supported=supports_generic_video_rendering(),
        )

    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(clip_path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode != 0:
        logger.warning(
            "ffprobe (%s) exited with code %d for %s.\nstderr: %s",
            ffprobe,
            result.returncode,
            clip_path,
            result.stderr.strip(),
        )

    payload = (result.stdout or "").strip()
    if not payload:
        return ClipMetadata(
            clip_path=clip_path,
            clip_fps=None,
            timecode_base_fps=None,
            start_timecode=None,
            total_frames=None,
            resolution=None,
            timecode_source="ffprobe empty",
            drop_frame=False,
            sync_basis=(
                f"ffprobe ({ffprobe}) returned no probe data. "
                f"stderr: {result.stderr.strip() or '(none)'}"
            ),
            metadata_ok=False,
            raw_fields={},
            end_timecode=None,
            manufacturer=manufacturer,
            format_type=format_type,
            provider_name=provider_kind,
            timecode_supported=False,
            sync_eligible=False,
            render_supported=supports_generic_video_rendering(),
        )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}

    video_stream = _first_video_stream(data)
    raw_fields = {}
    if isinstance(video_stream, dict):
        raw_fields.update({str(key): str(value) for key, value in video_stream.items() if value not in (None, "")})
    if isinstance(data.get("format"), dict):
        raw_fields.update(
            {
                f"format.{key}": str(value)
                for key, value in data["format"].items()
                if value not in (None, "")
            }
        )

    fps = _parse_rate_text(_stream_value(video_stream, "avg_frame_rate") or _stream_value(video_stream, "r_frame_rate"))
    total_frames = _parse_int_text(_stream_value(video_stream, "nb_frames"))
    width = _parse_int_text(_stream_value(video_stream, "width"))
    height = _parse_int_text(_stream_value(video_stream, "height"))
    resolution = f"{width}x{height}" if width and height else None
    start_timecode = _extract_ffprobe_timecode(data)
    drop_frame = bool(start_timecode and ";" in start_timecode)

    end_timecode = None
    if start_timecode and fps and total_frames and total_frames > 0:
        start_frame = _timecode_to_abs_frame(start_timecode, fps, drop_frame)
        end_timecode = frame_to_timecode(start_frame + total_frames - 1, fps, drop_frame=drop_frame)

    metadata_ok = bool(start_timecode and end_timecode and fps and total_frames and total_frames > 0)
    timecode_supported = bool(start_timecode and end_timecode)
    sync_basis = (
        f"Generic video provider ({provider_kind}, ffprobe @ {ffprobe})"
        if metadata_ok
        else f"Generic video provider ({provider_kind}) could not confirm trustworthy LTC/timecode metadata."
    )
    return ClipMetadata(
        clip_path=clip_path,
        clip_fps=fps,
        timecode_base_fps=fps,
        start_timecode=start_timecode,
        total_frames=total_frames,
        resolution=resolution,
        timecode_source="ffprobe stream/format tags" if timecode_supported else "unsupported",
        drop_frame=drop_frame,
        sync_basis=sync_basis,
        metadata_ok=metadata_ok,
        raw_fields=raw_fields,
        end_timecode=end_timecode,
        manufacturer=manufacturer,
        format_type=format_type,
        provider_name=provider_kind,
        timecode_supported=timecode_supported,
        sync_eligible=metadata_ok,
        render_supported=supports_generic_video_rendering(),
    )


def _manufacturer_for_extension(suffix: str, provider_kind: str) -> str:
    if provider_kind == "braw":
        return "Blackmagic"
    mapping = {
        ".mov": "Generic Video",
        ".mp4": "Generic Video",
        ".m4v": "Generic Video",
        ".mxf": "Generic Video",
        ".avi": "Generic Video",
        ".mkv": "Generic Video",
        ".webm": "Generic Video",
    }
    return mapping.get(suffix, "Generic Video")


def _first_video_stream(data: dict) -> dict:
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        return {}
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            return stream
    return {}


def _stream_value(stream: dict, key: str) -> Optional[str]:
    if not isinstance(stream, dict):
        return None
    value = stream.get(key)
    return str(value) if value not in (None, "") else None


def _parse_rate_text(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    cleaned = value.strip()
    if "/" in cleaned:
        left, right = cleaned.split("/", 1)
        try:
            numerator = float(left)
            denominator = float(right)
        except ValueError:
            return None
        if denominator == 0:
            return None
        return numerator / denominator
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int_text(value: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _extract_ffprobe_timecode(data: dict) -> Optional[str]:
    for container in _iter_tag_containers(data):
        for key, value in container.items():
            if key.lower() == "timecode" and value:
                return str(value).strip()
    return None


def _iter_tag_containers(data: dict):
    streams = data.get("streams") if isinstance(data, dict) else None
    if isinstance(streams, list):
        for stream in streams:
            tags = stream.get("tags") if isinstance(stream, dict) else None
            if isinstance(tags, dict):
                yield tags
    fmt = data.get("format") if isinstance(data, dict) else None
    if isinstance(fmt, dict):
        tags = fmt.get("tags")
        if isinstance(tags, dict):
            yield tags


def _timecode_to_abs_frame(tc: str, fps: float, drop_frame: bool) -> int:
    from .timecode import timecode_to_frame

    return timecode_to_frame(tc, fps, drop_frame=drop_frame)
