"""Storage: SQLite on your own computer, PostgreSQL (e.g. Supabase) when DATABASE_URL is set. Every fact about an entity is stored with where it came from and when it was seen.

Tables
  datasets     one public data page (e.g. "Food packaging suppliers in Ghana")
  entities     a business/organisation within a dataset
  facts        field/value pairs with source_url, source_type and observed_at (many per field allowed)
  corrections  suggestions and removal requests from the public, reviewed before they change anything
  search       full-text index over entities (FTS5 in SQLite, tsvector in PostgreSQL)

The SQL below is written for SQLite; _PgConn translates the few differences (placeholders, new-row ids, instr) for PostgreSQL.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
  slug TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT DEFAULT '', country TEXT DEFAULT '',
  definition TEXT DEFAULT '{}', built_at REAL, demo INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY, dataset TEXT NOT NULL REFERENCES datasets(slug) ON DELETE CASCADE,
  name TEXT NOT NULL, slug TEXT NOT NULL, city TEXT DEFAULT '', region TEXT DEFAULT '', website TEXT DEFAULT '',
  description TEXT DEFAULT '', description_sources TEXT DEFAULT '[]', products TEXT DEFAULT '[]',
  lat REAL, lon REAL, hidden INTEGER DEFAULT 0, demo INTEGER DEFAULT 0, updated_at REAL,
  UNIQUE(dataset, slug)
);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  field TEXT NOT NULL, value TEXT NOT NULL, source_url TEXT NOT NULL, source_type TEXT NOT NULL,
  observed_at REAL NOT NULL, UNIQUE(entity, field, value, source_url)
);
CREATE INDEX IF NOT EXISTS facts_entity ON facts(entity, field);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT, dataset TEXT, kind TEXT NOT NULL,
  field TEXT DEFAULT '', value TEXT DEFAULT '', evidence_url TEXT DEFAULT '', message TEXT DEFAULT '',
  contact TEXT DEFAULT '', is_owner INTEGER DEFAULT 0, status TEXT DEFAULT 'open', created_at REAL, reviewed_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(entity UNINDEXED, dataset UNINDEXED, name, city, products, description);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT DEFAULT '', password TEXT NOT NULL,
  role TEXT DEFAULT 'member', disabled INTEGER DEFAULT 0, created_at REAL, last_login REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, dataset TEXT NOT NULL, user_id INTEGER, kind TEXT DEFAULT 'build',
  status TEXT DEFAULT 'queued', log TEXT DEFAULT '', stats TEXT DEFAULT '{}', cancel INTEGER DEFAULT 0,
  created_at REAL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS lists (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, created_at REAL
);
CREATE TABLE IF NOT EXISTS list_items (
  list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE, entity TEXT NOT NULL, note TEXT DEFAULT '',
  added_at REAL, PRIMARY KEY(list_id, entity)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS crawl_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, what TEXT NOT NULL, country TEXT NOT NULL, keywords TEXT DEFAULT '',
  products TEXT DEFAULT '', every_days REAL DEFAULT 14, enabled INTEGER DEFAULT 1, dataset TEXT, last_run REAL,
  last_new INTEGER DEFAULT 0, created_by INTEGER, created_at REAL, UNIQUE(what, country)
);
CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, q TEXT NOT NULL, what TEXT, country TEXT, dataset TEXT, started INTEGER DEFAULT 0,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS searches_time ON searches(created_at);
CREATE TABLE IF NOT EXISTS monitors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, q TEXT NOT NULL,
  dataset TEXT NOT NULL, lens TEXT DEFAULT 'mixed', created_at REAL, seen_at REAL, last_run REAL
);
CREATE TABLE IF NOT EXISTS identities (
  provider TEXT NOT NULL, subject TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  email TEXT, created_at REAL, PRIMARY KEY(provider, subject)
);
CREATE TABLE IF NOT EXISTS world_runs (category TEXT NOT NULL, country TEXT NOT NULL, dataset TEXT, last_run REAL, PRIMARY KEY(category, country));
CREATE TABLE IF NOT EXISTS business_reviews (
  entity TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), body TEXT NOT NULL,
  hidden INTEGER DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(entity, user_id)
);
CREATE TABLE IF NOT EXISTS business_votes (
  entity TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  value INTEGER NOT NULL CHECK(value IN (-1,1)), updated_at REAL NOT NULL,
  PRIMARY KEY(entity, user_id)
);

"""

