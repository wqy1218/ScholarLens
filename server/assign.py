"""Embedding + domain assignment (centroid cosine, thresholds 0.5/0.35)."""
import base64
import json
import threading

import numpy as np

from . import store

MODEL_NAME = "BAAI/bge-small-en-v1.5"


def _thresholds() -> tuple[float, float]:
    return (float(store.get_config("assign_threshold")), float(store.get_config("link_threshold")))

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME, device="cpu")
        return _model


def embed(texts: list[str]) -> np.ndarray:
    return _get_model().encode(texts, normalize_embeddings=True, show_progress_bar=False)


def embed_chunks(paper_id: int) -> int:
    """Encode chunk texts into chunks.vec (retrieval needs per-chunk vectors)."""
    rows = store.query("SELECT chunk_id, text FROM chunks WHERE paper_id=? AND vec IS NULL", (paper_id,))
    if not rows:
        return 0
    vecs = embed([r["text"] for r in rows])
    store.update_chunk_vecs([
        (np.asarray(v, dtype=np.float32).tobytes(), r["chunk_id"]) for v, r in zip(vecs, rows)])
    return len(rows)


def embed_paper(meta: dict) -> np.ndarray:
    """Embed from title + abstract (assignment uses metadata only, not full text)."""
    return embed([f'{meta.get("title", "")}. {meta.get("abstract", "")[:1000]}'])[0]



def rebuild_centroid(domain_id: int) -> np.ndarray | None:
    rows = store.query(
        "SELECT p.meta_json FROM papers p JOIN paper_domain_links l"
        " ON l.paper_id=p.paper_id WHERE l.domain_id=? AND l.is_primary=1"
        " AND l.deleted_at IS NULL AND p.parse_status!='failed' AND p.deleted_at IS NULL",
        (domain_id,))
    vecs = []
    for r in rows:
        meta = json.loads(r["meta_json"] or "{}")
        if meta.get("_vec64"):
            vecs.append(np.frombuffer(base64.b64decode(meta["_vec64"]), dtype=np.float32))
    if not vecs:
        store.set_centroid(domain_id, None)
        return None
    centroid = np.mean(vecs, axis=0)
    centroid /= (np.linalg.norm(centroid) + 1e-9)
    store.set_centroid(domain_id, centroid.astype(np.float32).tobytes())
    return centroid


def domain_centroid(domain_id: int) -> np.ndarray | None:
    d = store.get_domain(domain_id)
    if d and d["centroid"]:
        return np.frombuffer(d["centroid"], dtype=np.float32)
    return rebuild_centroid(domain_id)



def _heuristic_domain_name(title: str, abstract: str) -> str | None:
    """Interim naming for domains created from local-only papers (no arXiv category).
 Top-2 frequent non-stopword terms from title+abstract; replaced by LLM naming in ."""
    import re
    from collections import Counter
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
        "have", "has", "our", "can", "not", "using", "based", "which", "into", "such",
        "also", "between", "more", "than", "these", "those", "their", "over", "both",
        "across", "when", "while", "through", "each", "other", "new", "two", "three",
        "first", "second", "propose", "paper", "show", "present", "study", "we", "its",
    }
    words = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", f"{title} {abstract}")
    freq = Counter(w.lower() for w in words if w.lower() not in stop)
    top = [w for w, _ in freq.most_common(8)][:2]
    if not top:
        return None
    return "-".join(w.capitalize() for w in top)[:40]


def domain_dispersion(domain_id: int) -> float:
    """领域离散度 = 成员论文与质心的 1-余弦 均值。
 主题集中的领域 ≈0.05-0.1；跨类别宽领域明显更高。无成员/无质心 → 0。"""
    c = domain_centroid(domain_id)
    if c is None:
        return 0.0
    rows = store.query(
        "SELECT p.meta_json FROM papers p JOIN paper_domain_links l"
        " ON l.paper_id=p.paper_id WHERE l.domain_id=? AND l.is_primary=1"
        " AND l.deleted_at IS NULL AND p.parse_status!='failed' AND p.deleted_at IS NULL",
        (domain_id,))
    ds = []
    for r in rows:
        m = json.loads(r["meta_json"] or "{}")
        if m.get("_vec64"):
            v = np.frombuffer(base64.b64decode(m["_vec64"]), dtype=np.float32)
            ds.append(1.0 - float(np.dot(v, c)))
    return sum(ds) / len(ds) if ds else 0.0


