"""Clip discovery and batch job creation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Literal, Optional

from .frame_index import (
    FrameResolution,
    FrameTargetRequest,
    MatchSelectionState,
    MatchingMoment,
    OverlapSubset,
    analyze_overlap_subsets,
    resolve_clip_frame,
    resolve_clip_frame_for_selection,
    resolve_matching_moment,
)
from .metadata import ClipMetadata, load_clip_metadata
from .redline import RenderJob, RenderSettings


GroupMode = Literal["flat", "parent_folder", "reel_prefix", "custom"]


@dataclass(frozen=True)
class ClipEntry:
    source_path: Path
    clip_name: str
    reel_name: str
    group_name: str
    source_kind: Literal["r3d", "rdc"]
    container_path: Optional[Path] = None
    package_path: Optional[Path] = None
    segment_count: int = 1
    segment_index: int = 1


@dataclass(frozen=True)
class BatchOptions:
    output_dir: Path
    frame_request: FrameTargetRequest
    settings: RenderSettings
    group_mode: GroupMode = "flat"
    alphabetize: bool = True
    custom_group_name: Optional[str] = None
    redline_exe: Optional[str] = None


@dataclass
class PreviewContext:
    clips: List[ClipEntry]
    options: BatchOptions
    metadata_by_clip: dict[Path, ClipMetadata]
    overlap_subsets: list[OverlapSubset]
    selection: MatchSelectionState


@dataclass(frozen=True)
class JobPlanItem:
    clip: ClipEntry
    clip_metadata: ClipMetadata
    frame_resolution: FrameResolution
    matching_moment: MatchingMoment
    output_group: str
    output_file: Path
    render_job: RenderJob


def discover_r3d_clips(path: Path, group_mode: GroupMode = "flat", alphabetize: bool = True) -> List[ClipEntry]:
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source path does not exist: {src}")

    if src.is_file():
        clip = _resolve_clip_path(src, group_mode)
        if clip is None:
            raise FileNotFoundError(f"Selected file is not a usable .R3D clip: {src}")
        clips = [clip]
    elif _is_rdc_dir(src):
        clips = [_resolve_rdc_package(src, group_mode)]
    else:
        clips = _scan_source_tree(src, group_mode)

    if not clips:
        raise FileNotFoundError(
            f"No usable RED clips were found in {src}. Select a .R3D, an .RDC package, or a folder containing them."
        )

    if alphabetize:
        clips.sort(key=lambda item: (_natural_sort_key(item.group_name), _natural_sort_key(item.clip_name)))
    return clips


def build_job_plan(clips: Iterable[ClipEntry], options: BatchOptions) -> List[JobPlanItem]:
    context = build_preview_context(clips, options)
    return build_job_plan_from_context(context)


def build_preview_context(clips: Iterable[ClipEntry], options: BatchOptions) -> PreviewContext:
    output_dir = options.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    clip_list = list(clips)
    if not options.redline_exe:
        raise ValueError("A REDline executable is required for metadata-driven sync resolution.")
    metadata_by_clip = {
        clip.source_path: load_clip_metadata(clip.source_path, options.redline_exe)
        for clip in clip_list
    }
    overlap_subsets, selection = analyze_overlap_subsets(list(metadata_by_clip.values()), options.frame_request)
    return PreviewContext(
        clips=clip_list,
        options=options,
        metadata_by_clip=metadata_by_clip,
        overlap_subsets=overlap_subsets,
        selection=selection,
    )


def build_job_plan_from_context(
    context: PreviewContext,
    selection: Optional[MatchSelectionState] = None,
) -> List[JobPlanItem]:
    output_dir = context.options.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    plan: List[JobPlanItem] = []
    selection = selection or context.selection
    active_subset = next((subset for subset in context.overlap_subsets if subset.subset_id == selection.active_subset_id), None)
    matching_moment = _matching_moment_from_selection(context, active_subset, selection)

    for index, clip in enumerate(context.clips, start=1):
        clip_metadata = context.metadata_by_clip[clip.source_path]
        frame_resolution = resolve_clip_frame_for_selection(clip_metadata, active_subset, selection)
        output_group = _resolve_output_group(clip, context.options)
        output_name = _build_output_name(clip, index)
        output_file = frames_dir / output_name
        render_job = RenderJob(
            input_file=clip.source_path,
            frame_index=frame_resolution.frame_index,
            output_file=output_file,
            settings=context.options.settings,
        )
        plan.append(
            JobPlanItem(
                clip=clip,
                clip_metadata=clip_metadata,
                frame_resolution=frame_resolution,
                matching_moment=matching_moment,
                output_group=output_group,
                output_file=output_file,
                render_job=render_job,
            )
        )
    return plan


def _matching_moment_from_selection(
    context: PreviewContext,
    active_subset: Optional[OverlapSubset],
    selection: MatchSelectionState,
) -> MatchingMoment:
    metadata_items = list(context.metadata_by_clip.values())
    total_clips = len(metadata_items)
    if active_subset is None or selection.selected_abs_frame is None:
        return resolve_matching_moment(metadata_items, context.options.frame_request)
    matched_subset = [
        item for item in metadata_items
        if str(item.clip_path.resolve()) in active_subset.clip_paths
    ]
    unmatched_subset = [
        item for item in metadata_items
        if str(item.clip_path.resolve()) not in active_subset.clip_paths
    ]
    reference_clip = matched_subset[0] if matched_subset else metadata_items[0]
    source_timecode = None
    if selection.selected_abs_frame is not None:
        source_timecode = resolve_clip_frame_for_selection(reference_clip, active_subset, selection).match_timecode
    sync_mode = "full" if len(active_subset.clip_paths) == total_clips else "partial" if len(active_subset.clip_paths) >= 2 else "none"
    note = (
        "Selected shared frame is contained by every clip in the active overlap subset."
        if sync_mode != "none"
        else "No shared overlap subset was available. Per-clip metadata timecodes remain authoritative."
    )
    ambiguity = "" if sync_mode == "full" else "Not every clip is part of the active overlap subset."
    return MatchingMoment(
        absolute_frame=selection.selected_abs_frame,
        source_timecode=source_timecode,
        source="overlap_selection",
        sync_status="verified" if sync_mode == "full" else "ambiguous",
        note=note,
        ambiguity_reason=ambiguity,
        sync_mode=sync_mode,
        matched_subset_size=len(active_subset.clip_paths),
        total_clips=total_clips,
        matched_clip_paths=active_subset.clip_paths,
        unmatched_clip_paths=tuple(str(item.clip_path.resolve()) for item in unmatched_subset),
        overlap_group_paths=active_subset.clip_paths,
    )


def _resolve_output_group(clip: ClipEntry, options: BatchOptions) -> str:
    if options.group_mode == "custom":
        custom = (options.custom_group_name or "").strip()
        return custom or "renders"
    if options.group_mode == "flat":
        return "renders"
    return clip.group_name


def describe_source_selection(path: Path) -> str:
    src = Path(path).expanduser().resolve()
    if not src.exists():
        return f"Missing source: {src}"
    if src.is_file():
        if src.suffix.lower() == ".r3d":
            return f"Selected clip: {src.name}"
        return f"Selected file is not a RED clip: {src.name}"
    if _is_rdc_dir(src):
        segments = _list_r3d_segments(src)
        if not segments:
            return f"{src.name} contains no .R3D files."
        if len(segments) == 1:
            return f"Selected RDC package: {src.name}"
        return f"Selected RDC package: {src.name} ({len(segments)} segments, using {segments[0].name})"
    return f"Selected folder: {src}"


def _scan_source_tree(src: Path, group_mode: GroupMode) -> List[ClipEntry]:
    clips: List[ClipEntry] = []
    for root, dirnames, filenames in os.walk(src):
        root_path = Path(root)

        # Treat RDC packages as single clip containers and do not recurse inside them.
        package_dirs = [name for name in dirnames if name.lower().endswith(".rdc")]
        for package_name in sorted(package_dirs, key=_natural_sort_key):
            clips.append(_resolve_rdc_package(root_path / package_name, group_mode))
        dirnames[:] = [name for name in dirnames if name.lower() not in {name.lower() for name in package_dirs}]

        # Standalone R3D files not already represented by an RDC package.
        for filename in sorted(filenames, key=_natural_sort_key):
            candidate = root_path / filename
            if candidate.suffix.lower() != ".r3d":
                continue
            if _find_rdc_ancestor(candidate, src) is not None:
                continue
            clip = _resolve_clip_path(candidate, group_mode)
            if clip is not None:
                clips.append(clip)
    return clips


def _resolve_clip_path(path: Path, group_mode: GroupMode) -> Optional[ClipEntry]:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_file() and candidate.suffix.lower() == ".r3d":
        package_path = _find_rdc_ancestor(candidate)
        segment_count = 1
        segment_index = 1
        source_kind: Literal["r3d", "rdc"] = "r3d"

        if package_path is not None:
            segments = _list_r3d_segments(package_path)
            segment_count = len(segments)
            segment_index = max(1, segments.index(candidate) + 1) if candidate in segments else 1
            source_kind = "rdc"

        clip_name = candidate.stem
        reel_name = _derive_reel_name(clip_name)
        group_name = _derive_group_name(candidate, package_path or candidate.parent, reel_name, group_mode)
        return ClipEntry(
            source_path=candidate,
            clip_name=clip_name,
            reel_name=reel_name,
            group_name=group_name,
            source_kind=source_kind,
            container_path=candidate.parent,
            package_path=package_path,
            segment_count=segment_count,
            segment_index=segment_index,
        )
    if candidate.is_dir() and _is_rdc_dir(candidate):
        return _resolve_rdc_package(candidate, group_mode)
    return None


def _resolve_rdc_package(path: Path, group_mode: GroupMode) -> ClipEntry:
    package_path = Path(path).expanduser().resolve()
    segments = _list_r3d_segments(package_path)
    if not segments:
        raise FileNotFoundError(f"{package_path} contains no .R3D files.")

    primary = _choose_primary_segment(segments)
    clip_name = primary.stem
    reel_name = _derive_reel_name(clip_name)
    group_name = _derive_group_name(primary, package_path, reel_name, group_mode)
    return ClipEntry(
        source_path=primary,
        clip_name=clip_name,
        reel_name=reel_name,
        group_name=group_name,
        source_kind="rdc",
        container_path=package_path,
        package_path=package_path,
        segment_count=len(segments),
        segment_index=max(1, segments.index(primary) + 1),
    )


def _list_r3d_segments(package_path: Path) -> List[Path]:
    return sorted(
        [path.resolve() for path in package_path.iterdir() if path.is_file() and path.suffix.lower() == ".r3d"],
        key=lambda item: _natural_sort_key(item.name),
    )


def _choose_primary_segment(segments: List[Path]) -> Path:
    for candidate in segments:
        if candidate.stem.endswith("_001"):
            return candidate
    return segments[0]


def _build_output_name(clip: ClipEntry, index: int) -> str:
    parts = clip.clip_name.split("_")
    if len(parts) >= 2:
        return f"{index:03d}_{parts[0]}_{parts[1]}.jpg"
    return f"{index:03d}_{clip.clip_name}.jpg"


def _derive_group_name(source_path: Path, container_path: Path, reel_name: str, group_mode: GroupMode) -> str:
    if group_mode == "parent_folder":
        if _is_rdc_dir(container_path):
            return container_path.stem or reel_name
        return container_path.name or reel_name
    if group_mode == "reel_prefix":
        return reel_name
    return "renders"


def _derive_reel_name(clip_name: str) -> str:
    parts = clip_name.split("_")
    if len(parts) >= 2:
        return _logical_group_value(parts[0])
    return _logical_group_value(clip_name)


def _logical_group_value(value: str) -> str:
    match = re.search(r"([A-Za-z]?)(\d+)", value)
    if match:
        return match.group(2)
    return value


def _find_rdc_ancestor(path: Path, stop_at: Optional[Path] = None) -> Optional[Path]:
    stop_at_resolved = stop_at.resolve() if stop_at is not None else None
    for parent in path.parents:
        if stop_at_resolved is not None and parent == stop_at_resolved.parent:
            break
        if _is_rdc_dir(parent):
            return parent
    return None


def _is_rdc_dir(path: Path) -> bool:
    return path.is_dir() and path.suffix.lower() == ".rdc"


def _natural_sort_key(value: str) -> tuple:
    parts = re.split(r"(\d+)", value)
    normalized: List[object] = []
    for part in parts:
        if not part:
            continue
        normalized.append(int(part) if part.isdigit() else part.lower())
    return tuple(normalized)
