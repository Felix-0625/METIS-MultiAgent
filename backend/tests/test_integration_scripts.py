import runpy
import uuid
from pathlib import Path

import pytest
import requests


LEGACY_SCRIPT_TESTS = [
    "test_viz_project_flow.py",
]

pytestmark = pytest.mark.live_integration

_shared_session: requests.Session | None = None


def _authenticated_session() -> requests.Session:
    """Create the same cookie-backed session a real browser uses."""
    global _shared_session
    # Keep the hostname identical to the legacy scripts. Host-only auth cookies
    # issued for 127.0.0.1 are correctly rejected for localhost.
    base_url = "http://localhost:8000"
    if _shared_session is not None:
        check = _shared_session.get(f"{base_url}/auth/me", timeout=10)
        if check.status_code == 200:
            return _shared_session
    username = f"integration_{uuid.uuid4().hex[:12]}"
    password = "MetisIntegration!2026"
    session = requests.Session()

    health = session.get(f"{base_url}/health", timeout=10)
    health.raise_for_status()
    register = session.post(
        f"{base_url}/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.test",
            "password": password,
        },
        timeout=15,
    )
    register.raise_for_status()
    login = session.post(
        f"{base_url}/auth/login",
        json={"login": username, "password": password},
        timeout=15,
    )
    login.raise_for_status()
    assert session.cookies, "login succeeded without the browser auth cookie"
    _shared_session = session
    return session


@pytest.mark.parametrize("script_name", LEGACY_SCRIPT_TESTS)
def test_legacy_integration_script(script_name, monkeypatch):
    session = _authenticated_session()

    # The legacy scripts import the shared requests module. Route those calls
    # through the authenticated browser-like session and enforce a timeout so
    # a stopped backend produces a useful failure instead of hanging pytest.
    for method in ("get", "post", "put", "patch", "delete"):
        session_method = getattr(session, method)

        def call(*args, _session_method=session_method, **kwargs):
            kwargs.setdefault("timeout", 30)
            return _session_method(*args, **kwargs)

        monkeypatch.setattr(requests, method, call)

    script_path = Path(__file__).with_name(script_name)
    # The scripts intentionally inspect data/dagent.db. Run them from the
    # backend directory, which is also the documented service working dir.
    monkeypatch.chdir(script_path.parent.parent)
    try:
        runpy.run_path(str(script_path), run_name=f"__metis_{script_name}")
    except SystemExit as exc:
        assert exc.code in (0, None), f"{script_name} exited with {exc.code}"
