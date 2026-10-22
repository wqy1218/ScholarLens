"""End-to-end flow: import, reading position, outline, survey draft and snapshot restore."""
import atexit
import hashlib
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="sl_e2e_")
os.environ["SCHOLARLENS_DATA_DIR"] = TMP

import pymupdf  # noqa: 
from PIL import Image  # noqa: 

PDF = os.path.join(TMP, "e2e-paper.pdf")
BLOCK = os.path.join(TMP, "block.png")
Image.new("RGB", (220, 120), (200, 40, 40)).save(BLOCK)

doc = pymupdf.open()
p1 = doc.new_page(width=595, height=842)
p1.insert_text((60, 70), "1 Introduction")
p1.insert_text((60, 100), "This paper studies a cross domain reading workflow. " * 4)
p1.insert_image(pymupdf.Rect(60, 150, 280, 270), filename=BLOCK)
p1.insert_text((60, 320), "2 Method")
p1.insert_text((60, 350), "We build a local first workbench with per domain state. " * 4)
p2 = doc.new_page(width=595, height=842)
p2.insert_text((60, 70), "3 Results")
p2.insert_text((60, 100), "The reading position is restored on the correct page. " * 4)
p2.insert_text((60, 260), "4 Conclusion")
p2.insert_text((60, 290), "Domain context is carried through every paper endpoint. " * 4)
doc.save(PDF)
doc.close()

from server import assign as _assign  # noqa: 


def fake_embed(texts):
    out = []
    for t in texts:
        h = hashlib.sha256((t or "").encode("utf-8")).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


_assign.embed = fake_embed

from server import api  # noqa: 
from server import store as _store  # noqa: 
from fastapi.testclient import TestClient  # noqa: 


def _cleanup():
    try:
        _store.close()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)


atexit.register(_cleanup)

OK = []


def check(name, cond, detail=""):
    OK.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + name +
          (("  " + str(detail)[:300]) if detail and not cond else ""))


with TestClient(api.app) as client:
    did = client.post("/domains", json={"name": "E2E-Domain"}).json()["domain_id"]
    client.put(f"/domains/{did}/goal", json={"research_goal": "快速了解"})
    check("e2e: domain created", did > 0)

    queued = client.post("/papers/import",
                         json={"local_paths": [PDF], "domain_id": did}).json()["queued"]
    pid = queued[0].get("paper_id")
    check("e2e: import queued", bool(pid), queued)

    deadline = time.time() + 60
    paper = {}
    while time.time() < deadline:
        paper = client.get(f"/papers/{pid}?domain_id={did}").json()
        if paper.get("parse_status") in ("done", "failed"):
            break
        time.sleep(0.1)
    check("e2e: parse finished", paper.get("parse_status") == "done",
          paper.get("parse_error"))
    check("e2e: imported into the selected domain", paper.get("domain_id") == did)
    check("e2e: detail reports the view domain", paper.get("view_domain_id") == did)

    outline = client.get(f"/papers/{pid}/outline?domain_id={did}").json()
    check("e2e: outline has two-level sections", len(outline.get("sections") or []) >= 3,
          outline.get("sections"))
    check("e2e: outline exposes a section_tree", isinstance(outline.get("section_tree"), list)
          and len(outline["section_tree"]) >= 1)
    check("e2e: outline exposes n_figures", outline.get("n_figures") == len(outline.get("figures") or []))
    figs = outline.get("figures") or []
    check("e2e: figure rect is top-left origin",
          bool(figs) and 0 < figs[0]["y0"] < figs[0]["y1"] < 421,
          figs[:1])

    r = client.post(f"/papers/{pid}/reading",
                    json={"domain_id": did, "page": 2, "scroll_ratio": 0.4})
    check("e2e: save reading position", r.status_code == 200, r.text[:200])
    detail = client.get(f"/papers/{pid}?domain_id={did}").json()
    check("e2e: position read back",
          detail["position"]["last_page"] == 2
          and abs(detail["position"]["scroll_ratio"] - 0.4) < 1e-6, detail["position"])
    check("e2e: positions list carries the domain",
          any(p["domain_id"] == did for p in detail["positions"]))

    ov = client.get(f"/domains/{did}/overview").json()
    check("e2e: overview last_view points at the paper",
          ov.get("last_view", {}).get("paper_id") == pid, ov.get("last_view"))
    check("e2e: overview last_view carries the page",
          ov.get("last_view", {}).get("last_page") == 2)

    survey = client.post(f"/domains/{did}/survey", json={
        "domain_ids": [did], "group_by": "topic", "include_gaps": True,
        "title": "E2E Survey", "save": True}).json()
    check("e2e: survey draft generated and saved", bool(survey.get("survey_id")))
    check("e2e: survey cites the paper", f"[P{pid}]" in survey.get("markdown", ""))
    check("e2e: survey has a reference section", "## 参考" in survey.get("markdown", ""))
    got = client.get(f"/surveys/{survey['survey_id']}").json()
    check("e2e: survey draft readable", got.get("markdown") == survey.get("markdown"))

    snap = client.post(f"/domains/{did}/snapshots", json={"name": "e2e"}).json()["snapshot_id"]
    check("e2e: snapshot created", snap > 0)

    client.post(f"/papers/{pid}/reading",
                json={"domain_id": did, "page": 1, "scroll_ratio": 0.0})
    moved = client.get(f"/papers/{pid}?domain_id={did}").json()
    check("e2e: position moved before restore", moved["position"]["last_page"] == 1)

    rest = client.post(f"/domains/{did}/snapshots/{snap}/restore",
                       json={"include_notes": False, "include_qa": False})
    check("e2e: snapshot restored", rest.status_code == 200, rest.text[:200])
    back = client.get(f"/papers/{pid}?domain_id={did}").json()
    check("e2e: position restored to page 2", back["position"]["last_page"] == 2,
          back["position"])

    health = client.get("/health").json()
    check("e2e: health is ok", health.get("status") == "ok", health)

print(f"\nRESULT: {'ALL OK' if all(OK) else 'FAILURES'} ({len(OK)} checks)")
if not all(OK):
    sys.exit(1)
