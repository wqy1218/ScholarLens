"""Regression suite for schema, domain context and survey persistence (temporary data directory)."""
import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="sl_regression_")
os.environ["SCHOLARLENS_DATA_DIR"] = TMP

from server import store  # noqa: 


def _cleanup():
    try:
        store.close()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)


atexit.register(_cleanup)


def fake_embed(texts):
    out = []
    for t in texts:
        h = hashlib.sha256((t or "").encode("utf-8")).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


OK = []


def check(name, cond, detail=""):
    OK.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + name +
          (("  " + str(detail)[:300]) if detail and not cond else ""))


def legacy_fixture():
    db = os.path.join(TMP, "legacy.db")
    con = sqlite3.connect(db)
    con.executescript("""
 CREATE TABLE survey_drafts (
 survey_id INTEGER PRIMARY KEY AUTOINCREMENT,
 domain_id INTEGER,
 domain_ids_json TEXT DEFAULT '[]',
 paper_ids_json TEXT DEFAULT '[]',
 group_by TEXT DEFAULT 'topic',
 title TEXT DEFAULT '',
 markdown TEXT DEFAULT '',
 stats_json TEXT DEFAULT '{}',
 created_at TEXT, updated_at TEXT
 );
 INSERT INTO survey_drafts(domain_id, title, markdown) VALUES (NULL, 'keep', '# keep');
 INSERT INTO survey_drafts(domain_id, title, markdown) VALUES (999, 'orphan', '# orphan');
 PRAGMA user_version=5;
    """)
    con.commit()
    con.close()
    store.connect(db)
    ver = store.query("PRAGMA user_version")[0][0]
    fk = [dict(r) for r in store.query("PRAGMA foreign_key_list(survey_drafts)")]
    rows = store.query("SELECT domain_id, title FROM survey_drafts ORDER BY survey_id")
    check("legacy: user_version 6", ver == 6, ver)
    check("legacy: survey_drafts has a domains foreign key",
          any(r["table"] == "domains" for r in fk), fk)
    check("legacy: survey index recreated",
          bool(store.query("SELECT 1 FROM sqlite_master"
                           " WHERE type='index' AND name='idx_survey_domain'")))
    check("legacy: orphan draft dropped, NULL kept",
          [r["title"] for r in rows] == ["keep"], [dict(r) for r in rows])

    did = store.create_domain("Legacy-D")
    sid = store.save_survey_draft(did, [did], [], "topic", "d", "# d")
    store.delete_domain(did)
    check("legacy: domain delete removes survey draft",
          store.get_survey_draft(sid) is None)
    legacy_cols = [r["name"] for r in store.query("PRAGMA table_info(survey_drafts)")]
    store.close()
    return db, legacy_cols


LEGACY_DB, LEGACY_COLS = legacy_fixture()

from server import api  # noqa: 
from server import assign as _assign  # noqa: 
from server import summarize  # noqa: 
from server import survey  # noqa: 

_assign.embed = fake_embed
from fastapi.testclient import TestClient  # noqa: 

client = TestClient(api.app)

cols = {
    "papers": {r["name"] for r in store.query("PRAGMA table_info(papers)")},
    "domains": {r["name"] for r in store.query("PRAGMA table_info(domains)")},
    "paper_domain_links": {r["name"] for r in store.query("PRAGMA table_info(paper_domain_links)")},
    "qa_sessions": {r["name"] for r in store.query("PRAGMA table_info(qa_sessions)")},
}
check("schema: current user_version", store.query("PRAGMA user_version")[0][0] == 6)
check("schema: papers.progress/last_opened_at declared",
      {"progress", "last_opened_at"} <= cols["papers"])
check("schema: domains.discovery_sig/last_paper_id declared",
      {"discovery_sig", "last_paper_id"} <= cols["domains"])
check("schema: links.last_page/scroll_ratio declared",
      {"last_page", "scroll_ratio"} <= cols["paper_domain_links"])
check("schema: qa_sessions.paper_ids_json declared",
      "paper_ids_json" in cols["qa_sessions"])

server_text = ""
for name in os.listdir(os.path.join(ROOT, "server")):
    if name.endswith(".py"):
        server_text += open(os.path.join(ROOT, "server", name), encoding="utf-8").read()
check("network: no hard-coded hf-mirror endpoint", "hf-mirror" not in server_text)
check("assets: dead splash page removed",
      not os.path.exists(os.path.join(ROOT, "app", "renderer", "splash.html")))

icon_out = os.path.join(TMP, "icons")
env = dict(os.environ, SCHOLARLENS_ICON_OUT=icon_out)
icon_run = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "make_icons.py")],
                          cwd=ROOT, env=env, capture_output=True, text=True)
