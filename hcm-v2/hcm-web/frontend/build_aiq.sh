#!/bin/sh
set -e
cd /fe
echo "STEP extract" 
rm -rf /tmp/fe
mkdir -p /tmp/fe
tar cf - --exclude=node_modules --exclude=dist . | (cd /tmp/fe && tar xf -)
echo "STEP npmci"
cd /tmp/fe
npm ci --no-audit --no-fund > /fe/b3.log 2>&1
echo "STEP build"
node ./node_modules/vite/bin/vite.js build >> /fe/b3.log 2>&1
echo "BUILD_EXIT=$?" >> /fe/b3.log
echo "STEP copy"
rm -rf /fe/dist
cp -r /tmp/fe/dist /fe/dist
echo "COPY_DONE" >> /fe/b3.log
ls /fe/dist/assets | wc -l >> /fe/b3.log
echo "ALL_DONE" >> /fe/b3.log
