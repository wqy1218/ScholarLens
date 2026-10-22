"""SQLite storage (WAL).

`paper_domain_links` is the single source of truth for paper membership;
`papers.domain_id` is only a cache of the primary domain. Citations, concepts,
anchors, summaries, questions and review items are first-class tables. Reading
position, survey drafts and domain configuration are persisted so state survives
a restart, a reinstall or a snapshot restore.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
 domain_id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL UNIQUE,
 description TEXT DEFAULT '',
 research_goal TEXT DEFAULT '',
 weight REAL DEFAULT 1.0,
 parent_domain_id INTEGER REFERENCES domains(domain_id) ON DELETE SET NULL,
 centroid BLOB,
 meta_json TEXT DEFAULT '{}',
 discovery_sig TEXT DEFAULT '',
 last_paper_id INTEGER,
 created_at TEXT, last_accessed_at TEXT,
 deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS papers (
 paper_id INTEGER PRIMARY KEY AUTOINCREMENT,
 domain_id INTEGER,
 title TEXT DEFAULT '', authors TEXT DEFAULT '', abstract TEXT DEFAULT '',
 doi TEXT DEFAULT '', doi_key TEXT DEFAULT '', venue TEXT DEFAULT '', year INTEGER,
 arxiv_id TEXT UNIQUE,
 file_path TEXT UNIQUE,
 sha1 TEXT,
 parse_status TEXT DEFAULT 'queued', -- queued/parsing/done/failed
 parse_error TEXT DEFAULT '',
 parse_version INTEGER DEFAULT 0,
 ocr_status TEXT DEFAULT 'none', -- none/pending/running/done/failed
 reading_status TEXT DEFAULT 'unread', -- 未读/unread, reading, skimmed, done, review
 progress REAL DEFAULT 0,
 weight REAL DEFAULT 1.0, -- user-marked importance ( node size)
 source TEXT DEFAULT '', -- local/arxiv/zotero/openreview
 external_id TEXT DEFAULT '',
 meta_json TEXT DEFAULT '{}',
 last_opened_at TEXT,
 created_at TEXT, updated_at TEXT, deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
 chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
 paper_id INTEGER NOT NULL,
 domain_id INTEGER,
 section TEXT DEFAULT '',
 page INTEGER, start_char INTEGER, end_char INTEGER,
 text TEXT NOT NULL,
 vec BLOB,
 UNIQUE(paper_id, start_char, end_char)
);
CREATE INDEX IF NOT EXISTS idx_chunks_paper ON chunks(paper_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
 text, domain_id UNINDEXED, paper_id UNINDEXED, content='chunks', content_rowid='chunk_id',
 tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
 INSERT INTO chunks_fts(rowid, text, domain_id, paper_id) VALUES (new.chunk_id, new.text, new.domain_id, new.paper_id);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
 INSERT INTO chunks_fts(chunks_fts, rowid, text, domain_id, paper_id) VALUES ('delete', old.chunk_id, old.text, old.domain_id, old.paper_id);
END;
CREATE TABLE IF NOT EXISTS paper_references (
 src_paper_id INTEGER, dst_paper_id INTEGER,
 dst_title TEXT DEFAULT '', dst_doi TEXT DEFAULT '', resolved INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paper_domain_links (
 paper_id INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 domain_id INTEGER NOT NULL REFERENCES domains(domain_id) ON DELETE CASCADE,
 score REAL DEFAULT 0, source TEXT DEFAULT 'auto',
 is_primary INTEGER DEFAULT 0, -- exactly one active primary link per paper
 confidence REAL DEFAULT 0,
 status TEXT DEFAULT 'auto', -- auto/confirmed/pending/rejected
 reading_status TEXT, -- per-domain reading state (NULL = inherit)
 progress REAL, -- per-domain progress 0..100
 last_page INTEGER, -- durable reading position
 scroll_ratio REAL, -- 0..1 position inside the page
 weight REAL DEFAULT 1.0,
 added_at TEXT, updated_at TEXT, deleted_at TEXT,
 UNIQUE(paper_id, domain_id)
);
CREATE TABLE IF NOT EXISTS interactions (
 interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
 domain_id INTEGER, paper_id INTEGER,
 type TEXT, anchor_json TEXT DEFAULT '', content TEXT DEFAULT '',
 evidence_json TEXT DEFAULT '',
 anchor_id INTEGER REFERENCES anchors(anchor_id) ON DELETE SET NULL,
 domains_json TEXT DEFAULT '', -- domains involved (cross-domain QA)
 importance INTEGER DEFAULT 0,
 created_at TEXT, updated_at TEXT, deleted_at TEXT,
 tags_json TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS config (
 key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translations (
 paper_id INTEGER NOT NULL,
 sentence TEXT NOT NULL,
 mode TEXT NOT NULL,
 translation TEXT NOT NULL,
 created_at TEXT,
 PRIMARY KEY (paper_id, sentence, mode)
);
CREATE TABLE IF NOT EXISTS qa_sessions (
 session_id INTEGER PRIMARY KEY AUTOINCREMENT,
 paper_id INTEGER, -- NULL = 领域级会话（跨论文比较）
 domain_id INTEGER,
 domain_ids_json TEXT, -- cross-domain QA: all domains involved
 paper_ids_json TEXT, -- cross-paper session: explicit paper subset
 name TEXT NOT NULL,
 created_at TEXT
);
-- citation graph (paper -> paper / external reference), replaces paper_references
CREATE TABLE IF NOT EXISTS citations (
 citation_id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_paper_id INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 target_paper_id INTEGER REFERENCES papers(paper_id) ON DELETE SET NULL,
 target_doi TEXT DEFAULT '', target_doi_key TEXT DEFAULT '',
 target_title TEXT DEFAULT '', target_title_key TEXT DEFAULT '',
 target_arxiv_id TEXT DEFAULT '',
 raw TEXT DEFAULT '',
 status TEXT NOT NULL DEFAULT 'unresolved', -- resolved/unresolved/external/rejected
 confidence REAL DEFAULT 0,
 evidence_json TEXT DEFAULT '',
 created_at TEXT, updated_at TEXT,
 UNIQUE(source_paper_id, target_doi_key, target_title_key)
);
-- concepts are first-class; definitions are per (concept, domain)
CREATE TABLE IF NOT EXISTS concepts (
 concept_id INTEGER PRIMARY KEY AUTOINCREMENT,
 concept_key TEXT NOT NULL UNIQUE,
 display_en TEXT DEFAULT '', display_zh TEXT DEFAULT '',
 created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS concept_definitions (
 definition_id INTEGER PRIMARY KEY AUTOINCREMENT,
 concept_id INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
 domain_id INTEGER NOT NULL REFERENCES domains(domain_id) ON DELETE CASCADE,
 term_zh TEXT DEFAULT '', definition TEXT DEFAULT '',
 source_paper_id INTEGER REFERENCES papers(paper_id) ON DELETE SET NULL,
 created_at TEXT, updated_at TEXT,
 UNIQUE(concept_id, domain_id)
);
CREATE TABLE IF NOT EXISTS concept_mappings (
 mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
 concept_id INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
 domain_a INTEGER NOT NULL REFERENCES domains(domain_id) ON DELETE CASCADE,
 domain_b INTEGER NOT NULL REFERENCES domains(domain_id) ON DELETE CASCADE,
 mapping_type TEXT NOT NULL DEFAULT 'related', -- same/different/related
 user_note TEXT DEFAULT '',
 created_at TEXT, updated_at TEXT,
 CHECK(domain_a < domain_b),
 CHECK(mapping_type IN ('same','different','related')),
 UNIQUE(concept_id, domain_a, domain_b)
);
-- one anchor representation shared by highlights/notes
CREATE TABLE IF NOT EXISTS anchors (
 anchor_id INTEGER PRIMARY KEY AUTOINCREMENT,
 paper_id INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 page INTEGER, quote TEXT DEFAULT '', quote_hash TEXT DEFAULT '',
 char_start INTEGER, char_end INTEGER,
 rects_json TEXT DEFAULT '', parse_version INTEGER DEFAULT 0,
 created_at TEXT,
 UNIQUE(paper_id, page, quote_hash, char_start, char_end)
);
-- summary cache keyed by granularity + research goal + parse version
CREATE TABLE IF NOT EXISTS summaries (
 summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
 paper_id INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 granularity TEXT DEFAULT 'five',
 research_goal TEXT DEFAULT '',
 parse_version INTEGER DEFAULT 0,
 model TEXT DEFAULT '',
 content_json TEXT DEFAULT '[]',
 created_at TEXT, updated_at TEXT,
 UNIQUE(paper_id, granularity, research_goal, parse_version)
);
CREATE TABLE IF NOT EXISTS questions (
 question_id INTEGER PRIMARY KEY AUTOINCREMENT,
 paper_id INTEGER REFERENCES papers(paper_id) ON DELETE CASCADE,
 domain_id INTEGER REFERENCES domains(domain_id) ON DELETE CASCADE,
 text TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', -- open/resolved/dismissed
 source_type TEXT DEFAULT '', source_id INTEGER,
 created_at TEXT, resolved_at TEXT,
 CHECK(status IN ('open','resolved','dismissed')),
 UNIQUE(paper_id, domain_id, text)
);
CREATE TABLE IF NOT EXISTS review_items (
 item_id INTEGER PRIMARY KEY AUTOINCREMENT,
 citation_id INTEGER REFERENCES citations(citation_id) ON DELETE CASCADE,
 paper_id INTEGER REFERENCES papers(paper_id) ON DELETE CASCADE,
 domain_id INTEGER REFERENCES domains(domain_id) ON DELETE CASCADE,
 reason TEXT DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending', -- pending/confirmed/rejected
 created_at TEXT, resolved_at TEXT,
 CHECK(status IN ('pending','confirmed','rejected')),
 UNIQUE(citation_id, domain_id)
);
CREATE TABLE IF NOT EXISTS domain_config (
 domain_id INTEGER PRIMARY KEY REFERENCES domains(domain_id) ON DELETE CASCADE,
 highlight_density TEXT DEFAULT 'normal',
 color_scheme_json TEXT DEFAULT '{}',
 sort_json TEXT DEFAULT '{}',
 reading_path_json TEXT DEFAULT '{}',
 index_mode TEXT DEFAULT 'logical', -- logical/physical/migrating
 extra_json TEXT DEFAULT '{}',
 updated_at TEXT
);
CREATE TABLE IF NOT EXISTS operation_log (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 actor TEXT DEFAULT 'system',
 op TEXT NOT NULL,
 entity_type TEXT DEFAULT '', entity_id INTEGER,
 domain_id INTEGER,
 payload_json TEXT DEFAULT '{}',
 created_at TEXT
);
CREATE TABLE IF NOT EXISTS card_templates (
 template_id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL UNIQUE,
 scope TEXT DEFAULT 'global',
 domain_id INTEGER,
 content_json TEXT DEFAULT '{}',
 is_default INTEGER DEFAULT 0,
 created_at TEXT
);
CREATE TABLE IF NOT EXISTS export_records (
 export_id INTEGER PRIMARY KEY AUTOINCREMENT,
 scope TEXT DEFAULT '', scope_id INTEGER,
 format TEXT DEFAULT '', paper_ids_json TEXT DEFAULT '[]',
 path TEXT DEFAULT '', note TEXT DEFAULT '',
 created_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_sources (
 source_id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL, external_id TEXT NOT NULL,
 config_json TEXT DEFAULT '{}', last_sync_at TEXT,
 UNIQUE(kind, external_id)
);
CREATE TABLE IF NOT EXISTS reading_cards (
 card_id INTEGER PRIMARY KEY AUTOINCREMENT,
 paper_id INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 domain_id INTEGER REFERENCES domains(domain_id) ON DELETE CASCADE,
 template_id INTEGER,
 content_json TEXT DEFAULT '{}',
 created_at TEXT, updated_at TEXT,
 UNIQUE(paper_id, domain_id)
);
CREATE TABLE IF NOT EXISTS subtopics (
 subtopic_id INTEGER PRIMARY KEY AUTOINCREMENT,
 domain_id INTEGER NOT NULL REFERENCES domains(domain_id) ON DELETE CASCADE,
 parent_id INTEGER REFERENCES subtopics(subtopic_id) ON DELETE SET NULL,
 name TEXT NOT NULL,
 created_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_subtopics (
 paper_id INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 subtopic_id INTEGER NOT NULL REFERENCES subtopics(subtopic_id) ON DELETE CASCADE,
 UNIQUE(paper_id, subtopic_id)
);
-- read-only state snapshots (archive; restore requires explicit confirmation)
CREATE TABLE IF NOT EXISTS domain_snapshots (
 snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
 domain_id INTEGER NOT NULL REFERENCES domains(domain_id) ON DELETE CASCADE,
 name TEXT DEFAULT '',
 last_seq INTEGER DEFAULT 0, schema_version INTEGER DEFAULT 2,
 snapshot_json TEXT DEFAULT '{}', created_at TEXT
);
-- cross-domain suggestions; fingerprint prevents "忽略后重新生成又推送"
CREATE TABLE IF NOT EXISTS cross_domain_suggestions (
 suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_domain_id INTEGER REFERENCES domains(domain_id) ON DELETE CASCADE,
 target_domain_id INTEGER REFERENCES domains(domain_id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL UNIQUE,
 description TEXT DEFAULT '', evidence_json TEXT DEFAULT '[]',
 confidence REAL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 1),
 status TEXT DEFAULT 'pending' CHECK(status IN ('pending','explored','ignored','snoozed')),
 snooze_until TEXT, created_at TEXT, updated_at TEXT,
 CHECK(source_domain_id <> target_domain_id)
);
-- saved literature-survey drafts.
-- domain_id is the domain the draft was opened from; domain_ids_json records the
-- full (possibly cross-domain) selection so the draft can be regenerated.
CREATE TABLE IF NOT EXISTS survey_drafts (
 survey_id INTEGER PRIMARY KEY AUTOINCREMENT,
 domain_id INTEGER REFERENCES domains(domain_id) ON DELETE CASCADE,
 domain_ids_json TEXT DEFAULT '[]',
 paper_ids_json TEXT DEFAULT '[]',
 group_by TEXT DEFAULT 'topic', -- topic/year/method
 title TEXT DEFAULT '',
 markdown TEXT DEFAULT '',
 stats_json TEXT DEFAULT '{}',
 created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_survey_domain ON survey_drafts(domain_id, survey_id);
-- lifecycle guard for tables that pre-date FK support.
-- (papers are deleted through store.delete_paper(), which removes dependents
-- explicitly; SQLite raises SQLITE_CANTOPEN when an AFTER DELETE trigger and
-- FTS external-content triggers run inside the same cascading delete.)
CREATE TRIGGER IF NOT EXISTS domains_ad AFTER DELETE ON domains BEGIN
 DELETE FROM paper_domain_links WHERE domain_id = OLD.domain_id;
 DELETE FROM concept_definitions WHERE domain_id = OLD.domain_id;
 DELETE FROM concept_mappings WHERE domain_a = OLD.domain_id OR domain_b = OLD.domain_id;
 DELETE FROM questions WHERE domain_id = OLD.domain_id;
 DELETE FROM review_items WHERE domain_id = OLD.domain_id;
 DELETE FROM domain_config WHERE domain_id = OLD.domain_id;
 DELETE FROM survey_drafts WHERE domain_id = OLD.domain_id;
 UPDATE interactions SET domain_id = NULL WHERE domain_id = OLD.domain_id;
 UPDATE qa_sessions SET domain_id = NULL WHERE domain_id = OLD.domain_id;
 UPDATE chunks SET domain_id = NULL WHERE domain_id = OLD.domain_id;
 UPDATE domains SET parent_domain_id = NULL WHERE parent_domain_id = OLD.domain_id;
END;
"""

_conn = None
_lock = threading.RLock()


def connect(db_path: str) -> None:
    global _conn
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            pass
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(_SCHEMA)
    _migrate_schema()
    for k, v in CONFIG_DEFAULTS.items():
        _conn.execute("INSERT OR IGNORE INTO config(key, value) VALUES (?,?)", (k, v))
    _conn.commit()


def close() -> None:
    """Close the module-level connection."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            finally:
                _conn = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


now = _now



def _table_cols(table: str) -> set:
    return {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}


def _add_col(table: str, col: str, ddl: str) -> None:
    if col not in _table_cols(table):
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _json_col(col: str) -> str:
    """wrap a JSON text column for json_extract().

 SQLite's JSON1 functions raise "malformed JSON" when handed a non-empty
 string that is not JSON — but the legacy default of `interactions.anchor_json`
 is exactly `''`. Every query that reads a field out of that column must go
 through this expression (or `json_valid`), otherwise withdrawing a QA turn on
 an upgraded library dies with a 500.
    """
    return f"(CASE WHEN json_valid({col}) THEN {col} ELSE '{{}}' END)"


def _norm_key(text: str | None) -> str:
    """Case/punctuation-insensitive identity key for concepts and titles."""
    import re
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", (text or "").lower()).strip()


def _doi_key(doi: str | None) -> str:
    d = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.strip()


def _title_key(title: str | None) -> str:
    return _norm_key(title)


norm_key = _norm_key
doi_key = _doi_key
title_key = _title_key


def _quote_hash(quote: str) -> str:
    return hashlib.sha1((quote or "").strip().encode("utf-8")).hexdigest()[:20]


def _paper_parse_version(paper_id: int) -> int:
    row = _conn.execute("SELECT parse_version FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
    return int((row["parse_version"] if row else 0) or 0)


def _insert_anchor_raw(paper_id: int, page, quote: str, start, end, rects,
                       parse_version: int | None = None) -> int:
    """Canonical anchor row shared by highlights and notes.

