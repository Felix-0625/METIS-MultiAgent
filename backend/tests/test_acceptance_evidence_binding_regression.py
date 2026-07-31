import hashlib
from types import SimpleNamespace

import pytest

from api import routes_phases, routes_supervisor
from agents import quality_agents
from core.phase_execution_contract import build_phase_evidence_bundle
from core.pre_qa_verifier import CommandGate


GENERATION = "generation-acceptance-binding"
RUN_ID = "run-acceptance-binding"
TASK_ID = "task-acceptance-binding"


def _context_and_phase(raw_evidence):
    task = {
        "task_id": TASK_ID,
        "acceptance_criteria": [
            "pytest unit suite passes",
            "pytest integration suite passes",
        ],
    }
    receipt = {
        "status": "succeeded",
        "completion_run_id": RUN_ID,
        "execution_generation": GENERATION,
        "required_files": [],
        "result": {"delivery_evidence": {}},
    }
    phase = {
        "phase_id": "phase-binding",
        "execution_generation": GENERATION,
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": "sha256:" + ("b" * 64),
        "pre_qa_result": {
            "passed": True,
            "status": "passed",
            "issues": [],
            "evidence": [raw_evidence],
        },
    }
    agent = {
        "id": "agent-binding",
        "phase_id": phase["phase_id"],
        "locked_tasks": [task],
        "task_execution_receipts": {TASK_ID: receipt},
    }
    ctx = SimpleNamespace(
        project_id="project-binding",
        agents={agent["id"]: agent},
        supervisor_quality_runs={},
    )
    return ctx, phase, receipt


def _raw_gate(*, criterion_bindings=None):
    raw = {
        "kind": "test",
        "gate_id": "pytest",
        "command": "pytest -q",
        "exit_code": 0,
        "passed": True,
        "task_id": TASK_ID,
        "task_run_id": RUN_ID,
        "execution_generation": GENERATION,
        # A producer-controlled coverage claim is not an authoritative
        # controller binding and must not widen the gate's scope.
        "covered_criterion_ids": [
            f"{TASK_ID}:acceptance:1",
            f"{TASK_ID}:acceptance:2",
        ],
        "log_digest": "sha256:" + ("c" * 64),
        "log_excerpt": "2 passed",
    }
    if criterion_bindings is not None:
        raw["criterion_bindings"] = criterion_bindings
    return raw


def test_unbound_raw_gate_cannot_claim_any_acceptance_criterion():
    ctx, phase, receipt = _context_and_phase(_raw_gate())

    routes_phases._record_server_acceptance_evidence(ctx, phase)

    assert receipt.get("criterion_evidence") in (None, [])
    assert phase.get("authoritative_criterion_evidence") in (None, [])


def test_one_explicit_gate_binding_cannot_expand_to_a_second_criterion():
    first_id = f"{TASK_ID}:acceptance:1"
    ctx, phase, receipt = _context_and_phase(_raw_gate(
        criterion_bindings=[{
            "task_id": TASK_ID,
            "task_run_id": RUN_ID,
            "execution_generation": GENERATION,
            "criterion_id": first_id,
        }],
    ))

    routes_phases._record_server_acceptance_evidence(ctx, phase)

    bindings = receipt.get("criterion_evidence") or []
    assert [binding["criterion_id"] for binding in bindings] == [first_id]
    assert bindings[0]["record"]["payload"]["covered_criterion_ids"] == [
        first_id,
    ]
    assert {
        record["evidence_id"]
        for record in phase.get("authoritative_criterion_evidence") or []
    } == {bindings[0]["record"]["evidence_id"]}


