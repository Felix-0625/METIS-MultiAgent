"""Full-flow tests for durable application-state recovery.

The main test writes project, phase, agent, QC, and workspace state to SQLite,
discards the in-memory process state, and restores the snapshot exactly as a
restarted backend does.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import app_state, database, expert_lock
from core.phase_manager import PhaseManager
from core.project_context import ProjectContext


class PersistenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.original_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        os.chdir(self.root)

        self.original = {
            "projects": dict(app_state.projects),
            "agents_config": dict(app_state.agents_api_config),
            "user_api_configs": dict(app_state.user_api_configs),
            "skills": dict(app_state.global_sm_agent.skill_pool),
            "pm_teams": dict(app_state._pm_teams),
            "phase_managers": dict(app_state._phase_managers),
            "supervisor_leaders": dict(app_state._supervisor_leaders),
        }
        self.patchers = [
            patch.object(database, "DATABASE_URL", ""),
            patch.object(database, "REQUIRE_DATABASE_URL", False),
            patch.object(database, "_pg_pool", None),
            patch.object(database, "_sqlite_path", str(self.root / "state.db")),
            patch(
                "core.project_context._project_workspace",
                side_effect=lambda name, project_id: self.root / "projects" / project_id,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

        app_state._persist_lock = asyncio.Lock()
        app_state.projects.clear()
        app_state.agents_api_config.clear()
        app_state.user_api_configs.clear()
        app_state.global_sm_agent.skill_pool.clear()
        app_state._pm_teams.clear()
        app_state._phase_managers.clear()
        app_state._supervisor_leaders.clear()
        database.init_db()

    def tearDown(self):
        app_state.projects.clear()
        app_state.projects.update(self.original["projects"])
        app_state.agents_api_config.clear()
        app_state.agents_api_config.update(self.original["agents_config"])
        app_state.user_api_configs.clear()
        app_state.user_api_configs.update(self.original["user_api_configs"])
        app_state.global_sm_agent.skill_pool.clear()
        app_state.global_sm_agent.skill_pool.update(self.original["skills"])
        app_state._pm_teams.clear()
        app_state._pm_teams.update(self.original["pm_teams"])
        app_state._phase_managers.clear()
        app_state._phase_managers.update(self.original["phase_managers"])
        app_state._supervisor_leaders.clear()
        app_state._supervisor_leaders.update(self.original["supervisor_leaders"])

        for patcher in reversed(self.patchers):
            patcher.stop()
        os.chdir(self.original_cwd)
        self.temp_dir.cleanup()

    def test_sqlite_kv_round_trip_and_literal_prefix(self):
        database.kv_many_set(
            {
                "project_%_literal": {"value": 1},
                "project_XX_literal": {"value": 2},
                "nullable": None,
            }
        )

        self.assertEqual(database.kv_get("project_%_literal"), {"value": 1})
        self.assertIsNone(database.kv_get("nullable", "fallback"))
        self.assertIsNone(database.kv_get("missing", None))
        self.assertEqual(database.kv_keys_prefix("project_%"), ["project_%_literal"])

        database.kv_delete("project_%_literal")
        self.assertIsNone(database.kv_get("project_%_literal", None))

    def test_database_url_selection_never_silently_falls_back(self):
        with patch.object(database, "DATABASE_URL", "postgres://db.example/metis"):
            self.assertTrue(database._use_postgres())
        with patch.object(database, "DATABASE_URL", "postgresql://db.example/metis"):
            self.assertTrue(database._use_postgres())

        fallback_path = self.root / "must-not-exist.db"
        with (
            patch.object(database, "DATABASE_URL", "mysql://db.example/metis"),
            patch.object(database, "_sqlite_path", str(fallback_path)),
            self.assertRaisesRegex(RuntimeError, "Unsupported DATABASE_URL scheme"),
        ):
            database.init_db()
        self.assertFalse(fallback_path.exists())

    def test_per_user_llm_configs_survive_application_restart(self):
        configs = {
            "user-a": {
                "model": "model-a", "api_base": "https://a.example/v1",
                "api_key": "secret-a", "max_tokens": 5000, "temperature": 0.2,
            },
            "user-b": {
                "model": "model-b", "api_base": "https://b.example/v1",
                "api_key": "secret-b", "max_tokens": 8192, "temperature": 0.4,
            },
        }
        with patch.dict(
            os.environ,
            {"METIS_DATA_ENCRYPTION_KEY": "", "JWT_SECRET": "stable-render-jwt-secret"},
            clear=False,
        ):
            app_state.user_api_configs.update(configs)
            app_state._do_persist_unlocked()
            raw = database.kv_get("user_api_configs")
            self.assertIsInstance(raw, str)
            self.assertNotIn("secret-a", raw)
            self.assertNotIn("secret-b", raw)

            app_state.user_api_configs.clear()
            app_state._restore_from_disk()

        self.assertEqual(app_state.user_api_configs, configs)
        self.assertNotEqual(
            app_state.user_api_configs["user-a"]["api_key"],
            app_state.user_api_configs["user-b"]["api_key"],
        )

    def test_reclaimed_rebuild_lock_becomes_active_again(self):
        created = expert_lock.atomic_claim_lock(
            expert_id="expert-backend",
            project_id="proj-lock-rebuild",
            task_id="phase-1",
            file_scope=["backend/"],
        )
        self.assertTrue(created["success"])
        expert_lock.release_lock(created["lock_id"])

        reclaimed = expert_lock.atomic_claim_lock(
            expert_id="expert-backend",
            project_id="proj-lock-rebuild",
            task_id="phase-1",
            file_scope=["backend/src/"],
        )

        self.assertTrue(reclaimed["success"])
        active = expert_lock.get_active_locks(project_id="proj-lock-rebuild")
        self.assertEqual(len(active), 1)
        self.assertIsNone(active[0]["released_at"])
        self.assertEqual(active[0]["file_scope"], ["backend/src/"])

    def test_atomic_claim_rejects_overlapping_scope_with_different_lock_id(self):
        first = expert_lock.atomic_claim_lock(
            expert_id="expert-one",
            project_id="proj-overlap",
            task_id="task-one",
            file_scope=["src/shared.py"],
        )
        second = expert_lock.atomic_claim_lock(
            expert_id="expert-two",
            project_id="proj-overlap",
            task_id="task-two",
            file_scope=["src/shared.py"],
        )

        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertIn("冲突", second["error"])

    def test_complete_project_state_survives_rapid_writes_and_restart(self):
        project_id = "proj-recovery"
        ctx = ProjectContext(
            project_id=project_id,
            name="Recovery project",
            description="End-to-end persistence test",
            owner_user_id="user-1",
            hermes_client=app_state.hermes_client,
            global_sm_agent=app_state.global_sm_agent,
        )
        ctx.subprojects = [
            {
                "id": "sp-api",
                "name": "API",
                "phase_id": "phase-1",
                "agent_id": "agent-api",
                "status": "working",
                "progress": 40,
            }
        ]
        ctx.agents = {
            "agent-api": {
                "id": "agent-api",
                "role": "Backend engineer",
                "project_id": project_id,
                "phase_id": "phase-1",
                "subproject_id": "sp-api",
                "status": "working",
                "progress": 40,
                "required_rebuild_files": ["src/api.py"],
            }
        }
        ctx.qc_results = {"sp-api": {"passed": False, "issues_detail": [{"id": "warning-1"}]}}
        source_file = ctx.workspace / "src" / "api.py"
        source_file.write_text("VERSION = 1\n", encoding="utf-8")
        app_state.projects[project_id] = ctx

        phase_manager = PhaseManager(project_id, ctx.workspace)
        phase_manager.phases = [
            {
                "phase_id": "phase-1",
                "name": "Core API",
                "status": "active",
                "reviewed": True,
                "review_passed": False,
                "agents": ["agent-api"],
                "subprojects": ["sp-api"],
                "plan_reviews": [{"result": "changes_requested"}],
            }
        ]
        phase_manager.current_phase_index = 0
        phase_manager.phase_agents = {"phase-1": ["agent-api"]}
        phase_manager.register_file("src/api.py", "agent-api", "Backend engineer", "phase-1", "sp-api")
        app_state._phase_managers[project_id] = phase_manager

        async def persist_rapid_user_actions():
            await app_state._persist_all_async()
            # This immediate second write was dropped by the old one-second
            # throttle, leaving restarted agents in their earlier state.
            ctx.agents["agent-api"].update(status="completed", progress=100)
            ctx.subprojects[0].update(status="completed", progress=100)
            ctx.qc_results["sp-api"] = {"passed": True, "issues_detail": []}
            phase_manager.phases[0].update(status="completed", review_passed=True)
            source_file.write_text("VERSION = 2\n", encoding="utf-8")
            await app_state._persist_all_async()

        asyncio.run(persist_rapid_user_actions())

        persisted = database.kv_get("projects")
        self.assertEqual(persisted[project_id]["agents"]["agent-api"]["status"], "completed")
        self.assertTrue(database.kv_get("phase_managers")[project_id]["phases"][0]["review_passed"])

        # Simulate restart: no in-memory state and no local generated source.
        app_state.projects.clear()
        app_state.projects["proj-deleted-remotely"] = object()
        app_state._phase_managers.clear()
        source_file.unlink()
        app_state._restore_from_disk()

        self.assertEqual(set(app_state.projects), {project_id})
        restored = app_state.projects[project_id]
        self.assertEqual(restored.owner_user_id, "user-1")
        self.assertEqual(restored.agents["agent-api"]["status"], "completed")
        self.assertEqual(restored.agents["agent-api"]["required_rebuild_files"], ["src/api.py"])
        self.assertTrue(restored.qc_results["sp-api"]["passed"])
        self.assertEqual(
            (restored.workspace / "src" / "api.py").read_text(encoding="utf-8"),
            "VERSION = 2\n",
        )

        restored_phase_manager = app_state._phase_managers[project_id]
        self.assertEqual(restored_phase_manager.current_phase_index, 0)
        self.assertEqual(restored_phase_manager.phase_agents, {"phase-1": ["agent-api"]})
        self.assertEqual(restored_phase_manager.file_registry["src/api.py"]["subproject_id"], "sp-api")
        self.assertEqual(restored_phase_manager.phases[0]["plan_reviews"], [{"result": "changes_requested"}])

    def test_restore_skips_bad_project_without_losing_valid_projects(self):
        database.kv_many_set(
            {
                "projects": {
                    "proj-valid": {
                        "project_id": "wrong-id",
                        "name": "Valid",
                        "description": "still restored",
                        "agents": {},
                        "subprojects": [],
                    },
                    "proj-invalid": "not-an-object",
                },
                "workspace_files": {"proj-valid": {"src/bad.txt": "invalid-base64"}},
            }
        )

        app_state._restore_from_disk()

        self.assertEqual(set(app_state.projects), {"proj-valid"})
        self.assertEqual(app_state.projects["proj-valid"].project_id, "proj-valid")


if __name__ == "__main__":
    unittest.main()
