import base64
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from sourcebook import sso
from sourcebook.db import DB
from sourcebook.web import create_app


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid.apps")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setenv("SOURCEBOOK_OFFLINE", "1")
    monkeypatch.setenv("SOURCEBOOK_CRAWL_INTERVAL", "0")
    seen = {}

    def handler(req: httpx.Request):
        if req.url.host == "oauth2.googleapis.com":
            form = parse_qs(req.content.decode())
            seen["google"] = form
            claims = {"iss": "https://accounts.google.com", "aud": "gid.apps", "sub": "g-123", "email": "Ama@Example.org",
                      "email_verified": True, "name": "Ama Mensah", "exp": time.time() + 300, "nonce": seen["nonce"]}
            return httpx.Response(200, json={"id_token": f"h.{b64(claims)}.s"})
        if req.url.path.endswith("/functions/v1/accounts/token"):
            body = json.loads(req.content)
            seen["kenuxa"] = body
            ok = body["code"] == "good-code" and body["clientId"] == "sourcebook"
            return httpx.Response(200 if ok else 400, json={"tokenHash": "th"} if ok else {"message": "bad code"})
        if req.url.path.endswith("/auth/v1/verify"):
            return httpx.Response(200, json={"access_token": "at", "user": {"id": "k-9", "email": "kofi@example.org", "email_confirmed_at": "2026-01-01",
                                                                             "user_metadata": {"full_name": "Kofi Owusu"}}})
        if req.url.path.endswith("/auth/v1/logout"):
            seen["logout"] = True
            return httpx.Response(204)
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(sso.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    return seen


def start(c, provider):
    r = c.get(f"/auth/{provider}?next=/app/lists", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q


def test_buttons_and_kenuxa_sign_in(tmp_path, providers):
    db = DB(tmp_path / "a.db")
    with TestClient(create_app(db, run_jobs=False)) as c:
        page = c.get("/signin").text
        assert "Continue with Kenuxa Account" in page and "Continue with Google" in page
        q = start(c, "kenuxa")
        assert q["client_id"] == ["sourcebook"] and q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].endswith("/auth/callback")
        # wrong state is refused
        assert c.get("/auth/callback?code=good-code&state=nope").status_code == 400
        q = start(c, "kenuxa")
        r = c.get(f"/auth/callback?code=good-code&state={q['state'][0]}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/app/lists")
        assert providers["kenuxa"]["codeVerifier"] and providers.get("logout")
        u = db.user(email="kofi@example.org")
        assert u and u["name"] == "Kofi Owusu" and u["role"] == "admin" and u["password"] == "!sso"
        assert c.get("/app").status_code == 200
        # the same state can't be used twice
        assert c.get(f"/auth/callback?code=good-code&state={q['state'][0]}").status_code == 400
        acc = c.get("/app/account").text
        assert "your Kenuxa Account" in acc and "Add a password" in acc


def test_google_links_to_existing_email_account(tmp_path, providers, monkeypatch):
    db = DB(tmp_path / "b.db")
    from sourcebook import auth
    db.create_user("someone@first.test", "First", auth.hash_password("correct horse battery"), "admin")
    db.create_user("ama@example.org", "Ama", auth.hash_password("correct horse battery"), "member")
    real_start = sso.start

    def capture(provider, redirect_uri, session, next_url):
        url = real_start(provider, redirect_uri, session, next_url)
        providers["nonce"] = session["sso"]["nonce"]
        return url
    monkeypatch.setattr(sso, "start", capture)
    with TestClient(create_app(db, run_jobs=False)) as c:
        q = start(c, "google")
        assert q["scope"] == ["openid email profile"] and q["nonce"]
        r = c.get(f"/auth/callback?code=abc&state={q['state'][0]}", follow_redirects=False)
        assert r.status_code == 303
        assert providers["google"]["code_verifier"] and providers["google"]["client_secret"] == ["gsecret"]
        assert db.identity_user("google", "g-123")["email"] == "ama@example.org"     # joined the existing account
        assert len(db.users()) == 2


def test_google_hidden_without_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setenv("SOURCEBOOK_OFFLINE", "1")
    with TestClient(create_app(DB(tmp_path / "c.db"), run_jobs=False)) as c:
        page = c.get("/signin").text
        assert "Continue with Google" not in page and "Continue with Kenuxa Account" in page
        assert c.get("/auth/google", follow_redirects=False).status_code == 400
        assert "Kenuxa apps" in c.get("/").text and "/static/heimdall.svg" in c.get("/").text
