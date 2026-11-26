"""Sign in with Google or with a Kenuxa Account.

Google: OpenID Connect authorization-code flow with PKCE. The ID token comes straight from Google's token endpoint over
TLS in exchange for our client secret, so (as OpenID Connect allows) its claims are checked rather than its signature:
issuer, audience, expiry, nonce and a verified email.

Kenuxa Account: the same "Sign in with Kenuxa" flow Bask and Heimdall use. Kenuxa Accounts sends the person back with a
one-time code; we swap it, with our PKCE verifier, at the shared `accounts` edge function for a single-use token, redeem that
with Supabase Auth to learn the confirmed account, and sign that Supabase session out at once. Sourcebook keeps no Kenuxa
session or refresh token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}

# Public values shared by every Kenuxa app (the same ones Kenuxa Accounts' .env.example lists). Override with env vars.
KENUXA_DEFAULTS = {
    "KENUXA_ACCOUNTS_URL": "https://kenuxa-accounts.vercel.app",
    "KENUXA_SUPABASE_URL": "https://mshhfieupbayynxpowit.supabase.co",
    "KENUXA_SUPABASE_ANON_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1zaGhmaWV1cGJheXlueHBvd2l0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTEzNzg0NjYsImV4cCI6MjEwNjk1NDQ2Nn0.F5x5I4bJGu6s14dYPf_rW4kwGhYuPkWOj40LtzwAFgk",
    "KENUXA_CLIENT_ID": "sourcebook",
}


class SsoError(Exception):
    pass


def setting(name: str) -> str:
    return os.environ.get(name) or KENUXA_DEFAULTS.get(name, "")


def providers() -> dict[str, bool]:
    return {"google": bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")),
            "kenuxa": os.environ.get("KENUXA_SIGNIN", "1") != "0" and bool(setting("KENUXA_ACCOUNTS_URL") and setting("KENUXA_SUPABASE_URL"))}


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def start(provider: str, redirect_uri: str, session: dict[str, Any], next_url: str) -> str:
    """Returns the address to send the browser to, and remembers what the callback must check."""
    if not providers().get(provider):
        raise SsoError("That sign-in option isn't set up on this Sourcebook.")
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    session["sso"] = {"provider": provider, "state": state, "nonce": nonce, "verifier": verifier, "next": next_url, "at": time.time()}
    if provider == "google":
        return GOOGLE_AUTH + "?" + urlencode({
            "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": "openid email profile", "state": state, "nonce": nonce, "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256", "prompt": "select_account"})
    return setting("KENUXA_ACCOUNTS_URL").rstrip("/") + "/authorize?" + urlencode({
        "client_id": setting("KENUXA_CLIENT_ID"), "redirect_uri": redirect_uri, "state": state,
        "code_challenge": _challenge(verifier), "code_challenge_method": "S256"})


def _claims(id_token: str) -> dict[str, Any]:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise SsoError("Google sent an unreadable sign-in.") from exc


async def finish(params: dict[str, str], redirect_uri: str, session: dict[str, Any],
                 client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Checks the callback and returns {provider, subject, email, name, next}. Raises SsoError with a readable message."""
    st = session.pop("sso", None)
    if not st or not params.get("state") or not secrets.compare_digest(params["state"], st.get("state", "")):
        raise SsoError("This sign-in expired or was already used. Please start again.")
    if time.time() - st.get("at", 0) > 600:
        raise SsoError("This sign-in took too long. Please start again.")
    if params.get("error"):
        raise SsoError("Sign-in was cancelled.")
    code = params.get("code")
    if not code:
        raise SsoError("The sign-in provider didn't send a code. Please start again.")
    own = client or httpx.AsyncClient(timeout=10)
    try:
        if st["provider"] == "google":
            r = await own.post(GOOGLE_TOKEN, data={
                "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": st["verifier"]})
            if r.status_code != 200 or "id_token" not in r.json():
                raise SsoError("Google refused the sign-in. Please try again.")
            c = _claims(r.json()["id_token"])
            aud = c.get("aud")
            if c.get("iss") not in GOOGLE_ISSUERS or (os.environ["GOOGLE_CLIENT_ID"] not in (aud if isinstance(aud, list) else [aud])) \
                    or float(c.get("exp", 0)) < time.time() - 60 or c.get("nonce") != st["nonce"]:
                raise SsoError("Google's answer didn't check out. Please try again.")
            if c.get("email_verified") not in (True, "true"):
                raise SsoError("Verify your email address with Google first.")
            return {"provider": "google", "subject": str(c["sub"]), "email": str(c.get("email", "")).lower(),
                    "name": str(c.get("name") or c.get("email") or ""), "next": st.get("next", "/app")}
        base = setting("KENUXA_SUPABASE_URL").rstrip("/")
        anon = setting("KENUXA_SUPABASE_ANON_KEY")
        headers = {"content-type": "application/json", "apikey": anon, "authorization": f"Bearer {anon}"}
        tr = await own.post(f"{base}/functions/v1/accounts/token", headers=headers, json={
            "clientId": setting("KENUXA_CLIENT_ID"), "redirectUri": redirect_uri, "code": code, "codeVerifier": st["verifier"]})
        tj = tr.json() if tr.headers.get("content-type", "").startswith("application/json") else {}
        if tr.status_code != 200 or not tj.get("tokenHash"):
            raise SsoError(tj.get("message") or "Kenuxa Accounts refused the sign-in code. Please try again.")
        vr = await own.post(f"{base}/auth/v1/verify", headers=headers, json={"type": "magiclink", "token_hash": tj["tokenHash"]})
        vj = vr.json() if vr.headers.get("content-type", "").startswith("application/json") else {}
        u = vj.get("user") or {}
        if vr.status_code != 200 or not u.get("id") or not u.get("email"):
            raise SsoError("Kenuxa Accounts couldn't confirm who you are. Please try again.")
        if vj.get("access_token"):
            try:
                await own.post(f"{base}/auth/v1/logout?scope=local", headers={**headers, "authorization": f"Bearer {vj['access_token']}"})
            except httpx.HTTPError:
                pass
        if not (u.get("email_confirmed_at") or u.get("confirmed_at")):
            raise SsoError("Confirm your email address with Kenuxa Accounts first.")
        meta = u.get("user_metadata") or {}
        return {"provider": "kenuxa", "subject": str(u["id"]), "email": str(u["email"]).lower(),
                "name": str(meta.get("full_name") or meta.get("name") or meta.get("username") or u["email"]), "next": st.get("next", "/app")}
    except httpx.HTTPError as exc:
        raise SsoError("The sign-in provider couldn't be reached. Please try again.") from exc
    finally:
        if client is None:
            await own.aclose()