char offsets, rects and parse_version are written for real; they used
 to be schema-only columns. Re-anchoring the same quote after a re-parse
 refreshes the rects and moves parse_version forward instead of silently
 reusing a stale anchor.
    """
    h = _quote_hash(quote)
    row = _conn.execute(
        "SELECT anchor_id, parse_version, rects_json FROM anchors"
        " WHERE paper_id=? AND page IS ? AND quote_hash=?"
        " AND char_start IS ? AND char_end IS ?",
        (paper_id, page, h, start, end)).fetchone()
    if row:
        fresh = json.dumps(rects or [], ensure_ascii=False)
        old_rects = row["rects_json"] or "[]"
        newer = (parse_version or 0) > (row["parse_version"] or 0)
        gained_rects = fresh not in ("[]", "") and old_rects in ("[]", "")
        if newer or gained_rects:
            _conn.execute(
                "UPDATE anchors SET rects_json=?,"
                " parse_version=MAX(COALESCE(parse_version,0), ?) WHERE anchor_id=?",
                (fresh, parse_version or 0, row["anchor_id"]))
        return row["anchor_id"]
    cur = _conn.execute(
        "INSERT INTO anchors(paper_id, page, quote, quote_hash, char_start, char_end,"
        " rects_json, parse_version, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (paper_id, page, quote or "", h, start, end,
         json.dumps(rects or [], ensure_ascii=False), parse_version or 0, _now()))
    return cur.lastrowid


def _insert_citation_raw(src: int, target, doi: str, title: str, status: str,
                         confidence: float = 0.0, raw: str = "") -> None:
    dk, tk = _doi_key(doi), _title_key(title)
    _conn.execute(
        "INSERT OR IGNORE INTO citations(source_paper_id, target_paper_id, target_doi,"
        " target_doi_key, target_title, target_title_key, status, confidence, raw,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (src, target, doi or "", dk, title or "", tk, status, confidence, raw,
         _now(), _now()))


def _upsert_concept_raw(term_en: str, term_zh: str = "", definition: str = "",
                        domain_id: int | None = None, source_paper_id: int | None = None) -> int:
    key = _norm_key(term_en)
    if not key:
        return 0
    row = _conn.execute("SELECT concept_id FROM concepts WHERE concept_key=?", (key,)).fetchone()
    if row:
        cid = row["concept_id"]
        _conn.execute("UPDATE concepts SET display_zh=COALESCE(NULLIF(?,''),display_zh),"
                      " updated_at=? WHERE concept_id=?", (term_zh, _now(), cid))
    else:
        cur = _conn.execute(
            "INSERT INTO concepts(concept_key, display_en, display_zh, created_at, updated_at)"
            " VALUES (?,?,?,?,?)", (key, term_en.strip(), term_zh, _now(), _now()))
        cid = cur.lastrowid
    if domain_id:
        _conn.execute(
            "INSERT INTO concept_definitions(concept_id, domain_id, term_zh, definition,"
            " source_paper_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(concept_id, domain_id) DO UPDATE SET"
            " term_zh=COALESCE(NULLIF(excluded.term_zh,''),term_zh),"
            " definition=CASE WHEN excluded.definition<>'' THEN excluded.definition ELSE definition END,"
            " updated_at=excluded.updated_at",
            (cid, domain_id, term_zh, definition, source_paper_id, _now(), _now()))
    return cid


def _has_fk(table: str, column: str) -> bool:
    return any(r["from"] == column for r in _conn.execute(f"PRAGMA foreign_key_list({table})"))


def _rebuild_interactions_fk() -> None:
    """Give `interactions.anchor_id` its missing FOREIGN KEY (schema ).

 SQLite cannot add a foreign key with ALTER TABLE, so the table is rebuilt.
 The pragma is switched off *outside* a transaction; the rebuild itself runs
 as one script so an interrupted upgrade leaves the old table intact.
    """
    if _has_fk("interactions", "anchor_id"):
        return
    _conn.commit()
    _conn.execute("PRAGMA foreign_keys=OFF")
    try:
        # the trigger on interactions is dropped here and recreated from _SCHEMA
        _conn.executescript("""
 DROP TRIGGER IF EXISTS domains_ad;
 BEGIN;
 CREATE TABLE interactions_new (
 interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
 domain_id INTEGER, paper_id INTEGER,
 type TEXT, anchor_json TEXT DEFAULT '', content TEXT DEFAULT '',
 evidence_json TEXT DEFAULT '',
 anchor_id INTEGER REFERENCES anchors(anchor_id) ON DELETE SET NULL,
 domains_json TEXT DEFAULT '',
 importance INTEGER DEFAULT 0,
 created_at TEXT, updated_at TEXT, deleted_at TEXT,
 tags_json TEXT DEFAULT '[]'
 );
 INSERT INTO interactions_new(interaction_id, domain_id, paper_id, type,
 anchor_json, content, evidence_json, anchor_id, domains_json, importance,
 created_at, updated_at, deleted_at, tags_json)
 SELECT interaction_id, domain_id, paper_id, type, anchor_json, content,
 evidence_json, anchor_id, domains_json, importance, created_at,
 updated_at, deleted_at, tags_json FROM interactions;
 DROP TABLE interactions;
 ALTER TABLE interactions_new RENAME TO interactions;
 COMMIT;
        """)
    finally:
        _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(_SCHEMA)
    _conn.executescript("""
 CREATE INDEX IF NOT EXISTS idx_interactions_anchor ON interactions(anchor_id);
 CREATE INDEX IF NOT EXISTS idx_interactions_type ON interactions(paper_id, type);
    """)


_SURVEY_DRAFT_COLS = ("survey_id", "domain_id", "domain_ids_json", "paper_ids_json",
                      "group_by", "title", "markdown", "stats_json",
                      "created_at", "updated_at")


def _has_table_fk(table: str, target: str) -> bool:
    return any(r["table"] == target
               for r in _conn.execute(f"PRAGMA foreign_key_list({table})"))


def _rebuild_survey_drafts_fk() -> int:
    """Give survey_drafts a real foreign key on an older database.

 SQLite cannot add a foreign key with ALTER TABLE, so the table is rebuilt:
 rename, let `_SCHEMA` recreate it, copy rows whose domain still exists, then
 drop the old table. Orphan drafts are dropped instead of aborting the
 migration. Returns the number of dropped orphans.
    """
    if _has_table_fk("survey_drafts", "domains"):
        return 0
    cols = ", ".join(_SURVEY_DRAFT_COLS)
    _conn.execute("DROP INDEX IF EXISTS idx_survey_domain")
    _conn.execute("ALTER TABLE survey_drafts RENAME TO survey_drafts_old")
    _conn.executescript(_SCHEMA)
    kept = _conn.execute(
        f"INSERT INTO survey_drafts({cols}) SELECT {cols} FROM survey_drafts_old"
        " WHERE domain_id IS NULL OR domain_id IN (SELECT domain_id FROM domains)").rowcount
    total = _conn.execute("SELECT COUNT(*) c FROM survey_drafts_old").fetchone()["c"]
    _conn.execute("DROP TABLE survey_drafts_old")
    return max(0, total - kept)


def _migrate_schema() -> None:
    """Bring a legacy database to the current schema in place.

 Idempotent: every column is checked, every backfill uses INSERT OR IGNORE,
 and table rebuilds only run when the foreign key they add is missing.
    """
    with _lock:
        _conn.execute("DROP TRIGGER IF EXISTS papers_ad")
        for table, col, ddl in (
            ("domains", "parent_domain_id", "parent_domain_id INTEGER"),
            ("domains", "deleted_at", "deleted_at TEXT"),
            ("papers", "doi_key", "doi_key TEXT DEFAULT ''"),
            ("papers", "parse_version", "parse_version INTEGER DEFAULT 0"),
            ("papers", "ocr_status", "ocr_status TEXT DEFAULT 'none'"),
            ("papers", "weight", "weight REAL DEFAULT 1.0"),
            ("papers", "source", "source TEXT DEFAULT ''"),
            ("papers", "external_id", "external_id TEXT DEFAULT ''"),
            ("papers", "updated_at", "updated_at TEXT"),
            ("papers", "deleted_at", "deleted_at TEXT"),
            ("papers", "progress", "progress REAL DEFAULT 0"),
            ("papers", "last_opened_at", "last_opened_at TEXT"),
            ("paper_domain_links", "is_primary", "is_primary INTEGER DEFAULT 0"),
            ("paper_domain_links", "confidence", "confidence REAL DEFAULT 0"),
            ("paper_domain_links", "status", "status TEXT DEFAULT 'auto'"),
            ("paper_domain_links", "reading_status", "reading_status TEXT"),
            ("paper_domain_links", "progress", "progress REAL"),
            ("paper_domain_links", "weight", "weight REAL DEFAULT 1.0"),
            ("paper_domain_links", "added_at", "added_at TEXT"),
            ("paper_domain_links", "updated_at", "updated_at TEXT"),
            ("paper_domain_links", "deleted_at", "deleted_at TEXT"),
            ("interactions", "anchor_id", "anchor_id INTEGER"),
            ("interactions", "domains_json", "domains_json TEXT DEFAULT ''"),
            ("interactions", "importance", "importance INTEGER DEFAULT 0"),
            ("interactions", "updated_at", "updated_at TEXT"),
            ("interactions", "deleted_at", "deleted_at TEXT"),
            ("interactions", "tags_json", "tags_json TEXT DEFAULT '[]'"),
            ("domains", "discovery_sig", "discovery_sig TEXT DEFAULT ''"),
            ("qa_sessions", "paper_ids_json", "paper_ids_json TEXT"),
            ("qa_sessions", "domain_ids_json", "domain_ids_json TEXT"),
            ("paper_domain_links", "last_page", "last_page INTEGER"),
            ("paper_domain_links", "scroll_ratio", "scroll_ratio REAL"),
            ("domains", "last_paper_id", "last_paper_id INTEGER"),
        ):
            _add_col(table, col, ddl)

        for r in _conn.execute("SELECT paper_id, doi, sha1, created_at FROM papers").fetchall():
            _conn.execute("UPDATE papers SET doi_key=?, updated_at=COALESCE(updated_at, ?)"
                          " WHERE paper_id=?", (_doi_key(r["doi"]), r["created_at"] or _now(),
                                                r["paper_id"]))

        for r in _conn.execute(
                "SELECT paper_id, domain_id, reading_status, progress, created_at"
                " FROM papers WHERE domain_id IS NOT NULL").fetchall():
            _conn.execute(
                "INSERT OR IGNORE INTO paper_domain_links(paper_id, domain_id, score, source,"
                " is_primary, status, reading_status, progress, added_at, updated_at)"
                " VALUES (?,?,0,'migrated',1,'confirmed',?,?,?,?)",
                (r["paper_id"], r["domain_id"], r["reading_status"], r["progress"],
                 r["created_at"], _now()))
        _conn.execute(
            "UPDATE paper_domain_links SET is_primary=0 WHERE is_primary=1"
            " AND domain_id IS NOT (SELECT p.domain_id FROM papers p"
            "                        WHERE p.paper_id=paper_domain_links.paper_id)")
        _conn.execute(
            "UPDATE paper_domain_links SET is_primary=1, status=CASE WHEN status='auto'"
            " THEN 'confirmed' ELSE status END"
            " WHERE domain_id = (SELECT p.domain_id FROM papers p"
            "                     WHERE p.paper_id=paper_domain_links.paper_id)")

        for r in _conn.execute(
                "SELECT interaction_id, paper_id, anchor_json FROM interactions"
                " WHERE anchor_id IS NULL AND anchor_json IS NOT NULL AND anchor_json<>''").fetchall():
            try:
                a = json.loads(r["anchor_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            quote = a.get("quote") or ""
            if not quote and a.get("page") is None:
                continue
            aid = _insert_anchor_raw(r["paper_id"], a.get("page"), quote,
                                     a.get("start"), a.get("end"), a.get("rects"))
            _conn.execute("UPDATE interactions SET anchor_id=? WHERE interaction_id=?",
                          (aid, r["interaction_id"]))

        ref = _conn.execute("SELECT type FROM sqlite_master WHERE name='paper_references'").fetchone()
        if ref and ref["type"] == "table":
            legacy = _conn.execute(
                "SELECT pr.* FROM paper_references pr WHERE EXISTS(SELECT 1 FROM papers p"
                " WHERE p.paper_id=pr.src_paper_id)").fetchall()
            for r in legacy:
                target = r["dst_paper_id"] if r["dst_paper_id"] else None
                if target and not _conn.execute("SELECT 1 FROM papers WHERE paper_id=?",
                                                (target,)).fetchone():
                    target = None
                status = "resolved" if target else "unresolved"
                _insert_citation_raw(r["src_paper_id"], target, r["dst_doi"] or "",
                                     r["dst_title"] or "", status)
            _conn.execute("DROP TABLE paper_references")
        if not _conn.execute("SELECT 1 FROM sqlite_master WHERE name='paper_references'").fetchone():
            _conn.execute(
                "CREATE VIEW paper_references AS SELECT source_paper_id AS src_paper_id,"
                " target_paper_id AS dst_paper_id, target_title AS dst_title,"
                " target_doi AS dst_doi,"
                " CASE WHEN target_paper_id IS NOT NULL THEN 1 ELSE 0 END AS resolved"
                " FROM citations")
        title_sets = []
        for p in _conn.execute("SELECT title FROM papers WHERE title IS NOT NULL AND title<>''").fetchall():
            toks = set(_title_key(p["title"]).split())
            if toks:
                title_sets.append(toks)
        if title_sets:
            for c in _conn.execute(
                    "SELECT citation_id, target_title_key FROM citations"
                    " WHERE status='unresolved' AND target_paper_id IS NULL").fetchall():
                toks = set((c["target_title_key"] or "").split())
                best = 0.0
                for t in title_sets:
                    best = max(best, len(toks & t) / len(t))
                if best < 0.6:
                    _conn.execute("UPDATE citations SET status='external'"
                                  " WHERE citation_id=?", (c["citation_id"],))
        _conn.execute(
            "DELETE FROM review_items WHERE status='pending' AND citation_id IN"
            " (SELECT citation_id FROM citations WHERE status<>'unresolved')")
        for c in _conn.execute(
                "SELECT c.citation_id, c.source_paper_id, p.domain_id FROM citations c"
                " JOIN papers p ON p.paper_id=c.source_paper_id"
                " WHERE c.status='unresolved' AND p.domain_id IS NOT NULL").fetchall():
            _conn.execute(
                "INSERT OR IGNORE INTO review_items(citation_id, paper_id, domain_id,"
                " reason, status, created_at) VALUES (?,?,?,?,'pending',?)",
                (c["citation_id"], c["source_paper_id"], c["domain_id"],
                 "参考文献未能匹配到库内论文", _now()))

        for p in _conn.execute("SELECT paper_id, meta_json, parse_version FROM papers").fetchall():
            try:
                meta = json.loads(p["meta_json"] or "{}")
            except json.JSONDecodeError:
                continue
            summaries = meta.get("summaries")
            if isinstance(summaries, dict) and summaries:
                _conn.execute(
                    "INSERT OR IGNORE INTO summaries(paper_id, granularity, research_goal,"
                    " parse_version, model, content_json, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (p["paper_id"], "five", "", p["parse_version"] or 0, "",
                     json.dumps(summaries, ensure_ascii=False), _now(), _now()))

        for d in _conn.execute("SELECT domain_id, meta_json FROM domains").fetchall():
            try:
                meta = json.loads(d["meta_json"] or "{}")
            except json.JSONDecodeError:
                continue
            for item in (meta.get("glossary") or []):
                if isinstance(item, dict) and item.get("en"):
                    _upsert_concept_raw(item["en"], item.get("zh", ""), "", d["domain_id"])

        _conn.execute(
            "INSERT OR IGNORE INTO domain_config(domain_id, updated_at)"
            " SELECT domain_id, ? FROM domains", (_now(),))
        _conn.execute(
            "INSERT OR IGNORE INTO card_templates(name, scope, content_json, is_default, created_at)"
            " VALUES (?,?,?,?,?)",
            ("默认阅读卡片", "global",
             json.dumps({"sections": ["研究问题", "方法", "核心结论", "笔记摘要", "领域关系"]},
                        ensure_ascii=False), 1, _now()))

        for row in _conn.execute(
                "SELECT DISTINCT paper_id, domain_id FROM interactions"
                " WHERE type='qa' AND (anchor_json='' OR anchor_json IS NULL)").fetchall():
            cur = _conn.execute(
                "INSERT INTO qa_sessions(paper_id, domain_id, name, created_at) VALUES (?,?,?,?)",
                (row["paper_id"], row["domain_id"], "会话 1", _now()))
            _conn.execute(
                "UPDATE interactions SET anchor_json=? WHERE type='qa' AND paper_id=?"
                " AND (anchor_json='' OR anchor_json IS NULL)",
                (json.dumps({"session_id": cur.lastrowid}), row["paper_id"]))

        statements = [
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_links_primary ON paper_domain_links(paper_id)"
            " WHERE is_primary=1 AND deleted_at IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_links_domain ON paper_domain_links(domain_id)",
            "CREATE INDEX IF NOT EXISTS idx_links_paper ON paper_domain_links(paper_id)",
            "CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi_key)",
            "CREATE INDEX IF NOT EXISTS idx_papers_deleted ON papers(deleted_at)",
            "CREATE INDEX IF NOT EXISTS idx_citations_source ON citations(source_paper_id)",
            "CREATE INDEX IF NOT EXISTS idx_citations_target ON citations(target_paper_id)",
            "CREATE INDEX IF NOT EXISTS idx_anchors_paper ON anchors(paper_id)",
            "CREATE INDEX IF NOT EXISTS idx_questions_domain ON questions(domain_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_reviews_domain ON review_items(domain_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_oplog_domain ON operation_log(domain_id, seq)",
            "CREATE INDEX IF NOT EXISTS idx_interactions_anchor ON interactions(anchor_id)",
            "CREATE INDEX IF NOT EXISTS idx_interactions_type ON interactions(paper_id, type)",
            "CREATE INDEX IF NOT EXISTS idx_domains_parent ON domains(parent_domain_id)",
        ]
        for sql in statements:
            try:
                _conn.execute(sql)
            except sqlite3.IntegrityError as e:
                print(f"[migrate] index skipped ({e}): {sql[:60]}")
        try:
            _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_sha1 ON papers(sha1)"
                          " WHERE sha1 IS NOT NULL AND sha1<>''")
            _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_doi ON papers(doi_key)"
                          " WHERE doi_key IS NOT NULL AND doi_key<>''")
        except sqlite3.IntegrityError as e:
            print(f"[migrate] duplicate identity rows kept (needs manual merge): {e}")

        _conn.commit()
        if _conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            _conn.execute("PRAGMA user_version=2")
            _conn.execute(
                "INSERT INTO operation_log(actor, op, entity_type, payload_json, created_at)"
                " VALUES ('system','schema.migrate','database',?,?)",
                (json.dumps({"from": 1, "to": 2}), _now()))
        if _conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            _conn.execute("PRAGMA user_version=3")
            _conn.execute(
                "INSERT INTO operation_log(actor, op, entity_type, payload_json, created_at)"
                " VALUES ('system','schema.migrate','database',?,?)",
                (json.dumps({"from": 2, "to": 3,
                             "added": ["interactions.tags_json", "domains.discovery_sig"]}),
                 _now()))
        if _conn.execute("PRAGMA user_version").fetchone()[0] < 4:
            _rebuild_interactions_fk()
            _conn.execute("PRAGMA user_version=4")
            _conn.execute(
                "INSERT INTO operation_log(actor, op, entity_type, payload_json, created_at)"
                " VALUES ('system','schema.migrate','database',?,?)",
                (json.dumps({"from": 3, "to": 4,
                             "changed": ["interactions.anchor_id -> anchors(anchor_id) FK"]}),
                 _now()))
        if _conn.execute("PRAGMA user_version").fetchone()[0] < 5:
            # JSON1 rejects an empty string; legacy rows can carry anchor_json=''
            fixed = _conn.execute(
                "UPDATE interactions SET anchor_json='{}'"
                " WHERE anchor_json IS NULL OR TRIM(anchor_json)=''").rowcount
            _conn.execute("PRAGMA user_version=5")
            _conn.execute(
                "INSERT INTO operation_log(actor, op, entity_type, payload_json, created_at)"
                " VALUES ('system','schema.migrate','database',?,?)",
                (json.dumps({"from": 4, "to": 5,
                             "added": ["paper_domain_links.last_page",
                                       "paper_domain_links.scroll_ratio",
                                       "domains.last_paper_id",
                                       "survey_drafts"],
                             "normalized_anchor_json": fixed}),
                 _now()))
        if _conn.execute("PRAGMA user_version").fetchone()[0] < 6:
            dropped = _rebuild_survey_drafts_fk()
            _conn.execute("DROP TRIGGER IF EXISTS domains_ad")
            _conn.executescript(_SCHEMA)
            _conn.execute("PRAGMA user_version=6")
            _conn.execute(
                "INSERT INTO operation_log(actor, op, entity_type, payload_json, created_at)"
                " VALUES ('system','schema.migrate','database',?,?)",
                (json.dumps({"from": 5, "to": 6,
                             "changed": ["survey_drafts.domain_id -> domains(domain_id) FK",
                                         "domains_ad deletes survey_drafts",
                                         "_SCHEMA declares every migrated column"],
                             "dropped_orphan_surveys": dropped}),
                 _now()))
        _conn.commit()


def paper_filename(title: str, arxiv_id: str | None, sha1: str) -> str:
    """Readable, standardized storage name: {arxiv_id|sha1[:10]}-{title-slug}.pdf.
 Dedup key stays the sha1 column — this is only for humans browsing the folder."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40].strip("-")
    prefix = arxiv_id or (sha1[:10] if sha1 else "paper")
    return f"{prefix}-{slug or 'untitled'}.pdf"


