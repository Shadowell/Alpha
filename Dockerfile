# Alpha — A-share quant screening (API / data runtime with Kronos CPU inference)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

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

EXPOSE 18888

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18888"]
