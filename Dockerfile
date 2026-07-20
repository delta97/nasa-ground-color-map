FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home appuser \
    && mkdir -p /data/tile-cache \
    && chown -R appuser:appuser /data/tile-cache
USER appuser

ENV CACHE_DIR=/data/tile-cache
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "nasa_ground_color_map.main:app", "--host", "0.0.0.0", "--port", "8000"]
