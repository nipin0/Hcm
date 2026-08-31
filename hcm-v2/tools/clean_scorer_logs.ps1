$logDir = "D:\HCM_ASST\hcm-v2\tools\models"
$keepDays = 7
$cut = (Get-Date).AddDays(-$keepDays)
# 鍒犻櫎婊氬姩澶囦唤 quality_scorer.log.1 / .2 ... 瓒呰繃淇濈暀鏈?
Get-ChildItem -Path $logDir -File | Where-Object {
    $_.Name -like "quality_scorer.log.*" -and $_.LastWriteTime -lt $cut
} | Remove-Item -Force
# 涓绘棩蹇?quality_scorer.log 鐢?RotatingFileHandler 鑷锛屼笉鍒?
Write-Host ("[clean] removed rotated backups older than {0} days" -f $keepDays)
