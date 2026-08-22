#!/usr/bin/env python3
"""Lightweight multi-terminal bridge watchdog (terminal-driven, 禁用硬编码).

设计：扫描本机运行中的 MT5 终端（terminal64.exe），为每个终端拉起一个
`mt5_bridge.py --terminal-path=<真实路径>`。bridge 自身读取该终端「实际登录的账号」，
反查 PG 得到 account_id 并抢单实例锁——看门狗这里不出现任何账户 ID / 路径硬编码，
新增/启用/换账户零代码改动。

存活判定：看门狗记录「终端路径 → 已拉起 bridge 的 PID」，每轮用 OpenProcess 验证该
PID 是否仍存活（崩溃退出则 PID 消失 → 重新拉起）。简单可靠，不依赖脆弱的进程命令行
解析，也不依赖 Redis。
"""
import os
import time
import subprocess
import ctypes
import msvcrt
import asyncio
from ctypes import wintypes, Structure, c_void_p

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PY = r"C:\Python313\python.exe"
BRIDGE = os.path.join(TOOLS_DIR, "mt5_bridge.py")
CHECK_INTERVAL = 20
LOG = os.path.join(TOOLS_DIR, "bridge_watchdog_pa.log")

# 账户状态闸门：看门狗拉桥前需识别 MT5 账户接入页的账户状态(is_active)，
# 停用的账户不能拉起/保留桥。数据源 = hcm_broker.accounts（与桥启动时发现账号同源）。
PG_DSN = os.getenv("PG_DSN", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# 账户状态缓存（避免每轮对 MT5 / PG 反复查询）
_ACCT_BY_TERM = {}      # norm_terminal_path -> (enabled, login, reason, ts)
_ACCT_ACTIVE = {}       # login -> (active: True/False/None, ts)
_ACCT_CACHE_TTL = 60    # 秒；控制「禁用→启用」后看门狗重新拉起的延迟上限

# 防污染退避：某终端桥连续失败/秒退时, 指数退避重拉,
# 避免"拉起即崩→再拉→污染终端会话→再崩"的疯狂重拉死亡螺旋。
FAST_FAIL_SECONDS = 60    # 拉起后在此时间内崩溃视为"秒退"(计入失败计数)
RECOVERY_SECONDS = 300    # 稳定运行超过此时间则清零失败计数(视为已恢复)
BACKOFF_BASE = 30         # 退避基数(秒)
BACKOFF_MAX = 600         # 退避上限(秒)

kernel32 = ctypes.windll.kernel32

# Windows 命名互斥体 — 内核级单实例, 进程死亡自动释放, 杜绝双 watchdog 并存(看门狗打架根因)。
# 旧代码完全无此单实例守护: 一旦双 launcher/旧进程残存, 两 watchdog 互抢桥锁→频闪/断桥。
_WATCHDOG_MUTEX_HANDLE = None


def _acquire_watchdog_singleton():
    """内核级单实例锁。若已有存活 watchdog 持有互斥体则返回 False(本实例应退出)。"""
    global _WATCHDOG_MUTEX_HANDLE
    try:
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "Global\\hcm_bridge_watchdog_singleton")
        if not h:
            return True  # 创建失败则放行(降级, 不阻断)
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _WATCHDOG_MUTEX_HANDLE = h  # 持句柄防 GC; 进程退出(含崩溃)时内核自动释放
        return True
    except Exception:
        return True


def _backoff_seconds(streak):
    """连续失败 streak 次后的退避时长(秒)。streak<=0 返回 0。"""
    if streak <= 0:
        return 0
    return min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (streak - 1)))


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as lf:
            lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def scan_terminals():
    """返回本机运行中的 terminal64.exe / terminal.exe 镜像真实路径列表。"""
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == -1:
        return []
    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.CHAR * 260),
        ]
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    ret = kernel32.Process32First(snapshot, ctypes.byref(pe))
    found = []
    while ret:
        name = pe.szExeFile.decode("ascii", "ignore").lower()
        pid = pe.th32ProcessID
        if "terminal" in name:
            h = kernel32.OpenProcess(0x1000, False, pid)
            if h:
                buf = ctypes.create_string_buffer(4096)
                size = wintypes.DWORD(4096)
                if kernel32.QueryFullProcessImageNameA(h, 0, buf, ctypes.byref(size)):
                    found.append(buf.value.decode("mbcs", "ignore"))
                kernel32.CloseHandle(h)
        ret = kernel32.Process32Next(snapshot, ctypes.byref(pe))
    kernel32.CloseHandle(snapshot)
    return found


