"""数据模型与请求/响应 Schema"""
from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any, Literal
from pathlib import Path

class ProjectRequest(BaseModel):
    name: str
    description: str

    @field_validator("name")
    @classmethod
    def validate_project_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("project name is required")
        if len(normalized) > 120:
            raise ValueError("project name must be at most 120 characters")
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("project name contains control characters")
        return normalized

    @field_validator("description")
    @classmethod
    def validate_project_description(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 20_000:
            raise ValueError("project description must be at most 20000 characters")
        if "\x00" in normalized:
            raise ValueError("project description contains a null byte")
        return normalized

class AnalyzeRequest(BaseModel):
    requirements: str
    history: Optional[List[Dict]] = None          # 前端传来的历史消息，用于多轮上下文
    context_summary: Optional[str] = None         # 前端缓存的压缩摘要，超过阈值后使用

class SubprojectConfirmRequest(BaseModel):
    subproject_id: str
    confirmed: bool
    modifications: Optional[str] = None

class SubprojectRequest(BaseModel):
    id: str
    name: str
    description: str
    agent_id: Optional[str] = None

class AgentCreateRequest(BaseModel):
    role: str
    skills: List[str] = []
    subproject_id: Optional[str] = None

class AgentApiConfigRequest(BaseModel):
    """Agent 独立 API 配置"""
    model: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None

class SkillImportRequest(BaseModel):
    name: str
    description: str
    version: str = "1.0.0"
    content: str
    source: str = "manual"

class SkillSearchRequest(BaseModel):
    query: str
    filters: Optional[Dict] = None

class ChangeRequest(BaseModel):
    change_type: str
    description: str
    affected_subprojects: List[str] = []

class GiteeConfigRequest(BaseModel):
    repo_url: str
    token: str
    provider: str = "gitee"

class GiteePushRequest(BaseModel):
    commit_message: Optional[str] = None

class LLMRoleConfigRequest(BaseModel):
    """Model overrides for a specific LLM responsibility."""
    model: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)

    @field_validator("model")
    @classmethod
    def validate_role_model(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized


class ThinkingConfigRequest(BaseModel):
    """Provider-compatible thinking-mode control for the current user."""
    type: Literal["enabled", "disabled"]


class DefaultApiConfigRequest(BaseModel):
    model: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    thinking: Optional[ThinkingConfigRequest] = None
    generator: Optional[LLMRoleConfigRequest] = None
    reviewer: Optional[LLMRoleConfigRequest] = None

class SupervisorChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict]] = None
    context_summary: Optional[str] = None

class RepairStartRequest(BaseModel):
    subproject_id: str
    forbidden_zone: Optional[List[str]] = None   # 禁止修改的文件/区域

class ProposalSubmitRequest(BaseModel):
    proposal: str                                # 修复方案（≤100字）

class ProposalReviewRequest(BaseModel):
    approved: bool
    reason: Optional[str] = ""
    reviewer: str = "PM"

class ArbiterForcePassRequest(BaseModel):
    subproject_id: str

class SkillIngestRequest(BaseModel):
    """Skill Agent 入口请求：文件内容或 URL"""
    raw_text: Optional[str] = None      # 文件文本内容
    url: Optional[str] = None           # Skill 文件 URL（GitHub/任意 HTTP）
    filename: Optional[str] = ""        # 文件名（用于推断 name）
    auto_confirm: bool = False          # True=直接入库，False=返回 pending 等待确认

class SkillConfirmRequest(BaseModel):
    skill_id: str
    override_classification: Optional[Dict] = None
    tags: Optional[List[str]] = None

class ChatHistorySaveRequest(BaseModel):
    messages: List[Dict]  # type: List[Dict]
    
    @field_validator('messages')
    @classmethod
    def check_message_count(cls, v: List[Dict]) -> List[Dict]:
        """防止恶意请求传入过大的聊天历史，限制最大 500 条消息"""
        if len(v) > 500:
            raise ValueError(f'聊天历史消息数超过限制（最大500条，实际{len(v)}条）')
        return v

class FileWriteRequest(BaseModel):
    path: str
    content: str

