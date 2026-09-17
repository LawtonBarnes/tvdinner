#!/usr/bin/env python3
"""TV DINNER main entry point -- ties the guide (Phases 1-3) and playback
(Phase 4) together behind one real evdev-driven event loop, matching the
fleet's established pattern (see bars.py): pygame runs fully headless
(SDL_VIDEODRIVER=dummy) purely to build surfaces/render fonts, the guide
draws to /dev/fb0 directly (framebuffer.py), and mpv (player.py) takes
real DRM master only while a video is actually playing -- the two never
run at once.
"""

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

VERSION = "0.1"

MEDIA_ROOT = "/mnt/tvdinner"
CACHE_DIR = "/opt/tvdinner-cache"
INDEX_PATH = os.path.join(CACHE_DIR, "index.json")
ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")

GUIDE_SELECT_TIMEOUT_S = 1.0
PLAYBACK_SELECT_TIMEOUT_S = 0.2

OK_CODES = (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE)
BACK_CODES = (ecodes.KEY_Q, ecodes.KEY_ESC, ecodes.KEY_BACK, ecodes.KEY_COMPOSE)
HOME_CODES = (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME)


def find_keyboard_devices():
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.capabilities().get(ecodes.EV_KEY):
            devices.append(dev)
    if not devices:
        print("No keyboard input device found -- running headless/unattended.", file=sys.stderr)
    return devices


class TvDinnerApp:
    def __init__(self):
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

    # -- guide rendering -----------------------------------------------------

    def _thumbnail_surface(self, program):
        path = thumbnail_path(CACHE_DIR, program)
        if path:
            try:
                return pygame.image.load(path).convert()
            except pygame.error:
                pass
        return self.no_thumb

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
        mpv = MpvController()
        overlays = OverlayManager(mpv, self.overlay_renderer)
        self.player = PlaybackController(mpv, overlays, self.channels)
        self.player.start(MEDIA_ROOT, channel_index, program_index)
        self.selector.register(mpv.fileno(), selectors.EVENT_READ, data="mpv")
        self.mode = "playback"

    def exit_playback(self):
        self.selector.unregister(self.player.mpv.fileno())
        self.player.stop()
        self.player = None
        self.mode = "guide"
        self.render_guide()

    # -- input handling ----------------------------------------------------

    def handle_guide_keycode(self, code):
        if code == ecodes.KEY_UP:
            self.nav.handle_up()
        elif code == ecodes.KEY_DOWN:
            self.nav.handle_down()
        elif code == ecodes.KEY_LEFT:
            self.nav.handle_left()
        elif code == ecodes.KEY_RIGHT:
            self.nav.handle_right()
        elif code in OK_CODES:
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
        if self.mode == "guide" and code in BACK_CODES:
            return "quit"
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
                timeout = PLAYBACK_SELECT_TIMEOUT_S if self.mode == "playback" else GUIDE_SELECT_TIMEOUT_S
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
                                if result == "quit":
                                    running = False
                                elif result == "quit_home":
                                    self.pending_exit_code = 1
                                    running = False
                            if not running:
                                break
                    elif key.data == "mpv" and self.player:
                        for event in self.player.mpv.read_events():
                            if event.get("event") == "end-file" and event.get("reason") == "eof":
                                self.player.handle_end_of_file()
                    if not running:
                        break

                if self.mode == "playback" and self.player:
                    self.player.overlays.update()
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
