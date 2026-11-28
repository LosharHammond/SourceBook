"""Source-linked CC0 legal-entity seed catalogue. Never invents contacts or industries.
The registry complements local businesses collected from OpenStreetMap and websites.
"""
from __future__ import annotations
import asyncio
import json
import time
import httpx
from .collect import USER_AGENT
from .collect.gleif import ENDPOINT, parse
from .db import DB, slugify
from .pages import countries, country_name

PRIORITY = "GH NG KE ZA CI SN EG MA TZ UG RW ZM US GB IN DE BR AU JP AE CA FR SG NZ MX ID PH PK BD AR CL CO PE KR MY TH VN TR ES IT NL BE CH AT SE NO DK FI IE PT PL CZ GR RO HU IL SA QA BH KW OM JO LB TN DZ CM ET MU NA BW MZ ZW".split()


def ingest(db: DB, country: str, records: list[dict]) -> int:
    slug = "registered-businesses-" + country.lower()
    if not db.dataset(slug):
        db.create_dataset(slug, "Registered businesses in " + country_name(country),
                          "Active legal entities from GLEIF (CC0). Registry entries are not proof of trading activity, services, or endorsement.", country,
                          {"slug": slug, "country": country, "sources": ["gleif"], "what": "registered businesses"}, None, "published")
    now = time.time()
    entities, facts = [], []
    for row in records:
        lei = row["registry_id"]
        eslug = slugify(row["name"])[:55] + "-" + lei.lower()
        eid = slug + ":" + eslug
        text = f"{row['name']} is recorded as an active legal entity in {country_name(country)} by GLEIF. Services and contact details have not yet been checked."
        entities.append((eid, slug, row["name"], eslug, row.get("city", ""), row.get("region", ""), text,
                         json.dumps([row["source_url"]]), now, now))
        for field, value in (("name",row["name"]),("registry_id",lei),("address",row.get("address", ""))):
            if value:
                facts.append((eid,field,value,row["source_url"],"gleif",now))
    with db.conn() as c:
        before = c.execute("SELECT COUNT(*) FROM entities WHERE dataset=?", (slug,)).fetchone()[0]
        sql = "INSERT INTO entities(id,dataset,name,slug,city,region,description,description_sources,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,city=excluded.city,region=excluded.region,updated_at=excluded.updated_at"
        factsql = "INSERT INTO facts(entity,field,value,source_url,source_type,observed_at) VALUES(?,?,?,?,?,?) ON CONFLICT(entity,field,value,source_url) DO UPDATE SET observed_at=excluded.observed_at"
        if db.pg:
            with c.raw.cursor() as cur:
                cur.executemany(sql.replace("?", "%s"), entities)
                cur.executemany(factsql.replace("?", "%s"), facts)
        else:
            c.executemany(sql, entities)
            c.executemany(factsql, facts)
        after = c.execute("SELECT COUNT(*) FROM entities WHERE dataset=?", (slug,)).fetchone()[0]
        c.execute("UPDATE datasets SET built_at=? WHERE slug=?", (now,slug))
    db.reindex(slug)
    return after - before


async def grow(db: DB, target: int, seconds: float = 1800, log=print) -> dict:
    """Add up to target new records, rotating countries and paginating between runs.
    Durable cursor: reruns do not keep importing the first page or count updates as additions.
    """
    from .discover import today_start
    end = time.time() + seconds
    codes = list(dict.fromkeys(PRIORITY + [code for code,_ in countries()]))
    offset = int(db.meta("catalog_country_cursor", "0")) % len(codes)
    added = fetched = errors = 0
    touched = set()
    async with httpx.AsyncClient(headers={"User-Agent":USER_AGENT,"Accept":"application/vnd.api+json"}, timeout=45, follow_redirects=True) as client:
        for i in range(len(codes)*4):
            if time.time() > end - 50 or added >= target:
                break
            index = (offset+i) % len(codes)
            code = codes[index]
            page = int(db.meta("catalog_page_"+code, "1"))
            try:
                response = await client.get(ENDPOINT, params={"filter[entity.legalAddress.country]":code,
                    "filter[entity.status]":"ACTIVE", "page[size]":200, "page[number]":page})
                response.raise_for_status()
                data = response.json()
                rows = parse(data)
                new = ingest(db, code, rows) if rows else 0
                added += new
                fetched += len(rows)
                touched.add(code)
                last = int(data.get("meta",{}).get("pagination",{}).get("lastPage") or 1)
                db.set_meta("catalog_page_"+code, str(page+1 if page < last else 1))
                db.set_meta("catalog_country_cursor", str((index+1) % len(codes)))
                log(f"Catalogue {code}: {len(rows)} sourced records, {new} new; {added}/{target} added this run", flush=True)
            except (httpx.HTTPError, ValueError) as exc:
                errors += 1
                log(f"Catalogue {code}: source unavailable ({type(exc).__name__}); retry next run", flush=True)
            await asyncio.sleep(.25)
    if added == 0 and errors and not touched:
        raise RuntimeError("The catalogue sources could not be reached; no additions were made.")
    db.set_meta("catalog_last_run", json.dumps({"at":time.time(),"new":added,"fetched":fetched,"countries":len(touched),"source_errors":errors}))
    return {"new":added,"fetched":fetched,"countries":len(touched),"source_errors":errors}


def load_curated(db: DB) -> int:
    """Small, manually verified service listings. Sourced facts, never imported third-party reviews."""
    from pathlib import Path
    from .discover import page_for
    path = Path(__file__).resolve().parent.parent / "datasets" / "verified-businesses.json"
    if not path.exists():
        return 0
    version = "2026-11-28-v1"
    if db.meta("curated_version", "") == version:
        return 0
    added = 0
    touched = set()
    for row in json.loads(path.read_text()):
        slug = page_for(db, row["topic"], row["country"])
        eid = f"{slug}:{slugify(row['name'])}"
        old = db.entity(slug, slugify(row["name"]))
        db.upsert_entity(slug,row["name"],city=row.get("city",""),website=row["website"])
        src = row["source_url"]
        for field,value in (("name",row["name"]),("website",row["website"]),("city",row.get("city",""))):
            if value:
                db.add_fact(eid,field,value,src,"curator")
        for service in row["services"]:
            db.add_fact(eid,"product",service,src,"curator")
        db.set_description(eid, f"{row['name']} publishes {', '.join(row['services']).lower()} services in {country_name(row['country'])} on its website. Details manually checked on {row['verified_at']}.", [src], row["services"])
        touched.add(slug)
        added += int(old is None)
    for slug in touched:
        db.reindex(slug)
    db.set_meta("curated_version", version)
    return added
