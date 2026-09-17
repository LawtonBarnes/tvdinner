"""Loads the indexer's cache/index.json into the shape NavigationState/
guide_render expect."""

import json
import os


def load_index(index_path):
    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    channels = []
    for ch in data["channels"]:
        if not ch["programs"]:
            continue  # NavigationState requires every channel to have >=1 program
        channels.append({
            "number": ch["number"],
            "callsign": ch["callsign"],
            "programs": ch["programs"],
        })
    channels.sort(key=lambda c: c["number"])
    return channels


def thumbnail_path(cache_dir, program):
    if not program.get("thumbnail_relpath"):
        return None
    path = os.path.join(cache_dir, "thumbnails", program["thumbnail_relpath"])
    return path if os.path.exists(path) else None
