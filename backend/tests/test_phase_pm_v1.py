import asyncio
import json
from types import SimpleNamespace

from api import routes_experts, routes_phases
from api import routes_execution
from core.expert_pool import get_expert_pool
from core.phase_manager import PhaseManager
from core import persistence


def test_phase_discussion_rows_only_use_latest_pm_plan_and_skip_table_header():
    discussion = """assistant:
| # | 任务 | 验收标准 |
|---|---|---|
| 1 | 旧任务一 | 旧验收一 |
| 2 | 旧任务二 | 旧验收二 |
user:
请重新规划
assistant:
| # | 任务 | 验收标准 |
|---|---|---|
| 1 | 新任务一 | 新验收一 |
| 2 | 新任务二 | 新验收二 |
"""

    latest = routes_phases._latest_phase_pm_content(discussion)
    rows = routes_phases._phase_discussion_task_rows(latest)

    assert rows == [
        {"name": "新任务一", "objective": "新任务一", "acceptance": "新验收一"},
        {"name": "新任务二", "objective": "新任务二", "acceptance": "新验收二"},
    ]


def test_phase_discussion_rows_parse_numbered_markdown_task_sections():
    content = """## 阶段规划
**任务1：前后端联调**
- 前端所有 API 调用打通
- 验收：手动操作无报错

**任务2：重启恢复验证**
- 重启服务并检查数据
- 验收：任务和计时状态全部恢复
"""

    assert routes_phases._phase_discussion_task_rows(content) == [
        {
            "name": "前后端联调",
            "objective": "前端所有 API 调用打通",
            "acceptance": "手动操作无报错",
        },
        {
            "name": "重启恢复验证",
            "objective": "重启服务并检查数据",
            "acceptance": "任务和计时状态全部恢复",
        },
    ]


def test_deterministic_phase_plan_does_not_replace_unstructured_pm_agreement_with_old_work_items():
    phase = {
        "phase_id": "phase-3",
        "name": "联调验收",
        "objective": "完成联调与验收",
        "work_items": ["前后端联调", "重启恢复验证", "输出验收文档"],
    }
    plan = routes_phases._deterministic_phase_plan_v1(
        phase=phase,
        requirements_snapshot={
            "phase_user_requirements": "assistant:\n请把系统全面检查好，然后交付。",
            "inherited_technical_requirements": [],
        },
        expert_snapshot={
            "revision": "experts-v1",
            "experts": [{"expert_id": "expert-1", "role": "全栈专家"}],
        },
    )

    assert plan["tasks"] == []


def test_phase_discussion_rows_parse_numbered_markdown_without_task_prefix():
    content = """\
**二、任务拆分**
1. **任务数据模型设计**
- 字段：id、标题、优先级
- 验收：模型定义完整，字段类型与约束正确
2. **SQLite 持久化层**
- 使用 sqlite3 实现连接管理
- 验收：服务重启后数据不丢失
"""

    rows = routes_phases._phase_discussion_task_rows(content)

    assert [row["name"] for row in rows] == ["任务数据模型设计", "SQLite 持久化层"]
    assert rows[0]["acceptance"] == "模型定义完整，字段类型与约束正确"


def test_deterministic_phase_plan_records_all_predecessor_dependencies():
    phase = {
        "phase_id": "phase-1",
        "name": "Backend",
        "objective": "Implement backend",
    }
    plan = routes_phases._deterministic_phase_plan_v1(
        phase=phase,
        requirements_snapshot={
            "phase_user_requirements": "assistant:\n1. Model\n- acceptance: model works\n2. API\n- acceptance: API works\n3. Filters\n- acceptance: filters work",
            "inherited_technical_requirements": ["FastAPI"],
        },
        expert_snapshot={
            "revision": "experts-v1",
            "experts": [{"expert_id": "expert-1", "role": "backend"}],
        },
    )

    assert plan["tasks"][2]["dependencies"] == [
        "phase-1-task-1",
        "phase-1-task-2",
    ]