def test_controller_attaches_and_materializes_exact_task_and_phase_bindings():
    phase_id = "phase-controller-binding"
    task_id = "task-controller-binding"
    generation = "generation-controller-binding"
    run_id = "run-controller-binding"
    criterion = "npm test in backend exits 0"
    receipt = {
        "status": "succeeded",
        "completion_run_id": run_id,
        "execution_generation": generation,
        "required_files": [],
        "result": {"delivery_evidence": {}},
    }
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "acceptance_criteria": [criterion],
        "task_contract": [{
            "task_id": task_id,
            "acceptance_criteria": [criterion],
        }],
    }
    agent = {
        "id": "agent-controller-binding",
        "phase_id": phase_id,
        "locked_tasks": phase["task_contract"],
        "task_execution_receipts": {task_id: receipt},
    }
    supervisor_run = {}
    ctx = SimpleNamespace(
        project_id="project-controller-binding",
        agents={agent["id"]: agent},
        supervisor_quality_runs={phase_id: supervisor_run},
    )
    gate = CommandGate(
        "test-backend",
        "test",
        ("npm", "test", "--", "--runInBand"),
        "backend",
    )
    payload = {
        "passed": True,
        "evidence": [{
            "kind": "test",
            "gate_id": "test-backend",
            "command": "npm test -- --runInBand",
            "exit_code": 0,
            "passed": True,
            "log_digest": "sha256:" + ("d" * 64),
            "log_excerpt": "12 passed",
        }],
    }

    routes_phases._attach_pre_qa_criterion_bindings(
        ctx, phase, payload, (gate,),
    )
    phase["pre_qa_result"] = payload
    routes_phases._record_server_acceptance_evidence(ctx, phase)

    raw_bindings = payload["evidence"][0]["criterion_bindings"]
    assert {
        (item["task_id"], item["task_run_id"], item["criterion_id"])
        for item in raw_bindings
    } == {
        (task_id, run_id, f"{task_id}:acceptance:1"),
        (phase_id, "", f"{phase_id}:acceptance:1"),
    }
    assert [
        item["criterion_id"]
        for item in receipt["criterion_evidence"]
    ] == [f"{task_id}:acceptance:1"]
    assert [
        item["criterion_id"]
        for item in supervisor_run["phase_evidence"]
    ] == [f"{phase_id}:acceptance:1"]


def test_unscoped_command_contract_matches_only_exact_root_gate():
    row = {
        "contract": {
            "criterion": "npm test exits 0",
            "evidence_spec": {
                "source_class": "command_test",
                "command_tokens": ["npm test", "test"],
            },
        },
        "scope_text": "",
    }

    assert routes_phases._pre_qa_gate_matches_contract(
        "test-root", row,
    ) is True
    assert routes_phases._pre_qa_gate_matches_contract(
        "test-backend", row,
    ) is False


def test_phase_command_scope_ignores_nested_project_contract_noise():
    phase = {
        "phase_id": "phase-root-tests",
        "execution_generation": "generation-root-tests",
        "name": "QA and Testing",
        "description": "Run the Node.js built-in test suite.",
        "tech_stack": ["Node.js", "node:test"],
        "acceptance_criteria": ["npm test exits 0"],
        "project_contract": {
            "source_requirements": "Build a backend and frontend application",
            "phases": [
                {"name": "Backend implementation"},
                {"name": "Frontend implementation"},
            ],
        },
    }
    ctx = SimpleNamespace(project_id="project-root-tests", agents={})

    rows = routes_phases._phase_pre_qa_command_contracts(ctx, phase)

    assert len(rows) == 1
    assert routes_phases._pre_qa_gate_matches_contract(
        "test-root", rows[0],
    ) is True
    assert routes_phases._pre_qa_gate_matches_contract(
        "test-backend", rows[0],
    ) is False
    assert routes_phases._pre_qa_gate_matches_contract(
        "test-frontend", rows[0],
    ) is False


def test_api_evidence_expected_status_comes_from_criterion_contract():
    task_id = "task-api-binding"
    run_id = "run-api-binding"
    generation = "generation-api-binding"
    criterion = "POST /api/todos returns 201 JSON"
    criterion_id = f"{task_id}:acceptance:1"
    receipt = {
        "status": "succeeded",
        "completion_run_id": run_id,
        "execution_generation": generation,
        "required_files": [],
    }
    raw = {
        "kind": "api",
        "gate_id": "create-todo",
        "passed": True,
        "endpoint": "/api/todos",
        "status_code": 201,
        "expected_statuses": [200],
        "assertions": [{"name": "response is JSON", "passed": True}],
        "log_digest": "sha256:" + ("f" * 64),
        "criterion_bindings": [{
            "task_id": task_id,
            "task_run_id": run_id,
            "execution_generation": generation,
            "criterion_id": criterion_id,
        }],
    }
    phase = {
        "phase_id": "phase-api-binding",
        "execution_generation": generation,
        "pre_qa_result": {"evidence": [raw]},
    }
    agent = {
        "id": "agent-api-binding",
        "phase_id": phase["phase_id"],
        "locked_tasks": [{
            "task_id": task_id,
            "acceptance_criteria": [criterion],
        }],
        "task_execution_receipts": {task_id: receipt},
    }
    ctx = SimpleNamespace(
        project_id="project-api-binding",
        agents={agent["id"]: agent},
        supervisor_quality_runs={},
    )

    routes_phases._record_server_acceptance_evidence(ctx, phase)

    payload = receipt["criterion_evidence"][0]["record"]["payload"]
    assert payload["status_code"] == 201
    assert payload["expected_statuses"] == [201]


