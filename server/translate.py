"""三种模式的划词翻译与分层解释。

按句落库：选区先切句，逐句查 translations(paper_id+sentence+mode)，
重叠部分复用已有译文，只为新句子发一次批量请求（编号句子 → JSON 数组），
结果经 validators 校验后入库。

提示词布局：系统提示词放跨模式稳定的术语表与规则，
模式标记与待译句子放在用户消息末尾。
"""
import json
import re

from . import llm, retrieval, store, validators as v

MODES = ("literal", "free", "academic")
MODE_LABEL = {"literal": "直译", "free": "意译", "academic": "学术优化"}
CONTEXT_CHARS = 800


def segment_sentences(text: str) -> list[str]:
    return v.split_sentences(text)


def _context(paper_id: int, quote: str) -> str:
    """quote + surrounding text (≤ CONTEXT_CHARS) from the chunk containing it."""
    chunks = store.query(
        "SELECT text FROM chunks WHERE paper_id=? ORDER BY chunk_id", (paper_id,))
    nq = v.norm_text(quote)
    for c in chunks:
        text = c["text"]
        if nq and nq in v.norm_text(text):
            pos = v.norm_text(text).find(nq)
            start = max(0, pos - CONTEXT_CHARS // 2)
            end = min(len(text), pos + len(quote) + CONTEXT_CHARS // 2)
            return text[start:end]
    return quote


def _domain_glossary(paper_id: int) -> str:
    """domain glossary. reads the concept tables (canonical) and falls
 back to domains.meta_json for databases that have not been regenerated yet.
 Injected at the TAIL of the user message so the system prompt prefix — and
 thus the platform prompt cache — stays stable."""
    p = store.get_paper(paper_id)
    if not p or not p["domain_id"]:
        return ""
    rows = store.query(
        "SELECT c.display_en, COALESCE(d.term_zh, c.display_zh) AS zh FROM concepts c"
        " JOIN concept_definitions d ON d.concept_id=c.concept_id"
        " WHERE d.domain_id=? ORDER BY c.concept_key LIMIT 15", (p["domain_id"],))
    if rows:
        pairs = "；".join(f"{r['display_en']}={r['zh']}" for r in rows if r["display_en"])
        return f"\n领域术语表（优先遵守）：{pairs}" if pairs else ""
    d = store.query("SELECT meta_json FROM domains WHERE domain_id=?", (p["domain_id"],))
    try:
        g = (json.loads(d[0]["meta_json"] or "{}").get("glossary") or [])
    except (IndexError, json.JSONDecodeError):
        return ""
    if not g:
        return ""
    pairs = "；".join(f"{item.get('en', '')}={item.get('zh', '')}" for item in g if item.get("en"))
    return f"\n领域术语表（优先遵守）：{pairs}"


def generate_glossary(domain_id: int, update: bool = False) -> dict:
    """/: distill a domain glossary (8-15 terms, 英=中).
 update=True: incremental — existing terms are shown to the model and merged
 with the NEWEST papers, so papers added later enrich the table in place."""
    d = store.query("SELECT meta_json FROM domains WHERE domain_id=?", (domain_id,))
    if not d:
        raise ValueError("domain not found")
    try:
        meta = json.loads(d[0]["meta_json"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    existing = meta.get("glossary") or []
    if existing and not update:
        return {"skipped": True, "n_terms": len(existing)}
    papers = store.query(
        "SELECT p.title, p.abstract FROM papers p JOIN paper_domain_links l"
        " ON l.paper_id=p.paper_id WHERE l.domain_id=? AND l.deleted_at IS NULL"
        " AND p.deleted_at IS NULL AND p.parse_status!='failed'"
        " ORDER BY p.last_opened_at DESC NULLS LAST, p.paper_id DESC LIMIT 8",
        (domain_id,))
    material = "\n\n".join(
        f"标题：{v.norm_text(r['title'])}\n摘要：{v.norm_text(r['abstract'])[:400]}"
        for r in papers)
    existing_note = ""
    if existing:
        pairs = "；".join(f"{t.get('en', '')}={t.get('zh', '')}" for t in existing)
        existing_note = (f"\n\n已有术语表（保留仍适用的条目，融入新论文带来的新术语，"
                         f"总数 8-15 条）：{pairs}")
    messages = [
        {"role": "system", "content": (
            "# 角色\n术语提炼器。从给定论文标题与摘要中提炼该研究领域的核心术语对照表。\n"
            "# 规则\n1. 8-15 个术语，按重要性排序；2. en 为英文原词，zh 为规范中文译名"
            "（无通行译名的保留英文）；3. 只收领域术语，不收通用词汇。\n"
            "# 输出格式\n只输出 JSON 数组：[{\"en\": str, \"zh\": str}]。\n"
            "# 禁止\n输出 JSON 以外的任何内容。")},
        {"role": "user", "content": material + existing_note},
    ]
    r = llm.chat(task="glossary", messages=messages, model=llm.fast_model(),
                 temperature=0.2, max_tokens=768, cache_extra="glossary")
    arr = v.parse_json_blob(r["content"], "array")
    terms = [{"en": v.norm_text(x.get("en")), "zh": v.norm_text(x.get("zh"))}
             for x in (arr or [])[:15] if isinstance(x, dict) and x.get("en")]
    if len(terms) < 5:
        raise ValueError("术语提炼结果过少，请重试")
    meta["glossary"] = terms
    meta["glossary_updated_at"] = store.now()
    store.set_domain_meta(domain_id, meta)
    for t in terms:
        store.upsert_concept(t.get("en", ""), t.get("zh", ""), "", domain_id)
    return {"skipped": False, "updated": bool(existing), "n_terms": len(terms), "terms": terms}


def translate(paper_id: int, quote: str, mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    quote = v.norm_text(quote)
    if not quote:
        raise ValueError("empty quote")
    if v.has_garbled_math(quote):
        raise ValueError("选区包含公式字符，文本提取不可靠——请用 📷 图片模式截取该公式提问")
    sents = segment_sentences(quote)

    parts: list[dict] = []
    missing: list[tuple[int, str]] = []
    for i, s in enumerate(sents, 1):
        hit = store.get_translation(paper_id, s, mode)
        if hit is not None:
            parts.append({"i": i, "sentence": s, "t": hit, "cached": True})
        else:
            missing.append((i, s))

    latency_ms = 0
    model_served = ""
    all_cached = not missing
    if missing:
        numbered = "\n".join(f"{i}. {s}" for i, s in missing)
        max_tokens = max(512, min(sum(len(s) for _, s in missing) * 3, 4096))
        messages = [
            {"role": "system", "content": store.get_config("prompt_translate")},
            {"role": "user",
             "content": (f"模式：{MODE_LABEL[mode]}\n上下文（仅消歧，勿翻译）：\n{quote}\n\n"
                         f"待翻译编号句子：\n{numbered}{_domain_glossary(paper_id)}")},
        ]
        r = llm.chat(task="translate", messages=messages, model=llm.fast_model(),
                     temperature=0.3, max_tokens=max_tokens, cache_extra=f"{mode}:seg")
        latency_ms = r["latency_ms"]
        model_served = r["model_served"]
        if r["finish_reason"] == "length" and r["content"]:
            r2 = llm.chat(task="translate", messages=messages + [
                {"role": "assistant", "content": r["content"]},
                {"role": "user", "content": "继续，从断点接着输出，不要重复。"}],
                model=llm.fast_model(), temperature=0.3, max_tokens=max_tokens,
                cache_extra=f"{mode}:seg:cont")
            r = {**r2, "content": r["content"] + r2["content"]}
            latency_ms += r2["latency_ms"]
            model_served = r2["model_served"]
        arr = v.parse_json_blob(r["content"], "array")
        tmap: dict[int, str] = {}
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict) and item.get("t"):
                    try:
                        tmap[int(item["i"])] = v.norm_text(item["t"])
                    except (TypeError, ValueError):
                        continue
        for i, s in missing:
            t = tmap.get(i)
            if not t:
                raise ValueError(f"译文缺少第 {i} 句（宁缺毋错，请重试）")
            store.put_translation(paper_id, s, mode, t)
            parts.append({"i": i, "sentence": s, "t": t, "cached": False})

    parts.sort(key=lambda p: p["i"])
    return {"parts": parts, "translation": " ".join(p["t"] for p in parts),
            "all_cached": all_cached, "n_cached": len(sents) - len(missing),
            "n_fresh": len(missing), "mode": mode,
            "model_served": model_served, "latency_ms": latency_ms}


def explain(paper_id: int, quote: str) -> dict:
    """分层解释：说了什么 → 涉及概念 → 为什么在这里。
注入本论文检索到的 top-3 chunk（按 paper_id 硬过滤）。"""
    quote = v.norm_text(quote)
    if not quote:
        raise ValueError("empty quote")
    if v.has_garbled_math(quote):
        raise ValueError("选区包含公式字符，文本提取不可靠——请用 📷 图片模式截取该公式提问")
    results, top_sim = retrieval.retrieve(paper_id, quote, k=3)
    related = "\n".join(f"[{i+1}] (score {c['score']:.4f}) {v.norm_text(c['text'])[:300]}"
                        for i, c in enumerate(results)) or "（无相关检索结果）"
    messages = [
        {"role": "system", "content": store.get_config("prompt_explain")},
        {"role": "user",
         "content": f"待解释文本：\n{quote}\n\n本文相关片段（辅助定位论证角色）：\n{related}"},
    ]
    r = llm.chat(task="explain", messages=messages, model=llm.fast_model(),
                 temperature=0.2, max_tokens=1536, cache_extra="explain")
    arr = v.parse_json_blob(r["content"], "object")
    if not isinstance(arr, dict):
        return {"plain": v.norm_text(r["content"]), "concepts": [], "why": "",
                "cached": r.get("cached", False), "model_served": r["model_served"],
                "latency_ms": r["latency_ms"], "parsed": False}
    return {"plain": v.norm_text(arr.get("plain", "")),
            "concepts": [{"term": v.norm_text(c.get("term", "")), "def": v.norm_text(c.get("def", ""))}
                         for c in (arr.get("concepts") or [])[:6]
                         if isinstance(c, dict) and c.get("term")],
            "why": v.norm_text(arr.get("why", "")),
            "cached": r.get("cached", False), "model_served": r["model_served"],
            "latency_ms": r["latency_ms"], "parsed": True,
            "top_sim": round(top_sim, 3)}
