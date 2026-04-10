"""REDline metadata extraction helpers."""

from __future__ import annotations

import csv
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Iterable, Optional


class RedlineMetadataError(RuntimeError):
    """Raised when REDline metadata extraction cannot produce trustworthy timing data."""


@dataclass(frozen=True)
class RedlineCommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    payload_text: str


@dataclass(frozen=True)
class ClipMetadata:
    clip_path: Path
    clip_fps: Optional[float]
    timecode_base_fps: Optional[float]
    start_timecode: Optional[str]
    total_frames: Optional[int]
    resolution: Optional[str]
    timecode_source: str
    drop_frame: bool
    sync_basis: str
    metadata_ok: bool
    raw_fields: dict[str, str]
    end_timecode: Optional[str] = None
    manufacturer: str = ""
    format_type: str = ""
    provider_name: str = "red"
    timecode_supported: bool = False
    sync_eligible: bool = False
    render_supported: bool = False
    metadata_error: str = ""


def load_clip_metadata(clip_path: Path, redline_exe: str, timeout: float = 20.0) -> ClipMetadata:
    clip_path = clip_path.expanduser().resolve()
    executable = _validate_redline_executable(redline_exe)
    fields = _load_metadata_fields(clip_path, executable, timeout)
    clip_fps = _extract_rate(fields, _FPS_KEYS)
    timecode_base_fps = _extract_rate(fields, _TIMECODE_BASE_KEYS) or clip_fps
    resolution = _extract_resolution(fields)
    perframe_rows, perframe_note = _load_perframe_csv(clip_path, executable, timeout)
    if perframe_rows:
        start_value = _first_tc_from_perframe(perframe_rows)
        end_value = _last_tc_from_perframe(perframe_rows)
        total_frames = len(perframe_rows)
        source = "per-frame CSV"
        print(f"Parsed {len(perframe_rows)} per-frame rows for {clip_path}", flush=True)
    else:
        start_value = None
        end_value = None
        total_frames = None
        source = "untrusted"
        print(f"Per-frame metadata unavailable for {clip_path}: {perframe_note}", flush=True)

    drop_frame = bool(start_value and ";" in start_value)
    metadata_ok = (
        clip_fps is not None
        and timecode_base_fps is not None
        and start_value is not None
        and end_value is not None
        and total_frames is not None
        and total_frames > 0
    )
    basis = "REDline printMeta"
    if metadata_ok:
        basis += f" ({source}, clip fps {clip_fps:g}, TC base {timecode_base_fps:g}, TC in {start_value}, TC out {end_value}, frames {total_frames})"
    else:
        basis += f" (incomplete metadata: {perframe_note})"
    return ClipMetadata(
        clip_path=clip_path,
        clip_fps=clip_fps,
        timecode_base_fps=timecode_base_fps,
        start_timecode=start_value,
        total_frames=total_frames,
        end_timecode=end_value,
        resolution=resolution,
        timecode_source=source,
        drop_frame=drop_frame,
        sync_basis=basis,
        metadata_ok=metadata_ok,
        raw_fields=fields,
        manufacturer="RED",
        format_type="R3D",
        provider_name="red",
        timecode_supported=bool(start_value and end_value),
        sync_eligible=metadata_ok,
        render_supported=True,
        metadata_error="" if metadata_ok else perframe_note,
    )


def _validate_redline_executable(redline_exe: str | Path) -> Path:
    path = Path(redline_exe).expanduser().resolve()
    if not os.path.isfile(path):
        raise RedlineMetadataError(
            f"REDline path is not a file: {path}. Choose the REDline binary inside REDCINE-X PRO, not the .app bundle."
        )
    if not os.access(path, os.X_OK):
        raise RedlineMetadataError(f"REDline path is not executable: {path}")
    return path


def _load_perframe_csv(clip_path: Path, redline_exe: Path, timeout: float) -> tuple[list[dict[str, str]], str]:
    try:
        result = _run_redline_printmeta(clip_path, redline_exe, "5", timeout)
    except RedlineMetadataError as exc:
        return [], str(exc)

    payload = result.payload_text
    if not payload:
        return [], _format_redline_failure(
            clip_path,
            result,
            "REDline returned no per-frame CSV output.",
        )
    rows = _parse_perframe_csv(payload)

    if not rows:
        return [], _format_redline_failure(
            clip_path,
            result,
            "REDline per-frame CSV contained no usable frame rows.",
        )
    return rows, ""


