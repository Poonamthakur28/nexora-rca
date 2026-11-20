"""LLM provider adapters behind one small interface. Plain HTTP (httpx), no vendor SDKs, so switching
providers is configuration only: RCA_PROVIDER, RCA_MODEL and the provider's key or URL."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import Settings

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4.1-mini",
    "ollama": "llama3.1:8b",
}


class ProviderError(Exception):
    """The provider could not be reached or rejected the request. Message is safe to show (no keys)."""


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMClient:
    name = "base"

    def __init__(self, settings: Settings, http: httpx.Client):
        self.settings = settings
        self.http = http
        self.model = settings.rca_model or DEFAULT_MODELS.get(self.name, "")

    def complete(self, system: str, user: str) -> LLMResult:  # pragma: no cover - interface
        raise NotImplementedError

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        try:
            r = self.http.post(url, headers=headers, json=body, timeout=self.settings.rca_timeout_seconds)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} unreachable: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            detail = r.text[:300].replace("\n", " ")
            raise ProviderError(f"{self.name} returned HTTP {r.status_code}: {detail}")
        return r.json()


class AnthropicClient(LLMClient):
    name = "anthropic"

    def complete(self, system: str, user: str) -> LLMResult:
        if not self.settings.anthropic_api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        data = self._post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.settings.anthropic_api_key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": self.settings.rca_max_output_tokens, "system": system,
             "messages": [{"role": "user", "content": user}]},
        )
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage") or {}
        return LLMResult(text, data.get("model", self.model), usage.get("input_tokens"), usage.get("output_tokens"))


class OpenAIClient(LLMClient):
    name = "openai"

    def complete(self, system: str, user: str) -> LLMResult:
        if not self.settings.openai_api_key:
            raise ProviderError("OPENAI_API_KEY is not set")
        data = self._post(
            f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {self.settings.openai_api_key}"},
            {"model": self.model, "max_tokens": self.settings.rca_max_output_tokens,
             "response_format": {"type": "json_object"},
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        )
        choices = data.get("choices") or [{}]
        usage = data.get("usage") or {}
        return LLMResult((choices[0].get("message") or {}).get("content") or "", data.get("model", self.model),
                         usage.get("prompt_tokens"), usage.get("completion_tokens"))


class OllamaClient(LLMClient):
    name = "ollama"

    def complete(self, system: str, user: str) -> LLMResult:
        data = self._post(
            f"{self.settings.ollama_url.rstrip('/')}/api/chat", {},
            {"model": self.model, "stream": False, "format": "json",
             "options": {"temperature": 0, "num_predict": self.settings.rca_max_output_tokens},
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        )
        return LLMResult((data.get("message") or {}).get("content") or "", data.get("model", self.model),
                         data.get("prompt_eval_count"), data.get("eval_count"))


CLIENTS = {c.name: c for c in (AnthropicClient, OpenAIClient, OllamaClient)}
PROVIDERS = ("heuristic", *CLIENTS)


def make_client(settings: Settings, http: httpx.Client) -> LLMClient:
    try:
        return CLIENTS[settings.rca_provider](settings, http)
    except KeyError:
        raise ProviderError(f"unknown RCA_PROVIDER '{settings.rca_provider}' (use one of {', '.join(PROVIDERS)})")
