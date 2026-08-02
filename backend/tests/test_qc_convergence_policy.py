from api import routes_supervisor
from core.supervisor_quality_state import _is_blocker


def test_model_discovery_is_bounded_but_deterministic_gates_remain_active():
    model_issue = {"layer": "functionality", "severity": "error"}
    syntax_issue = {"layer": "syntax", "severity": "error"}

    assert routes_supervisor._should_append_qc_issue(model_issue, True)
    assert not routes_supervisor._should_append_qc_issue(model_issue, False)
    assert routes_supervisor._should_append_qc_issue(syntax_issue, False)


def test_manual_handoff_is_visible_but_not_an_automatic_loop_blocker():
    handed_off = {
        "status": "needs_manual",
        "severity": "critical",
        "file_path": "backend/tasks.py",
        "message": "still reproducible",
    }
    active = {**handed_off, "status": "open"}

    assert not routes_supervisor._has_blocking_qc_issues([handed_off])
    assert routes_supervisor._has_blocking_qc_issues([active])
    assert not _is_blocker(handed_off)
    assert _is_blocker(active)


def test_convergence_limits_match_product_contract():
    assert routes_supervisor.QC_DISCOVERY_PASS_LIMIT == 2
    assert routes_supervisor.QC_ISSUE_REPAIR_ATTEMPT_LIMIT == 3
