"""LLM 输出的统一校验：入库或展示前都经过这里。

每个 validator 返回规范化后的值或 None，由调用方决定丢弃/计数/重试。
"""
import json
import re
import unicodedata


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def canon(s: str) -> str:
    """Comparison space: NFKD + lowercase + strip non-alphanumeric."""
    s = unicodedata.normalize("NFKD", s or "").lower()
    return re.sub(r"[^a-z0-9]", "", s)


def has_garbled_math(s: str) -> bool:
    """formula selections extract as garbage glyphs (private-use area,
 math symbol planes, replacement chars). >12% such chars → the text layer
 is unreliable for this span; callers should advise 图片模式 instead."""
    if not s:
        return False
    bad = sum(1 for c in s if (
        '' <= c <= '' or
        c == '�' or
        '∀' <= c <= '⋿' or
        '𝐀' <= c <= '𝟿'))
    return bad / max(1, len(s)) > 0.12


def parse_json_blob(text: str, kind: str = "array"):
    """Extract the first JSON blob of `kind` ("array"|"object") from LLM output.
 Tolerates markdown fences and leading prose."""
    if not text:
        return None
    pattern = r"\[.*\]" if kind == "array" else r"\{.*\}"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def validate_verbatim_quote(quote: str, texts: list[str], *,
                            min_chars: int = 0, min_words: int = 0,
                            canon_fallback: bool = True) -> str | None:
    """quote must land verbatim (or canon-equal) inside one of `texts`.
 Returns the normalized quote, or None when too short / not found."""
    q = norm_text(quote)
    if len(q) < min_chars or len(q.split()) < min_words:
        return None
    normed = [norm_text(t) for t in texts]
    if any(q in t for t in normed):
        return q
    if canon_fallback:
        cq = canon(quote)
        if len(cq) < 10:
            return None
        if any(cq in canon(t) for t in texts):
            return q
    return None


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"(\[])")


def split_sentences(text: str, *, min_chars: int = 2) -> list[str]:
    """Sentence split heuristic (shared by translation & highlighting).
 Known limit: abbreviations (et al./Fig. 3) can over-split — harmless when
 each fragment is used with full context."""
    parts = _SENT_SPLIT.split((text or "").strip())
    sents: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) < min_chars and sents:
            sents[-1] += " " + p
        else:
            sents.append(p)
    return sents or [(text or "").strip()]
