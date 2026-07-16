FROM python:3.13-slim

WORKDIR /app

# Only the runtime deps the server imports. Versions match pyproject.toml.
RUN pip install --no-cache-dir "mcp>=1.28.0" "psycopg[binary]>=3.3.4"

COPY ipeds_mcp.py ./

# Overridable at runtime via -e DATABASE_URL / --env-file / ECS task definition.
ENV DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ipeds_db"

# Run as a non-root user.
RUN useradd --create-home ipedsuser
USER ipedsuser

# Streamable-HTTP transport listens here; MCP endpoint is at /mcp.
EXPOSE 8000

CMD ["python", "ipeds_mcp.py"]