@pytest.mark.parametrize("criterion", [
    "backend/package.json exists and npm run build passes",
    "backend/package.json exists and GET /health returns 200",
])
def test_artifact_bytes_alone_cannot_pass_a_compound_runtime_criterion(
    tmp_path,
    criterion,
):
    task_id = "compound-task"
    agent_id = "agent-compound"
    run_id = "run-compound"
    path = "backend/package.json"
    payload = b'{"scripts":{"build":"echo placeholder"}}'
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    phase = {
        "phase_id": "phase-compound",
        "execution_generation": "generation-compound",
        "execution_contract_digest": "sha256:" + ("d" * 64),
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": "sha256:" + ("e" * 64),
        "roles_needed": ["backend"],
        "dependencies": [],
        "acceptance_criteria": [],
        "task_contract": [{
            "task_id": task_id,
            "name": "Compound task",
            "roles": ["backend"],
            "dependencies": [],
            "acceptance_criteria": [criterion],
        }],
        "expert_requirements": [{
            "task_id": task_id,
            "task_name": "Compound task",
            "task_description": "Compound task",
            "required_role": "backend",
            "dependencies": [],
            "acceptance_criteria": [criterion],
        }],
    }
    assignment = {
        "agent_id": agent_id,
        "required_role": "backend",
        "task_ids": [task_id],
        "required_files": [path],
        "delivery_evidence": {
            "files": [{
                "path": path,
                "sha256": digest,
                "size": len(payload),
            }],
        },
    }
    bundle = build_phase_evidence_bundle(
        phase,
        [assignment],
        workspace=tmp_path,
        file_registry={
            path: {"phase_id": phase["phase_id"], "agent_id": agent_id},
        },
        run_records={
            task_id: {
                "run_id": run_id,
                "status": "succeeded",
                "started_at": 1,
                "finished_at": 2,
                "payload": {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "phase_id": phase["phase_id"],
                    "execution_generation": phase["execution_generation"],
                    "contract_digest": phase["execution_contract_digest"],
                    "requirements_revision": 1,
                    "artifact_baseline_digest": (
                        phase["execution_artifact_baseline_digest"]
                    ),
                },
            },
        },
    )

    acceptance = bundle["tasks"][0]["acceptance"][0]
    assert acceptance["passed"] is False
    assert acceptance["evidence_ids"] == []


def test_generic_supervisor_pass_cannot_manufacture_semantic_observations():
    phase_id = "phase-generic-supervisor"
    task_id = "task-generic-supervisor"
    generation = "generation-generic-supervisor"
    receipt = {
        "status": "succeeded",
        "completion_run_id": "run-task-generic-supervisor",
        "execution_generation": generation,
        "required_files": [],
        "result": {},
    }
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "acceptance_criteria": [],
    }
    supervisor_run = {
        "run_id": "run-supervisor-generic",
        "status": "completed",
        "completion_gate": {"passed": True},
        "scope": {"scope_digest": "f" * 64},
        "rounds": [{
            "state": "verified",
            "issues": [],
            # This proves only that generic QA found no blocker. It does not
            # attest the locked semantic criterion below.
            "evidence": [{
                "kind": "qa",
                "passed": True,
                "command": "QAAgent.inspect",
                "log": "score=100; no issues",
            }],
        }],
    }
    agent = {
        "id": "agent-generic-supervisor",
        "phase_id": phase_id,
        "locked_tasks": [{
            "task_id": task_id,
            "acceptance_criteria": [
                "the user-visible behavior matches the locked workflow",
            ],
        }],
        "task_execution_receipts": {task_id: receipt},
    }
    ctx = SimpleNamespace(
        project_id="project-generic-supervisor",
        agents={agent["id"]: agent},
        supervisor_quality_runs={phase_id: supervisor_run},
    )

    routes_phases._record_supervisor_acceptance_evidence(ctx, phase)

    assert receipt.get("criterion_evidence") in (None, [])
    assert phase.get("authoritative_criterion_evidence") in (None, [])


