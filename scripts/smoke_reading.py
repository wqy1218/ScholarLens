"""Reading position, snapshot, split and survey smoke test (temporary data directory)."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="sl_reading_")
os.environ["SCHOLARLENS_DATA_DIR"] = TMP

from server import store  # noqa: 
from server import assign as _assign  # noqa: 


def _fake_embed(texts):
    out = []
    for t in texts:
        h = hashlib.sha256((t or "").encode("utf-8")).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


_assign.embed = _fake_embed

OK = []


def check(name, cond, detail=""):
    OK.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + name +
          (("  " + str(detail)[:300]) if detail and not cond else ""))


def migrate_legacy_reading():
    db = os.path.join(TMP, "legacy.db")
    con = sqlite3.connect(db)
    con.executescript("""
 CREATE TABLE interactions (
 interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
 domain_id INTEGER, paper_id INTEGER,
 type TEXT, anchor_json TEXT DEFAULT '', content TEXT DEFAULT '',
 evidence_json TEXT DEFAULT '',
 anchor_id INTEGER, domains_json TEXT DEFAULT '',
 importance INTEGER DEFAULT 0,
 created_at TEXT, updated_at TEXT, deleted_at TEXT
 );
 INSERT INTO interactions(paper_id, type, anchor_json, content, created_at)
 VALUES (1, 'note', '', 'legacy note', '2026-01-01');
 PRAGMA user_version=4;
    """)
    con.commit()
    con.close()
    store.connect(db)
    ver = store.query("PRAGMA user_version")[0][0]
    row = store.query("SELECT anchor_json FROM interactions WHERE type='note'")[0]
    cols = {r["name"] for r in store.query("PRAGMA table_info(paper_domain_links)")}
    dcols = {r["name"] for r in store.query("PRAGMA table_info(domains)")}
    check("migrate: user_version 5", ver == 6, ver)
    check("migrate: empty anchor_json normalised to {}", row["anchor_json"] == "{}", row)
    check("migrate: reading position columns", {"last_page", "scroll_ratio"} <= cols, cols)
    check("migrate: domains.last_paper_id", "last_paper_id" in dcols, dcols)
    check("migrate: survey_drafts table",
          bool(store.query("SELECT 1 FROM sqlite_master WHERE name='survey_drafts'")))
    store._conn.close()


migrate_legacy_reading()

from fastapi.testclient import TestClient  # noqa: 

from server import api  # noqa: (connects the app DB)

c = TestClient(api.app)

d1 = store.create_domain("SM104 Alpha")
d2 = store.create_domain("SM104 Beta")
p1 = store.upsert_paper(d1, "SM104 Paper One", None, arxiv_id="2401.90001", year=2023)
p2 = store.upsert_paper(d2, "SM104 Paper Two", None, arxiv_id="2401.90002", year=2024)
store.set_paper_domain(p1, d1)
store.set_paper_domain(p2, d2)
for pid, title in ((p1, "SM104 Paper One"), (p2, "SM104 Paper Two")):
    store.save_summary(pid, {"five": [
        {"section": "研究问题", "text": f"RQ {pid}"},
        {"section": "方法路径", "text": f"Method {pid}"},
        {"section": "核心成果", "text": f"Result {pid}"},
        {"section": "适用边界与局限", "text": f"Limitation {pid}"}]})

r = c.post(f"/papers/{p1}/reading",
           json={"domain_id": d1, "page": 5, "scroll_ratio": 0.4, "status": "reading"})
check("reading: save position 200", r.status_code == 200, r.text[:200])
detail = c.get(f"/papers/{p1}").json()
check("reading: detail carries position",
      (detail.get("position") or {}).get("last_page") == 5, detail.get("position"))
check("reading: detail carries per-domain positions",
      any(x["domain_id"] == d1 for x in detail.get("positions") or []), detail.get("positions"))
ov = c.get(f"/domains/{d1}/overview").json()
check("reading: overview last_view", (ov.get("last_view") or {}).get("paper_id") == p1,
      ov.get("last_view"))
check("reading: overview last_view page", (ov.get("last_view") or {}).get("last_page") == 5,
      ov.get("last_view"))

note_id = store.add_note(p1, d1, 1, "snapshot quote", 0, "snapshot note", ["方法"])
store.execute("INSERT INTO questions(paper_id, domain_id, text, status, created_at)"
              " VALUES (?,?,?,'open',?)", (p1, d1, "snapshot open question", store.now()))
store.execute("INSERT INTO citations(source_paper_id, target_title, status, created_at)"
              " VALUES (?,?, 'unresolved', ?)", (p1, "some external ref", store.now()))
cid = store.query("SELECT citation_id FROM citations WHERE source_paper_id=?", (p1,))[0]["citation_id"]
store.execute("INSERT INTO review_items(citation_id, paper_id, domain_id, reason, status,"
              " created_at) VALUES (?,?,?,?,'pending',?)",
              (cid, p1, d1, "unresolved", store.now()))
store.set_domain_config(d1, {"highlight_density": "0"})
snap = c.post(f"/domains/{d1}/snapshots", json={"name": "sm104"}).json()
sid = snap["snapshot_id"]
check("snapshot: created", bool(sid), snap)

c.post(f"/papers/{p1}/reading", json={"domain_id": d1, "page": 12, "scroll_ratio": 0.9,
                                      "status": "done"})
store.set_domain_config(d1, {"highlight_density": "5"})
c.delete(f"/papers/{p1}/notes/{note_id}")
c.delete(f"/papers/{p1}/domains/{d1}")
store.execute("UPDATE questions SET status='resolved' WHERE domain_id=?", (d1,))

res = c.post(f"/domains/{d1}/snapshots/{sid}/restore", json={}).json()
check("restore: link re-added", res.get("links_added", 0) >= 1, res)
check("restore: membership back",
      any(l["domain_id"] == d1 for l in store.list_paper_domains(p1)),
      store.list_paper_domains(p1))
check("restore: position back", store.get_reading_position(p1, d1)["last_page"] == 5,
      store.get_reading_position(p1, d1))
check("restore: reading state back",
      [l for l in store.list_paper_domains(p1) if l["domain_id"] == d1][0]["reading_status"]
      == "reading", store.list_paper_domains(p1))
check("restore: config back", c.get(f"/domains/{d1}/config").json()["highlight_density"] == "0",
      c.get(f"/domains/{d1}/config").json())
qs = c.get(f"/domains/{d1}/questions").json()
check("restore: open question re-opened",
      any(q["text"] == "snapshot open question" and q["status"] == "open" for q in qs), qs)
rs = c.get(f"/domains/{d1}/reviews").json()
check("restore: pending review back", bool(rs), rs)
notes_after = [n for n in c.get(f"/papers/{p1}/notes").json() if n.get("text") == "snapshot note"]
check("restore: notes NOT resurrected by default", not notes_after, notes_after)
res2 = c.post(f"/domains/{d1}/snapshots/{sid}/restore", json={"include_notes": True}).json()
check("restore: include_notes restores the note", res2.get("notes", 0) >= 1, res2)
notes_after = [n for n in c.get(f"/papers/{p1}/notes").json() if n.get("text") == "snapshot note"]
check("restore: note visible again", bool(notes_after), notes_after)

ds = store.create_domain("SM104 Split Src")
sp1 = store.upsert_paper(ds, "SM104 Split A", None)
sp2 = store.upsert_paper(ds, "SM104 Split B", None)
store.set_paper_domain(sp1, ds)
store.set_paper_domain(sp2, ds)
store.add_note(sp1, ds, 1, "split quote", 0, "split note", [])
store.execute("INSERT INTO questions(paper_id, domain_id, text, status, created_at)"
              " VALUES (?,?,?,'open',?)", (sp1, ds, "split question", store.now()))
r = c.post(f"/domains/{ds}/split", json={"name": "SM104 Split Dst", "paper_ids": [sp1]})
check("split: 200", r.status_code == 200, r.text[:200])
new_id = r.json()["domain_id"]
check("split: source link removed",
      not any(l["domain_id"] == ds for l in store.list_paper_domains(sp1)),
      store.list_paper_domains(sp1))
check("split: moved to new domain",
      any(l["domain_id"] == new_id for l in store.list_paper_domains(sp1)))
check("split: primary follows", store.get_paper(sp1)["domain_id"] == new_id)
check("split: unselected stays", store.get_paper(sp2)["domain_id"] == ds)
check("split: source paper list shrinks",
      [x["paper_id"] for x in store.list_papers(ds)] == [sp2],
      [x["paper_id"] for x in store.list_papers(ds)])
moved_notes = [n for n in c.get(f"/papers/{sp1}/notes").json() if n.get("text") == "split note"]
check("split: note followed the paper", bool(moved_notes), moved_notes)
moved_q = c.get(f"/domains/{new_id}/questions").json()
check("split: question followed the paper", any(q["text"] == "split question" for q in moved_q),
      moved_q)

store.execute("INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content,"
              " created_at) VALUES (?,?,'qa',?,?,?)",
              (d1, p1, json.dumps({"session_id": 987}), json.dumps({"question": "q", "answer": "a"}),
               store.now()))
store.execute("INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content,"
              " created_at) VALUES (?,?,'note','','legacy empty anchor note',?)",
              (d1, p1, store.now()))
r = c.delete(f"/papers/{d1}/qa/sessions/987")
check("session delete with empty anchor survives", r.status_code == 200, r.text[:200])
qid = store.execute("INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content,"
                    " created_at) VALUES (?,?,'qa',?,?,?)",
                    (d1, p1, json.dumps({"session_id": 988}),
                     json.dumps({"question": "q2", "answer": "a2"}), store.now())).lastrowid
r = c.post(f"/papers/{p1}/qa/turns/{qid}/important", json={"important": True})
check("important with empty anchor survives", r.status_code == 200, r.text[:200])
check("overview survives empty anchor", c.get("/overview").status_code == 200)

store.merge_paper_meta(p1, {"sections": [
    {"title": "1 Introduction", "level": 1, "page": 1},
    {"title": "1.1 Motivation", "level": 2, "page": 1},
    {"title": "2 Method", "level": 1, "page": 2}],
    "figures": [{"page": 2, "num": "1", "label": "Figure 1", "caption": "cap",
                 "x0": 10, "y0": 20, "x1": 200, "y1": 150, "w": 600, "h": 800}]})
out = c.get(f"/papers/{p1}/outline").json()
tree = out.get("section_tree") or []
check("C5: section_tree depth", len(tree) == 2 and len(tree[0].get("children") or []) == 1, tree)
check("C5: figure count exposed", out.get("n_figures") == 1, out.get("n_figures"))

r = c.post(f"/domains/{d1}/survey", json={
    "domain_ids": [d1, new_id], "group_by": "topic", "include_gaps": True, "save": True})
check("F2: survey 200", r.status_code == 200, r.text[:300])
body = r.json()
check("F2: cross-domain selection", body["stats"]["domains"] == sorted([d1, new_id])
      or set(body["stats"]["domains"]) == {d1, new_id}, body["stats"])
md = body.get("markdown") or ""
check("F2: markdown cites papers", "[P" in md and "## 参考" in md, md[:200])
check("F2: research gaps section", "## 研究空白" in md)
check("F2: gaps counted", body["stats"]["gaps"] >= 1, body["stats"])
check("F2: draft persisted", bool(body.get("survey_id")))
sid2 = body["survey_id"]
check("F2: draft readable", c.get(f"/surveys/{sid2}").json().get("markdown") == md)
check("F2: draft listed", any(x["survey_id"] == sid2 for x in c.get(f"/domains/{d1}/surveys").json()))
r = c.put(f"/surveys/{sid2}", json={"markdown": "edited draft"})
check("F2: draft editable", r.status_code == 200 and
      c.get(f"/surveys/{sid2}").json()["markdown"] == "edited draft", r.text[:160])
r = c.post(f"/domains/{d1}/survey", json={"domain_ids": [d1], "group_by": "method"})
check("F2: method comparison table", "| 论文 | 年份 | 研究问题 |" in r.json()["markdown"])
r = c.post(f"/domains/{d1}/survey", json={"domain_ids": [d1], "group_by": "year"})
check("F2: year grouping", "## 2" in r.json()["markdown"] or "未标注年份" in r.json()["markdown"])
r = c.get(f"/domains/{d1}/survey?group_by=year")
check("F2: GET preview still works", r.status_code == 200 and "文献综述草稿" in r.json()["markdown"],
      r.text[:160])
check("F2: draft delete", c.delete(f"/surveys/{sid2}").status_code == 200 and
      c.get(f"/surveys/{sid2}").status_code == 404)

check("integrity: still ok", store.integrity_report()["ok"], store.integrity_report())
check("migrate: op log written", bool(store.query(
    "SELECT 1 FROM operation_log WHERE op='schema.migrate'")))

print("\nRESULT:", "ALL OK" if all(OK) else f"{OK.count(False)} FAILED", f"({len(OK)} checks)")
sys.exit(0 if all(OK) else 1)
