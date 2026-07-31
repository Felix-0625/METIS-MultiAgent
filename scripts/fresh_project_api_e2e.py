from __future__ import annotations

import argparse
import copy
import getpass
import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


def bootstrap_environment() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--state")
    parser.add_argument("--base")
    parser.add_argument("--login")
    parser.add_argument("--project-prefix")
    parser.add_argument("--min-phases")
    parser.add_argument("--max-phases")
    parser.add_argument("--requirements")
    parser.add_argument("--prompt-password", action="store_true")
    args, _unknown = parser.parse_known_args()
    config: dict[str, Any] = {}
    if args.config:
        config = json.loads(Path(args.config).resolve().read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("E2E config must be a JSON object")
        if "password" in config:
            raise ValueError("E2E config must not contain a password")

    def option(name: str, cli_value: Any) -> Any:
        return cli_value if cli_value is not None else config.get(name)

    state = option("state", args.state)
    values = {
        "METIS_E2E_STATE": state,
        "METIS_E2E_STATE_PATH": state,
        "METIS_E2E_BASE": option("base", args.base),
        "METIS_E2E_LOGIN": option("login", args.login),
        "METIS_E2E_PROJECT_PREFIX": option(
            "project_prefix", args.project_prefix
        ),
        "METIS_E2E_MIN_PHASES": option("min_phases", args.min_phases),
        "METIS_E2E_MAX_PHASES": option("max_phases", args.max_phases),
        "METIS_E2E_REQUIREMENTS": option("requirements", args.requirements),
    }
    for key, value in values.items():
        if value is not None:
            os.environ[key] = str(value)
    if args.prompt_password and not os.environ.get("METIS_E2E_PASSWORD"):
        os.environ["METIS_E2E_PASSWORD"] = getpass.getpass("E2E password: ")


bootstrap_environment()

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path(os.environ["METIS_E2E_STATE"]).resolve()
REQUIREMENTS = os.environ["METIS_E2E_REQUIREMENTS"].strip()
PROJECT_PREFIX = os.environ["METIS_E2E_PROJECT_PREFIX"].strip()
MIN_PHASES = int(os.environ["METIS_E2E_MIN_PHASES"])
MAX_PHASES = int(os.environ.get("METIS_E2E_MAX_PHASES", "0")) or None

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def load_base_runner() -> Any:
    candidates = sorted(
        (ROOT / ".tmp" / "__pycache__").glob("local_todo_api_e2e*.pyc"),
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("compiled API E2E runner is unavailable")
    loader = importlib.machinery.SourcelessFileLoader(
        "metis_base_api_e2e", str(candidates[0])
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("cannot load compiled API E2E runner")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return value is not None


def phase_count_valid(count: int) -> bool:
    return count >= MIN_PHASES and (MAX_PHASES is None or count <= MAX_PHASES)


def main() -> None:
    if STATE_PATH.exists():
        raise RuntimeError(f"refusing to resume existing state: {STATE_PATH}")
    runner = load_base_runner()
    runner.STATE_PATH = STATE_PATH
    runner.ROOT = ROOT
    runner.BACKEND_ERROR_LOG = ROOT / ".codex-run-backend-e2e-current.err.log"
    runner.REQUIREMENTS = REQUIREMENTS
    runner.AGENT_TIMEOUT = 1800
    runner.QA_TIMEOUT = 2400
    runner.QA_FAILURE = set(runner.QA_FAILURE) - {"pre_qa_failed"}

    base_validate_plan = runner.validate_structured_plan
    base_validate_phase_plan = runner.validate_phase_plan
    base_qa_is_running = runner.qa_is_running
    base_post = runner.post
    base_get = runner.get
    base_save_state = runner.save_state
    identity: dict[str, str] = {}

    def qa_is_running(status: dict[str, Any]) -> bool:
        if str(status.get("status") or "") == "model_failed":
            return False
        if (
            str(status.get("status") or "") == "pre_qa_failed"
            and str(status.get("supervisor_state") or "") == "waiting_engineer"
            and status.get("waiting_for")
        ):
            return True
        return base_qa_is_running(status)
    phase_pm_outputs: dict[str, dict[str, Any]] = {}

    def save_state(**values: Any) -> None:
        base_save_state(
            **values,
            phase_pm_outputs=copy.deepcopy(phase_pm_outputs),
        )

    def phase_id_for(path: str, suffix: str) -> str:
        project_id = identity.get("project_id", "")
        prefix = f"/projects/{project_id}/phases/"
        if not project_id or not path.startswith(prefix) or not path.endswith(suffix):
            return ""
        return path[len(prefix) : -len(suffix)].strip("/")

    def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
        evidence = base_validate_plan(plan)
        generation_mode = str(
            plan.get("source")
            or (plan.get("artifact_metadata") or {}).get("source")
            or ""
        )
        if generation_mode not in {"model", "model_repaired"}:
            raise RuntimeError(
                f"total PM did not produce a real model plan: {generation_mode or 'missing'}"
            )
        phases = [item for item in plan.get("phases") or [] if isinstance(item, dict)]
        if not phase_count_valid(len(phases)):
            raise RuntimeError(f"invalid phase count: {len(phases)}")
        missing: dict[str, list[str]] = {}
        for phase in phases:
            phase_id = str(phase.get("phase_id") or "unknown")
            tasks = phase.get("tasks") or phase.get("task_contract") or []
            fields = {
                "tasks": tasks,
                "implementation": (
                    phase.get("implementation_method")
                    or phase.get("implementation")
                    or phase.get("description")
                ),
                "tech_stack": phase.get("tech_stack"),
                "roles": phase.get("roles_needed") or phase.get("roles"),
                "personnel_count": (
                    phase.get("agent_count") or phase.get("personnel_count")
                ),
                "personnel_allocation": phase.get("personnel_allocation"),
                "acceptance_criteria": (
                    phase.get("acceptance_criteria") or phase.get("acceptance")
                ),
            }
            absent = [key for key, value in fields.items() if not present(value)]
            if any(
                not isinstance(task, dict)
                or not present(task.get("required_files"))
                for task in tasks
            ):
                absent.append("task.required_files")
            if absent:
                missing[phase_id] = sorted(set(absent))
        if missing:
            raise RuntimeError(f"incomplete total PM plan: {missing}")
        evidence["generation_mode"] = generation_mode
        evidence["required_fields_complete"] = True
        return evidence

    def validate_phase_plan(
        items: list[dict[str, Any]], phase_id: str
    ) -> dict[str, Any]:
        evidence = base_validate_phase_plan(items, phase_id)
        missing: dict[str, list[str]] = {}
        for item in items:
            task_id = str(item.get("task_id") or "unknown")
            fields = {
                "implementation": (
                    item.get("implementation_method") or item.get("implementation")
                ),
                "tech_stack": item.get("tech_stack"),
                "responsibilities": item.get("responsibilities"),
                "personnel_count": (
                    item.get("personnel_count") or item.get("agent_count")
                ),
                "personnel_allocation": item.get("personnel_allocation"),
                "acceptance_criteria": item.get("acceptance_criteria"),
                "required_files": item.get("required_files"),
            }
            absent = [key for key, value in fields.items() if not present(value)]
            if absent:
                missing[task_id] = absent
        if missing:
            raise RuntimeError(f"incomplete phase PM plan {phase_id}: {missing}")
        evidence["required_fields_complete"] = True
        return evidence

    def post(
        session: Any,
        path: str,
        step: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int = runner.HTTP_TIMEOUT,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if path == "/projects" and payload is not None:
            payload = {
                **payload,
                "name": str(payload.get("name") or "").replace(
                    "Local-Todo-E2E", PROJECT_PREFIX
                ),
            }
        result = base_post(
            session,
            path,
            step,
            payload=payload,
            params=params,
            timeout=timeout,
            headers=headers,
        )
        phase_id = phase_id_for(path, "/plan-experts")
        if phase_id:
            generation_mode = str(result.get("generation_mode") or "")
            if generation_mode not in {"model", "model_repaired"}:
                raise RuntimeError(
                    "phase PM did not produce a real model plan: "
                    f"{generation_mode or 'missing'}"
                )
            attempts = result.get("attempts")
            if (
                not isinstance(attempts, list)
                or not attempts
                or not isinstance(attempts[-1], dict)
                or attempts[-1].get("status") != "generated"
            ):
                raise RuntimeError(
                    f"phase PM generation did not end as generated: {phase_id}"
                )
            validation = result.get("validation")
            if not isinstance(validation, dict) or validation.get("valid") is not True:
                raise RuntimeError(
                    f"phase PM response is not contract-valid: {phase_id}"
                )
            requirements = [
                copy.deepcopy(item)
                for item in (result.get("expert_requirements") or [])
                if isinstance(item, dict)
            ]
            validate_phase_plan(requirements, phase_id)

            persisted = base_get(
                session,
                f"/projects/{identity['project_id']}/phases",
                f"verify persisted phase PM output {phase_id}",
            )
            persisted_phase = next(
                (
                    item
                    for item in (persisted.get("phases") or [])
                    if isinstance(item, dict)
                    and str(item.get("phase_id") or "") == phase_id
                ),
                None,
            )
            if persisted_phase is None:
                raise RuntimeError(
                    f"persisted phase PM output is missing: {phase_id}"
                )
            artifact = persisted_phase.get("plan_artifact_metadata") or {}
            persisted_source = str(
                persisted_phase.get("plan_generation_mode")
                or artifact.get("source")
                or ""
            )
            if (
                persisted_source != generation_mode
                or (
                    artifact.get("source")
                    and str(artifact.get("source")) != generation_mode
                )
                or persisted_phase.get("plan_generation_attempts") != attempts
                or persisted_phase.get("expert_requirements") != requirements
                or persisted_phase.get("plan_contract_validated") is not True
            ):
                raise RuntimeError(
                    f"persisted phase PM output does not match response: {phase_id}"
                )
            phase_pm_outputs[phase_id] = {
                "generation_mode": generation_mode,
                "attempts": copy.deepcopy(attempts),
                "validation": copy.deepcopy(validation),
                "expert_requirements": requirements,
                "persistence_verified": True,
                "persisted_source": persisted_source,
            }
            save_state()
            runner.emit(
                "phase_pm_model_verified",
                phase_id=phase_id,
                generation_mode=generation_mode,
                task_ids=[
                    str(item.get("task_id") or "") for item in requirements
                ],
            )
        start_phase_id = phase_id_for(path, "/start")
        if start_phase_id:
            expected_output = phase_pm_outputs.get(start_phase_id)
            if expected_output is None:
                raise RuntimeError(
                    f"phase started without verified phase PM output: {start_phase_id}"
                )
            created_agents = [
                item
                for item in (result.get("created_agents") or [])
                if isinstance(item, dict)
            ]
            if not created_agents:
                raise RuntimeError(
                    f"phase start created no execution agents: {start_phase_id}"
                )
            expected_tasks = {
                str(item.get("task_id") or ""): item
                for item in expected_output["expert_requirements"]
            }
            actual_tasks: dict[str, dict[str, Any]] = {}
            for agent in created_agents:
                locked_tasks = [
                    item
                    for item in (agent.get("locked_tasks") or [])
                    if isinstance(item, dict)
                ]
                locked_ids = [str(item.get("task_id") or "") for item in locked_tasks]
                if agent.get("assigned_task_ids") != locked_ids:
                    raise RuntimeError(
                        f"agent task IDs do not match locked tasks: {start_phase_id}"
                    )
                for task_id, task in zip(locked_ids, locked_tasks):
                    if not task_id or task_id in actual_tasks:
                        raise RuntimeError(
                            f"invalid duplicate locked task: {start_phase_id}/{task_id}"
                        )
                    actual_tasks[task_id] = task
            if set(actual_tasks) != set(expected_tasks):
                raise RuntimeError(
                    f"phase start task IDs drifted: {start_phase_id}"
                )
            for task_id, expected in expected_tasks.items():
                actual = actual_tasks[task_id]
                if any(actual.get(key) != value for key, value in expected.items()):
                    raise RuntimeError(
                        f"phase start task contract drifted: {start_phase_id}/{task_id}"
                    )
            expected_output["start_contract_verified"] = True
            expected_output["created_agent_ids"] = [
                str(item.get("id") or "") for item in created_agents
            ]
            save_state()
            runner.emit(
                "phase_start_contract_verified",
                phase_id=start_phase_id,
                task_ids=sorted(expected_tasks),
            )
        if path == "/auth/login":
            identity["user_id"] = str(result.get("user_id") or "")
        elif path == "/projects":
            project_id = str(result.get("project_id") or "")
            expected_owner = identity.get("user_id", "")
            if (
                not project_id
                or str(result.get("owner_user_id") or "") != expected_owner
                or project_id not in str(result.get("workspace") or "")
                or not str(result.get("name") or "").startswith(PROJECT_PREFIX)
                or str(result.get("description") or "") != REQUIREMENTS
            ):
                raise RuntimeError("new project failed strict isolation checks")
            identity["project_id"] = project_id
        return result

    def get(session: Any, path: str, step: str) -> Any:
        result = base_get(session, path, step)
        project_id = identity.get("project_id", "")
        if project_id and path == f"/projects/{project_id}/phases":
            phases = [
                item for item in result.get("phases") or [] if isinstance(item, dict)
            ]
            if not phase_count_valid(len(phases)):
                raise RuntimeError("initialized phase count drifted")
            ids = [str(item.get("phase_id") or "") for item in phases]
            if not all(ids) or len(ids) != len(set(ids)):
                raise RuntimeError("initialized phase identities are invalid")
            if len(phases) > 1 and not any(
                phase.get("dependencies") for phase in phases[1:]
            ):
                raise RuntimeError("multi-phase project has no dependency ordering")
            # The confirmed total-PM plan is a valid contract seed, not proof
            # that the independent phase-PM model link was exercised. This
            # response-only override makes the E2E runner invoke /plan-experts
            # for every phase without mutating persisted project state.
            for phase in phases:
                phase["plan_contract_validated"] = False
                phase["expert_requirements"] = []
                phase["plan_generation_mode"] = "pending_real_model_verification"
        return result

    runner.validate_structured_plan = validate_plan
    runner.validate_phase_plan = validate_phase_plan
    runner.qa_is_running = qa_is_running
    runner.post = post
    runner.get = get
    runner.save_state = save_state
    try:
        runner.main()
    except Exception as exc:
        runner.save_state(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            backend_log_tail=runner.log_tail(),
        )
        runner.emit("e2e_failed", error=f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
