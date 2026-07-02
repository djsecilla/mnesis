#!/bin/sh
# IAM5: the server-side bearer-token injection is RETIRED.
#
# The browser no longer carries an API token. It authenticates with a real login
# (POST /api/auth/login) that sets an httpOnly session cookie, and nginx simply
# forwards the request (Cookie / Set-Cookie pass through the proxy). Per-user auth
# and the policy decision point are enforced by the mnesis backend on every /api
# request and SSE stream.
#
# This script is kept as a no-op so any older nginx.conf that still `include`d the
# generated auth header file finds an empty one instead of failing to start.
set -e

CONF=/etc/nginx/auth_header.conf
: > "$CONF"
if [ -n "${MNESIS_MCP_TOKEN:-}" ]; then
    echo "mnesis-ui: MNESIS_MCP_TOKEN is set but NO LONGER injected into /api (IAM5);" \
         "the web UI authenticates with a login + session cookie. The token now only" \
         "guards the /mcp agent surface on the backend."
fi
