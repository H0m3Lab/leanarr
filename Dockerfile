# Leanarr - seed-aware library shrinker (by H0m3Lab)
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY leanarr.py leanarr_worker.py Setup-LeanarrWorker.ps1 /app/
ENV PYTHONUNBUFFERED=1
EXPOSE 8282
HEALTHCHECK --interval=60s --timeout=10s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8282/health', timeout=5)"
ENTRYPOINT ["python3", "/app/leanarr.py"]
