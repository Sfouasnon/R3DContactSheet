"""Provider-backed media metadata helpers for RED and generic video clips."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image

from .metadata import ClipMetadata, load_clip_metadata
from .timecode import frame_to_timecode
from .tool_resolver import resolve_ffmpeg, resolve_ffprobe, resolve_ltcdump, resolve_mediainfo

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
RED_THUMBNAIL_EXTENSIONS = {".rtn"}
RED_THUMBNAIL_MAGIC = b"REDTHUMBNAIL"

LTC_AUDIO_ENV = "R3DCONTACTSHEET_ENABLE_LTC_AUDIO"


@dataclass(frozen=True)
class GenericMetadataProbe:
    clip_fps: Optional[float]
    total_frames: Optional[int]
    resolution: Optional[str]
    start_timecode: Optional[str]
    end_timecode: Optional[str]
    timecode_source: str
    sync_basis: str
    raw_fields: dict[str, str]
    metadata_ok: bool
    timecode_supported: bool
    sync_eligible: bool
    metadata_error: str
    metadata_provenance: str
    metadata_confidence: str
    manufacturer: str
    format_type: str


def provider_kind_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".r3d" or path.name.lower().endswith(".rdc"):
        return "red"
    if suffix == ".braw":
        return "braw"
    if suffix in RED_THUMBNAIL_EXTENSIONS:
        return "thumbnail"
    if suffix in GENERIC_VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def supports_generic_video_metadata() -> bool:
    return resolve_ffprobe() is not None or resolve_mediainfo() is not None


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
    if provider_kind == "thumbnail":
        return _load_red_thumbnail_metadata(clip_path)
    return _load_generic_video_metadata(clip_path, provider_kind=provider_kind, timeout=timeout)


def extract_red_thumbnail_jpeg(clip_path: Path, destination: Path) -> Path:
    """Extract the embedded JPEG from a RED .rtn preview sidecar."""

    payload = _red_thumbnail_jpeg_bytes(clip_path)
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def _load_red_thumbnail_metadata(clip_path: Path) -> ClipMetadata:
    clip_path = clip_path.expanduser().resolve()
    payload = _red_thumbnail_jpeg_bytes(clip_path)
    with Image.open(BytesIO(payload)) as image:
        resolution = f"{image.width}x{image.height}"
    note = "RED thumbnail preview only; source timecode and frame synchronization are unavailable."
    return ClipMetadata(
        clip_path=clip_path,
        clip_fps=None,
        timecode_base_fps=None,
        start_timecode=None,
        total_frames=1,
        resolution=resolution,
        timecode_source="thumbnail sidecar",
        drop_frame=False,
        sync_basis=note,
        metadata_ok=False,
        raw_fields={"preview_kind": "REDTHUMBNAIL"},
        manufacturer="RED",
        format_type="RED Thumbnail",
        provider_name="thumbnail",
        timecode_supported=False,
        sync_eligible=False,
        render_supported=True,
        metadata_error=note,
        metadata_provenance="red_thumbnail",
        metadata_confidence="preview_only",
    )


def _red_thumbnail_jpeg_bytes(clip_path: Path) -> bytes:
    clip_path = clip_path.expanduser().resolve()
    payload = clip_path.read_bytes()
    if not payload.startswith(RED_THUMBNAIL_MAGIC):
        raise ValueError(f"Not a RED thumbnail sidecar: {clip_path}")
    start = payload.find(b"\xff\xd8")
    end = payload.rfind(b"\xff\xd9")
    if start < 0 or end < start:
        raise ValueError(f"RED thumbnail contains no complete JPEG preview: {clip_path}")
    return payload[start:end + 2]


def _load_generic_video_metadata(
    clip_path: Path,
    *,
    provider_kind: str,
    timeout: float,
) -> ClipMetadata:
    clip_path = clip_path.expanduser().resolve()
    manufacturer = _manufacturer_for_extension(clip_path.suffix.lower(), provider_kind)
    format_type = clip_path.suffix.lstrip(".").upper() or "Video"

    ffprobe_probe = _probe_with_ffprobe(clip_path, provider_kind=provider_kind, timeout=timeout)
    mediainfo_probe = _empty_probe(
        manufacturer,
        format_type,
        provenance="mediainfo",
        confidence="unresolved",
        error="mediainfo: not attempted",
        basis="MediaInfo fallback was not needed.",
    )
    if not ffprobe_probe.metadata_ok:
        mediainfo_probe = _probe_with_mediainfo(clip_path, provider_kind=provider_kind, timeout=timeout)

    ltc_probe = _empty_probe(
        manufacturer,
        format_type,
        provenance="ltc_audio",
        confidence="unresolved",
        error="ltc audio decode not attempted",
        basis="LTC audio fallback was not needed.",
    )
    ffprobe_or_mediainfo_has_timecode = bool(ffprobe_probe.start_timecode or mediainfo_probe.start_timecode)
    if not ffprobe_or_mediainfo_has_timecode:
        ltc_probe = _probe_with_ltc_audio(
            clip_path,
            provider_kind=provider_kind,
            timeout=timeout,
            preferred_fps=ffprobe_probe.clip_fps or mediainfo_probe.clip_fps,
            preferred_total_frames=ffprobe_probe.total_frames or mediainfo_probe.total_frames,
            resolution=ffprobe_probe.resolution or mediainfo_probe.resolution,
        )

    merged = _merge_generic_probes(
        clip_path,
        provider_kind=provider_kind,
        manufacturer=manufacturer,
        format_type=format_type,
        probes=[ffprobe_probe, mediainfo_probe, ltc_probe],
    )
    return ClipMetadata(
        clip_path=clip_path,
        clip_fps=merged.clip_fps,
        timecode_base_fps=merged.clip_fps,
        start_timecode=merged.start_timecode,
        total_frames=merged.total_frames,
        resolution=merged.resolution,
        timecode_source=merged.timecode_source,
        drop_frame=bool(merged.start_timecode and ";" in merged.start_timecode),
        sync_basis=merged.sync_basis,
        metadata_ok=merged.metadata_ok,
        raw_fields=merged.raw_fields,
        end_timecode=merged.end_timecode,
        manufacturer=merged.manufacturer,
        format_type=merged.format_type,
        provider_name=provider_kind,
        timecode_supported=merged.timecode_supported,
        sync_eligible=merged.sync_eligible,
        render_supported=supports_generic_video_rendering(),
        metadata_error=merged.metadata_error,
        metadata_provenance=merged.metadata_provenance,
        metadata_confidence=merged.metadata_confidence,
    )


def _probe_with_ffprobe(clip_path: Path, *, provider_kind: str, timeout: float) -> GenericMetadataProbe:
    manufacturer = _manufacturer_for_extension(clip_path.suffix.lower(), provider_kind)
    format_type = clip_path.suffix.lstrip(".").upper() or "Video"
    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="unresolved",
            confidence="unresolved",
            error="ffprobe unavailable",
            basis="ffprobe is not installed or could not be resolved.",
        )

    try:
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
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="ffprobe",
            confidence="unresolved",
            error=f"ffprobe failed: {exc}",
            basis=f"ffprobe could not inspect {clip_path.name}: {exc}",
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
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="ffprobe",
            confidence="unresolved",
            error="ffprobe: no probe data",
            basis=f"ffprobe ({ffprobe}) returned no structured output. stderr: {result.stderr.strip() or '(none)'}",
        )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="ffprobe",
            confidence="unresolved",
            error="ffprobe: invalid JSON",
            basis=f"ffprobe ({ffprobe}) returned unparseable JSON.",
        )

    video_stream = _first_video_stream(data)
    raw_fields = {}
    if isinstance(video_stream, dict):
        raw_fields.update({str(key): str(value) for key, value in video_stream.items() if value not in (None, "")})
    if isinstance(data.get("format"), dict):
        raw_fields.update({f"format.{key}": str(value) for key, value in data["format"].items() if value not in (None, "")})

    fps = _parse_rate_text(_stream_value(video_stream, "avg_frame_rate") or _stream_value(video_stream, "r_frame_rate"))
    total_frames = _parse_int_text(_stream_value(video_stream, "nb_frames")) or _parse_int_text(raw_fields.get("format.nb_frames"))
    if not total_frames and fps:
        duration = _duration_seconds(video_stream, data.get("format") if isinstance(data.get("format"), dict) else {})
        if duration and duration > 0:
            total_frames = max(1, int(round(duration * fps)))
            raw_fields["derived.total_frames"] = str(total_frames)
            raw_fields["derived.from_duration_seconds"] = f"{duration:.9f}"
    width = _parse_int_text(_stream_value(video_stream, "width"))
    height = _parse_int_text(_stream_value(video_stream, "height"))
    resolution = f"{width}x{height}" if width and height else None
    start_timecode = _extract_ffprobe_timecode(data)
    end_timecode = None
    if start_timecode and fps and total_frames and total_frames > 0:
        start_frame = _timecode_to_abs_frame(start_timecode, fps, ";" in start_timecode)
        end_timecode = frame_to_timecode(start_frame + total_frames - 1, fps, drop_frame=";" in start_timecode)

    metadata_ok = bool(start_timecode and end_timecode and fps and total_frames and total_frames > 0)
    timecode_supported = bool(start_timecode)
    return GenericMetadataProbe(
        clip_fps=fps,
        total_frames=total_frames,
        resolution=resolution,
        start_timecode=start_timecode,
        end_timecode=end_timecode,
        timecode_source="ffprobe stream/format tags" if timecode_supported else "ffprobe: no timecode",
        sync_basis=(
            f"ffprobe metadata ({ffprobe})"
            if metadata_ok
            else f"ffprobe metadata ({ffprobe}) did not provide a complete timing window."
        ),
        raw_fields=raw_fields,
        metadata_ok=metadata_ok,
        timecode_supported=timecode_supported,
        sync_eligible=metadata_ok,
        metadata_error="" if metadata_ok else ("ffprobe: no usable first-frame timecode" if not start_timecode else "ffprobe: incomplete timing metadata"),
        metadata_provenance="ffprobe",
        metadata_confidence="high" if metadata_ok else "unresolved",
        manufacturer=manufacturer,
        format_type=format_type,
    )


def _probe_with_mediainfo(clip_path: Path, *, provider_kind: str, timeout: float) -> GenericMetadataProbe:
    manufacturer = _manufacturer_for_extension(clip_path.suffix.lower(), provider_kind)
    format_type = clip_path.suffix.lstrip(".").upper() or "Video"
    mediainfo = resolve_mediainfo()
    if not mediainfo:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="mediainfo",
            confidence="unresolved",
            error="mediainfo: not installed",
            basis="MediaInfo CLI could not be resolved on this system.",
        )

    try:
        result = subprocess.run(
            [mediainfo, "--Output=JSON", str(clip_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="mediainfo",
            confidence="unresolved",
            error=f"mediainfo failed: {exc}",
            basis=f"MediaInfo could not inspect {clip_path.name}: {exc}",
        )
    if result.returncode != 0:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="mediainfo",
            confidence="unresolved",
            error=f"mediainfo: exit {result.returncode}",
            basis=f"MediaInfo ({mediainfo}) failed: {result.stderr.strip() or result.stdout.strip() or '(no output)'}",
        )

    payload = (result.stdout or "").strip()
    if not payload:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="mediainfo",
            confidence="unresolved",
            error="mediainfo: no output",
            basis=f"MediaInfo ({mediainfo}) returned no structured output.",
        )
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="mediainfo",
            confidence="unresolved",
            error="mediainfo: invalid JSON",
            basis=f"MediaInfo ({mediainfo}) returned invalid JSON.",
        )

    tracks = (((data.get("media") or {}).get("track")) if isinstance(data, dict) else None) or []
    raw_fields: dict[str, str] = {}
    for track in tracks:
        if isinstance(track, dict):
            track_type = str(track.get("@type", "track")).lower()
            for key, value in track.items():
                if key.startswith("@") or value in (None, ""):
                    continue
                raw_fields[f"{track_type}.{key}"] = str(value)

    general_track = _find_track(tracks, "general")
    video_track = _find_track(tracks, "video")
    manufacturer = str(general_track.get("Encoded_Application_CompanyName") or general_track.get("Recorded_Application_CompanyName") or manufacturer)
    fps = _parse_rate_text(_get_first_value(video_track, "FrameRate", "FrameRate_Num")) or _parse_rate_text(_get_first_value(general_track, "FrameRate"))
    total_frames = _parse_int_text(_get_first_value(video_track, "FrameCount")) or _parse_int_text(_get_first_value(general_track, "FrameCount"))
    width = _parse_int_text(_get_first_value(video_track, "Width"))
    height = _parse_int_text(_get_first_value(video_track, "Height"))
    resolution = f"{width}x{height}" if width and height else None
    start_timecode = _extract_track_timecode(general_track, prefix="First") or _extract_track_timecode(video_track, prefix="First")
    end_timecode = _extract_track_timecode(general_track, prefix="Last") or _extract_track_timecode(video_track, prefix="Last")
    if start_timecode and not end_timecode and fps and total_frames and total_frames > 0:
        start_frame = _timecode_to_abs_frame(start_timecode, fps, ";" in start_timecode)
        end_timecode = frame_to_timecode(start_frame + total_frames - 1, fps, drop_frame=";" in start_timecode)

    metadata_ok = bool(start_timecode and end_timecode and fps and total_frames and total_frames > 0)
    timecode_supported = bool(start_timecode)
    return GenericMetadataProbe(
        clip_fps=fps,
        total_frames=total_frames,
        resolution=resolution,
        start_timecode=start_timecode,
        end_timecode=end_timecode,
        timecode_source="mediainfo container metadata" if timecode_supported else "mediainfo: no timecode",
        sync_basis=(
            f"MediaInfo CLI ({mediainfo})"
            if metadata_ok
            else f"MediaInfo CLI ({mediainfo}) did not provide a complete timing window."
        ),
        raw_fields=raw_fields,
        metadata_ok=metadata_ok,
        timecode_supported=timecode_supported,
        sync_eligible=metadata_ok,
        metadata_error="" if metadata_ok else ("mediainfo: no usable first-frame timecode" if not start_timecode else "mediainfo: incomplete timing metadata"),
        metadata_provenance="mediainfo",
        metadata_confidence="medium" if metadata_ok else "unresolved",
        manufacturer=manufacturer,
        format_type=format_type,
    )


def _probe_with_ltc_audio(
    clip_path: Path,
    *,
    provider_kind: str,
    timeout: float,
    preferred_fps: Optional[float],
    preferred_total_frames: Optional[int],
    resolution: Optional[str],
) -> GenericMetadataProbe:
    manufacturer = _manufacturer_for_extension(clip_path.suffix.lower(), provider_kind)
    format_type = clip_path.suffix.lstrip(".").upper() or "Video"
    if os.environ.get(LTC_AUDIO_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="ltc_audio",
            confidence="unresolved",
            error="ltc audio decode disabled",
            basis=f"LTC audio fallback is disabled. Set {LTC_AUDIO_ENV}=1 to enable it.",
        )
    ffmpeg = resolve_ffmpeg()
    ltcdump = resolve_ltcdump()
    if not ffmpeg or not ltcdump:
        missing = []
        if not ffmpeg:
            missing.append("ffmpeg")
        if not ltcdump:
            missing.append("ltcdump")
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="ltc_audio",
            confidence="unresolved",
            error=f"ltc audio decode unavailable ({', '.join(missing)})",
            basis=f"LTC audio fallback requires {' and '.join(missing)}.",
        )

    try:
        with tempfile.NamedTemporaryFile(prefix="r3dcontactsheet_ltc_", suffix=".wav", delete=True) as temp_wav:
            extract = subprocess.run(
                [ffmpeg, "-y", "-i", str(clip_path), "-map", "0:a:0", "-ac", "1", "-ar", "48000", temp_wav.name],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=os.environ.copy(),
            )
            if extract.returncode != 0:
                return _empty_probe(
                    manufacturer,
                    format_type,
                    provenance="ltc_audio",
                    confidence="unresolved",
                    error="ltc audio decode unavailable",
                    basis=f"ffmpeg audio extract failed: {extract.stderr.strip() or extract.stdout.strip() or '(no output)'}",
                )
            decode = subprocess.run(
                [ltcdump, temp_wav.name],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=os.environ.copy(),
            )
    except Exception as exc:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="ltc_audio",
            confidence="unresolved",
            error="ltc audio decode unavailable",
            basis=f"LTC audio fallback failed: {exc}",
        )

    if decode.returncode != 0:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="ltc_audio",
            confidence="unresolved",
            error=f"ltc audio decode failed (exit {decode.returncode})",
            basis=f"ltcdump failed: {decode.stderr.strip() or decode.stdout.strip() or '(no output)'}",
        )

    decoded_timecode = _extract_ltc_timecode(decode.stdout)
    if not decoded_timecode:
        return _empty_probe(
            manufacturer,
            format_type,
            provenance="ltc_audio",
            confidence="unresolved",
            error="ltc audio decode unavailable",
            basis="LTC audio decode produced no parseable timecode.",
        )

    end_timecode = None
    if preferred_fps and preferred_total_frames and preferred_total_frames > 0:
        start_frame = _timecode_to_abs_frame(decoded_timecode, preferred_fps, ";" in decoded_timecode)
        end_timecode = frame_to_timecode(start_frame + preferred_total_frames - 1, preferred_fps, drop_frame=";" in decoded_timecode)
    metadata_ok = bool(decoded_timecode and end_timecode and preferred_fps and preferred_total_frames)
    return GenericMetadataProbe(
        clip_fps=preferred_fps,
        total_frames=preferred_total_frames,
        resolution=resolution,
        start_timecode=decoded_timecode,
        end_timecode=end_timecode,
        timecode_source="ltc audio decode",
        sync_basis=f"LTC audio decode via ffmpeg + ltcdump ({ltcdump})",
        raw_fields={"ltc_audio.stdout": decode.stdout.strip()},
        metadata_ok=metadata_ok,
        timecode_supported=bool(decoded_timecode),
        sync_eligible=metadata_ok,
        metadata_error="" if metadata_ok else "ltc audio decode: partial timing only",
        metadata_provenance="ltc_audio",
        metadata_confidence="low" if decoded_timecode else "unresolved",
        manufacturer=manufacturer,
        format_type=format_type,
    )


def _merge_generic_probes(
    clip_path: Path,
    *,
    provider_kind: str,
    manufacturer: str,
    format_type: str,
    probes: list[GenericMetadataProbe],
) -> GenericMetadataProbe:
    ffprobe_probe, mediainfo_probe, ltc_probe = probes
    preferred_time_probe = next(
        (
            probe
            for probe in probes
            if probe.start_timecode and probe.metadata_provenance in {"ffprobe", "mediainfo"}
        ),
        None,
    )
    if preferred_time_probe is None and ltc_probe.start_timecode:
        preferred_time_probe = ltc_probe

    fps = ffprobe_probe.clip_fps or mediainfo_probe.clip_fps or ltc_probe.clip_fps
    total_frames = ffprobe_probe.total_frames or mediainfo_probe.total_frames or ltc_probe.total_frames
    resolution_value = ffprobe_probe.resolution or mediainfo_probe.resolution or ltc_probe.resolution
    start_timecode = preferred_time_probe.start_timecode if preferred_time_probe else None
    end_timecode = preferred_time_probe.end_timecode if preferred_time_probe else None
    if start_timecode and not end_timecode and fps and total_frames and total_frames > 0:
        start_frame = _timecode_to_abs_frame(start_timecode, fps, ";" in start_timecode)
        end_timecode = frame_to_timecode(start_frame + total_frames - 1, fps, drop_frame=";" in start_timecode)

    metadata_ok = bool(start_timecode and end_timecode and fps and total_frames and total_frames > 0)
    timecode_supported = bool(start_timecode and end_timecode)
    raw_fields: dict[str, str] = {}
    for probe in probes:
        raw_fields.update(probe.raw_fields)
    if preferred_time_probe is not None:
        provenance = preferred_time_probe.metadata_provenance
        confidence = preferred_time_probe.metadata_confidence if metadata_ok else "unresolved"
        timecode_source = preferred_time_probe.timecode_source
        sync_basis = preferred_time_probe.sync_basis
        metadata_error = "" if metadata_ok else preferred_time_probe.metadata_error
    else:
        provenance = "unresolved"
        confidence = "unresolved"
        timecode_source = "unsupported"
        sync_basis = "No trustworthy timecode metadata was recovered from ffprobe, mediainfo, or LTC audio decode."
        metadata_error = _best_error_message(probes)
    return GenericMetadataProbe(
        clip_fps=fps,
        total_frames=total_frames,
        resolution=resolution_value,
        start_timecode=start_timecode,
        end_timecode=end_timecode,
        timecode_source=timecode_source,
        sync_basis=sync_basis,
        raw_fields=raw_fields,
        metadata_ok=metadata_ok,
        timecode_supported=timecode_supported,
        sync_eligible=metadata_ok,
        metadata_error=metadata_error,
        metadata_provenance=provenance,
        metadata_confidence=confidence,
        manufacturer=manufacturer,
        format_type=format_type,
    )


def _empty_probe(
    manufacturer: str,
    format_type: str,
    *,
    provenance: str,
    confidence: str,
    error: str,
    basis: str,
) -> GenericMetadataProbe:
    return GenericMetadataProbe(
        clip_fps=None,
        total_frames=None,
        resolution=None,
        start_timecode=None,
        end_timecode=None,
        timecode_source="unsupported",
        sync_basis=basis,
        raw_fields={},
        metadata_ok=False,
        timecode_supported=False,
        sync_eligible=False,
        metadata_error=error,
        metadata_provenance=provenance,
        metadata_confidence=confidence,
        manufacturer=manufacturer,
        format_type=format_type,
    )


def _best_error_message(probes: list[GenericMetadataProbe]) -> str:
    messages = [
        probe.metadata_error
        for probe in probes
        if probe.metadata_error and "not attempted" not in probe.metadata_error
    ]
    return " | ".join(messages) if messages else "manual assignment required"


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
    digits = re.sub(r"[^\d]", "", str(value))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _extract_ffprobe_timecode(data: dict) -> Optional[str]:
    for container in _iter_tag_containers(data):
        for key, value in container.items():
            if key.lower() == "timecode" and value:
                return str(value).strip()
    return None


def _parse_float_text(value: object) -> Optional[float]:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _duration_seconds(video_stream: dict, format_data: dict) -> Optional[float]:
    direct = _parse_float_text(video_stream.get("duration")) or _parse_float_text(format_data.get("duration"))
    if direct:
        return direct
    duration_ts = _parse_float_text(video_stream.get("duration_ts"))
    time_base = _parse_rate_text(str(video_stream.get("time_base") or ""))
    if duration_ts and time_base:
        return duration_ts * time_base
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


def _find_track(tracks: list[dict], track_type: str) -> dict:
    for track in tracks:
        if isinstance(track, dict) and str(track.get("@type", "")).lower() == track_type:
            return track
    return {}


def _get_first_value(track: dict, *keys: str) -> Optional[str]:
    for key in keys:
        value = track.get(key) if isinstance(track, dict) else None
        if value not in (None, ""):
            return str(value)
    return None


def _extract_track_timecode(track: dict, *, prefix: str) -> Optional[str]:
    if not isinstance(track, dict):
        return None
    candidates = (
        f"TimeCode_{prefix}Frame",
        f"TimeCode_{prefix}Frame_String",
        f"TimeCode{prefix}Frame",
        f"TimeCode_{prefix}",
        f"TimeCode_{prefix}TimeCode",
        f"Source_{prefix}_Frame_TimeCode",
        f"{prefix}TimeCode",
    )
    for key in candidates:
        value = track.get(key)
        if value:
            tc = _extract_timecode_string(str(value))
            if tc:
                return tc
    for key, value in track.items():
        if prefix.lower() in key.lower() and "timecode" in key.lower():
            tc = _extract_timecode_string(str(value))
            if tc:
                return tc
    return None


def _extract_ltc_timecode(text: str) -> Optional[str]:
    return _extract_timecode_string(text)


def _extract_timecode_string(value: str) -> Optional[str]:
    match = re.search(r"\b\d{2}:\d{2}:\d{2}[:;]\d{2}\b", value)
    if match:
        return match.group(0)
    return None


def _timecode_to_abs_frame(tc: str, fps: float, drop_frame: bool) -> int:
    from .timecode import timecode_to_frame

    return timecode_to_frame(tc, fps, drop_frame=drop_frame)