def _total_plan():
    return {
        "schema_version": "total-plan/v1",
        "project_name": "番茄时钟",
        "summary": "实现浏览器番茄时钟",
        "phases": [
            {
                "phase_id": "phase-1",
                "name": "计时核心",
                "objective": "完成计时与控制功能",
                "work_items": ["专注/休息计时", "开始、暂停和重置"],
                "technical_requirements": ["React", "Tailwind CSS"],
                "dependencies": [],
                "source_requirement_ids": ["req-1", "req-2"],
            },
            {
                "phase_id": "phase-2",
                "name": "统计与持久化",
                "objective": "完成计数和本地持久化",
                "work_items": ["今日完成数量", "localStorage 持久化"],
                "technical_requirements": ["React", "localStorage"],
                "dependencies": ["phase-1"],
                "source_requirement_ids": ["req-3"],
            },
        ],
        "project_contract": {
            "locked": True,
            "source_requirements": "构建番茄时钟",
            "requirements_revision": 1,
            "requirements_digest": "sha256:test",
            "required_files": [],
            "phases": [],
        },
    }


def test_phase_plan_restore_preserves_newer_execution_runtime(monkeypatch):
    runtime_phase = {
        "phase_id": "phase-1",
        "description": "runtime mirror of old plan",
        "status": "reviewing",
        "execution_generation": "generation-current",
        "execution_coordinator": {
            "status": "completed",
            "durable_run_id": "coordinator-current",
            "durable_status": "succeeded",
        },
        "execution_dispatch_result": {"success": True},
    }
    monkeypatch.setattr(
        persistence,
        "kv_get",
        lambda key, default=None: {
            "project": {
                "phases": [runtime_phase],
                "project_contract": {"locked": True, "contract_version": 3},
            },
        } if key == "phase_managers" else default,
    )
    monkeypatch.setattr(
        persistence,
        "list_phase_plan_commits",
        lambda: [{
            "project_id": "project",
            "phase_id": "phase-1",
            "phase": {
                "phase_id": "phase-1",
                "description": "authoritative committed plan",
                "status": "pending",
                "phase_plan_revision": 2,
                "phase_plan": {"schema_version": "phase-plan/v1"},
            },
            "project_contract": {"locked": True, "contract_version": 3},
        }],
    )

    restored = persistence.load_phase_managers()["project"]["phases"][0]

    assert restored["description"] == "authoritative committed plan"
    assert restored["phase_plan_revision"] == 2
    assert restored["status"] == "reviewing"
    assert restored["execution_generation"] == "generation-current"
    assert restored["execution_coordinator"]["durable_run_id"] == (
        "coordinator-current"
    )
    assert restored["execution_dispatch_result"] == {"success": True}


def test_uncommitted_conflicting_qa_scope_is_detached_without_budget_loss(
    monkeypatch,
):
    project_id = "project"
    phase_id = "phase-1"
    key = f"{project_id}-{phase_id}"
    phase = {"phase_id": phase_id, "reviewed": True, "review_passed": False}
    state = {
        "status": "blocked",
        "action_required": {
            "message": "Conflicting replay for active qa_round_id",
        },
    }
    machine = {
        "run_id": "qa-run-old",
        "state": "qa_running",
        "business_rounds_used": 0,
        "active_qa_round_id": "qa-old:1",
        "scope": {
            "scope_digest": "same-scope",
            "phase_generation_id": "same-generation",
        },
        "rounds": [{
            "qa_round_id": "qa-old:1",
            "consumes_business_round": False,
            "qa_snapshot_committed": False,
        }],
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        supervisor_quality_runs={phase_id: machine},
        qc_results={phase_id: {"passed": False}},
    )
    monkeypatch.setitem(routes_phases._auto_repair_states, key, state)
    monkeypatch.setitem(routes_phases._auto_repair_api_configs, key, {"model": "x"})
    monkeypatch.setattr(
        routes_phases,
        "_supervisor_scope_snapshot",
        lambda _ctx, _phase_id: {
            "scope_digest": "same-scope",
            "phase_generation_id": "same-generation",
        },
    )

    detached = routes_phases._detach_uncommitted_stale_qa_scope(
        ctx, phase_id, phase,
    )

    assert detached is True
    assert key not in routes_phases._auto_repair_states
    assert key not in routes_phases._auto_repair_api_configs
    assert ctx.supervisor_quality_runs == {}
    assert ctx.qc_results == {}
    assert phase["reviewed"] is False
    assert phase["superseded_quality_runs"][-1]["business_rounds_used"] == 0


def test_total_plan_maps_exactly_to_phase_board_with_full_phase_scope(tmp_path):
    manager = PhaseManager("project", tmp_path)

    phases = manager.init_phases_from_plan(_total_plan())

    assert [phase["phase_id"] for phase in phases] == ["phase-1", "phase-2"]
    assert phases[0]["description"] == "完成计时与控制功能"
    assert phases[0]["objective"] == "完成计时与控制功能"
    assert phases[0]["work_items"] == ["专注/休息计时", "开始、暂停和重置"]
    assert phases[0]["technical_requirements"] == ["React", "Tailwind CSS"]
    assert phases[0]["roles_needed"] == []
    assert phases[0]["task_contract"] == []


