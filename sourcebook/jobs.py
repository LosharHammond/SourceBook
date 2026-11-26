"""Background builds. Jobs live in the database, so a restart picks up where it left off."""

from __future__ import annotations

import asyncio
import os
import time
import traceback

from .build import Cancelled, build, definition_for
from .db import DB


class Runner:
    def __init__(self, db: DB, workers: int | None = None, poll: float | None = None) -> None:
        self.db = db
        self.workers = workers or int(os.environ.get("SOURCEBOOK_WORKERS", "2"))
        self.poll = poll if poll is not None else float(os.environ.get("SOURCEBOOK_POLL", "2"))
        self.refresh_days = float(os.environ.get("SOURCEBOOK_REFRESH_DAYS", "7"))
        self.crawl_interval = float(os.environ.get("SOURCEBOOK_CRAWL_INTERVAL", "3600"))   # seconds; 0 turns the crawler off
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.db.requeue_interrupted()
        self.tasks = [asyncio.create_task(self._worker(i)) for i in range(self.workers)]
        if self.refresh_days > 0:
            self.tasks.append(asyncio.create_task(self._refresher()))
        if self.crawl_interval > 0:
            self.tasks.append(asyncio.create_task(self._crawler()))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _worker(self, n: int) -> None:
        while True:
            job = self.db.claim_job()
            if not job:
                await asyncio.sleep(self.poll)
                continue
            await self.run(job)

    async def run(self, job: dict) -> None:
        jid, slug = job["id"], job["dataset"]
        log = lambda line: self.db.job_log(jid, line)  # noqa: E731
        d = definition_for(self.db, slug)
        if not d:
            self.db.finish_job(jid, "failed")
            return
        page = self.db.dataset(slug)
        was = page["status"] if page else "draft"
        if was in ("draft", "rejected", "building", "review"):
            self.db.set_status(slug, "building")
        log(f"Building “{d['title']}”")
        stopped = lambda: bool((self.db.job(jid) or {}).get("cancel"))  # noqa: E731
        try:
            stats = await build(d, self.db, log=log, stop=stopped)
        except Cancelled:
            self.db.finish_job(jid, "cancelled")
            self._after(slug, was, job)
            return
        except Exception as exc:  # noqa: BLE001 - shown to the page owner
            log(f"The build stopped with an error: {exc.__class__.__name__}: {exc}")
            log(traceback.format_exc(limit=3))
            self.db.finish_job(jid, "failed")
            self._after(slug, was, job)
            return
        self.db.finish_job(jid, "done", stats)
        self._after(slug, was, job)

    def _after(self, slug: str, was: str, job: dict) -> None:
        """Published pages stay published. A new page with results goes to review (or straight out if an admin built it);
        one with nothing on it goes back to draft so its owner can change the keywords."""
        if was in ("published", "discovered"):
            self.db.set_status(slug, was)
            return
        if not self.db.entities(slug):
            self.db.set_status(slug, "draft")
            return
        user = self.db.user(job["user_id"]) if job.get("user_id") else None
        self.db.set_status(slug, "published" if user and user["role"] == "admin" else "review")

    async def _refresher(self) -> None:
        while True:
            await asyncio.sleep(3600)
            cutoff = time.time() - self.refresh_days * 86400
            busy = {j["dataset"] for j in self.db.jobs(status=("queued", "running"), limit=500)}
            for d in self.db.datasets():
                if not d["demo"] and (d["built_at"] or 0) < cutoff and d["slug"] not in busy:
                    self.db.add_job(d["slug"], None, "refresh")

    async def _crawler(self) -> None:
        from .discover import crawl_tick
        await asyncio.sleep(min(60, self.crawl_interval))
        while True:
            try:
                queued = crawl_tick(self.db)
                self.db.set_meta("crawler_last_tick", str(time.time()))
                if queued:
                    self.db.set_meta("crawler_last_queued", ", ".join(queued))
            except Exception:  # noqa: BLE001 - the crawler must keep its schedule
                traceback.print_exc()
            await asyncio.sleep(self.crawl_interval)
