#!/bin/sh
# Align nginx's client_max_body_size with the backend upload limit.
#
# The browser uploads sources as multipart/form-data, which is larger than the
# raw file (boundaries + headers). We set nginx's limit to MNESIS_MAX_UPLOAD_BYTES
# plus 1 MiB of headroom so the multipart envelope never trips nginx's own 413 —
# the backend stays the authority on the exact byte limit (and returns the
# friendly {code,message} error). Runs before nginx starts (nginx image convention).
set -e

CONF=/etc/nginx/client_max_body.conf
LIMIT="${MNESIS_MAX_UPLOAD_BYTES:-2000000}"

# Guard against a non-numeric override; fall back to the default.
case "$LIMIT" in
    ''|*[!0-9]*) LIMIT=2000000 ;;
esac

NGINX_LIMIT=$((LIMIT + 1048576))   # + 1 MiB headroom for the multipart envelope
printf 'client_max_body_size %s;\n' "$NGINX_LIMIT" > "$CONF"
echo "mnesis-ui: client_max_body_size set to ${NGINX_LIMIT} bytes (backend limit ${LIMIT})"
