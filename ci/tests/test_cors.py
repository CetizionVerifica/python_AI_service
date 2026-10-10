"""CORS origins come from CORS_ALLOWED_ORIGINS (F-20)."""


def test_origin_list_parsing():
    from app.core.config import Settings

    s = Settings(CORS_ALLOWED_ORIGINS=" https://app.example.com/, https://staging.example.com ,,")
    assert s.cors_allowed_origins == ["https://app.example.com", "https://staging.example.com"]
    default = Settings.model_fields["CORS_ALLOWED_ORIGINS"].default
    assert Settings(CORS_ALLOWED_ORIGINS=default).cors_allowed_origins == ["http://localhost:5173", "http://localhost:3000"]


def test_app_allows_only_configured_origins():
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    assert "http://localhost:5173" in settings.cors_allowed_origins  # CI runs with the default

    client = TestClient(app)
    ok = client.options("/health", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.options("/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers
