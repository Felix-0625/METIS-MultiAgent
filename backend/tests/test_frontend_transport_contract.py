from pathlib import Path


FRONTEND = Path(__file__).parents[2] / "frontend" / "src" / "pages"


def _source(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_supervisor_chat_uses_phase_scoped_route() -> None:
    source = _source("SupervisorLeaderPage.tsx")
    assert "/phases/${selectedPhaseId}/supervisor-chat" in source
    assert "/phases/supervisor-chat" not in source
    assert "if (!selectedPhaseId)" in source


def test_cross_origin_fetches_include_cookie_credentials() -> None:
    for name in ("ExpertPool.tsx", "GiteePage.tsx", "SkillPool.tsx"):
        source = _source(name)
        assert "credentials: 'include'" in source or 'credentials: "include"' in source
        assert source.count("fetch(") == 1, name

    project_list = _source("ProjectList.tsx")
    assert "fetch(url, { credentials: 'include' })" in project_list
