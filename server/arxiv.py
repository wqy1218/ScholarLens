"""arXiv 元数据与 PDF 下载：串行请求，间隔 3 秒，失败重试 2 次。"""
import os
import re
import time
import xml.etree.ElementTree as ET

import requests

API = "https://export.arxiv.org/api/query"
PDF = "https://arxiv.org/pdf/{id}"
NS = {"a": "http://www.w3.org/2005/Atom"}
INTERVAL_S = 3.0
_last_request_ts = 0.0

_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?$")


def normalize_id(url_or_id: str) -> str | None:
    s = url_or_id.strip()
    s = re.sub(r"^https?://(www\.)?arxiv\.org/(abs|pdf)/", "", s).removesuffix(".pdf")
    s = re.sub(r"^arxiv:", "", s)
    m = _ID_RE.search(s)
    return m.group(1) if m else None


def _throttle() -> None:
    global _last_request_ts
    wait = INTERVAL_S - (time.time() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()


def fetch_metadata(arxiv_ids: list[str]) -> dict:
    """arxiv_id -> {title, authors, abstract, year, primary_category}"""
    out = {}
    for i in range(0, len(arxiv_ids), 100):
        batch = arxiv_ids[i:i + 100]
        _throttle()
        resp = requests.get(API, params={"id_list": ",".join(batch), "max_results": len(batch)}, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for entry in root.findall("a:entry", NS):
            aid_raw = entry.find("a:id", NS).text
            aid = normalize_id(aid_raw)
            cat = entry.find("a:category", NS)
            out[aid] = {
                "title": " ".join(entry.find("a:title", NS).text.split()),
                "authors": ", ".join(a.find("a:name", NS).text for a in entry.findall("a:author", NS)),
                "abstract": " ".join((entry.find("a:summary", NS).text or "").split()),
                "year": int(entry.find("a:published", NS).text[:4]),
                "primary_category": cat.get("term") if cat is not None else "",
            }
    return out


def search(query: str, max_results: int = 20) -> list[dict]:
    """Search arXiv; returns [{arxiv_id, ...meta}] sorted by relevance.
arXiv API 间歇 429/hang（对指纹与突发都敏感）——与 download 同样 2 次退避重试。"""
    last_err = None
    for attempt in range(2):
        _throttle()
        try:
            resp = requests.get(API, params={
                "search_query": query, "start": 0, "max_results": max_results,
                "sortBy": "relevance", "sortOrder": "descending",
            }, timeout=60)
            resp.raise_for_status()
            break
        except Exception as e:
            last_err = e
    else:
        raise last_err
    root = ET.fromstring(resp.content)
    results = []
    for entry in root.findall("a:entry", NS):
        aid = normalize_id(entry.find("a:id", NS).text)
        cat = entry.find("a:category", NS)
        results.append({
            "arxiv_id": aid,
            "title": " ".join(entry.find("a:title", NS).text.split()),
            "authors": ", ".join(a.find("a:name", NS).text for a in entry.findall("a:author", NS)),
            "abstract": " ".join((entry.find("a:summary", NS).text or "").split()),
            "year": int(entry.find("a:published", NS).text[:4]),
            "primary_category": cat.get("term") if cat is not None else "",
        })
    return results


def download_pdf(arxiv_id: str, papers_dir: str, progress_cb=None) -> tuple[str, str]:
    """下载到 papers_dir/{sha1}.pdf；progress_cb(percent) 按块回调，返回 (path, sha1)。"""
    os.makedirs(papers_dir, exist_ok=True)
    last_err = None
    for _ in range(2):
        _throttle()
        try:
            resp = requests.get(PDF.format(id=arxiv_id), timeout=60, stream=True,
                                headers={"User-Agent": "ScholarLens/0.1"})
            resp.raise_for_status()
            import hashlib
            h = hashlib.sha1()
            total = int(resp.headers.get("content-length") or 0)
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=1 << 16):
                buf.extend(chunk)
                h.update(chunk)
                if progress_cb and total:
                    progress_cb(min(99.0, len(buf) * 100.0 / total))
            content = bytes(buf)
            if content[:5] != b"%PDF-":
                raise ValueError("not a PDF response")
            sha1 = h.hexdigest()
            path = os.path.join(papers_dir, f"{sha1}.pdf")
            with open(path, "wb") as f:
                f.write(content)
            if progress_cb:
                progress_cb(100.0)
            return path, sha1
        except Exception as e:
            last_err = e
    raise RuntimeError(f"download {arxiv_id} failed: {last_err}")
