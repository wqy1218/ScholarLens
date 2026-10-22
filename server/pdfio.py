"""PDF parsing with PyMuPDF: column-aware reading order, sections, links, chunking,
metadata guessing (local imports lack arXiv metadata)."""
import pymupdf
import re

from . import ocr

CHUNK_TARGET_CHARS = 2000
HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\.?\s+([A-Z][A-Za-z0-9\-: ]{2,70})$")
# figure / table / algorithm captions
FLOAT_RE = re.compile(
    r"^\s*(Figure|Fig\.?|Table|Algorithm|图|表|算法)\s*([0-9]+(?:[.\-][0-9]+)?)\b",
    re.IGNORECASE)


def _float_kind(token: str) -> str:
    t = token.strip().lower()
    if t.startswith("fig") or t == "图":
        return "figure"
    if t.startswith("tab") or t == "表":
        return "table"
    return "algorithm"


def _float_label(token: str, num: str) -> str:
    kind = _float_kind(token)
    if token.strip() in ("图", "表", "算法"):
        base = {"figure": "图", "table": "表", "algorithm": "算法"}[kind]
    else:
        base = {"figure": "Figure", "table": "Table", "algorithm": "Algorithm"}[kind]
    return f"{base} {num}"


def guess_metadata(file_path: str) -> dict:
    """Best-effort title/authors/year/abstract for locally imported PDFs."""
    doc = pymupdf.open(file_path)
    try:
        meta = doc.metadata or {}
        title = (meta.get("title") or "").strip()
        if title.lower().endswith(".pdf") or "\\" in title or "/" in title or len(title) < 8:
            title = ""
        authors = (meta.get("author") or "").strip()
        year = None
        cd = meta.get("creationDate") or meta.get("modDate") or ""
        if len(cd) >= 6 and cd[2:6].isdigit():
            year = int(cd[2:6])
        if not title:
            best_size, best_text = 0.0, ""
            for b in doc[0].get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                for line in b.get("lines", []):
                    for s in line["spans"]:
                        t = s["text"].strip()
                        if 8 < len(t) < 200 and s["size"] > best_size:
                            best_size, best_text = s["size"], t
            title = best_text
        abstract = ""
        for pno in range(min(2, doc.page_count)):
            for b in doc[pno].get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                text = "\n".join(
                    "".join(s["text"] for s in line["spans"]) for line in b.get("lines", [])
                ).strip()
                low = text.lower()
                if low.startswith("abstract") and len(text) > 100:
                    abstract = text[8:].lstrip(" —:-")[:3000]
                    break
            if abstract:
                break
        return {"title": title, "authors": authors, "year": year, "abstract": abstract}
    finally:
        doc.close()


def _page_text_order(text_blocks: list, width: float, mid: float) -> list:
    """ reading order. Pure two-column pages: left column then right.
 Pages containing FULL-WIDTH blocks (titles, abstract bars, wide tables):
 hybrid band order — each full-width block is a READING-ORDER BARRIER,
 blocks between barriers are column-ordered within their band. A naive
 whole-page fallback misorders the two-column body below a full-width title."""
    def is_full(b: dict) -> bool:
        return (b["bbox"][2] - b["bbox"][0]) > width * 0.6

    def col_order(blocks: list) -> list:
        left = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 < mid - width * 0.05]
        right = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 > mid + width * 0.05]
        if len(left) >= 2 and len(right) >= 2 and (len(left) + len(right)) >= len(blocks) * 0.8:
            left.sort(key=lambda b: b["bbox"][1])
            right.sort(key=lambda b: b["bbox"][1])
            return left + right
        return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))

    full = sorted([b for b in text_blocks if is_full(b)], key=lambda b: b["bbox"][1])
    if not full:
        return col_order(text_blocks)

    rest = sorted([b for b in text_blocks if not is_full(b)], key=lambda b: b["bbox"][1])
    out: list = []
    group: list = []
    bi = 0
    for b in rest:
        while bi < len(full) and full[bi]["bbox"][1] < b["bbox"][1]:
            out.extend(col_order(group))
            group = []
            out.append(full[bi])
            bi += 1
        group.append(b)
    out.extend(col_order(group))
    out.extend(full[bi:])
    return out


MATH_FONT_HINTS = ("CMMI", "CMEX", "CMSY", "MSAM", "MSBM", "Math", "math")


