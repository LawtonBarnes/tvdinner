# TV DINNER

An EPG-style (Electronic Program Guide) front end for browsing and playing
MP4s stored on a USB drive, streamed to whichever Pi puppet(s) it's
assigned to. Modeled on an old-school channel-surfing cable box guide:
UP/DOWN/LEFT/RIGHT browse a grid of channels x NOW/NEXT/LATER programs,
OK (or BACK) plays the highlighted program, and once playing, UP/DOWN
change channel, LEFT/RIGHT skip within the current channel, and BACK
returns to the guide.

## Media drive: file and folder nomenclature

The drive (labeled `BANGER`) is organized as one folder per channel, each
folder full of that channel's programs:

```
BANGER/
  2-DUNG/
    Poison-Talk_Dirty_To_Me.mp4
    Motley_Crue-Girls_Girls_Girls.mp4
  22-BUTT/
    House_Of_Lords-I_Wanna_Be_Loved.mp4
    ...
  4-ODOR/
    Fiona-Everything_You_Do_Youre_Sexing_Me.mp4
    ...
```

### Folder naming: `<number>-<CALLSIGN>`

- `<number>` is the channel number. **Not zero-padded on disk** -- `2-DUNG`,
  not `02-DUNG`. The app zero-pads to 2 digits for display only
  (`format_channel_number()` in `parsing.py`), so the raw folder name and
  what shows on screen don't have to match character-for-character.
