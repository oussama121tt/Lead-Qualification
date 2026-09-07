"""LLM provider abstraction — scoring AND email generation talk to the
interface, never to a specific vendor. Switching either flow between Groq and
Anthropic (Claude) is a single .env change, no business code to rewrite:

    SCORING_LLM_PROVIDER=groq|anthropic   (default groq)
    EMAIL_LLM_PROVIDER=groq|anthropic     (default groq)

Every call returns (data, meta): the parsed JSON dict plus a meta dict with
{provider, model, tokens_in, tokens_out} — callers hand meta to costlog so
every LLM call in the system is logged and budget-checked (FR-7). Token counts
come from the provider's own usage report, never estimated.
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod

GROQ_TIMEOUT_SECONDS = 90  # a stalled call must not block a batch forever

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse_json_text(text: str) -> dict:
    """Parses model output as JSON, tolerating markdown code fences."""
    cleaned = _FENCE_RE.sub("", (text or "").strip())
    return json.loads(cleaned)


class LLMProvider(ABC):
    name: str = "?"
    model: str = "?"

    @abstractmethod
    def generate_json(self, prompt: str, *, system: str | None = None,
                      temperature: float | None = None,
                      max_tokens: int = 1024) -> tuple[dict, dict]:
        """Sends the prompt, returns (parsed JSON dict, usage meta dict)."""
        ...

    def _meta(self, tokens_in: int, tokens_out: int) -> dict:
        return {"provider": self.name, "model": self.model,
                "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0)}


def load_groq_keys() -> list[str]:
    """GROQ_API_KEY plus GROQ_API_KEY1/2/... and GROQ_API_KEY_2/_3... — the
    free tier is limited PER KEY (8k tokens/min, 1k requests/day), so several
    keys multiply throughput exactly like the SGAI ring."""
    found = []
    for name, val in sorted(os.environ.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if re.fullmatch(r"GROQ_API_KEY_?\d*", name) and val and val.strip():
            found.append(val.strip())
    seen, out = set(), []
    for k in found:
        if k not in seen:
            seen.add(k); out.append(k)
    return out


class RateLimited(RuntimeError):
    """Raised when every Groq key is rate-limited after backoff. Callers must
    treat this as a RETRYABLE failure (SCORE_FAILED), never as a verdict."""


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self):
        from openai import OpenAI

        self.keys = load_groq_keys()
        if not self.keys:
            raise RuntimeError("no GROQ_API_KEY configured")
        self.clients = [OpenAI(api_key=k, base_url="https://api.groq.com/openai/v1") for k in self.keys]
        self._rr = 0
        self.model = os.getenv("GROQ_SCORING_MODEL") or os.getenv("GROQ_EMAIL_MODEL") or "openai/gpt-oss-120b"

    @staticmethod
    def _is_rate_limit(e: Exception) -> bool:
        status = getattr(e, "status_code", None)
        msg = str(e).lower()
        return status == 429 or "rate_limit" in msg or "rate limit" in msg or "tokens per minute" in msg

    @staticmethod
    def _retry_after(e: Exception, default: float) -> float:
        # Groq returns "Please try again in 12.3s" in the message body.
        m = re.search(r"try again in ([\d.]+)\s*s", str(e))
        if m:
            try:
                return min(float(m.group(1)) + 0.5, 90.0)
            except ValueError:
                pass
        return default

    def generate_json(self, prompt: str, *, system: str | None = None,
                      temperature: float | None = None,
                      max_tokens: int = 1024) -> tuple[dict, dict]:
        import time as _time
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Rotate keys per call; on 429 move to the next key immediately, and
        # only sleep once every key has been tried in this round. Up to 4
        # rounds (~a few minutes worst case) before giving up as RateLimited.
        n = len(self.clients)
        last_err = None
        for round_ in range(4):
            wait = 0.0
            for step in range(n):
                idx = (self._rr + step) % n
                client = self.clients[idx]
                try:
                    response = client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=temperature if temperature is not None else 0.2,
                        max_tokens=max_tokens,
                        response_format={"type": "json_object"},
                        timeout=GROQ_TIMEOUT_SECONDS,
                    )
                    self._rr = (idx + 1) % n
                    usage = getattr(response, "usage", None)
                    meta = self._meta(getattr(usage, "prompt_tokens", 0),
                                      getattr(usage, "completion_tokens", 0))
                    meta["key_index"] = idx
                    return json.loads(response.choices[0].message.content), meta
                except Exception as e:
                    if not self._is_rate_limit(e):
                        raise
                    last_err = e
                    wait = max(wait, self._retry_after(e, 15.0 * (round_ + 1)))
                    continue
            _time.sleep(wait or 15.0)
        raise RateLimited(f"all {n} Groq key(s) rate-limited after backoff: {str(last_err)[:160]}")


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self):
        from anthropic import Anthropic

        self.client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        # Model named by the original spec (FR: cost/quality balance).
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def generate_json(self, prompt: str, *, system: str | None = None,
                      temperature: float | None = None,
                      max_tokens: int = 1024) -> tuple[dict, dict]:
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = self.client.messages.create(**kwargs)
        text = next((b.text for b in response.content if b.type == "text"), "")
        usage = getattr(response, "usage", None)
        meta = self._meta(getattr(usage, "input_tokens", 0),
                          getattr(usage, "output_tokens", 0))
        return _parse_json_text(text), meta


_PROVIDER_ENV = {
    "email": "EMAIL_LLM_PROVIDER",
    "scoring": "SCORING_LLM_PROVIDER",
}

_instances: dict[str, LLMProvider] = {}


def get_llm_provider(purpose: str = "email") -> LLMProvider:
    """Provider for a given purpose ('email' or 'scoring'), cached per name."""
    env_var = _PROVIDER_ENV.get(purpose, "EMAIL_LLM_PROVIDER")
    name = os.environ.get(env_var, "groq").strip().lower()
    if name not in ("groq", "anthropic"):
        name = "groq"
    if name not in _instances:
        _instances[name] = AnthropicProvider() if name == "anthropic" else GroqProvider()
    return _instances[name]
