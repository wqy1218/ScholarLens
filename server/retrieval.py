"""混合检索：FTS5(BM25) 与向量余弦，RRF 融合（k=60）。"""
import time

import numpy as np

from . import store

RRF_K = 60


def retrieve(paper_id: int, query: str, k: int = 8, domain_id: int | None = None) -> tuple[list[dict], float]:
    """按 paper_id 过滤，给出 domain_id 时一并过滤。
返回 (RRF 融合后的 top-k, 最高向量相似度)，后者用于无答案判定。"""
    rows = store.query(
        "SELECT chunk_id, text, page, vec FROM chunks WHERE paper_id=?", (paper_id,))
    if not rows:
        return [], 0.0
    results, top_sim = _fuse(rows, query, k)
    return results, top_sim


def retrieve_notes(paper_id: int | None, domain_ids: list[int] | None, query: str,
                   k: int = 3) -> list[dict]:
    """用户笔记的语义 top-k，来源标记为"用户笔记"。

笔记没有 FTS/向量列，按范围取回后即时向量化、按余弦排序。
无笔记或未配置模型输入时返回 []，不向问答流抛异常。
    """
    if not (query or "").strip():
        return []
    rows = store.list_notes_for_qa(paper_id=paper_id, domain_ids=domain_ids, limit=80)
    if not rows:
        return []
    texts = [f"{r['quote']} {r['text']}".strip() for r in rows]
    try:
        from . import assign
        vecs = assign.embed([query] + texts)
    except Exception:
        return []
    q = vecs[0]
    scored = []
    for r, v in zip(rows, vecs[1:]):
        if not (r["text"] or "").strip():
            continue
        scored.append((float(np.dot(q, v)), r))
    scored.sort(key=lambda x: -x[0])
    return [{**r, "score": round(s, 5), "source": "note"} for s, r in scored[:k] if s > 0]


def retrieve_domain(domain_id: int, query: str, k: int = 8,
                    paper_ids: list[int] | None = None) -> tuple[list[dict], float]:
    """cross-paper retrieval over a whole domain (多论文比较 sessions).
 Results additionally carry paper_id/title for source display.
paper_ids 指定时只在该子集内检索（跨论文=指定几篇对比）。"""
    if paper_ids:
        marks = ",".join("?" for _ in paper_ids)
        rows = store.query(
            "SELECT c.chunk_id, c.text, c.page, c.vec, c.paper_id, p.title"
            " FROM chunks c JOIN papers p ON p.paper_id = c.paper_id"
            " WHERE EXISTS(SELECT 1 FROM paper_domain_links l WHERE l.paper_id=p.paper_id"
            "   AND l.domain_id=? AND l.deleted_at IS NULL) AND p.deleted_at IS NULL"
            f" AND c.paper_id IN ({marks})",
            (domain_id, *paper_ids))
    else:
        rows = store.query(
            "SELECT c.chunk_id, c.text, c.page, c.vec, c.paper_id, p.title"
            " FROM chunks c JOIN papers p ON p.paper_id = c.paper_id"
            " WHERE EXISTS(SELECT 1 FROM paper_domain_links l WHERE l.paper_id=p.paper_id"
            "   AND l.domain_id=? AND l.deleted_at IS NULL) AND p.deleted_at IS NULL",
            (domain_id,))
    if not rows:
        return [], 0.0
    return _fuse(rows, query, k)


def retrieve_domains(domain_ids: list[int], query: str, k: int = 8,
                     paper_ids: list[int] | None = None) -> tuple[list[dict], float]:
    """Cross-domain retrieval: union of the given domains' papers."""
    if not domain_ids:
        return [], 0.0
    marks = ",".join("?" for _ in domain_ids)
    sql = ("SELECT c.chunk_id, c.text, c.page, c.vec, c.paper_id, p.title"
           " FROM chunks c JOIN papers p ON p.paper_id = c.paper_id"
           f" WHERE EXISTS(SELECT 1 FROM paper_domain_links l WHERE l.paper_id=p.paper_id"
           f"   AND l.domain_id IN ({marks}) AND l.deleted_at IS NULL)"
           " AND p.deleted_at IS NULL")
    params: list = list(domain_ids)
    if paper_ids:
        pm = ",".join("?" for _ in paper_ids)
        sql += f" AND c.paper_id IN ({pm})"
        params += paper_ids
    rows = store.query(sql, tuple(params))
    if not rows:
        return [], 0.0
    return _fuse(rows, query, k)


