#!/usr/bin/env python3
"""reanchor_ai_thresholds.py — 【F2 2026-09-18】把 AI 闸门阈值**锚定到校准后的真实分布**。

════════════════════════════════════════════════════════════════════════════
为什么需要它（根因，实证）
════════════════════════════════════════════════════════════════════════════
`quality_gate._simplified_action` 用**绝对概率**阈值裁决策略动作：

    p = c_ai / 100
    p < veto_floor        → VETO
    p < down_threshold    → DOWNGRADE
    p >= up_threshold(且 passed) → UPGRADE
    其余                  → HOLD

问题：`p` 是**校准后**概率，其值域由**当轮冠军模型的校准器**决定，而阈值是人工
写死的（`ai.lm.down_threshold=0.60` / `ai.lm.up_threshold=0.70`）。一旦换模型后
校准器把概率压进更窄/更低的区间，阈值就会**在数学上不可达**。

2026-09-18 实测（`hcm_ai.inference_log`，30 天 n=557,789）：

    冠军 v108 上线前（<09-14 21:44）：ai_score min=3.00  p50=33.91 p90=80.00 max=100.00
    冠军 v108 上线后               ：ai_score min=20.00 p50=28.78 p90=41.12 max=42.04

⇒ `p ∈ [0.20, 0.4204]` **永远 < 0.60** ⇒ HOLD / UPGRADE **不可达**，
`gate_decision` 30 天动作分布随之退化为：DOWNGRADE 732 / UPGRADE 368(=09-11 后归零)
/ HOLD 94(=09-04 后归零) / VETO 30(=09-11 后归零) —— 即**恒降级、信息量归零**。
（旁证：`ai.lm.veto_floor` 被人工置 0 以缓解"中等分全杀"。）

本工具把阈值**重锚**为近窗口 `ai_score` 的**分位数**，从而：
  1) 换模型/换校准器后无需人工重调 —— 阈值随分布自动跟随；
  2) 动作分布保持可解释的固定比例（如 p50 以下降级、p90 以上升级）。

════════════════════════════════════════════════════════════════════════════
安全设计（默认关闭 / 双写 / 限幅 / 可回滚）
════════════════════════════════════════════════════════════════════════════
1. **默认关闭**：`ai.lm.reanchor_enabled` 缺省/非 true ⇒ 直接退出（需 `--force` 才跑）。
   ⇒ 上线本工具**不改变任何现有行为**。（"阈值自我修改"是风控语义，必须显式开启。）
2. **双写合规**（铁律 5.2：PG 为真源）：先 PG(`hcm_config.metadata`) 提交成功，
   再写 Redis(`hcm:config:v2`) 并 PUB(`hcm:config:invalidate`)。失败即整体失败。
3. **限幅**：单次调整幅度不超过 `ai.lm.reanchor_max_step`（默认 0.10），防跳变。
4. **门槛**：窗口内样本 < `ai.lm.reanchor_min_samples`（默认 2000）⇒ 不动。
5. **单调性守卫**：算出的 up 必须 > down + `ai.lm.reanchor_min_gap`（默认 0.02），否则不动。
6. `--dry-run` 只打印将写入的值，不落库。

════════════════════════════════════════════════════════════════════════════
用法
════════════════════════════════════════════════════════════════════════════
    python reanchor_ai_thresholds.py --dry-run          # 只看会改成什么
    python reanchor_ai_thresholds.py --force            # 忽略 enabled 开关执行
    python reanchor_ai_thresholds.py                    # 仅在 enabled=true 时执行

退出码：0 成功/无需动作；1 参数或数据源错误；2 被开关拦截。
"""
from __future__ import annotations

import argparse
import os
import sys

# 注意：必须用 127.0.0.1（强制 IPv4）—— `localhost` 会解析到 ::1 被 wslrelay 劫持、
# 连不上 docker 生产（同 quality_scorer_launcher.py:33-35 的实测记录）。
DB_URL_DEFAULT = os.environ.get("DB_URL", "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2")
REDIS_HOST_DEFAULT = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT_DEFAULT = int(os.environ.get("REDIS_PORT", "6379"))
CONFIG_HASH = "hcm:config:v2"
INVALIDATE_CHANNEL = "hcm:config:invalidate"

