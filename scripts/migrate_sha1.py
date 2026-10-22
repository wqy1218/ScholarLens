"""Backfill papers.sha1 and rename stored PDFs to {sha1}.pdf. Safe to re-run.
Run: python scripts/migrate_sha1.py"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import store

store.connect(os.path.join("data", "scholarlens.db"))
migrated = skipped = 0
for p in store.list_papers():
    full = store.get_paper(p["paper_id"])
    if not full["file_path"] or not os.path.isfile(full["file_path"]):
        continue
    h = hashlib.sha1()
    with open(full["file_path"], "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    sha1 = h.hexdigest()
    rows = store.query("SELECT paper_id FROM papers WHERE sha1=? AND paper_id!=?", (sha1, p["paper_id"]))
    if rows:
        print(f"  paper {p['paper_id']} duplicates paper {rows[0]['paper_id']} — remove manually")
        skipped += 1
        continue
    dest = os.path.join("data", "papers", f"{sha1}.pdf")
    if os.path.abspath(full["file_path"]) != os.path.abspath(dest):
        if os.path.exists(dest):
            print(f"  {dest} already exists; dropping old copy {full['file_path']}")
            os.remove(full["file_path"])
        else:
            os.rename(full["file_path"], dest)
        store.execute("UPDATE papers SET file_path=? WHERE paper_id=?", (dest, p["paper_id"]))
    store.execute("UPDATE papers SET sha1=? WHERE paper_id=?", (sha1, p["paper_id"]))
    migrated += 1
print(f"migrated={migrated} skipped={skipped}")
