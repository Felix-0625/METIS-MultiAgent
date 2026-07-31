from agents.pm_team import PMLeaderAgent
from core.project_contract import (
    build_required_files_manifest,
    finalize_project_contract,
    hydrate_plan_required_files,
    parse_project_contract,
    required_file_owner_type,
    validate_required_files_manifest,
)
from models.schemas import PlanConfirmRequest
from pydantic import ValidationError
import pytest


def _node_plan():
    return {
        "tech_stack": {
            "frontend": "React + Vite + TypeScript",
            "backend": "Node.js + Express",
            "deploy": "Docker",
        },
        "phases": [{
            "phase_id": "phase-1",
            "name": "Delivery",
            "description": "Deliver the complete application and deployment package.",
            "roles_needed": ["fullstack engineer"],
            "implementation_method": "Implement, integrate, test, and package the application.",
            "tech_stack": ["Node.js", "Express", "React", "Vite", "TypeScript", "Docker"],
            "responsibilities": ["Full-stack engineer owns implementation and verification."],
            "agent_count": 1,
            "personnel_allocation": ["Full-stack engineer: 1"],
            "task_contract": [{
                "task_id": "phase-1-task-1",
                "name": "Deliver application",
                "description": "Implement and package the complete application.",
                "roles": ["fullstack engineer"],
                "implementation_method": "Build both application layers and verify integration.",
                "tech_stack": ["Node.js", "Express", "React", "Vite", "TypeScript", "Docker"],
                "responsibilities": ["Full-stack engineer implements, integrates, and tests."],
                "personnel_count": 1,
                "personnel_allocation": ["Full-stack engineer: 1"],
                "deliverables": [
                    "package.json",
                    "backend/package.json",
                    "frontend/package.json",
                    "Dockerfile",
                    ".env.example",
                    "README.md",
                ],
                "acceptance_criteria": ["application starts"],
            }],
            "acceptance_criteria": ["application starts"],
        }],
    }


def _node_contract(plan):
    return parse_project_contract(
        "Node.js Express React Vite TypeScript Docker. Strictly 1 phase. Roles: fullstack engineer",
        plan,
    )


def test_confirmation_manifest_covers_node_boundaries_and_integration_files():
    plan = _node_plan()
    contract = finalize_project_contract(_node_contract(plan), plan)

    entries = {item.path: item for item in contract.required_files}
    assert contract.locked is True
    assert {
        "package.json",
        "backend/package.json",
        "frontend/package.json",
        "frontend/index.html",
        "frontend/tsconfig.json",
        "Dockerfile",
        ".env.example",
        "README.md",
    } <= set(entries)
    assert entries["package.json"].owner_type == "devops"
    assert entries["backend/package.json"].owner_type == "backend"
    assert entries["frontend/package.json"].owner_type == "frontend"
    assert entries["frontend/index.html"].owner_type == "frontend"
    assert entries["frontend/tsconfig.json"].owner_type == "frontend"
    assert all(item.phase_id == "phase-1" and item.required for item in entries.values())
    assert all(
        entries[path].task_id == "phase-1-task-1"
        and entries[path].criterion
        and entries[path].evidence_spec == "registry_byte_digest"
        for path in ("frontend/index.html", "frontend/tsconfig.json")
    )
    assert validate_required_files_manifest(contract.required_files, contract.phases) == ()


def test_python_only_manifest_does_not_invent_node_or_react_files():
    plan = {
        "tech_stack": {"backend": "Python + FastAPI"},
        "phases": [{"phase_id": "phase-1", "roles_needed": ["backend engineer"]}],
    }
    contract = parse_project_contract("Python FastAPI. Strictly 1 phase.", plan)
    manifest = build_required_files_manifest(
        contract.source_requirements, plan, contract.phases, contract.technology_stack,
    )

    paths = {item.path for item in manifest}
    assert "package.json" not in paths
    assert "backend/package.json" not in paths
    assert "frontend/package.json" not in paths
    assert "frontend/index.html" not in paths
    assert "frontend/tsconfig.json" not in paths


def test_backend_only_typescript_does_not_invent_frontend_scaffold():
    plan = {
        "tech_stack": {"backend": "Node.js + Express + TypeScript"},
        "phases": [{"phase_id": "phase-1", "roles_needed": ["backend engineer"]}],
    }
    contract = parse_project_contract(
        "Use Node.js, Express and TypeScript for a backend service. Strictly 1 phase.",
        plan,
    )
    manifest = build_required_files_manifest(
        contract.source_requirements,
        plan,
        contract.phases,
        contract.technology_stack,
    )

    paths = {item.path for item in manifest}
    assert "frontend/package.json" not in paths
    assert "frontend/index.html" not in paths
    assert "frontend/tsconfig.json" not in paths


