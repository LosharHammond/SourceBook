"""Independent live lookups. Persist only open, attributable business records.
Google reviews and directory reviews are not scraped or mixed with community feedback.
"""
from __future__ import annotations
import asyncio
import json
import time
from .db import DB, slugify
from .build import merge, name_key
from .collect import osm, wikidata, gleif, websearch

PROVIDERS = {"osm":"OpenStreetMap", "wikidata":"Wikidata", "gleif":"GLEIF company register"}


async def fetch_source(provider: str, definition: dict) -> list[dict]:
    d = definition
    keywords = d.get("keywords") or d.get("relevance_keywords") or []
    if provider == "osm":
        parsed = await osm.fetch(d["country"],d.get("osm_selectors",[]),d.get("osm_name_regex"),place=d.get("place"))
        return [{**p, "website":p["contacts"].get("website", ""), "source_url":p["osm_url"], "source_type":"osm"} for p in parsed]
    if provider == "wikidata":
        rows = await wikidata.fetch(d["country"],keywords)
        return [{**p,"website":p.get("contacts",{}).get("website", "")} for p in rows]
    if provider == "gleif":
        return await gleif.fetch(d["country"],keywords,max_pages=1)
    raise ValueError("Unknown source")


def save_results(db: DB, slug: str, rows: list[dict]) -> int:
    """Batch writes: no network round trip per fact. Existing checked descriptions win."""
    page = db.dataset(slug)
    definition = json.loads(page["definition"] or "{}")
    place = definition.get("place")
    if place:
        town = name_key(place["name"])
        rows = [r for r in rows if r.get("source_type") == "osm" or (r.get("city") and town in name_key(r["city"]))]
    candidates = merge(rows)
    from .build import _domain
    with db.conn() as conn:
        old = conn.execute("SELECT name,slug,website FROM entities WHERE dataset=? AND hidden=0",(slug,)).fetchall()
    by_domain = {_domain(r["website"]): r for r in old if r["website"]}
    by_name = {name_key(r["name"]):r for r in old}
    now = time.time()
    entities, facts = [], []
    for row in candidates[:200]:
        if not row.get("name") or not str(row.get("source_url", "")).startswith("https://"):
            continue
        name = row["name"][:200]
        match = by_domain.get(_domain(row.get("website", ""))) or by_name.get(name_key(name))
        eslug = match["slug"] if match else slugify(name)
        eid = slug+":"+eslug
        kind = row.get("kind", "")
        products = row.get("products",[]) or ([kind] if kind else [])
        sources = list(dict.fromkeys(r["source_url"] for r in row["records"]))
        description = f"{name} is listed by {PROVIDERS.get(row['source_type'],row['source_type'])}" + (f" as {kind}" if kind else "") + ". See the source for the original listing. Its services and website checks may still be in progress."
        website = row.get("website") or row.get("contacts",{}).get("website", "")
        if website and "://" not in website:
            website = "https://"+website
        if not website.startswith(("http://","https://")):
            website = ""
        entities.append((eid,slug,name,eslug,row.get("city", ""),row.get("region", ""),website,description,json.dumps(sources),json.dumps(products),row.get("lat"),row.get("lon"),now,now))
        for rec in row["records"]:
            source, st = rec["source_url"],rec["source_type"]
            for field,value in [("name",rec.get("name",name)),("city",rec.get("city", "")),("address",rec.get("address", "")),*rec.get("contacts",{}).items()]:
                if value:
                    facts.append((eid,field,str(value)[:2000],source,st,now))
            for product in rec.get("products",[]):
                facts.append((eid,"product",str(product)[:2000],source,st,now))
        if row.get("registry_id"):
            facts.append((eid,"registry_id",row["registry_id"],row["source_url"],"gleif",now))
    if not entities:
        return 0
    sql = "INSERT INTO entities(id,dataset,name,slug,city,region,website,description,description_sources,products,lat,lon,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET city=CASE WHEN entities.city='' THEN excluded.city ELSE entities.city END, website=CASE WHEN entities.website='' THEN excluded.website ELSE entities.website END, lat=COALESCE(entities.lat,excluded.lat),lon=COALESCE(entities.lon,excluded.lon),updated_at=excluded.updated_at"
    fs = "INSERT INTO facts(entity,field,value,source_url,source_type,observed_at) VALUES(?,?,?,?,?,?) ON CONFLICT(entity,field,value,source_url) DO UPDATE SET observed_at=excluded.observed_at"
    with db.conn() as c:
        if db.pg:
            with c.raw.cursor() as cur:
                cur.executemany(sql.replace("?","%s"),entities)
                cur.executemany(fs.replace("?","%s"),facts)
        else:
            c.executemany(sql,entities)
            c.executemany(fs,facts)
    db.reindex(slug)
    return len(entities)


def existing_candidates(db: DB, slug: str) -> list[dict]:
    out = []
    for e in db.entities(slug):
        groups = {}
        for fact in e["facts"]:
            if fact["source_type"] in ("check", "demo"):
                continue
            key=(fact["source_url"],fact["source_type"])
            rec=groups.setdefault(key,{"name":e["name"],"source_url":key[0],"source_type":key[1],"contacts":{},"products":[],"city":e.get("city", ""),"_existing":True})
            if fact["field"] in ("phone","email","website","whatsapp","telegram"):
                rec["contacts"][fact["field"]]=fact["value"]
            elif fact["field"]=="product": rec["products"].append(fact["value"])
        for rec in groups.values():
            rec.update(website=e.get("website", ""),lat=e.get("lat"),lon=e.get("lon"))
            out.append(rec)
    return out
