# Distributed central control plane only. Workspaces run inside Exegol.

FROM golang:1.27-bookworm AS go-toolchain

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RMM_STORAGE_MODE=workspace \
    PATH="/usr/local/go/bin:${PATH}"

WORKDIR /app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-mcp.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=go-toolchain /usr/local/go /usr/local/go
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod 0755 /app/docker-entrypoint.sh

EXPOSE 8080 8081

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "rmm_deployment.py", "run", "--root", "/deployment", "--component", "control"]