def test_manifest_assigns_requirement_section_files_to_distinct_phases():
    requirements = """
Use Node.js, Express, React, Vite and Docker. Strictly 4 phases.
Phase 1 Foundation
Create README.md
Phase 2 Backend delivery
Create backend/package.json and backend/src/server.js
Phase 3 Frontend delivery
Create frontend/package.json and frontend/src/App.tsx
Phase 4 Release delivery
Create package.json, Dockerfile and .env.example
"""
    plan = {
        "phases": [
            {"phase_id": "phase-1", "name": "Foundation", "roles_needed": ["backend"]},
            {"phase_id": "phase-2", "name": "Backend delivery", "roles_needed": ["backend"]},
            {"phase_id": "phase-3", "name": "Frontend delivery", "roles_needed": ["frontend"]},
            {"phase_id": "phase-4", "name": "Release delivery", "roles_needed": ["devops"]},
        ],
    }
    contract = parse_project_contract(requirements, plan)
    manifest = build_required_files_manifest(
        requirements, plan, contract.phases, contract.technology_stack,
    )
    entries = {item.path: item for item in manifest}

    assert entries["backend/package.json"].phase_id == "phase-2"
    assert entries["backend/src/server.js"].phase_id == "phase-2"
    assert entries["frontend/package.json"].phase_id == "phase-3"
    assert entries["frontend/src/App.tsx"].phase_id == "phase-3"
    assert entries["package.json"].phase_id == "phase-4"
    assert entries["Dockerfile"].phase_id == "phase-4"
    assert entries[".env.example"].phase_id == "phase-4"
    assert len(entries) == len({item.path.lower() for item in manifest})


def test_plan_file_mentions_override_requirement_section_mentions():
    requirements = """
Node.js Express. Strictly 2 phases.
Phase 1 Foundation
Create backend/src/server.js
Phase 2 Backend delivery
Complete backend/package.json
"""
    plan = {
        "phases": [
            {"phase_id": "phase-1", "name": "Foundation", "roles_needed": ["backend"]},
            {
                "phase_id": "phase-2",
                "name": "Backend delivery",
                "roles_needed": ["backend"],
                "deliverables": ["backend/src/server.js", "backend/package.json"],
            },
        ],
    }
    contract = parse_project_contract(requirements, plan)
    manifest = build_required_files_manifest(
        requirements, plan, contract.phases, contract.technology_stack,
    )
    entries = {item.path: item for item in manifest}

    assert entries["backend/src/server.js"].phase_id == "phase-2"


def test_structured_task_files_keep_earliest_shared_root_ownership():
    plan = {
        "phases": [
            {
                "phase_id": "phase-1",
                "name": "Backend foundation",
                "roles_needed": ["Backend Developer"],
                "tasks": [{
                    "task_id": "phase-1-task-1",
                    "name": "Create shared runtime",
                    "roles": ["Backend Developer"],
                    "required_files": [
                        ".gitignore",
                        "package.json",
                        "server.js",
                    ],
                }],
            },
            {
                "phase_id": "phase-2",
                "name": "QA",
                "description": "Run tests through the package.json scripts.",
                "roles_needed": ["QA Engineer"],
                "tasks": [{
                    "task_id": "phase-2-task-1",
                    "name": "Verify runtime",
                    "description": "Consume the existing package.json scripts.",
                    "roles": ["QA Engineer"],
                    "required_files": ["tests/api.test.js"],
                }],
            },
        ],
    }

    locked = finalize_project_contract(
        parse_project_contract("Build and test a small Node.js application."),
        plan,
    )
    entries = {item.path: item for item in locked.required_files}

    assert entries["package.json"].phase_id == "phase-1"
    assert entries["package.json"].task_id == "phase-1-task-1"
    assert entries[".gitignore"].phase_id == "phase-1"
    assert entries[".gitignore"].task_id == "phase-1-task-1"
    assert validate_required_files_manifest(
        locked.required_files, locked.phases,
    ) == ()


