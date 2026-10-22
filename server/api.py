"""ScholarLens backend API. : + SSE observer (/events) for frontend sync."""
import asyncio
import hashlib
import json
import math
import os
import queue
import re
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import arxiv
from . import assign
from . import highlight
from . import llm
from . import pdfio
from . import retrieval
from . import store
from . import summarize
from . import survey
from . import translate

APP_VERSION = "1.1.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SCHOLARLENS_DATA_DIR") or os.path.join(ROOT, "data")
PAPERS_DIR = os.path.join(DATA_DIR, "papers")

_subscribers: set = set()
_main_loop: asyncio.AbstractEventLoop | None = None


def _startup_init() -> None:
    """initialize derived state that older runs may have left inconsistent.

 Everything here is idempotent and non-destructive: numeric config sanity,
 the FTS index (chunks written before the triggers existed would otherwise be
 invisible to search/retrieval forever) and missing centroids. Runs in a
 daemon thread so a large library never delays the first paint.
    """
    for key in ("assign_threshold", "link_threshold", "domain_dispersion_weight"):
        try:
            value = float(store.get_config(key))
            if not 0.0 <= value <= 1.0:
                raise ValueError
        except (TypeError, ValueError):
            store.set_config(key, store.CONFIG_DEFAULTS[key])
            print(f"[init] {key} reset to default")
    try:
        n_chunks = store.query("SELECT COUNT(*) c FROM chunks")[0]["c"]
        if n_chunks:
            store.query("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            print(f"[init] FTS index rebuilt ({n_chunks} chunks)")
    except Exception as e:
        print(f"[init] FTS rebuild skipped: {e}")
    try:
        rows = store.query("SELECT domain_id FROM domains")
    except Exception:
        rows = []
    for r in rows:
        try:
            assign.rebuild_centroid(r["domain_id"])
        except Exception as e:
            print(f"[init] centroid rebuild failed for domain {r['domain_id']}: {e}")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    _resume_n = _resume_interrupted()
    if _resume_n:
        print(f"[resume] re-enqueued {_resume_n} interrupted papers")
    _audit_r, _audit_l = _audit_files()
    if _audit_r or _audit_l:
        print(f"[audit] re-downloading {_audit_r} missing arXiv PDFs, {_audit_l} local files lost")
    threading.Thread(target=_startup_init, daemon=True).start()
    yield


app = FastAPI(title="ScholarLens", version=APP_VERSION, lifespan=_lifespan)


@app.middleware("http")
async def _same_origin_only(request: Request, call_next):
    """Only the app's own origin may call the API.

 allowed every `http://localhost:<any-port>` origin via
 CORS, so any local web server could read `/llm/profiles` (which carried the
 provider key in clear text). Requests without an Origin header (the Electron
 main process, curl, same-origin GETs) pass; a browser request from another
 port is rejected before it can read anything.
    """
    origin = request.headers.get("origin")
    if origin:
        port = request.url.port or request.headers.get("host", "").rpartition(":")[2]
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if origin not in allowed:
            return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
    return await call_next(request)

store.connect(os.path.join(DATA_DIR, "scholarlens.db"))

from fastapi.staticfiles import StaticFiles
app.mount("/ui", StaticFiles(html=True, directory=os.path.join(ROOT, "app", "renderer")), name="ui")


_subscribers: set = set()


def _notify(event: str) -> None:
    """推送给所有 SSE 订阅者；无订阅者时不做任何事。

参数可以是已序列化的 JSON，也可以是裸关键字（如 "domains_dirty"）；
裸关键字在这里包成 JSON，渲染端按 JSON 解析每一帧。
    """
    if not event.lstrip().startswith("{"):
        event = json.dumps({event: True})
    if _main_loop is None or not _subscribers:
        return
    for q in list(_subscribers):
        try:
            _main_loop.call_soon_threadsafe(q.put_nowait, event)
        except RuntimeError:
            _subscribers.discard(q)


def _llm_guard() -> None:
    """未配置模型 API Key 时直接返回 428，不发空 Bearer 请求。"""
    if not llm.configured():
        raise HTTPException(428, "未配置模型 API Key：请在「设置 → API」填写后重试")


@app.get("/events")
async def events():
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.add(q)

    async def gen():
        try:
            yield f"data: {json.dumps({'version': APP_VERSION})}\n\n"
            while True:
                ev = await q.get()
                yield f"data: {ev}\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



_task_q: queue.Queue = queue.Queue()
_task_state = {"current": None, "done": 0, "failed": 0, "queued": 0, "last_error": "",
               "batch_total": 0, "batch_done": 0}
_cancel_requested: set[int] = set()


def _queue_state() -> dict:
    """/health 与 /overview 用的队列快照。"""
    state = dict(_task_state)
    state["queued"] = max(0, _task_q.qsize())
    state["idle"] = state["queued"] == 0 and not state["current"]
    return state


def _check_cancel(paper_id: int) -> None:
    """解析阶段间的协作取消点 —— 请求取消则抛错，由 worker 标记失败落库。"""
    if paper_id in _cancel_requested:
        _cancel_requested.discard(paper_id)
        raise RuntimeError("用户取消")


def _worker():
    while True:
        job = _task_q.get()
        try:
            if job.get("paper_id") in _cancel_requested:
                _cancel_requested.discard(job["paper_id"])
                try:
                    store.set_parse_status(job["paper_id"], "failed", "用户取消")
                except Exception as db_err:
                    print(f"[worker] cancel status write failed: {db_err}")
                _task_q.task_done()
                if job.get("paper_id") is not None:
                    _task_state["batch_done"] += 1
                _notify("domains_dirty")
                continue
            _task_state["current"] = job["label"]
            try:
                job["fn"]()
                _task_state["done"] += 1
            except Exception as e:
                _task_state["failed"] += 1
                _task_state["last_error"] = str(e)
                if "paper_id" in job:
                    try:
                        store.set_parse_status(job["paper_id"], "failed", str(e))
                    except Exception as db_err:
                        print(f"[worker] failed-status write failed: {db_err}")
            _task_state["current"] = None
            _task_q.task_done()
            if job.get("paper_id") is not None:
                _task_state["batch_done"] += 1
            _notify(json.dumps({"queue_idle": _task_q.qsize() == 0,
                                "batch_total": _task_state["batch_total"],
                                "batch_done": _task_state["batch_done"],
                                "domains_dirty": True}, ensure_ascii=False))
        except BaseException as e:
            print(f"[worker] unexpected error: {e}")
            _task_state["current"] = None
            try:
                _task_q.task_done()
            except ValueError:
                pass


threading.Thread(target=_worker, daemon=True).start()


def _enqueue(label: str, fn, paper_id: int | None = None) -> None:
    if paper_id is not None:
        _task_state["batch_total"] += 1
    job = {"label": label, "fn": fn}
    if paper_id is not None:
        job["paper_id"] = paper_id
    _task_q.put(job)


def _safe_summary(paper_id: int, research_goal: str | None = None) -> None:
    """Summary failure must not fail the import batch (mark and move on)."""
    try:
        summarize.generate(paper_id, "five", research_goal=research_goal)
    except Exception as e:
        print(f"[summary] paper {paper_id} failed: {e}")
        try:
            store.merge_paper_meta(paper_id, {"summary_error": str(e)[:500]})
        except Exception:
            pass


def _resolve_view_domain(paper: dict, domain_id: int | None) -> int | None:
    """The domain the reader is currently in.

 An explicit id is honoured only when the paper is linked to it; otherwise
 the primary domain is used so a stale UI keeps working.
    """
    primary = paper.get("domain_id")
    if domain_id is None:
        return primary
    linked = store.query(
        "SELECT 1 FROM paper_domain_links WHERE paper_id=? AND domain_id=?"
        " AND deleted_at IS NULL", (paper["paper_id"], domain_id))
    return domain_id if linked else primary


def _domain_goal(domain_id: int | None) -> str:
    if not domain_id:
        return ""
    d = store.get_domain(domain_id)
    return ((d or {}).get("research_goal") or "").strip()


def _safe_glossary(domain_id: int) -> None:
    """Glossary generation; incremental merge after the first build."""
    try:
        translate.generate_glossary(domain_id, update=True)
    except Exception as e:
        print(f"[glossary] domain {domain_id} failed: {e}")


# One glossary run per domain.
_glossary_lock = threading.Lock()
_glossary_queued: set[int] = set()


def _glossary_claim(domain_id: int) -> bool:
    with _glossary_lock:
        if domain_id in _glossary_queued:
            return False
        _glossary_queued.add(domain_id)
        return True


def _glossary_release(domain_id: int) -> None:
    with _glossary_lock:
        _glossary_queued.discard(domain_id)


def _enqueue_glossary(domain_id: int) -> bool:
    """Queue at most one glossary job per domain; returns True when queued."""
    if not _glossary_claim(domain_id):
        return False

    def run(d: int = domain_id) -> None:
        try:
            _safe_glossary(d)
        finally:
            _glossary_release(d)

    try:
        _enqueue(f"glossary:{domain_id}", run)
    except Exception:
        _glossary_release(domain_id)
        raise
    return True


def _safe_highlight(paper_id: int) -> None:
    try:
        r = highlight.auto_highlight(paper_id)
        if r.get("added"):
            _notify("domains_dirty")
    except Exception as e:
        print(f"[auto-hl] paper {paper_id} failed: {e}")


def _resume_interrupted() -> int:
    """启动时把停在 queued/parsing 的行重新入队；下载是单次写入，解析可重复执行。"""
    rows = store.query("SELECT paper_id FROM papers WHERE parse_status IN ('queued','parsing')")
    for r in rows:
        store.set_parse_status(r["paper_id"], "queued")
        store.set_progress(r["paper_id"], 0)
        pid = r["paper_id"]
        _enqueue(f"resume:{pid}", lambda p=pid: _process_paper(p), paper_id=pid)
    return len(rows)


def _audit_files() -> tuple[int, int]:
    """Startup self-check: every 'done' paper must have its stored PDF on disk.
 Missing arXiv papers are re-downloaded; missing local files are marked failed."""
    requeued = lost = 0
    for p in store.list_papers():
        if p["parse_status"] != "done":
            continue
        full = store.get_paper(p["paper_id"])
        if full["file_path"] and os.path.isfile(full["file_path"]):
            continue
        if full["arxiv_id"]:
            store.execute("UPDATE papers SET file_path=NULL, parse_status='queued', progress=0 WHERE paper_id=?",
                          (p["paper_id"],))
            pid = p["paper_id"]
            _enqueue(f"redownload:{full['arxiv_id']}", lambda x=pid: _process_paper(x), paper_id=pid)
            requeued += 1
        else:
            store.set_parse_status(p["paper_id"], "failed", "PDF 文件丢失（本地源文件已不存在，请重新导入）")
            lost += 1
    return requeued, lost




def _finalize_pdf_name(paper_id: int) -> None:
    """Rename stored PDF to the standardized readable name after metadata is known."""
    p = store.get_paper(paper_id)
    if not p or not p["file_path"] or not p["sha1"]:
        return
    final = os.path.join(PAPERS_DIR, store.paper_filename(p["title"], p["arxiv_id"], p["sha1"]))
    if os.path.abspath(p["file_path"]) != os.path.abspath(final):
        if os.path.exists(final):
            os.remove(p["file_path"])
        else:
            os.rename(p["file_path"], final)
        store.execute("UPDATE papers SET file_path=? WHERE paper_id=?", (final, paper_id))


def _process_paper(paper_id: int):
    paper = store.get_paper(paper_id)
    if not paper:
        return
    store.set_parse_status(paper_id, "parsing")

    last_pct = [0]

    def report(pct: float):
        store.set_progress(paper_id, pct)
        if int(pct // 10) > last_pct[0]:
            last_pct[0] = int(pct // 10)
            _notify(json.dumps({"progress": {"paper_id": paper_id, "pct": pct,
                                             "title": paper["title"]}}))

    _check_cancel(paper_id)
    if paper["arxiv_id"] and not paper["file_path"]:
        path, sha1 = arxiv.download_pdf(paper["arxiv_id"], PAPERS_DIR,
                                        progress_cb=lambda p: report(p * 0.5))
        _check_cancel(paper_id)
        dup = store.query("SELECT paper_id FROM papers WHERE sha1=? AND paper_id!=?", (sha1, paper_id))
        if dup:
            store.delete_paper(paper_id)
            os.remove(path)
            return
        store.execute("UPDATE papers SET file_path=?, sha1=? WHERE paper_id=?", (path, sha1, paper_id))
        paper = store.get_paper(paper_id)
    else:
        report(50)

    if not paper["abstract"] or not paper["title"] or paper["year"] is None:
        try:
            gm = pdfio.guess_metadata(paper["file_path"])
            store.execute(
                "UPDATE papers SET title=?, authors=?, abstract=?, year=? WHERE paper_id=?",
                (paper["title"] or gm["title"], paper["authors"] or gm["authors"],
                 paper["abstract"] or gm["abstract"], paper["year"] if paper["year"] is not None else gm["year"],
                 paper_id),
            )
            paper = store.get_paper(paper_id)
        except Exception:
            pass
    report(60)
    _check_cancel(paper_id)
    result = pdfio.extract(paper["file_path"])
    ocr_pages = result.get("ocr_pages") or []
    try:
        # ocr_status: done / failed / none
        if ocr_pages:
            store.set_ocr_status(paper_id, "done")
        elif result.get("ocr_errors"):
            store.set_ocr_status(paper_id, "failed")
        else:
            store.set_ocr_status(paper_id, "none")
    except Exception as e:
        print(f"[ocr] status write failed: {e}")
    report(80)
    _check_cancel(paper_id)
    meta = json.loads(paper["meta_json"] or "{}")
    import_domain = meta.get("_import_domain")
    store.merge_paper_meta(paper_id, {
        "sections": result["sections"],
        "links": result["links"],
        "n_pages": result["n_pages"],
        "n_chars": len(result["full_text"]),
        "ocr_pages": ocr_pages,
        # figure rectangles and the figure/table/algorithm index
        "figures": result.get("figures") or [],
        "floats": result.get("floats") or [],
    })
    store.replace_chunks(paper_id, paper["domain_id"], result["chunks"], mark_done=False)
    report(90)
    try:
        assign.embed_chunks(paper_id)
    except Exception as emb_err:
        print(f"[parse] chunk embedding skipped for paper {paper_id}: {emb_err}")
    store.save_references(paper_id, _match_references(result["full_text"], paper_id))
    if import_domain is not None:
        store.set_paper_domain(paper_id, import_domain, source="import")
        store.merge_paper_meta(paper_id, {"lock_domain": True}, drop=("_import_domain",))
        try:
            assign.embed_only(paper_id)
        except Exception as vec_err:
            print(f"[assign] vector for paper {paper_id} failed: {vec_err}")
        assign.rebuild_centroid(import_domain)
    else:
        assign.assign_paper(paper_id)
    _finalize_pdf_name(paper_id)
    store.set_progress(paper_id, 100)
    store.refresh_review_items(paper_id)
    _enqueue(f"summary:{paper_id}", lambda p=paper_id: _safe_summary(p))
    cur = store.get_paper(paper_id) or {}
    domain_for_glossary = cur.get("domain_id")
    if domain_for_glossary:
        _enqueue_glossary(domain_for_glossary)
    else:
        print(f"[glossary] paper {paper_id} has no domain yet; glossary skipped")
    # parse_status "done" covers chunks, domain, review items and
    # the queued summary/glossary.
    store.set_parse_status(paper_id, "done")


_REF_LINE = None


def _match_references(full_text: str, source_paper_id: int | None = None) -> list[dict]:
    """Resolve the bibliography against library papers.

 PDF extraction often puts every token on its own line (``word\\n \\nword``),
 so splitting on numbered entries is unreliable. therefore scans the
 normalized references section for library titles and DOIs: the citation
 graph only needs edges to papers we actually have, and external entries are
 recorded by DOI without flooding 待复核.
    """
    import re
    idx = max(full_text.rfind("References"), full_text.rfind("REFERENCES"))
    if idx < 0:
        return []
    tail = re.sub(r"\s+", " ", full_text[idx: idx + 40000])
    papers = store.list_papers()
    by_doi = {store.doi_key(p.get("doi")): p["paper_id"] for p in papers if p.get("doi")}
    fkey = store.title_key(tail)
    refs: list[dict] = []
    seen: set[int] = set()
    for p in papers:
        pid = p["paper_id"]
        if pid == source_paper_id:
            continue
        tkey = store.title_key(p.get("title", ""))
        if len(tkey.split()) < 3 or tkey not in fkey:
            continue
        refs.append({"dst_paper_id": pid, "dst_title": p.get("title", ""),
                     "dst_doi": p.get("doi", ""), "confidence": 0.7,
                     "status": "resolved", "raw": p.get("title", "")})
        seen.add(pid)
    for doi in dict.fromkeys(re.findall(r"10\.\d{4,9}/[^\s;,)\]]+", tail)):
        target = by_doi.get(store.doi_key(doi))
        if target and target != source_paper_id and target not in seen:
            seen.add(target)
            refs.append({"dst_paper_id": target, "dst_title": "", "dst_doi": doi,
                         "confidence": 0.9, "status": "resolved", "raw": doi})
        elif not target:
            refs.append({"dst_paper_id": None, "dst_title": f"DOI: {doi}",
                         "dst_doi": doi, "confidence": 0.0,
                         "status": "external", "raw": doi})
    return refs[:200]



class DomainIn(BaseModel):
    name: str = ""


class ImportIn(BaseModel):
    arxiv_ids: list[str] = []
    local_paths: list[str] = []
    domain_id: int | None = None


class SearchIn(BaseModel):
    query: str
    max_results: int = 20


class AssignIn(BaseModel):
    domain_id: int | None = None



@app.get("/overview")
def overview():
    """工作台首页数据：统计 + 最近导入 + 未闭环事项（）。"""
    n_domains = store.query("SELECT COUNT(*) c FROM domains WHERE deleted_at IS NULL")[0]["c"]
    n_papers = store.query("SELECT COUNT(*) c FROM papers WHERE deleted_at IS NULL")[0]["c"]
    n_parsing = store.query("SELECT COUNT(*) c FROM papers WHERE parse_status IN ('queued','parsing')"
                            " AND deleted_at IS NULL")[0]["c"]
    n_highlights = store.query("SELECT COUNT(*) c FROM interactions WHERE type='highlight'"
                               " AND deleted_at IS NULL")[0]["c"]
    open_questions = store.query("SELECT COUNT(*) c FROM questions WHERE status='open'")[0]["c"]
    pending_reviews = store.query("SELECT COUNT(*) c FROM review_items WHERE status='pending'")[0]["c"]
    unresolved_citations = store.query("SELECT COUNT(*) c FROM citations WHERE status='unresolved'")[0]["c"]
    recent = store.query(
        """SELECT p.paper_id, p.title, p.year, p.reading_status, p.parse_status,
 d.name AS domain_name
 FROM papers p LEFT JOIN domains d ON d.domain_id = p.domain_id
 WHERE p.deleted_at IS NULL
 ORDER BY COALESCE(p.last_opened_at, p.created_at) DESC LIMIT 6""")
    qa_recent = store.query(
        """SELECT i.interaction_id, i.created_at, i.paper_id, p.title,
 CAST(json_extract(CASE WHEN json_valid(i.anchor_json)
 THEN i.anchor_json ELSE '{}' END,
 '$.session_id') AS INTEGER) AS sid,
 s.name AS session_name,
 json_extract(CASE WHEN json_valid(i.content)
 THEN i.content ELSE '{}' END, '$.question') AS question
 FROM interactions i
 JOIN papers p ON p.paper_id = i.paper_id
 LEFT JOIN qa_sessions s
 ON s.session_id = CAST(json_extract(CASE WHEN json_valid(i.anchor_json)
 THEN i.anchor_json ELSE '{}' END,
 '$.session_id') AS INTEGER)
 WHERE i.type='qa' ORDER BY i.interaction_id DESC LIMIT 20""")
    return {"n_domains": n_domains, "n_papers": n_papers, "n_parsing": n_parsing,
            "n_highlights": n_highlights, "recent": [dict(r) for r in recent],
            "qa_recent": [dict(r) for r in qa_recent],
            "open_questions": open_questions, "pending_reviews": pending_reviews,
            "unresolved_citations": unresolved_citations,
            "queue": _queue_state()}


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION, "queue": _queue_state()}


@app.get("/domains")
def get_domains():
    return store.list_domains()


@app.post("/domains")
def create_domain(body: DomainIn):
    name = body.name.strip() or f"Domain-{store.next_domain_slot()}"
    existing = [d for d in store.list_domains() if d["name"] == name]
    if existing:
        raise HTTPException(400, f"domain '{name}' already exists")
    domain_id = store.create_domain(name)
    _notify("domains_dirty")
    return {"domain_id": domain_id, "name": name}


@app.get("/domains/{domain_id}/papers")
def get_domain_papers(domain_id: int):
    return store.list_papers(domain_id, include_meta=False)



@app.get("/domains/discover")
def discover_domains(k: int | None = None):
    try:
        return assign.discover_domains(k)
    except ValueError as e:
        raise HTTPException(400, str(e))


class DiscoverCluster(BaseModel):
    name: str = ""
    paper_ids: list[int] = []
    signature: str = ""


class DiscoverAdjust(BaseModel):
    paper_id: int
    domain_id: int


class DiscoverApplyIn(BaseModel):
    clusters: list[DiscoverCluster] = []
    adjustments: list[DiscoverAdjust] = []


@app.post("/domains/discover/apply")
def apply_discovery(body: DiscoverApplyIn):
    """Apply a discovery preview.

 semantics: applying never overwrites an existing primary
 domain, never touches a user-locked paper, and reuses the domain a previous
 apply created for the same cluster instead of piling up duplicates.
    """
    if not body.clusters and not body.adjustments:
        raise HTTPException(400, "no clusters")
    created, reused, skipped = [], [], []
    linked = 0
    adjusted = []
    taken_names = {d["name"] for d in store.list_domains()}
    for i, c in enumerate(body.clusters):
        ids = [pid for pid in dict.fromkeys(c.paper_ids) if store.get_paper(pid)]
        if not ids:
            continue
        sig = c.signature or hashlib.sha1(
            ",".join(str(p) for p in sorted(ids)).encode()).hexdigest()[:16]
        existing = store.domain_by_sig(sig)
        if existing:
            did = existing["domain_id"]
            reused.append({"domain_id": did, "name": existing["name"], "papers": len(ids)})
        else:
            name = (c.name or f"Discovery-{i + 1}").strip()
            base, n = name, 2
            while name in taken_names:
                name = f"{base}-{n}"
                n += 1
            did = store.create_domain(name)
            store.set_domain_sig(did, sig)
            taken_names.add(name)
            created.append({"domain_id": did, "name": name, "papers": len(ids)})
        for pid in ids:
            meta = json.loads((store.get_paper(pid) or {}).get("meta_json") or "{}")
            if meta.get("lock_domain"):
                skipped.append(pid)
                continue
            if any(l["domain_id"] == did for l in store.list_paper_domains(pid)):
                continue
            store.add_paper_domain(pid, did, score=1.0, source="discovery")
            linked += 1

    for a in body.adjustments:
        p = store.get_paper(a.paper_id)
        if not p or not store.get_domain(a.domain_id):
            continue
        meta = json.loads(p.get("meta_json") or "{}")
        if meta.get("lock_domain"):
            skipped.append(a.paper_id)
            continue
        store.set_paper_domain(a.paper_id, a.domain_id, source="discovery")
        adjusted.append({"paper_id": a.paper_id, "domain_id": a.domain_id})

    if not (created or reused or adjusted):
        raise HTTPException(400, "no valid paper ids")
    _notify("domains_dirty")
    return {"created": created, "reused": reused, "linked": linked,
            "adjusted": adjusted, "skipped_locked": skipped}



@app.get("/domains/{domain_id}/overview")
def domain_overview(domain_id: int):
    d = store.get_domain(domain_id)
    if not d:
        raise HTTPException(404, "domain not found")
    papers = store.list_papers(domain_id)
    pids = {p["paper_id"] for p in papers}
    in_deg = {}
    if pids:
        marks = ",".join("?" for _ in pids)
        for r in store.query(
                f"SELECT target_paper_id tid, COUNT(*) c FROM citations"
                f" WHERE target_paper_id IN ({marks}) GROUP BY target_paper_id",
                tuple(pids)):
            in_deg[r["tid"]] = r["c"]
    meta = json.loads(d.get("meta_json") or "{}")
    core = [pid for pid in meta.get("core_papers", []) if pid in pids]
    qs = store.list_questions(domain_id=domain_id, status="open")
    rs = store.list_reviews(domain_id=domain_id, status="pending")
    try:
        concepts = store.list_concepts(domain_id)[:10]
    except Exception:
        concepts = []
    recent = sorted(papers, key=lambda p: p.get("last_opened_at") or "", reverse=True)[:5]
    nav = sorted(papers, key=lambda p: (-in_deg.get(p["paper_id"], 0), -(p.get("year") or 0)))[:5]
    subtopics = []
    for st in store.list_subtopics(domain_id):
        n = store.query("SELECT COUNT(*) c FROM paper_subtopics WHERE subtopic_id=?",
                        (st["subtopic_id"],))[0]["c"]
        subtopics.append({"subtopic_id": st["subtopic_id"], "name": st["name"],
                          "parent_id": st.get("parent_id"), "n_papers": n})
    subtopics.sort(key=lambda x: (-x["n_papers"], x["name"]))
    overlaps = store.concept_overlaps(domain_id, limit=5)
    return {
        "domain": {"domain_id": d["domain_id"], "name": d["name"],
                   "description": d.get("description", ""),
                   "research_goal": d.get("research_goal", ""),
                   "weight": d.get("weight", 1.0),
                   "parent_domain_id": d.get("parent_domain_id")},
        "n_papers": len(papers), "core_papers": core,
        "recent": [{"paper_id": p["paper_id"], "title": p["title"],
                    "reading_status": p.get("reading_status")} for p in recent],
        "open_questions": qs, "pending_reviews": rs,
        "subtopics": subtopics, "concepts": concepts,
        "concept_overlaps": overlaps,
        "last_view": store.domain_last_view(domain_id),
        "navigation": [{"paper_id": p["paper_id"], "title": p["title"],
                        "citations": in_deg.get(p["paper_id"], 0),
                        "reason": f"领域内被引 {in_deg.get(p['paper_id'], 0)} 次" if in_deg.get(p["paper_id"], 0) else "较新论文"} for p in nav],
    }


class GoalIn(BaseModel):
    research_goal: str = ""
    core_papers: list[int] | None = None


@app.put("/domains/{domain_id}/goal")
def set_domain_goal(domain_id: int, body: GoalIn):
    d = store.get_domain(domain_id)
    if not d:
        raise HTTPException(404, "domain not found")
    store.execute("UPDATE domains SET research_goal=? WHERE domain_id=?",
                  (body.research_goal.strip(), domain_id))
    if body.core_papers is not None:
        meta = json.loads(d.get("meta_json") or "{}")
        meta["core_papers"] = [int(x) for x in body.core_papers]
        store.set_domain_meta(domain_id, meta)
    store.log_operation("domain.goal", "domain", domain_id, domain_id,
                        {"goal": body.research_goal})
    return {"ok": True}



class WeightIn(BaseModel):
    weight: float | str | None = None
    level: str | None = None


@app.put("/domains/{domain_id}/weight")
def update_domain_weight(domain_id: int, body: WeightIn):
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    value = body.weight if body.weight is not None else body.level
    if value is None:
        raise HTTPException(400, "weight or level is required")
    try:
        weight = store.set_domain_weight(domain_id, value)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    _notify("domains_dirty")
    return {"domain_id": domain_id, "weight": weight}



@app.get("/domains/{domain_id}/path")
def domain_path(domain_id: int, src: int, dst: int):
    papers = {p["paper_id"] for p in store.list_papers(domain_id)}
    if src not in papers or dst not in papers:
        raise HTTPException(400, "src/dst must belong to this domain")
    if src == dst:
        return {"found": True, "path": [src], "edges": []}
    edges = [dict(r) for r in store.query(
        "SELECT citation_id, source_paper_id s, target_paper_id t, status FROM citations"
        " WHERE target_paper_id IS NOT NULL AND status='resolved'")]
    adj = {}
    for e in edges:
        if e["s"] in papers and e["t"] in papers:
            adj.setdefault(e["s"], []).append((e["t"], e))
            adj.setdefault(e["t"], []).append((e["s"], e))
    prev = {src: None}
    queue = [src]
    while queue:
        cur = queue.pop(0)
        if cur == dst:
            break
        for nxt, e in adj.get(cur, []):
            if nxt not in prev:
                prev[nxt] = (cur, e)
                queue.append(nxt)
    if dst not in prev:
        return {"found": False, "path": [], "edges": []}
    path, used = [], []
    cur = dst
    while cur is not None:
        path.append(cur)
        step = prev[cur]
        if step:
            used.append(step[1])
            cur = step[0]
        else:
            cur = None
    path.reverse()
    used.reverse()
    return {"found": True, "path": path,
            "edges": [{"citation_id": e["citation_id"], "source": e["s"],
                       "target": e["t"]} for e in used]}


@app.post("/domains/{domain_id}/glossary")
def build_glossary(domain_id: int, body: dict | None = None):
    """/: (re)generate the domain glossary. body {"update": true} merges
 incrementally with the existing table."""
    _llm_guard()
    try:
        update = bool(isinstance(body, dict) and body.get("update"))
        return translate.generate_glossary(domain_id, update=update)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/domains/{domain_id}/graph")
def domain_graph(domain_id: int):
    """domain-relation graph data. 一篇论文 = 主领域（唯一）+ 弱关联领域
 （paper_domain_links, score=质心相似度）——模糊归属在这里成图。
主领域边带真实 score（论文向量·领域质心），另加领域↔领域边（质心相似度）。"""
    import base64
    import numpy as np

    d = store.query("SELECT domain_id, name FROM domains WHERE domain_id=?", (domain_id,))
    if not d:
        raise HTTPException(404, "domain not found")
    papers = store.query(
        "SELECT p.paper_id, p.title, p.meta_json, p.domain_id, p.weight FROM papers p"
        " JOIN paper_domain_links l ON l.paper_id=p.paper_id"
        " WHERE l.domain_id=? AND l.deleted_at IS NULL AND p.deleted_at IS NULL",
        (domain_id,))
    center_c = assign.domain_centroid(domain_id)
    link_th = float(store.get_config("link_threshold") or 0.35)
    edges = []
    domains_here = {domain_id: d[0]["name"]}
    for p in papers:
        score = None
        meta = json.loads(p["meta_json"] or "{}")
        if meta.get("_vec64") and center_c is not None:
            vec = np.frombuffer(base64.b64decode(meta["_vec64"]), dtype=np.float32)
            score = round(float(np.dot(vec, center_c)), 3)
        edges.append({"paper_id": p["paper_id"], "domain_id": domain_id,
                      "kind": "primary" if p["domain_id"] == domain_id else "weak",
                      "score": score})
        links = store.query(
            "SELECT l.domain_id, l.score, dm.name FROM paper_domain_links l"
            " JOIN domains dm ON dm.domain_id = l.domain_id"
            " WHERE l.paper_id=?", (p["paper_id"],))
        for lk in links:
            if lk["domain_id"] == p["domain_id"]:
                continue
            if score is not None and lk["score"] >= link_th and lk["score"] >= score - 0.2:
                edges.append({"paper_id": p["paper_id"], "domain_id": lk["domain_id"],
                              "kind": "weak", "score": round(lk["score"], 3)})
                domains_here.setdefault(lk["domain_id"], lk["name"])
    dom_edges = []
    for did in domains_here:
        if did == domain_id:
            continue
        c2 = assign.domain_centroid(did)
        if center_c is not None and c2 is not None:
            dom_edges.append({"domain_a": domain_id, "domain_b": did,
                              "kind": "domain",
                              "score": round(float(np.dot(center_c, c2)), 3)})
    cite_rows = store.query(
        "SELECT c.citation_id, c.source_paper_id, c.target_paper_id, c.status, c.confidence"
        " FROM citations c JOIN paper_domain_links l ON l.paper_id=c.source_paper_id"
        " WHERE l.domain_id=? AND l.deleted_at IS NULL AND c.target_paper_id IS NOT NULL"
        " AND c.status='resolved'", (domain_id,))
    citations = [dict(r) for r in cite_rows]
    return {"center": domain_id, "name": d[0]["name"],
            "domains": [{"domain_id": did, "name": name} for did, name in domains_here.items()],
            "papers": [{"paper_id": p["paper_id"], "title": p["title"],
                        "weight": p["weight"] if p["weight"] is not None else 1.0} for p in papers],
            "edges": edges, "domain_edges": dom_edges, "citations": citations}


@app.get("/domains/{domain_id}/search")
def domain_search(domain_id: int, q: str):
    """in-domain full-text search (FTS5, hard domain filter via JOIN)."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    import re as _re
    tokens = [t for t in _re.split(r"\W+", q, flags=_re.UNICODE) if t]
    if not tokens:
        return []
    match = " AND ".join(f'"{t}"' for t in tokens[:8])
    rows = store.query(
        "SELECT c.paper_id, p.title, c.page, c.text FROM chunks_fts f"
        " JOIN chunks c ON c.chunk_id = f.rowid"
        " JOIN papers p ON p.paper_id = c.paper_id"
        " WHERE chunks_fts MATCH ? AND EXISTS(SELECT 1 FROM paper_domain_links l"
        "   WHERE l.paper_id=p.paper_id AND l.domain_id=? AND l.deleted_at IS NULL)"
        " ORDER BY bm25(chunks_fts) LIMIT 12",
        (match, domain_id))
    out = []
    for r in rows:
        text = r["text"]
        low = text.lower()
        pos = low.find(q.lower().split()[0]) if q.split() else -1
        start = max(0, pos - 40)
        snippet = ("…" if start else "") + text[start:start + 160].replace("\n", " ") + "…"
        out.append({"paper_id": r["paper_id"], "title": r["title"], "page": r["page"],
                    "snippet": snippet})
    return out


@app.get("/domains/{domain_id}/knowledge")
def domain_knowledge(domain_id: int):
    """domain-level sediment view — highlights/notes/QA aggregated across
 the papers of one domain. This is the 领域感知 story: the unit of reading
 output is the DOMAIN, not just the paper."""
    d = store.query("SELECT domain_id, name FROM domains WHERE domain_id=?", (domain_id,))
    if not d:
        raise HTTPException(404, "domain not found")
    papers = {r["paper_id"]: r["title"] for r in store.query(
        "SELECT p.paper_id, p.title FROM papers p JOIN paper_domain_links l"
        " ON l.paper_id=p.paper_id WHERE l.domain_id=? AND l.deleted_at IS NULL"
        " AND p.deleted_at IS NULL", (domain_id,))}

    def enrich(r):
        a = json.loads(r["anchor_json"] or "{}")
        return {"id": r["interaction_id"], "paper_id": r["paper_id"],
                "paper": papers.get(r["paper_id"], "(已移出)"),
                "page": a.get("page"), "quote": a.get("quote", ""),
                "category": r["content"], "text": r["content"]
                if r["type"] == "note" else None,
                "tags": store.parse_tags(r["tags_json"] if "tags_json" in r.keys() else "[]"),
                "created_at": r["created_at"]}

    rows = store.query(
        "SELECT i.interaction_id, i.paper_id, i.type, i.anchor_json, i.content, i.tags_json,"
        " i.created_at"
        " FROM interactions i JOIN paper_domain_links l ON l.paper_id = i.paper_id"
        " WHERE l.domain_id=? AND l.deleted_at IS NULL AND i.deleted_at IS NULL"
        " AND i.type IN ('highlight','note')"
        " ORDER BY i.interaction_id DESC", (domain_id,))
    highlights = [enrich(r) for r in rows if r["type"] == "highlight"]
    notes = [enrich(r) for r in rows if r["type"] == "note"]
    qa_rows = store.query(
        "SELECT interaction_id, paper_id, content, created_at FROM interactions i"
        " WHERE i.type='qa' AND i.deleted_at IS NULL AND (i.domain_id=? OR EXISTS("
        "   SELECT 1 FROM paper_domain_links l WHERE l.paper_id=i.paper_id"
        "   AND l.domain_id=? AND l.deleted_at IS NULL))"
        " ORDER BY i.interaction_id DESC LIMIT 30", (domain_id, domain_id))
    qa = [{"id": r["interaction_id"], "paper_id": r["paper_id"],
           "paper": papers.get(r["paper_id"], "(已移出)"),
           **json.loads(r["content"]), "created_at": r["created_at"]} for r in qa_rows]
    cat_counts = {}
    for h in highlights:
        cat_counts[h["category"]] = cat_counts.get(h["category"], 0) + 1
    return {"domain_id": domain_id, "name": d[0]["name"],
            "n_papers": len(papers), "n_highlights": len(highlights),
            "n_notes": len(notes), "n_qa": len(qa),
            "categories": cat_counts,
            "highlights": highlights, "notes": notes, "qa": qa}


@app.post("/domains/reassign")
def reassign_all():
    return {"moved": assign.reassign_all()}


@app.post("/domains/auto-threshold")
def auto_threshold():
    """estimate thresholds from the current corpus (same-domain sim distribution)."""
    try:
        return assign.auto_threshold()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/domains/reset-threshold")
def reset_threshold():
    """Restore the user-specified defaults (0.5 / 0.35)."""
    return assign.reset_threshold()


class MoveIn(BaseModel):
    domain_id: int


@app.post("/papers/{paper_id}/cancel")
def cancel_paper(paper_id: int):
    """取消排队中/解析中的论文。排队未跑 = 直接跳过；解析中 = 在下一个
 阶段检查点退出（状态置 failed/用户取消，可重试）。"""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    if p["parse_status"] not in ("queued", "parsing"):
        raise HTTPException(400, "论文不在解析队列中")
    _cancel_requested.add(paper_id)
    _notify("domains_dirty")
    return {"cancelling": paper_id}


@app.post("/papers/{paper_id}/retry")
def retry_paper(paper_id: int):
    """re-enqueue a failed paper (sync/retry audit — a visible recovery path)."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    if p["parse_status"] != "failed":
        raise HTTPException(400, "paper is not in failed state")
    store.set_parse_status(paper_id, "queued")
    store.set_progress(paper_id, 0)
    _enqueue(f"retry:{paper_id}", lambda pid=paper_id: _process_paper(pid), paper_id=paper_id)
    _notify("domains_dirty")
    return {"ok": True}


@app.post("/papers/{paper_id}/reparse")
def reparse_paper(paper_id: int):
    """Re-run the local parser (no download) — used after parser upgrades to
 refresh chunks, anchors, references and summaries."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    if not p["file_path"] or not os.path.isfile(p["file_path"]):
        raise HTTPException(400, "PDF file missing")
    if p["parse_status"] in ("queued", "parsing"):
        raise HTTPException(400, "paper is already parsing")
    store.set_parse_status(paper_id, "queued")
    store.set_progress(paper_id, 0)
    _enqueue(f"reparse:{paper_id}", lambda pid=paper_id: _process_paper(pid), paper_id=paper_id)
    _notify("domains_dirty")
    return {"ok": True}


@app.post("/papers/{paper_id}/move")
def move_paper(paper_id: int, body: MoveIn):
    """interactive domain judgment — user moves a paper to another domain."""
    try:
        r = assign.move_paper(paper_id, body.domain_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _notify("domains_dirty")
    return r


class DomainsIn(BaseModel):
    primary: int
    secondary: list[int] = []


@app.post("/papers/{paper_id}/domains")
def set_paper_domains(paper_id: int, body: DomainsIn):
    """手动归属编辑 — 主领域 + 兼属领域一次设定（数量不限，不看阈值）。"""
    try:
        r = assign.set_paper_domains(paper_id, body.primary, body.secondary)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _notify("domains_dirty")
    return r


@app.get("/papers")
def get_papers(domain_id: int | None = None):
    return store.list_papers(domain_id, include_meta=False)


@app.get("/papers/{paper_id}")
def get_paper_detail(paper_id: int, domain_id: int | None = None):
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    view_domain = _resolve_view_domain(p, domain_id)
    meta = json.loads(p["meta_json"] or "{}")
    n_chunks = store.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=?", (paper_id,))[0]["c"]
    attr = _paper_attribution(p)
    memberships = store.list_paper_domains(paper_id)
    linked = {m["domain_id"] for m in memberships}
    for a in attr:
        a["linked"] = a["domain_id"] in linked
        a["is_primary"] = a["domain_id"] == p["domain_id"]
    overlaps = []
    if view_domain:
        overlaps = store.concept_overlaps(
            view_domain, f"{p.get('title', '')} {p.get('abstract', '')}", limit=5)
    return {**p, "meta": meta, "n_chunks": n_chunks,
            "attribution": attr, "domains": memberships,
            "concept_overlaps": overlaps,
            "view_domain_id": view_domain,
            "position": store.get_reading_position(paper_id, view_domain),
            "positions": store.get_reading_positions(paper_id)}


def _paper_attribution(p: dict) -> list[dict]:
    """论文对各领域的原始余弦相似度（不归一——用户明确移除 Σ=1 权重，
 占比在领域少时≈1/N 无信息量；原始分可比跨论文）。降序。"""
    import base64
    import numpy as np

    meta = json.loads(p["meta_json"] or "{}")
    if not meta.get("_vec64"):
        return []
    vec = np.frombuffer(base64.b64decode(meta["_vec64"]), dtype=np.float32)
    scores = []
    for d in store.list_domains():
        c = assign.domain_centroid(d["domain_id"])
        if c is None:
            continue
        scores.append({"domain_id": d["domain_id"], "name": d["name"],
                       "score": round(float(np.dot(vec, c)), 3)})
    scores.sort(key=lambda x: -x["score"])
    return scores


class HighlightIn(BaseModel):
    page: int
    quote: str = Field(max_length=20000)
    category: str
    start: int | None = None
    end: int | None = None
    rects: list[dict] = Field(default_factory=list, max_length=100)
    occurrence: int = 0
    domain_id: int | None = None


class TranslateIn(BaseModel):
    quote: str = Field(max_length=20000)
    mode: str = "literal"


class ExplainIn(BaseModel):
    quote: str = Field(max_length=20000)


class NoteIn(BaseModel):
    page: int
    quote: str = Field(default="", max_length=20000)
    text: str = Field(max_length=20000)
    hl_id: int | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    start: int | None = None
    end: int | None = None
    rects: list[dict] = Field(default_factory=list, max_length=100)
    occurrence: int = 0
    domain_id: int | None = None


class NoteEditIn(BaseModel):
    text: str | None = None
    tags: list[str] | None = None


@app.get("/papers/{paper_id}/pdf")
def get_paper_pdf(paper_id: int):
    p = store.get_paper(paper_id)
    if not p or not p["file_path"] or not os.path.isfile(p["file_path"]):
        raise HTTPException(404, "pdf not available")
    try:
        real = os.path.realpath(p["file_path"])
        root = os.path.realpath(PAPERS_DIR)
        inside = os.path.commonpath([real, root]) == root
    except ValueError:
        inside = False
    if not inside:
        raise HTTPException(403, "pdf outside library")
    from fastapi.responses import FileResponse
    return FileResponse(real, media_type="application/pdf", filename=f"paper_{paper_id}.pdf")


def _locate_offsets(paper_id: int, page: int | None, quote: str,
                    occurrence: int = 0) -> tuple[int | None, int | None]:
    """Best-effort global character offsets for a selection.

 Coordinates match `chunks.start_char/end_char` (the paper's concatenated chunk
 text). Chunks that start on the selection page win; otherwise the whole paper
 is scanned. `occurrence` lets the client disambiguate the n-th identical quote
 on that page instead of always anchoring the first one.
    """
    q = (quote or "").strip()
    if not q:
        return None, None
    rows = store.query("SELECT page, start_char, text FROM chunks WHERE paper_id=?"
                       " ORDER BY chunk_id", (paper_id,))
    occ = max(0, int(occurrence or 0))
    tokens = [re.escape(t) for t in q.split()]
    flex = re.compile(r"\s+".join(tokens), re.IGNORECASE) if tokens else None
    subsets = []
    if page:
        subsets.append([r for r in rows if r["page"] == page])
    subsets.append(rows)
    for subset in subsets:
        hits = []
        for r in subset:
            text = r["text"] or ""
            base = int(r["start_char"] or 0)
            at = 0
            while True:
                idx = text.find(q, at)
                if idx < 0:
                    break
                hits.append((base + idx, base + idx + len(q)))
                at = idx + len(q)
        if not hits and flex is not None:
            for r in subset:
                text = r["text"] or ""
                base = int(r["start_char"] or 0)
                for m in flex.finditer(text):
                    hits.append((base + m.start(), base + m.end()))
        if hits:
            k = min(occ, len(hits) - 1)
            return hits[k]
    return None, None


def _hl_row(r) -> dict:
    try:
        a = json.loads(r["anchor_json"] or "{}")
    except (TypeError, ValueError):
        a = {}
    return {"id": r["interaction_id"], "page": a.get("page"),
            "quote": a.get("quote"), "source": a.get("source", "manual"),
            "category": r["content"], "created_at": r["created_at"]}


@app.get("/papers/{paper_id}/highlights")
def get_highlights(paper_id: int, domain_id: int | None = None):
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    view_domain = _resolve_view_domain(p, domain_id)
    rows = store.query(
        "SELECT * FROM interactions WHERE paper_id=? AND type='highlight'"
        " AND domain_id IS ? ORDER BY interaction_id",
        (paper_id, view_domain))
    return [_hl_row(r) for r in rows]


@app.post("/papers/{paper_id}/highlights")
def add_highlight(paper_id: int, body: HighlightIn):
    if body.category not in ("background", "approach", "limitation"):
        raise HTTPException(400, "category must be background/approach/limitation")
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    start, end = body.start, body.end
    if start is None or end is None:
        start, end = _locate_offsets(paper_id, body.page, body.quote, body.occurrence)
    anchor = json.dumps({"page": body.page, "quote": body.quote, "start": start,
                         "end": end, "rects": body.rects or []}, ensure_ascii=False)
    anchor_id = store.add_anchor(paper_id, body.page, body.quote, start, end, body.rects)
    cur = store.execute(
        "INSERT INTO interactions(domain_id, paper_id, type, anchor_json, content, anchor_id, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (_resolve_view_domain(p, body.domain_id), paper_id, "highlight", anchor,
         body.category, anchor_id, store.now()))
    if p["reading_status"] == "unread":
        store.set_reading_status(paper_id, "reading")
    _notify("domains_dirty")
    return {"id": cur.lastrowid}


@app.post("/papers/{paper_id}/highlights/auto")
def run_auto_highlight(paper_id: int, body: dict | None = None):
    """User-triggered auto-identify; a rerun replaces this domain's auto set."""
    _llm_guard()
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    if paper_id in _hl_inflight:
        raise HTTPException(409, "该论文的自动识别正在进行中")
    _hl_inflight.add(paper_id)
    try:
        rerun = bool(isinstance(body, dict) and body.get("rerun"))
        requested = body.get("domain_id") if isinstance(body, dict) else None
        r = highlight.auto_highlight(paper_id, rerun=rerun,
                                     domain_id=_resolve_view_domain(p, requested))
        if r.get("error"):
            raise HTTPException(502, r["error"])
        if r.get("added"):
            _notify("domains_dirty")
        return r
    finally:
        _hl_inflight.discard(paper_id)


@app.delete("/papers/{paper_id}/highlights/{hl_id}")
def delete_highlight(paper_id: int, hl_id: int):
    store.execute("DELETE FROM interactions WHERE interaction_id=? AND paper_id=? AND type='highlight'",
                  (hl_id, paper_id))
    return {"deleted": hl_id}



@app.post("/papers/{paper_id}/translate")
def translate_quote(paper_id: int, body: TranslateIn):
    p = store.get_paper(paper_id)
    if not p or p["parse_status"] != "done":
        raise HTTPException(404, "paper not parsed")
    _llm_guard()
    try:
        return translate.translate(paper_id, body.quote, body.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))



@app.post("/papers/{paper_id}/explain")
def explain_quote(paper_id: int, body: ExplainIn):
    p = store.get_paper(paper_id)
    if not p or p["parse_status"] != "done":
        raise HTTPException(404, "paper not parsed")
    _llm_guard()
    try:
        return translate.explain(paper_id, body.quote)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _note_row(r) -> dict:
    try:
        a = json.loads(r["anchor_json"] or "{}")
    except (TypeError, ValueError):
        a = {}
    tags = store.parse_tags(r["tags_json"] if "tags_json" in r.keys() else "[]")
    return {"id": r["interaction_id"], "page": a.get("page"), "quote": a.get("quote"),
            "hl_id": a.get("hl_id"), "qa_turn_id": a.get("qa_turn_id"),
            "text": r["content"], "tags": tags, "created_at": r["created_at"]}


@app.get("/papers/{paper_id}/notes")
def get_notes(paper_id: int, tag: str = ""):
    return [_note_row(r) for r in store.list_notes(paper_id, tag or None)]


@app.post("/papers/{paper_id}/notes")
def add_note(paper_id: int, body: NoteIn):
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    if not body.text.strip():
        raise HTTPException(400, "note text is empty")
    start, end = body.start, body.end
    if start is None or end is None:
        start, end = _locate_offsets(paper_id, body.page, body.quote, body.occurrence)
    note_id = store.add_note(paper_id, _resolve_view_domain(p, body.domain_id), body.page,
                             body.quote, body.hl_id, body.text.strip(), body.tags,
                             start, end, body.rects)
    if p["reading_status"] == "unread":
        store.set_reading_status(paper_id, "reading")
    return {"id": note_id}


@app.put("/papers/{paper_id}/notes/{note_id}")
def edit_note(paper_id: int, note_id: int, body: NoteEditIn):
    text = body.text.strip() if body.text is not None else None
    if text is not None and not text:
        raise HTTPException(400, "note text is empty")
    if text is None and body.tags is None:
        raise HTTPException(400, "nothing to update")
    store.update_note(paper_id, note_id, text, body.tags)
    return {"id": note_id}


@app.delete("/papers/{paper_id}/notes/{note_id}")
def delete_note(paper_id: int, note_id: int):
    store.delete_note(paper_id, note_id)
    return {"deleted": note_id}


@app.get("/domains/{domain_id}/notes")
def get_domain_notes(domain_id: int, tag: str = ""):
    """domain-wide note digest (过滤键与论文内一致)."""
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    rows = store.domain_notes(domain_id, tag or None)
    out = []
    for r in rows:
        item = _note_row(r)
        item["paper_id"] = r["paper_id"]
        item["paper_title"] = r["paper_title"]
        out.append(item)
    return out


@app.get("/notes/tags")
def note_tags():
    return {"tags": list(store.NOTE_TAGS)}


@app.post("/arxiv/search")
def search_arxiv(body: SearchIn):
    return arxiv.search(body.query, min(body.max_results, 50))


class BatchImportIn(BaseModel):
    query: str = ""
    max_results: int = 5
    domain_id: int | None = None


@app.post("/arxiv/batch-import")
def batch_import(body: BatchImportIn):
    """批量下载。query 直接作 arXiv 检索词；为空且指定 domain_id 时
 用领域术语表 top-5 英文词拼查询（领域扩充）。已入库的 ID 跳过不重复下载，
 其余复用 /papers/import 管线（限速/队列/归属全共享）。"""
    q = body.query.strip()
    if body.domain_id is not None and not store.query(
            "SELECT domain_id FROM domains WHERE domain_id=?", (body.domain_id,)):
        raise HTTPException(400, "target domain not found")
    if not q:
        if body.domain_id is None:
            raise HTTPException(400, "query required (or domain_id for domain-expansion mode)")
        dm = store.query("SELECT name, meta_json FROM domains WHERE domain_id=?", (body.domain_id,))
        try:
            g = json.loads(dm[0]["meta_json"] or "{}").get("glossary") or []
        except json.JSONDecodeError:
            g = []
        terms = [t["en"] for t in g[:5] if isinstance(t, dict) and t.get("en")]
        q = " OR ".join(terms) if terms else dm[0]["name"]
    try:
        hits = arxiv.search(q, min(max(1, body.max_results), 20))
    except Exception as e:
        raise HTTPException(502, f"arXiv 检索失败（网络超时或限流），请稍后重试: {e}")
    existing = {r["arxiv_id"] for r in store.query(
        "SELECT arxiv_id FROM papers WHERE arxiv_id IS NOT NULL")}
    fresh = [h for h in hits if h["arxiv_id"] and h["arxiv_id"] not in existing]
    if fresh:
        import_papers(ImportIn(arxiv_ids=[h["arxiv_id"] for h in fresh],
                               local_paths=[], domain_id=body.domain_id))
    return {"query": q, "found": len(hits), "queued": len(fresh),
            "skipped_existing": len(hits) - len(fresh),
            "papers": [{"arxiv_id": h["arxiv_id"], "title": h["title"]} for h in fresh]}



@app.get("/papers/{paper_id}/summary")
def get_summary(paper_id: int, domain_id: int | None = None):
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    view_domain = _resolve_view_domain(p, domain_id)
    goal = _domain_goal(view_domain)
    meta = json.loads(p["meta_json"] or "{}")
    cached = store.get_summary(paper_id, "five", goal, int(p.get("parse_version") or 0))
    content = (cached or {}).get("content") or meta.get("summaries", {})
    generated_for = ((meta.get("summaries") or {}).get("five_meta") or {}).get("research_goal", "")
    stale = bool(content and not cached and generated_for and goal and generated_for != goal)
    return {"summaries": content,
            "summary_error": meta.get("summary_error", ""),
            "domain_id": view_domain,
            "cache": {"hit": bool(cached), "research_goal": goal,
                      "generated_for": generated_for, "stale": stale,
                      "parse_version": p.get("parse_version") or 0},
            "parse_status": p["parse_status"],
            "title": p["title"], "authors": p["authors"], "abstract": p["abstract"]}


class GranularityIn(BaseModel):
    granularity: str = "five"
    domain_id: int | None = None


@app.post("/papers/{paper_id}/touch")
def touch_paper(paper_id: int):
    """Record an open event — the home page's「最近打开」is driven by this."""
    store.execute("UPDATE papers SET last_opened_at=? WHERE paper_id=?", (store.now(), paper_id))
    p = store.get_paper(paper_id)
    if p and p["reading_status"] == "unread":
        store.set_reading_status(paper_id, "reading")
    return {"ok": True}


@app.post("/papers/{paper_id}/summary/generate")
def gen_summary(paper_id: int, body: GranularityIn):
    """SSE summary generation with per-section progress and cancellation."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    _llm_guard()
    if body.granularity not in ("five", "para", "onesent"):
        raise HTTPException(400, "granularity must be five/para/onesent")
    if paper_id in _summary_inflight:
        raise HTTPException(409, "该论文的摘要正在生成中，请等待完成或取消")
    goal = _domain_goal(_resolve_view_domain(p, body.domain_id))
    _summary_inflight.add(paper_id)

    q: queue.Queue = queue.Queue()

    def work():
        try:
            if paper_id in _summary_cancelled:
                q.put({"canceled": True})
                return
            summaries = summarize.generate(
                paper_id, body.granularity,
                progress_cb=lambda done, total, section:
                    q.put({"progress": {"done": done, "total": total, "section": section}}),
                cancel_check=lambda: paper_id in _summary_cancelled,
                research_goal=goal)
            _notify("domains_dirty")
            q.put({"done": {"summaries": summaries}})
        except ValueError as e:
            q.put({"canceled": True} if str(e) == "__canceled__" else {"error": str(e)})
        except Exception as e:
            q.put({"error": f"summary generation failed: {e}"})
        finally:
            _summary_inflight.discard(paper_id)
            _summary_cancelled.discard(paper_id)

    def gen():
        threading.Thread(target=work, daemon=True).start()
        while True:
            item = q.get()
            yield _sse(item)
            if any(k in item for k in ("done", "canceled", "error")):
                break

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


_summary_cancelled: set[int] = set()
_summary_inflight: set[int] = set()
_hl_inflight: set[int] = set()


@app.post("/papers/{paper_id}/summary/cancel")
def cancel_summary(paper_id: int):
    _summary_cancelled.add(paper_id)
    return {"ok": True}


class ScopeInferIn(BaseModel):
    question: str = ""
    domain_id: int | None = None


@app.post("/papers/{paper_id}/qa/scope/infer")
def infer_qa_scope(paper_id: int, body: ScopeInferIn):
    """Infer the QA scope before answering; the UI shows a clickable chip."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    return assign.infer_scope(paper_id, _resolve_view_domain(p, body.domain_id),
                              body.question)


