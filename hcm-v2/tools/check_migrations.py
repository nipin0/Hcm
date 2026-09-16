#!/usr/bin/env python3
"""check_migrations.py — 迁移对账：把每个迁移文件**声明的对象**与 DB 实际状态逐项比对。

为什么需要它（2026-09-15 事故）：
  `deploy/migrations/0034_state_trigger_config.sql` 写了但**从未被应用**，导致
  ① 观测表缺 4 列 → `market_state_log` 落库**静默失败约 2 小时**；
  ② 9 个 `state.trigger.*` / `state.dir.*` 键在 PG 与 Redis 均不存在。
  根因是**没有任何迁移追踪表** ⇒ "写了迁移但没做"无法被自动发现。
  本工具即该缺口的补救：**静态解析 SQL 声明 → 查 DB 验证 → 报告/登记**。

支持解析的对象（覆盖本仓库迁移的实际形态）：
  · `INSERT INTO hcm_config.metadata` 里的**配置键**（VALUES 元组首元素）
  · `CREATE TABLE [IF NOT EXISTS] <schema.table>`
  · `ALTER TABLE <schema.table> ADD COLUMN [IF NOT EXISTS] <col>`（含一 ALTER 多列）

用法：
  python check_migrations.py                       # 只报告
  python check_migrations.py --record              # 把所有对象齐全的文件登记进追踪表
  python check_migrations.py --create-tracking     # 先建 hcm_config.schema_migrations

退出码：0 = 无缺失；1 = 存在缺失（便于 CI/管线卡住）。
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

import psycopg2

try:
    import redis as _redis
except Exception:                     # noqa: BLE001
    _redis = None

try:                      # 控制台可能是 GBK；不重设会在打印 ⚠ 等字符时崩溃
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:         # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
MIG_DIR = os.path.join(os.path.dirname(HERE), "deploy", "migrations")
DB_URL = os.environ.get("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")

# ── SQL 静态解析 ──────────────────────────────────────────────
_COMMENT_RX = re.compile(r"--[^\n]*")
# VALUES 元组首元素形如 ('some.dotted.key',
_KEY_RX = re.compile(r"\(\s*'([A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9_.]+)'\s*,")
_CREATE_TBL_RX = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)",
    re.I)
_ALTER_RX = re.compile(r"ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)", re.I)
_ADDCOL_RX = re.compile(r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.I)

# ── 移除感知（**关键**：后续迁移合法删除时不得误报为"缺失"）──
# 没有这层，工具会把 `0028_drop_dead_calibration.sql` 之类**故意删掉**的对象
# 长期报成缺失 → 噪音化 → 没人再看（比没有工具更糟）。
_DEL_KEY_RX = re.compile(
    r"DELETE\s+FROM\s+hcm_config\.metadata\b[^;]*?config_key\s*"
    r"(?:=\s*'([A-Za-z0-9_.]+)'|LIKE\s*'([A-Za-z0-9_.]+)%'|IN\s*\(([^)]*)\))",
    re.I | re.S)
_DROP_TBL_RX = re.compile(
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)", re.I)
_DROP_COL_RX = re.compile(
    r"ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.I)

TRACKING_DDL = """
CREATE SCHEMA IF NOT EXISTS hcm_config;
CREATE TABLE IF NOT EXISTS hcm_config.schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_keys      INTEGER,
    n_tables    INTEGER,
    n_columns   INTEGER,
    note        TEXT
);
COMMENT ON TABLE hcm_config.schema_migrations IS
    '迁移追踪：登记"其声明的对象已全部存在于 DB"的迁移文件。'
    '由 tools/check_migrations.py --record 写入（对账式登记，非执行记录）。';
