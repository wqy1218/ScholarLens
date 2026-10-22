"""Automatic three-category highlighting."""
import json

from . import llm, store, validators as v

CATEGORIES = ("background", "approach", "limitation")
# categories that exist in stored rows but are no longer written
LEGACY_CATEGORIES = ("delete",)
BATCH_SENTS = 55
MIN_CHARS, MIN_WORDS = 60, 8
PER_CATEGORY_CAP = 6


def _is_prose_sentence(s: str) -> bool:
    """Reject non-prose fragments (author blocks, emails, URLs): a real sentence
 ends with terminal punctuation and contains no address markers."""
    if "@" in s or "http" in s or "www." in s:
        return False
    return s[-1] in '.!?"\u201d\u300d\uff09)'


def _numbered_sentences(chunks: list[dict]) -> list[dict]:
    """[(n, page, text)] — global numbering across the paper, canon-deduped."""
    out: list[dict] = []
    seen: set[str] = set()
    n = 0
    for c in chunks:
        for raw in v.split_sentences(c["text"]):
            s = v.norm_text(raw)
            if len(s) < MIN_CHARS or len(s.split()) < MIN_WORDS:
                continue
            if not _is_prose_sentence(s):
                continue
            key = v.canon(s)
            if key in seen or len(key) < 10:
                continue
            seen.add(key)
            n += 1
            out.append({"n": n, "page": c["page"], "text": s})
    return out


def auto_highlight(paper_id: int, rerun: bool = False,
                   domain_id: int | None = None) -> dict:
    """Runs once per (paper, domain); a rerun only replaces this domain's set."""
    paper = store.get_paper(paper_id)
    if not paper or paper["parse_status"] != "done":
        return {"skipped": "not parsed"}
    view_domain = domain_id if domain_id is not None else paper["domain_id"]
    if not rerun and store.query(
            "SELECT COUNT(*) c FROM interactions WHERE paper_id=? AND type='highlight'"
            " AND domain_id IS ?"
            " AND anchor_json LIKE '%\"source\": \"auto\"%'",
            (paper_id, view_domain))[0]["c"]:
        return {"skipped": True}

    chunks = [dict(r) for r in store.query(
        "SELECT chunk_id, text, page FROM chunks WHERE paper_id=? ORDER BY chunk_id", (paper_id,))]
    sents = _numbered_sentences(chunks)
    if not sents:
        return {"added": 0, "dropped": 0}
    prompt_tmpl = store.get_config("prompt_highlight")

    added = dropped = failed = 0
    pending: list[tuple[str, str]] = []
    for start in range(0, len(sents), BATCH_SENTS):
        batch = sents[start:start + BATCH_SENTS]
        material = "\n".join(f"{s['n']}. (p.{s['page']}) {s['text']}" for s in batch)
        messages = [
            {"role": "system", "content": prompt_tmpl},
            {"role": "user", "content": f"编号句子：\n{material}"},
        ]
        try:
            r = llm.chat(task="auto-highlight", messages=messages, model=llm.fast_model(),
                         temperature=0.2, max_tokens=1024)
        except Exception as e:
            print(f"[auto-hl] paper {paper_id} batch failed: {e}")
            failed += 1
            continue
        items = v.parse_json_blob(r["content"], "array")
        if not isinstance(items, list):
            failed += 1
            continue
        by_n = {s["n"]: s for s in batch}
        cat_counts: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                dropped += 1
                continue
            cat = item.get("category")
            s = by_n.get(n)
            if not s or cat not in CATEGORIES:
                dropped += 1
                continue
            if cat_counts.get(cat, 0) >= PER_CATEGORY_CAP:
                continue
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            anchor = json.dumps({"page": s["page"], "quote": s["text"], "source": "auto"},
                                ensure_ascii=False)
            pending.append((anchor, cat))
    if failed and not pending:
        return {"added": 0, "dropped": dropped, "failed_batches": failed,
                "error": "auto-highlight failed for every batch"}
    if pending:
        store.replace_auto_highlights(paper_id, view_domain, pending, replace=rerun)
        added = len(pending)
    return {"added": added, "dropped": dropped, "failed_batches": failed,
            "kept_previous": bool(rerun and not pending)}
