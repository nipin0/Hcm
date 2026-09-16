"""reviewer.py — 【P2 2026-09-11】信号级评审器（scheduler 进程内，亚毫秒）。

依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §3

职责边界（铁律 5.1，务必遵守）：
  · AI **无独立开仓权**：评审只有 PASS / DOWNGRADE / VETO 三档，
    "评审合格后触发下单" = 放行信号塔**已产出**的信号，AI 自己不产生信号、不反向开仓。
  · 评审**只读**，不修改 HEXP 方向（方向恒由 HEXP 决定）。

设计要点：
  · 进程内 LightGBM 推理（<1ms）。特征顺序**从模型自身 `feature_name()` 读取**，
    因此容器无需挂载训练侧的特征契约代码，只需模型文件目录 + lightgbm 依赖。
  · 降级链逐级显式（方案 §3.4）：模型缺失/特征缺失/超时/校准退化 → pass_through
    （纯 HEXP 放行），绝不影响信号流。
  · 默认全关（ai.review.enabled=false）；mode=shadow 时只记录不拦截。
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── 配置默认值（全部经 config_provider 双写；生产以 PG/Redis 为准）──
DEFAULTS: dict = {
    "ai.review.enabled": False,
    "ai.review.mode": "shadow",            # shadow(只记录) / canary(只 DOWNGRADE) / active(含 VETO)
    "ai.review.model_dir": "/app/review_models",
    # 【F 2026-09-11】shadow 专用模型目录（空 → 回落 model_dir）。允许指向 staging
    # 候选以产出 review_log；canary/active **不读** 此键（§6.1 闸门语义不被破坏）。
    "ai.review.shadow_model_dir": "",
    "ai.review.w_entry": 0.6,
    "ai.review.w_quality": 0.4,
    "ai.review.pass_threshold": 0.45,
    "ai.review.veto_floor": 0.15,
    "ai.review.dir_conflict_prob": 0.65,
    "ai.review.dir_conflict_action": "downgrade",   # downgrade / veto
    "ai.review.max_feat_missing": 0.3,
    "ai.review.calib_min_levels": 8,
    # 【§3.4 降级链补齐 2026-09-11】此前三键缺失 → FEAT_STALE/TIMEOUT 未实现、
    # ai.review.calib_min_levels 沦为死配置（全仓无引用）。现补齐：
    "ai.review.timeout_ms": 50,             # 评审硬超时（超时 → pass_through + TIMEOUT）
    "ai.review.fallback": "pass_through",   # 降级动作（当前实现固定 pass_through）
    "ai.review.feat_max_age_bars": 2,       # 特征快照最大滞后（M5 根数；1 根容差 + 1）
}

_GRADE_RANK = {"S": 4, "A": 3, "B": 2, "C": 1}


@dataclass
class ReviewInput:
    """评审输入（方案 §3.1）：信号属性 + 市场特征快照。"""
    symbol: str
    direction: str                      # BUY / SELL（HEXP 决定）
    entry_price: float
    sl_price: float
    tp_price: float
    hp_score: float
    grade: str
    session: str
    regime: str
    signal_mode: str
    feats: dict                          # 市场特征快照（含训练契约所需的键）
    signal_id: Optional[int] = None
    feat_bar_time: Optional[str] = None


@dataclass
class ReviewVerdict:
    action: str = "pass_through"         # PASS / DOWNGRADE / VETO / pass_through
    review_score: Optional[float] = None
    dir_prob: Optional[float] = None
    dir_pred: Optional[str] = None
    entry_prob: Optional[float] = None
    quality_prob: Optional[float] = None
    # 【P3 2026-09-11 在线滚动校准】原始（未校准）概率：重拟合 isotonic 必须用原始分
    # （对"已校准分"再拟合是退化操作）。落 review_log.extra 供离线重校准消费。
    entry_prob_raw: Optional[float] = None
    quality_prob_raw: Optional[float] = None
    dir_prob_raw: Optional[float] = None
    reason_codes: list = field(default_factory=list)
    model_version: str = ""
    calib_version: str = ""
    latency_ms: float = 0.0
    mode: str = "shadow"


def derive_dir_sign(direction: str) -> float:
    """信号方向 → ±1。与训练侧 tools/review_dataset.py::derive_dir_sign **同式**（契约）。"""
    d = (direction or "").strip().upper()
    return 1.0 if d == "BUY" else (-1.0 if d == "SELL" else 0.0)


class Reviewer:
    """信号级评审器。惰性加载；任一环节失败即整体降级为 pass_through。"""

    def __init__(self, model_dir: Optional[str] = None):
        self._model_dir = model_dir
        self._available = False
        self._loaded_dir: Optional[str] = None
        self._cols: list = []
        self._m_quality = None
        self._m_entry = None
        self._m_dir = None
        self._c_quality = None
        self._c_entry = None
        self._c_dir = None
        self._dir_classes = None
        self.model_version = ""
        self.calib_version = ""

    # ── 加载（幂等；失败静默，保持 pass_through）──
    def load(self, model_dir: Optional[str] = None) -> bool:
        md = model_dir or self._model_dir
        if not md:
            return False
        if self._available and self._loaded_dir == md:
            return True
        try:
            import lightgbm as lgb  # 延迟导入：容器缺该依赖时不影响信号塔启动
        except Exception as exc:  # noqa: BLE001
            logger.warning("[reviewer] lightgbm unavailable (%s) → pass_through", exc)
            return False
        try:
            q = os.path.join(md, "lgbm_review_quality.txt")
            e = os.path.join(md, "lgbm_review_entry.txt")
            d = os.path.join(md, "lgbm_review_direction.txt")
            if not (os.path.exists(q) and os.path.exists(e) and os.path.exists(d)):
                logger.info("[reviewer] models not found under %s → pass_through", md)
                return False
            self._m_quality = lgb.Booster(model_file=q)
            self._m_entry = lgb.Booster(model_file=e)
            self._m_dir = lgb.Booster(model_file=d)
            # 【P2 2026-09-11·关键性能修复】模型文件里存的是训练机的 num_threads(=24)，
            # 单行推理每次都要同步 24 个 OpenMP 线程 → 实测 16ms/次（三头 ~49ms）。
            # 单行推理设 num_threads=1 → 实测 0.088ms（约 180×），才是真正的"亚毫秒"。
            for _b in (self._m_quality, self._m_entry, self._m_dir):
                try:
                    _b.params["num_threads"] = 1
                except Exception:  # noqa: BLE001
                    pass
            # 特征顺序以质量头为权威（三头同契约 REVIEW_FEATURE_COLS）
            self._cols = list(self._m_quality.feature_name())
            self._dir_classes = list(getattr(self._m_dir, "classes_", None) or [-1, 0, 1])
            self._c_quality = self._load_calib(md, "quality")
            self._c_entry = self._load_calib(md, "entry")
            self._c_dir = self._load_calib(md, "direction")
            self._model_dir = md
            self._loaded_dir = md
            self._available = True
            self.model_version = os.path.basename(q)
            # 【§3.2 2026-09-11】calib_version = 校准器文件指纹（mtime），供 review_log
            # 按"模型+校准器"归因（原实现恒空 → 无法回看某次评审用的哪套校准器）。
            try:
                _mts = [os.path.getmtime(os.path.join(md, f"calib_review_{_h}.pkl"))
                        for _h in ("quality", "entry", "direction")
                        if os.path.exists(os.path.join(md, f"calib_review_{_h}.pkl"))]
                self.calib_version = f"c{int(max(_mts))}" if _mts else ""
            except Exception:  # noqa: BLE001
                self.calib_version = ""
            logger.info("[reviewer] loaded review models from %s (n_feats=%d)", md, len(self._cols))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[reviewer] load failed (%s) → pass_through", exc)
            self._available = False
            return False

    @staticmethod
    def _load_calib(md: str, head: str):
        p = os.path.join(md, f"calib_review_{head}.pkl")
        try:
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return pickle.load(f)
        except Exception:  # noqa: BLE001
            return None
        return None

    @property
    def available(self) -> bool:
        return self._available

    # ── 评审主流程（铁律：任何异常 → pass_through）──
    def review(self, inp: ReviewInput, cfg: dict) -> ReviewVerdict:
        t0 = time.perf_counter()
        _mode = str(cfg.get("ai.review.mode", "shadow")).strip().lower()
        v = ReviewVerdict(mode=_mode)
        try:
            # 【F 方案 2026-09-11】模型目录按模式选择 —— 打破「无数据→无法验证→
            # 无法达标→无法启用」死锁：
            #   shadow        : 优先 ai.review.shadow_model_dir（可指向 staging 候选）。
            #                   shadow 只记录、不拦单，用未验收候选是安全的（本义）。
            #   canary/active : **只**用 ai.review.model_dir —— 严格 §6.1 闸门语义，
            #                   未验收模型绝不进入可拦单的模式。
            _dir = cfg.get("ai.review.model_dir")
            if _mode == "shadow":
                _dir = cfg.get("ai.review.shadow_model_dir") or _dir
            if not self.load(str(_dir or self._model_dir or "")):
                v.reason_codes.append("MODEL_MISSING")
                return self._finish(v, t0)

            # 【2026-09-11 计时语义修正】模型加载（首次 ~450ms 磁盘 IO + Booster 构建）
            # **不计入**评审延迟：`ai.review.timeout_ms` 约束的是**推理计算**，不是加载。
            # 原实现把加载计入 → 每次重启/换目录后的首条信号都因 TIMEOUT 被降级为
            # pass_through，且脏耗时进入 latency_ms 统计（污染 P99 告警）。
            # 加载本身失败已有 MODEL_MISSING 保护，故此处重置计时是安全的。
            t0 = time.perf_counter()

            # 【§3.2 2026-09-11 修复】版本追溯：原实现 ReviewVerdict.model_version /
            # calib_version 恒为空 → review_log 无法归因（线上实测 model_version 列为空）。
            v.model_version = self.model_version
            v.calib_version = self.calib_version

            # 0) 特征快照新鲜度（方案 §3.1/§3.4）：快照过期 → pass_through + FEAT_STALE
            if inp.feat_bar_time and self._feat_stale(inp.feat_bar_time, cfg):
                v.reason_codes.append("FEAT_STALE")
                return self._finish(v, t0)

            # 1) 组装特征向量（含信号属性 dir_sign —— 训练/推理同式）
            feats = dict(inp.feats or {})
            feats["dir_sign"] = derive_dir_sign(inp.direction)
            miss = sum(1 for c in self._cols if feats.get(c) is None)
            if miss and (miss / max(len(self._cols), 1)) > float(cfg.get("ai.review.max_feat_missing", 0.3)):
                v.reason_codes.append("FEAT_MISSING")
                return self._finish(v, t0)
            x = [[float(feats.get(c) or 0.0) for c in self._cols]]

            # 2) 三头推理
            qp_raw = float(self._m_quality.predict(x, num_threads=1)[0])
            ep_raw = float(self._m_entry.predict(x, num_threads=1)[0])
            dp3 = self._m_dir.predict(x, num_threads=1)[0]
            import numpy as _np
            _i = int(_np.argmax(dp3))
            dp_raw = float(dp3[_i])
            _ml = int(cfg.get("ai.review.calib_min_levels", 8))
            v.quality_prob = self._cal(self._c_quality, qp_raw, _ml)
            v.entry_prob = self._cal(self._c_entry, ep_raw, _ml)
            v.dir_prob = self._cal(self._c_dir, dp_raw, _ml)
            v.quality_prob_raw = round(float(qp_raw), 6)
            v.entry_prob_raw = round(float(ep_raw), 6)
            v.dir_prob_raw = round(float(dp_raw), 6)
            _cls = self._dir_classes[_i] if _i < len(self._dir_classes) else 0
            v.dir_pred = "BUY" if _cls == 1 else ("SELL" if _cls == -1 else "HOLD")

            # 3) 整合评审分（买点头主权重 + 质量头辅权重；方向头只挡反向、不加分）
            w_e = float(cfg.get("ai.review.w_entry", 0.6))
            w_q = float(cfg.get("ai.review.w_quality", 0.4))
            s = (w_e * (v.entry_prob or 0.0) + w_q * (v.quality_prob or 0.0)) / max(w_e + w_q, 1e-9)
            v.review_score = round(float(s), 4)

            # 4) 裁决（方案 §3.3）
            _opp = {"BUY": "SELL", "SELL": "BUY"}
            if (v.dir_pred in ("BUY", "SELL")
                    and v.dir_pred == _opp.get((inp.direction or "").upper())
                    and (v.dir_prob or 0.0) >= float(cfg.get("ai.review.dir_conflict_prob", 0.65))):
                v.reason_codes.append("DIR_CONFLICT")
                v.action = ("VETO" if str(cfg.get("ai.review.dir_conflict_action", "downgrade"))
                            .lower() == "veto" else "DOWNGRADE")
            elif s < float(cfg.get("ai.review.veto_floor", 0.15)):
                v.action, _ = "VETO", v.reason_codes.append("LOW_SCORE")
            elif s < float(cfg.get("ai.review.pass_threshold", 0.45)):
                v.action, _ = "DOWNGRADE", v.reason_codes.append("BELOW_PASS")
            else:
                v.action = "PASS"

            # 5) 硬超时降级（方案 §3.4）：同步计算无法中途中断，故事后校验；
            #    超时 → 放弃本次裁决（pass_through），绝不带病拦单。
            _tmo = float(cfg.get("ai.review.timeout_ms", 50))
            if (time.perf_counter() - t0) * 1000.0 > _tmo:
                v.action = "pass_through"
                v.reason_codes.append("TIMEOUT")
            return self._finish(v, t0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[reviewer] review failed (%s) → pass_through", exc)
            v.action = "pass_through"
            v.reason_codes.append("REVIEW_ERROR")
            return self._finish(v, t0)

    @staticmethod
    def _cal_levels(cal) -> int:
        """校准器分辨率（方案 §3.4 退化判据）。

        【2026-09-11】优先调用校准器自身的 `level_count()`（单一真值，语义对齐）：
          · NumpyCalibrator(isotonic) → 阶梯数（受样本量约束）；
          · PlattCalibrator(参数化)  → [0,1] 网格上的输出分辨率（a≈0 → 1 判退化）。
        两者都表达「映射是否退化为常数」，故同一判据对两类校准器一致有效。
        回退：旧产物仅有 `y_fit`/`y_thresholds_` 属性 → 取其唯一值个数。
        无法判定 → 99（fail-open：宁可放行也不误降级）。
        """
        try:
            _lc = getattr(cal, "level_count", None)
            if callable(_lc):
                return int(_lc())
            import numpy as _np
            y = getattr(cal, "y_fit", None)
            if y is None:
                y = getattr(cal, "y_thresholds_", None)
            if y is None:
                return 99
            return int(len(_np.unique(_np.round(_np.asarray(y, float), 4))))
        except Exception:  # noqa: BLE001
            return 99

    @staticmethod
    def _feat_stale(bar_time, cfg: dict) -> bool:
        """特征快照是否过期（方案 §3.1：与信号 bar 同根，M5 容差由 feat_max_age_bars 控制）。

        解析失败一律返回 False（fail-open：不因时间格式问题误杀信号，缺失由
        FEAT_MISSING 兜底）。
        """
        try:
            import datetime as _dt
            if isinstance(bar_time, (int, float)):
                _bt = float(bar_time)
                if _bt > 1e11:              # 毫秒时间戳
                    _bt /= 1000.0
            else:
                _s = str(bar_time).strip().replace("Z", "+00:00")
                _d = _dt.datetime.fromisoformat(_s)
                if _d.tzinfo is None:
                    _d = _d.replace(tzinfo=_dt.timezone.utc)
                _bt = _d.timestamp()
            _max_bars = float(cfg.get("ai.review.feat_max_age_bars", 2))
            return (time.time() - _bt) > _max_bars * 300.0
        except Exception:  # noqa: BLE001
            return False

    def _cal(self, cal, p: float, min_levels: int = 8) -> Optional[float]:
        """校准；缺失 / 退化(档位<min_levels) / 异常 → 返回原始概率（方案 §3.4）。

        退化退用 raw 与 `ai.lm.calib_blend_w` 机制语义一致（quality_scorer.py:963-967）。
        """
        if cal is None:
            return round(float(p), 4)
        if self._cal_levels(cal) < min_levels:
            return round(float(p), 4)       # 校准器退化 → 退用 raw 概率
        try:
            out = cal.predict(float(p))
            try:  # NumpyCalibrator 可能返回标量/数组
                out = float(out[0])
            except (TypeError, IndexError):
                out = float(out)
            return round(out, 4)
        except Exception:  # noqa: BLE001
            return round(float(p), 4)

    @staticmethod
    def _finish(v: ReviewVerdict, t0: float) -> ReviewVerdict:
        v.latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        return v


# ── 留痕（P0 建表 hcm_ai.review_log；fail-open，绝不影响信号流）──
def _as_dt(v):
    """把 feat_bar_time 归一化为 datetime（或 None）。

    【2026-09-11 修复】线上实测：scheduler 传的是 AI 快照 `ts`（float epoch），
    而 `review_log.feat_bar_time` 列是 timestamp → asyncpg 拒绝 float：
        invalid input for query argument $12: 1789096205.65
        (expected a datetime.date or datetime.datetime instance, got 'float')
    导致 review_log **一条都写不进**（评审链路有数据但落库全丢）。此处统一转换：
    float/integer（秒或毫秒）→ UTC datetime；ISO 字符串 → datetime；不可解析 → None。
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        if isinstance(v, (int, float)):
            _t = float(v)
            if _t > 1e11:               # 毫秒时间戳
                _t /= 1000.0
            return datetime.fromtimestamp(_t, tz=timezone.utc)
        _s = str(v).strip().replace("Z", "+00:00")
        _d = datetime.fromisoformat(_s)
        return _d if _d.tzinfo else _d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