def execute(sql: str, params=()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def query(sql: str, params=()) -> list:
    with _lock:
        return _conn.execute(sql, params).fetchall()



def _smallest_free_domain_id() -> int:
    """Reuse ids released by delete/merge."""
    used = {r["domain_id"] for r in query("SELECT domain_id FROM domains")}
    i = 1
    while i in used:
        i += 1
    return i


def next_domain_slot() -> int:
    """Smallest n such that the name 'Domain-n' is free (numbering compacts after
 delete/merge instead of growing forever)."""
    names = {r["name"] for r in query("SELECT name FROM domains")}
    i = 1
    while f"Domain-{i}" in names:
        i += 1
    return i


def create_domain(name: str, description: str = "") -> int:
    new_id = _smallest_free_domain_id()
    execute(
        "INSERT INTO domains(domain_id, name, description, created_at, last_accessed_at) VALUES (?,?,?,?,?)",
        (new_id, name, description, _now(), _now()),
    )
    return new_id


def list_domains() -> list:
    rows = query(
        """SELECT d.domain_id, d.name, d.description, d.research_goal, d.weight,
 d.parent_domain_id, d.created_at, d.last_accessed_at,
 (SELECT COUNT(DISTINCT l.paper_id) FROM paper_domain_links l
 JOIN papers p ON p.paper_id = l.paper_id
 WHERE l.domain_id = d.domain_id AND l.deleted_at IS NULL
 AND p.deleted_at IS NULL) AS paper_count,
 (SELECT COUNT(*) FROM questions q
 WHERE q.domain_id = d.domain_id AND q.status='open') AS open_questions,
 (SELECT COUNT(*) FROM review_items r
 WHERE r.domain_id = d.domain_id AND r.status='pending') AS pending_reviews
 FROM domains d WHERE d.deleted_at IS NULL
 ORDER BY d.last_accessed_at DESC"""
    )
    return [dict(r) for r in rows]


def get_domain(domain_id: int) -> dict | None:
    rows = query("SELECT * FROM domains WHERE domain_id=? AND deleted_at IS NULL", (domain_id,))
    return dict(rows[0]) if rows else None


def rename_domain(domain_id: int, name: str) -> None:
    execute("UPDATE domains SET name=? WHERE domain_id=?", (name, domain_id))


def set_domain_meta(domain_id: int, meta: dict) -> None:
    execute("UPDATE domains SET meta_json=? WHERE domain_id=?", (json.dumps(meta, ensure_ascii=False), domain_id))


def touch_domain(domain_id: int) -> None:
    execute("UPDATE domains SET last_accessed_at=? WHERE domain_id=?", (_now(), domain_id))


def set_centroid(domain_id: int, blob: bytes) -> None:
    execute("UPDATE domains SET centroid=? WHERE domain_id=?", (blob, domain_id))



DOMAIN_WEIGHT_LEVELS = {"primary": 1.0, "secondary": 0.6, "casual": 0.3}
DOMAIN_WEIGHT_NAMES = {1.0: "主攻", 0.6: "交叉", 0.3: "了解"}


def set_domain_weight(domain_id: int, weight: float | str) -> float:
    """Accepts 1.0/0.6/0.3 or primary/secondary/casual; clamps to [0.1, 2.0]."""
    if isinstance(weight, str):
        key = weight.strip().lower()
        value = DOMAIN_WEIGHT_LEVELS.get(key)
        if value is None:
            raise ValueError("weight must be primary/secondary/casual or a number")
    else:
        value = float(weight)
    value = max(0.1, min(2.0, value))
    execute("UPDATE domains SET weight=? WHERE domain_id=?", (value, domain_id))
    log_operation("domain.weight", "domain", domain_id, domain_id, {"weight": value})
    return value


def domain_weights(domain_ids: list[int] | None = None) -> dict:
    rows = query("SELECT domain_id, weight FROM domains WHERE deleted_at IS NULL")
    out = {r["domain_id"]: float(r["weight"] or 1.0) for r in rows}
    if domain_ids is None:
        return out
    return {d: out.get(d, 1.0) for d in domain_ids}


def set_domain_sig(domain_id: int, sig: str) -> None:
    """Remember which discovery cluster created this domain (idempotent apply)."""
    execute("UPDATE domains SET discovery_sig=? WHERE domain_id=?", (sig or "", domain_id))


def domain_by_sig(sig: str) -> dict | None:
    if not sig:
        return None
    rows = query("SELECT * FROM domains WHERE discovery_sig=? AND deleted_at IS NULL", (sig,))
    return dict(rows[0]) if rows else None


def set_ocr_status(paper_id: int, status: str) -> None:
    execute("UPDATE papers SET ocr_status=? WHERE paper_id=?", (status, paper_id))



CONFIG_DEFAULTS = {
    "assign_threshold": "0.5",
    "link_threshold": "0.35",
    "domain_dispersion_weight": "0.3",
    "max_tokens_default": "2048",
    "max_output_limit": "100000",
    "vision_model": "",
    "llm_profiles": json.dumps([{
        "profile_id": "deepseek-default",
        "name": "DeepSeek",
        "api_base": "https://api.deepseek.com",
        "api_key": "",
        "model_fast": "deepseek-flash",
        "model_pro": "deepseek-flash",
        "thinking": "disabled",
    }], ensure_ascii=False),
    "llm_active": "DeepSeek",
    "prompt_summary": (
        "# 角色\n"
        "论文精读助手，面向中文读者精读英文论文。\n"
        "# 输入\n"
        "user 消息：论文全文（可能被截断到上限）。\n"
        "# 规则\n"
        "1. 只陈述论文实际内容，禁止推测、禁止引入外部知识；\n"
        "2. 每段中文 2-4 句，术语/方法名/数据集名保留英文；\n"
        "3. evidence 必须是支撑该段结论的一句英文原文，逐字摘自全文，"
        "禁止改写、拼接、翻译；优先含具体数字、方法名或结论性表述的句子。\n"
        "# 输出格式\n"
        "只输出 JSON 数组，section 依次为：\n"
        '["研究问题", "背景动机", "方法路径", "核心成果", "适用边界与局限"]，'
        '每个对象 {"section": str, "text": str, "evidence": str}；'
        "user 消息可能限定只生成其中一段，此时数组只含该对象。\n"
        "# 禁止\n"
        "输出 JSON 以外的任何内容（无解释、无 markdown 代码块标记）。"),
    "prompt_qa": (
        "# 角色\n"
        "学术论文问答助手，只依据给定材料回答。\n"
        "# 输入\n"
        "user 消息：检索命中的论文片段（编号 [1..n]）+ 用户问题（可含多轮历史）。\n"
        "# 规则\n"
        "1. 只依据材料回答，禁止使用材料之外的任何知识；\n"
        "2. 先给一句直接结论，再分点展开；每条结论末尾标注引用编号 [n]，"
        "n 必须指向本次材料中的编号；\n"
        "3. 材料不足时必须明确说「材料中未找到相关内容」，禁止编造；\n"
        "4. 中文回答，术语/方法名/数据集名保留英文；\n"
        "5. 数字精确引用材料原值，禁止四舍五入。\n"
        "# 输出格式\n"
        "纯文本 Markdown（可含列表），结论句尾带 [n]。\n"
        "# 禁止\n"
        "引用材料中不存在的编号；输出与材料无关的背景科普。"),
    "prompt_highlight": (
        "# 角色\n"
        "论文精读标注员，为值得精读的句子做三类荧光标注。\n"
        "# 输入\n"
        "user 消息：编号句子列表，格式「n. (p.页码) 句子」。\n"
        "# 规则\n"
        "1. 三类定义：background=研究背景/领域现状/问题与动机；"
        "approach=解决思路/方法设计/核心创新；limitation=实验结论/适用边界/局限与后续工作；\n"
        "2. 每类最多挑 6 句，全批共 6-12 句，只挑最重要、最能代表该类的完整句子；\n"
        "3. 只能引用给定的编号 n，禁止发明编号、禁止改写句子；\n"
        "4. 宁可漏标不可错标：类别不确定、句子平庸就跳过；\n"
        "5. 一批句子通常全部来自论文的同一区域，注意选取信息密度最高的句子。\n"
        "# 输出格式\n"
        '只输出 JSON 数组：[{"n": int, "category": "background|approach|limitation"}]。\n'
        "# 禁止\n"
        "输出 JSON 以外的任何内容；输出句子原文（只要编号）；category 只能是上述三类。"),
    "prompt_translate": (
        "# 角色\n"
        "学术论文翻译引擎，英译中，面向 CS 领域读者。\n"
        "# 术语表（严格遵守；未列出的专业术语一律保留英文）\n"
        "attention=注意力；Transformer=Transformer；embedding=嵌入；token=token；\n"
        "fine-tune=微调；pre-train=预训练；inference=推理；benchmark=基准测试；\n"
        "baseline=基线；state-of-the-art=最优（SOTA）；ablation=消融实验。\n"
        "# 规则\n"
        "1. 方法名/数据集名/模型名/专有名词保留英文原文；\n"
        "2. 数字、公式、引用编号 [n] 原样保留；\n"
        "3. 上下文仅用于消歧，不翻译上下文；\n"
        "4. 逐句翻译：保持每句语义完整，不合并、不拆分句子；\n"
        "5. 三种模式（由 user 消息首行的「模式：」指定）：\n"
        "   直译=逐句对应，保持原文语序与结构；\n"
        "   意译=中文流畅优先，允许语序重排与句式合并；\n"
        "   学术优化=按中文 CS 教材表述习惯改写，术语规范统一，可省略冗余虚词。\n"
        "# 输出格式\n"
        '只输出 JSON 数组：[{"i": 句子编号, "t": "该句译文"}]，i 与输入编号一一对应，按顺序。\n'
        "# 禁止\n"
        "输出 JSON 以外的任何内容；遗漏任何编号；输出解释或原文对照。"),
    "prompt_explain": (
        "# 角色\n"
        "论文精读解释器，把一句英文原文讲透给中文读者。\n"
        "# 输入\n"
        "user 消息：待解释文本 + 本文相关片段（用于定位论证角色）。\n"
        "# 规则\n"
        "1. plain：这句话在说什么，中文 1-2 句，通俗但准确；\n"
        "2. concepts：文本中的关键概念，最多 4 个，term 为英文原词，def 为中文一句话定义；\n"
        "3. why：该句在论证中的角色（假设/方法/结果/局限）+ 它为什么出现在这里，"
        "结合相关片段，中文 1-2 句；引用论文原句时保持英文原句；\n"
        "4. 只依据文本与相关片段，禁止外部知识。\n"
        "# 输出格式\n"
        '只输出 JSON 对象 {"plain": str, "concepts": [{"term": str, "def": str}], "why": str}。\n'
        "# 禁止\n"
        "输出 JSON 以外的任何内容；编造 concepts 之外的长列表。"),
}


def get_config(key: str) -> str:
    rows = query("SELECT value FROM config WHERE key=?", (key,))
    if rows:
        return rows[0]["value"]
    return CONFIG_DEFAULTS.get(key, "")


def set_config(key: str, value: str) -> None:
    execute("INSERT OR REPLACE INTO config(key, value) VALUES (?,?)", (key, value))


def set_configs(values: dict[str, str]) -> None:
    """apply a config patch atomically (PUT /config validates first)."""
    if not values:
        return
    with _lock:
        _conn.executemany("INSERT OR REPLACE INTO config(key, value) VALUES (?,?)",
                          list(values.items()))
        _conn.commit()



def get_translation(paper_id: int, sentence: str, mode: str) -> str | None:
    rows = query("SELECT translation FROM translations WHERE paper_id=? AND sentence=? AND mode=?",
                 (paper_id, sentence, mode))
    return rows[0]["translation"] if rows else None


def put_translation(paper_id: int, sentence: str, mode: str, translation: str) -> None:
    execute("INSERT OR REPLACE INTO translations(paper_id, sentence, mode, translation, created_at)"
            " VALUES (?,?,?,?,?)", (paper_id, sentence, mode, translation, now()))



def qa_sessions(paper_id: int, domain_id: int | None = None) -> list:
    """Paper-scoped sessions + domain-wide sessions, filtered by domain."""
    if domain_id is not None:
        return query(
            "SELECT * FROM qa_sessions"
            " WHERE (paper_id=? AND domain_id IS ?)"
            " OR (paper_id IS NULL AND domain_id=?)"
            " ORDER BY session_id", (paper_id, domain_id, domain_id))
    return query("SELECT * FROM qa_sessions WHERE paper_id=? AND domain_id IS NULL"
                 " ORDER BY session_id", (paper_id,))


def qa_session_get(session_id: int) -> sqlite3.Row | None:
    rows = query("SELECT * FROM qa_sessions WHERE session_id=?", (session_id,))
    return rows[0] if rows else None


def qa_session_create(paper_id: int | None, domain_id: int | None, name: str,
                      paper_ids: list[int] | None = None,
                      domain_ids: list[int] | None = None) -> int:
    """Create a QA thread. : domain_ids records every domain the thread may
 touch (cross-domain QA), while domain_id keeps the primary scope."""
    dids = domain_ids or ([domain_id] if domain_id else [])
    cur = execute("INSERT INTO qa_sessions(paper_id, domain_id, name, created_at,"
                  " paper_ids_json, domain_ids_json) VALUES (?,?,?,?,?,?)",
                  (paper_id, domain_id, name, now(),
                   json.dumps(paper_ids) if paper_ids else None,
                   json.dumps(dids) if dids else None))
    return cur.lastrowid


def qa_session_rename(session_id: int, name: str) -> None:
    execute("UPDATE qa_sessions SET name=? WHERE session_id=?", (name, session_id))


def qa_session_set_papers(session_id: int, paper_ids: list[int] | None) -> None:
    """跨论文会话的指定论文子集（None = 全领域）。"""
    execute("UPDATE qa_sessions SET paper_ids_json=? WHERE session_id=?",
            (json.dumps(paper_ids) if paper_ids else None, session_id))


def qa_session_set_domains(session_id: int, domain_ids: list[int] | None) -> None:
    execute("UPDATE qa_sessions SET domain_ids_json=? WHERE session_id=?",
            (json.dumps(domain_ids) if domain_ids else None, session_id))


def qa_session_delete(session_id: int) -> None:
    with _lock:
        # a mirrored note belongs to the turn that produced it
        _conn.execute(
            "DELETE FROM interactions WHERE type='note'"
            f" AND CAST(json_extract({_json_col('anchor_json')}, '$.qa_turn_id') AS INTEGER) IN"
            " (SELECT interaction_id FROM interactions WHERE type='qa'"
            f"  AND CAST(json_extract({_json_col('anchor_json')}, '$.session_id') AS INTEGER)=?)",
            (session_id,))
        _conn.execute("DELETE FROM interactions WHERE type='qa'"
                      f" AND CAST(json_extract({_json_col('anchor_json')}, '$.session_id')"
                      " AS INTEGER)=?",
                      (session_id,))
        _conn.execute("DELETE FROM qa_sessions WHERE session_id=?", (session_id,))
        _conn.commit()


def delete_qa_turn(interaction_id: int, paper_id: int | None = None) -> bool:
    """删除一轮已持久化的问答。

paper_id 给定时只删该论文下的记录；与该轮绑定的镜像笔记一并删除。
    """
    sql = "DELETE FROM interactions WHERE interaction_id=? AND type='qa'"
    params: list = [interaction_id]
    if paper_id is not None:
        sql += " AND paper_id=?"
        params.append(paper_id)
    with _lock:
        check = "SELECT 1 FROM interactions WHERE interaction_id=? AND type='qa'"
        params_check: list = [interaction_id]
        if paper_id is not None:
            check += " AND paper_id=?"
            params_check.append(paper_id)
        if not _conn.execute(check, tuple(params_check)).fetchone():
            return False
        # a turn marked important mirrors into a note; the mirror goes with it
        _conn.execute(
            "DELETE FROM interactions WHERE type='note'"
            f" AND CAST(json_extract({_json_col('anchor_json')}, '$.qa_turn_id') AS INTEGER)=?",
            (interaction_id,))
        cur = _conn.execute(sql, tuple(params))
        _conn.commit()
    return (cur.rowcount or 0) > 0


def set_qa_importance(paper_id: int, interaction_id: int, important: bool = True) -> dict:
    """Mark/unmark a QA turn, mirroring it into the domain note list.

 A marked turn gets one `type='note'` row carrying `qa_turn_id` in its anchor;
 the note appears in the paper notes tab and the domain note summary. The
 mirror is idempotent and is removed when the turn is unmarked or deleted.
    """
    with _lock:
        turn = _conn.execute(
            "SELECT interaction_id, paper_id, domain_id, content FROM interactions"
            " WHERE interaction_id=? AND paper_id=? AND type='qa'",
            (interaction_id, paper_id)).fetchone()
        if not turn:
            raise ValueError("qa turn not found")
        note = _conn.execute(
            "SELECT interaction_id FROM interactions WHERE type='note'"
            f" AND CAST(json_extract({_json_col('anchor_json')}, '$.qa_turn_id') AS INTEGER)=?",
            (interaction_id,)).fetchone()
        if not important:
            _conn.execute("UPDATE interactions SET importance=0, updated_at=?"
                          " WHERE interaction_id=?", (_now(), interaction_id))
            if note:
                _conn.execute("DELETE FROM interactions WHERE interaction_id=?",
                              (note["interaction_id"],))
            _conn.commit()
            return {"interaction_id": interaction_id, "important": False,
                    "note_id": None}
        try:
            content = json.loads(turn["content"] or "{}")
        except (TypeError, ValueError):
            content = {}
        question = str(content.get("question") or "").strip()
        answer = str(content.get("answer") or "").strip()
        text = f"Q：{question}\n\nA：{answer}"[:20000]
        domain_id = turn["domain_id"]
        if domain_id is None:
            p = _conn.execute("SELECT domain_id FROM papers WHERE paper_id=?",
                              (paper_id,)).fetchone()
            domain_id = p["domain_id"] if p else None
        if note:
            _conn.execute("UPDATE interactions SET content=?, domain_id=?, updated_at=?"
                          " WHERE interaction_id=?",
                          (text, domain_id, _now(), note["interaction_id"]))
            note_id = note["interaction_id"]
        else:
            anchor = json.dumps({"page": 1, "quote": question[:2000],
                                 "qa_turn_id": interaction_id},
                                ensure_ascii=False)
            anchor_id = _insert_anchor_raw(paper_id, 1, question[:2000], None, None, [],
                                           _paper_parse_version(paper_id))
            cur = _conn.execute(
                "INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content,"
                " tags_json, anchor_id, created_at, updated_at)"
                " VALUES (?,?,'note',?,?,?,?,?,?)",
                (domain_id, paper_id, anchor, text, json.dumps(["结论"], ensure_ascii=False),
                 anchor_id, _now(), _now()))
            note_id = cur.lastrowid
        _conn.execute("UPDATE interactions SET importance=1, updated_at=?"
                      " WHERE interaction_id=?", (_now(), interaction_id))
        _conn.commit()
    log_operation("qa.important", "interaction", interaction_id, domain_id,
                  {"important": True, "note_id": note_id})
    return {"interaction_id": interaction_id, "important": True, "note_id": note_id}



def delete_domain(domain_id: int) -> list[int]:
    """Delete a domain; its papers become unassigned (caller re-runs assignment).
membership lives in paper_domain_links, so deleting the domain drops
 exactly those links (plus definitions/config/questions) through the cascade
 trigger; papers/interactions/chunks only lose the domain pointer."""
    with _lock:
        orphans = [r["paper_id"] for r in _conn.execute(
            "SELECT paper_id FROM papers WHERE domain_id=?", (domain_id,)).fetchall()]
        _conn.execute("UPDATE papers SET domain_id=NULL, updated_at=? WHERE domain_id=?",
                      (_now(), domain_id))
        _conn.execute("UPDATE interactions SET domain_id=NULL WHERE domain_id=?", (domain_id,))
        _conn.execute("UPDATE qa_sessions SET domain_id=NULL WHERE domain_id=?", (domain_id,))
        _conn.execute("UPDATE chunks SET domain_id=NULL WHERE domain_id=?", (domain_id,))
        _conn.execute("DELETE FROM domains WHERE domain_id=?", (domain_id,))
        _conn.commit()
    log_operation("domain.delete", "domain", domain_id, domain_id,
                  {"orphan_papers": orphans})
    return orphans


def merge_domains(src_id: int, dst_id: int) -> int:
    """Move every paper of src into dst (links, sediment, concepts, config, cards,
 questions, reviews, snapshots, subtopics), then delete src.

the whole move is ONE transaction with an explicit rollback.
 The previous version ran 20+ statements and only committed at the end; a
 duplicated (paper_id, text) question or (citation, domain) review raised
 IntegrityError halfway through and the next unrelated write committed the
 half-merged state. Rows that would collide are now merged with INSERT OR
 IGNORE (the destination copy wins) and the source copy is deleted.

child domains are re-parented to dst (merging a domain
 into its own descendant is rejected), and concept mappings that mention src
 are rewritten to dst instead of being deleted, so A↔C mapping survives A→B
 as B↔C.
    """
    if src_id == dst_id:
        raise ValueError("cannot merge a domain into itself")
    with _lock:
        # merging a domain into its own descendant would create a cycle
        cur = dst_id
        seen = set()
        while cur:
            if cur == src_id:
                raise ValueError("cannot merge a domain into its descendant")
            if cur in seen:
                break
            seen.add(cur)
            row = _conn.execute("SELECT parent_domain_id FROM domains WHERE domain_id=?",
                                (cur,)).fetchone()
            cur = row["parent_domain_id"] if row else None
        try:
            moved = _conn.execute(
                "SELECT COUNT(*) c FROM papers WHERE domain_id=?", (src_id,)).fetchone()["c"]
            primaries = [r["paper_id"] for r in _conn.execute(
                "SELECT paper_id FROM paper_domain_links WHERE domain_id=? AND is_primary=1"
                " AND deleted_at IS NULL", (src_id,)).fetchall()]
            _conn.execute(
                "UPDATE paper_domain_links SET is_primary=0, updated_at=?"
                " WHERE paper_id IN (SELECT paper_id FROM paper_domain_links"
                " WHERE domain_id=? AND is_primary=1)", (_now(), src_id))
            _conn.execute(
                "INSERT INTO paper_domain_links(paper_id, domain_id, score, source, is_primary,"
                " confidence, status, reading_status, progress, weight, added_at, updated_at)"
                " SELECT paper_id, ?, score, source, 0, confidence, status,"
                " reading_status, progress, weight, added_at, ?"
                " FROM paper_domain_links WHERE domain_id=? AND deleted_at IS NULL"
                " ON CONFLICT(paper_id, domain_id) DO UPDATE SET"
                " score=MAX(score, excluded.score),"
                " reading_status=COALESCE(excluded.reading_status, reading_status),"
                " progress=COALESCE(excluded.progress, progress), updated_at=excluded.updated_at",
                (dst_id, _now(), src_id))
            _conn.execute("DELETE FROM paper_domain_links WHERE domain_id=?", (src_id,))
            for pid in primaries:
                _conn.execute("UPDATE paper_domain_links SET is_primary=1, updated_at=?"
                              " WHERE paper_id=? AND domain_id=?", (_now(), pid, dst_id))
            for table in ("papers", "interactions", "qa_sessions", "chunks"):
                _conn.execute(f"UPDATE {table} SET domain_id=? WHERE domain_id=?",
                              (dst_id, src_id))

            # concepts / definitions / config
            _conn.execute(
                "INSERT OR IGNORE INTO concept_definitions(concept_id, domain_id, term_zh,"
                " definition, source_paper_id, created_at, updated_at)"
                " SELECT concept_id, ?, term_zh, definition, source_paper_id, created_at, ?"
                " FROM concept_definitions WHERE domain_id=?", (dst_id, _now(), src_id))
            _conn.execute("DELETE FROM concept_definitions WHERE domain_id=?", (src_id,))
            # a mapping A↔C becomes B↔C when A is merged into B
            for m in _conn.execute(
                    "SELECT concept_id, domain_a, domain_b, mapping_type, user_note,"
                    " created_at FROM concept_mappings"
                    " WHERE domain_a=? OR domain_b=?", (src_id, src_id)).fetchall():
                a = dst_id if m["domain_a"] == src_id else m["domain_a"]
                b = dst_id if m["domain_b"] == src_id else m["domain_b"]
                if a == b:
                    continue
                if a > b:
                    a, b = b, a
                _conn.execute(
                    "INSERT OR IGNORE INTO concept_mappings(concept_id, domain_a, domain_b,"
                    " mapping_type, user_note, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (m["concept_id"], a, b, m["mapping_type"], m["user_note"] or "",
                     m["created_at"], _now()))
            _conn.execute("DELETE FROM concept_mappings WHERE domain_a=? OR domain_b=?",
                          (src_id, src_id))
            _conn.execute("INSERT OR IGNORE INTO domain_config(domain_id, highlight_density,"
                          " color_scheme_json, sort_json, reading_path_json, index_mode,"
                          " extra_json, updated_at)"
                          " SELECT ?, highlight_density, color_scheme_json, sort_json,"
                          " reading_path_json, index_mode, extra_json, ? FROM domain_config"
                          " WHERE domain_id=?", (dst_id, _now(), src_id))
            _conn.execute("DELETE FROM domain_config WHERE domain_id=?", (src_id,))

            # rows whose primary key includes domain_id: merge instead of UPDATE
            _conn.execute(
                "INSERT OR IGNORE INTO questions(paper_id, domain_id, text, status,"
                " source_type, source_id, created_at, resolved_at)"
                " SELECT paper_id, ?, text, status, source_type, source_id, created_at,"
                " resolved_at FROM questions WHERE domain_id=?", (dst_id, src_id))
            _conn.execute("DELETE FROM questions WHERE domain_id=?", (src_id,))
            _conn.execute(
                "INSERT OR IGNORE INTO review_items(citation_id, paper_id, domain_id, reason,"
                " status, created_at, resolved_at)"
                " SELECT citation_id, paper_id, ?, reason, status, created_at, resolved_at"
                " FROM review_items WHERE domain_id=?", (dst_id, src_id))
            _conn.execute("DELETE FROM review_items WHERE domain_id=?", (src_id,))
            _conn.execute(
                "INSERT OR IGNORE INTO reading_cards(paper_id, domain_id, template_id,"
                " content_json, created_at, updated_at)"
                " SELECT paper_id, ?, template_id, content_json, created_at, updated_at"
                " FROM reading_cards WHERE domain_id=?", (dst_id, src_id))
            _conn.execute("DELETE FROM reading_cards WHERE domain_id=?", (src_id,))

            # survey drafts follow the merge: owner moves to dst, selections deduped
            for row in _conn.execute(
                    "SELECT survey_id, domain_id, domain_ids_json FROM survey_drafts").fetchall():
                owner = dst_id if row["domain_id"] == src_id else row["domain_id"]
                try:
                    old_ids = json.loads(row["domain_ids_json"] or "[]")
                except (TypeError, ValueError):
                    old_ids = []
                new_ids = []
                for d in old_ids:
                    nd = dst_id if d == src_id else d
                    if nd not in new_ids:
                        new_ids.append(nd)
                if owner != row["domain_id"] or new_ids != old_ids:
                    _conn.execute(
                        "UPDATE survey_drafts SET domain_id=?, domain_ids_json=?, updated_at=?"
                        " WHERE survey_id=?",
                        (owner, json.dumps(new_ids, ensure_ascii=False), _now(),
                         row["survey_id"]))

            # archive / taxonomy follows the surviving domain
            _conn.execute("UPDATE domain_snapshots SET domain_id=? WHERE domain_id=?",
                          (dst_id, src_id))
            _conn.execute("UPDATE subtopics SET domain_id=? WHERE domain_id=?",
                          (dst_id, src_id))
            # keep the hierarchy
            _conn.execute("UPDATE domains SET parent_domain_id=? WHERE parent_domain_id=?",
                          (dst_id, src_id))

            # JSON columns that listed the merged id (scope chips / source labels)
            for s in _conn.execute(
                    "SELECT session_id, domain_ids_json FROM qa_sessions"
                    " WHERE domain_ids_json LIKE ?", (f"%{src_id}%",)).fetchall():
                try:
                    dids = json.loads(s["domain_ids_json"] or "[]")
                except (TypeError, ValueError):
                    continue
                if src_id not in dids:
                    continue
                dids = list(dict.fromkeys(dst_id if d == src_id else d for d in dids))
                _conn.execute("UPDATE qa_sessions SET domain_ids_json=? WHERE session_id=?",
                              (json.dumps(dids), s["session_id"]))
            for it in _conn.execute(
                    "SELECT interaction_id, domains_json FROM interactions"
                    " WHERE domains_json LIKE ?", (f"%{src_id}%",)).fetchall():
                try:
                    dids = json.loads(it["domains_json"] or "[]")
                except (TypeError, ValueError):
                    continue
                if src_id not in dids:
                    continue
                dids = list(dict.fromkeys(dst_id if d == src_id else d for d in dids))
                _conn.execute("UPDATE interactions SET domains_json=? WHERE interaction_id=?",
                              (json.dumps(dids), it["interaction_id"]))

            _conn.execute("DELETE FROM domains WHERE domain_id=?", (src_id,))
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
    log_operation("domain.merge", "domain", src_id, dst_id, {"moved": moved, "dst": dst_id})
    return moved



