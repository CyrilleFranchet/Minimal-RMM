# Central control plane and the optional workspace sidecar share one image.
# The control plane has no network namespace privileges; only a workspace
# container that owns a local TUN device needs NET_ADMIN and /dev/net/tun.

FROM golang:1.27-bookworm AS helper-builder

WORKDIR /src
COPY server-tun ./server-tun

RUN mkdir -p /out/tools \
    && cd server-tun \
    && go build -trimpath -ldflags "-s -w" -o /out/tools/rmm-tun .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RMM_STORAGE_MODE=workspace \
    RMM_PLUGINS_DIR=/app/plugins \
    RMM_PROFILES_DIR=/app/profiles \
    RMM_WORKSPACE_TUN_HELPER=/app/tools/rmm-tun

WORKDIR /app
COPY requirements.txt requirements-mcp.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=helper-builder /out/tools/rmm-tun /app/tools/rmm-tun
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod 0755 /app/tools/rmm-tun /app/docker-entrypoint.sh

EXPOSE 8080 8081 8787

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "server_rmm.py", "--admin-port", "8080", "--beacon-port", "8081", "--bind", "0.0.0.0", "--beacon-bind", "0.0.0.0"]
