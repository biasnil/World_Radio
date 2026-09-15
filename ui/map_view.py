"""The flat 2D map widget: matplotlib figure embedded in tkinter, drawing
country outlines plus station markers, with click-to-play wired through a
callback. Degrades to an install hint if matplotlib/numpy aren't present.

Self-contained: knows how to draw a list of station dicts as markers and
report which one the user clicked. Doesn't know about the API or the
player. Takes its colors from the caller (a light/dark theme dict) rather
than owning its own palette, so it stays in sync with the rest of the UI.

Performance model: rather than plotting one dot per station always (which
at the full catalog's scale, tripled for wrap-around, is well over
100,000 points every redraw), stations are grouped into "bins" based on
screen proximity at the current zoom level - see _recompute_bins(). A bin
with more than one station renders as a single, larger marker; the
cluster-picker menu (already used for exact pixel-overlap) is reused to
choose a specific station out of a bin. Bins are only computed for
stations within (or near) the current view, and only recomputed when the
view actually settles - not on every intermediate pan/zoom frame - so the
amount of work per redraw stays roughly constant regardless of how many
thousands of stations are loaded, whether zoomed out to the whole world
or in on a city.

Hover recoloring itself is also kept cheap independently of binning: the
three semantic colors (normal/playing/hover) are parsed to RGBA once
(_refresh_semantic_colors), and a highlight change patches only the 1-2
affected marker positions in persistent numpy arrays rather than
rebuilding/re-parsing colors for everything on every mouse move.
"""

import math
import threading
import time
import tkinter as tk
from tkinter import ttk

try:
    import numpy as np
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.colors import to_rgba
except ImportError:
    np = None
    matplotlib = None
    to_rgba = None

from core.geodata import load_world_geojson
from common.theme import HOVER_HIGHLIGHT_COLOR, PLAYING_HIGHLIGHT_COLOR

# How close (in screen pixels) a click has to land to a marker to count
# as a hit on it. Reused as the hover radius too, for a consistent feel
# between clicking and hovering.
CLICK_RADIUS_PX = 15

# Zoom-in/out multiplier applied per scroll notch, and the world-degree
# span we won't zoom in/out past (so you can't scroll to nothing, or
# scroll out past the initial whole-world view).
ZOOM_STEP = 1.25
MIN_SPAN_DEG = 2.0
MAX_SPAN_DEG = 360.0

NORMAL_DOT_SIZE = 18
MAX_MARKER_SIZE = 240  # cap on how big a single bin's marker can grow regardless of how many stations it holds
HOVER_SIZE_BOOST = 35
PLAYING_SIZE_BOOST = 35

# Land and station markers get considered at these three longitude
# offsets so the view can pan continuously past either edge without
# hitting empty space - dragging past -180 reveals the +360 copy's left
# edge, which lines up exactly with the primary copy's right edge
# (they're identical, 360 degrees being a full trip around). Only the
# offsets actually relevant to the current view get used when binning
# stations (see _relevant_offsets) - land is drawn with all three
# unconditionally since that's a one-time cost per geometry (re)draw.
WRAP_OFFSETS = (-360, 0, 360)

# Two stations land in the same bin (and render as one merged marker) if
# they'd fall within this many screen pixels of each other at the
# current zoom level - so bin size in degrees shrinks as you zoom in,
# and a merged marker splits back apart once its members would actually
# be visually distinguishable.
CLUSTER_BIN_PX = 42

# Bins are recomputed only after the view has settled - not on every
# intermediate pan-drag frame or scroll notch - debounced by this many
# milliseconds of no further view changes.
BIN_RECOMPUTE_DEBOUNCE_MS = 120

# How far past the visible edges (as a fraction of the current view
# span) to still bin stations, so a small pan doesn't show an empty gap
# right at the edge before the next recompute catches up.
VIEW_MARGIN_FRAC = 0.15

