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


def format_duration(total_seconds):
    """Seconds -> HH:MM:SS / M:SS with no leading zero on the leading unit."""
    total_seconds = int(round(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
