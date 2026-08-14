FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY frontend_prototype.html ./
COPY Workflow ./Workflow
COPY Rule ./Rule
COPY Skill ./Skill
COPY SubAgents ./SubAgents
COPY MCP ./MCP
COPY data/sample ./data/sample

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[akshare,llm]"

RUN groupadd --gid 10001 agentapp \
    && useradd --uid 10001 --gid agentapp --no-create-home --shell /usr/sbin/nologin agentapp \
    && mkdir -p /app/data \
    && chown -R agentapp:agentapp /app

USER agentapp

EXPOSE 8003

CMD ["python", "-m", "uvicorn", "agent_platform.api.main:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