@app.get("/papers/{paper_id}/qa/suggestions")
def qa_suggestions(paper_id: int, domain_id: int | None = None):
    """Goal-aware question suggestions (deterministic; no model call)."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    view_domain = _resolve_view_domain(p, domain_id)
    goal = _domain_goal(view_domain)
    meta = json.loads(p["meta_json"] or "{}")
    sections = " ".join(s.get("title", "") for s in (meta.get("sections") or [])).lower()
    out: list[str] = []
    for q in store.list_questions(domain_id=view_domain, paper_id=paper_id, status="open")[:2]:
        out.append(f"「{q['text'][:40]}」这个问题，论文里给了什么线索？")
    templates = {
        "找 idea": ["这篇论文的局限与未来工作是什么？", "方法里有哪些设计可以迁移到别的任务？",
                    "实验没有覆盖哪些场景？"],
        "写 related work": ["本文相对领域内其他论文的核心差异是什么？",
                            "如何用三句话概括这篇论文的贡献？", "它引用了哪些关键工作？"],
        "快速了解": ["用三句话概括这篇论文。", "方法的关键步骤是什么？", "主要实验结果说明了什么？"],
        "审稿": ["实验设置与对比基线是否充分？", "作者是否充分讨论了局限性？", "贡献的新颖性体现在哪里？"],
        "教学备课": ["这篇论文适合讲解哪些概念？", "如何用一个例子解释核心方法？", "读懂它需要哪些前置知识？"],
    }
    pool = list(templates.get(goal) or ["这篇论文解决了什么问题？", "核心方法是什么？",
                                        "结论在什么范围内成立？"])
    if "limitation" in sections or "局限" in sections:
        pool.insert(0, "作者承认的局限是什么？")
    for item in pool:
        if item not in out:
            out.append(item)
        if len(out) >= 4:
            break
    return {"goal": goal, "suggestions": out[:4]}


class QaIn(BaseModel):
    question: str = Field(default="", max_length=4000)
    session_id: int | None = None
    thinking: bool = False
    image_b64: str | None = Field(default=None, max_length=15_000_000)
    domain_ids: list[int] = Field(default_factory=list, max_length=100)
    scope: str = ""
    persist_scope: bool = True
    domain_id: int | None = None


NOANSWER_GATE = 0.30


@app.get("/papers/{paper_id}/qa/sessions")
def list_qa_sessions(paper_id: int, domain_id: int | None = None):
    """Sessions of this paper + domain-wide sessions, for the current domain."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    view_domain = _resolve_view_domain(p, domain_id)
    rows = store.qa_sessions(paper_id, view_domain)
    if not rows:
        store.qa_session_create(paper_id, view_domain, "会话 1")
        rows = store.qa_sessions(paper_id, view_domain)
    return [dict(r) for r in rows]


