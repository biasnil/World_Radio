"""Top-level application window.

Builds the search bar, the List/Map notebook tabs, and the playback bar,
and wires user actions through to the RadioBrowser API client and
RadioPlayer. This is the only module that knows about all the pieces,
including the active light/dark theme (common/theme.py) and the
persisted preferences/favorites/recent list (common/settings.py) that it
hands down to (or loads for) the station list and map widgets.
"""

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from common.settings import load_settings, save_settings, add_recent, is_favorite, toggle_favorite
from common.theme import THEMES, load_theme_name, save_theme_name
from core.player import RadioPlayer
from core.radio_browser import RadioBrowser
from ui.map_view import MapView
from ui.station_list_view import StationListView

SEARCH_DEBOUNCE_MS = 450  # how long to wait after typing stops before auto-searching
SETTINGS_SAVE_DEBOUNCE_MS = 800  # coalesces rapid changes (e.g. dragging the volume slider) into one write
MIN_REPAINT_INTERVAL_SEC = 0.35  # caps how often a progressive load repaints the list/map while streaming in

# Widgets that already use Space/Left/Right/etc. for their own purpose -
# our global keyboard shortcuts below skip firing while one of these has
# focus, so typing a search term or nudging a slider isn't hijacked.
_SHORTCUT_IGNORE_TYPES = (tk.Entry, ttk.Entry, ttk.Combobox, ttk.Button, ttk.Scale, ttk.Treeview)