# 键名与默认值（与 deploy/init_config_metadata.sql / ai_config.py 口径一致）
KEY_ENABLED = "ai.lm.reanchor_enabled"
KEY_Q_DOWN = "ai.lm.reanchor_quantile_down"
KEY_Q_UP = "ai.lm.reanchor_quantile_up"
KEY_WINDOW_DAYS = "ai.lm.reanchor_window_days"
KEY_MIN_SAMPLES = "ai.lm.reanchor_min_samples"
KEY_MAX_STEP = "ai.lm.reanchor_max_step"
KEY_MIN_GAP = "ai.lm.reanchor_min_gap"
KEY_DOWN = "ai.lm.down_threshold"
KEY_UP = "ai.lm.up_threshold"

DEFAULTS = {
    KEY_Q_DOWN: 0.50,
    KEY_Q_UP: 0.90,
    KEY_WINDOW_DAYS: 7.0,
    KEY_MIN_SAMPLES: 2000,
    KEY_MAX_STEP: 0.10,
    KEY_MIN_GAP: 0.02,
}


def _log(msg: str) -> None:
    print(f"[reanchor] {msg}", flush=True)


def _f(v, default: float) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def read_cfg(r) -> dict:
    """取配置真值：**PG 为真源**（Redis 仅作缺失补位，与 config_provider 口径一致）。"""
    out: dict = {}
    keys = list(DEFAULTS) + [KEY_ENABLED, KEY_DOWN, KEY_UP]
    pg: dict = {}
    try:
        import psycopg2
        conn = psycopg2.connect(DB_URL_DEFAULT, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config_key, COALESCE(current_value, default_value) "
                    "FROM hcm_config.metadata WHERE config_key = ANY(%s)",
                    (keys,),
                )
                pg = {k: v for k, v in cur.fetchall()}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        _log(f"WARN PG 读配置失败，回退 Redis：{exc}")
    for k in keys:
        v = pg.get(k)
        if v is None:
            v = r.hget(CONFIG_HASH, k)
        out[k] = v
    return out


def fetch_quantiles(db_url: str, window_days: float, q_down: float, q_up: float):
    """取窗口内 ai_score 的分位数与样本量。返回 (n, p_down, p_up, p50)。

    `ai_score` 是 0-100 尺度；分位数除 100 后即为 `quality_gate` 消费的 p 尺度。
    """
    import psycopg2
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), "
                "       percentile_disc(%s) WITHIN GROUP (ORDER BY ai_score), "
                "       percentile_disc(%s) WITHIN GROUP (ORDER BY ai_score), "
                "       percentile_disc(0.5) WITHIN GROUP (ORDER BY ai_score) "
                "FROM hcm_ai.inference_log "
                "WHERE created_at > now() - (%s || ' days')::interval "
                "  AND ai_score IS NOT NULL",
                (q_down, q_up, str(float(window_days))),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    n = int(row[0] or 0)
    if n == 0:
        return 0, None, None, None
    return n, float(row[1]), float(row[2]), float(row[3])


def write_config(pairs: list[tuple[str, str]], redis_host: str, redis_port: int) -> bool:
    """PG 真源先写并提交 → 再 Redis 双写 + PUB（该顺序不可颠倒，见铁律 5.2）。"""
    import psycopg2
    import redis
    try:
        conn = psycopg2.connect(DB_URL_DEFAULT, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                for key, val in pairs:
                    cur.execute(
                        "INSERT INTO hcm_config.metadata "
                        "(config_key, current_value, default_value, value_type, category) "
                        "VALUES (%s, %s, %s, 'string', 'ai') "
                        "ON CONFLICT (config_key) DO UPDATE "
                        "SET current_value = EXCLUDED.current_value, updated_at = now()",
                        (key, val, val),
                    )
                conn.commit()
        finally:
            conn.close()
        _log("PG hcm_config.metadata updated")
    except Exception as exc:  # noqa: BLE001
        _log(f"FAILED (PG)：{exc} —— Redis 未改动（真源优先，保持一致性）")
        return False
    try:
        r = redis.Redis(host=redis_host, port=redis_port, socket_timeout=5,
                        decode_responses=True)
        for key, val in pairs:
            r.hset(CONFIG_HASH, key, val)
        for key, _ in pairs:
            r.publish(INVALIDATE_CHANNEL, key)
        _log("Redis hcm:config:v2 updated + PUB")
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"WARN Redis 写失败（PG 已是真值，引擎热重载会从 PG 回填）：{exc}")
        return True


