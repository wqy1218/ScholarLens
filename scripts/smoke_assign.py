"""Smoke test: embedding + automatic domain assignment.
Run: python scripts/smoke_assign.py"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import arxiv
from server import assign
from server import store

store.connect(os.path.join("data", "smoke_assign.db"))
store.execute("DELETE FROM chunks")
store.execute("DELETE FROM papers")
store.execute("DELETE FROM domains")
store.execute("DELETE FROM paper_domain_links")

PAPERS = ["1706.03762", "2005.11401"]

print("1. download metadata...")
metas = arxiv.fetch_metadata(PAPERS)
for aid in PAPERS:
    m = metas[aid]
    pid = store.upsert_paper(domain_id=None, title=m["title"], file_path="", arxiv_id=aid,
                             authors=m["authors"], abstract=m["abstract"], year=m["year"],
                             meta={"primary_category": m["primary_category"]})
    print(f"   {aid} -> paper_id={pid} [{m['primary_category']}] {m['title'][:50]}")

print("2. model load + assign (first call downloads weights ~130MB)...")
for p in store.list_papers():
    did = assign.assign_paper(p["paper_id"])
    d = store.get_domain(did)
    print(f"   paper '{p['title'][:40]}' -> domain '{d['name']}' (id={did})")

print("3. synthetic cross-domain paper (blockchain, unrelated to NLP)...")
pid = store.upsert_paper(
    domain_id=None,
    title="Sharded Blockchain Consensus via Two-Phase Atomic Commit",
    file_path=None,
    abstract="We study Byzantine fault tolerant consensus for sharded blockchains. "
             "Our protocol reduces cross-shard latency through atomic commit and "
             "verifiable random functions, achieving linear message complexity.",
    meta={"primary_category": "cs.DC"},
)
did = assign.assign_paper(pid)
d = store.get_domain(did)
print(f"   synthetic paper -> domain '{d['name']}' (id={did})")

print("4. state after assignment:")
for dd in store.list_domains():
    print(f"   domain '{dd['name']}': centroid={'yes' if dd['centroid'] else 'no'}")
links = store.query("SELECT * FROM paper_domain_links")
print(f"   weak links: {len(links)}")
for p in store.list_papers():
    print(f"   paper '{p['title'][:40]}' -> domain_id={p['domain_id']}")

domains = store.list_domains()
assert len(domains) == 2, f"expected 2 auto-created domains, got {len(domains)}"
papers = store.list_papers()
by_title = {p["title"]: p for p in papers}
att = next(p for t, p in by_title.items() if "Attention" in t)
rag = next(p for t, p in by_title.items() if "Retrieval-Augmented" in t)
bc = next(p for t, p in by_title.items() if "Blockchain" in t)
assert att["domain_id"] == rag["domain_id"], "two cs.CL papers should share a domain"
assert bc["domain_id"] != att["domain_id"], "blockchain paper should land in its own domain"
print("SMOKE OK")
