"""The station list widget: a Treeview + scrollbar showing search/browse
results, with double-click-to-play wired through a callback.

Self-contained: knows how to display a list of station dicts and report
which one the user picked, is hovering, or has selected. Doesn't know
about the API, the player, or the map - callbacks just report an index
so a caller (the app) can react.

Sorting by column header reorders rows visually (via Treeview.move) but
never changes a row's iid - iids stay "index into the most recent
populate() list" throughout, which every other index-based piece of the
app (play, hover, favorite, the map's highlighting) depends on.
"""

from tkinter import ttk

from common.theme import (
    HOVER_HIGHLIGHT_COLOR, HOVER_HIGHLIGHT_TEXT,
    PLAYING_HIGHLIGHT_COLOR, PLAYING_HIGHLIGHT_TEXT,
)

_COLUMNS = ("name", "country", "tags", "bitrate")
_HEADINGS = {"name": "Station", "country": "Country", "tags": "Tags", "bitrate": "Bitrate"}


class StationListView:
    def __init__(self, parent, on_play_requested, on_hover_changed=None, on_selection_changed=None):
        """on_play_requested(index): called with the index into the most
        recent populate() list when the user double-clicks a row.
        on_hover_changed(index_or_None): called whenever the row under
        the mouse changes - None when the mouse leaves the list.
        on_selection_changed(index_or_None): called whenever the selected
        row changes, including via select() below."""
        self._on_play_requested = on_play_requested
        self._on_hover_changed = on_hover_changed
        self._on_selection_changed = on_selection_changed
        self._hovered_iid = None
        self._playing_iid = None
        self._zebra = {}  # iid -> "evenrow"/"oddrow", by current visual position (updates on sort)
        self._sort_col = None
        self._sort_reverse = False

        self.tree = ttk.Treeview(parent, columns=_COLUMNS, show="headings", selectmode="browse")
        for col in _COLUMNS:
            self.tree.heading(col, text=_HEADINGS[col], command=lambda c=col: self._sort_by(c))
        self.tree.column("name", width=280)
        self.tree.column("country", width=140)
        self.tree.column("tags", width=260)
        self.tree.column("bitrate", width=70, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        # Hover highlighting - a different row color under the cursor so
        # it's clear which station a double-click (or the Play button,
        # once selected) would actually start playing. Also reported out
        # via on_hover_changed so other views (the map) can echo it.
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", self._on_leave)

    def populate(self, stations):
        # Rows are about to be replaced entirely, so any remembered iids
        # are about to be stale - the app re-marks the playing row (if
        # any) right after this via mark_playing(), once it's worked out
        # which new index, if any, matches the still-playing station.
        self._hovered_iid = None
        self._playing_iid = None
        self._sort_col = None
        self._sort_reverse = False
        for col in _COLUMNS:
            self.tree.heading(col, text=_HEADINGS[col])
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(stations):
            bitrate = s.get("bitrate", "")
            self.tree.insert(
                "", "end", iid=str(i),
                values=(s.get("name", "?"), s.get("country", ""), s.get("tags", ""), f"{bitrate}k" if bitrate else ""),
            )
        self._restripe()
        if self._on_hover_changed:
            self._on_hover_changed(None)

    def select(self, index):
        self.tree.selection_set(str(index))
        self.tree.see(str(index))

    def get_selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def mark_playing(self, index):
        """Persistently highlights the row for the given index (into the
        most recent populate() list) green, as the currently-playing
        station - unlike the hover highlight, this stays until changed
        again, regardless of mouse position. Pass None to clear."""
        old_iid = self._playing_iid
        new_iid = str(index) if index is not None else None
        if new_iid == old_iid:
            return
        self._playing_iid = new_iid
        if old_iid is not None:
            self._refresh_row_tags(old_iid)
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

    def _sort_by(self, col):
        """Reorders the visible rows by the clicked column, toggling
        ascending/descending on repeated clicks - but only ever moves
        existing rows (Treeview.move), never touches their iid, so every
        index-based reference elsewhere (play, favorite, map highlight)
        stays valid no matter how the list is currently sorted."""
        reverse = (self._sort_col == col) and not self._sort_reverse
        self._sort_col = col
        self._sort_reverse = reverse

        def sort_key(iid):
            val = self.tree.set(iid, col)
            if col == "bitrate":
                digits = "".join(ch for ch in val if ch.isdigit())
                return int(digits) if digits else -1
            return val.lower()

        items = sorted(self.tree.get_children(""), key=sort_key, reverse=reverse)
        for pos, iid in enumerate(items):
            self.tree.move(iid, "", pos)

        for c in _COLUMNS:
            label = _HEADINGS[c]
            if c == col:
                label += " ▼" if reverse else " ▲"
            self.tree.heading(c, text=label)

        self._restripe()

    def _restripe(self):
        """Recomputes zebra striping by current visual row position, not
        by iid - needed because sorting reorders rows without changing
        their iids, so striping has to follow the display order instead."""
        for pos, iid in enumerate(self.tree.get_children("")):
            self._zebra[iid] = "evenrow" if pos % 2 == 0 else "oddrow"
        for iid in self.tree.get_children(""):
            self._refresh_row_tags(iid)

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
        current visual position), then "playing" if it's the currently-
        playing row, then "hover" last if the mouse is over it - Treeview
        gives the last tag in the list priority on style conflicts, so
        this makes hover always visually win over the persistent playing
        highlight while you're actually pointing at it, and playing shows
        through everywhere else."""
        if not self.tree.exists(iid):
            return
        tags = [self._zebra.get(iid, "evenrow")]
        if iid == self._playing_iid:
            tags.append("playing")
        if iid == self._hovered_iid:
            tags.append("hover")
        self.tree.item(iid, tags=tags)