def test_phase_plan_v1_parser_and_gate_accept_dynamic_experts_without_files():
    experts = {
        "revision": "sha256:pool",
        "experts": [
            {"expert_id": "expert-frontend", "name": "前端专家", "role": "前端开发"},
            {"expert_id": "expert-qa", "name": "测试专家", "role": "QA工程师"},
        ],
    }
    phase = _total_plan()["phases"][0]
    snapshot = {
        "inherited_technical_requirements": ["React", "Tailwind CSS"],
        "phase_user_requirements": "",
    }
    payload = {
        "schema_version": "phase-plan/v1",
        "phase_id": "phase-1",
        "summary": "完成核心计时功能",
        "effective_technical_requirements": ["React", "Tailwind CSS"],
        "technical_overrides": [],
        "tasks": [{
            "task_id": "phase-1-task-1",
            "name": "实现计时器",
            "objective": "完成专注和休息计时",
            "functional_details": ["25 分钟专注", "5 分钟休息", "暂停与重置"],
            "implementation": "使用 React 状态和定时器实现",
            "implementation_technologies": ["React"],
            "dependencies": [],
            "acceptance_criteria": ["计时、暂停和重置行为可验证"],
        }],
        "assignments": [{
            "expert_id": "expert-frontend",
            "task_ids": ["phase-1-task-1"],
            "responsibility": "实现计时器及交互",
        }],
        "expert_pool_revision": "sha256:pool",
    }

    parsed = routes_phases._parse_phase_plan_v1(json.dumps(payload, ensure_ascii=False))
    issues = routes_phases._validate_phase_plan_v1(
        parsed,
        phase=phase,
        requirements_snapshot=snapshot,
        expert_snapshot=experts,
        reserved_files={},
    )
    tasks = routes_phases._phase_plan_execution_tasks(parsed, experts)

    assert issues == []
    assert tasks[0]["required_role"] == "前端开发"
    assert tasks[0]["required_files"] == []
    assert tasks[0]["assigned_expert_ids"] == ["expert-frontend"]


def test_phase_plan_v1_parser_skips_nested_deliverable_object_before_full_plan():
    payload = {
        "schema_version": "phase-plan/v1",
        "phase_id": "phase-1",
        "summary": "完整阶段规划",
        "effective_technical_requirements": ["React"],
        "technical_overrides": [],
        "tasks": [],
        "assignments": [],
        "expert_pool_revision": "sha256:pool",
    }
    content = (
        '分析片段：{"path":"src/Timer.jsx","action":"create",'
        '"purpose":"计时器"}\n最终结果：'
        + json.dumps(payload, ensure_ascii=False)
    )

    assert routes_phases._parse_phase_plan_v1(content) == payload
    assert routes_phases._parse_phase_plan_v1(
        '{"path":"src/Timer.jsx","action":"create","purpose":"计时器"}'
    ) is None


def test_phase_plan_v1_selection_survives_persisted_or_regenerated_phase():
    phase = _total_plan()["phases"][0]

    assert routes_phases._uses_phase_plan_v1(phase, None) is True

    phase["task_contract"] = [{"task_id": "phase-1-task-1"}]
    phase["phase_plan_version"] = "phase-plan/v1"
    assert routes_phases._uses_phase_plan_v1(phase, None) is True

    legacy_phase = {
        "phase_id": "phase-legacy",
        "task_contract": [{"task_id": "legacy-task-1"}],
    }
    assert routes_phases._uses_phase_plan_v1(legacy_phase, None) is False


def test_phase_plan_v1_prompt_is_utf8_and_contains_no_legacy_contract_fields():
    messages = routes_phases._phase_plan_v1_messages(
        requirements_snapshot={
            "schema_version": "phase-requirements/v1",
            "current_phase": {"phase_id": "phase-1", "name": "计时核心"},
        },
        expert_snapshot={
            "revision": "sha256:pool",
            "experts": [{"expert_id": "expert-frontend", "role": "前端开发"}],
        },
        reserved_files={},
    )
    system = messages[0].content

    assert "你是 MeTis 阶段 PM" in system
    assert '"schema_version":"phase-plan/v1"' in system
    assert '"deliverable_files"' not in system
    assert "不要规划文件路径、文件数量或文件归属" in system
    assert '"reserved_files"' not in messages[1].content
    assert "required_role" not in system
    assert "JSON 数组" not in system
    assert "\ufffd" not in system
    assert "浣犳槸" not in system
    assert "闃舵" not in system


