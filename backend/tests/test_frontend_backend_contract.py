import re
from pathlib import Path

from fastapi.testclient import TestClient

from main import app


CALL_PATTERN = re.compile(
    r"apiClient\.(get|post|put|patch|delete)\(\s*([`'\"])(.*?)\2",
    re.DOTALL,
)
TEMPLATE_VALUE = re.compile(r"\$\{[^}]+}")


def route_shape(path: str) -> tuple[str, ...]:
    path = path.split("?", 1)[0]
    return tuple(
        "{}" if segment.startswith("{") and segment.endswith("}") else segment
        for segment in path.strip("/").split("/")
    )


def route_matches(frontend: tuple[str, ...], backend: tuple[str, ...]) -> bool:
    return len(frontend) == len(backend) and all(
        backend_segment == "{}" or frontend_segment == backend_segment
        for frontend_segment, backend_segment in zip(frontend, backend)
    )


def test_frontend_api_calls_have_matching_backend_routes() -> None:
    frontend_root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    calls: set[tuple[str, tuple[str, ...], str]] = set()
    for source in frontend_root.rglob("*"):
        if source.suffix not in {".ts", ".tsx"}:
            continue
        content = source.read_text(encoding="utf-8")
        for match in CALL_PATTERN.finditer(content):
            method, raw_path = match.group(1).upper(), match.group(3)
            if not raw_path.startswith("/"):
                continue
            normalized = TEMPLATE_VALUE.sub("{value}", raw_path)
            calls.add((method, route_shape(normalized), str(source.relative_to(frontend_root))))

    backend_routes = [
        (method.upper(), route_shape(path))
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    ]
    missing = sorted(
        f"{method} /{'/'.join(shape)} ({source})"
        for method, shape, source in calls
        if not any(
            method == backend_method and route_matches(shape, backend_shape)
            for backend_method, backend_shape in backend_routes
        )
    )

    assert missing == [], "Frontend calls missing backend routes:\n" + "\n".join(missing)


def test_openapi_documents_global_security_responses() -> None:
    schema = app.openapi()
    protected = schema["paths"]["/agents"]["get"]

    assert protected["security"] == [
        {"CookieAuth": []},
        {"BearerAuth": []},
    ]
    assert {"400", "401", "403", "429"} <= set(protected["responses"])
    assert "401" in schema["paths"]["/auth/login"]["post"]["responses"]


def test_auth_middleware_does_not_mask_unsupported_methods() -> None:
    client = TestClient(app)

    trace = client.request("TRACE", "/agents")
    static = client.patch("/employee-pool/default-team")
    nested = client.post("/experts/%C2%A5%C2%88%23/chat-train", json={"message": ""})
    assert trace.status_code == 405
    assert trace.headers["Allow"] == "GET"
    assert static.status_code == 405
    assert static.headers["Allow"] == "GET"
    assert nested.status_code == 401
