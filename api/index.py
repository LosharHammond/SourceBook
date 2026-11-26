"""Vercel entry point: Sourcebook as one Python function (ASGI). See docs/DEPLOY-FREE.md."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SOURCEBOOK_INLINE_JOBS", "1")   # no background worker on Vercel: searches run in their own request
os.environ.setdefault("DATABASE_POOL_MAX", "2")

from sourcebook.web import create_app  # noqa: E402

app = create_app()
