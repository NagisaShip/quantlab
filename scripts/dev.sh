# 一键启动（Linux / macOS）：API（控制面）+ Worker（执行面），默认 SQLite + 本地唤醒
# 用法：bash scripts/dev.sh            生产部署请用 docker compose（见 README）
# 可选：export QAGENT_DATABASE_URL=postgresql://...  QAGENT_REDIS_URL=redis://...
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"

echo "==> 安装依赖（锁定版本）"
"$PY" -m pip install -q -r requirements-dev.lock || "$PY" -m pip install -q -e ".[dev]"
"$PY" -m pip install -q --no-deps -e .

if [ ! -f agent.config.toml ]; then
  echo "==> 未找到 agent.config.toml，复制示例配置（可只用环境变量填写模型密钥）"
  cp agent.config.example.toml agent.config.toml
fi

if [ -d web ] && [ ! -d web/dist ]; then
  echo "==> 构建前端"
  (cd web && npm ci --no-audit --no-fund && npm run build)
fi

export PYTHONPATH="$ROOT/src"
export QAGENT_WORKER=external

echo "==> 数据库迁移"
"$PY" -m quant_agent.cli migrate

echo "==> 启动 worker（日志：logs/worker.log）"
mkdir -p logs
"$PY" -m quant_agent.cli worker --slots "${QAGENT_WORKER_SLOTS:-2}" > logs/worker.log 2>&1 &
WORKER_PID=$!
trap 'echo "==> 停止 worker"; kill $WORKER_PID 2>/dev/null || true' EXIT INT TERM

echo "==> 启动 API：http://127.0.0.1:8000（API 文档 /docs）。第一个注册的账号成为管理员"
"$PY" -m quant_agent.cli serve --host 127.0.0.1 --port 8000