def _clamp_step(old: float, new: float, max_step: float) -> float:
    if old is None or max_step <= 0:
        return new
    if new > old + max_step:
        return old + max_step
    if new < old - max_step:
        return old - max_step
    return new


def main() -> int:
    ap = argparse.ArgumentParser(description="按近窗分位数重锚 AI 闸门阈值（默认关闭）")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印，不落库")
    ap.add_argument("--force", action="store_true", help="忽略 ai.lm.reanchor_enabled 开关")
    ap.add_argument("--db-url", default=DB_URL_DEFAULT)
    ap.add_argument("--redis-host", default=REDIS_HOST_DEFAULT)
    ap.add_argument("--redis-port", type=int, default=REDIS_PORT_DEFAULT)
    args = ap.parse_args()

    import redis
    r = redis.Redis(host=args.redis_host, port=args.redis_port, socket_timeout=5,
                    decode_responses=True)
    cfg = read_cfg(r)

    enabled = str(cfg.get(KEY_ENABLED) or "").strip().lower() in ("true", "1", "yes", "t")
    if not enabled and not args.force:
        _log(f"{KEY_ENABLED} 未开启（当前={cfg.get(KEY_ENABLED)!r}）⇒ 跳过。"
             f"确认要执行请加 --force，或先把该键置 true。")
        return 2

    q_down = _f(cfg.get(KEY_Q_DOWN), DEFAULTS[KEY_Q_DOWN])
    q_up = _f(cfg.get(KEY_Q_UP), DEFAULTS[KEY_Q_UP])
    window_days = _f(cfg.get(KEY_WINDOW_DAYS), DEFAULTS[KEY_WINDOW_DAYS])
    min_samples = int(_f(cfg.get(KEY_MIN_SAMPLES), DEFAULTS[KEY_MIN_SAMPLES]))
    max_step = _f(cfg.get(KEY_MAX_STEP), DEFAULTS[KEY_MAX_STEP])
    min_gap = _f(cfg.get(KEY_MIN_GAP), DEFAULTS[KEY_MIN_GAP])

    if not (0.0 < q_down < q_up < 1.0):
        _log(f"FAILED：分位数非法（down={q_down} up={q_up}）—— 需 0<down<up<1")
        return 1

    n, p_down, p_up, p50 = fetch_quantiles(args.db_url, window_days, q_down, q_up)
    if n == 0:
        _log(f"FAILED：窗口 {window_days} 天内无 ai_score 样本")
        return 1
    _log(f"窗口 {window_days} 天 n={n} | p50={p50 / 100:.4f} | "
         f"q{int(q_down * 100)}={p_down / 100:.4f} | q{int(q_up * 100)}={p_up / 100:.4f}")

    if n < min_samples:
        _log(f"SKIP：样本 {n} < {min_samples}（避免小样本把阈值调飞）")
        return 0

    old_down = _f(cfg.get(KEY_DOWN), 0.60)
    old_up = _f(cfg.get(KEY_UP), 0.70)
    new_down = round(_clamp_step(old_down, p_down / 100.0, max_step), 4)
    new_up = round(_clamp_step(old_up, p_up / 100.0, max_step), 4)

    if new_up <= new_down + min_gap:
        _log(f"SKIP：算出的域过窄（down={new_down} up={new_up} gap≤{min_gap}）"
             f"—— 该情形更像「校准器退化」（应由 CALIB_MIN_IQR 判据处理），"
             f"此处不把阈值压成一对。")
        return 0

    if abs(new_down - old_down) < 1e-6 and abs(new_up - old_up) < 1e-6:
        _log(f"NoChange：阈值已与分布一致（down={old_down} up={old_up}）")
        return 0

    _log(f"WILL SET {KEY_DOWN}: {old_down} → {new_down} | "
         f"{KEY_UP}: {old_up} → {new_up}"
         + ("（dry-run，不落库）" if args.dry_run else ""))
    if args.dry_run:
        return 0

    ok = write_config([(KEY_DOWN, f"{new_down}"), (KEY_UP, f"{new_up}")],
                      args.redis_host, args.redis_port)
    _log("DONE" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
