from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.api.commitments import router as commitments_router
from app.api.projects import router as projects_router
from app.core.db import get_session

app = FastAPI(title="CUE")
app.include_router(projects_router)
app.include_router(commitments_router)


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
