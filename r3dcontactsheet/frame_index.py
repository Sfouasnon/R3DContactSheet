"""Automatic metadata-based sync resolution for multicamera RED clips."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .metadata import ClipMetadata
from .timecode import frame_to_timecode, timecode_to_frame


@dataclass(frozen=True)
class FrameTargetRequest:
    target_timecode: Optional[str] = None
    fps: Optional[float] = None
    drop_frame: bool = False


@dataclass(frozen=True)
class OverlapSubset:
    subset_id: str
    clip_paths: tuple[str, ...]
    start_abs_frame: int
    end_abs_frame: int
    start_timecode: str
    end_timecode: str
    shared_frame_count: int
    recommended_abs_frame: int


@dataclass(frozen=True)
class MatchSelectionState:
    active_subset_id: Optional[str]
    selected_abs_frame: Optional[int]
    selection_mode: str = "auto"


@dataclass(frozen=True)
class MatchingMoment:
    absolute_frame: Optional[int]
    source_timecode: Optional[str]
    source: str
    sync_status: str
    note: str
    ambiguity_reason: str = ""
    sync_mode: str = "none"
    matched_subset_size: int = 0
    total_clips: int = 0
    matched_clip_paths: tuple[str, ...] = ()
    unmatched_clip_paths: tuple[str, ...] = ()
    overlap_group_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrameResolution:
    frame_index: int
    source: str
    verification_note: str
    absolute_frame: Optional[int] = None
    resolved_timecode: Optional[str] = None
    clip_fps: Optional[float] = None
    timecode_base_fps: Optional[float] = None
    sync_basis: str = ""
    sync_status: str = "ambiguous"
    ambiguity_reason: str = ""
    in_matched_subset: bool = False
    source_timecode_in: Optional[str] = None
    source_timecode_out: Optional[str] = None
    match_timecode: Optional[str] = None
    match_frame: Optional[int] = None
    range_relation: str = ""


def analyze_overlap_subsets(
    metadata_items: Sequence[ClipMetadata],
    request: Optional[FrameTargetRequest] = None,
) -> tuple[list[OverlapSubset], MatchSelectionState]:
    request = request or FrameTargetRequest()
    usable = [item for item in metadata_items if item.metadata_ok and _frame_range_is_finite(item)]
    subsets = _collect_overlap_subsets(usable)
    if not subsets:
        return ([], MatchSelectionState(active_subset_id=None, selected_abs_frame=None, selection_mode="auto"))

    ranked = _rank_subsets(subsets, request)
    active_subset = ranked[0]
    if request.target_timecode:
        fps_basis = request.fps or _common_value([item.timecode_base_fps for item in usable])
        if fps_basis is not None:
            requested_abs = timecode_to_frame(request.target_timecode, fps_basis, drop_frame=request.drop_frame)
            containing = [subset for subset in ranked if subset.start_abs_frame <= requested_abs <= subset.end_abs_frame]
            if containing:
                active_subset = containing[0]
                selected_abs_frame = requested_abs
                return (
                    ranked,
                    MatchSelectionState(
                        active_subset_id=active_subset.subset_id,
                        selected_abs_frame=selected_abs_frame,
                        selection_mode="custom",
                    ),
                )
    return (
        ranked,
        MatchSelectionState(
            active_subset_id=active_subset.subset_id,
            selected_abs_frame=active_subset.recommended_abs_frame,
            selection_mode="auto",
        ),
    )


def resolve_matching_moment(
    metadata_items: Sequence[ClipMetadata],
    request: Optional[FrameTargetRequest] = None,
) -> MatchingMoment:
    request = request or FrameTargetRequest()
    usable = [item for item in metadata_items if item.metadata_ok and item.start_timecode and item.timecode_base_fps]
    total_clips = len(metadata_items)
    if not usable:
        return MatchingMoment(
            absolute_frame=None,
            source_timecode=None,
            source="metadata",
            sync_status="metadata_incomplete",
            note="No clips had enough metadata to resolve a shared matching moment.",
            ambiguity_reason="Clip metadata is incomplete.",
            sync_mode="none",
            matched_subset_size=0,
            total_clips=total_clips,
        )

    subsets, selection = analyze_overlap_subsets(usable, request)
    active_subset = _subset_by_id(subsets, selection.active_subset_id)
    if active_subset is None or selection.selected_abs_frame is None:
        reference_clip = usable[0]
        return MatchingMoment(
            absolute_frame=_start_absolute_frame(reference_clip),
            source_timecode=reference_clip.start_timecode,
            source="no_common_moment",
            sync_status="ambiguous",
            note="No useful shared moment exists across the selected clips. Each clip will keep its own real metadata timecode/frame in the contact sheet.",
            ambiguity_reason="No common moment across clips.",
            sync_mode="none",
            matched_subset_size=1,
            total_clips=total_clips,
            matched_clip_paths=_clip_paths((reference_clip,)),
            unmatched_clip_paths=_clip_paths(item for item in usable[1:]),
            overlap_group_paths=(),
        )

    matched_subset, unmatched_subset = _partition_by_frame(selection.selected_abs_frame, usable)
    reference_clip = _clip_for_frame(selection.selected_abs_frame, usable) or usable[0]
    sync_mode = "full" if len(matched_subset) == len(usable) else "partial" if len(matched_subset) >= 2 else "none"
    return MatchingMoment(
        absolute_frame=selection.selected_abs_frame,
        source_timecode=_timecode_for_absolute_frame(reference_clip, selection.selected_abs_frame),
        source="overlap_selection",
        sync_status="verified" if sync_mode == "full" else "ambiguous",
        note="Matching moment resolved from the strongest real metadata overlap subset.",
        ambiguity_reason="" if sync_mode == "full" else "Not every clip contains the selected shared frame.",
        sync_mode=sync_mode,
        matched_subset_size=len(active_subset.clip_paths),
        total_clips=total_clips,
        matched_clip_paths=active_subset.clip_paths,
        unmatched_clip_paths=_clip_paths(unmatched_subset),
        overlap_group_paths=active_subset.clip_paths,
    )


def resolve_clip_frame(
    clip_metadata: ClipMetadata,
    matching_moment: MatchingMoment,
) -> FrameResolution:
    if not clip_metadata.metadata_ok or not clip_metadata.start_timecode or clip_metadata.timecode_base_fps is None:
        return FrameResolution(
            frame_index=0,
            source=matching_moment.source,
            verification_note=matching_moment.note,
            absolute_frame=None,
            resolved_timecode=None,
            clip_fps=clip_metadata.clip_fps,
            timecode_base_fps=clip_metadata.timecode_base_fps,
            sync_basis=clip_metadata.sync_basis,
            sync_status="metadata_incomplete",
            ambiguity_reason="Clip metadata is incomplete.",
            source_timecode_in=clip_metadata.start_timecode,
            source_timecode_out=clip_timecode_out(clip_metadata),
        )

    clip_start = _start_absolute_frame(clip_metadata)
    if clip_start is None or matching_moment.absolute_frame is None:
        return FrameResolution(
            frame_index=0,
            source=matching_moment.source,
            verification_note=matching_moment.note,
            absolute_frame=None,
            resolved_timecode=matching_moment.source_timecode,
            clip_fps=clip_metadata.clip_fps,
            timecode_base_fps=clip_metadata.timecode_base_fps,
            sync_basis=clip_metadata.sync_basis,
            sync_status="ambiguous",
            ambiguity_reason=matching_moment.ambiguity_reason or "Matching moment unavailable.",
            in_matched_subset=False,
            source_timecode_in=clip_metadata.start_timecode,
            source_timecode_out=clip_timecode_out(clip_metadata),
            match_timecode=clip_metadata.start_timecode,
            match_frame=clip_start,
        )

    clip_end = _end_absolute_frame(clip_metadata)
    path_text = str(clip_metadata.clip_path.resolve())
    in_overlap_group = path_text in matching_moment.overlap_group_paths
    if matching_moment.sync_mode == "none":
        frame_index = 0
        absolute_frame = clip_start
        resolved_timecode = clip_metadata.start_timecode
        sync_status = "outside_overlap"
        in_subset = False
    else:
        chosen_frame = matching_moment.absolute_frame
        in_subset = path_text in matching_moment.matched_clip_paths
        if chosen_frame is not None and in_subset and _frame_contains(chosen_frame, clip_metadata):
            absolute_frame = chosen_frame
            frame_index = max(0, absolute_frame - clip_start)
            resolved_timecode = _timecode_for_absolute_frame(clip_metadata, absolute_frame)
            sync_status = "exact_match"
        elif chosen_frame is not None and clip_end is not None:
            absolute_frame = clip_start if abs(chosen_frame - clip_start) <= abs(chosen_frame - clip_end) else clip_end
            frame_index = max(0, absolute_frame - clip_start)
            resolved_timecode = _timecode_for_absolute_frame(clip_metadata, absolute_frame)
            sync_status = "nearest_available" if in_overlap_group else "outside_overlap"
        else:
            absolute_frame = clip_start
            frame_index = 0
            resolved_timecode = clip_metadata.start_timecode
            sync_status = "outside_overlap"
    return FrameResolution(
        frame_index=frame_index,
        source=matching_moment.source,
        verification_note=matching_moment.note,
        absolute_frame=absolute_frame,
        resolved_timecode=resolved_timecode,
        clip_fps=clip_metadata.clip_fps,
        timecode_base_fps=clip_metadata.timecode_base_fps,
        sync_basis=clip_metadata.sync_basis,
        sync_status=sync_status,
        ambiguity_reason=matching_moment.ambiguity_reason,
        in_matched_subset=in_subset,
        source_timecode_in=clip_metadata.start_timecode,
        source_timecode_out=clip_timecode_out(clip_metadata),
        match_timecode=resolved_timecode,
        match_frame=absolute_frame,
    )


def resolve_clip_frame_for_selection(
    clip_metadata: ClipMetadata,
    active_subset: Optional[OverlapSubset],
    selection: MatchSelectionState,
) -> FrameResolution:
    if not clip_metadata.metadata_ok or not clip_metadata.start_timecode or clip_metadata.timecode_base_fps is None:
        return FrameResolution(
            frame_index=0,
            source="overlap_selection",
            verification_note="Clip metadata is incomplete.",
            absolute_frame=None,
            resolved_timecode=None,
            clip_fps=clip_metadata.clip_fps,
            timecode_base_fps=clip_metadata.timecode_base_fps,
            sync_basis=clip_metadata.sync_basis,
            sync_status="metadata_incomplete",
            ambiguity_reason="Clip metadata is incomplete.",
            in_matched_subset=False,
            source_timecode_in=clip_metadata.start_timecode,
            source_timecode_out=clip_timecode_out(clip_metadata),
            match_timecode=None,
            match_frame=None,
            range_relation="Unavailable",
        )

    clip_start, clip_end = _frame_range(clip_metadata)
    selected_abs = selection.selected_abs_frame
    path_text = str(clip_metadata.clip_path.resolve())
    in_subset = bool(active_subset and path_text in active_subset.clip_paths)

    if selected_abs is None:
        return FrameResolution(
            frame_index=0,
            source="overlap_selection",
            verification_note="No valid overlap subset was available.",
            absolute_frame=clip_start,
            resolved_timecode=clip_metadata.start_timecode,
            clip_fps=clip_metadata.clip_fps,
            timecode_base_fps=clip_metadata.timecode_base_fps,
            sync_basis=clip_metadata.sync_basis,
            sync_status="outside_overlap",
            ambiguity_reason="No selected overlap frame.",
            in_matched_subset=False,
            source_timecode_in=clip_metadata.start_timecode,
            source_timecode_out=clip_timecode_out(clip_metadata),
            match_timecode=clip_metadata.start_timecode,
            match_frame=clip_start,
            range_relation="Unavailable",
        )

    if clip_start <= selected_abs <= clip_end:
        chosen_abs = selected_abs
        relation = "In Range"
        sync_status = "exact_match"
    elif selected_abs < clip_start:
        chosen_abs = clip_start
        relation = "Later Only"
        sync_status = "outside_overlap"
    else:
        chosen_abs = clip_end
        relation = "Earlier Only"
        sync_status = "outside_overlap"

    frame_index = max(0, chosen_abs - clip_start)
    return FrameResolution(
        frame_index=frame_index,
        source="overlap_selection",
        verification_note="Clip frame resolved from the selected shared overlap frame.",
        absolute_frame=chosen_abs,
        resolved_timecode=_timecode_for_absolute_frame(clip_metadata, chosen_abs),
        clip_fps=clip_metadata.clip_fps,
        timecode_base_fps=clip_metadata.timecode_base_fps,
        sync_basis=clip_metadata.sync_basis,
        sync_status=sync_status,
        ambiguity_reason="" if sync_status == "exact_match" else "Clip does not contain the selected shared frame.",
        in_matched_subset=in_subset,
        source_timecode_in=clip_metadata.start_timecode,
        source_timecode_out=clip_timecode_out(clip_metadata),
        match_timecode=_timecode_for_absolute_frame(clip_metadata, chosen_abs),
        match_frame=chosen_abs,
        range_relation=relation,
    )


def _frame_range(item: ClipMetadata) -> tuple[int, Optional[int]]:
    start = _start_absolute_frame(item)
    if start is None:
        raise ValueError("Metadata range requested without a valid clip start frame.")
    if item.total_frames is None or item.total_frames <= 0:
        return (start, None)
    return (start, start + item.total_frames - 1)


def _frame_range_is_finite(item: ClipMetadata) -> bool:
    _, end = _frame_range(item)
    return end is not None


def _frame_in_all_ranges(frame: int, items: Sequence[ClipMetadata]) -> bool:
    for item in items:
        if not _frame_contains(frame, item):
            return False
    return True


def _clip_for_frame(frame: int, items: Sequence[ClipMetadata]) -> Optional[ClipMetadata]:
    for item in items:
        start, end = _frame_range(item)
        if frame >= start and (end is None or frame <= end):
            return item
    return None


def _frame_contains(frame: int, item: ClipMetadata) -> bool:
    start, end = _frame_range(item)
    if end is None:
        return frame == start
    return start <= frame <= end


def clip_timecode_out(item: ClipMetadata) -> Optional[str]:
    end_abs = _end_absolute_frame(item)
    if end_abs is None:
        return None
    return _timecode_for_absolute_frame(item, end_abs)


def _partition_by_frame(frame: int, items: Sequence[ClipMetadata]) -> tuple[list[ClipMetadata], list[ClipMetadata]]:
    matched: list[ClipMetadata] = []
    unmatched: list[ClipMetadata] = []
    for item in items:
        if _frame_contains(frame, item):
            matched.append(item)
        else:
            unmatched.append(item)
    return matched, unmatched


def _largest_matching_subset(items: Sequence[ClipMetadata]) -> tuple[Optional[int], list[ClipMetadata]]:
    ranges = {id(item): _frame_range(item) for item in items}
    candidate_frames = {
        point
        for item in items
        for point in ranges[id(item)]
        if point is not None
    }
    for index, left in enumerate(items):
        left_start, left_end = ranges[id(left)]
        for right in items[index + 1 :]:
            right_start, right_end = ranges[id(right)]
            if left_end is None or right_end is None:
                candidate_frames.add(max(left_start, right_start))
                continue
            overlap_start = max(left_start, right_start)
            overlap_end = min(left_end, right_end)
            if overlap_start <= overlap_end:
                candidate_frames.add(overlap_start)
    candidate_frames = sorted(candidate_frames)
    best_frame: Optional[int] = None
    best_subset: list[ClipMetadata] = []
    for candidate in candidate_frames:
        subset, _ = _partition_by_frame(candidate, items)
        if len(subset) > len(best_subset):
            best_frame = candidate
            best_subset = subset
    if len(best_subset) < 2:
        return None, []
    return best_frame, best_subset


def _collect_overlap_subsets(items: Sequence[ClipMetadata]) -> list[OverlapSubset]:
    if not items:
        return []
    ranges = {str(item.clip_path.resolve()): _frame_range(item) for item in items}
    boundaries: set[int] = set()
    for start, end in ranges.values():
        if end is None:
            continue
        boundaries.add(start)
        boundaries.add(end + 1)
    if not boundaries:
        return []
    sorted_boundaries = sorted(boundaries)
    segments: list[tuple[tuple[str, ...], int, int]] = []
    for index, start in enumerate(sorted_boundaries[:-1]):
        end = sorted_boundaries[index + 1] - 1
        if start > end:
            continue
        active = tuple(
            sorted(
                path
                for path, (clip_start, clip_end) in ranges.items()
                if clip_end is not None and clip_start <= start <= clip_end
            )
        )
        if not active:
            continue
        if segments and segments[-1][0] == active and segments[-1][2] + 1 == start:
            segments[-1] = (active, segments[-1][1], end)
        else:
            segments.append((active, start, end))

    subsets: list[OverlapSubset] = []
    for index, (clip_paths, start, end) in enumerate(segments, start=1):
        reference = _clip_from_path(items, clip_paths[0])
        if reference is None:
            continue
        subsets.append(
            OverlapSubset(
                subset_id=f"subset_{index}",
                clip_paths=clip_paths,
                start_abs_frame=start,
                end_abs_frame=end,
                start_timecode=_timecode_for_absolute_frame(reference, start) or "Unavailable",
                end_timecode=_timecode_for_absolute_frame(reference, end) or "Unavailable",
                shared_frame_count=(end - start) + 1,
                recommended_abs_frame=start if start == end else start + ((end - start) // 2),
            )
        )
    return subsets


def _rank_subsets(subsets: Sequence[OverlapSubset], request: FrameTargetRequest) -> list[OverlapSubset]:
    requested_abs: Optional[int] = None
    if request.target_timecode and request.fps:
        requested_abs = timecode_to_frame(request.target_timecode, request.fps, drop_frame=request.drop_frame)

    def _key(subset: OverlapSubset) -> tuple[int, int, int]:
        distance = abs(subset.recommended_abs_frame - requested_abs) if requested_abs is not None else 0
        return (-len(subset.clip_paths), -subset.shared_frame_count, distance)

    return sorted(subsets, key=_key)


def _subset_by_id(subsets: Sequence[OverlapSubset], subset_id: Optional[str]) -> Optional[OverlapSubset]:
    if subset_id is None:
        return None
    for subset in subsets:
        if subset.subset_id == subset_id:
            return subset
    return None


def _clip_from_path(items: Sequence[ClipMetadata], path_text: str) -> Optional[ClipMetadata]:
    for item in items:
        if str(item.clip_path.resolve()) == path_text:
            return item
    return None


def _overlap_groups(items: Sequence[ClipMetadata]) -> list[list[ClipMetadata]]:
    if not items:
        return []
    remaining = list(items)
    groups: list[list[ClipMetadata]] = []
    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        changed = True
        while changed:
            changed = False
            next_remaining: list[ClipMetadata] = []
            for candidate in remaining:
                if any(_ranges_overlap(candidate, member) for member in group):
                    group.append(candidate)
                    changed = True
                else:
                    next_remaining.append(candidate)
            remaining = next_remaining
        groups.append(group)
    groups.sort(key=lambda group: (-len(group), _group_start(group)))
    return groups


def _ranges_overlap(left: ClipMetadata, right: ClipMetadata) -> bool:
    left_start, left_end = _frame_range(left)
    right_start, right_end = _frame_range(right)
    if left_end is None and right_end is None:
        return left_start == right_start
    if left_end is None:
        return right_start <= left_start <= right_end
    if right_end is None:
        return left_start <= right_start <= left_end
    return max(left_start, right_start) <= min(left_end, right_end)


def _group_start(items: Sequence[ClipMetadata]) -> int:
    starts = [_start_absolute_frame(item) for item in items]
    usable = [start for start in starts if start is not None]
    return min(usable) if usable else 0


def _common_value(values: Sequence[Optional[float]]) -> Optional[float]:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    if len({round(value, 6) for value in usable}) == 1:
        return usable[0]
    return None


def _start_absolute_frame(item: ClipMetadata) -> Optional[int]:
    if not item.start_timecode or item.timecode_base_fps is None:
        return None
    return timecode_to_frame(
        item.start_timecode,
        item.timecode_base_fps,
        drop_frame=item.drop_frame,
    )


def _end_absolute_frame(item: ClipMetadata) -> Optional[int]:
    if not item.end_timecode or item.timecode_base_fps is None:
        return None
    return timecode_to_frame(
        item.end_timecode,
        item.timecode_base_fps,
        drop_frame=item.drop_frame,
    )


def _timecode_for_absolute_frame(item: ClipMetadata, absolute_frame: int) -> Optional[str]:
    if not item.start_timecode or item.timecode_base_fps is None:
        return None
    start_abs = _start_absolute_frame(item)
    if start_abs is None:
        return None
    if absolute_frame < start_abs:
        return None
    start_frame_number = timecode_to_frame(item.start_timecode, item.timecode_base_fps, drop_frame=item.drop_frame)
    if absolute_frame == start_frame_number:
        return item.start_timecode
    # Preserve the original metadata clock while advancing frame-accurately.
    return frame_to_timecode(absolute_frame, item.timecode_base_fps, drop_frame=item.drop_frame)


def _clip_paths(items: Sequence[ClipMetadata]) -> tuple[str, ...]:
    return tuple(str(item.clip_path.resolve()) for item in items)