@app.post("/papers/{paper_id}/qa/sessions")
def create_qa_session(paper_id: int, body: dict):
    name = (body.get("name") or "").strip() or "新会话"
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    view_domain = _resolve_view_domain(p, body.get("domain_id"))
    domain_wide = bool(body.get("domain_wide"))
    requested = [d for d in (body.get("domain_ids") or []) if isinstance(d, int)]
    sid = store.qa_session_create(None if domain_wide else paper_id,
                                  view_domain, name,
                                  domain_ids=requested or ([view_domain] if view_domain else []))
    return {"session_id": sid, "name": name, "paper_id": None if domain_wide else paper_id,
            "domain_id": view_domain}


@app.put("/papers/{paper_id}/qa/sessions/{session_id}")
def update_qa_session(paper_id: int, session_id: int, body: dict):
    """会话改名 + 跨论文会话的指定论文子集（paper_ids 为空/缺省 = 全领域）。"""
    name = body.get("name")
    if name is not None:
        name = str(name).strip()
        if not name:
            raise HTTPException(400, "name is empty")
        store.qa_session_rename(session_id, name)
    if "paper_ids" in body:
        ids = body.get("paper_ids")
        if ids is not None and (
                not isinstance(ids, list) or not all(isinstance(i, int) for i in ids)):
            raise HTTPException(400, "paper_ids must be a list of ints")
        store.qa_session_set_papers(session_id, ids or None)
    if "domain_ids" in body:
        ids = body.get("domain_ids")
        if ids is not None and (
                not isinstance(ids, list) or not all(isinstance(i, int) for i in ids)):
            raise HTTPException(400, "domain_ids must be a list of ints")
        store.qa_session_set_domains(session_id, ids or None)
    return {"ok": True}


