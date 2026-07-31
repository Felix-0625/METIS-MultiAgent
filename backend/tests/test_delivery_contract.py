from core.delivery_contract import (
    collect_required_file_paths,
    is_delivery_file_path,
    required_files_for_scopes,
)
from core.role_mapping import infer_english_expert_type


def test_common_english_roles_map_to_correct_experts() -> None:
    assert infer_english_expert_type("Full-stack Developer") == "fullstack_engineer"
    assert infer_english_expert_type("Developer") == "fullstack_engineer"
    assert infer_english_expert_type("Software Engineer") == "fullstack_engineer"
    assert infer_english_expert_type("开发工程师") == "fullstack_engineer"
    assert infer_english_expert_type("Frontend Developer") == "frontend"
    assert infer_english_expert_type("UI/UX Designer") == "frontend"
    assert infer_english_expert_type("Backend Developer") == "backend"
    assert infer_english_expert_type("QA Engineer") == "qa"
    assert infer_english_expert_type("DevOps Engineer") == "devops"


def test_required_delivery_files_are_extracted_without_api_paths() -> None:
    requirements = (
        "Required: root package.json, backend/package.json, frontend/package.json, "
        "README.md, .env.example, .dockerignore and Dockerfile. Implement GET /api/health."
    )

    assert collect_required_file_paths(requirements) == [
        ".dockerignore",
        ".env.example",
        "backend/package.json",
        "Dockerfile",
        "frontend/package.json",
        "package.json",
        "README.md",
    ]


def test_declared_conventional_file_names_are_case_normalized() -> None:
    assert collect_required_file_paths(
        "",
        {"required_files": ["readme.md", "Package.json"]},
    ) == ["package.json", "README.md"]
    assert collect_required_file_paths(
        "",
        {"required_files": ["Frontend/Package.json"]},
    ) == ["Frontend/package.json"]


def test_plan_newlines_do_not_create_fake_n_prefixed_paths() -> None:
    plan = {
        "description": (
            "Required repository contract: backend/package.json;\n"
            "frontend/package.json; README.md"
        )
    }

    assert collect_required_file_paths("", plan) == [
        "backend/package.json",
        "frontend/package.json",
        "README.md",
    ]


def test_structured_required_files_accept_root_test_and_config_paths() -> None:
    plan = {
        "phases": [{
            "tasks": [{
                "required_files": [
                    ".gitignore",
                    "server.js",
                    "tests/api.test.js",
                    "playwright.config.js",
                ],
            }],
        }],
    }

    assert collect_required_file_paths("", plan) == [
        ".gitignore",
        "playwright.config.js",
        "server.js",
        "tests/api.test.js",
    ]


def test_explicit_required_files_accept_safe_uncommon_paths_only() -> None:
    plan = {
        "required_files": [
            "start.sh",
            "nginx.conf",
            "assets/logo.svg",
            ".npmrc",
            "../escape.sh",
            "/absolute/start.sh",
            "C:/absolute/start.sh",
            "output/phase-1_execution.log",
        ],
    }

    assert collect_required_file_paths("", plan) == [
        ".npmrc",
        "assets/logo.svg",
        "nginx.conf",
        "start.sh",
    ]


def test_chinese_prose_before_nested_path_does_not_drop_first_segment() -> None:
    plan = {
        "description": "创建cypress/integration/todo_e2e.js并执行测试",
        "required_files": ["cypress/integration/todo_e2e.js"],
    }

    assert collect_required_file_paths("", plan) == [
        "cypress/integration/todo_e2e.js",
    ]


def test_required_files_are_assigned_to_the_owning_expert_scope() -> None:
    files = [".env.example", "backend/package.json", "frontend/package.json", "Dockerfile"]

    assert required_files_for_scopes(files, ["Dockerfile", ".env.example", "deploy/"]) == [
        ".env.example",
        "Dockerfile",
    ]
    assert required_files_for_scopes(files, ["frontend/"]) == ["frontend/package.json"]


def test_runner_owned_output_logs_are_never_delivery_files() -> None:
    text = (
        "Required: backend/src/auth.js, output/phase-1_execution.log, "
        ".project/versions/1/commit.json"
    )

    assert collect_required_file_paths(text) == ["backend/src/auth.js"]
    assert required_files_for_scopes(
        ["backend/src/auth.js", "output/sp-1_execution.log"], []
    ) == ["backend/src/auth.js"]
    assert not is_delivery_file_path("output/sp-1_execution.log")
