"""Fetches and caches the world-country outline GeoJSON used by the map view."""

import json
import os

import requests

from common.constants import HEADERS, WORLD_GEOJSON_CACHE, WORLD_GEOJSON_URL


def load_world_geojson():
    """Returns the parsed world-countries GeoJSON, downloading it once and
    caching to disk so later runs work without internet for the map shape
    (station data still needs the network regardless)."""
    if os.path.exists(WORLD_GEOJSON_CACHE):
        with open(WORLD_GEOJSON_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)

    r = requests.get(WORLD_GEOJSON_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    try:
        with open(WORLD_GEOJSON_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass  # cache write failing is fine, we still have the data in memory
    return data
