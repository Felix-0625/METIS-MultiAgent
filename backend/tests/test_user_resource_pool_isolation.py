from core import expert_pool, global_agent_pool
from core.app_state import (
    _legacy_sm_agent,
    _persisted_user_skills,
    _user_sm_agents,
    get_user_sm_agent,
)
from core.expert_pool import ExpertProfile
from core.global_agent_pool import EmployeeProfile
from core.user_scope import current_user_id


def test_member_and_expert_pools_are_isolated_by_active_user(monkeypatch, tmp_path):
    monkeypatch.setattr(global_agent_pool, "metis_data_path", lambda *_a, **_k: tmp_path / "members")
    monkeypatch.setattr(expert_pool, "metis_data_path", lambda *_a, **_k: tmp_path / "experts")
    global_agent_pool._global_pools.clear()
    expert_pool._user_expert_pools.clear()

    token = current_user_id.set("user-a")
    try:
        member_a = global_agent_pool.get_global_agent_pool()
        member_a.create_employee(EmployeeProfile(
            employee_id="private-member-a", name="A", role="A", agent_type="pg",
        ))
        expert_a = expert_pool.get_expert_pool()
        expert_a.create_expert(ExpertProfile(
            expert_id="private-expert-a", name="A", role="A", agent_type="pg",
        ))
    finally:
        current_user_id.reset(token)

    token = current_user_id.set("user-b")
    try:
        member_b = global_agent_pool.get_global_agent_pool()
        expert_b = expert_pool.get_expert_pool()
        assert member_b.get_employee("private-member-a") is None
        assert expert_b.get_expert("private-expert-a") is None
        assert member_b is not member_a
        assert expert_b is not expert_a
    finally:
        current_user_id.reset(token)


def test_new_user_expert_pool_contains_nine_presets(monkeypatch, tmp_path):
    monkeypatch.setattr(expert_pool, "metis_data_path", lambda *_a, **_k: tmp_path / "experts")
    expert_pool._user_expert_pools.clear()
    token = current_user_id.set("new-user")
    try:
        pool = expert_pool.get_expert_pool()
        presets = [item for item in pool.list_experts() if item.expert_id.startswith("preset-")]
        assert len(presets) == 9
        assert {item.role for item in presets} == {
            "前端专家", "后端专家", "数据库专家", "API设计专家", "架构师",
            "DevOps专家", "安全专家", "测试专家", "数据专家",
        }
    finally:
        current_user_id.reset(token)
        expert_pool._user_expert_pools.clear()


def test_skill_pool_is_an_independent_seeded_copy_per_user():
    _user_sm_agents.clear()
    _persisted_user_skills.clear()
    _legacy_sm_agent.skill_pool["builtin-test"] = {"id": "builtin-test", "name": "Builtin"}
    try:
        skills_a = get_user_sm_agent("user-a")
        skills_b = get_user_sm_agent("user-b")
        skills_a.skill_pool["private-skill-a"] = {"id": "private-skill-a", "name": "Private"}

        assert "builtin-test" in skills_a.skill_pool
        assert "builtin-test" in skills_b.skill_pool
        assert "private-skill-a" not in skills_b.skill_pool
        assert skills_a.skill_pool is not skills_b.skill_pool
    finally:
        _legacy_sm_agent.skill_pool.pop("builtin-test", None)
        _user_sm_agents.clear()
        _persisted_user_skills.clear()
