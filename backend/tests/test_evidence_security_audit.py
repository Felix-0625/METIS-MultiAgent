import logging

import pytest

from core import database
from core.evidence import (
    EvidenceKind,
    create_evidence,
    evaluate_evidence_gate,
    validate_evidence_record,
)
from core.security_audit import (
    REDACTED,
    RedactingFormatter,
    query_audit_events,
    record_audit_event,
    redact_text,
    redact_value,
)


@pytest.fixture
def isolated_audit_db(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_sqlite_path", str(tmp_path / "audit.db"))
    monkeypatch.setattr(database, "_pg_pool", None)
    monkeypatch.setattr(database, "_pg_unavailable", False)
    database.init_db()
    return tmp_path / "audit.db"


def test_recursive_redaction_covers_keys_embedded_tokens_urls_and_cycles():
    cyclic = []
    cyclic.append(cyclic)
    value = {
        "password": "plain-password",
        "nested": {
            "access_token": "eyJabcdefghijk.abcdefghijkl.abcdefghijkl",
            "passwordHash": "must-also-be-redacted",
            "message": "Authorization: Bearer secret-token-value",
            "dsn": "postgresql://metis:db-password@db.example.test/app",
            "vendor": "request failed for sk-abcdefghijklmnopqrstuvwxyz",
        },
        "cycle": cyclic,
    }

    safe = redact_value(value)

    rendered = repr(safe)
    assert safe["password"] == REDACTED
    assert safe["nested"]["access_token"] == REDACTED
    assert safe["nested"]["passwordHash"] == REDACTED
    assert "plain-password" not in rendered
    assert "secret-token-value" not in rendered
    assert "db-password" not in rendered
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "[CYCLE]" in rendered


def test_redacting_formatter_scrubs_message_arguments_and_exception_text():
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError("token=super-secret-token")
    except RuntimeError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "provider error: %s",
            ("Bearer abcdefghijklmnop",),
            __import__("sys").exc_info(),
        )

    rendered = formatter.format(record)

    assert "abcdefghijklmnop" not in rendered
    assert "super-secret-token" not in rendered
    assert REDACTED in rendered


def test_audit_events_are_redacted_persistent_and_scope_isolated(isolated_audit_db):
    own = record_audit_event(
        "PROJECT.UPDATE",
        "user-1",
        project_id="project-1",
        resource_type="project",
        resource_id="project-1",
        source_ip="127.0.0.1",
        details={"api_key": "should-not-persist", "change": "name"},
        occurred_at=100.0,
    )
    record_audit_event(
        "PROJECT.DELETE",
        "user-2",
        project_id="project-2",
        details={"password": "also-secret"},
        occurred_at=200.0,
    )

    visible = query_audit_events(
        "user-1",
        "user",
        allowed_project_ids={"project-1"},
    )

    assert [event.event_id for event in visible] == [own.event_id]
    assert visible[0].details["api_key"] == REDACTED
    assert "should-not-persist" not in repr(visible)
    with pytest.raises(PermissionError):
        query_audit_events(
            "user-1",
            "user",
            allowed_project_ids={"project-1"},
            actor_id="user-2",
        )
    all_events = query_audit_events("admin-1", "admin")
    assert len(all_events) == 2


def test_tampered_audit_event_is_not_returned(isolated_audit_db):
    event = record_audit_event("CONFIG.UPDATE", "admin-1")
    database.kv_set(
        f"audit:v1:{event.event_id}",
        {**event.to_dict(), "outcome": "failure"},
    )

    assert query_audit_events("admin-1", "admin") == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "invalid action", "actor_id": "user-1"},
        {"action": "VALID.ACTION", "actor_id": "user/../unsafe"},
        {"action": "VALID.ACTION", "actor_id": "user-1", "source_ip": "not-an-ip"},
    ],
)
def test_audit_input_validation_fails_closed(isolated_audit_db, kwargs):
    with pytest.raises(ValueError):
        record_audit_event(**kwargs)


def test_successful_test_evidence_requires_exit_code_counts_and_output():
    passed = create_evidence(
        EvidenceKind.TEST,
        "run-1",
        "metis.runner",
        {
            "command": "pytest -q",
            "exit_code": 0,
            "tests_total": 12,
            "tests_failed": 0,
            "output_summary": "12 passed",
        },
        observed_at=100.0,
    )
    failed = create_evidence(
        "test",
        "run-1",
        "metis.runner",
        {
            "command": "pytest -q",
            "exit_code": 1,
            "tests_total": 12,
            "tests_failed": 1,
            "output_summary": "1 failed, 11 passed",
        },
        observed_at=101.0,
    )

    assert passed.status == "passed"
    assert validate_evidence_record(passed) == ()
    assert failed.status == "failed"
    assert validate_evidence_record(failed) == ()


