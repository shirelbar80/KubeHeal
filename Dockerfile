# KubeHeal container image (Phase 4 — optional in-cluster deployment).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py .
COPY prompts/ ./prompts/
COPY kubeheal/ ./kubeheal/

# Config comes from env vars / a mounted Secret (NOT a baked-in .env).
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "kubeheal.main"]
