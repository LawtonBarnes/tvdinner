"""Static EPG grid renderer -- Phase 2. Draws onto the pre-built background
(which already contains the TV DINNER logo, header row coloring, grid lines,
and the static NOW/NEXT/LATER labels) using real pixel-measured text fitting.

Run directly for an offline render (no live display needed) to check the
result against the mockup before this ever touches a real Pi's framebuffer.
"""

import os
import pygame

import layout
from parsing import format_channel_number, format_duration
from text_layout import truncate_hard, wrap_on_space

WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
CLOCK_COLOR = (255, 255, 255)


def draw_text_centered(surface, font, text, color, center_x, y):
    if not text:
        return
    rendered = font.render(text, True, color)
    rect = rendered.get_rect(midtop=(center_x, y))
    surface.blit(rendered, rect)


def draw_text_left(surface, font, text, color, x, y):
    if not text:
        return
    rendered = font.render(text, True, color)
    surface.blit(rendered, (x, y))


def draw_clock(surface, font, clock_text):
    x, y, w, h = layout.CLOCK_RECT
    draw_text_centered(surface, font, clock_text, CLOCK_COLOR, x + w // 2, y + (h - font.get_height()) // 2)


def draw_channel_column(surface, font, channels):
    """channels: list of 4 dicts (top-to-bottom) with 'number'/'callsign', for VISIBLE_CHANNEL_ROWS."""
    for row, channel in zip(layout.VISIBLE_CHANNEL_ROWS, channels):
        x, y, w, h = layout.cell_rect("A", row)
        number_text = format_channel_number(channel["number"])
        callsign_text = channel["callsign"]
        line_h = font.get_height()
        total_h = line_h * 2
        start_y = y + (h - total_h) // 2
        draw_text_centered(surface, font, number_text, YELLOW, x + w // 2, start_y)
        draw_text_centered(surface, font, callsign_text, YELLOW, x + w // 2, start_y + line_h)


def draw_program_grid(surface, font, channels):
    """channels: same 4 dicts, each with 'programs': list of up to 3 dicts
    (artist/title) for VISIBLE_PROGRAM_COLS, in order."""
    for row, channel in zip(layout.VISIBLE_CHANNEL_ROWS, channels):
        for col, program in zip(layout.VISIBLE_PROGRAM_COLS, channel["programs"]):
            x, y, w, h = layout.cell_rect(col, row)
            max_text_width = w - 2 * layout.CELL_TEXT_PADDING_X
            artist_line = truncate_hard(font, program["artist"], max_text_width)
            title_line = truncate_hard(font, program["title"], max_text_width)
            text_x = x + layout.CELL_TEXT_PADDING_X
            text_y = y + layout.CELL_TEXT_PADDING_Y
            draw_text_left(surface, font, artist_line, WHITE, text_x, text_y)
            draw_text_left(surface, font, title_line, WHITE, text_x, text_y + font.get_height())


def draw_highlight(surface, col, row):
    if row not in layout.HIGHLIGHTABLE_ROWS:
        raise ValueError(f"row {row} is outside the highlightable range {layout.HIGHLIGHTABLE_ROWS}")
    x, y, w, h = layout.cell_rect(col, row)
    inset = layout.HIGHLIGHT_INSET
    rect = pygame.Rect(x + inset, y + inset, w - 2 * inset, h - 2 * inset)
    pygame.draw.rect(surface, layout.HIGHLIGHT_COLOR, rect, layout.HIGHLIGHT_BORDER_WIDTH)


def draw_main_title(surface, font, artist, title, duration_seconds):
    zone_x, zone_y, zone_w, zone_h = layout.TITLE_ZONE
    center_x = zone_x + zone_w // 2
    line_h = font.get_height()

    artist_line = truncate_hard(font, artist, zone_w)
    title_lines = wrap_on_space(font, title, zone_w, max_lines=2)
    duration_line = format_duration(duration_seconds)

    lines = [artist_line] + title_lines + [duration_line]
    for i, line in enumerate(lines):
        draw_text_centered(surface, font, line, WHITE, center_x, zone_y + i * line_h)


def draw_thumbnail(surface, thumbnail_surface):
    box_x, box_y, box_w, box_h = layout.THUMBNAIL_BOX
    thumb_w, thumb_h = layout.THUMBNAIL_SIZE
    dest_x = box_x + (box_w - thumb_w) // 2
    dest_y = box_y + (box_h - thumb_h) // 2
    scaled = pygame.transform.smoothscale(thumbnail_surface, (thumb_w, thumb_h))
    surface.blit(scaled, (dest_x, dest_y))


def render_guide(surface, font, background, data, highlight):
    surface.blit(background, (0, 0))
    draw_clock(surface, font, data["clock"])
    draw_channel_column(surface, font, data["channels"])
    draw_program_grid(surface, font, data["channels"])
    draw_main_title(surface, font, data["now_playing"]["artist"], data["now_playing"]["title"],
                     data["now_playing"]["duration_seconds"])
    draw_thumbnail(surface, data["thumbnail_surface"])
    draw_highlight(surface, *highlight)


if __name__ == "__main__":
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    pygame.display.set_mode(layout.CANVAS_SIZE)  # dummy driver -- no real window, just satisfies convert()
    ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")

    font = pygame.font.Font(os.path.join(ASSET_DIR, layout.FONT_PATH_GUIDE), layout.GUIDE_FONT_SIZE)
    background = pygame.image.load(os.path.join(ASSET_DIR, "TV_DINNER_BACKGROUND_BLANK.png")).convert()
    thumbnail = pygame.image.load(os.path.join(ASSET_DIR, "sample_thumbnail.jpg")).convert()

    surface = pygame.Surface(layout.CANVAS_SIZE)

    sample_data = {
        "clock": "12:45",
        "now_playing": {
            "artist": "Def Leppard",
            "title": "Have You Ever Needed Someone So Bad",
            "duration_seconds": 225,
        },
        "thumbnail_surface": thumbnail,
        "channels": [
            {"number": 22, "callsign": "BUTT", "programs": [
                {"artist": "House Of Lords", "title": "I Wanna Be Loved"},
                {"artist": "Flotsam And Jetsam", "title": "Saturday Nights Alright For Fighting"},
                {"artist": "Dokken", "title": "Walk Away"},
            ]},
            {"number": 23, "callsign": "VILE", "programs": [
                {"artist": "WASP", "title": "Arena Of Pleasure"},
                {"artist": "Led Zeppelin", "title": "Rock And Roll"},
                {"artist": "Trixter", "title": "Surrender"},
            ]},
            {"number": 24, "callsign": "LAME", "programs": [
                {"artist": "Scorpions", "title": "Dont Believe Her"},
                {"artist": "Tesla", "title": "What You Give"},
                {"artist": "Def Leppard", "title": "Have You Ever Needed Someone So Bad"},
            ]},
            {"number": 25, "callsign": "ICKY", "programs": [
                {"artist": "Bonfire", "title": "Sweet Obsession"},
                {"artist": "Ugly Kid Joe", "title": "Under The Bottle"},
                {"artist": "Aerosmith", "title": "Angel"},
            ]},
        ],
    }

    render_guide(surface, font, background, sample_data, highlight=("B", 2))

    out_path = os.path.join(os.path.dirname(__file__), "render_output.png")
    pygame.image.save(surface, out_path)
    print(f"Saved {out_path}")
