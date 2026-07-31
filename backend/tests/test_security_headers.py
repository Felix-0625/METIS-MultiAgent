from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _block(text: str, marker: str, next_marker: str) -> str:
    start = text.index(marker)
    end = text.index(next_marker, start)
    return text[start:end]


def test_render_nginx_sets_modern_security_headers() -> None:
    nginx = (ROOT / "nginx-render.conf").read_text(encoding="utf-8")

    required = (
        "Strict-Transport-Security",
        "Content-Security-Policy",
        "Permissions-Policy",
        "Cross-Origin-Opener-Policy",
        "Cross-Origin-Resource-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
    )
    for header in required:
        assert f"add_header {header} " in nginx

    assert "object-src 'none'" in nginx
    assert "base-uri 'self'" in nginx
    assert "frame-ancestors 'self'" in nginx
    assert "'unsafe-eval'" not in nginx


def test_app_location_does_not_drop_inherited_security_headers() -> None:
    nginx = (ROOT / "nginx-render.conf").read_text(encoding="utf-8")
    app = _block(nginx, "location /app/ {", "location ~* ^/app/assets/")

    # nginx does not inherit parent add_header directives when a location has
    # any add_header of its own (the app sets Cache-Control), so these must be
    # repeated in the SPA location.
    for header in (
        "Strict-Transport-Security",
        "Content-Security-Policy",
        "Permissions-Policy",
        "Cross-Origin-Opener-Policy",
        "Cross-Origin-Resource-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
    ):
        assert f"add_header {header} " in app

