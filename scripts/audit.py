"""Sample QA turns and print the evidence each one retrieved, with hit-rate numbers.
Run: python scripts/audit.py [N]"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import llm, store

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5

store.connect(os.path.join("data", "scholarlens.db"))
rows = store.query(
    "SELECT interaction_id, paper_id, content FROM interactions WHERE type='qa'"
    " ORDER BY interaction_id DESC LIMIT ?", (N,))
if not rows:
    print("no QA turns recorded yet")
    sys.exit(0)

cited_total = 0
valid_total = 0
for r in rows:
    turn = json.loads(r["content"])
    refs = turn.get("refs", [])
    cited = [t.split("]")[0] for t in turn["answer"].split("[")[1:] if "]" in t]
    cited_nums = {int(c) for c in cited if c.isdigit()}
    valid = {n for n in cited_nums if 1 <= n <= len(refs)}
    cited_total += len(cited_nums)
    valid_total += len(valid)
    print(f"--- QA #{r['interaction_id']} (paper {r['paper_id']})")
    print(f"  Q: {turn['question'][:80]}")
    print(f"  A: {turn['answer'][:120]}...")
    print(f"  citations used: {sorted(cited_nums)} | valid: {sorted(valid)} |"
          f" invalid: {sorted(cited_nums - valid)}")

print(f"\neffect citation rate: {valid_total}/{cited_total}"
      f" = {valid_total / cited_total * 100:.0f}%" if cited_total else "no citations yet")
print("llm stats:", llm.api_call_stats())
