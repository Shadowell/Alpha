# Alpha — A-share quant screening (API / data runtime with Kronos CPU inference)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/alpha \
    HF_HOME=/root/.cache/huggingface \
    APP_USER=alpha \
    APP_UID=10001 \
    APP_GID=10001

WORKDIR /app

# gosu lets the root entrypoint repair volume ownership and then drop
# privileges to the non-root runtime user (uid/gid 10001).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. The application process is always launched as this
# user by docker-entrypoint.sh; root is only used to fix mounted volumes.
RUN groupadd --gid "$APP_GID" "$APP_USER" \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home \
        --shell /usr/sbin/nologin "$APP_USER"

# CPU-only torch first so the kronos requirement is already satisfied
# (kronos_predict_service defaults to device="cpu"; avoids the multi-GB CUDA image).
COPY requirements-kronos.txt ./requirements-kronos.txt
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# API / data runtime + Kronos + MCP. Training backend (lightgbm) stays optional.
COPY requirements-base.txt ./requirements-base.txt
COPY requirements-mcp.txt ./requirements-mcp.txt
RUN pip install -r requirements-base.txt -r requirements-kronos.txt -r requirements-mcp.txt

COPY app/ ./app/
COPY strategy/ ./strategy/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Writable directories for the non-root user. /app/data and /app/logs are
# normally overridden by compose volumes; the entrypoint re-chowns those at
# startup. /root/.cache/huggingface stays the HF_HOME path so the existing
# compose named volume keeps working; /root only needs traversal (o+x).
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/data /app/logs /root/.cache/huggingface \
    && chown -R "$APP_UID:$APP_GID" /app/data /app/logs /root/.cache/huggingface \
    && chmod 0755 /root

EXPOSE 18888

# Runs as root briefly to repair volume ownership, then execs the CMD as the
# non-root alpha user via gosu (UID/GID 10001). Do not add a compose-level
# `user:` override for the default path, otherwise the entrypoint cannot chown
# root-owned named volumes.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18888"]