# ------------------------------------------------------------------ Kenuxa app menu
_apps_cache: dict[str, Any] = {"at": 0.0, "apps": []}
FALLBACK_APPS = [
    {"client_id": "accounts", "name": "Kenuxa Accounts", "home_url": "https://kenuxa-accounts.vercel.app"},
    {"client_id": "bask", "name": "Kenuxa Bask", "home_url": "https://kenuxabask.vercel.app"},
    {"client_id": "heimdall", "name": "Kenuxa Heimdall", "home_url": "https://kenuxa-heimdall.vercel.app"},
    {"client_id": "sourcebook", "name": "Kenuxa Sourcebook", "home_url": "/"},
]


def kenuxa_apps() -> list[dict[str, Any]]:
    """Every Kenuxa app, from the same kenuxa_apps table the Accounts and Bask app menus read (cached 10 minutes)."""
    if time.time() - _apps_cache["at"] < 600 and _apps_cache["apps"]:
        return _apps_cache["apps"]
    apps = FALLBACK_APPS
    base, anon = setting("KENUXA_SUPABASE_URL").rstrip("/"), setting("KENUXA_SUPABASE_ANON_KEY")
    if base and anon and os.environ.get("SOURCEBOOK_OFFLINE") != "1":
        try:
            r = httpx.get(f"{base}/rest/v1/kenuxa_apps", params={"select": "client_id,name,home_url,icon_url,sort", "active": "eq.true", "order": "sort"},
                          headers={"apikey": anon, "authorization": f"Bearer {anon}"}, timeout=4)
            if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                apps = r.json()
        except (httpx.HTTPError, ValueError):
            pass
    out = []
    for a in apps:
        short = a["name"].replace("Kenuxa ", "") if a["client_id"] != "accounts" else "Account"
        out.append({**a, "short": short, "here": a["client_id"] == "sourcebook"})
    _apps_cache.update(at=time.time(), apps=out)
    return out