@app.delete("/papers/{paper_id}/qa/sessions/{session_id}")
def delete_qa_session(paper_id: int, session_id: int):
    store.qa_session_delete(session_id)
    return {"deleted": session_id}


@app.delete("/papers/{paper_id}/qa/turns/{interaction_id}")
def delete_qa_turn(paper_id: int, interaction_id: int):
    """Retract one persisted QA turn.

the scope chip re-asks the question, so the turn it replaces
 must disappear from the history — otherwise every correction leaves two
 contradictory answers.
    """
    if not store.delete_qa_turn(interaction_id, paper_id):
        raise HTTPException(404, "qa turn not found")
    return {"deleted": interaction_id}


class ImportantIn(BaseModel):
    important: bool = True


@app.post("/papers/{paper_id}/qa/turns/{interaction_id}/important")
def mark_qa_turn(paper_id: int, interaction_id: int, body: ImportantIn):
    try:
        result = store.set_qa_importance(paper_id, interaction_id, body.important)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _notify("domains_dirty")
    return result


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/papers/{paper_id}/qa")
def paper_qa(paper_id: int, body: QaIn):
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "empty question")
    p = store.get_paper(paper_id)
    if not p or p["parse_status"] != "done":
        raise HTTPException(400, "paper not parsed")
    _llm_guard()
    view_domain = _resolve_view_domain(p, body.domain_id)

    if body.session_id:
        sess = store.qa_session_get(body.session_id)
        if not sess:
            raise HTTPException(404, "session not found")
    else:
        rows = store.qa_sessions(paper_id, view_domain)
        if not rows:
            store.qa_session_create(paper_id, view_domain, "会话 1")
            rows = store.qa_sessions(paper_id, view_domain)
        sess = rows[0]
    session_id = sess["session_id"]
    domain_wide = sess["paper_id"] is None
    try:
        involved = (json.loads(sess["domain_ids_json"])
                    if sess["domain_ids_json"] else [view_domain])
    except (json.JSONDecodeError, TypeError):
        involved = [view_domain]
    involved = [d for d in involved if d is not None]

    explicit = []
    valid_domains = {d["domain_id"] for d in store.list_domains()}
    if body.scope == "all":
        explicit = sorted(valid_domains)
    elif body.domain_ids:
        explicit = [d for d in dict.fromkeys(body.domain_ids) if d in valid_domains]
    force_paper = body.scope == "paper"
    if force_paper:
        involved = [view_domain] if view_domain else []
    elif explicit:
        involved = explicit
        if body.persist_scope:
            try:
                store.qa_session_set_domains(session_id, explicit)
            except Exception:
                pass

    concepts = store.concept_context(involved or ([view_domain] if view_domain else []),
                                     question)
    same_terms = [c["term"] for c in concepts
                  if any(m.get("type") == "same" for m in (c.get("mappings") or []))]
    search_query = question
    if explicit and same_terms:
        search_query = question + " " + " ".join(dict.fromkeys(same_terms))

    primary_domain_id = None
    if explicit:
        primary_domain_id, _ = assign.primary_domain(question, explicit)

    incomplete: list[dict] = []
    if force_paper:
        if store.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=? AND vec IS NULL",
                       (paper_id,))[0]["c"]:
            assign.embed_chunks(paper_id)
        results, top_sim = retrieval.retrieve(paper_id, question, k=8)
    elif explicit:
        paper_scope = None
        try:
            paper_scope = json.loads(sess["paper_ids_json"]) if sess["paper_ids_json"] else None
        except (json.JSONDecodeError, TypeError):
            paper_scope = None
        if store.query(
                "SELECT COUNT(*) c FROM chunks c JOIN papers p ON p.paper_id=c.paper_id"
                f" WHERE EXISTS(SELECT 1 FROM paper_domain_links l WHERE l.paper_id=p.paper_id"
                f"   AND l.domain_id IN ({','.join('?' for _ in explicit)})"
                "    AND l.deleted_at IS NULL) AND c.vec IS NULL",
                tuple(explicit))[0]["c"]:
            for r in store.query(
                    "SELECT DISTINCT l.paper_id FROM paper_domain_links l"
                    f" WHERE l.domain_id IN ({','.join('?' for _ in explicit)})"
                    " AND l.deleted_at IS NULL", tuple(explicit)):
                assign.embed_chunks(r["paper_id"])
        results, top_sim, incomplete = retrieval.retrieve_domains_scoped(
            explicit, search_query, k=8, paper_ids=paper_scope,
            weights=store.domain_weights(explicit), primary_domain_id=primary_domain_id)
    elif domain_wide:
        if store.query(
                "SELECT COUNT(*) c FROM chunks c JOIN papers p ON p.paper_id=c.paper_id"
                " WHERE EXISTS(SELECT 1 FROM paper_domain_links l WHERE l.paper_id=p.paper_id"
                "   AND l.domain_id=? AND l.deleted_at IS NULL) AND c.vec IS NULL",
                (view_domain,))[0]["c"]:
            for r in store.query(
                    "SELECT DISTINCT l.paper_id FROM paper_domain_links l"
                    " WHERE l.domain_id=? AND l.deleted_at IS NULL", (view_domain,)):
                assign.embed_chunks(r["paper_id"])
        try:
            sel_papers = json.loads(sess["paper_ids_json"]) if sess["paper_ids_json"] else None
        except (json.JSONDecodeError, TypeError):
            sel_papers = None
        results, top_sim = retrieval.retrieve_domain(view_domain, question, k=8,
                                                     paper_ids=sel_papers)
    else:
        if store.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=? AND vec IS NULL", (paper_id,))[0]["c"]:
            assign.embed_chunks(paper_id)
        results, top_sim = retrieval.retrieve(paper_id, question, k=8)

    note_domains = explicit or ([view_domain] if domain_wide else [])
    note_results = retrieval.retrieve_notes(
        paper_id if (force_paper or not note_domains) else None,
        note_domains if not force_paper else None, search_query, k=3)
    if note_results:
        top_sim = max(top_sim, max(float(n["score"]) for n in note_results))

    history = store.query(
        "SELECT content FROM interactions WHERE type='qa'"
        " AND CAST(json_extract(CASE WHEN json_valid(anchor_json)"
        "            THEN anchor_json ELSE '{}' END, '$.session_id') AS INTEGER)=?"
        " ORDER BY interaction_id DESC LIMIT 3", (session_id,))
    hist_msgs = []
    for row in reversed(history):
        try:
            turn = json.loads(row["content"] or "{}")
        except (TypeError, ValueError):
            continue
        hist_msgs.append({"role": "user", "content": turn.get("question", "")})
        hist_msgs.append({"role": "assistant", "content": (turn.get("answer") or "")[:1500]})

    evidence = []
    for c in results:
        evidence.append({"kind": "paper", **c})
    for n in note_results:
        evidence.append({"kind": "note", **n})

    refs = []
    for i, c in enumerate(evidence):
        text = c.get("text") or c.get("quote") or ""
        if c["kind"] == "note":
            ref = {"n": i + 1, "page": c.get("page"), "preview": text[:120],
                   "source": "note", "paper_id": c.get("paper_id"),
                   "title": "用户笔记", "network": False}
        else:
            ref = {"n": i + 1, "page": c.get("page"), "preview": text[:120],
                   "source": "paper", "network": False}
            if "title" in c:
                ref["paper_id"] = c["paper_id"]
                ref["title"] = c["title"]
        refs.append(ref)

    system = store.get_config("prompt_qa")
    if explicit:
        scope_note = (f"以下材料来自 {len(explicit)} 个研究领域（跨领域比较），"
                      "每条标注来源论文与页码；领域间定义可能冲突，需分别说明。")
        if primary_domain_id:
            names = {d["domain_id"]: d["name"] for d in store.list_domains()}
            scope_note += (f" 主领域：{names.get(primary_domain_id, primary_domain_id)}"
                           "（深度检索），其他领域按广度检索。")
    elif domain_wide:
        scope_note = "以下材料来自本领域多篇论文（用于跨论文比较），每条标注来源论文与页码。"
    else:
        scope_note = "以下材料来自当前论文。"
    if note_results:
        scope_note += (" 材料中标注为「用户笔记」的条目来自用户的批注；引用时须说明来自笔记，"
                       "不要把笔记内容说成论文原文。")
    scope_note += " 本版不进行网络检索，不得声称信息来自网络。"
    if incomplete:
        names = "、".join(x["name"] or f"#{x['domain_id']}" for x in incomplete)
        scope_note += (f" 以下领域的检索未完成或失败：{names}，"
                       "其证据不完整，回答时须明确说明。")
    if concepts:
        name_by_id = {d["domain_id"]: d["domain"] for c in concepts for d in c["definitions"]}
        lines = []
        for c in concepts:
            defs = "；".join(f"{d['domain']}＝{d['definition'] or '（未填写）'}"
                            for d in c["definitions"])
            flags = []
            for m in c.get("mappings") or []:
                label = {"different": "含义不同", "same": "含义相同",
                         "related": "相关"}.get(m["type"], m["type"])
                a = name_by_id.get(m["a"], f"#{m['a']}")
                b = name_by_id.get(m["b"], f"#{m['b']}")
                flags.append(f"{a}↔{b}：{label}")
            suffix = f"（映射：{'；'.join(flags)}）" if flags else ""
            lines.append(f"- {c['term']}{suffix}：{defs}")
        scope_note += ("\n概念对照（来自领域概念库，引用同一术语时须按来源领域分别说明定义）：\n"
                       + "\n".join(lines))
        if same_terms:
            scope_note += ("\n含义相同的概念已作为跨领域检索连接点加入查询："
                           + "、".join(dict.fromkeys(same_terms)))
    material_lines = []
    for i, c in enumerate(evidence):
        text = c.get("text") or c.get("quote") or ""
        label = "用户笔记" if c["kind"] == "note" else (c.get("title") or "本文")
        material_lines.append(f"[{i + 1}] ({label} p.{c.get('page') or '?'}) {text}")
    material = "\n\n".join(material_lines)
    messages = [{"role": "system", "content": system + f"\n范围说明：{scope_note}"}]
    messages += hist_msgs
    if body.image_b64:
        user_content = [
            {"type": "text", "text": f"材料：\n{material}\n\n问题：{question}"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{body.image_b64}"}},
        ]
    else:
        user_content = f"材料：\n{material}\n\n问题：{question}"
    messages.append({"role": "user", "content": user_content})

    def gen():
        stream = None
        try:
            if not evidence and top_sim < NOANSWER_GATE and not body.image_b64:
                yield _sse({"noanswer": True,
                            "reason": "材料中未找到相关内容（检索无命中）"})
                yield _sse({"done": {"empty": True}})
                return
            yield _sse({"refs": refs, "incomplete": incomplete,
                        "scope": {"kind": body.scope or ("domain" if domain_wide else "paper"),
                                  "domain_ids": involved,
                                  "primary_domain_id": primary_domain_id,
                                  "sources": ["paper"] + (["note"] if note_results else [])}})
            answer_parts = []
            try:
                stream = llm.chat_stream(task="qa", messages=messages,
                                         model=llm.pro_model(), temperature=0.2,
                                         max_tokens=2048,
                                         thinking="enabled" if body.thinking else None)
                for delta in stream:
                    if delta.startswith("__meta__"):
                        meta = json.loads(delta[len("__meta__"):])
                        answer = "".join(answer_parts)
                        # every [n] in the answer is checked against the
                        # material sent in this turn
                        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})
                        invalid = [n for n in cited if n < 1 or n > len(refs)]
                        refs_check = {"total": len(refs), "cited": cited,
                                      "invalid": invalid}
                        cur = store.execute(
                            "INSERT INTO interactions(domain_id, paper_id, type, anchor_json,"
                            " content, domains_json, created_at)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (view_domain, paper_id, "qa",
                             json.dumps({"session_id": session_id, "ask_paper_id": paper_id},
                                        ensure_ascii=False),
                             json.dumps({"question": question, "answer": answer, "refs": refs,
                                         "has_image": bool(body.image_b64),
                                         "incomplete": incomplete,
                                         "invalid_refs": invalid,
                                         "thinking": body.thinking},
                                        ensure_ascii=False),
                             json.dumps(involved), store.now()))
                        if cur is not None and cur.lastrowid:
                            meta["interaction_id"] = cur.lastrowid
                        yield _sse({"refs_check": refs_check})
                        yield _sse({"done": meta})
                    else:
                        answer_parts.append(delta)
                        yield _sse({"token": delta})
            except Exception as e:
                yield _sse({"error": str(e)[:200]})
                yield _sse({"done": {"failed": True}})
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/papers/{paper_id}/qa/history")
def qa_history(paper_id: int, session_id: int | None = None):
    if session_id:
        rows = store.query(
            "SELECT interaction_id, anchor_json, content, created_at, importance"
            " FROM interactions WHERE type='qa'"
            " AND CAST(json_extract(CASE WHEN json_valid(anchor_json)"
            "            THEN anchor_json ELSE '{}' END, '$.session_id') AS INTEGER)=?"
            " ORDER BY interaction_id", (int(session_id),))
    else:
        rows = store.query(
            "SELECT interaction_id, anchor_json, content, created_at, importance"
            " FROM interactions WHERE paper_id=? AND type='qa'"
            " ORDER BY interaction_id", (paper_id,))
    out = []
    for r in rows:
        try:
            turn = json.loads(r["content"] or "{}")
        except (TypeError, ValueError):
            turn = {}
        try:
            sid = (json.loads(r["anchor_json"] or "{}")).get("session_id")
        except (TypeError, ValueError):
            sid = None
        out.append({**turn, "created_at": r["created_at"], "session_id": sid,
                    "interaction_id": r["interaction_id"],
                    "importance": bool(r["importance"])})
    return out


