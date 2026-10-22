"""API smoke test (TestClient, temporary data directory)."""
import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp(prefix="sl_api_")
os.environ["SCHOLARLENS_DATA_DIR"] = tmp
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient

from server import api, store


def _cleanup() -> None:
    try:
        store._conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


atexit.register(_cleanup)


def main() -> None:
    with TestClient(api.app) as c:
        assert c.get("/health").json()["status"] == "ok"
        did = c.post("/domains", json={"name": "NLP"}).json()["domain_id"]
        pid = store.upsert_paper(None, "Attention Is All You Need", None,
                                 arxiv_id="1706.03762", authors="Vaswani",
                                 abstract="transformer", sha1="abc", doi="10.1/xyz")
        store.set_parse_status(pid, "done")
        store.set_paper_domain(pid, did, source="import")
        store.replace_chunks(pid, did, [{"section": "Intro", "page": 1, "start_char": 0,
                                         "end_char": 10, "text": "attention is all you need"}])

        assert len(c.get(f"/papers/{pid}").json()["domains"]) == 1
        assert c.post(f"/papers/{pid}/reading",
                      json={"domain_id": did, "status": "done", "progress": 55}).status_code == 200
        assert c.post("/questions", json={"paper_id": pid, "domain_id": did,
                                          "text": "why h=8?"}).status_code == 200
        assert c.get(f"/domains/{did}/questions").json()
        assert c.put(f"/domains/{did}/config", json={"highlight_density": "dense"}).status_code == 200
        cid = c.post(f"/domains/{did}/concepts", json={"term_en": "attention", "term_zh": "注意力"}).json()["concept_id"]
        d2 = c.post("/domains", json={"name": "CV"}).json()["domain_id"]
        assert c.post("/concepts/mappings", json={"concept_id": cid, "domain_a": did,
                                                  "domain_b": d2, "mapping_type": "different"}).status_code == 200
        assert c.get(f"/domains/{did}/graph").status_code == 200
        assert c.get(f"/domains/{did}/search", params={"q": "attention"}).json()
        assert c.get(f"/papers/{pid}/summary").json()["cache"]["hit"] is False
        assert c.get("/health/integrity").json()["ok"] is True
        assert c.post("/exports", json={"scope": "domain", "scope_id": did,
                                        "paper_ids": [pid]}).status_code == 200
        assert c.post(f"/domains/{did}/snapshots", json={"name": "before-merge"}).status_code == 200
        assert c.get(f"/domains/{did}/snapshots").json()
        sid = c.post("/suggestions", json={"source_domain_id": did, "target_domain_id": d2,
                                           "description": "attention may help CV",
                                           "confidence": 0.6}).json()["suggestion_id"]
        assert c.post(f"/suggestions/{sid}/feedback",
                      json={"status": "ignored"}).status_code == 200
        assert c.get(f"/papers/{pid}/card").json()["card"]["paper_id"] == pid
        assert c.post(f"/papers/{pid}/card", json={"content": {"title": "t"}}).status_code == 200
        assert c.get(f"/papers/{pid}/skim").json()["path"]
        assert c.get(f"/papers/{pid}/export").json()["markdown"]
        assert c.get(f"/domains/{did}/report").json()["markdown"]
        assert c.get(f"/domains/{did}/survey").json()["markdown"]
        st = c.post(f"/domains/{did}/subtopics", json={"name": "sub"}).json()["subtopic_id"]
        assert c.post(f"/papers/{pid}/subtopics",
                      json={"subtopic_ids": [st]}).status_code == 200
        snap = c.post(f"/domains/{did}/snapshots", json={"name": "s1"}).json()["snapshot_id"]
        assert c.post(f"/domains/{did}/snapshots/{snap}/restore").status_code == 200
        assert c.post(f"/domains/{did}/index/migrate").json()["mode"] == "physical"
        print("test_api OK", c.get("/health/integrity").json()["stats"])


if __name__ == "__main__":
    main()
