"""Resolves a stable base directory for files the app persists next to
itself (settings.json, the map outline cache) - both when running from
source and when frozen into a standalone executable (e.g. via
PyInstaller).

Source runs: this is the project root (the parent of common/).

Frozen (PyInstaller) runs: __file__ inside a --onefile build resolves
into the ephemeral extraction temp folder (sys._MEIPASS), which gets
wiped after the process exits - anything written there wouldn't survive
to the next launch. sys.executable is the actual, stable path to the
running .exe in every PyInstaller mode (--onefile or --onedir), so
frozen runs use that instead, keeping data files next to the .exe.
"""

import os
import sys


def app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(relative_path):
    """Resolves a path to a bundled, read-only resource (e.g. the app
    icon) - different from app_base_dir() above, which is for files the
    app WRITES and needs to persist next to itself between runs.
    PyInstaller extracts/collects bundled data files (see the `datas`
    list in world_radio.spec) into sys._MEIPASS at runtime; from source,
    resources just live in the project tree next to main.py."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)