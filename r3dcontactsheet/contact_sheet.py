"""Build readable multi-page contact sheet PDFs from rendered stills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont


PDF_SIZE = (3300, 2550)
PAGE_MARGIN = 100
HEADER_HEIGHT = 260
FOOTER_HEIGHT = 50
CELL_GAP_X = 42
CELL_GAP_Y = 18
CAPTION_HEIGHT = 132
GRID_COLUMNS = 3
GRID_ROWS = 4
ITEMS_PER_PAGE = GRID_COLUMNS * GRID_ROWS
PAGE_BG = "#202226"
CARD_BG = "#2B2E34"
CARD_STROKE = "#454952"
TEXT_PRIMARY = "#F3F5F7"
TEXT_SECONDARY = "#C7CCD3"


@dataclass(frozen=True)
class ContactSheetItem:
    image_path: Path
    clip_label: str
    group_label: str
    frame_label: str
    timecode_label: str
    fps_label: str = ""
    resolution_label: str = ""
    sync_label: str = ""


def build_contact_sheet_pdf(
    items: Sequence[ContactSheetItem],
    destination: Path,
    title: str,
    header_lines: Sequence[str] = (),
    logo_path: Path | None = None,
) -> Path:
    if not items:
        raise ValueError("No rendered stills were available for contact sheet generation.")

    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    pages = []
    total_pages = (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    for page_index in range(total_pages):
        page_items = items[page_index * ITEMS_PER_PAGE : (page_index + 1) * ITEMS_PER_PAGE]
        page = _build_page(
            page_items,
            title=title,
            header_lines=header_lines,
            page_number=page_index + 1,
            total_pages=total_pages,
            logo_path=logo_path,
        )
        pages.append(page.convert("RGB"))
    first, rest = pages[0], pages[1:]
    first.save(destination, "PDF", resolution=300.0, save_all=True, append_images=rest)
    return destination


def _build_page(
    items: Sequence[ContactSheetItem],
    *,
    title: str,
    header_lines: Sequence[str],
    page_number: int,
    total_pages: int,
    logo_path: Path | None,
) -> Image.Image:
    page = Image.new("RGB", PDF_SIZE, color=PAGE_BG)
    draw = ImageDraw.Draw(page)
    title_font = _load_font(46, bold=True)
    section_font = _load_font(30, bold=True)
    body_font = _load_font(30)
    small_font = _load_font(25)
    status_font = _load_font(28, bold=True)

    content_x = PAGE_MARGIN
    if logo_path is not None and logo_path.exists():
        content_x = _paste_header_logo(page, logo_path)
    draw.text((content_x, 34), title, fill=TEXT_PRIMARY, font=title_font)
    y = 96
    for line in header_lines:
        draw.text((content_x, y), line, fill=TEXT_SECONDARY if y > 96 else TEXT_PRIMARY, font=section_font if y == 96 else body_font)
        y += 36
    draw.text(
        (PDF_SIZE[0] - PAGE_MARGIN - 200, 34),
        f"Page {page_number}/{total_pages}",
        fill=TEXT_PRIMARY,
        font=section_font,
    )

    usable_width = PDF_SIZE[0] - (PAGE_MARGIN * 2) - (CELL_GAP_X * (GRID_COLUMNS - 1))
    usable_height = PDF_SIZE[1] - HEADER_HEIGHT - FOOTER_HEIGHT - (PAGE_MARGIN * 0) - (CELL_GAP_Y * (GRID_ROWS - 1))
    cell_width = usable_width // GRID_COLUMNS
    cell_height = usable_height // GRID_ROWS
    image_height = max(220, cell_height - CAPTION_HEIGHT - 4)

    for index, item in enumerate(items):
        row = index // GRID_COLUMNS
        column = index % GRID_COLUMNS
        x = PAGE_MARGIN + column * (cell_width + CELL_GAP_X)
        y = HEADER_HEIGHT + row * (cell_height + CELL_GAP_Y)
        draw.rounded_rectangle(
            (x, y, x + cell_width, y + cell_height),
            radius=18,
            outline=CARD_STROKE,
            width=2,
            fill=CARD_BG,
        )
        image_box = (x + 14, y + 14, x + cell_width - 14, y + image_height)
        _paste_cover(page, item.image_path, image_box)
        caption_y = y + image_height + 6
        _draw_centered_text(draw, (x + cell_width / 2, caption_y), item.clip_label, section_font, TEXT_PRIMARY)
        _draw_centered_text(draw, (x + cell_width / 2, caption_y + 34), item.timecode_label, body_font, TEXT_PRIMARY)
        _draw_centered_text(draw, (x + cell_width / 2, caption_y + 66), item.frame_label, small_font, TEXT_SECONDARY)
        details = " • ".join(filter(None, [item.fps_label, item.resolution_label]))
        _draw_centered_text(draw, (x + cell_width / 2, caption_y + 94), details, small_font, TEXT_SECONDARY)
        _draw_centered_text(draw, (x + cell_width / 2, caption_y + 120), item.sync_label, status_font, _status_fill(item.sync_label))

    return page


def _paste_cover(page: Image.Image, image_path: Path, box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    target_width = right - left
    target_height = bottom - top
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        scale = max(target_width / image.width, target_height / image.height)
        resized = image.resize((int(image.width * scale), int(image.height * scale)))
        crop_left = max(0, (resized.width - target_width) // 2)
        crop_top = max(0, (resized.height - target_height) // 2)
        cropped = resized.crop((crop_left, crop_top, crop_left + target_width, crop_top + target_height))
        page.paste(cropped, (left, top))


def _paste_header_logo(page: Image.Image, image_path: Path) -> int:
    with Image.open(image_path) as image:
        image = image.convert("RGBA")
        target_height = 118
        scale = target_height / image.height
        target_width = max(220, int(image.width * scale))
        resized = image.resize((target_width, target_height))
        page.paste(resized, (PAGE_MARGIN, 20), resized)
    return PAGE_MARGIN + target_width + 24


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    draw.text((center[0] - width / 2, center[1]), text, fill=fill, font=font)


def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _status_fill(status: str) -> str:
    if status == "Exact match":
        return "#67D36F"
    if status == "Out Of Frame Sync":
        return "#E46666"
    if status == "Nearest available":
        return "#E5B94B"
    return TEXT_PRIMARY
