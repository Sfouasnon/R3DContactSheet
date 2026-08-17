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


@dataclass(frozen=True)
class LayoutSpec:
    columns: int
    card_height: int
    caption_height: int
    cell_gap_x: int = CELL_GAP_X
    cell_gap_y: int = CELL_GAP_Y


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
    layout = _layout_spec(len(items))
    usable_width = PDF_SIZE[0] - (PAGE_MARGIN * 2) - (layout.cell_gap_x * (layout.columns - 1))
    cell_width = usable_width // layout.columns
    content_top = HEADER_HEIGHT
    content_bottom = PDF_SIZE[1] - PAGE_MARGIN - FOOTER_HEIGHT
    max_y = content_bottom

    pages: list[list[dict]] = [[]]
    current_y = content_top
    current_group: str | None = None
    current_subgroup: str | None = None
    column_index = 0

    def start_page(group_label: str, subgroup_label: str) -> None:
        nonlocal current_y, column_index
        pages.append([])
        current_y = content_top
        column_index = 0
        pages[-1].append({"type": "group", "label": group_label, "y": current_y})
        current_y += GROUP_HEADER_HEIGHT
        if subgroup_label:
            pages[-1].append({"type": "subgroup", "label": subgroup_label, "y": current_y})
            current_y += SUBGROUP_HEADER_HEIGHT

    for item in items:
        group_label = item.group_label or "Uncategorized"
        subgroup_label = item.subgroup_label or ""
        group_changed = current_group != group_label
        subgroup_changed = group_changed or current_subgroup != subgroup_label

        if group_changed or subgroup_changed:
            if column_index != 0:
                current_y += layout.card_height + layout.cell_gap_y
                column_index = 0
            headers_height = (GROUP_HEADER_HEIGHT if group_changed else 0) + (SUBGROUP_HEADER_HEIGHT if subgroup_label and subgroup_changed else 0)
            if current_y + headers_height + layout.card_height > max_y and pages[-1]:
                start_page(group_label, subgroup_label)
                current_group = group_label
                current_subgroup = subgroup_label
            else:
                if group_changed:
                    pages[-1].append({"type": "group", "label": group_label, "y": current_y})
                    current_y += GROUP_HEADER_HEIGHT
                if subgroup_label and subgroup_changed:
                    pages[-1].append({"type": "subgroup", "label": subgroup_label, "y": current_y})
                    current_y += SUBGROUP_HEADER_HEIGHT
                current_group = group_label
                current_subgroup = subgroup_label

        if column_index == 0 and current_y + layout.card_height > max_y:
            start_page(group_label, subgroup_label)
            current_subgroup = subgroup_label
            current_group = group_label
        x = PAGE_MARGIN + column_index * (cell_width + layout.cell_gap_x)
        y = current_y
        pages[-1].append(
            {
                "type": "item",
                "item": item,
                "x": x,
                "y": y,
                "width": cell_width,
                "height": layout.card_height,
                "caption_height": layout.caption_height,
            }
        )
        column_index += 1
        if column_index >= layout.columns:
            column_index = 0
            current_y = y + layout.card_height + layout.cell_gap_y
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

    _draw_fitted_text(draw, (PAGE_MARGIN, 30), title, font_size=46, bold=True, fill=theme.header_fill, max_width=PDF_SIZE[0] - (PAGE_MARGIN * 2) - 240)
    y = 92
    for index, line in enumerate(header_lines):
        font_size = 34 if index == 0 else 28
        fill = theme.text_primary if index == 0 else theme.text_secondary
        _draw_fitted_text(draw, (PAGE_MARGIN, y), line, font_size=font_size, bold=index == 0, fill=fill, max_width=PDF_SIZE[0] - (PAGE_MARGIN * 2))
        y += 34
    draw.text(
        (PDF_SIZE[0] - PAGE_MARGIN - 190, 32),
        f"Page {page_number}/{total_pages}",
        fill=theme.text_primary,
        font=page_font,
    )

    for command in commands:
        if command["type"] == "group":
            _draw_fitted_text(draw, (PAGE_MARGIN, command["y"]), command["label"], font_size=34, bold=True, fill=theme.header_fill, max_width=PDF_SIZE[0] - (PAGE_MARGIN * 2))
        elif command["type"] == "subgroup":
            _draw_fitted_text(draw, (PAGE_MARGIN + 14, command["y"]), command["label"], font_size=24, bold=True, fill=theme.subgroup_fill, max_width=PDF_SIZE[0] - (PAGE_MARGIN * 2) - 14)
        else:
            _draw_card(
                page,
                draw,
                command["item"],
                x=command["x"],
                y=command["y"],
                width=command["width"],
                height=command["height"],
                caption_height=command["caption_height"],
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
    caption_height: int,
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
    image_height = height - caption_height - 18
    image_box = (x + 12, y + 12, x + width - 12, y + image_height)
    _paste_cover(page, item.image_path, image_box, card_bg=theme.card_bg)

    caption_y = y + image_height + 10
    text_width = width - 36
    _draw_centered_fitted_text(draw, (x + width / 2, caption_y), item.clip_label, 28, True, theme.text_primary, text_width)
    _draw_centered_fitted_text(draw, (x + width / 2, caption_y + 30), item.timecode_label, 25, False, theme.text_primary, text_width)
    _draw_centered_fitted_text(draw, (x + width / 2, caption_y + 58), item.frame_label, 25, False, theme.text_secondary, text_width)
    details = " • ".join(filter(None, [item.fps_label, item.resolution_label]))
    _draw_centered_fitted_text(draw, (x + width / 2, caption_y + 86), details, 22, False, theme.text_secondary, text_width)
    if item.sync_label:
        _draw_centered_fitted_text(draw, (x + width / 2, caption_y + 110), item.sync_label, 24, True, _status_fill(item.sync_label, theme), text_width)


def _layout_spec(item_count: int) -> LayoutSpec:
    if item_count <= 1:
        return LayoutSpec(columns=1, card_height=760, caption_height=160)
    if item_count == 2:
        return LayoutSpec(columns=2, card_height=760, caption_height=160)
    if item_count <= 4:
        return LayoutSpec(columns=2, card_height=640, caption_height=145)
    if item_count <= 6:
        return LayoutSpec(columns=3, card_height=545, caption_height=140)
    return LayoutSpec(columns=3, card_height=470, caption_height=136)


def _paste_cover(page: Image.Image, image_path: Path, box: tuple[int, int, int, int], card_bg: str = "#2B2F37") -> None:
    left, top, right, bottom = box
    target_width = right - left
    target_height = bottom - top
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        # Scale to fit entirely within box (no cropping), letterbox with card background
        scale = min(target_width / image.width, target_height / image.height)
        fitted_w = int(image.width * scale)
        fitted_h = int(image.height * scale)
        resized = image.resize((fitted_w, fitted_h), Image.LANCZOS)
        # Fill box with card background first, then paste centred image on top
        bg = Image.new("RGB", (target_width, target_height), color=card_bg)
        paste_x = (target_width - fitted_w) // 2
        paste_y = (target_height - fitted_h) // 2
        bg.paste(resized, (paste_x, paste_y))
        page.paste(bg, (left, top))


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


def _draw_fitted_text(
    draw: ImageDraw.ImageDraw,
    origin: tuple[float, float],
    text: str,
    *,
    font_size: int,
    bold: bool,
    fill: str,
    max_width: int,
    min_size: int = 16,
) -> None:
    fitted, font = _fit_text(draw, text, font_size=font_size, bold=bold, max_width=max_width, min_size=min_size)
    draw.text(origin, fitted, fill=fill, font=font)


def _draw_centered_fitted_text(
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    text: str,
    font_size: int,
    bold: bool,
    fill: str,
    max_width: int,
) -> None:
    fitted, font = _fit_text(draw, text, font_size=font_size, bold=bold, max_width=max_width, min_size=16)
    _draw_centered_text(draw, center, fitted, font, fill)


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font_size: int,
    bold: bool,
    max_width: int,
    min_size: int,
) -> tuple[str, ImageFont.ImageFont]:
    value = str(text)
    for size in range(font_size, min_size - 1, -1):
        font = _load_font(size, bold=bold)
        if draw.textlength(value, font=font) <= max_width:
            return value, font
    font = _load_font(min_size, bold=bold)
    ellipsis = "..."
    while value and draw.textlength(value + ellipsis, font=font) > max_width:
        value = value[:-1]
    return value.rstrip() + ellipsis, font


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
