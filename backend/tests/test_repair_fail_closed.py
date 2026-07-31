from core.repair_loop import (
    DefectSeverity,
    DefectStatus,
    DefectTicket,
    PhaseArbiter,
    RepairLoopController,
)


def _defect(status=DefectStatus.FIXED, security=DefectStatus.OPEN):
    return DefectTicket(
        defect_id="D-test",
        severity=DefectSeverity.P0,
        layer="syntax",
        file_path="app.py",
        line_no=1,
        message="syntax error",
        fix_hint="fix syntax",
        subproject_id="sp-test",
        agent_id="agent-test",
        agent_role="Backend Developer",
        status=status,
        security_status=security,
    )


def test_direct_verify_cannot_turn_fixed_defect_green():
    controller = RepairLoopController("sp-test", "Test")
    controller.defects["D-test"] = _defect()

    result = controller.verify_fixed("D-test")

    assert result["success"] is False
    assert controller.defects["D-test"].status == DefectStatus.FIXED


def test_manual_defect_is_not_resolved():
    controller = RepairLoopController("sp-test", "Test")
    controller.defects["D-test"] = _defect(
        status=DefectStatus.MANUAL,
        security=DefectStatus.MANUAL,
    )

    assert controller.is_all_resolved() is False


def test_resolution_requires_both_independent_gates():
    controller = RepairLoopController("sp-test", "Test")
    controller.defects["D-test"] = _defect(
        status=DefectStatus.VERIFIED,
        security=DefectStatus.OPEN,
    )
    assert controller.is_all_resolved() is False

    controller.defects["D-test"].security_status = DefectStatus.VERIFIED
    assert controller.is_all_resolved() is True


def test_force_pass_escalates_to_manual_without_resolving():
    controller = RepairLoopController("sp-test", "Test")
    controller.defects["D-test"] = _defect(
        status=DefectStatus.ESCALATED,
        security=DefectStatus.OPEN,
    )
    arbiter = PhaseArbiter("phase-test", "Test")

    result = arbiter.apply_force_pass(controller)

    assert result["success"] is False
    assert result["requires_manual"] == 1
    assert controller.defects["D-test"].status == DefectStatus.MANUAL
    assert controller.is_all_resolved() is False
