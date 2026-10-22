"""OpenAI 兼容接口客户端。

- 校验 finish_reason：思考模式可能把较小的 max_tokens 全部吃掉，HTTP 200 但正文为空；
- 正文为空且 finish_reason=length 时，以 4 倍预算重试一次；
- 记录响应里的 model_served（服务商会静默改写模型别名）；
- 响应缓存与调用日志在独立的 data/llm_cache.db。
"""
import hashlib
import json
import os
import sqlite3
import threading
import time

import requests

BASE_URL = "https://api.deepseek.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("SCHOLARLENS_DATA_DIR") or os.path.join(ROOT, "data")
KEY_PATH = os.path.join(ROOT, "APIKEY.txt")
CACHE_PATH = os.path.join(DATA_DIR, "llm_cache.db")

MODEL_FAST = "deepseek-flash"
MODEL_PRO = "deepseek-flash"

_conn = None
_lock = threading.RLock()
_session = requests.Session()
HTTP_TIMEOUT = 60


def _profile() -> dict:
    """Active provider profile (multi-vendor since ). Falls back to first
 profile, then to an empty dict where BASE_URL/APIKEY.txt apply."""
    try:
        from . import store
        active = store.get_config("llm_active")
        profiles = json.loads(store.get_config("llm_profiles") or "[]")
        for p in profiles:
            if p.get("name") == active:
                return p
        return profiles[0] if profiles else {}
    except Exception:
        return {}


def _cfg(key: str, default: str) -> str:
    """Runtime config (config table). Defaults live in store.CONFIG_DEFAULTS; this
 fallback keeps llm usable in isolation (scripts/tests)."""
    try:
        from . import store
        v = store.get_config(key)
        return v if v else default
    except Exception:
        return default


def _base_url() -> str:
    return (_profile().get("api_base") or BASE_URL).rstrip("/")