"""


def strip_comments(sql: str) -> str:
    return _COMMENT_RX.sub("", sql)


def parse_declared(sql: str) -> dict:
    """从迁移 SQL 解析出它声明的对象：{keys, tables, columns:[(table,col)]}。"""
    s = strip_comments(sql)
    keys = sorted(set(_KEY_RX.findall(s)))
    tables = sorted(set(_CREATE_TBL_RX.findall(s)))
    columns = []
    # ALTER TABLE 与其后的 ADD COLUMN 配对（一个 ALTER 可带多列）
    for m in _ALTER_RX.finditer(s):
        tbl = m.group(1)
        # 该 ALTER 语句范围：到下一个 ';' 为止
        end = s.find(";", m.end())
        seg = s[m.end(): end if end != -1 else len(s)]
        for col in _ADDCOL_RX.findall(seg):
            columns.append((tbl, col))
    return {"keys": keys, "tables": tables, "columns": sorted(set(columns))}


def parse_removals(sql: str) -> dict:
    """解析该迁移**移除**的对象：{'keys': {精确...}, 'key_prefix': {...},
    'tables': {..}, 'columns': {('t','c')}}"""
    s = strip_comments(sql)
    exact, prefix = set(), set()
    for m in _DEL_KEY_RX.finditer(s):
        if m.group(1):
            exact.add(m.group(1))
        elif m.group(2):
            prefix.add(m.group(2))
        elif m.group(3):
            exact.update(re.findall(r"'([A-Za-z0-9_.]+)'", m.group(3)))
    return {
        "keys": exact,
        "key_prefix": prefix,
        "tables": set(_DROP_TBL_RX.findall(s)),
        "columns": set(_DROP_COL_RX.findall(s)),
    }


def verify(cur, decl: dict) -> dict:
    """返回各类缺失项。"""
    miss = {"keys": [], "tables": [], "columns": []}
    for k in decl["keys"]:
        cur.execute("SELECT 1 FROM hcm_config.metadata WHERE config_key=%s", (k,))
        if cur.fetchone() is None:
            miss["keys"].append(k)
    for t in decl["tables"]:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
        if not cur.fetchone()[0]:
            miss["tables"].append(t)
    for t, c in decl["columns"]:
        sch, _, tbl = t.partition(".")
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
            (sch, tbl, c))
        if cur.fetchone() is None:
            miss["columns"].append(f"{t}.{c}")
    return miss


def split_superseded(miss: dict, later_rm: dict) -> tuple[dict, dict]:
    """把缺失项拆成 (已由后续迁移移除, 真缺失)。"""
    gone = {"keys": [], "tables": [], "columns": []}
    real = {"keys": [], "tables": [], "columns": []}
    for k in miss["keys"]:
        if k in later_rm["keys"] or any(k.startswith(p) for p in later_rm["key_prefix"]):
            gone["keys"].append(k)
        else:
            real["keys"].append(k)
    for t in miss["tables"]:
        (gone if t in later_rm["tables"] else real)["tables"].append(t)
    for c in miss["columns"]:
        tbl, _, col = c.rpartition(".")
        (gone if (tbl, col) in later_rm["columns"] else real)["columns"].append(c)
    return gone, real


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=DB_URL)
    ap.add_argument("--dir", default=MIG_DIR)
    ap.add_argument("--record", action="store_true",
                    help="把对象齐全的文件登记进 hcm_config.schema_migrations")
    ap.add_argument("--create-tracking", action="store_true", help="先建追踪表")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "*.sql")))
    if not files:
        raise SystemExit(f"[fatal] 未找到迁移文件：{args.dir}")

    conn = psycopg2.connect(args.db_url)
    conn.autocommit = True
    cur = conn.cursor()

    # Redis 热层检查（**必须**）：桥直接 `hgetall("hcm:config:v2")` 取配置，
    # 故"PG 无、Redis 有"≠"缺失"（实例：`datasource.max_bars` 长期只存在于 Redis，
    # 见方案 §50.6）—— 那属于**真源缺失/漂移**，是另一类问题，不能与"完全没用上"混为一谈。
    r = None
    if _redis is not None:
        try:
            r = _redis.Redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379"),
                decode_responses=True, socket_timeout=3)
            r.ping()
        except Exception as exc:      # noqa: BLE001
            print(f"[warn] Redis 不可达（将只查 PG）：{exc}")
            r = None

    if args.create_tracking:
        cur.execute(TRACKING_DDL)
        print("[tracking] hcm_config.schema_migrations 已就绪")

    # 先解析**所有**文件的移除声明（供"后续迁移已合法删除"判定）
    removals: dict[str, dict] = {}
    for path in files:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            removals[os.path.basename(path)] = parse_removals(f.read())

    n_bad = 0
    n_sup = 0
    clean = []
    print(f"=== 迁移对账（{len(files)} 个文件）===")
    for path in files:
        name = os.path.basename(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            sql = f.read()
        decl = parse_declared(sql)
        if not (decl["keys"] or decl["tables"] or decl["columns"]):
            print(f"  [skip] {name}（未解析到可校验对象）")
            continue
        miss = verify(cur, decl)
        if sum(len(v) for v in miss.values()) == 0:
            clean.append((name, decl))
            continue
        # 该文件**之后**的迁移所做的移除（文件名排序即版本序）
        later = {"keys": set(), "key_prefix": set(), "tables": set(), "columns": set()}
        for other, rm in removals.items():
            if other > name:
                later["keys"] |= rm["keys"]
                later["key_prefix"] |= rm["key_prefix"]
                later["tables"] |= rm["tables"]
                later["columns"] |= rm["columns"]
        gone, real = split_superseded(miss, later)
        n_gone = sum(len(v) for v in gone.values())
        n_real = sum(len(v) for v in real.values())
        if n_gone:
            n_sup += 1
            print(f"  [已移除] {name}：{n_gone} 项由后续迁移合法删除（非缺失）"
                  + (f"，另有 {n_real} 项真缺失" if n_real else ""))
        if n_real == 0:
            clean.append((name, decl))
            continue
        n_bad += 1
        print(f"  [缺失] {name}"
              f"  键缺 {len(real['keys'])}/{len(decl['keys'])}"
              f"  表缺 {len(real['tables'])}/{len(decl['tables'])}"
              f"  列缺 {len(real['columns'])}/{len(decl['columns'])}")
        for k in real["keys"]:
            tag = ""
            if r is not None:
                try:
                    tag = ("  <- Redis 有值（PG 真源缺失/漂移，属另一类问题）"
                           if r.hexists("hcm:config:v2", k)
                           else "  <- PG/Redis 均无（**完全未落地**）")
                except Exception:      # noqa: BLE001
                    tag = ""
            print(f"         - 配置键 {k}{tag}")
        for t in real["tables"]:
            print(f"         - 表 {t}")
        for c in real["columns"]:
            print(f"         - 列 {c}")

    print(f"\n[summary] 对象齐全 {len(clean)} 个 / **有真缺失 {n_bad} 个**"
          f" / 含'后续已移除项' {n_sup} 个（非缺失）")

    if args.record and clean:
        for name, decl in clean:
            cur.execute(
                """INSERT INTO hcm_config.schema_migrations
                       (filename, n_keys, n_tables, n_columns, note)
                   VALUES (%s,%s,%s,%s,
                           'check_migrations 对账登记：声明的对象均已在 DB 中')
                   ON CONFLICT (filename) DO UPDATE SET
                       applied_at = now(),
                       n_keys = EXCLUDED.n_keys, n_tables = EXCLUDED.n_tables,
                       n_columns = EXCLUDED.n_columns""",
                (name, len(decl["keys"]), len(decl["tables"]), len(decl["columns"])))
        print(f"[tracking] 已登记/更新 {len(clean)} 个文件")

    conn.close()
    if n_bad:
        print("\n⚠ 存在缺失迁移 —— 逐项补齐后重跑本工具（`--record` 登记）。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
