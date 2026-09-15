"""World Radio - a local "radio.garden"-style app, with a flat 2D world map.

Browse and play thousands of internet radio stations worldwide, searchable
by name, country, and genre/tag, or by clicking dots on a flat world map.

This file is intentionally tiny - it just creates the window and hands
off to ui.app.WorldRadioApp. All behavior lives in:
    common/   - shared constants (API mirrors, headers, country names)
    core/     - Radio Browser API client, map geodata loader, VLC player
                (none of these import tkinter or matplotlib UI widgets)
    ui/       - the window and its widgets (station list, map, wiring)

REQUIREMENTS:
    pip install requests python-vlc matplotlib numpy
    VLC media player must be installed on your system:
        https://www.videolan.org/vlc/  (python-vlc just binds to it)

USAGE:
    python main.py
"""

import tkinter as tk
from tkinter import ttk

from common.paths import resource_path
from ui.app import WorldRadioApp


def _set_app_icon(root):
    """Sets the window/taskbar icon. iconphoto (PNG) works cross-platform
    including Linux and macOS; iconbitmap additionally sets the crisper
    Windows-specific .ico for the title bar there. Both are best-effort -
    a missing or unreadable icon file shouldn't stop the app from
    starting, just leave it with Tk's default icon."""
    try:
        icon_image = tk.PhotoImage(file=resource_path("assets/icon.png"))
        root.iconphoto(True, icon_image)
        root._icon_image_ref = icon_image  # keep a reference - Tk drops the icon if this gets garbage collected
    except Exception:
        pass

    try:
        if root.tk.call("tk", "windowingsystem") == "win32":
            root.iconbitmap(resource_path("assets/icon.ico"))
    except Exception:
        pass


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    _set_app_icon(root)
    WorldRadioApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()