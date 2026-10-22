"""扫描页文字识别：没有文字层的页面渲染成 PNG，交给配置的视觉模型转写。

结果写入 `papers.ocr_status`，解析完成后一并落库，不单独显示进度。
"""
import base64
import os
import re

from . import llm

MIN_PAGE_CHARS = int(os.environ.get("SCHOLARLENS_OCR_MIN_CHARS", "40") or 40)
MAX_PAGES = int(os.environ.get("SCHOLARLENS_OCR_MAX_PAGES", "12") or 12)
DPI = int(os.environ.get("SCHOLARLENS_OCR_DPI", "180") or 180)
MAX_TOKENS = int(os.environ.get("SCHOLARLENS_OCR_MAX_TOKENS", "4096") or 4096)

PROMPT = (
    "你是 PDF 文本转写器。把图片（PDF 的一页）中的全部正文转写为纯文本：\n"
    "1. 只转写，不解释、不总结、不补全；\n"
    "2. 保留段落之间的空行，保留公式的可见符号；\n"
    "3. 页眉页脚（页码、期刊名、作者栏）可以省略；\n"
    "4. 无法辨认的字符用 [无法辨认] 占位，不要猜测。"
)


def available() -> bool:
    """OCR needs a configured model; without a key parsing must not block."""
    try:
        return llm.configured()
    except Exception:
        return False


def page_text(page) -> str:
    """Transcribe one pymupdf page. Returns "" when unavailable or empty."""
    if not available():
        return ""
    pix = page.get_pixmap(dpi=DPI)
    b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
    ]}]
    result = llm.chat(task="ocr", messages=messages, model=llm.vision_model(),
                      max_tokens=MAX_TOKENS, temperature=0.0, use_cache=False)
    text = (result.get("content") or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    return text
