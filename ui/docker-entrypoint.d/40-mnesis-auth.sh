#!/bin/sh
# Server-side bearer-token injection for the /api proxy.
#
# On this trusted-host deployment the browser never handles the API token: if
# MNESIS_MCP_TOKEN is set, nginx adds the Authorization header to every proxied
# /api request here, so the token stays on the server. When unset, the include
# is emptied and /api is proxied without auth (matches an unguarded mnesis).
#
# Tradeoff: anyone who can reach this UI port reaches the API with the proxy's
# privileges — the host/network is the trust boundary. Per-user auth is a
# future iteration. Runs (nginx image convention) before nginx starts.
set -e

CONF=/etc/nginx/auth_header.conf

if [ -n "${MNESIS_MCP_TOKEN:-}" ]; then
    printf 'proxy_set_header Authorization "Bearer %s";\n' "$MNESIS_MCP_TOKEN" > "$CONF"
    echo "mnesis-ui: injecting Authorization header on the /api proxy"
else
    : > "$CONF"
    echo "mnesis-ui: MNESIS_MCP_TOKEN unset — proxying /api without an auth header"
fi
