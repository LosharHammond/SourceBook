import asyncio
import json
import re
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from sourcebook import build as build_mod
from sourcebook.collect import osm
from sourcebook.collect.site import SiteFacts, extract, normalise_phone
from sourcebook.db import DB
from sourcebook.web import create_app

DEF = Path(__file__).resolve().parent.parent / "datasets" / "food-packaging-ghana.json"
TERMS = json.loads(DEF.read_text())["products"]

SITE_HTML = """<html><head><meta name="description" content="We manufacture PET preforms and bottle caps for water and juice producers across Ghana.">
<script type="application/ld+json">{"@type":"Organization","telephone":"030 222 3344","address":{"streetAddress":"Plot 5","addressLocality":"Tema"}}</script></head>
<body><a href="tel:+233 24 123 4567">Call</a><a href="mailto:sales@acmepack.test">Email</a>
<a href="https://wa.me/233241234567">WhatsApp</a><a href="https://t.me/acmepack">Telegram</a>
<a href="/contact-us">Contact</a><a href="https://other.test/contact">Elsewhere</a>
<p>Our range: PET preforms, closures and shrink film.</p></body></html>"""


def test_osm_query_and_parse():
    q = osm.build_query("gh", ['["shop"="packaging"]'], "packag")
    assert '"ISO3166-1"="GH"' in q and 'nwr["shop"="packaging"](area.a);' in q
    data = {"elements": [
        {"type": "node", "id": 1, "lat": 5.6, "lon": -0.2, "tags": {"name": "Acme Pack", "shop": "packaging", "phone": "+233 30 111 2222",
                                                                   "website": "acmepack.test", "addr:city": "Accra", "contact:whatsapp": "+233241234567"}},
        {"type": "way", "id": 2, "center": {"lat": 6.7, "lon": -1.6}, "tags": {"industrial": "packaging"}},  # no name: skipped
    ]}
    rows = osm.parse(data)
    assert len(rows) == 1 and rows[0]["osm_url"] == "https://www.openstreetmap.org/node/1"
    assert rows[0]["contacts"]["whatsapp"] == "+233241234567" and rows[0]["city"] == "Accra"


def test_site_extract():
    f = SiteFacts(url="https://acmepack.test")
    links = extract(SITE_HTML, "https://acmepack.test/", f, TERMS)
    vals = {k: [v for v, _ in vs] for k, vs in f.contacts.items()}
    assert "+233241234567" in vals["phone"] and "+233302223344" in vals["phone"]
    assert vals["email"] == ["sales@acmepack.test"]
    assert vals["whatsapp"] == ["+233241234567"] and vals["telegram"] == ["https://t.me/acmepack"]
    products = [p for p, _ in f.products]
    assert "PET bottles and preforms" in products and "Caps and closures" in products
    assert links == ["https://acmepack.test/contact-us"]           # never leaves the business's own site
    assert f.address[0][0].startswith("Plot 5") and f.about


def test_normalise_phone():
    assert normalise_phone("024 123 4567") == "+233241234567"
    assert normalise_phone("00233 24 123 4567") == "+233241234567"


def no_sources(monkeypatch):
    from sourcebook.collect import gleif, wikidata
    async def none(*a, **k):
        return []
    monkeypatch.setattr(wikidata, "fetch", none)
    monkeypatch.setattr(gleif, "fetch", none)
    async def unknown(*a, **k):
        return None
    monkeypatch.setattr(gleif, "has_lei", unknown)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    from sourcebook.collect import geocode
    async def town(query, country=None, db=None):
        return {"name": query.title(), "country": "US" if "austin" in query else (country or "GH"), "lat": 30.27, "lon": -97.74,
                "bbox": [30.1, -97.9, 30.5, -97.5], "area_id": 3600113314, "display": query}
    monkeypatch.setattr(geocode, "find_place", town)


