"""Codifies the NULLS NOT DISTINCT fix (CUE-Tech-Stack.md §5) caught during
this build: without it, Postgres treats NULL != NULL, so two identical
universal-core ontology terms (vertical_id AND organisation_id both NULL)
would NOT violate a plain UNIQUE(category, code, vertical_id, organisation_id)
constraint — silently allowing duplicate 'commitment_act'/'confirm' rows.
This was reasoned through and fixed, but never had an automated regression
test until now.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import OntologyTerm


def _term(**overrides):
    defaults = dict(
        category="test_category",
        code="test_code",
        label_en="Test",
        label_zh="测试",
        active=True,
        effective_from=datetime.now(timezone.utc),
        version=1,
    )
    defaults.update(overrides)
    return OntologyTerm(**defaults)


@pytest.mark.asyncio
async def test_duplicate_universal_core_term_is_rejected(owner_session):
    owner_session.add(_term())
    await owner_session.commit()

    owner_session.add(_term())  # same category/code, vertical_id and organisation_id both NULL
    with pytest.raises(IntegrityError):
        await owner_session.commit()
    await owner_session.rollback()


@pytest.mark.asyncio
async def test_same_code_different_vertical_is_allowed(owner_session, seeded_vertical_id):
    """The constraint must not be over-restrictive: the same code is legitimate
    across two different verticals (or one universal + one vertical-scoped) —
    they aren't the same term."""
    owner_session.add(_term(vertical_id=None))
    owner_session.add(_term(vertical_id=seeded_vertical_id))
    await owner_session.commit()  # must not raise
