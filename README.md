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
  plus a probed duration and a generated thumbnail per file, to
  `cache/index.json` / `cache/thumbnails/`.
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