def test_custom_expert_role_keeps_identity_and_uses_supported_executor_type():
    plan = {
        "tasks": [{
            "task_id": "phase-1-task-1",
            "name": "声音提醒",
            "objective": "实现番茄时钟声音反馈",
            "functional_details": ["计时结束播放提示音"],
            "implementation": "使用 Web Audio API",
            "implementation_technologies": ["Web Audio API"],
            "dependencies": [],
            "acceptance_criteria": ["计时结束可听到提示音"],
        }],
        "assignments": [{
            "expert_id": "expert-sound",
            "task_ids": ["phase-1-task-1"],
            "responsibility": "负责声音体验",
        }],
    }
    experts = {
        "revision": "sha256:pool",
        "experts": [{
            "expert_id": "expert-sound",
            "name": "声音体验专家",
            "role": "声音体验设计师",
        }],
    }

    task = routes_phases._phase_plan_execution_tasks(plan, experts)[0]

    assert task["required_role"] == "声音体验设计师"
    assert task["assigned_expert_ids"] == ["expert-sound"]
    assert task["executor_type"] == "fullstack_engineer"


def test_pathless_tasks_still_map_to_fixed_runtime_executors():
    assert routes_phases._phase_task_executor_type({
        "name": "实现浏览器计数器",
        "objective": "完成前端交互界面",
        "functional_details": ["点击按钮更新计数"],
        "implementation": "使用原生 HTML 和 JavaScript",
        "implementation_technologies": ["HTML", "CSS"],
    }, ["浏览器体验匠"]) == "frontend"
    assert routes_phases._phase_task_executor_type({
        "name": "功能测试",
        "objective": "验证计数和持久化行为",
        "functional_details": ["测试增加、重置和刷新恢复"],
        "implementation": "编写自动化测试",
        "implementation_technologies": ["Playwright"],
    }, ["质量体验匠"]) == "qa"


def test_pathless_task_uses_workspace_exclusive_transaction_scope():
    assert routes_execution._execution_transaction_scopes(
        SimpleNamespace(),
        "agent-test",
        {
            "required_files": [],
            "allowed_path_prefixes": [],
            "workspace_exclusive": True,
        },
    ) == ["*"]


def test_pathless_task_clears_both_artifact_scope_aliases():
    policy = routes_phases._locked_task_artifact_policy(
        {"allowed_prefixes": ["backend/"], "kind": "code"},
        task_id="phase-1-task-1",
        task_dependencies=[],
        required_files=[],
        rebuild_file_specs=[],
    )

    assert policy["required_files"] == []
    assert policy["allowed_prefixes"] == []
    assert policy["allowed_path_prefixes"] == []
    assert policy["workspace_exclusive"] is True


def test_pathless_phase_planning_lock_does_not_claim_workspace():
    assert routes_phases._phase_planning_lock_scope([]) == []
    assert routes_phases._phase_planning_lock_scope(
        ["src/app.js"],
    ) == ["src/app.js"]


def test_phase_planning_locks_are_released_before_execution(monkeypatch):
    released = []
    agents = [
        {"id": "agent-1", "lock_id": "planning-1", "locked_until": 123.0},
        {"id": "agent-2", "lock_id": "planning-2", "locked_until": 456.0},
        {"id": "agent-3", "lock_id": None, "locked_until": None},
        {
            "id": "agent-running", "lock_id": "execution-1",
            "lock_run_id": "run-1", "locked_until": 789.0,
        },
    ]
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "release_lock",
        lambda lock_id: released.append(lock_id) or {"success": True},
    )

    routes_phases._release_phase_planning_locks(agents)

    assert released == ["planning-1", "planning-2"]
    assert [agent.get("lock_id") for agent in agents] == [
        None, None, None, "execution-1",
    ]
    assert agents[-1]["locked_until"] == 789.0


