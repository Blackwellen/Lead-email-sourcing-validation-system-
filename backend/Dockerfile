FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxslt1.1 curl && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN python - <<'PY'
import tomllib, subprocess, sys
deps = tomllib.load(open("pyproject.toml","rb"))["project"]["dependencies"]
subprocess.check_call([sys.executable, "-m", "pip", "install", *deps])
PY
COPY . .
RUN useradd -r -u 10001 lead && mkdir -p /data && chown -R lead /data /app
USER lead
ENV LEAD_DATA_DIR=/data LEAD_EXPORT_DIR=/data/exports
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:8000/healthz || exit 1
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
