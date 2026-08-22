"""continuity_engine.py — 持仓动态调仓（单独使用 AI 延续分 continuity_score）。

三档（continuity_score 0-100）：
  ≥ strong_min 强延续：放宽止盈 / 追踪止损 / 允许加仓复核
  weak_max ~ strong_min 中性：不动、不加仓、不调止盈
  < weak_max 弱延续：收紧止损 / 压缩止盈 / 锁定利润

纪律红线：
  - 默认 ai.cont.enabled=false + ai.cont.mode=log（仅记录），act（执行）需另行红线授权。
  - 本模块只产出「建议动作」，绝不直接改活仓；执行落点由 bridge/风控在 act 模式下另行消费。
"""

from __future__ import annotations

from typing import Any

CFG_FALLBACK: dict[str, Any] = {
    "ai.cont.enabled": False,
    "ai.cont.strong_min": 70,
    "ai.cont.weak_max": 49,
    "ai.cont.mode": "log",
}


def _num(cfg: dict, key: str):
    v = cfg.get(key, CFG_FALLBACK[key])
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(CFG_FALLBACK[key])


def classify(continuity: float, cfg: dict) -> dict:
    strong_min = _num(cfg, "ai.cont.strong_min")
    weak_max = _num(cfg, "ai.cont.weak_max")
    if continuity >= strong_min:
        band = "strong"
        advice = "放宽止盈 / 追踪止损 / 允许加仓复核"
    elif continuity >= weak_max + 1:
        band = "neutral"
        advice = "不动 / 不加仓 / 不调止盈"
    else:
        band = "weak"
        advice = "收紧止损 / 压缩止盈 / 锁定利润"
    return {"band": band, "advice": advice, "continuity": continuity}


def decide(continuity: float | None, cfg: dict, position: dict | None = None) -> dict:
    """主入口。返回建议动作（绝不执行）。continuity=None → 中性降级。"""
    enabled = str(cfg.get("ai.cont.enabled", CFG_FALLBACK["ai.cont.enabled"])).lower() in (
        "true", "1", "yes", "on")
    mode = str(cfg.get("ai.cont.mode", CFG_FALLBACK["ai.cont.mode"]))
    if not enabled or continuity is None:
        return {"action": "none", "band": "neutral", "advice": "AI 调仓未启用或延续分缺失", "mode": mode}
    c = float(max(0.0, min(100.0, continuity)))
    res = classify(c, cfg)
    res["mode"] = mode
    res["action"] = "log" if mode == "log" else "act"
    return res


if __name__ == "__main__":
    cfg = dict(CFG_FALLBACK)
    cfg["ai.cont.enabled"] = True
    for c, tag in [(80, "强延续"), (60, "中性"), (30, "弱延续")]:
        d = decide(c, cfg)
        print(f"continuity={c:<3} [{tag}] band={d['band']:<8} mode={d['mode']} → {d['advice']}")
    d = decide(None, cfg)
    print(f"continuity=None → {d['advice']}")