class FileStagingRequest(BaseModel):
    """
    文件暂存请求体。
    注意：使用 path + content 字段（而非 files 数组），与 /files/write 等其他文件 API 保持一致。
    """
    path: str
    content: str

class FileCommitRequest(BaseModel):
    message: str = ""

class FileRollbackRequest(BaseModel):
    version: int

class HRReassignRequest(BaseModel):
    change_description: str          # 变更描述（用户要求修改什么）
    affected_subproject_ids: List[str] = []  # 受影响的子项目 ID
    new_tasks: Optional[List[Dict]] = None   # 新增任务列表

class RequirementsRevisionRequest(BaseModel):
    content: str
    expected_revision: int
    expected_digest: str
    replace: bool = False
    supersedes: List[str] = Field(default_factory=list)
    source: Literal["user", "chat", "attachment"] = "user"


class PMTeamChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict]] = None
    context_summary: Optional[str] = None
    requirements_revision: Optional[RequirementsRevisionRequest] = None


class RequiredFileContractRequest(BaseModel):
    path: str
    owner_type: Literal[
        "frontend", "backend", "database", "qa", "devops", "security",
        "architecture", "data", "fullstack_engineer",
    ]
    phase_id: str
    required: bool = True

    @field_validator("path")
    @classmethod
    def validate_required_file_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        candidate = Path(normalized)
        if not normalized or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("required file path must be project-relative")
        return normalized

    @field_validator("phase_id")
    @classmethod
    def validate_phase_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("phase_id is required")
        return normalized


class PlanConfirmRequest(BaseModel):
    modifications: Optional[str] = ""
    required_files: Optional[List[RequiredFileContractRequest]] = None
    requirements_revision: Optional[int] = None
    requirements_digest: Optional[str] = None


PlanningStatus = Literal[
    "generating", "generated", "validation_failed", "model_failed", "saved", "confirmed",
    "contract_locked",
]


class ContractValidationIssue(BaseModel):
    layer: Literal["json_schema", "project_contract", "phase_contract"]
    code: str
    path: str
    message: str
    expected: Any = None
    actual: Any = None


class ContractValidationResult(BaseModel):
    valid: bool
    artifact_type: str
    contract_version: int = 1
    schema_version: int = 1
    issues: List[ContractValidationIssue] = Field(default_factory=list)


class PlanningArtifactMetadata(BaseModel):
    artifact_type: Literal["plan", "phase_plan"]
    version: int
    source: str
    contract_version: int
    validation: ContractValidationResult
    auto_corrections: List[Dict[str, Any]] = Field(default_factory=list)

class SupervisorReviewChatRequest(BaseModel):
    message: str
    phase_id: Optional[str] = None
    history: Optional[List[Dict]] = None
    context_summary: Optional[str] = None

class PhasePMChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict]] = []
    context_summary: Optional[str] = ""

class PhaseDescUpdateRequest(BaseModel):
    description: str

class IssueSubmitToPMRequest(BaseModel):
    user_description: str

class BatchIssueSubmitToPMRequest(BaseModel):
    issue_ids: List[str]  # 要批量提交的问题 ID 列表（空列表 = 提交所有 open 问题）
    user_note: str = ""   # 用户附加说明（可选）

class PhasePlanExpertRequest(BaseModel):
    """阶段 PM 生成专家需求规划的请求"""
    phase_description: str = ""          # 阶段任务描述（可选，不传则从阶段信息读取）
    user_requirements: str = ""          # 用户对本阶段的额外要求

class ExpertRequirement(BaseModel):
    """单个任务的专家需求"""
    task_id: str = ""
    task_name: str
    task_description: str = ""
    required_role: str
    required_domains: List[str] = []
    required_skills: List[str] = []
    acceptance_criteria: List[str] = []
    priority: str = "normal"             # high / normal / low

class PhaseExpertMatchRequest(BaseModel):
    """HR 从专家池匹配专家的请求"""
    expert_requirements: List[Dict]      # 阶段 PM 输出的专家需求列表

class ExpertAssignment(BaseModel):
    """单个任务的专家分配确认"""
    task_id: str
    expert_id: str                       # 用户选择的专家 ID
    task_name: str = ""
    task_description: str = ""
    acceptance_criteria: List[str] = []
    phase_name: str = ""

