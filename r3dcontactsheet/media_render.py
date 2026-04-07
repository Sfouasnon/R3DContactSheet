"""Dispatch still rendering across REDline and generic video providers."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .batch import JobPlanItem
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
    return _render_generic_frame(item, min_output_bytes=min_output_bytes)


def build_replay_command(item: JobPlanItem, *, redline_exe: Optional[str]) -> list[str]:
    provider_name = item.clip_metadata.provider_name or item.clip.provider_kind
    if provider_name == "red":
        if item.render_job is None:
            raise MediaRenderError("RED render job was not prepared.")
        from .redline import build_redline_command

        return build_redline_command(redline_exe or "", item.render_job)

    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return [
            "echo",
            (
                f"Generic render unavailable for {item.clip.source_path.name}: "
                "ffmpeg could not be located. Check /opt/homebrew/bin or /usr/local/bin."
            ),
        ]
    select_expr = f"select=eq(n\\,{max(0, item.frame_resolution.frame_index)})"
    return [
        ffmpeg,
        "-y",
        "-i",
        str(item.clip.source_path),
        "-vf",
        select_expr,
        "-frames:v",
        "1",
        str(item.output_file),
    ]


def _render_generic_frame(item: JobPlanItem, *, min_output_bytes: int) -> GenericRenderResult:
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise MediaRenderError(
            "Generic video rendering requires ffmpeg, but it could not be located. "
            "Searched: PATH (shutil.which), /opt/homebrew/bin, /usr/local/bin. "
            "Install ffmpeg or configure an explicit path in application preferences."
        )

    frame_index = max(0, item.frame_resolution.frame_index)
    select_expr = f"select=eq(n\\,{frame_index})"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(item.clip.source_path),
        "-vf",
        select_expr,
        "-frames:v",
        "1",
        str(item.output_file),
    ]

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
