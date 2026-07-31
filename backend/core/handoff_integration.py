"""
Handoff 交接协议 + Orchestrator 三层编排集成层
参考设计文档第 10、11 节
"""

import time
import uuid
import logging
from typing import Dict, List, Optional, Any

from core.handoff import HandoffPayload, QAVerdict, RubricItem, make_rubric_from_requirements
from core.orchestrator import get_orchestrator

logger = logging.getLogger(__name__)


def build_phase_rubric(phase: dict) -> List[RubricItem]:
    """从阶段描述构建验收 Rubric"""
    requirements = phase.get("deliverables", [])
    if not requirements:
        requirements = [phase.get("description", "阶段完成")]
    return make_rubric_from_requirements(requirements)


def create_handoff_for_phase(
    project_id: str,
    phase: dict,
    agent_info: dict,
) -> HandoffPayload:
    """创建阶段 Agent 的结构化交接 Payload"""
    rubric = build_phase_rubric(phase)
    return HandoffPayload(
        handoff_id=f"ho-{uuid.uuid4().hex[:8]}",
        phase=phase.get("phase_id", ""),
        phase_name=phase.get("name", ""),
        task_id=agent_info.get("subproject_id", ""),
        from_agent_id="pm",
        to_agent_id=agent_info.get("id", ""),
        requirements=phase.get("deliverables", [phase.get("description", "")]),
        deliverable={"files": [], "agent_id": agent_info.get("id")},
        acceptance_criteria=phase.get("acceptance_criteria", []),
        rubric=rubric,
        status="OK",
        created_at=time.time(),
    )


def build_qa_context(
    project_id: str,
    phase_id: str,
    agent_info: dict,
) -> Dict[str, Any]:
    """构建质检上下文：整合 Rubric + Handoff + 阶段信息"""
    orchestrator = get_orchestrator(project_id)
    phase = orchestrator.get_phase(phase_id) if hasattr(orchestrator, 'get_phase') else {}
    rubric = build_phase_rubric(phase or {})
    handoff = create_handoff_for_phase(project_id, phase or {}, agent_info)
    qa_handoff = orchestrator.build_qa_handoff(
        phase_id=phase_id,
        requirements=handoff.requirements,
        rubric=rubric,
    )
    return {"handoff": handoff, "rubric": rubric, "qa_handoff": qa_handoff, "phase": phase}


def verify_deliverable(
    project_id: str,
    handoff_id: str,
    qa_result: Dict[str, Any],
) -> QAVerdict:
    """验证交付物质量，根据 QA 检测结果生成 QAVerdict"""
    orchestrator = get_orchestrator(project_id)
    verdict = QAVerdict(
        handoff_id=handoff_id,
        task_id=qa_result.get("task_id", ""),
        qa_agent_id=qa_result.get("qa_agent_id", "qa-default"),
        status=qa_result.get("passed", False),
        score=qa_result.get("score", 0),
    )
    if qa_result.get("passed"):
        verdict.status = "PASS"
    elif qa_result.get("score", 0) < 40:
        verdict.status = "BLOCKED"
    else:
        verdict.status = "FAIL"
    return verdict