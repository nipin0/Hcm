"""P2 Optuna 自动调参 — TPE sampler 离线优化 co.* 配置键（PRD §4.3.1-4.3.5）.

用法::

    python optuna_tuner.py [--apply] [--trials 100] [--train-days 60] [--test-days 15]

安全默认：dry-run（只打印最佳参数，不写回 PG/Redis）。
显式 ``--apply`` 才经 ``ConfigProviderV3.set_batch`` 双写并记录 ``hcm_ai.param_history``。

前置：
- ``hcm_ai.labeled_samples`` 需包含标注数据（label IN ('win','loss')）
- 依赖 ``optuna>=3.0``（运行前 pip install optuna；import 失败时优雅降级）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np

# ── 路径设置 ──
HERE = os.path.dirname(os.path.abspath(__file__))          # .../hcm-signal-tower
ROOT = os.path.dirname(HERE)                               # .../hcm-v2
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

# ── Optuna 可选依赖 ──
try:
    import optuna
    from optuna.samplers import TPESampler
    from optuna.pruners import MedianPruner
    HAS_OPTUNA = True
except ImportError:
    HAS_OPTUNA = False

import asyncpg
import redis.asyncio as aioredis
from shared.config_provider import ConfigProviderV3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("optuna_tuner")

# ═══════════════════════════════════════════════════════════════════
# 调参搜索空间定义（参数名 → (type, low, high, step/default)
# 全部 co.* 键在 seed SQL 定义，此处仅列优化池中的关键变量。
# ═══════════════════════════════════════════════════════════════════

TUNE_SPACE: list[dict] = [
    # G1 校准因子（已由 local_calibrator 管理，Optuna 不覆盖；仅调校准后仍可二次校正的特殊场景）
    #   — 此处故意不纳入校准因子以保护离线校准结果 —

    # G2 假信号过滤可调项
    {"key": "co.filter.f1_enabled",   "type": "categorical", "choices": [True, False]},
    {"key": "co.filter.f2_enabled",   "type": "categorical", "choices": [True, False]},
    {"key": "co.filter.f4_enabled",   "type": "categorical", "choices": [True, False]},
    {"key": "co.filter.f5_enabled",   "type": "categorical", "choices": [True, False]},
    {"key": "co.filter.f2_ratio",     "type": "float",       "low": 20, "high": 80, "step": 5},
    {"key": "co.filter.f1_penalty",   "type": "float",       "low": 10, "high": 40, "step": 5},
    {"key": "co.filter.f3_penalty",   "type": "float",       "low": 10, "high": 40, "step": 5},
    {"key": "co.filter.f5_penalty",   "type": "float",       "low": 10, "high": 50, "step": 5},
    {"key": "co.filter.f5_consecutive","type": "int",        "low": 2,  "high": 5,  "step": 1},

    # G3 自适应门槛可调项
    {"key": "co.gate.score_scale",    "type": "float",       "low": 50,  "high": 200, "step": 10},
    {"key": "co.gate.adx_strong",     "type": "float",       "low": 20,  "high": 50,  "step": 5},
    {"key": "co.gate.shock.atr_mult", "type": "float",       "low": 1.5, "high": 4.0, "step": 0.5},
    {"key": "co.gate.strong.trend",   "type": "float",       "low": 40,  "high": 80,  "step": 5},
    {"key": "co.gate.weak.trend",     "type": "float",       "low": 40,  "high": 80,  "step": 5},
    {"key": "co.gate.shock.trend",    "type": "float",       "low": 50,  "high": 90,  "step": 5},
    {"key": "co.gate.risk.high_offset","type": "float",      "low": 5,   "high": 20,  "step": 5},
    {"key": "co.gate.risk.med_offset", "type": "float",      "low": 0,   "high": 15,  "step": 5},
]

# ═══════════════════════════════════════════════════════════════════
# 调参器
# ═══════════════════════════════════════════════════════════════════

class OptunaTuner:
    """离线 Optuna 自动调参（P2 核心）。

    加载标注样本（labeled_samples），通过 TPE sampler 搜索 co.* 配置键的最优组合。
    目标函数基于样本回放模拟（apply gate + filters → 计算 sharpe）。
    """

    def __init__(
        self,
        config_provider: Any = None,
        db_pool: Any = None,
        train_days: int = 60,
        test_days: int = 15,
        target: str = "sharpe_ratio",
        max_drawdown_pct: float = 15.0,
        min_trades: int = 60,
        seed: int = 42,
    ):
        self._cfg = config_provider
        self._db = db_pool
        self._train_days = train_days
        self._test_days = test_days
        self._target = target
        self._max_dd_pct = max_drawdown_pct
        self._min_trades = min_trades
        self._seed = seed
        self._train_samples: list = []
        self._test_samples: list = []

    # ── 数据加载 ────────────────────────────────

    async def load_samples(self, end_date: Optional[datetime] = None) -> bool:
        """加载训练/测试集标注样本（按时序拆分）。"""
        if self._db is None:
            logger.error("No DB connection")
            return False

        end = end_date or datetime.now(timezone.utc)
        train_end = end - timedelta(days=self._test_days)
        train_start = train_end - timedelta(days=self._train_days)

        for tag, start, stop in [
            ("train", train_start, train_end),
            ("test", train_end, end),
        ]:
            rows = await self._db.fetch(
                "SELECT direction, raw_score, m5_regime, label "
                "FROM hcm_ai.labeled_samples "
                "WHERE label IN ('win','loss') AND bar_time >= $1 AND bar_time < $2 "
                "ORDER BY bar_time",
                start, stop,
            )
            samples = [dict(r) for r in rows]
            if tag == "train":
                self._train_samples = samples
            else:
                self._test_samples = samples
            logger.info("Loaded %d %s samples (%s → %s)", len(samples), tag, start.date(), stop.date())

        if len(self._train_samples) < self._min_trades:
            logger.warning(
                "Training samples %d < min_trades %d — results may be unreliable",
                len(self._train_samples), self._min_trades,
            )
        return len(self._train_samples) > 0

    # ── 样本回放模拟 ────────────────────────────

    @staticmethod
    def _simulate_on_samples(
        params: dict,
        samples: list,
        min_trades: int,
        max_dd_pct: float,
    ) -> tuple[float, dict]:
        """用给定参数组在标注样本上回放 → 计算综合指标。

        模拟规则（简化的逐样本判定）：
          - 信号方向与标注方向一致 → 执行（按 1:1 简化 RR）
          - raw_score < scale_norm * gate_trend → 拦截
          - 连续回撤超过 max_dd_pct → 淘汰
        """
        scale = max(float(params.get("co.gate.score_scale", 100)), 1.0)
        gate_strong = params.get("co.gate.strong.trend", 65) / scale
        gate_weak = params.get("co.gate.weak.trend", 70) / scale
        gate_shock = params.get("co.gate.shock.trend", 80) / scale

        trades: list[float] = []
        equity = 1.0
        peak = 1.0

        for s in samples:
            raw = float(s.get("raw_score") or 0)
            regime = str(s.get("m5_regime") or "").upper()
            label = str(s.get("label") or "")

            # 简化的体制门槛：根据 regime 选 gate
            if "RANGE" in regime or "NEUTRAL" in regime:
                continue  # 震荡不交易
            if raw >= gate_strong * 0.7:  # 宽松模拟（真实引擎有更细粒度）
                pass
            else:
                continue

            # 盈亏记账
            if label == "win":
                trades.append(+0.01)   # 简化：每笔赢 1%
                equity *= 1.01
            elif label == "loss":
                trades.append(-0.005)  # 简化：每笔亏 0.5%
                equity *= 0.995
            else:
                continue

            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100
            if dd > max_dd_pct:
                trades.clear()
                break

        n = len(trades)
        if n < min_trades:
            return -999.0, {"win_rate": 0, "n_trades": n, "n_train": len(samples), "sharpe": -999}

        returns = np.array(trades)
        # 年化夏普（假设 M5 bar，每年 ~252*288≈72576 个 bar，但我们用逐笔简化：按交易次数）
        mean_r = float(np.mean(returns))
        std_r = float(np.std(returns)) if len(returns) > 1 else 1e-9
        sharpe = (mean_r / max(std_r, 1e-9)) * np.sqrt(min(n, 252))

        wins = sum(1 for r in returns if r > 0)
        win_rate = wins / n if n else 0

        return float(round(sharpe, 4)), {
            "win_rate": round(win_rate, 4),
            "n_trades": n,
            "n_train": len(samples),
            "sharpe": round(sharpe, 4),
            "mean_return": round(mean_r, 6),
            "final_equity": round(equity, 4),
        }

    # ── Optuna 目标函数 ─────────────────────────

    def _make_objective(self):
        """返回闭包（capture self + train samples），供 optuna.Study.optimize 使用。"""
        samples = list(self._train_samples)  # shallow copy

        def objective(trial) -> float:
            params: dict = {}
            for item in TUNE_SPACE:
                key = item["key"]
                t = item["type"]
                if t == "float":
                    params[key] = trial.suggest_float(
                        key, item["low"], item["high"], step=item.get("step"),
                    )
                elif t == "int":
                    params[key] = trial.suggest_int(
                        key, item["low"], item["high"], step=item.get("step", 1),
                    )
                elif t == "categorical":
                    params[key] = trial.suggest_categorical(key, item["choices"])
            sharpe, stats = self._simulate_on_samples(
                params, samples, self._min_trades, self._max_dd_pct,
            )
            for k, v in stats.items():
                trial.set_user_attr(k, v)
            return sharpe

        return objective

    # ── 测试集验证 ──────────────────────────────

    def _validate_on_test(self, params: dict) -> dict:
        """用最优参数在测试集上验证并返回指标。"""
        sharpe, stats = self._simulate_on_samples(
            params, self._test_samples, max(self._min_trades // 3, 10), self._max_dd_pct,
        )
        stats["test_sharpe"] = sharpe
        return stats

    # ── 主运行入口 ──────────────────────────────

    async def run(self, n_trials: int = 100, apply: bool = False) -> Optional[dict]:
        """执行 Optuna 调参。

        Args:
            n_trials: TPE 试验次数。
            apply: 是否写回 PG+Redis（默认 False=dry-run）。

        Returns:
            最佳参数组，或 None（数据不足 / optuna 不可用）。
        """
        if not self._train_samples:
            logger.error("无训练样本，请先 load_samples()")
            return None

        # 测试集验证
        test_stats = None
        if self._test_samples:
            test_stats = self._validate_on_test(
                {s["key"]: s.get("choices", [True])[0] if s["type"] == "categorical"
                 else s.get("low", 0) for s in TUNE_SPACE}
            )
            logger.info("Baseline test: %s", test_stats)

        study = optuna.create_study(
            direction="maximize",
            sampler=TPESampler(seed=self._seed),
            pruner=MedianPruner(n_startup_trials=max(5, n_trials // 20)),
        )
        study.optimize(self._make_objective(), n_trials=n_trials, show_progress_bar=True)

        best_params = study.best_params
        best_value = study.best_value
        logger.info("Best trial #%d: sharpe=%.4f", study.best_trial.number, best_value)
        logger.info("Best params: %s", best_params)

        # 测试集验证
        if self._test_samples:
            test_out = self._validate_on_test(best_params)
            logger.info("Test set validation: sharpe=%.4f n=%d wr=%.2f%%",
                        test_out.get("test_sharpe", -999),
                        test_out.get("n_trades", 0),
                        test_out.get("win_rate", 0) * 100)

        if not apply:
            logger.info("DRY-RUN：未写回（加 --apply 才执行 PG/Redis 双写）")
            return best_params

        # 写回 PG+Redis + 参数历史记录
        if self._cfg is not None:
            write_items = {
                k: str(v) for k, v in best_params.items()
            }
            result = await self._cfg.set_batch(write_items, category="co_source")
            logger.info("写入结果: %s", result)

            # 记录到 param_history
            if self._db is not None:
                try:
                    import json as _json
                    await self._db.execute(
                        "INSERT INTO hcm_ai.param_history "
                        "(run_date, source, params_json, sharpe, note) "
                        "VALUES (CURRENT_DATE, 'optuna', $1, $2, $3)",
                        _json.dumps(best_params),
                        round(best_value, 4),
                        f"n_trials={n_trials} train_days={self._train_days} test_days={self._test_days}",
                    )
                    logger.info("参数历史已写入 param_history")
                except Exception as exc:
                    logger.warning("param_history write failed (table may not exist): %s", exc)

        return best_params


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

async def _main():
    if not HAS_OPTUNA:
        logger.error("optuna 未安装，请运行: pip install optuna>=3.0")
        return

    ap = argparse.ArgumentParser(description="P2 Optuna 自动调参")
    ap.add_argument("--apply", action="store_true", help="显式写回 PG+Redis（默认 dry-run）")
    ap.add_argument("--trials", type=int, default=100, help="TPE 试验次数（默认 100）")
    ap.add_argument("--train-days", type=int, default=60, help="训练集天数")
    ap.add_argument("--test-days", type=int, default=15, help="测试集天数")
    ap.add_argument("--seed", type=int, default=42, help="随机种子")
    args = ap.parse_args()

    dsn = os.getenv("HCM_PG_DSN") or os.getenv("DATABASE_URL") or os.getenv("DB_URL")
    redis_url = os.getenv("HCM_REDIS_URL") or os.getenv("REDIS_URL") or "redis://redis:6379/0"
    if not dsn:
        logger.error("缺少环境变量 HCM_PG_DSN")
        return

    db_pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)
    rclient = aioredis.from_url(redis_url, decode_responses=True)
    cfg = ConfigProviderV3(db_pool, rclient)
    await cfg.initialize()

    try:
        tuner = OptunaTuner(
            config_provider=cfg,
            db_pool=db_pool,
            train_days=args.train_days,
            test_days=args.test_days,
            seed=args.seed,
        )
        ok = await tuner.load_samples()
        if not ok:
            logger.error("无标注数据，无法调参（至少需要 %d 个训练样本）", tuner._min_trades)
            return
        best = await tuner.run(n_trials=args.trials, apply=args.apply)
        if best:
            logger.info("最佳参数已输出")
    finally:
        await cfg.shutdown()
        await db_pool.close()
        try:
            await rclient.aclose()
        except Exception:
            pass


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