@pytest.fixture
def built(tmp_path, monkeypatch):
    async def fake_osm(country, selectors, regex, place=None):
        return osm.parse({"elements": [
            {"type": "node", "id": 1, "lat": 5.6, "lon": -0.2, "tags": {"name": "Acme Pack", "shop": "packaging", "website": "https://acmepack.test", "addr:city": "Tema"}},
            {"type": "node", "id": 2, "tags": {"name": "Golden Box Office", "office": "company"}},     # not packaging: hidden
        ]})

    async def fake_site(url, terms, delay=2.0):
        f = SiteFacts(url=url, pages=[url])
        extract(SITE_HTML, url, f, terms)
        return f

    monkeypatch.setattr(build_mod.osm, "fetch", fake_osm)
    monkeypatch.setattr(build_mod, "read_site", fake_site)
    no_sources(monkeypatch)
    db = DB(tmp_path / "t.db")
    build_mod.load_demo(db, DEF)
    stats = asyncio.run(build_mod.build(DEF, db, log=lambda m: None))
    return db, stats


def test_build_keeps_sources_and_replaces_demo(built):
    db, stats = built
    assert stats["kept"] == 1 and stats["skipped"] == 1
    rows = db.entities("food-packaging-suppliers-ghana")
    assert [r["name"] for r in rows] == ["Acme Pack"]               # sample rows removed by a real build
    e = rows[0]
    assert all(f["source_url"].startswith("https://") for f in e["facts"])
    assert {"osm", "website"} <= {f["source_type"] for f in e["facts"]}
    assert "Tema" in e["description"] and "PET bottles and preforms" in e["description"]
    assert e["description_sources"] and "WhatsApp" in e["description"]


