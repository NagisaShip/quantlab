# syntax=docker/dockerfile:1.6
# QuantLab v 0.1 preview —— 一个镜像，两种角色：api（控制面）与 worker（执行面），由 command 区分。

# ---------- 前端构建 ----------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------- 运行时 ----------
FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    QAGENT_HOME=/data/home \
    QAGENT_WORKER=external
WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY pyproject.toml README.md agent.config.example.toml ./
COPY src ./src
COPY prompts ./prompts
RUN pip install --no-deps -e . \
    # 策略子进程会降权到 nobody，源码必须对其可读、不可写
    && chmod -R a+rX,go-w /app \
    && mkdir -p /data/home && chmod 700 /data
COPY --from=web /web/dist ./web/dist

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/healthz',timeout=4).status==200 else 1)"
CMD ["qagent", "serve", "--host", "0.0.0.0", "--port", "8000"]
