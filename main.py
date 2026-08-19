from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.api.admin import router as admin_router
from app.api.ask import router as ask_router
from app.api.auth import router as auth_router
from app.api.budget import router as budget_router
from app.api.channel_types import router as channel_types_router
from app.api.channels import router as channels_router
from app.api.commitments import router as commitments_router
from app.api.supersession import router as supersession_router
from app.api.consent import router as consent_router
from app.api.deviations import router as deviations_router
from app.api.documents import router as documents_router
from app.api.foresight_admin import quiet_hours_router, threshold_router
from app.api.identities import router as identities_router
from app.api.layer_a_admin import router as layer_a_admin_router
from app.api.milestones import router as milestones_router
from app.api.notifications import router as notifications_router
from app.api.ontology import router as ontology_router
from app.api.parties import list_router as parties_list_router
from app.api.parties import organisation_router as party_organisation_router
from app.api.parties import router as parties_router
from app.api.projects import router as projects_router
from app.api.reports import router as reports_router
from app.api.retention import router as retention_router
from app.api.risks import router as risks_router
from app.api.twin import router as twin_router
from app.api.users import router as users_router
from app.api.webhooks import router as webhooks_router
from app.api.writeback import router as writeback_router
from app.core.config import get_settings
from app.core.db import engine, get_session
from app.observability.otel import configure_otel

app = FastAPI(title="CUE")
configure_otel("cue-api", app=app, engine=engine)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(ontology_router)
app.include_router(parties_list_router)
app.include_router(projects_router)
app.include_router(supersession_router)
app.include_router(commitments_router)
app.include_router(milestones_router)
app.include_router(twin_router)
app.include_router(budget_router)
app.include_router(channels_router)
app.include_router(channel_types_router)
app.include_router(consent_router)
app.include_router(retention_router)
app.include_router(admin_router)
app.include_router(layer_a_admin_router)
app.include_router(documents_router)
app.include_router(risks_router)
app.include_router(deviations_router)
app.include_router(notifications_router)
app.include_router(webhooks_router)
app.include_router(threshold_router)
app.include_router(quiet_hours_router)
app.include_router(reports_router)
app.include_router(ask_router)
app.include_router(parties_router)
app.include_router(party_organisation_router)
app.include_router(identities_router)
app.include_router(writeback_router)
app.include_router(users_router)


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