- `<CALLSIGN>` is a short, all-caps channel name (fits a 4-character slot
  in the guide's column A).
- Only the first hyphen in the folder name is meaningful as a separator --
  callsigns themselves never contain a hyphen, so this doesn't need any
  special-casing beyond a simple partition.

### Filename naming: `<Artist_Name>-<Song_Title>.mp4`

- Underscores render as spaces (`Def_Leppard` -> `Def Leppard`).
- Only the **first** hyphen divides artist from title -- everything after
  it, including any further hyphens, belongs to the title
  (`AC_DC-Its_A_Long_Way_To_The_Top-If_You_Wanna_Rock_N_Roll.mp4` splits
  into artist `AC DC` and title `Its A Long Way To The Top-If You Wanna
  Rock N Roll`, hyphen and all).
- Extension must be `.mp4` -- nothing else is scanned by the indexer.
- Renaming a file changes its mtime, which the indexer uses to detect
  files that need re-probing (duration/thumbnail) -- a renamed file gets
  picked up automatically on the next index rebuild, no full rescan
  needed.

Both parsing rules live in `parsing.py` (`parse_channel_folder()`,
`parse_media_filename()`) -- that file is the single source of truth if
either convention ever needs to change.

## How it's built

- `indexer.py` walks the drive once (in the background, not on-demand) and
  caches each channel's program list -- sorted by file modified date, not
  alphabetically, so a channel doesn't always play in the same order --
  plus a probed duration, a generated thumbnail and the audio's loudness
  (ffmpeg `ebur128`: integrated LUFS and sample peak) per file, to
  `cache/index.json` / `cache/thumbnails/`. `player.py` turns each file
  up or down to the same loudness (`TARGET_LUFS`) with a fixed per-file
  gain -- boosts stop short of a -1 dB peak, so nothing clips.
- `navigation.py` is the guide's pure state machine: which channel/program
  is highlighted, scrolling behavior at the grid's edges, wraparound.
- `guide_render.py` / `layout.py` / `text_layout.py` draw the guide itself
  onto a pygame surface, pixel-measuring text against the real font rather
  than guessing at character counts, then `framebuffer.py` pushes it to
  `/dev/fb0` directly (this hardware's GPU driver doesn't handle SDL's
  usual async page-flips cleanly).
- `player.py` / `mpv_control.py` / `overlay.py` drive mpv over its JSON IPC
  socket for actual video playback (`--vo=drm`, direct KMS, no X11), with
  OSD indicators (CH/MUTE/VOL+/PAUSE/SKIP) composited on top via mpv's own
  `overlay-add` command.
- `app.py` ties it together behind one evdev-driven event loop -- the only
  thing reading the remote -- and owns the guide/playback mode switch.
- `ascii_video.py`, `pixel_video.py` and `mask_video.py` are the optional
  ASCII, PIXEL and DOT MASK modes (below), sharing `fb_video.py`'s
  decode/timing/overlay pipeline.

## Video modes

`video_mode` in `settings.ini` (copy `settings.example.ini`) picks how
playback draws the picture: `normal` (mpv), `ascii`, `pixel` or `dotmask`.
It's also changeable on the ABOUT screen (BACK from the guide): UP/DOWN
picks the VIDEO MODE or PALETTE row, LEFT/RIGHT changes it, and the change
is saved back to `settings.ini`. (An older file's `ascii_mode = yes` still
means `ascii` until the first save.)

In ASCII, PIXEL and DOT MASK mode mpv plays only the audio (same gain
compensation, pause, mute and auto-advance as normal) and reports its
playback clock; ffmpeg decodes the video straight down to the mode's small
grid; numpy turns each small frame into a full one written to `/dev/fb0`,
with the playback indicators drawn in place of mpv's `overlay-add`. All
need `ffmpeg` and fall back to normal video without it.

### ASCII mode

Plays video as colored text characters on black instead of normal video
-- built for a 2x2 CRT video wall fed at XGA (1024x768), where the Linux
console's 8x8 VGA font gives a 128x96 character grid, 64x48 per tube: a
soft SD source becomes crisp lettering. ffmpeg decodes the video down to
one pixel per character cell; each cell's brightness
picks a character from a density ramp built from the font itself, tinted
with the cell's own color at full brightness; the whole frame is composed
in numpy from a glyph atlas. Runs at ~27 fps on a Pi 3B+ at 1024x768.
Needs the console font (`ascii_font`, standard on Raspberry Pi OS).

### PIXEL mode

Plays video like an old computer or game console: blocky pixels, a fixed
limited palette (`pixel_palette`: NES, PICO-8, EGA, C64, GAME BOY, CGA),
and 8x8 Bayer ordered dithering to fake the in-between colors -- ordered
rather than error diffusion because its pattern holds still on moving
video. The default `pixel_size` 256x192 is exactly 4x4 framebuffer pixels
per chunky pixel at 1024x768; other sizes stretch to fit, nearest
neighbor. Each frame gets a saturation/contrast boost before the palette
(pale SD tints would otherwise flatten to gray), and nearest-color is a
65536-entry RGB565 lookup table built once per palette, so a frame is a
few milliseconds of numpy.

### DOT MASK mode

Normal full-color video multiplied by a CRT shadow-mask pattern,
`assets/DOT-MASK.png` (soft red, green and blue phosphor dots on black,
1024x768 -- a Photoshop Multiply layer over the video), with a brightness
gain (`dotmask_gain`, default 2.0) to make up for the mask's dark gaps.
ffmpeg scales to the full framebuffer and hands over RGB565 directly; since
every mask pixel lights exactly one channel, the multiply is a small
(mask shade, channel value) lookup table -- ~19ms a frame on a Pi 3B+,
~24 fps on the XGA wall. mpv can't do this itself: its `overlay-add` only
alpha-blends, and ffmpeg's `blend=multiply` filter ran at under half real
time. A slot-mask version was tried too and dropped -- through the
composite chain it read as a screen door.

## Drive mounts

- The `BANGER` drive is physically attached to MP and mounted **read-only**
  at `/mnt/tvdinner` (`ro,nofail,x-systemd.automount`) -- deliberately, so
  nothing in the app can ever write to the removable media by accident.
  Fixing a mis-tagged filename (like the Fiona rename above) requires a
  manual `mount -o remount,rw`, the edit, then `mount -o remount,ro`.
- The index/thumbnail cache lives on MP's own SD card
  (`/opt/tvdinner/cache`), not on the drive itself, since it needs to be
  writable and MP is the only machine with the drive attached.
- Puppets and `production` reach both over Samba/CIFS
  (`tvdinner-media`, `tvdinner-cache`), read-only there too.