_HOVER_THROTTLE_SEC = 0.04


class MapView:
    def __init__(self, parent, on_station_clicked, colors):
        """on_station_clicked(index): called with the index into the most
        recent update_stations() list when the user clicks a plotted
        marker representing a single station (or picks one from the
        cluster menu when a marker represents several merged stations).
        colors: the active theme dict (see common/theme.py) - set_theme()
        can be called later to re-color for a light/dark switch."""
        self._on_station_clicked = on_station_clicked
        self._colors = colors
        self._stations = []
        self._geojson = None  # cached so a theme switch can redraw without a re-fetch

        # The current set of plotted bins, rebuilt by _recompute_bins().
        self._bins = {}  # bin_key -> {"idxs": [...], "lon": centroid, "lat": centroid, "count": n}
        self._bin_keys = []  # parallel to the plotted array - bin_keys[pos] is that marker's key
        self._key_to_pos = {}  # bin_key -> position in the plotted array
        self._station_to_bin = {}  # station index -> bin_key, for whichever bins are currently plotted
        self._map_points = None
        self._station_scatter = None

        self._highlighted_index = None
        self._playing_index = None
        self._face_colors = None  # numpy array, shape (n, 4): current RGBA per plotted marker
        self._sizes_arr = None  # numpy array, shape (n,): current size per plotted marker
        self._normal_rgba = None
        self._playing_rgba = None
        self._hover_rgba = None

        self._panning = False
        self._pan_start_px = None
        self._pan_start_xlim = None
        self._pan_start_ylim = None
        self._last_hover_check = 0.0
        self._bin_recompute_after_id = None

        self.available = matplotlib is not None
        self.fig = None
        self.ax = None
        self.canvas = None

        if not self.available:
            ttk.Label(
                parent,
                text="Map view needs matplotlib and numpy.\n\nInstall with:\n    pip install matplotlib numpy",
                padding=30, justify="center",
            ).pack(expand=True)
            return

        self._refresh_semantic_colors()

        self.fig = Figure(figsize=(6, 4), dpi=100, facecolor=self._colors["map_bg"])
        self.ax = self.fig.add_subplot(111)
        self._reset_axes()
        self.ax.text(0, 10, "Loading map...", color=self._colors["text_muted"], ha="center", va="center", fontsize=11)

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        toolbar_frame = ttk.Frame(parent)
        toolbar_frame.pack(fill="x")
        toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame)
        toolbar.update()
        ttk.Button(toolbar_frame, text="Reset View", command=self.reset_view).pack(side="right", padx=6)

        self.canvas.mpl_connect("button_press_event", self._on_click)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("figure_leave_event", self._on_figure_leave)
        self.canvas.draw()

        self._load_geometry_async()

    def reset_view(self):
        """Snaps the map back to the initial whole-world view. A dedicated
        button rather than relying only on the toolbar's home button,
        since our own scroll-zoom/Ctrl-drag panning below move the axes
        directly and don't go through the toolbar's view-history stack."""
        if not self.available or self.ax is None:
            return
        self.ax.set_xlim(-180, 180)
        self.ax.set_ylim(-60, 84)
        self.canvas.draw_idle()
        self._recompute_bins()

    def _rewrap_xlim(self):
        """Keeps the pan position anchored near the primary -180..180
        copy of the map by snapping the view sideways by a full 360
        degrees whenever it drifts past it - land is drawn at all three
        WRAP_OFFSETS, so shifting by exactly 360 lands on an identical
        copy of the content and the view doesn't visibly change at all;
        it just keeps panning from running out of drawn world after a
        couple of wraps."""
        xlim = self.ax.get_xlim()
        center = (xlim[0] + xlim[1]) / 2
        if center > 180:
            shift = -360
        elif center < -180:
            shift = 360
        else:
            return
        self.ax.set_xlim(xlim[0] + shift, xlim[1] + shift)

    def _refresh_semantic_colors(self):
        """Parses the three semantic colors (normal/playing/hover) to RGBA
        once, so per-marker recoloring later is just numpy array writes,
        never string parsing."""
        self._normal_rgba = np.array(to_rgba(self._colors["map_station"]))
        self._playing_rgba = np.array(to_rgba(PLAYING_HIGHLIGHT_COLOR))
        self._hover_rgba = np.array(to_rgba(HOVER_HIGHLIGHT_COLOR))

    def set_theme(self, colors):
        """Re-colors the map for a light/dark switch. If the land outlines
        are already loaded, redraws them in place using the cached
        GeoJSON instead of re-fetching - only the colors changed, not the
        geometry."""
        self._colors = colors
        if not self.available:
            return
        self._refresh_semantic_colors()
        self.fig.set_facecolor(colors["map_bg"])
        if self._geojson is not None:
            self._draw_geometry(self._geojson)
        else:
            self._reset_axes()
            self.canvas.draw_idle()

    def _reset_axes(self):
        self.ax.clear()
        self.ax.set_facecolor(self._colors["map_bg"])
        self.ax.set_xlim(-180, 180)
        self.ax.set_ylim(-60, 84)
        self.ax.set_aspect("equal")
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        for spine in self.ax.spines.values():
            spine.set_visible(False)

    def _load_geometry_async(self):
        def worker():
            try:
                geojson = load_world_geojson()
                self.canvas.get_tk_widget().after(0, lambda: self._draw_geometry(geojson))
            except Exception as e:
                self.canvas.get_tk_widget().after(0, lambda: self._draw_load_failed(e))

        threading.Thread(target=worker, daemon=True).start()

    def _draw_geometry(self, geojson):
        self._geojson = geojson
        self._reset_axes()

        def draw_ring(ring):
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            for offset in WRAP_OFFSETS:
                self.ax.fill(
                    [x + offset for x in xs], ys, facecolor=self._colors["map_land"],
                    edgecolor=self._colors["map_land_edge"], linewidth=0.4, zorder=1,
                )

        for feature in geojson.get("features", []):
            geom = feature.get("geometry") or {}
            gtype = geom.get("type")
            coords = geom.get("coordinates", [])
            if gtype == "Polygon":
                if coords:
                    draw_ring(coords[0])  # outer ring only; holes ignored for simplicity
            elif gtype == "MultiPolygon":
                for polygon in coords:
                    if polygon:
                        draw_ring(polygon[0])

        self._station_scatter = self.ax.scatter([], [], alpha=0.85, zorder=2)
        self._recompute_bins()
        self.canvas.draw_idle()

    def _draw_load_failed(self, error):
        self.ax.clear()
        self.ax.set_facecolor(self._colors["map_bg"])
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.ax.text(
            0.5, 0.5, f"Could not load map outlines:\n{error}",
            color="#e07a5f", ha="center", va="center", transform=self.ax.transAxes, fontsize=10,
        )
        self.canvas.draw_idle()

    # ---- Station binning / plotting -----------------------------------

    def update_stations(self, stations):
        """Call whenever the station list (search/top results) changes."""
        self._stations = stations
        self._highlighted_index = None  # old index likely no longer means the same station
        self._playing_index = None  # caller re-marks it via mark_playing() once it knows the new index
        if self._station_scatter is not None:
            self._recompute_bins()

    def _relevant_wrap_offsets(self, xlim, margin):
        """Which of the three WRAP_OFFSETS copies could actually have any
        content inside the current (margin-padded) view - usually just
        one, sometimes two near a wrap boundary, letting _recompute_bins
        skip scanning stations for copies that can't be visible at all."""
        lo, hi = xlim[0] - margin, xlim[1] + margin
        return [off for off in WRAP_OFFSETS if (off - 180) <= hi and (off + 180) >= lo]

    def _bin_size_degrees(self, xlim):
        """Converts the fixed pixel-based bin size (CLUSTER_BIN_PX) into
        degrees at the current zoom level, using the axes' actual pixel
        width - so bins shrink as you zoom in and grow as you zoom out,
        always representing roughly the same visual size on screen."""
        try:
            ax_width_px = max(1.0, self.ax.get_window_extent().width)
        except Exception:
            ax_width_px = 600.0  # sane fallback if called before the canvas has ever been drawn
        xspan = xlim[1] - xlim[0]
        return max(0.01, (xspan / ax_width_px) * CLUSTER_BIN_PX)

    def _recompute_bins(self):
        """Rebuilds the plotted marker set from self._stations, merging
        stations that would land within CLUSTER_BIN_PX screen pixels of
        each other into one marker. Only stations within (or near) the
        current view are considered at all - this is what keeps a
        redraw's cost roughly constant regardless of the total catalog
        size, since a world-view redraw only ever has to deal with
        however many bins fit on screen, not however many stations are
        loaded."""
        if self._station_scatter is None:
            return

        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        xspan = xlim[1] - xlim[0]
        yspan = ylim[1] - ylim[0]
        margin_x = xspan * VIEW_MARGIN_FRAC
        margin_y = yspan * VIEW_MARGIN_FRAC
        bin_deg = self._bin_size_degrees(xlim)
        offsets = self._relevant_wrap_offsets(xlim, margin_x)

        x_lo, x_hi = xlim[0] - margin_x, xlim[1] + margin_x
        y_lo, y_hi = ylim[0] - margin_y, ylim[1] + margin_y

        bins = {}  # (offset, col, row) -> [(station_idx, wx, lat), ...]
        for i, s in enumerate(self._stations):
            lat, lon = s.get("geo_lat"), s.get("geo_long")
            if lat is None or lon is None:
                continue
            if lat == 0 and lon == 0:
                continue  # common placeholder for "unknown" in the dataset
            for offset in offsets:
                wx = lon + offset
                if not (x_lo <= wx <= x_hi and y_lo <= lat <= y_hi):
                    continue
                col = math.floor((wx - xlim[0]) / bin_deg)
                row = math.floor((lat - ylim[0]) / bin_deg)
                bins.setdefault((offset, col, row), []).append((i, wx, lat))

        bin_keys = list(bins.keys())
        n = len(bin_keys)

        new_bins = {}
        points = np.empty((n, 2))
        for pos, key in enumerate(bin_keys):
            members = bins[key]
            idxs = [m[0] for m in members]
            cx = sum(m[1] for m in members) / len(members)
            cy = sum(m[2] for m in members) / len(members)
            points[pos] = (cx, cy)
            new_bins[key] = {"idxs": idxs, "lon": cx, "lat": cy, "count": len(idxs)}

        self._bins = new_bins
        self._bin_keys = bin_keys
        self._key_to_pos = {k: p for p, k in enumerate(bin_keys)}
        self._station_to_bin = {}
        for key, data in new_bins.items():
            for idx in data["idxs"]:
                self._station_to_bin[idx] = key

        self._map_points = points
        self._face_colors = np.tile(self._normal_rgba, (n, 1)) if n else np.empty((0, 4))
        self._sizes_arr = np.array([self._size_for_count(new_bins[k]["count"]) for k in bin_keys], dtype=float)

        self._station_scatter.set_offsets(self._map_points)
        self._apply_playing_and_hover()

    def _size_for_count(self, count):
        if count <= 1:
            return NORMAL_DOT_SIZE
        return min(MAX_MARKER_SIZE, NORMAL_DOT_SIZE + 22 * math.log2(count + 1))

    def _schedule_bin_recompute(self, delay_ms=BIN_RECOMPUTE_DEBOUNCE_MS):
        """Debounced recompute - used after a scroll-zoom notch, since
        those can arrive in a rapid burst. Panning calls _recompute_bins()
        directly instead, once at drag-end, since a mouse release is
        already a single discrete "the view has settled" moment."""
        widget = self.canvas.get_tk_widget()
        if self._bin_recompute_after_id is not None:
            widget.after_cancel(self._bin_recompute_after_id)
        self._bin_recompute_after_id = widget.after(delay_ms, self._fire_scheduled_recompute)

    def _fire_scheduled_recompute(self):
        self._bin_recompute_after_id = None
        self._recompute_bins()

    # ---- Highlighting (hover / currently playing) ----------------------

    def highlight_station(self, index):
        """Recolors the marker representing the given station index (into
        the most recent update_stations() list) so it stands out - used
        to show which marker corresponds to a station being hovered
        elsewhere in the UI, e.g. the station list. Pass None to clear.
        A no-op if that station isn't part of any currently-plotted bin
        (out of view, no coordinates, or bins not computed yet).

        Patches only the specific marker(s) that changed rather than
        rebuilding the whole color array - this is the hot path (fires
        on every mouse move over the map), so it matters."""
        if index == self._highlighted_index:
            return
        if self._face_colors is None:
            self._highlighted_index = index
            return

        affected = set()
        old_key = self._station_to_bin.get(self._highlighted_index)
        if old_key is not None and old_key in self._key_to_pos:
            affected.add(self._key_to_pos[old_key])
        self._highlighted_index = index
        new_key = self._station_to_bin.get(index)
        if new_key is not None and new_key in self._key_to_pos:
            affected.add(self._key_to_pos[new_key])

        for pos in affected:
            self._apply_style_to(pos)
        if affected:
            self._push_dot_style()

    def mark_playing(self, index):
        """Persistently marks the marker for the given station index
        green, as the currently-playing station - unlike
        highlight_station() above, this isn't cleared by the mouse moving
        away. Pass None to clear. A no-op if that station isn't part of
        any currently-plotted bin."""
        if index == self._playing_index:
            return
        if self._face_colors is None:
            self._playing_index = index
            return

        affected = set()
        old_key = self._station_to_bin.get(self._playing_index)
        if old_key is not None and old_key in self._key_to_pos:
            affected.add(self._key_to_pos[old_key])
        self._playing_index = index
        new_key = self._station_to_bin.get(index)
        if new_key is not None and new_key in self._key_to_pos:
            affected.add(self._key_to_pos[new_key])

        for pos in affected:
            self._apply_style_to(pos)
        if affected:
            self._push_dot_style()

    def _style_for(self, pos):
        """The correct color/size for one marker position given the
        CURRENT playing/hover indices - hover takes precedence over
        playing, which takes precedence over the plain normal look. Size
        is the bin's own count-based size plus a highlight boost, not a
        fixed replacement, so a highlighted large cluster still reads as
        a cluster."""
        key = self._bin_keys[pos]
        base_size = self._size_for_count(self._bins[key]["count"])
        hover_key = self._station_to_bin.get(self._highlighted_index)
        playing_key = self._station_to_bin.get(self._playing_index)
        if key == hover_key:
            return self._hover_rgba, base_size + HOVER_SIZE_BOOST
        if key == playing_key:
            return self._playing_rgba, base_size + PLAYING_SIZE_BOOST
        return self._normal_rgba, base_size

    def _apply_style_to(self, pos):
        color, size = self._style_for(pos)
        self._face_colors[pos] = color
        self._sizes_arr[pos] = size

    def _apply_playing_and_hover(self):
        """Full rebuild of the playing/hover overlay on top of the (just
        freshly rebuilt) normal-colored arrays - used after a bin
        recompute, where everything needs to be right from a blank
        slate. Not used on the hover hot path; see highlight_station()."""
        if self._station_scatter is None or self._face_colors is None:
            return
        for pos in range(len(self._bin_keys)):
            self._apply_style_to(pos)
        self._push_dot_style()

    def _push_dot_style(self):
        """Sends the current _face_colors/_sizes_arr numpy arrays to the
        scatter plot. Passing pre-parsed RGBA arrays (rather than hex
        color strings) is what keeps this cheap - no per-marker string
        parsing happens here."""
        self._station_scatter.set_facecolors(self._face_colors)
        self._station_scatter.set_sizes(self._sizes_arr)
        self.canvas.draw_idle()

    # ---- Zoom ------------------------------------------------------

    def _on_scroll(self, event):
        """Mouse-wheel zoom, centered on the cursor position (same feel as
        radio.garden / Google Maps) rather than always zooming on the
        center of the view."""
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            return

        if event.button == "up":
            factor = 1 / ZOOM_STEP  # zoom in
        elif event.button == "down":
            factor = ZOOM_STEP  # zoom out
        else:
            return

        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        xspan = (xlim[1] - xlim[0]) * factor
        yspan = (ylim[1] - ylim[0]) * factor

        # Clamp so you can't zoom in to nothing or out past the whole world.
        if xspan < MIN_SPAN_DEG or xspan > MAX_SPAN_DEG:
            return

        xdata, ydata = event.xdata, event.ydata
        relx = (xlim[1] - xdata) / (xlim[1] - xlim[0])
        rely = (ylim[1] - ydata) / (ylim[1] - ylim[0])

        self.ax.set_xlim(xdata - xspan * (1 - relx), xdata + xspan * relx)
        self.ax.set_ylim(ydata - yspan * (1 - rely), ydata + yspan * rely)
        self._rewrap_xlim()
        self.canvas.draw_idle()
        self._schedule_bin_recompute()  # debounced - a fast scroll burst only recomputes once it stops

    # ---- Clicking / cluster picking ---------------------------------

    def _on_click(self, event):
        if event.inaxes != self.ax or event.xdata is None:
            return

        # Middle-click drag, or Ctrl+left-click drag, pans the map - both
        # as alternatives to toggling the toolbar's dedicated pan tool.
        if event.button == 2 or (event.button == 1 and event.key == "control"):
            self._panning = True
            self._pan_start_px = (event.x, event.y)
            self._pan_start_xlim = self.ax.get_xlim()
            self._pan_start_ylim = self.ax.get_ylim()
            return

        if event.button != 1:
            return  # right-click etc. - nothing bound here

        if self._map_points is None or len(self._map_points) == 0:
            return

        pixel_points = self.ax.transData.transform(self._map_points)
        click_px = np.array([event.x, event.y])
        dists = np.hypot(pixel_points[:, 0] - click_px[0], pixel_points[:, 1] - click_px[1])
        nearest = int(np.argmin(dists))
        if dists[nearest] > CLICK_RADIUS_PX:
            return

        key = self._bin_keys[nearest]
        idxs = self._bins[key]["idxs"]
        if len(idxs) == 1:
            self._on_station_clicked(idxs[0])
        else:
            self._show_cluster_menu(idxs, event)

    def _on_motion(self, event):
        if self._panning and self._pan_start_px is not None:
            if event.x is None or event.y is None:
                return

            # Convert the pixel-space drag distance into data-space
            # (degrees) via the axes' own transform, so pan speed tracks
            # the current zoom level rather than being a fixed number of
            # degrees per pixel. Bins are NOT recomputed mid-drag - the
            # already-plotted markers just slide with the view, which is
            # correct (their data-space positions haven't changed) and
            # cheap (only however many markers are currently plotted get
            # redrawn, regardless of catalog size).
            inv = self.ax.transData.inverted()
            x0, y0 = inv.transform(self._pan_start_px)
            x1, y1 = inv.transform((event.x, event.y))
            ddx, ddy = x1 - x0, y1 - y0

            xlim, ylim = self._pan_start_xlim, self._pan_start_ylim
            self.ax.set_xlim(xlim[0] - ddx, xlim[1] - ddx)
            self.ax.set_ylim(ylim[0] - ddy, ylim[1] - ddy)
            self._rewrap_xlim()
            self.canvas.draw_idle()
            return

        now = time.monotonic()
        if now - self._last_hover_check < _HOVER_THROTTLE_SEC:
            return
        self._last_hover_check = now
        self._update_hover(event)

    def _update_hover(self, event):
        """Highlights whichever marker is under the cursor, teal, directly
        on the map - the same highlight the station list drives via
        highlight_station(), but triggered locally so it's visible even
        without the list involved at all."""
        if (
            event.inaxes != self.ax or event.x is None or event.y is None
            or self._map_points is None or len(self._map_points) == 0
        ):
            if self._highlighted_index is not None:
                self.highlight_station(None)
            return

        pixel_points = self.ax.transData.transform(self._map_points)
        hover_px = np.array([event.x, event.y])
        dists = np.hypot(pixel_points[:, 0] - hover_px[0], pixel_points[:, 1] - hover_px[1])
        nearest = int(np.argmin(dists))

        if dists[nearest] <= CLICK_RADIUS_PX:
            key = self._bin_keys[nearest]
            # Highlighting picks any one member as the "representative"
            # station for this marker - since they're all merged into
            # the same visual dot, any of them lights up the same thing.
            self.highlight_station(self._bins[key]["idxs"][0])
        elif self._highlighted_index is not None:
            self.highlight_station(None)

    def _on_figure_leave(self, _event):
        if self._highlighted_index is not None:
            self.highlight_station(None)

    def _on_release(self, event):
        was_panning = self._panning
        self._panning = False
        self._pan_start_px = None
        if was_panning:
            self._recompute_bins()  # the view just settled - refresh what's near it now

    def _show_cluster_menu(self, station_indices, event):
        """Several stations were merged into one marker (or happen to
        land in exactly the same spot) - pop up a small menu at the
        click point so the user can choose which one to play. The menu's
        own active-item look stays the normal orange accent, but as you
        move through the entries, the marker on the map lights up teal
        too - the same "you're pointing at this one" cue as hovering the
        station list, just reached from inside the menu instead."""
        widget = self.canvas.get_tk_widget()
        # matplotlib's event.y is measured from the bottom of the canvas;
        # Tk screen coordinates are measured from the top, so flip it.
        x_root = widget.winfo_rootx() + int(event.x)
        y_root = widget.winfo_rooty() + (widget.winfo_height() - int(event.y))

        c = self._colors
        MAX_MENU_ITEMS = 25  # keep a very dense cluster from producing an unusable menu
        menu = tk.Menu(
            widget, tearoff=0, bg=c["surface"], fg=c["text"],
            activebackground=c["accent"], activeforeground="#ffffff",
            disabledforeground=c["text_muted"], borderwidth=0,
        )
        shown = station_indices[:MAX_MENU_ITEMS]
        for idx in shown:
            s = self._stations[idx]
            name = s.get("name") or "?"
            country = s.get("country") or ""
            label = f"{name}  —  {country}" if country else name
            menu.add_command(label=label, command=lambda i=idx: self._on_station_clicked(i))
        if len(station_indices) > MAX_MENU_ITEMS:
            menu.add_separator()
            menu.add_command(
                label=f"...and {len(station_indices) - MAX_MENU_ITEMS} more here (zoom in to narrow down)",
                state="disabled",
            )

        def on_menu_select(_event=None):
            try:
                active = menu.index("active")
            except tk.TclError:
                active = None
            if active is not None and 0 <= active < len(shown):
                self.highlight_station(shown[active])
            else:
                self.highlight_station(None)  # over the separator/disabled "...and N more" row, or nothing

        menu.bind("<<MenuSelect>>", on_menu_select)

        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()
            self.highlight_station(None)  # clear whatever was highlighted while browsing the menu