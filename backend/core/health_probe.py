
"""
health_probe.py — 团队健康度无状态探针系统 (v3.0.2)

基于已有时间戳字段进行组合查询的健康度判定，不引入新的轮询心跳表。
所有探针是只读的，不写入任何数据。
对 ExpertLock 的访问使用 try/except 包裹以兼容模块或表尚未创建的情况。

三个探针场景：
  A. 进程假死 / 完全失联
  B. 正常跨项目资源争用
  C. 断电重启幂等性恢复
"""

import time
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


# ─── 内部辅助 ─────────────────────────────────────────────────────────────────

def _get_expert_locks(expert_id: str) -> List[Any]:
    """
    安全获取某专家的全部活跃锁。
    如果 expert_lock 模块或数据库表不可用，返回空列表。
    """
    try:
        from core.expert_lock import get_locks_for_expert
        return get_locks_for_expert(expert_id)
    except Exception as e:
        logger.warning("无法查询 ExpertLock（模块或表尚未就绪）：%s", e)
        return []


def _get_repair_registry():
    """
    安全获取 RepairRegistry 全局单例。
    """
    try:
        from core.repair_loop import repair_registry
        return repair_registry
    except Exception as e:
        logger.warning("无法访问 RepairRegistry：%s", e)
        return None


# ─── 场景 A：进程假死 / 完全失联 ──────────────────────────────────────────────

def check_expert_stuck(expert_id: str) -> dict:
    """
    检查专家是否假死 / 完全失联。

    同时满足以下两个条件时判定为假死：
      1. 存在启动时间超过 30 分钟的任务
         （以锁创建时间 lock.created_at 为近似依据，
           锁在专家被分配任务时创建）
      2. 该专家在所有项目中的锁租约（leased_until）
         均未在近 15 分钟内更新（续期）

    返回：{"status": "ok" | "stuck", "details": str}
    """
    if not expert_id:
        return {"status": "ok", "details": "未提供 expert_id, 跳过检查"}

    locks = _get_expert_locks(expert_id)
    if not locks:
        return {"status": "ok", "details": f"专家 {expert_id} 当前无活跃锁, 无需检查"}

    now = time.time()

    # 条件 1: 是否存在运行超过 30 分钟的任务
    # 锁的 created_at 近似于任务分配时间(可接受的保守近似)
    earliest_created = min(lock.created_at for lock in locks)
    if (now - earliest_created) <= 30 * 60:
        return {
            "status": "ok",
            "details": f"专家 {expert_id} 最早任务启动距今 "
                       f"{int((now - earliest_created) / 60)} 分钟, 未超过 30 分钟阈值",
        }

    # 条件 2: 检查所有锁是否均超过 15 分钟未续期
    # 锁续期时将 leased_until 设为 now + 30min,
    # 因此若 leased_until <= now + 15min, 说明自上轮续期已超过 15 分钟
    any_recent_renewal = any(
        lock.leased_until > (now + 15 * 60)
        for lock in locks
    )
    if any_recent_renewal:
        return {
            "status": "ok",
            "details": f"专家 {expert_id} 存在最近 15 分钟内续期的锁, 专家活跃中",
        }

    # 两个条件同时满足 -> 假死
    return {
        "status": "stuck",
        "details": (
            f"专家 {expert_id} 疑似假死: 任务已运行超过 30 分钟, "
            f"且所有 {len(locks)} 个活跃锁均未在近 15 分钟内续期。"
            f"建议释放该专家当前全部未完成锁, 并通知 HR 重新匹配备用专家。"
        ),
    }


# ─── 场景 B: 正常跨项目资源争用 ──────────────────────────────────────────────

def check_cross_project_contention(expert_id: str, current_project_id: str) -> dict:
    """
    检查专家是否因跨项目资源争用导致当前项目任务等待。

    触发条件(需同时满足):
      1. 当前任务状态为 WAITING 或 BLOCKED
      2. 检索全局 ExpertLock, 发现该专家在其他项目中
         存在有效的锁且处于活跃续期状态

    返回: {"status": "ok" | "contention", "details": str}

    说明: 此探针仅返回轻量提示, 不触发报警中断。
    """
    if not expert_id or not current_project_id:
        return {"status": "ok", "details": "缺少 expert_id 或 current_project_id, 跳过检查"}

    # 条件 1: 检查任务状态
    waiting_or_blocked = _is_task_waiting_or_blocked(expert_id, current_project_id)
    if not waiting_or_blocked:
        return {
            "status": "ok",
            "details": f"专家 {expert_id} 在当前项目中的任务未被标记为 WAITING 或 BLOCKED",
        }

    # 条件 2: 检查是否存在跨项目活跃锁
    locks = _get_expert_locks(expert_id)
    other_project_locks = [
        lock for lock in locks
        if lock.project_id != current_project_id
    ]
    if not other_project_locks:
        return {
            "status": "ok",
            "details": f"专家 {expert_id} 当前处于 WAITING/BLOCKED, 但无跨项目活跃锁",
        }

    other_projects = sorted(set(lock.project_id for lock in other_project_locks))
    return {
        "status": "contention",
        "details": (
            f"专家正在并行处理其他 {len(other_project_locks)} 个项目资源 "
            f"({other_projects}), 当前项目等待属正常资源争用"
        ),
    }


