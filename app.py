#!/usr/bin/env python3
"""TV DINNER main entry point -- ties the guide (Phases 1-3) and playback
(Phase 4) together behind one real evdev-driven event loop, matching the
fleet's established pattern (see bars.py): pygame runs fully headless
(SDL_VIDEODRIVER=dummy) purely to build surfaces/render fonts, the guide
draws to /dev/fb0 directly (framebuffer.py), and mpv (player.py) takes
real DRM master only while a video is actually playing -- the two never
run at once.
"""

import configparser
import datetime
import os
import selectors
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import evdev
from evdev import ecodes
import pygame

import guide_render
import layout
from framebuffer import FrameBuffer, enter_console_graphics_mode, restore_console_text_mode
from index_loader import load_index, thumbnail_path
from mpv_control import MpvController
from navigation import NavigationState
from overlay import OverlayManager, OverlayRenderer
from parsing import format_clock_12h, format_duration
from player import PlaybackController
from ascii_video import AsciiVideo, font_path

VERSION = "1.3"

MEDIA_ROOT = "/mnt/tvdinner"
CACHE_DIR = "/opt/tvdinner-cache"
INDEX_PATH = os.path.join(CACHE_DIR, "index.json")
ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.ini")
GUIDE_SELECT_TIMEOUT_S = 1.0
PLAYBACK_SELECT_TIMEOUT_S = 0.2
# ASCII mode draws its own frames, so the loop has to come around far more
# often than the 0.2s that's plenty for overlay timeouts.
ASCII_SELECT_TIMEOUT_S = 0.005

OK_CODES = (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE)
BACK_CODES = (ecodes.KEY_Q, ecodes.KEY_ESC, ecodes.KEY_BACK, ecodes.KEY_COMPOSE)
HOME_CODES = (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME)

LOGO_PATH = os.path.join(ASSET_DIR, "metalshop-logo.png")  # ABOUT screen -- see draw_about_screen()
BLACK = (0, 0, 0)
WHITE = (0xFF, 0xFF, 0xFF)
ORANGE = (0xFF, 0xA5, 0x00)

# ABOUT screen (2026-09-22, fleet-wide -- same layout in every app except
# WX, duplicated per this codebase's no-shared-library convention). BACK
# toggles it from the app's home level. Title top center, settings in the
# middle, METAL SHOP logo bottom center. The logo is square-pixel art, so
# it's stretched horizontally by 720/640 to look right on the CRT's
# narrower-than-square 720x480 pixels.
ABOUT_MARGIN_Y = 48  # keeps title/logo inside the CRT's visible area
ABOUT_LINE_GAP = 8
ABOUT_COLUMN_GAP = 16
LOGO_X_STRETCH = 720 / 640


def load_about_logo():
    try:
        img = pygame.image.load(LOGO_PATH).convert_alpha()
    except (pygame.error, OSError) as exc:
        print(f"Logo load failed: {exc}", file=sys.stderr)
        return None
    w, h = img.get_size()
    return pygame.transform.smoothscale(img, (round(w * LOGO_X_STRETCH), h))


