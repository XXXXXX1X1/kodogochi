FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kodogochi ./kodogochi
COPY config ./config
RUN useradd -u 10001 -m app && mkdir -p /data && chown app /data
USER app
ENV DB_PATH=/data/kodogochi.db BACKUP_DIR=/data/backups
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["uvicorn", "kodogochi.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
