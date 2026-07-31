import copy
import hashlib
import json
from types import SimpleNamespace

from api import routes_phases
from core.evidence import EvidenceKind, create_evidence
from core.phase_execution_contract import (
    acceptance_criterion_contracts,
    build_phase_evidence_bundle,
    evidence_record_proves_criterion,
)


def _regenerated_artifact_bundle(tmp_path):
    path = "artifact.txt"
    payload = b"stable artifact bytes\n"
    (tmp_path / path).write_bytes(payload)
    byte_digest = hashlib.sha256(payload).hexdigest()
    phase = {
        "phase_id": "phase-1",
        "execution_generation": "generation-1",
        "roles_needed": ["backend"],
        "dependencies": [],
        "acceptance_criteria": [f"{path} exists"],
        "task_contract": [{
            "task_id": "task-1",
            "name": "Deliver artifact",
            "roles": ["backend"],
            "dependencies": [],
            "acceptance_criteria": [f"{path} exists"],
        }],
        "expert_requirements": [{
            "task_id": "task-1",
            "task_name": "Deliver artifact",
            "task_description": "Deliver artifact",
            "required_role": "backend",
            "dependencies": [],
            "acceptance_criteria": [f"{path} exists"],
        }],
    }
    assignments = [{
        "agent_id": "agent-1",
        "required_role": "backend",
        "task_ids": ["task-1"],
        "required_files": [path],
        "delivery_evidence": {
            "files": [{
                "path": path,
                "sha256": byte_digest,
                "size": len(payload),
            }],
        },
    }]
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry={
            path: {"phase_id": "phase-1", "agent_id": "agent-1"},
        },
        run_records={
            "task-1": {
                "run_id": "run-1",
                "status": "succeeded",
                "started_at": 1,
                "finished_at": 2,
                "payload": {
                    "agent_id": "agent-1",
                    "task_id": "task-1",
                    "phase_id": "phase-1",
                    "execution_generation": "generation-1",
                },
            },
        },
    )
    return phase, assignments, bundle


