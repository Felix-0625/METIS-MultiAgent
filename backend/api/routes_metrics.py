"""指标路由"""

import asyncio, time, json

from fastapi import APIRouter, HTTPException

from typing import Optional, List, Dict, Any

from pydantic import BaseModel

from core.app_state import (

    projects, hermes_client, global_sm_agent, _get_project,

    logger, _persist_all_async, repair_registry, DefectStatus,

    ProjectContext, PMLeaderAgent, PMMemberAgent,

    IdeaLandingAgent,
    save_idea_landing, load_idea_landing,
    _phase_managers,

)

from models.schemas import InjectQCRequest

router = APIRouter(tags=["metrics"])



@router.get("/projects/{project_id}/metrics")

async def get_project_metrics(project_id: str):

    """

    计算项目全部研究指标，一次性返回所有数据。



    效率指标：

      - e2e_hours          端到端交付时间（项目创建→最后一个阶段完成），None 表示未完成

      - phase_durations    各阶段耗时（秒）

      - qc_fix_cycles      每阶段质检-修复循环次数

      - manual_interventions  人工介入次数（needs_manual 问题总数）



    质量指标：

      - first_pass_rate    阶段一次性通过率（首次质检即通过）

      - feature_completeness  功能完整度（completed / total 子项目）

      - bug_density        Bug 密度（issue 总数 / 代码总行数）



    可控性指标：

      - regression_rate    退化率（修复后引入新问题 / 原问题数）

      - fix_success_rate   修复成功率（fixed / (fixed + needs_manual + open)）

      - phase_leak_rate    阶段间泄漏率（后阶段发现的前阶段问题 / 总问题数）

      - detected_phase_dist  各阶段首次发现的问题数分布

    """

    ctx = _get_project(project_id)

    pm = _phase_managers.get(project_id)



    now = time.time()



    # ── 阶段数据 ────────────────────────────────────────────────────────────

    phases = pm.phases if pm else []



    # 端到端交付时间

    project_created_at = ctx.created_at

    last_completed = None

    for p in phases:

        if p.get("status") == "completed" and p.get("completed_at"):

            ca = p["completed_at"]

            if last_completed is None or ca > last_completed:

                last_completed = ca

    e2e_seconds = (last_completed - project_created_at) if last_completed else None

    e2e_hours = round(e2e_seconds / 3600, 2) if e2e_seconds else None



    # 各阶段耗时

    phase_durations: list = []

    for p in phases:

        started = p.get("started_at")

        ended = p.get("completed_at") or (now if p.get("status") == "active" else None)

        phase_durations.append({

            "phase_id": p.get("phase_id"),

            "phase_name": p.get("name", ""),

            "status": p.get("status", "pending"),

            "duration_seconds": round(ended - started, 1) if (started and ended) else None,

            "started_at": started,

            "completed_at": p.get("completed_at"),

        })



    # ── 质检数据 ─────────────────────────────────────────────────────────────

    all_issues: list = []

    for sp_id, qc in ctx.qc_results.items():

        for iss in qc.get("issues_detail", []):

            all_issues.append({**iss, "subproject_id": sp_id})



    total_issues = len(all_issues)

    fixed_issues   = [i for i in all_issues if i.get("status") == "fixed"]

    open_issues    = [i for i in all_issues if i.get("status") == "open"]

    manual_issues  = [i for i in all_issues if i.get("status") == "needs_manual"]

    fixing_issues  = [i for i in all_issues if i.get("status") == "fixing"]



    # 人工介入次数

    manual_interventions = len(manual_issues)



    # 质检-修复循环次数（per 阶段）

    qc_fix_cycles: dict = {}

    for iss in all_issues:

        ph = iss.get("detected_phase") or "unknown"

        qc_fix_cycles[ph] = qc_fix_cycles.get(ph, 0) + iss.get("fix_rounds", 0)



    # 阶段一次性通过率（review_passed 且 fix_rounds 全为 0）

    total_phases_reviewed = sum(1 for p in phases if p.get("reviewed"))

    first_pass_phases = 0

    for p in phases:

        if not p.get("reviewed"):

            continue

        phase_id = p.get("phase_id", "")

        phase_issues = [i for i in all_issues if i.get("detected_phase") == phase_id]

        if all(i.get("fix_rounds", 0) == 0 for i in phase_issues):

            first_pass_phases += 1

    first_pass_rate = round(first_pass_phases / total_phases_reviewed, 4) if total_phases_reviewed else None



    # 功能完整度

    total_sp = len(ctx.subprojects)

    completed_sp = sum(1 for s in ctx.subprojects if s.get("status") == "completed")

    feature_completeness = round(completed_sp / total_sp, 4) if total_sp else None



    # Bug 密度（issue 数 / 代码行数）

    total_lines = 0

    try:

        src_dir = ctx.workspace / "src"

        if src_dir.exists():

            for f in src_dir.rglob("*"):

                if f.is_file() and f.suffix in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java"):

                    try:

                        total_lines += len(f.read_text(encoding="utf-8", errors="replace").splitlines())

                    except Exception:

                        pass

    except Exception:

        pass

    bug_density = round(total_issues / total_lines, 4) if total_lines > 0 else None



    # 修复成功率

    resolved = len(fixed_issues)

    unresolved = len(open_issues) + len(manual_issues) + len(fixing_issues)

    fix_success_rate = round(resolved / (resolved + unresolved), 4) if (resolved + unresolved) > 0 else None



    # 退化率（修复后 fix_rounds > 1 的视为曾引入新问题）

    regressed = [i for i in all_issues if i.get("fix_rounds", 0) > 1]

    original_count = total_issues - len(regressed)

    regression_rate = round(len(regressed) / original_count, 4) if original_count > 0 else None



    # 阶段间泄漏率（后阶段发现前阶段的问题）

    phase_order = {p.get("phase_id"): i for i, p in enumerate(phases)}

    leaked = 0

    for iss in all_issues:

        detected = iss.get("detected_phase", "")

        sp_id = iss.get("subproject_id", "")

        sp = next((s for s in ctx.subprojects if s["id"] == sp_id), {})

        sp_phase = sp.get("phase_id", "")

        if detected and sp_phase and detected != sp_phase:

            d_order = phase_order.get(detected, -1)

            s_order = phase_order.get(sp_phase, -1)

            if d_order > s_order:

                leaked += 1

    phase_leak_rate = round(leaked / total_issues, 4) if total_issues > 0 else None



    # 各阶段首次发现问题数分布

    detected_phase_dist: dict = {}

    for iss in all_issues:

        ph = iss.get("detected_phase") or "unknown"

        detected_phase_dist[ph] = detected_phase_dist.get(ph, 0) + 1



    return {

        "project_id": project_id,

        "project_name": ctx.name,

        "computed_at": now,

        "data_completeness": {

            "has_phases": len(phases) > 0,

            "has_qc_data": total_issues > 0,

            "has_code": total_lines > 0,

            "total_issues": total_issues,

            "total_lines": total_lines,

        },

        "efficiency": {

            "e2e_hours": e2e_hours,

            "phase_durations": phase_durations,

            "qc_fix_cycles_by_phase": qc_fix_cycles,

            "manual_interventions": manual_interventions,

        },

        "quality": {

            "first_pass_rate": first_pass_rate,

            "feature_completeness": feature_completeness,

            "bug_density": bug_density,

            "total_issues": total_issues,

            "open_issues": len(open_issues),

            "fixed_issues": len(fixed_issues),

            "manual_issues": manual_issues.__len__(),

        },

        "controllability": {

            "regression_rate": regression_rate,

            "fix_success_rate": fix_success_rate,

            "phase_leak_rate": phase_leak_rate,

            "detected_phase_dist": detected_phase_dist,

        },

    }

