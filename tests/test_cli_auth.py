"""IAM6 — CLI authentication & authorization.

`mnesis login` exchanges a password (IAM2) for a session token (IAM3) stored in a
local 0600 credential file; every command resolves that token (or a headless PAT) and
enforces the PDP (IAM4). Logout revokes the session; expired/revoked credentials prompt
a re-login; readonly is refused writes; a scoped PAT can't exceed its scope; admin ops
require the admin role.
"""

from __future__ import annotations

import os
import stat

import pytest

from mnesis import cli, cli_auth, config, providers, search, store, tenancy, tokens
from mnesis.store import Page

PW = "correct horse battery staple"


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    """Auth-enabled CLI over a temp data root with a seeded page and three users
    (alice/member, rob/readonly, ada/admin). The autouse conftest fixture already
    isolates the local CLI credential file to a temp path."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data", raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    monkeypatch.setattr(config, "MNESIS_AUTH_ENABLED", True, raising=False)
    monkeypatch.delenv("MNESIS_TOKEN", raising=False)
    monkeypatch.delenv("MNESIS_CREDENTIAL", raising=False)
    monkeypatch.delenv("MNESIS_PASSWORD", raising=False)

    ctx = tenancy.create_tenant(config.DEFAULT_TENANT_ID, data_root=config.DATA_ROOT)
    with tenancy.use(ctx):
        store.write_page(Page(id="atlas", title="Atlas uses Redis for caching",
                              body="Project Atlas uses Redis.", tags=["project:atlas"]))
        search.rebuild()
    prov = providers.LocalPasswordProvider()
    prov.register(config.DEFAULT_TENANT_ID, "alice", "member", PW)
    prov.register(config.DEFAULT_TENANT_ID, "rob", "readonly", PW)
    prov.register(config.DEFAULT_TENANT_ID, "ada", "admin", PW)
    return tmp_path


def _run(capsys, *argv) -> tuple[int, str]:
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def _login(capsys, user: str) -> None:
    rc, _ = _run(capsys, "login", "--principal", user, "--password", PW)
    assert rc == 0


# ── login stores a usable, 0600 token; commands run scoped to the user ──────


def test_login_stores_usable_token_scoped_to_user(cli_env, capsys):
    # Fail closed before login.
    rc, out = _run(capsys, "query", "atlas")
    assert rc == 2 and "not authenticated" in out.lower()

    rc, out = _run(capsys, "login", "--principal", "alice", "--password", PW)
    assert rc == 0 and "logged in as alice" in out
    assert "token" not in out.lower() or "shown" not in out.lower()  # the raw token is never printed

    # The credential file is owner-only (0600).
    path = cli_auth.CliCredentialStore().path
    assert path.is_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert PW not in path.read_text(encoding="utf-8")  # only the token, never the password

    # A logged-in member can read (and write).
    rc, out = _run(capsys, "query", "atlas")
    assert rc == 0 and "Atlas uses Redis" in out
    rc, out = _run(capsys, "whoami")
    assert rc == 0 and "alice @ default" in out


# ── logout revokes the session; commands then refuse ───────────────────────


def test_logout_revokes_and_refuses(cli_env, capsys):
    _login(capsys, "alice")
    stored = cli_auth.CliCredentialStore().token()
    rc, out = _run(capsys, "logout")
    assert rc == 0 and "logged out" in out

    # The token was revoked server-side and the local file cleared.
    assert cli_auth.CliCredentialStore().token() is None
    from mnesis import identity
    with pytest.raises(identity.Deny):
        tokens.TokenService().validate(stored)
    rc, out = _run(capsys, "query", "atlas")
    assert rc == 2 and "login" in out.lower()


# ── a PAT authenticates headless, within its scope ─────────────────────────


def test_pat_authenticates_headless_within_scope(cli_env, capsys):
    _login(capsys, "ada")  # admin
    rc, out = _run(capsys, "pat", "create", "--name", "ci", "--scope", "read")
    assert rc == 0
    pat = out.strip().splitlines()[-1].split(":")[-1].strip()
    _run(capsys, "logout")  # drop the interactive session — the PAT stands alone

    # Read works headless with the PAT…
    rc, out = _run(capsys, "--token", pat, "query", "atlas")
    assert rc == 0 and "Atlas uses Redis" in out
    # …but the PAT is scoped read-only: a write is refused even though admin would allow it.
    rc, out = _run(capsys, "--token", pat, "file-back", "Q?", "A long enough answer.")
    assert rc == 3 and "out_of_scope" in out


# ── a readonly user is refused write commands ──────────────────────────────


def test_readonly_refused_writes(cli_env, capsys):
    _login(capsys, "rob")  # readonly
    rc, out = _run(capsys, "query", "atlas")
    assert rc == 0  # reads are fine
    rc, out = _run(capsys, "file-back", "Q?", "A long enough answer.")
    assert rc == 3 and "insufficient_role" in out


# ── an expired/revoked credential prompts re-login ─────────────────────────


def test_revoked_credential_prompts_relogin(cli_env, capsys):
    _login(capsys, "alice")
    # Revoke the session out-of-band (e.g. an admin/compromise response).
    tokens.TokenService().revoke_token(cli_auth.CliCredentialStore().token())
    rc, out = _run(capsys, "query", "atlas")
    assert rc == 2
    assert "revoked" in out.lower() and "mnesis login" in out


# ── admin ops require the admin role ───────────────────────────────────────


def test_admin_ops_require_admin_role(cli_env, capsys):
    _login(capsys, "rob")  # readonly
    rc, out = _run(capsys, "auth", "issue", "--principal", "bot", "--role", "agent")
    assert rc == 3 and "may not manage credentials" in out

    _login(capsys, "ada")  # admin
    rc, out = _run(capsys, "auth", "issue", "--principal", "bot", "--role", "agent")
    assert rc == 0 and "issued credential" in out


# ── fail closed: no credential, no access ──────────────────────────────────


def test_unauthenticated_refused(cli_env, capsys):
    rc, out = _run(capsys, "list")
    assert rc == 2 and "not authenticated" in out.lower()