def test_workspace_exclusive_tasks_are_isolated_into_serial_waves():
    plan = {
        "phase_id": "phase-1",
        "task_ids": ["task-a", "task-b", "task-c"],
        "waves": [[
            {"task_id": "task-a"},
            {"task_id": "task-b"},
            {"task_id": "task-c"},
        ]],
    }

    execution_plan = routes_phases._isolate_workspace_exclusive_tasks(
        plan, {"task-b"},
    )

    assert [[task["task_id"] for task in wave]
            for wave in execution_plan["waves"]] == [
        ["task-a"], ["task-b"], ["task-c"],
    ]
    assert plan["waves"][0][1]["task_id"] == "task-b"


def test_phase_plan_gate_requires_one_expert_per_task():
    phase = _total_plan()["phases"][0]
    payload = {
        "schema_version": "phase-plan/v1",
        "phase_id": "phase-1",
        "summary": "计时器",
        "effective_technical_requirements": ["React", "Tailwind CSS"],
        "technical_overrides": [],
        "tasks": [{
            "task_id": "phase-1-task-1",
            "name": "计时器",
            "objective": "实现计时",
            "functional_details": ["开始和暂停"],
            "implementation": "React Hook",
            "implementation_technologies": ["React"],
            "dependencies": [],
            "acceptance_criteria": ["计时正确"],
        }],
        "assignments": [
            {
                "expert_id": "expert-a",
                "task_ids": ["phase-1-task-1"],
                "responsibility": "实现",
            },
            {
                "expert_id": "expert-b",
                "task_ids": ["phase-1-task-1"],
                "responsibility": "协作",
            },
        ],
        "expert_pool_revision": "sha256:pool",
    }

    issues = routes_phases._validate_phase_plan_v1(
        payload,
        phase=phase,
        requirements_snapshot={
            "inherited_technical_requirements": ["React", "Tailwind CSS"],
        },
        expert_snapshot={
            "revision": "sha256:pool",
            "experts": [
                {"expert_id": "expert-a", "role": "任意角色 A"},
                {"expert_id": "expert-b", "role": "任意角色 B"},
            ],
        },
        reserved_files={},
    )

    assert "task_expert_count" in {issue["code"] for issue in issues}


def test_phase_start_preflight_detects_deleted_assigned_expert():
    phase = {
        "phase_plan_version": "phase-plan/v1",
        "expert_pool_revision": "sha256:old",
        "expert_requirements": [{
            "task_id": "phase-1-task-1",
            "required_role": "任意角色",
            "executor_type": "fullstack_engineer",
            "assigned_expert_ids": ["expert-deleted"],
        }],
    }

    issues = routes_phases._phase_v1_expert_binding_issues(
        phase,
        {"revision": "sha256:new", "experts": []},
    )

    assert {issue["code"] for issue in issues} == {
        "assigned_expert_unavailable",
    }


def test_phase_start_allows_unrelated_expert_pool_revision_change():
    phase = {
        "phase_plan_version": "phase-plan/v1",
        "expert_pool_revision": "sha256:old",
        "expert_requirements": [{
            "task_id": "phase-1-task-1",
            "required_role": "任意角色",
            "executor_type": "fullstack_engineer",
            "assigned_expert_ids": ["expert-kept"],
        }],
    }

    issues = routes_phases._phase_v1_expert_binding_issues(
        phase,
        {
            "revision": "sha256:new",
            "experts": [
                {"expert_id": "expert-kept"},
                {"expert_id": "expert-new"},
            ],
        },
    )

    assert issues == []


