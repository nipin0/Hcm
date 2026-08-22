"""共源信号 — P1b 校准层离线计算器（local_calibrator.py）.

PRD §4.1.2 校准因子：``calibrated_score = raw_score × calib[M5_regime]``
本脚本每日（或按需）离线运行：

  1. 从 ``hcm_ai.labeled_samples`` 读已标注样本（``label IN ('win','loss')``）
  2. 按 ``m5_regime`` 分组统计胜率
  3. **冷启动门控**：distinct 标注天数 < ``co.calib.min_days``(默认 7)
     → 跳过写回，校准因子保持冷启动值 1.0（co_source.py 默认）
  4. 计算每态校准因子 = ``f(win_rate)``，带 clamp 边界
  5. 经 ``ConfigProviderV3.set_batch`` 双写回 PG ``hcm_config.metadata``
     + Redis ``hcm:config:v2``（约束 ② PG+Redis 双写）

安全默认：``dry-run``（只打印、不写回）。必须显式 ``--apply`` 才执行
写回（写 PG 属数据改动红线，需用户授权命令）。

前置：标注样本需由信号塔 / DeepSeek 写入（P1b.1 采集）。本脚本只**消费**
已存在的标注，不负责采集。无标注数据时自动判定冷启动并跳过。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

import asyncpg
import redis.asyncio as aioredis

# ── 路径：使脚本可在 hcm-signal-tower/ 下直接 ``python local_calibrator.py`` 运行 ──
HERE = os.path.dirname(os.path.abspath(__file__))          # .../hcm-signal-tower
ROOT = os.path.dirname(HERE)                               # .../hcm-v2
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from signal_tower.co_source import _CALIB_KEYS             # 复用 Regime→配置键 映射
from shared.config_provider import ConfigProviderV3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("local_calibrator")

# ── 算法常数（实现细节，非业务可调参数；约束 ① 针对业务阈值/因子/门槛，此处为数学映射）──
CALIB_ALPHA = 1.0            # win_rate 偏离基准 0.5 的放大系数
CALIB_CLAMP_MIN = 0.6        # 校准因子下界（避免某体制被过度压制）
CALIB_CLAMP_MAX = 1.4        # 校准因子上界（避免某体制被过度放大）
MIN_SAMPLES_PER_REGIME = 20  # 单态最小标注样本；不足则信任默认 1.0


def _win_rate_to_calib(win_rate: float, n_samples: int) -> float:
    """胜率 → 校准因子。

    设计：``calib = 1.0 + ALPHA * (win_rate - 0.5)``，anchored 在基准胜率 0.5。
    - win_rate=0.7 → 1.2（放大可信信号）
    - win_rate=0.3 → 0.8（压缩不可信信号）
    样本不足时回退 1.0（不拿小样本冒险）。
    """
    if n_samples < MIN_SAMPLES_PER_REGIME:
        return 1.0
    raw = 1.0 + CALIB_ALPHA * (win_rate - 0.5)
    return round(min(CALIB_CLAMP_MAX, max(CALIB_CLAMP_MIN, raw)), 4)


def _regime_key(regime_str: str) -> Optional[str]:
    """把标注表的 m5_regime 字符串映射到配置键（复用 co_source._CALIB_KEYS）。"""
    s = (regime_str or "").upper()
    for reg, key in _CALIB_KEYS.items():
        if reg.value.upper() == s or reg.name.upper() == s:
            return key
    return None


async def compute_factors(db_pool, min_days: int) -> Optional[dict]:
    """读标注、算冷启动、返回 {config_key: calib_factor} 或 None(冷启动/数据不足)。

    只读 SELECT，不写任何数据（符合只读侦察红线）。
    """
    days = await db_pool.fetchval(
        "SELECT COUNT(DISTINCT date(bar_time)) "
        "FROM hcm_ai.labeled_samples WHERE label IN ('win','loss')"
    )
    if days is None or int(days) < min_days:
        logger.info(
            "冷启动：标注天数=%s < min_days=%s → 跳过写回，校准因子保持 1.0",
            days, min_days,
        )
        return None

    rows = await db_pool.fetch(
        "SELECT m5_regime, COUNT(*) AS n, "
        "SUM(CASE WHEN label='win' THEN 1 ELSE 0 END) AS wins "
        "FROM hcm_ai.labeled_samples WHERE label IN ('win','loss') "
        "GROUP BY m5_regime"
    )
    factors: dict = {}
    for r in rows:
        regime_str = r["m5_regime"]
        n = int(r["n"])
        wins = int(r["wins"] or 0)
        win_rate = (wins / n) if n else 0.5
        calib = _win_rate_to_calib(win_rate, n)
        key = _regime_key(regime_str)
        if key:
            factors[key] = calib
            logger.info("  %-22s n=%-4d win_rate=%.2f → calib=%s", key, n, win_rate, calib)
        else:
            logger.warning("  未知 m5_regime=%r 已忽略", regime_str)
    return factors or None


async def run_calibration(apply: bool) -> None:
    dsn = os.getenv("HCM_PG_DSN") or os.getenv("DATABASE_URL") or os.getenv("DB_URL")
    redis_url = os.getenv("HCM_REDIS_URL") or os.getenv("REDIS_URL") or "redis://redis:6379/0"
    if not dsn:
        logger.error("缺少环境变量 HCM_PG_DSN（或 DATABASE_URL），无法连接 PG")
        return

    db_pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    rclient = aioredis.from_url(redis_url, decode_responses=True)
    cfg = ConfigProviderV3(db_pool, rclient)
    await cfg.initialize()
    try:
        min_days = await cfg.get_int("co.calib.min_days", 7)
        factors = await compute_factors(db_pool, min_days)
        if factors is None:
            logger.info("无校准因子可写回（冷启动或数据不足）")
            return
        logger.info("计算得到的校准因子: %s", factors)
        if not apply:
            logger.info("DRY-RUN：未写回（加 --apply 才执行 PG/Redis 双写）")
            return
        # 约束 ②：经 ConfigProviderV3.set_batch 双写（PG SoT → Redis → 失效广播）
        result = await cfg.set_batch(factors, category="co_source")
        logger.info("写回结果: %s", result)
    finally:
        await cfg.shutdown()
        await db_pool.close()
        try:
            await rclient.aclose()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="P1b 校准因子离线计算器")
    ap.add_argument(
        "--apply", action="store_true",
        help="显式写回 PG+Redis（默认 dry-run，只打印不写）",
    )
    args = ap.parse_args()
    asyncio.run(run_calibration(args.apply))


if __name__ == "__main__":
    main()