def is_alive(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    kernel32.CloseHandle(h)
    return True


def _terminate_bridge(pid):
    """强制结束孤儿桥进程（终端已关闭但桥仍因 MT5 重连循环存活的场景）。
    仅用于本 watchdog 监管的桥；杀掉后 Redis 键 TTL 自然释放锁/存活键，
    自愈 clear_bridge_keys 可立即清理。"""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass


def _norm_path(p):
    return (p or "").rstrip(chr(92)).rstrip(chr(47)).lower()


def _mt5_login(terminal_path):
    """瞬时连 MT5 读取当前登录账号（读后立刻 shutdown，不影响桥自身连接）。
    失败/未登录返回 None。"""
    try:
        import MetaTrader5 as mt5
    except Exception:
        return None
    try:
        if not mt5.initialize(path=terminal_path):
            return None
        info = mt5.account_info()
        login = int(info.login) if info else None
        try:
            mt5.shutdown()
        except Exception:
            pass
        return login
    except Exception:
        return None


async def _pg_account_row(login):
    import asyncpg
    conn = await asyncpg.connect(PG_DSN, timeout=5)
    try:
        return await conn.fetchrow(
            "SELECT account_id, is_active FROM hcm_broker.accounts "
            "WHERE account_number=$1",
            login,
        )
    finally:
        await conn.close()


def _login_for_terminal(terminal_path):
    """反查某终端登录账号：优先读桥写入的 Redis 存活键(避免与桥争抢 MT5 连接)，
    退路连 MT5 读取。"""
    p = _norm_path(terminal_path)
    try:
        import redis
        r = redis.Redis.from_url(REDIS_URL)
        for key in r.scan_iter("bridge:terminal_for:*"):
            try:
                val = r.get(key)
                if val and _norm_path(val.decode("utf-8", "ignore")) == p:
                    return int(key.decode("utf-8", "ignore").rsplit(":", 1)[-1])
            except Exception:
                continue
    except Exception:
        pass
    return _mt5_login(terminal_path)


def _account_active(login):
    """查 hcm_broker.accounts.is_active（只按 login，不连 MT5）。
    返回 True(启用) / False(停用或未注册) / None(DB 故障, 未知态)。"""
    now = time.time()
    if login in _ACCT_ACTIVE:
        act, ts = _ACCT_ACTIVE[login]
        if now - ts < _ACCT_CACHE_TTL:
            return act
    try:
        row = asyncio.run(_pg_account_row(login))
    except Exception:
        _ACCT_ACTIVE[login] = (None, now)
        return None
    if not row:
        _ACCT_ACTIVE[login] = (False, now)
        return False
    act = bool(row["is_active"])
    _ACCT_ACTIVE[login] = (act, now)
    return act


def _account_check(terminal_path):
    """该终端登录账户是否允许被看门狗拉桥（识别 MT5 账户接入页的 is_active）。
    返回 (enabled, login, reason)。
    enabled=False：未登录 / 未注册 / 停用(is_active=false)。
    DB 故障 → 保守放行(enabled=True)，让桥自身决策，避免误杀正常账户。"""
    p = _norm_path(terminal_path)
    now = time.time()
    if p in _ACCT_BY_TERM:
        en, lg, why, ts = _ACCT_BY_TERM[p]
        if now - ts < _ACCT_CACHE_TTL:
            return en, lg, why
    login = _mt5_login(terminal_path)
    if login is None:
        _ACCT_BY_TERM[p] = (False, None, "no-mt5-login", now)
        return False, None, "no-mt5-login"
    active = _account_active(login)
    if active is False:
        _ACCT_BY_TERM[p] = (False, login, "disabled", now)
        return False, login, "disabled"
    # active 为 True 或 None(未知/DB故障) → 放行
    why = "ok" if active else "unknown-or-db-error"
    _ACCT_BY_TERM[p] = (True, login, why, now)
    return True, login, why


def discover_bridge_pids():
    """返回 {norm_terminal_path: pid}，覆盖本机所有存活且带 --terminal-path 的
    mt5_bridge 进程。用于 spawn 前认领既有桥，避免重复拉起导致锁冲突秒退
    （频闪根因）。等价于"查 bridge:instance:lock:<login> 是否已被存活 pid 持有"——
    既有桥进程必然持锁，按 terminal 维度直接认领更简洁（无需 login 映射）。
    """
    result = {}
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter 'Name=''python.exe''' | "
             "Select-Object ProcessId,CommandLine | "
             "ForEach-Object { ($_.ProcessId.ToString() + '|' + ($_.CommandLine -join '')) }"],
            stderr=subprocess.DEVNULL, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        ).decode("utf-8", "ignore")
        for line in out.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            pid_s, cmd = line.split("|", 1)
            marker = "--terminal-path="
            if marker not in cmd:
                continue
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            if not is_alive(pid):
                continue
            idx = cmd.find(marker)
            # 取 marker 后至行尾的完整路径串(含空格安全): 不再用 .split()[0] 截断,
            # 否则 "C:\Program Files\..." 会被截成 "C:\Program" 导致认领恒失配→重复拉起抢锁秒退(频闪根因)。
            rest = cmd[idx + len(marker):]
            val = rest.strip().strip(chr(34)).strip(chr(39)).strip()
            result[_norm_path(val)] = pid
    except Exception:
        # 探测失败(如 powershell 超时/异常)→ 返回 None 而非空 dict,
        # 主循环对 discovered is None 保守跳过 spawn(探测失败≠无桥)。
        return None
    return result


