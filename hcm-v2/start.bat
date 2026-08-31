@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title HCM v2 一键启动
cd /d "%~dp0"

REM ===== ANSI 颜色（让正常状态一眼可见；缺 powershell 时自动降级为纯文本）=====
for /F %%C in ('powershell -NoProfile -Command "[char]27" 2^>nul') do set "ESC=%%C"
if not defined ESC set "ESC="
set "RST=%ESC%[0m"
set "GREEN=%ESC%[92m"
set "YEL=%ESC%[93m"
set "RED=%ESC%[91m"
set "CYAN=%ESC%[96m"
set "M_OK=[%GREEN% OK %RST%]"
set "M_W=[%YEL%WARN%RST%]"
set "M_X=[%RED%FAIL%RST%]"
set "M_I=[%CYAN%INFO%RST%]"
set "ITEM_FAIL=0"

REM ===== 启动模式解析 =====
REM 用法: start.bat [normal|restart|rebuild|clean]
REM   normal  (默认) : 智能启动，仅首次构建，之后复用镜像（无 --build，秒级）
REM   restart        : 停+起容器 + 彻底重置桥链（杀 launcher/看门狗/桥并重拉全新）
REM   rebuild        : docker compose down + up -d --build (代码/Dockerfile 变更后)
REM   clean          : 等同 rebuild（保留 pgdata/redisdata 数据卷，不加 -v）
set MODE=normal
if not "%~1"=="" set MODE=%~1

set NODE_DIR=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2
if not exist "%NODE_DIR%\node.exe" set NODE_DIR=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2
if not exist "%NODE_DIR%\node.exe" set NODE_DIR=C:\Program Files\nodejs
if not exist "%NODE_DIR%\node.exe" set NODE_DIR=C:\Program Files (x86)\nodejs

set PATH=%NODE_DIR%;%PATH%

echo.
echo   ============================================
echo         HCM v2 量化交易系统 - 启动中
echo   ============================================
echo.

echo [1/5] 检查 Docker...
docker info >nul 2>&1
if errorlevel 1 (
    call :ITEM "Docker Desktop" "FAIL" "未安装或未启动"
    echo       请先启动 Docker Desktop 后再运行此脚本
    echo.
    pause
    exit /b 1
)
call :ITEM "Docker Desktop" "OK" "运行中"

echo [2/5] 检查 Node.js...
node --version >nul 2>&1
if errorlevel 1 (
    call :ITEM "Node.js" "FAIL" "未安装"
    echo       请安装 Node.js 18+ 或检查 PATH 环境变量
    echo.
    pause
    exit /b 1
)
call :ITEM "Node.js" "OK" "已就绪"

echo [3/5] 检查 .env...
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        call :ITEM ".env 配置" "OK" "已从模板创建"
    ) else (
        call :ITEM ".env 配置" "FAIL" ".env.example 不存在"
        pause
        exit /b 1
    )
) else (
    call :ITEM ".env 配置" "OK" "已存在"
)

echo [3.5/5] 预检 Redis AOF 完整性...
set REDIS_VOL=hcm-v2_redisdata
docker volume inspect %REDIS_VOL% >nul 2>&1
if errorlevel 1 (
    call :ITEM "Redis AOF 完整性" "OK" "卷不存在，跳过"
    goto aof_ok
)
docker run --rm -v %REDIS_VOL%:/data redis:7-alpine redis-check-aof /data/appendonlydir/appendonly.aof.manifest > "%TEMP%\aof_check.txt" 2>&1
findstr /C:"All AOF files and manifest are valid" "%TEMP%\aof_check.txt" >nul
if not errorlevel 1 (
    call :ITEM "Redis AOF 完整性" "OK" "正常"
    goto aof_ok
)
echo       %M_W% 检测到 Redis AOF 损坏，自动备份并修复...
if not exist "%~dp0backup" mkdir "%~dp0backup"
docker run --rm -v %REDIS_VOL%:/data -v "%~dp0backup":/backup redis:7-alpine sh -c "mkdir -p /backup/redisdata_bak_auto && cp -a /data/. /backup/redisdata_bak_auto/"
docker run --rm -v %REDIS_VOL%:/data redis:7-alpine sh -c "echo y | redis-check-aof --fix /data/appendonlydir/appendonly.aof.manifest"
call :ITEM "Redis AOF 完整性" "WARN" "检测到损坏，已自动修复"
:aof_ok

