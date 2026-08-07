from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.api.admin import router as admin_router
from app.api.budget import router as budget_router
from app.api.channels import router as channels_router
from app.api.commitments import router as commitments_router
from app.api.consent import router as consent_router
from app.api.milestones import router as milestones_router
from app.api.projects import router as projects_router
from app.api.retention import router as retention_router
from app.api.twin import router as twin_router
from app.core.db import get_session

app = FastAPI(title="CUE")
app.include_router(projects_router)
app.include_router(commitments_router)
app.include_router(milestones_router)
app.include_router(twin_router)
app.include_router(budget_router)
app.include_router(channels_router)
app.include_router(consent_router)
app.include_router(retention_router)
app.include_router(admin_router)


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
