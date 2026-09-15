"""The flat 2D map widget: matplotlib figure embedded in tkinter, drawing
country outlines plus station dots, with click-to-play wired through a
callback. Degrades to an install hint if matplotlib/numpy aren't present.

Self-contained: knows how to draw a list of station dicts as dots and
report which one the user clicked. Doesn't know about the API or the
player. Takes its colors from the caller (a light/dark theme dict) rather
than owning its own palette, so it stays in sync with the rest of the UI.
"""

import threading
import tkinter as tk
from tkinter import ttk

try:
    import numpy as np
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
except ImportError:
    np = None
    matplotlib = None

from core.geodata import load_world_geojson
from common.theme import HOVER_HIGHLIGHT_COLOR, PLAYING_HIGHLIGHT_COLOR

# How close (in screen pixels) a click has to land to a dot to count as a
# hit on it. With only a few hundred stations this rarely matters, but
# with the full station catalog loaded, many stations sit close enough
# together on a world-scale map that several dots fall within this radius
# of a single click - that's a "cluster" and triggers the picker menu
# below instead of a single guessed pick. Reused as the hover radius too,
# for a consistent feel between clicking and hovering.
CLICK_RADIUS_PX = 15

# Zoom-in/out multiplier applied per scroll notch, and the world-degree
# span we won't zoom in/out past (so you can't scroll to nothing, or
# scroll out past the initial whole-world view).
ZOOM_STEP = 1.25
MIN_SPAN_DEG = 2.0
MAX_SPAN_DEG = 360.0

HOVER_HIGHLIGHT_SIZE = 70
PLAYING_HIGHLIGHT_SIZE = 70
NORMAL_DOT_SIZE = 18

# Land and station dots get drawn three times side by side, at these
# longitude offsets, so the view can pan continuously past either edge
# without hitting empty space - dragging past -180 reveals the +360
# copy's left edge, which lines up exactly with the primary copy's right
# edge (they're identical, 360 degrees being a full trip around).
WRAP_OFFSETS = (-360, 0, 360)