def test_structured_task_files_do_not_expand_relative_paths_from_prose():
    plan = {
        "phases": [{
            "phase_id": "phase-1",
            "name": "Frontend delivery",
            "roles_needed": ["Full-stack Developer"],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Build frontend",
                "description": (
                    "Create the frontend directory with index.html, "
                    "src/main.jsx, src/App.jsx and src/components/TodoList.jsx."
                ),
                "roles": ["Full-stack Developer"],
                "required_files": [
                    "frontend/index.html",
                    "frontend/src/main.jsx",
                    "frontend/src/App.jsx",
                    "frontend/src/components/TodoList.jsx",
                ],
            }],
        }],
    }

    locked = finalize_project_contract(
        parse_project_contract("Build a small React application."),
        plan,
    )
    paths = {item.path for item in locked.required_files}

    assert "frontend/src/App.jsx" in paths
    assert "src/App.jsx" not in paths
    assert "src/main.jsx" not in paths
    assert "src/components/TodoList.jsx" not in paths


def test_structured_task_files_are_unioned_with_stack_required_paths():
    plan = {
        "tech_stack": ["Node.js", "Express"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "Backend",
            "roles_needed": ["Backend Developer"],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Build API",
                "roles": ["Backend Developer"],
                "required_files": ["backend/server.js"],
            }],
        }],
    }

    locked = finalize_project_contract(
        parse_project_contract(
            "Use Node.js and Express. Exactly 1 phase.",
            plan,
        ),
        plan,
    )
    entries = {item.path: item for item in locked.required_files}

    assert {
        "README.md",
        "package.json",
        "backend/package.json",
        "backend/server.js",
    } <= set(entries)
    assert all(item.task_id == "phase-1-task-1" for item in entries.values())
    assert validate_required_files_manifest(
        locked.required_files, locked.phases,
    ) == ()


def test_root_express_project_does_not_invent_backend_package_manifest():
    plan = {
        "tech_stack": ["Node.js", "Express"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "Backend",
            "roles_needed": ["Backend Developer"],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Build API",
                "roles": ["Backend Developer"],
                "required_files": ["package.json", "src/server.js"],
            }],
        }],
    }

    locked = finalize_project_contract(
        parse_project_contract(
            "Use Node.js and Express. Exactly 1 phase.",
            plan,
        ),
        plan,
    )

    paths = {item.path for item in locked.required_files}
    assert "package.json" in paths
    assert "src/server.js" in paths
    assert "backend/package.json" not in paths


def test_auto_stack_readme_has_deterministic_task_binding():
    plan = {
        "tech_stack": ["Node.js"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "Backend",
            "roles_needed": ["Backend Developer"],
            "tasks": [
                {
                    "task_id": "phase-1-task-1",
                    "name": "Runtime",
                    "roles": ["Backend Developer"],
                    "required_files": ["backend/server.js"],
                },
                {
                    "task_id": "phase-1-task-2",
                    "name": "Health endpoint",
                    "roles": ["Backend Developer"],
                    "required_files": ["backend/health.js"],
                },
            ],
        }],
    }

    locked = finalize_project_contract(
        parse_project_contract("Use Node.js. Exactly 1 phase.", plan),
        plan,
    )
    readme = next(
        item for item in locked.required_files if item.path == "README.md"
    )

    assert readme.task_id == "phase-1-task-1"
    assert readme.criterion
    assert readme.evidence_spec == "registry_byte_digest"


def test_pytest_ini_is_owned_by_qa():
    assert required_file_owner_type("pytest.ini") == "qa"


def test_hydrate_plan_required_files_uses_canonical_manifest_bindings():
    plan = {
        "tech_stack": ["Node.js"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "Backend",
            "roles_needed": ["Backend Developer"],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Build API",
                "roles": ["Backend Developer"],
                "required_files": ["backend/server.js"],
            }],
        }],
    }
    locked = finalize_project_contract(
        parse_project_contract("Use Node.js. Exactly 1 phase.", plan),
        plan,
    )

    hydrate_plan_required_files(plan, locked)

    expected = sorted(
        (
            item.path
            for item in locked.required_files
            if item.task_id == "phase-1-task-1"
        ),
        key=str.lower,
    )
    assert plan["phases"][0]["tasks"][0]["required_files"] == expected


def test_english_industrial_contract_locks_stack_and_four_phases():
    contract = parse_project_contract(
        "Use Node.js, Express, SQLite, React, TypeScript and Vite. "
        "The delivery must contain exactly four implementation phases."
    )
    assert contract.phase_count == 4
    assert len(contract.phases) == 4
    assert {"node.js", "express", "sqlite", "react", "typescript", "vite"} <= set(contract.technology_stack)