def _ensure_bridge_deps():
    """确保桥运行所需第三方模块(MetaTrader5/redis/requests)可用。
    workbuddy 自管理 python 被重装/重置时会清空 site-packages → 桥 import 必崩 → 断桥。
    每次 spawn 前先探测，缺失则自动 pip 安装（优先本地缓存 wheel），
    使看门狗具备依赖自愈能力，根治'缺模块断桥'复发。
    """
    probe = "import MetaTrader5, asyncpg, redis, requests"
    try:
        subprocess.run([PY, "-c", probe], capture_output=True, timeout=30, check=True)
        return True
    except Exception:
        pass
    try:
        subprocess.run(
            [PY, "-m", "pip", "install", "MetaTrader5", "asyncpg", "redis", "requests", "--no-input"],
            capture_output=True, timeout=300,
        )
    except Exception:
        pass
    try:
        subprocess.run([PY, "-c", probe], capture_output=True, timeout=30, check=True)
        return True
    except Exception:
        return False


def spawn(path):
    # 关键：绝不能用 DEVNULL —— 子进程若在 logging 初始化前崩溃，
    # traceback 会被吞、watchdog 误以为"拉起了其实没起来"反复空转。
    # 改为把子进程 stdout/stderr 重定向到 bridge_launch.log（append），
    # 既给合法文件句柄、又留档崩溃现场，便于闭环诊断。
    #
    # Windows 句柄修复(崩溃螺旋根因): 看门狗自身是 DETACHED 进程, 若仅用
    # stdout=fileobj 让 Python 自动继承 stdin/stdout 句柄, 孙子进程在
    # 无控制台上下文中可能拿到非法 stdin 句柄 → CRT 早期静默退出。
    # 显式用 STARTUPINFO + STARTF_USESTDHANDLES 把 hStdInput 置
    # INVALID_HANDLE_VALUE、hStdOutput/Error 指向同一日志文件句柄,
    # 并 bInheritHandles=True / close_fds=False, 彻底规避该坑。
    # 依赖自愈：workbuddy 重装/重置 embedded python 会清空 site-packages，
    # 导致桥 import MetaTrader5/redis 秒崩→断桥。spawn 前先探测，缺失则自动
    # pip 安装（优先本地缓存），根治'缺模块断桥'复发。
    if not _ensure_bridge_deps():
        _log(f"依赖自检失败，跳过 spawn（终端 {path}）；检查外网或手动 pip install")
        return None
    _log(f"spawn bridge for {path}")
    # 每个终端独立启动日志, 避免主号/跟单号 stdout 混写导致重拉现场被淹没
    term_tag = os.path.basename(os.path.dirname(path.rstrip("\\/"))) or os.path.basename(path)
    launch_log = os.path.join(TOOLS_DIR, f"bridge_launch_{term_tag}.log")
    with open(launch_log, "ab", buffering=0) as lf:
        lf.write(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] === launching bridge for {path} ===\n".encode("utf-8")
        )
    child_out = open(launch_log, "ab", buffering=0)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESTDHANDLES
    si.hStdInput = -1  # INVALID_HANDLE_VALUE (Windows 句柄常量)
    si.hStdOutput = msvcrt.get_osfhandle(child_out.fileno())
    si.hStdError = si.hStdOutput
    try:
        p = subprocess.Popen(
            [PY, BRIDGE, f"--terminal-path={path}"],
            # CREATE_NEW_CONSOLE 给子进程独立控制台+合法 std 句柄, 规避
            # "detached 祖父→detached 孙子"在 Windows 下加载原生 DLL(MetaTrader5)
            # 时句柄继承失败、进程静默退出的坑(首拉偶发成功/重拉必死)。
            # 子进程会有独立控制台窗口, 对常驻桥服务可接受。
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
            startupinfo=si,
            stdout=child_out,
            stderr=subprocess.STDOUT,
            close_fds=False,
        )
        pid = p.pid
    finally:
        child_out.close()
    # 拉起后短暂停顿并验证存活: 若秒退, 把崩溃现场(日志尾)记下来便于诊断,
    # 并返回 None 让主循环下一轮重试, 避免"拉起即死"被误判为成功。
    time.sleep(3)
    if not is_alive(pid):
        try:
            with open(launch_log, "r", encoding="utf-8", errors="ignore") as lf:
                tail = "".join(list(lf)[-20:])
            _log(f"WARN: pid {pid} for {path} died immediately. launch tail:\n{tail}")
        except Exception:
            pass
        return None
    _log(f"verified pid {pid} alive for {path}")
    return pid