class MapView:
    def __init__(self, parent, on_station_clicked, colors):
        """on_station_clicked(index): called with the index into the most
        recent update_stations() list when the user clicks a plotted dot
        (or picks one from the cluster menu when several dots overlap).
        colors: the active theme dict (see common/theme.py) - set_theme()
        can be called later to re-color for a light/dark switch."""
        self._on_station_clicked = on_station_clicked
        self._colors = colors
        self._stations = []
        self._map_points = None
        self._map_station_idx = []
        self._idx_to_point_pos = {}
        self._highlighted_index = None
        self._playing_index = None
        self._station_scatter = None
        self._geojson = None  # cached so a theme switch can redraw without a re-fetch

        self._panning = False
        self._pan_start_px = None
        self._pan_start_xlim = None
        self._pan_start_ylim = None

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

    def _rewrap_xlim(self):
        """Keeps the pan position anchored near the primary -180..180
        copy of the map by snapping the view sideways by a full 360
        degrees whenever it drifts past it. Since the land/dots are
        drawn three times over (WRAP_OFFSETS), shifting by exactly 360
        lands on an identical copy of the content - the view doesn't
        visibly change at all, it just keeps panning from running out of
        drawn world after a couple of wraps."""
        xlim = self.ax.get_xlim()
        center = (xlim[0] + xlim[1]) / 2
        if center > 180:
            shift = -360
        elif center < -180:
            shift = 360
        else:
            return
        self.ax.set_xlim(xlim[0] + shift, xlim[1] + shift)

    def set_theme(self, colors):
        """Re-colors the map for a light/dark switch. If the land outlines
        are already loaded, redraws them in place using the cached
        GeoJSON instead of re-fetching - only the colors changed, not the
        geometry."""
        self._colors = colors
        if not self.available:
            return
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

        self._station_scatter = self.ax.scatter([], [], s=18, c=self._colors["map_station"], alpha=0.85, zorder=2)
        self._replot_stations()
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

    def update_stations(self, stations):
        """Call whenever the station list (search/top results) changes."""
        self._stations = stations
        self._highlighted_index = None  # old index likely no longer means the same station
        self._playing_index = None  # caller re-marks it via mark_playing() once it knows the new index
        if self._station_scatter is not None:
            self._replot_stations()

    def highlight_station(self, index):
        """Recolors the map dot for the given station index (into the most
        recent update_stations() list) so it stands out - used to show
        which dot corresponds to a station being hovered elsewhere in the
        UI, e.g. the station list. Pass None to clear. A no-op if that
        station has no plottable coordinates (never had a dot to begin
        with)."""
        self._highlighted_index = index
        self._apply_colors()

    def mark_playing(self, index):
        """Persistently marks the dot for the given station index green,
        as the currently-playing station - unlike highlight_station()
        above, this isn't cleared by the mouse moving away. Pass None to
        clear. A no-op if that station has no plottable coordinates."""
        self._playing_index = index
        self._apply_colors()

    def _apply_colors(self):
        if self._station_scatter is None or self._map_points is None:
            return
        n = len(self._map_points)
        if n == 0:
            return
        colors = [self._colors["map_station"]] * n
        sizes = [NORMAL_DOT_SIZE] * n

        # Playing (persistent) applied first, hover (transient) applied
        # second so it visually wins if you're pointing at the same dot
        # that's currently playing. Each station has up to 3 plotted
        # positions (one per wrap-around copy) - color every one of them,
        # since whichever copy happens to be in view should still show it.
        for pos in self._idx_to_point_pos.get(self._playing_index, ()):
            colors[pos] = PLAYING_HIGHLIGHT_COLOR
            sizes[pos] = PLAYING_HIGHLIGHT_SIZE

        for pos in self._idx_to_point_pos.get(self._highlighted_index, ()):
            colors[pos] = HOVER_HIGHLIGHT_COLOR
            sizes[pos] = HOVER_HIGHLIGHT_SIZE

        self._station_scatter.set_color(colors)
        self._station_scatter.set_sizes(sizes)
        self.canvas.draw_idle()

    def _replot_stations(self):
        points = []
        idxs = []
        for i, s in enumerate(self._stations):
            lat, lon = s.get("geo_lat"), s.get("geo_long")
            if lat is None or lon is None:
                continue
            if lat == 0 and lon == 0:
                continue  # common placeholder for "unknown" in the dataset
            for offset in WRAP_OFFSETS:
                points.append((lon + offset, lat))
                idxs.append(i)

        self._map_station_idx = idxs
        self._idx_to_point_pos = {}
        for pos, orig in enumerate(idxs):
            self._idx_to_point_pos.setdefault(orig, []).append(pos)
        self._map_points = np.array(points) if points else np.empty((0, 2))
        self._station_scatter.set_offsets(self._map_points)
        self._apply_colors()

        if self.canvas is not None:
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

        # Compare in pixel space (not data/degree space) so the click
        # tolerance stays sane regardless of zoom level.
        pixel_points = self.ax.transData.transform(self._map_points)
        click_px = np.array([event.x, event.y])
        dists = np.hypot(pixel_points[:, 0] - click_px[0], pixel_points[:, 1] - click_px[1])

        nearby_mask = dists <= CLICK_RADIUS_PX
        if not nearby_mask.any():
            return

        # Closest first, in case the caller (or the picker menu) only wants
        # the top few out of a large cluster.
        local_idxs = np.nonzero(nearby_mask)[0]
        local_idxs = local_idxs[np.argsort(dists[local_idxs])]
        station_indices = [self._map_station_idx[i] for i in local_idxs]

        if len(station_indices) == 1:
            self._on_station_clicked(station_indices[0])
        else:
            self._show_cluster_menu(station_indices, event)

    def _on_motion(self, event):
        if self._panning and self._pan_start_px is not None:
            if event.x is None or event.y is None:
                return

            # Convert the pixel-space drag distance into data-space
            # (degrees) via the axes' own transform, so pan speed tracks
            # the current zoom level rather than being a fixed number of
            # degrees per pixel.
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

        self._update_hover(event)

    def _update_hover(self, event):
        """Highlights whichever dot is under the cursor, teal, directly on
        the map - the same highlight the station list drives via
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
            self.highlight_station(self._map_station_idx[nearest])
        elif self._highlighted_index is not None:
            self.highlight_station(None)

    def _on_figure_leave(self, _event):
        if self._highlighted_index is not None:
            self.highlight_station(None)

    def _on_release(self, event):
        self._panning = False
        self._pan_start_px = None

    def _show_cluster_menu(self, station_indices, event):
        """Several stations are close enough together on screen to look
        like one dot (a "cluster", same idea as radio.garden's picker) -
        pop up a small menu at the click point so the user can choose
        which one to play. The menu's own active-item look stays the
        normal orange accent, but as you move through the entries, the
        matching dot on the map lights up teal too - the same "you're
        pointing at this one" cue as hovering the station list, just
        reached from inside the menu instead."""
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