def split_domain(domain_id: int, name: str, paper_ids: list[int]) -> dict:
    """Move selected papers (and their sediment) out of a domain into a new one.

a split really *moves* the membership. A paper whose primary
 domain is the source becomes primary in the new domain; a paper that was only
 secondary in the source keeps its existing primary and gets a secondary link
 to the new domain. In both cases the source link is removed, so the paper no
 longer shows up in the old domain ( kept it as a secondary link).
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("new domain name is required")
    ids = [int(x) for x in dict.fromkeys(paper_ids or [])]
    if not ids:
        raise ValueError("select at least one paper")
    with _lock:
        src = _conn.execute("SELECT domain_id FROM domains WHERE domain_id=?",
                            (domain_id,)).fetchone()
        if not src:
            raise ValueError("domain not found")
        if _conn.execute("SELECT 1 FROM domains WHERE name=?", (name,)).fetchone():
            raise ValueError(f"domain '{name}' already exists")
        linked = {r["paper_id"] for r in _conn.execute(
            "SELECT paper_id FROM paper_domain_links"
            " WHERE domain_id=? AND deleted_at IS NULL", (domain_id,)).fetchall()}
        bad = [pid for pid in ids if pid not in linked]
        if bad:
            raise ValueError(f"papers not in this domain: {bad[:5]}")
        new_id = _smallest_free_domain_id()
        try:
            _conn.execute(
                "INSERT INTO domains(domain_id, name, description, created_at, last_accessed_at)"
                " VALUES (?,?,?,?,?)", (new_id, name, "", _now(), _now()))
            moved = 0
            marks = ",".join("?" for _ in ids)
            links = {r["paper_id"]: dict(r) for r in _conn.execute(
                "SELECT paper_id, is_primary, confidence, reading_status, progress,"
                f" last_page, scroll_ratio FROM paper_domain_links"
                f" WHERE domain_id=? AND paper_id IN ({marks}) AND deleted_at IS NULL",
                (domain_id, *ids)).fetchall()}
            for pid in ids:
                link = links.get(pid) or {}
                was_primary = bool(link.get("is_primary"))
                if was_primary:
                    _conn.execute("UPDATE paper_domain_links SET is_primary=0, updated_at=?"
                                  " WHERE paper_id=? AND is_primary=1", (_now(), pid))
                # a split moves the membership
                _conn.execute("DELETE FROM paper_domain_links WHERE paper_id=? AND domain_id=?",
                              (pid, domain_id))
                _conn.execute(
                    "INSERT INTO paper_domain_links(paper_id, domain_id, score, source,"
                    " is_primary, confidence, status, reading_status, progress,"
                    " last_page, scroll_ratio, added_at, updated_at)"
                    " VALUES (?,?,0,'split',?,?,'confirmed',?,?,?,?,?,?)"
                    " ON CONFLICT(paper_id, domain_id) DO UPDATE SET"
                    " is_primary=excluded.is_primary, source='split', status='confirmed',"
                    " reading_status=COALESCE(excluded.reading_status, reading_status),"
                    " progress=COALESCE(excluded.progress, progress),"
                    " last_page=COALESCE(excluded.last_page, last_page),"
                    " scroll_ratio=COALESCE(excluded.scroll_ratio, scroll_ratio),"
                    " updated_at=excluded.updated_at",
                    (pid, new_id, 1 if was_primary else 0, link.get("confidence") or 0,
                     link.get("reading_status"), link.get("progress"),
                     link.get("last_page"), link.get("scroll_ratio"), _now(), _now()))
                if was_primary:
                    _conn.execute("UPDATE papers SET domain_id=?, updated_at=? WHERE paper_id=?",
                                  (new_id, _now(), pid))
                moved += 1
            # the moved papers take their notes, QA, chunks and cards along
            for table in ("interactions", "qa_sessions", "chunks", "questions",
                          "review_items", "reading_cards"):
                _conn.execute(f"UPDATE {table} SET domain_id=? WHERE domain_id=?"
                              f" AND paper_id IN ({marks})", (new_id, domain_id, *ids))
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
    log_operation("domain.split", "domain", domain_id, new_id,
                  {"new": name, "moved": moved})
    return {"domain_id": new_id, "name": name, "moved": moved}


def upsert_paper(domain_id: int | None, title: str, file_path: str | None, arxiv_id: str | None = None,
                 authors: str = "", abstract: str = "", year: int | None = None,
                 sha1: str = "", meta: dict | None = None, doi: str = "",
                 source: str = "", external_id: str = "") -> int | None:
    """Idempotent by arxiv_id / file_path / sha1 / normalized DOI. Returns the
 existing paper_id when the identity is already known (one row per paper)."""
    file_path = file_path or None
    dk = _doi_key(doi)
    row = None
    for sql, param in (
            ("SELECT paper_id FROM papers WHERE arxiv_id=?", arxiv_id),
            ("SELECT paper_id FROM papers WHERE file_path=?", file_path),
            ("SELECT paper_id FROM papers WHERE sha1=?", sha1),
            ("SELECT paper_id FROM papers WHERE doi_key=? AND doi_key<>''", dk)):
        if param:
            rows = query(sql, (param,))
            if rows:
                row = rows[0]
                break
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    if row:
        pid = row["paper_id"]
        # meta_json is merged key by key; an empty argument changes nothing
        old = query("SELECT meta_json FROM papers WHERE paper_id=?", (pid,))
        merged = {}
        if old:
            try:
                merged = json.loads(old[0]["meta_json"] or "{}")
            except (TypeError, ValueError):
                merged = {}
        merged.update(meta or {})
        execute(
            "UPDATE papers SET domain_id=COALESCE(?, domain_id),"
            " title=COALESCE(NULLIF(?,''),title),"
            " authors=COALESCE(NULLIF(?,''),authors),"
            " abstract=COALESCE(NULLIF(?,''),abstract),"
            " year=COALESCE(?,year), sha1=COALESCE(NULLIF(?,''),sha1),"
            " doi=COALESCE(NULLIF(?,''),doi), doi_key=COALESCE(NULLIF(?,''),doi_key),"
            " source=COALESCE(NULLIF(?,''),source),"
            " external_id=COALESCE(NULLIF(?,''),external_id),"
            " meta_json=?, updated_at=? WHERE paper_id=?",
            (domain_id, title, authors, abstract, year, sha1, doi or "", dk,
             source, external_id, json.dumps(merged, ensure_ascii=False), _now(), pid),
        )
        if domain_id:
            _ensure_primary_link(pid, domain_id, "import")
        return pid
    cur = execute(
        "INSERT INTO papers(domain_id, title, authors, abstract, year, arxiv_id, file_path,"
        " sha1, doi, doi_key, source, external_id, parse_status, meta_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'queued', ?, ?, ?)",
        (domain_id, title, authors, abstract, year, arxiv_id, file_path, sha1, doi or "", dk,
         source, external_id, meta_json, _now(), _now()),
    )
    pid = cur.lastrowid
    if domain_id:
        _ensure_primary_link(pid, domain_id, "import")
    return pid


def list_papers(domain_id: int | None = None, include_meta: bool = True) -> list:
    """Papers visible in a domain. : membership is read from the link table,
 so a paper with 兼属 appears in every domain it belongs to; per-domain
 reading state overrides the paper-level cache when present.