echo [4/5] 启动后端（模式: %MODE%）...
if "%MODE%"=="rebuild" goto do_rebuild
if "%MODE%"=="clean" goto do_rebuild
if "%MODE%"=="restart" goto do_restart

REM ===== 默认模式：复用已有镜像，不重建 =====
REM 注：日常开发中代码更新用 docker cp ，无需重建镜像。
REM    只有修改了 Dockerfile 或 requirements.txt 才需要 start.bat rebuild
echo       normal 模式 — 复用已有镜像...
docker compose up -d
goto compose_done

:do_rebuild
echo       重新构建全部镜像 (rebuild/clean 模式)...
docker compose down >nul 2>&1
set DOCKER_BUILDKIT=1
docker compose up -d --build
goto compose_done

:do_restart
echo       重启前先彻底重置桥保活链（杀 launcher+看门狗+桥，清旧锁/存活键）...
call :reset_bridge_chain
echo       仅重启容器，不重建镜像 (restart 模式)...
docker compose down >nul 2>&1
docker compose up -d
goto compose_done

:compose_done
if errorlevel 1 (
    call :ITEM "后端容器启动" "FAIL" "Compose 启动失败（端口占用/镜像拉取失败/磁盘空间不足）"
    echo.
    pause
    exit /b 1
)
call :ITEM "后端容器启动" "OK" "compose up 完成"
echo       等待就绪...

REM ===== 等待 PostgreSQL 健康（每 2s 探一次，上限 60s）=====
set /a n=0
set PG_OK=0
:wait_pg
timeout /t 2 /nobreak >nul
set /a n+=2
docker compose ps postgres 2>nul | findstr /i "healthy" >nul
if not errorlevel 1 (set PG_OK=1 & goto pg_ok)
if %n% lss 60 goto wait_pg
echo       %M_W% PostgreSQL 等待超时，继续...
:pg_ok
if %PG_OK%==1 (call :ITEM "PostgreSQL" "OK" "healthy") else (call :ITEM "PostgreSQL" "WARN" "等待超时，继续启动")

REM ===== 轮询等待后端 API (hcm-web) 健康，替代固定死等 =====
echo       等待后端 API 就绪...
set /a m=0
set WEB_OK=0
:wait_web
timeout /t 2 /nobreak >nul
set /a m+=2
docker compose ps hcm-web 2>nul | findstr /i "healthy" >nul
if not errorlevel 1 (set WEB_OK=1 & goto web_ok)
if %m% lss 45 goto wait_web
echo       %M_W% 后端 API 等待超时，继续...
:web_ok
if %WEB_OK%==1 (call :ITEM "后端 API (hcm-web)" "OK" "healthy") else (call :ITEM "后端 API (hcm-web)" "WARN" "等待超时，继续启动")