def _page_items(page: pymupdf.Page) -> tuple:
    """Reading items for one page: [(y, kind, text)] — paragraphs + figure
 placeholders interleaved. anti-hallucination rules:
 - figures become [图示] placeholders at their reading position (never guessed text);
 - blocks dominated by math fonts get a [公式] marker (glyphs often garble);
 - short blocks in the header/footer bands are dropped (page numbers, running heads).
 Column-aware interleave: on two-column pages each figure joins its own column
 by x-center (mid figures go to the column matching their y-half).
 Returns (items, figure_rects, captions). additionally returns the
 figure/table/algorithm captions with their y position, so `extract` can pair a
 figure rectangle with its caption and build the structure index."""
    items: list[tuple] = []
    fig_rects: list[dict] = []
    captions: list[dict] = []
    rect = page.rect
    width, height = rect.width, rect.height

    blocks = page.get_text("dict")["blocks"]
    text_blocks = [b for b in blocks if b.get("type") == 0]
    mid = width / 2

    def block_text(b: dict) -> str:
        math_chars = 0
        total = 0
        lines = []
        for line in b.get("lines", []):
            for sp in line["spans"]:
                t = sp["text"]
                lines.append(t)
                total += len(t)
                if any(h in (sp.get("font") or "") for h in MATH_FONT_HINTS):
                    math_chars += len(t)
        text = "\n".join("".join(l) for l in lines).strip()
        if not text:
            return ""
        import re
        text = re.sub(r"-\s*\n\s*(?=[a-z])", "", text)
        text = re.sub(r"\s*\n\s*", " ", text).strip()
        for lig, plain in (("ﬁ", "fi"), ("ﬂ", "fl"), ("ﬀ", "ff"),
                           ("ﬃ", "ffi"), ("ﬄ", "ffl")):
            text = text.replace(lig, plain)
        text = text.replace("\u00ad", "").replace("\u200b", "")
        if total and math_chars / total > 0.3:
            text = "[公式] " + text
        return text

    ordered = _page_text_order(text_blocks, width, mid)
    items = []
    for idx, b in enumerate(ordered):
        text = block_text(b)
        if not text:
            continue
        y_mid = (b["bbox"][1] + b["bbox"][3]) / 2
        if (y_mid < height * 0.045 or y_mid > height * 0.955) and len(text) <= 120:
            continue
        m = FLOAT_RE.match(text)
        if m:
            captions.append({"page": page.number + 1,
                             "kind": _float_kind(m.group(1)),
                             "num": m.group(2),
                             "label": _float_label(m.group(1), m.group(2)),
                             "text": text[:240],
                             "y": round(b["bbox"][1], 1)})
        items.append((float(idx), "para", text))

    def fig_rank(r) -> float:
        """Insert a figure just before the first reading-order block below it in
 the same horizontal band; fall back to the current end."""
        rank = float(len(ordered))
        for j, b in enumerate(ordered):
            if b["bbox"][2] < r.x0 or b["bbox"][0] > r.x1:
                continue
            if b["bbox"][1] >= r.y0:
                return j - 0.5
            rank = j + 0.5
        return rank

    seen_rects = []
    page_area = rect.get_area()

    def _accept(r) -> bool:
        a = r.get_area()
        if a < page_area * 0.03 or a > page_area * 0.7:
            return False
        if r.width < 30 or r.height < 30:
            return False
        for s in seen_rects:
            ix = max(0, min(r.x1, s.x1) - max(r.x0, s.x0))
            iy = max(0, min(r.y1, s.y1) - max(r.y0, s.y0))
            inter = ix * iy
            if inter and inter / min(a, s.get_area()) > 0.6:
                return False
        seen_rects.append(r)
        return True

    cand = []
    for img in page.get_images(full=True):
        try:
            cand.extend(page.get_image_rects(img[0]))
        except Exception:
            continue
    try:
        cand.extend(page.cluster_drawings())
    except Exception:
        pass
    cand.sort(key=lambda r: -r.get_area())
    for r in cand:
        if _accept(r):
            fig_rects.append({"page": page.number + 1,
                              "x0": round(r.x0, 1), "y0": round(r.y0, 1),
                              "x1": round(r.x1, 1), "y1": round(r.y1, 1),
                              "w": round(width, 1), "h": round(height, 1)})
            items.append((fig_rank(r), "fig",
                          f"[图示]（第 {page.number + 1} 页图像区域，文字未解析）"))

    items.sort(key=lambda it: it[0])
    return items, fig_rects, captions