def embed_only(paper_id: int) -> None:
    """persist the paper vector without touching domain assignment.

 Manual-domain imports skip assign_paper(), which was the only writer of
 meta["_vec64"]. Those papers then had no attribution scores, no weak links,
 no graph edge and were skipped by auto-threshold/reassign forever.
    """
    paper = store.get_paper(paper_id)
    if not paper:
        return
    vec = embed_paper({"title": paper["title"], "abstract": paper["abstract"]})
    store.merge_paper_meta(paper_id, {
        "_vec64": base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode(),
    })


def assign_paper(paper_id: int) -> int:
    """Assign one paper to best domain; auto-create a domain if none reaches threshold.
比较用离散度扣罚分 adj = cos − λ·dispersion（λ 可配置）——跨类别宽领域
 的质心"万金油"效应被抑制；连边/展示仍用原始余弦。Returns the domain_id."""
    paper = store.get_paper(paper_id)
    if not paper:
        raise ValueError(f"paper {paper_id} not found")
    vec = embed_paper({"title": paper["title"], "abstract": paper["abstract"]})
    store.merge_paper_meta(paper_id, {
        "_vec64": base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode(),
    })

    domains = store.list_domains()
    assign_th, link_th = _thresholds()
    try:
        lam = float(store.get_config("domain_dispersion_weight") or 0.3)
    except ValueError:
        lam = 0.3
    best_id, best_adj = None, -1e9
    scores = []
    for d in domains:
        c = domain_centroid(d["domain_id"])
        if c is None:
            continue
        raw = float(np.dot(vec, c))
        adj = raw - lam * domain_dispersion(d["domain_id"])
        scores.append((d["domain_id"], raw))
        if adj > best_adj:
            best_id, best_adj = d["domain_id"], adj

    if best_id is None or best_adj < assign_th:
        primary = (paper.get("meta_json") and json.loads(paper["meta_json"]).get("primary_category")) or ""
        name = (primary.replace(".", "-")
                or _heuristic_domain_name(paper["title"] or "", paper["abstract"] or "")
                or f"Domain-{store.next_domain_slot()}")
        if any(d["name"] == name for d in domains):
            name = f"{name}-{store.next_domain_slot()}"
        new_id = store.create_domain(name)
        best_id = new_id
        scores.append((new_id, 1.0))

    store.set_paper_domain(paper_id, best_id, source="auto")
    for did, score in scores:
        if did != best_id and score >= link_th:
            store.add_paper_domain(paper_id, did, score=round(score, 3), source="auto",
                                   status="auto", confidence=round(score, 3))
    rebuild_centroid(best_id)
    return best_id


def reassign_all() -> list[dict]:
    """Re-run assignment for all papers except user-locked (meta_json.lock_domain)."""
    moved = []
    for p in store.list_papers():
        meta = json.loads(p["meta_json"] or "{}")
        if meta.get("lock_domain"):
            continue
        old = p["domain_id"]
        new = assign_paper(p["paper_id"])
        if old != new:
            moved.append({"paper_id": p["paper_id"], "title": p["title"], "from": old, "to": new})
    return moved