REM ===== 热修复恢复 — docker compose up -d 会重置容器为镜像版本，补回修补文件 =====
echo       恢复热修复文件...
set TMP_ERR=0
docker cp "%~dp0hcm-web\web\api\config.py"     hcm-v2-hcm-web-1:/app/web/api/config.py         2>nul || set TMP_ERR=1
docker cp "%~dp0hcm-web\web\api\dashboard.py"   hcm-v2-hcm-web-1:/app/web/api/dashboard.py       2>nul || set TMP_ERR=1
docker cp "%~dp0hcm-signal-tower\signal_tower\scoring_engine.py"  hcm-v2-hcm-signal-tower-1:/app/signal_tower/scoring_engine.py 2>nul || set TMP_ERR=1
docker cp "%~dp0hcm-signal-tower\signal_tower\scheduler.py"      hcm-v2-hcm-signal-tower-1:/app/signal_tower/scheduler.py     2>nul || set TMP_ERR=1
docker cp "%~dp0hcm-signal-tower\signal_tower\indicator_calculator.py" hcm-v2-hcm-signal-tower-1:/app/signal_tower/indicator_calculator.py 2>nul || set TMP_ERR=1
docker cp "%~dp0shared\signal_tower_defaults.py" hcm-v2-hcm-signal-tower-1:/app/shared/signal_tower_defaults.py 2>nul || set TMP_ERR=1
docker cp "%~dp0hcm-risk-engine\risk_engine\rule_chain.py"       hcm-v2-hcm-risk-engine-1:/app/risk_engine/rule_chain.py       2>nul || set TMP_ERR=1
if %TMP_ERR%==0 (call :ITEM "热修复文件恢复" "OK" "已完成") else (call :ITEM "热修复文件恢复" "WARN" "部分跳过（容器缺失？）")

REM ===== 前端 dist 部署 — 必须在容器启动后 cp 进去再重启才能注册 SPA 路由 =====
set FRONTEND_DIR=hcm-web\frontend
if exist "%FRONTEND_DIR%\dist\index.html" (
    echo       部署前端 dist...
    docker exec hcm-v2-hcm-web-1 mkdir -p /app/frontend/dist 2>nul
    docker cp "%FRONTEND_DIR%\dist\." hcm-v2-hcm-web-1:/app/frontend/dist/ 2>nul
    docker restart hcm-v2-hcm-web-1 >nul 2>&1
    echo       前端已部署，hcm-web 重启中...
    timeout /t 5 /nobreak >nul
)

REM ===== 启动 AI 质量评分 sidecar（LightGBM 实时打分，主机进程，不在容器）=====
REM sidecar 持续发布 hcm:live:hexp:ai:{sym}；停了则面板 AI 分显示"离线"。
REM 注意：sidecar 依赖 C:\Python313（含 lightgbm 等依赖），与桥独立进程。
REM 自启机制：HCM_AIScorerGuard 计划任务（register_ai_scorer_guard.ps1 注册）每 3 分钟 +
REM 登录时调用 ai_scorer_boot.ps1 做 OS 层守护（进程缺失自动重拉 + 模型/脚本完整性自愈）。
REM 本段仅作为 start.bat 手动运行时的显式启动入口，与计划任务互补。
echo.
echo [5.5/6] 启动 AI 质量评分 sidecar（quality_scorer.py）...
REM 变量前移（桥段也要用），避免前面引用为空
set BRIDGE_DIR=%~dp0tools
set PY_EXE=C:\Python313\pythonw.exe
set PYWIN=C:\Python313\pythonw.exe
set SCORER=%BRIDGE_DIR%\quality_scorer.py
REM 启动前先跑 OS 层守护脚本做预检自愈（模型/脚本缺失自动从 _baseline 还原）
powershell -NoProfile -ExecutionPolicy Bypass -File "%BRIDGE_DIR%\ai_scorer_boot.ps1" -Snapshot >nul 2>&1
set "ST_AI=OK" & set "DS_AI=已运行"
if not exist "%SCORER%" (
    set "ST_AI=FAIL" & set "DS_AI=quality_scorer.py 不存在"
    goto ai_done
)
REM 是否已在跑（sidecar 由 pythonw.exe 运行，探测必须查 pythonw 而非 python）
set "AI_RUN=0"
for /f "usebackq tokens=*" %%L in (`tasklist /fi "imagename eq pythonw.exe" /fo list ^| findstr "quality_scorer"`) do set "AI_RUN=1"
if !AI_RUN!==1 (
    set "ST_AI=OK" & set "DS_AI=已在运行，跳过"
    goto ai_done
)
REM 经 launcher 拉起（与桥保活同模式）：launcher 自带单实例互斥 + 运行中探测，
REM 避免 start.bat 并发/重复调用产生多 sidecar 进程；launcher 拉起即退出，
REM 由 _sidecar_running 保证全局只有一个 sidecar 进程。
set SCORER_LAUNCHER=%BRIDGE_DIR%\quality_scorer_launcher.py
if not exist "%SCORER_LAUNCHER%" (
    set "ST_AI=FAIL" & set "DS_AI=quality_scorer_launcher.py 不存在"
    goto ai_done
)
echo       启动 quality_scorer_launcher.py（单实例守护，日志: tools\models\quality_scorer.log）...
"%PY_EXE%" -c "import subprocess; subprocess.Popen([r'%PY_EXE%', r'%SCORER_LAUNCHER%'], creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, stdout=open(r'%BRIDGE_DIR%\models\launcher_scorer.log','a'), stderr=subprocess.STDOUT)" >nul 2>&1
timeout /t 4 /nobreak >nul
REM 轮询 Redis 键确认已发布（最多 10s）
set "AI_OK=0"
for /L %%T in (1,1,5) do (
    docker exec hcm-v2-redis-1 redis-cli GET "hcm:live:hexp:ai:XAUUSD" >nul 2>&1
    if not errorlevel 1 (
        set "AI_OK=1"
        goto ai_check_ok
    )
    timeout /t 2 /nobreak >nul
)
:ai_check_ok
if !AI_OK!==1 (
    set "ST_AI=OK" & set "DS_AI=已启动，等待首次发布（约 15s 内）"
) else (
    set "ST_AI=FAIL" & set "DS_AI=启动后未发布 AI 评分，查看 tools\models\quality_scorer.log"
)
:ai_done

