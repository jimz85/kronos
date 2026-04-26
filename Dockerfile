# Kronos Trading System Dockerfile
# ================================
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 appuser

# Create logs and data directories
RUN mkdir -p /app/logs /app/data && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Healthcheck using OKX time API
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f https://www.okx.com/api/v5/general/time || exit 1

# Default command
CMD ["python", "kronos_pilot.py"]
