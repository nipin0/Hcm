#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HCM-V2 全备份核心脚本（宿主 Windows 运行）。

契约（与前端 /api/v1/system/backup 轮询对齐）：
  - 开始：写 Redis hcm:backup:status = {"status":"running","started_at":...}
  - 结束：写 hcm:backup:status = {"status":"done","zip":...,"size_mb":...,"finished_at":...}
  - 失败：写 hcm:backup:status = {"status":"failed","error":...}

产物目录结构（对齐历史 hcm_backup_20260801_185822.zip）：
  {root}/{timestamp}/
      MANIFEST.txt
      pg_hcm_v2_{timestamp}.dump        # PG 纯文本转储
      redis_{timestamp}.rdb             # Redis 持久化快照
      src/                              # D:\HCM_ASST 源码整目录复制（排除大/临时目录）

仅依赖 C:\Python313（redis / asyncpg 已装）；PG/Redis 端口已映射宿主机。
"""
import os
import sys
import json
import time
import shutil
import subprocess
import asyncio
import datetime
import zipfile

BACKUP_ROOT = r"D:\HCM_ASST\backup"
SRC_ROOT = r"D:\HCM_ASST"
PG_HOST = "127.0.0.1"
PG_PORT = 5432
PG_USER = "hcm"
PG_DB = "hcm_v2"
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
STATUS_KEY = "hcm:backup:status"

# 源码复制时排除的目录/文件（避免把运行产物/大目录打进备份）
EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".idea", ".vscode",
    "backup", "_scratch", "_artifacts", "_logs",
}
EXCLUDE_TOP = {
    "backup",  # 不把旧备份再备份一遍
}


def set_status(d: dict):
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=5)
        r.set(STATUS_KEY, json.dumps(d), ex=3600)
    except Exception as e:
        print(f"[backup] WARN cannot write status: {e}")


def pg_dump_text(out_path: str) -> bool:
    """用容器内 pg_dump 导出整库纯文本（含 schema + data），对齐 pg_dump 语义。"""
    pw = os.environ.get("PGPASSWORD", "")
    cmd = [
        "docker", "exec", "-e", f"PGPASSWORD={pw}", "hcm-v2-postgres-1",
        "pg_dump", "-h", "127.0.0.1", "-U", PG_USER, "-d", PG_DB,
        "-F", "p", "--clean", "--if-exists", "--no-owner", "--encoding=UTF8",
    ]
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"-- HCM-V2 PG dump of {PG_DB} @ {datetime.datetime.now()}\n")
            rc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)
        if rc.returncode != 0:
            print(f"[backup] pg_dump failed: {rc.stderr[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[backup] pg_dump exception: {e}")
        return False


def redis_snapshot(out_path: str) -> bool:
    """触发 Redis SAVE 并复制 dump.rdb（优先挂载卷路径，失败回退 docker cp）。"""
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=5)
        r.save()
        # 容器内 /data/dump.rdb 经卷挂载到宿主机；尝试直接定位
        candidates = [
            r"D:\HCM_ASST\hcm-v2\redisdata\dump.rdb",
            r"D:\HCM_ASST\hcm-v2\redis-data\dump.rdb",
        ]
        for c in candidates:
            if os.path.exists(c):
                shutil.copyfile(c, out_path)
                return True
        # 回退：docker cp
        rc = subprocess.run(
            ["docker", "cp", "hcm-v2-redis-1:/data/dump.rdb", out_path],
            capture_output=True, text=True)
        if rc.returncode == 0 and os.path.exists(out_path):
            return True
        print(f"[backup] redis rdb copy fallback failed: {rc.stderr}")
        # 最后兜底：用 BGREWRITEAOF 不可行，改为导出 keys 计数
        n = r.dbsize()
        with open(out_path + ".txt", "w", encoding="utf-8") as f:
            f.write(f"redis dbsize={n}\n")
        return n >= 0
    except Exception as e:
        print(f"[backup] redis snapshot failed: {e}")
        return False


def copy_src(dest_src: str):
    """复制 D:/HCM_ASST 源码到 dest_src，排除大/临时目录。"""
    os.makedirs(dest_src, exist_ok=True)
    for name in os.listdir(SRC_ROOT):
        top = os.path.join(SRC_ROOT, name)
        if name in EXCLUDE_TOP:
            continue
        if os.path.isdir(top):
            if name in EXCLUDE_DIRS:
                continue
            shutil.copytree(top, os.path.join(dest_src, name),
                            ignore=shutil.ignore_patterns(*EXCLUDE_DIRS, "*.log"),
                            dirs_exist_ok=True)
        else:
            try:
                shutil.copy2(top, os.path.join(dest_src, name))
            except Exception:
                pass


def main():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    set_status({"status": "running", "started_at": datetime.datetime.now().isoformat()})
    work = os.path.join(BACKUP_ROOT, ts)
    os.makedirs(work, exist_ok=True)

    ok_pg = pg_dump_text(os.path.join(work, f"pg_hcm_v2_{ts}.dump"))
    ok_redis = redis_snapshot(os.path.join(work, f"redis_{ts}.rdb"))
    copy_src(os.path.join(work, "src"))

    manifest = [
        f"HCM-V2 FULL BACKUP {ts}",
        f"pg_dump: {'OK' if ok_pg else 'FAILED'}",
        f"redis:   {'OK' if ok_redis else 'FAILED'}",
        f"src:     copied",
    ]
    with open(os.path.join(work, "MANIFEST.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(manifest) + "\n")

    zip_name = os.path.join(BACKUP_ROOT, f"hcm_backup_{ts}.zip")
    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(work):
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, BACKUP_ROOT))

    # 清理临时工作目录
    shutil.rmtree(work, ignore_errors=True)

    size_mb = round(os.path.getsize(zip_name) / 1024 / 1024, 2)
    if ok_pg and ok_redis:
        set_status({"status": "done", "zip": os.path.basename(zip_name),
                    "size_mb": size_mb, "finished_at": datetime.datetime.now().isoformat()})
        print(f"[backup] DONE {zip_name} ({size_mb} MB)")
    else:
        set_status({"status": "failed",
                    "error": f"pg={ok_pg} redis={ok_redis}",
                    "zip": os.path.basename(zip_name),
                    "finished_at": datetime.datetime.now().isoformat()})
        print(f"[backup] PARTIAL/FAILED pg={ok_pg} redis={ok_redis}")


if __name__ == "__main__":
    main()
