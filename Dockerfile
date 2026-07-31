FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY . /app

WORKDIR /app
RUN uv sync --locked

# Run as a non-root user.
RUN useradd --create-home ipedsuser
USER ipedsuser

# Streamable-HTTP transport listens here; MCP endpoint is at /mcp.
EXPOSE 8000

CMD ["uv", "run", "python", "server.py"]
