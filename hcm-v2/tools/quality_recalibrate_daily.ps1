# quality_recalibrate_daily.ps1 — 【C 项·2026-09-11】质量/方向/买点头 在线滚动校准 每日启动器
#
# 由计划任务 HCM_QualityRecalibrate 每日 11:20 调用（与 review_recalibrate_daily.ps1
# 同约定：计划任务只跑 .ps1，避免引号/重定向在 schtasks 命令行被 PowerShell 改写）。
#
# 行为：用 hcm_ai.ai_pred_raw（sidecar 每 M5 棒落的三头原始分）
#       join hcm_signal.signals → build_labels 的 labels.csv 真实标签，
#       滚动重拟合 quality/entry 单校准器 + direction 三分类字典，
#       原子替换 ai.lm.{calib,dir_calib,entry_calib}_path 指向的文件
#       → sidecar 靠 mtime 指纹热加载（A 项）自动生效。
#
# 安全：脚本自身在「无数据 / 样本不足(<min_samples) / 档位<min_levels / 单调性<=0」
#       时安全跳过或拒绝替换；只换校准器、不动模型权重；替换前保留 .prev.pkl 备份，
#       可秒级回滚。依赖：sidecar 处于 ai.lm.raw_record_enabled=true 以产生 ai_pred_raw。

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = "C:\Python313\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"==== $stamp quality_recalibrate start ====" | Out-File -FilePath "$root\quality_recalibrate.log" -Append -Encoding utf8
& $py "$root\recalibrate_quality.py" --labels "_artifacts/labels.csv" *>> "$root\quality_recalibrate.log"
"==== exit=$LASTEXITCODE ====" | Out-File -FilePath "$root\quality_recalibrate.log" -Append -Encoding utf8
