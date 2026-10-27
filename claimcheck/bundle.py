"""Demo bundle: a precomputed run (database + PDFs + mock CMS state) committed to the repo, so a hosted dashboard
shows real LLM-extraction results without running a model.

    python -m claimcheck.bundle export --db data/claimcheck_hybrid.db     # after scripts/run_llm_evals.sh
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3

from claimcheck import config

BUNDLE = config.ROOT / "demo_bundle"


def export(db_path: str) -> None:
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    BUNDLE.mkdir()
    src, dst = sqlite3.connect(db_path), sqlite3.connect(BUNDLE / "claimcheck.db")
    src.backup(dst)                    # consistent copy even if the source is open
    dst.execute("VACUUM")
    src.close()
    dst.close()
    shutil.copytree(config.DATA_DIR / "cms_downloads", BUNDLE / "cms_downloads")
    shutil.copy(config.DOCS_DIR / "gold.json", BUNDLE / "gold.json")
    shutil.copy(config.DATA_DIR / "mock_cms.json", BUNDLE / "mock_cms.json")
    print(f"bundle written to {BUNDLE}")


def restore() -> bool:
    if not (BUNDLE / "claimcheck.db").exists():
        return False
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(BUNDLE / "claimcheck.db", config.DATA_DIR / "claimcheck.db")
    shutil.copytree(BUNDLE / "cms_downloads", config.DATA_DIR / "cms_downloads", dirs_exist_ok=True)
    shutil.copytree(BUNDLE / "cms_downloads", config.DOCS_DIR, dirs_exist_ok=True)   # mock CMS serves from here
    shutil.copy(BUNDLE / "gold.json", config.DOCS_DIR / "gold.json")
    shutil.copy(BUNDLE / "mock_cms.json", config.DATA_DIR / "mock_cms.json")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["export"])
    ap.add_argument("--db", required=True)
    export(ap.parse_args().db)
