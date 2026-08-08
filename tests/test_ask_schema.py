"""app/ask/schema.py's AskAnswerOut — FR-ASK-02's 'no source, no assertion'
rule and FR-ASK-06's 'no faked action' rule made structural, not
conventional: Pydantic itself refuses to construct an available=True answer
with no citations or a refusal_kind set, or an available=False answer that
carries prose or omits a refusal_kind.
"""

import uuid

import pytest
from pydantic import ValidationError

from app.ask.schema import AskAnswerOut, Citation


def test_available_answer_without_citations_is_rejected():
    with pytest.raises(ValidationError, match="at least one citation"):
        AskAnswerOut(available=True, answer="some answer", citations=[], conversation_id=uuid.uuid4())


def test_available_answer_with_refusal_kind_is_rejected():
    with pytest.raises(ValidationError, match="must not carry a refusal_kind"):
        AskAnswerOut(
            available=True, answer="some answer",
            citations=[Citation(source_type="commitment", source_id=uuid.uuid4())],
            refusal_kind="no_citable_source", conversation_id=uuid.uuid4(),
        )


def test_unavailable_answer_with_prose_is_rejected():
    with pytest.raises(ValidationError, match="must not carry prose"):
        AskAnswerOut(
            available=False, answer="some answer", refusal_kind="no_citable_source",
            conversation_id=uuid.uuid4(),
        )


def test_unavailable_answer_without_refusal_kind_is_rejected():
    with pytest.raises(ValidationError, match="must carry a refusal_kind"):
        AskAnswerOut(available=False, answer=None, conversation_id=uuid.uuid4())


def test_valid_available_answer_constructs():
    out = AskAnswerOut(
        available=True, answer="LED screens confirmed.",
        citations=[Citation(source_type="commitment", source_id=uuid.uuid4())],
        conversation_id=uuid.uuid4(),
    )
    assert out.available is True
    assert out.refusal_kind is None


def test_valid_unavailable_answer_constructs():
    out = AskAnswerOut(
        available=False, answer=None, unavailable_reason="no source found",
        refusal_kind="no_citable_source", conversation_id=uuid.uuid4(),
    )
    assert out.available is False


def test_valid_action_refusal_constructs():
    out = AskAnswerOut(
        available=False, answer=None, unavailable_reason="CUE can't do that yet",
        refusal_kind="action_not_yet_supported", conversation_id=uuid.uuid4(),
    )
    assert out.refusal_kind == "action_not_yet_supported"