`include_meta=False` is used by the list APIs. meta_json carries
 _vec64 (384 floats, base64) plus summaries and sections; sending it for
 every paper made a large domain list unnecessarily heavy. Assignment /
 discovery still ask for it explicitly.
    """
    meta_col = " p.meta_json," if include_meta else ""
    sel = ("SELECT p.paper_id, p.domain_id, p.title, p.authors, p.abstract, p.doi,"
           " p.venue, p.year, p.arxiv_id, p.file_path, p.sha1, p.parse_status,"
           " p.parse_error, p.parse_version, p.ocr_status, p.weight, p.source,"
           " p.external_id, p.created_at, p.updated_at, p.last_opened_at,"
           + meta_col +
           " l.is_primary, l.confidence, l.status AS link_status,"
           " COALESCE(l.reading_status, p.reading_status) AS reading_status,"
           " COALESCE(l.progress, p.progress) AS progress, l.score AS link_score,"
           " (SELECT COUNT(*) FROM citations c WHERE c.target_paper_id=p.paper_id"
           "   AND c.status='resolved') AS cited_by,"
           " (SELECT COUNT(*) FROM review_items r WHERE r.paper_id=p.paper_id"
           "   AND r.status='pending') AS pending_reviews")
    if domain_id is None:
        rows = query(
            sel + " FROM papers p LEFT JOIN paper_domain_links l"
            " ON l.paper_id=p.paper_id AND l.is_primary=1 AND l.deleted_at IS NULL"
            " WHERE p.deleted_at IS NULL ORDER BY p.paper_id DESC")
    else:
        rows = query(
            sel + " FROM papers p JOIN paper_domain_links l ON l.paper_id=p.paper_id"
            " WHERE l.domain_id=? AND l.deleted_at IS NULL AND p.deleted_at IS NULL"
            " ORDER BY l.is_primary DESC, p.paper_id DESC", (domain_id,))
    return [dict(r) for r in rows]


def get_paper(paper_id: int) -> dict | None:
    rows = query("SELECT * FROM papers WHERE paper_id=? AND deleted_at IS NULL", (paper_id,))
    return dict(rows[0]) if rows else None


def set_parse_status(paper_id: int, status: str, error: str = "") -> None:
    execute("UPDATE papers SET parse_status=?, parse_error=?, updated_at=? WHERE paper_id=?",
            (status, error, _now(), paper_id))


def set_progress(paper_id: int, progress: float) -> None:
    pct = max(0.0, min(100.0, progress))
    with _lock:
        _conn.execute("UPDATE papers SET progress=?, updated_at=? WHERE paper_id=?",
                      (pct, _now(), paper_id))
        _conn.execute("UPDATE paper_domain_links SET progress=?, updated_at=?"
                      " WHERE paper_id=? AND is_primary=1 AND deleted_at IS NULL",
                      (pct, _now(), paper_id))
        _conn.commit()


def merge_paper_meta(paper_id: int, patch: dict | None = None, drop: tuple[str, ...] = ()) -> None:
    """在 store 锁内按顶层键合并/删除 papers.meta_json。

patch 只覆盖它包含的键，其余键保持不变；空 patch 且空 drop 时直接返回。
    """
    patch = patch or {}
    if not patch and not drop:
        return
    with _lock:
        row = _conn.execute("SELECT meta_json FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
        if not row:
            return
        meta = json.loads(row["meta_json"] or "{}")
        meta.update(patch)
        for key in drop:
            meta.pop(key, None)
        _conn.execute("UPDATE papers SET meta_json=? WHERE paper_id=?",
                      (json.dumps(meta, ensure_ascii=False), paper_id))
        _conn.commit()


def set_paper_domain(paper_id: int, domain_id: int | None, source: str = "user") -> None:
    """Reassign the primary domain.

 invariant: a paper has at most one active primary link, and
 `papers.domain_id` is only a cache of it. The old domain keeps a
 non-primary link as historical evidence (never a second primary).
    """
    with _lock:
        rows = _conn.execute("SELECT domain_id FROM papers WHERE paper_id=?",
                             (paper_id,)).fetchone()
        if not rows:
            return
        old = rows["domain_id"]
        _conn.execute("UPDATE paper_domain_links SET is_primary=0, updated_at=?"
                      " WHERE paper_id=? AND is_primary=1", (_now(), paper_id))
        if domain_id:
            _conn.execute(
                "INSERT INTO paper_domain_links(paper_id, domain_id, score, source,"
                " is_primary, status, reading_status, progress, added_at, updated_at)"
                " VALUES (?,?,0,?,1,'confirmed',"
                " (SELECT reading_status FROM papers WHERE paper_id=?),"
                " (SELECT progress FROM papers WHERE paper_id=?), ?, ?)"
                " ON CONFLICT(paper_id, domain_id) DO UPDATE SET"
                " is_primary=1, source=excluded.source, status='confirmed',"
                " updated_at=excluded.updated_at",
                (paper_id, domain_id, source, paper_id, paper_id, _now(), _now()))
        _conn.execute("UPDATE papers SET domain_id=?, updated_at=? WHERE paper_id=?",
                      (domain_id, _now(), paper_id))
        _conn.commit()
    if old != domain_id:
        log_operation("paper.move", "paper", paper_id, domain_id,
                      {"from": old, "to": domain_id, "source": source})


def _ensure_primary_link(paper_id: int, domain_id: int, source: str = "auto") -> None:
    """Idempotently guarantee exactly one primary link for (paper, domain)."""
    with _lock:
        _conn.execute("UPDATE paper_domain_links SET is_primary=0 WHERE paper_id=?"
                      " AND domain_id<>?", (paper_id, domain_id))
        _conn.execute(
            "INSERT INTO paper_domain_links(paper_id, domain_id, score, source, is_primary,"
            " status, reading_status, progress, added_at, updated_at)"
            " VALUES (?,?,0,?,1,'confirmed',"
            " (SELECT reading_status FROM papers WHERE paper_id=?),"
            " (SELECT progress FROM papers WHERE paper_id=?), ?, ?)"
            " ON CONFLICT(paper_id, domain_id) DO UPDATE SET is_primary=1,"
            " status='confirmed', updated_at=excluded.updated_at",
            (paper_id, domain_id, source, paper_id, paper_id, _now(), _now()))
        _conn.execute("UPDATE papers SET domain_id=? WHERE paper_id=?", (domain_id, paper_id))
        _conn.commit()



def replace_chunks(paper_id: int, domain_id: int | None, chunks: list[dict],
                   mark_done: bool = True) -> None:
    """chunks: [{section, page, start_char, end_char, text}] — single transaction, idempotent.

`mark_done=False` lets the parse pipeline keep the paper in 'parsing'
 until the *whole* pipeline finished (domain assignment, review items, queue
 of the summary/glossary tasks). Marking done here meant a freshly imported
 paper was briefly visible as done while it still had no domain, so features
 keyed on the domain (reading position, survey, glossary) saw nothing.
    """
    with _lock:
        _conn.execute("DELETE FROM chunks WHERE paper_id=?", (paper_id,))
        _conn.executemany(
            "INSERT INTO chunks(paper_id, domain_id, section, page, start_char, end_char, text) VALUES (?,?,?,?,?,?,?)",
            [(paper_id, domain_id, c["section"], c["page"], c["start_char"], c["end_char"], c["text"]) for c in chunks],
        )
        if mark_done:
            _conn.execute("UPDATE papers SET parse_status='done',"
                          " parse_version=COALESCE(parse_version,0)+1 WHERE paper_id=?", (paper_id,))
        else:
            _conn.execute("UPDATE papers SET parse_version=COALESCE(parse_version,0)+1"
                          " WHERE paper_id=?", (paper_id,))
        _conn.commit()


def update_chunk_vecs(pairs: list[tuple[bytes, int]]) -> None:
    """pairs: [(vec_blob, chunk_id)] — single transaction."""
    with _lock:
        _conn.executemany("UPDATE chunks SET vec=? WHERE chunk_id=?", pairs)
        _conn.commit()


def replace_auto_highlights(paper_id: int, domain_id, items: list[tuple], replace: bool) -> None:
    """Insert auto highlights atomically; when `replace`, drop the previous auto
 set in the same transaction ( — a failed rerun must not erase them).
every highlight also gets a canonical anchor row (anchor_id)."""
    with _lock:
        if replace:
            _conn.execute(
                "DELETE FROM interactions WHERE paper_id=? AND type='highlight'"
                " AND domain_id IS ?"
                " AND anchor_json LIKE '%\"source\": \"auto\"%'", (paper_id, domain_id))
        rows = []
        for anchor_json, cat in items:
            try:
                a = json.loads(anchor_json or "{}")
            except json.JSONDecodeError:
                a = {}
            aid = _insert_anchor_raw(paper_id, a.get("page"), a.get("quote", ""),
                                     a.get("start"), a.get("end"), a.get("rects"))
            rows.append((domain_id, paper_id, "highlight", anchor_json, cat, aid, _now()))
        _conn.executemany(
            "INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content,"
            " anchor_id, created_at) VALUES (?,?,?,?,?,?,?)", rows)
        _conn.commit()


def save_references(paper_id: int, refs: list[dict]) -> None:
    """Persist the citation list and open a 待复核 item for references that look
 like a library paper but were not resolved (status='unresolved').

 refs: [{dst_paper_id, dst_title, dst_doi, status?, confidence?, raw?}]
    """
    with _lock:
        _conn.execute("DELETE FROM citations WHERE source_paper_id=?", (paper_id,))
        _conn.execute("DELETE FROM review_items WHERE paper_id=?", (paper_id,))
        p = _conn.execute("SELECT domain_id FROM papers WHERE paper_id=?",
                          (paper_id,)).fetchone()
        domain_id = p["domain_id"] if p else None
        unresolved = 0
        for r in refs[:200]:
            target = r.get("dst_paper_id")
            if target and not _conn.execute("SELECT 1 FROM papers WHERE paper_id=?",
                                            (target,)).fetchone():
                target = None
            status = r.get("status") or ""
            if target:
                status = "resolved"
            elif status not in ("unresolved", "external", "rejected"):
                status = "unresolved"
            _insert_citation_raw(paper_id, target, r.get("dst_doi", "") or "",
                                 r.get("dst_title", "") or "", status,
                                 float(r.get("confidence", 0) or 0),
                                 (r.get("raw", "") or "")[:500])
        if domain_id:
            for c in _conn.execute(
                    "SELECT citation_id FROM citations WHERE source_paper_id=?"
                    " AND status='unresolved'", (paper_id,)).fetchall():
                _conn.execute(
                    "INSERT OR IGNORE INTO review_items(citation_id, paper_id, domain_id,"
                    " reason, status, created_at) VALUES (?,?,?,?,'pending',?)",
                    (c["citation_id"], paper_id, domain_id, "参考文献未能匹配到库内论文", _now()))
        _conn.commit()



def log_operation(op: str, entity_type: str = "", entity_id: int | None = None,
                  domain_id: int | None = None, payload: dict | None = None,
                  actor: str = "user") -> None:
    execute("INSERT INTO operation_log(actor, op, entity_type, entity_id, domain_id,"
            " payload_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (actor, op, entity_type, entity_id, domain_id,
             json.dumps(payload or {}, ensure_ascii=False), _now()))


def list_operations(domain_id: int | None = None, limit: int = 50) -> list:
    if domain_id is None:
        rows = query("SELECT * FROM operation_log ORDER BY seq DESC LIMIT ?", (limit,))
    else:
        rows = query("SELECT * FROM operation_log WHERE domain_id=? ORDER BY seq DESC LIMIT ?",
                     (domain_id, limit))
    return [dict(r) for r in rows]



def list_paper_domains(paper_id: int) -> list:
    rows = query(
        "SELECT l.domain_id, d.name, l.score, l.source, l.is_primary, l.confidence,"
        " l.status, COALESCE(l.reading_status, p.reading_status) AS reading_status,"
        " COALESCE(l.progress, p.progress) AS progress, l.weight"
        " FROM paper_domain_links l JOIN domains d ON d.domain_id=l.domain_id"
        " JOIN papers p ON p.paper_id=l.paper_id"
        " WHERE l.paper_id=? AND l.deleted_at IS NULL AND d.deleted_at IS NULL"
        " ORDER BY l.is_primary DESC, l.score DESC", (paper_id,))
    return [dict(r) for r in rows]


def add_paper_domain(paper_id: int, domain_id: int, score: float = 0.0,
                     source: str = "manual", status: str = "confirmed",
                     confidence: float | None = None) -> None:
    if not query("SELECT 1 FROM papers WHERE paper_id=? AND deleted_at IS NULL", (paper_id,)):
        raise ValueError("paper not found")
    if not query("SELECT 1 FROM domains WHERE domain_id=? AND deleted_at IS NULL", (domain_id,)):
        raise ValueError("domain not found")
    with _lock:
        has_primary = _conn.execute(
            "SELECT 1 FROM paper_domain_links WHERE paper_id=? AND is_primary=1"
            " AND deleted_at IS NULL", (paper_id,)).fetchone()
        _conn.execute(
            "INSERT INTO paper_domain_links(paper_id, domain_id, score, source, is_primary,"
            " confidence, status, added_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(paper_id, domain_id) DO UPDATE SET score=excluded.score,"
            " source=excluded.source, confidence=excluded.confidence,"
            " status=excluded.status, deleted_at=NULL, updated_at=excluded.updated_at",
            (paper_id, domain_id, score, source, 0 if has_primary else 1,
             score if confidence is None else confidence, status, _now(), _now()))
        if not has_primary:
            _conn.execute("UPDATE papers SET domain_id=? WHERE paper_id=?", (domain_id, paper_id))
        _conn.commit()
    log_operation("paper.link.add", "paper", paper_id, domain_id,
                  {"source": source, "score": score})


def remove_paper_domain(paper_id: int, domain_id: int) -> None:
    with _lock:
        row = _conn.execute(
            "SELECT is_primary FROM paper_domain_links WHERE paper_id=? AND domain_id=?"
            " AND deleted_at IS NULL", (paper_id, domain_id)).fetchone()
        if not row:
            return
        _conn.execute("DELETE FROM paper_domain_links WHERE paper_id=? AND domain_id=?",
                      (paper_id, domain_id))
        if row["is_primary"]:
            nxt = _conn.execute(
                "SELECT domain_id FROM paper_domain_links WHERE paper_id=?"
                " AND deleted_at IS NULL ORDER BY score DESC LIMIT 1", (paper_id,)).fetchone()
            if nxt:
                _conn.execute("UPDATE paper_domain_links SET is_primary=1 WHERE paper_id=?"
                              " AND domain_id=?", (paper_id, nxt["domain_id"]))
                _conn.execute("UPDATE papers SET domain_id=? WHERE paper_id=?",
                              (nxt["domain_id"], paper_id))
            else:
                _conn.execute("UPDATE papers SET domain_id=NULL WHERE paper_id=?", (paper_id,))
        _conn.commit()
    log_operation("paper.link.remove", "paper", paper_id, domain_id, {})


READING_STATES = ("unread", "reading", "skimmed", "done", "review")


def set_reading_status(paper_id: int, status: str) -> None:
    """State machine guard: unknown states are rejected, not silently stored."""
    if status not in READING_STATES:
        raise ValueError(f"reading_status must be one of {READING_STATES}")
    with _lock:
        _conn.execute("UPDATE papers SET reading_status=?, updated_at=? WHERE paper_id=?",
                      (status, _now(), paper_id))
        _conn.execute("UPDATE paper_domain_links SET reading_status=?, updated_at=?"
                      " WHERE paper_id=? AND is_primary=1 AND deleted_at IS NULL",
                      (status, _now(), paper_id))
        _conn.commit()


def set_domain_reading(paper_id: int, domain_id: int, status: str | None = None,
                       progress: float | None = None,
                       last_page: int | None = None,
                       scroll_ratio: float | None = None) -> None:
    """Reading state is per (paper, domain): the same paper can be unread in one
 domain and done in another. Primary-domain changes mirror to papers.*.

