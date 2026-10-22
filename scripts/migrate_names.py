"""Rename stored PDFs to {arxiv_id|sha1[:10]}-{title-slug}.pdf. Safe to re-run.
Run: python scripts/migrate_names.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import store

store.connect(os.path.join("data", "scholarlens.db"))
renamed = kept = 0
for p in store.list_papers():
    full = store.get_paper(p["paper_id"])
    if not full["file_path"] or not os.path.isfile(full["file_path"]):
        continue
    final = os.path.join("data", "papers",
                         store.paper_filename(full["title"], full["arxiv_id"], full["sha1"] or ""))
    if os.path.abspath(full["file_path"]) == os.path.abspath(final):
        kept += 1
        continue
    n = 2
    while os.path.exists(final):
        final = final[:-4] + f"-{n}.pdf"
        n += 1
    os.rename(full["file_path"], final)
    store.execute("UPDATE papers SET file_path=? WHERE paper_id=?", (final, p["paper_id"]))
    renamed += 1
print(f"renamed={renamed} kept={kept}")
print("sample:", os.listdir("data/papers")[:5])
