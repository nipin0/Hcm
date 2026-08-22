"""Lightweight DeepSeek (OpenAI-compatible) chat client — shared across services.

Why a new module (not reusing hcm-market-intel's AiScorer):
- AiScorer lives in a different container/package (market-intel) and is wired for
  macro/sentiment/event *scoring* (numeric score in a fixed band). This client is
  a generic chat-completion wrapper used by signal-tower for *calibration diagnosis*
  (natural-language narrative), so it must be importable from the shared package.
- Same transport pattern as AiScorer (httpx async, retries, timeout, stub fallback)
  but config-agnostic: the caller supplies api_key/base_url/model; if absent the
  client reports unavailable and the caller falls back to its deterministic output.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_TOKENS = 900
DEFAULT_TEMPERATURE = 0.3


class DeepSeekClient:
    """Minimal OpenAI-compatible chat client for DeepSeek.

    Usage:
        client = DeepSeekClient(api_key, base_url, model)
        if client.is_available:
            text = await client.complete(system, user)
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = (base_url or os.getenv("DEEPSEEK_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Return the assistant message content (raw text). Raises on failure."""
        if not self.is_available:
            raise RuntimeError("DeepSeek API key not configured")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


def parse_json_block(raw: str) -> dict | None:
    """Best-effort parse of a model response that should be JSON.

    Handles leading/trailing prose and optional ```json fences.
    Returns None on failure so callers can fall back gracefully.
    """
    if not raw:
        return None
    text = raw.strip()
    # strip markdown fences if present
    if "```" in text:
        start = text.find("```")
        end = text.find("```", start + 3)
        seg = text[start + 3 : end] if end != -1 else text[start + 3 :]
        seg = seg.strip()
        if seg.lower().startswith("json"):
            seg = seg[4:].strip()
        text = seg
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # try to locate the first { ... } span
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(text[s : e + 1])
            except json.JSONDecodeError:
                return None
        return None