`last_page` / `scroll_ratio` persist the reading *position* on
 the link, and touching a paper marks it as the domain's `last_paper_id`, so
 switching domains can reopen "上次离开时的那一页" instead of the old
 localStorage-only behaviour (which did not survive reinstall / another PC).
    """
    if status is not None and status not in READING_STATES:
        raise ValueError(f"reading_status must be one of {READING_STATES}")
    with _lock:
        linked = _conn.execute(
            "SELECT 1 FROM paper_domain_links WHERE paper_id=? AND domain_id=?"
            " AND deleted_at IS NULL", (paper_id, domain_id)).fetchone()
        if not linked:
            raise ValueError("paper is not linked to this domain")
        sets, params = [], []
        if status is not None:
            sets.append("reading_status=?")
            params.append(status)
        if progress is not None:
            sets.append("progress=?")
            params.append(max(0.0, min(100.0, progress)))
        if last_page is not None:
            sets.append("last_page=?")
            params.append(max(1, int(last_page)))
        if scroll_ratio is not None:
            sets.append("scroll_ratio=?")
            params.append(max(0.0, min(1.0, float(scroll_ratio))))
        if not sets:
            return
        _conn.execute(f"UPDATE paper_domain_links SET {', '.join(sets)}, updated_at=?"
                      f" WHERE paper_id=? AND domain_id=?", (*params, _now(), paper_id, domain_id))
        _conn.execute("UPDATE domains SET last_paper_id=?, last_accessed_at=?"
                      " WHERE domain_id=?", (paper_id, _now(), domain_id))
        _conn.execute("UPDATE papers SET last_opened_at=? WHERE paper_id=?", (_now(), paper_id))
        prim = _conn.execute("SELECT 1 FROM paper_domain_links WHERE paper_id=? AND domain_id=?"
                             " AND is_primary=1", (paper_id, domain_id)).fetchone()
        if prim:
            if status is not None:
                _conn.execute("UPDATE papers SET reading_status=? WHERE paper_id=?",
                              (status, paper_id))
            if progress is not None:
                _conn.execute("UPDATE papers SET progress=? WHERE paper_id=?",
                              (max(0.0, min(100.0, progress)), paper_id))
        _conn.commit()


def get_reading_position(paper_id: int, domain_id: int | None) -> dict:
    """per-(paper, domain) reading position for resume-on-open."""
    if not domain_id:
        return {"domain_id": None, "last_page": None, "scroll_ratio": None}
    rows = query(
        "SELECT last_page, scroll_ratio FROM paper_domain_links"
        " WHERE paper_id=? AND domain_id=? AND deleted_at IS NULL",
        (paper_id, domain_id))
    row = dict(rows[0]) if rows else {}
    return {"domain_id": domain_id,
            "last_page": row.get("last_page"),
            "scroll_ratio": row.get("scroll_ratio")}


def get_reading_positions(paper_id: int) -> list:
    """positions for every domain the paper belongs to, so opening a
 paper from a secondary domain resumes that domain's place, not the primary's."""
    rows = query(
        "SELECT domain_id, last_page, scroll_ratio FROM paper_domain_links"
        " WHERE paper_id=? AND deleted_at IS NULL ORDER BY is_primary DESC, domain_id",
        (paper_id,))
    return [dict(r) for r in rows]


def domain_last_view(domain_id: int) -> dict | None:
    """"上次离开领域时在读哪篇、读到哪一页"."""
    rows = query(
        "SELECT d.last_paper_id, p.title, p.paper_id, l.last_page, l.scroll_ratio"
        " FROM domains d LEFT JOIN papers p ON p.paper_id = d.last_paper_id"
        " LEFT JOIN paper_domain_links l ON l.paper_id = d.last_paper_id"
        " AND l.domain_id = d.domain_id AND l.deleted_at IS NULL"
        " WHERE d.domain_id=? AND d.deleted_at IS NULL", (domain_id,))
    if not rows:
        return None
    r = dict(rows[0])
    if not r.get("last_paper_id") or not r.get("paper_id"):
        return None
    return {"paper_id": r["paper_id"], "title": r.get("title") or "",
            "last_page": r.get("last_page"), "scroll_ratio": r.get("scroll_ratio")}


def delete_paper(paper_id: int) -> None:
    """Hard delete with explicit cascade (papers pre-date FK support; an AFTER
 DELETE trigger cannot safely coexist with the FTS external-content trigger).

qa_sessions / reading_cards / paper_subtopics are legacy tables
 without a paper foreign key, so they are deleted here explicitly; leaving the
 sessions behind made deleted papers show up in the QA session list forever.
    """
    with _lock:
        for table in ("interactions", "chunks", "anchors", "summaries", "questions",
                      "review_items", "translations", "paper_domain_links",
                      "reading_cards", "paper_subtopics"):
            _conn.execute(f"DELETE FROM {table} WHERE paper_id=?", (paper_id,))
        _conn.execute("DELETE FROM qa_sessions WHERE paper_id=?", (paper_id,))
        _conn.execute("DELETE FROM citations WHERE source_paper_id=?", (paper_id,))
        _conn.execute("UPDATE citations SET target_paper_id=NULL, status='unresolved'"
                      " WHERE target_paper_id=?", (paper_id,))
        _conn.execute("DELETE FROM papers WHERE paper_id=?", (paper_id,))
        _conn.commit()
    log_operation("paper.delete", "paper", paper_id, None, {})


def refresh_review_items(paper_id: int) -> int:
    """Create 待复核 items for unresolved citations. Needed when references are
 parsed before the paper gets its domain (import-time domain is applied later)."""
    p = get_paper(paper_id)
    if not p or not p["domain_id"]:
        return 0
    created = 0
    with _lock:
        for c in _conn.execute(
                "SELECT citation_id FROM citations WHERE source_paper_id=?"
                " AND status='unresolved'", (paper_id,)).fetchall():
            cur = _conn.execute(
                "INSERT OR IGNORE INTO review_items(citation_id, paper_id, domain_id,"
                " reason, status, created_at) VALUES (?,?,?,?,'pending',?)",
                (c["citation_id"], paper_id, p["domain_id"],
                 "参考文献未能匹配到库内论文", _now()))
            created += cur.rowcount if cur.rowcount > 0 else 0
        _conn.commit()
    return created


def set_paper_weight(paper_id: int, weight: float) -> None:
    execute("UPDATE papers SET weight=?, updated_at=? WHERE paper_id=?",
            (max(0.0, min(10.0, weight)), _now(), paper_id))



def list_citations(paper_id: int | None = None, domain_id: int | None = None,
                   status: str | None = None, limit: int = 300) -> list:
    sql = ("SELECT c.*, p.title AS source_title, p.domain_id AS source_domain_id,"
           " t.title AS target_title_lib"
           " FROM citations c JOIN papers p ON p.paper_id=c.source_paper_id"
           " LEFT JOIN papers t ON t.paper_id=c.target_paper_id WHERE 1=1")
    params: list = []
    if paper_id is not None:
        sql += " AND c.source_paper_id=?"
        params.append(paper_id)
    if domain_id is not None:
        sql += (" AND EXISTS(SELECT 1 FROM paper_domain_links l WHERE l.paper_id=c.source_paper_id"
                " AND l.domain_id=? AND l.deleted_at IS NULL)")
        params.append(domain_id)
    if status:
        sql += " AND c.status=?"
        params.append(status)
    sql += " ORDER BY c.citation_id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in query(sql, tuple(params))]


def resolve_citation(citation_id: int, target_paper_id: int | None = None,
                     status: str = "resolved") -> dict:
    if status not in ("resolved", "unresolved", "external", "rejected"):
        raise ValueError("invalid citation status")
    with _lock:
        row = _conn.execute("SELECT * FROM citations WHERE citation_id=?",
                            (citation_id,)).fetchone()
        if not row:
            raise ValueError("citation not found")
        if target_paper_id is not None:
            t = _conn.execute("SELECT paper_id FROM papers WHERE paper_id=?",
                              (target_paper_id,)).fetchone()
            if not t:
                raise ValueError("target paper not found")
            status = "resolved"
        _conn.execute("UPDATE citations SET target_paper_id=COALESCE(?,target_paper_id),"
                      " status=?, updated_at=? WHERE citation_id=?",
                      (target_paper_id, status, _now(), citation_id))
        if status in ("resolved", "rejected", "external"):
            _conn.execute("UPDATE review_items SET status=?, resolved_at=?"
                          " WHERE citation_id=? AND status='pending'",
                          ("confirmed" if status == "resolved" else "rejected", _now(),
                           citation_id))
        _conn.commit()
    log_operation("citation.resolve", "citation", citation_id, row["source_paper_id"],
                  {"status": status, "target": target_paper_id})
    return dict(_conn.execute("SELECT * FROM citations WHERE citation_id=?",
                              (citation_id,)).fetchone())



def add_anchor(paper_id: int, page=None, quote: str = "", start=None, end=None,
               rects=None, parse_version: int | None = None) -> int:
    with _lock:
        pv = _paper_parse_version(paper_id) if parse_version is None else parse_version
        aid = _insert_anchor_raw(paper_id, page, quote, start, end, rects, pv)
        _conn.commit()
    return aid


def get_anchor(anchor_id: int) -> dict | None:
    rows = query("SELECT * FROM anchors WHERE anchor_id=?", (anchor_id,))
    return dict(rows[0]) if rows else None



def save_summary(paper_id: int, summaries: dict, granularity: str = "five",
                 research_goal: str = "", model: str = "",
                 parse_version: int | None = None) -> None:
    if parse_version is None:
        row = query("SELECT parse_version FROM papers WHERE paper_id=?", (paper_id,))
        parse_version = (row[0]["parse_version"] if row else 0) or 0
    execute("INSERT INTO summaries(paper_id, granularity, research_goal, parse_version,"
            " model, content_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(paper_id, granularity, research_goal, parse_version)"
            " DO UPDATE SET model=excluded.model, content_json=excluded.content_json,"
            " updated_at=excluded.updated_at",
            (paper_id, granularity, research_goal, parse_version, model,
             json.dumps(summaries, ensure_ascii=False), _now(), _now()))


def get_summary(paper_id: int, granularity: str = "five", research_goal: str = "",
                parse_version: int | None = None) -> dict | None:
    if parse_version is None:
        row = query("SELECT parse_version FROM papers WHERE paper_id=?", (paper_id,))
        parse_version = (row[0]["parse_version"] if row else 0) or 0
    rows = query("SELECT * FROM summaries WHERE paper_id=? AND granularity=?"
                 " AND research_goal=? AND parse_version=?",
                 (paper_id, granularity, research_goal, parse_version))
    if not rows:
        return None
    out = dict(rows[0])
    out["content"] = json.loads(out["content_json"] or "{}")
    return out



def upsert_concept(term_en: str, term_zh: str = "", definition: str = "",
                   domain_id: int | None = None, source_paper_id: int | None = None) -> int:
    with _lock:
        cid = _upsert_concept_raw(term_en, term_zh, definition, domain_id, source_paper_id)
        _conn.commit()
    return cid


def list_concepts(domain_id: int | None = None) -> list:
    if domain_id is None:
        rows = query("SELECT c.*, NULL AS domain_id, NULL AS term_zh, NULL AS definition"
                     " FROM concepts c ORDER BY c.display_en")
    else:
        rows = query(
            "SELECT c.concept_id, c.concept_key, c.display_en, c.display_zh,"
            " d.domain_id, d.term_zh, d.definition, d.source_paper_id"
            " FROM concepts c LEFT JOIN concept_definitions d"
            " ON d.concept_id=c.concept_id AND d.domain_id=?"
            " WHERE c.concept_id IN (SELECT concept_id FROM concept_definitions WHERE domain_id=?)"
            " ORDER BY c.display_en", (domain_id, domain_id))
    return [dict(r) for r in rows]


def set_concept_definition(concept_id: int, domain_id: int, definition: str,
                           term_zh: str = "", source_paper_id: int | None = None) -> None:
    if not query("SELECT 1 FROM concepts WHERE concept_id=?", (concept_id,)):
        raise ValueError("concept not found")
    execute("INSERT INTO concept_definitions(concept_id, domain_id, term_zh, definition,"
            " source_paper_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(concept_id, domain_id) DO UPDATE SET definition=excluded.definition,"
            " term_zh=COALESCE(NULLIF(excluded.term_zh,''),term_zh),"
            " source_paper_id=COALESCE(excluded.source_paper_id,source_paper_id),"
            " updated_at=excluded.updated_at",
            (concept_id, domain_id, term_zh, definition, source_paper_id, _now(), _now()))


def add_concept_mapping(concept_id: int, domain_a: int, domain_b: int,
                        mapping_type: str = "related", user_note: str = "") -> int:
    if mapping_type not in ("same", "different", "related"):
        raise ValueError("mapping_type must be same/different/related")
    if domain_a == domain_b:
        raise ValueError("a concept mapping needs two different domains")
    a, b = sorted((int(domain_a), int(domain_b)))
    cur = execute("INSERT INTO concept_mappings(concept_id, domain_a, domain_b, mapping_type,"
                  " user_note, created_at, updated_at) VALUES (?,?,?,?,?,?,?)"
                  " ON CONFLICT(concept_id, domain_a, domain_b) DO UPDATE SET"
                  " mapping_type=excluded.mapping_type, user_note=excluded.user_note,"
                  " updated_at=excluded.updated_at",
                  (concept_id, a, b, mapping_type, user_note, _now(), _now()))
    return cur.lastrowid


def list_concept_mappings(domain_id: int | None = None) -> list:
    sql = ("SELECT m.*, c.display_en, c.display_zh, da.name AS domain_a_name,"
           " db.name AS domain_b_name FROM concept_mappings m"
           " JOIN concepts c ON c.concept_id=m.concept_id"
           " JOIN domains da ON da.domain_id=m.domain_a"
           " JOIN domains db ON db.domain_id=m.domain_b")
    params: tuple = ()
    if domain_id is not None:
        sql += " WHERE m.domain_a=? OR m.domain_b=?"
        params = (domain_id, domain_id)
    return [dict(r) for r in query(sql + " ORDER BY m.mapping_id DESC", params)]



NOTE_TAGS = ("问题", "方法", "结论", "局限", "想法")


def _tags_json(tags: list[str] | None) -> str:
    clean: list[str] = []
    for t in tags or []:
        t = str(t).strip()
        if t and t in NOTE_TAGS and t not in clean:
            clean.append(t)
    return json.dumps(clean, ensure_ascii=False)


def parse_tags(blob) -> list[str]:
    try:
        data = json.loads(blob or "[]")
    except (TypeError, ValueError):
        return []
    return [t for t in data if isinstance(t, str)]


def list_notes(paper_id: int, tag: str | None = None) -> list:
    sql = "SELECT * FROM interactions WHERE paper_id=? AND type='note'"
    params: list = [paper_id]
    if tag:
        sql += " AND tags_json LIKE ?"
        params.append(f'%"{tag}"%')
    sql += " ORDER BY interaction_id DESC"
    return [dict(r) for r in query(sql, tuple(params))]


def domain_notes(domain_id: int, tag: str | None = None, limit: int = 200) -> list:
    sql = ("SELECT i.*, p.title AS paper_title FROM interactions i"
           " JOIN papers p ON p.paper_id=i.paper_id"
           " WHERE i.type='note' AND i.domain_id=? AND p.deleted_at IS NULL")
    params: list = [domain_id]
    if tag:
        sql += " AND i.tags_json LIKE ?"
        params.append(f'%"{tag}"%')
    sql += " ORDER BY i.interaction_id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return [dict(r) for r in query(sql, tuple(params))]


def list_notes_for_qa(paper_id: int | None = None, domain_ids: list[int] | None = None,
                      limit: int = 80) -> list[dict]:
    """可作为问答证据的用户笔记。

与 chunk 使用同一套范围过滤，返回带 page/quote 的记录，共用 [n] 编号。
    """
    sql = ("SELECT i.interaction_id, i.paper_id, i.domain_id, i.content,"
           " i.anchor_json, p.title AS paper_title FROM interactions i"
           " JOIN papers p ON p.paper_id=i.paper_id"
           " WHERE i.type='note' AND p.deleted_at IS NULL")
    params: list = []
    if paper_id is not None:
        sql += " AND i.paper_id=?"
        params.append(paper_id)
    elif domain_ids:
        marks = ",".join("?" for _ in domain_ids)
        sql += f" AND i.domain_id IN ({marks})"
        params.extend(domain_ids)
    else:
        return []
    sql += " ORDER BY i.interaction_id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    out = []
    for r in query(sql, tuple(params)):
        try:
            a = json.loads(r["anchor_json"] or "{}")
        except (TypeError, ValueError):
            a = {}
        out.append({
            "note_id": r["interaction_id"], "paper_id": r["paper_id"],
            "domain_id": r["domain_id"], "text": r["content"] or "",
            "quote": a.get("quote") or "", "page": a.get("page"),
            "paper_title": r["paper_title"] or "",
        })
    return out


def add_note(paper_id: int, domain_id, page, quote: str, hl_id, text: str,
             tags: list[str] | None = None, start=None, end=None,
             rects=None) -> int:
    """Note + canonical anchor. : the anchor keeps the character offsets and
 rectangles of the selection, so repeated quotes on the same page stay
 distinguishable (the original reason `anchors` exists)."""
    anchor = json.dumps({"page": page, "quote": quote, "hl_id": hl_id,
                         "start": start, "end": end, "rects": rects or []},
                        ensure_ascii=False)
    anchor_id = add_anchor(paper_id, page, quote, start, end, rects)
    cur = execute(
        "INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content,"
        " tags_json, anchor_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (domain_id, paper_id, "note", anchor, text, _tags_json(tags), anchor_id, _now()))
    return cur.lastrowid


def update_note(paper_id: int, note_id: int, text: str | None = None,
                tags: list[str] | None = None) -> None:
    sets, params = [], []
    if text is not None:
        sets.append("content=?")
        params.append(text)
    if tags is not None:
        sets.append("tags_json=?")
        params.append(_tags_json(tags))
    if not sets:
        return
    params += [note_id, paper_id]
    execute(f"UPDATE interactions SET {', '.join(sets)}"
            " WHERE interaction_id=? AND paper_id=? AND type='note'", tuple(params))


def delete_note(paper_id: int, note_id: int) -> None:
    execute("DELETE FROM interactions WHERE interaction_id=? AND paper_id=? AND type='note'",
            (note_id, paper_id))



def concept_context(domain_ids: list[int], text: str, limit: int = 6) -> list[dict]:
    """text 中出现、且设有分领域定义的术语。

返回 [{concept_id, term, term_zh, definitions:[{domain_id, domain, definition}],
mappings:[{a, b, type}]}]；只有确实存在该领域对的映射条目时才返回对应概念。
    """
    ids = [d for d in dict.fromkeys(domain_ids or []) if d]
    low = (text or "").lower()
    if not ids or not low:
        return []
    marks = ",".join("?" for _ in ids)
    rows = query(
        "SELECT c.concept_id, c.display_en, d.domain_id, d.term_zh, d.definition,"
        " dm.name AS domain_name FROM concepts c"
        " JOIN concept_definitions d ON d.concept_id=c.concept_id"
        " JOIN domains dm ON dm.domain_id=d.domain_id"
        f" WHERE d.domain_id IN ({marks})", tuple(ids))
    by: dict[int, dict] = {}
    for r in rows:
        term = r["display_en"] or ""
        if not term or term.lower() not in low:
            continue
        item = by.setdefault(r["concept_id"], {
            "concept_id": r["concept_id"], "term": term,
            "term_zh": r["term_zh"] or "", "definitions": []})
        item["definitions"].append({"domain_id": r["domain_id"], "domain": r["domain_name"],
                                    "definition": (r["definition"] or "").strip()})
    out = []
    for item in list(by.values())[:limit]:
        ds = sorted({d["domain_id"] for d in item["definitions"]})
        mappings = []
        if len(ds) >= 2:
            marks = ",".join("?" for _ in ds)
            mrows = query(
                "SELECT domain_a, domain_b, mapping_type FROM concept_mappings"
                f" WHERE concept_id=? AND domain_a IN ({marks}) AND domain_b IN ({marks})"
                " ORDER BY domain_a, domain_b",
                (item["concept_id"], *ds, *ds))
            mappings = [{"a": r["domain_a"], "b": r["domain_b"], "type": r["mapping_type"]}
                        for r in mrows]
        out.append({**item, "mappings": mappings,
                    "mapping": mappings[0]["type"] if mappings else ""})
    return out


def concept_overlaps(domain_id: int, text: str | None = None, limit: int = 5) -> list[dict]:
    """Concepts defined in `domain_id` that also have a definition elsewhere.