def test_confirm_locks_contract_and_rejects_in_place_reconfirmation():
    plan = _node_plan()
    plan["project_contract"] = _node_contract(plan).as_mapping()
    leader = PMLeaderAgent()
    leader.draft_plan = plan

    first = leader.confirm_plan()
    assert first["success"] is True
    assert first["plan"]["project_contract"]["locked"] is True
    assert first["plan"]["project_contract"]["contract_version"] == 2
    assert first["plan"]["project_contract"]["required_files"]

    second = leader.confirm_plan()
    assert second["success"] is False
    assert second["status"] == "contract_locked"
    assert second["validation"]["issues"][0]["code"] == "contract_version_locked"
    assert leader.final_plan is first["plan"]


def test_required_file_without_task_id_is_rejected_when_same_role_tasks_are_ambiguous():
    plan = {
        "phases": [{
            "phase_id": "phase-1", "name": "Backend",
            "roles_needed": ["backend"],
            "tasks": [
                {"task_id": "one", "name": "First", "roles": ["backend"]},
                {"task_id": "two", "name": "Second", "roles": ["backend"]},
            ],
        }],
    }
    contract = finalize_project_contract(
        parse_project_contract("Build the backend."), plan,
        [{"path": "backend/app.py", "owner_type": "backend", "phase_id": "phase-1"}],
    )
    result = validate_required_files_manifest(contract.required_files, contract.phases)
    assert "file_task_unbound" in {issue.code for issue in result}


def test_required_file_is_bound_only_by_unique_task_deliverable_path():
    plan = {
        "phases": [{
            "phase_id": "phase-1", "name": "Backend",
            "roles_needed": ["backend"],
            "tasks": [
                {
                    "task_id": "one", "name": "First", "roles": ["backend"],
                    "deliverable": "backend/one.py",
                    "description": "Coordinate changes involving backend/two.py",
                },
                {
                    "task_id": "two", "name": "Second", "roles": ["backend"],
                    "deliverable": "backend/two.py",
                },
            ],
        }],
    }
    contract = finalize_project_contract(
        parse_project_contract("Build backend/one.py and backend/two.py.", plan),
        plan,
    )
    entries = {item.path: item for item in contract.required_files}
    assert entries["backend/one.py"].task_id == "one"
    assert entries["backend/two.py"].task_id == "two"
    target_files = (
        entries["backend/one.py"],
        entries["backend/two.py"],
    )
    assert validate_required_files_manifest(target_files, contract.phases) == ()


def test_required_files_bind_to_the_unique_owner_role_task():
    plan = {
        "tech_stack": {
            "frontend": "React + Vite",
            "backend": "Node.js + Express",
            "deploy": "Docker",
        },
        "phases": [{
            "phase_id": "phase-1",
            "name": "Delivery",
            "roles_needed": ["devops", "backend", "frontend"],
            "deliverables": [
                "Dockerfile",
                "backend/package.json",
                "frontend/package.json",
            ],
            "tasks": [
                {"task_id": "devops-task", "name": "Package", "roles": ["devops"]},
                {"task_id": "backend-task", "name": "API", "roles": ["backend"]},
                {"task_id": "frontend-task", "name": "UI", "roles": ["frontend"]},
            ],
            "acceptance_criteria": ["application starts"],
        }],
    }
    contract = finalize_project_contract(
        parse_project_contract(
            "Use Node.js, Express, React, Vite and Docker. Strictly 1 phase.",
            plan,
        ),
        plan,
    )
    entries = {item.path: item for item in contract.required_files}

    assert entries["Dockerfile"].task_id == "devops-task"
    assert entries["backend/package.json"].task_id == "backend-task"
    assert entries["frontend/package.json"].task_id == "frontend-task"
    assert all(
        item.criterion and item.evidence_spec == "registry_byte_digest"
        for item in entries.values()
    )
    assert validate_required_files_manifest(contract.required_files, contract.phases) == ()


def test_confirmed_v2_without_trusted_requirements_is_blocked_on_reload():
    plan = {"phases": [{"phase_id": "phase-1"}], "project_contract": {
        "contract_version": 2,
        "locked": False,
        "phases": [{"phase_id": "phase-1"}],
    }}
    leader = PMLeaderAgent()

    leader.from_persist({
        "draft_plan": plan,
        "final_plan": plan,
        "plan_confirmed": True,
        "project_contract": plan["project_contract"],
    })

    assert leader.plan_confirmed is False
    assert leader.final_plan is None
    assert leader.blocked_draft == plan
    assert leader.draft_blocked_reason == "legacy_confirmed_requires_regeneration"


