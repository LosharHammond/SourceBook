"""Find a town or city with OpenStreetMap's Nominatim (usage policy: at most 1 request a second, identify yourself, cache)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from . import USER_AGENT

ENDPOINT = "https://nominatim.openstreetmap.org/search"
_lock = asyncio.Lock()
_last = [0.0]


def parse(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for r in rows:
        if r.get("category") in ("boundary", "place") or r.get("addresstype") in ("city", "town", "village", "municipality", "county",
                                                                                     "state", "suburb", "city_district", "borough"):
            addr = r.get("address", {})
            name = r.get("name") or addr.get("city") or addr.get("town") or r.get("display_name", "").split(",")[0]
            s, n, w, e = (float(x) for x in r["boundingbox"])
            area = None
            if r.get("osm_type") == "relation":
                area = 3600000000 + int(r["osm_id"])
            elif r.get("osm_type") == "way":
                area = 2400000000 + int(r["osm_id"])
            return {"name": name, "country": (addr.get("country_code") or "").upper(), "lat": float(r["lat"]), "lon": float(r["lon"]),
                    "bbox": [s, w, n, e], "area_id": area, "display": r.get("display_name", "")}
    return None


async def find_place(query: str, country: str | None = None, db: Any = None) -> dict[str, Any] | None:
    key = f"geocode:{(country or '').upper()}:{query.strip().lower()}"
    if db is not None:
        cached = db.meta(key)
        if cached:
            return json.loads(cached) or None
    params = {"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1}
    if country:
        params["countrycodes"] = country.lower()
    async with _lock:                                    # be a polite Nominatim user: one request a second
        wait = 1.05 - (time.time() - _last[0])
        if wait > 0:
            await asyncio.sleep(wait)
        _last[0] = time.time()
        try:
            async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=10) as c:
                r = await c.get(ENDPOINT, params=params)
                r.raise_for_status()
                place = parse(r.json())
        except (httpx.HTTPError, ValueError, KeyError):
            return None
    if db is not None:
        db.set_meta(key, json.dumps(place))
    return place