def _parse_perframe_csv(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("[")]
    if not lines:
        return []
    header_index = None
    header = None
    for index, line in enumerate(lines):
        try:
            row = next(csv.reader(StringIO(line)))
        except Exception:
            continue
        normalized = [_normalize_key(cell) for cell in row if cell.strip()]
        if "frameno" in normalized and any("timecode" in key for key in normalized):
            header_index = index
            header = [cell.strip() for cell in row]
            break
    if header_index is None or header is None:
        return []

    body = "\n".join(lines[header_index:])
    reader = csv.DictReader(StringIO(body))
    rows: list[dict[str, str]] = []
    for row in reader:
        if not row:
            continue
        cleaned = {str(key).strip(): (value.strip() if isinstance(value, str) else "") for key, value in row.items() if key}
        if not cleaned:
            continue
        tc_key = _find_tc_column(cleaned)
        if tc_key is None or not _extract_tc_value(cleaned.get(tc_key, "")):
            continue
        rows.append(cleaned)
    return rows


def _find_tc_column(row: dict[str, str]) -> Optional[str]:
    for key in row:
        if "timecode" in key.lower():
            return key
    for key in row:
        normalized = _normalize_key(key)
        if normalized in _EDGE_TIMECODE_KEYS or normalized in _TIMECODE_KEYS or normalized in _RECORD_TIMECODE_KEYS:
            return key
    for key, value in row.items():
        if _extract_tc_value(value):
            return key
    return None


def _first_tc_from_perframe(rows: list[dict[str, str]]) -> Optional[str]:
    if not rows:
        return None
    tc_key = _find_tc_column(rows[0])
    if tc_key is None:
        return None
    return _extract_tc_value(rows[0].get(tc_key, ""))


def _last_tc_from_perframe(rows: list[dict[str, str]]) -> Optional[str]:
    if not rows:
        return None
    tc_key = _find_tc_column(rows[-1])
    if tc_key is None:
        return None
    return _extract_tc_value(rows[-1].get(tc_key, ""))


def _load_metadata_fields(clip_path: Path, redline_exe: Path, timeout: float) -> dict[str, str]:
    merged: dict[str, str] = {}
    for mode in ("3", "1"):
        try:
            result = _run_redline_printmeta(clip_path, redline_exe, mode, timeout, silent=True)
        except RedlineMetadataError as exc:
            print(f"REDline metadata mode {mode} failed for {clip_path}: {exc}", flush=True)
            continue
        payload = result.payload_text
        parsed = parse_redline_printmeta(payload)
        for key, value in parsed.items():
            if key not in merged or not merged[key]:
                merged[key] = value
    return merged


def _run_redline_printmeta(
    clip_path: Path,
    redline_exe: Path,
    mode: str,
    timeout: float,
    *,
    silent: bool = False,
) -> RedlineCommandResult:
    command = [str(redline_exe), "--i", str(clip_path), "--printMeta", str(mode)]
    if silent:
        command.append("--silent")
    print(f"REDline metadata command: {shlex.join(command)}", flush=True)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except Exception as exc:
        raise RedlineMetadataError(f"Failed to execute REDline metadata command for {clip_path}: {exc}") from exc

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    payload = stdout or stderr
    print(f"REDline metadata return code ({mode}) for {clip_path.name}: {result.returncode}", flush=True)
    if stdout and (mode == "5" or result.returncode != 0):
        print(f"REDline metadata stdout ({mode}) for {clip_path.name}:\n{stdout}", flush=True)
    if stderr:
        print(f"REDline metadata stderr ({mode}) for {clip_path.name}:\n{stderr}", flush=True)
    return RedlineCommandResult(
        command=tuple(command),
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
        payload_text=payload,
    )


def _format_redline_failure(clip_path: Path, result: RedlineCommandResult, reason: str) -> str:
    details = [reason, f"Clip: {clip_path}", f"Command: {shlex.join(result.command)}", f"Return code: {result.returncode}"]
    if result.stderr:
        details.append(f"stderr: {result.stderr}")
    elif result.stdout:
        details.append(f"stdout: {result.stdout}")
    return " | ".join(details)


