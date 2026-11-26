"""python -m sourcebook  serve | build <definition.json> | demo | refresh-all | make-admin <email>"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .build import build, load_demo
from .db import DB

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEF = ROOT / "datasets" / "food-packaging-ghana.json"


def main() -> None:
    p = argparse.ArgumentParser(prog="sourcebook", description="Public, source-linked data pages.")
    p.add_argument("--db", help="database file (default: $SOURCEBOOK_DB or sourcebook.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the website")
    s.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8100")))
    s.add_argument("--build-if-empty", action="store_true", help="start a build in the background when there are no data pages yet")
    b = sub.add_parser("build", help="collect and publish a data page")
    b.add_argument("definition", nargs="?", default=str(DEFAULT_DEF))
    b.add_argument("--no-websites", action="store_true", help="skip reading business websites (OpenStreetMap + list only)")
    b.add_argument("--delay", type=float, default=2.0, help="seconds between page requests on one website")
    d = sub.add_parser("demo", help="load clearly-labelled fictional sample data to preview the design")
    d.add_argument("definition", nargs="?", default=str(DEFAULT_DEF))
    sub.add_parser("refresh-all", help="rebuild every definition in datasets/")
    ma = sub.add_parser("make-admin", help="give an existing account admin rights")
    ma.add_argument("email")
    a = p.parse_args()
    db = DB(a.db)
    if a.cmd == "serve":
        import uvicorn
        from .web import create_app
        if a.build_if_empty and not db.datasets():
            import threading
            threading.Thread(target=lambda: asyncio.run(build(DEFAULT_DEF, db)), daemon=True).start()
            print("No data pages yet: building the first one in the background.")
        print(f"Sourcebook running at http://{a.host}:{a.port}")
        uvicorn.run(create_app(db), host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "build":
        asyncio.run(build(Path(a.definition), db, websites=not a.no_websites, delay=a.delay))
    elif a.cmd == "demo":
        load_demo(db, Path(a.definition))
        print("Sample data loaded. Run:  python -m sourcebook serve")
    elif a.cmd == "make-admin":
        user = db.user(email=a.email)
        if not user:
            raise SystemExit(f"No account with the email {a.email}. Sign up on the website first.")
        db.update_user(user["id"], role="admin")
        print(f"{a.email} is now an admin.")
    elif a.cmd == "refresh-all":
        for f in sorted((ROOT / "datasets").glob("*.json")):
            print(f"== {f.name}")
            asyncio.run(build(f, db))


if __name__ == "__main__":
    main()
