"""Remap highlight categories to the current taxonomy:
 problem(问题) -> background(背景, 蓝)
 finding(结论/局限) -> limitation(局限, 红)
 approach(方法) -> approach(解决思路, 绿) [key unchanged]
Also rewrites the prompt_highlight config so auto-identify uses these keys.

 python scripts/migrate_categories.py --apply
"""
import sqlite3
import sys

DB = r"D:\ScholarLens\data\scholarlens.db"

PROMPT = (
    "从给定的论文材料中挑选值得标记的关键句。三类定义：\n"
    "- background：研究背景、领域现状、要解决的问题与动机\n"
    "- approach：解决思路、方法设计、核心创新\n"
    "- limitation：实验结论、适用边界、局限与后续工作\n"
    "输出 JSON 数组，每项 {\"category\": \"background|approach|limitation\", \"quote\": \"英文原句\"}。"
    "quote 必须逐字摘自材料（禁止改写拼接），总共挑 6-12 句最重要的。只输出 JSON。")

MAP = {"problem": "background", "finding": "limitation"}


def main(apply: bool) -> None:
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT content, COUNT(*) c FROM interactions WHERE type='highlight' GROUP BY content").fetchall()
    print("before:", dict(rows))
    if apply:
        for old, new in MAP.items():
            db.execute("UPDATE interactions SET content=? WHERE type='highlight' AND content=?", (new, old))
        db.execute("UPDATE config SET value=? WHERE key='prompt_highlight'", (PROMPT,))
        db.commit()
    rows = db.execute("SELECT content, COUNT(*) c FROM interactions WHERE type='highlight' GROUP BY content").fetchall()
    print("after :", dict(rows))
    print("prompt_highlight updated" if apply else "DRY-RUN (use --apply)")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
