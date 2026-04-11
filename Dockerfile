FROM python:3.11-slim

WORKDIR /app

# System deps for pymupdf and tesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install build tools first
RUN pip install --no-cache-dir --upgrade pip hatchling

# Copy sources and install dependencies
COPY . .
RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["sh", "-c", "set -e; echo '=== Running migrations ==='; alembic upgrade head; echo '=== Migrations done ==='; uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
