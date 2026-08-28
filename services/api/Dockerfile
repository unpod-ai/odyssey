FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY packages/odyssey-core packages/odyssey-core
COPY packages/odyssey-schemas packages/odyssey-schemas
COPY data_preparation data_preparation
COPY training training
COPY evaluation evaluation
COPY services/api services/api

WORKDIR /app/services/api
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "odyssey_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
