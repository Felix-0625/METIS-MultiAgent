from core.issue_ledger import (
    CANONICAL_ID_PROVENANCE,
    ISSUE_SCHEMA_VERSION,
    canonicalize_issue,
    defect_fingerprint,
    find_canonical_issue,
    find_owner_for_path,
    mark_needs_manual,
)
from core.repair_loop import DefectStatus, RepairLoopController
from api.routes_supervisor import (
    _reconcile_needs_manual_qc_issue,
    _reconcile_pending_verification_qc_issue,
    _should_reopen_fixed_issue,
)


def _http_issue(expected: int, actual: int) -> dict:
    return {
        "rule_id": "api.http_status",
        "layer": "api_contract",
        "file_path": "./backend\\routes\\users.py",
        "symbol": "GET /users/{id}",
        "expected": expected,
        "actual": actual,
        "message": f"expected {expected}, actual {actual}",
    }


def test_opposite_http_status_semantics_have_distinct_defect_identity() -> None:
    assert defect_fingerprint(_http_issue(200, 403)) != defect_fingerprint(
        _http_issue(403, 200)
    )


def test_legacy_ids_do_not_define_canonical_defect_identity() -> None:
    one = canonicalize_issue({**_http_issue(200, 403), "id": "legacy-one"})
    two = canonicalize_issue({
        **_http_issue(200, 403),
        "issue_id": "legacy-two",
    })

    assert one["defect_id"] == two["defect_id"]
    assert one["legacy_id"] == "legacy-one"
    assert two["source_issue_id"] == "legacy-two"


def test_reused_legacy_id_cannot_merge_opposite_structured_semantics() -> None:
    one = canonicalize_issue({**_http_issue(200, 403), "defect_id": "legacy-shared"})
    opposite = canonicalize_issue({
        **_http_issue(403, 200),
        "defect_id": "legacy-shared",
    })

    assert one["source_issue_id"] == opposite["source_issue_id"] == "legacy-shared"
    assert one["defect_id"] != opposite["defect_id"]


def test_only_versioned_ledger_id_is_trusted() -> None:
    generated = canonicalize_issue(_http_issue(200, 403))
    trusted = canonicalize_issue(dict(generated))
    forged = canonicalize_issue({
        **_http_issue(200, 403),
        "defect_id": "issue-forged",
        "issue_schema_version": ISSUE_SCHEMA_VERSION,
        "defect_id_provenance": CANONICAL_ID_PROVENANCE,
    })

    assert trusted["defect_id"] == generated["defect_id"]
    assert forged["defect_id"] == generated["defect_id"]
    assert "source_issue_id" not in trusted
    assert forged["source_issue_id"] == "issue-forged"


def test_runtime_log_tail_changes_observation_not_defect() -> None:
    first = canonicalize_issue(
        {
            **_http_issue(200, 403),
            "message": "authorization contract failed\n2026-07-23T10:01:02Z request_id=one",
            "evidence": {"trace_id": "one", "duration_ms": 17},
        },
        observation_context={"qa_round_id": "round-a"},
    )
    second = canonicalize_issue(
        {
            **_http_issue(200, 403),
            "message": "authorization contract failed\n2026-07-23T10:05:09Z request_id=two",
            "evidence": {"trace_id": "two", "duration_ms": 93},
        },
        observation_context={"qa_round_id": "round-b"},
    )

    assert first["defect_id"] == second["defect_id"]
    assert first["fingerprint"] == second["fingerprint"]
    assert first["observation_id"] != second["observation_id"]


def test_location_line_drift_changes_observation_not_defect() -> None:
    first = canonicalize_issue({
        "rule_id": "python.output",
        "file_path": "app.py",
        "location": "app.py:41",
        "message": "wrong output",
    })
    second = canonicalize_issue({
        "rule_id": "python.output",
        "file_path": "app.py",
        "location": "app.py:77",
        "message": "wrong output",
    })

    assert first["defect_id"] == second["defect_id"]
    assert first["identity_confidence"] == "low"
    assert first["observation_id"] != second["observation_id"]


def test_stable_subject_distinguishes_same_rule_and_path() -> None:
    first = canonicalize_issue({
        "rule_id": "python.output",
        "file_path": "app.py",
        "subject": "render_admin",
    })
    second = canonicalize_issue({
        "rule_id": "python.output",
        "file_path": "app.py",
        "subject": "render_guest",
    })

    assert first["identity_confidence"] == "high"
    assert second["identity_confidence"] == "high"
    assert first["defect_id"] != second["defect_id"]


def test_unstructured_llm_blocker_is_low_confidence_and_order_sensitive() -> None:
    first = canonicalize_issue({
        "layer": "functionality",
        "file_path": "src/app.py",
        "severity": "error",
        "message": "allow authenticated users and deny anonymous users",
    })
    opposite = canonicalize_issue({
        "layer": "functionality",
        "file_path": "src/app.py",
        "severity": "error",
        "message": "deny authenticated users and allow anonymous users",
    })

    assert first["identity_confidence"] == "low"
    assert first["requires_identity_review"] is True
    assert first["defect_id"] != opposite["defect_id"]


