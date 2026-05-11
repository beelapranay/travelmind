# TravelMind container: Python 3.11 + Node 20 + prewarmed MCP servers.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_ENV=production

# Node 20 via NodeSource + curl/ca-certs for the bootstrap.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# Prewarm MCP servers so the first user request doesn't pay a 30s npm download.
RUN npm install -g --omit=dev \
        tavily-mcp \
        @modelcontextprotocol/server-filesystem \
        @modelcontextprotocol/server-google-maps \
 && npm cache clean --force

WORKDIR /app

# Python deps in their own layer so they cache across code edits.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Render injects $PORT. Default to 8000 for local docker run.
ENV PORT=8000
EXPOSE 8000

# exec replaces the shell so uvicorn becomes PID 1 and receives SIGTERM directly.
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