def parse_redline_printmeta(text: str) -> dict[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    csv_lines = [line for line in lines if "," in line and not line.startswith("[")]
    if len(csv_lines) >= 2:
        header, row = _pick_csv_pair(csv_lines)
        if header and row:
            return _csv_to_fields(header, row)
    return _kv_to_fields(lines)


def _pick_csv_pair(lines: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    parsed = []
    for line in lines:
        try:
            row = next(csv.reader(StringIO(line)))
        except Exception:
            continue
        parsed.append((line, row))
    for index in range(len(parsed) - 1):
        header_line, header_row = parsed[index]
        data_line, data_row = parsed[index + 1]
        if len(header_row) == len(data_row) and len(header_row) > 2:
            return header_line, data_line
    return None, None


def _csv_to_fields(header_line: str, row_line: str) -> dict[str, str]:
    header = next(csv.reader(StringIO(header_line)))
    row = next(csv.reader(StringIO(row_line)))
    return {h.strip(): r.strip() for h, r in zip(header, row) if h.strip()}


def _kv_to_fields(lines: Iterable[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        clean = re.sub(r"^\[[^\]]+\]\s*", "", line)
        if ":" in clean:
            key, value = clean.split(":", 1)
        elif "=" in clean:
            key, value = clean.split("=", 1)
        else:
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            fields[key] = value
    return fields


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _extract_rate(fields: dict[str, str], candidates: set[str]) -> Optional[float]:
    for key, value in fields.items():
        normalized = _normalize_key(key)
        if normalized in candidates or any(token in normalized for token in candidates):
            parsed = _parse_rate(value)
            if parsed is not None:
                return parsed
    return None


def _parse_rate(value: str) -> Optional[float]:
    cleaned = value.strip().lower().replace("fps", "").strip()
    if "/" in cleaned:
        left, right = cleaned.split("/", 1)
        try:
            denominator = float(right)
            if denominator:
                return float(left) / denominator
        except ValueError:
            return None
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _extract_timecode(fields: dict[str, str]) -> tuple[Optional[str], str]:
    for source_name, candidate_keys in (
        ("edge timecode", _EDGE_TIMECODE_KEYS),
        ("timecode", _TIMECODE_KEYS),
        ("record timecode", _RECORD_TIMECODE_KEYS),
    ):
        for key, value in fields.items():
            normalized = _normalize_key(key)
            if normalized in candidate_keys or any(token in normalized for token in candidate_keys):
                tc = _extract_tc_value(value)
                if tc:
                    return tc, source_name
    return None, "unknown"


def _extract_resolution(fields: dict[str, str]) -> Optional[str]:
    for key, value in fields.items():
        normalized = _normalize_key(key)
        if normalized in _RESOLUTION_KEYS or any(token in normalized for token in _RESOLUTION_KEYS):
            direct = _extract_resolution_value(value)
            if direct:
                return direct
    width = _extract_int_field(fields, _WIDTH_KEYS)
    height = _extract_int_field(fields, _HEIGHT_KEYS)
    if width and height:
        return f"{width}x{height}"
    return None


def _extract_tc_value(value: str) -> Optional[str]:
    match = re.search(r"\d{2}:\d{2}:\d{2}[:;]\d{2}", value)
    return match.group(0) if match else None


def _extract_resolution_value(value: str) -> Optional[str]:
    match = re.search(r"(\d{3,5})\s*[xX]\s*(\d{3,5})", value)
    if not match:
        return None
    return f"{match.group(1)}x{match.group(2)}"


def _extract_int_field(fields: dict[str, str], candidates: set[str]) -> Optional[int]:
    for key, value in fields.items():
        normalized = _normalize_key(key)
        if normalized in candidates or any(token in normalized for token in candidates):
            match = re.search(r"\d{3,5}", value)
            if match:
                return int(match.group(0))
    return None


_FPS_KEYS = {
    "clipframerate",
    "framerate",
    "recordframerate",
    "projectframerate",
    "fps",
}
_TIMECODE_BASE_KEYS = {
    "timecodebase",
    "timecodeframerate",
    "projecttimecodebase",
    "projectframerate",
    "basetimecodeframerate",
    "timecodefps",
}
_EDGE_TIMECODE_KEYS = {"edgetimecode", "edgecode", "edge"}
_TIMECODE_KEYS = {
    "timecode",
    "starttimecode",
    "abscliptimecode",
    "absolutetimecode",
    "todtimecode",
    "externaltimecode",
    "mastertimecode",
}
_RECORD_TIMECODE_KEYS = {"recordtimecode", "cliptimecode", "recordtc", "clipstarttimecode"}
_RESOLUTION_KEYS = {"resolution", "framesize", "videosize", "raster"}
_WIDTH_KEYS = {"width", "framewidth", "projectwidth"}
_HEIGHT_KEYS = {"height", "frameheight", "projectheight"}
_TOTAL_FRAMES_KEYS = {"framecount", "totalframes", "clipframes", "durationframes", "recordedframes"}
