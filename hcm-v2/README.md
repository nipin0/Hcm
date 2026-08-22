# HCM v2 — 量化交易系统

多品种量化交易系统 v2，基于 PostgreSQL + Redis 事件驱动架构，支持 XAUUSD/BTCUSD 双品种交易。

## 架构

```
hcm-gateway       — MT5 桥接（gRPC + WebSocket）
hcm-collector     — 行情采集（K线/Tick）
hcm-signal-tower  — 信号塔（五级市况判定 + AI 研判）
hcm-market-intel  — 市场情报（宏观/情绪/事件/流动性）
hcm-risk-engine   — 风控引擎（规则链）
hcm-dispatcher    — 信号分发（gRPC 下单）
hcm-copy-trading  — 跟单引擎（<100ms 延迟）
hcm-web           — Web 管理后台（Dashboard + 配置中心）
```

## 快速启动

```bash
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY 等

docker compose up -d
```

## 端口

| 服务 | 端口 | 协议 |
|------|------|------|
| hcm-web | 8000 | HTTP |
| hcm-collector | 8001 | HTTP |
| hcm-signal-tower | 8002 | HTTP |
| hcm-gateway | 8004 | gRPC |
| hcm-gateway WS | 8005 | WebSocket |
| hcm-market-intel | 8006 | HTTP |
| hcm-risk-engine | 8007 | HTTP |
| hcm-dispatcher | 8008 | HTTP |
| hcm-copy-trading | 8009 | HTTP |
| PostgreSQL | 5432 | TCP |
| Redis | 6379 | TCP |

## 技术栈

- Python 3.11 + FastAPI / asyncio
- PostgreSQL 15 + Redis 7
- gRPC + WebSocket
- Docker Compose
