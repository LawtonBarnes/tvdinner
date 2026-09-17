"""Builds/refreshes the TV DINNER media index: walks the drive, sorts each
channel's files by mtime (deliberately not alphabetical, per design), and
caches duration (ffprobe), a 160x120 thumbnail (ffmpeg, grabbed from ~1/3
into the file rather than right at the start), and peak/mean audio volume
(ffmpeg's volumedetect filter) per file so the guide never has to shell
out live while the user is navigating.

peak_volume_db/mean_volume_db are stored for future use only -- these
source videos were ripped from YouTube and their levels are all over the
map, but nothing here auto-adjusts playback volume. Any per-file gain
compensation based on this data is a separate, not-yet-built feature.

Designed to run locally on the machine with the drive physically attached
(MP) for speed -- writes into a separate cache directory, never onto the
media drive itself. Re-running is fully manual (no cron/timer) -- see
project memory for what happens to the index if the drive's content
changes between runs.

Usage: python3 indexer.py --media /mnt/tvdinner --cache /opt/tvdinner/cache
"""

import argparse
import json
import os
import re
import subprocess
import sys

from parsing import parse_channel_folder, parse_media_filename

VIDEO_EXT = ".mp4"
THUMB_SIZE = "160:120"
# Grab the thumbnail from ~1/3 into the video rather than right at the
# start -- the first few seconds are often a fade-in/title card, not
# representative of the video. Falls back to a fixed 5s in if duration
# couldn't be probed.
THUMB_POSITION_FRACTION = 1 / 3
DEFAULT_THUMB_TIMESTAMP_S = 5.0
INDEX_FILENAME = "index.json"

MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")
MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")


def probe_duration_seconds(video_path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def generate_thumbnail(video_path, thumb_path, timestamp_seconds, fallback_timestamp_seconds=1.0):
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-ss", f"{timestamp_seconds:.2f}", "-i", video_path,
        "-frames:v", "1", "-vf", f"scale={THUMB_SIZE}", thumb_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(thumb_path):
        # Timestamp landed past the end (e.g. a bad duration probe). Retry near the start.
        cmd[3] = f"{fallback_timestamp_seconds:.2f}"
        subprocess.run(cmd, capture_output=True, text=True, check=True)


def probe_volume_db(video_path):
    """Full-file audio pass via ffmpeg's volumedetect filter -- peak
    (max_volume) and mean_volume in dBFS, both from the same pass since
    parsing the second regex out of the same stderr output is free.
    Not used to adjust playback yet -- stored for future use (a per-file
    gain offset), not applied automatically."""
    result = subprocess.run(
        ["ffmpeg", "-i", video_path, "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    max_match = MAX_VOLUME_RE.search(result.stderr)
    mean_match = MEAN_VOLUME_RE.search(result.stderr)
    peak_db = float(max_match.group(1)) if max_match else None
    mean_db = float(mean_match.group(1)) if mean_match else None
    return peak_db, mean_db


def load_existing_index(cache_dir):
    index_path = os.path.join(cache_dir, INDEX_FILENAME)
    if not os.path.exists(index_path):
        return {}
    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Flatten to filename -> program dict for fast mtime-based reuse lookups.
    by_path = {}
    for channel in data.get("channels", []):
        for program in channel.get("programs", []):
            by_path[program["relpath"]] = program
    return by_path


def build_index(media_root, cache_dir):
    thumb_dir = os.path.join(cache_dir, "thumbnails")
    existing = load_existing_index(cache_dir)
    channels = []
    skipped = []

    for entry in sorted(os.scandir(media_root), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        try:
            number, callsign = parse_channel_folder(entry.name)
        except ValueError as e:
            skipped.append(str(e))
            continue

        files = [
            f for f in os.scandir(entry.path)
            if f.is_file() and f.name.lower().endswith(VIDEO_EXT)
        ]
        files.sort(key=lambda f: f.stat().st_mtime)

        programs = []
        for f in files:
            relpath = f"{entry.name}/{f.name}"
            mtime = f.stat().st_mtime
            try:
                artist, title = parse_media_filename(f.name)
            except ValueError as e:
                skipped.append(str(e))
                continue

            cached = existing.get(relpath)
            if cached and cached.get("mtime") == mtime:
                programs.append(cached)
                continue

            video_path = f.path
            try:
                duration = probe_duration_seconds(video_path)
            except (subprocess.CalledProcessError, ValueError) as e:
                skipped.append(f"{relpath}: ffprobe failed ({e})")
                duration = None

            thumb_timestamp = duration * THUMB_POSITION_FRACTION if duration else DEFAULT_THUMB_TIMESTAMP_S
            thumb_relpath = os.path.splitext(relpath)[0] + ".jpg"
            thumb_path = os.path.join(thumb_dir, thumb_relpath)
            try:
                generate_thumbnail(video_path, thumb_path, thumb_timestamp)
                thumb_ok = True
            except subprocess.CalledProcessError as e:
                skipped.append(f"{relpath}: ffmpeg thumbnail failed ({e})")
                thumb_ok = False

            peak_volume_db, mean_volume_db = probe_volume_db(video_path)
            if peak_volume_db is None:
                skipped.append(f"{relpath}: volumedetect failed to parse ffmpeg output")

            programs.append({
                "relpath": relpath,
                "filename": f.name,
                "artist": artist,
                "title": title,
                "mtime": mtime,
                "duration_seconds": duration,
                "thumbnail_relpath": thumb_relpath if thumb_ok else None,
                "peak_volume_db": peak_volume_db,
                "mean_volume_db": mean_volume_db,
            })

        channels.append({
            "number": number,
            "callsign": callsign,
            "folder_name": entry.name,
            "programs": programs,
        })

    channels.sort(key=lambda c: c["number"])
    index = {"media_root": media_root, "channels": channels}

    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, INDEX_FILENAME), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)

    return index, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", required=True, help="Path to the mounted media drive")
    parser.add_argument("--cache", required=True, help="Path to the writable cache directory")
    args = parser.parse_args()

    index, skipped = build_index(args.media, args.cache)

    total_programs = sum(len(c["programs"]) for c in index["channels"])
    print(f"Indexed {len(index['channels'])} channels, {total_programs} programs.")
    if skipped:
        print(f"\n{len(skipped)} skipped/failed:", file=sys.stderr)
        for line in skipped:
            print(f"  - {line}", file=sys.stderr)


if __name__ == "__main__":
    main()
