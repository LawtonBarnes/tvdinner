"""Phase 3 visual proof: real channel/filename data (scanned directly, no
duration/thumbnail lookup -- those were already verified in Phase 2 and the
full-metadata indexer is still building) driven through the real
NavigationState, stepping through the user's own example walkthrough and
saving a PNG after each step.
"""

import os
import pygame

import layout
import guide_render
from navigation import NavigationState
from parsing import parse_channel_folder, parse_media_filename

MEDIA_ROOT = "/mnt/tvdinner"
OUT_DIR = os.path.join(os.path.dirname(__file__), "nav_demo_frames")


def scan_channels_lightweight(media_root):
    channels = []
    for entry in sorted(os.scandir(media_root), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        try:
            number, callsign = parse_channel_folder(entry.name)
        except ValueError:
            continue
        files = [f for f in os.scandir(entry.path) if f.is_file() and f.name.lower().endswith(".mp4")]
        files.sort(key=lambda f: f.stat().st_mtime)
        programs = []
        for f in files:
            try:
                artist, title = parse_media_filename(f.name)
            except ValueError:
                continue
            programs.append({"artist": artist, "title": title, "duration_seconds": None})
        if programs:
            channels.append({"number": number, "callsign": callsign, "programs": programs})
    channels.sort(key=lambda c: c["number"])
    return channels


def build_render_data(nav, clock_text, thumbnail_surface):
    focused = nav.focused_program()
    return {
        "clock": clock_text,
        "now_playing": {
            "artist": focused["artist"],
            "title": focused["title"],
            "duration_seconds": focused["duration_seconds"] or 0,
        },
        "thumbnail_surface": thumbnail_surface,
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

    channels = scan_channels_lightweight(MEDIA_ROOT)
    print(f"Scanned {len(channels)} channels from real media, "
          f"{sum(len(c['programs']) for c in channels)} programs total.")

    nav = NavigationState(channels)
    os.makedirs(OUT_DIR, exist_ok=True)

    steps = [
        ("00_initial", None),
        ("01_down", nav.handle_down),
        ("02_down", nav.handle_down),
        ("03_down_scrolls", nav.handle_down),
        ("04_right", nav.handle_right),
        ("05_right", nav.handle_right),
        ("06_right_scrolls", nav.handle_right),
    ]

    surface = pygame.Surface(layout.CANVAS_SIZE)
    for label, action in steps:
        if action:
            action()
        data = build_render_data(nav, "12:00", no_thumb)
        guide_render.render_guide(surface, font, background, data, highlight=nav.highlight_position())
        out_path = os.path.join(OUT_DIR, f"{label}.png")
        pygame.image.save(surface, out_path)
        col, row = nav.highlight_position()
        print(f"{label}: highlight=({col},{row}) scroll_offset={nav.channel_scroll_offset} -> {out_path}")
