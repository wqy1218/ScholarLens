"""Settings, provider-profile and API-guard smoke test (temporary data directory)."""
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="sl_settings_")
os.environ["SCHOLARLENS_DATA_DIR"] = TMP

from server import store  # noqa: 

from server import assign as _assign  # noqa: 
import hashlib as _hashlib  # noqa: 


def _fake_embed(texts):
    out = []
    for t in texts:
        h = _hashlib.sha256((t or "").encode("utf-8")).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


_assign.embed = _fake_embed

OK = []


def check(name, cond, detail=""):
    OK.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + name +
          (("  " + str(detail)[:300]) if detail and not cond else ""))


def migrate_legacy_settings():
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
 INSERT INTO interactions(paper_id, type, content, created_at)
 VALUES (1, 'qa', '{"question":"q"}', '2026-01-01');
 PRAGMA user_version=3;
    """)
    con.commit()
    con.close()
    store.connect(db)
    ver = store.query("PRAGMA user_version")[0][0]
    fk = store.query("PRAGMA foreign_key_list(interactions)")
    cols = {r["name"] for r in store.query("PRAGMA table_info(interactions)")}
    rows = store.query("SELECT interaction_id, paper_id, type FROM interactions")
    check("migrate: user_version 5", ver == 6, ver)
    check("migrate: anchor_id foreign key added",
          any(r["from"] == "anchor_id" and r["table"] == "anchors" for r in fk), [dict(r) for r in fk])
    check("migrate: tags_json backfilled", "tags_json" in cols, cols)
    check("migrate: existing rows preserved",
          len(rows) == 1 and rows[0]["type"] == "qa", [dict(r) for r in rows])
    store._conn.close()


migrate_legacy_settings()

from server import api  # noqa: (connects to TMP/scholarlens.db)
from fastapi.testclient import TestClient  # noqa: 

c = TestClient(api.app)

LLM_ON = {"value": True}
api.llm.configured = lambda: LLM_ON["value"]


def fake_chat_stream(task, messages, model=None, max_tokens=0, temperature=0.2,
                     _retry=True, thinking=None, **kw):
    yield "结论一[1]。"
    yield "编造的引用[9]。"
    yield "__meta__" + json.dumps({"model_served": "stub", "finish_reason": "stop",
                                   "latency_ms": 1})


api.llm.chat_stream = fake_chat_stream
api.retrieval.retrieve = lambda paper_id, query, k=8, domain_id=None: (
    [{"chunk_id": 1, "text": "stub material", "page": 1, "score": 1.0}], 0.9)
api.retrieval.retrieve_domain = lambda domain_id, query, k=8, paper_ids=None: (
    [{"chunk_id": 1, "text": "stub material", "page": 1, "score": 1.0,
      "paper_id": 1, "title": "P"}], 0.9)
api.retrieval.retrieve_domains_scoped = lambda ids, query, k=8, paper_ids=None, weights=None: (
    [{"chunk_id": 1, "text": "stub material", "page": 1, "score": 1.0,
      "paper_id": 1, "title": "P"}], 0.9, [])


def mk_paper(domain_id, title="Paper", **kw):
    pid = store.upsert_paper(domain_id, title, None, **kw)
    store.set_parse_status(pid, "done")
    store.replace_chunks(pid, domain_id, [
        {"section": "Intro", "page": 1, "start_char": 0, "end_char": 20,
         "text": "attention is all you need"}])
    store.execute("UPDATE chunks SET vec=? WHERE paper_id=?", (b"\x00" * 16, pid))
    return pid


d1 = c.post("/domains", json={"name": "Merge A"}).json()["domain_id"]
d2 = c.post("/domains", json={"name": "Merge B"}).json()["domain_id"]
p = mk_paper(d1, "Merge Paper")
store.save_references(p, [{"dst_title": "Unknown Work", "status": "unresolved"}])
store.set_paper_domain(p, d2)
store.refresh_review_items(p)
store.add_question(p, d1, "same text?")
store.add_question(p, d2, "same text?")
store.add_paper_domain(p, d1, score=0.4)
before_domains = len(store.list_domains())
r = c.post("/domains/merge", json={"src_id": d1, "dst_id": d2})
check("merge: HTTP 200", r.status_code == 200, r.text[:300])
check("merge: source domain gone", not store.get_domain(d1))
check("merge: destination kept", bool(store.get_domain(d2)))
check("merge: paper primary points to dst", store.get_paper(p)["domain_id"] == d2)
check("merge: links only dst", [x["domain_id"] for x in store.list_paper_domains(p)] == [d2],
      store.list_paper_domains(p))
check("merge: duplicate question merged", len(store.list_questions(paper_id=p, status=None)) == 1,
      store.list_questions(paper_id=p, status=None))
check("merge: duplicate review merged", len(store.list_reviews(domain_id=d2, status="pending")) == 1,
      store.list_reviews(domain_id=d2, status="pending"))
check("merge: integrity still ok", store.integrity_report()["ok"], store.integrity_report())

lp = c.get(f"/domains/{d2}/papers").json()
row = next((x for x in lp if x["paper_id"] == p), {})
check("list: link_score/cited_by/pending_reviews present",
      {"link_score", "cited_by", "pending_reviews"} <= set(row), sorted(row))

card_content = {"title": "EDITED BY USER", "research_question": "RQ?",
                "method": "M", "findings": "F", "limitations": "L",
                "notes": ["n1"], "open_questions": ["q1"]}
check("card: save 200", c.put(f"/papers/{p}/card", json={"content": card_content}).status_code == 200)
got = c.get(f"/papers/{p}/card").json()
check("card: read-back saved flag", got["saved"] is True, got)
check("card: read-back edited content", got["card"]["research_question"] == "RQ?", got["card"])
check("card: markdown export", "研究问题" in c.get(f"/papers/{p}/card/export").json()["markdown"])
check("card: delete resets to generated", c.delete(f"/papers/{p}/card").status_code == 200
      and c.get(f"/papers/{p}/card").json()["saved"] is False)

r = c.post(f"/papers/{p}/highlights", json={
    "page": 1, "quote": "attention is all you need", "category": "background",
    "rects": [{"x": 10, "y": 20, "w": 100, "h": 12}], "occurrence": 0})
check("anchor: highlight created", r.status_code == 200, r.text[:200])
anchors = store.query("SELECT char_start, char_end, rects_json, parse_version FROM anchors"
                      " WHERE paper_id=?", (p,))
a = dict(anchors[0]) if anchors else {}
check("anchor: char offsets written", a.get("char_start") == 0 and a.get("char_end") == 25, a)
check("anchor: rects written", a.get("rects_json") not in (None, "", "[]"), a)
check("anchor: parse_version written", a.get("parse_version") == store.get_paper(p)["parse_version"], a)
r = c.post(f"/papers/{p}/notes", json={
    "page": 1, "quote": "attention is all you need", "text": "note",
    "tags": ["方法"], "rects": [{"x": 1, "y": 2, "w": 3, "h": 4}], "occurrence": 0})
check("anchor: note created", r.status_code == 200, r.text[:200])
note_anchor = store.query(
    "SELECT a.char_start, a.rects_json FROM interactions i JOIN anchors a ON a.anchor_id=i.anchor_id"
    " WHERE i.paper_id=? AND i.type='note'", (p,))
check("anchor: note shares offsets/rects",
      bool(note_anchor) and note_anchor[0]["char_start"] == 0
      and note_anchor[0]["rects_json"] != "[]", [dict(x) for x in note_anchor])

store.merge_paper_meta(p, {
    "sections": [{"title": "1 Introduction", "level": 1, "page": 1},
                 {"title": "2 Method", "level": 1, "page": 2}],
    "figures": [{"page": 2, "x0": 10, "y0": 20, "x1": 200, "y1": 120,
                 "w": 595, "h": 842, "num": "1", "label": "Figure 1",
                 "caption": "Figure 1: Architecture"}],
    "floats": [{"page": 2, "kind": "figure", "num": "1", "label": "Figure 1",
                "text": "Figure 1: Architecture"},
               {"page": 3, "kind": "table", "num": "2", "label": "Table 2",
                "text": "Table 2: Results"},
               {"page": 4, "kind": "algorithm", "num": "1", "label": "Algorithm 1",
                "text": "Algorithm 1: Training"}],
})
out = c.get(f"/papers/{p}/outline").json()
check("outline: sections", len(out["sections"]) == 2, out["sections"])
check("outline: figures with rect", bool(out["figures"]) and "x0" in out["figures"][0], out["figures"])
check("outline: tables/algorithms split",
      len(out["tables"]) == 1 and len(out["algorithms"]) == 1,
      (out["tables"], out["algorithms"]))

d3 = c.post("/domains", json={"name": "Concept A"}).json()["domain_id"]
d4 = c.post("/domains", json={"name": "Concept B"}).json()["domain_id"]
d5 = c.post("/domains", json={"name": "Concept C"}).json()["domain_id"]
cid = store.upsert_concept("attention", "注意力", "def A", d3)
store.upsert_concept("attention", "注意力", "def B", d4)
store.add_concept_mapping(cid, d3, d5, "different")
ctx = store.concept_context([d3, d4], "how does attention work")
check("concept: unrelated pair has no mapping",
      ctx and not ctx[0]["mappings"], ctx)
store.add_concept_mapping(cid, d3, d4, "different")
ctx = store.concept_context([d3, d4], "how does attention work")
check("concept: matching pair carries mapping",
      ctx and len(ctx[0]["mappings"]) == 1 and ctx[0]["mappings"][0]["type"] == "different", ctx)

store.merge_paper_meta(p, {"summaries": {"five": [
    {"section": "研究问题", "text": "RQ body", "page": 1,
     "evidence": "attention is all you need"}]}})
store.save_summary(p, {"five": [{"section": "研究问题", "text": "RQ body", "page": 1,
                                 "evidence": "attention is all you need"}]},
                   research_goal="", parse_version=store.get_paper(p)["parse_version"])
c.put(f"/domains/{d2}/goal", json={"research_goal": "找 idea", "core_papers": None})
summary = c.get(f"/papers/{p}/summary").json()
check("summary: goal change keeps sections via meta fallback",
      len((summary["summaries"] or {}).get("five", [])) == 1, summary["summaries"])
check("summary: export keeps sections after goal change",
      "研究问题" in c.get(f"/papers/{p}/export").json()["markdown"])
from server import summarize  # noqa: 
check("summary: goal hint is injected for 找 idea", "局限" in summarize._goal_note("找 idea"))

sess = c.get(f"/papers/{p}/qa/sessions").json()[0]
with c.stream("POST", f"/papers/{p}/qa",
              json={"question": "这篇论文讲了什么？", "scope": "paper",
                    "session_id": sess["session_id"]}) as resp:
    body = "".join(chunk for chunk in resp.iter_text())
check("qa: refs_check reports out-of-range [9]", '"invalid": [9]' in body, body[-500:])
check("qa: done carries interaction_id", '"interaction_id"' in body, body[-300:])
turn = store.query("SELECT interaction_id, content FROM interactions WHERE paper_id=?"
                   " AND type='qa' ORDER BY interaction_id DESC", (p,))
content = json.loads(turn[0]["content"]) if turn else {}
check("qa: invalid refs persisted", content.get("invalid_refs") == [9], content.get("invalid_refs"))
tid = turn[0]["interaction_id"] if turn else 0
check("qa: retract endpoint", c.delete(f"/papers/{p}/qa/turns/{tid}").status_code == 200)
check("qa: retracted turn gone",
      not store.query("SELECT 1 FROM interactions WHERE interaction_id=?", (tid,)))

qa_sid = store.qa_session_create(p, d2, "会话 1")
store.execute("INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content, created_at)"
              " VALUES (?,?,?,?,?,?)",
              (d2, p, "qa", json.dumps({"session_id": qa_sid}), "{}", store.now()))
store.delete_paper(p)
check("delete: qa_sessions removed", not store.query(
    "SELECT 1 FROM qa_sessions WHERE paper_id=?", (p,)))
check("delete: integrity ok", store.integrity_report()["ok"], store.integrity_report())
store.execute("INSERT INTO qa_sessions(paper_id, domain_id, name, created_at)"
              " VALUES (?,?,?,?)", (99999, d2, "orphan", store.now()))
report = store.integrity_report()
check("integrity: orphan qa session detected",
      any(x["check"] == "orphan_qa_sessions" for x in report["problems"]), report["problems"])
store.execute("DELETE FROM qa_sessions WHERE paper_id=99999")

p2 = mk_paper(d2, "Meta Paper", arxiv_id="1706.00001")
store.merge_paper_meta(p2, {"lock_domain": True, "summaries": {"five": []}, "_vec64": "AAAA"})
store.upsert_paper(None, "Meta Paper", None, arxiv_id="1706.00001",
                   meta={"primary_category": "cs.CL"})
meta = json.loads(store.get_paper(p2)["meta_json"])
check("upsert: meta merged instead of replaced",
      meta.get("lock_domain") is True and meta.get("_vec64") == "AAAA"
      and meta.get("primary_category") == "cs.CL", meta)
res = c.post("/papers/import", json={"arxiv_ids": ["1706.00001"]}).json()["queued"]
check("import: duplicate arxiv reported and skipped",
      any(x.get("duplicate") for x in res), res)

r = c.put("/llm/profiles", json={"active": "DeepSeek", "profiles": [{
    "name": "DeepSeek", "api_base": "https://api.deepseek.com", "api_key": "sk-secret",
    "model_fast": "m1", "model_pro": "m2", "thinking": "disabled"}]})
check("profiles: save with key", r.status_code == 200, r.text[:200])
prof = c.get("/llm/profiles").json()["profiles"][0]
check("profiles: key redacted + has_key", "api_key" not in prof and prof.get("has_key") is True, prof)
r = c.put("/llm/profiles", json={"active": "DeepSeek", "profiles": [{
    "name": "DeepSeek", "api_base": "https://api.deepseek.com", "api_key": "",
    "model_fast": "m1", "model_pro": "m2", "thinking": "disabled"}]})
stored = json.loads(store.get_config("llm_profiles"))[0]
check("profiles: empty key keeps stored secret", stored.get("api_key") == "sk-secret", stored)
r = c.put("/llm/profiles", json={"active": "DeepSeek", "profiles": [{
    "name": "DeepSeek", "api_base": "https://api.deepseek.com", "api_key": "",
    "clear_key": True, "model_fast": "m1", "model_pro": "m2", "thinking": "disabled"}]})
check("profiles: clear_key clears secret",
      json.loads(store.get_config("llm_profiles"))[0].get("api_key") == "", r.text[:200])
check("config: rejects llm_profiles write",
      c.put("/config", json={"values": {"llm_profiles": "[]"}}).status_code == 400)
check("config: dispersion weight must be numeric",
      c.put("/config", json={"values": {"domain_dispersion_weight": "abc"}}).status_code == 400)
check("config: dispersion weight accepts number",
      c.put("/config", json={"values": {"domain_dispersion_weight": "0.25"}}).status_code == 200)
LLM_ON["value"] = False
check("llm guard: summary 428 without key",
      c.post(f"/papers/{p2}/summary/generate", json={"granularity": "five"}).status_code == 428)
check("llm guard: translate 428 without key",
      c.post(f"/papers/{p2}/translate", json={"quote": "x", "mode": "literal"}).status_code == 428)
LLM_ON["value"] = True

r = c.get("/health", headers={"Origin": "http://127.0.0.1:12345"})
check("cors: foreign localhost port blocked", r.status_code == 403, r.status_code)
r = c.get("/health")
check("cors: no Origin allowed", r.status_code == 200, r.status_code)

print("\nRESULT:", "ALL OK" if all(OK) else f"{OK.count(False)} FAILED", f"({len(OK)} checks)")
sys.exit(0 if all(OK) else 1)
