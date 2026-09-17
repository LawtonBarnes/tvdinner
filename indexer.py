"""Builds/refreshes the TV DINNER media index: walks the drive, sorts each
channel's files by mtime (deliberately not alphabetical, per design), and
caches duration (ffprobe) + a 160x120 thumbnail (ffmpeg) per file so the
guide never has to shell out live while the user is navigating.

Designed to run locally on the machine with the drive physically attached
(MP) for speed -- writes into a separate cache directory, never onto the
media drive itself.

Usage: python3 indexer.py --media /mnt/tvdinner --cache /opt/tvdinner/cache
"""

import argparse
import json
import os
import subprocess
import sys

from parsing import parse_channel_folder, parse_media_filename

VIDEO_EXT = ".mp4"
THUMB_SIZE = "160:120"
THUMB_TIMESTAMP = "00:00:05"
INDEX_FILENAME = "index.json"


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


def generate_thumbnail(video_path, thumb_path, fallback_timestamp="00:00:01"):
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-ss", THUMB_TIMESTAMP, "-i", video_path,
        "-frames:v", "1", "-vf", f"scale={THUMB_SIZE}", thumb_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(thumb_path):
        # Short clip -- THUMB_TIMESTAMP may be past the end. Retry near the start.
        cmd[3] = fallback_timestamp
        subprocess.run(cmd, capture_output=True, text=True, check=True)


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

            thumb_relpath = os.path.splitext(relpath)[0] + ".jpg"
            thumb_path = os.path.join(thumb_dir, thumb_relpath)
            try:
                generate_thumbnail(video_path, thumb_path)
                thumb_ok = True
            except subprocess.CalledProcessError as e:
                skipped.append(f"{relpath}: ffmpeg thumbnail failed ({e})")
                thumb_ok = False

            programs.append({
                "relpath": relpath,
                "filename": f.name,
                "artist": artist,
                "title": title,
                "mtime": mtime,
                "duration_seconds": duration,
                "thumbnail_relpath": thumb_relpath if thumb_ok else None,
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
