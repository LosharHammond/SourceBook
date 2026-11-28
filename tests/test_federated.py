import re
from starlette.testclient import TestClient
from sourcebook.db import DB
from sourcebook.pages import definition_from_form
from sourcebook.web import create_app
from sourcebook import federated


def test_live_sources_merge_with_saved_businesses_and_cache_success_only(tmp_path,monkeypatch):
    monkeypatch.setenv("SOURCEBOOK_OFFLINE","1")
    db=DB(tmp_path / "live.db")
    definition,_=definition_from_form({"what":"web developer","country":"GH"})
    slug=db.create_dataset(definition["slug"],definition["title"],"","GH",definition,None,"discovered")
    eid=db.upsert_entity(slug,"Saved Agency",website="https://agency.example.test")
    calls=[]
    async def lookup(source,d):
        calls.append(source)
        if source=="wikidata": raise TimeoutError()
        return [{"name":"Agency Ltd", "website":"https://agency.example.test/", "contacts":{"phone":"+233200000000"},
                 "source_type":"osm","source_url":"https://www.openstreetmap.org/node/123","kind":"office=it"}]
    monkeypatch.setattr(federated,"fetch_source",lookup)
    with TestClient(create_app(db,run_jobs=False)) as c:
        page=c.get("/search?q=web+developer+in+Ghana").text
        csrf=re.search(r'name="csrf" value="([^"]+)"',page)
        # The public search page carries its session-bound token for live lookup requests.
        token=re.search(r'data-csrf="([^"]+)"',page).group(1)
        url=f"/search/source/{slug}/osm"
        assert c.post(url,data={}).status_code==400
        result=c.post(url,data={"csrf":token}).json()
        assert result["status"]=="done" and result["count"]==1
        assert c.post(url,data={"csrf":token}).json()["status"]=="recent"
        assert calls==["osm"]
        assert db.entity_count()==1
        fact=next(f for f in db.facts(eid) if f["field"]=="phone")
        assert fact["source_url"]=="https://www.openstreetmap.org/node/123"
        assert c.get(f"/search/live/{slug}").json()["count"]==1
        for _ in range(2):
            assert c.post(f"/search/source/{slug}/wikidata",data={"csrf":token}).json()["status"]=="unavailable"
        assert calls==["osm","wikidata","wikidata"]
        assert db.entity_count()==1
        assert c.post(f"/search/source/{slug}/google",data={"csrf":token}).status_code==404
