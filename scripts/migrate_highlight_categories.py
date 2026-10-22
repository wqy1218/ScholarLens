"""Sync system prompts and remove undersized automatic highlights."""
import json
import sqlite3
import sys

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])
from server.store import CONFIG_DEFAULTS

DB = r"D:\ScholarLens\data\scholarlens.db"
PROMPT_KEYS = ["prompt_summary", "prompt_qa", "prompt_highlight",
               "prompt_translate", "prompt_explain"]
MIN_CHARS, MIN_WORDS = 60, 8


def main(apply: bool) -> None:
    db = sqlite3.connect(DB)
    if apply:
        for k in PROMPT_KEYS:
            db.execute("INSERT OR REPLACE INTO config(key, value) VALUES (?,?)",
                       (k, CONFIG_DEFAULTS[k]))
        db.commit()
        print(f"prompts synced: {PROMPT_KEYS}")
    else:
        print("prompts (dry-run):", PROMPT_KEYS)

    rows = db.execute("SELECT interaction_id, anchor_json FROM interactions WHERE type='highlight'").fetchall()
    bad = []
    for iid, aj in rows:
        quote = json.loads(aj).get("quote", "")
        if len(quote) < MIN_CHARS or len(quote.split()) < MIN_WORDS:
            bad.append((iid, quote))
    print(f"highlights total={len(rows)}, substandard={len(bad)}")
    for iid, q in bad[:8]:
        print("  e.g.", iid, repr(q[:60]))
    if apply and bad:
        db.executemany("DELETE FROM interactions WHERE interaction_id=?", [(i,) for i, _ in bad])
        db.commit()
        print(f"deleted {len(bad)} substandard highlights")
    print("APPLIED" if apply else "DRY-RUN (use --apply)")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
