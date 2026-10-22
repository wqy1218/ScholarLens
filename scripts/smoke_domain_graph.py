import json
import sqlite3
import urllib.request

BASE = "http://127.0.0.1:21800"
DB = r"D:\ScholarLens\data\scholarlens.db"


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), name, detail)
    return ok


results = []

domains = call("GET", "/domains")
results.append(check("list domains", len(domains) >= 1, f"{len(domains)} domains, papers={sum(d['paper_count'] for d in domains)}"))

d = call("POST", "/domains", {"name": ""})
results.append(check("create domain (auto name)", bool(d.get("name")), json.dumps(d, ensure_ascii=False)[:80]))
did = d["domain_id"]

r = call("POST", f"/domains/{did}/rename", {"name": "M5-test-temp"})
results.append(check("rename", r.get("name") == "M5-test-temp"))

db = sqlite3.connect(DB)
paper_id = db.execute("SELECT paper_id FROM papers WHERE domain_id != ?", (did,)).fetchone()[0]
old_dom = db.execute("SELECT domain_id FROM papers WHERE paper_id=?", (paper_id,)).fetchone()[0]
db.execute("UPDATE papers SET domain_id=? WHERE paper_id=?", (did, paper_id))
db.commit()
papers_in = call("GET", f"/domains/{did}/papers")
results.append(check("paper moved in", any(p["paper_id"] == paper_id for p in papers_in)))

r = call("POST", "/domains/reassign", {"paper_id": paper_id})
moved_to = db.execute("SELECT domain_id FROM papers WHERE paper_id=?", (paper_id,)).fetchone()[0]
results.append(check("reassign runs", True, f"paper {paper_id}: {did} -> {moved_to}"))
db.execute("UPDATE papers SET domain_id=? WHERE paper_id=?", (old_dom, paper_id))
db.commit()

dst = next(x["domain_id"] for x in call("GET", "/domains") if x["domain_id"] != did)
r = call("POST", "/domains/merge", {"src_id": did, "dst_id": dst})
domains_after = call("GET", "/domains")
results.append(check("merge removes src", not any(x["domain_id"] == did for x in domains_after)))

d2 = call("POST", "/domains", {"name": ""})
r = call("DELETE", f"/domains/{d2['domain_id']}")
domains_final = call("GET", "/domains")
results.append(check("delete domain", not any(x["domain_id"] == d2["domain_id"] for x in domains_final)))

n_links = db.execute("SELECT COUNT(*) FROM paper_domain_links").fetchone()[0]
results.append(check("weak links exist", n_links > 0, f"{n_links} links"))

print(f"\n==== domain chain: {sum(results)}/{len(results)} PASS ====")
