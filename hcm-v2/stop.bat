@echo off
chcp 65001 >nul
title HCM v2 停止服务

echo.
echo [INFO] 停止 Docker Compose 服务...
docker compose down
echo [OK] 所有服务已停止
echo.
pause
