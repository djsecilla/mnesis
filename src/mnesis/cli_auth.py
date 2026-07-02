"""CLI authentication — local secure credential storage + the shared resolver (IAM6).

`mnesis login` exchanges a username/password (IAM2 :class:`LocalPasswordProvider`) for a
web-style session token (IAM3 :class:`~mnesis.tokens.TokenService`) and stores the raw
token in a **local secure credential file** (owner-only ``0600``). Subsequent commands
read that token — or a PAT supplied via ``--token`` / ``MNESIS_TOKEN`` for headless
automation — and resolve it through the **same** validator + PDP the other surfaces use.

Storage:
  - Default path ``~/.config/mnesis/credentials.json`` (XDG-ish), overridable with
    ``MNESIS_CLI_CREDENTIALS`` (or a ``MNESIS_CONFIG_HOME`` base). The file is written
    ``0600`` in a ``0700`` directory; **only the raw token is a secret** and it is never
    logged (the store never prints it). Logout revokes the token server-side and clears
    the file.

Resolution (:func:`resolve_token`) accepts either credential kind so nothing regresses:
an **IAM3 token/PAT** (validated by the token service) or a **legacy IAM1 credential**
(``mnesis auth issue``). It fails closed and surfaces the token service's deny reason
(``expired`` / ``revoked`` / ``unknown``) so the CLI can prompt a re-login.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config, tokens

#: Env override for the local credential file (tests point this at a temp path).
CLI_CREDENTIALS_ENV = "MNESIS_CLI_CREDENTIALS"
#: Optional base dir for the default location (else ``~/.config``).
CONFIG_HOME_ENV = "MNESIS_CONFIG_HOME"


def default_path() -> Path:
    """Where the CLI stores its session token, honouring the env overrides."""
    override = os.environ.get(CLI_CREDENTIALS_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get(CONFIG_HOME_ENV)
    root = Path(base).expanduser() if base else Path.home() / ".config" / "mnesis"
    return root / "credentials.json"


class CliCredentialStore:
    """The owner-only local credential file. Holds the raw session token plus
    non-secret display metadata; the token is never logged."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()

    def save(self, token: str, *, tenant_id: str, principal_id: str, roles) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass  # best-effort on platforms without POSIX perms
        payload = {
            "token": token,
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "roles": sorted(roles),
            "created": config.now_iso(),
        }
        # Create the file 0600 *before* writing the secret (no world-readable window).
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def load(self) -> dict | None:
        if not self.path.is_file():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "null")
        except (ValueError, OSError):
            return None

    def token(self) -> str | None:
        data = self.load()
        return data.get("token") if data else None

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False


def resolve_token(raw: str, *, data_root: Path | str | None = None):
    """Resolve an opaque ``raw`` credential to ``(TenantContext, Principal)`` — the
    **same** shared resolver the MCP surface uses (:func:`mnesis.tokens.resolve_bearer`):
    an IAM3 token/PAT first, then a legacy IAM1 credential. Fail-closed."""
    return tokens.resolve_bearer(raw, data_root=data_root)
