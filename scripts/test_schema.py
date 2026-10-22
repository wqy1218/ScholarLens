"""Migration and lifecycle test on a synthetic legacy database."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import store

LEGACY = """
CREATE TABLE domains (domain_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
 description TEXT DEFAULT '', research_goal TEXT DEFAULT '', weight REAL DEFAULT 1.0,
 centroid BLOB, meta_json TEXT DEFAULT '{}', created_at TEXT, last_accessed_at TEXT);
CREATE TABLE papers (paper_id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id INTEGER,
 title TEXT DEFAULT '', authors TEXT DEFAULT '', abstract TEXT DEFAULT '', doi TEXT DEFAULT '',
 venue TEXT DEFAULT '', year INTEGER, arxiv_id TEXT UNIQUE, file_path TEXT UNIQUE, sha1 TEXT,
 parse_status TEXT DEFAULT 'queued', parse_error TEXT DEFAULT '', reading_status TEXT DEFAULT 'unread',
 meta_json TEXT DEFAULT '{}', created_at TEXT, progress REAL DEFAULT 0, last_opened_at TEXT);
CREATE TABLE paper_references (src_paper_id INTEGER, dst_paper_id INTEGER,
 dst_title TEXT DEFAULT '', dst_doi TEXT DEFAULT '', resolved INTEGER DEFAULT 0);
CREATE TABLE paper_domain_links (paper_id INTEGER, domain_id INTEGER, score REAL DEFAULT 0,
 source TEXT DEFAULT 'auto', UNIQUE(paper_id, domain_id));
CREATE TABLE interactions (interaction_id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id INTEGER,
 paper_id INTEGER, type TEXT, anchor_json TEXT DEFAULT '', content TEXT DEFAULT '',
 evidence_json TEXT DEFAULT '', created_at TEXT);
CREATE TABLE qa_sessions (session_id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER,
 domain_id INTEGER, name TEXT NOT NULL, created_at TEXT);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def build(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(LEGACY)
    meta = json.dumps({"glossary": [{"en": "attention", "zh": "注意力"}],
                       "summaries": {"five": [{"section": "研究问题", "text": "x"}]}},
                      ensure_ascii=False)
    con.execute("INSERT INTO domains(domain_id,name,meta_json,created_at,last_accessed_at)"
                " VALUES (1,'NLP',?,'2026-01-01','2026-01-01')", (meta,))
    con.execute("INSERT INTO papers(paper_id,domain_id,title,doi,sha1,parse_status,"
                " reading_status,meta_json,created_at) VALUES (1,1,'Attention Is All You Need',"
                " 'https://doi.org/10.1/ABC','sha1a','done','reading',?,'2026-01-01')", (meta,))
    con.execute("INSERT INTO papers(paper_id,domain_id,title,doi,sha1,parse_status,created_at)"
                " VALUES (2,1,'BERT','10.1/def','sha1b','done','2026-01-01')")
    con.execute("INSERT INTO paper_references(src_paper_id,dst_paper_id,dst_title,dst_doi)"
                " VALUES (2,1,'Attention Is All You Need','10.1/abc')")
    con.execute("INSERT INTO paper_references(src_paper_id,dst_paper_id,dst_title,dst_doi)"
                " VALUES (1,NULL,'Some External Paper','10.9/xyz')")
    con.execute("INSERT INTO paper_references(src_paper_id,dst_paper_id,dst_title,dst_doi)"
                " VALUES (1,NULL,'BERT pre-training of deep bidirectional transformers','')")
    con.execute("INSERT INTO interactions(paper_id,domain_id,type,anchor_json,content,created_at)"
                " VALUES (1,1,'highlight',?,'background','2026-01-01')",
                (json.dumps({"page": 3, "quote": "self-attention", "start": 10, "end": 23}),))
    con.commit()
    con.close()


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="sl_schema_")
    try:
        db = os.path.join(tmp, "test.db")
        build(db)
        store.connect(db)

        assert store.query("PRAGMA user_version")[0][0] == 6, "schema version"
        fk = store.query("PRAGMA foreign_key_list(interactions)")
        assert any(r["from"] == "anchor_id" and r["table"] == "anchors" for r in fk), fk
        assert not store.query("PRAGMA foreign_key_check"), "foreign key violations"
        cols = {r["name"] for r in store.query("PRAGMA table_info(interactions)")}
        assert "tags_json" in cols, cols
        dcols = {r["name"] for r in store.query("PRAGMA table_info(domains)")}
        assert "discovery_sig" in dcols, dcols
        lcols = {r["name"] for r in store.query("PRAGMA table_info(paper_domain_links)")}
        assert {"last_page", "scroll_ratio"} <= lcols, lcols
        assert store.query("SELECT 1 FROM sqlite_master WHERE name='survey_drafts'"), "survey_drafts"
        links = [dict(r) for r in store.query("SELECT * FROM paper_domain_links")]
        assert len(links) == 2 and all(r["is_primary"] == 1 for r in links), links
        cites = [dict(r) for r in store.query("SELECT * FROM citations")]
        assert len(cites) == 3 and {c["status"] for c in cites} == {
            "resolved", "external", "unresolved"}, cites
        assert store.query("SELECT COUNT(*) c FROM anchors")[0]["c"] == 1
        assert store.query("SELECT COUNT(*) c FROM concepts")[0]["c"] == 1
        assert store.query("SELECT COUNT(*) c FROM summaries")[0]["c"] == 1
        assert store.query("SELECT COUNT(*) c FROM review_items")[0]["c"] == 1
        report = store.integrity_report()
        assert report["ok"], report

        store.create_domain("CV")
        store.add_paper_domain(1, 2, score=0.4)
        store.set_paper_domain(1, 2)
        assert store.get_paper(1)["domain_id"] == 2
        store.merge_domains(2, 1)
        assert store.get_paper(1)["domain_id"] == 1
        assert store.integrity_report()["ok"], store.integrity_report()

        store.set_domain_reading(1, 1, "done", 42.5)
        assert [d["reading_status"] for d in store.list_paper_domains(1) if d["domain_id"] == 1] == ["done"]
        store.save_summary(1, {"five": []}, research_goal="idea")
        assert store.get_summary(1, "five", "idea") is not None
        cid = store.list_citations(paper_id=2)[0]["citation_id"]
        store.resolve_citation(cid, target_paper_id=1)
        assert store.list_citations(paper_id=2, status="resolved")

        store.upsert_paper(None, "BERT", None, arxiv_id=None, sha1="sha1b")
        assert store.get_paper(2)["doi_key"] == "10.1/def", store.get_paper(2)["doi_key"]

        store.delete_paper(2)
        assert not store.query("SELECT 1 FROM paper_domain_links WHERE paper_id=2")
        assert not store.query("SELECT 1 FROM citations WHERE source_paper_id=2")

        d3 = store.create_domain("TMP")
        store.add_paper_domain(1, d3, score=0.2)
        store.add_question(1, d3, "temporary?")
        store.delete_domain(d3)
        assert not store.query("SELECT 1 FROM paper_domain_links WHERE domain_id=?", (d3,))
        assert not store.query("SELECT 1 FROM questions WHERE domain_id=?", (d3,))
        assert store.get_paper(1)["domain_id"] == 1
        assert store.integrity_report()["ok"], store.integrity_report()
        print("test_schema OK", json.dumps(store.integrity_report()["stats"], ensure_ascii=False))
    finally:
        try:
            store._conn.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
