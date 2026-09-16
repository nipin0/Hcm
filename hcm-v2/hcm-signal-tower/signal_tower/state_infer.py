"""state_infer.py — 行情状态模型（4 类）**进程内推理装配**。

依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §4（模型）§5（推理与校验）

职责边界：
  · 只做「K 线序列 → 4 类概率 → 单根判定」，无状态、无 IO 副作用（除模型文件读取）。
  · 防抖与状态迁移由 state_machine.MarketStateMachine 负责；本模块不持久化任何东西。
  · **绝不改变交易行为**：本模块的产物在 shadow 阶段只落库/上屏（见 scheduler._run_shadow_state）。

降级链（逐级显式，任一级失败都不影响信号塔主流程）：
  lightgbm 缺失 / 模型文件缺失      → ok=False reason=no_model
  模型特征列与本契约不一致          → ok=False reason=contract_mismatch（拒绝加载，防静默错列）
  K 线不足 / 特征非有限值           → ok=False reason=<具体原因>
  置信度不足（max_prob < min_conf） → ok=True decided=False（**不参与防抖**，见方案 §5 补充）

配置键（生产以 PG/Redis 为准，下列仅兜底；`{tf}` 为周期占位）：
  state.enabled            总开关（默认 False → 完全不加载）
  state.model_dir          模型目录（默认 /app/review_models，与 reviewer 同挂载点）
  state.model_path.{tf}    显式模型路径；留空则按 state.model_dir 内
                           `lgbm_state_{tf}_v{N}_s*.txt` 自动选最高版本、聚合全部种子
  state.min_conf           单根判定最低置信（默认 0.45）
"""

from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .state_features import (
    STATE_FEATURE_COLS,
    STATE_FEATURE_COLS_L1,
    check_feature_contract,
    compute_features_at,
    compute_indicators,
    DEFAULT_PARAMS,
)

logger = logging.getLogger(__name__)

DEFAULTS: dict = {
    "state.enabled": False,
    "state.model_dir": "/app/review_models",
    "state.min_conf": 0.45,
}

# 类别顺序（必须与 tools/build_state_labels.STATE_NAMES / 模型 num_class 一致，契约）
STATE_NAMES = ["oscillation", "trend_init", "trend_mid", "trend_fade"]

_MODEL_RX = r"lgbm_state_([A-Z0-9]+)_v(\d+)_s(\d+)\.txt$"
# 起点模型命名（tools/train_onset_model.py 的产物）；刻意与 4 类模型分开匹配，防混用
_ONSET_RX = r"lgbm_onset_([A-Z0-9]+)_v(\d+)_s(\d+)\.txt$"
_ONSET_META_RX = r"lgbm_onset_([A-Z0-9]+)_v(\d+)_meta\.json$"
# 波动扩张模型命名（`tools/train_onset_model.py --target vol` 的产物）。
# **必须与起点模型分开匹配**：两者语义不同（起点 vs 波动扩张），不可互相加载。
_VOL_RX = r"lgbm_vol_([A-Z0-9]+)_v(\d+)_s(\d+)\.txt$"
_VOL_META_RX = r"lgbm_vol_([A-Z0-9]+)_v(\d+)_meta\.json$"


@dataclass
class StateInferResult:
    """单根 K 线状态判定结果（纯值对象）。"""
    ok: bool = False                 # 是否成功产出判定（含低置信）
    decided: bool = False            # ok 且置信达标 → 应参与防抖
    state: Optional[str] = None      # 类别名（STATE_NAMES 之一）
    proba: dict = field(default_factory=dict)   # 类别 → 概率
    margin: float = 0.0              # top1 - top2
    model_version: str = ""
    reason: str = ""
    # 本次入模的特征（27 维）。供策略层复用（slope_linreg / atr_14 等）与面板调试，
    # 避免调用方重新计算特征（保证策略与模型看到**同一份**世界状态）。
    feats: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "decided": self.decided, "state": self.state,
            "proba": self.proba, "margin": round(self.margin, 6),
            "model_version": self.model_version, "reason": self.reason,
        }


