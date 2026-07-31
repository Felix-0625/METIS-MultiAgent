"""
认证 API 路由
    POST /auth/login             登录（用户名或邮箱）→ JWT HttpOnly Cookie
    POST /auth/logout            退出登录（清除 Cookie）
    POST /auth/register          注册新用户（无需登录，发送验证邮件）
    POST /auth/verify-email      验证邮箱（提交验证码）
    POST /auth/forgot-password   忘记密码（发送重置邮件）
    POST /auth/reset-password    重置密码（提交 token + 新密码）
    PUT  /auth/change-password   修改密码（需登录 + 旧密码验证）
    POST /auth/resend-verification 重发验证邮件
    GET  /auth/me                获取当前用户信息
    GET  /auth/users             列出所有用户（管理员）
    DELETE /auth/users/{id}      删除用户（管理员）
"""

import logging
import secrets
import time
from typing import List

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, status, Depends, Request
from fastapi.responses import JSONResponse

from core.auth import (
    UserModel,
    AUTH_COOKIE_NAME,
    JWT_EXPIRY_HOURS,
    authenticate_user,
    create_token,
    create_user,
    change_password,
    reset_password,
    verify_user_email,
    get_user_by_email,
    get_user_by_username,
    list_users,
    delete_user,
    get_current_user,
    get_admin_user,
    extract_client_ip,
    check_email_rate_limit,
    save_verification_code,
    verify_email_code,
    is_verify_locked,
    audit_log,
)
from core.email import is_email_configured, send_verification_email
from models.schemas import (
    LoginRequest,
    RegisterRequest,
    VerifyEmailRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    ChangePasswordRequest,
    ResendVerificationRequest,
)

router = APIRouter(prefix="/auth", tags=["认证"])
logger = logging.getLogger(__name__)


def _check_email_service():
    """检查邮件服务是否已配置，未配置则返回 501"""
    if not is_email_configured():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="邮件服务未配置，此功能仅在云端可用"
        )


# ── 登录 ────────────────────────────────────────────────────────────────────────

class LoginResponse(BaseModel):
    user_id: str
    username: str
    role: str
    email: str = ""
    email_verified: bool = False
    expires_in: int

@router.post("/login")
def login(req: LoginRequest, request: Request):
    """
    用户登录（支持用户名或邮箱），设置 HttpOnly Cookie 防止 XSS 窃取 Token。
    """
    ip = extract_client_ip(request)
    user, error = authenticate_user(req.login, req.password, ip)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error)

    token = create_token(user.user_id, user.username, user.role, user.token_version)
    response = JSONResponse(content={
        "user_id": user.user_id,
        "username": user.username,
        "role": user.role,
        "email": user.email,
        "email_verified": user.email_verified,
        "expires_in": JWT_EXPIRY_HOURS * 3600,
    })
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=JWT_EXPIRY_HOURS * 3600,
        httponly=True,
        secure=bool(request.headers.get("X-Forwarded-Proto") == "https"
                     or not request.url.hostname.startswith(("127.", "localhost"))),
        samesite="lax",
        path="/",
    )
    return response


# ── 退出 ────────────────────────────────────────────────────────────────────────

@router.post("/logout")
def logout():
    """退出登录，清除 HttpOnly Cookie"""
    response = JSONResponse(content={"detail": "已退出登录"})
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
    return response


# ── 注册（开放自主注册）────────────────────────────────────────────────────────

@router.post("/register")
def register(req: RegisterRequest, request: Request):
    """
    开放自主注册。
    - SMTP / Brevo 已配置：创建 user（email_verified=False），发送验证邮件。
    - 邮件服务未配置（开发模式）：创建 user（email_verified=True），跳过邮件验证。
    """
    ip = extract_client_ip(request)

    # 限流
    allowed, err = check_email_rate_limit(req.email, ip)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=err)

    # 检查邮箱是否被锁定（验证次数过多）
    locked, remaining = is_verify_locked(req.email)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"验证次数过多，请{remaining}秒后重试"
        )

    # 邮件服务未配置时跳过邮箱验证（开发/本地模式）
    email_enabled = is_email_configured()
    email_verified = not email_enabled  # 开发模式下自动验证

    # 创建用户
    try:
        user = create_user(
            username=req.username,
            password=req.password,
            email=req.email,
            email_verified=email_verified,
        )
    except ValueError as e:
        conflict = "已存在" in str(e) or "已注册" in str(e)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if conflict else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    if not email_enabled:
        # 开发模式：自动验证，可直接登录
        audit_log("USER_REGISTERED", req.username, ip, f"email={req.email} (dev auto-verified)")
        return {
            "detail": "注册成功，现在可以登录了",
            "email_verified": True,
            "email_verification_required": False,
        }
    else:
        # 生产模式：发送验证邮件
        code = str(secrets.randbelow(1_000_000)).zfill(6)
        save_verification_code(req.email, code)

        try:
            send_verification_email(req.email, req.username, code)
        except Exception as e:
            logger.error("发送验证邮件失败: %s", e)
            # 邮件发送失败 → 回滚用户创建，让用户可以重新注册
            try:
                delete_user(user.user_id)
            except Exception as del_e:
                logger.error("回滚用户创建失败: %s", del_e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="邮件服务暂时不可用，请稍后重试或联系管理员"
            )

        audit_log("USER_REGISTERED", req.username, ip, f"email={req.email}")
        return {
            "detail": "注册成功，验证邮件已发送至你的邮箱",
            "email_verified": False,
            "email_verification_required": True,
        }