async def get_all_projects_metrics():
    """
    获取所有项目的指标汇总（用于横向对比分析）。
    适合论文中的多项目对比表格。
    """
    result = []
    for pid in projects:
        try:
            ctx = _get_project(pid)
            pm = _phase_managers.get(pid)
            phases = pm.phases if pm else []

            # 快速计算核心指标
            all_issues = [
                i for qc in ctx.qc_results.values()
                for i in qc.get("issues_detail", [])
            ]
            total_issues = len(all_issues)
            fixed = sum(1 for i in all_issues if i.get("status") == "fixed")
            manual = sum(1 for i in all_issues if i.get("status") == "needs_manual")
            total_sp = len(ctx.subprojects)
            completed_sp = sum(1 for s in ctx.subprojects if s.get("status") == "completed")

            last_completed = max(
                (p["completed_at"] for p in phases if p.get("completed_at")),
                default=None
            )
            e2e_h = round((last_completed - ctx.created_at) / 3600, 2) if last_completed else None
            fr = round(fixed / total_issues, 4) if total_issues > 0 else None

            result.append({
                "project_id": pid,
                "project_name": ctx.name,
                "status": ctx.status,
                "e2e_hours": e2e_h,
                "total_phases": len(phases),
                "completed_phases": sum(1 for p in phases if p.get("status") == "completed"),
                "total_issues": total_issues,
                "fix_success_rate": fr,
                "manual_interventions": manual,
                "feature_completeness": round(completed_sp / total_sp, 4) if total_sp else None,
            })
        except Exception:
            pass

    return {"projects": result, "count": len(result)}