def test_phase_start_uses_exact_custom_role_expert_without_fallback(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("METIS_TEST_MODE", "1")
    project_id = "custom-expert-start"
    phase_id = "phase-1"

    class Profile:
        expert_id = "expert-sound"
        name = "声音体验专家"
        role = "声音体验设计师"
        status = "available"
        avg_quality_score = 88.0
        domains = ["Web Audio"]

        def to_dict(self):
            return {
                "expert_id": self.expert_id,
                "name": self.name,
                "role": self.role,
                "agent_type": "pg",
                "domains": self.domains,
                "skills": [],
                "status": self.status,
                "updated_at": 1,
            }

    profile = Profile()

    class Pool:
        def list_experts(self, status=None):
            return [profile] if status in (None, "available") else []

        def get_expert(self, expert_id):
            return profile if expert_id == profile.expert_id else None

        def match_experts(self, **_kwargs):
            raise AssertionError("fixed role rematching must not replace assignment")

    pool = Pool()
    revision = routes_phases._expert_pool_snapshot(pool)["revision"]
    manager = PhaseManager(project_id, tmp_path)
    manager.init_phases_from_plan(_total_plan())
    phase = manager.get_phase(phase_id)
    phase.update({
        "phase_plan_version": "phase-plan/v1",
        "phase_plan": {
            "schema_version": "phase-plan/v1",
            "phase_id": phase_id,
        },
        "expert_pool_revision": revision,
        "plan_contract_validated": True,
        "roles_needed": [profile.role],
        "execution_roles": ["fullstack_engineer"],
        "task_contract": [{
            "task_id": "phase-1-task-1",
            "name": "声音提醒",
            "required_role": profile.role,
            "executor_type": "fullstack_engineer",
            "assigned_expert_ids": [profile.expert_id],
            "required_files": ["src/audio/notification.js"],
            "dependencies": [],
            "acceptance_criteria": ["声音可播放"],
        }],
        "expert_requirements": [{
            "task_id": "phase-1-task-1",
            "task_name": "声音提醒",
            "task_description": "实现声音反馈",
            "required_role": profile.role,
            "executor_type": "fullstack_engineer",
            "assigned_expert_ids": [profile.expert_id],
            "required_files": ["src/audio/notification.js"],
            "dependencies": [],
            "acceptance_criteria": ["声音可播放"],
        }],
    })
    manager.project_contract = {
        "locked": False,
        "required_files": [{
            "path": "src/audio/notification.js",
            "phase_id": phase_id,
            "task_id": "phase-1-task-1",
            "owner_role": profile.role,
            "owner_type": "fullstack_engineer",
            "required": True,
        }],
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        owner_user_id="",
        workspace=tmp_path,
        status="planning",
        description="custom expert project",
        pm=SimpleNamespace(context_summary="context"),
        agents={},
        subprojects=[],
        qc_results={},
        _derive_project_status=lambda _rows: "running",
    )

    async def persist():
        return None

    def discard_task(coro, name=""):
        coro.close()

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, manager)
    monkeypatch.setitem(
        routes_phases._pm_teams,
        project_id,
        SimpleNamespace(
            canonical_requirements=(
                "完整规范：必须实现声音提醒；禁止 Docker。"
            ),
            requirements_revision=0,
            requirements_digest="",
        ),
    )
    monkeypatch.setattr("core.expert_pool.get_expert_pool", lambda: pool)
    monkeypatch.setattr(
        "core.global_agent_pool.get_global_agent_pool",
        lambda: object(),
    )
    monkeypatch.setattr(
        "core.dispatch_integration.register_phase_agents",
        lambda *_args: 1,
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "atomic_claim_lock",
        lambda **_kwargs: {
            "success": True,
            "lock_id": "lock-custom",
            "leased_until": 9999999999,
        },
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)
    monkeypatch.setattr(routes_execution, "_safe_create_task", discard_task)

    result = asyncio.run(routes_phases.start_phase(project_id, phase_id))

    assert result["success"] is True
    assert len(result["created_agents"]) == 1
    agent = result["created_agents"][0]
    assert agent["expert_id"] == profile.expert_id
    assert agent["role"] == profile.name
    assert agent["required_role"] == profile.role
    assert agent["expert_type"] == "fullstack_engineer"
    assert agent["assigned_task_ids"] == ["phase-1-task-1"]
    assert not agent["expert_id"].startswith("fallback:")
    assert agent["execution_contract"]["project_context"] == (
        "完整规范：必须实现声音提醒；禁止 Docker。"
    )


def test_phase_plan_gate_accepts_no_files_but_rejects_unknown_experts_and_stale_pool():
    phase = _total_plan()["phases"][0]
    payload = {
        "schema_version": "phase-plan/v1",
        "phase_id": "phase-1",
        "summary": "无效规划",
        "effective_technical_requirements": [],
        "technical_overrides": [],
        "tasks": [{
            "task_id": "phase-1-task-1",
            "name": "计时器",
            "objective": "实现计时",
            "functional_details": ["计时"],
            "implementation": "实现",
            "implementation_technologies": [],
            "dependencies": [],
            "acceptance_criteria": ["通过"],
        }],
        "assignments": [{
            "expert_id": "invented-expert",
            "task_ids": ["phase-1-task-1"],
            "responsibility": "实现",
        }],
        "expert_pool_revision": "sha256:stale",
    }
    experts = {
        "revision": "sha256:current",
        "experts": [{"expert_id": "expert-frontend", "role": "前端开发"}],
    }

    issues = routes_phases._validate_phase_plan_v1(
        payload,
        phase=phase,
        requirements_snapshot={
            "inherited_technical_requirements": ["React", "Tailwind CSS"],
            "phase_user_requirements": "",
        },
        expert_snapshot=experts,
        reserved_files={},
    )

    assert {
        issue["code"] for issue in issues
    } >= {
        "expert_pool_revision_mismatch",
        "unknown_expert_id",
        "inherited_technology_missing",
    }


