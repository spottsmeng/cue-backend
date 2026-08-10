import json
import re
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


class FakeClient:
    """CI-only stand-in for a real Ollama/Anthropic ModelClient — no network
    call, no external model, fully deterministic and instant. This
    project's own dev strategy (CLAUDE.md's Models table) scopes Ollama to
    the developer's own local machine only — dev, test, demo — switching to
    a frontier model for production once the app is solid; CI (a remote,
    ephemeral, GPU-less runner) was never meant to be a third place either
    one runs. `FakeClient` is what CI uses instead, so e2e specs can still
    exercise the real backend/DB/UI wiring around a model call (does the UI
    correctly render `available=True` with citations? does it correctly
    render each `refusal_kind` distinctly?) without touching a real model at
    all.

    Mirrors the per-test `FakeModelClient` pattern already established in
    tests/test_extractor.py / tests/test_document_extractor.py (a canned
    response standing in for the real model), generalised here to be
    schema-aware — a real end-to-end run makes many different real calls in
    one session, each needing an appropriately-shaped response for the UI
    assertions exercising it, not one fixed dict.

    Response rules, deliberately simple and legible rather than "smart":
    - FR-ASK-06's intent-classification schema: a small, honest keyword
      check against the *question text only* — extracted from the prompt's
      own trailing "Message: " field, never the prompt's static
      instructional/example text (which itself names "chase the vendor" as
      an example and would otherwise always match). Good enough to drive
      both of Ask's real refusal branches in a test without pretending to
      understand language.
    - Ask's answer-generation schema: an honest word-overlap check between
      the question and each retrieved excerpt, not an unconditional "yes" —
      this schema is requested whenever retrieval found *some* hit
      (app/ask/answer.py's own control flow), but with a fake embedding
      client in play (CUE_EMBED_PROVIDER=fake) semantic search always
      returns its "closest" vector regardless of true relevance, the same
      way real cosine-distance ranking would for a genuinely irrelevant
      question — so "a hit exists" can no longer stand in for "the excerpts
      actually answer this." Citing every excerpt unconditionally would
      make `no_citable_source` untestable; a real word-overlap judgment
      keeps both this schema's honest-refusal path and its grounded-answer
      path meaningfully exercised.
    - Write-back's compose-draft schema: a fixed, valid closed question.
    - Anything else: a type-appropriate minimal stub synthesized directly
      from the schema, so a not-yet-anticipated call never crashes a test
      with a validation error, the same "no path the API can structurally
      never satisfy" discipline this codebase already holds itself to,
      applied to the fake side of that same contract.
    """

    _EXCERPT_BLOCK_RE = re.compile(
        r"\[([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\] \([a-z_]+\): (.+)"
    )
    # app/ledger/supersession.py's own _PROMPT_TEMPLATE: two fixed lines,
    # "Older commitment ...: amount X, due ..." then "Newer commitment
    # ...: amount Y, due ...", in that order.
    _SUPERSESSION_AMOUNT_RE = re.compile(r"amount ([^,]+),")
    _WORD_RE = re.compile(r"[a-z0-9]+")
    # Words too short/common to count as a real topical overlap signal — a
    # bare stopword match ("the", "for") would make almost any two English
    # sentences look "related", defeating the whole point of the check.
    _STOPWORDS = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "this", "that", "these", "those",
        "what", "when", "where", "which", "who", "with", "from", "about", "and", "for",
    })
    _ACTION_KEYWORDS = (
        "chase", "reschedule", "draft a message", "draft an update",
        "tell the client", "tell the vendor", "confirm the date", "confirm this with",
    )

    def __init__(self, model: str = "fake"):
        self.model = model

    async def complete(self, prompt: str, schema: dict) -> tuple[str, LLMUsage]:
        required = set(schema.get("required", []))
        if required == {"is_action_request", "action_summary"}:
            response = self._fake_intent(prompt)
        elif required == {"has_support", "answer", "citation_source_ids"}:
            response = self._fake_ask_answer(prompt)
        elif required == {"question"} and set(schema.get("properties", {})) == {"question"}:
            response = {"question": "Can you confirm this is still on track?"}
        elif required == {"supersedes", "reasoning"}:
            response = self._fake_supersession(prompt)
        else:
            response = _synthesize_from_schema(schema)
        usage = LLMUsage(provider="fake", model=self.model, tokens_in=0, tokens_out=0, estimated_cost_usd=0.0)
        return json.dumps(response), usage

    def _fake_intent(self, prompt: str) -> dict:
        question = prompt.rsplit("Message: ", 1)[-1].strip().lower()
        is_action = any(keyword in question for keyword in self._ACTION_KEYWORDS)
        return {
            "is_action_request": is_action,
            "action_summary": "take the requested outbound action" if is_action else None,
        }

    def _fake_ask_answer(self, prompt: str) -> dict:
        question = prompt.rsplit("Question: ", 1)[-1]
        question_words = self._significant_words(question)

        relevant_ids = [
            excerpt_id
            for excerpt_id, text in self._EXCERPT_BLOCK_RE.findall(prompt)
            if question_words & self._significant_words(text)
        ]

        if not relevant_ids:
            return {"has_support": False, "answer": None, "citation_source_ids": []}
        return {
            "has_support": True,
            "answer": f"[fake model] grounded in {len(relevant_ids)} retrieved source excerpt(s).",
            "citation_source_ids": relevant_ids,
        }

    def _fake_supersession(self, prompt: str) -> dict:
        """FR-LED-05's candidate-proposal schema (app/ledger/supersession.py).
        An honest, if simple, judgment — not an unconditional "yes" — same
        discipline `_fake_ask_answer` above already establishes for the same
        reason: the real caller (`find_candidate_priors`) only ever asks
        this question about two commitments that already share a party and
        an exact deliverable name, so an unconditional "yes" here would
        never actually exercise this feature's own "reject, these are
        unrelated" path in a CI-driven e2e spec. The two amounts are parsed
        straight from the prompt's own fixed template (both call sites
        format it identically); genuinely differing means "looks like a
        revision," identical or unparseable means "no signal either way,"
        the same fallback-to-false posture `_synthesize_value` already takes
        for an unrecognised boolean field.
        """
        amounts = self._SUPERSESSION_AMOUNT_RE.findall(prompt)
        if len(amounts) == 2 and amounts[0] != amounts[1]:
            return {
                "supersedes": True,
                "reasoning": (
                    f"[fake model] same deliverable, amount changed from {amounts[0]} to {amounts[1]}."
                ),
            }
        return {
            "supersedes": False,
            "reasoning": "[fake model] no material difference found between the two commitments.",
        }

    def _significant_words(self, text: str) -> set[str]:
        return {w for w in self._WORD_RE.findall(text.lower()) if len(w) > 3 and w not in self._STOPWORDS}


def _synthesize_from_schema(schema: dict) -> dict:
    """A minimal, type-conformant JSON object for any schema this fake
    client hasn't been taught to recognise by name — never a crash, never a
    validation error, just an honest placeholder."""
    return {name: _synthesize_value(spec) for name, spec in schema.get("properties", {}).items()}


def _synthesize_value(spec: dict):
    raw_type = spec.get("type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if "null" in types:
        return None
    if "boolean" in types:
        return False
    if "array" in types:
        return []
    if "object" in types:
        return {}
    if "integer" in types or "number" in types:
        return 0
    return ""
