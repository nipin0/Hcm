#!/usr/bin/env python3
"""配置键登记巡检 —— 根治「配置了≠生效」。

背景（2026-09-11 发现的系统性缺陷）：
  某些模块的 cfg 并非直读配置中心，而是**先快照一张登记表**：
    · hexp_engine : `cfg = {k: await self._get(k) for k in _DEFAULTS}`（本文件 _reload_locked）
    · scheduler   : `_ai_cfg_dict()` 的 keys 白名单（quality_gate 的 cfg 来源）
  因此**未登记进登记表的键，配置中心/面板改了永远不生效**（cfg 恒无此键 →
  读取点的字面默认恒生效）。历史铁证：counter_block 日志恒显示 `th=0.50`
  （=字面默认），而 PG 当时=0.55。

本工具用 **AST**（非 grep，避免注释/字符串误报）比对：
  [1] hexp_engine: _DEFAULTS  vs  引擎实际读取点
  [2] web HEXP_KEYS(面板)  vs  引擎 _DEFAULTS（双向差集）
  [3] scheduler._ai_cfg_dict 白名单  vs  quality_gate 实际读取

用法：
  python config_key_audit.py            # 人类可读
  python config_key_audit.py --json     # 机器可读
退出码：0=无缺失；1=存在「读了但未登记」的键。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "hcm-signal-tower", "signal_tower", "hexp_engine.py")
QG = os.path.join(ROOT, "hcm-signal-tower", "signal_tower", "quality_gate.py")
SCHED = os.path.join(ROOT, "hcm-signal-tower", "signal_tower", "scheduler.py")
WEB_HEXP = os.path.join(ROOT, "hcm-web", "web", "api", "hexp.py")

KEY_RE = re.compile(r"^(hexp|ai|close|risk|signal_tower|co)\.")
GETTERS = ("get", "get_float", "get_int", "get_bool", "get_current", "get_str")


def parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def dict_keys(tree, name):
    """`name = {...}` 或 `name: T = {...}` 的常量字符串键集合。"""
    out = set()
    for node in ast.walk(tree):
        val = None
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) == name:
                    val = node.value
        elif isinstance(node, ast.AnnAssign):
            if getattr(node.target, "id", None) == name:
                val = node.value
        if isinstance(val, ast.Dict):
            for k in val.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    out.add(k.value)
    return out


def read_keys(tree, receivers=("cfg",)):
    """提取 cfg.get("k") / cfg["k"] / self._get("k") / _g(cfg,"k") 的键。"""
    out = set()

    def add(v):
        if isinstance(v, str) and KEY_RE.match(v):
            out.add(v)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            fname = node.func.attr
            base = getattr(node.func.value, "id", None)
            if fname in GETTERS:
                if base in receivers or base is None:
                    if node.args:
                        add(getattr(node.args[0], "value", None))
            if fname == "_get" and base == "self" and node.args:
                add(getattr(node.args[0], "value", None))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_g" and len(node.args) >= 2:
            add(getattr(node.args[1], "value", None))
        if isinstance(node, ast.Subscript) and getattr(node.value, "id", None) in receivers:
            add(getattr(node.slice, "value", None))
    return out


def ai_whitelist(tree):
    """scheduler._ai_cfg_dict() 内 keys 列表的字符串常量。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_ai_cfg_dict":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and isinstance(sub.value, (ast.List, ast.Tuple)):
                    for e in sub.value.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            out.add(e.value)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    e_tree, w_tree, s_tree, q_tree = (parse(ENGINE), parse(WEB_HEXP),
                                      parse(SCHED), parse(QG))
    defaults = dict_keys(e_tree, "_DEFAULTS")
    e_reads = read_keys(e_tree)
    panel = dict_keys(w_tree, "HEXP_KEYS")
    wl = ai_whitelist(s_tree)
    q_reads = read_keys(q_tree)

    res = {
        "engine_defaults": len(defaults),
        "engine_reads": len(e_reads),
        "engine_read_but_unregistered": sorted(e_reads - defaults),   # 致命：改了不生效
        "registered_but_unread": sorted(defaults - e_reads),
        "panel_only": sorted(panel - defaults),                        # 面板可改但引擎不读
        "engine_only": sorted(defaults - panel),                       # 引擎读但面板不可管
        "ai_whitelist": len(wl),
        "quality_gate_reads": len(q_reads),
        "qg_read_but_not_whitelisted": sorted(q_reads - wl),           # 恒读 CFG_FALLBACK
    }
    bad = len(res["engine_read_but_unregistered"]) + len(res["qg_read_but_not_whitelisted"])

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 1 if bad else 0

    print("=" * 78)
    print("[1] hexp_engine: _DEFAULTS =", res["engine_defaults"],
          "| engine reads =", res["engine_reads"])
    if res["engine_read_but_unregistered"]:
        print(f"    [X] 读了但【未登记 _DEFAULTS】= {len(res['engine_read_but_unregistered'])}"
              f" -> 配置写了不生效：")
        for k in res["engine_read_but_unregistered"]:
            print("       -", k)
    else:
        print("    [OK] 引擎读取的键全部已登记")
    print(f"    [!] 登记但本文件未见读取 = {len(res['registered_but_unread'])}")

    print("=" * 78)
    print("[2] web HEXP_KEYS =", len(panel), " vs engine _DEFAULTS =", len(defaults))
    print(f"    [!] 面板有/引擎无 = {len(res['panel_only'])}（面板改了引擎不读）")
    print(f"    [!] 引擎有/面板无 = {len(res['engine_only'])}（引擎读但面板不可管）")

    print("=" * 78)
    print("[3] scheduler._ai_cfg_dict 白名单 =", res["ai_whitelist"],
          "| quality_gate reads =", res["quality_gate_reads"])
    if res["qg_read_but_not_whitelisted"]:
        print(f"    [X] quality_gate 读了但不在白名单 ="
              f" {len(res['qg_read_but_not_whitelisted'])} -> 恒读 CFG_FALLBACK：")
        for k in res["qg_read_but_not_whitelisted"]:
            print("       -", k)
    else:
        print("    [OK] quality_gate 读取的键全部在白名单")
    print("=" * 78)
    print("RESULT:", "FAIL (存在未登记键)" if bad else "PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
