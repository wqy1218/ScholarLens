"""Import and parse pipeline smoke test (temporary data directory; arXiv network by default)."""
import hashlib
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

TMP = tempfile.mkdtemp(prefix="sl_import_")
os.environ["SCHOLARLENS_DATA_DIR"] = TMP
PAPERS = os.path.join(TMP, "papers")
os.makedirs(PAPERS, exist_ok=True)

from server import arxiv
from server import pdfio
from server import store

AID = "1706.03762"
LOCAL_PDF = os.environ.get("SCHOLARLENS_SMOKE_IMPORT_PDF")

try:
    store.connect(os.path.join(TMP, "smoke_test.db"))

    if LOCAL_PDF:
        print("1. metadata... (local mode)")
        path = os.path.abspath(LOCAL_PDF)
        with open(path, "rb") as f:
            sha1 = hashlib.sha1(f.read()).hexdigest()
        meta = {"title": os.path.splitext(os.path.basename(path))[0],
                "primary_category": "local", "year": None,
                "authors": "", "abstract": ""}
        print("   title:", meta["title"], "| sha1:", sha1[:12])
    else:
        print("1. metadata...")
        meta = arxiv.fetch_metadata([AID])[AID]
        print("   title:", meta["title"])
        print("   category:", meta["primary_category"], "| year:", meta["year"])

        print("2. download...")
        path, sha1 = arxiv.download_pdf(AID, PAPERS)
        print("   saved:", path, os.path.getsize(path), "bytes", "sha1:", sha1[:12])

    print("3. parse...")
    pid = store.upsert_paper(domain_id=None, title=meta["title"], file_path=path,
                             arxiv_id=None if LOCAL_PDF else AID, authors=meta["authors"],
                             abstract=meta["abstract"], year=meta["year"], sha1=sha1)
    result = pdfio.extract(path)
    print("   pages:", result["n_pages"], "| chars:", len(result["full_text"]),
          "| chunks:", len(result["chunks"]), "| links:", len(result["links"]),
          "| sections:", len(result["sections"]))

    p1 = [c for c in result["chunks"] if c["page"] == 1]
    print("   page-1 chunk count:", len(p1))
    if not LOCAL_PDF:
        ft = result["full_text"]
        i_abs, i_sec = ft.find("Abstract"), ft.find("Scaled Dot-Product")
        print("   Abstract offset:", i_abs, "| ScaledDotProduct offset:", i_sec,
              "-> left-before-right OK" if 0 < i_abs < i_sec else "-> ORDER PROBLEM")

    store.replace_chunks(pid, None, result["chunks"])
    n = store.query("SELECT COUNT(*) c FROM chunks")[0]["c"]
    print("4. chunks in db:", n)
    fts = store.query("SELECT text FROM chunks_fts WHERE chunks_fts MATCH 'attention' LIMIT 1")
    print("   FTS5 match 'attention':", "OK" if fts else "EMPTY")
    print("SMOKE OK")
finally:
    try:
        store.close()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)