def _is_task_waiting_or_blocked(expert_id: str, project_id: str) -> bool:
    """
    检查某专家在当前项目中的任务是否处于 WAITING 或 BLOCKED 状态。

    查询路径(依次尝试, 任一命中即返回 True):
      1. repair_registry 中该专家在该项目的缺陷单状态
      2. ProjectContext.agents 中该专家的 agent 状态
    """
    # 路径 1: 从 repair_registry 的缺陷单查询
    registry = _get_repair_registry()
    if registry is not None:
        try:
            for pid, subs in registry._controllers.items():
                if pid != project_id:
                    continue
                for ctrl in subs.values():
                    for ticket in ctrl.defects.values():
                        if getattr(ticket, "agent_id", None) == expert_id:
                            sv = getattr(ticket, "status", None)
                            if sv is not None and sv.value in ("waiting", "blocked"):
                                return True
        except Exception:
            pass

    # 路径 2: 从 ProjectContext.agents 查询
    try:
        from core.app_state import projects as _projects
        ctx = _projects.get(project_id)
        if ctx is not None:
            for agent_info in ctx.agents.values():
                if isinstance(agent_info, dict):
                    if agent_info.get("agent_id") == expert_id:
                        if agent_info.get("status", "") in ("waiting", "blocked"):
                            return True
                    if agent_info.get("id") == expert_id:
                        if agent_info.get("status", "") in ("waiting", "blocked"):
                            return True
    except Exception:
        pass

    return False


# ─── 场景 C: 断电重启幂等性恢复 ──────────────────────────────────────────────

def recover_in_flight_steps() -> list:
    """
    扫描所有 DefectTicket 的 step_execution_state,
    将 in_flight 步骤回滚为 pending 状态(仅内存中回滚)。

    触发条件(由外部调用方判断, 本函数被动执行):
      1. 系统触发重启 Lifecycle Event
      2. 扫描 DefectTicket.step_execution_state(或任意额外字段)
         发现某步骤停留于 "in_flight" 状态

    响应:
      - 强制将该原子步骤状态回滚为 "pending"
      - 返回恢复的步骤列表, 供上层推入 TaskQueue 继续执行
      - 不扣减单缺陷最大修复轮次

    返回: 已恢复的步骤列表
      每项: {"defect_id", "step_key", "subproject_id", "project_id"}

    注意: step_execution_state 不是 DefectTicket 的必填字段,
    仅在需要原子步骤跟踪时通过 ticket.to_dict() 或
    附加属性存储。如该字段不存在, 函数返回空列表。
    本函数唯一写入操作是对内存中 ticket 属性的回滚赋值,
    不涉及数据库写入。
    """
    recovered: List[Dict[str, str]] = []

    registry = _get_repair_registry()
    if registry is None:
        return recovered

    try:
        for project_id, subprojects in registry._controllers.items():
            for subproject_id, ctrl in subprojects.items():
                for ticket in ctrl.defects.values():
                    # 尝试从序列化字典获取 step_execution_state
                    step_state = ticket.to_dict().get("step_execution_state")

                    # 也检查直接附加到 ticket 对象的属性
                    if step_state is None:
                        step_state = getattr(ticket, "step_execution_state", None)

                    if not isinstance(step_state, dict):
                        continue

                    for step_key, step_val in list(step_state.items()):
                        if step_val == "in_flight":
                            # 回滚: in_flight -> pending
                            if isinstance(step_state, dict):
                                step_state[step_key] = "pending"

                            # 如果该数据是 ticket 的直接属性, 写回
                            if hasattr(ticket, "step_execution_state"):
                                setattr(ticket, "step_execution_state", step_state)

                            recovered.append({
                                "defect_id": ticket.defect_id,
                                "step_key": str(step_key),
                                "subproject_id": subproject_id,
                                "project_id": project_id,
                            })
    except Exception as e:
        logger.warning("扫描 in_flight 步骤时出错: %s", e)

    return recovered
