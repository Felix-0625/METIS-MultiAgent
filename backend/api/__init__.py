"""API 路由"""
from fastapi import APIRouter

from .routes_basic import router as routes_basic_router
from .routes_projects import router as routes_projects_router
from .routes_pm import router as routes_pm_router
from .routes_hr import router as routes_hr_router
from .routes_subprojects import router as routes_subprojects_router
from .routes_supervisor import router as routes_supervisor_router
from .routes_repair import router as routes_repair_router
from .routes_employee import router as routes_employee_router
from .routes_config import router as routes_config_router
from .routes_files import router as routes_files_router
from .routes_chat import router as routes_chat_router
from .routes_data import router as routes_data_router
from .routes_gitee import router as routes_gitee_router
from .routes_execution import router as routes_execution_router
from .routes_mcp import router as routes_mcp_router
from .routes_skills import router as routes_skills_router
from .routes_phases import router as routes_phases_router
from .routes_experts import router as routes_experts_router
from .routes_team import router as routes_team_router
from .routes_engineer import router as routes_engineer_router
from .routes_adjustments import router as routes_adjustments_router
from .routes_metrics import router as routes_metrics_router
from .routes_misc import router as routes_misc_router
from .routes_idea import router as routes_idea_router
from .routes_auth import router as routes_auth_router
from .websocket import router as websocket_router
from .routes_reliability import router as routes_reliability_router
from .routes_dashboard import router as routes_dashboard_router

def register_routers(app):
    app.include_router(routes_auth_router)      # 认证路由（无需鉴权）
    app.include_router(routes_basic_router)
    app.include_router(routes_projects_router)
    app.include_router(routes_pm_router)
    app.include_router(routes_hr_router)
    app.include_router(routes_subprojects_router)
    app.include_router(routes_supervisor_router)
    app.include_router(routes_repair_router)
    app.include_router(routes_employee_router)
    app.include_router(routes_config_router)
    app.include_router(routes_files_router)
    app.include_router(routes_chat_router)
    app.include_router(routes_data_router)
    app.include_router(routes_gitee_router)
    app.include_router(routes_execution_router)
    app.include_router(routes_mcp_router)
    app.include_router(routes_skills_router)
    app.include_router(routes_phases_router)
    app.include_router(routes_experts_router)
    app.include_router(routes_team_router)
    app.include_router(routes_engineer_router)
    app.include_router(routes_adjustments_router)
    app.include_router(routes_metrics_router)
    app.include_router(routes_misc_router)
    app.include_router(routes_idea_router)
    app.include_router(websocket_router)
    app.include_router(routes_reliability_router)
    app.include_router(routes_dashboard_router)
