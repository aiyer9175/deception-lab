"""Model clients. Each returns {"text", "usage", "error"}.

Supported providers (set in config.toml):
  openai            - OpenAI chat completions
  openai_compatible - any OpenAI-style endpoint (OpenRouter, Together, Groq, a local Ollama server...)
  anthropic         - Anthropic Messages API
  gemini            - Google Gemini generateContent
  mock              - simulated sender for testing the pipeline (no API calls, NOT real data)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random

import httpx

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class ProviderError(Exception):
    pass


class FatalProviderError(ProviderError):
    """An error that retrying cannot fix and that will hit every remaining call: spend limit reached
    (OpenAI 429 insufficient_quota, OpenRouter 402) or a bad/expired/under-permissioned key (401/403).
    The runner stops the whole run on this instead of retrying all night."""


class AdaptiveLimiter:
    """Concurrency limit per model that backs off on rate limits: after 3 HTTP 429s in a row the limit
    halves (minimum 1); after every 50 successes it grows by 1, back up to the configured maximum."""

    def __init__(self, n: int, name: str = ""):
        self.max = self.limit = max(1, int(n))
        self.name = name
        self.active = 0
        self._cond = asyncio.Condition()
        self._streak = 0
        self._ok = 0

    async def __aenter__(self):
        async with self._cond:
            await self._cond.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc):
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    def rate_limited(self):
        self._streak += 1
        self._ok = 0
        if self._streak >= 3 and self.limit > 1:
            self.limit = max(1, self.limit // 2)
            self._streak = 0
            print(f"  [{self.name}] repeated rate limits: concurrency lowered to {self.limit}", flush=True)

    def succeeded(self):
        self._streak = 0
        self._ok += 1
        if self._ok >= 50 and self.limit < self.max:
            self.limit += 1
            self._ok = 0


class BaseProvider:
    def __init__(self, cfg: dict, temperature, max_tokens: int):
        self.cfg = cfg
        self.model = cfg.get("model")
        # per-model temperature override; "omit" leaves it unset (some reasoning models reject it)
        t = cfg.get("temperature", temperature)
        self.temperature = None if t == "omit" else t
        self.max_tokens = cfg.get("max_tokens", max_tokens)
        self.timeout = cfg.get("timeout_s", 120)
        self.limiter: AdaptiveLimiter | None = None  # set by run.py

    def _key(self) -> str:
        env = self.cfg.get("api_key_env")
        if not env:
            return ""
        key = os.environ.get(env)
        if not key:
            raise ProviderError(f"environment variable {env} is not set for model {self.cfg['name']}")
        return key

    async def complete(self, client: httpx.AsyncClient, prompt: str, trial: dict) -> dict:
        raise NotImplementedError

    async def _post(self, client, url, headers, payload) -> dict:
        delay = 2.0
        for attempt in range(7):
            try:
                r = await client.post(url, headers=headers, json=payload, timeout=self.timeout)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                err = f"transport: {e!r}"
            else:
                if r.status_code == 200:
                    if self.limiter:
                        self.limiter.succeeded()
                    return r.json()
                err = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code in (401, 402, 403) or (r.status_code == 429 and "insufficient_quota" in r.text):
                    raise FatalProviderError(f"{self.cfg.get('name')}: {err}")
                if r.status_code == 429 and self.limiter:
                    self.limiter.rate_limited()
                if r.status_code not in RETRY_STATUS:
                    raise ProviderError(err)
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 60)
        raise ProviderError(f"gave up after retries: {err}")


class OpenAIProvider(BaseProvider):
    default_base = "https://api.openai.com/v1"
    default_token_param = "max_completion_tokens"

    async def complete(self, client, prompt, trial):
        base = self.cfg.get("base_url", self.default_base).rstrip("/")
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        payload[self.cfg.get("token_param", self.default_token_param)] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        payload.update(self.cfg.get("extra", {}))  # e.g. extra = { reasoning_effort = "low" }
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = await self._post(client, f"{base}/chat/completions", headers, payload)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return {"text": text, "usage": data.get("usage"), "error": None}


class OpenAICompatibleProvider(OpenAIProvider):
    default_token_param = "max_tokens"


class AnthropicProvider(BaseProvider):
    async def complete(self, client, prompt, trial):
        payload = {"model": self.model, "max_tokens": self.max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        headers = {"x-api-key": self._key(), "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        base = self.cfg.get("base_url", "https://api.anthropic.com").rstrip("/")
        data = await self._post(client, f"{base}/v1/messages", headers, payload)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return {"text": text, "usage": data.get("usage"), "error": None}


class GeminiProvider(BaseProvider):
    async def complete(self, client, prompt, trial):
        gen = {"maxOutputTokens": self.max_tokens}
        if self.temperature is not None:
            gen["temperature"] = self.temperature
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
        headers = {"x-goog-api-key": self._key(), "Content-Type": "application/json"}
        base = self.cfg.get("base_url", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        data = await self._post(client, f"{base}/models/{self.model}:generateContent", headers, payload)
        cands = data.get("candidates") or [{}]
        parts = cands[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        return {"text": text, "usage": data.get("usageMetadata"), "error": None}


class MockProvider(BaseProvider):
    """Simulated sender with a known utility model, used only to test the pipeline end to end.

    Lies if  (2*phi - 1)*g - alpha*h - c + noise > 0   (deception arm)
    Selfish if g - alpha*h + noise > 0                 (dictator arm: no lying cost)
    alpha = weight on the receiver's loss, c = cost of lying itself, phi = the sender's belief that the
    receiver follows (lying gains (2*phi - 1)*g over truth), noise ~ Logistic(0, mock_noise).
    True cutoff in the deception arm: g* = (alpha*h + c) / (2*phi - 1). Output is fake by construction.
    """

    async def complete(self, client, prompt, trial):
        await asyncio.sleep(0.001)
        rng = random.Random(trial["trial_id"])
        alpha = self.cfg.get("mock_alpha", 0.5)
        c = self.cfg.get("mock_lie_cost", 1.0)
        phi = self.cfg.get("mock_phi", 1.0)
        noise_scale = self.cfg.get("mock_noise", 2.0)
        bad_rate = self.cfg.get("mock_invalid_rate", 0.02)
        if trial.get("arm") == "preflight":  # comprehension check: answer correctly unless told not to
            ans = dict(trial["answers"])
            if rng.random() < self.cfg.get("mock_preflight_error", 0.0):
                ans["better_for_me"] = "A" if ans["better_for_me"] == "B" else "B"
            return {"text": json.dumps(ans), "usage": None, "error": None}
        if rng.random() < bad_rate:
            return {"text": "I would rather not choose.", "usage": None, "error": None}
        u = rng.random() or 1e-12
        noise = noise_scale * math.log(u / (1 - u))  # logistic noise (one uniform draw)
        g, h = trial["g"], trial["h"]
        if trial["arm"] == "deception":
            lie = (2 * phi - 1) * g - alpha * h - c + noise > 0
            msg = trial["msg_for_lie"] if lie else 3 - trial["msg_for_lie"]
            fp = max(0, min(100, round(100 * phi + rng.uniform(-5, 5))))
            obj = {"reason": "mock", "follow_prob": fp, "message": msg}
        else:
            selfish = g - alpha * h + noise > 0
            obj = {"reason": "mock", "choice": trial["lie_label"] if selfish else trial["truth_label"]}
        return {"text": json.dumps(obj), "usage": None, "error": None}


PROVIDERS = {
    "openai": OpenAIProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "mock": MockProvider,
}


def make_provider(cfg: dict, temperature, max_tokens) -> BaseProvider:
    kind = cfg["provider"]
    if kind not in PROVIDERS:
        raise ValueError(f"unknown provider {kind!r}; choose from {sorted(PROVIDERS)}")
    return PROVIDERS[kind](cfg, temperature, max_tokens)