REM ===== 启动 TimesFM 每日 T+1 增量抽取调度器（离线特征，B 方案架构层）=====
REM 调度器每日 21:30 UTC 增量抽取并落库 hcm_ai.timesfm_features（G0 影子，不进决策）；
REM 停了则样本停止积累，13 天后 §7.2 重测无数据。
REM 自启机制：HCM_TimesFMDailyGuard 计划任务（register_timesfm_daily_guard.ps1 注册）每 10 分钟 +
REM 登录时调用 timesfm_daily_boot.ps1 做 OS 层守护（进程缺失自动重拉 + 脚本/产物完整性自愈）。
REM 本段仅作为 start.bat 手动运行时的显式启动入口，与计划任务互补；launcher 自带单实例
REM 互斥 + 运行中探测，重复调用安全幂等（不会因 start.bat 并发产生多个调度器）。
echo.
echo [5.6/6] 启动 TimesFM 每日 T+1 增量抽取调度器（timesfm_daily_scheduler.py）...
set "TF_DIR=%BRIDGE_DIR%"
set "TF_PY=D:\.venv_timesfm\Scripts\python.exe"
set "TF_SCHED=%TF_DIR%\timesfm_daily_scheduler.py"
set "TF_LAUNCHER=%TF_DIR%\timesfm_daily_launcher.py"
set "ST_TF=OK" & set "DS_TF=运行中"
REM 启动前先跑 OS 层守护脚本做预检自愈（脚本/PCA/检索库缺失自动从 _baseline 还原）
powershell -NoProfile -ExecutionPolicy Bypass -File "%TF_DIR%\timesfm_daily_boot.ps1" -Snapshot >nul 2>&1
if not exist "%TF_SCHED%" (
    set "ST_TF=FAIL" & set "DS_TF=timesfm_daily_scheduler.py 不存在"
    goto tf_done
)
if not exist "%TF_LAUNCHER%" (
    set "ST_TF=FAIL" & set "DS_TF=timesfm_daily_launcher.py 不存在"
    goto tf_done
)
REM 经 launcher 拉起（与桥保活同模式）：launcher 自带单实例互斥 + 运行中探测
"%TF_PY%" -c "import subprocess; subprocess.Popen([r'%TF_PY%', r'%TF_LAUNCHER%'], creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, stdout=open(r'%TF_DIR%\_logs\timesfm_daily_launcher.log','a'), stderr=subprocess.STDOUT)" >nul 2>&1
timeout /t 4 /nobreak >nul
REM 轮询 Redis 心跳确认调度器已上线（最多 10s）
set "TF_OK=0"
for /L %%T in (1,1,5) do (
    docker exec hcm-v2-redis-1 redis-cli GET "hcm:ai:timesfm:daily" >nul 2>&1
    if not errorlevel 1 (set "TF_OK=1" & goto tf_check_ok)
    timeout /t 2 /nobreak >nul
)
:tf_check_ok
if !TF_OK!==1 (set "ST_TF=OK" & set "DS_TF=已启动，待首次心跳") else (set "ST_TF=FAIL" & set "DS_TF=无心跳，查 tools\_logs\timesfm_daily.log")
:tf_done

