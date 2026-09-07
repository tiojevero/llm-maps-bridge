# Backend API image for llm-places-finder.
# Build/run: docker compose up --build, then curl localhost:8000/places/search
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements first so dependency install is cached
# across rebuilds when only source files change.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Run as a non-root user inside the container.
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
COPY --chown=appuser:appuser api/ ./api/

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
