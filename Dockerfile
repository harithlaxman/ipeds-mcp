FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency manifests first: editing the source doesn't invalidate this layer.
# The project itself is virtual (never installed into the venv), so this single
# sync is all the environment needs.
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked

COPY . /app

# Run as a non-root user; it owns /app so uv never has to write a root-owned venv.
RUN useradd --create-home ipedsuser && chown -R ipedsuser /app
USER ipedsuser

# Streamable-HTTP transport listens here; MCP endpoint is at /mcp.
EXPOSE 8000

# --no-sync: the environment is already built above, so start-up never re-syncs.
CMD ["uv", "run", "--no-sync", "python", "server.py"]
