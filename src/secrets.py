"""Out-of-repo credential access — mode-600 files, read at runtime, never logged.

Mirrors the Cloudflare-token convention: secrets live in files outside the repo
and outside any image layer. This module returns *paths* and *parsed values*,
but never echoes a secret into an exception message or a log line.

Credentials:
  * Coinbase CDP key JSON — passed to ``RESTClient(key_file=...)``.
  * SMTP App Password file — ``SMTP_USER=`` / ``SMTP_PASS=`` lines.
  * CryptoPanic API token — a single-line token used as a query param.

Host default paths live under ``~/.config/coinbase/``; in the container they are
bind-mounted read-only at ``/run/secrets/``. Both are auto-detected.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

log = logging.getLogger(__name__)

_HOST_KEY = Path.home() / ".config" / "coinbase" / "grow-my-money.key"
_HOST_SMTP = Path.home() / ".config" / "coinbase" / "grow-my-money.smtp"
_HOST_CRYPTOPANIC = Path.home() / ".config" / "coinbase" / "grow-my-money.cryptopanic"
_CONTAINER_KEY = Path("/run/secrets/coinbase.key")
_CONTAINER_SMTP = Path("/run/secrets/smtp.env")
_CONTAINER_CRYPTOPANIC = Path("/run/secrets/cryptopanic.token")


class SecretError(Exception):
    """Raised when a required credential file is missing/unreadable.

    The message NEVER contains a secret value — only the path and the reason.
    """


def _resolve(configured: str | None, host: Path, container: Path) -> Path:
    if configured:
        return Path(configured).expanduser()
    if container.exists():
        return container
    return host


def _check_mode_600(path: Path) -> None:
    """Warn (do not fail) if the file is more permissive than 0600."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        log.warning(
            "Credential file %s has permissive mode %o; expected 600. "
            "Tighten with: chmod 600 %s",
            path,
            mode,
            path,
        )


def coinbase_key_path(configured: str | None = None) -> str:
    """Return the path to the Coinbase CDP key file, verifying it exists."""
    path = _resolve(configured, _HOST_KEY, _CONTAINER_KEY)
    if not path.exists():
        raise SecretError(
            f"Coinbase key file not found at {path}. Create a trade-only CDP "
            f"key and place it there (mode 600). See README go-live checklist."
        )
    _check_mode_600(path)
    return str(path)


def cryptopanic_token(configured: str | None = None) -> str:
    """Return the CryptoPanic API token value (the file's stripped contents).

    Unlike ``coinbase_key_path`` (which returns a *path* handed to the SDK), the
    CryptoPanic token is used directly as a query param, so this returns the
    token *value*. The value is NEVER logged or placed in an exception message.
    """
    path = _resolve(configured, _HOST_CRYPTOPANIC, _CONTAINER_CRYPTOPANIC)
    if not path.exists():
        raise SecretError(
            f"CryptoPanic token file not found at {path}. Sign up at "
            f"cryptopanic.com, generate an API token, and place it there "
            f"(mode 600)."
        )
    _check_mode_600(path)
    return path.read_text(encoding="utf-8").strip()


def smtp_credentials(configured: str | None = None) -> dict:
    """Parse the SMTP creds file into ``{'user':..., 'pass':...}``.

    Raises ``SecretError`` (without leaking values) if missing or malformed.
    """
    path = _resolve(configured, _HOST_SMTP, _CONTAINER_SMTP)
    if not path.exists():
        raise SecretError(
            f"SMTP credentials file not found at {path}. Place a mode-600 file "
            f"with SMTP_USER= and SMTP_PASS= (Gmail App Password) lines."
        )
    _check_mode_600(path)
    creds: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        creds[k.strip().upper()] = v.strip()
    user = creds.get("SMTP_USER", "")
    password = creds.get("SMTP_PASS", "")
    if not user or not password:
        raise SecretError(
            f"SMTP credentials file {path} is missing SMTP_USER or SMTP_PASS."
        )
    return {"user": user, "pass": password}