class PhaseExpertConfirmRequest(BaseModel):
    """用户确认专家分配的请求"""
    assignments: List[ExpertAssignment]

class EmployeeCreateRequest(BaseModel):
    name: str
    role: str
    agent_type: str = "pg"
    avatar: str = "🤖"
    department: str = "execution"
    role_description: str = ""
    working_style: str = ""
    communication_style: str = ""
    domains: List[str] = []
    skills: List[str] = []
    skill_ids: List[str] = []
    behavior_rules: List[str] = []
    output_format: str = ""
    api_config: Dict = {}

class EmployeeUpdateRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    avatar: Optional[str] = None
    department: Optional[str] = None
    role_description: Optional[str] = None
    working_style: Optional[str] = None
    communication_style: Optional[str] = None
    domains: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    skill_ids: Optional[List[str]] = None
    behavior_rules: Optional[List[str]] = None
    output_format: Optional[str] = None
    api_config: Optional[Dict] = None
    status: Optional[str] = None

class ProjectTeamAssignRequest(BaseModel):
    employee_ids: List[str]

class ExpertCreateRequest(BaseModel):
    name: str
    role: str
    agent_type: str = "pg"
    avatar: str = ""
    role_description: str = ""
    working_style: str = ""
    communication_style: str = ""
    decision_style: str = ""
    domains: List[str] = []
    skills: List[Dict] = []          # [{"name": "Python", "level": "expert"}]
    skill_ids: List[str] = []
    behavior_rules: List[str] = []
    rejection_policy: str = ""
    output_format: str = ""
    api_config: Dict = {}

class ExpertUpdateRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    avatar: Optional[str] = None
    role_description: Optional[str] = None
    working_style: Optional[str] = None
    communication_style: Optional[str] = None
    decision_style: Optional[str] = None
    domains: Optional[List[str]] = None
    skills: Optional[List[Dict]] = None
    skill_ids: Optional[List[str]] = None
    behavior_rules: Optional[List[str]] = None
    rejection_policy: Optional[str] = None
    output_format: Optional[str] = None
    api_config: Optional[Dict] = None
    status: Optional[str] = None

class ExpertMemoryRequest(BaseModel):
    content: str
    memory_type: str = "context"   # decision / feedback / context / issue / milestone
    importance: float = 1.0
    scope: str = "project"         # project / phase
    phase_id: str = ""

class ExpertMatchRequest(BaseModel):
    required_role: str
    required_domains: List[str] = []
    required_skills: List[str] = []
    agent_type: Optional[str] = None
    top_k: int = 3
    exclude_busy: bool = False

class ExpertTrainingRequest(BaseModel):
    """添加训练会话请求"""
    session_type: str = "feedback"          # feedback/correction/style_tune/logic_tune/output_tune
    feedback: str                           # 用户反馈内容（必填）
    user_input: Optional[str] = None        # 用户给的原始输入（可选）
    agent_output: Optional[str] = None      # agent 当时的输出（可选，用于对比）
    correction: Optional[str] = None        # 期望的正确输出/行为（可选）

class ExpertWorkModeRequest(BaseModel):
    """更新工作模式请求（支持部分更新）"""
    name: Optional[str] = None
    description: Optional[str] = None
    thinking_style: Optional[str] = None   # chain_of_thought/step_by_step/direct/socratic
    execution_style: Optional[str] = None  # conservative/balanced/aggressive
    verbosity: Optional[str] = None        # brief/normal/detailed
    enable_cot: Optional[bool] = None
    enable_self_check: Optional[bool] = None
    custom_prompt_addon: Optional[str] = None

class ExpertConfigRequest(BaseModel):
    """更新专家配置（工作模式 + 思考框架 + 项目背景 + 偏好）"""
    thinking_framework: Optional[str] = None
    project_background: Optional[str] = None
    user_preferences: Optional[List[str]] = None
    long_term_memory: Optional[str] = None

class ExpertChatTrainRequest(BaseModel):
    """对话训练请求"""
    message: str
    history: Optional[List[Dict]] = None   # [{"role": "user"/"assistant", "content": "..."}]

