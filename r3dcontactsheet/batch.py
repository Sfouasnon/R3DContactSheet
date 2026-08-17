"""Clip discovery and batch job creation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Literal, Optional

from .frame_index import (
    FrameResolution,
    FrameTargetRequest,
    MatchSelectionState,
    MatchingMoment,
    OverlapSubset,
    analyze_overlap_subsets,
    resolve_clip_frame_for_selection,
    resolve_matching_moment,
)
from .metadata import ClipMetadata
from .media_providers import (
    GENERIC_VIDEO_EXTENSIONS,
    RED_THUMBNAIL_EXTENSIONS,
    load_provider_metadata,
    provider_kind_for_path,
)
from .redline import RenderJob, RenderSettings
from .timecode import timecode_to_frame


GroupMode = Literal["flat", "parent_folder", "reel_prefix", "custom"]
SyncMode = Literal["sync_on", "sync_off"]
MetadataCacheKey = tuple[str, str, int, int, str]
BatchAssignmentState = Literal["auto_assigned", "needs_assignment", "excluded_by_operator"]


@dataclass(frozen=True)
class ClipEntry:
    source_path: Path
    clip_name: str
    reel_name: str
    group_name: str
    source_kind: Literal["r3d", "rdc", "video", "braw", "thumbnail"]
    provider_kind: str = "red"
    manufacturer: str = ""
    format_type: str = ""
    container_path: Optional[Path] = None
    package_path: Optional[Path] = None
    segment_count: int = 1
    segment_index: int = 1


MetadataProgressCallback = Callable[[int, int, ClipEntry, ClipMetadata], None]


@dataclass
class ClipOrganization:
    group_name: str
    subgroup_name: str = ""
    camera_label: str = ""
    clip_family: str = ""
    manufacturer: str = ""
    format_type: str = ""


@dataclass(frozen=True)
class GenericRenderSettings:
    """Provider-neutral controls for decoded video stills."""

    source_colorspace_fallback: str = "rec709"
    max_workers: int = 4
    seek_threshold_frames: int = 240


@dataclass(frozen=True)
class BatchOptions:
    output_dir: Path
    frame_request: FrameTargetRequest
    settings: RenderSettings
    group_mode: GroupMode = "flat"
    alphabetize: bool = True
    custom_group_name: Optional[str] = None
    redline_exe: Optional[str] = None
    sync_mode: SyncMode = "sync_on"
    generic_settings: GenericRenderSettings = field(default_factory=GenericRenderSettings)


@dataclass
class PreviewContext:
    clips: List[ClipEntry]
    options: BatchOptions
    metadata_by_clip: dict[Path, ClipMetadata]
    overlap_subsets: list[OverlapSubset]
    selection: MatchSelectionState
    clip_fields: dict[Path, ClipOrganization]


@dataclass
class BatchAssignment:
    clip: ClipEntry
    reel_number: Optional[str]
    clip_number: Optional[str]
    assignment_state: BatchAssignmentState
    camera_label: str
    clip_family: str
    subgroup_name: str
    manufacturer: str
    format_type: str
    metadata_source: str = "filename"
    metadata_confidence: str = "high"
    assignment_reason: str = ""

    @property
    def batch_id(self) -> Optional[str]:
        if not self.reel_number or not self.clip_number:
            return None
        return f"{self.reel_number}_{self.clip_number}"

    @property
    def included(self) -> bool:
        return self.assignment_state != "excluded_by_operator"


@dataclass(frozen=True)
class BatchGroup:
    reel_number: str
    clip_number: str
    clips: tuple[ClipEntry, ...]
    camera_labels: tuple[str, ...]
    output_pdf_name: str

    @property
    def batch_id(self) -> str:
        return f"{self.reel_number}_{self.clip_number}"

    @property
    def source_clip_count(self) -> int:
        return len(self.clips)

    @property
    def camera_count(self) -> int:
        return len(self.camera_labels)


BatchScanProgressCallback = Callable[[str, int, int], None]
BatchLogCallback = Callable[[str], None]


@dataclass(frozen=True)
class BatchScanResult:
    assignments: tuple[BatchAssignment, ...]
    groups: tuple[BatchGroup, ...]


@dataclass(frozen=True)
class JobPlanItem:
    clip: ClipEntry
    clip_metadata: ClipMetadata
    clip_fields: ClipOrganization
    frame_resolution: FrameResolution
    matching_moment: MatchingMoment
    output_group: str
    output_file: Path
    render_job: Optional[RenderJob]
    generic_settings: GenericRenderSettings = field(default_factory=GenericRenderSettings)


def discover_r3d_clips(path: Path, group_mode: GroupMode = "flat", alphabetize: bool = True) -> List[ClipEntry]:
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source path does not exist: {src}")

    if src.is_file():
        clip = _resolve_clip_path(src, group_mode)
        if clip is None:
            raise FileNotFoundError(f"Selected file is not a supported clip or video source: {src}")
        clips = [clip]
    elif _is_rdc_dir(src):
        clips = [_resolve_rdc_package(src, group_mode)]
    else:
        clips = _scan_source_tree(src, group_mode)

    if not clips:
        raise FileNotFoundError(
            f"No usable clips were found in {src}. Select a media file, an .RDC package, or a folder containing supported media."
        )

    if alphabetize:
        clips.sort(key=lambda item: (_natural_sort_key(item.group_name), _natural_sort_key(item.clip_name)))
    return clips


discover_media_clips = discover_r3d_clips


def build_job_plan(clips: Iterable[ClipEntry], options: BatchOptions) -> List[JobPlanItem]:
    context = build_preview_context(clips, options)
    return build_job_plan_from_context(context)


def build_preview_context(
    clips: Iterable[ClipEntry],
    options: BatchOptions,
    *,
    metadata_cache: Optional[dict[MetadataCacheKey, ClipMetadata]] = None,
    progress_callback: Optional[MetadataProgressCallback] = None,
) -> PreviewContext:
    clip_list = _dedupe_clip_entries(list(clips))
    if any(clip.provider_kind == "red" for clip in clip_list) and not options.redline_exe:
        raise ValueError("RED clips require a REDline executable for metadata-driven sync resolution.")
    metadata_by_clip = {}
    clip_fields: dict[Path, ClipOrganization] = {}
    cache = metadata_cache if metadata_cache is not None else {}
    total = len(clip_list)
    for index, clip in enumerate(clip_list, start=1):
        cache_key = _metadata_cache_key(clip, options.redline_exe)
        metadata = cache.get(cache_key)
        if metadata is None:
            try:
                metadata = load_provider_metadata(
                    clip.source_path,
                    provider_kind=clip.provider_kind,
                    redline_exe=options.redline_exe,
                )
            except Exception as exc:
                metadata = _unavailable_metadata(clip, exc)
            cache[cache_key] = metadata
        metadata_by_clip[clip.source_path] = metadata
        clip_fields[clip.source_path] = ClipOrganization(
            group_name=clip.group_name,
            subgroup_name="",
            camera_label=_default_camera_label(clip.clip_name),
            clip_family=suggest_clip_family(clip),
            manufacturer=metadata.manufacturer or clip.manufacturer or _manufacturer_for_provider(clip.provider_kind),
            format_type=metadata.format_type or clip.format_type or clip.source_path.suffix.lstrip(".").upper(),
        )
        if progress_callback is not None:
            progress_callback(index, total, clip, metadata)
    eligible = [item for item in metadata_by_clip.values() if item.sync_eligible]
    overlap_subsets, selection = analyze_overlap_subsets(eligible, options.frame_request)
    return PreviewContext(
        clips=clip_list,
        options=options,
        metadata_by_clip=metadata_by_clip,
        overlap_subsets=overlap_subsets,
        selection=selection,
        clip_fields=clip_fields,
    )


def build_job_plan_from_context(
    context: PreviewContext,
    selection: Optional[MatchSelectionState] = None,
) -> List[JobPlanItem]:
    output_dir = context.options.output_dir.expanduser().resolve()
    frames_dir = output_dir / "frames"
    plan: List[JobPlanItem] = []
    selection = selection or context.selection
    active_subset = next((subset for subset in context.overlap_subsets if subset.subset_id == selection.active_subset_id), None)
    matching_moment = _matching_moment_from_selection(context, active_subset, selection)

    for index, clip in enumerate(context.clips, start=1):
        clip_metadata = context.metadata_by_clip[clip.source_path]
        clip_fields = context.clip_fields.get(clip.source_path) or ClipOrganization(group_name=clip.group_name)
        frame_resolution = resolve_clip_frame_for_selection(
            clip_metadata,
            active_subset,
            selection,
            sync_mode=context.options.sync_mode,
        )
        output_group = _resolve_output_group(clip, clip_fields, context.options)
        output_name = _build_output_name(clip, clip_fields, index)
        output_file = frames_dir / output_name
        render_job = None
        if clip.provider_kind == "red":
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
                clip_fields=clip_fields,
                frame_resolution=frame_resolution,
                matching_moment=matching_moment,
                output_group=output_group,
                output_file=output_file,
                render_job=render_job,
                generic_settings=context.options.generic_settings,
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


def _resolve_output_group(clip: ClipEntry, clip_fields: ClipOrganization, options: BatchOptions) -> str:
    if options.group_mode == "custom":
        custom = (options.custom_group_name or "").strip()
        return custom or "renders"
    if clip_fields.group_name.strip():
        return clip_fields.group_name.strip()
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
        if src.suffix.lower() in GENERIC_VIDEO_EXTENSIONS:
            return f"Selected media file: {src.name}"
        return f"Selected file is not a supported media clip: {src.name}"
    if _is_rdc_dir(src):
        segments = _list_r3d_segments(src)
        if not segments:
            return f"{src.name} contains no .R3D files."
        if len(segments) == 1:
            return f"Selected RDC package: {src.name}"
        return f"Selected RDC package: {src.name} ({len(segments)} segments, using {segments[0].name})"
    return f"Selected folder: {src}"


def build_batch_groups(
    source_root: Path,
    *,
    alphabetize: bool = True,
    redline_exe: Optional[str] = None,
    progress_callback: Optional[BatchScanProgressCallback] = None,
    log_callback: Optional[BatchLogCallback] = None,
) -> list[BatchGroup]:
    return list(
        build_batch_scan_result(
            source_root,
            alphabetize=alphabetize,
            redline_exe=redline_exe,
            progress_callback=progress_callback,
            log_callback=log_callback,
        ).groups
    )


def build_batch_scan_result(
    source_root: Path,
    *,
    alphabetize: bool = True,
    redline_exe: Optional[str] = None,
    progress_callback: Optional[BatchScanProgressCallback] = None,
    log_callback: Optional[BatchLogCallback] = None,
) -> BatchScanResult:
    clips = _discover_batch_clips(
        Path(source_root).expanduser().resolve(),
        alphabetize=alphabetize,
        progress_callback=progress_callback,
    )
    assignments: list[BatchAssignment] = []
    for clip in clips:
        batch_key = parse_batch_key(clip.clip_name)
        family = suggest_clip_family(clip)
        subgroup = suggest_subgroup(clip, family)
        if batch_key is None:
            if log_callback is not None:
                log_callback(f"Needs batch assignment: {clip.clip_name}")
            assignments.append(
                BatchAssignment(
                    clip=clip,
                    reel_number=None,
                    clip_number=None,
                    assignment_state="needs_assignment",
                    camera_label=_default_camera_label(clip.clip_name),
                    clip_family=family,
                    subgroup_name=subgroup,
                    manufacturer=clip.manufacturer or _manufacturer_for_provider(clip.provider_kind),
                    format_type=clip.format_type or clip.source_path.suffix.lstrip(".").upper() or "Unknown",
                    metadata_source="unresolved",
                    metadata_confidence="unresolved",
                    assignment_reason="Needs batch assignment: filename parsing did not produce a reel/clip key.",
                )
            )
            continue
        reel_number, clip_number = batch_key
        assignments.append(
            BatchAssignment(
                clip=clip,
                reel_number=reel_number,
                clip_number=clip_number,
                assignment_state="auto_assigned",
                camera_label=_default_camera_label(clip.clip_name),
                clip_family=family,
                subgroup_name=subgroup,
                manufacturer=clip.manufacturer or _manufacturer_for_provider(clip.provider_kind),
                format_type=clip.format_type or clip.source_path.suffix.lstrip(".").upper() or "Unknown",
                metadata_source="filename",
                metadata_confidence="high",
                assignment_reason=f"Auto-assigned from filename parse into Reel {reel_number} / Clip {clip_number}.",
            )
        )
    _auto_assign_mixed_media_batches(assignments, redline_exe=redline_exe, log_callback=log_callback)
    groups = regroup_batch_groups(assignments, progress_callback=progress_callback)
    return BatchScanResult(assignments=tuple(assignments), groups=tuple(groups))


def regroup_batch_groups(
    assignments: Iterable[BatchAssignment],
    *,
    progress_callback: Optional[BatchScanProgressCallback] = None,
) -> list[BatchGroup]:
    grouped: dict[tuple[str, str], list[BatchAssignment]] = {}
    active_assignments = [
        assignment for assignment in assignments
        if assignment.included and assignment.reel_number and assignment.clip_number
    ]
    for assignment in active_assignments:
        grouped.setdefault((assignment.reel_number, assignment.clip_number), []).append(assignment)
    batch_groups: list[BatchGroup] = []
    grouped_items = sorted(grouped.items())
    total_groups = len(grouped_items)
    for index, ((reel_number, clip_number), batch_assignments) in enumerate(grouped_items, start=1):
        ordered = sorted(batch_assignments, key=lambda item: _natural_sort_key(item.clip.clip_name))
        camera_labels = tuple(item.camera_label for item in ordered)
        batch_groups.append(
            BatchGroup(
                reel_number=reel_number,
                clip_number=clip_number,
                clips=tuple(item.clip for item in ordered),
                camera_labels=camera_labels,
                output_pdf_name=build_batch_pdf_name(reel_number, clip_number),
            )
        )
        if progress_callback is not None:
            progress_callback("build", index, total_groups)
    return batch_groups


@dataclass(frozen=True)
class _BatchTimingWindow:
    reel_number: str
    clip_number: str
    start_abs_frame: int
    end_abs_frame: int
    fps: float
    clip_count: int


def _auto_assign_mixed_media_batches(
    assignments: list[BatchAssignment],
    *,
    redline_exe: Optional[str] = None,
    log_callback: Optional[BatchLogCallback] = None,
) -> None:
    unresolved = [assignment for assignment in assignments if assignment.assignment_state == "needs_assignment"]
    if not unresolved:
        return

    parsed_groups = regroup_batch_groups(assignments)
    if not parsed_groups:
        return

    metadata_cache: dict[MetadataCacheKey, ClipMetadata] = {}
    windows = _build_batch_timing_windows(parsed_groups, metadata_cache, redline_exe=redline_exe, log_callback=log_callback)
    if not windows:
        return

    for assignment in unresolved:
        metadata = _load_assignment_metadata(assignment, metadata_cache, redline_exe=redline_exe, log_callback=log_callback)
        assignment.metadata_source = metadata.metadata_provenance or "unresolved"
        assignment.metadata_confidence = metadata.metadata_confidence or "unresolved"
        assignment.assignment_reason = metadata.metadata_error or f"{assignment.metadata_source}: manual assignment required"
        assignment.manufacturer = metadata.manufacturer or assignment.manufacturer
        assignment.format_type = metadata.format_type or assignment.format_type
        if not _metadata_strong_enough_for_auto_assignment(metadata):
            continue
        match = _unique_matching_batch_window(metadata, windows)
        if match is None:
            if metadata.metadata_error:
                assignment.assignment_reason = metadata.metadata_error
            else:
                assignment.assignment_reason = (
                    f"{assignment.metadata_source}: metadata did not map uniquely to a single batch."
                )
            continue
        assignment.reel_number = match.reel_number
        assignment.clip_number = match.clip_number
        assignment.assignment_state = "auto_assigned"
        assignment.assignment_reason = (
            f"Auto-assigned from {assignment.metadata_source} ({assignment.metadata_confidence}) "
            f"into Reel {match.reel_number} / Clip {match.clip_number}."
        )
        if log_callback is not None:
            log_callback(
                f"Auto-assigned {assignment.clip.clip_name} to Reel {match.reel_number} / Clip {match.clip_number} via {assignment.metadata_source}."
            )


def _build_batch_timing_windows(
    groups: list[BatchGroup],
    metadata_cache: dict[MetadataCacheKey, ClipMetadata],
    *,
    redline_exe: Optional[str] = None,
    log_callback: Optional[BatchLogCallback] = None,
) -> list[_BatchTimingWindow]:
    windows: list[_BatchTimingWindow] = []
    for group in groups:
        usable: list[tuple[int, int, float]] = []
        for clip in group.clips:
            cache_key = _metadata_cache_key(clip, redline_exe)
            metadata = metadata_cache.get(cache_key)
            if metadata is None:
                if clip.provider_kind == "red" and not redline_exe:
                    continue
                metadata = load_provider_metadata(clip.source_path, provider_kind=clip.provider_kind, redline_exe=redline_exe)
                metadata_cache[cache_key] = metadata
            window = _metadata_frame_window(metadata)
            if window is None:
                continue
            start_abs, end_abs = window
            if metadata.clip_fps is None:
                continue
            usable.append((start_abs, end_abs, metadata.clip_fps))
        if not usable:
            continue
        overlap_start = max(item[0] for item in usable)
        overlap_end = min(item[1] for item in usable)
        fps = _first_common_fps([item[2] for item in usable])
        if fps is None:
            continue
        if overlap_start > overlap_end:
            overlap_start = min(item[0] for item in usable)
            overlap_end = max(item[1] for item in usable)
        windows.append(
            _BatchTimingWindow(
                reel_number=group.reel_number,
                clip_number=group.clip_number,
                start_abs_frame=overlap_start,
                end_abs_frame=overlap_end,
                fps=fps,
                clip_count=len(group.clips),
            )
        )
        if log_callback is not None:
            log_callback(
                f"Timing window for Reel {group.reel_number} / Clip {group.clip_number}: "
                f"{overlap_start} → {overlap_end} ({len(group.clips)} clips)."
            )
    return windows


def _load_assignment_metadata(
    assignment: BatchAssignment,
    metadata_cache: dict[MetadataCacheKey, ClipMetadata],
    *,
    redline_exe: Optional[str] = None,
    log_callback: Optional[BatchLogCallback] = None,
) -> ClipMetadata:
    cache_key = _metadata_cache_key(assignment.clip, redline_exe)
    metadata = metadata_cache.get(cache_key)
    if metadata is not None:
        return metadata
    metadata = load_provider_metadata(
        assignment.clip.source_path,
        provider_kind=assignment.clip.provider_kind,
        redline_exe=redline_exe,
    )
    metadata_cache[cache_key] = metadata
    if log_callback is not None and metadata.metadata_error:
        log_callback(f"{assignment.clip.clip_name}: {metadata.metadata_error}")
    return metadata


def _metadata_frame_window(metadata: ClipMetadata) -> Optional[tuple[int, int]]:
    if not metadata.metadata_ok or not metadata.start_timecode or not metadata.end_timecode or metadata.clip_fps is None:
        return None
    drop_frame = bool(";" in metadata.start_timecode)
    start_abs = timecode_to_frame(metadata.start_timecode, metadata.clip_fps, drop_frame=drop_frame)
    end_abs = timecode_to_frame(metadata.end_timecode, metadata.clip_fps, drop_frame=drop_frame)
    if end_abs < start_abs:
        return None
    return start_abs, end_abs


def _metadata_strong_enough_for_auto_assignment(metadata: ClipMetadata) -> bool:
    return bool(
        metadata.metadata_ok
        and metadata.sync_eligible
        and metadata.metadata_confidence in {"high", "medium"}
        and metadata.metadata_provenance in {"ffprobe", "mediainfo"}
    )


def _unique_matching_batch_window(metadata: ClipMetadata, windows: list[_BatchTimingWindow]) -> Optional[_BatchTimingWindow]:
    clip_window = _metadata_frame_window(metadata)
    if clip_window is None or metadata.clip_fps is None:
        return None
    clip_start, clip_end = clip_window
    compatible: list[tuple[int, _BatchTimingWindow]] = []
    for window in windows:
        if abs(window.fps - metadata.clip_fps) > 0.02:
            continue
        overlap_start = max(clip_start, window.start_abs_frame)
        overlap_end = min(clip_end, window.end_abs_frame)
        if overlap_end < overlap_start:
            continue
        overlap_frames = overlap_end - overlap_start + 1
        compatible.append((overlap_frames, window))
    if len(compatible) != 1:
        compatible.sort(key=lambda item: (item[0], item[1].clip_count), reverse=True)
        if len(compatible) >= 2 and compatible[0][0] > compatible[1][0]:
            return compatible[0][1]
        return None
    return compatible[0][1]


def _first_common_fps(values: list[float]) -> Optional[float]:
    if not values:
        return None
    first = values[0]
    if all(abs(value - first) <= 0.02 for value in values[1:]):
        return first
    return None


def build_batch_pdf_name(reel_number: str, clip_number: str) -> str:
    return f"Reel_{reel_number}_{clip_number}_contact_sheet.pdf"


def build_batch_output_path(output_root: Path, batch_group: BatchGroup) -> Path:
    root = Path(output_root).expanduser().resolve()
    return root / f"Reel_{batch_group.reel_number}" / batch_group.output_pdf_name


def parse_batch_key(clip_name: str) -> Optional[tuple[str, str]]:
    match = re.match(r"^[A-Za-z]+(?P<reel>\d{3,})_[A-Za-z]+(?P<clip>\d{3,})", clip_name)
    if not match:
        return None
    return match.group("reel"), match.group("clip")


def suggest_clip_family(clip: ClipEntry) -> str:
    path_parts = [part.lower() for part in clip.source_path.parts]
    clip_name = clip.clip_name.lower()
    manufacturer = (clip.manufacturer or "").lower()
    format_type = (clip.format_type or "").lower()
    provider_kind = clip.provider_kind.lower()

    if provider_kind == "red" or manufacturer == "red" or format_type == "r3d":
        return "RED"
    if provider_kind == "braw" or "blackmagic" in manufacturer or "braw" in format_type:
        return "Blackmagic"
    if "canon" in manufacturer or any("canon" in part for part in path_parts):
        return "Canon"
    if "sony" in manufacturer or any("sony" in part for part in path_parts):
        return "Sony"
    if "hyperdeck" in manufacturer or "hyperdeck" in clip_name or any("hyperdeck" in part for part in path_parts):
        return "HyperDeck"
    return "Generic Video"


def suggest_subgroup(clip: ClipEntry, clip_family: Optional[str] = None) -> str:
    family = clip_family or suggest_clip_family(clip)
    path_parts = [part.lower() for part in clip.source_path.parts]
    clip_name = clip.clip_name.lower()
    searchable = path_parts + [clip_name]

    if any("body" in value for value in searchable):
        return "Body Cams"
    if any("face" in value for value in searchable):
        return "Face Cams"
    if any("witness" in value for value in searchable):
        return "Witness Cameras"
    if any("reference" in value or "ref" == value for value in searchable):
        return "Reference"
    if any("main" in value for value in searchable):
        return "Main Cameras"

    family_defaults = {
        "RED": "Main Cameras",
        "Blackmagic": "Witness Cameras",
        "Canon": "Witness Cameras",
        "Sony": "Witness Cameras",
        "HyperDeck": "Reference",
        "Generic Video": "Reference",
    }
    return family_defaults.get(family, "Reference")


def _scan_source_tree(src: Path, group_mode: GroupMode) -> List[ClipEntry]:
    clips: List[ClipEntry] = []
    seen_sources: set[Path] = set()
    for root, dirnames, filenames in os.walk(src):
        root_path = Path(root)

        # Treat RDC packages as single clip containers and do not recurse inside them.
        package_dirs = [name for name in dirnames if name.lower().endswith(".rdc")]
        for package_name in sorted(package_dirs, key=_natural_sort_key):
            clip = _resolve_rdc_package(root_path / package_name, group_mode)
            if clip.source_path not in seen_sources:
                seen_sources.add(clip.source_path)
                clips.append(clip)
        dirnames[:] = [name for name in dirnames if name.lower() not in {name.lower() for name in package_dirs}]

        # Standalone media not already represented by an RDC package.
        for filename in sorted(filenames, key=_natural_sort_key):
            candidate = root_path / filename
            if candidate.suffix.lower() not in {".r3d", *GENERIC_VIDEO_EXTENSIONS, *RED_THUMBNAIL_EXTENSIONS}:
                continue
            if _find_rdc_ancestor(candidate, src) is not None:
                continue
            clip = _resolve_clip_path(candidate, group_mode)
            if clip is not None and clip.source_path not in seen_sources:
                seen_sources.add(clip.source_path)
                clips.append(clip)
    return clips


def _discover_batch_clips(
    src: Path,
    *,
    alphabetize: bool,
    progress_callback: Optional[BatchScanProgressCallback] = None,
) -> list[ClipEntry]:
    candidates = _collect_media_candidates(src)
    clips: list[ClipEntry] = []
    seen_sources: set[Path] = set()
    total = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        clip = _resolve_clip_path(candidate, "flat")
        if progress_callback is not None:
            progress_callback("scan", index, total)
        if clip is None or clip.source_path in seen_sources:
            continue
        seen_sources.add(clip.source_path)
        clips.append(clip)
    if alphabetize:
        clips.sort(key=lambda item: (_natural_sort_key(item.clip_name), _natural_sort_key(item.group_name)))
    return clips


def _collect_media_candidates(src: Path) -> list[Path]:
    if not src.exists():
        raise FileNotFoundError(f"Source path does not exist: {src}")
    if src.is_file():
        return [src.resolve()]
    if _is_rdc_dir(src):
        return [src.resolve()]

    candidates: list[Path] = []
    seen_packages: set[Path] = set()
    for root, dirnames, filenames in os.walk(src):
        root_path = Path(root)
        package_dirs = [name for name in dirnames if name.lower().endswith(".rdc")]
        for package_name in sorted(package_dirs, key=_natural_sort_key):
            package_path = (root_path / package_name).resolve()
            if package_path not in seen_packages:
                seen_packages.add(package_path)
                candidates.append(package_path)
        dirnames[:] = [name for name in dirnames if name.lower() not in {name.lower() for name in package_dirs}]
        for filename in sorted(filenames, key=_natural_sort_key):
            candidate = (root_path / filename).resolve()
            if candidate.suffix.lower() not in {".r3d", *GENERIC_VIDEO_EXTENSIONS, *RED_THUMBNAIL_EXTENSIONS}:
                continue
            if _find_rdc_ancestor(candidate, src) is not None:
                continue
            candidates.append(candidate)
    return candidates


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
            provider_kind="red",
            manufacturer="RED",
            format_type="R3D",
            container_path=candidate.parent,
            package_path=package_path,
            segment_count=segment_count,
            segment_index=segment_index,
        )
    if candidate.is_file() and candidate.suffix.lower() in GENERIC_VIDEO_EXTENSIONS:
        clip_name = candidate.stem
        reel_name = _derive_reel_name(clip_name)
        group_name = _derive_group_name(candidate, candidate.parent, reel_name, group_mode)
        provider_kind = provider_kind_for_path(candidate)
        source_kind: Literal["video", "braw"] = "braw" if provider_kind == "braw" else "video"
        return ClipEntry(
            source_path=candidate,
            clip_name=clip_name,
            reel_name=reel_name,
            group_name=group_name,
            source_kind=source_kind,
            provider_kind=provider_kind,
            manufacturer=_manufacturer_for_provider(provider_kind),
            format_type=candidate.suffix.lstrip(".").upper() or "Video",
            container_path=candidate.parent,
            package_path=None,
        )
    if candidate.is_file() and candidate.suffix.lower() in RED_THUMBNAIL_EXTENSIONS:
        clip_name = candidate.stem
        reel_name = _derive_reel_name(clip_name)
        package_path = _find_rdc_ancestor(candidate)
        group_name = _derive_group_name(candidate, package_path or candidate.parent, reel_name, group_mode)
        return ClipEntry(
            source_path=candidate,
            clip_name=clip_name,
            reel_name=reel_name,
            group_name=group_name,
            source_kind="thumbnail",
            provider_kind="thumbnail",
            manufacturer="RED",
            format_type="RED Thumbnail",
            container_path=package_path or candidate.parent,
            package_path=package_path,
        )
    if candidate.is_dir() and _is_rdc_dir(candidate):
        return _resolve_rdc_package(candidate, group_mode)
    return None


def _resolve_rdc_package(path: Path, group_mode: GroupMode) -> ClipEntry:
    package_path = Path(path).expanduser().resolve()
    segments = _list_r3d_segments(package_path)
    if not segments:
        thumbnails = _list_red_thumbnails(package_path)
        if not thumbnails:
            raise FileNotFoundError(f"{package_path} contains no .R3D media or RED thumbnail preview.")
        return _resolve_clip_path(thumbnails[0], group_mode)  # type: ignore[return-value]

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
        provider_kind="red",
        manufacturer="RED",
        format_type="R3D",
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


def _list_red_thumbnails(package_path: Path) -> List[Path]:
    return sorted(
        [path.resolve() for path in package_path.iterdir() if path.is_file() and path.suffix.lower() in RED_THUMBNAIL_EXTENSIONS],
        key=lambda item: _natural_sort_key(item.name),
    )


def _choose_primary_segment(segments: List[Path]) -> Path:
    for candidate in segments:
        if candidate.stem.endswith("_001"):
            return candidate
    return segments[0]


def _build_output_name(clip: ClipEntry, clip_fields: ClipOrganization, index: int) -> str:
    label = clip_fields.camera_label.strip() or clip.clip_name
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
    if not normalized:
        normalized = clip.clip_name
    return f"{index:03d}_{normalized}.jpg"


def _derive_group_name(source_path: Path, container_path: Path, reel_name: str, group_mode: GroupMode) -> str:
    if group_mode == "parent_folder":
        if _is_rdc_dir(container_path):
            return container_path.stem or reel_name
        return container_path.name or reel_name
    if group_mode == "reel_prefix":
        return reel_name
    return "renders"


def _default_camera_label(clip_name: str) -> str:
    parts = clip_name.split("_")
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return clip_name.replace("_", " ")


def _dedupe_clip_entries(clips: List[ClipEntry]) -> List[ClipEntry]:
    unique: list[ClipEntry] = []
    seen_sources: set[Path] = set()
    for clip in clips:
        if clip.source_path in seen_sources:
            continue
        seen_sources.add(clip.source_path)
        unique.append(clip)
    return unique


def _metadata_cache_key(clip: ClipEntry, redline_exe: Optional[str]) -> MetadataCacheKey:
    stat = clip.source_path.stat()
    redline_token = redline_exe or ""
    return (
        clip.provider_kind,
        str(clip.source_path.resolve()),
        stat.st_mtime_ns,
        stat.st_size,
        redline_token,
    )


def _manufacturer_for_provider(provider_kind: str) -> str:
    if provider_kind in {"red", "thumbnail"}:
        return "RED"
    if provider_kind == "braw":
        return "Blackmagic"
    return "Generic Video"


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


def _unavailable_metadata(clip: ClipEntry, exc: Exception) -> ClipMetadata:
    """Keep a bad asset visible without aborting discovery or the whole batch."""

    return ClipMetadata(
        clip_path=clip.source_path,
        clip_fps=None,
        timecode_base_fps=None,
        start_timecode=None,
        total_frames=None,
        resolution=None,
        timecode_source="unavailable",
        drop_frame=False,
        sync_basis=f"Metadata probe failed: {exc}",
        metadata_ok=False,
        raw_fields={},
        manufacturer=clip.manufacturer or _manufacturer_for_provider(clip.provider_kind),
        format_type=clip.format_type or clip.source_path.suffix.lstrip(".").upper(),
        provider_name=clip.provider_kind,
        timecode_supported=False,
        sync_eligible=False,
        render_supported=clip.provider_kind == "thumbnail",
        metadata_error=str(exc),
        metadata_provenance="probe_error",
        metadata_confidence="unresolved",
    )
