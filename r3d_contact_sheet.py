#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont


SUPPORTED_LAYOUTS = {
    12: (4, 3),
    24: (6, 4),
    36: (6, 6),
}


@dataclass(frozen=True)
class ClipInfo:
    source_path: Path
    clip_label: str
    reel_label: str


@dataclass(frozen=True)
class RenderedClip:
    clip: ClipInfo
    image_path: Path
    frame_index: int


class ContactSheetError(RuntimeError):
    pass


def _natural_sort_key(value: str) -> Tuple:
    parts = re.split(r"(\d+)", value)
    normalized: List[object] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            normalized.append(int(part))
        else:
            normalized.append(part.lower())
    return tuple(normalized)


def discover_r3d_clips(src: Path) -> List[ClipInfo]:
    if not src.exists():
        raise ContactSheetError(f"Source path does not exist: {src}")
    clips: List[ClipInfo] = []
    for path in sorted(src.rglob("*.R3D")):
        clip_name = path.stem
        reel_label = _derive_reel_label(clip_name)
        clips.append(ClipInfo(source_path=path.resolve(), clip_label=clip_name, reel_label=reel_label))
    if not clips:
        raise ContactSheetError(f"No .R3D clips found under: {src}")
    clips.sort(key=lambda item: _natural_sort_key(item.reel_label))
    return clips


def _derive_reel_label(clip_name: str) -> str:
    parts = clip_name.split("_")
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[1]}"
    return clip_name


def choose_layout(count: int, explicit_layout: Optional[str]) -> Tuple[int, int]:
    if explicit_layout:
        match = re.fullmatch(r"(\d+)x(\d+)", explicit_layout.strip().lower())
        if not match:
            raise ContactSheetError("Layout must look like 4x3, 6x4, or 6x6.")
        return int(match.group(1)), int(match.group(2))
    if count in SUPPORTED_LAYOUTS:
        return SUPPORTED_LAYOUTS[count]
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    return cols, rows


def resolve_redline_executable(explicit_path: Optional[str]) -> str:
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if candidate.exists():
            return str(candidate.resolve())
        resolved = shutil.which(explicit_path)
        if resolved:
            return resolved
        raise ContactSheetError(f"REDline executable not found: {explicit_path}")
    resolved = shutil.which("REDline")
    if resolved:
        return resolved
    raise ContactSheetError("REDline not found in PATH. Use --redline /full/path/to/REDline")


