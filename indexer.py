"""Builds/refreshes the TV DINNER media index: walks the drive, sorts each
channel's files by mtime (deliberately not alphabetical, per design), and
caches duration (ffprobe), a 160x120 thumbnail (ffmpeg, grabbed from ~1/3
into the file rather than right at the start), and the audio's loudness
(ffmpeg's ebur128 filter: integrated loudness in LUFS plus peak level) per
file so the guide never has to shell out live while the user is navigating.

The loudness data drives player.py's per-file gain, which plays every video
at the same loudness (2026-10-23, replacing the earlier boost-only gain
from volumedetect's peak level -- peak says how close a track is to
clipping, not how loud it sounds).

Designed to run locally on the machine with the drive physically attached
(MP) for speed -- writes into a separate cache directory, never onto the
media drive itself. Re-running is fully manual (no cron/timer) -- see
project memory for what happens to the index if the drive's content
changes between runs. A cached entry whose file hasn't changed (same mtime)
is reused as-is; if it predates the loudness fields, only the loudness pass
runs for it (no new thumbnail/duration probe).

Usage: python3 indexer.py --media /mnt/tvdinner --cache /opt/tvdinner/cache
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

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

# ebur128's end-of-file summary block ("Integrated loudness: I: -14.2 LUFS"
# ... "Sample peak: Peak: -0.3 dBFS"); the per-frame lines before it don't
# match these patterns.
LUFS_RE = re.compile(r"^\s+I:\s+(-?\d+(?:\.\d+)?|-inf) LUFS", re.MULTILINE)
PEAK_RE = re.compile(r"^\s+Peak:\s+(-?\d+(?:\.\d+)?|-inf) dBFS", re.MULTILINE)
# Sample peak, not true peak: mpv's gain is applied to the decoded samples,
# so clipping depends on those; and true peak's 4x oversampling made the
# pass ~4x slower on MP's Pi 3B+ (38s vs 9s a file, ~11h vs ~3h a library).
EBUR128_PEAK_MODE = "sample"


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


def _db(text):
    return None if text == "-inf" else float(text)


def probe_loudness(video_path):
    """Full-file audio pass via ffmpeg's ebur128 filter -> (integrated
    loudness in LUFS, peak in dBFS), either None if it couldn't be
    measured. Integrated loudness is the EBU R128 / streaming-service
    measure of how loud a track sounds overall (gated, so silent stretches
    don't drag it down)."""
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", video_path, "-vn",
         "-af", f"ebur128=peak={EBUR128_PEAK_MODE}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    lufs = LUFS_RE.findall(result.stderr)
    peak = PEAK_RE.findall(result.stderr)
    return (_db(lufs[-1]) if lufs else None), (_db(peak[-1]) if peak else None)


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


def _write_index(cache_dir, media_root, channels):
    index = {"media_root": media_root, "channels": sorted(channels, key=lambda c: c["number"])}
    os.makedirs(cache_dir, exist_ok=True)
    tmp_path = os.path.join(cache_dir, INDEX_FILENAME + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp_path, os.path.join(cache_dir, INDEX_FILENAME))  # atomic swap
    return index


def _measure_loudness(program, video_path, relpath, skipped):
    lufs, peak = probe_loudness(video_path)
    if lufs is None:
        skipped.append(f"{relpath}: ebur128 loudness failed to parse ffmpeg output")
    program["integrated_lufs"] = lufs
    program["peak_db"] = peak
    # The old volumedetect fields are superseded -- drop them so a stale
    # value can never be mistaken for current data.
    program.pop("peak_volume_db", None)
    program.pop("mean_volume_db", None)


def build_index(media_root, cache_dir, force=False):
    thumb_dir = os.path.join(cache_dir, "thumbnails")
    existing = {} if force else load_existing_index(cache_dir)
    channels = []
    skipped = []

    channel_entries = [e for e in sorted(os.scandir(media_root), key=lambda e: e.name) if e.is_dir()]

    # Pre-scan just to get an accurate total for progress reporting -- a
    # full force run redoes ffprobe+thumbnail+loudness per file, taking a
    # couple of hours on this hardware, so an "is it hung?" progress line
    # matters a lot more than it did for the lightweight cached-reuse case.
    total_files = 0
    for entry in channel_entries:
        try:
            parse_channel_folder(entry.name)
        except ValueError:
            continue
        total_files += sum(
            1 for f in os.scandir(entry.path) if f.is_file() and f.name.lower().endswith(VIDEO_EXT)
        )
    print(f"Found {len(channel_entries)} channel folders, {total_files} video files. "
          f"{'Forcing full re-probe (ignoring cache).' if force else 'Reusing cached entries where mtime matches.'}",
          flush=True)

    start_time = time.monotonic()
    done_files = 0
    probed_files = 0  # files that needed any ffmpeg work -- the progress estimate's basis

    for entry in channel_entries:
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
                done_files += 1
                if "integrated_lufs" in cached:
                    programs.append(cached)
                    continue
                # Unchanged file indexed before loudness was measured: keep
                # its duration/thumbnail, add just the loudness pass.
                program = dict(cached)
                _measure_loudness(program, f.path, relpath, skipped)
                programs.append(program)
            else:
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

                program = {
                    "relpath": relpath,
                    "filename": f.name,
                    "artist": artist,
                    "title": title,
                    "mtime": mtime,
                    "duration_seconds": duration,
                    "thumbnail_relpath": thumb_relpath if thumb_ok else None,
                }
                _measure_loudness(program, video_path, relpath, skipped)
                programs.append(program)
                done_files += 1

            probed_files += 1
            elapsed = time.monotonic() - start_time
            remaining = elapsed / probed_files * (total_files - done_files)
            print(f"[{done_files}/{total_files}] {relpath} "
                  f"I {program.get('integrated_lufs')} LUFS, peak {program.get('peak_db')} dB "
                  f"({elapsed / 60:.1f}m elapsed, ~{remaining / 60:.1f}m remaining)", flush=True)

        channels.append({
            "number": number,
            "callsign": callsign,
            "folder_name": entry.name,
            "programs": programs,
        })

        # Checkpoint after each channel -- a multi-hour unattended run
        # shouldn't lose everything if it's interrupted partway through.
        _write_index(cache_dir, media_root, channels)

    index = _write_index(cache_dir, media_root, channels)
    return index, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", required=True, help="Path to the mounted media drive")
    parser.add_argument("--cache", required=True, help="Path to the writable cache directory")
    parser.add_argument("--force", action="store_true",
                         help="Ignore cached mtime matches and re-probe every file "
                              "(thumbnails and durations too, not just loudness)")
    args = parser.parse_args()

    start_time = time.monotonic()
    index, skipped = build_index(args.media, args.cache, force=args.force)

    total_programs = sum(len(c["programs"]) for c in index["channels"])
    elapsed_min = (time.monotonic() - start_time) / 60
    print(f"Indexed {len(index['channels'])} channels, {total_programs} programs in {elapsed_min:.1f} minutes.")
    if skipped:
        print(f"\n{len(skipped)} skipped/failed:", file=sys.stderr)
        for line in skipped:
            print(f"  - {line}", file=sys.stderr)


if __name__ == "__main__":
    main()
