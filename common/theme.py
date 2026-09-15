"""Color palettes for the app's light/dark themes, plus tiny persistence
for which one is active so the choice survives between runs.

Deliberately has no tkinter/ttk imports - it's just color values and a
small JSON file - so any module (ui/ or core/) can read it without
pulling in UI machinery.
"""

import json
import os

from common.paths import app_base_dir

# File lands next to main.py when running from source, or next to the
# .exe when frozen (e.g. via PyInstaller) - see common/paths.py.
SETTINGS_FILE = os.path.join(app_base_dir(), "settings.json")

DARK = {
    "bg": "#151b26",
    "bg_alt": "#1c2431",
    "surface": "#1c2431",
    "text": "#e8ecf3",
    "text_muted": "#8b93a3",
    "accent": "#ff8a3d",
    "accent_hover": "#ffa564",
    "border": "#2b3547",
    "tree_bg": "#101826",
    "tree_alt": "#161f2e",
    "map_bg": "#101826",
    "map_land": "#27344a",
    "map_land_edge": "#3c4c69",
    "map_station": "#ff8a3d",
}

LIGHT = {
    "bg": "#f4f5f8",
    "bg_alt": "#e9ebf0",
    "surface": "#ffffff",
    "text": "#1b2330",
    "text_muted": "#68707d",
    "accent": "#e0662a",
    "accent_hover": "#f08a52",
    "border": "#d3d7e0",
    "tree_bg": "#ffffff",
    "tree_alt": "#f2f3f6",
    "map_bg": "#e6eaf3",
    "map_land": "#cdd6ea",
    "map_land_edge": "#a9b6d1",
    "map_station": "#e0662a",
}

THEMES = {"light": LIGHT, "dark": DARK}
DEFAULT_THEME = "dark"

# Fixed regardless of theme, so "this is what you're pointing at" always
# reads the same way against either palette. Used for both the station
# list's row-hover highlight and the map's dot-hover highlight, so the
# two feel like the same feature.
HOVER_HIGHLIGHT_COLOR = "#2dd4bf"
HOVER_HIGHLIGHT_TEXT = "#062420"

# Also fixed regardless of theme - marks whichever station is currently
# playing, persistently (unlike the hover highlight above, which only
# applies while the mouse is actually over that station).
PLAYING_HIGHLIGHT_COLOR = "#22c55e"
PLAYING_HIGHLIGHT_TEXT = "#062b12"


def load_theme_name():
    """Returns the last-used theme name, or DEFAULT_THEME if there's no
    settings file yet (first run) or it can't be read."""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            name = data.get("theme")
            if name in THEMES:
                return name
    except (OSError, ValueError):
        pass
    return DEFAULT_THEME


def save_theme_name(name):
    """Best-effort persistence - if the write fails, the next run just
    falls back to the default theme, which is harmless."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"theme": name}, f)
    except OSError:
        pass