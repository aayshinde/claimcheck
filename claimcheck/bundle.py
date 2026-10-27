"""Demo bundle: a precomputed run (database + PDFs + mock CMS state) committed to the repo, so a hosted dashboard
shows real LLM-extraction results without running a model.

    python -m claimcheck.bundle export --db data/claimcheck_hybrid.db     # after scripts/run_llm_evals.sh
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

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
    con = sqlite3.connect(BUNDLE / "claimcheck.db")
    for (path,) in con.execute("SELECT path FROM documents"):   # only files this run ingested
        src_pdf = Path(path)
        dst_pdf = BUNDLE / "cms_downloads" / src_pdf.parent.name / src_pdf.name
        dst_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src_pdf, dst_pdf)
    con.close()
    shutil.copy(config.DOCS_DIR / "gold.json", BUNDLE / "gold.json")
    # CMS state as it was after this run's push (the shared mock_cms.json may belong to a later run)
    con = sqlite3.connect(BUNDLE / "claimcheck.db")
    status = dict((e, (st, dec)) for e, st, dec in con.execute("SELECT external_id, status, decision FROM claimants"))
    con.close()
    gold = json.loads((config.DOCS_DIR / "gold.json").read_text())
    matters = {g["external_id"]: {"matter_id": g["external_id"], "client_name": g["name"], "dob": g["dob"],
                                  "case_code": config.DEFAULT_CASE, "intake_date": None,
                                  "claimcheck_status": status.get(g["external_id"], (None, None))[0],
                                  "attorney_decision": status.get(g["external_id"], (None, None))[1], "notes": []}
               for g in gold}
    (BUNDLE / "mock_cms.json").write_text(json.dumps({"matters": matters, "events": []}, indent=1))
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
