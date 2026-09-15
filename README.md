# World Radio

A local, "radio.garden"-style desktop app for browsing and playing
thousands of internet radio stations worldwide - searchable by name,
country, or genre/tag, or by clicking dots on a flat, pannable 2D world
map. Built with Python, Tkinter, and matplotlib, streaming via VLC.

## Features

**Browsing**
- Search by name, country, and/or genre/tag, with results updating
  automatically shortly after you stop typing (or press Enter/Search)
- Selecting a country or typing a tag loads *every* matching station,
  not just a capped page
- "Top Stations" (most-played), "All Stations" (the full catalog),
  Favorites, and Recently Played, all from one **Browse** menu
- Sortable columns (Station / Country / Tags / Bitrate) in the list view
- Paginated list view (1,000 rows per page) so browsing stays fast even
  with the full ~60,000-station catalog loaded
- Your last search/country/tag filter is restored automatically next
  time you open the app

**The map**
- Click a dot to play that station; click a dense cluster of dots to
  pick from a list of what's there
- Scroll to zoom (centered on the cursor), Ctrl+drag or middle-click
  drag to pan, with a **Reset View** button
- The world wraps seamlessly - pan past the left edge and you land on
  the right side of the globe, same as a real map
- Nearby stations merge into a single, size-scaled marker at low zoom
  and split apart as you zoom in, so the map stays fast regardless of
  how many stations are loaded
- Hovering a station (on the map or in the list) highlights it in teal
  on both; the currently-playing station stays highlighted in green

**Playback**
- Play/Stop/Retry (auto-appears if a stream fails), volume with mute,
  and Space/Left/Right keyboard shortcuts (play-pause / volume)
- Favorite stations (☆ button) and a Recently Played list, both saved
  locally and available from the Browse menu
- Currently-playing station is tracked and highlighted everywhere it
  appears (list, map) as you browse elsewhere

**Other**
- Light/dark theme (⚙ Settings menu), remembered between sessions
- An in-memory cache means reselecting something you already browsed
  this session is instant; a **⟳ Refresh** button forces a fresh fetch
  when you want the latest data for what's currently shown

## Requirements

- Python 3.9+
- [VLC media player](https://www.videolan.org/vlc/) installed on the
  system (the `python-vlc` package just binds to your existing VLC
  install - it isn't bundled)
- Python packages in `requirements.txt`: `requests`, `python-vlc`,
  `matplotlib`, `numpy`

## Running from source

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

pip install -r requirements.txt
python main.py
```

## Building a standalone Windows app

```
build_windows.bat
```

This sets up a virtual environment, installs dependencies plus
PyInstaller, and builds via `world_radio.spec`. The finished app lands
at `dist\World Radio\World Radio.exe`.

It's a `--onedir` build (a folder, not a single file) so the app starts
quickly and keeps its data files (see below) sitting right next to the
`.exe`. VLC still needs to be installed separately on whatever machine
runs the built app - PyInstaller bundles Python packages, not native
system applications.

To build for macOS or Linux instead, run PyInstaller on that OS directly
(a Windows build won't run elsewhere) - `world_radio.spec` itself isn't
Windows-specific, only `build_windows.bat` is.

## Project structure

```
main.py              - entry point; creates the window, sets the icon, hands off to ui.app
common/               - shared, tkinter-free config and helpers
  constants.py           API mirrors, headers, ISO country-code -> name table
  theme.py                light/dark color palettes + theme persistence
  settings.py             volume/filters/favorites/recent persistence
  paths.py                where to read/write files, source vs. frozen build
core/                  - data access and playback; no tkinter/matplotlib imports
  radio_browser.py        Radio Browser API client (paginated search/browse)
  player.py                VLC playback wrapper
  geodata.py                world country-outline GeoJSON, cached locally
ui/                    - the window and its widgets
  app.py                    top-level window; wires everything together
  map_view.py                the 2D map (matplotlib embedded in Tk)
  station_list_view.py       the paginated, sortable station list
assets/                - icon.png (in-app window icon), icon.ico (Windows .exe icon)
requirements.txt       - Python dependencies
world_radio.spec       - PyInstaller build configuration
build_windows.bat      - one-step Windows build script
```

## Data files

A few small files are created next to `main.py` (or next to the `.exe`
in a built app) on first run - safe to delete if you want a clean slate,
they'll just be recreated:

- `settings.json` - active theme
- `app_settings.json` - volume/mute, last filters, favorites, recently
  played
- `countries.geo.json` - cached map outlines (re-downloaded if missing)

## Known limitations

- The map's land outlines are a simplified public dataset - fine for
  browsing, not survey-accurate
- Filtered/country search results are ordered alphabetically by name
  rather than by popularity, a side effect of how full-catalog
  pagination works (see `core/radio_browser.py`)
- Sorting a very large list by column has a real, one-time cost since
  it resorts the actual underlying data, not just what's visible
- No true bitmap-level blitting during map panning yet - see
  `ui/map_view.py`'s module docstring for why that's a bigger, separate
  change from what's here now
