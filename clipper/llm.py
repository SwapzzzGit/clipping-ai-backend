"""Tiny provider-agnostic LLM wrapper.

Select the backend with env vars (they override config.yaml):
  LLM_PROVIDER  gemini | anthropic | openai | mock
  LLM_MODEL     model id (defaults below)
  LLM_API_KEY   API key (falls back to GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY)
  LLM_BASE_URL  optional base URL (e.g. http://localhost:11434/v1 for Ollama)

Uses plain HTTP so no provider SDK is required. The `mock` provider makes no network
calls: it returns whatever the caller's `fallback` produces, for free dry runs and tests.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from clipper.util import ClipperError, log

DEFAULT_MODELS = {
    "gemini": "gemini-flash-lite-latest",
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-5-mini",
    "mock": "mock",
}
KEY_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}


class LLM:
    def __init__(self, cfg: dict, overrides: dict | None = None):
        """`overrides` (provider/model/base_url/api_key, e.g. from the web app's settings) beat env vars."""
        o = {k: v for k, v in (overrides or {}).items() if v}
        self.provider = (o.get("provider") or os.environ.get("LLM_PROVIDER") or cfg.get("provider") or "gemini").lower()
        if self.provider not in DEFAULT_MODELS:
            raise ClipperError(f"Unknown LLM_PROVIDER {self.provider!r} (use {', '.join(DEFAULT_MODELS)})")
        self.model = o.get("model") or os.environ.get("LLM_MODEL") or cfg.get("model") or DEFAULT_MODELS[self.provider]
        self.base_url = o.get("base_url") or os.environ.get("LLM_BASE_URL") or cfg.get("base_url")
        self.api_key = (o.get("api_key") or os.environ.get("LLM_API_KEY")
                        or os.environ.get(KEY_ENV.get(self.provider, ""), ""))
        self.temperature = cfg.get("temperature", 0.2)
        self.max_tokens = cfg.get("max_tokens", 4096)
        self.timeout = cfg.get("timeout_sec", 120)
        if self.provider not in ("mock", "openai") and not self.api_key:
            raise ClipperError(
                f"No API key for {self.provider}. Set LLM_API_KEY or {KEY_ENV[self.provider]}."
            )
        self._lock = threading.Lock()
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    def complete(self, system: str, user: str, fallback: Callable[[], str] | None = None) -> str:
        """Return the model's text reply. `fallback` supplies the reply for the mock provider."""
        if self.provider == "mock":
            if fallback is None:
                raise ClipperError("mock LLM provider needs a fallback")
            self._count(0, 0)
            return fallback()
        call = {"gemini": self._gemini, "anthropic": self._anthropic, "openai": self._openai}[self.provider]
        return call(system, user)

    # -- providers ---------------------------------------------------------------

    def _gemini(self, system: str, user: str) -> str:
        base = self.base_url or "https://generativelanguage.googleapis.com/v1beta"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
                "responseMimeType": "application/json",
            },
        }
        data = self._post(f"{base}/models/{self.model}:generateContent", body, {"x-goog-api-key": self.api_key})
        usage = data.get("usageMetadata", {})
        self._count(usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            raise ValueError(f"empty Gemini response: {json.dumps(data)[:300]}")
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    def _anthropic(self, system: str, user: str) -> str:
        base = self.base_url or "https://api.anthropic.com"
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        data = self._post(f"{base}/v1/messages", body, headers)
        usage = data.get("usage", {})
        self._count(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    def _openai(self, system: str, user: str) -> str:
        base = self.base_url or "https://api.openai.com/v1"
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._post(f"{base}/chat/completions", body, headers)
        usage = data.get("usage", {})
        self._count(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return data["choices"][0]["message"]["content"] or ""

    # -- plumbing ----------------------------------------------------------------

    def _post(self, url: str, body: dict, headers: dict, attempts: int = 5) -> dict:
        payload = json.dumps(body).encode()
        for attempt in range(attempts):
            req = urllib.request.Request(
                url, data=payload, method="POST", headers={"content-type": "application/json", **headers}
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code in RETRY_STATUS and attempt < attempts - 1:
                    wait = _retry_after(e) or min(60, 3 * 2**attempt)
                    log.warning("LLM HTTP %s, retrying in %ss", e.code, wait)
                    time.sleep(wait)
                    continue
                raise ClipperError(f"LLM request failed ({self.name}): HTTP {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < attempts - 1:
                    time.sleep(min(60, 3 * 2**attempt))
                    continue
                raise ClipperError(f"LLM request failed ({self.name}): {e}")
        raise AssertionError("unreachable")

    def _count(self, inp: int, out: int) -> None:
        with self._lock:
            self.usage["calls"] += 1
            self.usage["input_tokens"] += inp or 0
            self.usage["output_tokens"] += out or 0


def _retry_after(err: urllib.error.HTTPError) -> float | None:
    try:
        return min(60.0, float(err.headers.get("retry-after")))
    except (TypeError, ValueError):
        return None