def test_manifest_rejects_duplicate_cross_phase_ownership_and_wrong_path_owner():
    plan = {
        "tech_stack": {"backend": "Node.js + Express", "deploy": "Docker"},
        "phases": [
            {"phase_id": "phase-1", "roles_needed": ["backend engineer"]},
            {"phase_id": "phase-2", "roles_needed": ["devops engineer"]},
        ],
    }
    contract = parse_project_contract("Node.js Express Docker. Strictly 2 phases.", plan)
    manifest = build_required_files_manifest(
        contract.source_requirements,
        plan,
        contract.phases,
        contract.technology_stack,
        [
            {"path": "Dockerfile", "owner_type": "backend", "phase_id": "phase-1", "required": True},
            {"path": "Dockerfile", "owner_type": "devops", "phase_id": "phase-2", "required": True},
        ],
    )
    codes = {item.code for item in validate_required_files_manifest(manifest, contract.phases)}
    assert "duplicate_file_owner" in codes
    assert "file_path_permission" in codes


def test_confirm_request_rejects_unsafe_manifest_paths():
    with pytest.raises(ValidationError):
        PlanConfirmRequest(required_files=[{
            "path": "../secrets.env",
            "owner_type": "devops",
            "phase_id": "phase-1",
        }])


def test_manifest_repairs_untrusted_task_binding_and_evidence_for_single_locked_task():
    requirements = (
        "创建 README.md。严格规划1阶段，每阶段1任务；"
        "每阶段必须包含实现细节、实现方式、技术栈、职责、人员分配和验收标准。"
    )
    contract = parse_project_contract(requirements)
    plan = {
        "phases": [{
            "phase_id": "phase-1",
            "tasks": [{
                "task_id": "model-invented-task",
                "name": "模型越界任务",
                "deliverables": ["README.md"],
            }],
        }],
        "required_files": [{
            "path": "README.md",
            "owner_type": "devops",
            "phase_id": "phase-1",
            "task_id": "",
            "evidence_spec": "model_claim",
        }],
    }

    locked = finalize_project_contract(contract, plan)
    readme = next(item for item in locked.required_files if item.path == "README.md")

    assert readme.task_id == "phase-1-task-1"
    assert readme.criterion
    assert readme.evidence_spec == "registry_byte_digest"
    assert validate_required_files_manifest(locked.required_files, locked.phases) == ()


def test_root_package_and_readme_follow_the_declared_task_role():
    requirements = (
        "创建最小待办 Web 应用。严格规划3阶段，每阶段1任务；"
        "提供运行说明和自动化测试。"
    )
    contract = parse_project_contract(requirements)
    plan = {
        "phases": [
            {
                "phase_id": "phase-1",
                "name": "阶段一",
                "roles_needed": ["前端开发工程师"],
                "tasks": [{
                    "task_id": "phase-1-task-1",
                    "name": "实现待办",
                    "roles": ["前端开发工程师"],
                }],
            },
            {
                "phase_id": "phase-2",
                "name": "阶段二",
                "roles_needed": ["前端开发工程师"],
                "tasks": [{
                    "task_id": "phase-2-task-1",
                    "name": "完善界面",
                    "roles": ["前端开发工程师"],
                }],
            },
            {
                "phase_id": "phase-3",
                "name": "阶段三",
                "roles_needed": ["前端开发工程师", "测试工程师"],
                "tasks": [{
                    "task_id": "phase-3-task-1",
                    "name": "运行说明和自动化测试",
                    "roles": ["前端开发工程师", "测试工程师"],
                    "deliverables": ["package.json", "README.md"],
                }],
            },
        ],
    }

    locked = finalize_project_contract(contract, plan)
    entries = {
        item.path: item
        for item in locked.required_files
        if item.path in {"package.json", "README.md"}
    }

    assert set(entries) == {"package.json", "README.md"}
    assert all(item.owner_type == "frontend" for item in entries.values())
    assert all(item.phase_id == "phase-3" for item in entries.values())
    assert all(
        item.task_id == "phase-3-task-1" for item in entries.values()
    )
    assert validate_required_files_manifest(
        locked.required_files, locked.phases,
    ) == ()


