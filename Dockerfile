FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system fraudguard && adduser --system --ingroup fraudguard fraudguard

COPY requirements-api.txt .
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements-api.txt

COPY alembic.ini .
COPY migrations migrations
COPY scripts scripts
COPY src src

USER fraudguard
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"

CMD ["uvicorn", "src.app.api:app", "--host", "0.0.0.0", "--port", "8000"]
