"""Literature-survey draft generator."""
from __future__ import annotations

import json

from . import store

GROUP_MODES = ("topic", "year", "method")
GROUP_LABELS = {"topic": "按主题分组", "year": "按时间线", "method": "按方法对比"}

_SEC_LABELS = {
    "研究问题": "research_question",
    "背景动机": "motivation",
    "方法路径": "method",
    "核心成果": "results",
    "适用边界与局限": "limitations",
}


def _clean(text: str | None, limit: int = 0) -> str:
    """Collapse whitespace (Markdown tables cannot contain raw newlines)."""
    s = " ".join((text or "").replace("\r", " ").split())
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _sections_of(paper_id: int, domain_id: int | None = None) -> dict:
    """Five-section summary for one paper, keyed by English field name.

 `domain_id` is the domain the paper was selected from; its research goal
 selects the summary cache entry.
    """
    p = store.get_paper(paper_id)
    if not p:
        return {}
    view_domain = domain_id if domain_id is not None else p.get("domain_id")
    goal = ""
    if view_domain:
        d = store.get_domain(view_domain)
        goal = (d or {}).get("research_goal", "") or ""
    row = store.get_summary(paper_id, "five", goal, int(p.get("parse_version") or 0))
    content = (row or {}).get("content") or {}
    if not content:
        try:
            content = (json.loads(p.get("meta_json") or "{}").get("summaries") or {})
        except (TypeError, ValueError):
            content = {}
    out = {}
    for sec in (content.get("five") or []) if isinstance(content, dict) else []:
        if not isinstance(sec, dict):
            continue
        key = _SEC_LABELS.get(str(sec.get("section") or "").strip())
        if key and sec.get("text"):
            out[key] = _clean(sec.get("text"))
    return out


def _brief(paper: dict, domain_names: dict[int, str],
           domain_id: int | None = None) -> dict:
    """One paper's survey material, attributed to the domain it was picked from."""
    picked_domain = domain_id if domain_id is not None else paper.get("domain_id")
    sections = _sections_of(paper["paper_id"], picked_domain)
    raw = {}
    try:
        raw = json.loads(paper.get("meta_json") or "{}")
    except (TypeError, ValueError):
        raw = {}
    return {
        "paper_id": paper["paper_id"],
        "title": _clean(paper.get("title") or "(untitled)"),
        "year": paper.get("year"),
        "venue": _clean(paper.get("venue") or ""),
        "arxiv_id": paper.get("arxiv_id") or "",
        "domain_id": picked_domain,
        "domain": domain_names.get(picked_domain or -1, ""),
        "primary_category": _clean((raw.get("primary_category") or "")),
        "sections": sections,
        "cite": f"[P{paper['paper_id']}]",
    }


def _subtopics_map(paper_ids: list[int]) -> dict[int, list[str]]:
    if not paper_ids:
        return {}
    marks = ",".join("?" for _ in paper_ids)
    rows = store.query(
        "SELECT ps.paper_id, s.name FROM paper_subtopics ps"
        " JOIN subtopics s ON s.subtopic_id = ps.subtopic_id"
        f" WHERE ps.paper_id IN ({marks}) ORDER BY s.name", tuple(paper_ids))
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["paper_id"], []).append(r["name"])
    return out


def _group(papers: list[dict], group_by: str, subs: dict[int, list[str]]) -> list[dict]:
    """Return [{key, papers}] in display order."""
    if group_by == "year":
        buckets: dict = {}
        for p in papers:
            buckets.setdefault(p.get("year") or 0, []).append(p)
        return [{"key": (str(y) if y else "未标注年份"),
                 "papers": sorted(buckets[y], key=lambda x: x["paper_id"])}
                for y in sorted(buckets, reverse=True)]
    if group_by == "method":
        # 方法对比是一张跨论文对照表，单列一节
        return [{"key": "方法对比", "papers": sorted(papers, key=lambda x: (-(x.get("year") or 0),
                                                                           x["paper_id"]))}]
    # topic：子主题优先，跨领域退回领域名，再退回"未归类"
    buckets: dict[str, list[dict]] = {}
    for p in papers:
        names = subs.get(p["paper_id"]) or []
        key = "、".join(names) if names else (p.get("domain") or "未归类主题")
        buckets.setdefault(key, []).append(p)
    return [{"key": k, "papers": sorted(buckets[k], key=lambda x: (-(x.get("year") or 0),
                                                                   x["paper_id"]))}
            for k in sorted(buckets)]