this is the "主动提示" feed. A paper card / overview can show
 "term X is also defined in domain Y" without waiting for the user to open
 the concept tool. `text` (title+abstract or a question) narrows the terms.
    """
    rows = query(
        "SELECT c.concept_id, c.display_en, c.display_zh, d.domain_id,"
        " d.term_zh, d.definition, dm.name AS domain_name"
        " FROM concepts c"
        " JOIN concept_definitions d ON d.concept_id=c.concept_id"
        " JOIN domains dm ON dm.domain_id=d.domain_id"
        " WHERE c.concept_id IN (SELECT concept_id FROM concept_definitions"
        "                        WHERE domain_id=?)"
        " ORDER BY c.concept_id, d.domain_id", (domain_id,))
    low = (text or "").lower()
    by: dict[int, dict] = {}
    for r in rows:
        term = r["display_en"] or ""
        if not term or (low and term.lower() not in low):
            continue
        item = by.setdefault(r["concept_id"], {
            "concept_id": r["concept_id"], "term": term,
            "term_zh": r["display_zh"] or r["term_zh"] or "", "definitions": []})
        item["definitions"].append({
            "domain_id": r["domain_id"], "domain": r["domain_name"],
            "definition": (r["definition"] or "").strip()})
    out = []
    for item in by.values():
        doms = {d["domain_id"] for d in item["definitions"]}
        if len(doms) < 2:
            continue
        out.append({**item, "domains": sorted(doms)})
        if len(out) >= limit:
            break
    return out



def add_question(paper_id: int | None, domain_id: int | None, text: str,
                 source_type: str = "", source_id: int | None = None) -> int:
    text = (text or "").strip()
    if not text:
        raise ValueError("question text is empty")
    cur = execute("INSERT INTO questions(paper_id, domain_id, text, status, source_type,"
                  " source_id, created_at) VALUES (?,?,?,'open',?,?,?)"
                  " ON CONFLICT(paper_id, domain_id, text) DO UPDATE SET"
                  " source_type=excluded.source_type, source_id=excluded.source_id",
                  (paper_id, domain_id, text, source_type, source_id, _now()))
    return cur.lastrowid


def list_questions(domain_id: int | None = None, paper_id: int | None = None,
                   status: str | None = "open") -> list:
    sql = ("SELECT q.*, p.title AS paper_title FROM questions q"
           " LEFT JOIN papers p ON p.paper_id=q.paper_id WHERE 1=1")
    params: list = []
    if domain_id is not None:
        sql += " AND q.domain_id=?"
        params.append(domain_id)
    if paper_id is not None:
        sql += " AND q.paper_id=?"
        params.append(paper_id)
    if status:
        sql += " AND q.status=?"
        params.append(status)
    return [dict(r) for r in query(sql + " ORDER BY q.question_id DESC", tuple(params))]


def resolve_question(question_id: int, status: str = "resolved") -> None:
    if status not in ("open", "resolved", "dismissed"):
        raise ValueError("invalid question status")
    execute("UPDATE questions SET status=?, resolved_at=? WHERE question_id=?",
            (status, _now() if status != "open" else None, question_id))


def list_reviews(domain_id: int | None = None, status: str | None = "pending") -> list:
    sql = ("SELECT r.*, c.target_title, c.target_doi, c.target_paper_id,"
           " p.title AS source_title FROM review_items r"
           " JOIN citations c ON c.citation_id=r.citation_id"
           " JOIN papers p ON p.paper_id=r.paper_id WHERE 1=1")
    params: list = []
    if domain_id is not None:
        sql += " AND r.domain_id=?"
        params.append(domain_id)
    if status:
        sql += " AND r.status=?"
        params.append(status)
    return [dict(r) for r in query(sql + " ORDER BY r.item_id DESC", tuple(params))]


def resolve_review(item_id: int, status: str = "confirmed",
                   target_paper_id: int | None = None) -> None:
    if status not in ("confirmed", "rejected", "pending"):
        raise ValueError("invalid review status")
    with _lock:
        row = _conn.execute("SELECT citation_id FROM review_items WHERE item_id=?",
                            (item_id,)).fetchone()
        if not row:
            raise ValueError("review item not found")
        _conn.execute("UPDATE review_items SET status=?, resolved_at=? WHERE item_id=?",
                      (status, _now() if status != "pending" else None, item_id))
        if status != "pending":
            _conn.execute("UPDATE citations SET status=?, target_paper_id=COALESCE(?,"
                          " target_paper_id), updated_at=? WHERE citation_id=?",
                          ("resolved" if status == "confirmed" else "rejected",
                           target_paper_id, _now(), row["citation_id"]))
        _conn.commit()



DOMAIN_CONFIG_DEFAULTS = {
    "highlight_density": "normal",
    "color_scheme_json": "{}",
    "sort_json": "{}",
    "reading_path_json": "{}",
    "index_mode": "logical",
    "extra_json": "{}",
}


def get_domain_config(domain_id: int) -> dict:
    rows = query("SELECT * FROM domain_config WHERE domain_id=?", (domain_id,))
    out = dict(DOMAIN_CONFIG_DEFAULTS)
    if rows:
        out.update({k: v for k, v in dict(rows[0]).items() if v is not None})
    out["domain_id"] = domain_id
    return out


def set_domain_config(domain_id: int, patch: dict) -> dict:
    allowed = {k: patch[k] for k in patch if k in DOMAIN_CONFIG_DEFAULTS}
    if allowed:
        cols = ", ".join(f"{k}=?" for k in allowed)
        vals = list(allowed.values())
        execute(f"INSERT INTO domain_config(domain_id, {', '.join(allowed)}, updated_at)"
                f" VALUES (?,{','.join('?' for _ in allowed)},?)"
                f" ON CONFLICT(domain_id) DO UPDATE SET {cols}, updated_at=excluded.updated_at",
                (domain_id, *vals, _now(), *vals))
    return get_domain_config(domain_id)


def record_export(scope: str, scope_id: int | None, fmt: str, paper_ids: list[int],
                  path: str = "", note: str = "") -> int:
    cur = execute("INSERT INTO export_records(scope, scope_id, format, paper_ids_json,"
                  " path, note, created_at) VALUES (?,?,?,?,?,?,?)",
                  (scope, scope_id, fmt, json.dumps(paper_ids), path, note, _now()))
    log_operation("export.create", "export", cur.lastrowid, scope_id,
                  {"format": fmt, "scope": scope})
    return cur.lastrowid


def register_sync_source(kind: str, external_id: str, config: dict | None = None) -> None:
    execute("INSERT INTO sync_sources(kind, external_id, config_json, last_sync_at)"
            " VALUES (?,?,?,?) ON CONFLICT(kind, external_id) DO UPDATE SET"
            " config_json=excluded.config_json, last_sync_at=excluded.last_sync_at",
            (kind, external_id, json.dumps(config or {}, ensure_ascii=False), _now()))


def create_snapshot(domain_id: int, name: str = "") -> int:
    """归档领域的完整工作状态，并记录操作日志水位。

包含归属关系与每篇的阅读状态/位置、未决问题、待复核引用、领域配置、最后打开的论文，
以及只读的笔记与问答副本。
    """
    if not get_domain(domain_id):
        raise ValueError("domain not found")
    with _lock:
        last_seq = _conn.execute("SELECT COALESCE(MAX(seq),0) s FROM operation_log").fetchone()["s"]
        papers = [dict(r) for r in _conn.execute(
            "SELECT l.paper_id, l.is_primary, l.confidence, l.status, l.reading_status,"
            " l.progress, l.last_page, l.scroll_ratio, l.weight, p.title, p.year"
            " FROM paper_domain_links l LEFT JOIN papers p ON p.paper_id=l.paper_id"
            " WHERE l.domain_id=? AND l.deleted_at IS NULL AND p.deleted_at IS NULL",
            (domain_id,)).fetchall()]
        questions = [dict(r) for r in _conn.execute(
            "SELECT question_id, paper_id, text, source_type, source_id FROM questions"
            " WHERE domain_id=? AND status='open'", (domain_id,)).fetchall()]
        reviews = [dict(r) for r in _conn.execute(
            "SELECT item_id, citation_id, paper_id, reason FROM review_items"
            " WHERE domain_id=? AND status='pending'", (domain_id,)).fetchall()]
        notes = [dict(r) for r in _conn.execute(
            "SELECT interaction_id, paper_id, anchor_json, content, tags_json, created_at"
            " FROM interactions WHERE domain_id=? AND type='note' AND deleted_at IS NULL"
            " ORDER BY interaction_id", (domain_id,)).fetchall()]
        qa = [dict(r) for r in _conn.execute(
            "SELECT interaction_id, paper_id, anchor_json, content, importance, created_at"
            " FROM interactions WHERE domain_id=? AND type='qa' AND deleted_at IS NULL"
            " ORDER BY interaction_id", (domain_id,)).fetchall()]
        last_paper_id = _conn.execute(
            "SELECT last_paper_id FROM domains WHERE domain_id=?", (domain_id,)).fetchone()
        payload = {
            "schema": 5,
            "config": get_domain_config(domain_id),
            "papers": papers,
            "open_questions": questions,
            "pending_reviews": reviews,
            "notes": notes,
            "qa_turns": qa,
            "last_paper_id": (last_paper_id["last_paper_id"] if last_paper_id else None),
            "counts": {"papers": len(papers), "open_questions": len(questions),
                       "pending_reviews": len(reviews), "notes": len(notes),
                       "qa_turns": len(qa)},
        }
        cur = _conn.execute(
            "INSERT INTO domain_snapshots(domain_id, name, last_seq, schema_version,"
            " snapshot_json, created_at) VALUES (?,?,?,5,?,?)",
            (domain_id, name or f"snapshot-{_now()}", last_seq,
             json.dumps(payload, ensure_ascii=False), _now()))
        _conn.commit()
    log_operation("domain.snapshot", "domain", domain_id, domain_id,
                  {"snapshot_id": cur.lastrowid, "last_seq": last_seq})
    return cur.lastrowid


def list_snapshots(domain_id: int) -> list:
    rows = query("SELECT snapshot_id, domain_id, name, last_seq, schema_version, created_at"
                 " FROM domain_snapshots WHERE domain_id=? ORDER BY snapshot_id DESC", (domain_id,))
    return [dict(r) for r in rows]


def add_suggestion(source_domain_id: int, target_domain_id: int, description: str,
                   evidence: list | None = None, confidence: float = 0.0,
                   fingerprint: str = "") -> int:
    """Idempotent by fingerprint: an ignored suggestion is never re-pushed just
 because it was regenerated with a new id."""
    if source_domain_id == target_domain_id:
        raise ValueError("source and target domain must differ")
    if not get_domain(source_domain_id) or not get_domain(target_domain_id):
        raise ValueError("domain not found")
    fp = fingerprint or hashlib.sha1(
        f"{source_domain_id}|{target_domain_id}|{_norm_key(description)}".encode("utf-8")
    ).hexdigest()[:24]
    cur = execute(
        "INSERT INTO cross_domain_suggestions(source_domain_id, target_domain_id, fingerprint,"
        " description, evidence_json, confidence, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,'pending',?,?)"
        " ON CONFLICT(fingerprint) DO UPDATE SET description=excluded.description,"
        " evidence_json=excluded.evidence_json, confidence=excluded.confidence",
        (source_domain_id, target_domain_id, fp, description,
         json.dumps(evidence or [], ensure_ascii=False),
         max(0.0, min(1.0, float(confidence))), _now(), _now()))
    rows = query("SELECT suggestion_id FROM cross_domain_suggestions WHERE fingerprint=?", (fp,))
    return rows[0]["suggestion_id"] if rows else cur.lastrowid


def list_suggestions(domain_id: int | None = None, status: str | None = None) -> list:
    sql = "SELECT * FROM cross_domain_suggestions WHERE 1=1"
    params: list = []
    if domain_id is not None:
        sql += " AND (source_domain_id=? OR target_domain_id=?)"
        params += [domain_id, domain_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    return [dict(r) for r in query(sql + " ORDER BY suggestion_id DESC", tuple(params))]


def set_suggestion_feedback(suggestion_id: int, status: str,
                            snooze_until: str | None = None) -> None:
    if status not in ("pending", "explored", "ignored", "snoozed"):
        raise ValueError("invalid suggestion status")
    execute("UPDATE cross_domain_suggestions SET status=?, snooze_until=?, updated_at=?"
            " WHERE suggestion_id=?", (status, snooze_until, _now(), suggestion_id))



def build_reading_card(paper_id: int, domain_id: int | None = None) -> dict:
    """Assemble a card from what the user already produced (no LLM call)."""
    p = get_paper(paper_id)
    if not p:
        raise ValueError("paper not found")
    domain_id = domain_id or p["domain_id"]
    goal = ""
    if domain_id:
        d = get_domain(domain_id)
        goal = (d or {}).get("research_goal", "") or ""
    summary = get_summary(paper_id, "five", goal, int(p.get("parse_version") or 0))
    content = (summary or {}).get("content")
    if not content:
        # falls back to meta_json.summaries like the other export paths
        try:
            content = (json.loads(p.get("meta_json") or "{}").get("summaries") or {})
        except (TypeError, ValueError):
            content = {}
    sections = {}
    for s in (content or {}).get("five", []):
        if isinstance(s, dict) and s.get("section"):
            sections[s["section"]] = s.get("text", "")
    notes = [r["content"] for r in query(
        "SELECT content FROM interactions WHERE paper_id=? AND type='note'"
        " AND deleted_at IS NULL ORDER BY interaction_id", (paper_id,))]
    n_hl = query("SELECT COUNT(*) c FROM interactions WHERE paper_id=? AND type='highlight'"
                 " AND deleted_at IS NULL", (paper_id,))[0]["c"]
    cites = query("SELECT COUNT(*) c FROM citations WHERE source_paper_id=?", (paper_id,))[0]["c"]
    questions = [r["text"] for r in query(
        "SELECT text FROM questions WHERE paper_id=? AND status='open'", (paper_id,))]
    return {
        "paper_id": paper_id, "domain_id": domain_id,
        "title": p["title"], "authors": p["authors"], "year": p["year"],
        "research_question": sections.get("研究问题", ""),
        "method": sections.get("方法路径", ""),
        "findings": sections.get("核心成果", ""),
        "limitations": sections.get("适用边界与局限", ""),
        "notes": notes, "open_questions": questions,
        "n_highlights": n_hl, "n_citations": cites,
    }


def get_reading_card(paper_id: int, domain_id: int | None = None) -> dict | None:
    rows = query("SELECT * FROM reading_cards WHERE paper_id=? AND domain_id IS ?",
                 (paper_id, domain_id))
    if not rows:
        return None
    out = dict(rows[0])
    out["content"] = json.loads(out["content_json"] or "{}")
    return out


def save_reading_card(paper_id: int, domain_id: int | None, content: dict,
                      template_id: int | None = None) -> int:
    """保存 (paper, domain) 的阅读卡片。

SQLite 中 NULL domain_id 不参与 UNIQUE 约束，因此先删除旧的 NULL 行；
非 NULL 的行按 upsert 更新。
    """
    payload = json.dumps(content, ensure_ascii=False)
    with _lock:
        if domain_id is None:
            _conn.execute("DELETE FROM reading_cards WHERE paper_id=? AND domain_id IS NULL",
                          (paper_id,))
            cur = _conn.execute(
                "INSERT INTO reading_cards(paper_id, domain_id, template_id, content_json,"
                " created_at, updated_at) VALUES (?,NULL,?,?,?,?)",
                (paper_id, template_id, payload, _now(), _now()))
        else:
            cur = _conn.execute(
                "INSERT INTO reading_cards(paper_id, domain_id, template_id, content_json,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(paper_id, domain_id) DO UPDATE SET"
                " content_json=excluded.content_json,"
                " template_id=COALESCE(excluded.template_id,template_id),"
                " updated_at=excluded.updated_at",
                (paper_id, domain_id, template_id, payload, _now(), _now()))
        _conn.commit()
    return cur.lastrowid


def delete_reading_card(paper_id: int, domain_id: int | None = None) -> bool:
    """Drop the saved card so the next read falls back to the generated one."""
    with _lock:
        if domain_id is None:
            cur = _conn.execute(
                "DELETE FROM reading_cards WHERE paper_id=? AND domain_id IS NULL", (paper_id,))
        else:
            cur = _conn.execute(
                "DELETE FROM reading_cards WHERE paper_id=? AND domain_id=?",
                (paper_id, domain_id))
        _conn.commit()
    return (cur.rowcount or 0) > 0


def list_subtopics(domain_id: int) -> list:
    return [dict(r) for r in query(
        "SELECT * FROM subtopics WHERE domain_id=? ORDER BY parent_id, name", (domain_id,))]


def create_subtopic(domain_id: int, name: str, parent_id: int | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("subtopic name is empty")
    cur = execute("INSERT INTO subtopics(domain_id, parent_id, name, created_at)"
                  " VALUES (?,?,?,?)", (domain_id, parent_id, name, _now()))
    return cur.lastrowid


def delete_subtopic(subtopic_id: int) -> None:
    execute("DELETE FROM subtopics WHERE subtopic_id=?", (subtopic_id,))


def assign_paper_subtopics(paper_id: int, subtopic_ids: list[int]) -> None:
    with _lock:
        _conn.execute("DELETE FROM paper_subtopics WHERE paper_id=?", (paper_id,))
        for sid in set(subtopic_ids):
            _conn.execute("INSERT OR IGNORE INTO paper_subtopics(paper_id, subtopic_id)"
                          " VALUES (?,?)", (paper_id, sid))
        _conn.commit()


def list_paper_subtopics(paper_id: int) -> list:
    return [dict(r) for r in query(
        "SELECT s.* FROM subtopics s JOIN paper_subtopics ps ON ps.subtopic_id=s.subtopic_id"
        " WHERE ps.paper_id=? ORDER BY s.name", (paper_id,))]


def restore_snapshot(snapshot_id: int, include_notes: bool = False,
                     include_qa: bool = False,
                     expect_domain_id: int | None = None) -> dict:
    """把领域恢复到归档时的状态，只增不删。

