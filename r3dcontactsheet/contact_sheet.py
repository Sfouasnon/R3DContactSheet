"""Build readable themed multi-page contact sheet PDFs from rendered stills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont


PDF_SIZE = (3300, 2550)
PAGE_MARGIN = 90
HEADER_HEIGHT = 230
FOOTER_HEIGHT = 50
CELL_GAP_X = 34
CELL_GAP_Y = 20
CARD_HEIGHT = 470
CAPTION_HEIGHT = 116
GRID_COLUMNS = 3
SECTION_GAP = 18
GROUP_HEADER_HEIGHT = 52
SUBGROUP_HEADER_HEIGHT = 34


@dataclass(frozen=True)
class SheetTheme:
    page_bg: str
    card_bg: str
    card_stroke: str
    text_primary: str
    text_secondary: str
    header_fill: str
    subgroup_fill: str
    accent: str
    exact_fill: str
    warning_fill: str


THEMES = {
    "dark": SheetTheme(
        page_bg="#1F2228",
        card_bg="#2B2F37",
        card_stroke="#4C525D",
        text_primary="#F2F4F7",
        text_secondary="#C8CDD5",
        header_fill="#E8EBF0",
        subgroup_fill="#D1D6DE",
        accent="#6E9CEB",
        exact_fill="#67D36F",
        warning_fill="#E46666",
    ),
    "light": SheetTheme(
        page_bg="#F0F1F4",
        card_bg="#FBFBFD",
        card_stroke="#C9CDD6",
        text_primary="#1E2430",
        text_secondary="#536072",
        header_fill="#202634",
        subgroup_fill="#405068",
        accent="#476EBA",
        exact_fill="#3BAA47",
        warning_fill="#C25151",
    ),
}


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
    subgroup_label: str = ""


def build_contact_sheet_pdf(
    items: Sequence[ContactSheetItem],
    destination: Path,
    title: str,
    header_lines: Sequence[str] = (),
    *,
    theme_name: str = "dark",
) -> Path:
    if not items:
        raise ValueError("No rendered stills were available for contact sheet generation.")

    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    theme = THEMES.get(theme_name, THEMES["dark"])
    pages = _build_pages(items, title=title, header_lines=header_lines, theme=theme)
    total_pages = len(pages)
    rendered_pages = [
        _render_page(page_items, title=title, header_lines=header_lines, theme=theme, page_number=index + 1, total_pages=total_pages)
        for index, page_items in enumerate(pages)
    ]
    first, rest = rendered_pages[0], rendered_pages[1:]
    first.save(destination, "PDF", resolution=300.0, save_all=True, append_images=rest)
    return destination


def _build_pages(
    items: Sequence[ContactSheetItem],
    *,
    title: str,
    header_lines: Sequence[str],
    theme: SheetTheme,
) -> list[list[dict]]:
    usable_width = PDF_SIZE[0] - (PAGE_MARGIN * 2) - (CELL_GAP_X * (GRID_COLUMNS - 1))
    cell_width = usable_width // GRID_COLUMNS
    content_top = HEADER_HEIGHT
    content_bottom = PDF_SIZE[1] - PAGE_MARGIN - FOOTER_HEIGHT
    max_y = content_bottom

    pages: list[list[dict]] = [[]]
    current_y = content_top
    current_group = None
    current_subgroup = None
    row_index = 0
    column_index = 0

    for item in items:
        group_label = item.group_label or "Uncategorized"
        subgroup_label = item.subgroup_label or ""
        if current_group != group_label:
            if column_index != 0:
                current_y += CARD_HEIGHT + CELL_GAP_Y
                column_index = 0
                row_index = 0
            current_group = group_label
            current_subgroup = None
            if current_y + GROUP_HEADER_HEIGHT > max_y:
                pages.append([])
                current_y = content_top
                row_index = 0
                column_index = 0
            pages[-1].append({"type": "group", "label": group_label, "y": current_y})
            current_y += GROUP_HEADER_HEIGHT
            row_index = 0
            column_index = 0
        if subgroup_label and current_subgroup != subgroup_label:
            if column_index != 0:
                current_y += CARD_HEIGHT + CELL_GAP_Y
                column_index = 0
                row_index = 0
            current_subgroup = subgroup_label
            if current_y + SUBGROUP_HEADER_HEIGHT > max_y:
                pages.append([])
                current_y = content_top
                row_index = 0
                column_index = 0
            pages[-1].append({"type": "subgroup", "label": subgroup_label, "y": current_y})
            current_y += SUBGROUP_HEADER_HEIGHT
            row_index = 0
            column_index = 0
        if column_index == 0 and current_y + CARD_HEIGHT > max_y:
            pages.append([])
            current_y = content_top
            row_index = 0
            column_index = 0
            pages[-1].append({"type": "group", "label": group_label, "y": current_y})
            current_y += GROUP_HEADER_HEIGHT
            if subgroup_label:
                pages[-1].append({"type": "subgroup", "label": subgroup_label, "y": current_y})
                current_y += SUBGROUP_HEADER_HEIGHT
        x = PAGE_MARGIN + column_index * (cell_width + CELL_GAP_X)
        y = current_y + row_index * (CARD_HEIGHT + CELL_GAP_Y)
        pages[-1].append({"type": "item", "item": item, "x": x, "y": y, "width": cell_width, "height": CARD_HEIGHT})
        column_index += 1
        if column_index >= GRID_COLUMNS:
            column_index = 0
            row_index += 1
            current_y = y + CARD_HEIGHT + CELL_GAP_Y
            row_index = 0
    return pages


def _render_page(
    commands: Sequence[dict],
    *,
    title: str,
    header_lines: Sequence[str],
    theme: SheetTheme,
    page_number: int,
    total_pages: int,
) -> Image.Image:
    page = Image.new("RGB", PDF_SIZE, color=theme.page_bg)
    draw = ImageDraw.Draw(page)
    title_font = _load_font(46, bold=True)
    section_font = _load_font(34, bold=True)
    subgroup_font = _load_font(24, bold=True)
    body_font = _load_font(28)
    page_font = _load_font(26, bold=True)

    draw.text((PAGE_MARGIN, 30), title, fill=theme.header_fill, font=title_font)
    y = 92
    for index, line in enumerate(header_lines):
        font = section_font if index == 0 else body_font
        fill = theme.text_primary if index == 0 else theme.text_secondary
        draw.text((PAGE_MARGIN, y), line, fill=fill, font=font)
        y += 34
    draw.text(
        (PDF_SIZE[0] - PAGE_MARGIN - 190, 32),
        f"Page {page_number}/{total_pages}",
        fill=theme.text_primary,
        font=page_font,
    )

    for command in commands:
        if command["type"] == "group":
            draw.text((PAGE_MARGIN, command["y"]), command["label"], fill=theme.header_fill, font=section_font)
        elif command["type"] == "subgroup":
            draw.text((PAGE_MARGIN + 14, command["y"]), command["label"], fill=theme.subgroup_fill, font=subgroup_font)
        else:
            _draw_card(
                page,
                draw,
                command["item"],
                x=command["x"],
                y=command["y"],
                width=command["width"],
                height=command["height"],
                theme=theme,
            )
    return page.convert("RGB")


def _draw_card(
    page: Image.Image,
    draw: ImageDraw.ImageDraw,
    item: ContactSheetItem,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    theme: SheetTheme,
) -> None:
    section_font = _load_font(28, bold=True)
    body_font = _load_font(25)
    small_font = _load_font(22)
    status_font = _load_font(27, bold=True)

    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=18,
        outline=theme.card_stroke,
        width=2,
        fill=theme.card_bg,
    )
    image_height = height - CAPTION_HEIGHT - 18
    image_box = (x + 12, y + 12, x + width - 12, y + image_height)
    _paste_cover(page, item.image_path, image_box)

    caption_y = y + image_height + 10
    _draw_centered_text(draw, (x + width / 2, caption_y), item.clip_label, section_font, theme.text_primary)
    _draw_centered_text(draw, (x + width / 2, caption_y + 30), item.timecode_label, body_font, theme.text_primary)
    _draw_centered_text(draw, (x + width / 2, caption_y + 58), item.frame_label, body_font, theme.text_secondary)
    details = " • ".join(filter(None, [item.fps_label, item.resolution_label]))
    _draw_centered_text(draw, (x + width / 2, caption_y + 86), details, small_font, theme.text_secondary)
    if item.sync_label:
        _draw_centered_text(draw, (x + width / 2, caption_y + 110), item.sync_label, status_font, _status_fill(item.sync_label, theme))


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


def _status_fill(status: str, theme: SheetTheme) -> str:
    if status == "Exact match":
        return theme.exact_fill
    if status == "Out Of Frame Sync":
        return theme.warning_fill
    if status == "Nearest available":
        return theme.accent
    return theme.text_primary
