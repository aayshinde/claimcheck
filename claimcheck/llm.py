"""LLM access: one OpenAI-compatible client (local Ollama by default), embeddings, and per-call cost logging.

Every call is written to llm_calls with tokens, latency and cost, which feeds the cost panel. Local models have no API
fee, so we also report what the same token volume would cost at a reference paid-API price (config.REFERENCE_PRICE_*).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from functools import lru_cache

import numpy as np

from claimcheck import config
from claimcheck.db import LLMCall, session_scope


def llm_enabled() -> bool:
    return bool(config.LLM_BASE_URL)


@lru_cache(maxsize=1)
def client():
    from openai import OpenAI
    return OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=config.LLM_TIMEOUT, max_retries=2)


def _log(purpose: str, model: str, claimant_id, usage, latency_ms: int, ok: bool = True) -> None:
    i = getattr(usage, "prompt_tokens", 0) or 0
    o = getattr(usage, "completion_tokens", 0) or 0
    local = "localhost" in config.LLM_BASE_URL or "127.0.0.1" in config.LLM_BASE_URL or "ollama" in config.LLM_BASE_URL
    cost = 0.0 if local else (i * config.REFERENCE_PRICE_IN + o * config.REFERENCE_PRICE_OUT) / 1e6
    with session_scope() as s:
        s.add(LLMCall(purpose=purpose, model=model, claimant_id=claimant_id, input_tokens=i, output_tokens=o,
                      latency_ms=latency_ms, cost_usd=cost, ok=ok))


def chat(messages: list[dict], purpose: str, claimant_id: int | None = None, tools: list | None = None,
         json_mode: bool = False, temperature: float = 0.0, max_tokens: int = 1200):
    """One chat completion, logged. Returns the message object (content and/or tool_calls).
    max_tokens caps runaway generations (small models occasionally loop) so one page can't stall a batch."""
    kw = {"model": config.CHAT_MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        kw["tools"] = tools
    if json_mode:
        kw["response_format"] = {"type": "json_object"}
    t0 = time.time()
    try:
        r = client().chat.completions.create(**kw)
    except Exception:
        _log(purpose, config.CHAT_MODEL, claimant_id, None, int((time.time() - t0) * 1000), ok=False)
        raise
    _log(purpose, config.CHAT_MODEL, claimant_id, r.usage, int((time.time() - t0) * 1000))
    return r.choices[0].message


def parse_json(text: str) -> dict:
    """Tolerant JSON parse: small local models sometimes wrap JSON in prose or code fences."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.S)
        return json.loads(m.group(0)) if m else {}


# ------------------------------------------------------------------------------------------------ embeddings
_TOKEN = re.compile(r"[a-z0-9]+")


def hash_embed(text: str, dim: int = config.EMBED_DIM) -> list[float]:
    """Offline embedding: signed feature hashing of word unigrams + bigrams, L2-normalised. Keyword-like, but free."""
    v = np.zeros(dim, dtype=np.float32)
    toks = _TOKEN.findall(text.lower())
    for t in toks + [a + "_" + b for a, b in zip(toks, toks[1:])]:
        h = int.from_bytes(hashlib.md5(t.encode()).digest()[:8], "little")
        v[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


def embed(texts: list[str], claimant_id: int | None = None) -> list[list[float]]:
    if config.EMBED_MODEL == "hash" or not llm_enabled():
        return [hash_embed(t) for t in texts]
    t0 = time.time()
    r = client().embeddings.create(model=config.EMBED_MODEL, input=texts)
    _log("embed", config.EMBED_MODEL, claimant_id, r.usage, int((time.time() - t0) * 1000))
    return [d.embedding for d in r.data]