def csrf_of(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no CSRF field on page"
    return m.group(1)


def test_web_pages_exports_and_corrections(built):
    db, _ = built
    c = TestClient(create_app(db, run_jobs=False))
    slug = "food-packaging-suppliers-ghana"
    assert c.get("/").status_code == 200
    page = c.get(f"/d/{slug}").text
    assert "Acme Pack" in page and "Golden Box" not in page
    prof = c.get(f"/d/{slug}/acme-pack").text
    assert "https://wa.me/233241234567" in prof and "openstreetmap.org/node/1" in prof
    data = c.get(f"/d/{slug}.json?channel=telegram").json()
    assert data["count"] == 1 and "OpenStreetMap" in data["licence"] and data["records"][0]["telegram"] == ["https://t.me/acmepack"]
    csv_text = c.get(f"/d/{slug}.csv").text
    assert csv_text.splitlines()[0].lstrip("\ufeff").startswith("name,city") and "ODbL" in csv_text
    assert c.get(f"/d/{slug}.json?product=Paper bags").json()["count"] == 0
    assert "Acme Pack" in c.get("/search?q=preforms").text
    # a correction without the form's token is refused; with it, it's queued
    assert c.post(f"/d/{slug}/acme-pack/correct", data={"kind": "change", "field": "email", "value": "x@y.test"}).status_code == 400
    r = c.post(f"/d/{slug}/acme-pack/correct", data={"csrf": csrf_of(prof), "kind": "change", "field": "email", "value": "orders@acmepack.test",
                                                     "evidence_url": "https://acmepack.test/contact"}, follow_redirects=False)
    assert r.status_code == 303 and len(db.corrections("open")) == 1
    before = len(db.corrections("open"))
    c.post(f"/d/{slug}/acme-pack/correct", data={"csrf": csrf_of(prof), "website_url": "spam"})      # honeypot
    assert len(db.corrections("open")) == before


def signup(c, name, email, password="correct horse battery"):
    html = c.get("/signup").text
    r = c.post("/signup", data={"csrf": csrf_of(html), "name": name, "email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    return csrf_of(c.get("/app").text)


@pytest.fixture
def fake_sources(monkeypatch):
    async def fake_osm(country, selectors, regex, place=None):
        return osm.parse({"elements": [
            {"type": "node", "id": 7, "lat": 5.3, "lon": -4.0, "tags": {"name": "Golden Cocoa Export", "office": "company", "website": "https://goldencocoa.test", "addr:city": "Abidjan"}},
        ]})

    async def fake_site(url, terms, delay=2.0):
        f = SiteFacts(url=url, pages=[url])
        extract("<html><body><a href='https://wa.me/2250700000000'>WhatsApp</a><p>We export cocoa beans and cocoa butter.</p></body></html>", url, f, terms)
        return f

    monkeypatch.setattr(build_mod.osm, "fetch", fake_osm)
    monkeypatch.setattr(build_mod, "read_site", fake_site)
    no_sources(monkeypatch)
    monkeypatch.setenv("SOURCEBOOK_POLL", "0.05")
    monkeypatch.setenv("SOURCEBOOK_CRAWL_INTERVAL", "0")


def wait_for(fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def test_workspace_flow(tmp_path, fake_sources):
    db = DB(tmp_path / "w.db")
    app = create_app(db)
    with TestClient(app) as admin, TestClient(app) as member, TestClient(app) as guest:
        assert admin.get("/app", follow_redirects=False).headers["location"].startswith("/signin")
        a_tok = signup(admin, "Ama Admin", "ama@example.org")
        m_tok = signup(member, "Kofi Member", "kofi@example.org")
        assert db.user(email="ama@example.org")["role"] == "admin" and db.user(email="kofi@example.org")["role"] == "member"
        assert member.get("/app/admin").status_code == 403
        # validation
        r = member.post("/app/new", data={"csrf": m_tok, "what": "x", "country": "ZZ"})
        assert r.status_code == 400 and "Choose a country" in r.text
        # member builds a page from the browser
        r = member.post("/app/new", data={"csrf": m_tok, "what": "Cocoa exporters", "country": "CI", "keywords": "cocoa",
                                          "products": "Cocoa beans\nCocoa butter", "websites": "Ivory Beans, ivorybeans.test"}, follow_redirects=False)
        assert r.status_code == 303
        slug = r.headers["location"].split("/app/pages/")[1].split("?")[0]
        assert wait_for(lambda: db.dataset(slug)["status"] == "review"), db.jobs(dataset=slug)
        job = db.jobs(dataset=slug)[0]
        assert job["status"] == "done" and "Building" in job["log"]
        live = member.get(f"/app/jobs/{job['id']}").json()
        assert live["status"] == "done" and live["count"] == 2
        names = [e["name"] for e in db.entities(slug)]
        assert "Golden Cocoa Export" in names and "Ivory Beans" in names
        # not public until approved
        assert guest.get(f"/d/{slug}").status_code == 404 and slug not in guest.get("/sitemap.xml").text
        assert member.get(f"/d/{slug}").status_code == 200                      # owner preview
        assert "Golden Cocoa" not in guest.get("/search?q=cocoa").text
        assert guest.get(f"/app/jobs/{job['id']}").status_code in (303, 307, 200) and "log" not in guest.get(f"/app/jobs/{job['id']}", follow_redirects=False).text
        # another member can't manage it
        other = TestClient(app)
        o_tok = signup(other, "Esi", "esi@example.org")
        assert other.get(f"/app/pages/{slug}").status_code == 404
        assert other.post(f"/app/pages/{slug}/delete", data={"csrf": o_tok, "confirm": db.dataset(slug)["title"]}).status_code == 404
        # admin approves → public
        assert slug in admin.get("/app/admin").text
        admin.post(f"/app/pages/{slug}/publish", data={"csrf": a_tok})
        assert db.dataset(slug)["status"] == "published"
        assert guest.get(f"/d/{slug}").status_code == 200 and "Golden Cocoa" in guest.get("/search?q=cocoa").text
        # one build at a time, and rebuilding keeps it public
        member.post(f"/app/pages/{slug}/build", data={"csrf": m_tok})
        assert wait_for(lambda: db.jobs(dataset=slug)[0]["status"] == "done")
        assert db.dataset(slug)["status"] == "published"
        # shortlists
        eid = next(e["id"] for e in db.entities(slug) if e["name"] == "Golden Cocoa Export")
        prof = member.get(f"/d/{slug}/{eid.split(':')[1]}").text
        assert "Save to a shortlist" in prof
        member.post("/app/lists/save", data={"csrf": m_tok, "entity": eid, "list": "new", "new_name": "Call this week"})
        lst = db.lists(db.user(email="kofi@example.org")["id"])[0]
        assert lst["name"] == "Call this week" and lst["n"] == 1
        member.post(f"/app/lists/{lst['id']}/note", data={"csrf": m_tok, "entity": eid, "note": "Ask about MOQ"})
        csv_text = member.get(f"/app/lists/{lst['id']}.csv").text
        assert "Golden Cocoa Export" in csv_text and "Ask about MOQ" in csv_text and "+2250700000000" in csv_text
        assert other.get(f"/app/lists/{lst['id']}").status_code == 404          # private to its owner
        # corrections: the page owner reviews their own page's corrections
        g_prof = guest.get(f"/d/{slug}/{eid.split(':')[1]}").text
        guest.post(f"/d/{slug}/{eid.split(':')[1]}/correct", data={"csrf": csrf_of(g_prof), "kind": "change", "field": "city", "value": "San-Pédro"})
        assert "San-Pédro" in member.get("/app/corrections").text and "San-Pédro" not in other.get("/app/corrections").text
        cid = db.corrections("open")[0]["id"]
        assert other.post(f"/app/corrections/{cid}", data={"csrf": o_tok, "action": "approve"}).status_code == 404
        member.post(f"/app/corrections/{cid}", data={"csrf": m_tok, "action": "approve"})
        assert any(f["value"] == "San-Pédro" and f["source_type"] == "correction" for f in db.entity(slug, eid.split(":")[1])["facts"])
        # forms without the token are refused
        assert member.post(f"/app/pages/{slug}/delete", data={"confirm": db.dataset(slug)["title"]}).status_code == 400
        # account: password change signs out other sessions
        member2 = TestClient(app)
        h = member2.get("/signin").text
        member2.post("/signin", data={"csrf": csrf_of(h), "email": "kofi@example.org", "password": "correct horse battery"})
        assert member2.get("/app", follow_redirects=False).status_code == 200
        member.post("/app/account", data={"csrf": m_tok, "action": "password", "current": "correct horse battery", "new": "another long password"})
        assert member2.get("/app", follow_redirects=False).status_code == 303
        # wrong password is refused
        h = member2.get("/signin").text
        assert member2.post("/signin", data={"csrf": csrf_of(h), "email": "kofi@example.org", "password": "nope"}).status_code == 400
        # delete
        title = db.dataset(slug)["title"]
        m_tok = csrf_of(member.get("/app").text)
        member.post(f"/app/pages/{slug}/delete", data={"csrf": m_tok, "confirm": title})
        assert db.dataset(slug) is None


def test_build_limits(tmp_path, fake_sources, monkeypatch):
    import sourcebook.web as web
    monkeypatch.setattr(web, "DAILY_BUILDS", 1)
    db = DB(tmp_path / "l.db")
    app = create_app(db, run_jobs=False)                     # jobs stay queued
    with TestClient(app) as admin, TestClient(app) as m:
        signup(admin, "Admin", "a@example.org")
        tok = signup(m, "Member", "m@example.org")
        r = m.post("/app/new", data={"csrf": tok, "what": "Cocoa exporters", "country": "CI"}, follow_redirects=False)
        assert r.status_code == 303
        r = m.post("/app/new", data={"csrf": tok, "what": "Cashew exporters", "country": "CI"}, follow_redirects=False)
        assert "already+have+a+build" in r.headers["location"] or "already%20have%20a%20build" in r.headers["location"]


def test_demo_is_marked(tmp_path):
    db = DB(tmp_path / "d.db")
    build_mod.load_demo(db, DEF)
    rows = db.entities("food-packaging-suppliers-ghana")
    assert rows and all(r["demo"] and ".example" in r["website"] and "(sample)" in r["name"] for r in rows)
    out = TestClient(create_app(db)).get("/d/food-packaging-suppliers-ghana.json").json()
    assert all(r["example_data"] for r in out["records"])


def test_private_addresses_are_never_read():
    from sourcebook.collect.site import public_host, read_site
    assert not public_host("http://127.0.0.1/") and not public_host("http://169.254.169.254/") and not public_host("http://10.1.2.3/")
    assert not public_host("http://[::1]/") and not public_host("https://example.org:8443/") and not public_host("file:///etc/passwd")
    f = asyncio.run(read_site("http://localhost:8100", {}))
    assert f.pages == [] and f.blocked


def test_wikidata_gleif_websearch_parsers():
    from sourcebook.collect import gleif, websearch, wikidata
    q = wikidata.build_query("CI", ["cocoa", 'x"; DROP'])
    assert '"CI"' in q and 'mwapi:search "cocoa"' in q and "DROP" in q and '";' not in q.split("DROP")[0][-3:]
    rows = wikidata.parse({"results": {"bindings": [
        {"item": {"value": "http://www.wikidata.org/entity/Q1"}, "itemLabel": {"value": "Cargill Cocoa CI"}, "website": {"value": "https://cargill.test"},
         "hqLabel": {"value": "Abidjan"}, "industryLabel": {"value": "cocoa industry"}},
        {"item": {"value": "http://www.wikidata.org/entity/Q2"}, "itemLabel": {"value": "Q2"}}]}})
    assert len(rows) == 1 and rows[0]["contacts"]["website"] == "https://cargill.test" and rows[0]["city"] == "Abidjan"
    g = gleif.parse({"data": [{"id": "LEI1", "attributes": {"lei": "LEI1", "entity": {"legalName": {"name": "IVORY COCOA PRODUCTS"}, "status": "ACTIVE",
                     "legalAddress": {"addressLines": ["ZONE PORTUAIRE"], "city": "SAN PEDRO", "country": "CI"}}}},
                              {"id": "LEI2", "attributes": {"lei": "LEI2", "entity": {"legalName": {"name": "OLD CO"}, "status": "INACTIVE"}}}]})
    assert [x["name"] for x in g] == ["Ivory Cocoa Products"] and g[0]["city"] == "San Pedro" and "LEI1" in g[0]["source_url"]
    w = websearch.parse({"web": {"results": [{"url": "https://www.goldbean.test/about", "title": "Gold Bean Exports | Home"},
                                             {"url": "https://www.facebook.com/goldbean", "title": "Gold Bean"},
                                             {"url": "https://goldbean.test/contact", "title": "dupe"}]}})
    assert len(w) == 1 and w[0]["name"] == "Gold Bean Exports" and w[0]["needs_evidence"]


def test_query_parsing_and_merge():
    from sourcebook.discover import parse_query
    assert parse_query("cocoa exporters in Côte d’Ivoire", "GH") == ("cocoa exporters", "CI")
    assert parse_query("Find solar installers in Kenya", None) == ("solar installers", "KE")
    assert parse_query("ghanaian shea butter producers", None) == ("shea butter producers", "GH")
    assert parse_query("pharmacies in Accra", "GH") == ("pharmacies", "GH")
    merged = build_mod.merge([
        {"name": "Ivory Cocoa Products Ltd", "source_type": "gleif", "source_url": "g", "contacts": {}},
        {"name": "IVORY COCOA PRODUCTS", "source_type": "wikidata", "source_url": "w", "website": "https://ivory.test", "contacts": {"website": "https://ivory.test"}},
        {"name": "Ivory Cocoa | Home", "source_type": "web", "source_url": "https://www.ivory.test/", "website": "https://www.ivory.test/", "contacts": {}, "needs_evidence": True},
    ])
    assert len(merged) == 1 and len(merged[0]["records"]) == 3 and merged[0]["name"] == "Ivory Cocoa Products Ltd"


def test_gaps_found_from_checks():
    from sourcebook import gaps
    site = SiteFacts(url="http://oldpack.test", pages=["http://oldpack.test/"], final_url="http://oldpack.test/")
    extract("<html><body><p>Cartons and boxes. © 2018 Old Pack</p><a href='tel:0302223344'>call</a></body></html>", "http://oldpack.test/", site, TERMS)
    facts = [{"field": "phone", "value": "+233302223344", "source_url": "http://oldpack.test/", "source_type": "website"}]
    codes = [c for c, _, _ in gaps.find(website="http://oldpack.test", site=site, facts=facts, on_map=False)]
    for c in ("no_https", "not_mobile", "outdated_site", "no_online_ordering", "no_email", "no_whatsapp", "no_social", "not_on_map", "no_contact_form"):
        assert c in codes, c
    assert "no_phone" not in codes and "no_products_listed" not in codes and "no_website" not in codes
    good = SiteFacts(url="https://good.test", pages=["https://good.test/"], final_url="https://good.test/")
    extract("<html><head><meta name='viewport' content='width=device-width'></head><body><a href='/cart'>Cart</a>"
            "<a href='https://facebook.com/good'>fb</a><a href='https://wa.me/233241234567'>wa</a><form><textarea></textarea></form></body></html>",
            "https://good.test/", good, TERMS)
    facts = [{"field": f, "value": "x", "source_url": "https://good.test/", "source_type": "website"} for f in ("whatsapp", "email", "facebook")]
    good.signals |= {"meta_description", "tracking", "meta_pixel", "booking", "delivery", "hours", "address"}
    good.load_seconds = 0.4
    codes = [c for c, _, _ in gaps.find(website="https://good.test", site=good, facts=facts, on_map=True)]
    assert codes == ["no_products_listed"], codes
    none = [c for c, _, _ in gaps.find(website="", site=None, facts=[{"field": "name", "value": "x", "source_url": "https://osm.test/1", "source_type": "osm"}], on_map=True)]
    assert none[0] == "no_website" and "no_online_ordering" not in none


def test_search_discovers_and_crawler(tmp_path, fake_sources):
    db = DB(tmp_path / "s.db")
    app = create_app(db)
    with TestClient(app) as guest:
        html = guest.get("/search?q=cocoa+exporters+ivory+coast").text
        assert 'id="live"' in html
        slug = re.search(r'data-slug="([^"]+)"', html).group(1)
        assert slug.startswith("cocoa-exporters-in-c")
        assert wait_for(lambda: db.jobs(dataset=slug)[0]["status"] == "done"), db.jobs(dataset=slug)
        data = guest.get(f"/search/live/{slug}").json()
        assert data["status"] == "done" and data["count"] >= 1 and "Traceback" not in data["progress"]
        assert data["tabs"][0]["key"] == "all" and data["businesses"][0]["reasons"]
        assert any("No email address published" in r["missing"] for r in guest.get(f"/d/{slug}.json").json()["records"])
        ads = guest.get(f"/search/live/{slug}?lens=ads").json()
        assert ads["lens_label"] == "Paid ads" and all(b["priority"] >= 0 for b in ads["businesses"])
        csv_text = guest.get(f"/search/export/{slug}.csv?lens=web").text
        assert csv_text.lstrip("\ufeff").startswith("rank,name") and "Golden Cocoa Export" in csv_text
        assert db.dataset(slug)["status"] == "discovered"
        page = guest.get(f"/d/{slug}").text
        assert "Collected automatically" in page and 'content="noindex"' in page and slug not in guest.get("/sitemap.xml").text
        # same search again doesn't start another crawl
        n = len(db.jobs(dataset=slug))
        guest.get("/search?q=cocoa+exporters+in+Cote+d%27Ivoire")
        assert len(db.jobs(dataset=slug)) == n
        # unclear searches don't crawl
        assert 'id="live"' not in guest.get("/search?q=zz").text
        # find clients: businesses lacking email, with their published WhatsApp
        opp = guest.get("/opportunities?lacks=no_email").text
        assert "Golden Cocoa Export" in opp and "wa.me/2250700000000" in opp
        assert "Golden Cocoa Export" in guest.get("/opportunities.csv?lacks=no_email").text
        prof = guest.get(f"/d/{slug}/golden-cocoa-export").text
        assert "doesn’t have yet" in prof and "Who could help" in prof
        export = guest.get(f"/d/{slug}.json").json()
        assert "No email address published" in export["records"][0]["missing"]
    # crawler: starter topics, one round queues due topics and marks them run
    from sourcebook import discover
    assert discover.load_starter_targets(db) >= 30
    assert discover.load_starter_targets(db) == 0
    queued = discover.crawl_tick(db, force=True)
    assert len(queued) == discover.crawler_settings(db)["per_run"]
    assert all(j["priority"] == 9 and j["kind"] == "crawl" for j in db.jobs(status=("queued",)))
    assert discover.crawl_tick(db, force=True) == []               # previous round still queued
    db.set_meta("crawler_enabled", "0")
    assert discover.crawl_tick(db) == []


def test_admin_crawler_screen(tmp_path, fake_sources):
    db = DB(tmp_path / "c.db")
    app = create_app(db, run_jobs=False)
    with TestClient(app) as admin:
        tok = signup(admin, "Ama", "ama@example.org")
        assert "Crawler" in admin.get("/app/admin/crawler").text
        admin.post("/app/admin/crawler/starter", data={"csrf": tok})
        assert len(db.targets()) >= 30
        admin.post("/app/admin/crawler/add", data={"csrf": tok, "what": "Mango exporters", "country": "BF", "every_days": "7"})
        assert any(t["what"] == "Mango exporters" for t in db.targets())
        admin.post("/app/admin/crawler/settings", data={"csrf": tok, "enabled": "1", "daily_target": "1000", "per_run": "6"})
        assert discover_settings(db) == (True, 1000, 6)
        admin.post("/app/admin/crawler/run", data={"csrf": tok})
        assert len(db.jobs(status=("queued",))) == 6


def discover_settings(db):
    from sourcebook.discover import crawler_settings
    s = crawler_settings(db)
    return s["enabled"], s["daily_target"], s["per_run"]


def test_stated_needs_and_general_gaps():
    from sourcebook import gaps
    site = SiteFacts(url="https://agro.test", pages=["https://agro.test/"], final_url="https://agro.test/")
    extract("<html><head><meta name='viewport' content='x'></head><body><p>We are a cassava processing factory. "
            "We are looking for investors to expand our plant. Nous recrutons des techniciens. "
            "Recherchons des distributeurs en Afrique de l'Ouest.</p></body></html>", "https://agro.test/", site, TERMS)
    assert {"seeking_investment", "hiring", "seeking_distributors"} <= set(site.statements)
    found = {c: d for c, d, _ in gaps.find(website="https://agro.test", site=site, facts=[], on_map=True,
                                           osm_tags={"wheelchair": "no", "payment:cash": "yes", "payment:cards": "no"}, has_lei=False)}
    for code in ("seeking_investment", "hiring", "no_export", "no_certification", "no_delivery", "no_card_payment", "no_lei",
                 "not_accessible", "no_opening_hours"):
        assert code in found, code
    assert found["seeking_investment"].startswith("Its website says: “")
    # a certified exporter with delivery doesn't get those gaps
    site2 = SiteFacts(url="https://x.test", pages=["https://x.test/"], final_url="https://x.test/")
    extract("<p>Our factory is ISO 22000 and HACCP certified. We export to Europe and deliver nationwide.</p>", "https://x.test/", site2, TERMS)
    found2 = {c for c, _, _ in gaps.find(website="https://x.test", site=site2, facts=[], on_map=True)}
    assert not {"no_export", "no_certification", "no_delivery"} & found2


def test_leads_scoring_and_opening_lines():
    from sourcebook import leads
    e = {"name": "Acme Dental", "city": "Austin", "contacts": {"email": [{"value": "a@b.test"}]},
         "gaps": [{"code": "no_meta_pixel", "label": "No Meta pixel", "category": "Online presence", "detail": "x"},
                  {"code": "no_tracking", "label": "No tracking", "category": "Online presence", "detail": "y"},
                  {"code": "no_website", "label": "No website", "category": "Online presence", "detail": "z"}]}
    pr, reasons = leads.score(e, "ads")
    assert pr == 68 and [r["code"] for r in reasons] == ["no_meta_pixel", "no_tracking"]
    assert leads.score(e, "hiring") == (0, [])
    assert "Meta pixel" in leads.opening_line(e, "ads", reasons)
    assert leads.rank([e], "web")[0]["reasons"][0]["code"] == "no_website"


def test_city_search_world_round_and_monitors(tmp_path, fake_sources):
    from sourcebook import discover
    db = DB(tmp_path / "m.db")
    app = create_app(db)
    with TestClient(app) as c:
        tok = signup(c, "Ama", "ama@example.org")
        html = c.get("/search?q=dentists+in+Austin").text
        slug = re.search(r'data-slug="([^"]+)"', html).group(1)
        assert slug == "dentists-in-austin-united-states", slug
        d = json.loads(db.dataset(slug)["definition"])
        assert d["place"]["area_id"] == 3600113314 and d["country"] == "US" and '["amenity"="dentist"]' in d["osm_selectors"]
        assert wait_for(lambda: db.jobs(dataset=slug)[0]["status"] == "done")
        r = c.post("/app/monitors", data={"csrf": tok, "dataset": slug, "q": "dentists in Austin", "lens": "seo"}, follow_redirects=False)
        assert r.status_code == 303 and len(db.monitors(db.user(email="ama@example.org")["id"])) == 1
        page = c.get("/app/monitors").text
        assert "dentists in Austin" in page and "Local SEO" in page
        assert "dentists in Austin" in c.get("/app/monitors").text
    # world coverage: the first round touches different countries with the first business type
    db.set_meta("crawler_world", "1")
    queued = discover.world_round(db, 5)
    assert len(queued) == 5 and len({json.loads(db.dataset(s)["definition"])["country"] for s in queued}) == 5
    assert all(json.loads(db.dataset(s)["definition"])["osm_selectors"] == ['["amenity"="pharmacy"]'] for s in queued)
    cov = discover.world_coverage(db)
    assert cov["countries"] == 5 and cov["countries_total"] > 190


def test_inline_jobs_pause_and_crawler_finishes(tmp_path, fake_sources, monkeypatch):
    """Vercel-style hosting: no background worker; the search page runs its job in a request with a time limit,
    and the scheduled crawler run picks up anything paused or left behind."""
    import asyncio
    import time as _time
    from sourcebook.jobs import Runner
    monkeypatch.setenv("SOURCEBOOK_INLINE_JOBS", "1")
    db = DB(tmp_path / "v.db")
    app = create_app(db)
    with TestClient(app) as guest:
        html = guest.get("/search?q=cocoa+exporters+ivory+coast").text
        slug = re.search(r'data-slug="([^"]+)"', html).group(1)
        _time.sleep(0.5)
        assert db.jobs(dataset=slug)[0]["status"] == "queued"           # nothing runs in the background
        live = guest.get(f"/search/live/{slug}").json()
        assert live["run"] == f"/search/run/{slug}"
        assert guest.post(f"/search/run/{slug}").json() == {"ran": True}
        assert db.jobs(dataset=slug)[0]["status"] == "done"
        assert guest.post(f"/search/run/{slug}").json() == {"ran": False}  # nothing left to run
        assert guest.get(f"/search/live/{slug}").json()["run"] is None
    # a job past its deadline pauses, keeps its results and queues a follow-up
    jid = db.add_job(slug, None, "refresh", priority=5)
    runner = Runner(db, workers=1)
    asyncio.run(runner.run(db.claim_job(), deadline=_time.time() - 1))
    assert db.job(jid)["status"] == "done" and "Paused" in db.job(jid)["log"]
    follow = db.jobs(dataset=slug, status=("queued",))
    assert follow and follow[0]["kind"] == "continue"
    # a job a host stopped mid-run is picked up again by the crawler
    db.claim_job(dataset=slug)                                       # the follow-up starts, then its host stops
    with db.conn() as c:
        c.execute("UPDATE jobs SET started_at=? WHERE status='running'", (_time.time() - 3600,))
    result = asyncio.run(Runner(db, workers=2).run_for(120))
    assert result["requeued"] == 1 and result["jobs_run"] >= 1
    assert all(j["status"] != "running" for j in db.jobs(dataset=slug))
