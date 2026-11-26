"""Accounts: scrypt password hashes, a signed session cookie, CSRF tokens and simple rate limits."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Any

from starlette.requests import Request

from .db import DB

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
ROLES = ("member", "admin")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _algo, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "Use at least 10 characters for your password."
    if len(password) > 200:
        return "That password is too long."
    return None


def session_secret(db: DB) -> str:
    env = os.environ.get("SOURCEBOOK_SECRET")
    if env:
        return env
    stored = db.meta("session_secret")
    if not stored:
        stored = secrets.token_urlsafe(48)
        db.set_meta("session_secret", stored)
    return stored


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("SOURCEBOOK_ADMIN_EMAILS", "").split(",") if e.strip()}


def role_for_new_user(db: DB, email: str) -> str:
    """Emails in SOURCEBOOK_ADMIN_EMAILS become admins. If that isn't set, the very first account does."""
    listed = admin_emails()
    if listed:
        return "admin" if email.lower() in listed else "member"
    return "admin" if db.user_count() == 0 else "member"


def _pw_tag(user: dict[str, Any]) -> str:
    """Changes whenever the password does, so old sessions end; reveals nothing about the hash."""
    return hashlib.sha256(("sourcebook-session:" + user["password"]).encode()).hexdigest()[:16]


def current_user(request: Request) -> dict[str, Any] | None:
    cached = request.scope.get("sb_user", False)
    if cached is not False:
        return cached
    uid = request.session.get("uid")
    user = request.app.state.db.user(uid) if uid else None
    if user and (user["disabled"] or request.session.get("pw") != _pw_tag(user)):
        request.session.clear()          # disabled, or the password changed since this session began
        user = None
    request.scope["sb_user"] = user
    return user


def sign_in(request: Request, user: dict[str, Any]) -> None:
    request.session.clear()
    request.session.update({"uid": user["id"], "pw": _pw_tag(user), "csrf": secrets.token_urlsafe(24)})
    request.app.state.db.update_user(user["id"], last_login=time.time())


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def csrf_ok(request: Request, form: Any) -> bool:
    sent = str(form.get("csrf", "")) or request.headers.get("x-csrf-token", "")
    return bool(sent) and hmac.compare_digest(sent, request.session.get("csrf", ""))


class RateLimit:
    """At most `count` events per `window` seconds per key (kept in memory)."""

    def __init__(self, count: int, window: float) -> None:
        self.count, self.window = count, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        q = self.hits[key]
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.count:
            return False
        q.append(now)
        return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