# columns added after the first release: (table, column, declaration)
MIGRATIONS = [
    ("datasets", "owner", "INTEGER"),
    ("datasets", "status", "TEXT DEFAULT 'published'"),   # draft → building → review → published / rejected
    ("datasets", "created_at", "REAL"),
    ("datasets", "review_note", "TEXT DEFAULT ''"),
    ("entities", "created_at", "REAL"),
    ("jobs", "priority", "INTEGER DEFAULT 5"),
    ("searches", "user_id", "INTEGER"),      # 0 = someone is waiting on a search, 5 = workspace, 9 = scheduled crawl
]

CONTACT_FIELDS = ("phone", "email", "whatsapp", "telegram", "website", "facebook", "instagram", "linkedin", "x")
SOURCE_LABEL = {"osm": "OpenStreetMap", "website": "the business's own website", "submission": "submitted by the business",
                "correction": "a reviewed correction", "curator": "the curator's list", "demo": "example data",
                "wikidata": "Wikidata", "check": "Sourcebook's check", "gleif": "the GLEIF company register", "web": "a web search result"}


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "item"


def default_path() -> Path:
    return Path(os.environ.get("SOURCEBOOK_DB", "sourcebook.db"))

STOP = {"in", "the", "of", "and", "for", "a", "an", "to", "near", "suppliers", "supplier", "companies", "company", "list", "who"}


SCHEMA_VERSION = "4"   # raise when SCHEMA or MIGRATIONS change, so PostgreSQL re-applies them once


def pg_schema_sql() -> str:
    """SCHEMA in PostgreSQL types, with every MIGRATIONS column, and a tsvector table in place of FTS5."""
    sql = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY").replace(" REAL", " DOUBLE PRECISION")
    sql = re.sub(r"CREATE VIRTUAL TABLE[^\n]*\n", "CREATE TABLE IF NOT EXISTS search (entity TEXT PRIMARY KEY, dataset TEXT, doc TSVECTOR);\n"
                 "CREATE INDEX IF NOT EXISTS search_doc ON search USING GIN (doc);\nCREATE INDEX IF NOT EXISTS search_dataset ON search(dataset);\n", sql)
    for table, col, decl in MIGRATIONS:
        sql += f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {decl.replace('REAL', 'DOUBLE PRECISION')};\n"
    sql += "CREATE INDEX IF NOT EXISTS entities_dataset ON entities(dataset);\nCREATE INDEX IF NOT EXISTS entities_created ON entities(created_at);\n"
    return sql


# tables whose rows get a new id from the database (cursor.lastrowid)
_SERIAL = {"facts", "corrections", "users", "jobs", "lists", "crawl_targets", "searches", "monitors"}
_pools: dict[str, Any] = {}


class _Row(dict):
    """A result row readable both by column name and by position, like sqlite3.Row."""
    def __init__(self, names: list[str], values: tuple) -> None:
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, k: Any) -> Any:
        return self._values[k] if isinstance(k, int) else dict.__getitem__(self, k)


class _Result:
    def __init__(self, rows: list[_Row], rowcount: int, lastrowid: int | None) -> None:
        self.rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    def fetchone(self) -> _Row | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[_Row]:
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _PgConn:
    """Runs Sourcebook's SQLite-flavoured SQL on PostgreSQL."""
    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, sql: str, args: Any = ()) -> _Result:
        sql = re.sub(r"\binstr\(", "strpos(", sql)
        lastid = False
        m = re.match(r"\s*INSERT INTO (\w+)", sql)
        if m and m.group(1) in _SERIAL and "RETURNING" not in sql.upper():
            sql, lastid = sql.rstrip().rstrip(";") + " RETURNING id", True
        params = list(args) if args else None
        if params is not None:
            sql = sql.replace("%", "%%").replace("?", "%s")
        cur = self.raw.execute(sql, params)
        rows: list[_Row] = []
        if cur.description:
            names = [d.name for d in cur.description]
            rows = [_Row(names, tuple(v)) for v in cur.fetchall()]
        return _Result(rows, cur.rowcount, (rows[0][0] if rows else None) if lastid else None)

    def executescript(self, sql: str) -> None:
        self.raw.execute(sql)