class StateInferer:
    """行情状态模型推理器（按周期各自独立加载，**禁止跨周期混用**）。"""

    def __init__(self, config_provider: Any = None, model_dir: Optional[str] = None):
        self._config = config_provider
        self._model_dir = model_dir or DEFAULTS["state.model_dir"]
        self._enabled = bool(DEFAULTS["state.enabled"])
        self._min_conf = float(DEFAULTS["state.min_conf"])
        self._explicit_path: dict[str, str] = {}
        # 每个周期一套：{tf: {"boosters": [...], "names": [...], "version": str}}
        self._loaded: dict[str, dict] = {}
        self._load_failed: dict[str, str] = {}
        self._warned_no_lgbm = False
        # ── 起点模型（触发器用；与 4 类模型独立加载，任一失败不影响另一个）──
        self._onset_loaded: dict[str, dict] = {}
        self._onset_failed: dict[str, str] = {}
        # ── 波动扩张模型（路线 B；与上面两组独立加载，任一失败不影响其余）──
        self._vol_loaded: dict[str, dict] = {}
        self._vol_failed: dict[str, str] = {}

    # ── 配置 ───────────────────────────────────────────────
    async def load_config(self) -> None:
        """热加载配置；异常仅告警，保留上次/默认值。"""
        if self._config is None:
            return
        try:
            self._enabled = await self._config.get_bool("state.enabled", self._enabled)
            self._model_dir = (await self._config.get(
                "state.model_dir", self._model_dir)) or self._model_dir
            self._min_conf = await self._config.get_float("state.min_conf", self._min_conf)
            for tf in ("M5", "M15", "H1"):
                p = (await self._config.get(f"state.model_path.{tf}", "")
                     or "").strip()
                if p:
                    self._explicit_path[tf] = p
                else:
                    self._explicit_path.pop(tf, None)
            # 仅当配置**实际变化**时打 INFO：_config_reload_loop 每 30s 调一次，
            # 每次打 INFO 会产生约 3k 行/天噪音；不变时降 DEBUG（仍可回溯）。
            _sig = (self._enabled, self._model_dir, round(self._min_conf, 6),
                    tuple(sorted(self._explicit_path.items())))
            if _sig != getattr(self, "_cfg_sig", None):
                self._cfg_sig = _sig
                logger.info(
                    "StateInferer config loaded | enabled=%s dir=%s min_conf=%.3f "
                    "explicit_paths=%s",
                    self._enabled, self._model_dir, self._min_conf,
                    self._explicit_path or "{}",
                )
            else:
                logger.debug("StateInferer config unchanged")
        except Exception as exc:  # pragma: no cover
            logger.warning("StateInferer config load failed (using defaults): %s", exc)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def min_conf(self) -> float:
        return self._min_conf

    @property
    def min_bars(self) -> int:
        """推理所需最少已收盘 bar（调用方据此决定是否补取更多 K 线）。

        注意：scheduler._fetch_klines 默认只取 100 根，而本特征集需要 ~122 根
        （percentile_window=120 等）→ 调用方必须按本值判断补取，否则恒 insufficient_bars。
        """
        return _min_bars(DEFAULT_PARAMS)

    # ── 模型发现与加载 ─────────────────────────────────────
    def _discover(self, tf: str) -> tuple[list[str], str]:
        """返回 (模型文件列表[按种子排序], 版本名)。显式路径优先。

        仅匹配本方案的命名 `lgbm_state_{tf}_v{N}_s{N}.txt`——
        刻意**不会**误取既有诊断头 `lgbm_state.pkl`（类别语义不同，见方案 §14.5）。
        """
        explicit = self._explicit_path.get(tf)
        if explicit:
            if not os.path.exists(explicit):
                return [], ""
            base = os.path.basename(explicit)
            m = re.search(_MODEL_RX, base)
            return [explicit], (f"v{m.group(2)}" if m else "explicit")
        pattern = os.path.join(self._model_dir, f"lgbm_state_{tf}_v*_s*.txt")
        files = [p for p in glob.glob(pattern) if re.search(_MODEL_RX, os.path.basename(p))]
        if not files:
            return [], ""
        versions: dict[int, list[str]] = {}
        for p in files:
            m = re.search(_MODEL_RX, os.path.basename(p))
            versions.setdefault(int(m.group(2)), []).append(p)
        best = max(versions)
        return sorted(versions[best]), f"v{best}"

    def load_models(self, tf: str, force: bool = False) -> bool:
        """按周期惰性加载（幂等）。失败静默降级，绝不抛出。

        【失败分类·关键】只把**永久性失败**记入 `_load_failed` 以避免每根 bar 重试：
          · no_model / contract_mismatch / load_failed —— 需人工换文件才会变
        **环境性失败（lightgbm 未安装）刻意不缓存**：容器内补装依赖后无需重启进程，
        下一根 bar 的 import 即可生效（Python 不缓存失败的 import）。
        """
        if not force and (tf in self._loaded or tf in self._load_failed):
            return tf in self._loaded
        paths, version = self._discover(tf)
        if not paths:
            self._load_failed[tf] = "no_model"
            logger.warning("[state_infer] %s 未找到模型（dir=%s）→ 推理不可用",
                           tf, self._model_dir)
            return False
        try:
            import lightgbm as lgb  # 延迟导入：容器缺该依赖时不影响信号塔启动
        except Exception as exc:  # noqa: BLE001
            # 不记入 _load_failed：环境补齐后自动恢复（见 docstring）
            if not self._warned_no_lgbm:
                logger.warning(
                    "[state_infer] lightgbm 不可用（%s）→ 状态机推理暂不可用；"
                    "容器内补齐依赖后自动恢复，无需重启进程", exc)
                self._warned_no_lgbm = True
            return False
        try:
            boosters = [lgb.Booster(model_file=p) for p in paths]
        except Exception as exc:  # noqa: BLE001
            self._load_failed[tf] = f"load_failed:{exc}"
            logger.error("[state_infer] %s 模型加载失败：%s", tf, exc)
            return False
        # 特征契约比对（fail-fast：列不一致 → 拒绝使用，防静默错列）
        diff = check_feature_contract(boosters[0].feature_name())
        if diff:
            self._load_failed[tf] = f"contract_mismatch:{diff}"
            logger.error(
                "[state_infer] %s 模型特征列与本契约不一致 %s → 拒绝加载"
                "（训练/推理特征契约必须同源，见 state_features.py）", tf, diff)
            return False
        self._loaded[tf] = {"boosters": boosters, "version": version, "files": paths}
        self._load_failed.pop(tf, None)
        logger.info("[state_infer] %s 模型已加载 version=%s seeds=%d min_conf=%.3f",
                    tf, version, len(boosters), self._min_conf)
        return True

    # ── 推理 ───────────────────────────────────────────────
    def infer(
        self,
        tf: str,
        high: Any,
        low: Any,
        close: Any,
        open_epoch_s: Any = None,
        params: Optional[dict] = None,
    ) -> StateInferResult:
        """对**最后一根已收盘 bar** 做 4 类判定（同步、亚毫秒；LightGBM predict）。

        Args:
            tf: 周期（M5/M15/H1）—— 必须与建仓/训练周期一致，禁止跨周期混用。
            high/low/close: 已收盘 K 线序列（numpy 数组或等长 list）。
            open_epoch_s: 各 bar 的 open_time（Unix 秒 UTC），用于时段哑变量；可 None。
            params: 特征参数覆盖（默认 state_features.DEFAULT_PARAMS）。

        Returns:
            StateInferResult（失败时 ok=False，reason 说明原因；不抛异常）。
        """
        if not self._enabled:
            return StateInferResult(reason="disabled")
        if not self.load_models(tf):
            return StateInferResult(reason=self._load_failed.get(tf, "no_model"))

        p = dict(DEFAULT_PARAMS)
        if params:
            p.update({k: v for k, v in params.items() if v is not None})

        try:
            import numpy as np
            hi = np.asarray(high, dtype=float)
            lo = np.asarray(low, dtype=float)
            cl = np.asarray(close, dtype=float)
            ep = None if open_epoch_s is None else np.asarray(open_epoch_s)
        except Exception as exc:  # noqa: BLE001
            return StateInferResult(reason=f"input_invalid:{exc}")

        min_bars = _min_bars(p)
        if len(cl) < min_bars:
            return StateInferResult(reason="insufficient_bars")
        if not (len(hi) == len(lo) == len(cl)):
            return StateInferResult(reason="length_mismatch")

        ind = compute_indicators(hi, lo, cl, p)
        feat = compute_features_at(len(cl) - 1, hi, lo, cl, ind, ep, p)
        if feat is None:
            # 特征缺失/非有限值 → 按方案 §5 判"推理失败"（由 FSM 决定保持态或 S9）
            return StateInferResult(reason="bad_features")

        loaded = self._loaded[tf]
        try:
            import pandas as pd
            x = pd.DataFrame([{k: float(feat[k]) for k in STATE_FEATURE_COLS}])
            proba = None
            for b in loaded["boosters"]:
                pr = b.predict(x)
                proba = pr if proba is None else proba + pr
            proba = proba / len(loaded["boosters"])
            row = [float(v) for v in proba[0]]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[state_infer] %s 预测失败：%s", tf, exc)
            return StateInferResult(reason=f"predict_failed:{exc}")

        order = sorted(range(len(row)), key=lambda i: -row[i])
        top, second = row[order[0]], row[order[1]]
        margin = top - second
        res = StateInferResult(
            ok=True,
            decided=top >= self._min_conf,
            state=STATE_NAMES[order[0]],
            proba={STATE_NAMES[i]: round(row[i], 6) for i in range(len(row))},
            margin=margin,
            model_version=loaded["version"],
            reason="ok" if top >= self._min_conf else "low_conf",
            feats=dict(feat),
        )
        return res


    # ── 起点模型（趋势起点触发器；见 trend_trigger.py docstring 的实测依据）──
    def load_onset_models(self, tf: str) -> bool:
        """按周期加载起点模型 + 读 meta 里的决策阈值。失败静默降级。"""
        if tf in self._onset_loaded or tf in self._onset_failed:
            return tf in self._onset_loaded
        pattern = os.path.join(self._model_dir, f"lgbm_onset_{tf}_v*_s*.txt")
        files = [p for p in glob.glob(pattern)
                 if re.search(_ONSET_RX, os.path.basename(p))]
        if not files:
            self._onset_failed[tf] = "no_onset_model"
            logger.warning("[state_infer] %s 未找到起点模型（dir=%s）→ 触发器仅剩突破分支",
                           tf, self._model_dir)
            return False
        versions: dict[int, list[str]] = {}
        for p in files:
            m = re.search(_ONSET_RX, os.path.basename(p))
            versions.setdefault(int(m.group(2)), []).append(p)
        best = max(versions)
        paths = sorted(versions[best])
        try:
            import lightgbm as lgb
        except Exception:  # noqa: BLE001
            return False
        try:
            boosters = [lgb.Booster(model_file=p) for p in paths]
        except Exception as exc:  # noqa: BLE001
            self._onset_failed[tf] = f"onset_load_failed:{exc}"
            logger.error("[state_infer] %s 起点模型加载失败：%s", tf, exc)
            return False
        diff = check_feature_contract(boosters[0].feature_name())
        if diff:
            self._onset_failed[tf] = f"onset_contract_mismatch:{diff}"
            logger.error("[state_infer] %s 起点模型特征列不一致 %s → 拒绝加载", tf, diff)
            return False
        # 阈值来自训练产物 meta（OOF 标定），缺失则回退 trend_trigger 默认
        thr = None
        try:
            import json as _json
            meta_p = os.path.join(self._model_dir,
                                  f"lgbm_onset_{tf}_v{best}_meta.json")
            if os.path.exists(meta_p):
                with open(meta_p, "r", encoding="utf-8") as fh:
                    thr = float(_json.load(fh).get("threshold") or 0.0) or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[state_infer] %s 起点模型 meta 读取失败：%s", tf, exc)
        self._onset_loaded[tf] = {"boosters": boosters, "version": f"v{best}",
                                  "files": paths, "threshold": thr}
        self._onset_failed.pop(tf, None)
        logger.info("[state_infer] %s 起点模型已加载 version=v%s seeds=%d",
                    tf, best, len(boosters))
        return True

    def infer_onset_tail(self, tf: str, high: Any, low: Any, close: Any,
                         tail: int = 8) -> Optional[list]:
        """返回 P(起点) 的**末尾 tail 根**（供 ΔP 判据用），失败返回 None。

        为什么只要末尾几根：`trend_trigger` 的 rise 判据只用 P[t] 与 P[t−m]，
        故无需对全历史重算（每根 bar 全段 predict 是无谓开销）。
        """
        if not self.load_onset_models(tf):
            return None
        p = dict(DEFAULT_PARAMS)
        try:
            import numpy as np
            hi = np.asarray(high, dtype=float)
            lo = np.asarray(low, dtype=float)
            cl = np.asarray(close, dtype=float)
        except Exception:  # noqa: BLE001
            return None
        n = len(cl)
        if n < _min_bars(p) or not (len(hi) == len(lo) == n):
            return None
        t = max(2, int(tail))
        idx = list(range(n - t, n))
        ind = compute_indicators(hi, lo, cl, p)
        rows, ok_idx = [], []
        for i in idx:
            feat = compute_features_at(i, hi, lo, cl, ind, None, p)
            if feat is not None:
                rows.append({k: float(feat[k]) for k in STATE_FEATURE_COLS})
                ok_idx.append(i)
        if not rows:
            return None
        try:
            import pandas as pd
            x = pd.DataFrame(rows)
            loaded = self._onset_loaded[tf]
            pr = None
            for b in loaded["boosters"]:
                q = b.predict(x)
                pr = q if pr is None else pr + q
            pr = pr / len(loaded["boosters"])
            # ⚠ 二分类 Booster.predict 返回**一维**（正类概率），不是 (n,2)。
            #   原写法 `pr.shape[1]` 会 IndexError → 被 except 吞掉 → **rise 分支静默失效**
            #   （实测踩到）。故显式分支处理两种返回形状。
            if getattr(pr, "ndim", 1) == 1:
                vals = [float(v) for v in pr]
            else:
                col = 1 if pr.shape[1] > 1 else 0
                vals = [float(v) for v in pr[:, col]]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[state_infer] %s 起点概率计算失败：%s", tf, exc)
            return None
        # 对齐到末尾：缺失处用 nan 占位（rise 判据会因非有限而判 False）
        out = [float("nan")] * t
        for k, i in enumerate(ok_idx):
            out[t - (n - i)] = vals[k]
        return out

    @property
    def onset_threshold(self) -> Optional[float]:
        """当前已加载起点模型的阈值（mape 自 meta；未加载则 None）。"""
        for v in self._onset_loaded.values():
            if v.get("threshold"):
                return float(v["threshold"])
        return None

    # ── 波动扩张模型（路线 B：判"未来窗口振幅/ATR 是否 ≥ 阈值"）──────────────────
    # 依据（2026-09-16 实测，全部在 `_scratch/state_M5_l1.csv` 上走前式 OOF）：
    #   · 同一特征集在"形态/起点"任务上 AUC **0.554**，在"波动扩张"任务上 **0.6465**
    #     （base 0.6210）⇒ 换目标 +9.2pt，且单特征 atr_14 仅 0.5732 ⇒ 非平凡可预测；
    #   · 验收门（真值 = 波动扩张上升沿）：本模型 **漏检 91 / 误报 11.6% / 中位 +0.5**，
    #     而"当期波动分位"平凡规则为 **漏检 547 / 中位 +12.0（滞后 12 根）**
    #     ⇒ **它把"滞后 12 根的波动确认"变成"同步预警"**（这正是本迭代要解决的指标滞后）。
    # 特征集为 **base + L1（量价/点差）** ⇒ 本方法需要 volume/spread；
    #   而 `scheduler._fetch_klines` 的 SELECT **本就包含** `tick_volume, spread` ⇒ 直接透传。
    # 调用方契约（重要）：**返回 None ⇒ 不裁决**，不得当作 0 用
    #   —— 0 会被上游读成"波动收敛"而**放行**箱体单，语义反向。
    def load_vol_models(self, tf: str) -> bool:
        """按周期加载波动扩张模型。失败静默降级（返回 False，调用方不裁决）。"""
        if tf in self._vol_loaded or tf in self._vol_failed:
            return tf in self._vol_loaded
        pattern = os.path.join(self._model_dir, f"lgbm_vol_{tf}_v*_s*.txt")
        files = [p for p in glob.glob(pattern)
                 if re.search(_VOL_RX, os.path.basename(p))]
        if not files:
            self._vol_failed[tf] = "no_vol_model"
            logger.warning("[state_infer] %s 未找到波动扩张模型（dir=%s）→ 波动闸不启用",
                           tf, self._model_dir)
            return False
        versions: dict[int, list[str]] = {}
        for p in files:
            m = re.search(_VOL_RX, os.path.basename(p))
            versions.setdefault(int(m.group(2)), []).append(p)
        best = max(versions)
        paths = sorted(versions[best])
        try:
            import lightgbm as lgb
        except Exception:  # noqa: BLE001
            return False
        try:
            boosters = [lgb.Booster(model_file=p) for p in paths]
        except Exception as exc:  # noqa: BLE001
            self._vol_failed[tf] = f"vol_load_failed:{exc}"
            logger.error("[state_infer] %s 波动扩张模型加载失败：%s", tf, exc)
            return False
        # 允许 base 或 base+L1 两种集合（**显式声明**，见 check_feature_contract 的 allowed）
        diff = check_feature_contract(
            boosters[0].feature_name(),
            allowed=(STATE_FEATURE_COLS, STATE_FEATURE_COLS_L1))
        if diff:
            self._vol_failed[tf] = f"vol_contract_mismatch:{diff}"
            logger.error("[state_infer] %s 波动模型特征列不一致 %s → 拒绝加载", tf, diff)
            return False
        self._vol_loaded[tf] = {"boosters": boosters, "version": f"v{best}",
                                "files": paths,
                                "cols": list(boosters[0].feature_name())}
        self._vol_failed.pop(tf, None)
        logger.info("[state_infer] %s 波动扩张模型已加载 version=v%s seeds=%d cols=%d",
                    tf, best, len(boosters), len(boosters[0].feature_name()))
        return True

    def infer_vol_proba(self, tf: str, high: Any, low: Any, close: Any,
                        volume: Any = None, spread: Any = None) -> Optional[float]:
        """**最后一根已收盘 bar** 的 P(波动扩张)。不可用返回 None（调用方须"不裁决"）。

        为什么只算最后一根：接入点是"本根是否允许开箱体单"，不需要全历史概率
        （与 `infer_onset_tail` 的取舍同理，避免无谓开销）。
        """
        if not self.load_vol_models(tf):
            return None
        p = dict(DEFAULT_PARAMS)
        try:
            import numpy as np
            hi = np.asarray(high, dtype=float)
            lo = np.asarray(low, dtype=float)
            cl = np.asarray(close, dtype=float)
        except Exception:  # noqa: BLE001
            return None
        n = len(cl)
        if n < _min_bars(p) or not (len(hi) == len(lo) == n):
            return None
        vol = None if volume is None else np.asarray(volume, dtype=float)
        spr = None if spread is None else np.asarray(spread, dtype=float)
        loaded = self._vol_loaded[tf]
        if any(c in loaded["cols"] for c in ("vol_ratio", "vol_chg", "vol_pctile",
                                             "spread_atr", "spread_pctile",
                                             "vol_price_align")) and (vol is None or spr is None):
            # 模型需要 L1 量价/点差列，但调用方没给数组 ⇒ 明确降级，不猜数值
            logger.warning("[state_infer] %s 波动模型需要 volume/spread，但调用方未提供"
                           " → 本根不裁决", tf)
            return None
        ind = compute_indicators(hi, lo, cl, p)
        feat = compute_features_at(n - 1, hi, lo, cl, ind, None, p,
                                   volume=vol, spread=spr)
        if feat is None:
            return None
        missing = [c for c in loaded["cols"] if c not in feat]
        if missing:
            logger.warning("[state_infer] %s 波动模型需要但特征缺失 %s → 本根不裁决",
                           tf, missing)
            return None
        try:
            import pandas as pd
            x = pd.DataFrame([{c: float(feat[c]) for c in loaded["cols"]}])
            pr = None
            for b in loaded["boosters"]:
                q = b.predict(x)
                pr = q if pr is None else pr + q
            return float((pr / len(loaded["boosters"]))[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[state_infer] %s 波动扩张概率计算失败：%s", tf, exc)
            return None


def _min_bars(params: dict) -> int:
    """所需最少已收盘 bar（与 state_features.min_bars 同源，避免重复常量）。"""
    from .state_features import min_bars as _mb
    return _mb(params)