def _api_key() -> str:
    key = (_profile().get("api_key") or "").strip()
    if key:
        return key
    try:
        with open(KEY_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def configured() -> bool:
    """True when a key is available (profile or APIKEY.txt)."""
    return bool(_api_key())


def _model(fast: bool) -> str:
    p = _profile()
    return p.get("model_fast" if fast else "model_pro") or (MODEL_FAST if fast else MODEL_PRO)


def fast_model() -> str:
    return _model(True)


def pro_model() -> str:
    return _model(False)


def vision_model() -> str:
    """Model for image input (OCR pages / screenshot questions).

 Falls back to the fast model: providers with vision support usually expose
 it under the same name; a dedicated one can be set in the profile."""
    return (_profile().get("vision_model") or "").strip() or fast_model()


def _thinking_param() -> dict | None:
    """User-specified thinking switch. Measured: 'disabled' works on this platform
 and prevents reasoning from burning max_tokens."""
    t = _profile().get("thinking", "disabled")
    return {"type": t} if t in ("disabled", "enabled") else None


def _token_budget(user_tokens: int) -> int:
    default = int(_cfg("max_tokens_default", "2048") or 2048)
    limit = int(_cfg("max_output_limit", "100000") or 100000)
    return max(256, min(user_tokens or default, limit))


def _cache():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            _conn = sqlite3.connect(CACHE_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript("""
 CREATE TABLE IF NOT EXISTS llm_cache (
 cache_key TEXT PRIMARY KEY, response TEXT NOT NULL,
 model_served TEXT, latency_ms INTEGER, created_at TEXT);
 CREATE TABLE IF NOT EXISTS api_calls (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task TEXT, model TEXT, model_served TEXT, finish_reason TEXT,
 latency_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
 cache_hit INTEGER DEFAULT 0, ok INTEGER, error TEXT DEFAULT '',
 created_at TEXT);
            """)
            _conn.commit()
        return _conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _log_call(**kw) -> None:
    c = _cache()
    with _lock:
        c.execute(
            "INSERT INTO api_calls(task, model, model_served, finish_reason, latency_ms,"
            " prompt_tokens, completion_tokens, cache_hit, ok, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kw.get("task"), kw.get("model"), kw.get("model_served"), kw.get("finish_reason"),
             kw.get("latency_ms"), kw.get("prompt_tokens"), kw.get("completion_tokens"),
             1 if kw.get("cache_hit") else 0, 1 if kw.get("ok") else 0, kw.get("error", ""), _now()))
        c.commit()


def _cache_key(task: str, messages: list, model: str, temperature: float, extra: str) -> str:
    blob = json.dumps({"task": task, "messages": messages, "model": model,
                       "temperature": temperature, "extra": extra}, ensure_ascii=False,
                      sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()


def chat(task: str, messages: list, model: str = MODEL_FAST, max_tokens: int = 2048,
         temperature: float = 0.2, use_cache: bool = True, cache_extra: str = "") -> dict:
    """Non-streaming chat. Returns {content, model_served, finish_reason, latency_ms, cached}."""
    key = _cache_key(task, messages, model, temperature, cache_extra)
    if use_cache:
        row = _cache_get(key)
        if row:
            _log_call(task=task, model=model, model_served=row["model_served"],
                      finish_reason="cache", latency_ms=0, cache_hit=True, ok=True)
            return {"content": row["response"], "model_served": row["model_served"],
                    "finish_reason": "cache", "latency_ms": row["latency_ms"], "cached": True}

    try:
        result = _call_once(task, messages, model, max_tokens, temperature)
    except requests.exceptions.Timeout:
        result = _call_once(task, messages, model, max_tokens, temperature, note="retry-timeout")
    if not result["content"] and result["finish_reason"] == "length":
        result = _call_once(task, messages, model, max_tokens * 4, temperature,
                            note="retry-4x")
    if use_cache and result["content"]:
        _cache_put(key, result)
    return result


def cache_lookup(task: str, messages: list, model: str, temperature: float, extra: str) -> dict:
    """Public cache probe for streaming callers: hit → full content, miss → caller
 streams then stores with cache_store()."""
    key = _cache_key(task, messages, model, temperature, extra)
    row = _cache_get(key)
    if row:
        _log_call(task=task, model=model, model_served=row["model_served"],
                  finish_reason="cache", latency_ms=0, cache_hit=True, ok=True)
        return {"cached": True, "key": key, "content": row["response"],
                "model_served": row["model_served"], "latency_ms": row["latency_ms"]}
    return {"cached": False, "key": key}


def cache_store(key: str, content: str, model_served: str, latency_ms: int) -> None:
    _cache_put(key, {"content": content, "model_served": model_served, "latency_ms": latency_ms})


def _cache_get(key: str):
    with _lock:
        return _cache().execute(
            "SELECT response, model_served, latency_ms FROM llm_cache WHERE cache_key=?",
            (key,)).fetchone()


def _cache_put(key: str, result: dict) -> None:
    with _lock:
        _cache().execute(
            "INSERT OR REPLACE INTO llm_cache(cache_key, response, model_served, latency_ms, created_at)"
            " VALUES (?,?,?,?,?)",
            (key, result["content"], result["model_served"], result["latency_ms"], _now()))
        _cache().commit()


def _call_once(task: str, messages: list, model: str, max_tokens: int, temperature: float,
               note: str = "") -> dict:
    payload = {"model": model, "messages": messages,
               "max_tokens": _token_budget(max_tokens), "temperature": temperature}
    thinking = _thinking_param()
    if thinking:
        payload["thinking"] = thinking
    t0 = time.time()
    try:
        resp = _session.post(f"{_base_url()}/chat/completions",
                             headers={"Authorization": f"Bearer {_api_key()}"},
                             json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
        out = {"content": content, "model_served": data.get("model", model),
               "finish_reason": choice.get("finish_reason"),
               "latency_ms": int((time.time() - t0) * 1000), "cached": False, "note": note}
        _log_call(task=task + (f":{note}" if note else ""), model=model,
                  model_served=out["model_served"], finish_reason=out["finish_reason"],
                  latency_ms=out["latency_ms"],
                  prompt_tokens=data.get("usage", {}).get("prompt_tokens"),
                  completion_tokens=data.get("usage", {}).get("completion_tokens"), ok=True)
        return out
    except Exception as e:
        _log_call(task=task, model=model, latency_ms=int((time.time() - t0) * 1000),
                  ok=False, error=str(e)[:300])
        raise


def chat_stream(task: str, messages: list, model: str = MODEL_PRO, max_tokens: int = 2048,
                temperature: float = 0.2, _retry: bool = True, thinking: str | None = None):
    """Yield content deltas via SSE stream. Caller must consume fully.
 finish_reason/model_served delivered in the final ('__meta__') yield.
 Same empty-content retry as chat(): thinking mode can eat the whole budget.
 thinking: per-call override ("enabled"/"disabled") — 对话级思考模式."""
    payload = {"model": model, "messages": messages,
               "max_tokens": _token_budget(max_tokens), "temperature": temperature, "stream": True}
    thinking_param = {"type": thinking} if thinking in ("enabled", "disabled") else _thinking_param()
    if thinking_param:
        payload["thinking"] = thinking_param
    t0 = time.time()
    resp = _session.post(f"{_base_url()}/chat/completions",
                         headers={"Authorization": f"Bearer {_api_key()}"},
                         json=payload, timeout=(10, HTTP_TIMEOUT * 5), stream=True)
    resp.raise_for_status()
    served, finish, usage = model, None, {}
    yielded = False
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            break
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if obj.get("model"):
            served = obj["model"]
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            yielded = True
            yield delta["content"]
        if choices[0].get("finish_reason"):
            finish = choices[0]["finish_reason"]
        if obj.get("usage"):
            usage = obj["usage"]
    if not yielded and finish == "length" and _retry:
        yield from chat_stream(task, messages, model, max_tokens * 4, temperature, _retry=False)
        return
    meta = {"model_served": served, "finish_reason": finish,
            "latency_ms": int((time.time() - t0) * 1000),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens")}
    _log_call(task=task, model=model, model_served=served, finish_reason=finish,
              latency_ms=meta["latency_ms"], prompt_tokens=meta["prompt_tokens"],
              completion_tokens=meta["completion_tokens"], ok=finish == "stop")
    yield "__meta__" + json.dumps(meta, ensure_ascii=False)


def api_call_stats() -> dict:
    """调用统计：总次数 / 缓存命中率 / 平均延迟。"""
    with _lock:
        c = _cache()
        total = c.execute("SELECT COUNT(*) n FROM api_calls").fetchone()["n"]
        hits = c.execute("SELECT COUNT(*) n FROM api_calls WHERE cache_hit=1").fetchone()["n"]
        mean = c.execute("SELECT AVG(latency_ms) m FROM api_calls WHERE ok=1 AND cache_hit=0").fetchone()["m"]
    return {"total_calls": total, "cache_hits": hits,
            "mean_latency_ms": int(mean) if mean else 0}


def query_audit_by_task() -> list[dict]:
    """ audit page: per-task call counts / cache hits / ok / mean latency."""
    with _lock:
        rows = _cache().execute(
            "SELECT task, COUNT(*) n, SUM(cache_hit) hits, SUM(ok) ok_n,"
            " AVG(CASE WHEN cache_hit=0 THEN latency_ms END) mean_ms"
            " FROM api_calls GROUP BY task ORDER BY n DESC").fetchall()
    return [{"task": r["task"], "n": r["n"], "cache_hits": r["hits"] or 0,
             "ok_n": r["ok_n"] or 0, "mean_ms": int(r["mean_ms"] or 0)} for r in rows]
