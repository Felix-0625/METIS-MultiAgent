"""Run a real authenticated MeTis workflow against the Render deployment."""

import os
import sys
import time
from datetime import datetime

import requests


BASE = os.getenv("METIS_E2E_BASE", "https://metis-cho3.onrender.com/api").rstrip("/")
LOGIN = os.environ["METIS_E2E_LOGIN"]
PASSWORD = os.environ["METIS_E2E_PASSWORD"]
TIMEOUT = 120


def require(response: requests.Response, step: str):
    if not response.ok:
        raise RuntimeError(f"{step}: HTTP {response.status_code} {response.text[:500]}")
    return response.json()


session = requests.Session()
session.trust_env = False

auth = require(
    session.post(f"{BASE}/auth/login", json={"login": LOGIN, "password": PASSWORD}, timeout=TIMEOUT),
    "login",
)
print(f"PASS login user={auth.get('username')} role={auth.get('role')}")

skills = require(session.get(f"{BASE}/skills/status", timeout=TIMEOUT), "skills")
print(f"PASS skills active={skills.get('active_skills')}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
description = (
    "构建一个可直接打开运行的单文件 HTML 番茄钟与任务管理应用。"
    "必须交付 index.html，支持任务新增、完成、删除，25分钟计时的开始、暂停、重置，"
    "使用 localStorage 持久化，适配手机和桌面，不依赖构建工具或外部服务。"
)
project = require(
    session.post(
        f"{BASE}/projects",
        json={"name": f"Render-E2E-{stamp}", "description": description},
        timeout=TIMEOUT,
    ),
    "create project",
)
project_id = project["project_id"]
print(f"PASS project {project_id}")

plan = require(
    session.post(
        f"{BASE}/projects/{project_id}/pm-team/synthesize",
        json={"requirements": description, "fast_mode": True},
        timeout=300,
    ),
    "synthesize plan",
)
print(f"PASS synthesize keys={sorted(plan)[:8]}")

confirmed = require(
    session.post(
        f"{BASE}/projects/{project_id}/pm-team/confirm-plan",
        json={"modifications": ""},
        timeout=300,
    ),
    "confirm plan",
)
if confirmed.get("status") != "confirmed" and not confirmed.get("plan_confirmed"):
    raise RuntimeError(f"confirm plan: unexpected response {confirmed}")
print("PASS plan confirmed")

phase_data = require(session.get(f"{BASE}/projects/{project_id}/phases", timeout=TIMEOUT), "phases")
phases = phase_data.get("phases", [])
if not phases:
    raise RuntimeError("phases: empty")
phase_id = phases[0]["phase_id"]
print(f"PASS phases count={len(phases)} first={phase_id}")

started = require(
    session.post(f"{BASE}/projects/{project_id}/phases/{phase_id}/start", json={}, timeout=TIMEOUT),
    "start phase",
)
agents = started.get("created_agents", [])
if not agents:
    raise RuntimeError(f"start phase: no agents {started}")
agent_ids = [agent["id"] for agent in agents]
print(f"PASS phase started agents={agent_ids}")

execution = require(
    session.post(f"{BASE}/projects/{project_id}/execute-all", json={}, timeout=TIMEOUT),
    "execute all",
)
print(f"PASS execution started count={len(execution.get('started', []))}")

deadline = time.time() + 900
final_statuses = {}
while time.time() < deadline:
    final_statuses = {}
    for agent_id in agent_ids:
        status = require(
            session.get(f"{BASE}/projects/{project_id}/agents/{agent_id}/status", timeout=TIMEOUT),
            f"agent status {agent_id}",
        )
        final_statuses[agent_id] = status
    states = {agent_id: data.get("status") for agent_id, data in final_statuses.items()}
    print(f"WAIT agents={states}")
    if all(state in {"completed", "failed", "fix_limit_reached"} for state in states.values()):
        break
    time.sleep(10)
else:
    raise RuntimeError(f"agents timed out: {states}")

failures = {
    agent_id: data for agent_id, data in final_statuses.items()
    if data.get("status") != "completed"
}
if failures:
    details = {key: value.get("error") for key, value in failures.items()}
    raise RuntimeError(f"agent failures: {details}")

deliverables = []
for data in final_statuses.values():
    deliverables.extend(
        path for path in data.get("output_files", [])
        if not path.startswith("output/") and not path.endswith(".log")
    )
if not deliverables:
    raise RuntimeError("no deliverable files produced")
print(f"PASS deliverables={sorted(set(deliverables))}")

project_after = require(session.get(f"{BASE}/projects/{project_id}", timeout=TIMEOUT), "project result")
remaining = [
    item.get("id") for item in project_after.get("subprojects", [])
    if not item.get("agent_id") and item.get("status") != "completed"
]
if remaining and project_after.get("status") == "completed":
    raise RuntimeError(f"project prematurely completed with pending phases: {remaining}")
print(f"PASS project status={project_after.get('status')} pending_phases={remaining}")
print(f"E2E_SUCCESS project_id={project_id} phase_id={phase_id}")