class WorldRadioApp:
    def __init__(self, root):
        self.root = root
        self.root.title("World Radio")
        self.root.geometry("980x680")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.theme_name = load_theme_name()
        self.colors = THEMES[self.theme_name]
        self.settings = load_settings()

        self.api = RadioBrowser()
        self.stations = []  # currently listed stations (list of dicts)
        self.current_station = None
        self._label_to_code = {}
        self._sort_column = None
        self._sort_reverse = False
        self._load_generation = 0  # bumped on every new load; stale background results are dropped
        self._search_debounce_id = None
        self._settings_save_id = None
        self._muted = bool(self.settings.get("muted", False))
        self._failed_station = None
        self._station_cache = {}  # in-memory only - key -> station list, see _load_with_cache()
        self._current_view_key = None  # the cache key behind whatever's currently shown, or None (Favorites/Recent)

        self.player = RadioPlayer(on_error=self._on_stream_error, on_playing=self._on_stream_playing)

        self._build_ui()
        self._apply_theme()
        self._bind_shortcuts()
        self._load_countries_async()

        # If a filter was saved from last session, _populate_countries()
        # will restore and re-run it once the country list (needed to
        # resolve the saved country code to a label) arrives - starting
        # the full, unfiltered catalog load here too would just mean two
        # heavy loads racing each other at startup for no benefit, so
        # skip it in that case.
        has_saved_filter = bool(
            self.settings.get("last_search") or self.settings.get("last_country_code")
            or self.settings.get("last_tag")
        )
        if not has_saved_filter:
            self._load_all_stations_async()

    # ---- UI construction ----------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(14, 12))
        top.pack(fill="x")

        ttk.Label(top, text="Search:").grid(row=0, column=0, sticky="w")
        self.search_var = tk.StringVar(value=self.settings.get("last_search", ""))
        search_entry = ttk.Entry(top, textvariable=self.search_var, width=22)
        search_entry.grid(row=0, column=1, padx=(4, 14))
        search_entry.bind("<Return>", lambda e: self.do_search())

        ttk.Label(top, text="Country:").grid(row=0, column=2, sticky="w")
        self.country_var = tk.StringVar(value="(any)")
        self.country_combo = ttk.Combobox(top, textvariable=self.country_var, width=20, state="readonly")
        self.country_combo.grid(row=0, column=3, padx=(4, 14))
        self.country_combo.bind("<<ComboboxSelected>>", self._on_country_selected)

        ttk.Label(top, text="Genre/Tag:").grid(row=0, column=4, sticky="w")
        self.tag_var = tk.StringVar(value=self.settings.get("last_tag", ""))
        tag_entry = ttk.Entry(top, textvariable=self.tag_var, width=13)
        tag_entry.grid(row=0, column=5, padx=(4, 14))
        tag_entry.bind("<Return>", lambda e: self.do_search())

        # Live/debounced search - typing (or picking a country) re-runs the
        # search shortly after you stop, so pressing Search/Enter is a
        # shortcut rather than a requirement. Bound after the vars already
        # hold their restored values so restoring them doesn't itself fire
        # a spurious search.
        self.search_var.trace_add("write", self._on_filter_typed)
        self.tag_var.trace_add("write", self._on_filter_typed)

        ttk.Button(top, text="Search", command=self.do_search).grid(row=0, column=6, padx=3)
        self.browse_btn = ttk.Button(top, text="Browse ▾", command=self._open_browse_menu)
        self.browse_btn.grid(row=0, column=7, padx=3)
        self.clear_filters_btn = ttk.Button(
            top, text="Clear Filters", command=self._clear_filters, state="disabled"
        )
        self.clear_filters_btn.grid(row=0, column=8, padx=3)
        self.refresh_btn = ttk.Button(top, text="⟳ Refresh", command=self._refresh_current, state="disabled")
        self.refresh_btn.grid(row=0, column=9, padx=3)

        top.columnconfigure(10, weight=1)  # spacer - pushes the settings gear to the right edge
        self.gear_btn = ttk.Button(top, text="⚙ Settings", command=self._open_settings_menu)
        self.gear_btn.grid(row=0, column=11, sticky="e")

        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        # Station list / map
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=14, pady=10)

        list_tab = ttk.Frame(notebook)
        map_tab = ttk.Frame(notebook)
        notebook.add(list_tab, text="List")
        notebook.add(map_tab, text="Map")

        self.station_list = StationListView(
            list_tab, on_play_requested=self._play_index, on_hover_changed=self._on_station_hover,
            on_selection_changed=self._on_list_selection_changed, on_sort_requested=self._on_sort_requested,
        )
        self.map_view = MapView(map_tab, on_station_clicked=self._play_index, colors=self.colors)

        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        # Bottom control bar
        bottom = ttk.Frame(self.root, padding=(14, 10))
        bottom.pack(fill="x")

        self.now_playing_var = tk.StringVar(value="Not playing")
        ttk.Label(bottom, textvariable=self.now_playing_var, font=("TkDefaultFont", 10, "bold")).pack(side="left")

        controls = ttk.Frame(bottom)
        controls.pack(side="right")

        ttk.Button(controls, text="▶ Play", command=self._play_selected).pack(side="left", padx=3)
        ttk.Button(controls, text="■ Stop", command=self.stop_playback).pack(side="left", padx=3)
        self.retry_btn = ttk.Button(controls, text="⟳ Retry", command=self._retry_playback, state="disabled")
        self.retry_btn.pack(side="left", padx=3)
        self.favorite_btn = ttk.Button(controls, text="☆ Favorite", command=self._toggle_favorite)
        self.favorite_btn.pack(side="left", padx=3)

        self.mute_btn = ttk.Button(controls, text="🔇" if self._muted else "🔊", width=3, command=self._toggle_mute)
        self.mute_btn.pack(side="left", padx=(10, 4))
        ttk.Label(controls, text="Vol").pack(side="left", padx=(0, 4))
        self.volume_var = tk.IntVar(value=int(self.settings.get("volume", 80)))
        ttk.Scale(
            controls, from_=0, to=100, orient="horizontal",
            variable=self.volume_var, command=self._on_volume_change, length=120,
        ).pack(side="left")

        self.status_var = tk.StringVar(value="Loading...")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w", padding=6).pack(
            fill="x", side="bottom"
        )

        if not self.player.is_available:
            messagebox.showwarning(
                "VLC not found",
                "python-vlc could not load the VLC engine.\n\n"
                "Install VLC media player (https://www.videolan.org/vlc/) "
                "and restart this app to enable playback.",
            )

    def _bind_shortcuts(self):
        """Space toggles play/pause of the current or selected station;
        Left/Right nudge the volume. Both skip firing while a text entry,
        combobox, button, scale, or the list itself has keyboard focus,
        so they never hijack normal typing/navigation in those widgets."""
        self.root.bind_all("<space>", self._on_key_space)
        self.root.bind_all("<Left>", lambda e: self._on_key_volume(e, -5))
        self.root.bind_all("<Right>", lambda e: self._on_key_volume(e, 5))

    def _should_ignore_shortcut(self, widget):
        return isinstance(widget, _SHORTCUT_IGNORE_TYPES)

    def _on_key_space(self, event):
        if self._should_ignore_shortcut(event.widget):
            return
        if self.current_station is not None:
            self.stop_playback()
        else:
            self._play_selected()
        return "break"

    def _on_key_volume(self, event, delta):
        if self._should_ignore_shortcut(event.widget):
            return
        new_vol = max(0, min(100, self.volume_var.get() + delta))
        self.volume_var.set(new_vol)
        self._on_volume_change(new_vol)
        return "break"

    def _on_close(self):
        """Flushes any pending debounced settings save immediately, so a
        quick toggle-then-quit isn't lost to the 800ms debounce window."""
        if self._settings_save_id is not None:
            self.root.after_cancel(self._settings_save_id)
            self._settings_save_id = None
        save_settings(self.settings)
        self.root.destroy()

    # ---- Theme -------------------------------------------------------

    def _open_settings_menu(self):
        c = self.colors
        menu = tk.Menu(
            self.root, tearoff=0, bg=c["surface"], fg=c["text"],
            activebackground=c["accent"], activeforeground="#ffffff", borderwidth=0,
        )
        theme_var = tk.StringVar(value=self.theme_name)
        menu.add_radiobutton(label="☀  Light", variable=theme_var, value="light",
                              command=lambda: self._set_theme("light"))
        menu.add_radiobutton(label="🌙  Dark", variable=theme_var, value="dark",
                              command=lambda: self._set_theme("dark"))

        x = self.gear_btn.winfo_rootx()
        y = self.gear_btn.winfo_rooty() + self.gear_btn.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _set_theme(self, name):
        if name not in THEMES or name == self.theme_name:
            return
        self.theme_name = name
        self.colors = THEMES[name]
        self._apply_theme()
        save_theme_name(name)

    def _apply_theme(self):
        """Pushes the active theme's colors onto every ttk widget style,
        the root window, and the two custom widgets (station list, map)."""
        c = self.colors
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")  # clam actually honors custom colors; some native themes ignore them

        style.configure(".", background=c["bg"], foreground=c["text"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure("TSeparator", background=c["border"])

        style.configure("TButton", background=c["surface"], foreground=c["text"],
                         bordercolor=c["border"], focusthickness=0, padding=(8, 4))
        style.map(
            "TButton",
            background=[("disabled", c["bg_alt"]), ("pressed", c["accent"]), ("active", c["accent_hover"])],
            foreground=[("disabled", c["text_muted"]), ("pressed", "#ffffff"), ("active", "#ffffff")],
        )

        style.configure("TEntry", fieldbackground=c["surface"], foreground=c["text"], bordercolor=c["border"])
        style.map("TEntry", fieldbackground=[("readonly", c["surface"])])

        style.configure("TCombobox", fieldbackground=c["surface"], foreground=c["text"], background=c["surface"])
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", c["surface"])],
            foreground=[("readonly", c["text"])],
            selectbackground=[("readonly", c["surface"])],
            selectforeground=[("readonly", c["text"])],
        )

        style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
        style.configure("TNotebook.Tab", background=c["bg_alt"], foreground=c["text_muted"], padding=(16, 8))
        style.map(
            "TNotebook.Tab",
            background=[("selected", c["surface"])],
            foreground=[("selected", c["accent"])],
        )

        style.configure(
            "Treeview", background=c["tree_bg"], fieldbackground=c["tree_bg"],
            foreground=c["text"], bordercolor=c["border"], rowheight=26, borderwidth=0,
        )
        style.configure("Treeview.Heading", background=c["bg_alt"], foreground=c["text"], relief="flat")
        style.map("Treeview.Heading", background=[("active", c["accent_hover"])])
        style.map("Treeview", background=[("selected", c["accent"])], foreground=[("selected", "#ffffff")])

        style.configure(
            "Vertical.TScrollbar", background=c["bg_alt"], troughcolor=c["bg"],
            bordercolor=c["border"], arrowcolor=c["text_muted"],
        )
        style.configure("Horizontal.TScale", background=c["bg"], troughcolor=c["bg_alt"])

        self.root.configure(background=c["bg"])
        self.station_list.set_theme(c)
        self.map_view.set_theme(c)

    # ---- Browsing menu (Top/All/Favorites/Recent) ---------------------

    def _open_browse_menu(self):
        c = self.colors
        menu = tk.Menu(
            self.root, tearoff=0, bg=c["surface"], fg=c["text"],
            activebackground=c["accent"], activeforeground="#ffffff", borderwidth=0,
        )
        menu.add_command(label="Top Stations", command=self._browse_top_stations)
        menu.add_command(label="All Stations", command=self._browse_all_stations)
        menu.add_separator()
        menu.add_command(label="★ Favorites", command=self._load_favorites)
        menu.add_command(label="🕘 Recently Played", command=self._load_recent)

        x = self.browse_btn.winfo_rootx()
        y = self.browse_btn.winfo_rooty() + self.browse_btn.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _browse_top_stations(self):
        self._clear_filter_fields()
        self._load_top_stations_async()

    def _browse_all_stations(self):
        self._clear_filter_fields()
        self._load_all_stations_async()

    def _load_favorites(self):
        self._clear_filter_fields()
        self._load_generation += 1  # invalidate any network load still in flight
        self._set_current_view_key(None)  # nothing to refresh - this is just settings["favorites"], always live
        stations = list(self.settings.get("favorites", []))
        self._populate_stations(stations)
        if not stations:
            self.status_var.set("No favorites yet - use the ☆ Favorite button to add one.")

    def _load_recent(self):
        self._clear_filter_fields()
        self._load_generation += 1
        self._set_current_view_key(None)
        stations = list(self.settings.get("recent", []))
        self._populate_stations(stations)
        if not stations:
            self.status_var.set("Nothing played yet.")

    # ---- Data loading ----------------------------------------------

    def _load_countries_async(self):
        def worker():
            try:
                countries = self.api.countries()
                labels = ["(any)"] + [f"{name} ({count})" for name, code, count in countries]
                label_to_code = {f"{name} ({count})": code for name, code, count in countries}
                self.root.after(0, lambda: self._populate_countries(labels, label_to_code))
            except Exception as e:
                self.root.after(0, lambda: self._on_countries_failed(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_countries_failed(self, error):
        self.status_var.set(f"Could not load countries: {error}")
        # If we were relying on this to resolve a saved country filter
        # and skipped the startup catalog load as a result, fall back to
        # it now rather than leaving the app empty.
        if not self.stations:
            self._load_all_stations_async()

    def _populate_countries(self, labels, label_to_code):
        self.country_combo["values"] = labels
        self._label_to_code = label_to_code

        # Country codes are stable across sessions but the label text
        # (which includes a station count) usually isn't, so we match on
        # the saved code rather than the exact saved label.
        saved_code = self.settings.get("last_country_code", "")
        restored_label = None
        if saved_code:
            for label, code in label_to_code.items():
                if code == saved_code:
                    restored_label = label
                    break
        self.country_var.set(restored_label if restored_label else "(any)")
        self._update_clear_filters_state()

        # A filter restored from last session replaces the "all stations"
        # load already under way at startup - the load-generation guard
        # means whichever finishes last simply wins, so nothing stale can
        # clobber the correct, filtered result.
        if saved_code or self.search_var.get().strip() or self.tag_var.get().strip():
            self._trigger_search(save=False)

    def _load_top_stations_async(self):
        self._load_with_cache(("top",), lambda on_batch: self.api.top_stations(limit=500), label="top stations")

    def _load_all_stations_async(self):
        """Loads the entire Radio Browser catalog (tens of thousands of
        stations worldwide), not just a top-500 sample - this is what
        the map tab needs to look as dense as radio.garden's.

        Loads progressively: the first 500 land and get shown right away,
        then subsequent pages of 1000 keep arriving and repainting the
        list/map as they come in, so there's always something on screen
        rather than a blank wait for the full catalog."""
        self._load_with_cache(("all",), lambda on_batch: self.api.all_stations(on_batch=on_batch), label="stations")

    def _load_with_cache(self, key, fetch_callable, label):
        """Reuses an already-fetched result for this exact view (same
        Top/All/search+country+tag) instead of hitting the network again
        - e.g. reselecting a country you already browsed this session, or
        clicking "All Stations" twice, is instant. Cache is in-memory
        only (cleared on restart) since the underlying data does change
        over time; the ⟳ Refresh button bypasses it on purpose when you
        want the current view's latest data."""
        self._set_current_view_key(key)
        cached = self._station_cache.get(key)
        if cached is not None:
            self._load_generation += 1  # drop any older network load still in flight for a different view
            self._populate_stations(list(cached))
            return
        self._load_stations_progressive(fetch_callable, label, cache_key=key)

    def _load_for_key(self, key):
        """Reconstructs the right fetch_callable/label for a cache key -
        used by _refresh_current() to re-issue whatever's on screen."""
        if key == ("top",):
            self._load_stations_progressive(
                lambda on_batch: self.api.top_stations(limit=500), "top stations", cache_key=key
            )
        elif key == ("all",):
            self._load_stations_progressive(
                lambda on_batch: self.api.all_stations(on_batch=on_batch), "stations", cache_key=key
            )
        elif key[0] == "search":
            _, name, countrycode, tag = key
            self._load_stations_progressive(
                lambda on_batch: self.api.search(name=name, countrycode=countrycode, tag=tag, on_batch=on_batch),
                "matching stations", cache_key=key,
            )

    def _refresh_current(self):
        """Re-fetches whatever's currently shown from the network,
        bypassing (and replacing) any cached copy - for when you want
        this session's cache to stop being used for this particular view."""
        key = self._current_view_key
        if key is None:
            return  # Favorites/Recent - nothing cached to refresh, they're always live
        self._station_cache.pop(key, None)
        self._load_for_key(key)

    def _set_current_view_key(self, key):
        self._current_view_key = key
        self.refresh_btn.config(state="normal" if key is not None else "disabled")

    def _load_stations_progressive(self, fetch_callable, label, cache_key=None):
        """Runs fetch_callable(on_batch) in a background thread, where
        on_batch(stations_so_far) can be called zero or more times to
        repaint the list/map before the fetch finishes. Every call bumps
        a generation counter first; results tagged with an older
        generation than the current one are silently dropped, so a fast
        second load (e.g. picking a different country right after
        another) can't have its results clobbered by the first one
        finishing late.

        Repainting the list/map is expensive at a few thousand rows (the
        Treeview gets fully cleared and rebuilt, the map's scatter data
        recomputed), and pages can arrive much faster than that - so
        actual repaints are throttled to once every MIN_REPAINT_INTERVAL_SEC
        at most; batches in between just update the status count, which is
        cheap. The very first batch and the final result always repaint,
        so it never looks frozen at the start or stale at the end.

        cache_key, if given, is where the final (non-partial) result gets
        stored in self._station_cache once the load completes, for
        _load_with_cache() to reuse later without hitting the network."""
        self._load_generation += 1
        gen = self._load_generation
        self.status_var.set(f"Loading {label}...")
        last_repaint = {"t": 0.0}

        def on_batch(stations_so_far):
            now = time.monotonic()
            if last_repaint["t"] != 0.0 and now - last_repaint["t"] < MIN_REPAINT_INTERVAL_SEC:
                count = len(stations_so_far)
                self.root.after(0, lambda: self._apply_load_progress_text(gen, count))
                return
            last_repaint["t"] = now
            self.root.after(0, lambda: self._apply_load_result(gen, stations_so_far, True, cache_key))

        def worker():
            try:
                stations = fetch_callable(on_batch)
                self.root.after(0, lambda: self._apply_load_result(gen, stations, False, cache_key))
            except Exception as e:
                self.root.after(0, lambda: self._apply_load_error(gen, label, e))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_load_progress_text(self, gen, count):
        if gen != self._load_generation:
            return
        self.status_var.set(f"{count:,} stations so far, loading more...")

    def _apply_load_result(self, gen, stations, still_loading, cache_key=None):
        if gen != self._load_generation:
            return
        self._populate_stations(stations, still_loading=still_loading)
        if not still_loading and cache_key is not None:
            self._station_cache[cache_key] = stations

    def _apply_load_error(self, gen, label, error):
        if gen != self._load_generation:
            return
        self.status_var.set(f"Error loading {label}: {error}")

    # ---- Search & filters ----------------------------------------------

    def do_search(self):
        if self._search_debounce_id is not None:
            self.root.after_cancel(self._search_debounce_id)
            self._search_debounce_id = None
        self._trigger_search()

    def _on_country_selected(self, _event=None):
        if self._search_debounce_id is not None:
            self.root.after_cancel(self._search_debounce_id)
            self._search_debounce_id = None
        self._trigger_search()

    def _on_filter_typed(self, *_args):
        self._update_clear_filters_state()
        if self._search_debounce_id is not None:
            self.root.after_cancel(self._search_debounce_id)
        self._search_debounce_id = self.root.after(SEARCH_DEBOUNCE_MS, self._debounced_search_fire)

    def _debounced_search_fire(self):
        self._search_debounce_id = None
        self._trigger_search()

    def _trigger_search(self, save=True):
        name = self.search_var.get().strip()
        countrycode = self._selected_country_code()
        tag = self.tag_var.get().strip()

        if save:
            self.settings["last_search"] = name
            self.settings["last_country_code"] = countrycode
            self.settings["last_tag"] = tag
            self._save_settings_soon()

        self._update_clear_filters_state()

        if not name and not countrycode and not tag:
            self._load_all_stations_async()
            return

        # Selecting a country or typing a tag loads every matching
        # station (paginated), not a single capped page - same as "All
        # Stations", just filtered.
        self._load_with_cache(
            ("search", name, countrycode, tag),
            lambda on_batch: self.api.search(name=name, countrycode=countrycode, tag=tag, on_batch=on_batch),
            label="matching stations",
        )

    def _selected_country_code(self):
        label = self.country_var.get()
        if not label or label == "(any)":
            return ""
        return self._label_to_code.get(label, "")

    def _clear_filters(self):
        self._clear_filter_fields()
        self._trigger_search()

    def _clear_filter_fields(self):
        self.search_var.set("")
        self.tag_var.set("")
        self.country_combo.current(0)
        # The .set() calls above each scheduled their own debounced
        # search via _on_filter_typed - cancel whichever is pending now
        # so it can't fire a redundant/stale search later.
        if self._search_debounce_id is not None:
            self.root.after_cancel(self._search_debounce_id)
            self._search_debounce_id = None
        self._update_clear_filters_state()
        self.settings["last_search"] = ""
        self.settings["last_country_code"] = ""
        self.settings["last_tag"] = ""
        self._save_settings_soon()

    def _update_clear_filters_state(self):
        active = bool(self.search_var.get().strip() or self.tag_var.get().strip() or self._selected_country_code())
        self.clear_filters_btn.config(state="normal" if active else "disabled")

    def _populate_stations(self, stations, still_loading=False):
        self.stations = stations
        self.station_list.populate(stations)
        self.map_view.update_stations(stations)
        self._refresh_playing_highlight()
        self._update_favorite_button()
        if still_loading:
            self.status_var.set(f"{len(stations):,} stations so far, loading more...")
        else:
            self.status_var.set(f"{len(stations):,} stations found.")

    # ---- Playback ----------------------------------------------

    def _play_selected(self):
        idx = self.station_list.get_selected_index()
        if idx is None:
            messagebox.showinfo("No selection", "Select a station first.")
            return
        self._play_index(idx)

    def _play_index(self, index):
        """Shared entry point for both the list's double-click and the
        map's click-a-dot callbacks."""
        if index is None or index >= len(self.stations):
            return
        self.station_list.select(index)
        self.play_station(self.stations[index])

    def _on_station_hover(self, index):
        """Echoes station-list hover onto the map - the corresponding dot
        (if it has plottable coordinates) recolors to make it obvious
        which station you're pointing at."""
        self.map_view.highlight_station(index)

    def _on_list_selection_changed(self, _index):
        self._update_favorite_button()

    def _on_sort_requested(self, column):
        """The list view doesn't reorder itself (it only ever holds one
        page of a potentially huge list, so there's no "just reorder
        what's visible" option) - it reports the requested column and we
        resort the actual underlying data, then repopulate everything
        (list + map + highlights) consistently from the new order."""
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False

        def sort_key(s):
            if column == "bitrate":
                try:
                    return int(s.get("bitrate") or 0)
                except (TypeError, ValueError):
                    return -1
            return str(s.get(column) or "").lower()

        sorted_stations = sorted(self.stations, key=sort_key, reverse=self._sort_reverse)
        self._populate_stations(sorted_stations)
        self.station_list.set_sort_indicator(column, self._sort_reverse)

    def _refresh_playing_highlight(self):
        """Finds where the currently-playing station (if any) sits in the
        current station list, by stationuuid, and tells the list/map to
        mark it green there. Called whenever what's playing changes, or
        whenever the list itself changes (a new search may or may not
        still contain the playing station)."""
        idx = None
        if self.current_station is not None:
            uuid = self.current_station.get("stationuuid")
            if uuid:
                for i, s in enumerate(self.stations):
                    if s.get("stationuuid") == uuid:
                        idx = i
                        break
        self.station_list.mark_playing(idx)
        self.map_view.mark_playing(idx)

    def _get_favorite_target(self):
        """The station the ☆ Favorite button would act on: whatever's
        selected in the list, falling back to whatever's playing if
        nothing's selected."""
        idx = self.station_list.get_selected_index()
        if idx is not None and idx < len(self.stations):
            return self.stations[idx]
        return self.current_station

    def _toggle_favorite(self):
        station = self._get_favorite_target()
        if not station:
            return
        toggle_favorite(self.settings, station)
        self._save_settings_soon()
        self._update_favorite_button()

    def _update_favorite_button(self):
        station = self._get_favorite_target()
        if station and is_favorite(self.settings, station):
            self.favorite_btn.config(text="★ Favorited")
        else:
            self.favorite_btn.config(text="☆ Favorite")

    def play_station(self, station):
        if not self.player.is_available:
            messagebox.showerror("Playback unavailable", "VLC engine not loaded. Install VLC and restart.")
            return

        url = station.get("url_resolved") or station.get("url")
        if not url:
            messagebox.showerror("No stream URL", "This station has no playable URL.")
            return

        self.status_var.set(f"Connecting to {station.get('name')}...")
        self.retry_btn.config(state="disabled")  # a fresh attempt supersedes any earlier failure

        def worker():
            try:
                vol = 0 if self._muted else self.volume_var.get()
                self.player.play(url, volume=vol)
                self.current_station = station
                self.root.after(
                    0, lambda: self.now_playing_var.set(f"▶ {station.get('name')} — {station.get('country', '')}")
                )
                self.root.after(0, lambda: self.status_var.set("Playing."))
                self.root.after(0, self._refresh_playing_highlight)
                self.root.after(0, self._update_favorite_button)
                add_recent(self.settings, station)
                self.root.after(0, self._save_settings_soon)
                uuid = station.get("stationuuid")
                if uuid:
                    self.api.register_click(uuid)
            except Exception as e:
                self.root.after(0, lambda: self.status_var.set(f"Playback error: {e}"))
                self.root.after(0, lambda: self._mark_playback_failed(station))

        threading.Thread(target=worker, daemon=True).start()

    def stop_playback(self):
        self.player.stop()
        self.current_station = None
        self.now_playing_var.set("Not playing")
        self.status_var.set("Stopped.")
        self.retry_btn.config(state="disabled")
        self._refresh_playing_highlight()
        self._update_favorite_button()

    def _retry_playback(self):
        if self._failed_station:
            self.play_station(self._failed_station)

    def _mark_playback_failed(self, station):
        self._failed_station = station
        self.retry_btn.config(state="normal" if station else "disabled")

    def _toggle_mute(self):
        self._muted = not self._muted
        if self._muted:
            self.player.set_volume(0)
            self.mute_btn.config(text="🔇")
        else:
            self.player.set_volume(self.volume_var.get())
            self.mute_btn.config(text="🔊")
        self.settings["muted"] = self._muted
        self._save_settings_soon()

    def _on_volume_change(self, _value):
        vol = self.volume_var.get()
        if self._muted:
            self._muted = False
            self.mute_btn.config(text="🔊")
            self.settings["muted"] = False
        self.player.set_volume(vol)
        self.settings["volume"] = vol
        self._save_settings_soon()

    # ---- Settings persistence -----------------------------------------

    def _save_settings_soon(self):
        if self._settings_save_id is not None:
            self.root.after_cancel(self._settings_save_id)
        self._settings_save_id = self.root.after(SETTINGS_SAVE_DEBOUNCE_MS, self._save_settings_now)

    def _save_settings_now(self):
        self._settings_save_id = None
        save_settings(self.settings)

    # ---- VLC event callbacks (run on VLC's internal thread, so only
    # touch Tk state via root.after) ----------------------------------

    def _on_stream_error(self, event):
        failed_station = self.current_station
        name = failed_station.get("name", "station") if failed_station else "station"
        self.current_station = None
        self.root.after(0, lambda: self.status_var.set(f"Could not play '{name}' — stream is unreachable or offline."))
        self.root.after(0, lambda: self.now_playing_var.set("Not playing"))
        self.root.after(0, lambda: self._mark_playback_failed(failed_station))
        self.root.after(0, self._refresh_playing_highlight)
        self.root.after(0, self._update_favorite_button)

    def _on_stream_playing(self, event):
        self.root.after(0, lambda: self.status_var.set("Playing."))