@app.get("/qa/history-all")
def qa_history_all(limit: int = 50):
    """首页（未打开论文）问答面板的全库历史 — 只读浏览，带论文与会话来源。"""
    rows = store.query(
        """SELECT i.interaction_id, i.content, i.created_at, i.paper_id, i.importance,
 p.title,
 CAST(json_extract(CASE WHEN json_valid(i.anchor_json)
 THEN i.anchor_json ELSE '{}' END,
 '$.session_id') AS INTEGER) AS sid,
 s.name AS session_name
 FROM interactions i
 JOIN papers p ON p.paper_id = i.paper_id
 LEFT JOIN qa_sessions s
 ON s.session_id = CAST(json_extract(CASE WHEN json_valid(i.anchor_json)
 THEN i.anchor_json ELSE '{}' END,
 '$.session_id') AS INTEGER)
 WHERE i.type='qa' ORDER BY i.interaction_id DESC LIMIT ?""",
        (min(limit, 200),))
    out = []
    for r in reversed(rows):
        try:
            turn = json.loads(r["content"] or "{}")
        except (TypeError, ValueError):
            turn = {}
        out.append({**turn, "created_at": r["created_at"], "paper_id": r["paper_id"],
                    "paper_title": r["title"], "session": r["session_name"],
                    "session_id": r["sid"], "interaction_id": r["interaction_id"],
                    "importance": bool(r["importance"])})
    return out


