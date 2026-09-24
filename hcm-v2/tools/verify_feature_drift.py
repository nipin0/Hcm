"""verify_feature_drift.py — PSI 漂移监控生产接入的验证（离线，无 DB 依赖）。

覆盖：
  1) PSI 数值正确性：同分布 ≈0；均值平移 1.5σ → 显著 >0.2（回归 §3.3 的"共用边界"要求）
  2) `slot_due` 无状态幂等：同 bar 多次调用结论一致；跨周期/跨 every 的槽位正确
  3) `DictCfg` 适配：字符串/布尔/数值三态与缺省回退
  4) `sample_feature_windows` + `sample_and_report` 端到端（合成序列，adaptive 基准）
  5) 采样不可算时**返回 None 而非抛异常**（严格降级）
  6) 迁移脚本静态检查：幂等键与配置键齐备

用法：python tools/verify_feature_drift.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SIG = os.path.join(_ROOT, "hcm-signal-tower")
if _SIG not in sys.path:
    sys.path.insert(0, _SIG)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from signal_tower import feature_drift as FD  # noqa: E402

FAILED: list = []
N = 0


def ck(name: str, got, want) -> None:
    global N
    N += 1
    ok = got == want
    if isinstance(got, float) and isinstance(want, float):
        ok = (abs(got - want) < 1e-9) or (np.isnan(got) and np.isnan(want))
    if not ok:
        FAILED.append(f"{name}: got={got!r} want={want!r}")
        print(f"  [FAIL] {name}: got={got!r} want={want!r}")
    else:
        print(f"  [ok]   {name}")


def ckt(name: str, cond: bool, extra: str = "") -> None:
    global N
    N += 1
    if not cond:
        FAILED.append(f"{name} {extra}")
        print(f"  [FAIL] {name} {extra}")
    else:
        print(f"  [ok]   {name} {extra}")


# ══════════ 1) PSI 正确性 ══════════
print("=== 1) PSI：共用边界 + 漂移检出 ===")
rng = np.random.default_rng(7)
base = rng.normal(0.0, 1.0, 5000)
same = rng.normal(0.0, 1.0, 5000)
drift = rng.normal(1.5, 1.0, 5000)
ck("同分布 PSI ≈ 0（<0.05）", 1.0 if FD.calculate_psi(base, same, 10) < 0.05 else 0.0, 1.0)
ckt("漂移 PSI 显著（>0.2）", FD.calculate_psi(base, drift, 10) > 0.2,
    f"(psi={FD.calculate_psi(base, drift, 10):.4f})")
# 回归：各切各的边界会得到荒谬值
import pandas as pd  # noqa: E402

pb = pd.Series(pd.qcut(base, 10, duplicates="drop")).value_counts(normalize=True)
pn = pd.Series(pd.qcut(drift, 10, duplicates="drop")).value_counts(normalize=True)
wrong = float(sum((pb.get(b, 1e-8) - pn.get(b, 1e-8)) * np.log(pb.get(b, 1e-8) / pn.get(b, 1e-8))
                  for b in pb.index))
ckt("错误分箱与正确值显著不同（故必须共用边界）",
    abs(wrong - FD.calculate_psi(base, drift, 10)) > 0.5,
    f"(wrong={wrong:.2f})")
ck("基准退化（常量）→ NaN", float("nan"), FD.calculate_psi(np.zeros(500), drift, 10))

# ══════════ 2) slot_due 无状态幂等 ══════════
print("\n=== 2) slot_due：无状态槽位（幂等） ===")
cfg12 = FD.DictCfg({"state.drift.every_bars": 12})
# M5 × 12 = 3600s ⇒ 整点才采
ck("M5 整点 epoch 命中槽位", FD.slot_due(3600, "M5", cfg12), True)
ck("M5 非整点不命中", FD.slot_due(3900, "M5", cfg12), False)
ck("同 bar 重复调用结论一致（幂等）",
   (FD.slot_due(7200, "M5", cfg12), FD.slot_due(7200, "M5", cfg12)), (True, True))
cfg1 = FD.DictCfg({"state.drift.every_bars": 1})
ck("every_bars=1 ⇒ 每根都采", FD.slot_due(3900, "M5", cfg1), True)
cfg_h = FD.DictCfg({"state.drift.every_bars": 4})
ck("H1 × 4 = 4h 槽位", FD.slot_due(4 * 3600, "H1", cfg_h), True)

# ══════════ 3) DictCfg 适配 ══════════
print("\n=== 3) DictCfg：三态解析与缺省回退 ===")
c = FD.DictCfg({"a": "1", "b": "true", "c": 0.25, "d": "0", "e": "adaptive"})
ck("字符串数值 → 0.25", c.num("c"), 0.25)
ck("字符串 '1' → flag True", c.flag("a"), True)
ck("字符串 'true' → flag True", c.flag("b"), True)
ck("字符串 '0' → flag False", c.flag("d"), False)
ck("缺失键 → 回退 DEFAULTS 的 False", c.flag("state.drift.enabled"), False)
ck("缺失键 raw → DEFAULTS 值", c.raw("state.drift.bins"), FD.DEFAULTS["state.drift.bins"])

# ══════════ 4/5) 窗口采样 + 端到端 ══════════
print("\n=== 4) sample_feature_windows + sample_and_report ===")
n = 1200
t = np.arange(n)
close = 4000.0 + np.cumsum(rng.normal(0, 0.4, n)) + np.sin(t / 40.0) * 3.0
span = np.abs(rng.normal(0, 0.8, n))
high = close + span
low = close - span
eps = (1_780_000_000 + t * 300).astype(np.int64)

from signal_tower.state_features import STATE_FEATURE_COLS  # noqa: E402

cols = list(STATE_FEATURE_COLS)
ref, cur = FD.sample_feature_windows(high, low, close, eps, cols, 200)
ckt("采样窗口非空且列数一致",
    ref is not None and cur is not None and ref.shape[1] == len(cols),
    f"(shapes={None if ref is None else ref.shape}/{None if cur is None else cur.shape})")

cfg = FD.DictCfg({"state.drift.window_bars": 200, "state.drift.ref_kind": "adaptive"})
rep = FD.sample_and_report(high, low, close, eps, cols, cfg, tf="M5")
ckt("端到端返回报表", isinstance(rep, dict), f"(verdict={rep and rep['verdict']})")
ckt("报表字段齐备",
    isinstance(rep, dict) and
    set(("ref_kind", "window_bars", "n_ref", "n_cur", "max_psi", "max_col",
         "verdict", "blocked", "psi", "disable_hint")) <= set(rep.keys()))
ckt("逐特征 PSI 覆盖全部列",
    isinstance(rep, dict) and len(rep.get("psi", {})) == len(cols),
    f"(psi_n={len(rep.get('psi', {}))})")

# 降级：数据不足 → None（**不得抛异常**）
ckt("数据不足 → 返回 None（不抛异常）",
    FD.sample_and_report(high[:50], low[:50], close[:50], eps[:50], cols, cfg) is None)

# ══════════ 6) 迁移脚本静态检查 ══════════
print("\n=== 5) 迁移 0053 静态检查 ===")
sql_path = os.path.join(_ROOT, "deploy", "migrations", "0053_feature_drift.sql")
sql = open(sql_path, "r", encoding="utf-8").read() if os.path.exists(sql_path) else ""
ckt("迁移文件存在", bool(sql))
ckt("建表 IF NOT EXISTS（幂等）", "CREATE TABLE IF NOT EXISTS hcm_signal.feature_drift_log" in sql)
ckt("唯一索引（幂等键）", "uq_feature_drift_log" in sql)
ckt("默认全关：state.drift.enabled=false", "'state.drift.enabled', 'state', 'false'" in sql)
ckt("auto_disable 默认 false（不改变交易行为）",
    "'state.drift.auto_disable', 'state', 'false'" in sql)
for k in ("every_bars", "window_bars", "ref_kind", "psi_warn", "psi_block",
          "bins", "block_streak"):
    ckt(f"配置键 state.drift.{k} 已 seed", f"'state.drift.{k}'" in sql)

print("\n" + "=" * 70)
print(f"共 {N} 项，失败 {len(FAILED)} 项" + (f"：{FAILED}" if FAILED else " —— 全部通过"))
sys.exit(1 if FAILED else 0)
