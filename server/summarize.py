"""Five-section summary engine with verbatim-evidence validation."""
import json

from . import llm, store, validators as v

SECTIONS = ["研究问题", "背景动机", "方法路径", "核心成果", "适用边界与局限"]
MAX_CHARS = 24000

_GOAL_HINTS = {
    "找 idea": "优先突出局限、未解决问题、未来工作，以及方法设计里可迁移到其他任务的部分。",
    "写 related work": "优先突出与同领域工作的差异、方法对比和引用关系。",
    "快速了解": "优先用最短篇幅交代问题、方法与结论，少展开细节。",
    "审稿": "优先突出实验设置、对比基线、贡献新颖性与作者自述的局限。",
    "教学备课": "优先突出可讲解的概念、方法的直观解释与前置知识。",
}

_FIVE_INSTRUCTION = """你是论文精读助手。基于给定的论文全文，输出一个 JSON 数组，包含 5 个对象，
section 字段必须依次为：["研究问题", "背景动机", "方法路径", "核心成果", "适用边界与局限"]。
每个对象字段：
- "text": 该段的中文总结，2-4 句，术语保留英文
- "evidence": 支撑该段结论的一句英文原文，必须逐字摘自论文全文（禁止改写、拼接或翻译）
只输出 JSON 数组，不要输出其他任何内容。"""


def _research_goal(paper: dict) -> str:
    did = paper.get("domain_id")
    if not did:
        return ""
    rows = store.query("SELECT research_goal FROM domains WHERE domain_id=?", (did,))
    return ((rows[0]["research_goal"] if rows else "") or "").strip()


def _goal_note(goal: str) -> str:
    if not goal:
        return ""
    hint = _GOAL_HINTS.get(goal, "摘要必须优先突出与该目标最相关的内容。")
    return f"研究目标：{goal}。{hint}\n\n"


def _validate_evidence(evidence: str, full_text: str, chunks: list[dict]) -> tuple[str | None, int | None]:
    """evidence 必须在全文中精确命中（canon 兜底），否则丢弃。返回 (quote, page)。"""
    q = v.validate_verbatim_quote(evidence, [full_text], min_chars=30, min_words=5)
    if not q:
        return None, None
    for c in chunks:
        if q in v.norm_text(c["text"]) or (v.canon(q) and v.canon(q) in v.canon(c["text"])):
            return q, c["page"]
    return q, None


def _full_text(paper_id: int) -> str:
    rows = store.query(
        "SELECT text, page FROM chunks WHERE paper_id=? ORDER BY chunk_id", (paper_id,))
    return "\n\n".join(f"{r['text']}" for r in rows)[:MAX_CHARS], rows


def generate(paper_id: int, granularity: str = "five", progress_cb=None, cancel_check=None,
             research_goal: str | None = None) -> dict:
    """Generate summaries; `research_goal` selects the reading domain's goal."""
    paper = store.get_paper(paper_id)
    if not paper or paper["parse_status"] != "done":
        raise ValueError("paper not parsed")
    full_text, chunks = _full_text(paper_id)
    if not full_text.strip():
        raise ValueError("paper has no text")

    meta = json.loads(paper["meta_json"] or "{}")
    summaries = meta.get("summaries", {})
    goal = (research_goal if research_goal is not None else _research_goal(paper)) or ""
    goal = goal.strip()
    goal_note = _goal_note(goal)

    if granularity == "five":
        sections = []
        dropped = 0
        total = len(SECTIONS)
        for idx, section in enumerate(SECTIONS):
            if cancel_check and cancel_check():
                raise ValueError("__canceled__")
            if progress_cb:
                progress_cb(idx, total, section)
            messages = [
                {"role": "system", "content": store.get_config("prompt_summary")},
                {"role": "user",
                 "content": (f"{goal_note}论文全文：\n{full_text}\n\n"
                             f"本次只生成第 {idx + 1}/{total} 段（section=\"{section}\"），"
                             "输出只含这一个对象的 JSON 数组。")},
            ]
            r = llm.chat(task="summary-five", messages=messages, model=llm.fast_model(),
                         temperature=0.2, max_tokens=768,
                         cache_extra=f"{section}|{goal}")
            arr = v.parse_json_blob(r["content"], "array") or []
            item = next((x for x in arr if isinstance(x, dict)), None)
            if not item:
                raise ValueError(f"第 {idx + 1} 段（{section}）生成失败，请重试")
            quote, page = _validate_evidence(item.get("evidence", ""), full_text, chunks)
            if item.get("evidence") and not quote:
                dropped += 1
            sections.append({"section": section, "text": item.get("text", ""),
                             "evidence": quote, "page": page})
        if progress_cb:
            progress_cb(total, total, "完成")
        summaries["five"] = sections
        summaries["five_dropped_evidence"] = dropped
        summaries["five_meta"] = {"model_served": r["model_served"], "latency_ms": r["latency_ms"],
                                  "research_goal": goal}
    else:
        label = "一段话" if granularity == "para" else "一句话"
        messages = [
            {"role": "system",
             "content": f"基于论文全文输出{label}中文总结，术语保留英文，只输出总结本身。"},
            {"role": "user", "content": f"{goal_note}论文全文：\n{full_text}"},
        ]
        r = llm.chat(task=f"summary-{granularity}", messages=messages, model=llm.fast_model(),
                     temperature=0.2, max_tokens=1024, cache_extra=f"{label}|{goal}")
        summaries[granularity] = v.norm_text(r["content"])

    store.merge_paper_meta(paper_id, {"summaries": summaries, "summary_error": ""})
    try:
        store.save_summary(paper_id, summaries, granularity=granularity,
                           research_goal=goal, model=r.get("model_served", ""),
                           parse_version=int(paper.get("parse_version") or 0))
    except Exception as e:
        print(f"[summary] cache write failed for paper {paper_id}: {e}")
    return summaries
