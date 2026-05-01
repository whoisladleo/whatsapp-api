FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for psycopg2
RUN apt-get update && apt-get install -y \
    postgresql-client \
    gcc \
    python3-dev \
    musl-dev \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x scripts/run.sh

# Environment variables
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=3s \
  CMD curl -f http://127.0.0.1:8000/health || exit 1

CMD ["./scripts/run.sh"]
