"""The station list widget: a paginated Treeview + scrollbar showing
search/browse results, with double-click-to-play wired through a
callback.

Self-contained: knows how to display a list of station dicts and report
which one the user picked, is hovering, or has selected. Doesn't know
about the API, the player, or the map - callbacks just report an index
so a caller (the app) can react.

Performance model: only one page's worth of rows (PAGE_SIZE) is ever a
live Treeview item at once - each row is a real Tcl-side object, not a
lightweight data record, so holding tens of thousands of them
simultaneously is expensive regardless of how carefully they're
inserted. Prev/Next switch which slice of the underlying list is shown;
row iids stay "absolute index into the most recent populate() list"
throughout (even though only one page's iids exist in the tree at a
time), which every other index-based piece of the app (play, hover,
favorite, the map's highlighting) depends on.

Sorting is NOT handled internally (unlike a purely visual reorder) -
clicking a header just reports which column via on_sort_requested; the
app resorts the actual underlying station list and calls populate()
again with the new order. That's necessary here specifically because
this view only ever holds one page of the data, so there's no "reorder
what's currently displayed" option - the true order has to change.
"""

import tkinter as tk
from tkinter import ttk

from common.theme import (
    HOVER_HIGHLIGHT_COLOR, HOVER_HIGHLIGHT_TEXT,
    PLAYING_HIGHLIGHT_COLOR, PLAYING_HIGHLIGHT_TEXT,
)

_COLUMNS = ("name", "country", "tags", "bitrate")
_HEADINGS = {"name": "Station", "country": "Country", "tags": "Tags", "bitrate": "Bitrate"}

PAGE_SIZE = 1000