ok_icon = icon_run.returncode == 0
sizes = set()
ico = os.path.join(icon_out, "icon.ico")
if ok_icon and os.path.isfile(ico):
    blob = open(ico, "rb").read()
    count = struct.unpack_from("<H", blob, 4)[0]
    for i in range(count):
        sizes.add(blob[6 + 16 * i] or 256)
check("assets: make_icons writes a 256 px ICO frame", ok_icon and 256 in sizes, sizes)

missing = os.path.join(TMP, "does_not_exist.db")
r_bad = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_integrity.py"),
                        missing], cwd=ROOT, capture_output=True, text=True)
check("integrity: missing db path exits 2", r_bad.returncode == 2, r_bad.stdout[-200:])
check("integrity: missing db path creates no file", not os.path.exists(missing))
r_ok = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_integrity.py"),
                       LEGACY_DB], cwd=ROOT, capture_output=True, text=True)
check("integrity: real db exits 0", r_ok.returncode == 0, r_ok.stdout[-200:])

check("glossary: claim is atomic",
      api._glossary_claim(4242) is True
      and api._glossary_claim(4242) is False)
api._glossary_release(4242)
check("glossary: claim released", api._glossary_claim(4242) is True)
api._glossary_release(4242)

a = client.post("/domains", json={"name": "Domain-A"}).json()["domain_id"]
b = client.post("/domains", json={"name": "Domain-B"}).json()["domain_id"]
c = client.post("/domains", json={"name": "Domain-C"}).json()["domain_id"]
for did, goal in ((a, "goal-A"), (b, "goal-B"), (c, "goal-C")):
    assert client.put(f"/domains/{did}/goal", json={"research_goal": goal}).status_code == 200

pid = store.upsert_paper(a, "Cross Listed Paper", None, arxiv_id="105.00001",
                         abstract="cross domain abstract", year=2026)
store.add_paper_domain(pid, b)
store.add_paper_domain(pid, c)
store.replace_chunks(pid, a, [
    {"section": "1 Introduction", "page": 1, "start_char": 0, "end_char": 99,
     "text": "Introduction text about the problem."},
    {"section": "2 Method", "page": 1, "start_char": 100, "end_char": 199,
     "text": "Method text about the approach."},
    {"section": "3 Results", "page": 2, "start_char": 200, "end_char": 299,
     "text": "Results text about the findings."},
])
pv = int(store.get_paper(pid)["parse_version"])
store.save_summary(pid, {"five": [{"section": "研究问题", "text": "A-goal-body"}]},
                   research_goal="goal-A", parse_version=pv)
store.save_summary(pid, {"five": [{"section": "研究问题", "text": "B-goal-body"}]},
                   research_goal="goal-B", parse_version=pv)
store.merge_paper_meta(pid, {"summaries": {
    "five": [{"section": "研究问题", "text": "A-goal-body"}],
    "five_meta": {"research_goal": "goal-A"}}})

detail_b = client.get(f"/papers/{pid}?domain_id={b}").json()
check("domain: paper detail returns view domain", detail_b.get("view_domain_id") == b,
      detail_b.get("view_domain_id"))
check("domain: position is per view domain",
      detail_b.get("position", {}).get("domain_id") == b)

sum_b = client.get(f"/papers/{pid}/summary?domain_id={b}").json()
check("domain: summary uses current goal",
      sum_b["cache"]["research_goal"] == "goal-B" and sum_b["cache"]["hit"] is True)
check("domain: summary returns the B cache body",
      "B-goal-body" in json.dumps(sum_b["summaries"], ensure_ascii=False))
sum_c = client.get(f"/papers/{pid}/summary?domain_id={c}").json()
check("domain: stale summary flagged", sum_c["cache"]["stale"] is True
      and sum_c["cache"]["generated_for"] == "goal-A")

check("domain: skim uses current goal",
      client.get(f"/papers/{pid}/skim?domain_id={b}").json()["goal"] == "goal-B")
check("domain: suggestions use current goal",
      client.get(f"/papers/{pid}/qa/suggestions?domain_id={b}").json()["goal"] == "goal-B")
inferred = client.post(f"/papers/{pid}/qa/scope/infer",
                       json={"question": "这个方法的收敛性如何", "domain_id": b}).json()
check("domain: scope inference anchors on current domain",
      inferred.get("domain_ids") == [b] or "Domain-B" in inferred.get("label", ""),
      inferred)

exp_a = client.get(f"/papers/{pid}/export?domain_id={a}").json()["markdown"]
exp_b = client.get(f"/papers/{pid}/export?domain_id={b}").json()["markdown"]
check("domain: export follows the goal",
      "A-goal-body" in exp_a and "B-goal-body" in exp_b and "A-goal-body" not in exp_b)

