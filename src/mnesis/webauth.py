"""Web authentication & authorization (IAM5) — real login + cookie sessions + CSRF.

This retires the single injected bearer token the browser used to carry. The web UI
now logs in with a real user (the IAM2 :class:`~mnesis.providers.LocalPasswordProvider`),
receives an **opaque server-side session** (IAM3 :class:`~mnesis.tokens.TokenService`)
delivered as a **secure / httpOnly / SameSite** cookie, and every subsequent request is
authorized by the **single PDP** (IAM4 :mod:`mnesis.authz`) against the principal resolved
**server-side** from that session — never from client-supplied identity.

Pieces:

  - **Endpoints** (mounted under ``/api/auth``): ``login`` (password → session cookie),
    ``logout`` (immediate server-side session revoke), ``session`` (the current
    principal), and the password-reset ``reset/request`` / ``reset/confirm`` flow.
  - **`WebSessionMiddleware`** — the single choke point for ``/api/*``: it resolves the
    session cookie to an :class:`AuthenticatedPrincipal`, binds the tenant + principal
    for the request/SSE stream, and enforces **CSRF** (double-submit) on state-changing
    methods. Unauthenticated → ``401``; a bad CSRF token → ``403``. A few entry points
    (login, reset, config) are exempt so the login page can bootstrap.
  - **Exception handler** — a PDP :class:`~mnesis.authz.AuthorizationError` raised by a
    handler becomes a ``403`` with the deny reason.

Cookies never leave with the raw session in the body; identity is only ever the
server-resolved principal. CSRF uses the standard double-submit pattern (a JS-readable
CSRF cookie that must be echoed in ``X-CSRF-Token``), backed by ``SameSite`` on the
session cookie.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from http.cookies import SimpleCookie

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, authz, config, identity, providers, tenancy, tokens

log = logging.getLogger(__name__)

#: Cookie names. The session cookie is httpOnly (JS can't read it); the CSRF cookie is
#: deliberately readable by JS so the SPA can echo it in the X-CSRF-Token header.
SESSION_COOKIE = "mnesis_session"
CSRF_COOKIE = "mnesis_csrf"
CSRF_HEADER = "x-csrf-token"

#: Paths under /api that do NOT require an existing session (login page bootstrap).
_SESSION_EXEMPT = frozenset({
    "/api/auth/login",
    "/api/auth/reset/request",
    "/api/auth/reset/confirm",
    "/api/config",
})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# --- cookie helpers --------------------------------------------------------


def _set_cookie(resp: JSONResponse, name: str, value: str, *, http_only: bool, max_age: int) -> None:
    resp.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=http_only,
        secure=config.MNESIS_WEB_COOKIE_SECURE,
        samesite=config.MNESIS_WEB_COOKIE_SAMESITE,
        path="/",
    )


def _issue_cookies(resp: JSONResponse, session_token: str) -> str:
    """Attach the session (httpOnly) + CSRF (readable) cookies; return the CSRF token."""
    max_age = config.MNESIS_SESSION_ABSOLUTE_SECONDS or 0
    csrf = secrets.token_urlsafe(32)
    _set_cookie(resp, SESSION_COOKIE, session_token, http_only=True, max_age=max_age)
    _set_cookie(resp, CSRF_COOKIE, csrf, http_only=False, max_age=max_age)
    return csrf


def _clear_cookies(resp: JSONResponse) -> None:
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")


def _cookies_from_scope(scope) -> dict[str, str]:
    header = ""
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            header = v.decode("latin-1")
            break
    if not header:
        return {}
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(header)
    except Exception:  # noqa: BLE001 — a malformed cookie header is simply "no cookies"
        return {}
    return {k: m.value for k, m in jar.items()}


def _header_from_scope(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return ""


# --- endpoints -------------------------------------------------------------


async def _login(request: Request) -> JSONResponse:
    """Authenticate username/password (local provider) → issue a web session cookie.

    The tenant is taken from the login body (default the deployment's default tenant);
    identity is proven server-side and the raw session is delivered **only** as an
    httpOnly cookie (never in the response body)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    tenant_id = (body.get("tenant_id") or config.DEFAULT_TENANT_ID).strip()
    username = (body.get("username") or body.get("principal_id") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return JSONResponse({"error": "username and password are required"}, status_code=400)

    client_ip = request.client.host if request.client else None
    provider = providers.LocalPasswordProvider()
    try:
        principal = provider.authenticate(tenant_id, username, password, client_ip=client_ip)
    except providers.AccountLocked as exc:
        return JSONResponse(
            {"error": "account_locked", "retry_after": int(exc.retry_after)}, status_code=429
        )
    except identity.AuthError:
        # Generic — never distinguish unknown-user from wrong-password (no enumeration).
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)

    raw_session, _rec = tokens.TokenService().issue_session(principal)
    resp = JSONResponse({
        "principal_id": principal.principal_id,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "kind": principal.kind,
    })
    _issue_cookies(resp, raw_session)
    return resp


async def _logout(request: Request) -> JSONResponse:
    """Revoke the session **immediately** (server-side) and clear the cookies."""
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        try:
            tokens.TokenService().logout(raw)
        except Exception:  # noqa: BLE001 — logout is best-effort; always clear cookies
            log.debug("session revoke on logout failed", exc_info=True)
    resp = JSONResponse({"ok": True})
    _clear_cookies(resp)
    return resp


async def _session(request: Request) -> JSONResponse:
    """The current principal (the middleware has already bound it, else we'd be 401)."""
    p = auth.current_principal_or_none()
    if p is None:  # pragma: no cover — middleware guarantees a principal here
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    active = tenancy.current_or_none()
    return JSONResponse({
        "principal_id": p.principal_id,
        "tenant_id": p.tenant_id,
        "roles": sorted(p.roles),
        "scopes": sorted(p.scopes),
        "kind": p.kind,
        "permissions": sorted(authz.effective_permissions(p)),
        # The active vault (re-authorized by the middleware for this request) + the vaults
        # the principal may select (V5). The SPA's vault picker reads these.
        "active_vault": getattr(active, "vault_id", config.DEFAULT_VAULT_ID),
        "vaults": sorted(authz.accessible_vaults(p)),
    })


async def _vaults(request: Request) -> JSONResponse:
    """The vaults the bound principal may select (owned ∪ granted ∪ the transparent
    ``default``), plus the currently-active vault. Backs the web vault picker (V5)."""
    p = auth.current_principal_or_none()
    if p is None:  # pragma: no cover — middleware guarantees a principal here
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    active = tenancy.current_or_none()
    return JSONResponse({
        "active_vault": getattr(active, "vault_id", config.DEFAULT_VAULT_ID),
        "vaults": sorted(authz.accessible_vaults(p)),
    })


async def _reset_request(request: Request) -> JSONResponse:
    """Begin a password reset. Always returns ``202`` (never reveals whether the account
    exists); the single-use token is minted + audited server-side for out-of-band
    delivery by the operator."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    tenant_id = (body.get("tenant_id") or config.DEFAULT_TENANT_ID).strip()
    username = (body.get("username") or "").strip()
    if username:
        try:
            providers.LocalPasswordProvider().begin_reset(tenant_id, username)
        except Exception:  # noqa: BLE001 — never leak provider state to the caller
            log.debug("reset request failed", exc_info=True)
    return JSONResponse({"status": "accepted"}, status_code=202)


async def _reset_confirm(request: Request) -> JSONResponse:
    """Complete a password reset with a valid single-use token + a new password."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    tenant_id = (body.get("tenant_id") or config.DEFAULT_TENANT_ID).strip()
    username = (body.get("username") or "").strip()
    token = body.get("token") or ""
    new_password = body.get("new_password") or ""
    if not (username and token and new_password):
        return JSONResponse({"error": "username, token, and new_password are required"}, status_code=400)
    provider = providers.LocalPasswordProvider()
    try:
        provider.reset_password(tenant_id, username, token, new_password)
    except providers.PasswordPolicyError as exc:
        return JSONResponse({"error": "weak_password", "message": str(exc)}, status_code=400)
    except identity.AuthError:
        return JSONResponse({"error": "invalid_reset"}, status_code=400)
    # Revoke every existing session/token for the principal — a reset ends old sessions.
    tokens.TokenService().revoke_all_for_principal(tenant_id, username)
    return JSONResponse({"status": "reset"})


AUTH_ROUTES = [
    Route("/api/auth/login", _login, methods=["POST"]),
    Route("/api/auth/logout", _logout, methods=["POST"]),
    Route("/api/auth/session", _session, methods=["GET"]),
    Route("/api/vaults", _vaults, methods=["GET"]),
    Route("/api/auth/reset/request", _reset_request, methods=["POST"]),
    Route("/api/auth/reset/confirm", _reset_confirm, methods=["POST"]),
]


# --- the /api choke point --------------------------------------------------


async def _send_json(scope, receive, send, status: int, payload: dict) -> None:
    await JSONResponse(payload, status_code=status)(scope, receive, send)


class WebSessionMiddleware:
    """The single server-side choke point for ``/api/*`` (IAM5).

    Resolves the session cookie to an :class:`AuthenticatedPrincipal`, binds the tenant
    + principal for the request (so handlers + the PDP + the data-layer visibility
    filters all see the real principal), and enforces **CSRF** on state-changing
    methods. Non-``/api`` paths pass straight through (the MCP/agent bearer path owns
    them). Fail closed: no/invalid session → ``401``; bad CSRF → ``403``."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith("/api"):
            await self.app(scope, receive, send)  # not the web surface — pass through
            return
        if path in _SESSION_EXEMPT:
            await self.app(scope, receive, send)  # login/reset/config bootstrap
            return

        cookies = _cookies_from_scope(scope)
        raw = cookies.get(SESSION_COOKIE)
        try:
            principal_auth = tokens.TokenService().validate(raw)
        except identity.AuthError:
            await _send_json(scope, receive, send, 401, {"error": "unauthenticated"})
            return

        # CSRF (double-submit) on state-changing methods.
        if scope.get("method", "GET") not in _SAFE_METHODS:
            header_tok = _header_from_scope(scope, CSRF_HEADER.encode())
            cookie_tok = cookies.get(CSRF_COOKIE, "")
            if not cookie_tok or not header_tok or not hmac.compare_digest(header_tok, cookie_tok):
                await _send_json(scope, receive, send, 403, {"error": "csrf_failed"})
                return

        principal = principal_auth.to_principal()
        # Vault SELECTION from the client (header) is re-AUTHORIZED server-side against the
        # principal's grants before any store is opened (V5). An ungranted/unknown vault
        # fails closed → 403; the tenant still comes only from the credential.
        selected_vault = _header_from_scope(scope, config.VAULT_SELECTION_HEADER.encode()) or None
        try:
            ctx = authz.open_authorized_vault(principal, selected_vault)
        except identity.AuthError as exc:
            reason = getattr(exc, "reason", "vault_forbidden")
            await _send_json(scope, receive, send, 403, {"error": "vault_forbidden", "reason": reason})
            return
        with tenancy.use(ctx):
            ptok = auth.bind_principal(principal)
            try:
                await self.app(scope, receive, send)
            finally:
                auth.unbind_principal(ptok)


# --- exception handler: PDP denial -> 403 ----------------------------------


async def authz_error_handler(request: Request, exc: Exception) -> JSONResponse:
    reason = getattr(exc, "reason", "forbidden")
    return JSONResponse({"error": "forbidden", "reason": reason}, status_code=403)


# --- wiring ----------------------------------------------------------------


def mount_auth(app) -> None:
    """Append the /api/auth routes to an existing Starlette app."""
    app.router.routes.extend(AUTH_ROUTES)


def install(app) -> None:
    """Wire the full web-auth surface onto ``app``: the auth routes, the PDP-denial
    exception handler, and the ``/api`` session/CSRF choke point. Call **after**
    ``webapi.mount_api`` and before serving (used by ``build_http_app`` and tests)."""
    mount_auth(app)
    app.add_exception_handler(authz.AuthorizationError, authz_error_handler)
    app.add_middleware(WebSessionMiddleware)