REM ===== 启动桥保活 launcher（看门狗+双桥，数据驱动零硬编码，双层自愈）=====
REM 架构：start.bat 拉 launcher → launcher 拉 watchdog → watchdog 拉 mt5_bridge 按终端自动判定主/跟单
echo.
echo [6/6] 启动桥保活 launcher（自动扫描终端拉主号 + N 跟单桥 + 双层自愈）...
set BRIDGE_DIR=%~dp0tools
set PY_EXE=C:\Python313\python.exe
set LAUNCHER=%BRIDGE_DIR%\bridge_watchdog_launcher.py

REM ===== 必须项：MT5 终端 / 桥保活（状态最终在末尾总览统一打印）=====
set "ST_MT5=OK"    & set "DS_MT5=在运行"
set "ST_LAUNCH=OK" & set "DS_LAUNCH=已托管"
tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /I "terminal64.exe" >nul
if !errorlevel!==0 (set "ST_MT5=OK"    & set "DS_MT5=在运行") else (set "ST_MT5=FAIL" & set "DS_MT5=未运行，请先打开 MetaTrader 5 并登录账户")

REM 预检自愈 + 依赖安全网（tools\ 脚本曾凭空消失，先校验完整性再补依赖）
echo       预检桥栈脚本完整性 + 依赖...
powershell -NoProfile -ExecutionPolicy Bypass -File "%BRIDGE_DIR%\bridge_boot.ps1" -Snapshot >nul 2>&1
"%PY_EXE%" -m pip install MetaTrader5 asyncpg redis requests --no-input >nul 2>&1
if not exist "%LAUNCHER%" (
    set "ST_LAUNCH=FAIL" & set "DS_LAUNCH=bridge_watchdog_launcher.py 不存在"
    goto bridge_done
)
REM 若 Redis 中任一账户单实例锁已存在，说明桥栈已被 launcher/watchdog 托管，不重复拉
"%PY_EXE%" -c "import redis,sys; r=redis.Redis.from_url('redis://localhost:6379'); sys.exit(0 if any(r.scan_iter(match='bridge:instance:lock:*')) else 1)" >nul 2>&1
if !errorlevel!==0 (
    set "ST_LAUNCH=OK" & set "DS_LAUNCH=已在 launcher 托管中，跳过"
    goto bridge_done
)
echo       启动 bridge_watchdog_launcher.py（日志: tools\bridge_launcher.log）...
"%PY_EXE%" -c "import subprocess; subprocess.Popen([r'%PY_EXE%', r'%LAUNCHER%'], creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, stdout=open(r'%BRIDGE_DIR%\bridge_launcher.log','a'), stderr=subprocess.STDOUT)"
timeout /t 5 /nobreak >nul
"%PY_EXE%" -c "import redis,sys; r=redis.Redis.from_url('redis://localhost:6379'); sys.exit(0 if any(r.scan_iter(match='bridge:instance:lock:*')) else 1)" >nul 2>&1
if !errorlevel!==0 (
    set "ST_LAUNCH=OK" & set "DS_LAUNCH=已启动，将自动托管主号 + N 个动态跟单号桥并保活"
) else (
    set "ST_LAUNCH=FAIL" & set "DS_LAUNCH=启动失败，查看 tools\bridge_launcher.log"
)
:bridge_done

REM 看门狗不随 Windows 登录/开机自启（MT5 API 受 Windows 会话隔离，仅本 start.bat 手动启动有效）

