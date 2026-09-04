"""Suite-wide safeguards for developer-owned credential sidecars."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_secret_sidecars(monkeypatch, tmp_path):
    """Keep every test away from credential files in the repository root."""
    import web_app

    monkeypatch.setattr(web_app, "GEMINI_KEY_FILE", tmp_path / ".gemini_key")
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(
        web_app,
        "X_CREDENTIALS_FILE",
        tmp_path / ".x_credentials.json",
    )
