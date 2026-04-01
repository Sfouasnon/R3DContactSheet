"""Clip discovery and batch job creation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Literal, Optional

from .frame_index import FrameResolution, FrameTargetRequest, resolve_frame_target
from .redline import RenderJob, RenderSettings


GroupMode = Literal["flat", "parent_folder", "reel_prefix"]


@dataclass(frozen=True)
class ClipEntry:
    source_path: Path
    clip_name: str
    reel_name: str
    group_name: str


@dataclass(frozen=True)
class BatchOptions:
    output_dir: Path
    frame_request: FrameTargetRequest
    settings: RenderSettings
    group_mode: GroupMode = "flat"
    alphabetize: bool = True


@dataclass(frozen=True)
class JobPlanItem:
    clip: ClipEntry
    frame_resolution: FrameResolution
    output_file: Path
    render_job: RenderJob


def discover_r3d_clips(path: Path, group_mode: GroupMode = "flat", alphabetize: bool = True) -> List[ClipEntry]:
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source path does not exist: {src}")

    candidates: Iterable[Path]
    if src.is_file():
        candidates = [src]
    else:
        candidates = src.rglob("*")

    clips: List[ClipEntry] = []
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() == ".r3d":
            clip_name = candidate.stem
            reel_name = _derive_reel_name(clip_name)
            group_name = _derive_group_name(candidate, reel_name, group_mode)
            clips.append(
                ClipEntry(
                    source_path=candidate.resolve(),
                    clip_name=clip_name,
                    reel_name=reel_name,
                    group_name=group_name,
                )
            )

    if not clips:
        raise FileNotFoundError(f"No .R3D clips found in {src}")

    if alphabetize:
        clips.sort(key=lambda item: (_natural_sort_key(item.group_name), _natural_sort_key(item.clip_name)))
    return clips


def build_job_plan(clips: Iterable[ClipEntry], options: BatchOptions) -> List[JobPlanItem]:
    output_dir = options.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    plan: List[JobPlanItem] = []

    for clip in clips:
        frame_resolution = resolve_frame_target(options.frame_request, clip.source_path)
        group_dir = output_dir / clip.group_name if options.group_mode != "flat" else output_dir
        group_dir.mkdir(parents=True, exist_ok=True)
        output_name = _build_output_name(clip, frame_resolution.frame_index)
        output_file = group_dir / output_name
        render_job = RenderJob(
            input_file=clip.source_path,
            frame_index=frame_resolution.frame_index,
            output_file=output_file,
            settings=options.settings,
        )
        plan.append(
            JobPlanItem(
                clip=clip,
                frame_resolution=frame_resolution,
                output_file=output_file,
                render_job=render_job,
            )
        )
    return plan


def _build_output_name(clip: ClipEntry, frame_index: int) -> str:
    return f"{clip.clip_name}_frame{frame_index:06d}.jpg"


def _derive_group_name(path: Path, reel_name: str, group_mode: GroupMode) -> str:
    if group_mode == "parent_folder":
        return path.parent.name or reel_name
    if group_mode == "reel_prefix":
        return reel_name
    return "renders"


def _derive_reel_name(clip_name: str) -> str:
    parts = clip_name.split("_")
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[1]}"
    return clip_name


def _natural_sort_key(value: str) -> tuple:
    parts = re.split(r"(\d+)", value)
    normalized: List[object] = []
    for part in parts:
        if not part:
            continue
        normalized.append(int(part) if part.isdigit() else part.lower())
    return tuple(normalized)
