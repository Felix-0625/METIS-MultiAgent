import inspect

from api import routes_adjustments


def _issue(issue_id: str = "issue-1"):
    return {
        "id": issue_id,
        "severity": "error",
        "file": "backend/app.py",
        "message": "endpoint returns the wrong status",
        "status": "open",
    }


def test_final_qa_has_no_global_business_round_limit():
    source = inspect.getsource(routes_adjustments._run_final_qa_loop)

    assert "while True:" in source
    assert "range(1, MAX_FINAL_QC_ROUNDS + 1)" not in source
    assert "round_num >= MAX_FINAL_QC_ROUNDS" not in source


def test_final_qa_convergence_matches_phase_policy():
    assert routes_adjustments.FINAL_QA_DISCOVERY_PASS_LIMIT == 2
    assert routes_adjustments.FINAL_QA_ISSUE_REPAIR_ATTEMPT_LIMIT == 3


def test_local_runtime_failure_is_executed_and_actionable_for_qc_rework():
    result = {
        "enabled": False,
        "mode": "local",
        "status": "unsupported_runtime_profile",
        "passed": False,
        "error_category": "project_defect",
        "actionable": True,
    }

    assert routes_adjustments._runtime_acceptance_executed(result) is True
    assert routes_adjustments._runtime_failure_actionable(result) is True


def test_unconfigured_runtime_is_not_misclassified_as_executed():
    result = {
        "enabled": False,
        "mode": "remote",
        "passed": False,
        "error_category": "environment_misconfigured",
    }

    assert routes_adjustments._runtime_acceptance_executed(result) is False
    assert routes_adjustments._runtime_failure_actionable(result) is False


def test_runtime_machine_finding_is_reviewed_by_final_qc_before_rework():
    source = inspect.getsource(routes_adjustments._run_final_qa_loop)

    assert 'external_findings=[runtime_issue]' in source
    assert 'reviewed_runtime_issues' in source


def test_unowned_finding_requires_three_qc_reviews_before_handoff():
    source = inspect.getsource(routes_adjustments._run_final_qa_loop)

    assert 'reclassification_signatures' in source
    assert '>= FINAL_QA_ISSUE_REPAIR_ATTEMPT_LIMIT' in source
    assert 'status": "qc_reclassifying"' in source


def test_deferred_findings_complete_quality_cycle_without_release_bypass():
    source = inspect.getsource(routes_adjustments._run_final_qa_loop)

    assert '"status": "completed_with_deferred_issues"' in source
    assert '"quality_cycle_completed": True' in source
    assert '"release_ready": False' in source


def test_final_qa_handoff_is_deduplicated_and_visible():
    status = {"needs_manual": []}

    first = routes_adjustments._handoff_final_qa_issues(
        status, [_issue()], "repair failed three times",
    )
    second = routes_adjustments._handoff_final_qa_issues(
        status, [_issue()], "repair failed three times",
    )

    assert len(first) == 1
    assert len(second) == 1
    assert second[0]["status"] == "needs_manual"
    assert second[0]["handoff_target"] == "fullstack_engineer"
    assert second[0]["file_label"] == "待整改"
    assert status["handoff_count"] == 1


def test_handed_off_issue_remains_a_final_signoff_blocker():
    status = {"needs_manual": []}
    routes_adjustments._handoff_final_qa_issues(
        status, [_issue()], "repair failed three times",
    )

    assert routes_adjustments._final_qa_handoff_signatures(status)
    assert status["needs_manual"][0]["severity"] == "error"
