from pathlib import Path

from core.runtime_templates import (
    materialize_missing_template_files,
    render_template_files,
    select_runtime_template,
)


def test_python_only_project_does_not_select_node_template():
    assert select_runtime_template(["Python", "FastAPI"]) is None


def test_node_template_contains_hardened_runtime_files():
    assert select_runtime_template(["React", "Express", "Node.js"]) == "node_react_express"
    files, evidence = render_template_files(
        "node_react_express", project_name="acceptance", port=3000,
    )
    assert evidence == {"template": "node-react-express", "template_version": 4}
    assert {
        "package.json",
        ".env.example",
        ".dockerignore",
        "Dockerfile",
        "frontend/tsconfig.json",
        "frontend/index.html",
    } <= set(files)
    assert '"moduleResolution": "Bundler"' in files["frontend/tsconfig.json"]
    assert '"include": ["src"]' in files["frontend/tsconfig.json"]
    assert '<div id="root"></div>' in files["frontend/index.html"]
    assert '<script type="module" src="/src/main.tsx"></script>' in files[
        "frontend/index.html"
    ]
    assert "<title>Generated Application</title>" in files["frontend/index.html"]
    assert "process.env.JWT_SECRET" in files["backend/src/runtime/auth.js"]
    assert "JWT_SECRET is required" in files["backend/src/runtime/auth.js"]
    assert '|| "secret"' not in files["backend/src/runtime/auth.js"]
    assert "USER node" in files["Dockerfile"]
    assert "[ -f frontend/package-lock.json ]" in files["Dockerfile"]
    assert "npm ci --prefix frontend" in files["Dockerfile"]
    assert "npm install --prefix frontend --no-audit --no-fund" in files["Dockerfile"]
    assert "[ -f backend/package-lock.json ]" in files["Dockerfile"]
    assert "npm ci --omit=dev --prefix backend" in files["Dockerfile"]
    assert "npm install --omit=dev --prefix backend --no-audit --no-fund" in files["Dockerfile"]
    assert ".env" in files[".dockerignore"]


def test_template_materialization_never_overwrites_existing_file(tmp_path: Path):
    (tmp_path / "package.json").write_text("user-owned", encoding="utf-8")
    created = materialize_missing_template_files(
        tmp_path,
        {"package.json": "template", ".env.example": "JWT_SECRET=\n"},
    )
    assert created == [".env.example"]
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == "user-owned"
