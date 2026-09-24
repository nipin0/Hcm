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
  置信度不足（max_prob < min_conf，或 top1−top2 < min_margin） → ok=True decided=False（**不参与防抖**，见方案 §5 补充）

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
    # 单根判定最低「决策边际」(top1−top2)；0.0 = 不启用（仅靠 min_conf 单门）。
    # 四分类近均匀分布下 min_conf 拦噪乏力（top=0.36/margin=0.033 仍 decided），
    # 需在 argmax 之外再卡边际，过滤噪声决策（审计 P0-1 / M1+M2）。
    "state.min_margin": 0.0,
    # ── 【2026-09-19 阶段1】弃权闸（abstain）──────────────────────────────────
    # 判据与实测见 docs/方案_状态机判别力改进_20260919.md §8.2-D3、§8.4：
    #   · `margin` 与经济指标**无正相关**：S2_TREND_INIT 高置信档 −7.2bp/胜率 0.449
    #     vs 低置信档 +1.4bp/0.515 ⇒ **不能**做成"高置信才下单"（会挑出低波动 bar）。
    #   · split-conformal 实测（OOF n=31495）：覆盖率精确达标（95.0/90.0/80.0%），
    #     但**单例率仅 1.7%/4.3%/13.0%**，且**单例准确率仅 0.502/0.497/0.456**
    #     ⇒ 当前 4 类目标上"够格决策"的样本**并不比随机好**。
    # ⇒ 因此本闸**默认关闭**。启用前必须**同时**满足三条验收门（缺一不可）：
    #     ① `tools/train_state_model.py` 产物 meta 的 `conformal.*.singleton_acc` ≥ 0.80；
    #     ② 同 meta 的 `singleton_rate` ≥ 60%；
    #     ③ `market_state_log` 分层复算显示"置信度分桶 × 经济指标**单调递增**"。
    #   若三条不满足即启用 ⇒ 实测会**恶化**（见上）。
    "state.abstain.enabled": False,   # 总开关（False = 逐位保持既有行为）
    "state.abstain.min_conf": 0.0,    # 校准后置信下限（0.0 = 不启用该条件）
    "state.abstain.alpha": 0.0,       # conformal 显著性（0.0 = 不启用该条件）
    # ── 【P2 2026-09-19】vol 头（波动路由）的 conformal 显著性 ────────────────
    # 为什么与 `state.abstain.alpha` **分开**：两个头的目标与样本分布不同
    #   （多分类相位 vs 二分类波动扩张），"够格决策"的门槛理应可独立标定；
    #   共用一个键 ⇒ 调一个头会静默改变另一个头的行为（本仓库明令避免）。
    # 用途：`infer_vol_route()` 用它判定 vol 预测是否为**单例**
    #   ⇒ 供策略层 `state.vol.osc_skip_require_singleton`（仅高置信时才拦箱体单）。
    #   `0.0` = 不做单例判定（`singleton` 返回 None = 未知 ⇒ 调用方不得据此裁决）。
    # 实测标定参考（2026-09-19，OOF n=56995）：α=0.05 → 单例率 27.6% / 单例准确率 0.819；
    #   α=0.10 → 47.5% / 0.789；α=0.20 → 76.0% / 0.737（**两者此消彼长**，按需选）。
    "state.vol.alpha": 0.0,
}

# 类别顺序（必须与 tools/build_state_labels.STATE_NAMES / 模型 num_class 一致，契约）
STATE_NAMES = ["oscillation", "trend_init", "trend_mid", "trend_fade"]
# 【P2 2026-09-19】vol 头（二分类）的类别名 —— 契约与 `train_onset_model.ONSET_NAMES` 同序。
# 为什么另立常量而不复用 ONSET_NAMES：onset 与 vol 是**两个语义不同**的模型
#   （起点 vs 波动扩张），共用一个名字表会让"加载错模型"看起来正常（本仓库已踩过）。
VOL_NAMES = ["no_vol", "vol"]

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
    # ── 【2026-09-19 阶段1】弃权闸观测量（**仅为观测量**；`decided` 语义不变）──
    # `conf_cal`：isotonic 校准后的 top-1 置信（无校准器时 = raw top1）。
    # `pred_set`：split-conformal 预测集合（**空 = 未启用/样本不足 → 不裁决**）。
    #   `len(pred_set) == 1` 时其错误率有分布无关的 ≤ α 保证。
    # `abstain`：本 bar 建议**禁止新开/加仓**（消费方 = `state_strategy` 的入场分支，
    #   经 `scheduler` 透传；**它不改变 `decided`**，故 `low_conf_policy` 行为逐位不变）。
    conf_cal: float = 0.0
    pred_set: tuple = ()
    abstain: bool = False
    # 本次入模的特征（27 维）。供策略层复用（slope_linreg / atr_14 等）与面板调试，
    # 避免调用方重新计算特征（保证策略与模型看到**同一份**世界状态）。
    feats: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "decided": self.decided, "state": self.state,
            "proba": self.proba, "margin": round(self.margin, 6),
            "model_version": self.model_version, "reason": self.reason,
            # 【阶段1】弃权闸观测量（默认关闭时 conf_cal=raw top1、pred_set 为空、
            # abstain=False ⇒ 载荷仅多两个字段，不影响任何既有消费者）
            "conf_cal": round(self.conf_cal, 6),
            "abstain": self.abstain,
            "pred_set": list(self.pred_set),
        }