def test_exact_supervisor_observations_bind_current_task_and_phase_criteria():
    phase_id = "phase-precise-supervisor"
    task_id = "task-precise-supervisor"
    generation = "generation-precise-supervisor"
    task_run_id = "run-task-precise-supervisor"
    supervisor_run_id = "run-supervisor-precise"
    qa_round_id = "qa-round-supervisor-precise-1"
    artifact_digest = "d" * 64
    task_criterion = "the saved item is visible in the list"
    phase_criterion = "the phase workflow is usable end to end"
    receipt = {
        "status": "succeeded",
        "completion_run_id": task_run_id,
        "execution_generation": generation,
        "required_files": ["src/app.py"],
        "result": {},
    }
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "acceptance_criteria": [phase_criterion],
    }
    observations = [
        {
            "task_id": task_id,
            "task_run_id": task_run_id,
            "execution_generation": generation,
            "criterion_id": f"{task_id}:acceptance:1",
            "criterion": task_criterion,
            "passed": True,
            "observation": "src/app.py stores the item and returns it in list_items.",
            "observed_files": ["src/app.py", "package.json"],
            "artifact_digest": artifact_digest,
            "qa_round_id": qa_round_id,
        },
        {
            "task_id": phase_id,
            "task_run_id": supervisor_run_id,
            "execution_generation": generation,
            "criterion_id": f"{phase_id}:acceptance:1",
            "criterion": phase_criterion,
            "passed": True,
            "observation": "src/app.py connects the complete create and list workflow.",
            "observed_files": ["src/app.py", "package.json"],
            "artifact_digest": artifact_digest,
            "qa_round_id": qa_round_id,
        },
    ]
    supervisor_run = {
        "run_id": supervisor_run_id,
        "status": "completed",
        "completion_gate": {"passed": True},
        "scope": {
            "artifact_digest": artifact_digest,
            "files": ["src/app.py"],
            "delivery_manifest": {
                "files": [
                    {"path": "src/app.py"},
                    {"path": "package.json"},
                ],
            },
        },
        "rounds": [{
            "qa_round_id": qa_round_id,
            "state": "verified",
            "evidence": [{
                "kind": "qa",
                "passed": True,
                "exit_code": 0,
                "metadata": {
                    "acceptance_observations": observations,
                },
            }],
        }],
    }
    agent = {
        "id": "agent-precise-supervisor",
        "phase_id": phase_id,
        "locked_tasks": [{
            "task_id": task_id,
            "acceptance_criteria": [task_criterion],
        }],
        "task_execution_receipts": {task_id: receipt},
    }
    ctx = SimpleNamespace(
        project_id="project-precise-supervisor",
        agents={agent["id"]: agent},
        supervisor_quality_runs={phase_id: supervisor_run},
    )

    routes_phases._record_supervisor_acceptance_evidence(ctx, phase)

    task_binding = receipt["criterion_evidence"][0]
    phase_binding = supervisor_run["phase_evidence"][0]
    assert task_binding["criterion_id"] == f"{task_id}:acceptance:1"
    assert phase_binding["criterion_id"] == f"{phase_id}:acceptance:1"
    assert task_binding["record"]["kind"] == "supervisor_observation"
    assert task_binding["record"]["producer"] == "metis.supervisor"
    assert task_binding["record"]["status"] == "passed"
    assert phase_binding["record"]["status"] == "passed"


