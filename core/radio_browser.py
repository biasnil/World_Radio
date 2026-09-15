"""Client for the Radio Browser API (https://www.radio-browser.info/).

Pure data access - no tkinter, no VLC, no matplotlib. Safe to use from a
script or a test without ever opening a window.
"""

import requests

from common.constants import COUNTRY_CODE_NAMES, HEADERS, MIRRORS


class RadioBrowser:
    def __init__(self):
        self.base_url = None

    def _pick_base_url(self):
        """Find a mirror that actually responds."""
        for url in MIRRORS:
            try:
                r = requests.get(f"{url}/json/stats", headers=HEADERS, timeout=4)
                if r.ok:
                    self.base_url = url
                    return
            except requests.RequestException:
                continue
        raise ConnectionError(
            "Could not reach any Radio Browser mirror. Check your internet connection."
        )

    def _get(self, path, params=None, timeout=8):
        if not self.base_url:
            self._pick_base_url()
        r = requests.get(f"{self.base_url}{path}", params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _get_with_retry(self, path, params, timeout, max_retries):
        """Like _get, but on a timeout/connection failure it switches to a
        fresh mirror and retries, instead of giving up on the whole
        request. Pagination against /json/stations/search can run dozens
        of pages, and any single mirror occasionally stalls under load
        partway through - retrying against a different mirror is far more
        reliable than just failing the whole load at that point."""
        last_error = None
        for _attempt in range(max_retries):
            try:
                return self._get(path, params=params, timeout=timeout)
            except requests.RequestException as e:
                last_error = e
                self.base_url = None  # force _pick_base_url() to find another mirror
        raise last_error

    def countries(self):
        """List of (country_name, countrycode, station_count), sorted by name.

        Uses /json/countrycodes (ISO codes) rather than the free-text
        /json/countries list, since station "country" text fields are
        inconsistent (e.g. "USA" vs "United States") and filtering by
        exact-text match silently drops a lot of stations. Filtering by
        ISO code is reliable.
        """
        data = self._get("/json/countrycodes")
        items = []
        for d in data:
            code = d.get("name", "").strip()  # this endpoint's "name" field is the ISO code
            count = d.get("stationcount", 0)
            if not code:
                continue
            display = COUNTRY_CODE_NAMES.get(code.upper(), code)
            items.append((display, code, count))
        return sorted(items, key=lambda x: x[0])

    def top_stations(self, limit=500):
        return self._get("/json/stations/topclick", params={"limit": limit, "hidebroken": "true"})

    def _paginate(self, extra_params, first_batch, batch_size, max_total,
                   on_batch, progress_cb, page_timeout, max_retries):
        """Shared pagination engine behind both all_stations() and
        search() below - extra_params carries whatever filters (name/
        countrycode/tag) a given call adds on top of the base paging
        params. See all_stations() for the parameter docs."""
        results = []
        offset = 0
        size = first_batch
        while True:
            params = dict(extra_params)
            params.update({"limit": size, "offset": offset, "hidebroken": "true", "order": "name"})
            batch = self._get_with_retry(
                "/json/stations/search", params, timeout=page_timeout, max_retries=max_retries
            )
            if not batch:
                break
            results.extend(batch)
            offset += len(batch)
            if on_batch:
                on_batch(list(results))
            if progress_cb:
                progress_cb(len(results))
            if len(batch) < size:
                break  # short page means we've reached the end
            size = batch_size  # steady-state page size after the first
            if max_total and len(results) >= max_total:
                break
        return results

    def all_stations(self, first_batch=500, batch_size=1000, max_total=None,
                      on_batch=None, progress_cb=None, page_timeout=20, max_retries=3):
        """Fetch every station Radio Browser knows about (tens of thousands),
        via paginated requests against /json/stations/search.

        We page with limit/offset rather than hitting the unpaginated
        /json/stations endpoint directly, since that endpoint pulls the
        whole database in one huge response and is discouraged by the
        API's own docs for exactly that reason. Ordering by a stable key
        (name) keeps pages from overlapping or skipping stations as the
        underlying dataset changes between requests.

        The first page is small (first_batch) so a caller gets something
        to show almost immediately; every page after that uses batch_size,
        which is larger since by then the caller already has results on
        screen and fewer, bigger requests finish the job faster. Each page
        gets a longer timeout than ordinary requests (page_timeout) and a
        few retries against a fresh mirror (max_retries) before giving up,
        since a single slow/unresponsive mirror partway through a long
        pagination run shouldn't sink the whole load.

        on_batch(accumulated_so_far), if given, is called after every page
        with the full list collected so far - a caller can use this to
        repaint the list/map progressively instead of waiting for
        everything to finish. progress_cb(loaded_so_far), if given, is
        called at the same points for a lighter-weight caller that only
        wants a running count (e.g. a status-bar message).
        max_total, if given, stops pagination once that many stations have
        been collected (mainly useful for testing).
        """
        return self._paginate({}, first_batch, batch_size, max_total, on_batch, progress_cb, page_timeout, max_retries)

    def search(self, name="", countrycode="", tag="", first_batch=500, batch_size=1000, max_total=None,
               on_batch=None, progress_cb=None, page_timeout=20, max_retries=3):
        """Like all_stations(), but filtered by name/countrycode/tag, and
        just as complete - selecting a country or typing a tag loads
        every matching station via the same pagination, not a single
        capped page. With no filters at all this is equivalent to
        all_stations(). See all_stations() for the rest of the params."""
        extra = {}
        if name:
            extra["name"] = name
        if countrycode:
            extra["countrycode"] = countrycode
        if tag:
            extra["tag"] = tag
        return self._paginate(extra, first_batch, batch_size, max_total, on_batch, progress_cb, page_timeout, max_retries)

    def register_click(self, station_uuid):
        """Tells Radio Browser this station was played (helps keep their stats useful).
        Fire-and-forget, failures are ignored."""
        try:
            self._get(f"/json/url/{station_uuid}")
        except requests.RequestException:
            pass