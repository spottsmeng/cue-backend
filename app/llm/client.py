from typing import Protocol

import httpx

# Call shapes mirror cue-eval/run_eval.py's call_ollama/call_anthropic exactly,
# including the Ollama options (num_ctx, temperature) from CLAUDE.md's Models
# table. Deliberately not imported from cue-eval — that script is stdlib-only
# by design so it stays a portable smoke test; this module has real
# dependencies (httpx) already, so it keeps its own copy. Both return the raw
# JSON text the model produced; parsing and Pydantic validation are the
# caller's job (app/ledger/extractor.py), same division cue-eval itself uses.


class ModelClient(Protocol):
    async def complete(self, prompt: str, schema: dict) -> str: ...


class OllamaClient:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url
        self.model = model

    async def complete(self, prompt: str, schema: dict) -> str:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": "2h",
                    "format": schema,
                    "options": {"num_ctx": 16384, "temperature": 0},
                },
            )
            r.raise_for_status()
            return r.json()["response"]


class AnthropicClient:
    def __init__(self, api_key: str | None, model: str):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self.api_key = api_key
        self.model = model

    async def complete(self, prompt: str, schema: dict) -> str:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "content-type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.model,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                    "output_config": {"format": {"type": "json_schema", "schema": schema}},
                },
            )
            r.raise_for_status()
            out = r.json()
            for block in out.get("content", []):
                if block.get("type") == "text":
                    return block["text"]
            return ""