def test_api_evidence_accepts_a_contract_expected_400_response():
    record = create_evidence(
        EvidenceKind.API,
        "run-api-400",
        "metis.runtime_acceptance",
        {
            "endpoint": "/api/todos",
            "status_code": 400,
            "expected_statuses": [400],
            "assertions": [{"name": "expected status 400", "passed": True}],
        },
    )

    assert record.status == "passed"
    assert validate_evidence_record(record) == ()


def test_agent_self_report_and_file_existence_cannot_open_gate():
    self_report = create_evidence(
        "command",
        "run-1",
        "agent.self_report",
        {"command": "pytest", "exit_code": 0, "output_summary": "all good"},
    )

    result = evaluate_evidence_gate([self_report], ["command"], run_id="run-1")

    assert result.passed is False
    assert result.missing_kinds == ("command",)
    assert any("untrusted evidence producer" in reason for reason in result.reasons)
    with pytest.raises(ValueError, match="unsupported evidence kind"):
        create_evidence(
            "file_exists",
            "run-1",
            "metis.runner",
            {"path": "dist/app.js", "exists": True},
        )


def test_serialized_success_cannot_contradict_observed_exit_code():
    failed = create_evidence(
        "build",
        "run-1",
        "metis.runner",
        {"command": "npm run build", "exit_code": 2, "log_ref": "logs/build.log"},
    ).to_dict()
    failed["status"] = "passed"

    errors = validate_evidence_record(failed)

    assert "evidence status contradicts observed values" in errors
    assert evaluate_evidence_gate([failed], ["build"], run_id="run-1").passed is False


def test_gate_requires_every_kind_from_the_current_run():
    command = create_evidence(
        "command",
        "run-1",
        "metis.runner",
        {"command": "python -m compileall .", "exit_code": 0, "output_summary": "ok"},
    )
    health_from_old_run = create_evidence(
        "service_health",
        "run-old",
        "metis.runtime_acceptance",
        {"endpoint": "https://app.example.test/health", "status_code": 200, "healthy": True},
    )

    result = evaluate_evidence_gate(
        [command, health_from_old_run],
        ["command", "service_health"],
        run_id="run-1",
    )

    assert result.passed is False
    assert result.missing_kinds == ("service_health",)
    assert any("another run" in reason for reason in result.reasons)


def test_evidence_payload_is_redacted_before_it_can_be_persisted():
    record = create_evidence(
        "command",
        "run-1",
        "metis.runner",
        {
            "command": "curl https://api.example.test/health",
            "exit_code": 0,
            "output_summary": "Authorization: Bearer abcdefghijklmnop",
            "api_key": "sk-abcdefghijklmnopqrstuvwxyz",
        },
    )

    assert record.status == "passed"
    assert record.payload["api_key"] == REDACTED
    assert "abcdefghijklmnop" not in repr(record.to_dict())


def test_artifact_validation_allows_runner_deterministic_checks():
    record = create_evidence(
        "artifact_validation",
        "run-1",
        "metis.runner",
        {
            "validator": "metis.schema_validator.v1",
            "checks": [
                {"name": "JSON schema contract", "passed": True},
                {"name": "Python syntax parse", "passed": True},
            ],
        },
    )

    assert record.status == "passed"
    assert validate_evidence_record(record) == ()
    assert evaluate_evidence_gate(
        [record], ["artifact_validation"], run_id="run-1"
    ).passed is True


@pytest.mark.parametrize(
    ("producer", "checks", "expected_error"),
    [
        (
            "agent.self_report",
            [{"name": "schema contract", "passed": True}],
            "untrusted evidence producer",
        ),
        (
            "metis.ci",
            [{"name": "schema contract", "passed": True}],
            "artifact validation must be produced by metis.runner",
        ),
        (
            "metis.runner",
            [{"name": "file exists", "passed": True}],
            "existence-only checks",
        ),
    ],
)
def test_artifact_validation_rejects_self_report_non_runner_and_existence_only(
    producer, checks, expected_error
):
    record = create_evidence(
        "artifact_validation",
        "run-1",
        producer,
        {"validator": "metis.validator.v1", "checks": checks},
    )

    errors = validate_evidence_record(record)

    assert any(expected_error in error for error in errors)
    assert evaluate_evidence_gate(
        [record], ["artifact_validation"], run_id="run-1"
    ).passed is False


def test_redact_text_masks_database_urls_without_losing_host_diagnostics():
    safe = redact_text("postgresql://user:password@db.example.test:5432/app")

    assert "password" not in safe
    assert "db.example.test:5432/app" in safe
    assert REDACTED in safe
