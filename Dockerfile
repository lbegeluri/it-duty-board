# IT Duty: small, non-root, read-only-friendly image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.main \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runs as uid 10001 (the compose file also sets user: "10001:10001").
USER 10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

# --preload: the app is imported once before the workers fork (creates the secret key safely).
# --worker-tmp-dir /dev/shm: workers need no writable disk (the container root is read-only).
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--preload", \
     "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-", "app.main:app"]