class ExpertFeedbackRequest(BaseModel):
    """对话中的即时反馈（纠正专家回答）"""
    session_type: str = "correction"       # correction/style_tune/logic_tune/output_tune
    user_input: str = ""                   # 用户当时的问题
    agent_output: str = ""                 # 专家当时的回答
    feedback: str                          # 用户反馈（必填）
    correction: str = ""                   # 期望的正确回答（可选）
    auto_apply: bool = False               # 是否立即提炼到长期记忆

class ExpertKnowledgeRequest(BaseModel):
    """知识文件上传请求（文本内容）"""
    content: str                           # 知识内容（文件文本或粘贴文本）
    source_name: str = ""                  # 来源名称（文件名或标题）
    knowledge_type: str = "document"       # document/rule/example/reference

class CCBCheckDeleteMemberRequest(BaseModel):
    team_type: str          # pm_team / supervisor_team / management / execution
    member_id: str
    member_name: str
    is_leader: bool = False
    current_count: int = 0
    is_in_project: bool = False

class CCBCheckDeleteExpertRequest(BaseModel):
    expert_id: str
    expert_name: str
    is_in_project: bool = False
    current_project_name: str = ""
    current_count: int = 0

class CCBConfirmDeleteRequest(BaseModel):
    confirm_token: str

class EngineerRepairChatRequest(BaseModel):
    defect_id: str
    message: str
    defect_info: Optional[Dict] = None
    # 同一 needs_manual 批次的所有缺陷，供 Agent 按文件聚合后一次性处理
    all_defects: Optional[List[Dict]] = None

class EngineerConfirmFixRequest(BaseModel):
    defect_id: str
    proposal_digest: str
    observation_id: str
    file_path: str
    allow_whole_file: bool = False

class EngineerIdentityReviewRequest(BaseModel):
    rule_id: str
    symbol: str = ""
    location: str = ""
    expected: Optional[str] = None
    actual: Optional[str] = None
    review_reason: str

class EngineerApplyFixRequest(BaseModel):
    defect_id: str
    file_path: str
    new_content: str = ""          # 为空时由 Agent 根据 defect_info + 对话历史自动生成
    run_qa: bool = True
    defect_info: Optional[Dict] = None   # 缺陷单详情（供 LLM 精准生成时使用）
    confirmed_plan_version: str = ""  # 服务端 chat/repair 返回的已确认整改方案版本

class EngineerManualRequest(BaseModel):
    manual_type: str = "user"   # user / api / deploy
    extra_instruction: str = ""

class EngineerQAChatRequest(BaseModel):
    message: str

class EngineerQAInspectRequest(BaseModel):
    subproject_id: str
    output_files: Optional[List[str]] = None
    subproject_description: str = ""
    subproject_name: str = ""
    is_final_phase: bool = False

class AdjustmentChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict]] = None
    context_summary: Optional[str] = None

class AdjustmentConfirmRequest(BaseModel):
    adjustment_id: str
    confirmed: bool
    modifications: Optional[str] = ""

class AdjustmentPhaseConfirmRequest(BaseModel):
    adjustment_id: str
    phase_index: int   # 0-based 阶段序号

class InjectQCRequest(BaseModel):
    qc_results: Dict  # { sp_id: { issues_detail: [...], ... } }

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

# ═══════════════════════════════════════════════════════════════
# 认证相关 Schema
# ═══════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    login: str = ""          # 用户名或邮箱
    password: str

    @field_validator("login")
    @classmethod
    def validate_login(cls, value: str) -> str:
        normalized = value.strip()
        if "@" in normalized:
            if len(normalized) > 254 or normalized.startswith("@") or normalized.endswith("@"):
                raise ValueError("请输入有效的用户名或邮箱")
            return normalized
        from core.auth import validate_username
        return validate_username(normalized)

class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str

    @field_validator("username")
    @classmethod
    def validate_username_field(cls, value: str) -> str:
        from core.auth import validate_username
        return validate_username(value)

class VerifyEmailRequest(BaseModel):
    email: str
    code: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class ResendVerificationRequest(BaseModel):
    email: str