echo.
echo   %CYAN%============================================%RST%
echo   %CYAN%       运行时状态总览（按链顺序）%RST%
echo   %CYAN%============================================%RST%
echo.

REM --- [1] 本地进程（桥保活链：launcher → watchdog → bridge）---
echo   %CYAN%[ 1 / 本地进程（桥保活链）]%RST%
set "LAUN_PID="
set "WD_PID="
set "BR_PIDS="
for /f "usebackq tokens=*" %%L in (`wmic process where "name='python.exe'" get commandline^,processid 2^>nul ^| findstr "bridge"`) do (
    for %%P in (%%L) do set "LASTP=%%P"
    echo %%L | findstr /I "bridge_watchdog_launcher" >nul && set "LAUN_PID=!LASTP!"
    echo %%L | findstr /I "bridge_watchdog_pa" >nul && set "WD_PID=!LASTP!"
    echo %%L | findstr /I "mt5_bridge" >nul && set "BR_PIDS=!BR_PIDS! !LASTP!"
)
if defined LAUN_PID (call :LINE "OK" "bridge_watchdog_launcher.py" "PID !LAUN_PID!") else (call :LINE "FAIL" "bridge_watchdog_launcher.py" "未运行（保活链断裂）")
if defined WD_PID (call :LINE "OK" "bridge_watchdog_pa.py" "PID !WD_PID!") else (call :LINE "FAIL" "bridge_watchdog_pa.py" "未运行（保活链断裂）")
if defined BR_PIDS (call :LINE "OK" "mt5_bridge.py" "PID!BR_PIDS!") else (call :LINE "INFO" "mt5_bridge.py" "未运行（launcher 将自动拉起）")
call :LINE "!ST_MT5!"    "MT5 终端"        "!DS_MT5!"
call :LINE "!ST_LAUNCH!" "桥保活 launcher" "!DS_LAUNCH!"
call :LINE "!ST_AI!"      "AI 评分 sidecar" "!DS_AI!"
call :LINE "!ST_TF!"       "TimesFM 调度器"   "!DS_TF!"
echo.

REM --- [2] Docker 容器（后端服务）---
echo   %CYAN%[ 2 / Docker 容器（后端服务）]%RST%
set "DC_OK=0"
for /f "tokens=1,*" %%a in ('docker ps --format "{{.Names}} {{.Status}}" 2^>nul') do (
    call :LINE "OK" "%%a" "%%b"
    set /a DC_OK+=1
)
if !DC_OK!==0 (call :LINE "FAIL" "Docker 容器" "无容器在运行")
echo.

REM --- [3] 关键应用 ---
echo   %CYAN%[ 3 / 关键应用 ]%RST%
docker compose ps postgres 2>nul | findstr /i "healthy" >nul && (call :LINE "OK" "PostgreSQL" "healthy") || (call :LINE "WARN" "PostgreSQL" "未 healthy")
docker exec hcm-v2-redis-1 redis-cli ping 2>nul | findstr /i "PONG" >nul && (call :LINE "OK" "Redis" "PONG") || (call :LINE "FAIL" "Redis" "无响应")
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri http://localhost:8000/docs -UseBasicParsing -TimeoutSec 6; if($r.StatusCode -eq 200){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>&1
if !errorlevel!==0 (call :LINE "OK" "后端 API（hcm-web）" "http://localhost:8000 健康") else (call :LINE "WARN" "后端 API（hcm-web）" "未响应（可能仍在启动）")
echo.

REM --- [4] Redis 桥状态键 ---
echo   %CYAN%[ 4 / Redis 桥状态键 ]%RST%
set "LOCKS="
for /f "tokens=*" %%k in ('docker exec hcm-v2-redis-1 redis-cli --scan --pattern "bridge:instance:lock:*" 2^>nul') do set "LOCKS=!LOCKS! %%k"
if defined LOCKS (call :LINE "OK" "单实例锁" "!LOCKS!") else (call :LINE "WARN" "单实例锁" "无（桥未托管）")
set "ALIVES="
for /f "tokens=*" %%k in ('docker exec hcm-v2-redis-1 redis-cli --scan --pattern "bridge:alive:*" 2^>nul') do set "ALIVES=!ALIVES! %%k"
if defined ALIVES (call :LINE "OK" "存活心跳" "!ALIVES!") else (call :LINE "WARN" "存活心跳" "无")
echo.

