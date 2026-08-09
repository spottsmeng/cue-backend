"""GET /projects/{project_id}/ontology-terms — read-only discovery of the
valid *_code values for any ontology_terms category (milestone_type,
commitment_act, deviation_class, deliverable_class, vendor_category, phase),
resolved against the calling project's effective three-tier vocabulary
(universal core ∪ vertical pack ∪ tenant extension — CUE-PRD.md §4.2.1).

Added for the frontend (frontend/PROGRESS.md's F0 milestone): every create/
update endpoint that accepts one of these codes (MilestoneCreate.type_code,
CommitmentCreate.act_type, DeviationCreate.class_code, DocumentCreate's/
DocumentTagRequest's class_code/phase_code) assumed the caller already knew
the valid set — there was no way to discover it short of hardcoding
CUE-PRD.md §4.2's term tables client-side, which would silently drift from
this table's own versioned, seedable reality the moment a term is added or
deprecated. Read-only, plain project-membership-gated (same tier
GET /projects/{id}/milestones already uses) — this is reference data any
project member needs to build a form, not an admin action.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_project
from app.api.schemas import OntologyTermOut
from app.core.db import get_session
from app.models import Project
from app.twin.service import list_ontology_terms

router = APIRouter(prefix="/projects/{project_id}/ontology-terms", tags=["ontology"])


@router.get("", response_model=list[OntologyTermOut])
async def read_ontology_terms(
    category: str,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[OntologyTermOut]:
    terms = await list_ontology_terms(session, project, category)
    return [
        OntologyTermOut(code=t.code, label_en=t.label_en, label_zh=t.label_zh, sort_order=t.sort_order)
        for t in terms
    ]
