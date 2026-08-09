"""app/llm/cost.py — NFR-OBS-03's token/cost accounting. Doesn't test
OllamaClient/AnthropicClient's own HTTP parsing directly, same restraint
tests/test_extractor.py's own docstring establishes ("the live-Ollama path
... is not part of this automated suite on purpose") — this file covers
the genuinely new, infra-free logic: cost estimation and the best-effort,
transaction-safe recording helper.
"""

import uuid

import pytest
from sqlalchemy import select

from app.llm.cost import LLMUsage, estimate_cost_usd, record_llm_usage
from app.llm.models import LLMUsageEvent
from tests.conftest import set_org_context


def test_estimate_cost_usd_for_a_free_local_model():
    assert estimate_cost_usd("qwen2.5:14b", 1000, 500) == 0.0


def test_estimate_cost_usd_for_a_priced_model():
    cost = estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(1.0 + 5.0)


def test_estimate_cost_usd_unknown_model_is_none_not_zero():
    """An unrecognised model reports unknown cost, never a guessed number —
    same 'don't fabricate what you don't have' posture as everywhere else
    in this codebase (e.g. app/foresight/models.py's base_rate)."""
    assert estimate_cost_usd("some-future-model", 100, 100) is None


def test_estimate_cost_usd_missing_token_counts_is_none():
    assert estimate_cost_usd("claude-haiku-4-5", None, 100) is None
    assert estimate_cost_usd("claude-haiku-4-5", 100, None) is None


@pytest.mark.asyncio
async def test_record_llm_usage_writes_a_row(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    usage = LLMUsage(provider="ollama", model="qwen2.5:14b", tokens_in=120, tokens_out=45, estimated_cost_usd=0.0)
    await record_llm_usage(
        app_session, organisation_id=org_id, project_id=project_id,
        role="extraction", purpose="ledger_extraction", usage=usage,
    )
    await app_session.commit()

    row = (
        await app_session.execute(select(LLMUsageEvent).where(LLMUsageEvent.project_id == project_id))
    ).scalar_one()
    assert row.provider == "ollama"
    assert row.tokens_in == 120
    assert row.tokens_out == 45
    assert row.purpose == "ledger_extraction"


@pytest.mark.asyncio
async def test_record_llm_usage_failure_is_swallowed_and_does_not_poison_the_transaction(
    app_session, org_and_project
):
    """A bogus organisation_id violates the FK constraint — record_llm_usage
    must not raise, and critically, the caller's own transaction must still
    be usable afterward (this is what app/llm/cost.py's SAVEPOINT is for:
    without it, the failed INSERT would abort the whole transaction, and
    the ordinary session.execute() below would fail with 'current
    transaction is aborted')."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)

    usage = LLMUsage(provider="ollama", model="qwen2.5:14b", tokens_in=1, tokens_out=1)
    await record_llm_usage(
        app_session, organisation_id=uuid.uuid4(), project_id=project_id,
        role="extraction", purpose="ledger_extraction", usage=usage,
    )  # must not raise

    # The session must still be usable — proves the savepoint rollback
    # didn't leave the outer transaction aborted.
    result = await app_session.execute(select(LLMUsageEvent))
    assert result.scalars().all() == []
    await app_session.commit()
