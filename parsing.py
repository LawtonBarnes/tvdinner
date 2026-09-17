"""Filename/folder-naming conventions for the TV DINNER media drive.

Folder = "<number>-<CALLSIGN>" (number not zero-padded on disk, e.g. "2-DUNG", "22-BUTT").
Filename = "<Artist_Name>-<Song_Title>.mp4" -- underscore renders as a space,
the first hyphen divides artist from title and also renders as a line break.
"""

import os

CHANNEL_NAME_RE_SEP = "-"


def parse_channel_folder(folder_name):
    """'22-BUTT' -> (22, 'BUTT'). Raises ValueError if the folder doesn't match the convention."""
    number_str, sep, callsign = folder_name.partition(CHANNEL_NAME_RE_SEP)
    if not sep or not number_str.isdigit():
        raise ValueError(f"folder name {folder_name!r} doesn't match '<number>-<CALLSIGN>'")
    return int(number_str), callsign


def format_channel_number(number):
    """Always 2-digit for display, regardless of on-disk zero-padding."""
    return f"{number:02d}"


def underscores_to_spaces(text):
    return text.replace("_", " ")


def parse_media_filename(filename):
    """'Def_Leppard-Have_You_Ever_Needed_Someone_So_Bad.mp4' ->
    ('Def Leppard', 'Have You Ever Needed Someone So Bad').
    Splits on the FIRST hyphen only (titles may legitimately contain more)."""
    stem = os.path.splitext(filename)[0]
    artist_raw, sep, title_raw = stem.partition(CHANNEL_NAME_RE_SEP)
    if not sep:
        raise ValueError(f"filename {filename!r} doesn't match '<Artist>-<Title>.mp4'")
    return underscores_to_spaces(artist_raw), underscores_to_spaces(title_raw)


def truncate_hard(text, max_len):
    """Hard cut, no ellipsis -- used for the Artist line (Main Title line 1)."""
    return text[:max_len]


def truncate_on_space(text, max_len):
    """Cut to max_len, then back off to the last space so we don't cut mid-word.
    If there's no space to back off to, falls back to a hard cut."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    last_space = cut.rfind(" ")
    return cut[:last_space] if last_space > 0 else cut


def wrap_title_two_lines(title, total_max=40, line_max=20):
    """Wrap on spaces (former underscores) across up to 2 lines, total_max chars.
    Truncates on a space if the title is still too long to fit both lines."""
    title = truncate_on_space(title, total_max)
    if len(title) <= line_max:
        return [title, ""]
    line1 = truncate_on_space(title, line_max)
    remainder = title[len(line1):].strip()
    line2 = truncate_on_space(remainder, line_max) if len(remainder) > line_max else remainder
    return [line1, line2]


def format_duration(total_seconds):
    """Seconds -> HH:MM:SS / M:SS with no leading zero on the leading unit."""
    total_seconds = int(round(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
