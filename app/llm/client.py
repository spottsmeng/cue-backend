from typing import Protocol

import httpx

from app.llm.cost import LLMUsage, estimate_cost_usd

# Call shapes mirror cue-eval/run_eval.py's call_ollama/call_anthropic exactly,
# including the Ollama options (num_ctx, temperature) from CLAUDE.md's Models
# table. Deliberately not imported from cue-eval — that script is stdlib-only
# by design so it stays a portable smoke test; this module has real
# dependencies (httpx) already, so it keeps its own copy. Both return the raw
# JSON text the model produced (parsing/Pydantic validation is the caller's
# job, app/ledger/extractor.py, same division cue-eval itself uses) plus an
# LLMUsage (NFR-OBS-03) — both providers' raw responses already carry token
# counts (Ollama's prompt_eval_count/eval_count, Anthropic's usage.*); this
# module used to read the response and discard everything but the text.


class ModelClient(Protocol):
    async def complete(self, prompt: str, schema: dict) -> tuple[str, LLMUsage]: ...


class OllamaClient:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url
        self.model = model

    async def complete(self, prompt: str, schema: dict) -> tuple[str, LLMUsage]:
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
            body = r.json()
            tokens_in = body.get("prompt_eval_count")
            tokens_out = body.get("eval_count")
            usage = LLMUsage(
                provider="ollama",
                model=self.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                estimated_cost_usd=estimate_cost_usd(self.model, tokens_in, tokens_out),
            )
            return body["response"], usage


class AnthropicClient:
    def __init__(self, api_key: str | None, model: str):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self.api_key = api_key
        self.model = model

    async def complete(self, prompt: str, schema: dict) -> tuple[str, LLMUsage]:
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
            text = ""
            for block in out.get("content", []):
                if block.get("type") == "text":
                    text = block["text"]
                    break
            raw_usage = out.get("usage") or {}
            tokens_in = raw_usage.get("input_tokens")
            tokens_out = raw_usage.get("output_tokens")
            usage = LLMUsage(
                provider="anthropic",
                model=self.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                estimated_cost_usd=estimate_cost_usd(self.model, tokens_in, tokens_out),
            )
            return text, usage
