# Collector / K线 修复 — 执行与验证指南（2026-09-21）

> 配套报告：`docs/Collector_klines采集故障分析_20260921.md`
> 已落地项（沙箱内已执行、可验证）：配置热修复 + M15 配置暂存 + 代码加固（已编译）
> 待宿主执行项：桥链/容器重启、回补（可选）

---

## 一、沙箱内已完成（无需重启即生效 / 已编译待部署）

### 1. 配置热修复 — `state.trend.min_sl_atr`（立即生效，零重启）
- 写入 Redis `hcm:config:v2` + PG `hcm_config.config`：`state.trend.min_sl_atr = 0.3`
- 桥经 `_get_close_config` 每次调用热读 Redis → **立即生效**；值=原 fallback，零功能变化，去除硬编码依赖。
- 验证：桥日志不再出现 `REQUIRED close config 'state.trend.min_sl_atr' missing`；`hget hcm:config:v2 state.trend.min_sl_atr` → `0.3`。

### 2. M15 启用 — 配置暂存（重启桥后生效）
- 写入 Redis + PG：`datasource.timeframes = M5,M1,H1,H4,M30,D1,M15`（原缺 M15）。
- 桥在 `mt5_bridge.py:434` 启动期读取该键 → **须重启桥**才采集 M15。
- 验证：重启后桥日志 `Found klines for XAUUSD: 2000 bars (M15)`；`SELECT count(*) FROM hcm_market.klines_xauusd WHERE time_frame='M15'` > 0。

### 3. 代码加固（已 `py_compile` 通过，待重启部署）
文件：`tools/mt5_bridge.py`、`hcm-signal-tower/signal_tower/scheduler.py`
备份：`*.bak_20260921_180158_collectorfix`

| 改动 | 文件:锚点 | 作用 | 风险 |
|---|---|---|---|
| P2-6 misaligned 限频（每 tf 每小时≤1 条 WARNING） | mt5_bridge.py:563 附近 `_MISALIGN_LOG_INTERVAL`/`_misalign_last_log` + :620 节流 | 止住 H4/D1 每日 1~2 万条刷屏、淹没真错 | 低（纯增量） |
| P0-1 写停滞心跳 `bridge:last_kline_write:{login}` | mt5_bridge.py `LAST_KLINE_WRITE_TS` + 心跳块发布 | 外部可监测"桥在但 K 线零写入"的僵死 | 低（心跳块内 set，10s 节奏） |
| P0-2 feed-stale 持久告警 `hcm:alerts:feed_stale(_active)` | scheduler.py:1906 附近 | 断流落 Redis 标记，运维可直接感知（原仅暂停信号） | 低（try/except 包裹） |

> 注：P0-1 仅新增"写停滞可被监测"的能力；真正"超时强重启桥"需宿主看门狗配合（见下）。

---

## 二、需在宿主执行（沙箱 docker 不可达，无法从沙箱重启）

### 步骤 0（重要）：确认当前健康 + 选低活跃窗口
- 宿主 `docker ps` 确认 `hcm-collector` 状态（沙箱查不到，须宿主复核）；确认两桥 `bridge:alive:*` 心跳在。
- 重启桥链会短暂停交易 → 选亚/欧/美盘低活跃时段。

### 步骤 1：重启桥链以应用 M15 + 代码改动
```
cd D:\HCM_ASST\hcm-v2
start.bat restart        # 停+起容器 + 彻底重置桥链（杀 launcher/看门狗/桥并重拉全新）
```
> `start.bat restart` 同时重启容器（含 signal-tower，使 scheduler.py 改动生效）与桥保活链。
> 若仅想热更容器代码而桥已由宿主托管，亦可单独 `docker restart hcm-v2-hcm-signal-tower-1`。

### 步骤 2：验证 M15 启用
```
redis-cli hget hcm:config:v2 datasource.timeframes        # 应含 M15
# 桥日志出现：Found klines for XAUUSD: 2000 bars (M15)
psql -h localhost -U hcm -d hcm_v2 -c "SELECT count(*) FROM hcm_market.klines_xauusd WHERE time_frame='M15'"
```

### 步骤 3：验证 `min_sl_atr` 热修复
```
redis-cli hget hcm:config:v2 state.trend.min_sl_atr        # 0.3
# 重启后桥日志不再出现 "REQUIRED close config 'state.trend.min_sl_atr' missing"
```

### 步骤 4：验证 feed-stale 告警落 Redis（可人工制造/等下一次断流，或单测）
```
redis-cli get hcm:alerts:feed_stale_active                 # 断流时应为 "XAUUSD/M5"
```

### 步骤 5（可选）：回补 09-20 缺口 —— 【先 DRY-RUN 探可行性】
```
# 强烈建议：先停桥（避免第二路 MT5 连接冲突），再跑；回补完再 start.bat restart
cd D:\HCM_ASST\_scratch
python backfill_klines_0920.py                 # DRY-RUN：报各周期 MT5 在 09-20 00:00~09:00 返回棒数
python backfill_klines_0920.py --write         # 仅当 DRY-RUN 显示 MT5 确有数据时才加 --write
```
- **判读**：若某周期 DRY-RUN 返回 0 棒且 PG 也为 0 → 该时段 MT5 历史缺失（终端当时也宕机），回补无意义，**不要 --write**。
- 回补只 upsert、幂等，不删现有行。

---

## 三、风险 / 未核实 / 回滚
- ⚠️ 沙箱 `docker` 不可达 → 我**无法从沙箱重启桥/容器**；以上重启步骤须由宿主执行。
- ⚠️ 09-20 缺口**大概率不可从 MT5 回补**（桥+终端整体宕机 → MT5 本地历史同样缺失）；DRY-RUN 会证伪。
- 回滚：若代码改动导致桥/信号塔起不来，`copy` 回 `*.bak_20260921_180158_collectorfix` 同名文件即可（已备份）。
- 本次未改动 `hcm-collector` 容器；其 `pg_write_enabled` 保持关闭（避免双写）。
