import re
from starlette.testclient import TestClient
from sourcebook.db import DB
from sourcebook.web import create_app
from sourcebook.catalog import ingest


def test_real_seed_idempotent_and_search_country_filter(tmp_path):
    db = DB(tmp_path / "seed.db")
    row = {"registry_id":"TESTLEI0000000000001","name":"Fixture Registry Company", "city":"Accra", "source_url":"https://search.gleif.org/#/record/TESTLEI0000000000001"}
    assert ingest(db,"GH",[row]) == 1
    assert ingest(db,"GH",[row]) == 0
    assert db.entity_count() == 1
    assert db.search("Fixture",country="GH")
    assert db.search("Fixture",country="NG") == []
    entity = db.entities("registered-businesses-gh")[0]
    assert not entity["demo"] and entity["sources"] == [row["source_url"]]
    assert not entity["website"]


def test_community_auth_csrf_and_one_vote_per_account(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCEBOOK_OFFLINE", "1")
    db=DB(tmp_path / "reviews.db")
    db.create_dataset("businesses", "Businesses", "", "GH", {}, None, "published")
    eid=db.upsert_entity("businesses", "Fixture Business")
    db.reindex("businesses")
    with TestClient(create_app(db,run_jobs=False)) as c:
        url="/d/businesses/fixture-business"
        assert c.post(url+"/community/vote",data={"value":"1"},follow_redirects=False).status_code==303
        page=c.get("/signup").text
        csrf=re.search(r'name="csrf" value="([^"]+)"',page).group(1)
        r=c.post("/signup",data={"csrf":csrf,"name":"Test member","email":"member@example.test","password":"long test password"},follow_redirects=False)
        assert r.status_code==303
        page=c.get(url).text
        csrf=re.search(r'name="csrf" value="([^"]+)"',page).group(1)
        assert c.post(url+"/community/vote",data={"value":"1"}).status_code==400
        for value in (1,1,-1):
            assert c.post(url+"/community/vote",data={"value":str(value),"csrf":csrf},follow_redirects=False).status_code==303
        assert db.feedback(eid)["votes"]==1 and db.feedback(eid)["score"]==-1
        for body in ("A useful service based on my visit.","Updated opinion with <script>unsafe()</script>"):
            assert c.post(url+"/community/review",data={"rating":"4","body":body,"csrf":csrf},follow_redirects=False).status_code==303
        feedback=db.feedback(eid)
        assert feedback["count"]==1 and feedback["rating"]==4
        page=c.get(url).text
        assert "&lt;script&gt;unsafe()&lt;/script&gt;" in page
        assert c.post(url+"/community/review",data={"rating":"9","body":"Valid length review","csrf":csrf}).status_code==400