def draw_about_screen(canvas, title_font, body_font, logo, title, rows):
    """`rows` is a list of (label, value) -- labels right-aligned in an
    orange column, values left-aligned in white beside them, the block
    centered in the space between the title and the logo. A row with an
    empty label continues the previous row's value column."""
    frame_w, frame_h = canvas.get_size()
    canvas.fill(BLACK)
    title_surf = title_font.render(title, True, ORANGE)
    canvas.blit(title_surf, ((frame_w - title_surf.get_width()) // 2, ABOUT_MARGIN_Y))
    top = ABOUT_MARGIN_Y + title_surf.get_height()
    bottom = frame_h - ABOUT_MARGIN_Y
    if logo is not None:
        bottom -= logo.get_height()
        canvas.blit(logo, ((frame_w - logo.get_width()) // 2, bottom))
    if not rows:
        return
    label_surfs = [body_font.render(label, True, ORANGE) if label else None for label, _ in rows]
    value_surfs = [body_font.render(str(value), True, WHITE) for _, value in rows]
    label_w = max((l.get_width() for l in label_surfs if l), default=0)
    value_w = max(v.get_width() for v in value_surfs)
    line_h = body_font.get_linesize() + ABOUT_LINE_GAP
    x0 = (frame_w - (label_w + ABOUT_COLUMN_GAP + value_w)) // 2
    y = top + (bottom - top - line_h * len(rows)) // 2
    for label_surf, value_surf in zip(label_surfs, value_surfs):
        if label_surf:
            canvas.blit(label_surf, (x0 + label_w - label_surf.get_width(), y))
        canvas.blit(value_surf, (x0 + label_w + ABOUT_COLUMN_GAP, y))
        y += line_h




def find_keyboard_devices():
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.capabilities().get(ecodes.EV_KEY):
            devices.append(dev)
    if not devices:
        print("No keyboard input device found -- running headless/unattended.", file=sys.stderr)
    return devices


def load_settings(path=SETTINGS_PATH):
    """settings.ini is per machine and untracked (see settings.example.ini);
    missing file or keys fall back to these defaults."""
    cp = configparser.ConfigParser()
    cp.read(path)
    return {
        "ascii_mode": cp.getboolean("playback", "ascii_mode", fallback=False),
        "ascii_saturation": cp.getfloat("playback", "ascii_saturation", fallback=3.0),
        "ascii_font": cp.get("playback", "ascii_font", fallback="Lat15-VGA8"),
    }


class TvDinnerApp:
    def __init__(self):
        self.settings = load_settings()
        channels = load_index(INDEX_PATH)
        self.channels = channels
        self.nav = NavigationState(channels)

        pygame.display.set_mode(layout.CANVAS_SIZE)  # headless (dummy driver); needed for .convert()
        self.font = pygame.font.Font(os.path.join(ASSET_DIR, layout.FONT_PATH_GUIDE), layout.GUIDE_FONT_SIZE)
        self.background = pygame.image.load(os.path.join(ASSET_DIR, "TV_DINNER_BACKGROUND_BLANK.png")).convert()
        self.no_thumb = pygame.image.load(os.path.join(ASSET_DIR, "TV_DINNER_NO_THUMBNAIL.png")).convert()
        self.surface = pygame.Surface(layout.CANVAS_SIZE)

        self.fb = FrameBuffer()
        self.tty_fd, self.console_graphics_mode = enter_console_graphics_mode()

        self.kbd_devices = find_keyboard_devices()
        self.selector = selectors.DefaultSelector()
        for dev in self.kbd_devices:
            self.selector.register(dev, selectors.EVENT_READ, data="input")

        self.overlay_renderer = OverlayRenderer(ASSET_DIR)
        self.mode = "guide"
        self.player = None
        self.pending_exit_code = 0

        about_font_path = os.path.join(ASSET_DIR, "VCR_OSD_MONO_1.001.ttf")
        self.about_title_font = pygame.font.Font(about_font_path, 36)
        self.about_body_font = pygame.font.Font(about_font_path, 26)
        self.about_logo = load_about_logo()
        self.about_active = False

        # ASCII mode (settings.ini) -- falls back to normal video if its
        # font or ffmpeg can't be set up on this machine.
        self.ascii_video = None
        if self.settings["ascii_mode"]:
            try:
                self.ascii_video = AsciiVideo(self.fb, font_path=font_path(self.settings["ascii_font"]),
                                              saturation=self.settings["ascii_saturation"])
            except (OSError, ValueError) as exc:
                print(f"ASCII mode unavailable, playing normal video: {exc}", file=sys.stderr)

    # -- guide rendering -----------------------------------------------------

    def _thumbnail_surface(self, program):
        path = thumbnail_path(CACHE_DIR, program)
        if path:
            try:
                return pygame.image.load(path).convert()
            except pygame.error:
                pass
        return self.no_thumb

    def render_about(self):
        draw_about_screen(self.surface, self.about_title_font, self.about_body_font, self.about_logo,
                          f"TV DINNER {VERSION}", [
                              ("MEDIA", MEDIA_ROOT),
                              ("CHANNELS", len(self.channels)),
                              ("PROGRAMS", sum(len(ch["programs"]) for ch in self.channels)),
                              ("ASCII MODE", "ON" if self.ascii_video else "OFF"),
                          ])
        self.fb.write_surface(self.surface)

    def render_guide(self):
        focused = self.nav.focused_program()
        data = {
            "clock": format_clock_12h(datetime.datetime.now()),
            "now_playing": {
                "artist": focused["artist"],
                "title": focused["title"],
                "duration_seconds": focused.get("duration_seconds") or 0,
            },
            "thumbnail_surface": self._thumbnail_surface(focused),
            "channels": self.nav.visible_channels(),
        }
        guide_render.render_guide(self.surface, self.font, self.background, data, self.nav.highlight_position())
        self.fb.write_surface(self.surface)

    # -- mode transitions ------------------------------------------------

    def enter_playback(self):
        channel_index, program_index = self.nav.current_indices()
        self.fb.fill_black()  # nothing stale left over for a DRM-master hiccup to expose
        mpv = MpvController(audio_only=self.ascii_video is not None)
        # In ASCII mode the indicators are drawn by AsciiVideo, which takes
        # mpv's overlay-add calls in its place.
        overlays = OverlayManager(self.ascii_video or mpv, self.overlay_renderer)
        self.player = PlaybackController(mpv, overlays, self.channels, video=self.ascii_video)
        self.player.start(MEDIA_ROOT, channel_index, program_index)
        self.selector.register(mpv.fileno(), selectors.EVENT_READ, data="mpv")
        self.mode = "playback"

    def exit_playback(self):
        self.nav.sync_to(self.player.channel_index, self.player.program_index)
        self.selector.unregister(self.player.mpv.fileno())
        self.player.stop()
        self.player = None
        self.mode = "guide"
        self.render_guide()

    # -- input handling ----------------------------------------------------

    def handle_guide_keycode(self, code):
        # BACK toggles the fleet-wide ABOUT screen from the guide (TV
        # DINNER's home level) -- 2026-09-22, replacing BACK-plays-the-
        # highlighted-program (OK still does that). ABOUT is modal: only
        # BACK (and Home, handled in handle_keycode) do anything there.
        if code == ecodes.KEY_BACK:
            self.about_active = not self.about_active
            if self.about_active:
                self.render_about()
            else:
                self.render_guide()
            return
        if self.about_active:
            return
        if code == ecodes.KEY_UP:
            self.nav.handle_up()
        elif code == ecodes.KEY_DOWN:
            self.nav.handle_down()
        elif code == ecodes.KEY_LEFT:
            self.nav.handle_left()
        elif code == ecodes.KEY_RIGHT:
            self.nav.handle_right()
        elif code in OK_CODES or code in BACK_CODES:
            # Q/Esc/hamburger from the guide play the highlighted program,
            # same as OK -- they never quit/relaunch the app here. (BACK
            # itself is caught above for ABOUT.)
            self.enter_playback()
            return
        else:
            return
        self.render_guide()

    def handle_playback_keycode(self, code):
        p = self.player
        if code == ecodes.KEY_UP:
            p.change_channel(1)
        elif code == ecodes.KEY_DOWN:
            p.change_channel(-1)
        elif code == ecodes.KEY_LEFT:
            p.change_program(-1)
        elif code == ecodes.KEY_RIGHT:
            p.change_program(1)
        elif code in OK_CODES:
            p.toggle_pause()
        elif code == ecodes.KEY_VOLUMEDOWN:
            p.mute()
        elif code == ecodes.KEY_VOLUMEUP:
            p.unmute()
        elif code in BACK_CODES:
            self.exit_playback()

    def handle_keycode(self, code):
        if code in HOME_CODES:
            return "quit_home"
        if self.mode == "guide":
            self.handle_guide_keycode(code)
        else:
            self.handle_playback_keycode(code)
        return None

    # -- main loop -----------------------------------------------------------

    def run(self):
        self.render_guide()
        running = True
        try:
            while running:
                if self.mode != "playback":
                    timeout = GUIDE_SELECT_TIMEOUT_S
                elif self.player and self.player.video is not None:
                    timeout = ASCII_SELECT_TIMEOUT_S
                else:
                    timeout = PLAYBACK_SELECT_TIMEOUT_S
                for key, _ in self.selector.select(timeout=timeout):
                    if key.data == "input":
                        try:
                            events = list(key.fileobj.read())
                        except OSError:
                            # Device vanished (e.g. remote dongle unplugged) --
                            # drop it rather than crash the whole app.
                            self.selector.unregister(key.fileobj)
                            continue
                        for event in events:
                            if event.type == ecodes.EV_KEY and event.value == 1:  # key down only
                                result = self.handle_keycode(event.code)
                                if result == "quit_home":
                                    self.pending_exit_code = 1
                                    running = False
                            if not running:
                                break
                    elif key.data == "mpv" and self.player:
                        for event in self.player.mpv.read_events():
                            if event.get("event") == "end-file" and event.get("reason") == "eof":
                                self.player.handle_end_of_file()
                            elif (event.get("event") == "property-change" and event.get("name") == "time-pos"
                                  and self.player.video is not None):
                                self.player.video.set_time(event.get("data"))
                    if not running:
                        break

                if self.mode == "playback" and self.player:
                    self.player.overlays.update()
                    if self.player.video is not None:
                        self.player.video.tick()
                    if not self.player.mpv.is_running():
                        self.exit_playback()
        finally:
            if self.player:
                self.player.stop()
            self.fb.close()
            restore_console_text_mode(self.tty_fd, self.console_graphics_mode)
            pygame.quit()

        sys.exit(self.pending_exit_code)


def main():
    pygame.init()
    TvDinnerApp().run()


if __name__ == "__main__":
    main()
