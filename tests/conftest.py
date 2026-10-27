"""Tests run fully offline against a temporary SQLite database and a fresh synthetic dataset."""
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="claimcheck-test-"))
# TEST_DATABASE_URL=postgresql+psycopg://... runs the same suite on Postgres + pgvector (CI does both)
os.environ.update(DATA_DIR=str(_tmp / "data"), RESULTS_DIR=str(_tmp / "results"), LLM_BASE_URL="", WEBHOOK_URL="",
                  DATABASE_URL=os.getenv("TEST_DATABASE_URL") or f"sqlite:///{_tmp / 'data' / 'test.db'}")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def demo():
    from claimcheck.pipeline import build_demo
    return build_demo(n=24, method="pattern", agent_mode="offline", log=lambda *_: None)


@pytest.fixture
def ids(demo):
    from claimcheck import service
    return {c["external_id"]: c["id"] for c in service.claimant_summary()}
