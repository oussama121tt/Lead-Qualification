"""LLM provider abstraction — the email generator talks to the interface,
never to a specific vendor. Switching from Groq to Anthropic is a single
.env change (EMAIL_LLM_PROVIDER=anthropic), no business code to rewrite.
"""
import json
import os
from abc import ABC, abstractmethod

GROQ_TIMEOUT_SECONDS = 90  # same budget as scorer.py: a stalled call must not block a batch forever


class LLMProvider(ABC):
    @abstractmethod
    def generate_json(self, prompt: str) -> dict:
        """Sends the prompt, returns the parsed JSON of the response."""
        ...


class GroqProvider(LLMProvider):
    def __init__(self):
        from groq import Groq

        self.client = Groq(api_key=os.environ["GROQ_API_KEY"])
        self.model = "llama-3.3-70b-versatile"

    def generate_json(self, prompt: str) -> dict:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=GROQ_TIMEOUT_SECONDS,
        )
        return json.loads(response.choices[0].message.content)


class AnthropicProvider(LLMProvider):
    def __init__(self):
        from anthropic import Anthropic

        self.client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = "claude-sonnet-4-6"

    def generate_json(self, prompt: str) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(response.content[0].text)


def get_llm_provider() -> LLMProvider:
    provider = os.environ.get("EMAIL_LLM_PROVIDER", "groq")
    if provider == "anthropic":
        return AnthropicProvider()
    return GroqProvider()