async def log_review(db_pool, inp: ReviewInput, v: ReviewVerdict):
    """写 hcm_ai.review_log，返回新行 id（db_pool 为空或异常 → None）。

    【2026-09-11 signal_id 回填】改为返回行 id：评审发生在 `signal_id` 生成**之前**
    （scheduler 中 signal_id 在信号即将发布时才生成），故原实现 `signal_id` 恒 NULL
    （线上实测 49/49 全空）→ 在线校准 `review_recalibrate` 按 signal_id join labels
    时带 `AND signal_id IS NOT NULL`，**永远取不到数据**（③c 断链的真因）。
    现先落库占位并返回 id，scheduler 拿到真实 signal_id 后
    `UPDATE hcm_ai.review_log SET signal_id=$1 WHERE id=$2` 回填 ——
    既保留「每次评审全量落库」（方案 §3.5，含被拦信号），又让 join 键有值。
    """
    if db_pool is None:
        return None
    try:
        _row = await db_pool.fetchrow(
            "INSERT INTO hcm_ai.review_log "
            "(signal_id,symbol,direction,session,regime,signal_mode,entry_price,sl_price,"
            " tp_price,hp_score,grade,feat_bar_time,action,review_score,dir_prob,dir_pred,"
            " entry_prob,quality_prob,reason_codes,model_version,calib_version,latency_ms,mode,"
            " extra) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24::jsonb) "
            "RETURNING id",
            inp.signal_id, inp.symbol, inp.direction, inp.session, inp.regime, inp.signal_mode,
            inp.entry_price, inp.sl_price, inp.tp_price, inp.hp_score, inp.grade,
            _as_dt(inp.feat_bar_time), v.action, v.review_score, v.dir_prob, v.dir_pred,
            v.entry_prob, v.quality_prob, ",".join(v.reason_codes),
            v.model_version, v.calib_version, v.latency_ms, v.mode,
            # 【P3 2026-09-11】原始分落 extra：在线滚动校准用原始分重新拟合校准器
            json.dumps({"entry_raw": v.entry_prob_raw,
                        "quality_raw": v.quality_prob_raw,
                        "dir_raw": v.dir_prob_raw}, ensure_ascii=False),
        )
        return int(_row["id"]) if _row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[reviewer] log_review failed (non-fatal): %s", exc)
        return None
