#!/bin/bash
set -e
cd "$(dirname "$0")"

echo ""
echo "  ============================================"
echo "        HCM v2 量化交易系统 — 一键启动"
echo "  ============================================"
echo ""

# ── 1. 检查 Docker ──
if ! command -v docker &>/dev/null; then
    echo "[错误] 未检测到 Docker，请先安装 Docker Desktop"
    exit 1
fi
if ! docker info &>/dev/null; then
    echo "[错误] Docker Desktop 未启动"
    exit 1
fi
echo "[OK] Docker Desktop 运行中"

# ── 2. 检查 Node.js ──
command -v node &>/dev/null || { echo "[错误] Node.js 未安装"; exit 1; }
echo "[OK] Node.js 已就绪"

# ── 3. 初始化 .env ──
[ ! -f .env ] && cp .env.example .env

# ── 4. 清理旧容器 ──
echo ""
echo "[INFO] 清理旧容器..."
docker compose down 2>/dev/null

# ── 5. 启动 ──
echo "[INFO] 启动后端服务..."
docker compose up -d --build

# ── 6. 等待 PostgreSQL ──
echo "[INFO] 等待 PostgreSQL 就绪..."
for i in $(seq 1 40); do
    docker compose ps postgres 2>/dev/null | grep -q healthy && break
    sleep 3
done
echo "[OK] PostgreSQL 已就绪"

# ── 7. 等待 Redis ──
echo "[INFO] 等待 Redis 就绪..."
for i in $(seq 1 30); do
    docker compose ps redis 2>/dev/null | grep -q healthy && break
    sleep 2
done
echo "[OK] Redis 已就绪"

# ── 8. 等待初始化 ──
echo "[INFO] 等待数据初始化 (15s)..."
sleep 15

# ── 9. 前端依赖 ──
echo ""
echo "[INFO] 检查前端依赖..."
if [ ! -d "hcm-web/frontend/node_modules/@mui" ]; then
    echo "[INFO] 安装前端依赖..."
    (cd hcm-web/frontend && npm install)
fi
echo "[OK] 前端依赖已就绪"

# ── 10. 启动 ──
echo ""
echo "  ============================================"
echo "          全部服务启动完成！"
echo ""
echo "    后端 API : http://localhost:8000"
echo "    API 文档 : http://localhost:8000/docs"
echo "    前端面板 : http://localhost:3000"
echo ""
echo "    停止服务 : Ctrl+C 后运行 ./stop.sh"
echo "  ============================================"
echo ""

cd hcm-web/frontend && npm run dev
