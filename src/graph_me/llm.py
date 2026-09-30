"""AI models for Tier 1 labelling. Every adapter returns JSON matching a given schema.

Providers (``extraction.medium.llm.provider`` in config.yaml):

- ``agent``: no model call here; graph-me writes batch files for the calling agent (Claude
  Code) to fill in, then ``graph-me ingest`` validates them (pipeline/enrich.py).
- ``ollama``: a local model through Ollama's HTTP API (nothing leaves the computer).
- ``anthropic``: Claude through the official ``anthropic`` SDK (key from ANTHROPIC_API_KEY,
  ANTHROPIC_AUTH_TOKEN or an ``ant auth login`` profile).
- ``openai_compat``: any OpenAI-compatible chat server (OpenAI, LM Studio, vLLM, llama.cpp...).

Whatever a model answers is untrusted: callers validate it strictly (tier1/labels.py).
"""

from __future__ import annotations

import json
import os
import re
from typing import Protocol

from graph_me.config import LLMConfig

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "ollama": "qwen3:4b"}
REMOTE = {"anthropic", "openai_compat"}  # providers that may send data off this computer
TIMEOUT = 120.0


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str  # recorded in evidence as "llm:<name>"

    def complete_json(self, system: str, user: str, schema: dict) -> dict: ...


def _first_json_object(text: str) -> dict:
    """Parse a JSON object, tolerating prose or code fences around it."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise LLMError("the model did not return JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"the model returned invalid JSON: {exc}") from exc


class OllamaLLM:
    def __init__(self, cfg: LLMConfig, transport=None) -> None:
        import httpx

        self.model = cfg.model or DEFAULT_MODELS["ollama"]
        self.name = f"ollama:{self.model}"
        base = (cfg.base_url or "http://localhost:11434").rstrip("/")
        self.client = httpx.Client(base_url=base, timeout=TIMEOUT, transport=transport)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import httpx

        try:
            response = self.client.post(
                "/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "format": schema,  # Ollama constrains the output to this JSON schema
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama at {self.client.base_url}: {exc}") from exc
        return _first_json_object(response.json()["message"]["content"])


class OpenAICompatLLM:
    def __init__(self, cfg: LLMConfig, transport=None) -> None:
        import httpx

        if not cfg.model:
            raise LLMError("openai_compat needs `model:` in extraction.medium.llm")
        self.model = cfg.model
        self.name = f"openai_compat:{self.model}"
        key = os.environ.get(cfg.api_key_env or "OPENAI_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        self.client = httpx.Client(
            base_url=base, timeout=TIMEOUT, headers=headers, transport=transport
        )

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import httpx

        try:
            response = self.client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "labels", "schema": schema, "strict": True},
                    },
                    "temperature": 0,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.client.base_url}: {exc}") from exc
        return _first_json_object(response.json()["choices"][0]["message"]["content"])


class AnthropicLLM:
    """Claude via the official SDK, with structured output and the default refusal fallback."""

    def __init__(self, cfg: LLMConfig, client=None) -> None:
        self.model = cfg.model or DEFAULT_MODELS["anthropic"]
        self.name = f"anthropic:{self.model}"
        if client is None:
            import anthropic

            api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.client = client

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import anthropic

        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                # Labelling short strings is simple work: low effort keeps it fast and cheap.
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
                # On a policy decline, the API retries on a suitable fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"cannot reach the Claude API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this batch")
        if response.stop_reason == "max_tokens":
            raise LLMError("the answer was cut off; use a smaller --batch-size")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return _first_json_object(text)


_registered: dict[str, type] = {}


def register(name: str, cls: type) -> None:
    """Register an adapter class in-process (tests, embedding)."""
    _registered[name] = cls


def create(cfg: LLMConfig) -> LLM:
    if cfg.provider in _registered:
        return _registered[cfg.provider](cfg)
    if cfg.provider == "ollama":
        return OllamaLLM(cfg)
    if cfg.provider == "openai_compat":
        return OpenAICompatLLM(cfg)
    if cfg.provider == "anthropic":
        return AnthropicLLM(cfg)
    raise LLMError(f"provider {cfg.provider!r} has no model to call (agent mode uses batch files)")
