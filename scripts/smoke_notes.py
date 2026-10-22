import json
import urllib.request

BASE = "http://127.0.0.1:21800"


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


r1 = call("POST", "/papers/1/notes", {
    "page": 1, "quote": "We propose a new simple network architecture, the Transformer",
    "text": "核心主张：完全抛弃循环结构，纯注意力。面试可对比 RNN 的顺序计算瓶颈。",
    "hl_id": 11})
print("add1:", r1)
r2 = call("POST", "/papers/1/notes", {
    "page": 2, "quote": "This inherently sequential nature precludes parallelization",
    "text": "背景痛点：顺序计算无法并行训练。"})
print("add2:", r2)
notes = call("GET", "/papers/1/notes")
print("list:", [(n["id"], n["page"], n["hl_id"], n["text"][:20]) for n in notes])
print("edit:", call("PUT", f"/papers/1/notes/{r2['id']}", {"text": "背景痛点：顺序计算无法并行训练（编辑过）。"}))
notes = call("GET", "/papers/1/notes")
print("after edit:", [n["text"][:25] for n in notes])
print("delete:", call("DELETE", f"/papers/1/notes/{r2['id']}"))
print("final count:", len(call("GET", "/papers/1/notes")))
