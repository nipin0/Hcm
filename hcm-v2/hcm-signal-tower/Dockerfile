FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    # 【P2 2026-09-11】LightGBM 的 OpenMP 运行时（slim 基础镜像缺失 → import 报
    # OSError: libgomp.so.1；评审器进程内推理必需）。lightgbm 包本身见 requirements.txt。
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY shared /app/shared
COPY hcm-signal-tower/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY hcm-signal-tower/ /app/

HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/health')"

EXPOSE 8002

CMD ["python", "main.py"]
