"""Optional: find business websites through a web search API (Brave Search API — set BRAVE_API_KEY).
Only the result URLs are used: each site is then read on its own terms (robots.txt) and kept only if it shows evidence."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from . import USER_AGENT

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# Directories, marketplaces, social networks, news and reference sites: not the businesses themselves.
SKIP_DOMAINS = re.compile(
    r"(^|\.)(facebook|instagram|linkedin|twitter|x|youtube|tiktok|pinterest|wikipedia|wikidata|google|bing|yahoo|amazon|ebay|alibaba|"
    r"aliexpress|made-in-china|indiamart|tradeindia|globalsources|kompass|yelp|tripadvisor|yellowpages|yellow-pages|businesslist|"
    r"cybo|zoominfo|dnb|opencorporates|crunchbase|glassdoor|indeed|jiji|tonaton|jumia|ghanayello|vconnect|africa-business|"
    r"bloomberg|reuters|myjoyonline|graphic|citinewsroom|ghanaweb|modernghana|pulse|nation|punchng|allafrica|medium|blogspot|"
    r"wordpress|wix|scribd|issuu|slideshare|gov|europa|un|worldbank|reddit|quora)\.", re.I)


def available() -> bool:
    return bool(os.environ.get("BRAVE_API_KEY"))


def clean_title(title: str) -> str:
    title = re.sub(r"<[^>]+>", "", title)
    parts = re.split(r"\s+[|\-–—:]\s+", title)
    parts = [p for p in parts if not re.fullmatch(r"(home|homepage|welcome|official (web)?site|contact us|about us)", p.strip(), re.I)]
    return (parts[0] if parts else title).strip()[:120]


def parse(data: dict[str, Any]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for r in data.get("web", {}).get("results", []):
        url = r.get("url", "")
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if not host or host in seen or SKIP_DOMAINS.search(host + "."):
            continue
        seen.add(host)
        root = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
        out.append({"name": clean_title(r.get("title", "")) or host, "website": root, "source_url": url, "source_type": "web",
                    "contacts": {"website": root}, "products": [], "kind": "", "needs_evidence": True, "snippet": r.get("description", "")})
    return out


async def fetch(query: str, country: str, pages: int = 2) -> list[dict[str, Any]]:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "X-Subscription-Token": key, "Accept": "application/json"}, timeout=30) as c:
        for offset in range(pages):
            r = await c.get(ENDPOINT, params={"q": query, "country": country.upper(), "count": 20, "offset": offset})
            if r.status_code == 422:            # country not supported by the index: search everywhere instead
                r = await c.get(ENDPOINT, params={"q": query, "country": "ALL", "count": 20, "offset": offset})
            if r.status_code != 200:
                break
            data = r.json()
            rows += parse(data)
            if not data.get("query", {}).get("more_results_available"):
                break
    return rows
