FROM python:3.12-slim

WORKDIR /app

# Install build tools for any C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

# Data directory — mount a volume here for persistence
RUN mkdir -p /data
ENV AGENTBRIDGE_DB_PATH=/data/messages.db

EXPOSE 7890

CMD ["uvicorn", "agentbridge.server:app", "--host", "0.0.0.0", "--port", "7890"]