def render_clip_frame(
    *,
    redline_executable: str,
    clip: ClipInfo,
    output_dir: Path,
    frame_index: int,
    jpeg_quality: int,
    scale: str,
    color_space: str,
    gamma_curve: str,
    extra_args: Sequence[str],
) -> RenderedClip:
    output_path = output_dir / f"{clip.clip_label}.jpg"
    command = [
        redline_executable,
        "--input",
        str(clip.source_path),
        "--output",
        str(output_path),
        "--useMeta",
        "--colorScience",
        "ipp2",
        "--colorSpace",
        color_space,
        "--gammaCurve",
        gamma_curve,
        "--format",
        "3",
        "--frame",
        str(frame_index),
    ]
    if scale == "half":
        command.extend(["--resizeX", "960"])
    elif scale == "quarter":
        command.extend(["--resizeX", "640"])
    if jpeg_quality:
        command.extend(["--qt", str(int(jpeg_quality))])
    command.extend(list(extra_args))

    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ContactSheetError(
            f"REDline render failed for {clip.clip_label}.\n"
            f"Command: {' '.join(command)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    if not output_path.exists():
        matches = sorted(output_dir.glob(f"{clip.clip_label}.jpg*"))
        if matches:
            output_path = matches[0]
        else:
            raise ContactSheetError(f"REDline reported success but no image was produced for {clip.clip_label}")
    return RenderedClip(clip=clip, image_path=output_path, frame_index=frame_index)


def build_contact_sheet_pdf(
    *,
    rendered_clips: Sequence[RenderedClip],
    output_pdf: Path,
    title: str,
    requested_frame_text: str,
    columns: int,
    rows: int,
    page_size: Tuple[int, int] = (2550, 3300),
) -> None:
    page_width, page_height = page_size
    margin = 90
    header_height = 180
    gutter_x = 36
    gutter_y = 42
    footer_gap = 26
    tile_label_height = 44

    usable_width = page_width - (margin * 2)
    usable_height = page_height - margin - header_height - margin
    tile_width = int((usable_width - gutter_x * (columns - 1)) / columns)
    tile_height = int((usable_height - gutter_y * (rows - 1)) / rows)
    image_height = tile_height - tile_label_height - footer_gap

    font_title = _load_font(72)
    font_header = _load_font(34)
    font_tile = _load_font(28)

    pages: List[Image.Image] = []
    per_page = columns * rows
    for start in range(0, len(rendered_clips), per_page):
        page = Image.new("RGB", (page_width, page_height), "white")
        draw = ImageDraw.Draw(page)
        draw.text((margin, 52), title, font=font_title, fill="black")
        header_text = f"Frame: {requested_frame_text}"
        header_box = draw.textbbox((0, 0), header_text, font=font_header)
        draw.text((page_width - margin - (header_box[2] - header_box[0]), 78), header_text, font=font_header, fill="black")

        subset = rendered_clips[start:start + per_page]
        for index, rendered in enumerate(subset):
            col = index % columns
            row = index // columns
            x = margin + col * (tile_width + gutter_x)
            y = header_height + row * (tile_height + gutter_y)
            _draw_tile(
                page=page,
                rendered=rendered,
                x=x,
                y=y,
                tile_width=tile_width,
                image_height=image_height,
                tile_label_height=tile_label_height,
                font_tile=font_tile,
            )
        pages.append(page)

    if not pages:
        raise ContactSheetError("No pages were generated.")
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    rgb_pages = [page.convert("RGB") for page in pages]
    rgb_pages[0].save(output_pdf, save_all=True, append_images=rgb_pages[1:], resolution=200.0)


def _draw_tile(
    *,
    page: Image.Image,
    rendered: RenderedClip,
    x: int,
    y: int,
    tile_width: int,
    image_height: int,
    tile_label_height: int,
    font_tile: ImageFont.ImageFont,
) -> None:
    draw = ImageDraw.Draw(page)
    draw.rectangle([x, y, x + tile_width, y + image_height], outline="#B8B8B8", width=2)
    with Image.open(rendered.image_path) as src:
        image = src.convert("RGB")
        fitted = _fit_image(image, tile_width, image_height)
    paste_x = x + (tile_width - fitted.width) // 2
    paste_y = y + (image_height - fitted.height) // 2
    page.paste(fitted, (paste_x, paste_y))

    label = rendered.clip.reel_label
    label_y = y + image_height + 10
    bbox = draw.textbbox((0, 0), label, font=font_tile)
    label_width = bbox[2] - bbox[0]
    draw.text((x + (tile_width - label_width) // 2, label_y), label, font=font_tile, fill="black")


def _fit_image(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
    copy = image.copy()
    copy.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return copy


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a single-PDF R3D array contact sheet.")
    parser.add_argument("--src", required=True, help="Folder containing .R3D clips")
    parser.add_argument("--out", required=True, help="Output PDF path")
    parser.add_argument("--frame", type=int, required=True, help="Absolute frame index to render from every clip")
    parser.add_argument("--tc", default="", help="Requested LTC/timecode label to print in the PDF header")
    parser.add_argument("--title", default="R3D Contact Sheet", help="Title printed at top left")
    parser.add_argument("--layout", default="", help="Optional explicit layout, e.g. 4x3 or 6x6")
    parser.add_argument("--redline", default="", help="Optional REDline executable path")
    parser.add_argument("--scale", choices=["half", "quarter"], default="quarter", help="Render size hint")
    parser.add_argument("--jpeg-quality", type=int, default=82, help="JPEG quality for REDline output")
    parser.add_argument("--color-space", default="rec709", help="REDline color space")
    parser.add_argument("--gamma-curve", default="rec709", help="REDline gamma curve")
    parser.add_argument("--keep-temp", action="store_true", help="Keep rendered JPEGs")
    parser.add_argument("extra_redline_args", nargs="*", help="Extra REDline args appended as-is")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    src = Path(args.src).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()

    clips = discover_r3d_clips(src)
    columns, rows = choose_layout(len(clips), args.layout or None)
    redline_executable = resolve_redline_executable(args.redline or None)

    requested_frame_text = args.tc.strip() or f"frame {args.frame}"

    with tempfile.TemporaryDirectory(prefix="r3d_contact_sheet_") as temp_dir:
        temp_path = Path(temp_dir)
        rendered_dir = temp_path / "renders"
        rendered_dir.mkdir(parents=True, exist_ok=True)
        rendered: List[RenderedClip] = []
        for clip in clips:
            rendered.append(
                render_clip_frame(
                    redline_executable=redline_executable,
                    clip=clip,
                    output_dir=rendered_dir,
                    frame_index=int(args.frame),
                    jpeg_quality=int(args.jpeg_quality),
                    scale=str(args.scale),
                    color_space=str(args.color_space),
                    gamma_curve=str(args.gamma_curve),
                    extra_args=list(args.extra_redline_args),
                )
            )

        build_contact_sheet_pdf(
            rendered_clips=rendered,
            output_pdf=out,
            title=str(args.title),
            requested_frame_text=requested_frame_text,
            columns=columns,
            rows=rows,
        )

        if args.keep_temp:
            kept_dir = out.parent / f"{out.stem}_renders"
            if kept_dir.exists():
                shutil.rmtree(kept_dir)
            shutil.copytree(rendered_dir, kept_dir)
            print(f"Kept rendered JPEGs: {kept_dir}")

    print(f"Wrote contact sheet PDF: {out}")
    print(f"Clips: {len(clips)} | Layout: {columns}x{rows} | Frame: {args.frame}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContactSheetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
