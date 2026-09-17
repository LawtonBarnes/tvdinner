"""Full real-data integration check: real index.json (durations + thumbnail
paths from the completed indexer run), real NavigationState, real per-program
thumbnails loaded on demand (only the focused one, same as the live app will
do)."""

import datetime
import os
import pygame

import layout
import guide_render
from navigation import NavigationState
from index_loader import load_index, thumbnail_path
from parsing import format_duration, format_clock_12h

CACHE_DIR = "/opt/tvdinner-cache"
INDEX_PATH = os.path.join(CACHE_DIR, "index.json")


def build_render_data(nav, clock_text, no_thumb_surface):
    focused = nav.focused_program()
    thumb_path = thumbnail_path(CACHE_DIR, focused)
    if thumb_path:
        thumb_surface = pygame.image.load(thumb_path).convert()
    else:
        thumb_surface = no_thumb_surface
    return {
        "clock": clock_text,
        "now_playing": {
            "artist": focused["artist"],
            "title": focused["title"],
            "duration_seconds": focused["duration_seconds"] or 0,
        },
        "thumbnail_surface": thumb_surface,
        "channels": nav.visible_channels(),
    }


if __name__ == "__main__":
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    pygame.display.set_mode(layout.CANVAS_SIZE)

    ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")
    font = pygame.font.Font(os.path.join(ASSET_DIR, layout.FONT_PATH_GUIDE), layout.GUIDE_FONT_SIZE)
    background = pygame.image.load(os.path.join(ASSET_DIR, "TV_DINNER_BACKGROUND_BLANK.png")).convert()
    no_thumb = pygame.image.load(os.path.join(ASSET_DIR, "TV_DINNER_NO_THUMBNAIL.png")).convert()

    channels = load_index(INDEX_PATH)
    total_programs = sum(len(c["programs"]) for c in channels)
    print(f"Loaded real index: {len(channels)} channels, {total_programs} programs.")

    nav = NavigationState(channels)
    surface = pygame.Surface(layout.CANVAS_SIZE)

    focused = nav.focused_program()
    print(f"Focused: {focused['artist']} - {focused['title']} "
          f"({format_duration(focused['duration_seconds'])})")

    clock_text = format_clock_12h(datetime.datetime.now())
    data = build_render_data(nav, clock_text, no_thumb)
    guide_render.render_guide(surface, font, background, data, highlight=nav.highlight_position())
    out_path = os.path.join(os.path.dirname(__file__), "real_index_render.png")
    pygame.image.save(surface, out_path)
    print(f"Saved {out_path}")
