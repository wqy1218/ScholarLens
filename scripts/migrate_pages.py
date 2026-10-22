"""Recompute chunk pages, highlight anchor pages and summary evidence pages from the PDFs.

Deterministic: extract() reproduces identical start_char/end_char offsets, so
chunks map back by offset; quotes and evidence map back by paragraph text search.

Dry-run by default: python scripts/migrate_pages.py
Apply: python scripts/migrate_pages.py --apply
"""
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from server import pdfio

ROOT = os.path.join(os.path.dirname(__file__), "..")
DB = os.path.join(ROOT, "data", "scholarlens.db")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _canon(s: str) -> str:
    s = s.lower().replace("\ufb01", "fi").replace("\ufb02", "fl")
    return re.sub(r"[^a-z0-9]", "", s)


def _find_para_page(paras: list[dict], quote: str) -> int | None:
    """Locate quote in paragraph texts; return its 1-based page."""
    nq = _norm(quote)
    cq = _canon(quote)
    if len(cq) < 10:
        return None
    for p in paras:
        if nq and nq in _norm(p["text"]):
            return p["page"]
    for p in paras:
        if cq in _canon(p["text"]):
            return p["page"]
    return None


def _chunk_page(paras: list[dict], start_char: int) -> int | None:
    """Page of the paragraph containing chunk start offset (fallback: last para before it)."""
    best = None
    for p in paras:
        if p["start_char"] <= start_char < p["end_char"]:
            return p["page"]
        if p["start_char"] <= start_char:
            best = p["page"]
    return best


def main(apply: bool) -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    papers = db.execute("SELECT paper_id, file_path FROM papers WHERE parse_status='done'").fetchall()
    stats = {"papers": 0, "chunks": 0, "highlights": 0, "hl_unfound": 0,
             "evidence": 0, "ev_unfound": 0, "pdf_missing": 0}

    for p in papers:
        pdf = os.path.join(ROOT, p["file_path"].replace("\\", "/"))
        if not os.path.exists(pdf):
            stats["pdf_missing"] += 1
            continue
        ex = pdfio.extract(pdf)
        paras = ex["paras"]
        stats["papers"] += 1

        for row in db.execute("SELECT chunk_id, start_char, page FROM chunks WHERE paper_id=?", (p["paper_id"],)):
            new_page = _chunk_page(paras, row["start_char"])
            if new_page and new_page != row["page"]:
                stats["chunks"] += 1
                if apply:
                    db.execute("UPDATE chunks SET page=? WHERE chunk_id=?", (new_page, row["chunk_id"]))

        for row in db.execute(
                "SELECT interaction_id, anchor_json FROM interactions WHERE type='highlight' AND paper_id=?",
                (p["paper_id"],)):
            a = json.loads(row["anchor_json"])
            new_page = _find_para_page(paras, a.get("quote", ""))
            if new_page:
                if new_page != a.get("page"):
                    stats["highlights"] += 1
                    if apply:
                        a["page"] = new_page
                        db.execute("UPDATE interactions SET anchor_json=? WHERE interaction_id=?",
                                   (json.dumps(a, ensure_ascii=False), row["interaction_id"]))
            else:
                stats["hl_unfound"] += 1

        meta_row = db.execute("SELECT meta_json FROM papers WHERE paper_id=?", (p["paper_id"],)).fetchone()
        meta = json.loads(meta_row["meta_json"] or "{}")
        changed = False
        for sec in (meta.get("summaries", {}) or {}).get("five", []):
            ev = sec.get("evidence")
            if not ev:
                continue
            new_page = _find_para_page(paras, ev)
            if new_page:
                if new_page != sec.get("page"):
                    stats["evidence"] += 1
                    if apply:
                        sec["page"] = new_page
                        changed = True
            else:
                stats["ev_unfound"] += 1
        if apply:
            if changed:
                db.execute("UPDATE papers SET meta_json=? WHERE paper_id=?",
                           (json.dumps(meta, ensure_ascii=False), p["paper_id"]))
            db.commit()

    print(json.dumps(stats, ensure_ascii=False))
    print("APPLIED" if apply else "DRY-RUN (use --apply to write)")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
