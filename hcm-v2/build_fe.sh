#!/bin/sh
set -e
FE=/host/hcm-web/frontend
echo "TAR_START $(date)" > /tmp/fe_build.log
rm -rf /tmp/fe && mkdir -p /tmp/fe
tar --exclude=node_modules --exclude=dist -cf - -C "$FE" . | tar -xf - -C /tmp/fe
echo "TAR_DONE" >> /tmp/fe_build.log
echo "NPM_START $(date)" >> /tmp/fe_build.log
cd /tmp/fe && npm ci >> /tmp/fe_build.log 2>&1
echo "NPM_DONE" >> /tmp/fe_build.log
node ./node_modules/vite/bin/vite.js build >> /tmp/fe_build.log 2>&1
echo "VITE_EXIT=$?" >> /tmp/fe_build.log
# 关键：不清空 dist 目录本身，只删其内容后复制（保留目录，避免 Windows 绑定挂载同步失效）
find "$FE/dist" -mindepth 1 -delete
cp -r /tmp/fe/dist/. "$FE/dist/"
echo "DIST_SYNCED $(date)" >> /tmp/fe_build.log
echo "DONE" >> /tmp/fe_build.log
