"""Acceptance #5: the CryptoPanic token is read via the mode-600 file convention
(returns the stripped token value; missing -> SecretError; permissive -> warns)."""
from __future__ import annotations

import logging

import pytest

from src.secrets import SecretError, cryptopanic_token


def test_returns_stripped_token(tmp_path):
    f = tmp_path / "cp.token"
    f.write_text("  my-secret-token\n", encoding="utf-8")
    f.chmod(0o600)
    assert cryptopanic_token(configured=str(f)) == "my-secret-token"


def test_missing_file_raises_secret_error(tmp_path):
    missing = tmp_path / "nope.token"
    with pytest.raises(SecretError) as exc:
        cryptopanic_token(configured=str(missing))
    assert str(missing) in str(exc.value)
    # never leaks a token value (there is none), only the path/reason
    assert "my-secret-token" not in str(exc.value)


def test_permissive_mode_warns_but_returns_token(tmp_path, caplog):
    f = tmp_path / "cp.token"
    f.write_text("tok", encoding="utf-8")
    f.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        token = cryptopanic_token(configured=str(f))
    assert token == "tok"
    assert any("permissive mode" in r.message for r in caplog.records)


def test_default_host_path_used_when_unconfigured(tmp_path, monkeypatch):
    import src.secrets as secrets_mod

    host = tmp_path / "grow-my-money.cryptopanic"
    host.write_text("host-token", encoding="utf-8")
    host.chmod(0o600)
    monkeypatch.setattr(secrets_mod, "_HOST_CRYPTOPANIC", host)
    monkeypatch.setattr(secrets_mod, "_CONTAINER_CRYPTOPANIC", tmp_path / "absent")
    assert cryptopanic_token() == "host-token"