sess_b = client.get(f"/papers/{pid}/qa/sessions?domain_id={b}").json()
sess_a = client.get(f"/papers/{pid}/qa/sessions?domain_id={a}").json()
check("domain: QA sessions are per domain",
      sess_b and sess_a and sess_b[0]["session_id"] != sess_a[0]["session_id"]
      and sess_b[0]["domain_id"] == b and sess_a[0]["domain_id"] == a)

card_b = client.put(f"/papers/{pid}/card",
                    json={"content": {"method": "card-B"}, "domain_id": b}).json()
check("domain: card saved under current domain", card_b["card"]["method"] == "card-B")
get_b = client.get(f"/papers/{pid}/card?domain_id={b}").json()
get_a = client.get(f"/papers/{pid}/card?domain_id={a}").json()
check("domain: card reads back per domain", get_b["saved"] is True and get_a["saved"] is False)

client.post(f"/papers/{pid}/highlights",
            json={"page": 1, "quote": "Method text", "category": "approach",
                  "domain_id": b})
hl_domain = store.query("SELECT domain_id FROM interactions WHERE paper_id=? AND type='highlight'"
                        " ORDER BY interaction_id DESC LIMIT 1", (pid,))[0]["domain_id"]
check("domain: manual highlight belongs to view domain", hl_domain == b, hl_domain)
hl_b = client.get(f"/papers/{pid}/highlights?domain_id={b}").json()
hl_a = client.get(f"/papers/{pid}/highlights?domain_id={a}").json()
check("domain: highlights read back per domain", len(hl_b) == 1 and hl_a == [], (hl_b, hl_a))
client.post(f"/papers/{pid}/notes",
            json={"page": 1, "quote": "Method text", "text": "note in B", "domain_id": b})
note_domain = store.query("SELECT domain_id FROM interactions WHERE paper_id=? AND type='note'"
                          " ORDER BY interaction_id DESC LIMIT 1", (pid,))[0]["domain_id"]
check("domain: note belongs to view domain", note_domain == b, note_domain)

import server.llm as llm_mod  # noqa: 
_orig_chat = llm_mod.chat


def fake_chat(**kwargs):
    return {"content": json.dumps([{"section": "研究问题", "text": "C-goal-body",
                                    "evidence": ""}], ensure_ascii=False),
            "model_served": "stub", "latency_ms": 1}


llm_mod.chat = fake_chat
try:
    summarize.generate(pid, "five", research_goal="goal-C")
finally:
    llm_mod.chat = _orig_chat
check("domain: summary generation stores under requested goal",
      store.get_summary(pid, "five", "goal-C", pv) is not None)

snap = client.post(f"/domains/{a}/snapshots", json={"name": "s1"}).json()["snapshot_id"]
bad = client.post(f"/domains/{b}/snapshots/{snap}/restore", json={})
check("snapshot: restore via another domain rejected", bad.status_code == 400, bad.text[:200])
good = client.post(f"/domains/{a}/snapshots/{snap}/restore", json={})
check("snapshot: restore via owning domain works", good.status_code == 200, good.text[:200])

res = survey.build([b], None, "topic", True, False, False)
check("survey: title uses selected domain", res["title"] == "Domain-B", res["title"])
check("survey: paper grouped under selected domain",
      "## Domain-B" in res["markdown"] and "## Domain-A" not in res["markdown"])
check("survey: paper block attributed to selected domain",
      "· Domain-B" in res["markdown"])

d = client.post("/domains", json={"name": "Domain-D"}).json()["domain_id"]
sid = store.save_survey_draft(d, [d], [], "topic", "draft", "# draft")
assert client.delete(f"/domains/{d}").status_code == 200
check("survey: deleting a domain removes its draft", store.get_survey_draft(sid) is None)

m1 = client.post("/domains", json={"name": "Merge-Src"}).json()["domain_id"]
m2 = client.post("/domains", json={"name": "Merge-Dst"}).json()["domain_id"]
mid = store.save_survey_draft(m1, [m1, m2], [], "topic", "merge", "# merge")
merged = client.post("/domains/merge", json={"src_id": m1, "dst_id": m2})
draft = store.get_survey_draft(mid)
check("survey: merge keeps and re-owns draft",
      merged.status_code == 200 and draft is not None and draft["domain_id"] == m2
      and draft["domain_ids"] == [m2], draft)

fresh_cols = [r["name"] for r in store.query("PRAGMA table_info(survey_drafts)")]
check("schema: survey_drafts columns stable", fresh_cols == LEGACY_COLS,
      (fresh_cols, LEGACY_COLS))

print(f"\nRESULT: {'ALL OK' if all(OK) else 'FAILURES'} ({len(OK)} checks)")
if not all(OK):
    sys.exit(1)
