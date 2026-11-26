"""Search-driven discovery and the scheduled crawler.

A search like "cocoa exporters in Côte d'Ivoire" becomes a data page (status "discovered") that is built from every
source in the background while the searcher watches. The crawler does the same for a list of topics, every hour,
until the day's target of new businesses is reached."""

from __future__ import annotations

import csv
import os
import re
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from .db import DB, slugify
from .pages import countries, definition_from_form

FRESH_HOURS = float(os.environ.get("SOURCEBOOK_FRESH_HOURS", "6"))      # a search re-checks its businesses if older than this
STARTER = Path(__file__).resolve().parent.parent / "datasets" / "crawl-targets.csv"

EXTRA_ALIASES = {
    "ivory coast": "CI", "cote divoire": "CI", "cote d ivoire": "CI", "usa": "US", "united states of america": "US", "america": "US",
    "uk": "GB", "britain": "GB", "great britain": "GB", "england": "GB", "drc": "CD", "dr congo": "CD", "uae": "AE", "emirates": "AE",
    "south korea": "KR", "korea": "KR", "russia": "RU", "vietnam": "VN", "laos": "LA", "the gambia": "GM", "gambia": "GM",
    "ghanaian": "GH", "nigerian": "NG", "kenyan": "KE", "ivorian": "CI", "south african": "ZA", "senegalese": "SN", "togolese": "TG",
    "beninese": "BJ", "cameroonian": "CM", "ethiopian": "ET", "ugandan": "UG", "tanzanian": "TZ", "rwandan": "RW", "egyptian": "EG",
    "moroccan": "MA", "burkinabe": "BF", "malian": "ML", "liberian": "LR", "sierra leonean": "SL", "zambian": "ZM", "zimbabwean": "ZW",
    "british": "GB", "american": "US", "chinese": "CN", "indian": "IN", "turkish": "TR", "german": "DE", "french": "FR",
}
FILLER = re.compile(r"^(please\s+)?(find|show|search( for)?|list( of)?|get|all|best|top\s*\d*|the|some|any)\s+", re.I)
CONNECTOR = re.compile(r"\s+(in|from|of|at|near|across|within|based in|located in)\s*$", re.I)


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text).strip()


@lru_cache(maxsize=1)
def _aliases() -> list[tuple[str, str]]:
    names = {_plain(name): code for code, name in countries()}
    names.update({_plain(k): v for k, v in EXTRA_ALIASES.items()})
    names = {k: v for k, v in names.items() if len(k) >= 2}
    return sorted(names.items(), key=lambda kv: -len(kv[0]))     # longest first: "south africa" before "africa"


def parse_query(q: str, default_country: str | None = None) -> tuple[str, str | None]:
    """'cocoa exporters in Côte d’Ivoire' → ('cocoa exporters', 'CI'). Country is None if none found and no default."""
    p = parse_search(q, default_country)
    return p["what"], p["country"]


def parse_search(q: str, default_country: str | None = None) -> dict[str, Any]:
    """'dentists in Austin' → {what: 'dentists', place: 'austin', country: None (unless named or defaulted), named_country: False}"""
    plain = " " + re.sub(r"\s+", " ", _plain(q)) + " "
    country = None
    for alias, code in _aliases():
        if f" {alias} " in plain:
            country = code
            plain = plain.replace(f" {alias} ", " ", 1)
            break
    what = re.sub(r"\s+", " ", plain).strip()
    place = None
    m = re.search(r"\s+(?:in|near|around|at)\s+([a-z][a-z .'-]{1,40})$", what)
    if m:
        place, what = m.group(1).strip(), what[: m.start()]
    for _ in range(3):
        what = FILLER.sub("", what)
        what = CONNECTOR.sub("", what).strip()
    return {"what": what, "country": country or default_country, "place": place, "named_country": bool(country)}


def valid_topic(what: str) -> bool:
    return 3 <= len(what) <= 60 and len(re.findall(r"[a-z]{3,}", what)) >= 1 and len(what.split()) <= 8