def retrieve_domains_scoped(domain_ids: list[int], query: str, k: int = 8,
                            paper_ids: list[int] | None = None,
                            timeout_s: float = 8.0,
                            weights: dict | None = None,
                            primary_domain_id: int | None = None) -> tuple[list[dict], float, list[dict]]:
    """ + : per-domain retrieval under a deadline.

 Returns (results, top_sim, incomplete) where `incomplete` lists domains that
 timed out or raised — the answer must say their evidence is incomplete.
 `weights` (domain_id → 0.1..2.0) multiplies the fused score so 主攻领域 wins.

`primary_domain_id` gets the full depth (k); the other domains
 are breadth (k/2, at least 3). The caller determines it from the question
 vector before retrieval, so "先判断主要领域归属，再定深度" is real rather
 than a weight-only sort.
    """
    ids = [d for d in dict.fromkeys(domain_ids or []) if d]
    if not ids:
        return [], 0.0, []
    weights = weights or {}
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    names = {r["domain_id"]: r["name"] for r in
             store.query("SELECT domain_id, name FROM domains")}
    merged: dict[int, dict] = {}
    top_sim = 0.0
    incomplete: list[dict] = []
    for did in ids:
        if time.monotonic() > deadline:
            incomplete.append({"domain_id": did, "name": names.get(did, ""), "reason": "超时"})
            continue
        depth = k if (primary_domain_id is None or did == primary_domain_id) \
            else max(3, k // 2)
        try:
            rows, sim = retrieve_domain(did, query, k=max(depth * 2, 12),
                                        paper_ids=paper_ids)
        except Exception as e:
            incomplete.append({"domain_id": did, "name": names.get(did, ""),
                               "reason": f"检索失败：{str(e)[:60]}"})
            continue
        top_sim = max(top_sim, sim)
        w = float(weights.get(did, 1.0) or 1.0)
        for r in rows:
            score = float(r["score"]) * (0.5 + 0.5 * w)
            prev = merged.get(r["chunk_id"])
            if prev is None or score > prev["score"]:
                merged[r["chunk_id"]] = {
                    **r, "score": round(score, 5), "domain_id": did,
                    "depth": "primary" if did == primary_domain_id else "breadth"}
    results = sorted(merged.values(), key=lambda r: -r["score"])[:k]
    return results, top_sim, incomplete


def _fuse(rows: list, query: str, k: int) -> tuple[list[dict], float]:
    """RRF fusion over pre-fetched rows (shared by paper/domain retrieval)."""
    fts_rank: dict[int, int] = {}
    try:
        fts_rows = store.query(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?"
            " ORDER BY bm25(chunks_fts) LIMIT 20",
            (query.replace('"', '""'),))
        fts_rank = {r["rowid"]: i for i, r in enumerate(fts_rows)}
    except Exception:
        fts_rank = {}

    from . import assign
    qvec = assign.embed([query])[0]
    sims = []
    for r in rows:
        if r["vec"]:
            v = np.frombuffer(r["vec"], dtype=np.float32)
            sims.append((float(np.dot(qvec, v)), r["chunk_id"]))
    sims.sort(reverse=True)
    top_sim = sims[0][0] if sims else 0.0
    vec_rank = {cid: i for i, (_, cid) in enumerate(sims[:20])}

    by_id = {r["chunk_id"]: r for r in rows}
    rrf: dict[int, float] = {}
    for rank_map in (fts_rank, vec_rank):
        for cid, rank in rank_map.items():
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    fused = [(cid, s) for cid, s in sorted(rrf.items(), key=lambda x: -x[1])
             if cid in by_id][:k]
    results = [{"chunk_id": cid, "text": by_id[cid]["text"], "page": by_id[cid]["page"],
                "score": round(s, 5),
                **({"paper_id": by_id[cid]["paper_id"], "title": by_id[cid]["title"]}
                   if "paper_id" in by_id[cid].keys() else {})}
               for cid, s in fused]
    return results, top_sim