def test_phase_plan_gate_rejects_removed_deliverable_files_field():
    phase = _total_plan()["phases"][1]
    payload = {
        "schema_version": "phase-plan/v1",
        "phase_id": "phase-2",
        "summary": "统计",
        "effective_technical_requirements": ["React", "localStorage"],
        "technical_overrides": [],
        "tasks": [{
            "task_id": "phase-2-task-1",
            "name": "完成统计",
            "objective": "显示今日计数",
            "functional_details": ["今日完成数量"],
            "implementation": "读取 localStorage",
            "implementation_technologies": ["React", "localStorage"],
            "deliverable_files": [{
                "path": "src/App.jsx",
                "action": "modify",
                "purpose": "增加今日计数",
            }],
            "dependencies": [],
            "acceptance_criteria": ["今日计数正确显示"],
        }],
        "assignments": [{
            "expert_id": "expert-frontend",
            "task_ids": ["phase-2-task-1"],
            "responsibility": "实现统计",
        }],
        "expert_pool_revision": "sha256:pool",
    }

    issues = routes_phases._validate_phase_plan_v1(
        payload,
        phase=phase,
        requirements_snapshot={
            "inherited_technical_requirements": ["React", "localStorage"],
            "phase_user_requirements": "",
        },
        expert_snapshot={
            "revision": "sha256:pool",
            "experts": [{"expert_id": "expert-frontend", "role": "前端开发"}],
        },
        reserved_files={
            "src/App.jsx": {
                "phase_id": "phase-1",
                "task_id": "phase-1-task-1",
            },
        },
    )

    assert {issue["code"] for issue in issues} >= {"unexpected_task_fields"}


def test_expert_crud_and_phase_planning_share_one_live_pool():
    assert routes_experts._get_expert_pool() is get_expert_pool()


def test_phase_plan_no_longer_plans_project_root_files():
    phase = _total_plan()["phases"][0]
    plan = {
        "schema_version": "phase-plan/v1",
        "phase_id": "phase-1",
        "summary": "项目初始化",
        "effective_technical_requirements": ["React", "Tailwind CSS"],
        "technical_overrides": [],
        "tasks": [{
            "task_id": "phase-1-task-1",
            "name": "初始化",
            "objective": "建立前端项目",
            "functional_details": ["建立项目"],
            "implementation": "初始化 React",
            "implementation_technologies": ["React"],
            "dependencies": [],
            "acceptance_criteria": ["项目可安装"],
        }],
        "assignments": [{
            "expert_id": "expert-frontend",
            "task_ids": ["phase-1-task-1"],
            "responsibility": "初始化项目",
        }],
        "expert_pool_revision": "sha256:pool",
    }

    issues = routes_phases._validate_phase_plan_v1(
        plan,
        phase=phase,
        requirements_snapshot={
            "project_requirements": {"content": "构建单一 React 应用"},
            "inherited_technical_requirements": ["React", "Tailwind CSS"],
            "phase_user_requirements": "",
        },
        expert_snapshot={
            "revision": "sha256:pool",
            "experts": [{"expert_id": "expert-frontend", "role": "前端开发"}],
        },
        reserved_files={},
    )

    assert issues == []