def page_for(db: DB, what: str, country: str, keywords: str = "", products: str = "", status: str = "discovered",
             owner: int | None = None, place: dict | None = None, osm_tags: str = "") -> str | None:
    """Find the page for this topic, or create it. Returns its slug, or None if the topic isn't usable."""
    definition, errors = definition_from_form({"what": what, "country": country, "keywords": keywords, "products": products, "place": place,
                                               "osm_tags": osm_tags})
    if errors or not definition:
        return None
    existing = find_topic(db, what, country, place)
    if existing:
        return existing
    return db.create_dataset(definition["slug"], definition["title"], definition["summary"], country, definition, owner, status)


def needs_run(db: DB, slug: str) -> bool:
    d = db.dataset(slug)
    if not d or d["status"] == "rejected" or d["demo"]:
        return False
    if db.jobs(dataset=slug, status=("queued", "running")):
        return False
    return not d["built_at"] or d["built_at"] < time.time() - FRESH_HOURS * 3600 or not db.entities(slug)


async def resolve(db: DB, q: str, default_country: str | None) -> dict[str, Any]:
    """Parse a search and look up its town. Returns what, country, place (dict or None), problem."""
    from .collect.geocode import find_place
    p = parse_search(q, default_country)
    place = None
    if p["place"]:
        place = await find_place(p["place"], p["country"] if p["named_country"] else None, db)
        if place and not p["named_country"]:
            p["country"] = place["country"] or p["country"]
        if not place:
            place = await find_place(p["place"], p["country"], db) if p["country"] else None
    p["place_info"] = place
    p["slug"] = find_topic(db, p["what"], p["country"], place) if p["country"] and valid_topic(p["what"]) else None
    return p