def _table(papers: list[dict]) -> list[str]:
    lines = ["| 论文 | 年份 | 研究问题 | 方法 | 核心成果 | 局限 |",
             "|---|---|---|---|---|---|"]
    for p in papers:
        s = p["sections"]
        lines.append("| {title} {cite} | {year} | {rq} | {m} | {r} | {lim} |".format(
            title=p["title"], cite=p["cite"], year=p.get("year") or "—",
            rq=_clean(s.get("research_question"), 90) or "—",
            m=_clean(s.get("method"), 90) or "—",
            r=_clean(s.get("results"), 90) or "—",
            lim=_clean(s.get("limitations"), 90) or "—"))
    return lines


def _paper_block(p: dict) -> list[str]:
    s = p["sections"]
    head = f"### {p['title']} {p['cite']}"
    meta = " · ".join(x for x in [
        str(p.get("year") or ""),
        p.get("venue") or "",
        (f"arXiv:{p['arxiv_id']}" if p.get("arxiv_id") else ""),
        p.get("domain") or "",
    ] if x)
    lines = [head, ""]
    if meta:
        lines += [f"*{meta}*", ""]
    for label, key in (("研究问题", "research_question"), ("方法路径", "method"),
                       ("核心成果", "results"), ("适用边界与局限", "limitations")):
        if s.get(key):
            lines.append(f"- **{label}**：{s[key]}")
    if not s:
        lines.append(f"- 该论文还没有五段摘要 {p['cite']}，本轮只能列入清单，"
                     "建议先生成摘要再重跑综述。")
    lines.append("")
    return lines


def _gaps(papers: list[dict], domain_ids: list[int]) -> list[dict]:
    """研究空白：只从用户数据归纳，每条带来源。"""
    gaps: list[dict] = []
    seen: set[str] = set()
    for p in papers:
        lim = p["sections"].get("limitations")
        if lim:
            key = f"lim:{p['paper_id']}"
            if key not in seen:
                seen.add(key)
                gaps.append({"kind": "局限", "text": lim, "cite": p["cite"],
                             "paper_id": p["paper_id"]})
    for did in domain_ids:
        for q in store.list_questions(domain_id=did, status="open"):
            text = _clean(q.get("text"))
            if not text or text in seen:
                continue
            seen.add(text)
            cite = f"[P{q['paper_id']}]" if q.get("paper_id") else f"[D{did}]"
            gaps.append({"kind": "未决问题", "text": text, "cite": cite,
                         "paper_id": q.get("paper_id")})
        for r in store.list_reviews(domain_id=did, status="pending"):
            label = _clean(r.get("target_title") or r.get("target_doi") or "未命名")
            text = f"参考文献「{label}」尚未在库内解析"
            if text in seen:
                continue
            seen.add(text)
            cite = f"[P{r['paper_id']}]" if r.get("paper_id") else f"[D{did}]"
            gaps.append({"kind": "待复核引用", "text": text, "cite": cite,
                         "paper_id": r.get("paper_id")})
    return gaps


def _synthesize_markdown(title: str, papers: list[dict], gaps: list[dict],
                         domain_names: list[str]) -> str | None:
    """Optional model-written overview; returns None when no key is configured."""
    from . import llm
    if not llm.configured() or not papers:
        return None
    material = "\n".join(
        f"{p['cite']} {p['title']}（{p.get('year') or 'n.d.'}）："
        f"{_clean(p['sections'].get('method') or p['sections'].get('research_question'), 160)}"
        for p in papers)
    gaps_txt = "\n".join(f"- {g['text']} {g['cite']}" for g in gaps[:12])
    try:
        r = llm.chat(task="survey", messages=[
            {"role": "system", "content": (
                "# 角色\n文献综述助手。只依据给定材料写一段 120-200 字的中文导言。\n"
                "# 规则\n1. 只陈述材料里的内容，禁止引入外部知识；"
                "2. 提到某篇论文时必须在句末标出它的编号（如 [P3]）；"
                "3. 不写标题、不写列表，只写一段话。")},
            {"role": "user", "content": f"综述主题：{title}\n领域：{'、'.join(domain_names)}\n"
                                        f"论文材料：\n{material}\n\n已知研究空白：\n{gaps_txt}"},
        ], model=llm.fast_model(), temperature=0.2, max_tokens=600)
    except Exception:
        return None
    text = _clean(r.get("content"))
    return text or None


