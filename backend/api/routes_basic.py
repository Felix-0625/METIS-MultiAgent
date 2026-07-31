"""基础路由 - 首页 & 健康检查"""
import asyncio

from fastapi import APIRouter, File, Form
from fastapi.responses import JSONResponse
from core.app_state import projects
from core.database import database_healthcheck

router = APIRouter(tags=["basic"])

@router.get("/")
async def root():
    return {"message": "AI Multi-Agent System", "version": "0.3.0"}

@router.get("/health")
async def health():
    database = await asyncio.to_thread(database_healthcheck)
    payload = {"status": "healthy" if database["healthy"] else "unhealthy", "database": database}
    return payload if database["healthy"] else JSONResponse(status_code=503, content=payload)
