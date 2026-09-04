"""Tests for atomic local secret storage and DPAPI envelopes."""

import base64
import os
import stat

import pytest

import secret_store


def _install_fake_dpapi(monkeypatch) -> None:
    def protect(data: bytes) -> bytes:
        return b"fake-dpapi:" + bytes(value ^ 0xA5 for value in data)

    def unprotect(data: bytes) -> bytes:
        prefix = b"fake-dpapi:"
        if not data.startswith(prefix):
            raise ValueError("invalid protected data")
        return bytes(value ^ 0xA5 for value in data[len(prefix):])

    monkeypatch.setattr(secret_store, "is_encryption_available", lambda: True)
    monkeypatch.setattr(secret_store, "_protect_data", protect)
    monkeypatch.setattr(secret_store, "_unprotect_data", unprotect)


def test_encrypted_secret_round_trip(monkeypatch, tmp_path):
    _install_fake_dpapi(monkeypatch)
    secret_path = tmp_path / ".secret"

    secret_store.write_secret_text(secret_path, "round-trip-value")

    assert secret_store.read_secret_text(secret_path) == "round-trip-value"
    if os.name == "posix":
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600


def test_legacy_plaintext_read_migrates_to_envelope(monkeypatch, tmp_path):
    _install_fake_dpapi(monkeypatch)
    secret_path = tmp_path / ".secret"
    secret_path.write_text("legacy-value", encoding="utf-8")

    assert secret_store.read_secret_text(secret_path) == "legacy-value"
    assert secret_path.read_text(encoding="utf-8").startswith("CLIPSEC1:")
    assert secret_store.read_secret_text(secret_path) == "legacy-value"


def test_legacy_read_succeeds_when_migration_fails(monkeypatch, tmp_path, caplog):
    secret_path = tmp_path / ".secret"
    secret_path.write_text("legacy-value", encoding="utf-8")
    monkeypatch.setattr(secret_store, "is_encryption_available", lambda: True)

    def fail_to_encrypt(_data: bytes) -> bytes:
        raise OSError("migration failure must stay private")

    monkeypatch.setattr(secret_store, "_protect_data", fail_to_encrypt)

    with caplog.at_level("WARNING", logger=secret_store.logger.name):
        assert secret_store.read_secret_text(secret_path) == "legacy-value"

    assert secret_path.read_text(encoding="utf-8") == "legacy-value"
    assert str(secret_path) in caplog.text
    assert "legacy plaintext migration" in caplog.text
    assert "legacy-value" not in caplog.text
    assert "migration failure must stay private" not in caplog.text


def test_failed_envelope_decryption_is_unavailable_and_preserves_file(
    monkeypatch,
    tmp_path,
    caplog,
):
    secret_path = tmp_path / ".secret"
    encoded = base64.b64encode(b"corrupt-dpapi-blob").decode("ascii")
    secret_path.write_text(f"CLIPSEC1:{encoded}", encoding="utf-8")
    original_payload = secret_path.read_bytes()
    monkeypatch.setattr(secret_store, "is_encryption_available", lambda: True)

    def fail_to_decrypt(_data: bytes) -> bytes:
        raise OSError("underlying failure must stay private")

    monkeypatch.setattr(secret_store, "_unprotect_data", fail_to_decrypt)

    with caplog.at_level("WARNING", logger=secret_store.logger.name):
        with pytest.raises(secret_store.SecretUnavailableError):
            secret_store.read_secret_text(secret_path)

    assert secret_path.read_bytes() == original_payload
    assert str(secret_path) in caplog.text
    assert "decrypt" in caplog.text.lower()
    assert "underlying failure" not in caplog.text


def test_missing_secret_returns_empty(tmp_path):
    assert secret_store.read_secret_text(tmp_path / ".missing") == ""


def test_non_windows_fallback_writes_readable_plaintext(monkeypatch, tmp_path):
    secret_path = tmp_path / ".secret"
    monkeypatch.setattr(secret_store, "is_encryption_available", lambda: False)
    monkeypatch.setattr(
        secret_store,
        "_protect_data",
        lambda _data: pytest.fail("DPAPI must not be used in fallback mode"),
    )

    secret_store.write_secret_text(secret_path, "fallback-value")

    assert secret_path.read_text(encoding="utf-8") == "fallback-value"
    assert secret_store.read_secret_text(secret_path) == "fallback-value"


def test_failed_atomic_replace_preserves_file_and_cleans_temp(
    monkeypatch,
    tmp_path,
    caplog,
):
    secret_path = tmp_path / ".secret"
    secret_path.write_text("original-value", encoding="utf-8")
    monkeypatch.setattr(secret_store, "is_encryption_available", lambda: False)

    def fail_to_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(secret_store.os, "replace", fail_to_replace)

    with caplog.at_level("WARNING", logger=secret_store.logger.name):
        with pytest.raises(RuntimeError) as error:
            secret_store.write_secret_text(secret_path, "replacement-value")

    assert str(error.value) == "Secret storage operation failed"
    assert "injected replace failure" not in str(error.value)
    assert secret_path.read_text(encoding="utf-8") == "original-value"
    assert list(tmp_path.glob(f"{secret_path.name}.*.tmp")) == []
    assert str(secret_path) in caplog.text
    assert "write" in caplog.text
    assert "replacement-value" not in caplog.text
    assert "injected replace failure" not in caplog.text


def test_encrypted_file_does_not_contain_plaintext(monkeypatch, tmp_path):
    _install_fake_dpapi(monkeypatch)
    secret_path = tmp_path / ".secret"
    plaintext = "plaintext-must-not-appear"

    secret_store.write_secret_text(secret_path, plaintext)

    raw_bytes = secret_path.read_bytes()
    assert raw_bytes.startswith(b"CLIPSEC1:")
    assert plaintext.encode("utf-8") not in raw_bytes
