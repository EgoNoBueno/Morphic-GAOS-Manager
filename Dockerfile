FROM python:3.11-slim

# Build timestamp: 2026-03-22T09:05:00Z (forces cache invalidation)
WORKDIR /app
COPY . .

RUN pip install --no-cache-dir .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