# ── 【2026-09-19 阶段1】弃权闸的两个纯函数（无状态，便于自证用例直接断言）──────────
def apply_calibrator(calib: Optional[dict], p: float) -> float:
    """isotonic 校准（用 `np.interp` 线性插值复现，**推理侧不依赖 sklearn**）。

    `calib` 来自 `tools/train_state_model.py` 落到 meta 的 `(x, y)` 阈值对。
    缺产物 / 异常 → **原样返回**（绝不猜数值：静默改变概率尺度会污染下游闸门）。
    注意：校准是**单调映射** ⇒ **不改变 argmax 与排序**，只让概率有绝对含义。
    """
    if not isinstance(calib, dict):
        return float(p)
    try:
        import numpy as np
        x = np.asarray(calib.get("x") or [], dtype=float)
        y = np.asarray(calib.get("y") or [], dtype=float)
        if x.size == 0 or x.size != y.size:
            return float(p)
        return float(np.interp(float(p), x, y))
    except Exception:  # noqa: BLE001
        return float(p)


def conformal_set(conformal: Optional[dict], alpha: float, row) -> list:
    """split-conformal 预测集合：`{k : p_k ≥ 1 − q̂}`（下标列表）。

    **空 list 有确切语义 = "不裁决"**（α 无对应分位/产物缺失/未启用）
    —— 与 `infer_vol_proba` 返回 None 的契约同取向：调用方不得把"空"读成"集合为空"。
    """
    if not isinstance(conformal, dict) or alpha <= 0.0:
        return []
    v = conformal.get(f"{alpha:.2f}")
    if not isinstance(v, dict):
        return []
    try:
        thr = float(v["thr"])
    except (KeyError, TypeError, ValueError):
        return []
    return [i for i, p in enumerate(row) if float(p) >= thr]