def discover_domains(k: int | None = None) -> dict:
    """Cluster the current library by paper vectors and propose domains.

 Read-only preview; applying is a separate API call. Deterministic k-means++
 over the vectors auto-assignment already stores — no LLM, no network.
    """
    import base64
    import math

    rows = store.list_papers()
    items = []
    for p in rows:
        meta = json.loads(p.get("meta_json") or "{}")
        b64 = meta.get("_vec64")
        if not b64:
            continue
        vec = np.frombuffer(base64.b64decode(b64), dtype=np.float32)
        if vec.size:
            items.append((p, vec))
    n = len(items)
    if n < 3:
        raise ValueError("论文太少（<3 篇带向量），不足以做领域发现")

    k = int(k or max(2, min(8, round(math.sqrt(n / 2)))))
    k = max(2, min(k, n))
    mat = np.stack([v for _, v in items]).astype(np.float32)
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)

    mean = mat.mean(axis=0)
    first = int(np.argmax(mat @ (mean / (np.linalg.norm(mean) + 1e-9))))
    centers = [mat[first]]
    while len(centers) < k:
        sims = np.max(np.stack([mat @ c for c in centers], axis=1), axis=1)
        centers.append(mat[int(np.argmin(sims))])
    centers = np.stack(centers)

    labels = np.full(n, -1, dtype=int)
    for _ in range(25):
        sims = mat @ centers.T
        new = np.argmax(sims, axis=1)
        if np.array_equal(new, labels):
            break
        labels = new
        for j in range(k):
            members = mat[labels == j]
            if len(members):
                c = members.mean(axis=0)
                centers[j] = c / (np.linalg.norm(c) + 1e-9)

    clusters = []
    for j in range(k):
        idx = np.where(labels == j)[0]
        if not len(idx):
            continue
        members = [items[i] for i in idx]
        conf = float(np.mean([float(mat[i] @ centers[j]) for i in idx]))
        cats = []
        for p, _ in members:
            m = json.loads(p.get("meta_json") or "{}")
            if m.get("primary_category"):
                cats.append(m["primary_category"])
        if cats:
            from collections import Counter
            name = Counter(cats).most_common(1)[0][0].replace(".", "-")
        else:
            name = _heuristic_domain_name(" ".join(p.get("title") or "" for p, _ in members),
                                          " ".join(p.get("abstract") or "" for p, _ in members))
        papers = []
        for i in idx:
            p = items[i][0]
            score = float(mat[i] @ centers[j])
            papers.append({
                "paper_id": p["paper_id"], "title": p.get("title") or "",
                "confidence": round(score, 3),
                "current_domain_id": p.get("domain_id"),
            })
        papers.sort(key=lambda x: -x["confidence"])
        clusters.append({
            "name": name or f"Discovery-{j + 1}",
            "paper_ids": [p["paper_id"] for p, _ in members],
            "titles": [p.get("title") or "" for p, _ in members],
            "papers": papers,
            "size": len(members),
            "confidence": round(conf, 3),
            "example": (members[0][0].get("title") or "")[:80],
        })
    clusters.sort(key=lambda c: -c["size"])
    return {"k": len(clusters), "n_papers": n,
            "assign_threshold": _thresholds()[0], "clusters": clusters}



DEFAULT_ASSIGN_TH, DEFAULT_LINK_TH = "0.5", "0.35"


def auto_threshold() -> dict:
    """Estimate thresholds from the CURRENT corpus: the same-domain centroid
 similarity distribution tells us where 'belongs' actually starts.
 assign = 10th percentile (a paper below it is probably misassigned);
 link = assign × 0.7. Clamped to sane ranges, fully explainable."""
    domains = {d["domain_id"]: domain_centroid(d["domain_id"]) for d in store.list_domains()}
    sims = []
    for p in store.list_papers():
        meta = json.loads(p["meta_json"] or "{}")
        if meta.get("lock_domain"):
            continue
        c = domains.get(p["domain_id"])
        b64 = meta.get("_vec64")
        if c is None or not b64:
            continue
        vec = np.frombuffer(base64.b64decode(b64), dtype=np.float32)
        sims.append(float(np.dot(vec, c)))
    if len(sims) < 5:
        raise ValueError("论文数量太少（<5 篇），不足以自动估计阈值，请使用默认值")
    sims.sort()

    def pct(q: float) -> float:
        return sims[min(len(sims) - 1, int(q * (len(sims) - 1)))]

    assign_th = round(min(0.80, max(0.30, pct(0.10))), 2)
    link_th = round(min(0.60, max(0.25, assign_th * 0.7)), 2)
    store.set_config("assign_threshold", str(assign_th))
    store.set_config("link_threshold", str(link_th))
    return {"assign_threshold": assign_th, "link_threshold": link_th,
            "n_papers": len(sims),
            "same_domain_sim_min": round(sims[0], 3),
            "same_domain_sim_median": round(pct(0.5), 3),
            "same_domain_sim_max": round(sims[-1], 3)}


