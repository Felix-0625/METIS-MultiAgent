"""双轨可用性检查 — ExpertLock + EmployeeProfile 双轨并发追踪"""
import logging
import time
from typing import Dict, List
from core import expert_lock
from core.global_agent_pool import get_global_agent_pool

logger = logging.getLogger(__name__)


def get_project_experts_locks(project_id: str) -> List[Dict]:
    locks = expert_lock.get_active_locks(project_id=project_id)
    result = {}
    for lock in locks:
        eid = lock["expert_id"]
        if eid not in result:
            result[eid] = {"expert_id": eid, "locks": [], "total_files_locked": 0}
        result[eid]["locks"].append(lock)
        result[eid]["total_files_locked"] += len(lock.get("file_scope", []))
    return list(result.values())


def get_project_team_health(project_id: str) -> Dict:
    pool = get_global_agent_pool()
    team = pool.get_project_team(project_id)
    mgmt = [{
        "employee_id": e.employee_id, "name": e.name, "role": e.role,
        "agent_type": e.agent_type, "status": e.status,
        "current_projects": e.current_projects,
    } for e in team]
    return {
        "project_id": project_id,
        "execution_layer": get_project_experts_locks(project_id),
        "management_layer": mgmt,
        "timestamp": time.time(),
    }