"""Build (or refresh) one data page from its definition file: collect → merge → describe → index."""

from __future__ import annotations

import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .collect import osm
from .collect.site import read_site
from .db import DB, slugify
from . import gaps
from .describe import llm_rephrase, template_description


def _domain(url: str) -> str:
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def definition_for(db: DB, slug: str) -> dict[str, Any] | None:
    d = db.dataset(slug)
    if not d:
        return None
    out = json.loads(d["definition"] or "{}")
    out.setdefault("slug", slug)
    out.setdefault("title", d["title"])
    if not out.get("seeds") or not out.get("_dir"):
        out["_dir"] = str(Path(__file__).resolve().parent.parent / "datasets")
    return out


def load_definition(path: Path) -> dict[str, Any]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d["_dir"] = str(Path(path).parent)
    return d


def read_seeds(d: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [r for r in d.get("seed_rows", []) if (r.get("name") or "").strip()]        # websites typed into the workspace form
    path = Path(d.get("_dir", ".")) / d.get("seeds", "")
    if d.get("seeds") and path.exists():
        with path.open(encoding="utf-8-sig") as fh:
            rows += [row for row in csv.DictReader(fh) if (row.get("name") or "").strip()]
    return rows


class Cancelled(Exception):
    pass


CHECK_LEI = os.environ.get("SOURCEBOOK_CHECK_LEI", "1") == "1"
MAX_CANDIDATES = int(os.environ.get("SOURCEBOOK_MAX_BUSINESSES", "400"))


def relevant(d: dict[str, Any], name: str, kind: str, products: list[str]) -> bool:
    if products:
        return True
    text = f"{name} {kind}".lower()
    return any(k in text for k in d.get("relevance_keywords", []))


LEGAL_SUFFIX = re.compile(r"\b(ltd|limited|plc|llc|inc|co|company|corp|corporation|sarl|sa|sas|gmbh|bv|pty|enterprises?|group|"
                          r"ventures|industries|international|intl|ghana|nigeria|kenya)\b\.?", re.I)


def name_key(name: str) -> str:
    """Comparable form of a business name: 'Golden Cocoa Export Ltd.' == 'GOLDEN COCOA EXPORT LIMITED'."""
    s = slugify(LEGAL_SUFFIX.sub(" ", name))
    return s or slugify(name)


SOURCES = ("osm", "wikidata", "gleif", "web", "seeds")


async def collect(d: dict[str, Any], log: Callable[[str], None]) -> list[dict[str, Any]]:
    """Ask every source; return candidates in one shape."""
    from .collect import gleif, websearch, wikidata
    wanted = d.get("sources") or SOURCES
    keywords = d.get("keywords") or d.get("relevance_keywords") or []
    out: list[dict[str, Any]] = []

    async def run(label: str, coro) -> list[dict[str, Any]]:
        try:
            rows = await coro
            log(f"{label}: {len(rows)} found")
            return rows
        except Exception as exc:  # noqa: BLE001 - one source failing shouldn't stop the others
            log(f"{label} couldn't be reached ({exc.__class__.__name__}); carrying on with the other sources.")
            return []

    if "osm" in wanted and (d.get("osm_selectors") or d.get("osm_name_regex")):
        for p in await run("OpenStreetMap", osm.fetch(d["country"], d.get("osm_selectors", []), d.get("osm_name_regex"), place=d.get("place"))):
            out.append({"name": p["name"], "website": p["contacts"].get("website", ""), "city": p["city"], "region": p["region"],
                        "lat": p["lat"], "lon": p["lon"], "address": p["address"], "kind": p["kind"], "contacts": p["contacts"],
                        "products": p["products"], "source_url": p["osm_url"], "source_type": "osm", "osm_tags": p.get("tags", {})})
    if "wikidata" in wanted and keywords:
        for p in await run("Wikidata", wikidata.fetch(d["country"], keywords)):
            p["website"] = p["contacts"].get("website", "")
            out.append(p)
    if "gleif" in wanted and keywords:
        out += await run("Company register (GLEIF)", gleif.fetch(d["country"], keywords))
    place = d.get("place")
    if place:                                   # registers and Wikidata are national: keep the ones in this town
        town = name_key(place["name"])
        before = len(out)
        out = [r for r in out if r["source_type"] == "osm" or (r.get("city") and (name_key(r["city"]) == town or town in name_key(r["city"])))]
        if before - len(out):
            log(f"Kept businesses in {place['name']} only ({before - len(out)} elsewhere in the country left out)")
    if "web" in wanted and websearch.available():
        q = f"{d.get('what') or d['title']} {place['name'] + ' ' if place else ''}{d.get('country_name', '')}".strip()
        out += await run("Web search", websearch.fetch(q, d["country"]))
    if "seeds" in wanted:
        for row in read_seeds(d):
            out.append({"name": row["name"].strip(), "website": (row.get("website") or "").strip(), "city": (row.get("city") or "").strip(),
                        "contacts": {}, "products": [], "source_url": (row.get("website") or "").strip() or "curator-list",
                        "source_type": "curator", "curator": True})
    return out


def merge(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same business from several sources → one candidate with every source kept."""
    by_key: dict[str, dict[str, Any]] = {}
    alias: dict[str, str] = {}
    for c in rows:
        dom, nk = _domain(c.get("website", "")), name_key(c["name"])
        key = alias.get("d:" + dom) if dom else None
        key = key or alias.get("n:" + nk) or ("d:" + dom if dom else "n:" + nk)
        cur = by_key.get(key)
        record = {"source_type": c["source_type"], "source_url": c["source_url"], "contacts": c.get("contacts", {}),
                  "products": c.get("products", []), "city": c.get("city", ""), "address": c.get("address", ""), "name": c["name"]}
        if not cur:
            cur = by_key[key] = {**c, "records": [record]}
        else:
            cur["records"].append(record)
            for k in ("website", "city", "region", "lat", "lon", "kind", "address", "osm_tags", "registry_id"):
                if c.get(k) and not cur.get(k):
                    cur[k] = c[k]
            cur["curator"] = cur.get("curator") or c.get("curator")
            # a name from a register or the map beats a web page title
            if cur.get("source_type") == "web" and c["source_type"] != "web":
                cur["name"], cur["source_type"] = c["name"], c["source_type"]
            cur["needs_evidence"] = cur.get("needs_evidence") and c.get("needs_evidence")
        if dom:
            alias["d:" + dom] = key
        alias["n:" + nk] = key
    return list(by_key.values())


async def build(definition: Path | dict[str, Any], db: DB, *, websites: bool = True, delay: float = 2.0,
                log: Callable[[str], None] = print, stop: Callable[[], bool] | None = None) -> dict[str, int]:
    import asyncio
    d = definition if isinstance(definition, dict) else load_definition(definition)
    slug = d["slug"]
    db.upsert_dataset(slug, d["title"], d.get("summary", ""), d.get("country", ""),
                      {k: v for k, v in d.items() if not k.startswith("_")})
    db.clear_dataset(slug, demo_only=True)       # a real build replaces any example data
    before = len(db.entities(slug))
    candidates = merge(await collect(d, log))
    stats = {"found": len(candidates), "websites_read": 0, "kept": 0, "skipped": 0, "new": 0}
    if len(candidates) > MAX_CANDIDATES:
        log(f"Found {len(candidates)} candidates; checking the first {MAX_CANDIDATES}. Narrow the keywords to cover the rest.")
        candidates = sorted(candidates, key=lambda c: (not c.get("website"), c.get("needs_evidence", False)))[:MAX_CANDIDATES]
    else:
        log(f"{len(candidates)} different businesses after merging duplicates")
    stems = d.get("relevance_keywords") or []
    topic = f"{d.get('what', '')} {d.get('title', '')}".lower()
    booking_matters = any(k in topic for k in gaps.BOOKING_KINDS)
    osm_queried = "osm" in (d.get("sources") or SOURCES) and bool(d.get("osm_selectors") or d.get("osm_name_regex"))
    sem = asyncio.Semaphore(int(os.environ.get("SOURCEBOOK_SITE_CONCURRENCY", "6")))
    done = 0

    resume_after = time.time() - 6 * 3600 if d.get("_resume") else None
    fresh = {e["slug"] for e in db.entities(slug) if (e.get("updated_at") or 0) > resume_after} if resume_after else set()

    async def one(c: dict[str, Any]) -> None:
        nonlocal done
        if stop and stop():
            return
        if fresh and slugify(c["name"]) in fresh:      # a paused build: this one was checked a moment ago
            stats["kept"] += 1
            return
        site = None
        if websites and c.get("website"):
            async with sem:
                if stop and stop():
                    return
                site = await read_site(c["website"], d.get("products", {}), delay=delay)
        if site and site.pages:
            stats["websites_read"] += 1
        # web-search finds must prove themselves on their own site
        evidence = (site.mentions(stems) if site and stems else []) + ([p for p, _ in site.products] if site else [])
        named = [s for s in stems if s in f"{c['name']} {c.get('kind', '')}".lower()]
        if c.get("needs_evidence") and not evidence:
            stats["skipped"] += 1
            return
        if not (evidence or named or c.get("products") or c.get("curator")):
            stats["skipped"] += 1
            return
        name = c["name"]
        if c.get("source_type") == "web" and site and site.names:
            name = site.names[0][0]
        eid = db.upsert_entity(slug, name, city=c.get("city"), region=c.get("region"), website=c.get("website"),
                               lat=c.get("lat"), lon=c.get("lon"))
        for rec in c["records"]:
            src, st = rec["source_url"], rec["source_type"]
            db.add_fact(eid, "name", rec["name"] if st != "web" else name, src, st)
            for field in ("city", "address"):
                if rec.get(field):
                    db.add_fact(eid, field, rec[field], src, st)
            for field, value in rec.get("contacts", {}).items():
                db.add_fact(eid, field, value, src, st)
            for p in rec.get("products", []):
                db.add_fact(eid, "product", p, src, st)
        if c.get("registry_id"):
            db.add_fact(eid, "registry_id", c["registry_id"], c["source_url"], "gleif")
        products: list[tuple[str, str]] = []
        about: list[tuple[str, str]] = []
        if site:
            if site.blocked:
                log(f"  {name}: {site.blocked}")
            for field, values in site.contacts.items():
                for value, page in values[:4]:
                    db.add_fact(eid, field, value, page, "website")
            for text, page in site.address[:1]:
                db.add_fact(eid, "address", text, page, "website")
            for product, page in site.products:
                db.add_fact(eid, "product", product, page, "website")
            products, about = site.products, site.about
        facts = db.facts(eid)
        on_map = (not osm_queried) or any(r["source_type"] == "osm" for r in c["records"])
        lei: bool | None = None
        if any(r["source_type"] == "gleif" for r in c["records"]):
            lei = True
        elif CHECK_LEI and (c.get("website") or site):
            from .collect import gleif as _gleif
            async with sem:
                lei = await _gleif.has_lei(name, d["country"])
        db.clear_field(eid, "gap")
        for code, detail, src in gaps.find(website=c.get("website", ""), site=site, facts=facts, on_map=on_map,
                                           product_terms=bool(d.get("products")), osm_tags=c.get("osm_tags"), has_lei=lei,
                                           booking_matters=booking_matters):
            db.add_fact(eid, "gap", f"{code}: {detail}", src or "sourcebook-check", "check")
        facts = [f for f in db.facts(eid) if f["field"] != "gap"]
        product_names = list(dict.fromkeys([p for p, _ in products] + [f["value"] for f in facts if f["field"] == "product"]))
        text, sources = template_description(name, d.get("kind_label", "business"), d.get("country_name", d.get("country", "")),
                                             facts, products or [(p, facts[0]["source_url"]) for p in product_names], about, c.get("city", ""))
        nicer = await llm_rephrase(text, facts)
        db.set_description(eid, nicer or text, sources, product_names)
        stats["kept"] += 1
        done += 1
        log(f"  ✓ {name}")

    await asyncio.gather(*(one(c) for c in candidates))
    db.reindex(slug)
    if stop and stop():
        log("Stopped. Businesses already checked are kept.")
        raise Cancelled
    stats["new"] = max(0, len(db.entities(slug)) - before)
    db.upsert_dataset(slug, d["title"], d.get("summary", ""), d.get("country", ""), {k: v for k, v in d.items() if not k.startswith("_")})
    log(f"Done: {stats['kept']} businesses on the page, {stats['new']} new ({stats['skipped']} skipped as not relevant).")
    return stats


DEMO = [
    # Clearly fictional examples (".example" web addresses) so the page design can be seen before a real build.
    ("Example Packaging Co. (sample)", "Tema", ["PET bottles and preforms", "Caps and closures"], {"phone": "+233 30 000 0001", "whatsapp": "+233200000001", "email": "sales@packaging-co.example"}),
    ("Sample Flexible Films (sample)", "Kumasi", ["Sachet and roll film", "Flexible pouches", "Labels and printing"], {"phone": "+233 32 000 0002", "telegram": "https://t.me/sample_films"}),
    ("Demo Cartons Ltd (sample)", "Accra", ["Cartons and corrugated boxes", "Paper bags"], {"email": "hello@demo-cartons.example", "phone": "+233 30 000 0003"}),
    ("Placeholder Containers (sample)", "Takoradi", ["Food containers and takeaway", "Plastic bags"], {"whatsapp": "+233200000004"}),
]


DEMO_GAPS = {
    "Example Packaging Co. (sample)": ["no_online_ordering", "not_mobile"],
    "Sample Flexible Films (sample)": ["no_https", "no_whatsapp", "no_social"],
    "Demo Cartons Ltd (sample)": ["no_whatsapp", "outdated_site", "no_online_ordering"],
    "Placeholder Containers (sample)": ["no_website", "no_email", "not_on_map"],
}


def load_demo(db: DB, definition: Path) -> None:
    d = load_definition(definition)
    db.upsert_dataset(d["slug"], d["title"], d.get("summary", ""), d.get("country", ""), {k: v for k, v in d.items() if not k.startswith("_")}, demo=True)
    for name, city, products, contacts in DEMO:
        site = f"https://{slugify(name)}.example"
        eid = db.upsert_entity(d["slug"], name, city=city, website=site, demo=1)
        with db.conn() as c:
            c.execute("UPDATE entities SET demo=1 WHERE id=?", (eid,))
        for field, value in {**contacts, "website": site}.items():
            db.add_fact(eid, field, value, site + "/contact", "demo", time.time() - 86400 * 3)
        for p in products:
            db.add_fact(eid, "product", p, site + "/products", "demo")
        for code in DEMO_GAPS.get(name, []):
            db.add_fact(eid, "gap", f"{code}: Example finding for the sample data.", site, "check")
        text = f"{name} is a fictional example used to show how a supplier profile looks. It is not a real business."
        db.set_description(eid, text, [site], products)
    db.reindex(d["slug"])