def test_low_confidence_issue_never_auto_closes_or_reopens() -> None:
    low = canonicalize_issue({
        "id": "legacy-low",
        "layer": "functionality",
        "file_path": "src/app.py",
        "severity": "error",
        "message": "reviewer says behavior may be wrong",
    })

    manual = _reconcile_needs_manual_qc_issue(
        {**low, "status": "needs_manual"},
        None,
    )
    pending = _reconcile_pending_verification_qc_issue(
        {**low, "status": "pending_verification"},
        None,
    )

    assert manual["status"] == "needs_manual"
    assert pending["status"] == "pending_verification"
    assert manual["identity_review_required"] is True
    assert pending["identity_review_required"] is True
    assert _should_reopen_fixed_issue(low) is False


def test_manual_conversion_is_lossless_and_canonical() -> None:
    source = {
        **_http_issue(200, 403),
        "line": 41,
        "evidence": {"response": {"status": 403}},
        "acceptance_criteria": ["authorized request returns 200"],
        "detected_phase": "phase-api",
        "owner": {"agent_id": "backend-owner", "owner_type": "backend"},
        "custom_source_field": {"must_survive": True},
    }

    manual = mark_needs_manual(source, "automatic repair exhausted")

    assert manual["status"] == "needs_manual"
    assert manual["line"] == manual["line_no"] == 41
    assert manual["evidence"] == source["evidence"]
    assert manual["acceptance_criteria"] == source["acceptance_criteria"]
    assert manual["detected_phase"] == "phase-api"
    assert manual["responsible_agent_id"] == "backend-owner"
    assert manual["custom_source_field"] == {"must_survive": True}
    assert manual["needs_manual_reason"] == "automatic repair exhausted"


def test_owner_mapping_accepts_dot_and_slashes_but_rejects_case_ambiguity() -> None:
    registry = {
        "backend/routes/users.py": {"agent_id": "backend"},
    }
    assert find_owner_for_path(registry, ".\\backend\\routes\\users.py") == {
        "agent_id": "backend"
    }

    ambiguous = {
        "src/App.tsx": {"agent_id": "one"},
        "src/app.tsx": {"agent_id": "two"},
    }
    assert find_owner_for_path(ambiguous, "SRC/APP.TSX") == {}
    assert find_owner_for_path(registry, "../outside.py") == {}


def test_verified_defect_reopens_when_same_identity_is_reproduced() -> None:
    controller = RepairLoopController("subproject-1", "API")
    first = controller.ingest_qc_result(
        {"issues_detail": [{**_http_issue(200, 403), "severity": "error"}]},
        agent_id="backend",
        agent_role="Backend Developer",
    )
    assert len(first) == 1
    first[0].status = DefectStatus.VERIFIED

    repeated = controller.ingest_qc_result(
        {
            "issues_detail": [{
                **_http_issue(200, 403),
                "severity": "error",
                "message": "same contract failure; volatile log changed",
                "evidence": {"request_id": "new"},
            }]
        },
        agent_id="backend",
        agent_role="Backend Developer",
    )

    assert repeated == []
    assert first[0].status == DefectStatus.OPEN


def test_repair_registry_roundtrip_preserves_generated_defect_id() -> None:
    controller = RepairLoopController("subproject-1", "API")
    ticket = controller.ingest_qc_result(
        {"issues_detail": [{**_http_issue(200, 403), "severity": "error"}]},
        agent_id="backend",
        agent_role="Backend Developer",
    )[0]

    restored = RepairLoopController.from_dict(controller.to_dict())

    assert list(restored.defects) == [ticket.defect_id]
    assert restored.defects[ticket.defect_id].defect_id == ticket.defect_id


def test_defects_query_uses_same_canonical_projection() -> None:
    issue = mark_needs_manual(_http_issue(200, 403), "manual")
    results = {"__whole_project__": {"qa": {"issues_detail": [issue]}}}

    found = find_canonical_issue(results, issue["defect_id"])

    assert found is not None
    assert found["id"] == found["defect_id"] == found["issue_id"]
    assert found["file_path"] == "backend/routes/users.py"


def test_final_qa_legacy_alias_migration_is_unique_and_safe() -> None:
    first = {**_http_issue(200, 403), "issue_id": "legacy-final"}
    opposite = {**_http_issue(403, 200), "issue_id": "legacy-final"}
    results = {
        "__whole_project__": {
            "qa": {"issues_detail": [first, opposite]},
        },
    }
    canonical_first = canonicalize_issue(first)

    assert find_canonical_issue(results, canonical_first["defect_id"]) == canonical_first
    assert find_canonical_issue(results, "legacy-final") is None


def test_unique_legacy_alias_is_not_an_actionable_lookup_key() -> None:
    issue = {**_http_issue(200, 403), "issue_id": "legacy-only"}
    results = {"__whole_project__": {"qa": {"issues_detail": [issue]}}}

    assert find_canonical_issue(results, "legacy-only") is None
