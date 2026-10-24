from backend import main


def test_production_cors_uses_only_configured_and_host_origins(monkeypatch):
    monkeypatch.setattr(main.settings, "APP_ENV", "production")
    monkeypatch.setattr(main.settings, "CORS_ORIGINS", "https://ui.example, https://admin.example/")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)

    assert main._configured_cors_origins() == [
        "https://ui.example",
        "https://admin.example",
    ]


def test_production_cors_includes_render_origin_without_wildcard(monkeypatch):
    monkeypatch.setattr(main.settings, "APP_ENV", "production")
    monkeypatch.setattr(main.settings, "CORS_ORIGINS", None)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://atlas-demo.onrender.com/")

    assert main._configured_cors_origins() == ["https://atlas-demo.onrender.com"]