class StationListView:
    def __init__(self, parent, on_play_requested, on_hover_changed=None,
                 on_selection_changed=None, on_sort_requested=None):
        """on_play_requested(index): called with the index into the most
        recent populate() list when the user double-clicks a row.
        on_hover_changed(index_or_None): called whenever the row under
        the mouse changes - None when the mouse leaves the list.
        on_selection_changed(index_or_None): called whenever the selected
        row changes, including via select() below.
        on_sort_requested(column): called when a column header is
        clicked - the caller is responsible for actually resorting and
        calling populate() again; see the module docstring for why."""
        self._on_play_requested = on_play_requested
        self._on_hover_changed = on_hover_changed
        self._on_selection_changed = on_selection_changed
        self._on_sort_requested = on_sort_requested
        self._hovered_iid = None
        self._playing_iid = None
        self._zebra = {}  # iid -> "evenrow"/"oddrow", by position within the current page
        self._stations = []  # full underlying list from the last populate()
        self._last_stations = None  # for deciding whether to keep the current page on a progressive load
        self._page = 0

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=_COLUMNS, show="headings", selectmode="browse")
        for col in _COLUMNS:
            self.tree.heading(col, text=_HEADINGS[col], command=lambda c=col: self._header_clicked(c))
        self.tree.column("name", width=280)
        self.tree.column("country", width=140)
        self.tree.column("tags", width=260)
        self.tree.column("bitrate", width=70, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        page_bar = ttk.Frame(parent, padding=(2, 6))
        page_bar.pack(fill="x")
        self.prev_btn = ttk.Button(page_bar, text="◀ Prev", command=self._go_prev_page, state="disabled")
        self.prev_btn.pack(side="left")
        self.page_label_var = tk.StringVar(value="")
        ttk.Label(page_bar, textvariable=self.page_label_var).pack(side="left", padx=10)
        self.next_btn = ttk.Button(page_bar, text="Next ▶", command=self._go_next_page, state="disabled")
        self.next_btn.pack(side="left")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        # Hover highlighting - a different row color under the cursor so
        # it's clear which station a double-click (or the Play button,
        # once selected) would actually start playing. Also reported out
        # via on_hover_changed so other views (the map) can echo it.
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", self._on_leave)

    # ---- Data / paging -------------------------------------------

    def populate(self, stations):
        """Sets the full underlying station set and (re)draws the current
        page. During a progressive load (see app.py's
        _load_stations_progressive), each call passes the previous
        batch's stations plus more appended to the end (same objects,
        same order, just longer) - in that specific case the page you're
        currently looking at is left alone (just the page count/label
        updates), rather than yanking you back to page 1 while you're
        mid-browse. Anything else (a genuinely different list, or a
        fresh sort) resets to page 1."""
        extending = self._can_extend(stations)
        self._stations = stations
        if not extending:
            self._page = 0
            self._hovered_iid = None
            self._playing_iid = None
            for col in _COLUMNS:
                self.tree.heading(col, text=_HEADINGS[col])
        self._last_stations = stations
        self._redraw_current_page()
        if not extending and self._on_hover_changed:
            self._on_hover_changed(None)

    def _can_extend(self, stations):
        old = self._last_stations
        if not old or len(stations) <= len(old):
            return False
        # Cheap identity check (not a deep compare) - progressive batches
        # are built by extending the same underlying list, so the shared
        # prefix is literally the same dict objects, not just equal ones.
        return stations[0] is old[0] and stations[len(old) - 1] is old[-1]

    def page_count(self):
        return max(1, (len(self._stations) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _go_prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._redraw_current_page()

    def _go_next_page(self):
        if self._page < self.page_count() - 1:
            self._page += 1
            self._redraw_current_page()

    def _go_to_page_for_index(self, index):
        target_page = index // PAGE_SIZE
        if target_page != self._page and 0 <= target_page < self.page_count():
            self._page = target_page
            self._redraw_current_page()

    def _redraw_current_page(self):
        self._hovered_iid = None
        self.tree.delete(*self.tree.get_children())
        start = self._page * PAGE_SIZE
        end = min(start + PAGE_SIZE, len(self._stations))
        for pos, i in enumerate(range(start, end)):
            s = self._stations[i]
            bitrate = s.get("bitrate", "")
            iid = str(i)
            self.tree.insert(
                "", "end", iid=iid,
                values=(s.get("name", "?"), s.get("country", ""), s.get("tags", ""), f"{bitrate}k" if bitrate else ""),
            )
            self._zebra[iid] = "evenrow" if pos % 2 == 0 else "oddrow"
            self._refresh_row_tags(iid)
        self._update_page_controls()

    def _update_page_controls(self):
        total = len(self._stations)
        pages = self.page_count()
        self.page_label_var.set(f"Page {self._page + 1} of {pages}  ({total:,} stations)")
        self.prev_btn.config(state="normal" if self._page > 0 else "disabled")
        self.next_btn.config(state="normal" if self._page < pages - 1 else "disabled")

    # ---- Selection / playing (page-aware) ---------------------------

    def select(self, index):
        """Selects the given row, switching to its page first if it isn't
        the one currently shown - this is called right after a deliberate
        user action (clicking a map marker, double-clicking a row, the
        Play button), so jumping pages to reveal it is expected here."""
        self._go_to_page_for_index(index)
        iid = str(index)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def get_selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def mark_playing(self, index):
        """Persistently highlights the row for the given index (into the
        most recent populate() list) green, as the currently-playing
        station - unlike the hover highlight, this stays until changed
        again, regardless of mouse position. Pass None to clear.

        Deliberately does NOT switch pages to reveal it - this gets
        called every time the station list itself refreshes (including
        during a background load), and jumping the page out from under
        someone mid-browse just because playback state changed elsewhere
        would be disruptive. It'll show correctly once you page to it."""
        old_iid = self._playing_iid
        new_iid = str(index) if index is not None else None
        if new_iid == old_iid:
            return
        self._playing_iid = new_iid
        if old_iid is not None:
            self._refresh_row_tags(old_iid)  # safe no-op if that row isn't on the current page
        if new_iid is not None:
            self._refresh_row_tags(new_iid)

    def set_theme(self, colors):
        """Applies a light/dark palette to row banding. The hover and
        playing highlights themselves stay fixed colors regardless of
        theme (see common/theme.py) so they always read the same way."""
        self.tree.tag_configure("evenrow", background=colors["tree_bg"])
        self.tree.tag_configure("oddrow", background=colors["tree_alt"])
        self.tree.tag_configure("hover", background=HOVER_HIGHLIGHT_COLOR, foreground=HOVER_HIGHLIGHT_TEXT)
        self.tree.tag_configure("playing", background=PLAYING_HIGHLIGHT_COLOR, foreground=PLAYING_HIGHLIGHT_TEXT)

    def set_sort_indicator(self, column, reverse):
        """Purely cosmetic - puts a ▲/▼ arrow on the given header. Called
        by the app after it resorts the underlying list and repopulates;
        this view has no sort logic of its own (see module docstring)."""
        for c in _COLUMNS:
            label = _HEADINGS[c]
            if c == column:
                label += " ▼" if reverse else " ▲"
            self.tree.heading(c, text=label)

    def _header_clicked(self, column):
        if self._on_sort_requested:
            self._on_sort_requested(column)

    def _on_double_click(self, _event):
        idx = self.get_selected_index()
        if idx is not None:
            self._on_play_requested(idx)

    def _on_tree_select(self, _event):
        if self._on_selection_changed:
            self._on_selection_changed(self.get_selected_index())

    def _on_motion(self, event):
        row = self.tree.identify_row(event.y)
        if row == self._hovered_iid:
            return
        old = self._hovered_iid
        self._hovered_iid = row or None
        if old is not None:
            self._refresh_row_tags(old)
        if self._hovered_iid is not None:
            self._refresh_row_tags(self._hovered_iid)
        if self._on_hover_changed:
            self._on_hover_changed(int(row) if row else None)

    def _on_leave(self, _event):
        if self._hovered_iid is None:
            return
        old = self._hovered_iid
        self._hovered_iid = None
        self._refresh_row_tags(old)
        if self._on_hover_changed:
            self._on_hover_changed(None)

    def _refresh_row_tags(self, iid):
        """Rebuilds one row's tag list from scratch: zebra base tag (by
        position within the current page), then "playing" if it's the
        currently-playing row, then "hover" last if the mouse is over it
        - Treeview gives the last tag in the list priority on style
        conflicts, so this makes hover always visually win over the
        persistent playing highlight while you're actually pointing at
        it, and playing shows through everywhere else. A safe no-op if
        the row isn't on the currently-displayed page."""
        if not self.tree.exists(iid):
            return
        tags = [self._zebra.get(iid, "evenrow")]
        if iid == self._playing_iid:
            tags.append("playing")
        if iid == self._hovered_iid:
            tags.append("hover")
        self.tree.item(iid, tags=tags)