# ── 邮箱验证 ─────────────────────────────────────────────────────────────────────

@router.post("/verify-email")
def verify_email(req: VerifyEmailRequest, request: Request):
    """验证邮箱验证码"""
    ip = extract_client_ip(request)

    ok, err = verify_email_code(req.email, req.code)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err)

    # 标记用户邮箱已验证
    try:
        verify_user_email(req.email)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    audit_log("EMAIL_VERIFIED", "", ip, f"email={req.email}")
    return {"detail": "邮箱验证成功，现在可以登录了"}


# ── 重发验证码 ───────────────────────────────────────────────────────────────────

@router.post("/resend-verification")
def resend_verification(req: ResendVerificationRequest, request: Request):
    """重发邮箱验证码。锁定状态阻止重发，防无限循环绕过。"""
    _check_email_service()
    ip = extract_client_ip(request)

    # 限流
    allowed, err = check_email_rate_limit(req.email, ip)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=err)

    # 锁定期间阻止重发（防止 5次失败→锁定→重发新码→又5次 的无限循环）
    locked, remaining = is_verify_locked(req.email)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"验证次数过多，请{remaining}秒后重试"
        )

    user = get_user_by_email(req.email)
    if not user:
        # 不泄露邮箱是否存在
        time.sleep(0.5)
        return {"detail": "如果该邮箱已注册，验证邮件已发送"}

    code = str(secrets.randbelow(1_000_000)).zfill(6)
    save_verification_code(req.email, code)

    try:
        send_verification_email(req.email, user.username, code)
    except Exception as e:
        logger.error("重发验证邮件失败: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="邮件发送失败，请稍后重试"
        )

    return {"detail": "验证邮件已重新发送"}


# ── 忘记密码（邮件验证码）─────────────────────────────────────────────────────

@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, request: Request):
    """
    忘记密码：发送6位验证码到邮箱。
    无论邮箱是否存在，返回相同文案和时间延迟，防止用户枚举。
    """
    ip = extract_client_ip(request)
    # 检查邮件服务是否可用（未配置时自动验证 + 无需找回密码流程）
    email_enabled = is_email_configured()

    allowed, err = check_email_rate_limit(req.email, ip)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=err)

    user = get_user_by_email(req.email)
    if user and email_enabled:
        code = str(secrets.randbelow(1_000_000)).zfill(6)
        save_verification_code(req.email, code)
        try:
            send_verification_email(req.email, user.username, code)
        except Exception as e:
            logger.error("发送重置验证码失败: %s", e)
    elif not user:
        time.sleep(0.5)

    return {
        "detail": "如果该邮箱已注册，验证码已发送",
        "email_required": email_enabled,
    }


# ── 重置密码 ─────────────────────────────────────────────────────────────────────

@router.post("/reset-password")
def reset_password_route(req: ResetPasswordRequest, request: Request):
    """通过验证码验证身份后设置新密码。"""
    ip = extract_client_ip(request)

    ok, err = verify_email_code(req.email, req.code)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err)

    try:
        reset_password(req.email, req.new_password)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    audit_log("PASSWORD_RESET", "", ip, f"email={req.email}")
    return {"detail": "密码重置成功，请使用新密码登录"}


# ── 修改密码（已登录）────────────────────────────────────────────────────────────

@router.put("/change-password")
def change_password_route(
    req: ChangePasswordRequest,
    current_user: UserModel = Depends(get_current_user),
    request: Request = None,
):
    """
    修改密码（需验证旧密码）。
    修改后递增 token_version 使所有旧 JWT 失效。
    """
    ip = extract_client_ip(request) if request else "unknown"

    if not change_password(current_user, req.old_password, req.new_password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="旧密码错误")

    audit_log("PASSWORD_CHANGED", current_user.username, ip)
    return {"detail": "密码已修改，请使用新密码重新登录"}


# ── 获取当前用户 ─────────────────────────────────────────────────────────────────

class UserInfo(BaseModel):
    user_id: str
    username: str
    role: str
    email: str = ""
    email_verified: bool = False
    created_at: float

@router.get("/me", response_model=UserInfo)
def me(current_user: UserModel = Depends(get_current_user)):
    """获取当前登录用户信息"""
    return UserInfo(
        user_id=current_user.user_id,
        username=current_user.username,
        role=current_user.role,
        email=current_user.email,
        email_verified=current_user.email_verified,
        created_at=current_user.created_at,
    )


# ── 用户管理（管理员）────────────────────────────────────────────────────────────

@router.get("/users", response_model=List[UserInfo])
def get_users(admin: UserModel = Depends(get_admin_user)):
    """列出所有用户（管理员）"""
    return [UserInfo(**u) for u in list_users()]


@router.delete("/users/{user_id}")
def remove_user(user_id: str, admin: UserModel = Depends(get_admin_user)):
    """删除用户（管理员）"""
    if user_id == admin.user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能删除自己")
    if not delete_user(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return {"detail": "用户已删除"}
