FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY shared /app/shared
COPY hcm-risk-engine/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY hcm-risk-engine/ /app/

HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8007/health')"

EXPOSE 8007

CMD ["python", "main.py"]