def build(domain_ids: list[int], paper_ids: list[int] | None = None,
          group_by: str = "topic", include_gaps: bool = True,
          include_notes: bool = False, synthesize: bool = False,
          title: str = "") -> dict:
    """Generate a Markdown survey draft.

 `domain_ids` may contain several domains ( "可跨领域"); `paper_ids`
 narrows the selection. Nothing is written to the database here — the API
 layer decides whether to persist the draft.
    """
    domain_ids = [int(d) for d in dict.fromkeys(domain_ids or [])]
    if not domain_ids:
        raise ValueError("至少选择一个领域")
    domains = [d for d in (store.get_domain(i) for i in domain_ids) if d]
    if not domains:
        raise ValueError("领域不存在")
    domain_names = {d["domain_id"]: d["name"] for d in store.list_domains()}

    picked: list[dict] = []
    seen: set[int] = set()
    for did in [d["domain_id"] for d in domains]:
        for p in store.list_papers(did):
            pid = p["paper_id"]
            if pid in seen:
                continue
            if paper_ids and pid not in paper_ids:
                continue
            seen.add(pid)
            picked.append(_brief(p, domain_names, did))
    if not picked:
        raise ValueError("所选范围内没有论文")
    picked.sort(key=lambda x: (-(x.get("year") or 0), x["paper_id"]))

    subs = _subtopics_map([p["paper_id"] for p in picked])
    group_by = group_by if group_by in GROUP_MODES else "topic"
    groups = _group(picked, group_by, subs)
    gaps = _gaps(picked, [d["domain_id"] for d in domains]) if include_gaps else []
    notes_map: dict[int, dict] = {}
    if include_notes:
        for p in picked:
            rows = store.query(
                "SELECT COUNT(*) c FROM interactions WHERE paper_id=? AND type='note'"
                " AND deleted_at IS NULL", (p["paper_id"],))
            notes_map[p["paper_id"]] = {"notes": rows[0]["c"] if rows else 0}

    head_title = title.strip() or (
        domains[0]["name"] if len(domains) == 1 else " + ".join(d["name"] for d in domains))
    lines = [f"# {head_title} · 文献综述草稿", "",
             f"- 结构：{GROUP_LABELS[group_by]}",
             f"- 范围：{'、'.join(d['name'] for d in domains)}（{len(picked)} 篇）"]
    goals = [d.get("research_goal") for d in domains if d.get("research_goal")]
    if goals:
        lines.append(f"- 研究目标：{'；'.join(goals)}")
    lines.append("")

    intro = _synthesize_markdown(head_title, picked, gaps,
                                 [d["name"] for d in domains]) if synthesize else None
    if intro:
        lines += ["## 导言", "", intro, ""]

    for g in groups:
        lines += [f"## {g['key']}", ""]
        if group_by == "method" or len(g["papers"]) > 1:
            lines += _table(g["papers"]) + [""]
        for p in g["papers"]:
            lines += _paper_block(p)
            if include_notes and notes_map.get(p["paper_id"], {}).get("notes"):
                lines.append(f"- **笔记**：{notes_map[p['paper_id']]['notes']} 条 {p['cite']}")
                lines.append("")

    if include_gaps:
        lines += ["## 研究空白", "",
                  "以下条目来自你自己的数据（摘要局限、未决问题、待复核引用），"
                  "每条都给出出处，可作为下一步检索或实验的候选方向。", ""]
        if gaps:
            for g in gaps[:40]:
                lines.append(f"- **{g['kind']}**：{g['text']} {g['cite']}")
        else:
            lines.append("- 当前数据里没有可归纳的空白（没有摘要局限、未决问题或待复核引用）。")
        lines.append("")

    lines += ["## 参考", ""]
    for p in picked:
        bits = [p["title"]]
        if p.get("year"):
            bits.append(str(p["year"]))
        if p.get("venue"):
            bits.append(p["venue"])
        if p.get("arxiv_id"):
            bits.append(f"arXiv:{p['arxiv_id']}")
        lines.append(f"- {p['cite']} {' · '.join(bits)}")
    lines.append("")

    stats = {"papers": len(picked), "groups": len(groups), "gaps": len(gaps),
             "group_by": group_by, "domains": [d["domain_id"] for d in domains],
             "synthesized": bool(intro), "include_notes": bool(include_notes)}
    return {"markdown": "\n".join(lines), "stats": stats, "gaps": gaps,
            "title": head_title}