@app.get("/llm/stats")
def llm_stats():
    return llm.api_call_stats()


@app.get("/audit/report")
def audit_report():
    """quality audit page data (面试数字页) — per-task LLM stats,
 evidence verbatim rates, sediment counts. Read-only aggregation."""
    tasks = llm.query_audit_by_task()
    five = store.query(
        "SELECT meta_json FROM papers WHERE parse_status='done'")
    ev_total = ev_ok = 0
    for r in five:
        try:
            s = (json.loads(r["meta_json"] or "{}").get("summaries") or {}).get("five") or []
        except json.JSONDecodeError:
            continue
        for sec in s:
            if sec.get("evidence"):
                ev_total += 1
                if sec.get("page"):
                    ev_ok += 1
    hl = store.query(
        "SELECT content, COUNT(*) c FROM interactions WHERE type='highlight' GROUP BY content")
    notes_n = store.query("SELECT COUNT(*) c FROM interactions WHERE type='note'")[0]["c"]
    qa_n = store.query("SELECT COUNT(*) c FROM interactions WHERE type='qa'")[0]["c"]
    tr_n = store.query("SELECT COUNT(*) c FROM translations")[0]["c"]
    return {
        "tasks": tasks,
        "summary_evidence": {"total": ev_total, "with_page": ev_ok},
        "highlights": {r["content"]: r["c"] for r in hl},
        "notes": notes_n, "qa_turns": qa_n, "translation_segments": tr_n,
        "llm_stats": llm.api_call_stats(),
    }



def _redact_profiles(profiles: list) -> list[dict]:
    out = []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        item = {k: v for k, v in p.items() if k != "api_key"}
        item["has_key"] = bool(str(p.get("api_key") or "").strip())
        out.append(item)
    return out


def _ensure_profile_ids(profiles: list[dict]) -> bool:
    changed = False
    for p in profiles:
        if not isinstance(p, dict):
            continue
        if not str(p.get("profile_id") or "").strip():
            p["profile_id"] = uuid.uuid4().hex[:12]
            changed = True
    return changed


@app.get("/llm/profiles")
def get_profiles():
    """Provider profiles, with the secret replaced by a `has_key` flag.

the old response echoed `api_key` in clear text while /config
 redacted it; any page on a localhost port could read it through CORS.
profiles carry a stable profile_id so renaming keeps the key.
    """
    try:
        profiles = json.loads(store.get_config("llm_profiles") or "[]")
    except (TypeError, ValueError):
        profiles = []
    if not isinstance(profiles, list):
        profiles = []
    profiles = [p for p in profiles if isinstance(p, dict)]
    if _ensure_profile_ids(profiles):
        try:
            store.set_config("llm_profiles", json.dumps(profiles, ensure_ascii=False))
        except Exception:
            pass
    return {"profiles": _redact_profiles(profiles),
            "active": store.get_config("llm_active")}


class ProfilesIn(BaseModel):
    profiles: list[dict]
    active: str


def _validate_profiles(profiles: list[dict]) -> list[str]:
    if not profiles:
        raise HTTPException(400, "at least one profile required")
    names = [str(p.get("name", "")).strip() for p in profiles]
    if any(not n for n in names) or len(set(names)) != len(names):
        raise HTTPException(400, "profile 名称必须非空且唯一")
    ids = [str(p.get("profile_id", "")).strip() for p in profiles]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        raise HTTPException(400, "profile id 必须非空且唯一")
    for p in profiles:
        base = str(p.get("api_base", "")).strip()
        if not base.startswith(("http://", "https://")):
            raise HTTPException(400, f"profile {p.get('name')}: api_base 必须是 http(s) 地址")
        p["api_base"] = base.rstrip("/")
        p["api_key"] = str(p.get("api_key") or "").strip()
        for key in ("model_fast", "model_pro"):
            p[key] = str(p.get(key) or "").strip()
            if not p[key]:
                raise HTTPException(400, f"profile {p.get('name')}: {key} 不能为空")
        if "vision_model" in p:
            p["vision_model"] = str(p.get("vision_model") or "").strip()
        p["thinking"] = p.get("thinking") if p.get("thinking") in ("disabled", "enabled") else "disabled"
    return names


@app.put("/llm/profiles")
def put_profiles(body: ProfilesIn):
    try:
        old_list = json.loads(store.get_config("llm_profiles") or "[]")
    except (TypeError, ValueError):
        old_list = []
    old_list = [p for p in old_list if isinstance(p, dict)]
    _ensure_profile_ids(old_list)
    old_by_id = {str(p.get("profile_id")): p for p in old_list if p.get("profile_id")}
    old_by_name = {str(p.get("name")): p for p in old_list if p.get("name")}
    old_used: set[str] = set()
    profiles = []
    for p in body.profiles:
        name = str(p.get("name", "")).strip()
        requested_id = str(p.get("profile_id") or "").strip()
        old = old_by_id.get(requested_id) if requested_id else None
        if old is None:
            old = old_by_name.get(name)
        if old is None:
            candidates = [o for o in old_list
                          if str(o.get("profile_id")) not in old_used
                          and o.get("api_base") == str(p.get("api_base") or "").strip().rstrip("/")
                          and o.get("model_fast") == str(p.get("model_fast") or "").strip()
                          and o.get("model_pro") == str(p.get("model_pro") or "").strip()]
            if len(candidates) == 1:
                old = candidates[0]
        old = old or {}
        if old.get("profile_id"):
            old_used.add(str(old["profile_id"]))
        merged = dict(old)
        merged.update({k: v for k, v in p.items()
                       if v is not None and k not in ("clear_key", "has_key")})
        merged["name"] = name
        merged["profile_id"] = str(old.get("profile_id") or requested_id
                                   or uuid.uuid4().hex[:12])
        if not str(merged.get("api_key") or "").strip() and str(old.get("api_key") or "").strip():
            merged["api_key"] = old["api_key"]
        if p.get("clear_key"):
            merged["api_key"] = ""
        profiles.append(merged)
    names = _validate_profiles(profiles)
    if body.active not in names:
        raise HTTPException(400, "active profile 不在列表中")
    store.set_config("llm_profiles", json.dumps(profiles, ensure_ascii=False))
    store.set_config("llm_active", body.active)
    _notify("domains_dirty")
    return {"ok": True, "count": len(profiles), "active": body.active}


def _looks_like_pdf(path: str) -> bool:
    """local import accepts only real PDFs. Without this check any file
 (SSH key, browser profile, APIKEY.txt) could be copied into data/papers and
 downloaded back through GET /papers/{id}/pdf."""
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def _ingest_local_file(lp: str, domain_id: int | None = None) -> dict:
    """Copy local PDF into data/papers/{sha1}.pdf (app-owned storage, survives
 external deletion); sha1 dedup against the whole library."""
    import hashlib
    import shutil
    h = hashlib.sha1()
    with open(lp, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    sha1 = h.hexdigest()
    dup = store.query("SELECT paper_id FROM papers WHERE sha1=?", (sha1,))
    if dup:
        pid = dup[0]["paper_id"]
        linked = False
        if domain_id is not None and not any(
                l["domain_id"] == domain_id for l in store.list_paper_domains(pid)):
            store.add_paper_domain(pid, domain_id, score=0.0, source="import")
            linked = True
        return {"path": lp, "paper_id": pid, "duplicate": True, "linked": linked}
    os.makedirs(PAPERS_DIR, exist_ok=True)
    dest = os.path.join(PAPERS_DIR, f"{sha1}.pdf")
    if not os.path.exists(dest):
        tmp = dest + ".part"
        shutil.copy2(lp, tmp)
        os.replace(tmp, dest)
    extra = {"glossary_hint": None}
    if domain_id is not None:
        extra["_import_domain"] = domain_id
    try:
        gm = pdfio.guess_metadata(lp)
    except Exception:
        gm = {}
    title = (gm.get("title") or "").strip() or os.path.splitext(os.path.basename(lp))[0]
    pid = store.upsert_paper(domain_id=None, title=title,
                             file_path=dest, sha1=sha1,
                             authors=gm.get("authors", ""),
                             abstract=gm.get("abstract", ""),
                             year=gm.get("year"), meta=extra)
    store.set_parse_status(pid, "queued")
    _enqueue(f"local:{lp}", lambda p=pid: _process_paper(p), paper_id=pid)
    return {"path": lp, "paper_id": pid}


@app.post("/papers/import")
def import_papers(body: ImportIn):
    if body.domain_id is not None and not store.query(
            "SELECT domain_id FROM domains WHERE domain_id=?", (body.domain_id,)):
        raise HTTPException(400, "target domain not found")
    merged: list[str] = []
    for raw in body.arxiv_ids:
        line = raw.strip()
        if not line:
            continue
        if merged and not arxiv.normalize_id(line) and not line.lower().startswith("http"):
            merged[-1] += line
        else:
            merged.append(line)
    ids = []
    for raw in merged:
        aid = arxiv.normalize_id(raw)
        if aid:
            ids.append(aid)
    if not ids and not body.local_paths:
        raise HTTPException(400, "no valid arxiv id or local path")
    _task_state["batch_total"] = 0
    _task_state["batch_done"] = 0

    def import_meta(extra: dict) -> dict:
        if body.domain_id is not None:
            extra["_import_domain"] = body.domain_id
        return extra

    results = []
    if ids:
        # an already-imported arXiv id is reported as duplicate and not re-queued
        existing = {r["arxiv_id"]: r["paper_id"] for r in store.query(
            "SELECT arxiv_id, paper_id FROM papers WHERE arxiv_id IS NOT NULL")}
        ordered = list(dict.fromkeys(ids))
        for aid in ordered:
            if aid in existing:
                pid = existing[aid]
                linked = False
                if body.domain_id is not None and not any(
                        l["domain_id"] == body.domain_id
                        for l in store.list_paper_domains(pid)):
                    store.add_paper_domain(pid, body.domain_id, score=0.0, source="import")
                    linked = True
                results.append({"arxiv_id": aid, "paper_id": pid,
                                "duplicate": True, "skipped": True, "linked": linked})
        ids = [aid for aid in ordered if aid not in existing]
        if ids:
            metas = arxiv.fetch_metadata(ids)
            for aid in ids:
                meta = metas.get(aid)
                if not meta:
                    results.append({"arxiv_id": aid, "error": "not found on arXiv"})
                    continue
                pid = store.upsert_paper(
                    domain_id=None, title=meta["title"], file_path=None, arxiv_id=aid,
                    authors=meta["authors"], abstract=meta["abstract"], year=meta["year"],
                    meta=import_meta({"primary_category": meta["primary_category"]}),
                )
                store.set_parse_status(pid, "queued")
                _enqueue(f"arxiv:{aid}", lambda p=pid: _process_paper(p), paper_id=pid)
                results.append({"arxiv_id": aid, "paper_id": pid, "title": meta["title"]})
    for lp in body.local_paths:
        lp = os.path.abspath(lp)
        if not os.path.isfile(lp):
            results.append({"path": lp, "error": "file not found"})
            continue
        if os.path.splitext(lp)[1].lower() != ".pdf" or not _looks_like_pdf(lp):
            results.append({"path": lp, "error": "not a PDF file"})
            continue
        results.append(_ingest_local_file(lp, domain_id=body.domain_id))
    _notify("domains_dirty")
    return {"queued": results}


@app.get("/config")
def get_config():
    """Runtime config, with provider secrets redacted.

 llm_profiles carries api_key; the settings UI edits profiles through
 /llm/profiles, so /config has no reason to echo the key. Redact it here
 instead of handing a cleartext secret to every caller of this endpoint.
    """
    out = {k: store.get_config(k) for k in store.CONFIG_DEFAULTS}
    raw = out.get("llm_profiles")
    if raw:
        try:
            items = json.loads(raw)
            for item in items:
                if isinstance(item, dict) and item.get("api_key"):
                    item["api_key"] = ""
                    item["has_key"] = True
            out["llm_profiles"] = json.dumps(items, ensure_ascii=False)
        except (ValueError, TypeError):
            out.pop("llm_profiles", None)
    return out


NUMERIC_CONFIG = {"assign_threshold", "link_threshold", "domain_dispersion_weight",
                  "max_tokens_default", "max_output_limit"}
UNIT_INTERVAL_CONFIG = {"assign_threshold", "link_threshold", "domain_dispersion_weight"}


class ConfigIn(BaseModel):
    values: dict[str, str] = {}


@app.put("/config")
def put_config(body: ConfigIn):
    clean: dict[str, str] = {}
    for key, value in body.values.items():
        if key not in store.CONFIG_DEFAULTS:
            raise HTTPException(400, f"unknown config key: {key}")
        if key in ("llm_profiles", "llm_active"):
            raise HTTPException(400, "模型档案请通过 /llm/profiles 更新（含校验与脱敏）")
        if key in NUMERIC_CONFIG:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} must be numeric")
            if not math.isfinite(number):
                raise HTTPException(400, f"{key} must be a finite number")
            if key in UNIT_INTERVAL_CONFIG and not 0.0 <= number <= 1.0:
                raise HTTPException(400, f"{key} must be between 0 and 1")
            if key == "max_tokens_default" and not 256 <= number <= 200000:
                raise HTTPException(400, "max_tokens_default must be between 256 and 200000")
            if key == "max_output_limit" and not 256 <= number <= 2000000:
                raise HTTPException(400, "max_output_limit must be between 256 and 2000000")
        if len(value) > 4000:
            raise HTTPException(400, f"{key} too long (max 4000 chars)")
        clean[key] = value
    try:
        default_tokens = int(float(clean.get("max_tokens_default",
                                             store.get_config("max_tokens_default"))))
        limit_tokens = int(float(clean.get("max_output_limit",
                                           store.get_config("max_output_limit"))))
    except (TypeError, ValueError):
        raise HTTPException(400, "token limits must be numeric")
    if default_tokens > limit_tokens:
        raise HTTPException(400, "max_tokens_default must not exceed max_output_limit")
    store.set_configs(clean)
    _notify("domains_dirty")
    return get_config()


