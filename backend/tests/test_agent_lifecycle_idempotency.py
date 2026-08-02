from core.agent_lifecycle import transition_agent


def test_replayed_terminal_projection_does_not_move_finish_time_or_add_event():
    agent = {
        "status": "completed",
        "progress": 100,
        "finished_at": 123.0,
        "lifecycle_events": [{
            "status": "completed", "message": "Execution finished", "at": 123.0,
        }],
    }

    transition_agent(
        agent,
        "completed",
        progress=100,
        message="All locked task attempts completed",
    )

    assert agent["finished_at"] == 123.0
    assert agent["lifecycle_events"] == [{
        "status": "completed", "message": "Execution finished", "at": 123.0,
    }]
