"""Domain, merge, retrieval and QA smoke test (temporary data directory)."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="sl_domains_")
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


def migrate_legacy_domains():
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


migrate_legacy_domains()

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

r = c.put("/llm/profiles", json={"active": "DeepSeek", "profiles": [{
    "name": "DeepSeek", "api_base": "https://api.deepseek.com", "api_key": "sk-103",
    "model_fast": "m1", "model_pro": "m2", "thinking": "disabled"}]})
prof = c.get("/llm/profiles").json()["profiles"][0]
profile_id = prof.get("profile_id")
check("profile: stable id assigned", bool(profile_id), prof)
r = c.put("/llm/profiles", json={"active": "DS-103", "profiles": [{
    "profile_id": profile_id, "name": "DS-103", "api_base": "https://api.deepseek.com",
    "api_key": "", "model_fast": "m1", "model_pro": "m2", "thinking": "disabled"}]})
stored = json.loads(store.get_config("llm_profiles"))[0]
check("profile: rename keeps key + id",
      stored.get("api_key") == "sk-103" and stored.get("profile_id") == profile_id, stored)
r = c.put("/llm/profiles", json={"active": "DS-104", "profiles": [{
    "name": "DS-104", "api_base": "https://api.deepseek.com",
    "api_key": "", "model_fast": "m1", "model_pro": "m2", "thinking": "disabled"}]})
stored = json.loads(store.get_config("llm_profiles"))[0]
check("profile: old client rename fallback keeps key", stored.get("api_key") == "sk-103", stored)

dcard = store.create_domain("Card103")
pc = store.upsert_paper(dcard, "Card103 Paper", None)
store.set_parse_status(pc, "done")
store.merge_paper_meta(pc, {"summaries": {"five": [
    {"section": "研究问题", "text": "card RQ", "page": 1}]}})
store.save_summary(pc, {"five": [{"section": "研究问题", "text": "card RQ", "page": 1}]},
                   research_goal="", parse_version=store.get_paper(pc)["parse_version"])
store.execute("UPDATE domains SET research_goal=? WHERE domain_id=?", ("找 idea", dcard))
got = c.get(f"/papers/{pc}/card").json()
check("card: goal fallback returns five sections", got["card"].get("research_question") == "card RQ", got)
check("card: export uses fallback", "研究问题" in c.get(f"/papers/{pc}/card/export").json()["markdown"])

msrc = store.create_domain("Merge103 Src")
mchild = store.create_domain("Merge103 Child")
mdst = store.create_domain("Merge103 Dst")
mthird = store.create_domain("Merge103 Third")
store.set_domain_parent(mchild, msrc)
cid = store.upsert_concept("merge-term", "", "def", msrc)
store.add_concept_mapping(cid, msrc, mthird, "different")
c.post("/domains/merge", json={"src_id": msrc, "dst_id": mdst})
check("merge: child re-parented to dst", store.get_domain(mchild)["parent_domain_id"] == mdst)
mrows = store.list_concept_mappings()
check("merge: concept mapping rewritten to dst",
      any(m["domain_a"] == mdst and m["domain_b"] == mthird for m in mrows), mrows)
mparent = store.create_domain("Merge103 Parent")
mchild2 = store.create_domain("Merge103 Child2")
store.set_domain_parent(mchild2, mparent)
r = c.post("/domains/merge", json={"src_id": mparent, "dst_id": mchild2})
check("merge: parent into descendant rejected", r.status_code == 400, r.text[:120])

dsplit = store.create_domain("Split103 Src")
sp1 = store.upsert_paper(dsplit, "Split103 A", None)
sp2 = store.upsert_paper(dsplit, "Split103 B", None)
r = c.post(f"/domains/{dsplit}/split", json={"name": "Split103 New", "paper_ids": [sp1]})
check("split: HTTP 200", r.status_code == 200, r.text[:200])
new_id = r.json().get("domain_id") if r.status_code == 200 else None
check("split: primary moved", store.get_paper(sp1)["domain_id"] == new_id)
check("split: source link removed",
      not any(l["domain_id"] == dsplit for l in store.list_paper_domains(sp1)),
      store.list_paper_domains(sp1))
check("split: unselected stays", store.get_paper(sp2)["domain_id"] == dsplit)

st = store.create_subtopic(dsplit, "Subtopic103")
store.assign_paper_subtopics(sp2, [st])
ov = c.get(f"/domains/{dsplit}/overview").json()
check("overview: subtopics with counts", any(s["name"] == "Subtopic103" and s["n_papers"] == 1
                                             for s in ov.get("subtopics", [])), ov.get("subtopics"))

qa = store.execute(
    "INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content, created_at)"
    " VALUES (?,?,?,?,?,?)",
    (dsplit, sp2, "qa", json.dumps({"session_id": 1}),
     json.dumps({"question": "Q103?", "answer": "A103.", "refs": []}), store.now()))
qid = qa.lastrowid
r = c.post(f"/papers/{sp2}/qa/turns/{qid}/important", json={"important": True})
check("qa: mark important", r.status_code == 200 and r.json().get("note_id"), r.text[:200])
notes = c.get(f"/papers/{sp2}/notes").json()
check("qa: note mirrored", any(n.get("qa_turn_id") == qid for n in notes), notes)
hist = c.get(f"/papers/{sp2}/qa/history").json()
check("qa: history carries importance",
      any(h.get("interaction_id") == qid and h.get("importance") for h in hist), hist)
c.post(f"/papers/{sp2}/qa/turns/{qid}/important", json={"important": False})
notes = c.get(f"/papers/{sp2}/notes").json()
check("qa: unmark removes note", not any(n.get("qa_turn_id") == qid for n in notes), notes)

pqa = store.upsert_paper(dsplit, "QA103 Paper", None)
store.set_parse_status(pqa, "done")
store.replace_chunks(pqa, dsplit, [{"section": "Intro", "page": 1, "start_char": 0,
                                    "end_char": 20, "text": "note source material"}])
store.execute("UPDATE chunks SET vec=? WHERE paper_id=?", (b"\x00" * 16, pqa))
store.add_note(pqa, dsplit, 1, "note source material", None, "my note", ["方法"])
api.retrieval.retrieve = lambda paper_id, query, k=8, domain_id=None: (
    [{"chunk_id": 1, "text": "note source material", "page": 1, "score": 1.0}], 0.9)
api.retrieval.retrieve_notes = lambda paper_id, domain_ids, query, k=3: (
    [{"note_id": 1, "paper_id": pqa, "domain_id": dsplit, "text": "my note",
      "quote": "note source material", "page": 1, "paper_title": "QA103 Paper",
      "score": 0.9, "source": "note"}])
def _qa_stream(task, messages, model=None, max_tokens=0, temperature=0.2,
               _retry=True, thinking=None, **kw):
    yield "结论[1]。"
    yield "笔记[2]。"
    yield "__meta__" + json.dumps({"model_served": "stub", "finish_reason": "stop",
                                   "latency_ms": 1})
api.llm.chat_stream = _qa_stream
qs = c.get(f"/papers/{pqa}/qa/sessions").json()[0]
with c.stream("POST", f"/papers/{pqa}/qa",
              json={"question": "stub?", "scope": "paper",
                    "session_id": qs["session_id"]}) as resp:
    body = "".join(resp.iter_text())
check("qa: note source in refs", '"source": "note"' in body, body[-500:])
check("qa: refs total includes note", '"total": 2' in body, body[-300:])
seen = {}
_real_primary = api.assign.primary_domain
api.assign.primary_domain = lambda question, ids: (d2, 0.9)
def _scoped(ids, query, k=8, paper_ids=None, weights=None, primary_domain_id=None):
    seen["primary"] = primary_domain_id
    return ([{"chunk_id": 2, "text": "cross material", "page": 2, "score": 1.0,
              "paper_id": pqa, "title": "QA103 Paper", "domain_id": d2}], 0.9, [])
api.retrieval.retrieve_domains_scoped = _scoped
with c.stream("POST", f"/papers/{pqa}/qa",
              json={"question": "cross?", "scope": "all",
                    "session_id": qs["session_id"]}) as resp:
    body = "".join(resp.iter_text())
check("qa: primary domain reaches scoped retrieval", seen.get("primary") == d2, seen)
check("qa: scope carries primary", '"primary_domain_id"' in body, body[-300:])
api.assign.primary_domain = _real_primary

pof = store.upsert_paper(dcard, "Offset103", None)
store.set_parse_status(pof, "done")
store.replace_chunks(pof, dcard, [{"section": "Intro", "page": 1, "start_char": 0,
                                   "end_char": 18, "text": "alpha beta gamma"}])
r = c.post(f"/papers/{pof}/highlights", json={
    "page": 1, "quote": "alpha\nbeta", "category": "background", "occurrence": 0})
a = store.query("SELECT char_start, char_end FROM anchors WHERE paper_id=?", (pof,))
check("anchor: whitespace-tolerant offsets", bool(a) and a[0]["char_start"] == 0
      and a[0]["char_end"] == 10, [dict(x) for x in a])

rows = c.get(f"/domains/{dcard}/papers").json()
check("list: meta_json omitted", rows and all("meta_json" not in x for x in rows))

before = store.get_config("domain_dispersion_weight")
r = c.put("/config", json={"values": {"domain_dispersion_weight": "0.21",
                                      "assign_threshold": "5"}})
check("config: out-of-range rejected", r.status_code == 400, r.status_code)
check("config: rejected batch writes nothing",
      store.get_config("domain_dispersion_weight") == before,
      (before, store.get_config("domain_dispersion_weight")))
check("config: nan rejected",
      c.put("/config", json={"values": {"assign_threshold": "nan"}}).status_code == 400)

pdf103 = os.path.join(TMP, "dup103.pdf")
with open(pdf103, "wb") as fh:
    fh.write(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n")
sha = hashlib.sha1(open(pdf103, "rb").read()).hexdigest()
dupdom = store.create_domain("Dup103 Home")
otherdom = store.create_domain("Dup103 Other")
pidup = store.upsert_paper(None, "Dup103", None, sha1=sha)
res = c.post("/papers/import", json={"arxiv_ids": [], "local_paths": [pdf103],
                                     "domain_id": otherdom}).json()["queued"]
check("import: duplicate links requested domain",
      any(x.get("duplicate") and x.get("linked") for x in res), res)
check("import: duplicate link exists",
      any(l["domain_id"] == otherdom for l in store.list_paper_domains(pidup)))

dda = store.create_domain("Disc103 A")
ddb = store.create_domain("Disc103 B")
dpa = store.upsert_paper(dda, "Disc103 PA", None)
dpb = store.upsert_paper(dda, "Disc103 PB", None)
out = c.post("/domains/discover/apply", json={
    "clusters": [{"name": "Disc103 New", "paper_ids": [dpa]}],
    "adjustments": [{"paper_id": dpb, "domain_id": ddb}],
}).json()
check("discover: adjustment applied", bool(out.get("adjusted")), out)
check("discover: adjusted paper moved", store.get_paper(dpb)["domain_id"] == ddb)

cd1 = store.create_domain("Overlap103 A")
cd2 = store.create_domain("Overlap103 B")
store.upsert_concept("shared-term", "", "A", cd1)
store.upsert_concept("shared-term", "", "B", cd2)
ovl = store.concept_overlaps(cd1, "shared-term")
check("concept: overlap feed", bool(ovl) and ovl[0]["term"] == "shared-term"
      and len(ovl[0]["domains"]) >= 2, ovl)
r = c.put(f"/domains/{dcard}/config",
          json={"color_scheme_json": json.dumps({"background": "#112233"})})
check("config: color scheme saved",
      r.status_code == 200 and "#112233" in c.get(f"/domains/{dcard}/config").json()["color_scheme_json"])

check("domain: missing parent rejected",
      c.post(f"/domains/{dcard}/parent", json={"parent_id": 999999}).status_code == 400)

rblock = store.create_domain("Rollback103 Src")
rdst = store.create_domain("Rollback103 Dst")
rpaper = store.upsert_paper(rblock, "Rollback103 Paper", None)
store.execute("CREATE TRIGGER block_merge BEFORE UPDATE ON papers"
              " BEGIN SELECT RAISE(ABORT, 'blocked'); END")
r = c.post("/domains/merge", json={"src_id": rblock, "dst_id": rdst})
check("merge: forced failure returns 409", r.status_code == 409, r.text[:160])
check("merge: rollback keeps source", bool(store.get_domain(rblock)))
check("merge: rollback keeps paper domain", store.get_paper(rpaper)["domain_id"] == rblock)
check("merge: rollback keeps source link",
      any(l["domain_id"] == rblock for l in store.list_paper_domains(rpaper)))
store.execute("DROP TRIGGER IF EXISTS block_merge")

from server import retrieval as _retrieval  # noqa: 
from server import assign as _assign  # noqa: 
_old_embed, _old_centroid = _assign.embed, _assign.domain_centroid
try:
    _assign.embed = lambda texts: [
        [1.0, 0.0] if "my note" in (t or "") else [0.0, 1.0] for t in texts]
    _assign.domain_centroid = lambda did: [1.0, 0.0] if did == cd1 else [0.0, 1.0]
    got_notes = _retrieval.retrieve_notes(pqa, None, "my note", k=1)
    check("retrieval: notes top-1", bool(got_notes) and got_notes[0]["note_id"], got_notes)
    got_primary, _ = _assign.primary_domain("my note question", [cd1, cd2])
    check("assign: primary domain from centroids", got_primary == cd1, got_primary)
finally:
    _assign.embed, _assign.domain_centroid = _old_embed, _old_centroid

store.save_summary(pc, {"para": "para body"}, granularity="para",
                   research_goal="找 idea", parse_version=store.get_paper(pc)["parse_version"])
check("summary: para stored under para",
      store.get_summary(pc, "para", "找 idea",
                        store.get_paper(pc)["parse_version"]) is not None)
check("summary: para does not masquerade as five",
      store.get_summary(pc, "five", "找 idea",
                        store.get_paper(pc)["parse_version"]) is None)
qa2 = store.execute(
    "INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content, created_at)"
    " VALUES (?,?,?,?,?,?)",
    (dsplit, sp2, "qa", json.dumps({"session_id": 999}),
     json.dumps({"question": "Q999?", "answer": "A999.", "refs": []}), store.now()))
qid2 = qa2.lastrowid
c.post(f"/papers/{sp2}/qa/turns/{qid2}/important", json={"important": True})
c.delete(f"/papers/{sp2}/qa/sessions/999")
notes = c.get(f"/papers/{sp2}/notes").json()
check("qa: session delete removes mirrored note",
      not any(n.get("qa_turn_id") == qid2 for n in notes), notes)

print("\nRESULT:", "ALL OK" if all(OK) else f"{OK.count(False)} FAILED", f"({len(OK)} checks)")
sys.exit(0 if all(OK) else 1)
