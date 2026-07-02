# mnesis web UI — multi-stage: build the static bundle with node, serve it with
# nginx (which also reverse-proxies /api + SSE to the mnesis service). The image
# is stateless: all knowledge lives in the mnesis service / its volume, so this
# container holds nothing and is safe to rebuild anytime.

# --- stage 1: build the static bundle ---------------------------------------
FROM node:20-alpine AS build
WORKDIR /app
# Install deps from the lockfile first (cached unless the lockfile changes).
COPY package.json package-lock.json ./
RUN npm ci
# Then the source, and build the production bundle into /app/dist.
COPY . .
RUN npm run build

# --- stage 2: nginx static server + API/SSE proxy ---------------------------
FROM nginx:alpine AS runtime
# Server config: SPA fallback + /api proxy with SSE-friendly settings.
COPY nginx.conf /etc/nginx/conf.d/default.conf
# Startup hooks (run before nginx): the auth hook is now a no-op (IAM5 retired the
# injected bearer token — the web UI uses a login + session cookie), and the upload
# hook aligns client_max_body_size with the backend limit.
# Seed sane default includes so the config is valid even before the hooks run.
COPY docker-entrypoint.d/40-mnesis-auth.sh /docker-entrypoint.d/40-mnesis-auth.sh
COPY docker-entrypoint.d/41-mnesis-upload.sh /docker-entrypoint.d/41-mnesis-upload.sh
RUN chmod +x /docker-entrypoint.d/40-mnesis-auth.sh /docker-entrypoint.d/41-mnesis-upload.sh \
    && : > /etc/nginx/auth_header.conf \
    && printf 'client_max_body_size 3145728;\n' > /etc/nginx/client_max_body.conf
# The built assets (config.js ships in the bundle with a same-origin /api base).
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
