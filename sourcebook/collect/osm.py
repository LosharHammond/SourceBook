"""OpenStreetMap via the Overpass API. OSM data is © OpenStreetMap contributors, available under the ODbL —
exports that include it must keep the attribution and stay under the same licence (see README)."""

from __future__ import annotations

from typing import Any

import httpx

from . import USER_AGENT

OVERPASS = "https://overpass-api.de/api/interpreter"


def build_query(country: str, selectors: list[str], name_regex: str | None, limit: int = 400, place: dict | None = None) -> str:
    """Overpass QL for every node/way/relation in a country matching any tag selector, or a name pattern combined with a tag."""
    parts = []
    for sel in selectors:
        parts.append(f"nwr{sel}(area.a);")
    if name_regex:
        for base in ('["office"]', '["shop"]', '["industrial"]', '["craft"]', '["amenity"]', '["healthcare"]', '["tourism"]', '["man_made"="works"]'):
            parts.append(f'nwr{base}["name"~"{name_regex}",i](area.a);')
    if place and place.get("area_id"):
        area = f'area({int(place["area_id"])})->.a;'
    elif place:
        s, w, n, e = place["bbox"]
        area = ""
        parts = [p.replace("(area.a)", f"({s},{w},{n},{e})") for p in parts]
    else:
        area = f'area["ISO3166-1"="{country.upper()}"][admin_level=2]->.a;'
    return f'[out:json][timeout:90];{area}({"".join(parts)});out center tags {limit};'


def parse(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("brand") or tags.get("operator")
        if not name:
            continue
        center = el.get("center") or {}
        lat, lon = el.get("lat", center.get("lat")), el.get("lon", center.get("lon"))
        url = f"https://www.openstreetmap.org/{el['type']}/{el['id']}"
        contacts = {
            "phone": tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile"),
            "email": tags.get("email") or tags.get("contact:email"),
            "website": tags.get("website") or tags.get("contact:website") or tags.get("url"),
            "whatsapp": tags.get("contact:whatsapp"),
            "telegram": tags.get("contact:telegram"),
            "facebook": tags.get("contact:facebook") or tags.get("facebook"),
            "instagram": tags.get("contact:instagram"),
        }
        out.append({
            "name": name.strip(), "osm_url": url, "lat": lat, "lon": lon,
            "city": tags.get("addr:city") or tags.get("is_in:city") or "", "region": tags.get("addr:state") or tags.get("addr:region") or "",
            "address": ", ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:city")) if x),
            "contacts": {k: v for k, v in contacts.items() if v},
            "products": [p.strip() for p in (tags.get("product") or tags.get("products") or "").replace(",", ";").split(";") if p.strip()],
            "kind": next((f"{k}={tags[k]}" for k in ("industrial", "shop", "office", "craft", "amenity", "healthcare", "tourism", "leisure", "man_made") if k in tags), ""),
            "description": tags.get("description", ""),
            "tags": {k: v for k, v in tags.items() if k in ("opening_hours", "delivery", "wheelchair", "addr:street", "takeaway")
                     or k.startswith("payment:")},
        })
    return out


async def fetch(country: str, selectors: list[str], name_regex: str | None, place: dict | None = None) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=120) as c:
        r = await c.post(OVERPASS, data={"data": build_query(country, selectors, name_regex, place=place)})
        r.raise_for_status()
        return parse(r.json())
