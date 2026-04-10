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


def _render_indexed_item(
    index: int,
    item: JobPlanItem,
    *,
    redline_exe: Optional[str],
    min_output_bytes: int,
) -> PlanRenderOutcome:
    started = time.time()
    try:
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