def test_functionality_reviewer_emits_exact_per_criterion_observation(
    monkeypatch,
):
    criterion_id = "task-review:acceptance:1"
    criterion = "the saved item is visible in the list"
    response = {
        "passed": True,
        "score": 100,
        "issues": [],
        "summary": "implemented",
        "acceptance_results": [{
            "criterion_id": criterion_id,
            "criterion": criterion,
            "passed": True,
            "observation": "src/app.py returns stored items from list_items.",
            "observed_files": ["src/app.py"],
        }],
    }
    calls = []

    def _review(_client, messages, **_kwargs):
        calls.append(messages)
        return {
            "content": __import__("json").dumps(response),
        }

    monkeypatch.setattr("core.hermes_client.chat_for_purpose", _review)
    hermes = SimpleNamespace(
        base_url="http://unused.invalid",
        api_key="unused",
        model="unused",
        max_tokens=4096,
    )

    result = quality_agents.check_layer3_functionality(
        [(
            "src/app.py",
            "items = []\ndef save(item): items.append(item)\n"
            "def list_items(): return list(items)\n",
        )],
        "Save and list items",
        hermes,
        acceptance_contracts=[{
            "task_id": "task-review",
            "task_run_id": "run-task-review",
            "execution_generation": "generation-review",
            "criterion_id": criterion_id,
            "criterion": criterion,
            "source_class": "semantic",
            "evidence_spec": {"source_class": "semantic"},
        }],
    )

    assert result["passed"] is True
    system_prompt = calls[0][0].content
    assert "semantic source review" in system_prompt
    assert "do not fail it only because no command or browser was executed" in system_prompt
    assert result["acceptance_observations"] == [{
        "task_id": "task-review",
        "task_run_id": "run-task-review",
        "execution_generation": "generation-review",
        "criterion_id": criterion_id,
        "criterion": criterion,
        "source_class": "semantic",
        "evidence_spec": {"source_class": "semantic"},
        "passed": True,
        "observation": "src/app.py returns stored items from list_items.",
        "observed_files": ["src/app.py"],
    }]


def test_functionality_reviewer_ignores_invalid_optional_acceptance_evidence(
    monkeypatch,
):
    criterion_id = "task-review-retry:acceptance:1"
    criterion = "the saved item is visible in the list"
    responses = [
        {
            "passed": True,
            "score": 100,
            "issues": [],
            "summary": "implemented",
            "acceptance_results": [{
                "criterion_id": criterion_id,
                "criterion": "the item can be viewed",
                "passed": True,
                "observation": "The implementation appears complete.",
                "observed_files": [],
            }],
        },
        {
            "passed": True,
            "score": 100,
            "issues": [],
            "summary": "implemented",
            "acceptance_results": [{
                "criterion_id": criterion_id,
                "criterion": criterion,
                "passed": True,
                "observation": (
                    "src/app.py appends saved items and list_items returns them."
                ),
                "observed_files": ["src/app.py"],
            }],
        },
    ]
    calls = []

    def _review(_client, messages, **kwargs):
        calls.append((messages, kwargs))
        return {
            "content": __import__("json").dumps(
                responses[len(calls) - 1]
            ),
        }

    monkeypatch.setattr("core.hermes_client.chat_for_purpose", _review)
    hermes = SimpleNamespace(
        base_url="http://unused.invalid",
        api_key="unused",
        model="unused",
        max_tokens=4096,
    )

    result = quality_agents.check_layer3_functionality(
        [(
            "src/app.py",
            "items = []\ndef save(item): items.append(item)\n"
            "def list_items(): return list(items)\n",
        )],
        "Save and list items",
        hermes,
        acceptance_contracts=[{
            "task_id": "task-review-retry",
            "task_run_id": "run-task-review-retry",
            "execution_generation": "generation-review-retry",
            "criterion_id": criterion_id,
            "criterion": criterion,
            "source_class": "semantic",
            "evidence_spec": {"source_class": "semantic"},
        }],
    )

    assert len(calls) == 1
    assert result["passed"] is True
    assert result["acceptance_observations"] == []


def test_functionality_reviewer_does_not_fail_for_optional_acceptance_omission(
    monkeypatch,
):
    """After range(2)→range(3), the reviewer exhausts 3 calls before failing."""
    criterion_id = "task-review-fail:acceptance:1"
    criterion = "the saved item is visible in the list"
    invalid_response = {
        "passed": True,
        "score": 100,
        "issues": [],
        "summary": "implemented",
        "acceptance_results": [{
            "criterion_id": criterion_id,
            "criterion": criterion,
            "passed": True,
            "observation": "The implementation appears complete.",
            "observed_files": [],
        }],
    }
    calls = []

    def _review(_client, messages, **kwargs):
        calls.append((messages, kwargs))
        return {
            "content": __import__("json").dumps(invalid_response),
        }

    monkeypatch.setattr("core.hermes_client.chat_for_purpose", _review)
    hermes = SimpleNamespace(
        base_url="http://unused.invalid",
        api_key="unused",
        model="unused",
        max_tokens=4096,
    )

    result = quality_agents.check_layer3_functionality(
        [(
            "src/app.py",
            "items = []\ndef save(item): items.append(item)\n"
            "def list_items(): return list(items)\n",
        )],
        "Save and list items",
        hermes,
        acceptance_contracts=[{
            "task_id": "task-review-fail",
            "task_run_id": "run-task-review-fail",
            "execution_generation": "generation-review-fail",
            "criterion_id": criterion_id,
            "criterion": criterion,
            "source_class": "semantic",
            "evidence_spec": {"source_class": "semantic"},
        }],
    )

    assert len(calls) == 1
    assert result["passed"] is True
    assert result.get("acceptance_observations", []) == []
    assert result["issue_count"] == 0


