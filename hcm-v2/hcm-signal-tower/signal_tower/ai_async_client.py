"""ai_async_client.py — DeepSeek 后台异步数据源（固定 3 输出，非实时决策依赖）。

产出 3 参数（系统依赖）：
  1. 真假概率 fake_prob     —— DeepSeek 对 HEXP 信号真假的独立判断（异步校准 C_ai）
  2. ai_sl_coeff            —— 自适应止损系数 0.8~1.5×ATR
  3. continuity_score       —— 延续分 0-100（持仓调仓依据）

纪律红线：
  - 仅后台异步刷新，绝不阻塞实时决策；超时/限流/失败 → 回退缓存或默认，系统降级纯 HEXP。
  - 默认 ai.ds.enabled=false。

固定输入特征（prompt 组装用）：hp_value/hp_abs/方向/k/行情模式、因子冲突数量、
六维评分卡（状态/入场/位置/时段/波动/共振）、六维总分、ATR、M1-MM、相对高低点距离。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from shared.llm_client import DeepSeekClient, parse_json_block

# 降级默认（禁硬编码：真实回退值从 ai.ds.* 配置读，此处仅兜底）
# 2026-08-15 安全修正：ai_sl_coeff 默认由 0.0 改为 1.0。原 0.0 会让
# scheduler 的 `ai_sl_mult *= ai_sl_coeff` 把 SL 倍数抹成 0 → 无止损(危险)。
# 缺失/异常应"不乘"(=1.0)，而非"清零"。
FALLBACK = {"fake_prob": None, "ai_sl_coeff": 1.0, "continuity_score": 50}

OUTPUT_KEYS = ("fake_prob", "ai_sl_coeff", "continuity_score")


def _num(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def build_prompt(snapshot: dict, symbol: str = "") -> str:
    """把 HEXP 快照装配成 DeepSeek 固定输入特征 prompt。

    2026-08-15 闭环补全：prompt 新增"外部因子"段（来自 market_intel 的
    composite/macro/sentiment/event/liquidity 0~1 分 + stub_mode）。让 DeepSeek
    在重大财经事件 / 外部风险骤升 / 流动性枯竭时：
      - 下调 fake_prob（信号真假存疑时更保守）
      - 放大 ai_sl_coeff（外部冲击下加宽止损）
    外部因子为 None（采集器未就绪/stub）时如实标注"未知"，不伪造。
    """
    fr = snapshot.get("factor_raws") or {}
    sc = snapshot.get("scorecard") or {}
    fc = snapshot.get("factor_scores") or {}

    def _conflict_count() -> int:
        """因子冲突数量：方向分符号不一致的因子数（>0 看多，<0 看空）。"""
        cnt = 0
        pos = 0
        for v in fc.values():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0.02:
                pos += 1
            elif f < -0.02:
                cnt += 1
        return min(pos, cnt)  # 冲突 = 多空因子对数

    ext = snapshot.get("external") or {}
    if isinstance(ext, dict):
        _evt = ext.get("event")
        _comp = ext.get("composite")
        _liq = ext.get("liquidity")
        _macro = ext.get("macro")
        _sent = ext.get("sentiment")
        _stub = ext.get("stub_mode")
        _ext_lines = [
            f"外部因子(composite={_comp} macro={_macro} sentiment={_sent} "
            f"event={_evt} liquidity={_liq})",
            f"外部因子采集状态={'STUB(占位/未采集)' if _stub else '真实'}",
            "解读：event 越高=重大财经事件窗口越近(信号风险越高)；"
            "liquidity 越低=流动性越枯竭(滑点/假突破风险越高)；"
            "composite 越高=外部综合风险越高。",
        ]
    else:
        _ext_lines = ["外部因子=未知(采集器未就绪)"]

    # 【2026-08-15 三处对齐-3a】注入 LightGBM 同构 25 维特征块：
    # sidecar(quality_scorer.py) 用相同数据源算出的 FEATURE_COLS，随
    # hcm:live:hexp:ai:{symbol}.lm_features 发布，scheduler 已并入快照。
    # DeepSeek 与 LightGBM 看到同一组世界状态 → calibrate 融合出的 c_ai 才可比。
    _lmf = snapshot.get("lm_features") or {}
    if isinstance(_lmf, dict) and _lmf:
        _lm_lines = [
            "LightGBM特征(与本地AI评分模型同构，25维，单位已对齐)：",
            f"  adx_14={_lmf.get('adx_14')} rsi_14={_lmf.get('rsi_14')} "
            f"macd={_lmf.get('macd')} atr_14={_lmf.get('atr_14')}",
            f"  h1_adx={_lmf.get('h1_adx')} h1_trend_strength={_lmf.get('h1_trend_strength')}",
            f"  plus_di={_lmf.get('plus_di')} minus_di={_lmf.get('minus_di')} "
            f"er={_lmf.get('er')} bbw={_lmf.get('bbw')} bbw_pct={_lmf.get('bbw_pct')}",
            f"  hurst={_lmf.get('hurst')} mm={_lmf.get('mm')} "
            f"ema20_dist_atr={_lmf.get('ema20_dist_atr')} body_ratio={_lmf.get('body_ratio')}",
            f"  pullback_depth={_lmf.get('pullback_depth')} atr_pct={_lmf.get('atr_pct')} "
            f"spread_num={_lmf.get('spread_num')} spread_atr={_lmf.get('spread_atr')}",
            f"  session(asia/eu/us)={_lmf.get('session_asia')}/{_lmf.get('session_eu')}/{_lmf.get('session_us')}",
            f"  event_proximity_min={_lmf.get('event_proximity_min')} "
            f"macro_risk_score={_lmf.get('macro_risk_score')} "
            f"sentiment_risk_score={_lmf.get('sentiment_risk_score')}",
            "说明：以上 25 维与本地 LightGBM 质量分(ai_score)所用特征完全一致；"
            "其中 event_proximity_min 越小=重大事件越近，macro/sentiment_risk_score 越大=风险越高，"
            "与上方'外部因子'段的0~1分同义但尺度不同，请综合判断。",
        ]
    else:
        _lm_lines = ["LightGBM特征=未知(sidecar未发布，仅作HEXP文本特征裁判)"]

    # H1 确认态 + grade：quality_gate 裁决依赖 direction/grade 门槛，
    # 让 DeepSeek 知道当前信号是否处于被拦截边缘，针对性收紧/放宽 fake_prob。
    _grade = snapshot.get("grade")
    _pstates = snapshot.get("period_states") or {}
    _h1_state = _pstates.get("H1") if isinstance(_pstates, dict) else None
    _h1_confirm = None
    if _h1_state in ("TREND_UP", "TREND_DOWN"):
        _h1_confirm = _h1_state

    lines = [
        "你是黄金 XAUUSD 信号质量裁判。基于以下和乘幂(HEXP)信号特征，输出 JSON 三参数。",
        f"symbol={symbol or 'XAUUSD'}",
        f"hp_score={snapshot.get('hp_score')} hp_abs={abs(float(snapshot.get('hp_score') or 0))}",
        f"direction={snapshot.get('direction')} k={snapshot.get('k')} grade={_grade}",
        f"H1趋势确认态={_h1_confirm} (TREND_UP=已确认看多/TREND_DOWN=已确认看空/None=未确认)",
        f"行情模式(period_states)={snapshot.get('period_states')}",
        f"因子原始值={fr}",
        f"因子冲突数量={_conflict_count()}",
        f"六维评分卡={sc} 六维总分={snapshot.get('scorecard_total')}",
        f"ATR={snapshot.get('atr')} M1-MM={snapshot.get('mm')} verdict={snapshot.get('verdict')}",
        *_ext_lines,
        *_lm_lines,
        "输出仅一个 JSON 对象，键固定为：",
        '{"fake_prob": <0~1 该HEXP信号为真的概率>, '
        '"ai_sl_coeff": <0.8~1.5 自适应止损ATR倍数>, "continuity_score": <0~100 趋势延续分>}',
    ]
    return "\n".join(lines)


def parse_output(text: str, cfg: dict) -> dict:
    data = parse_json_block(text) or {}
    out = dict(FALLBACK)
    if not isinstance(data, dict):
        return out
    if "fake_prob" in data:
        try:
            out["fake_prob"] = float(max(0.0, min(1.0, data["fake_prob"])))
        except (TypeError, ValueError):
            pass
    if "ai_sl_coeff" in data:
        lo = _num(cfg, "ai.ds.sl_coeff_min", 0.8)
        hi = _num(cfg, "ai.ds.sl_coeff_max", 1.5)
        try:
            out["ai_sl_coeff"] = float(max(lo, min(hi, data["ai_sl_coeff"])))
        except (TypeError, ValueError):
            # 2026-08-15 安全修正：解析失败回退 1.0(不乘)，而非 0.0(抹零 SL)
            out["ai_sl_coeff"] = 1.0
    if "continuity_score" in data:
        try:
            out["continuity_score"] = int(max(0, min(100, data["continuity_score"])))
        except (TypeError, ValueError):
            pass
    return out


def _llm_available(llm: Any) -> bool:
    """兼容 DeepSeekClient.is_available 既可能是 property 也可能是方法的两种形态。

    历史 bug：此处曾写 `llm.is_available()`，而 shared.llm_client.DeepSeekClient
    的 is_available 是 @property → 调用 bool 抛 TypeError，被顶层 except 吞掉，
    导致 DeepSeek 永久静默降级（fake_prob 恒 None）。
    """
    if llm is None:
        return False
    attr = getattr(llm, "is_available", False)
    try:
        return bool(attr() if callable(attr) else attr)
    except Exception:
        return False


async def produce_async_outputs(
    symbol: str, snapshot: dict, cfg: dict,
    llm: Optional[DeepSeekClient] = None,
) -> dict:
    """异步产出 3 参数；失败返回降级默认（调用方据此回退纯 HEXP）。"""
    if not _llm_available(llm):
        return dict(FALLBACK)
    timeout = _num(cfg, "ai.ds.timeout_sec", 15.0)
    try:
        # DeepSeekClient.complete 不接受 timeout 参数（超时在构造时设定），
        # 故用 asyncio.wait_for 施加调用级超时，避免 TypeError 被吞成永久降级。
        import asyncio
        text = await asyncio.wait_for(
            llm.complete(
                "你是量化信号质量裁判，只输出 JSON。", build_prompt(snapshot, symbol),
            ),
            timeout=timeout,
        )
        parsed = parse_output(text or "", cfg)
        # 【2026-08-15 三处对齐-3b】值域可观测：DeepSeek 返回的 3 参数经 parse_output
        # 已做钳制(max/min)，但钳制意味着模型给了越界值(异常)。记录 warning 便于
        # 发现 prompt/模型漂移，避免"假闭环"——回退值静默生效却无人知晓。
        import logging
        log = logging.getLogger(__name__)
        try:
            _raw = parse_json_block(text or "") or {}
            if isinstance(_raw, dict):
                for _k, _lo, _hi in (("fake_prob", 0.0, 1.0),
                                     ("ai_sl_coeff", 0.8, 1.5),
                                     ("continuity_score", 0, 100)):
                    if _k in _raw:
                        try:
                            _v = float(_raw[_k])
                            if _v < _lo or _v > _hi:
                                log.warning(
                                    "DeepSeek out-of-range (%s): %s=%.4f (clamped to [%s,%s])",
                                    symbol, _k, _v, _lo, _hi,
                                )
                        except (TypeError, ValueError):
                            log.warning(
                                "DeepSeek non-numeric (%s): %s=%r (treated as missing)",
                                symbol, _k, _raw[_k],
                            )
        except Exception:
            pass
        return parsed
    except Exception as exc:
        # 超时/限流/失败 → 降级默认（caller 回退缓存或纯 HEXP）
        import logging
        logging.getLogger(__name__).warning(
            "DeepSeek async output failed (%s): %s — fallback to HEXP-only",
            symbol, exc,
        )
        return dict(FALLBACK)


def calibrate_lm_score(
    lm_score: float | None,
    ds_out: dict | None,
    cfg: dict,
) -> dict:
    """产出运行期闭环用的 C_ai —— 【单源：仅 LightGBM】。

    【2026-08-18 解耦】DeepSeek 与 LightGBM 评分解耦：
      运行期 C_ai 只取 LightGBM 概率分（lm_score 0~100），DeepSeek 的 fake_prob
      不再参与任何加权融合，回归设计文档 §2.1/§2.4 定位——
      「DeepSeek 仅作后台异步刷新数据源 / 训练期校准，非实时决策依赖」。

      DeepSeek 的赋能改由【离线训练管线】承载：
        build_labels --ds-calibrate → labels.csv 的 ds_calib_weight 列
        → train_signal_quality.py 用作 LightGBM 训练 sample_weight
      即 DeepSeek 通过「影响模型怎么学」间接校准 LightGBM，而非运行期改分。

      ds_out 参数保留（不改调用签名，向后兼容），仅用于观测落库：
      ds_score / ds_age_sec 仍返回，供 gate_decision 诊断与训练样本回溯，
      但绝不进入 c_ai 计算。

    降级链（解耦后仅两态）：
      1. LightGBM 票在场  → c_ai = lm，source="lm_only"
      2. LightGBM 票缺失  → c_ai=None，source="none"（上游 quality_gate 透传纯 HEXP）
      注：原 "fused" / "ds_only" 两态已废除——DeepSeek 单票不得独立否决信号。
    """
    import time as _time

    max_age = _num(cfg, "ai.fuse.ds_max_age_sec", 900.0)

    lm = None
    if lm_score is not None:
        try:
            lm = float(max(0.0, min(100.0, float(lm_score))))
        except (TypeError, ValueError):
            lm = None

    # DeepSeek 票：仅解析为观测值，不参与 c_ai
    ds = None
    ds_age = None
    if isinstance(ds_out, dict):
        fp = ds_out.get("fake_prob")
        if fp is not None:
            try:
                ds = float(max(0.0, min(1.0, float(fp)))) * 100.0
            except (TypeError, ValueError):
                ds = None
        ts = ds_out.get("ts")
        if ds is not None and ts is not None:
            try:
                ds_age = max(0.0, _time.time() - float(ts))
                if max_age > 0 and ds_age > max_age:
                    # 陈旧票：观测字段也置空，避免落库误导后续回溯
                    ds = None
            except (TypeError, ValueError):
                ds_age = None

    # 【解耦核心】c_ai 单源取 LightGBM，DeepSeek 不参与
    if lm is not None:
        c_ai, source = lm, "lm_only"
    else:
        c_ai, source = None, "none"

    return {
        "c_ai": round(c_ai, 2) if c_ai is not None else None,
        "source": source,
        "lm_score": round(lm, 2) if lm is not None else None,
        # 以下两项为纯观测（不影响裁决），供 gate_decision 诊断/训练回溯
        "ds_score": round(ds, 2) if ds is not None else None,
        "ds_age_sec": round(ds_age, 1) if ds_age is not None else None,
        "decoupled": True,
    }


async def read_ds_out(redis_client, symbol: str) -> dict | None:
    """读 DeepSeek 异步票 ai:ds:out:{symbol}；缺失/损坏返回 None（调用方降级）。"""
    try:
        raw = await redis_client.get(f"ai:ds:out:{symbol.upper()}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


async def _persist_ds(db_pool, symbol: str, out: dict, status: str, cached: bool = False):
    """落库 ds_output + runtime_event（纯观测，供报表①调用成功率 / 报表④）。

    db_pool 为 None 时静默跳过；任何异常不抛出（绝不影响 DeepSeek 主循环）。
    """
    if db_pool is None:
        return
    try:
        await db_pool.execute(
            "INSERT INTO hcm_ai.ds_output "
            "(symbol, fake_prob, ai_sl_coeff, continuity_score, status, cached, detail) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            symbol,
            out.get("fake_prob"),
            out.get("ai_sl_coeff"),
            out.get("continuity_score"),
            status,
            cached,
            None,
        )
        await db_pool.execute(
            "INSERT INTO hcm_ai.runtime_event (event_type, symbol, status, detail) "
            "VALUES ($1,$2,$3,$4)",
            f"ds_{status}",
            symbol,
            "ok" if status == "ok" else "fail",
            json.dumps({"fake_prob": out.get("fake_prob")}),
        )
    except Exception:
        pass


async def run_loop(
    symbol: str, get_snapshot, cfg: dict, redis_client, llm=None,
    cache_ttl_sec: int = 1800, interval_sec: int = 60,
    cfg_reader=None, enabled_reader=None, db_pool=None,
    trigger_key: Optional[str] = None, trigger_poll_sec: float = 2.0,
):
    """后台循环：规则触发调用 DeepSeek，写 Redis ai:ds:out:{symbol}（TTL）。

    【2026-08-15 改为规则触发】原实现是固定 while True + sleep(interval_sec) 的
    时间轮询（每 60s 必调 DeepSeek），浪费配额且与"实时决策"脱节。现改为事件驱
    动：只有当 Redis 触发标志 `ai:ds:trigger:{symbol}` 存在时才真正调用 DeepSeek，
    调用后删除标志；标志不存在时仅做极短轮询(trigger_poll_sec)等待触发，不调 AI。
    触发标志由 scheduler._read_ai_quality 在发现 DeepSeek 票 stale/缺失时写入。

    纪律：本循环绝不抛出（异常只记日志），绝不阻塞实时决策。
    改进（闭环所需）：
      - get_snapshot 支持同步/异步两种形态（scheduler 侧取快照多为 async）
      - enabled_reader 每轮热读 ai.ds.enabled → 面板关开关即停调用（不必重启）
      - cfg_reader 每轮热读 ai.* 配置 → 超时/权重热调生效
      - 失败不覆盖 Redis 缓存（保留上一次有效票，避免把好票冲成 None）
      - 异常打日志（原静默 pass 让 DeepSeek 故障完全不可观测）
    """
    import asyncio
    import inspect
    import logging
    import time as _time

    log = logging.getLogger(__name__)
    # 【B6·2026-08-17 根因修复】is_available 是 @property(返回 bool)，原写法
    # `getattr(llm,"is_available",lambda:False)()` 把 bool 当可调用 → 抛
    # `TypeError: 'bool' object is not callable` → run_loop 协程启动即崩溃，
    # DeepSeek 循环从未运行 → ai:ds:out 永远为空 → 融合恒 source=lm_only。
    # 复用本文件 _llm_available(llm)（已正确兼容 property/方法两种形态）。
    _llm_ok = _llm_available(llm)
    log.info("DeepSeek async loop (rule-triggered) started: symbol=%s trigger_key=%s "
             "api_key_available=%s", symbol, trigger_key, _llm_ok)
    if not _llm_ok:
        log.warning("DeepSeek api_key 未配置/不可用 → 校准将降级(continuity=%s, "
                    "ai_sl_coeff 回退 hexp.exec.sl_atr_mult)，不阻塞实时决策。",
                    _cfg.get("ai.ds.fallback_continuity", 50)
                    if isinstance(_cfg, dict) else 50)

    _trigger = trigger_key or f"ai:ds:trigger:{symbol.upper()}"

    while True:
        try:
            # 1) 热读配置与开关（面板改键即时生效，无需重启信号塔）
            _cfg = cfg
            if cfg_reader is not None:
                try:
                    _c = cfg_reader()
                    if inspect.isawaitable(_c):
                        _c = await _c
                    if isinstance(_c, dict) and _c:
                        _cfg = _c
                except Exception as exc:
                    log.debug("DeepSeek loop cfg read failed: %s", exc)

            _on = True
            if enabled_reader is not None:
                try:
                    _e = enabled_reader()
                    if inspect.isawaitable(_e):
                        _e = await _e
                    _on = bool(_e)
                except Exception as exc:
                    log.debug("DeepSeek loop enabled read failed: %s", exc)
                    _on = False

            if not _on:
                await asyncio.sleep(trigger_poll_sec)
                continue

            # 2) 规则触发：仅当触发标志存在才真正调 DeepSeek；否则短轮询等待。
            _fired = False
            if redis_client is not None:
                try:
                    _fired = bool(await redis_client.exists(_trigger))
                except Exception:
                    _fired = False
            if not _fired:
                await asyncio.sleep(trigger_poll_sec)
                continue
            # 消费触发标志（先删后跑，避免重复触发；若该次调用失败，
            # 下次 stale 检测会重新写入标志）。
            try:
                if redis_client is not None:
                    await redis_client.delete(_trigger)
            except Exception:
                pass

            # 3) 取 HEXP 快照（同步/异步 getter 均兼容）
            snap = get_snapshot(symbol)
            if inspect.isawaitable(snap):
                snap = await snap

            if snap:
                out = await produce_async_outputs(symbol, snap, _cfg, llm)
                # 4) 失败不覆盖缓存：仅当拿到有效 fake_prob 才写入，
                #    否则保留上一次有效票直到自然 TTL 过期（陈旧判定交融合侧 ds_max_age_sec）
                if out.get("fake_prob") is not None:
                    out["ts"] = _time.time()
                    await redis_client.set(
                        f"ai:ds:out:{symbol.upper()}", json.dumps(out), ex=cache_ttl_sec,
                    )
                    log.info(
                        "DeepSeek out %s: fake_prob=%.3f sl_coeff=%.2f continuity=%s",
                        symbol, float(out["fake_prob"]),
                        float(out.get("ai_sl_coeff") or 1.0),
                        out.get("continuity_score"),
                    )
                    await _persist_ds(db_pool, symbol, out, "ok")
                else:
                    log.debug("DeepSeek %s: no valid fake_prob → keep previous cache", symbol)
                    await _persist_ds(db_pool, symbol, out, "fail")
        except asyncio.CancelledError:
            log.info("DeepSeek async loop cancelled: %s", symbol)
            raise
        except Exception as exc:
            log.warning("DeepSeek async loop error (%s): %s", symbol, exc)
            await asyncio.sleep(trigger_poll_sec)