def reset_threshold() -> dict:
    store.set_config("assign_threshold", DEFAULT_ASSIGN_TH)
    store.set_config("link_threshold", DEFAULT_LINK_TH)
    return {"assign_threshold": float(DEFAULT_ASSIGN_TH), "link_threshold": float(DEFAULT_LINK_TH)}


def move_paper(paper_id: int, domain_id: int) -> dict:
    """Interactive domain judgment: user overrides the assignment. Vectors are
 NOT recomputed (the paper didn't change); both affected centroids rebuild and
 the weak links for this paper are refreshed against the current thresholds."""
    paper = store.get_paper(paper_id)
    if not paper:
        raise ValueError("paper not found")
    if not store.query("SELECT domain_id FROM domains WHERE domain_id=?", (domain_id,)):
        raise ValueError("target domain not found")
    old = paper["domain_id"]
    meta = json.loads(paper["meta_json"] or "{}")
    store.set_paper_domain(paper_id, domain_id, source="user")
    store.merge_paper_meta(paper_id, {"lock_domain": True})
    for m in list(store.list_paper_domains(paper_id)):
        if m["domain_id"] != domain_id:
            store.remove_paper_domain(paper_id, m["domain_id"])
    vec = None
    b64 = meta.get("_vec64")
    if b64:
        vec = np.frombuffer(base64.b64decode(b64), dtype=np.float32)
    if vec is not None:
        _, link_th = _thresholds()
        for d in store.list_domains():
            if d["domain_id"] == domain_id:
                continue
            c = domain_centroid(d["domain_id"])
            if c is None:
                continue
            score = float(np.dot(vec, c))
            if score >= link_th:
                store.add_paper_domain(paper_id, d["domain_id"], score=round(score, 3),
                                       source="manual", status="confirmed",
                                       confidence=round(score, 3))
    rebuild_centroid(old)
    rebuild_centroid(domain_id)
    return {"paper_id": paper_id, "from": old, "to": domain_id, "locked": True}


def set_paper_domains(paper_id: int, primary: int, secondary: list[int]) -> dict:
    """手动归属编辑 — 用户直接指定主领域 + 兼属领域（不限数量、不看阈值）。
主领域与兼属都写在 paper_domain_links（is_primary 区分），不再只写兼属。
 lock_domain 置位（重跑归属不覆盖人工判断）；新旧领域质心重建。"""
    paper = store.get_paper(paper_id)
    if not paper:
        raise ValueError("paper not found")
    valid = {d["domain_id"] for d in store.list_domains()}
    if primary not in valid:
        raise ValueError("primary domain not found")
    secondary = [s for s in dict.fromkeys(secondary) if s != primary]
    if any(s not in valid for s in secondary):
        raise ValueError("secondary domain not found")
    old = paper["domain_id"]
    meta = json.loads(paper["meta_json"] or "{}")
    store.set_paper_domain(paper_id, primary, source="manual")
    store.merge_paper_meta(paper_id, {"lock_domain": True})
    keep = set(secondary) | {primary}
    for m in list(store.list_paper_domains(paper_id)):
        if m["domain_id"] not in keep:
            store.remove_paper_domain(paper_id, m["domain_id"])
    vec = None
    if meta.get("_vec64"):
        vec = np.frombuffer(base64.b64decode(meta["_vec64"]), dtype=np.float32)
    for did in secondary:
        c = domain_centroid(did)
        score = float(np.dot(vec, c)) if (vec is not None and c is not None) else 0.0
        store.add_paper_domain(paper_id, did, score=round(score, 3), source="manual",
                               status="confirmed", confidence=round(score, 3))
    rebuild_centroid(old)
    rebuild_centroid(primary)
    return {"paper_id": paper_id, "primary": primary,
            "secondary": secondary, "locked": True}



PAPER_HINTS = ("这篇", "本文", "该论文", "这篇文章", "作者", "this paper", "the paper")
ALL_HINTS = ("全部领域", "所有领域", "跨领域", "各个领域", "all domains", "across domains")