async def start_discovery(db: DB, q: str, default_country: str | None, user_id: int | None = None,
                          parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Called by the search page. Returns what happened so the page can show it."""
    p = parsed or await resolve(db, q, default_country)
    what, country, place = p["what"], p["country"], p["place_info"]
    if not country or not valid_topic(what):
        db.log_search(q, None, None, None, False, user_id)
        return {"started": False, "reason": "unclear", "what": what, "country": country}
    slug = page_for(db, what, country, place=place)
    if not slug:
        db.log_search(q, what, country, None, False, user_id)
        return {"started": False, "reason": "unclear", "what": what, "country": country}
    busy = len(db.jobs(status=("queued",), limit=500))
    started = False
    if needs_run(db, slug):
        if busy >= int(os.environ.get("SOURCEBOOK_MAX_QUEUE", "40")):
            db.log_search(q, what, country, slug, False, user_id)
            return {"started": False, "reason": "busy", "what": what, "country": country, "slug": slug, "place": place}
        db.add_job(slug, None, "discover", priority=0)
        started = True
    db.log_search(q, what, country, slug, started, user_id)
    return {"started": started, "running": bool(db.jobs(dataset=slug, status=("queued", "running"))), "what": what,
            "country": country, "slug": slug, "place": place}


# ------------------------------------------------------------------ scheduled crawler
def crawler_settings(db: DB) -> dict[str, Any]:
    return {"enabled": db.meta("crawler_enabled", "1") == "1",
            "daily_target": int(db.meta("crawler_daily_target", os.environ.get("SOURCEBOOK_DAILY_TARGET", "1000")) or 1000),
            "per_run": int(db.meta("crawler_per_run", os.environ.get("SOURCEBOOK_CRAWL_PER_RUN", "12")) or 12),
            "world": db.meta("crawler_world", os.environ.get("SOURCEBOOK_WORLD", "1")) == "1"}


def today_start() -> float:
    t = time.gmtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)) - time.timezone


def crawl_tick(db: DB, force: bool = False) -> list[str]:
    """One round of the crawler: queue due topics until the daily target is in reach. Returns queued page slugs."""
    s = crawler_settings(db)
    if not s["enabled"] and not force:
        return []
    if db.new_entities_since(today_start()) >= s["daily_target"] and not force:
        return []
    if db.jobs(status=("queued", "running"), limit=500) and any(j["kind"] == "crawl" for j in db.jobs(status=("queued", "running"), limit=500)):
        return []                                    # previous round still working
    queued = []
    # monitored searches are re-checked daily, ahead of everything else
    watched = {m["dataset"] for m in db.monitors()}
    for slug in watched:
        d = db.dataset(slug)
        if d and (d["built_at"] or 0) < time.time() - 86400 and not db.jobs(dataset=slug, status=("queued", "running")):
            db.add_job(slug, None, "crawl", priority=5)
            queued.append(slug)
    for t in db.due_targets(s["per_run"]):
        slug = t["dataset"] if t["dataset"] and db.dataset(t["dataset"]) else page_for(db, t["what"], t["country"], t["keywords"], t["products"])
        db.update_target(t["id"], last_run=time.time(), dataset=slug)
        if slug and not db.jobs(dataset=slug, status=("queued", "running")):
            db.add_job(slug, None, "crawl", priority=9)
            queued.append(slug)
    # the rest of the round goes to covering every country
    room = s["per_run"] - len(queued)
    if s["world"] and room > 0:
        queued += world_round(db, room)
    if not queued:                                   # nothing due: refresh the stalest discovered page
        stale = [d for d in db.datasets(status="discovered") if needs_run(db, d["slug"])]
        stale.sort(key=lambda d: d["built_at"] or 0)
        for d in stale[: s["per_run"]]:
            db.add_job(d["slug"], None, "crawl", priority=9)
            queued.append(d["slug"])
    return queued


def load_starter_targets(db: DB, created_by: int | None = None) -> int:
    if not STARTER.exists():
        return 0
    n = 0
    with STARTER.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if db.add_target(row["what"], row["country"], row.get("keywords", ""), (row.get("products") or "").replace(";", "\n"),
                             float(row.get("every_days") or 14), created_by):
                n += 1
    return n


def find_topic(db: DB, what: str, country: str, place: dict | None = None) -> str | None:
    """The public (or search-found) page for this topic. Members' private pages with the same name are never reused."""
    definition, errors = definition_from_form({"what": what, "country": country, "place": place})
    if errors or not definition:
        return None
    base = definition["slug"]
    for slug in [base] + [f"{base}-{n}" for n in range(2, 6)]:
        d = db.dataset(slug)
        if d and d["status"] in ("published", "discovered"):
            return slug
    return None


# ------------------------------------------------------------------ every country
WORLD = Path(__file__).resolve().parent.parent / "datasets" / "world-categories.csv"


@lru_cache(maxsize=1)
def world_categories() -> list[dict[str, str]]:
    with WORLD.open(encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("what")]


def world_countries() -> list[str]:
    return [code for code, _ in countries()]


def world_round(db: DB, room: int) -> list[str]:
    """Queue the next (business type, country) pairs that have never run or ran longest ago.
    Order: every country gets the first business type before any country gets the second, so coverage spreads fast."""
    cats = world_categories()
    with db.conn() as c:
        done = {(r[0], r[1]): r[2] or 0 for r in c.execute("SELECT category, country, last_run FROM world_runs")}
    every_days = float(os.environ.get("SOURCEBOOK_WORLD_EVERY_DAYS", "30"))
    due = []
    for ci, cat in enumerate(cats):
        for country in world_countries():
            last = done.get((cat["what"], country))
            if last is None or last < time.time() - every_days * 86400:
                due.append((last or 0, ci, country, cat))
    due.sort(key=lambda x: (x[0], x[1], x[2]))
    queued = []
    for _last, _ci, country, cat in due[:room]:
        slug = page_for(db, cat["what"], country, cat.get("keywords", ""), osm_tags=(cat.get("osm_tags") or "").replace(";", "\n"))
        with db.conn() as c:
            c.execute("INSERT INTO world_runs(category, country, dataset, last_run) VALUES(?,?,?,?) "
                      "ON CONFLICT(category, country) DO UPDATE SET dataset=excluded.dataset, last_run=excluded.last_run",
                      (cat["what"], country, slug, time.time()))
        if slug and not db.jobs(dataset=slug, status=("queued", "running")):
            db.add_job(slug, None, "crawl", priority=9)
            queued.append(slug)
    return queued


def world_coverage(db: DB) -> dict[str, int]:
    with db.conn() as c:
        countries_done = c.execute("SELECT COUNT(DISTINCT country) FROM world_runs").fetchone()[0]
        pairs = c.execute("SELECT COUNT(*) FROM world_runs").fetchone()[0]
    return {"countries": countries_done, "countries_total": len(world_countries()), "pairs": pairs,
            "pairs_total": len(world_countries()) * len(world_categories())}
