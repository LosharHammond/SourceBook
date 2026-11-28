"""Wikidata (CC0): companies in a country whose name or industry matches the keywords."""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import USER_AGENT

ENDPOINT = "https://query.wikidata.org/sparql"


def _safe(term: str) -> str:
    return re.sub(r'[^\w\s-]', "", term).strip()[:40]


def build_query(country: str, keywords: list[str]) -> str:
    terms = [t for t in (_safe(k) for k in keywords[:6]) if len(t) >= 3]
    searches = "\n  UNION\n".join(
        f'  {{ SERVICE wikibase:mwapi {{ bd:serviceParam wikibase:endpoint "www.wikidata.org"; wikibase:api "EntitySearch"; '
        f'mwapi:search "{t}"; mwapi:language "en"; mwapi:limit "max". ?item wikibase:apiOutputItem mwapi:item. }} }}' for t in terms)
    industry = " || ".join(f'CONTAINS(LCASE(?il), "{t.lower()}")' for t in terms)
    return f"""SELECT DISTINCT ?item ?itemLabel ?website ?phone ?email ?hqLabel ?industryLabel WHERE {{
  ?c wdt:P297 "{country.upper()}".
  {{
{searches}
  UNION
  {{ ?item wdt:P17 ?c; wdt:P452 ?industry. ?industry rdfs:label ?il. FILTER(LANG(?il) = "en" && ({industry or 'false'})) }}
  }}
  ?item wdt:P17 ?c.
  FILTER NOT EXISTS {{ ?item wdt:P576 ?dissolved }}
  OPTIONAL {{ ?item wdt:P856 ?website }}
  OPTIONAL {{ ?item wdt:P1329 ?phone }}
  OPTIONAL {{ ?item wdt:P968 ?email }}
  OPTIONAL {{ ?item wdt:P159 ?hq }}
  OPTIONAL {{ ?item wdt:P452 ?industry }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,fr,pt,es". }}
}} LIMIT 400"""


def parse(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for b in data.get("results", {}).get("bindings", []):
        url = b["item"]["value"]
        name = b.get("itemLabel", {}).get("value", "")
        if not name or re.fullmatch(r"Q\d+", name):
            continue
        row = out.setdefault(url, {"name": name, "source_url": url, "source_type": "wikidata", "city": b.get("hqLabel", {}).get("value", ""),
                                   "contacts": {}, "products": [], "kind": ""})
        for key, field in (("website", "website"), ("phone", "phone"), ("email", "email")):
            if key in b and field not in row["contacts"]:
                row["contacts"][field] = b[key]["value"].replace("mailto:", "")
        if "industryLabel" in b and not row["kind"]:
            row["kind"] = b["industryLabel"]["value"]
        if re.fullmatch(r"Q\d+", row["city"]):
            row["city"] = ""
    return list(out.values())


async def fetch(country: str, keywords: list[str]) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}, timeout=70) as c:
        r = await c.get(ENDPOINT, params={"query": build_query(country, keywords), "format":"json"})
        r.raise_for_status()
        return parse(r.json())