# ─── IdeaLanding Agent 路由 ───────────────────────────────────────────────────
#
# 外部独立对话界面（项目外），帮助用户把想法落地然后创建项目继续。
# 每个对话记忆完全隔离，支持置顶/标签/分类管理。
# 用户有个人记忆（偏好/历史 idea 摘要），跨对话共享。
#
# 设计：全局单例，不挂在任何项目下
# ─────────────────────────────────────────────────────────────────────────────

# 全局单例
_idea_landing_agent: Optional[IdeaLandingAgent] = None


def _get_idea_landing() -> IdeaLandingAgent:
    global _idea_landing_agent
    if _idea_landing_agent is None:
        _idea_landing_agent = IdeaLandingAgent(hermes_client=hermes_client)
        # 尝试从持久化恢复
        saved = load_idea_landing()
        if saved:
            try:
                _idea_landing_agent.from_persist(saved)
            except Exception:
                pass
    return _idea_landing_agent


async def _persist_idea_landing():
    """保存 IdeaLanding Agent 状态"""
    agent = _get_idea_landing()
    try:
        save_idea_landing(agent.to_persist())
    except Exception:
        pass


# ── 请求体模型 ────────────────────────────────────────────────────────────────

class IdeaChatRequest(BaseModel):
    message: str
    conv_id: Optional[str] = None          # 不传则使用当前激活对话
    history: Optional[List[Dict]] = None   # 前端传来的最近消息
    context_summary: Optional[str] = None  # 压缩摘要

class IdeaNewConvRequest(BaseModel):
    title: str = "新对话"
    tags: List[str] = []
    category: str = "默认"

class IdeaConvMetaRequest(BaseModel):
    title: Optional[str] = None
    tags: Optional[List[str]] = None
    category: Optional[str] = None

class IdeaPinRequest(BaseModel):
    pinned: bool

class IdeaAdvancePhaseRequest(BaseModel):
    conv_id: str

class IdeaUserMemoryRequest(BaseModel):
    background: Optional[str] = None
    preferences: Optional[Dict] = None


# ── API 路由 ──────────────────────────────────────────────────────────────────

@router.get("/metrics/summary")
async def get_all_projects_metrics():
    """
    获取所有项目的指标汇总（用于横向对比分析）。
    适合论文中的多项目对比表格。
    """
    result = []
    for pid in projects:
        try:
            ctx = _get_project(pid)
            pm = _phase_managers.get(pid)
            phases = pm.phases if pm else []

            # 快速计算核心指标
            all_issues = [
                i for qc in ctx.qc_results.values()
                for i in qc.get("issues_detail", [])
            ]
            total_issues = len(all_issues)
            fixed = sum(1 for i in all_issues if i.get("status") == "fixed")
            manual = sum(1 for i in all_issues if i.get("status") == "needs_manual")
            total_sp = len(ctx.subprojects)
            completed_sp = sum(1 for s in ctx.subprojects if s.get("status") == "completed")

            last_completed = max(
                (p["completed_at"] for p in phases if p.get("completed_at")),
                default=None
            )
            e2e_h = round((last_completed - ctx.created_at) / 3600, 2) if last_completed else None
            fr = round(fixed / total_issues, 4) if total_issues > 0 else None

            result.append({
                "project_id": pid,
                "project_name": ctx.name,
                "status": ctx.status,
                "e2e_hours": e2e_h,
                "total_phases": len(phases),
                "completed_phases": sum(1 for p in phases if p.get("status") == "completed"),
                "total_issues": total_issues,
                "fix_success_rate": fr,
                "manual_interventions": manual,
                "feature_completeness": round(completed_sp / total_sp, 4) if total_sp else None,
            })
        except Exception:
            pass

    return {"projects": result, "count": len(result)}


# ─── IdeaLanding Agent 路由 ───────────────────────────────────────────────────
#
# 外部独立对话界面（项目外），帮助用户把想法落地然后创建项目继续。
# 每个对话记忆完全隔离，支持置顶/标签/分类管理。
# 用户有个人记忆（偏好/历史 idea 摘要），跨对话共享。
#
# 设计：全局单例，不挂在任何项目下
# ─────────────────────────────────────────────────────────────────────────────
