# mnesis — multi-stage image. The canonical store is a git repo, so git is a
# runtime dependency and the wiki root lives on a mountable volume (/data/mnesis).

# --- builder: install the package + deps into an isolated venv (wheels) ------
FROM python:3.12-slim AS builder

WORKDIR /app
# Only what the build backend needs (src layout + readme referenced by pyproject).
COPY pyproject.toml README.md ./
COPY src ./src

# The default SQLite graph backend needs no system build libraries; deps are
# pure-python / wheels, so no compiler toolchain is required here.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir .

# --- runtime: slim image with git, non-root, volume -------------------------
FROM python:3.12-slim AS runtime

# git is required (every page write is a commit); ca-certificates for HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MNESIS_ROOT=/data/mnesis

# Non-root user owning the data volume.
RUN useradd --create-home --uid 10001 mnesis \
    && mkdir -p /data/mnesis \
    && chown -R mnesis:mnesis /data/mnesis

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/maintenance.sh /usr/local/bin/maintenance.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/maintenance.sh

USER mnesis
VOLUME /data/mnesis

# Secrets (ANTHROPIC_API_KEY, MCP token) are NEVER baked in — supply at runtime.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