def test_phase_qc_transfers_controller_owned_acceptance_identity(
    monkeypatch, tmp_path,
):
    phase_id = "phase-qc-transfer"
    task_id = "task-qc-transfer"
    generation = "generation-qc-transfer"
    task_run_id = "run-task-qc-transfer"
    supervisor_run_id = "run-supervisor-qc-transfer"
    qa_round_id = "qa-round-qc-transfer"
    artifact_digest = "e" * 64
    task_criterion = "the saved item is visible in the list"
    phase_criterion = "the phase workflow is usable end to end"
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("def list_items(): return []\n", encoding="utf-8")
    phase = {
        "phase_id": phase_id,
        "description": "Save and list items",
        "execution_generation": generation,
        "acceptance_criteria": [phase_criterion],
    }
    agent = {
        "id": "agent-qc-transfer",
        "phase_id": phase_id,
        "role": "backend",
        "locked_tasks": [{
            "task_id": task_id,
            "acceptance_criteria": [task_criterion],
        }],
        "task_execution_receipts": {
            task_id: {
                "status": "succeeded",
                "completion_run_id": task_run_id,
                "execution_generation": generation,
                "required_files": ["src/app.py"],
            }
        },
    }
    ctx = SimpleNamespace(
        project_id="project-qc-transfer",
        workspace=tmp_path,
        subprojects=[{
            "id": task_id,
            "phase_id": phase_id,
            "agent_id": agent["id"],
            "description": "Save and list items",
        }],
        agents={agent["id"]: agent},
        qc_results={},
    )

    class _PhaseManager:
        phases = [phase, {"phase_id": "phase-later"}]
        file_registry = {
            "src/app.py": {
                "file_path": "src/app.py",
                "phase_id": phase_id,
                "agent_id": agent["id"],
            }
        }

        @staticmethod
        def get_phase(requested):
            return phase if requested == phase_id else None

        @staticmethod
        def get_files_by_phase(requested):
            return (
                [{"file_path": "src/app.py", "phase_id": phase_id}]
                if requested == phase_id else []
            )

    class _QA:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **kwargs):
            observations = [{
                **contract,
                "passed": True,
                "observation": "src/app.py implements the locked criterion.",
                "observed_files": ["src/app.py"],
            } for contract in kwargs["acceptance_contracts"]]
            return {
                "passed": True,
                "score": 100,
                "issues": [],
                "user_report": "passed",
                "developer_report": "passed",
                "layer_results": [],
                "needs_rewrite": False,
                "error_count": 0,
                "warning_count": 0,
                "acceptance_observations": observations,
            }

    monkeypatch.setattr(
        routes_supervisor,
        "_get_phase_manager",
        lambda _project_id: _PhaseManager(),
    )
    monkeypatch.setattr(quality_agents, "QAAgent", _QA)

    entry = routes_supervisor._run_qc_for_subproject(
        ctx,
        phase_id,
        "Phase QC transfer",
        qa_context={
            "run_id": supervisor_run_id,
            "qa_round_id": qa_round_id,
            "scope_digest": "f" * 64,
            "artifact_digest": artifact_digest,
        },
    )

    assert entry["passed"] is True
    assert {
        (
            item["task_id"],
            item["task_run_id"],
            item["criterion_id"],
            item["artifact_digest"],
            item["qa_round_id"],
        )
        for item in entry["acceptance_observations"]
    } == {
        (
            task_id,
            task_run_id,
            f"{task_id}:acceptance:1",
            artifact_digest,
            qa_round_id,
        ),
        (
            phase_id,
            supervisor_run_id,
            f"{phase_id}:acceptance:1",
            artifact_digest,
            qa_round_id,
        ),
    }