class DB:
    def __init__(self, path: Path | str | None = None, url: str | None = None) -> None:
        self.url = url if url is not None else (os.environ.get("DATABASE_URL") if path is None else None)
        self.pg = bool(self.url)
        self.path = "" if self.pg else str(path or default_path())
        if self.pg:
            self._pg_setup()
            return
        with self.conn() as c:
            c.executescript(SCHEMA)
            for table, col, decl in MIGRATIONS:
                if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # ------------------------------------------------------------------ PostgreSQL
    def _pg_setup(self) -> None:
        from psycopg_pool import ConnectionPool
        schema = os.environ.get("DATABASE_SCHEMA", "sourcebook")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", schema):
            raise ValueError("DATABASE_SCHEMA may only use a-z, 0-9 and _")

        def configure(conn: Any) -> None:
            # Supabase publishes the public schema through its REST API, so Sourcebook's tables live in their own schema.
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"; SET search_path TO "{schema}", public; SET client_encoding TO \'UTF8\'')
            conn.commit()

        key = f"{self.url}|{schema}"
        if key not in _pools:
            _pools[key] = ConnectionPool(self.url, min_size=0, max_size=int(os.environ.get("DATABASE_POOL_MAX", "3")),
                                         configure=configure, max_idle=120, timeout=30, open=True,
                                         kwargs={"prepare_threshold": None, "connect_timeout": 10})
        self._pool = _pools[key]
        with self.conn() as c:
            c.execute("SELECT pg_advisory_xact_lock(4207)")
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            r = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if not r or r[0] != SCHEMA_VERSION:
                c.executescript(pg_schema_sql())
                c.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (SCHEMA_VERSION,))

    @contextmanager
    def conn(self) -> Iterator[Any]:
        if self.pg:
            with self._pool.connection() as raw:   # commits on success, rolls back on error
                yield _PgConn(raw)
            return
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------------ datasets
    def upsert_dataset(self, slug: str, title: str, summary: str, country: str, definition: dict[str, Any], demo: bool = False) -> None:
        with self.conn() as c:
            c.execute("""INSERT INTO datasets(slug, title, summary, country, definition, built_at, demo) VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(slug) DO UPDATE SET title=excluded.title, summary=excluded.summary, country=excluded.country,
                         definition=excluded.definition, built_at=excluded.built_at, demo=excluded.demo""",
                      (slug, title, summary, country, json.dumps(definition), time.time(), int(demo)))

    def datasets(self, status: str | None = "published", owner: int | None = None) -> list[dict[str, Any]]:
        """Published pages by default; status=None for every page; owner= to limit to one user's pages."""
        where, args = [], []
        if status:
            where.append("d.status=?"); args.append(status)
        if owner is not None:
            where.append("d.owner=?"); args.append(owner)
        sql = ("SELECT d.*, (SELECT COUNT(*) FROM entities e WHERE e.dataset=d.slug AND e.hidden=0) AS n FROM datasets d"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY lower(d.title)")
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def create_dataset(self, slug: str, title: str, summary: str, country: str, definition: dict[str, Any], owner: int | None,
                       status: str = "draft") -> str:
        """A new page made in the workspace. Picks a free slug."""
        base, n = slug, 2
        while self.dataset(slug):
            slug = f"{base}-{n}"; n += 1
        definition = {**definition, "slug": slug}
        with self.conn() as c:
            c.execute("""INSERT INTO datasets(slug, title, summary, country, definition, owner, status, created_at)
                         VALUES(?,?,?,?,?,?,?,?)""", (slug, title, summary, country, json.dumps(definition), owner, status, time.time()))
        return slug

    def update_definition(self, slug: str, title: str, summary: str, country: str, definition: dict[str, Any]) -> None:
        with self.conn() as c:
            c.execute("UPDATE datasets SET title=?, summary=?, country=?, definition=? WHERE slug=?",
                      (title, summary, country, json.dumps({**definition, "slug": slug}), slug))

    def set_status(self, slug: str, status: str, note: str | None = None) -> None:
        with self.conn() as c:
            if note is None:
                c.execute("UPDATE datasets SET status=? WHERE slug=?", (status, slug))
            else:
                c.execute("UPDATE datasets SET status=?, review_note=? WHERE slug=?", (status, note, slug))

    def delete_dataset(self, slug: str) -> None:
        self.clear_dataset(slug)
        with self.conn() as c:
            c.execute("DELETE FROM jobs WHERE dataset=?", (slug,))
            c.execute("DELETE FROM corrections WHERE dataset=?", (slug,))
            c.execute("DELETE FROM datasets WHERE slug=?", (slug,))

    def dataset(self, slug: str) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM datasets WHERE slug=?", (slug,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ entities & facts
    def upsert_entity(self, dataset: str, name: str, **fields: Any) -> str:
        slug = fields.pop("slug", None) or slugify(name)
        eid = f"{dataset}:{slug}"
        cols = {k: v for k, v in fields.items() if k in ("city", "region", "website", "lat", "lon", "demo")}
        with self.conn() as c:
            c.execute("INSERT INTO entities(id, dataset, name, slug, updated_at, created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                      (eid, dataset, name, slug, time.time(), time.time()))
            for k, v in cols.items():
                if v not in (None, ""):
                    c.execute(f"UPDATE entities SET {k}=?, updated_at=? WHERE id=?", (v, time.time(), eid))
        return eid

    def add_fact(self, entity: str, field: str, value: str, source_url: str, source_type: str, observed_at: float | None = None) -> None:
        value = (value or "").strip()
        if not value:
            return
        with self.conn() as c:
            c.execute("""INSERT INTO facts(entity, field, value, source_url, source_type, observed_at) VALUES(?,?,?,?,?,?)
                         ON CONFLICT(entity, field, value, source_url) DO UPDATE SET observed_at=excluded.observed_at""",
                      (entity, field, value[:2000], source_url, source_type, observed_at or time.time()))

    def facts(self, entity: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM facts WHERE entity=? ORDER BY field, observed_at DESC", (entity,)).fetchall()
        return [dict(r) for r in rows]

    def clear_field(self, entity: str, field: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM facts WHERE entity=? AND field=?", (entity, field))

    def set_description(self, entity: str, text: str, sources: list[str], products: list[str]) -> None:
        with self.conn() as c:
            c.execute("UPDATE entities SET description=?, description_sources=?, products=?, updated_at=? WHERE id=?",
                      (text, json.dumps(sources), json.dumps(products), time.time(), entity))

    def entity(self, dataset: str, slug: str) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM entities WHERE dataset=? AND slug=? AND hidden=0", (dataset, slug)).fetchone()
        return self._entity(dict(r)) if r else None

    def entities(self, dataset: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM entities WHERE dataset=? AND hidden=0 ORDER BY lower(name)", (dataset,)).fetchall()
            by: dict[str, list[dict[str, Any]]] = {}
            for f in c.execute("""SELECT f.* FROM facts f JOIN entities e ON e.id=f.entity WHERE e.dataset=? AND e.hidden=0
                                  ORDER BY f.field, f.observed_at DESC""", (dataset,)).fetchall():
                by.setdefault(f["entity"], []).append(dict(f))
        return [self._entity(dict(r), by.get(r["id"], [])) for r in rows]

    def _entity(self, e: dict[str, Any], facts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        e["products"] = json.loads(e.get("products") or "[]")
        e["description_sources"] = json.loads(e.get("description_sources") or "[]")
        facts = self.facts(e["id"]) if facts is None else facts
        e["facts"] = facts
        contacts: dict[str, list[dict[str, Any]]] = {}
        for f in facts:
            if f["field"] in CONTACT_FIELDS:
                contacts.setdefault(f["field"], [])
                if f["value"] not in [x["value"] for x in contacts[f["field"]]]:
                    contacts[f["field"]].append(f)
        e["contacts"] = contacts
        from .gaps import ORDER, category, helper, label
        gaps = []
        for f in facts:
            if f["field"] == "gap":
                code, _, detail = f["value"].partition(": ")
                gaps.append({"code": code, "label": label(code), "helper": helper(code), "category": category(code), "detail": detail,
                             "source_url": f["source_url"], "observed_at": f["observed_at"]})
        e["gaps"] = sorted(gaps, key=lambda g: ORDER.index(g["code"]) if g["code"] in ORDER else 99)
        e["gap_codes"] = [g["code"] for g in e["gaps"]]
        e["sources"] = sorted({f["source_url"] for f in facts if f["source_type"] != "check"})
        e["checked_at"] = max([f["observed_at"] for f in facts], default=e.get("updated_at"))
        return e

    def hide(self, entity: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE entities SET hidden=1, updated_at=? WHERE id=?", (time.time(), entity))
            c.execute("DELETE FROM search WHERE entity=?", (entity,))

    def clear_dataset(self, dataset: str, demo_only: bool = False) -> None:
        with self.conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM entities WHERE dataset=?" + (" AND demo=1" if demo_only else ""), (dataset,))]
            for eid in ids:
                c.execute("DELETE FROM search WHERE entity=?", (eid,))
                c.execute("DELETE FROM entities WHERE id=?", (eid,))

    # ------------------------------------------------------------------ search
    def reindex(self, dataset: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM search WHERE dataset=?", (dataset,))
            if self.pg:
                c.execute("INSERT INTO search(entity,dataset,doc) SELECT id,dataset, setweight(to_tsvector('simple',name),'A') || setweight(to_tsvector('simple',COALESCE(city,'') || ' ' || products),'B') || to_tsvector('simple',COALESCE(description,'')) FROM entities WHERE dataset=? AND hidden=0 ON CONFLICT(entity) DO UPDATE SET doc=excluded.doc", (dataset,))
                return
            for r in c.execute("SELECT id, name, city, products, description FROM entities WHERE dataset=? AND hidden=0", (dataset,)).fetchall():
                products = " ".join(json.loads(r["products"] or "[]"))
                if self.pg:
                    c.execute("""INSERT INTO search(entity, dataset, doc) VALUES(?, ?, setweight(to_tsvector('simple', ?::text), 'A')
                                 || setweight(to_tsvector('simple', ?::text || ' ' || ?::text), 'B') || to_tsvector('simple', ?::text))
                                 ON CONFLICT(entity) DO UPDATE SET dataset=excluded.dataset, doc=excluded.doc""",
                              (r["id"], dataset, r["name"], r["city"] or "", products, r["description"] or ""))
                else:
                    c.execute("INSERT INTO search(entity, dataset, name, city, products, description) VALUES(?,?,?,?,?,?)",
                              (r["id"], dataset, r["name"], r["city"], products, r["description"]))

    def search(self, query: str, limit: int = 50, published_only: bool = True, country: str | None = None) -> list[dict[str, Any]]:
        terms = [t for t in re.findall(r"[\w']+", query.lower()) if len(t) > 1 and t not in STOP]
        if not terms:
            return []
        if self.pg:
            tsq = " | ".join(f"{t.replace(chr(39), '')}:*" for t in terms if t.replace(chr(39), ""))
            with self.conn() as c:
                rows = c.execute("""SELECT s.entity, s.dataset, e.slug, e.name, e.city, e.products, e.description, d.status AS dataset_status,
                                    -ts_rank(s.doc, to_tsquery('simple', ?::text)) AS rank
                                    FROM search s JOIN entities e ON e.id = s.entity JOIN datasets d ON d.slug = s.dataset
                                    WHERE s.doc @@ to_tsquery('simple', ?::text) AND (d.status IN ('published', 'discovered') OR ?=0) AND (?='' OR d.country=?)
                                    ORDER BY rank LIMIT ?""", (tsq, tsq, int(published_only), country or "", country or "", limit)).fetchall()
            return [{**dict(r), "products": json.loads(r["products"] or "[]")} for r in rows]
        fts = " OR ".join(f'"{t}"*' for t in terms)
        with self.conn() as c:
            rows = c.execute("""SELECT s.entity, s.dataset, e.slug, e.name, e.city, e.products, e.description, d.status AS dataset_status, bm25(search) AS rank
                                FROM search s JOIN entities e ON e.id = s.entity JOIN datasets d ON d.slug = s.dataset
                                WHERE search MATCH ? AND (d.status IN ('published', 'discovered') OR ?=0) AND (?='' OR d.country=?) ORDER BY rank LIMIT ?""",
                             (fts, int(published_only), country or "", country or "", limit)).fetchall()
        return [{**dict(r), "products": json.loads(r["products"] or "[]")} for r in rows]

    # ------------------------------------------------------------------ corrections
    def add_correction(self, **fields: Any) -> int:
        keys = ("entity", "dataset", "kind", "field", "value", "evidence_url", "message", "contact", "is_owner")
        vals = [fields.get(k, "") for k in keys]
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO corrections({', '.join(keys)}, created_at) VALUES({', '.join('?' * len(keys))}, ?)",
                            (*vals, time.time()))
            return int(cur.lastrowid)

    def corrections(self, status: str = "open", datasets: list[str] | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM corrections WHERE status=?", [status]
        if datasets is not None:
            if not datasets:
                return []
            sql += f" AND dataset IN ({','.join('?' * len(datasets))})"; args += datasets
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY created_at", args).fetchall()]

    def correction(self, cid: int) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM corrections WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def review(self, cid: int, approve: bool) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM corrections WHERE id=?", (cid,)).fetchone()
            if not r:
                return None
            c.execute("UPDATE corrections SET status=?, reviewed_at=? WHERE id=?", ("approved" if approve else "rejected", time.time(), cid))
        row = dict(r)
        if approve and row["entity"]:
            if row["kind"] == "remove":
                self.hide(row["entity"])
            elif row["field"] and row["value"]:
                self.add_fact(row["entity"], row["field"], row["value"], row["evidence_url"] or "correction", "correction")
        return row




    # ------------------------------------------------------------------ meta
    def meta(self, key: str, default: str | None = None) -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ------------------------------------------------------------------ users
    def create_user(self, email: str, name: str, password_hash: str, role: str = "member") -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO users(email, name, password, role, created_at) VALUES(?,?,?,?,?)",
                            (email.lower().strip(), name.strip(), password_hash, role, time.time()))
            return int(cur.lastrowid)

    def user(self, uid: int | None = None, email: str | None = None) -> dict[str, Any] | None:
        with self.conn() as c:
            if uid is not None:
                r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            else:
                r = c.execute("SELECT * FROM users WHERE email=?", ((email or "").lower().strip(),)).fetchone()
        return dict(r) if r else None

    def identity_user(self, provider: str, subject: str) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT u.* FROM identities i JOIN users u ON u.id=i.user_id WHERE i.provider=? AND i.subject=?", (provider, subject)).fetchone()
        return dict(r) if r else None

    def link_identity(self, user_id: int, provider: str, subject: str, email: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO identities(provider, subject, user_id, email, created_at) VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                      (provider, subject, user_id, email, time.time()))

    def identities(self, user_id: int) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM identities WHERE user_id=? ORDER BY created_at", (user_id,))]

    def users(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("""SELECT u.*, (SELECT COUNT(*) FROM datasets d WHERE d.owner=u.id) AS pages
                                FROM users u ORDER BY u.created_at""").fetchall()
        return [dict(r) for r in rows]

    def user_count(self) -> int:
        with self.conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def update_user(self, uid: int, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("name", "password", "role", "disabled", "last_login")}
        if not allowed:
            return
        with self.conn() as c:
            c.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?", (*allowed.values(), uid))

    def delete_user(self, uid: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE datasets SET owner=NULL WHERE owner=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))

    # ------------------------------------------------------------------ jobs
    def add_job(self, dataset: str, user_id: int | None, kind: str = "build", priority: int = 5) -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO jobs(dataset, user_id, kind, priority, created_at) VALUES(?,?,?,?,?)",
                            (dataset, user_id, kind, priority, time.time()))
            return int(cur.lastrowid)

    def job(self, jid: int) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return dict(r) if r else None

    def jobs(self, dataset: str | None = None, user_id: int | None = None, status: tuple[str, ...] | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        where, args = [], []
        if dataset:
            where.append("j.dataset=?"); args.append(dataset)
        if user_id is not None:
            where.append("j.user_id=?"); args.append(user_id)
        if status:
            where.append(f"j.status IN ({','.join('?' * len(status))})"); args += list(status)
        sql = ("SELECT j.*, d.title FROM jobs j LEFT JOIN datasets d ON d.slug=j.dataset"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY j.id DESC LIMIT ?")
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, (*args, limit)).fetchall()]

    def claim_job(self, dataset: str | None = None) -> dict[str, Any] | None:
        with self.conn() as c:
            if dataset:
                r = c.execute("SELECT * FROM jobs WHERE status='queued' AND dataset=? ORDER BY priority, id LIMIT 1", (dataset,)).fetchone()
            else:
                r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY priority, id LIMIT 1").fetchone()
            if not r:
                return None
            n = c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'", (time.time(), r["id"])).rowcount
        return dict(r) if n else None

    def job_log(self, jid: int, line: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE jobs SET log = log || ? WHERE id=?", (line.rstrip() + "\n", jid))

    def finish_job(self, jid: int, status: str, stats: dict[str, Any] | None = None) -> None:
        with self.conn() as c:
            c.execute("UPDATE jobs SET status=?, stats=?, finished_at=? WHERE id=?", (status, json.dumps(stats or {}), time.time(), jid))

    def cancel_job(self, jid: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE jobs SET cancel=1 WHERE id=? AND status='running'", (jid,))
            c.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'", (time.time(), jid))

    def requeue_interrupted(self, older_than: float | None = None) -> int:
        """Jobs marked running when nothing is running them: all of them at start-up, or (older_than) ones started that many
        seconds ago, which a host with a time limit stopped."""
        with self.conn() as c:
            if older_than is None:
                return c.execute("UPDATE jobs SET status='queued', log = log || 'Restarted after the server stopped.\n' "
                                 "WHERE status='running'").rowcount
            return c.execute("UPDATE jobs SET status='queued', log = log || 'Picked up again by the crawler.\n' "
                             "WHERE status='running' AND started_at < ?", (time.time() - older_than,)).rowcount

    def jobs_today(self, user_id: int) -> int:
        with self.conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM jobs WHERE user_id=? AND created_at>?", (user_id, time.time() - 86400)).fetchone()[0])

    # ------------------------------------------------------------------ shortlists
    def create_list(self, user_id: int, name: str) -> int:
        with self.conn() as c:
            return int(c.execute("INSERT INTO lists(user_id, name, created_at) VALUES(?,?,?)", (user_id, name.strip()[:80] or "Shortlist", time.time())).lastrowid)

    def lists(self, user_id: int) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("""SELECT l.*, (SELECT COUNT(*) FROM list_items i WHERE i.list_id=l.id) AS n FROM lists l
                                WHERE l.user_id=? ORDER BY l.created_at DESC""", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_(self, lid: int, user_id: int) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM lists WHERE id=? AND user_id=?", (lid, user_id)).fetchone()
        return dict(r) if r else None

    def rename_list(self, lid: int, user_id: int, name: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE lists SET name=? WHERE id=? AND user_id=?", (name.strip()[:80] or "Shortlist", lid, user_id))

    def delete_list(self, lid: int, user_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM lists WHERE id=? AND user_id=?", (lid, user_id))

    def add_to_list(self, lid: int, entity: str, note: str = "") -> None:
        with self.conn() as c:
            c.execute("INSERT INTO list_items(list_id, entity, note, added_at) VALUES(?,?,?,?) ON CONFLICT(list_id, entity) DO UPDATE SET note=excluded.note",
                      (lid, entity, note[:500], time.time()))

    def remove_from_list(self, lid: int, entity: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM list_items WHERE list_id=? AND entity=?", (lid, entity))

    def list_items(self, lid: int) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("""SELECT i.note, i.added_at, e.* FROM list_items i JOIN entities e ON e.id=i.entity
                                WHERE i.list_id=? AND e.hidden=0 ORDER BY i.added_at DESC""", (lid,)).fetchall()
        return [{**self._entity(dict(r)), "note": r["note"], "added_at": r["added_at"]} for r in rows]

    def saved_in(self, user_id: int, entity: str) -> list[int]:
        with self.conn() as c:
            return [r[0] for r in c.execute("""SELECT i.list_id FROM list_items i JOIN lists l ON l.id=i.list_id
                                               WHERE l.user_id=? AND i.entity=?""", (user_id, entity))]

    # ------------------------------------------------------------------ discovery & crawling
    def new_entities_since(self, ts: float) -> int:
        with self.conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM entities WHERE created_at>=? AND hidden=0 AND demo=0", (ts,)).fetchone()[0])

    def entity_count(self) -> int:
        with self.conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM entities WHERE hidden=0 AND demo=0").fetchone()[0])

    def daily_new(self, days: int = 14) -> list[tuple[str, int]]:
        with self.conn() as c:
            day = "to_char(to_timestamp(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD')" if self.pg else "date(created_at, 'unixepoch')"
            rows = c.execute(f"""SELECT {day} AS day, COUNT(*) FROM entities
                                WHERE created_at>=? AND hidden=0 AND demo=0 GROUP BY day ORDER BY day""",
                             (time.time() - days * 86400,)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def add_target(self, what: str, country: str, keywords: str = "", products: str = "", every_days: float = 14,
                   created_by: int | None = None) -> int | None:
        with self.conn() as c:
            cur = c.execute("""INSERT INTO crawl_targets(what, country, keywords, products, every_days, created_by, created_at)
                               VALUES(?,?,?,?,?,?,?) ON CONFLICT(what, country) DO NOTHING""",
                            (what.strip(), country.upper(), keywords, products, every_days, created_by, time.time()))
            return int(cur.lastrowid) if cur.rowcount else None

    def targets(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("""SELECT t.*, (SELECT COUNT(*) FROM entities e WHERE e.dataset=t.dataset AND e.hidden=0) AS n
                                FROM crawl_targets t ORDER BY t.enabled DESC, COALESCE(t.last_run, 0), t.id""").fetchall()
        return [dict(r) for r in rows]

    def target(self, tid: int) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM crawl_targets WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def update_target(self, tid: int, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("enabled", "dataset", "last_run", "last_new", "every_days")}
        if allowed:
            with self.conn() as c:
                c.execute(f"UPDATE crawl_targets SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?", (*allowed.values(), tid))

    def delete_target(self, tid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM crawl_targets WHERE id=?", (tid,))

    def due_targets(self, limit: int) -> list[dict[str, Any]]:
        now = time.time()
        with self.conn() as c:
            rows = c.execute("""SELECT * FROM crawl_targets WHERE enabled=1 AND (last_run IS NULL OR last_run < ? - every_days*86400)
                                ORDER BY COALESCE(last_run, 0), id LIMIT ?""", (now, limit)).fetchall()
        return [dict(r) for r in rows]

    def log_search(self, q: str, what: str | None, country: str | None, dataset: str | None, started: bool,
                   user_id: int | None = None) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO searches(q, what, country, dataset, started, created_at, user_id) VALUES(?,?,?,?,?,?,?)",
                      (q[:200], what, country, dataset, int(started), time.time(), user_id))

    def search_history(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("""SELECT h.q, h.dataset, h.last, h.times, d.title,
                                (SELECT COUNT(*) FROM entities e WHERE e.dataset=h.dataset AND e.hidden=0) AS n
                                FROM (SELECT MAX(s.q) AS q, MAX(s.dataset) AS dataset, MAX(s.created_at) AS last, COUNT(*) AS times
                                      FROM searches s WHERE s.user_id=? GROUP BY lower(s.q)) h
                                LEFT JOIN datasets d ON d.slug=h.dataset ORDER BY h.last DESC LIMIT ?""", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # monitors: saved searches that are re-checked and report new businesses
    def add_monitor(self, user_id: int, q: str, dataset: str, lens: str) -> int:
        with self.conn() as c:
            return int(c.execute("INSERT INTO monitors(user_id, q, dataset, lens, created_at, seen_at) VALUES(?,?,?,?,?,?)",
                                 (user_id, q[:200], dataset, lens, time.time(), time.time())).lastrowid)

    def monitors(self, user_id: int | None = None) -> list[dict[str, Any]]:
        sql = """SELECT m.*, d.title, d.built_at,
                 (SELECT COUNT(*) FROM entities e WHERE e.dataset=m.dataset AND e.hidden=0) AS n,
                 (SELECT COUNT(*) FROM entities e WHERE e.dataset=m.dataset AND e.hidden=0 AND e.created_at>m.seen_at) AS fresh
                 FROM monitors m LEFT JOIN datasets d ON d.slug=m.dataset"""
        with self.conn() as c:
            rows = c.execute(sql + (" WHERE m.user_id=?" if user_id is not None else "") + " ORDER BY m.created_at DESC",
                             (user_id,) if user_id is not None else ()).fetchall()
        return [dict(r) for r in rows]

    def monitor(self, mid: int, user_id: int) -> dict[str, Any] | None:
        return next((m for m in self.monitors(user_id) if m["id"] == mid), None)

    def touch_monitor(self, mid: int, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("seen_at", "lens", "last_run")}
        if allowed:
            with self.conn() as c:
                c.execute(f"UPDATE monitors SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?", (*allowed.values(), mid))

    def delete_monitor(self, mid: int, user_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM monitors WHERE id=? AND user_id=?", (mid, user_id))

    def popular_searches(self, days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("""SELECT what, country, COUNT(*) AS n, MAX(created_at) AS last, MAX(dataset) AS dataset FROM searches
                                WHERE created_at>? AND what IS NOT NULL GROUP BY what, country ORDER BY n DESC, last DESC LIMIT ?""",
                             (time.time() - days * 86400, limit)).fetchall()
        return [dict(r) for r in rows]

    # Community feedback is separate from facts collected from public sources.
    def feedback(self, entity: str, user_id: int | None = None) -> dict:
        with self.conn() as c:
            reviews = [dict(r) for r in c.execute("SELECT r.*, u.name AS author FROM business_reviews r JOIN users u ON u.id=r.user_id WHERE r.entity=? AND r.hidden=0 ORDER BY r.updated_at DESC LIMIT 100", (entity,))]
            totals = c.execute("SELECT COUNT(*), AVG(rating) FROM business_reviews WHERE entity=? AND hidden=0", (entity,)).fetchone()
            votes = c.execute("SELECT COALESCE(SUM(value),0), COUNT(*) FROM business_votes WHERE entity=?", (entity,)).fetchone()
            mine = c.execute("SELECT value FROM business_votes WHERE entity=? AND user_id=?", (entity, user_id or 0)).fetchone()
        return {"reviews": reviews, "count": totals[0], "rating": round(float(totals[1]), 1) if totals[1] is not None else None,
                "score": votes[0], "votes": votes[1], "my_vote": mine[0] if mine else 0}

    def review_business(self, entity: str, user_id: int, rating: int, body: str) -> None:
        if rating not in range(1, 6) or not 10 <= len(body.strip()) <= 2000:
            raise ValueError("Choose 1 to 5 stars and write a review of 10 to 2,000 characters.")
        now = time.time()
        with self.conn() as c:
            c.execute("INSERT INTO business_reviews(entity,user_id,rating,body,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(entity,user_id) DO UPDATE SET rating=excluded.rating, body=excluded.body, updated_at=excluded.updated_at", (entity,user_id,rating,body.strip(),now,now))

    def vote_business(self, entity: str, user_id: int, value: int) -> None:
        if value not in (-1, 0, 1):
            raise ValueError("Choose recommend, do not recommend, or remove vote.")
        with self.conn() as c:
            if value == 0:
                c.execute("DELETE FROM business_votes WHERE entity=? AND user_id=?", (entity,user_id))
            else:
                c.execute("INSERT INTO business_votes(entity,user_id,value,updated_at) VALUES(?,?,?,?) ON CONFLICT(entity,user_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (entity,user_id,value,time.time()))
