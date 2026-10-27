"""Command line entry points.

    python -m claimcheck.pipeline demo                    # synthetic data -> CMS pull -> extract -> qualify -> agent -> eval
    python -m claimcheck.pipeline demo --method llm       # same, extraction + agent with the configured LLM (Ollama)
    python -m claimcheck.pipeline process CC-1003         # re-run one claimant
    python -m claimcheck.pipeline watch                   # folder intake: data/inbox/<matter id>/*.pdf
    python -m claimcheck.pipeline eval
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from sqlalchemy import select

from claimcheck import config, evaluate, service, synth
from claimcheck.agent import run_agent
from claimcheck.db import Claimant, init_db, session_scope
from claimcheck.ingest import get_or_create_claimant, ingest_folder
from claimcheck.integrations import cms, mock_cms


def build_demo(n: int = 24, method: str = "auto", agent_mode: str = "auto", regenerate: bool = True,
               log=print) -> dict:
    t0 = time.time()
    if regenerate or not (config.DOCS_DIR / "gold.json").exists():
        synth.generate(n)
        log(f"generated {n} synthetic claimants")
    init_db(drop=True)
    mock_cms.seed_from_gold()
    client = cms.in_process_client()
    log(f"CMS pull: {client.pull()}")
    with session_scope() as s:
        ids = [(c.id, c.external_id) for c in s.scalars(select(Claimant).order_by(Claimant.id))]
    for cid, ext in ids:
        r = service.process_claimant(cid, method)
        log(f"{ext}: {r['status']:<14} facts={r['extraction']['facts']} verified={r['extraction']['verified']}")
    for cid, ext in ids:
        with session_scope() as s:
            status = s.get(Claimant, cid).status
        if status == "needs_review":
            run_agent(cid, agent_mode)
            log(f"{ext}: agent run complete")
    log(f"CMS push: {client.push()}")
    res = evaluate.evaluate()
    log(f"status accuracy {res['status_accuracy']:.0%}, fact F1 (verified) "
        f"{res['facts']['verified']['overall']['f1']:.2f}, false qualifications {len(res['false_qualifications'])}, "
        f"{time.time() - t0:.0f}s")
    return res


def watch(interval: float = 5.0) -> None:
    """Poll the inbox: any PDF dropped into inbox/<matter id>/ is ingested and the claimant re-qualified."""
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    print(f"watching {config.INBOX_DIR} (Ctrl+C to stop)")
    while True:
        for folder in sorted(p for p in config.INBOX_DIR.iterdir() if p.is_dir()):
            cid = get_or_create_claimant(folder.name, folder.name)
            if ingest_folder(cid, folder):
                print(f"{folder.name}: new documents -> {service.process_claimant(cid)['status']}")
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(prog="claimcheck")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--n", type=int, default=24)
    d.add_argument("--method", choices=["auto", "pattern", "llm", "hybrid"], default="auto")
    d.add_argument("--agent", choices=["auto", "offline", "llm"], default="auto")
    d.add_argument("--keep-data", action="store_true", help="reuse existing synthetic PDFs")
    p = sub.add_parser("process")
    p.add_argument("external_id")
    p.add_argument("--method", choices=["auto", "pattern", "llm", "hybrid"], default="auto")
    sub.add_parser("watch")
    sub.add_parser("eval")
    a = ap.parse_args()
    if a.cmd == "demo":
        build_demo(a.n, a.method, a.agent, regenerate=not a.keep_data)
    elif a.cmd == "process":
        with session_scope() as s:
            cid = s.scalar(select(Claimant.id).where(Claimant.external_id == a.external_id))
        print(service.process_claimant(cid, a.method))
    elif a.cmd == "watch":
        watch()
    else:
        print(evaluate.evaluate())


if __name__ == "__main__":
    main()
