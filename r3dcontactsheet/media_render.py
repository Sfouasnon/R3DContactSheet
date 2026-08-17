"""Dispatch still rendering across REDline and generic video providers."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .batch import GenericRenderSettings, JobPlanItem
from .media_providers import extract_red_thumbnail_jpeg
from .redline import RenderResult, render_frame
from .tool_resolver import resolve_ffmpeg

logger = logging.getLogger(__name__)


class MediaRenderError(RuntimeError):
    """Raised when a provider-specific still render cannot be completed."""


@dataclass(frozen=True)
class GenericRenderResult:
    command: list[str]
    output_path: Path
    output_size: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PlanRenderOutcome:
    index: int
    item: JobPlanItem
    result: RenderResult | GenericRenderResult | None
    error: Exception | None
    duration: float


def render_plan_item(
    item: JobPlanItem,
    *,
    redline_exe: Optional[str],
    min_output_bytes: int,
) -> RenderResult | GenericRenderResult:
    provider_name = item.clip_metadata.provider_name or item.clip.provider_kind
    if provider_name == "red":
        if item.render_job is None:
            raise MediaRenderError("RED render job was not prepared.")
        return render_frame(
            item.render_job,
            redline_exe=redline_exe,
            min_output_bytes=min_output_bytes,
        )
    if provider_name == "thumbnail":
        output_path = extract_red_thumbnail_jpeg(item.clip.source_path, item.output_file)
        return GenericRenderResult(
            command=["extract-red-thumbnail", str(item.clip.source_path), str(output_path)],
            output_path=output_path,
            output_size=output_path.stat().st_size,
            stdout="",
            stderr="",
        )
    return _render_generic_frame(item, min_output_bytes=min_output_bytes)


def render_plan_items_parallel(
    plan: list[JobPlanItem],
    *,
    redline_exe: Optional[str],
    min_output_bytes: int,
    max_workers: Optional[int] = None,
    progress_callback: Optional[Callable[[PlanRenderOutcome, int, int], None]] = None,
) -> list[PlanRenderOutcome]:
    if not plan:
        return []
    total = len(plan)
    worker_count = max_workers or min(total, max(os.cpu_count() or 1, 1))
    worker_count = max(1, min(worker_count, total))
    outcomes: list[PlanRenderOutcome] = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="r3dcontactsheet-render") as executor:
        future_map = {
            executor.submit(
                _render_indexed_item,
                index,
                item,
                redline_exe=redline_exe,
                min_output_bytes=min_output_bytes,
            ): index
            for index, item in enumerate(plan, start=1)
        }
        completed = 0
        for future in as_completed(future_map):
            outcome = future.result()
            outcomes.append(outcome)
            completed += 1
            if progress_callback is not None:
                progress_callback(outcome, completed, total)
    outcomes.sort(key=lambda item: item.index)
    return outcomes


def build_replay_command(item: JobPlanItem, *, redline_exe: Optional[str]) -> list[str]:
    provider_name = item.clip_metadata.provider_name or item.clip.provider_kind
    if provider_name == "red":
        if item.render_job is None:
            raise MediaRenderError("RED render job was not prepared.")
        from .redline import build_redline_command

        return build_redline_command(redline_exe or "", item.render_job)

    if provider_name == "thumbnail":
        return ["echo", f"Extract RED thumbnail preview: {item.clip.source_path}"]

    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return [
            "echo",
            (
                f"Generic render unavailable for {item.clip.source_path.name}: "
                "ffmpeg could not be located. Check /opt/homebrew/bin or /usr/local/bin."
            ),
        ]
    return _build_generic_ffmpeg_command(ffmpeg, item)


def _render_generic_frame(item: JobPlanItem, *, min_output_bytes: int) -> GenericRenderResult:
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise MediaRenderError(
            "Generic video rendering requires ffmpeg, but it could not be located. "
            "Searched: PATH (shutil.which), /opt/homebrew/bin, /usr/local/bin. "
            "Install ffmpeg or configure an explicit path in application preferences."
        )

    cmd = _build_generic_ffmpeg_command(ffmpeg, item)

    logger.debug("Generic render command: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        stderr_text = result.stderr.strip() or result.stdout.strip() or "ffmpeg render failed (no output)."
        logger.error(
            "ffmpeg (%s) exited with code %d.\nstderr: %s",
            ffmpeg,
            result.returncode,
            stderr_text,
        )
        raise MediaRenderError(
            f"ffmpeg render failed (exit {result.returncode}): {stderr_text}"
        )

    if not item.output_file.exists():
        raise MediaRenderError(
            f"ffmpeg render did not produce expected output file: {item.output_file}"
        )

    size = item.output_file.stat().st_size
    if size < min_output_bytes:
        raise MediaRenderError(
            f"ffmpeg render output is too small ({size} bytes < {min_output_bytes} minimum) "
            f"for {item.output_file}."
        )

    return GenericRenderResult(
        command=cmd,
        output_path=item.output_file,
        output_size=size,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _render_indexed_item(
    index: int,
    item: JobPlanItem,
    *,
    redline_exe: Optional[str],
    min_output_bytes: int,
) -> PlanRenderOutcome:
    started = time.time()
    try:
        item.output_file.parent.mkdir(parents=True, exist_ok=True)
        item.output_file.unlink(missing_ok=True)
        result = render_plan_item(
            item,
            redline_exe=redline_exe,
            min_output_bytes=min_output_bytes,
        )
        return PlanRenderOutcome(
            index=index,
            item=item,
            result=result,
            error=None,
            duration=time.time() - started,
        )
    except Exception as exc:  # pragma: no cover - exercised via caller tests
        return PlanRenderOutcome(
            index=index,
            item=item,
            result=None,
            error=exc,
            duration=time.time() - started,
        )


def _build_generic_ffmpeg_command(ffmpeg: str, item: JobPlanItem) -> list[str]:
    frame_index = max(0, item.frame_resolution.frame_index)
    settings = item.generic_settings
    seek_seconds, residual_frame = _seek_plan(frame_index, item.clip_metadata.clip_fps, settings)
    select_expr = f"select=eq(n\\,{residual_frame})"
    filters = [select_expr, _colorspace_filter(item, settings)]
    command = [ffmpeg, "-y"]
    if seek_seconds is not None:
        command.extend(["-ss", f"{seek_seconds:.9f}"])
    command.extend([
        "-i",
        str(item.clip.source_path),
        "-vf",
        ",".join(filters),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(item.output_file),
    ])
    return command


def _seek_plan(
    frame_index: int,
    fps: Optional[float],
    settings: GenericRenderSettings,
) -> tuple[Optional[float], int]:
    """Seek close to long-clip targets while retaining a short exact decode tail."""

    if not fps or fps <= 0 or frame_index < settings.seek_threshold_frames:
        return None, frame_index
    preroll_frames = max(1, int(round(fps * 2.0)))
    seek_frame = max(0, frame_index - preroll_frames)
    return seek_frame / fps, frame_index - seek_frame


def _colorspace_filter(item: JobPlanItem, settings: GenericRenderSettings) -> str:
    """Normalize decoded stills to an sRGB/Rec.709 display-referred JPEG."""

    raw = item.clip_metadata.raw_fields
    declared = any(
        str(raw.get(key, "")).strip().lower() not in {"", "unknown", "unspecified", "reserved", "2"}
        for key in ("color_space", "color_transfer", "color_primaries")
    )
    output = "space=bt709:primaries=bt709:trc=srgb:range=pc:format=yuv444p:dither=fsb"
    if declared:
        return f"colorspace={output}"
    fallback = {
        "rec709": "iall=bt709",
        "rec2020": "iall=bt2020",
        "p3_d65": "ispace=bt709:iprimaries=smpte432:itrc=srgb",
        "srgb": "ispace=bt709:iprimaries=bt709:itrc=srgb",
    }.get(settings.source_colorspace_fallback, "iall=bt709")
    return f"colorspace={output}:{fallback}"
