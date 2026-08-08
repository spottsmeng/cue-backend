from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.api.admin import router as admin_router
from app.api.ask import router as ask_router
from app.api.budget import router as budget_router
from app.api.channel_types import router as channel_types_router
from app.api.channels import router as channels_router
from app.api.commitments import router as commitments_router
from app.api.consent import router as consent_router
from app.api.deviations import router as deviations_router
from app.api.documents import router as documents_router
from app.api.foresight_admin import quiet_hours_router, threshold_router
from app.api.milestones import router as milestones_router
from app.api.notifications import router as notifications_router
from app.api.parties import router as parties_router
from app.api.projects import router as projects_router
from app.api.reports import router as reports_router
from app.api.retention import router as retention_router
from app.api.risks import router as risks_router
from app.api.twin import router as twin_router
from app.api.webhooks import router as webhooks_router
from app.core.db import get_session

app = FastAPI(title="CUE")
app.include_router(projects_router)
app.include_router(commitments_router)
app.include_router(milestones_router)
app.include_router(twin_router)
app.include_router(budget_router)
app.include_router(channels_router)
app.include_router(channel_types_router)
app.include_router(consent_router)
app.include_router(retention_router)
app.include_router(admin_router)
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


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
