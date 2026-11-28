"""Sourcebook web application.

Public site  /, /search, /d/<page>, /d/<page>/<business>, CSV/JSON export, corrections — server-rendered so it can be indexed.
Workspace    /app/... — signed-in members build data pages, keep shortlists and review corrections; admins approve pages.
"""

from __future__ import annotations

import contextlib
import csv
import time
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import auth
from .db import DB, SOURCE_LABEL
from .jobs import Runner
from . import discover, gaps as gapmod, leads as leadmod, sso
from .pages import country_name, countries, definition_from_form, form_from_definition, merge_definition

HERE = Path(__file__).parent
LICENCE = ("Includes data from OpenStreetMap © OpenStreetMap contributors, available under the Open Database Licence (ODbL) — "
           "https://www.openstreetmap.org/copyright. Other facts come from the businesses' own public websites, linked per fact. "
           "If you publish a database built from this export, keep this notice and share it under the ODbL.")
CONTACT_LABEL = {"phone": "Phone", "email": "Email", "whatsapp": "WhatsApp", "telegram": "Telegram", "website": "Website",
                 "facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "x": "X"}
FIELD_LABEL = {**CONTACT_LABEL, "name": "Name", "city": "Town or city", "address": "Address", "product": "Product",
               "registry_id": "Legal Entity Identifier"}
STATUS_LABEL = {"draft": "Draft", "building": "Building", "review": "Waiting for review", "published": "Public", "rejected": "Not approved",
                "discovered": "Found by search"}
JOB_LABEL = {"queued": "Waiting to start", "running": "Running", "done": "Finished", "failed": "Failed", "cancelled": "Stopped"}
DAILY_BUILDS = int(os.environ.get("SOURCEBOOK_DAILY_BUILDS", "5"))
MAX_PAGES_PER_MEMBER = int(os.environ.get("SOURCEBOOK_MAX_PAGES", "20"))

Handler = Callable[[Request], Awaitable[Response]]


def fmt_date(ts: float | None) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return f"{dt.day} {dt:%b %Y}"


def fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return f"{dt.day} {dt:%b %Y, %H:%M} UTC"


def contact_href(field: str, value: str) -> str:
    if field == "phone":
        return "tel:" + value.replace(" ", "")
    if field == "email":
        return "mailto:" + value
    if field == "whatsapp":
        return "https://wa.me/" + value.lstrip("+").replace(" ", "")
    if value.startswith(("http://", "https://")):
        return value
    return ""


def safe_next(url: str, default: str = "/app") -> str:
    p = urlparse(url or "")
    return url if url and not p.scheme and not p.netloc and url.startswith("/") and not url.startswith("//") else default


def site_url(request: Request) -> str:
    return os.environ.get("SOURCEBOOK_PUBLIC_URL") or str(request.base_url).rstrip("/")


def create_app(db: DB | None = None, run_jobs: bool | None = None) -> Starlette:
    seed_catalog = db is None
    db = db or DB()
    from .catalog import load_curated
    if seed_catalog:
        load_curated(db)
    # Hosts with no always-on process (Vercel): a search runs its own job inside a request (/search/run), within a time limit,
    # and the scheduled crawler (python -m sourcebook crawl, e.g. GitHub Actions) does everything else.
    inline = os.environ.get("SOURCEBOOK_INLINE_JOBS") == "1"
    run_seconds = float(os.environ.get("SOURCEBOOK_RUN_SECONDS", "240"))
    if run_jobs is None:
        run_jobs = not inline
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html", "xml"]))
    env.globals.update(fmt_date=fmt_date, fmt_time=fmt_time, contact_href=contact_href, CONTACT_LABEL=CONTACT_LABEL,
                       FIELD_LABEL=FIELD_LABEL, SOURCE_LABEL=SOURCE_LABEL, STATUS_LABEL=STATUS_LABEL, JOB_LABEL=JOB_LABEL,
                       LICENCE=LICENCE, year=datetime.now().year, COUNTRIES=countries(), GAPS=gapmod.GAPS, GAP_GROUPS=gapmod.grouped())
    env.filters["fromjson"] = json.loads
    env.globals["kenuxa_apps"] = sso.kenuxa_apps
    runner = Runner(db)
    signin_limit, signup_limit, correction_limit = auth.RateLimit(10, 600), auth.RateLimit(5, 3600), auth.RateLimit(20, 3600)
    discovery_limit = auth.RateLimit(int(os.environ.get("SOURCEBOOK_SEARCHES_PER_HOUR", "8")), 3600)
    default_country = os.environ.get("SOURCEBOOK_DEFAULT_COUNTRY", "GH").upper()

    # ------------------------------------------------------------------ helpers
    def render(name: str, request: Request, status: int = 200, **ctx: Any) -> HTMLResponse:
        user = auth.current_user(request)
        html = env.get_template(name).render(request=request, base=site_url(request), user=user, csrf=auth.csrf_token(request),
                                             path=request.url.path, **ctx)
        return HTMLResponse(html, status_code=status)

    def message(request: Request, status: int, title: str, text: str) -> HTMLResponse:
        return render("message.html", request, status, title=title, message=text)

    def not_found(request: Request) -> HTMLResponse:
        return message(request, 404, "Page not found", "That page doesn't exist, or it isn't public yet.")

    def back(url: str, flash: str = "", kind: str = "ok") -> RedirectResponse:
        if flash:
            url += ("&" if "?" in url else "?") + f"flash={quote(flash)}&fk={kind}"
        return RedirectResponse(url, status_code=303)

    def can_view(d: dict[str, Any] | None, user: dict[str, Any] | None) -> bool:
        return bool(d) and (d["status"] in ("published", "discovered") or can_manage(d, user))

    def can_manage(d: dict[str, Any] | None, user: dict[str, Any] | None) -> bool:
        return bool(d and user) and (user["role"] == "admin" or d.get("owner") == user["id"])

    def login_required(fn: Handler) -> Handler:
        async def wrapped(request: Request) -> Response:
            if not auth.current_user(request):
                return RedirectResponse(f"/signin?next={quote(request.url.path)}", status_code=303)
            if request.method == "POST":
                form = await request.form()
                if not auth.csrf_ok(request, form):
                    return message(request, 400, "Please try again", "This form expired. Go back, reload the page and send it again.")
            return await fn(request)
        wrapped.__name__ = fn.__name__
        return wrapped

    def admin_required(fn: Handler) -> Handler:
        async def inner(request: Request) -> Response:
            if auth.current_user(request)["role"] != "admin":
                return message(request, 403, "Admins only", "Only Sourcebook admins can open this page.")
            return await fn(request)
        inner.__name__ = fn.__name__
        return login_required(inner)

    def visible_rows(slug: str, request: Request) -> tuple[list[dict[str, Any]], dict[str, str]]:
        rows = db.entities(slug)
        f = {k: request.query_params.get(k, "").strip() for k in ("city", "product", "channel", "q", "lacks")}
        if f["lacks"]:
            rows = [r for r in rows if f["lacks"] in r["gap_codes"]]
        if f["city"]:
            rows = [r for r in rows if (r["city"] or "").lower() == f["city"].lower()]
        if f["product"]:
            rows = [r for r in rows if f["product"] in r["products"]]
        if f["channel"]:
            rows = [r for r in rows if r["contacts"].get(f["channel"])]
        if f["q"]:
            q = f["q"].lower()
            rows = [r for r in rows if q in (r["name"] + " " + (r["description"] or "") + " " + " ".join(r["products"])).lower()]
        return rows, f

    def record(r: dict[str, Any], base: str, slug: str) -> dict[str, Any]:
        out: dict[str, Any] = {"name": r["name"], "city": r["city"], "region": r["region"], "website": r["website"],
                               "products": r["products"], "description": r["description"],
                               "profile_url": f"{base}/d/{slug}/{r['slug']}", "last_checked": fmt_date(r["checked_at"]),
                               "sources": r["sources"], "example_data": bool(r["demo"])}
        for field in ("phone", "email", "whatsapp", "telegram"):
            out[field] = [c["value"] for c in r["contacts"].get(field, [])]
        out["missing"] = [g["label"] for g in r["gaps"]]
        out["who_could_help"] = sorted({g["helper"] for g in r["gaps"]})
        out["stated_needs"] = [g["detail"] for g in r["gaps"] if g["category"] == "Stated needs"]
        out["missing_details"] = [{"missing": g["label"], "detail": g["detail"], "checked": fmt_date(g["observed_at"]), "page_checked": g["source_url"]}
                                  for g in r["gaps"]]
        if "note" in r:
            out["note"] = r["note"]
        return out

    def csv_response(records: list[dict[str, Any]], filename: str, extra_cols: tuple[str, ...] = ()) -> Response:
        buf = io.StringIO()
        cols = ["name", "city", "region", "website", "phone", "email", "whatsapp", "telegram", "products", "description",
                "stated_needs", "missing", "who_could_help", "profile_url", "sources", "last_checked", "example_data", *extra_cols]
        w = csv.writer(buf)
        w.writerow(cols)
        for r in records:
            w.writerow(["; ".join(r.get(c) or []) if isinstance(r.get(c), list) else r.get(c, "") for c in cols])
        w.writerow([])
        w.writerow([LICENCE])
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    def queue_build(slug: str, user: dict[str, Any], kind: str = "build") -> str | None:
        """Queue a build if limits allow; returns a problem message otherwise."""
        if db.jobs(dataset=slug, status=("queued", "running")):
            return "This page is already building."
        if user["role"] != "admin":
            if db.jobs(user_id=user["id"], status=("queued", "running")):
                return "You already have a build running. Wait for it to finish, then try again."
            if db.jobs_today(user["id"]) >= DAILY_BUILDS:
                return f"You can start {DAILY_BUILDS} builds a day. Try again tomorrow."
        db.add_job(slug, user["id"], kind)
        if db.dataset(slug)["status"] in ("draft", "rejected", "review"):
            db.set_status(slug, "building")
        return None

    # ------------------------------------------------------------------ public pages
    async def home(request: Request) -> Response:
        recent = sorted(db.datasets(status="discovered"), key=lambda d: d["built_at"] or 0, reverse=True)[:8]
        return render("home.html", request, datasets=db.datasets(), recent=[d for d in recent if d["n"]], total=db.entity_count(),
                      today=db.new_entities_since(discover.today_start()))

    async def search(request: Request) -> Response:
        q = request.query_params.get("q", "").strip()[:200]
        chosen = request.query_params.get("in", "").upper() or default_country
        if chosen not in dict(countries()):
            chosen = default_country
        lens = request.query_params.get("lens", "mixed")
        if lens not in leadmod.LENSES:
            lens = "mixed"
        user = auth.current_user(request)
        visible = [d for d in db.datasets(status=None) if d["status"] in ("published", "discovered")]
        hits = db.search(q) if q else []
        names = {d["slug"]: d["title"] for d in visible}
        live = None
        if q:
            p = await discover.resolve(db, q, chosen)
            hits = db.search(p["what"], country=p["country"] if p["named_country"] or "in" in request.query_params else None)
            if p["slug"] and not discover.needs_run(db, p["slug"]):
                db.log_search(q, p["what"], p["country"], p["slug"], False, user["id"] if user else None)
                live = {"slug": p["slug"], "what": p["what"], "country": p["country"], "place": p["place_info"], "started": False,
                        "running": bool(db.jobs(dataset=p["slug"], status=("queued", "running")))}
            elif discovery_limit.allow(auth.client_ip(request)) or (user or {}).get("role") == "admin":
                live = await discover.start_discovery(db, q, chosen, user["id"] if user else None, parsed=p)
            else:
                live = {"started": False, "reason": "limit", "what": p["what"], "country": p["country"]}
            if live.get("slug"):
                hits = [h for h in hits if h["dataset"] != live["slug"]]
                live["title"] = (db.dataset(live["slug"]) or {}).get("title", "")
        monitored = bool(user and live and live.get("slug") and any(m["dataset"] == live["slug"] for m in db.monitors(user["id"])))
        return render("search.html", request, q=q, hits=hits, names=names, live=live, chosen=chosen, lens=lens, LENSES=leadmod.LENSES,
                      country_name=country_name, sources=source_list(), monitored=monitored)

    def source_list() -> list[str]:
        from .collect import websearch
        return ["OpenStreetMap", "Wikidata", "the GLEIF company register"] + (["the web"] if websearch.available() else []) + ["business websites"]

    def ranked(slug: str, lens: str) -> list[dict[str, Any]]:
        return leadmod.rank(db.entities(slug), lens)

    async def search_live(request: Request) -> Response:
        slug = request.path_params["slug"]
        d = db.dataset(slug)
        if not d or d["status"] not in ("published", "discovered"):
            return JSONResponse({"error": "not found"}, status_code=404)
        lens = request.query_params.get("lens", "mixed")
        lens = lens if lens in leadmod.LENSES else "mixed"
        active = db.jobs(dataset=slug, status=("queued", "running"), limit=1)
        last = active[0] if active else (db.jobs(dataset=slug, limit=1) or [None])[0]
        progress = ""
        if last:
            lines = [ln for ln in last["log"].splitlines() if ln.strip() and not ln.startswith(("Traceback", "  File", "    "))]
            found = [ln for ln in lines if ln.endswith(" found") or ln.startswith(("Checked", "Done:", "Kept businesses")) or "after merging" in ln]
            problems = [ln for ln in lines if "could not be reached" in ln or "timed out" in ln or "stopped with an error" in ln]
            progress = (problems[-1] if problems and last["status"] in ("done", "failed") else None) or (found[-1] if found else ("Waiting for a free worker…" if last["status"] == "queued" else "Starting…"))
        rows = ranked(slug, lens)
        tabs = [{"key": k, "label": lbl, "count": sum(1 for r in rows if test(r))} for k, lbl, test in leadmod.TABS]
        run = f"/search/run/{slug}" if inline and active and active[0]["status"] == "queued" else None
        return JSONResponse({
            "run": run,
            "status": "running" if active else (last["status"] if last else "idle"),
            "queued": bool(active and active[0]["status"] == "queued"), "progress": progress, "count": len(rows),
            "checked": fmt_time(d["built_at"]), "url": f"/d/{slug}", "lens": lens, "lens_label": leadmod.LENSES[lens][0], "tabs": tabs,
            "businesses": [{
                "name": r["name"], "url": f"/d/{slug}/{r['slug']}", "city": r["city"] or "", "kind": ", ".join(r["products"][:2]),
                "lat": r["lat"], "lon": r["lon"], "priority": r["priority"], "opening": r["opening"],
                "reasons": [{"label": g["label"], "detail": g["detail"], "need": g["category"] == "Stated needs"} for g in r["reasons"][:4]],
                "tabs": [k for k, _l, test in leadmod.TABS if test(r)],
                "contacts": {c: r["contacts"][c][0]["value"] for c in ("phone", "whatsapp", "email") if r["contacts"].get(c)},
                "website": r["website"] or "", "demo": bool(r["demo"]),
            } for r in rows[:200]],
        })

    async def search_run(request: Request) -> Response:
        """Inline mode: the search page asks for its queued job to run here. Only an already-queued job for this page can run,
        one at a time, and it stops before the host's time limit."""
        if not inline:
            return JSONResponse({"ran": False}, status_code=404)
        job = db.claim_job(dataset=request.path_params["slug"])
        if not job:
            return JSONResponse({"ran": False})
        import time as _t
        await runner.run(job, deadline=_t.time() + run_seconds)
        return JSONResponse({"ran": True})

    async def search_export(request: Request) -> Response:
        slug = request.path_params["slug"]
        d = db.dataset(slug)
        if not d or d["status"] not in ("published", "discovered"):
            return not_found(request)
        lens = request.query_params.get("lens", "mixed")
        lens = lens if lens in leadmod.LENSES else "mixed"
        base = site_url(request)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["rank", "name", "city", "phone", "whatsapp", "email", "website", "priority", "lens", "why_reach_out", "opening_line",
                    "profile_url", "latitude", "longitude", "last_checked"])
        for i, r in enumerate(ranked(slug, lens), 1):
            w.writerow([i, r["name"], r["city"], *["; ".join(c["value"] for c in r["contacts"].get(k, [])) for k in ("phone", "whatsapp", "email")],
                        r["website"], r["priority"], leadmod.LENSES[lens][0], " | ".join(f"{g['label']}: {g['detail']}" for g in r["reasons"]),
                        r["opening"], f"{base}/d/{slug}/{r['slug']}", r["lat"] or "", r["lon"] or "", fmt_date(r["checked_at"])])
        w.writerow([])
        w.writerow([LICENCE])
        return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{slug}-{lens}-leads.csv"'})

    async def dataset(request: Request) -> Response:
        slug = request.path_params["slug"]
        for ext in (".csv", ".json"):
            if slug.endswith(ext):
                return await export(request, slug[: -len(ext)], ext[1:])
        d = db.dataset(slug)
        user = auth.current_user(request)
        if not can_view(d, user):
            return not_found(request)
        all_rows = db.entities(slug)
        rows, f = visible_rows(slug, request)
        cities = sorted({r["city"] for r in all_rows if r["city"]})
        products = sorted({p for r in all_rows for p in r["products"]})
        counts = {ch: sum(1 for r in all_rows if r["contacts"].get(ch)) for ch in ("phone", "email", "whatsapp", "telegram")}
        gap_counts = {code: sum(1 for r in all_rows if code in r["gap_codes"]) for code in gapmod.ORDER}
        query = "&".join(f"{k}={quote(v)}" for k, v in f.items() if v)
        return render("dataset.html", request, gap_counts=gap_counts, gap_counts_nonzero=[k for k, v in gap_counts.items() if v], d=d, rows=rows, total=len(all_rows), cities=cities, products=products, counts=counts,
                      f=f, query=query, definition=json.loads(d["definition"] or "{}"), demo=any(r["demo"] for r in all_rows),
                      manage=can_manage(d, user))

    async def entity(request: Request) -> Response:
        slug, eslug = request.path_params["slug"], request.path_params["entity"]
        d, e = db.dataset(slug), db.entity(slug, eslug)
        user = auth.current_user(request)
        if not can_view(d, user) or not e:
            return not_found(request)
        by_field: dict[str, list[dict[str, Any]]] = {}
        for fct in e["facts"]:
            by_field.setdefault(fct["field"], []).append(fct)
        lists = db.lists(user["id"]) if user else []
        saved = db.saved_in(user["id"], e["id"]) if user else []
        return render("entity.html", request, d=d, e=e, by_field=by_field, sent=request.query_params.get("sent"), lists=lists, saved=saved, feedback=db.feedback(e["id"], user["id"] if user else None))

    async def community(request: Request) -> Response:
        slug, eslug = request.path_params["slug"], request.path_params["entity"]
        d, e = db.dataset(slug), db.entity(slug, eslug)
        if not can_view(d, auth.current_user(request)) or not e or e["demo"]:
            return not_found(request)
        user = auth.current_user(request)
        form = await request.form()
        if not correction_limit.allow(str(user["id"])):
            return message(request, 429, "Please wait", "Try again later.")
        try:
            if request.path_params["action"] == "review":
                db.review_business(e["id"], user["id"], int(form.get("rating", "0")), str(form.get("body", "")))
            elif request.path_params["action"] == "vote":
                db.vote_business(e["id"], user["id"], int(form.get("value", "0")))
            else:
                return not_found(request)
        except ValueError as exc:
            return message(request, 400, "Check your feedback", str(exc))
        return RedirectResponse(f"/d/{slug}/{eslug}#community", status_code=303)

    async def correct(request: Request) -> Response:
        slug, eslug = request.path_params["slug"], request.path_params["entity"]
        d, e = db.dataset(slug), db.entity(slug, eslug)
        if not can_view(d, auth.current_user(request)) or not e:
            return not_found(request)
        form = await request.form()
        if form.get("website_url"):            # honeypot: people never fill this hidden field
            return RedirectResponse(f"/d/{slug}/{eslug}?sent=1", status_code=303)
        if not auth.csrf_ok(request, form):
            return message(request, 400, "Please try again", "This form expired. Go back, reload the page and send it again.")
        if not correction_limit.allow(auth.client_ip(request)):
            return message(request, 429, "Too many corrections", "You've sent a lot of corrections in the last hour. Please try again later.")
        kind = "remove" if form.get("kind") == "remove" else "change"
        field = str(form.get("field", ""))[:40] if kind == "change" else ""
        if kind == "change" and field not in FIELD_LABEL:
            field = "other"
        db.add_correction(entity=e["id"], dataset=slug, kind=kind, field=field, value=str(form.get("value", ""))[:500],
                          evidence_url=str(form.get("evidence_url", ""))[:500], message=str(form.get("message", ""))[:2000],
                          contact=str(form.get("contact", ""))[:200], is_owner=int(bool(form.get("is_owner"))))
        return RedirectResponse(f"/d/{slug}/{eslug}?sent=1#correct", status_code=303)

    async def export(request: Request, slug: str, fmt: str) -> Response:
        d = db.dataset(slug)
        if not can_view(d, auth.current_user(request)):
            return not_found(request)
        rows, _f = visible_rows(slug, request)
        base = site_url(request)
        records = [record(r, base, slug) for r in rows]
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if fmt == "json":
            body = {"dataset": d["title"], "description": d["summary"], "exported": stamp, "count": len(records),
                    "licence": LICENCE, "records": records}
            return Response(json.dumps(body, ensure_ascii=False, indent=2), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{slug}-{stamp}.json"'})
        return csv_response(records, f"{slug}-{stamp}.csv")

    async def opportunities(request: Request) -> Response:
        """Find businesses that lack what you build — and the contacts they publish so you can offer it."""
        user = auth.current_user(request)
        lacks = request.query_params.get("lacks", "")
        country = request.query_params.get("in", "").upper()
        kind = request.query_params.get("kind", "").strip()[:80]
        channel = request.query_params.get("channel", "")
        pages = [d for d in db.datasets(status=None) if d["status"] in ("published", "discovered") and (not country or d["country"] == country)]
        if kind:
            words = [w for w in kind.lower().split() if len(w) > 2]
            pages = [d for d in pages if any(w.rstrip("s") in d["title"].lower() for w in words)]
        results, total = [], 0
        if lacks in gapmod.GAPS:
            for d in pages:
                for e in db.entities(d["slug"]):
                    if lacks in e["gap_codes"] and (not channel or e["contacts"].get(channel)):
                        total += 1
                        if len(results) < 200:
                            results.append((d, e))
            results.sort(key=lambda de: (-len([c for c in ("whatsapp", "phone", "email") if de[1]["contacts"].get(c)]), de[1]["name"]))
        lists = db.lists(user["id"]) if user else []
        opp_counts = opportunity_counts() if not lacks else {}
        countries_with_pages = sorted({d["country"] for d in db.datasets(status=None) if d["status"] in ("published", "discovered")})
        query = "&".join(f"{k}={quote(v)}" for k, v in (("lacks", lacks), ("in", country), ("kind", kind), ("channel", channel)) if v)
        return render("opportunities.html", request, lacks=lacks, country=country, kind=kind, channel=channel, results=results, total=total,
                      lists=lists, country_name=country_name, page_countries=countries_with_pages, query=query, opp_counts=opp_counts)

    _opp_cache: dict[str, Any] = {"at": 0.0, "counts": {}}

    def opportunity_counts() -> dict[str, int]:
        import time as _t
        if _t.time() - _opp_cache["at"] > 600:
            with db.conn() as c:
                rows = c.execute("""SELECT substr(f.value, 1, instr(f.value, ':') - 1) AS code, COUNT(DISTINCT f.entity) FROM facts f
                                    JOIN entities e ON e.id=f.entity JOIN datasets d ON d.slug=e.dataset
                                    WHERE f.field='gap' AND e.hidden=0 AND d.status IN ('published','discovered') GROUP BY code""").fetchall()
            _opp_cache.update(at=_t.time(), counts={r[0]: r[1] for r in rows})
        return _opp_cache["counts"]

    async def opportunities_csv(request: Request) -> Response:
        lacks = request.query_params.get("lacks", "")
        if lacks not in gapmod.GAPS:
            return not_found(request)
        country = request.query_params.get("in", "").upper()
        base = site_url(request)
        records = []
        for d in db.datasets(status=None):
            if d["status"] in ("published", "discovered") and (not country or d["country"] == country):
                records += [record(e, base, d["slug"]) for e in db.entities(d["slug"]) if lacks in e["gap_codes"]]
        return csv_response(records, f"businesses-without-{lacks.replace('_', '-')}.csv")

    async def api_datasets(request: Request) -> Response:
        base = site_url(request)
        return JSONResponse([{"slug": d["slug"], "title": d["title"], "count": d["n"], "url": f"{base}/d/{d['slug']}",
                              "json": f"{base}/d/{d['slug']}.json", "csv": f"{base}/d/{d['slug']}.csv"} for d in db.datasets()])

    async def robots(request: Request) -> Response:
        return PlainTextResponse(f"User-agent: *\nDisallow: /app\nDisallow: /search\nDisallow: /signin\nDisallow: /signup\nSitemap: {site_url(request)}/sitemap.xml\n")

    async def sitemap(request: Request) -> Response:
        base = site_url(request)
        urls = [base + "/"]
        for d in db.datasets():
            urls.append(f"{base}/d/{d['slug']}")
            urls += [f"{base}/d/{d['slug']}/{e['slug']}" for e in db.entities(d["slug"])]
        xml = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{xml}</urlset>',
                        media_type="application/xml")

    # ------------------------------------------------------------------ accounts
    async def signin(request: Request) -> Response:
        nxt = safe_next(request.query_params.get("next", ""))
        if auth.current_user(request):
            return RedirectResponse(nxt, status_code=303)
        if request.method == "GET":
            return render("auth.html", request, mode="signin", next=nxt, values={}, providers=sso.providers())
        form = await request.form()
        email = str(form.get("email", "")).strip().lower()
        if not auth.csrf_ok(request, form):
            return render("auth.html", request, 400, mode="signin", next=nxt, values={"email": email}, error="This form expired. Please try again.", providers=sso.providers())
        if not signin_limit.allow(auth.client_ip(request)) or not signin_limit.allow("email:" + email):
            return render("auth.html", request, 429, mode="signin", next=nxt, values={"email": email},
                          error="Too many attempts. Wait 10 minutes, then try again.", providers=sso.providers())
        user = db.user(email=email)
        if not user or not auth.check_password(str(form.get("password", "")), user["password"]) or user["disabled"]:
            return render("auth.html", request, 400, mode="signin", next=nxt, values={"email": email},
                          error="That email and password don't match an account. If you signed up with Google or Kenuxa, use that button.", providers=sso.providers())
        auth.sign_in(request, user)
        return RedirectResponse(safe_next(str(form.get("next", "")), nxt), status_code=303)

    async def signup(request: Request) -> Response:
        nxt = safe_next(request.query_params.get("next", ""))
        if auth.current_user(request):
            return RedirectResponse(nxt, status_code=303)
        if request.method == "GET":
            return render("auth.html", request, mode="signup", next=nxt, values={}, providers=sso.providers())
        form = await request.form()
        values = {"email": str(form.get("email", "")).strip().lower(), "name": str(form.get("name", "")).strip()[:80]}
        password = str(form.get("password", ""))
        error = None
        if form.get("company"):                      # honeypot
            return RedirectResponse("/", status_code=303)
        if not auth.csrf_ok(request, form):
            error = "This form expired. Please try again."
        elif not signup_limit.allow(auth.client_ip(request)):
            error = "Too many new accounts from this network. Try again in an hour."
        elif not auth.EMAIL_RE.match(values["email"]):
            error = "Enter a valid email address."
        elif db.user(email=values["email"]):
            error = "There's already an account with that email. Sign in instead."
        elif not values["name"]:
            error = "Enter your name."
        else:
            error = auth.password_problem(password)
        if error:
            return render("auth.html", request, 400, mode="signup", next=nxt, values=values, error=error, providers=sso.providers())
        uid = db.create_user(values["email"], values["name"], auth.hash_password(password), auth.role_for_new_user(db, values["email"]))
        auth.sign_in(request, db.user(uid))
        return back(safe_next(str(form.get("next", "")), "/app"), "Welcome to Sourcebook.")

    async def sso_start(request: Request) -> Response:
        provider = request.path_params["provider"]
        nxt = safe_next(request.query_params.get("next", ""))
        try:
            url = sso.start(provider, f"{site_url(request)}/auth/callback", request.session, nxt)
        except sso.SsoError as exc:
            return render("auth.html", request, 400, mode="signin", next=nxt, values={}, error=str(exc), providers=sso.providers())
        return RedirectResponse(url, status_code=302)

    async def sso_callback(request: Request) -> Response:
        try:
            ident = await sso.finish(dict(request.query_params), f"{site_url(request)}/auth/callback", request.session)
        except sso.SsoError as exc:
            return render("auth.html", request, 400, mode="signin", next="/app", values={}, error=str(exc), providers=sso.providers())
        if not auth.EMAIL_RE.match(ident["email"]):
            return render("auth.html", request, 400, mode="signin", next="/app", values={}, error="Your sign-in provider didn't share an email address.", providers=sso.providers())
        user = db.identity_user(ident["provider"], ident["subject"])
        if not user:
            # A confirmed email that already has an account joins it; otherwise a new account without a password.
            user = db.user(email=ident["email"])
            if not user:
                if not signup_limit.allow(auth.client_ip(request)):
                    return render("auth.html", request, 429, mode="signin", next="/app", values={}, error="Too many new accounts from this network. Try again in an hour.", providers=sso.providers())
                uid = db.create_user(ident["email"], ident["name"][:80], "!sso", auth.role_for_new_user(db, ident["email"]))
                user = db.user(uid)
            db.link_identity(user["id"], ident["provider"], ident["subject"], ident["email"])
        if user["disabled"]:
            return render("auth.html", request, 403, mode="signin", next="/app", values={}, error="This account has been disabled.", providers=sso.providers())
        auth.sign_in(request, user)
        return back(safe_next(ident.get("next", "/app")), f"Signed in with {'Google' if ident['provider'] == 'google' else 'your Kenuxa Account'}.")

    async def signout(request: Request) -> Response:
        form = await request.form()
        if auth.csrf_ok(request, form):
            request.session.clear()
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------------ workspace
    async def app_home(request: Request) -> Response:
        user = auth.current_user(request)
        mine = db.datasets(status=None, owner=user["id"])
        lists = db.lists(user["id"])
        my_slugs = [d["slug"] for d in (db.datasets(status=None) if user["role"] == "admin" else mine)]
        open_corrections = len(db.corrections("open", None if user["role"] == "admin" else my_slugs))
        pending = len(db.datasets(status="review")) if user["role"] == "admin" else 0
        return render("app_home.html", request, mine=mine, lists=lists, jobs=db.jobs(user_id=user["id"], limit=6),
                      open_corrections=open_corrections, pending=pending, public=db.datasets())

    async def app_new(request: Request) -> Response:
        user = auth.current_user(request)
        if request.method == "GET":
            return render("app_new.html", request, values={"country": "GH"}, errors=[])
        form = dict(await request.form())
        if user["role"] != "admin" and len(db.datasets(status=None, owner=user["id"])) >= MAX_PAGES_PER_MEMBER:
            return render("app_new.html", request, 400, values=form, errors=[f"You can have up to {MAX_PAGES_PER_MEMBER} pages. Delete one to make another."])
        definition, errors = definition_from_form(form)
        if errors:
            return render("app_new.html", request, 400, values=form, errors=errors)
        slug = db.create_dataset(definition["slug"], definition["title"], definition["summary"], definition["country"], definition, user["id"])
        problem = queue_build(slug, user)
        return back(f"/app/pages/{slug}", problem or "Your page is building. You can leave this screen; it keeps going.", "warn" if problem else "ok")

    async def app_pages(request: Request) -> Response:
        user = auth.current_user(request)
        show_all = user["role"] == "admin" and request.query_params.get("all") == "1"
        pages = db.datasets(status=None) if show_all else db.datasets(status=None, owner=user["id"])
        owners = {u["id"]: u["name"] or u["email"] for u in db.users()} if show_all else {}
        return render("app_pages.html", request, pages=pages, show_all=show_all, owners=owners)

    async def app_page(request: Request) -> Response:
        user = auth.current_user(request)
        d = db.dataset(request.path_params["slug"])
        if not can_manage(d, user):
            return not_found(request)
        jobs = db.jobs(dataset=d["slug"], limit=10)
        owner = db.user(d["owner"]) if d.get("owner") else None
        n = len(db.entities(d["slug"]))
        return render("app_page.html", request, d=d, jobs=jobs, job=jobs[0] if jobs else None, owner=owner, n=n,
                      definition=json.loads(d["definition"] or "{}"), corrections=len(db.corrections("open", [d["slug"]])))

    async def app_page_edit(request: Request) -> Response:
        user = auth.current_user(request)
        d = db.dataset(request.path_params["slug"])
        if not can_manage(d, user):
            return not_found(request)
        if request.method == "GET":
            return render("app_new.html", request, values=form_from_definition(json.loads(d["definition"] or "{}")), errors=[], editing=d)
        form = dict(await request.form())
        definition, errors = definition_from_form(form)
        if errors:
            return render("app_new.html", request, 400, values=form, errors=errors, editing=d)
        merge_definition(definition, json.loads(d["definition"] or "{}"))
        db.update_definition(d["slug"], definition["title"], definition["summary"], definition["country"], definition)
        problem = queue_build(d["slug"], user) if form.get("rebuild") else None
        return back(f"/app/pages/{d['slug']}", problem or ("Saved. Rebuilding with the new settings." if form.get("rebuild") else "Saved."),
                    "warn" if problem else "ok")

    async def app_page_action(request: Request) -> Response:
        user = auth.current_user(request)
        d = db.dataset(request.path_params["slug"])
        action = request.path_params["action"]
        if not can_manage(d, user):
            return not_found(request)
        form = await request.form()
        url = f"/app/pages/{d['slug']}"
        if action == "build":
            problem = queue_build(d["slug"], user, "refresh" if d["status"] == "published" else "build")
            return back(url, problem or "Build started.", "warn" if problem else "ok")
        if action == "cancel":
            for j in db.jobs(dataset=d["slug"], status=("queued", "running")):
                db.cancel_job(j["id"])
            if d["status"] == "building" and not db.jobs(dataset=d["slug"], status=("running",)):
                db.set_status(d["slug"], "review" if db.entities(d["slug"]) else "draft")
            return back(url, "Stopping the build. Businesses already checked are kept.")
        if action == "delete":
            if str(form.get("confirm", "")).strip() != d["title"]:
                return back(url, "Type the page title exactly to delete it.", "warn")
            for j in db.jobs(dataset=d["slug"], status=("queued", "running")):
                db.cancel_job(j["id"])
            db.delete_dataset(d["slug"])
            return back("/app/pages", f"Deleted “{d['title']}”.")
        if action in ("publish", "reject", "unpublish") and user["role"] == "admin":
            note = str(form.get("note", ""))[:500]
            db.set_status(d["slug"], {"publish": "published", "reject": "rejected", "unpublish": "review"}[action], note)
            return back(url, {"publish": "Page is now public.", "reject": "Page marked as not approved.", "unpublish": "Page taken off the public site."}[action])
        if action == "submit" and d["status"] in ("draft", "rejected") and db.entities(d["slug"]):
            db.set_status(d["slug"], "review")
            return back(url, "Sent for review.")
        return back(url)

    async def app_job(request: Request) -> Response:
        user = auth.current_user(request)
        job = db.job(request.path_params["jid"])
        d = db.dataset(job["dataset"]) if job else None
        if not job or not can_manage(d, user):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"id": job["id"], "status": job["status"], "label": JOB_LABEL.get(job["status"], job["status"]),
                             "log": job["log"][-20000:], "stats": json.loads(job["stats"] or "{}"),
                             "page_status": d["status"], "page_status_label": STATUS_LABEL.get(d["status"], d["status"]),
                             "count": len(db.entities(d["slug"]))})

    # shortlists
    async def app_lists(request: Request) -> Response:
        user = auth.current_user(request)
        if request.method == "POST":
            form = await request.form()
            lid = db.create_list(user["id"], str(form.get("name", "")))
            return back(f"/app/lists/{lid}", "Shortlist created.")
        return render("app_lists.html", request, lists=db.lists(user["id"]))

    async def app_list(request: Request) -> Response:
        user = auth.current_user(request)
        lid = request.path_params["lid"]
        lst = db.list_(lid, user["id"])
        if not lst:
            return not_found(request)
        items = db.list_items(lid)
        names = {d["slug"]: d["title"] for d in db.datasets(status=None)}
        return render("app_list.html", request, lst=lst, items=items, names=names)

    async def app_list_csv(request: Request) -> Response:
        user = auth.current_user(request)
        lst = db.list_(request.path_params["lid"], user["id"])
        if not lst:
            return not_found(request)
        base = site_url(request)
        records = [record(r, base, r["dataset"]) for r in db.list_items(lst["id"])]
        return csv_response(records, f"shortlist-{lst['id']}.csv", ("note",))

    async def app_list_action(request: Request) -> Response:
        user = auth.current_user(request)
        lid, action = request.path_params["lid"], request.path_params["action"]
        lst = db.list_(lid, user["id"])
        if not lst:
            return not_found(request)
        form = await request.form()
        if action == "rename":
            db.rename_list(lid, user["id"], str(form.get("name", "")))
            return back(f"/app/lists/{lid}", "Renamed.")
        if action == "delete":
            db.delete_list(lid, user["id"])
            return back("/app/lists", "Shortlist deleted.")
        if action == "remove":
            db.remove_from_list(lid, str(form.get("entity", "")))
            return back(f"/app/lists/{lid}", "Removed from the shortlist.")
        if action == "note":
            db.add_to_list(lid, str(form.get("entity", "")), str(form.get("note", "")))
            return back(f"/app/lists/{lid}", "Note saved.")
        return back(f"/app/lists/{lid}")

    async def app_save(request: Request) -> Response:
        user = auth.current_user(request)
        form = await request.form()
        entity_id = str(form.get("entity", ""))
        ds, _, es = entity_id.partition(":")
        e, d = db.entity(ds, es), db.dataset(ds)
        nxt = safe_next(str(form.get("next", "")), f"/d/{ds}/{es}")
        if not e or not can_view(d, user):
            return not_found(request)
        lid = form.get("list")
        if lid == "new" or not lid:
            lid = db.create_list(user["id"], str(form.get("new_name", "")) or "My shortlist")
        elif not db.list_(int(lid), user["id"]):
            return not_found(request)
        db.add_to_list(int(lid), e["id"])
        return back(nxt, f"Saved {e['name']} to your shortlist.")

    # corrections review
    def correction_scope(user: dict[str, Any]) -> list[str] | None:
        return None if user["role"] == "admin" else [d["slug"] for d in db.datasets(status=None, owner=user["id"])]

    async def app_corrections(request: Request) -> Response:
        user = auth.current_user(request)
        items = db.corrections("open", correction_scope(user))
        names = {}
        for c in items:
            if c["entity"]:
                ds, es = c["entity"].split(":", 1)
                ent = db.entity(ds, es)
                names[c["id"]] = (ent["name"], f"/d/{ds}/{es}") if ent else ("(removed)", "")
        return render("app_corrections.html", request, items=items, names=names)

    async def app_correction_review(request: Request) -> Response:
        user = auth.current_user(request)
        c = db.correction(request.path_params["cid"])
        scope = correction_scope(user)
        if not c or (scope is not None and c["dataset"] not in scope):
            return not_found(request)
        form = await request.form()
        approve = form.get("action") == "approve"
        db.review(c["id"], approve)
        db.reindex(c["dataset"])
        return back("/app/corrections", "Correction applied." if approve and c["kind"] != "remove" else
                    "Listing removed." if approve else "Correction rejected.")

    # admin
    async def app_admin(request: Request) -> Response:
        users = db.users()
        return render("app_admin.html", request, review=db.datasets(status="review"), users=users,
                      running=db.jobs(status=("queued", "running"), limit=50), owners={u["id"]: u for u in users})

    async def app_admin_user(request: Request) -> Response:
        me = auth.current_user(request)
        uid = request.path_params["uid"]
        target = db.user(uid)
        form = await request.form()
        if not target or uid == me["id"]:
            return back("/app/admin", "You can't change your own account here.", "warn")
        action = form.get("action")
        if action == "make_admin":
            db.update_user(uid, role="admin")
        elif action == "make_member":
            db.update_user(uid, role="member")
        elif action == "disable":
            db.update_user(uid, disabled=1)
        elif action == "enable":
            db.update_user(uid, disabled=0)
        return back("/app/admin", f"Updated {target['name'] or target['email']}.")

    # monitors and history
    MAX_MONITORS = int(os.environ.get("SOURCEBOOK_MAX_MONITORS", "10"))

    async def app_monitors(request: Request) -> Response:
        user = auth.current_user(request)
        if request.method == "POST":
            form = await request.form()
            slug, q = str(form.get("dataset", "")), str(form.get("q", ""))[:200]
            lens = str(form.get("lens", "mixed"))
            d = db.dataset(slug)
            if not d or d["status"] not in ("published", "discovered"):
                return not_found(request)
            mine = db.monitors(user["id"])
            if any(m["dataset"] == slug for m in mine):
                return back(f"/search?q={quote(q)}&lens={lens}", "You're already monitoring this search.")
            if len(mine) >= MAX_MONITORS and user["role"] != "admin":
                return back(f"/search?q={quote(q)}&lens={lens}", f"You can monitor up to {MAX_MONITORS} searches. Remove one first.", "warn")
            db.add_monitor(user["id"], q, slug, lens if lens in leadmod.LENSES else "mixed")
            return back(f"/search?q={quote(q)}&lens={lens}", "Monitoring this search. Sourcebook re-checks it every day and counts new businesses for you.")
        return render("app_monitors.html", request, monitors=db.monitors(user["id"]), history=db.search_history(user["id"]),
                      max_monitors=MAX_MONITORS, LENSES=leadmod.LENSES)

    async def app_monitor_action(request: Request) -> Response:
        user = auth.current_user(request)
        m = db.monitor(request.path_params["mid"], user["id"])
        if not m:
            return not_found(request)
        if request.path_params["action"] == "delete":
            db.delete_monitor(m["id"], user["id"])
            return back("/app/monitors", "Monitor removed.")
        return back("/app/monitors")

    async def app_monitor_open(request: Request) -> Response:
        user = auth.current_user(request)
        m = db.monitor(request.path_params["mid"], user["id"])
        if not m:
            return not_found(request)
        db.touch_monitor(m["id"], seen_at=time.time())
        return RedirectResponse(f"/search?q={quote(m['q'])}&lens={m['lens']}", status_code=303)

    # crawler
    async def app_crawler(request: Request) -> Response:
        settings = discover.crawler_settings(db)
        days = dict(db.daily_new(14))
        import datetime as _dt
        today = _dt.datetime.now(timezone.utc).date()
        series = [((today - _dt.timedelta(days=i)).isoformat(), days.get((today - _dt.timedelta(days=i)).isoformat(), 0)) for i in range(13, -1, -1)]
        last_tick = float(db.meta("crawler_last_tick", "0") or 0)
        return render("app_crawler.html", request, settings=settings, targets=db.targets(), series=series,
                      peak=max([n for _, n in series] + [settings["daily_target"], 1]),
                      today=db.new_entities_since(discover.today_start()), total=db.entity_count(),
                      discovered=sorted(db.datasets(status="discovered"), key=lambda d: d["built_at"] or 0, reverse=True)[:30],
                      popular=db.popular_searches(), last_tick=last_tick, running=db.jobs(status=("queued", "running"), limit=50),
                      web_search=__import__("sourcebook.collect.websearch", fromlist=["available"]).available(),
                      interval=runner.crawl_interval, country_name=country_name, coverage=discover.world_coverage(db),
                      world_types=len(discover.world_categories()))

    async def app_crawler_action(request: Request) -> Response:
        me = auth.current_user(request)
        form = await request.form()
        action = request.path_params["action"]
        url = "/app/admin/crawler"
        if action == "settings":
            db.set_meta("crawler_enabled", "1" if form.get("enabled") else "0")
            db.set_meta("crawler_world", "1" if form.get("world") else "0")
            try:
                db.set_meta("crawler_daily_target", str(max(10, min(20000, int(form.get("daily_target", 500))))))
                db.set_meta("crawler_per_run", str(max(1, min(20, int(form.get("per_run", 4))))))
            except ValueError:
                return back(url, "Enter whole numbers for the target and topics per hour.", "warn")
            return back(url, "Crawler settings saved.")
        if action == "add":
            what, country = str(form.get("what", "")).strip(), str(form.get("country", "")).upper()
            definition, errors = definition_from_form({"what": what, "country": country, "keywords": str(form.get("keywords", ""))})
            if errors:
                return back(url, " ".join(errors), "warn")
            try:
                every = max(1.0, min(365.0, float(form.get("every_days", 14))))
            except ValueError:
                every = 14.0
            tid = db.add_target(what, country, str(form.get("keywords", "")), str(form.get("products", "")), every, me["id"])
            return back(url, f"Added “{definition['title']}”." if tid else "That topic is already on the list.", "ok" if tid else "warn")
        if action == "starter":
            n = discover.load_starter_targets(db, me["id"])
            return back(url, f"Added {n} starter topics." if n else "The starter topics are already on the list.")
        if action == "run":
            queued = discover.crawl_tick(db, force=True)
            return back(url, f"Queued {len(queued)} topic{'s' if len(queued) != 1 else ''}." if queued else "Nothing is due, and no pages need refreshing.")
        if action == "from-search":
            what, country = str(form.get("what", "")), str(form.get("country", ""))
            tid = db.add_target(what, country, created_by=me["id"])
            return back(url, "Added to the crawl list." if tid else "That topic is already on the list.")
        if action.startswith("t-"):
            _, tid, op = action.split("-", 2)
            t = db.target(int(tid))
            if not t:
                return not_found(request)
            if op == "toggle":
                db.update_target(t["id"], enabled=0 if t["enabled"] else 1)
            elif op == "delete":
                db.delete_target(t["id"])
            elif op == "now":
                db.update_target(t["id"], last_run=None)
                discover.crawl_tick(db, force=True)
            return back(url, {"toggle": "Updated.", "delete": "Topic removed. Its page stays.", "now": "Queued."}.get(op, "Done."))
        return back(url)

    # account
    async def app_account(request: Request) -> Response:
        user = auth.current_user(request)
        if request.method == "GET":
            return render("app_account.html", request, identities=db.identities(user["id"]), has_password=not user["password"].startswith("!"))
        form = await request.form()
        action = form.get("action")
        if action == "profile":
            db.update_user(user["id"], name=str(form.get("name", "")).strip()[:80] or user["name"])
            return back("/app/account", "Saved.")
        if action == "password":
            if not user["password"].startswith("!") and not auth.check_password(str(form.get("current", "")), user["password"]):
                return back("/app/account", "Your current password isn't right.", "warn")
            problem = auth.password_problem(str(form.get("new", "")))
            if problem:
                return back("/app/account", problem, "warn")
            db.update_user(user["id"], password=auth.hash_password(str(form.get("new", ""))))
            auth.sign_in(request, db.user(user["id"]))
            return back("/app/account", "Password changed. Other devices have been signed out.")
        if action == "delete":
            if not user["password"].startswith("!") and not auth.check_password(str(form.get("current", "")), user["password"]):
                return back("/app/account", "Enter your password to delete your account.", "warn")
            if user["role"] == "admin" and sum(1 for u in db.users() if u["role"] == "admin") == 1:
                return back("/app/account", "You're the only admin. Make someone else an admin first.", "warn")
            db.delete_user(user["id"])
            request.session.clear()
            return RedirectResponse("/", status_code=303)
        return back("/app/account")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        if run_jobs:
            await runner.start()
        yield
        if run_jobs:
            await runner.stop()

    routes = [
        Route("/", home), Route("/search", search), Route("/search/live/{slug}", search_live), Route("/search/run/{slug}", search_run, methods=["POST"]), Route("/search/export/{slug}.csv", search_export),
        Route("/opportunities", opportunities), Route("/opportunities.csv", opportunities_csv), Route("/api/datasets", api_datasets),
        Route("/signin", signin, methods=["GET", "POST"]), Route("/signup", signup, methods=["GET", "POST"]),
        Route("/signout", signout, methods=["POST"]),
        Route("/auth/callback", sso_callback), Route("/auth/{provider:str}", sso_start),
        Route("/app", login_required(app_home)),
        Route("/app/new", login_required(app_new), methods=["GET", "POST"]),
        Route("/app/pages", login_required(app_pages)),
        Route("/app/pages/{slug}", login_required(app_page)),
        Route("/app/pages/{slug}/edit", login_required(app_page_edit), methods=["GET", "POST"]),
        Route("/app/pages/{slug}/{action}", login_required(app_page_action), methods=["POST"]),
        Route("/app/jobs/{jid:int}", login_required(app_job)),
        Route("/app/lists", login_required(app_lists), methods=["GET", "POST"]),
        Route("/app/lists/save", login_required(app_save), methods=["POST"]),
        Route("/app/lists/{lid:int}", login_required(app_list)),
        Route("/app/lists/{lid:int}.csv", login_required(app_list_csv)),
        Route("/app/lists/{lid:int}/{action}", login_required(app_list_action), methods=["POST"]),
        Route("/app/corrections", login_required(app_corrections)),
        Route("/app/corrections/{cid:int}", login_required(app_correction_review), methods=["POST"]),
        Route("/app/admin", admin_required(app_admin)),
        Route("/app/admin/users/{uid:int}", admin_required(app_admin_user), methods=["POST"]),
        Route("/app/admin/crawler", admin_required(app_crawler)),
        Route("/app/admin/crawler/{action}", admin_required(app_crawler_action), methods=["POST"]),
        Route("/app/account", login_required(app_account), methods=["GET", "POST"]),
        Route("/app/monitors", login_required(app_monitors), methods=["GET", "POST"]),
        Route("/app/monitors/{mid:int}", login_required(app_monitor_open)),
        Route("/app/monitors/{mid:int}/{action}", login_required(app_monitor_action), methods=["POST"]),
        Route("/d/{slug}", dataset), Route("/d/{slug}/{entity}", entity),
        Route("/d/{slug}/{entity}/correct", correct, methods=["POST"]),
        Route("/d/{slug}/{entity}/community/{action}", login_required(community), methods=["POST"]),
        Route("/robots.txt", robots), Route("/sitemap.xml", sitemap),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    secure = os.environ.get("SOURCEBOOK_PUBLIC_URL", "").startswith("https://")
    middleware = [Middleware(SessionMiddleware, secret_key=auth.session_secret(db), session_cookie="sourcebook",
                             max_age=60 * 60 * 24 * 30, same_site="lax", https_only=secure)]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.db = db
    app.state.runner = runner
    return app
