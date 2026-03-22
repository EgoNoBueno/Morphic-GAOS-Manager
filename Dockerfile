FROM python:3.11-slim

# Cache invalidation: pass --build-arg CACHE_BUST=$(date +%s) to force rebuild
ARG CACHE_BUST
LABEL cache_bust=${CACHE_BUST}

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