def _hide_console_window():
    """隐藏看门狗自己的控制台窗口（仅 Windows）。

    进程仍持有控制台(非 DETACHED)，故子桥 import MetaTrader5 原生 DLL 稳定；
    仅窗口不可见。彻底修复"DETACHED 祖父→重拉桥静默死"的根因。
    """
    try:
        _k = ctypes.windll.kernel32
        _u = ctypes.windll.user32
        hwnd = _k.GetConsoleWindow()
        if hwnd:
            _u.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def main() -> None:
    _hide_console_window()
    supervised = {}      # terminal_path -> {"pid", "spawned_at", "login"}
    fail_streak = {}     # terminal_path -> 连续失败次数
    last_attempt = {}    # terminal_path -> 上次尝试拉起 epoch
    missing_cycles = {}  # terminal_path -> 连续未发现终端的轮数(防扫描瞬断误杀)
    _log("terminal-driven watchdog started (account-status gate ON: disabled accounts "
         "are not launched; no hardcoded accounts; anti-pollution backoff ON)")
    while True:
        try:
            now = time.time()
            terminals = scan_terminals()
            # 回收已死亡 pid, 并按存活时长更新失败计数
            for path in list(supervised.keys()):
                info = supervised[path]
                pid, spawned_at = info["pid"], info["spawned_at"]
                if not is_alive(pid):
                    age = now - spawned_at
                    fail_streak[path] = fail_streak.get(path, 0) + 1
                    if age >= RECOVERY_SECONDS:
                        _log(f"bridge {path} died after stable {age:.0f}s (streak={fail_streak[path]})")
                    else:
                        _log(f"FAST-FAIL: {path} bridge died after {age:.0f}s (streak={fail_streak[path]}); backing off")
                    supervised.pop(path, None)
            # 账户被停用 → 解托管(杀掉桥): 停用的账户不能由看门狗拉起/保留桥。
            # 识别 MT5 账户接入页的 is_active；DB 故障/未知态不误杀(active=None)。
            for path in list(supervised.keys()):
                info = supervised[path]
                pid = info["pid"]
                if not is_alive(pid):
                    continue  # 已在上一个回收循环处理
                login = info.get("login")
                if login is None:
                    login = _login_for_terminal(path)
                    info["login"] = login
                if login is None:
                    continue  # 无法判定 login, 交桥自身运行时闸门处理
                if _account_active(login) is False:
                    _log(f"account {login} disabled (is_active=false); "
                         f"decommissioning bridge pid={pid} for {path}")
                    _terminate_bridge(pid)
                    supervised.pop(path, None)
                    fail_streak.pop(path, None)
                    missing_cycles.pop(path, None)
            # 终端已关闭 → 反注册孤儿桥：跟单号账号被移除或终端退出时，桥仍因 MT5
            # 重连循环存活(不退出)，须由看门狗主动结束，释放单实例锁/存活键，
            # 使主号/跟单号数量实时准确、自愈链路能正确判定。连续 2 轮确认(防扫描瞬断误杀)。
            terminals_norm = {_norm_path(t) for t in terminals}
            for path in list(supervised.keys()):
                pid = supervised[path]["pid"]
                if _norm_path(path) in terminals_norm:
                    missing_cycles.pop(path, None)
                    continue
                if not is_alive(pid):
                    continue  # 已在上一个回收循环处理
                missing_cycles[path] = missing_cycles.get(path, 0) + 1
                if missing_cycles[path] >= 2:
                    _log(f"terminal gone for {path} (missing {missing_cycles[path]} cycles); decommissioning orphan bridge pid={pid}")
                    _terminate_bridge(pid)
                    supervised.pop(path, None)
                    fail_streak.pop(path, None)
                    missing_cycles.pop(path, None)
                else:
                    _log(f"terminal possibly gone for {path} (cycle {missing_cycles[path]}); confirming before decommission")
            discovered = discover_bridge_pids()
            if discovered is None:
                # 探测失败(非真无桥): 保守跳过本轮 spawn, 避免误拉重复桥抢锁秒退
                _log("discover_bridge_pids returned None (probe failed); skip spawn this cycle")
                continue
            for path in terminals:
                info = supervised.get(path)
                if info and is_alive(info["pid"]):
                    # 稳定运行达 RECOVERY_SECONDS 则清零失败计数
                    if (now - info["spawned_at"]) >= RECOVERY_SECONDS and fail_streak.get(path, 0) > 0:
                        fail_streak[path] = 0
                        _log(f"{path} stable >= {RECOVERY_SECONDS}s; fail streak reset")
                    continue  # 仍在监管中
                # 锁感知：本机已有存活桥服务于该终端（可能由本 watchdog 之外拉起），
                # 直接认领其 pid 进 supervised，跳过 spawn —— 避免重复拉起抢锁秒退（频闪）。
                disc_pid = discovered.get(_norm_path(path))
                if disc_pid and is_alive(disc_pid):
                    supervised[path] = {
                        "pid": disc_pid,
                        "spawned_at": time.time(),
                        "login": _login_for_terminal(path),
                    }
                    fail_streak[path] = 0
                    _log(f"lock-aware: adopting existing bridge pid={disc_pid} for {path}; skip spawn")
                    continue
                # 退避判定: 连续失败则指数退避, 不立即重拉
                streak = fail_streak.get(path, 0)
                if streak > 0:
                    wait = _backoff_seconds(streak)
                    elapsed = now - last_attempt.get(path, 0)
                    if elapsed < wait:
                        _log(f"backoff: {path} retry in {wait - elapsed:.0f}s (streak={streak})")
                        continue
                    if streak >= 3:
                        _log(f"ALERT: {path} bridge failed {streak} consecutive times; terminal may be polluted — backing off {wait}s before next attempt")
                # 账户状态闸门: 停用的账户不拉桥(识别 MT5 账户接入页的 is_active)
                enabled, login_at_spawn, why = _account_check(path)
                if not enabled:
                    _log(f"skip spawn: account disabled/inactive for {path} "
                         f"(login={login_at_spawn}, reason={why}); not launching bridge")
                    continue  # 不计入失败计数(非桥崩溃), 仅跳过本轮
                new_pid = spawn(path)
                last_attempt[path] = time.time()
                if new_pid is not None:
                    supervised[path] = {
                        "pid": new_pid,
                        "spawned_at": time.time(),
                        "login": login_at_spawn,
                    }
                    fail_streak[path] = 0
                    _log(f"supervising {path} (PID {new_pid})")
                else:
                    fail_streak[path] = fail_streak.get(path, 0) + 1
                    _log(f"spawn failed for {path} (streak={fail_streak[path]})")
        except Exception as exc:
            _log(f"watchdog cycle error: {exc}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