def primary_domain(question: str, domain_ids: list[int]) -> tuple[int | None, float]:
    """Best-matching domain for a question among the requested domains.

cross-domain QA must retrieve the main domain at full depth and the
 others at breadth. This is the deterministic "main domain" decision; it
 uses the same centroids as scope inference.
    """
    ids = [d for d in dict.fromkeys(domain_ids or []) if d]
    q = (question or "").strip()
    if not ids or not q:
        return None, 0.0
    try:
        qvec = embed([q])[0]
    except Exception:
        return None, 0.0
    best_id, best = None, -1.0
    for did in ids:
        c = domain_centroid(did)
        if c is None:
            continue
        score = float(np.dot(qvec, c))
        if score > best:
            best_id, best = did, score
    return best_id, round(max(0.0, best), 4) if best_id is not None else 0.0


def infer_scope(paper_id: int, domain_id: int | None, question: str) -> dict:
    """从问题推断问答范围。

先走确定性线索（指代本篇、明确跨领域），再走向量：问题向量与本文 chunk、
各领域质心比较。返回 {scope, domain_ids, label, confidence, reason}，
供界面在作答前展示可点击的范围标签。
    """
    q = (question or "").strip()
    low = q.lower()
    domains = store.list_domains()
    cur = next((d for d in domains if d["domain_id"] == domain_id), None)
    cur_name = cur["name"] if cur else ""

    def domain_label(ids: list[int]) -> str:
        names = {d["domain_id"]: d["name"] for d in domains}
        return " + ".join(names.get(i, f"#{i}") for i in ids)

    if len(domains) > 1 and any(h in low for h in ALL_HINTS):
        return {"scope": "all", "domain_ids": [d["domain_id"] for d in domains],
                "label": "全部领域（推断）", "confidence": 0.6,
                "reason": "问题提到全部领域/跨领域"}
    if any(h in low for h in PAPER_HINTS):
        return {"scope": "paper", "domain_ids": [], "label": "当前论文（推断）",
                "confidence": 0.7, "reason": "问题用「这篇/本文」指代当前论文"}
    if not q:
        return {"scope": "domain", "domain_ids": [domain_id] if domain_id else [],
                "label": "当前领域（推断）", "confidence": 0.2, "reason": "空问题"}

    try:
        qvec = embed([q])[0]
        paper_sim = 0.0
        for r in store.query("SELECT vec FROM chunks WHERE paper_id=? AND vec IS NOT NULL",
                             (paper_id,)):
            v = np.frombuffer(r["vec"], dtype=np.float32)
            paper_sim = max(paper_sim, float(np.dot(qvec, v)))
        sims = []
        for d in domains:
            c = domain_centroid(d["domain_id"])
            if c is not None:
                sims.append((d, float(np.dot(qvec, c))))
    except Exception:
        return {"scope": "domain", "domain_ids": [domain_id] if domain_id else [],
                "label": f"当前领域：{cur_name}（推断）" if cur_name else "当前领域（推断）",
                "confidence": 0.2, "reason": "无法计算相似度，回退到当前领域"}

    sims.sort(key=lambda x: -x[1])
    cur_sim = next((s for d, s in sims if d["domain_id"] == domain_id), 0.0)
    best_d, best_s = (sims[0] if sims else (None, 0.0))

    if (best_d is not None and best_d["domain_id"] != domain_id
            and best_s >= cur_sim + 0.04 and best_s >= 0.40):
        ids = [i for i in (domain_id, best_d["domain_id"]) if i]
        return {"scope": "domains", "domain_ids": ids,
                "label": f"跨领域：{domain_label(ids)}（推断）",
                "confidence": round(min(0.9, 0.55 + (best_s - cur_sim)), 2),
                "reason": f"问题与「{best_d['name']}」更接近（{best_s:.2f} vs {cur_sim:.2f}）"}

    if paper_sim >= max(cur_sim, 0.32):
        return {"scope": "paper", "domain_ids": [], "label": "当前论文（推断）",
                "confidence": round(min(0.9, 0.45 + paper_sim), 2),
                "reason": f"问题与本文段落最接近（{paper_sim:.2f}）"}

    return {"scope": "domain", "domain_ids": [domain_id] if domain_id else [],
            "label": f"当前领域：{cur_name}（推断）" if cur_name else "当前领域（推断）",
            "confidence": round(min(0.85, 0.35 + cur_sim), 2),
            "reason": f"问题更贴近领域主题（{cur_sim:.2f}）"}