def test_phase_plan_retry_bypasses_cached_invalid_result(monkeypatch):
    phase = _total_plan()["phases"][0]

    class Profile:
        def to_dict(self):
            return {
                "expert_id": "expert-frontend",
                "name": "前端专家",
                "role": "前端开发",
                "agent_type": "pg",
                "domains": ["React"],
                "skills": [],
                "status": "available",
                "updated_at": 1,
            }

    class Pool:
        def list_experts(self, status=None):
            return [Profile()]

    expert_snapshot = routes_phases._expert_pool_snapshot(Pool())

    def plan(task_numbers):
        tasks = []
        for number in task_numbers:
            task_id = f"phase-1-task-{number}"
            tasks.append({
                "task_id": task_id,
                "name": f"任务 {number}",
                "objective": f"完成任务 {number}",
                "functional_details": [f"功能 {number}"],
                "implementation": "使用 React 实现",
                "implementation_technologies": ["React"],
                "dependencies": [],
                "acceptance_criteria": [f"任务 {number} 可验证"],
            })
        return {
            "schema_version": "phase-plan/v1",
            "phase_id": "phase-1",
            "summary": "完成计时核心",
            "effective_technical_requirements": ["React", "Tailwind CSS"],
            "technical_overrides": [],
            "tasks": tasks,
            "assignments": [{
                "expert_id": "expert-frontend",
                "task_ids": [task["task_id"] for task in tasks],
                "responsibility": "实现阶段任务",
            }],
            "expert_pool_revision": expert_snapshot["revision"],
        }

    generated = [
        {"content": json.dumps(plan([1, 3, 2]), ensure_ascii=False)},
        {"content": json.dumps(plan([1, 2, 3]), ensure_ascii=False)},
    ]
    cached = None
    cache_flags = []
    seen_messages = []

    def cache_sensitive_chat(_messages, use_cache=True, **_kwargs):
        nonlocal cached
        cache_flags.append(use_cache)
        seen_messages.append(_messages)
        if use_cache and cached is not None:
            return cached
        response = generated.pop(0)
        if use_cache:
            cached = response
        return response

    monkeypatch.setattr(
        "core.expert_pool.get_expert_pool",
        lambda _owner_user_id="": Pool(),
    )
    monkeypatch.setattr(routes_phases.hermes_client, "chat", cache_sensitive_chat)
    leader = SimpleNamespace(
        final_plan=_total_plan(),
        canonical_requirements="构建番茄时钟",
        requirements_revision=1,
        requirements_digest="sha256:test",
    )
    pm = SimpleNamespace(project_contract={"required_files": [], "phases": []})
    ctx = SimpleNamespace(
        name="番茄时钟",
        owner_user_id="user-1",
    )

    result = asyncio.run(routes_phases._generate_phase_plan_v1(
        project_id="project-1",
        ctx=ctx,
        pm=pm,
        phase=phase,
        leader=leader,
        phase_user_requirements="",
    ))

    assert result["status"] == "saved"
    assert cache_flags == [False]
    assert [task["task_id"] for task in phase["phase_plan"]["tasks"]] == [
        "phase-1-task-1",
        "phase-1-task-2",
    ]
    assert phase["plan_generation_attempts"][0]["issues"][0]["code"] == (
        "task_id_sequence"
    )


def test_phase_plan_falls_back_to_valid_contract_after_invalid_model_output(
    monkeypatch,
):
    phase = _total_plan()["phases"][0]

    class Profile:
        def to_dict(self):
            return {
                "expert_id": "expert-frontend",
                "name": "Frontend Expert",
                "role": "frontend",
                "agent_type": "pg",
                "domains": ["React"],
                "skills": [],
                "status": "available",
                "updated_at": 1,
            }

    class Pool:
        def list_experts(self, status=None):
            return [Profile()]

    monkeypatch.setattr(
        "core.expert_pool.get_expert_pool",
        lambda _owner_user_id="": Pool(),
    )
    monkeypatch.setattr(
        routes_phases.hermes_client,
        "chat",
        lambda *_args, **_kwargs: {"content": "not a phase plan"},
    )
    leader = SimpleNamespace(
        final_plan=_total_plan(),
        canonical_requirements="Build a focus timer",
        requirements_revision=1,
        requirements_digest="sha256:test",
    )
    pm = SimpleNamespace(project_contract={"required_files": [], "phases": []})
    ctx = SimpleNamespace(name="Focus timer", owner_user_id="user-1")

    result = asyncio.run(routes_phases._generate_phase_plan_v1(
        project_id="fallback-project",
        ctx=ctx,
        pm=pm,
        phase=phase,
        leader=leader,
        phase_user_requirements="arbitrary discussion text",
    ))

    assert result["status"] == "saved"
    assert phase["plan_generated"] is True
    assert phase["phase_plan"]["schema_version"] == "phase-plan/v1"
    assert phase["phase_plan"]["tasks"]
    assert phase["expert_requirements"]
    assert phase["plan_generation_attempts"][-1]["status"] == (
        "deterministic_fallback"
    )
    assert routes_phases._validate_phase_plan_v1(
        phase["phase_plan"],
        phase=phase,
        requirements_snapshot=phase["phase_requirements_snapshot"],
        expert_snapshot=routes_phases._expert_pool_snapshot(Pool()),
        reserved_files={},
    ) == []