def extract(file_path: str) -> dict:
    """Returns {full_text, sections:[{title,level,page}], links:[...],
 chunks:[{section,page,start_char,end_char,text}], n_pages}"""
    doc = pymupdf.open(file_path)
    toc_by_page = {}
    for level, title, page1 in doc.get_toc():
        toc_by_page.setdefault(page1, (level, title))

    page_paras = []
    sections = []
    links = []
    figures = []
    page_caps: dict = {}
    ocr_pages: list[int] = []
    ocr_attempts = 0
    ocr_errors = 0
    for page_no, page in enumerate(doc, start=1):
        if page_no in toc_by_page:
            sections.append({"title": toc_by_page[page_no][1], "level": toc_by_page[page_no][0], "page": page_no})
        paras = []
        page_figs: list[dict] = []
        items, page_figs, page_captions = _page_items(page)
        page_caps[page_no] = page_captions
        figures.extend(page_figs)
        text_chars = sum(len(t) for _, kind, t in items if kind == "para")
        if (text_chars < ocr.MIN_PAGE_CHARS and ocr_attempts < ocr.MAX_PAGES
                and ocr.available()):
            ocr_attempts += 1
            try:
                ocr_text = ocr.page_text(page)
            except Exception as e:
                ocr_text = ""
                ocr_errors += 1
                print(f"[ocr] page {page_no} failed: {str(e)[:120]}")
            if ocr_text:
                ocr_pages.append(page_no)
                paras.extend(seg.strip() for seg in ocr_text.split("\n\n") if seg.strip())
                items = []
        for _, kind, text in items:
            if kind != "para":
                continue
            para = text
            if para:
                paras.append(para)
        section_title = toc_by_page.get(page_no, ("", ""))[1] or (page_paras[-1][2] if page_paras else "")
        page_paras.append((page_no, paras, section_title))
        for lk in page.get_links():
            links.append({
                "page": page_no,
                "kind": int(lk.get("kind", 0)),
                "uri": lk.get("uri", ""),
                "to_page": (lk.get("page", -1) + 1) if lk.get("page", -1) >= 0 else -1,
            })
    n_pages = doc.page_count
    doc.close()

    full_parts = []
    offset = 0
    para_records = []
    for page_no, paras, section in page_paras:
        current = section
        for p in paras:
            heading = HEADING_RE.match(p.strip()) if len(p) <= 90 else None
            if heading:
                current = p.strip()
                sections.append({"title": current, "level": heading.group(1).count(".") + 1,
                                 "page": page_no})
            start = offset
            full_parts.append(p)
            full_parts.append("\n\n")
            offset = start + len(p) + 2
            para_records.append((page_no, current, start, start + len(p), p))
    full_text = "".join(full_parts)

    chunks = []
    cur = {"items": []}
    cur_start = cur_end = None

    def _chars() -> int:
        return sum(len(t) for t, _, _ in cur["items"])

    def close():
        if cur["items"]:
            chunks.append({"section": cur["items"][0][2], "page": cur["items"][0][1],
                           "start_char": cur_start, "end_char": cur_end,
                           "text": "\n\n".join(t for t, _, _ in cur["items"])})

    for page_no, section, start, end, text in para_records:
        if cur["items"] and _chars() + len(text) > CHUNK_TARGET_CHARS:
            close()
            ot, op, osec = cur["items"][-1]
            cur = {"items": [(ot, op, osec)]}
            cur_start = start - len(ot) - 2
        elif not cur["items"]:
            cur_start = start
        cur["items"].append((text, page_no, section))
        cur_end = end
    close()

    # pair each figure rectangle with its caption and build the float index
    for fig in figures:
        caps = [c for c in page_caps.get(fig["page"], []) if c["kind"] == "figure"]
        if not caps:
            continue
        below = [c for c in caps if c["y"] >= fig["y1"] - 2]
        pick = (min(below, key=lambda c: c["y"]) if below
                else min(caps, key=lambda c: abs(c["y"] - fig["y0"])))
        fig["label"] = pick["label"]
        fig["num"] = pick["num"]
        fig["caption"] = pick["text"]

    floats = []
    for pg in sorted(page_caps):
        for c in sorted(page_caps[pg], key=lambda x: x["y"]):
            floats.append({"page": c["page"], "kind": c["kind"], "num": c["num"],
                           "label": c["label"], "text": c["text"]})
    named = {(c["kind"], c["num"], c["page"]) for c in floats}
    for fig in figures:
        if fig.get("num") and ("figure", fig["num"], fig["page"]) in named:
            continue
        floats.append({"page": fig["page"], "kind": "figure",
                       "num": fig.get("num", ""),
                       "label": fig.get("label") or f"Figure p.{fig['page']}",
                       "text": fig.get("caption", "")})
    for f in floats:
        if f["kind"] != "figure":
            continue
        for fig in figures:
            if fig["page"] == f["page"] and fig.get("label") == f["label"]:
                f["rect"] = {"x0": fig["x0"], "y0": fig["y0"], "x1": fig["x1"],
                             "y1": fig["y1"], "w": fig["w"], "h": fig["h"]}
                break

    return {"full_text": full_text, "sections": sections, "links": links,
            "chunks": chunks, "n_pages": n_pages, "figures": figures,
            "floats": floats, "ocr_pages": ocr_pages, "ocr_attempts": ocr_attempts,
            "ocr_errors": ocr_errors,
            "paras": [{"page": pg, "start_char": s, "end_char": e, "text": t}
                      for pg, _, s, e, t in para_records]}