echo   %GREEN%============================================%RST%
echo   %GREEN%            启动完成，状态总览%RST%
if !ITEM_FAIL! gtr 0 (
    echo.
    echo   %M_W% 必须项中有 !ITEM_FAIL! 项异常（见上方红色 FAIL），请先排查再继续
) else (
    echo.
    echo   %GREEN% 所有必须项均已通过，系统处于正常状态%RST%
)
echo.
echo   %CYAN%-------- 访问入口 --------%RST%
echo       后端 API  : http://localhost:8000
echo       前端面板  : http://localhost:8000
echo       API 文檔  : http://localhost:8000/docs
echo.
echo   %CYAN%-------- 日志位置 --------%RST%
echo       MT5 桥日志    : D:\HCM_ASST\hcm-v2\tools\bridge.log
echo       跟单/主号桥 日志: D:\HCM_ASST\hcm-v2\tools\bridge_*.log
echo       AI 评分 sidecar 日志: D:\HCM_ASST\hcm-v2\tools\models\quality_scorer.log
echo.
echo   %M_I% 桥链为独立进程（DETACHED），关闭本窗口不会停止桥/看门狗；
echo        如需停止请运行 stop.bat 或手动结束 bridge_watchdog_launcher.py
echo   ============================================
echo.
pause
goto :eof

REM ===== 桥链彻底重置（杀 launcher+看门狗+桥 + 清 Redis 键）=====
:reset_bridge_chain
echo       [桥链重置] 终止 launcher / watchdog / 桥进程...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'bridge_watchdog|mt5_bridge' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
echo       [桥链重置] 清理 Redis 桥状态键（保留 bridge:processed:* 防重复单）...
for %%P in (bridge:instance:lock:* bridge:alive:* bridge:follow:circuit:* bridge:control:* bridge:mirror_pending_close:* bridge:zone_pending:*) do (
    for /f "tokens=*" %%k in ('docker exec hcm-v2-redis-1 redis-cli --scan --pattern "%%P" 2^>nul') do (
        docker exec hcm-v2-redis-1 redis-cli DEL %%k >nul 2>&1
    )
)
timeout /t 3 /nobreak >nul
echo       [桥链重置] 完成，准备重拉全新桥链
goto :eof

REM ===== 必须项状态打印子程序（统一格式： [标记] 名称 —— 说明）=====
:ITEM
set "I_ST=%~2"
if "%I_ST%"=="FAIL" set /a ITEM_FAIL+=1
call :LINE "%~2" "%~1" "%~3"
goto :eof

REM ===== 统一状态行打印（格式： [标记] 名称<按显示宽度对齐> 信息）=====
REM 显示宽度：ASCII 记 1、中文/全角记 2，避免中文名列错位
:LINE
set "L_ST=%~1"
set "L_NAME=%~2"
set "L_INFO=%~3"
if "%L_ST%"=="OK"   set "L_MARK=%M_OK%"
if "%L_ST%"=="FAIL" set "L_MARK=%M_X%"
if "%L_ST%"=="WARN" set "L_MARK=%M_W%"
if "%L_ST%"=="INFO" set "L_MARK=%M_I%"
if not defined L_MARK set "L_MARK=%M_I%"
for /f "delims=" %%P in ('powershell -NoProfile -Command "$n=$env:L_NAME; $w=0; foreach($c in $n.ToCharArray()){ $w+= if(([int]$c)-gt 255){2}else{1} }; $pad=[math]::Max(0,32-$w); ($n+(' '*$pad)+'#')"') do set "L_PADDED=%%P"
set "L_NAMEP=!L_PADDED:~0,-1!"
echo     !L_MARK! !L_NAMEP!!L_INFO!
goto :eof
