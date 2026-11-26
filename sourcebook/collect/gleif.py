"""GLEIF — the global register of Legal Entity Identifiers (open data, CC0). Gives registered legal names and addresses."""

from __future__ import annotations

from typing import Any

import httpx

from . import USER_AGENT

ENDPOINT = "https://api.gleif.org/api/v1/lei-records"


def parse(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for rec in data.get("data", []):
        ent = rec.get("attributes", {}).get("entity", {})
        name = (ent.get("legalName") or {}).get("name", "").strip()
        if not name or ent.get("status") != "ACTIVE":
            continue
        addr = ent.get("headquartersAddress") or ent.get("legalAddress") or {}
        lei = rec.get("attributes", {}).get("lei") or rec.get("id")
        out.append({"name": name.title() if name.isupper() else name, "source_url": f"https://search.gleif.org/#/record/{lei}",
                    "source_type": "gleif", "city": (addr.get("city") or "").title(), "region": addr.get("region") or "",
                    "address": ", ".join([*(addr.get("addressLines") or []), (addr.get("city") or "").title()]).strip(", "),
                    "contacts": {}, "products": [], "kind": "registered company", "registry_id": lei})
    return out


async def fetch(country: str, keywords: list[str], max_pages: int = 3) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.api+json"}, timeout=40) as c:
        for kw in keywords[:6]:
            for page in range(1, max_pages + 1):
                r = await c.get(ENDPOINT, params={"filter[fulltext]": kw, "filter[entity.legalAddress.country]": country.upper(),
                                                  "filter[entity.status]": "ACTIVE", "page[size]": 100, "page[number]": page})
                if r.status_code != 200:
                    break
                data = r.json()
                rows += parse(data)
                if page >= (data.get("meta", {}).get("pagination", {}).get("lastPage") or 1):
                    break
    return rows


async def has_lei(name: str, country: str, client: httpx.AsyncClient | None = None) -> bool | None:
    """True if an active LEI record with this name exists in the country; None if the register couldn't be asked."""
    from ..build import name_key
    own = client or httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.api+json"}, timeout=30)
    try:
        r = await own.get(ENDPOINT, params={"filter[fulltext]": name[:80], "filter[entity.legalAddress.country]": country.upper(),
                                            "page[size]": 10})
        if r.status_code != 200:
            return None
        want = name_key(name)
        return any(name_key(x["name"]) == want for x in parse(r.json()))
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if client is None:
            await own.aclose()
