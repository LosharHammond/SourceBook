"""Run the whole suite on PostgreSQL too:  SOURCEBOOK_TEST_PG=postgres://... python -m pytest
Each test gets its own fresh schema."""
import os
import uuid

import pytest

from sourcebook import db as dbmod

PG = os.environ.get("SOURCEBOOK_TEST_PG")


@pytest.fixture(autouse=True)
def _postgres(monkeypatch):
    if not PG:
        yield
        return
    monkeypatch.setenv("DATABASE_SCHEMA", "t_" + uuid.uuid4().hex[:12])
    original = dbmod.DB.__init__
    monkeypatch.setattr(dbmod.DB, "__init__", lambda self, path=None, url=None: original(self, url=PG))
    yield
