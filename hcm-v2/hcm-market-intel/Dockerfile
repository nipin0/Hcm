FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY shared /app/shared
COPY hcm-market-intel/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY hcm-market-intel/ /app/

HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8006/health')"

EXPOSE 8006

CMD ["python", "main.py"]