class RenameIn(BaseModel):
    name: str


@app.post("/domains/{domain_id}/rename")
def rename_domain(domain_id: int, body: RenameIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    if any(d["name"] == name and d["domain_id"] != domain_id for d in store.list_domains()):
        raise HTTPException(400, f"domain '{name}' already exists")
    store.rename_domain(domain_id, name)
    d = store.get_domain(domain_id)
    meta = json.loads(d["meta_json"] or "{}") if d else {}
    meta["user_named"] = True
    store.set_domain_meta(domain_id, meta)
    _notify("domains_dirty")
    return {"domain_id": domain_id, "name": name}


@app.post("/domains/{domain_id}/auto-name")
def auto_name_domain(domain_id: int):
    """自动命名建议（不直接改名）。arXiv 类别众数优先（过半论文同类别才
 可信），否则标题+摘要合并后的启发式高频词。前端填入行内输入框供确认。"""
    from collections import Counter
    d = store.query("SELECT name FROM domains WHERE domain_id=?", (domain_id,))
    if not d:
        raise HTTPException(404, "domain not found")
    papers = store.query(
        "SELECT p.title, p.abstract, p.meta_json FROM papers p"
        " JOIN paper_domain_links l ON l.paper_id=p.paper_id"
        " WHERE l.domain_id=? AND l.deleted_at IS NULL AND p.deleted_at IS NULL"
        " AND p.parse_status!='failed'", (domain_id,))
    if not papers:
        raise HTTPException(400, "领域内没有论文，无法自动命名")
    cats = []
    for p in papers:
        m = json.loads(p["meta_json"] or "{}")
        if m.get("primary_category"):
            cats.append(m["primary_category"])
    if cats:
        top, n = Counter(cats).most_common(1)[0]
        if n * 2 > len(papers):
            return {"name": top.replace(".", "-")}
    title = " ".join(p["title"] or "" for p in papers)
    abstract = " ".join(p["abstract"] or "" for p in papers)
    name = assign._heuristic_domain_name(title, abstract)
    if not name:
        raise HTTPException(400, "论文信息不足以自动命名，请手动输入")
    return {"name": name}


@app.delete("/domains/{domain_id}")
def remove_domain(domain_id: int):
    orphans = store.delete_domain(domain_id)
    for pid in orphans:
        _enqueue(f"reassign:{pid}", lambda p=pid: assign.assign_paper(p), paper_id=pid)
    _notify("domains_dirty")
    return {"deleted": domain_id, "orphans_reassigning": len(orphans)}


class MergeIn(BaseModel):
    src_id: int
    dst_id: int


@app.post("/domains/merge")
def merge_domains(body: MergeIn):
    if body.src_id == body.dst_id:
        raise HTTPException(400, "cannot merge a domain into itself")
    if not store.get_domain(body.src_id) or not store.get_domain(body.dst_id):
        raise HTTPException(404, "domain not found")
    try:
        moved = store.merge_domains(body.src_id, body.dst_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # the store rolled back; surface a conflict, not a 500
        raise HTTPException(409, f"合并失败，已回滚：{str(e)[:200]}")
    assign.rebuild_centroid(body.dst_id)
    _notify("domains_dirty")
    return {"merged": body.src_id, "into": body.dst_id, "moved": moved}


class SplitIn(BaseModel):
    name: str = ""
    paper_ids: list[int] = []


@app.post("/domains/{domain_id}/split")
def split_domain(domain_id: int, body: SplitIn):
    try:
        result = store.split_domain(domain_id, body.name, body.paper_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        assign.rebuild_centroid(domain_id)
        assign.rebuild_centroid(result["domain_id"])
    except Exception as e:
        print(f"[split] centroid rebuild skipped: {e}")
    _notify("domains_dirty")
    return result



@app.get("/health/integrity")
def health_integrity():
    """Invariant report (primary links, orphans, duplicate identities)."""
    return store.integrity_report()


class ReadingIn(BaseModel):
    domain_id: int | None = None
    status: str | None = None
    progress: float | None = None
    # reading position: page + scroll ratio within the page (0..1)
    page: int | None = None
    scroll_ratio: float | None = None


@app.post("/papers/{paper_id}/reading")
def set_reading(paper_id: int, body: ReadingIn):
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    try:
        if body.domain_id is not None:
            store.set_domain_reading(paper_id, body.domain_id, body.status, body.progress,
                                     body.page, body.scroll_ratio)
        else:
            if body.status:
                store.set_reading_status(paper_id, body.status)
            if body.progress is not None:
                store.set_progress(paper_id, body.progress)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.status is not None or body.progress is not None:
        _notify("domains_dirty")
    return {"paper_id": paper_id, "domains": store.list_paper_domains(paper_id)}


@app.get("/papers/{paper_id}/domains")
def get_paper_domains(paper_id: int):
    if not store.get_paper(paper_id):
        raise HTTPException(404, "paper not found")
    return store.list_paper_domains(paper_id)


class LinkIn(BaseModel):
    domain_id: int
    score: float = 0.0


@app.post("/papers/{paper_id}/domains/add")
def add_paper_domain(paper_id: int, body: LinkIn):
    try:
        store.add_paper_domain(paper_id, body.domain_id, score=body.score, source="manual")
    except ValueError as e:
        raise HTTPException(400, str(e))
    _notify("domains_dirty")
    return {"paper_id": paper_id, "domains": store.list_paper_domains(paper_id)}


@app.delete("/papers/{paper_id}/domains/{domain_id}")
def remove_paper_domain(paper_id: int, domain_id: int):
    store.remove_paper_domain(paper_id, domain_id)
    _notify("domains_dirty")
    return {"paper_id": paper_id, "domains": store.list_paper_domains(paper_id)}


@app.get("/papers/{paper_id}/citations")
def get_paper_citations(paper_id: int, status: str | None = None):
    return store.list_citations(paper_id=paper_id, status=status)


@app.post("/papers/{paper_id}/citations/rebuild")
def rebuild_citations(paper_id: int):
    """Re-resolve the bibliography after importing more papers (or after a
 matcher fix). Reads chunks, so it does not re-download/re-parse the PDF."""
    if not store.get_paper(paper_id):
        raise HTTPException(404, "paper not found")
    rows = store.query("SELECT text FROM chunks WHERE paper_id=? ORDER BY chunk_id", (paper_id,))
    full = "\n\n".join(r["text"] for r in rows)
    if not full.strip():
        raise HTTPException(400, "paper has no text")
    store.save_references(paper_id, _match_references(full, paper_id))
    store.refresh_review_items(paper_id)
    return {"paper_id": paper_id, "citations": store.list_citations(paper_id=paper_id)}


@app.get("/domains/{domain_id}/citations")
def get_domain_citations(domain_id: int, status: str | None = None):
    return store.list_citations(domain_id=domain_id, status=status)


class CitationResolveIn(BaseModel):
    target_paper_id: int | None = None
    status: str = "resolved"


@app.post("/citations/{citation_id}/resolve")
def resolve_citation(citation_id: int, body: CitationResolveIn):
    try:
        return store.resolve_citation(citation_id, body.target_paper_id, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/domains/{domain_id}/concepts")
def get_domain_concepts(domain_id: int):
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    return {"domain_id": domain_id, "concepts": store.list_concepts(domain_id)}


class ConceptIn(BaseModel):
    term_en: str
    term_zh: str = ""
    definition: str = ""
    source_paper_id: int | None = None


@app.post("/domains/{domain_id}/concepts")
def add_domain_concept(domain_id: int, body: ConceptIn):
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    if not body.term_en.strip():
        raise HTTPException(400, "term_en required")
    cid = store.upsert_concept(body.term_en, body.term_zh, body.definition,
                               domain_id, body.source_paper_id)
    return {"concept_id": cid, "concepts": store.list_concepts(domain_id)}


class DefinitionIn(BaseModel):
    definition: str
    term_zh: str = ""
    source_paper_id: int | None = None


@app.put("/concepts/{concept_id}/definitions/{domain_id}")
def put_concept_definition(concept_id: int, domain_id: int, body: DefinitionIn):
    try:
        store.set_concept_definition(concept_id, domain_id, body.definition,
                                     body.term_zh, body.source_paper_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"concept_id": concept_id, "domain_id": domain_id}


class MappingIn(BaseModel):
    concept_id: int
    domain_a: int
    domain_b: int
    mapping_type: str = "related"
    user_note: str = ""


@app.get("/concepts/mappings")
def get_concept_mappings(domain_id: int | None = None):
    return store.list_concept_mappings(domain_id)


@app.post("/concepts/mappings")
def post_concept_mapping(body: MappingIn):
    try:
        mid = store.add_concept_mapping(body.concept_id, body.domain_a, body.domain_b,
                                        body.mapping_type, body.user_note)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"mapping_id": mid, "mappings": store.list_concept_mappings(body.domain_a)}



class QuestionIn(BaseModel):
    paper_id: int | None = None
    domain_id: int | None = None
    text: str
    source_type: str = ""
    source_id: int | None = None


@app.get("/domains/{domain_id}/questions")
def get_domain_questions(domain_id: int, status: str | None = "open"):
    return store.list_questions(domain_id=domain_id, status=status)


@app.get("/papers/{paper_id}/questions")
def get_paper_questions(paper_id: int, status: str | None = "open"):
    return store.list_questions(paper_id=paper_id, status=status)


@app.post("/questions")
def post_question(body: QuestionIn):
    try:
        qid = store.add_question(body.paper_id, body.domain_id, body.text,
                                 body.source_type, body.source_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _notify("domains_dirty")
    return {"question_id": qid}


class ResolveIn(BaseModel):
    status: str = "resolved"


@app.post("/questions/{question_id}/resolve")
def resolve_question(question_id: int, body: ResolveIn):
    try:
        store.resolve_question(question_id, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _notify("domains_dirty")
    return {"question_id": question_id, "status": body.status}


@app.get("/domains/{domain_id}/reviews")
def get_domain_reviews(domain_id: int, status: str | None = "pending"):
    return store.list_reviews(domain_id=domain_id, status=status)


class ReviewResolveIn(BaseModel):
    status: str = "confirmed"
    target_paper_id: int | None = None


@app.post("/reviews/{item_id}/resolve")
def resolve_review(item_id: int, body: ReviewResolveIn):
    try:
        store.resolve_review(item_id, body.status, body.target_paper_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _notify("domains_dirty")
    return {"item_id": item_id, "status": body.status}



@app.get("/domains/{domain_id}/config")
def get_domain_config(domain_id: int):
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    return store.get_domain_config(domain_id)


@app.put("/domains/{domain_id}/config")
def put_domain_config(domain_id: int, body: dict):
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    return store.set_domain_config(domain_id, body or {})


@app.get("/operations")
def get_operations(domain_id: int | None = None, limit: int = 50):
    return store.list_operations(domain_id, min(max(1, limit), 200))


class ExportIn(BaseModel):
    scope: str
    scope_id: int | None = None
    format: str = "markdown"
    paper_ids: list[int] = []
    path: str = ""
    note: str = ""


@app.post("/exports")
def post_export(body: ExportIn):
    eid = store.record_export(body.scope, body.scope_id, body.format,
                              body.paper_ids, body.path, body.note)
    return {"export_id": eid}


class ParentIn(BaseModel):
    parent_id: int | None = None


@app.post("/domains/{domain_id}/parent")
def set_domain_parent(domain_id: int, body: ParentIn):
    try:
        store.set_domain_parent(domain_id, body.parent_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _notify("domains_dirty")
    return {"domain_id": domain_id, "parent_id": body.parent_id}



class SnapshotIn(BaseModel):
    name: str = ""


@app.post("/domains/{domain_id}/snapshots")
def create_snapshot(domain_id: int, body: SnapshotIn):
    try:
        sid = store.create_snapshot(domain_id, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"snapshot_id": sid, "snapshots": store.list_snapshots(domain_id)}


@app.get("/domains/{domain_id}/snapshots")
def get_snapshots(domain_id: int):
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    return store.list_snapshots(domain_id)


class SuggestionIn(BaseModel):
    source_domain_id: int
    target_domain_id: int
    description: str
    evidence: list = []
    confidence: float = 0.0


@app.get("/suggestions")
def get_suggestions(domain_id: int | None = None, status: str | None = None):
    return store.list_suggestions(domain_id, status)


@app.post("/suggestions")
def post_suggestion(body: SuggestionIn):
    try:
        sid = store.add_suggestion(body.source_domain_id, body.target_domain_id,
                                   body.description, body.evidence, body.confidence)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"suggestion_id": sid}


class FeedbackIn(BaseModel):
    status: str
    snooze_until: str | None = None


@app.post("/suggestions/{suggestion_id}/feedback")
def post_suggestion_feedback(suggestion_id: int, body: FeedbackIn):
    try:
        store.set_suggestion_feedback(suggestion_id, body.status, body.snooze_until)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"suggestion_id": suggestion_id, "status": body.status}



def _summary_content(paper_id: int, domain_id: int | None = None) -> dict:
    """Summary for export/report/card, with the same fallback as GET /summary."""
    p = store.get_paper(paper_id)
    if not p:
        return {}
    meta = json.loads(p["meta_json"] or "{}")
    goal = _domain_goal(_resolve_view_domain(p, domain_id))
    row = store.get_summary(paper_id, "five", goal, int(p.get("parse_version") or 0))
    return (row or {}).get("content") or (meta.get("summaries") or {})


def _paper_markdown(paper_id: int, domain_id: int | None = None) -> str:
    p = store.get_paper(paper_id)
    if not p:
        raise ValueError("paper not found")
    content = _summary_content(paper_id, domain_id)
    lines = [f"# {p['title']}", "",
             f"- 作者：{p.get('authors') or ''}",
             f"- 年份：{p.get('year') or ''}",
             f"- arXiv：{p.get('arxiv_id') or ''}",
             f"- DOI：{p.get('doi') or ''}", ""]
    if p.get("abstract"):
        lines += ["## 摘要", "", p["abstract"], ""]
    labels = {"研究问题": "研究问题", "背景动机": "背景动机", "方法路径": "方法路径",
              "核心成果": "核心成果", "适用边界与局限": "适用边界与局限"}
    for sec in content.get("five", []) if isinstance(content, dict) else []:
        if isinstance(sec, dict) and sec.get("section"):
            lines += [f"## {labels.get(sec['section'], sec['section'])}", "", sec.get("text", ""), ""]
    notes = store.query(
        "SELECT content, anchor_json FROM interactions WHERE paper_id=? AND type='note'"
        " AND deleted_at IS NULL ORDER BY interaction_id", (paper_id,))
    if notes:
        lines += ["## 笔记", ""]
        for n in notes:
            a = json.loads(n["anchor_json"] or "{}")
            q = (a.get("quote") or "").strip()
            lines.append(f"- {('> ' + q + ' — ' if q else '')}{n['content']}")
        lines.append("")
    hls = store.query(
        "SELECT content, anchor_json FROM interactions WHERE paper_id=? AND type='highlight'"
        " AND deleted_at IS NULL ORDER BY interaction_id LIMIT 30", (paper_id,))
    if hls:
        lines += ["## 高亮", ""]
        for h in hls:
            a = json.loads(h["anchor_json"] or "{}")
            lines.append(f"- [{h['content']}] p.{a.get('page', '?')} {a.get('quote', '')}")
        lines.append("")
    return "\n".join(lines)


def _domain_markdown(domain_id: int, kind: str = "report") -> str:
    d = store.get_domain(domain_id)
    if not d:
        raise ValueError("domain not found")
    papers = store.list_papers(domain_id)
    title = "学习报告" if kind == "report" else "文献综述草稿"
    lines = [f"# {d['name']} · {title}", ""]
    if d.get("research_goal"):
        lines += [f"研究目标：{d['research_goal']}", ""]
    lines += [f"论文数量：{len(papers)}", ""]
    grouped = {}
    for p in papers:
        grouped.setdefault(p.get("year") or 0, []).append(p)
    for year in sorted(grouped, reverse=True):
        lines += [f"## {year or '未标注年份'}", ""]
        for p in grouped[year]:
            lines += [f"### {p['title']}", ""]
            content = _summary_content(p["paper_id"], domain_id)
            for sec in content.get("five", []) if isinstance(content, dict) else []:
                if isinstance(sec, dict) and sec.get("text"):
                    lines.append(f"- **{sec.get('section')}**：{sec['text']}")
            lines.append("")
    if kind == "report":
        qs = store.list_questions(domain_id=domain_id, status="open")
        if qs:
            lines += ["## 未决问题", ""] + [f"- {q['text']}" for q in qs] + [""]
        rs = store.list_reviews(domain_id=domain_id, status="pending")
        if rs:
            lines += ["## 待复核引用", ""] + [
                f"- {r.get('target_title') or r.get('target_doi') or '未命名'}（来自 {r.get('source_title')}）"
                for r in rs] + [""]
    return "\n".join(lines)


def _card_or_built(paper_id: int, domain_id: int | None = None) -> tuple[dict, bool]:
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    view_domain = _resolve_view_domain(p, domain_id)
    saved = store.get_reading_card(paper_id, view_domain)
    return ((saved or {}).get("content")
            or store.build_reading_card(paper_id, view_domain)), bool(saved)


@app.get("/papers/{paper_id}/card")
def get_reading_card(paper_id: int, domain_id: int | None = None):
    card, saved = _card_or_built(paper_id, domain_id)
    return {"saved": saved, "card": card}


class CardIn(BaseModel):
    content: dict
    domain_id: int | None = None


@app.post("/papers/{paper_id}/card")
@app.put("/papers/{paper_id}/card")
def save_reading_card(paper_id: int, body: CardIn):
    """Save the card for the domain currently being read."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    card = body.content or {}
    card.setdefault("paper_id", paper_id)
    store.save_reading_card(paper_id, _resolve_view_domain(p, body.domain_id), card)
    return {"paper_id": paper_id, "saved": True, "card": card}


@app.delete("/papers/{paper_id}/card")
def delete_reading_card(paper_id: int, domain_id: int | None = None):
    """Discard the saved card; the next GET returns a freshly assembled one."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    removed = store.delete_reading_card(paper_id, _resolve_view_domain(p, domain_id))
    return {"paper_id": paper_id, "deleted": bool(removed)}


@app.get("/papers/{paper_id}/card/export")
def export_reading_card(paper_id: int, domain_id: int | None = None):
    """Download the saved card (or a freshly assembled one) as Markdown."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    card, _ = _card_or_built(paper_id, domain_id)
    lines = [f"# 阅读卡片 · {p['title']}", ""]
    if p.get("authors"):
        lines += [f"- 作者：{p['authors']}"]
    if p.get("year"):
        lines += [f"- 年份：{p['year']}"]
    lines.append("")
    for key, label in (("research_question", "研究问题"), ("method", "方法"),
                       ("findings", "核心结论"), ("limitations", "适用边界与局限")):
        if card.get(key):
            lines += [f"## {label}", "", str(card[key]), ""]
    for key, label in (("notes", "笔记"), ("open_questions", "未决问题")):
        vals = [v for v in (card.get(key) or []) if v]
        if vals:
            lines += [f"## {label}", ""] + [f"- {v}" for v in vals] + [""]
    lines += [f"- 高亮：{card.get('n_highlights', 0)} 条",
              f"- 引用：{card.get('n_citations', 0)} 条", ""]
    store.record_export("paper-card", paper_id, "markdown", [paper_id], "", "reading card")
    return {"filename": f"card-{paper_id}.md", "markdown": "\n".join(lines)}


@app.get("/papers/{paper_id}/skim")
def paper_skim(paper_id: int, domain_id: int | None = None):
    """Goal-aware reading path over parsed sections (no LLM)."""
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    goal = _domain_goal(_resolve_view_domain(p, domain_id))
    rows = store.query(
        "SELECT section, MIN(page) page, MIN(chunk_id) first_chunk FROM chunks"
        " WHERE paper_id=? AND section<>'' GROUP BY section ORDER BY first_chunk", (paper_id,))
    if not rows:
        total = store.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=?", (paper_id,))[0]["c"]
        return {"goal": goal, "path": [{"title": "全文", "page": 1, "reason": "未识别章节"}],
                "n_chunks": total}
    prefs = {
        "找 idea": ["局限", "未来", "讨论", "方法", "结果"],
        "写 related work": ["相关", "引言", "方法", "结论"],
        "快速了解": ["摘要", "引言", "方法", "结论", "结果"],
        "审稿": ["方法", "实验", "结果", "局限"],
        "教学备课": ["背景", "引言", "方法", "结论"],
    }.get(goal, ["摘要", "引言", "方法", "结果", "结论"])
    picked, seen = [], set()
    for want in prefs:
        for r in rows:
            if r["section"] in seen:
                continue
            if want in r["section"]:
                picked.append({"title": r["section"], "page": r["page"],
                               "reason": f"研究目标「{goal or '通用'}」优先"})
                seen.add(r["section"])
                break
    for r in rows:
        if len(picked) >= 6:
            break
        if r["section"] not in seen:
            picked.append({"title": r["section"], "page": r["page"], "reason": "章节补充"})
            seen.add(r["section"])
    if len(picked) < 3:
        pages = [r["page"] for r in store.query(
            "SELECT DISTINCT page FROM chunks WHERE paper_id=? AND page IS NOT NULL"
            " ORDER BY page", (paper_id,))]
        if pages:
            picks = [pages[0], pages[len(pages) // 4], pages[len(pages) // 2],
                     pages[(len(pages) * 3) // 4], pages[-1]]
            for pg in dict.fromkeys(picks):
                if len(picked) >= 5:
                    break
                if any(x["page"] == pg for x in picked):
                    continue
                picked.append({"title": f"第 {pg} 页", "page": pg,
                               "reason": "按页抽样（章节识别不可用）"})
    return {"goal": goal, "path": picked[:6]}


@app.get("/papers/{paper_id}/outline")
def paper_outline(paper_id: int):
    """section tree + figure/table/algorithm index.

 The structure panel and the in-text "Fig. 3 / Table 2 / Algorithm 1" jumps
 both read this. Papers parsed before have no `floats` in meta, so the
 index falls back to scanning the chunk text (no re-parse required).
    """
    p = store.get_paper(paper_id)
    if not p:
        raise HTTPException(404, "paper not found")
    meta = json.loads(p["meta_json"] or "{}")
    sections = meta.get("sections") or []
    figures = meta.get("figures") or []
    floats = list(meta.get("floats") or [])
    if not sections:
        rows = store.query(
            "SELECT section, MIN(page) page, MIN(chunk_id) first_chunk FROM chunks"
            " WHERE paper_id=? AND section<>'' GROUP BY section ORDER BY first_chunk",
            (paper_id,))
        sections = [{"title": r["section"], "level": 1, "page": r["page"]} for r in rows]
    if not floats:
        for r in store.query("SELECT page, text FROM chunks WHERE paper_id=? ORDER BY chunk_id",
                             (paper_id,)):
            for para in re.split(r"\n{2,}", r["text"] or ""):
                para = para.strip()
                m = pdfio.FLOAT_RE.match(para)
                if m:
                    floats.append({"page": r["page"],
                                   "kind": pdfio._float_kind(m.group(1)),
                                   "num": m.group(2),
                                   "label": pdfio._float_label(m.group(1), m.group(2)),
                                   "text": para[:240]})
    seen, uniq = set(), []
    for f in floats:
        key = (f.get("kind"), f.get("num"), f.get("page"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)
    return {"paper_id": paper_id, "n_pages": meta.get("n_pages"),
            "sections": sections,
            # nested tree for the collapsible outline
            "section_tree": _section_tree(sections),
            "figures": figures, "floats": uniq,
            "n_figures": len(figures),
            "tables": [f for f in uniq if f.get("kind") == "table"],
            "algorithms": [f for f in uniq if f.get("kind") == "algorithm"]}


def _section_tree(sections: list) -> list:
    """Build a nested section tree from a flat list carrying `level`."""
    roots: list = []
    stack: list = []
    for i, s in enumerate(sections):
        if not isinstance(s, dict):
            continue
        node = {**s, "index": i, "children": []}
        level = int(s.get("level") or 1)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((level, node))
    return roots


@app.get("/papers/{paper_id}/export")
def export_paper(paper_id: int, record: bool = True, domain_id: int | None = None):
    try:
        text = _paper_markdown(paper_id, domain_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    p = store.get_paper(paper_id)
    if record:
        store.record_export("paper", paper_id, "markdown", [paper_id], "",
                            "paper markdown")
    return {"filename": f"{p['title'][:60] or 'paper'}.md", "markdown": text}


@app.get("/domains/{domain_id}/report")
def domain_report(domain_id: int):
    try:
        text = _domain_markdown(domain_id, "report")
    except ValueError as e:
        raise HTTPException(404, str(e))
    store.record_export("domain-report", domain_id, "markdown", [], "", "learning report")
    return {"filename": f"report-{domain_id}.md", "markdown": text}


@app.get("/domains/{domain_id}/survey")
def domain_survey_preview(domain_id: int, group_by: str = "topic",
                          include_gaps: bool = True):
    """ preview: same generator as POST, but never persists and never calls a model."""
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    try:
        res = survey.build([domain_id], None, group_by, include_gaps, False, False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"filename": _survey_filename(res["title"]), "markdown": res["markdown"],
            "stats": res["stats"]}


class SurveyIn(BaseModel):
    domain_ids: list[int] = []
    paper_ids: list[int] = []
    group_by: str = "topic"
    include_gaps: bool = True
    include_notes: bool = False
    synthesize: bool = False
    title: str = ""
    save: bool = False
    survey_id: int | None = None


def _survey_filename(title: str) -> str:
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "-", (title or "survey")).strip("-") or "survey"
    return f"{safe}.md"


@app.post("/domains/{domain_id}/survey")
def domain_survey(domain_id: int, body: SurveyIn):
    """生成综述草稿，可跨领域选文。

`save=true` 或带 `survey_id` 时落库，之后可继续编辑。
    """
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    domain_ids = [d for d in dict.fromkeys(body.domain_ids or []) if store.get_domain(d)]
    if domain_id not in domain_ids:
        domain_ids = [domain_id] + domain_ids
    try:
        res = survey.build(domain_ids, body.paper_ids or None, body.group_by,
                           body.include_gaps, body.include_notes, body.synthesize,
                           body.title)
    except ValueError as e:
        raise HTTPException(400, str(e))
    survey_id = None
    if body.save or body.survey_id:
        try:
            survey_id = store.save_survey_draft(
                domain_id, domain_ids, body.paper_ids or [], res["stats"]["group_by"],
                res["title"], res["markdown"], res["stats"], body.survey_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
    store.record_export("domain-survey", domain_id, "markdown", body.paper_ids or [],
                        "", "survey draft")
    warn = None
    if body.synthesize and not res["stats"].get("synthesized"):
        warn = "未配置模型 Key，已跳过模型导言；其余内容照常生成。"
    return {"survey_id": survey_id, "filename": _survey_filename(res["title"]),
            "title": res["title"],
            "markdown": res["markdown"], "stats": res["stats"],
            "gaps": res["gaps"], "warning": warn}


@app.get("/domains/{domain_id}/surveys")
def list_surveys(domain_id: int):
    return store.list_survey_drafts(domain_id)


@app.get("/surveys/{survey_id}")
def get_survey(survey_id: int):
    d = store.get_survey_draft(survey_id)
    if not d:
        raise HTTPException(404, "survey draft not found")
    return d


class SurveyEditIn(BaseModel):
    title: str | None = None
    markdown: str | None = None


@app.put("/surveys/{survey_id}")
def edit_survey(survey_id: int, body: SurveyEditIn):
    try:
        return store.update_survey_draft(survey_id, body.title, body.markdown)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/surveys/{survey_id}")
def remove_survey(survey_id: int):
    store.delete_survey_draft(survey_id)
    return {"deleted": survey_id}



class SubtopicIn(BaseModel):
    name: str
    parent_id: int | None = None


@app.get("/domains/{domain_id}/subtopics")
def get_subtopics(domain_id: int):
    return store.list_subtopics(domain_id)


@app.post("/domains/{domain_id}/subtopics")
def post_subtopic(domain_id: int, body: SubtopicIn):
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    try:
        sid = store.create_subtopic(domain_id, body.name, body.parent_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"subtopic_id": sid, "subtopics": store.list_subtopics(domain_id)}


@app.delete("/subtopics/{subtopic_id}")
def remove_subtopic(subtopic_id: int):
    store.delete_subtopic(subtopic_id)
    return {"deleted": subtopic_id}


class PaperSubtopicsIn(BaseModel):
    subtopic_ids: list[int] = []


@app.get("/papers/{paper_id}/subtopics")
def get_paper_subtopics(paper_id: int):
    return store.list_paper_subtopics(paper_id)


@app.post("/papers/{paper_id}/subtopics")
def post_paper_subtopics(paper_id: int, body: PaperSubtopicsIn):
    store.assign_paper_subtopics(paper_id, body.subtopic_ids)
    return {"paper_id": paper_id, "subtopics": store.list_paper_subtopics(paper_id)}


class SnapshotRestoreIn(BaseModel):
    include_notes: bool = False
    include_qa: bool = False


@app.post("/domains/{domain_id}/snapshots/{snapshot_id}/restore")
def restore_snapshot(domain_id: int, snapshot_id: int,
                     body: SnapshotRestoreIn | None = None):
    opts = body or SnapshotRestoreIn()
    try:
        return store.restore_snapshot(snapshot_id, opts.include_notes, opts.include_qa,
                                      expect_domain_id=domain_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/domains/{domain_id}/index/migrate")
def migrate_domain_index(domain_id: int):
    if not store.get_domain(domain_id):
        raise HTTPException(404, "domain not found")
    path = store.export_domain_index(domain_id, os.path.join(DATA_DIR, "indexes"))
    return {"domain_id": domain_id, "mode": "physical", "file": path}
