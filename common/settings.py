"""Persistence for user preferences and small local datasets: volume,
mute state, the last search/country/tag filters, favorites, and recently
played stations.

Stored in its own local JSON file, separate from common/theme.py's
settings.json (which only ever holds the theme choice) - so a problem
reading/writing one never affects the other.
"""

import json
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(_PROJECT_ROOT, "app_settings.json")

MAX_RECENT = 20

DEFAULTS = {
    "volume": 80,
    "muted": False,
    "last_search": "",
    "last_country_code": "",
    "last_tag": "",
    "favorites": [],  # list of station dicts
    "recent": [],  # list of station dicts, most-recently-played first
}


def load_settings():
    """Returns the persisted settings dict, merged over DEFAULTS so a
    missing file (first run) or one from an older version still yields
    every expected key."""
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            data.update(saved)
    except (OSError, ValueError):
        pass
    return data


def save_settings(data):
    """Best-effort persistence - a failed write just means the next run
    falls back to defaults for whatever didn't save, which is harmless."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _station_key(station):
    """Identifies a station across sessions/searches. stationuuid is
    Radio Browser's stable ID; url_resolved/name are fallbacks for the
    rare station missing one."""
    return station.get("stationuuid") or station.get("url_resolved") or station.get("name")


def add_recent(settings, station):
    """Records a station as just-played, most-recent-first, deduped, and
    capped at MAX_RECENT entries."""
    key = _station_key(station)
    if key is None:
        return
    recent = [s for s in settings.get("recent", []) if _station_key(s) != key]
    recent.insert(0, station)
    settings["recent"] = recent[:MAX_RECENT]


def is_favorite(settings, station):
    key = _station_key(station)
    return any(_station_key(s) == key for s in settings.get("favorites", []))


def toggle_favorite(settings, station):
    """Adds or removes the station from favorites. Returns True if it's
    now a favorite, False if it was just removed."""
    key = _station_key(station)
    favorites = settings.get("favorites", [])
    if any(_station_key(s) == key for s in favorites):
        settings["favorites"] = [s for s in favorites if _station_key(s) != key]
        return False
    settings["favorites"] = favorites + [station]
    return True
