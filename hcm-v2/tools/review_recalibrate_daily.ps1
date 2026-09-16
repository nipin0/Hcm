# review_recalibrate_daily.ps1 — 【P3 2026-09-11】评审器在线滚动校准 每日启动器
#
# 由计划任务 HCM_ReviewRecalibrate 每日 08:10 调用（与项目既有 *_launcher.ps1 约定一致：
# 计划任务只跑 .ps1，避免引号/重定向在 schtasks 命令行被 PowerShell 改写）。
#
# 行为：用 review_log 的**原始分** vs K线触达真值，滚动重拟合 isotonic 校准器。
# 安全：脚本自身在「无数据 / 样本不足 / 档位<8 / 单调性<=0」时安全跳过或拒绝替换；
#       只换校准器，不动模型权重；替换前保留 .prev 备份，可秒级回滚。
# 依赖：评审器需处于启用状态（ai.review.enabled=true）以产生 review_log 记录，
#       否则本任务每日仅打印一行 skip（无副作用）。

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = "C:\Python313\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"==== $stamp review_recalibrate start ====" | Out-File -FilePath "$root\review_recalibrate.log" -Append -Encoding utf8
& $py "$root\review_recalibrate.py" *>> "$root\review_recalibrate.log"
"==== exit=$LASTEXITCODE ====" | Out-File -FilePath "$root\review_recalibrate.log" -Append -Encoding utf8