class StateInferer:
    """行情状态模型推理器（按周期各自独立加载，**禁止跨周期混用**）。"""

    def __init__(self, config_provider: Any = None, model_dir: Optional[str] = None):
        self._config = config_provider
        self._model_dir = model_dir or DEFAULTS["state.model_dir"]
        self._enabled = bool(DEFAULTS["state.enabled"])
        self._min_conf = float(DEFAULTS["state.min_conf"])
        self._min_margin = float(DEFAULTS["state.min_margin"])
        # 【2026-09-19 阶段1】弃权闸（默认关闭 ⇒ 逐位保持既有行为）
        self._abstain_enabled = bool(DEFAULTS["state.abstain.enabled"])
        self._abstain_min_conf = float(DEFAULTS["state.abstain.min_conf"])
        self._abstain_alpha = float(DEFAULTS["state.abstain.alpha"])
        # 【P2】vol 头的 conformal 显著性（与 4 类头解耦，见 DEFAULTS 同键注释）
        self._vol_alpha = float(DEFAULTS["state.vol.alpha"])
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
        # 【2026-09-19 晋升纪律】显式**版本钉**：{target: {tf: int}}，target ∈ state/onset/vol。
        #   为什么需要：三处选版此前一律 `best = max(versions)`（隐式）⇒ 「把训练产物
        #   放进模型目录」即等于上线（无人工确认环节）。版本钉使"选哪一版"成为
        #   **可见、可审计、可秒级回退**的配置项；缺键 = 沿用 max（零行为变化）。
        self._pinned: dict[str, dict[str, int]] = {"state": {}, "onset": {}, "vol": {}}

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
            self._min_margin = await self._config.get_float(
                "state.min_margin", self._min_margin)
            # 【阶段1】弃权闸三键（默认全关；配置中心缺键时回落 DEFAULTS）
            self._abstain_enabled = await self._config.get_bool(
                "state.abstain.enabled", self._abstain_enabled)
            self._abstain_min_conf = await self._config.get_float(
                "state.abstain.min_conf", self._abstain_min_conf)
            self._abstain_alpha = await self._config.get_float(
                "state.abstain.alpha", self._abstain_alpha)
            self._vol_alpha = await self._config.get_float(
                "state.vol.alpha", self._vol_alpha)
            for tf in ("M5", "M15", "H1"):
                p = (await self._config.get(f"state.model_path.{tf}", "")
                     or "").strip()
                if p:
                    self._explicit_path[tf] = p
                else:
                    self._explicit_path.pop(tf, None)
            # 【2026-09-19 晋升纪律】版本钉三键（缺键/空 = 沿用 `max(version)`，零行为变化）：
            #   `state.model_version.{tf}` / `state.onset_model_version.{tf}` /
            #   `state.vol_model_version.{tf}`，值形如 `2` 或 `v2`（容忍前缀）。
            # 非法值**忽略并告警**（不抛错：配置笔误不应让推理层停摆）。
            for tf in ("M5", "M15", "H1"):
                for _t, _key in (("state", f"state.model_version.{tf}"),
                                 ("onset", f"state.onset_model_version.{tf}"),
                                 ("vol", f"state.vol_model_version.{tf}")):
                    _raw = (await self._config.get(_key, "") or "").strip()
                    _v: Optional[int] = None
                    if _raw:
                        try:
                            _v = int(_raw.lstrip("vV"))
                        except ValueError:
                            logger.warning("[state_infer] %s 版本钉非法：%r → 忽略（沿用 max）",
                                           _key, _raw)
                    if _v is None:
                        self._pinned[_t].pop(tf, None)
                    else:
                        self._pinned[_t][tf] = _v
            # 仅当配置**实际变化**时打 INFO：_config_reload_loop 每 30s 调一次，
            # 每次打 INFO 会产生约 3k 行/天噪音；不变时降 DEBUG（仍可回溯）。
            _sig = (self._enabled, self._model_dir,
                    round(self._min_conf, 6), round(self._min_margin, 6),
                    self._abstain_enabled, round(self._abstain_min_conf, 6),
                    round(self._abstain_alpha, 6), round(self._vol_alpha, 6),
                    tuple(sorted(self._explicit_path.items())),
                    tuple(sorted((t, tuple(sorted(v.items())))
                                 for t, v in self._pinned.items())))
            if _sig != getattr(self, "_cfg_sig", None):
                self._cfg_sig = _sig
                logger.info(
                    "StateInferer config loaded | enabled=%s dir=%s min_conf=%.3f "
                    "min_margin=%.3f abstain(enabled=%s min_conf=%.3f alpha=%.2f) "
                    "vol_alpha=%.2f explicit_paths=%s pinned=%s",
                    self._enabled, self._model_dir, self._min_conf, self._min_margin,
                    self._abstain_enabled, self._abstain_min_conf, self._abstain_alpha,
                    self._vol_alpha, self._explicit_path or "{}",
                    {t: v for t, v in self._pinned.items() if v} or "{}",
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
    def _pick_version(self, target: str, tf: str, versions: dict) -> int:
        """【2026-09-19 晋升纪律】按**版本钉**选版；无钉/钉值不存在 ⇒ 回落 `max(versions)`。

        为什么钉值不存在时要**回落**而不是拒载：版本钉的用途是"把选择显式化"，
        不是新增一道"能否启动"的闸门 —— 一次配置笔误不应让整层推理停摆
        （与 `load_vol_models` 的 fail-safe 同取向）。
        但**回落必须可见**：打 WARNING（"配置了≠生效"是本仓库反复出现的盲区）。
        """
        pin = self._pinned.get(target, {}).get(tf)
        if pin is None:
            return max(versions)
        if pin in versions:
            return pin
        logger.warning("[state_infer] %s/%s 版本钉 v%s 不在可用版本 %s 中 → 回落 v%s",
                       target, tf, pin, sorted(versions), max(versions))
        return max(versions)

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
        best = self._pick_version("state", tf, versions)
        return sorted(versions[best]), f"v{best}"

    def _load_meta_artifacts(self, tf: str, version: str, prefix: str = "state"):
        """读训练产物 meta 里的**校准器**与 **conformal 分位**；缺失 → `(None, None)`。

        为什么必须容错而不是报错：这两块是【2026-09-19 阶段1】新增产物，
        既有线上 v5 模型的 meta **没有**它们 ⇒ 必须能原样加载（弃权闸退化为不可用），
        否则一次产物缺失就会让整个状态机推理失败（`load_models` 返回 False → S9）。

        `prefix`：【P2 2026-09-19】三类模型各有自己的 meta，命名随目标变化
        （`lgbm_{state|onset|vol}_{tf}_v{N}_meta.json`）。此前本方法写死 `state`
        ⇒ vol/onset 的产物永远读不到；注意**不可跨目标串读**（三类语义不同）。
        """
        import json as _json
        if not version.startswith("v"):
            return None, None
        path = os.path.join(self._model_dir, f"lgbm_{prefix}_{tf}_{version}_meta.json")
        if not os.path.exists(path):
            return None, None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                meta = _json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[state_infer] %s meta 读取失败（弃权闸不可用）：%s", tf, exc)
            return None, None
        calib = meta.get("calibration")
        if not (isinstance(calib, dict) and calib.get("x") and calib.get("y")):
            calib = None
        conf = meta.get("conformal")
        if not isinstance(conf, dict):
            conf = None
        return calib, conf

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
        # 【阶段1】顺带读 meta 里的校准器 / conformal 分位（缺失 ⇒ 弃权闸不可用，
        # **不抛错、不猜默认值**：既有 v5 模型没有这两块，必须能原样加载）。
        calib, conf = self._load_meta_artifacts(tf, version)
        self._loaded[tf] = {"boosters": boosters, "version": version, "files": paths,
                            "calib": calib, "conformal": conf}
        self._load_failed.pop(tf, None)
        logger.info("[state_infer] %s 模型已加载 version=%s seeds=%d min_conf=%.3f "
                    "calibration=%s conformal=%s",
                    tf, version, len(boosters), self._min_conf,
                    "有" if calib else "无", "有" if conf else "无")
        # 【阶段1】配置要求弃权、但产物缺失 ⇒ fail-closed 且**必须响**：
        #   否则线上表现为"系统忽然不下单"，排查方向会完全跑偏（此刻唯一线索就是这条日志）。
        if self._abstain_enabled and self._abstain_min_conf > 0.0 and calib is None:
            logger.warning(
                "[state_infer] %s 弃权闸要求校准器（state.abstain.min_conf=%.3f）但该模型 "
                "meta 无 calibration ⇒ 该条件 fail-closed（本根弃权）。请核对模型产物。",
                tf, self._abstain_min_conf)
        if self._abstain_enabled and self._abstain_alpha > 0.0 and conf is None:
            logger.warning(
                "[state_infer] %s 弃权闸要求 conformal（state.abstain.alpha=%.2f）但该模型 "
                "meta 无 conformal ⇒ 该条件 fail-closed（本根弃权）。请核对模型产物。",
                tf, self._abstain_alpha)
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
        decided = (top >= self._min_conf) and (margin >= self._min_margin)
        reason = ("ok" if decided
                  else "low_conf" if top < self._min_conf else "low_margin")

        # ── 【2026-09-19 阶段1】校准 + conformal 弃权（**默认关闭**）──────────────
        # 纪律：本段**不修改 `decided`** —— 否则会与 `low_conf_policy`（hold/decay）
        #   的既有语义纠缠，把"弃权"悄悄变成"低置信"的另一条路径。二者刻意正交：
        #   `decided` = 参与防抖的资格（旧机制，不变）；`abstain` = 本根禁新开（新机制）。
        conf_cal = apply_calibrator(loaded.get("calib"), top)
        pred_set = conformal_set(loaded.get("conformal"), self._abstain_alpha, row)
        abstain = False
        if self._abstain_enabled:
            # ① 校准后置信闸。⚠ 尺度错配陷阱：`min_conf` 是**校准后**的尺度，若模型 meta
            #    缺 `calibration`，拿 raw top1 去比就是两个尺度比较（结果无意义）⇒
            #    **fail-closed**（弃权）而不是"静默按 raw 比"。WARNING 在 `load_models` 打。
            if self._abstain_min_conf > 0.0:
                if loaded.get("calib") is None:
                    abstain, reason = True, "abstain_no_calibrator"
                elif conf_cal < self._abstain_min_conf:
                    abstain, reason = True, "abstain_low_conf"
            # ② conformal 集合闸（仅在①未弃权时评估）。集合 ≠ 单元素 ⇒ 弃权；
            #    产物缺失 / 集合为空 ⇒ 同样 **fail-closed**（无法评估 ⇒ 不冒险开新仓）。
            if not abstain and self._abstain_alpha > 0.0:
                if loaded.get("conformal") is None:
                    abstain, reason = True, "abstain_no_conformal"
                elif not pred_set:
                    abstain, reason = True, "abstain_empty_set"
                elif len(pred_set) != 1:
                    abstain, reason = True, "abstain_multi_class"

        res = StateInferResult(
            ok=True,
            decided=decided,
            state=STATE_NAMES[order[0]],
            proba={STATE_NAMES[i]: round(row[i], 6) for i in range(len(row))},
            margin=margin,
            model_version=loaded["version"],
            reason=reason,
            feats=dict(feat),
            conf_cal=conf_cal,
            pred_set=tuple(STATE_NAMES[i] for i in pred_set),
            abstain=abstain,
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
        best = self._pick_version("onset", tf, versions)
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
        best = self._pick_version("vol", tf, versions)
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
        # 【P2 2026-09-19】顺带读 vol 自己的 meta（校准器 + conformal）。
        # 缺产物 ⇒ 校准/单例判定不可用（路由退化为原始概率），**不抛错**。
        vcalib, vconf = self._load_meta_artifacts(tf, f"v{best}", prefix="vol")
        self._vol_loaded[tf] = {"boosters": boosters, "version": f"v{best}",
                                "files": paths,
                                "cols": list(boosters[0].feature_name()),
                                "calib": vcalib, "conformal": vconf}
        self._vol_failed.pop(tf, None)
        logger.info("[state_infer] %s 波动扩张模型已加载 version=v%s seeds=%d cols=%d "
                    "calibration=%s conformal=%s",
                    tf, best, len(boosters), len(boosters[0].feature_name()),
                    "有" if vcalib else "无", "有" if vconf else "无")
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

    # ── 【P2 2026-09-19】波动路由：校准后概率 + conformal 单例 ─────────────────
    def infer_vol_route(self, tf: str, high: Any, low: Any, close: Any,
                        volume: Any = None, spread: Any = None) -> Optional[dict]:
        """波动路由所需的**校准后** P(波动扩张) 与 conformal 单例判定。

        契约（与 `infer_vol_proba` 一致）：**不可用返回 None，调用方不得据此裁决**
        —— 不得把 None 读成"低波动"（那是语义反向，会误放行箱体单）。

        为什么不改 `infer_vol_proba` 而是另立方法：后者返回**原始概率**，其既有消费者
        （`state_strategy` 的 `state.vol.osc_skip_prob` 闸）的阈值语义建立在该尺度上；
        静默换尺度会让任何已标定阈值失效。故新增方法，由 `state.vol.alpha` 显式启用。

        依据（2026-09-19 准入判据 `tools/eval_vol_route_gate.py`，OOF n=56995）：
        校准后概率分桶的**未来振幅** 2.231 → 3.578 ATR（**桶间极差 1.347，单调**），
        优于平凡基线"当期 `atr_pct`"的 0.811（**+0.536 ATR**）⇒ 模型的波动分层有增量。

        返回 `{"p_raw","p_cal","singleton","pred_set"}`：
          · `p_cal` 无校准器时 = `p_raw`（**不猜数值**）；
          · `singleton` ∈ {True, False, **None**}；`state.vol.alpha <= 0` 或缺 conformal
            产物 ⇒ **None = 未知**（调用方必须据此"不裁决"，不得当成 False）。
        """
        p = self.infer_vol_proba(tf, high, low, close, volume=volume, spread=spread)
        if p is None:
            return None
        loaded = self._vol_loaded.get(tf) or {}
        p_cal = apply_calibrator(loaded.get("calib"), p)
        pred_set: list = []
        singleton: Optional[bool] = None
        if self._vol_alpha > 0.0:
            conf = loaded.get("conformal")
            if conf:
                pred_set = [VOL_NAMES[i] for i in
                            conformal_set(conf, self._vol_alpha, [1.0 - p, p])]
                singleton = len(pred_set) == 1
        return {"p_raw": float(p), "p_cal": float(p_cal),
                "singleton": singleton, "pred_set": pred_set}


def _min_bars(params: dict) -> int:
    """所需最少已收盘 bar（与 state_features.min_bars 同源，避免重复常量）。"""
    from .state_features import min_bars as _mb
    return _mb(params)
