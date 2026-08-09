"""NFR-OBS-03: per-call token/cost accounting for OllamaClient/
AnthropicClient (app/llm/client.py), attributed to project/organisation.

Deliberate substitute for standing up Langfuse (CUE-Tech-Stack.md §2.5's
actual named production choice) this session — Langfuse is a real service
to deploy, configure and operationally own, and that was too heavy for
this pass. The swap-out path, when it's worth it: replace
`record_llm_usage`'s body with a `langfuse.generation(...)` call (Langfuse
already accepts this exact shape — tokens in/out, cost, a project/
organisation-keyed trace), gated the same no-op-when-unconfigured way
app/observability/otel.py gates on `OTEL_EXPORTER_OTLP_ENDPOINT` (a
`CUE_LANGFUSE_PUBLIC_KEY`/`_SECRET_KEY` pair unset -> skip, same posture as
every other missing-credential case in this codebase). The 7 call sites in
app/ledger/extractor.py, app/documents/extractor.py, app/ask/, app/
foresight/contradiction.py and app/writeback/ would need no change at all
for that swap — they only ever see `record_llm_usage`.

Best-effort, fire-and-forget posture: a usage-recording failure must never
break, and must never poison, the actual extraction/ask/write-back call
it's describing — `record_llm_usage` runs its insert in a SAVEPOINT
(`session.begin_nested()`), not directly against the caller's own
transaction, so a failure here rolls back only the usage row, never the
caller's already-good work in the same transaction.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.models import LLMUsageEvent

logger = logging.getLogger("cue.llm.cost")


@dataclass
class LLMUsage:
    provider: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    estimated_cost_usd: float | None = None


# $ per 1M tokens, (input, output). Self-hosted Ollama models cost nothing
# beyond compute already paid for — reported as a real $0.0, not
# "unknown". Anthropic rates below are this session's best-effort figures
# at implementation time (2026-08) — confirm against Anthropic's current
# pricing page before relying on this for real budget reporting. An
# unrecognised model reports estimated_cost_usd=None (genuinely unknown),
# never a guessed number.
_PRICING_PER_1M_TOKENS: dict[str, tuple[float, float]] = {
    "qwen2.5:14b": (0.0, 0.0),
    "qwen2.5:32b": (0.0, 0.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (15.0, 75.0),
}


def estimate_cost_usd(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    rates = _PRICING_PER_1M_TOKENS.get(model)
    if rates is None or tokens_in is None or tokens_out is None:
        return None
    rate_in, rate_out = rates
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out


async def record_llm_usage(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    project_id: uuid.UUID | None,
    role: str,
    purpose: str,
    usage: LLMUsage,
) -> None:
    """Never raises. Runs in its own SAVEPOINT so a failure here (e.g. an
    unexpected FK/constraint issue) can't abort the caller's outer
    transaction — only this insert gets rolled back. Does not commit
    beyond the savepoint; the caller's own eventual commit is what
    persists the row for real, same convention every other service
    function in this codebase follows."""
    try:
        async with session.begin_nested():
            session.add(
                LLMUsageEvent(
                    organisation_id=organisation_id,
                    project_id=project_id,
                    role=role,
                    purpose=purpose,
                    provider=usage.provider,
                    model=usage.model,
                    tokens_in=usage.tokens_in,
                    tokens_out=usage.tokens_out,
                    estimated_cost_usd=usage.estimated_cost_usd,
                )
            )
            await session.flush()
    except Exception:
        logger.exception(
            "failed to record LLM usage (role=%s purpose=%s provider=%s model=%s) — continuing",
            role, purpose, usage.provider, usage.model,
        )
