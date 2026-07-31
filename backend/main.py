"""MeTis 后端入口 - 路由 + 全局异常处理器"""
from pathlib import Path
from dotenv import load_dotenv

# main.py runs from backend/, while the project .env is one level above.
# Load it before app_state imports HermesClient.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import uvicorn
import logging
import asyncio
from fastapi import Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

# 必须在导入任何依赖数据库的模块之前初始化数据库
from core.database import database_healthcheck, init_db
init_db()

from core.app_state import app
from api import register_routers

register_routers(app)


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    components["securitySchemes"]["CookieAuth"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": "auth_token",
    }
    common_responses = components.setdefault("responses", {})
    common_responses.update({
        "BadRequest": {
            "description": "The request body or business input is invalid",
        },
        "Unauthorized": {
            "description": "Authentication is required or the token is invalid",
        },
        "Forbidden": {
            "description": "The authenticated user lacks permission",
        },
        "RateLimited": {
            "description": "Request rate limit exceeded",
        },
        "Conflict": {
            "description": "The request conflicts with existing state",
        },
        "NotFound": {
            "description": "The requested resource does not exist",
        },
        "NotImplemented": {
            "description": "The configured service does not support this operation",
        },
    })
    public_paths = {
        "/", "/health", "/nginx-health",
        "/auth/login", "/auth/logout", "/auth/register",
        "/auth/verify-email", "/auth/resend-verification",
        "/auth/forgot-password", "/auth/reset-password",
    }
    for path, path_item in schema.get("paths", {}).items():
        protected = path not in public_paths
        for method, operation in path_item.items():
            if method not in {
                "get", "post", "put", "patch", "delete", "options", "head", "trace"
            } or not isinstance(operation, dict):
                continue
            responses = operation.setdefault("responses", {})
            responses.setdefault("400", {
                "$ref": "#/components/responses/BadRequest",
            })
            responses.setdefault("429", {
                "$ref": "#/components/responses/RateLimited",
            })
            responses.setdefault("404", {
                "$ref": "#/components/responses/NotFound",
            })
            if path == "/auth/login":
                responses.setdefault("401", {
                    "$ref": "#/components/responses/Unauthorized",
                })
            if path == "/auth/register":
                responses.setdefault("409", {
                    "$ref": "#/components/responses/Conflict",
                })
            if path == "/auth/resend-verification":
                responses.setdefault("501", {
                    "$ref": "#/components/responses/NotImplemented",
                })
            if protected:
                operation["security"] = [
                    {"CookieAuth": []},
                    {"BearerAuth": []},
                ]
                responses.setdefault("401", {
                    "$ref": "#/components/responses/Unauthorized",
                })
                responses.setdefault("403", {
                    "$ref": "#/components/responses/Forbidden",
                })
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

logger = logging.getLogger("main")


# ── 健康检查端点（同时支持 GET 和 HEAD，供 Render / nginx 健康探测使用）────────
@app.get("/health", include_in_schema=False)
@app.head("/health", include_in_schema=False)
async def health():
    database = await asyncio.to_thread(database_healthcheck)
    payload = {"status": "ok" if database["healthy"] else "unhealthy", "database": database}
    return payload if database["healthy"] else JSONResponse(status_code=503, content=payload)


@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
async def root():
    """根路径健康探测（Render 平台会对 / 发 HEAD 请求做存活检查）"""
    return {"status": "ok"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器：防止堆栈信息泄漏到 API 响应"""
    logger.error("未处理异常 [%s %s]: %s", request.method, request.url.path, exc, exc_info=True)
    # 仅对 HTTPException 保留原有响应，其余返回通用 500
    from fastapi.exceptions import HTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
    return JSONResponse(
        status_code=500,
        content={"detail": "内部服务器错误"},
    )


if __name__ == "__main__":
    # Render/container ingress requires listening beyond loopback.
    uvicorn.run(app, host="0.0.0.0", port=8000)  # nosec B104
