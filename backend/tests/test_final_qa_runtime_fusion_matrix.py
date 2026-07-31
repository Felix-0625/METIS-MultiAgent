"""Large deterministic matrix for the Final QA/runtime boundary."""

from __future__ import annotations

import pytest

from api.routes_adjustments import (
    FINAL_QA_ISSUE_FIELDS,
    _runtime_acceptance_issue,
)


_STAGES = (
    ("install_failed", "runtime.install"),
    ("build_failed", "runtime.build"),
    ("test_failed", "runtime.test"),
    ("startup_failed", "runtime.startup"),
    ("healthcheck_failed", "runtime.http"),
    ("http_failed", "runtime.http"),
)

_CASES = [
    (stage, criterion, index, index % 2 == 0)
    for stage, criterion in _STAGES
    for index in range(200)
]


@pytest.mark.parametrize(
    ("stage", "criterion", "index", "in_scope"),
    _CASES,
    ids=[
        f"{stage}-{index}-{'owned' if in_scope else 'unowned'}"
        for stage, _, index, in_scope in _CASES
    ],
)
def test_runtime_failure_is_compact_scope_bound_final_qa_issue(
    stage: str,
    criterion: str,
    index: int,
    in_scope: bool,
) -> None:
    path = f"src/case_{index}.ts"
    scope_path = path if in_scope else f"src/owned_{index}.ts"
    issue = _runtime_acceptance_issue(
        {
            "status": stage,
            "summary": "x" * 500,
            "file_path": path,
            "fix_hint": "f" * 500,
            "logs": [
                f"build: {path}:{index + 1} error",
                "build: expected: runtime succeeds",
                "build: received: runtime failed " + ("a" * 500),
            ],
        },
        scope_files={scope_path: {"agent_id": f"agent-{index % 7}"}},
        valid_criteria={criterion},
    )

    assert tuple(issue) == FINAL_QA_ISSUE_FIELDS
    assert issue["criterion"] == criterion
    assert issue["file"] == (path if in_scope else None)
    assert issue["line"] == (index + 1 if in_scope else None)
    assert issue["related_files"] == []
    assert len(issue["message"]) <= 160
    assert len(issue["expected"]) <= 120
    assert len(issue["actual"]) <= 240
    assert len(issue["fix"]) <= 200
    assert "package.json" not in {
        issue["file"],
        *(issue["related_files"] or []),
    }
