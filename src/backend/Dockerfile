FROM python:3.11-slim

WORKDIR /app

# Install Poetry
RUN pip install poetry --no-cache-dir

# Copy dependency files first (layer-cache friendly)
COPY pyproject.toml poetry.lock* ./

# Install deps without creating a venv (container is the env)
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root

# Copy source
COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