def test_structured_root_frontend_and_qa_files_follow_declaring_task_roles():
    plan = {
        "phases": [
            {
                "phase_id": "phase-1",
                "name": "Frontend",
                "roles_needed": ["Front-End Developer"],
                "tasks": [{
                    "task_id": "phase-1-task-1",
                    "name": "Build UI",
                    "roles": ["Front-End Developer"],
                    "required_files": [
                        "index.html",
                        "style.css",
                        "app.js",
                        "public/index.html",
                        "public/styles.css",
                        "public/app.js",
                    ],
                }],
            },
            {
                "phase_id": "phase-2",
                "name": "QA",
                "roles_needed": ["QA Engineer"],
                "tasks": [{
                    "task_id": "phase-2-task-1",
                    "name": "Automate tests",
                    "roles": ["QA Engineer"],
                    "required_files": [
                        "jest.config.js",
                        "backend/jest.config.js",
                        "cypress.json",
                        "cypress/integration/todo_e2e.js",
                        "test/api.test.js",
                    ],
                }],
            },
        ],
    }

    locked = finalize_project_contract(
        parse_project_contract("Build and test a small web application."),
        plan,
    )
    entries = {item.path: item for item in locked.required_files}

    assert {
        entries[path].owner_type
        for path in (
            "index.html",
            "style.css",
            "app.js",
            "public/index.html",
            "public/styles.css",
            "public/app.js",
        )
    } == {"frontend"}
    assert {
        entries[path].owner_type
        for path in (
            "jest.config.js",
            "backend/jest.config.js",
            "cypress.json",
            "cypress/integration/todo_e2e.js",
            "test/api.test.js",
        )
    } == {"qa"}
    assert validate_required_files_manifest(
        locked.required_files, locked.phases,
    ) == ()


def test_implementation_task_can_own_its_explicit_automated_tests():
    plan = {
        "phases": [{
            "phase_id": "phase-1",
            "name": "Backend and API tests",
            "roles_needed": ["Backend Developer"],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Build and test API",
                "roles": ["Backend Developer"],
                "required_files": [
                    "server.js",
                    "test/api.test.js",
                    "test/jest.config.js",
                ],
            }],
        }],
    }

    locked = finalize_project_contract(
        parse_project_contract("Build and test a small API."),
        plan,
    )
    entries = {item.path: item for item in locked.required_files}

    assert entries["test/api.test.js"].owner_type == "backend"
    assert entries["test/jest.config.js"].owner_type == "backend"
    assert validate_required_files_manifest(
        locked.required_files, locked.phases,
    ) == ()


def test_ambiguous_root_paths_follow_the_explicit_task_role():
    plan = {
        "phases": [
            {
                "phase_id": "phase-1",
                "name": "Backend",
                "roles_needed": ["Backend Developer"],
                "tasks": [{
                    "task_id": "phase-1-task-1",
                    "name": "Build backend",
                    "roles": ["Backend Developer"],
                    "required_files": [".env.example", "server.js"],
                }],
            },
            {
                "phase_id": "phase-2",
                "name": "Frontend",
                "roles_needed": ["Frontend Developer"],
                "tasks": [{
                    "task_id": "phase-2-task-1",
                    "name": "Build frontend",
                    "roles": ["Frontend Developer"],
                    "required_files": [
                        "src/App.js",
                        "src/App.css",
                        "src/components/TodoList.js",
                    ],
                }],
            },
        ],
    }

    locked = finalize_project_contract(
        parse_project_contract("Build a small full-stack application."),
        plan,
    )
    entries = {item.path: item for item in locked.required_files}

    assert entries[".env.example"].owner_type == "backend"
    assert {
        entries[path].owner_type
        for path in (
            "src/App.js",
            "src/App.css",
            "src/components/TodoList.js",
        )
    } == {"frontend"}
    assert validate_required_files_manifest(
        locked.required_files, locked.phases,
    ) == ()


def test_root_readme_uses_fullstack_owner_when_fullstack_owns_the_task():
    requirements = (
        "Create a todo app with README.md. Strictly 1 phase and 1 task."
    )
    contract = parse_project_contract(requirements)
    plan = {
        "phases": [{
            "phase_id": "phase-1",
            "name": "Delivery",
            "roles_needed": ["fullstack engineer"],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Build and document the app",
                "roles": ["fullstack engineer"],
                "deliverables": ["README.md"],
            }],
        }],
    }

    locked = finalize_project_contract(contract, plan)
    readme = next(
        item for item in locked.required_files if item.path == "README.md"
    )

    assert readme.owner_type == "fullstack_engineer"
    assert readme.task_id == "phase-1-task-1"
    assert validate_required_files_manifest(
        locked.required_files, locked.phases,
    ) == ()