归属、阅读状态与位置、未决问题、待复核引用、领域配置、最后打开的论文一并写回；
已存在的行就地更新或保持不变，不删除任何数据。
笔记与问答副本仅在调用方要求时写回。
    """
    rows = query("SELECT * FROM domain_snapshots WHERE snapshot_id=?", (snapshot_id,))
    if not rows:
        raise ValueError("snapshot not found")
    snap = rows[0]
    dom = snap["domain_id"]
    if expect_domain_id is not None and int(expect_domain_id) != int(dom):
        raise ValueError("snapshot does not belong to this domain")
    if not get_domain(dom):
        raise ValueError("the domain of this snapshot no longer exists")
    data = json.loads(snap["snapshot_json"] or "{}")
    counts = {"links_added": 0, "links_updated": 0, "papers_skipped": 0,
              "questions": 0, "questions_reopened": 0, "reviews": 0,
              "reviews_reopened": 0, "notes": 0, "qa_turns": 0,
              "config": bool(data.get("config")), "last_paper_id": None}

    def alive(pid) -> bool:
        return bool(pid) and bool(_conn.execute(
            "SELECT 1 FROM papers WHERE paper_id=? AND deleted_at IS NULL", (pid,)).fetchone())

    with _lock:
        for item in data.get("papers", []):
            pid = item.get("paper_id")
            if not alive(pid):
                counts["papers_skipped"] += 1
                continue
            want_primary = 1 if item.get("is_primary") else 0
            if want_primary and _conn.execute(
                    "SELECT 1 FROM paper_domain_links WHERE paper_id=? AND is_primary=1"
                    " AND deleted_at IS NULL AND domain_id<>?", (pid, dom)).fetchone():
                # at most one primary link per paper
                want_primary = 0
            row = _conn.execute(
                "SELECT 1 FROM paper_domain_links WHERE paper_id=? AND domain_id=?"
                " AND deleted_at IS NULL", (pid, dom)).fetchone()
            if row:
                _conn.execute(
                    "UPDATE paper_domain_links SET reading_status=COALESCE(?,reading_status),"
                    " progress=COALESCE(?,progress), last_page=COALESCE(?,last_page),"
                    " scroll_ratio=COALESCE(?,scroll_ratio), updated_at=?"
                    " WHERE paper_id=? AND domain_id=?",
                    (item.get("reading_status"), item.get("progress"),
                     item.get("last_page"), item.get("scroll_ratio"), _now(), pid, dom))
                counts["links_updated"] += 1
            else:
                _conn.execute(
                    "INSERT INTO paper_domain_links(paper_id, domain_id, score, source,"
                    " is_primary, confidence, status, reading_status, progress,"
                    " last_page, scroll_ratio, added_at, updated_at)"
                    " VALUES (?,?,0,'snapshot',?,?,?,?,?,?,?,?,?)",
                    (pid, dom, want_primary, item.get("confidence") or 0,
                     item.get("status") or "confirmed", item.get("reading_status"),
                     item.get("progress"), item.get("last_page"),
                     item.get("scroll_ratio"), _now(), _now()))
                counts["links_added"] += 1
            if want_primary:
                _conn.execute("UPDATE paper_domain_links SET is_primary=1, updated_at=?"
                              " WHERE paper_id=? AND domain_id=?", (_now(), pid, dom))
                _conn.execute("UPDATE papers SET domain_id=?, updated_at=? WHERE paper_id=?",
                              (dom, _now(), pid))

        for q in data.get("open_questions", []):
            text = (q.get("text") or "").strip()
            if not text:
                continue
            pid = q.get("paper_id")
            if pid and not alive(pid):
                continue
            exists = _conn.execute(
                "SELECT question_id, status FROM questions WHERE domain_id=? AND text=?"
                " AND (paper_id IS ?)", (dom, text, pid)).fetchone()
            if exists:
                # a question the snapshot recorded as open is re-opened
                if exists["status"] != "open":
                    _conn.execute("UPDATE questions SET status='open', resolved_at=NULL"
                                  " WHERE question_id=?", (exists["question_id"],))
                    counts["questions_reopened"] += 1
                continue
            _conn.execute(
                "INSERT INTO questions(paper_id, domain_id, text, status, source_type,"
                " source_id, created_at) VALUES (?,?,?,'open',?,?,?)",
                (pid, dom, text, q.get("source_type") or "", q.get("source_id"), _now()))
            counts["questions"] += 1

        for r in data.get("pending_reviews", []):
            cid = r.get("citation_id")
            if not cid or not _conn.execute(
                    "SELECT 1 FROM citations WHERE citation_id=?", (cid,)).fetchone():
                continue
            exists = _conn.execute(
                "SELECT item_id, status FROM review_items WHERE citation_id=? AND domain_id=?",
                (cid, dom)).fetchone()
            if exists:
                if exists["status"] != "pending":
                    _conn.execute("UPDATE review_items SET status='pending', resolved_at=NULL"
                                  " WHERE item_id=?", (exists["item_id"],))
                    counts["reviews_reopened"] += 1
                continue
            _conn.execute(
                "INSERT INTO review_items(citation_id, paper_id, domain_id, reason, status,"
                " created_at) VALUES (?,?,?,?,'pending',?)",
                (cid, r.get("paper_id"), dom, r.get("reason") or "", _now()))
            counts["reviews"] += 1

        if include_notes:
            for n in data.get("notes", []):
                pid = n.get("paper_id")
                text = (n.get("content") or "").strip()
                if not alive(pid) or not text:
                    continue
                exists = _conn.execute(
                    "SELECT 1 FROM interactions WHERE paper_id=? AND type='note'"
                    " AND content=? AND deleted_at IS NULL", (pid, n.get("content"))).fetchone()
                if exists:
                    continue
                _conn.execute(
                    "INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content,"
                    " tags_json, created_at, updated_at) VALUES (?,?,'note',?,?,?,?,?)",
                    (dom, pid, n.get("anchor_json") or "{}", n.get("content"),
                     n.get("tags_json") or "[]", n.get("created_at") or _now(), _now()))
                counts["notes"] += 1

        if include_qa:
            for t in data.get("qa_turns", []):
                pid = t.get("paper_id")
                content = t.get("content") or ""
                if not alive(pid) or not content:
                    continue
                exists = _conn.execute(
                    "SELECT 1 FROM interactions WHERE paper_id=? AND type='qa'"
                    " AND content=? AND deleted_at IS NULL", (pid, content)).fetchone()
                if exists:
                    continue
                _conn.execute(
                    "INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content,"
                    " importance, created_at, updated_at) VALUES (?,?,'qa',?,?,?,?,?)",
                    (dom, pid, t.get("anchor_json") or "{}", content,
                     int(t.get("importance") or 0), t.get("created_at") or _now(), _now()))
                counts["qa_turns"] += 1

        last_pid = data.get("last_paper_id")
        if alive(last_pid):
            _conn.execute("UPDATE domains SET last_paper_id=? WHERE domain_id=?",
                          (last_pid, dom))
            counts["last_paper_id"] = last_pid
        _conn.commit()

    cfg = data.get("config") or {}
    if cfg:
        set_domain_config(dom, {k: v for k, v in cfg.items() if k in DOMAIN_CONFIG_DEFAULTS})
    log_operation("domain.snapshot.restore", "domain", dom, dom,
                  {"snapshot_id": snapshot_id, **counts})
    return {"snapshot_id": snapshot_id, "domain_id": dom, **counts}


def save_survey_draft(domain_id: int | None, domain_ids: list[int], paper_ids: list[int],
                      group_by: str, title: str, markdown: str,
                      stats: dict | None = None, survey_id: int | None = None) -> int:
    """保存综述草稿，草稿可继续编辑。

传 `survey_id` 覆盖该草稿，否则新建。`domain_ids` 保存跨领域选文，
文献库变化后可按同一选择重新生成。
    """
    payload = (json.dumps(list(domain_ids or []), ensure_ascii=False),
               json.dumps(list(paper_ids or []), ensure_ascii=False),
               group_by or "topic", title or "", markdown or "",
               json.dumps(stats or {}, ensure_ascii=False))
    if survey_id:
        cur = execute(
            "UPDATE survey_drafts SET domain_id=?, domain_ids_json=?, paper_ids_json=?,"
            " group_by=?, title=?, markdown=?, stats_json=?, updated_at=?"
            " WHERE survey_id=?", (*payload, _now(), survey_id))
        if not cur.rowcount:
            raise ValueError("survey draft not found")
        log_operation("survey.update", "survey", survey_id, domain_id, {"group_by": group_by})
        return survey_id
    cur = execute(
        "INSERT INTO survey_drafts(domain_id, domain_ids_json, paper_ids_json, group_by,"
        " title, markdown, stats_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (domain_id, *payload, _now(), _now()))
    log_operation("survey.create", "survey", cur.lastrowid, domain_id,
                  {"group_by": group_by, "papers": len(paper_ids or [])})
    return cur.lastrowid


def get_survey_draft(survey_id: int) -> dict | None:
    rows = query("SELECT * FROM survey_drafts WHERE survey_id=?", (survey_id,))
    if not rows:
        return None
    out = dict(rows[0])
    for key, target in (("domain_ids_json", "domain_ids"), ("paper_ids_json", "paper_ids"),
                        ("stats_json", "stats")):
        try:
            out[target] = json.loads(out.get(key) or "[]")
        except (TypeError, ValueError):
            out[target] = [] if target != "stats" else {}
    return out


def list_survey_drafts(domain_id: int | None = None, limit: int = 30) -> list:
    if domain_id is None:
        rows = query("SELECT survey_id, domain_id, group_by, title, created_at, updated_at,"
                     " stats_json FROM survey_drafts ORDER BY survey_id DESC LIMIT ?", (limit,))
    else:
        rows = query("SELECT survey_id, domain_id, group_by, title, created_at, updated_at,"
                     " stats_json FROM survey_drafts WHERE domain_id=?"
                     " ORDER BY survey_id DESC LIMIT ?", (domain_id, limit))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["stats"] = json.loads(d.pop("stats_json") or "{}")
        except (TypeError, ValueError):
            d["stats"] = {}
        out.append(d)
    return out


def update_survey_draft(survey_id: int, title: str | None = None,
                        markdown: str | None = None) -> dict:
    sets, params = [], []
    if title is not None:
        sets.append("title=?")
        params.append(title)
    if markdown is not None:
        sets.append("markdown=?")
        params.append(markdown)
    if not sets:
        raise ValueError("nothing to update")
    sets.append("updated_at=?")
    params.append(_now())
    params.append(survey_id)
    cur = execute(f"UPDATE survey_drafts SET {', '.join(sets)} WHERE survey_id=?", tuple(params))
    if not cur.rowcount:
        raise ValueError("survey draft not found")
    log_operation("survey.edit", "survey", survey_id, None, {})
    return get_survey_draft(survey_id) or {}


def delete_survey_draft(survey_id: int) -> None:
    execute("DELETE FROM survey_drafts WHERE survey_id=?", (survey_id,))
    log_operation("survey.delete", "survey", survey_id, None, {})


def export_domain_index(domain_id: int, index_dir: str) -> str:
    """Write a physical, portable index snapshot for one domain (JSONL of chunks
 with vectors). Retrieval still reads SQLite; the file is the migration
 artifact/backup for `index_mode=physical`."""
    import base64
    import os
    os.makedirs(index_dir, exist_ok=True)
    path = os.path.join(index_dir, f"domain-{domain_id}.jsonl")
    rows = query(
        "SELECT c.chunk_id, c.paper_id, c.page, c.section, c.text, c.vec"
        " FROM chunks c JOIN paper_domain_links l ON l.paper_id=c.paper_id"
        " WHERE l.domain_id=? AND l.deleted_at IS NULL ORDER BY c.paper_id, c.chunk_id",
        (domain_id,))
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "chunk_id": r["chunk_id"], "paper_id": r["paper_id"],
                "page": r["page"], "section": r["section"], "text": r["text"],
                "vec": base64.b64encode(r["vec"]).decode() if r["vec"] else "",
            }, ensure_ascii=False) + "\n")
    set_domain_config(domain_id, {"index_mode": "physical"})
    log_operation("domain.index.physical", "domain", domain_id, domain_id,
                  {"file": path, "chunks": len(rows)})
    return path


def set_domain_parent(domain_id: int, parent_id: int | None) -> None:
    """Attach a subtopic/child domain; rejects cycles and self-parenting."""
    if not query("SELECT 1 FROM domains WHERE domain_id=?", (domain_id,)):
        raise ValueError("domain not found")
    if parent_id is not None and not query("SELECT 1 FROM domains WHERE domain_id=?",
                                           (parent_id,)):
        raise ValueError("parent domain not found")
    if parent_id == domain_id:
        raise ValueError("a domain cannot be its own parent")
    cur = parent_id
    seen = set()
    while cur:
        if cur == domain_id:
            raise ValueError("cycle: parent is a descendant of this domain")
        if cur in seen:
            raise ValueError("cycle in domain hierarchy")
        seen.add(cur)
        row = query("SELECT parent_domain_id FROM domains WHERE domain_id=?", (cur,))
        cur = row[0]["parent_domain_id"] if row else None
    execute("UPDATE domains SET parent_domain_id=? WHERE domain_id=?", (parent_id, domain_id))
    log_operation("domain.parent", "domain", domain_id, domain_id, {"parent": parent_id})



def integrity_report() -> dict:
    """Cheap invariants used by /health/integrity and the audit script. A non-empty
 `problems` list means the UI may be showing inconsistent data.

the report now also covers the legacy tables the explicit
 cascades touch — sessions of deleted papers, domain-less cards, subtopic
 assignments, dangling anchor references and domain pointers left behind by
 an interrupted merge.
    """
    def one(sql, params=()):
        return query(sql, params)[0][0]

    problems = []
    checks = {
        "papers_without_primary_link": one(
            "SELECT COUNT(*) FROM papers p WHERE p.deleted_at IS NULL AND ("
            " (p.domain_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM paper_domain_links l"
            "   WHERE l.paper_id=p.paper_id AND l.deleted_at IS NULL))"
            " OR (EXISTS(SELECT 1 FROM paper_domain_links l WHERE l.paper_id=p.paper_id"
            "   AND l.deleted_at IS NULL) AND NOT EXISTS(SELECT 1 FROM paper_domain_links l"
            "   WHERE l.paper_id=p.paper_id AND l.is_primary=1 AND l.deleted_at IS NULL)))"),
        "papers_with_multiple_primary_links": one(
            "SELECT COUNT(*) FROM (SELECT paper_id FROM paper_domain_links"
            " WHERE is_primary=1 AND deleted_at IS NULL GROUP BY paper_id"
            " HAVING COUNT(*)>1)"),
        "links_to_missing_papers": one(
            "SELECT COUNT(*) FROM paper_domain_links l WHERE NOT EXISTS"
            "(SELECT 1 FROM papers p WHERE p.paper_id=l.paper_id)"),
        "links_to_missing_domains": one(
            "SELECT COUNT(*) FROM paper_domain_links l WHERE NOT EXISTS"
            "(SELECT 1 FROM domains d WHERE d.domain_id=l.domain_id)"),
        "orphan_interactions": one(
            "SELECT COUNT(*) FROM interactions i WHERE i.deleted_at IS NULL AND NOT EXISTS"
            "(SELECT 1 FROM papers p WHERE p.paper_id=i.paper_id)"),
        "orphan_chunks": one(
            "SELECT COUNT(*) FROM chunks c WHERE NOT EXISTS"
            "(SELECT 1 FROM papers p WHERE p.paper_id=c.paper_id)"),
        "citations_to_missing_source": one(
            "SELECT COUNT(*) FROM citations c WHERE NOT EXISTS"
            "(SELECT 1 FROM papers p WHERE p.paper_id=c.source_paper_id)"),
        "duplicate_doi": one(
            "SELECT COUNT(*) FROM (SELECT doi_key FROM papers WHERE doi_key<>''"
            " AND deleted_at IS NULL GROUP BY doi_key HAVING COUNT(*)>1)"),
        "duplicate_sha1": one(
            "SELECT COUNT(*) FROM (SELECT sha1 FROM papers WHERE sha1 IS NOT NULL AND sha1<>''"
            " AND deleted_at IS NULL GROUP BY sha1 HAVING COUNT(*)>1)"),
        "orphan_qa_sessions": one(
            "SELECT COUNT(*) FROM qa_sessions s WHERE s.paper_id IS NOT NULL AND NOT EXISTS"
            "(SELECT 1 FROM papers p WHERE p.paper_id=s.paper_id)"),
        "orphan_reading_cards": one(
            "SELECT COUNT(*) FROM reading_cards c WHERE NOT EXISTS"
            "(SELECT 1 FROM papers p WHERE p.paper_id=c.paper_id) OR (c.domain_id IS NOT NULL"
            " AND NOT EXISTS(SELECT 1 FROM domains d WHERE d.domain_id=c.domain_id))"),
        "orphan_paper_subtopics": one(
            "SELECT COUNT(*) FROM paper_subtopics ps WHERE NOT EXISTS"
            "(SELECT 1 FROM papers p WHERE p.paper_id=ps.paper_id) OR NOT EXISTS"
            "(SELECT 1 FROM subtopics s WHERE s.subtopic_id=ps.subtopic_id)"),
        "orphan_subtopics": one(
            "SELECT COUNT(*) FROM subtopics s WHERE NOT EXISTS"
            "(SELECT 1 FROM domains d WHERE d.domain_id=s.domain_id)"),
        "orphan_anchors": one(
            "SELECT COUNT(*) FROM anchors a WHERE NOT EXISTS"
            "(SELECT 1 FROM papers p WHERE p.paper_id=a.paper_id)"),
        "dangling_anchor_refs": one(
            "SELECT COUNT(*) FROM interactions i WHERE i.anchor_id IS NOT NULL AND NOT EXISTS"
            "(SELECT 1 FROM anchors a WHERE a.anchor_id=i.anchor_id)"),
        "interactions_to_missing_domains": one(
            "SELECT COUNT(*) FROM interactions i WHERE i.domain_id IS NOT NULL AND NOT EXISTS"
            "(SELECT 1 FROM domains d WHERE d.domain_id=i.domain_id)"),
    }
    for name, n in checks.items():
        if n:
            problems.append({"check": name, "count": n})
    stats = {
        "papers": one("SELECT COUNT(*) FROM papers WHERE deleted_at IS NULL"),
        "domains": one("SELECT COUNT(*) FROM domains WHERE deleted_at IS NULL"),
        "links": one("SELECT COUNT(*) FROM paper_domain_links WHERE deleted_at IS NULL"),
        "citations": one("SELECT COUNT(*) FROM citations"),
        "unresolved_citations": one("SELECT COUNT(*) FROM citations WHERE status='unresolved'"),
        "anchors": one("SELECT COUNT(*) FROM anchors"),
        "concepts": one("SELECT COUNT(*) FROM concepts"),
        "open_questions": one("SELECT COUNT(*) FROM questions WHERE status='open'"),
        "pending_reviews": one("SELECT COUNT(*) FROM review_items WHERE status='pending'"),
        "summaries": one("SELECT COUNT(*) FROM summaries"),
    }
    return {"ok": not problems, "problems": problems, "stats": stats}