def test_receipt_digest_survives_real_artifact_evidence_rebuild(
    tmp_path, monkeypatch,
):
    phase, assignments, first = _regenerated_artifact_bundle(tmp_path)
    _same_phase, _same_assignments, second = _regenerated_artifact_bundle(
        tmp_path
    )

    # build_phase_evidence_bundle intentionally creates fresh record identity.
    assert json.dumps(first, sort_keys=True) != json.dumps(second, sort_keys=True)
    digest = routes_phases._phase_evidence_bundle_digest(first)
    assert routes_phases._phase_evidence_bundle_digest(second) == digest

    project_id = "stable-rebuild"
    contract_digest = "sha256:" + ("c" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    phase.update({
        "user_confirmed": True,
        "execution_artifact_baseline_digest": baseline_digest,
        "validated_completion_receipt": {
            "project_id": project_id,
            "phase_id": "phase-1",
            "execution_generation": "generation-1",
            "contract_digest": contract_digest,
            "requirements_revision": 1,
            "artifact_baseline_digest": baseline_digest,
            "bundle_digest_version": 2,
            "bundle_digest": digest,
            "task_ids": ["task-1"],
        },
    })
    monkeypatch.setattr(
        routes_phases,
        "_locked_phase_evidence_bundle",
        lambda *_args: (assignments, second),
    )
    monkeypatch.setattr(
        routes_phases,
        "validate_phase_execution_evidence",
        lambda *_args: SimpleNamespace(valid=True),
    )

    assert routes_phases._verified_completed_task_ids(
        SimpleNamespace(project_id=project_id),
        SimpleNamespace(phases=[phase]),
        project_id=project_id,
        contract_digest=contract_digest,
        requirements_revision=1,
    ) == ["task-1"]


def test_completed_phase_survives_future_phase_contract_expansion(
    tmp_path, monkeypatch,
):
    phase, assignments, bundle = _regenerated_artifact_bundle(tmp_path)
    project_id = "future-phase-contract-expansion"
    phase_contract_digest = "sha256:" + ("a" * 64)
    expanded_contract_digest = "sha256:" + ("b" * 64)
    baseline_digest = "sha256:" + ("c" * 64)
    phase.update({
        "user_confirmed": True,
        "execution_contract_digest": phase_contract_digest,
        "execution_artifact_baseline_digest": baseline_digest,
        "validated_completion_receipt": {
            "project_id": project_id,
            "phase_id": "phase-1",
            "execution_generation": "generation-1",
            "contract_digest": phase_contract_digest,
            "requirements_revision": 1,
            "artifact_baseline_digest": baseline_digest,
            "bundle_digest_version": 2,
            "bundle_digest": routes_phases._phase_evidence_bundle_digest(bundle),
            "task_ids": ["task-1"],
        },
    })
    monkeypatch.setattr(
        routes_phases,
        "_locked_phase_evidence_bundle",
        lambda *_args: (assignments, bundle),
    )
    monkeypatch.setattr(
        routes_phases,
        "validate_phase_execution_evidence",
        lambda *_args: SimpleNamespace(valid=True),
    )

    assert routes_phases._verified_completed_task_ids(
        SimpleNamespace(project_id=project_id),
        SimpleNamespace(phases=[phase]),
        project_id=project_id,
        contract_digest=expanded_contract_digest,
        requirements_revision=1,
    ) == ["task-1"]
    phase["execution_contract_digest"] = expanded_contract_digest
    assert routes_phases._verified_completed_task_ids(
        SimpleNamespace(project_id=project_id),
        SimpleNamespace(phases=[phase]),
        project_id=project_id,
        contract_digest=expanded_contract_digest,
        requirements_revision=1,
    ) == []


def test_completed_phase_dependency_uses_execution_gate_not_qa_projection(
    tmp_path, monkeypatch,
):
    phase, assignments, bundle = _regenerated_artifact_bundle(tmp_path)
    bundle["tasks"][0]["acceptance"][0].update({
        "passed": False,
        "evidence_ids": [],
    })
    project_id = "qc-confirmed-without-generated-machine-criterion"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    phase.update({
        "user_confirmed": True,
        "execution_contract_digest": contract_digest,
        "execution_artifact_baseline_digest": baseline_digest,
        "validated_completion_receipt": {
            "project_id": project_id,
            "phase_id": "phase-1",
            "execution_generation": "generation-1",
            "contract_digest": contract_digest,
            "requirements_revision": 1,
            "artifact_baseline_digest": baseline_digest,
            "bundle_digest_version": 2,
            "bundle_digest": routes_phases._phase_evidence_bundle_digest(
                bundle
            ),
            "task_ids": ["task-1"],
        },
    })
    monkeypatch.setattr(
        routes_phases,
        "_locked_phase_evidence_bundle",
        lambda *_args: (assignments, bundle),
    )

    assert routes_phases._verified_completed_task_ids(
        SimpleNamespace(project_id=project_id),
        SimpleNamespace(phases=[phase]),
        project_id=project_id,
        contract_digest=contract_digest,
        requirements_revision=1,
    ) == ["task-1"]


def test_legacy_receipt_migrates_via_persisted_bundle_semantics(
    tmp_path, monkeypatch,
):
    phase, assignments, persisted = _regenerated_artifact_bundle(tmp_path)
    _same_phase, _same_assignments, rebuilt = _regenerated_artifact_bundle(
        tmp_path
    )
    project_id = "legacy-stable-rebuild"
    contract_digest = "sha256:" + ("d" * 64)
    baseline_digest = "sha256:" + ("e" * 64)
    phase.update({
        "user_confirmed": True,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_evidence_bundle": copy.deepcopy(persisted),
        "validated_completion_receipt": {
            "project_id": project_id,
            "phase_id": "phase-1",
            "execution_generation": "generation-1",
            "contract_digest": contract_digest,
            "requirements_revision": 1,
            "artifact_baseline_digest": baseline_digest,
            "bundle_digest": (
                routes_phases._legacy_phase_evidence_bundle_digest(
                    persisted,
                )
            ),
            "task_ids": ["task-1"],
        },
    })
    monkeypatch.setattr(
        routes_phases,
        "_locked_phase_evidence_bundle",
        lambda *_args: (assignments, rebuilt),
    )
    monkeypatch.setattr(
        routes_phases,
        "validate_phase_execution_evidence",
        lambda *_args: SimpleNamespace(valid=True),
    )

    assert routes_phases._verified_completed_task_ids(
        SimpleNamespace(project_id=project_id),
        SimpleNamespace(phases=[phase]),
        project_id=project_id,
        contract_digest=contract_digest,
        requirements_revision=1,
    ) == ["task-1"]


def test_receipt_digest_still_binds_durable_authoritative_evidence_identity():
    durable = create_evidence(
        EvidenceKind.TEST,
        "run-1",
        "metis.runner",
        {
            "command": "pytest -q",
            "exit_code": 0,
            "tests_total": 1,
            "tests_failed": 0,
            "task_id": "task-1",
            "task_run_id": "run-1",
            "execution_generation": "generation-1",
            "covered_criterion_ids": ["task-1:acceptance:1"],
        },
        observed_at=100,
    ).to_dict()
    bundle = {
        "schema_version": 3,
        "tasks": [{
            "task_id": "task-1",
            "evidence": [durable],
            "acceptance": [{
                "criterion_id": "task-1:acceptance:1",
                "evidence_ids": [durable["evidence_id"]],
            }],
        }],
        "phase_evidence": [],
        "phase_acceptance": [],
    }
    replacement = copy.deepcopy(bundle)
    replacement_record = replacement["tasks"][0]["evidence"][0]
    replacement_record["evidence_id"] = "replacement-authoritative-id"
    replacement_record["observed_at"] = 200
    replacement["tasks"][0]["acceptance"][0]["evidence_ids"] = [
        replacement_record["evidence_id"]
    ]

    assert routes_phases._phase_evidence_bundle_digest(
        replacement
    ) != routes_phases._phase_evidence_bundle_digest(bundle)


def test_regression_suite_criterion_selects_backend_test_gate():
    contract = acceptance_criterion_contracts(
        "task-1", ["backend regression suite passes"]
    )[0]
    row = {
        "contract": contract,
        "scope_text": "backend quality assurance",
    }

    assert routes_phases._pre_qa_gate_matches_contract(
        "test-backend", row
    ) is True


def test_install_criterion_accepts_controller_selected_npm_ci_evidence():
    contract = acceptance_criterion_contracts(
        "task-1", ["backend install succeeds"]
    )[0]
    criterion_id = contract["criterion_id"]
    generation = "generation-1"
    raw = {
        "kind": "command",
        "command": "npm ci",
        "exit_code": 0,
        "passed": True,
        "log_digest": "sha256:" + ("1" * 64),
        "task_id": "task-1",
        "task_run_id": "run-1",
        "execution_generation": generation,
        "covered_criterion_ids": [criterion_id],
    }

    assert evidence_record_proves_criterion(
        raw,
        contract,
        task_id="task-1",
        task_run_id="run-1",
        execution_generation=generation,
    ) is True
