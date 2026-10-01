FROM python:3.11-slim

WORKDIR /app

# Install requirements from project root
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Keep the repo layout: `app.*` lives in src/backend, the agent layer in src/ai
# (the frontend is excluded by .dockerignore).
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY src/ ./src/

EXPOSE 8000

# Run migrations then start the server
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --app-dir src/backend --host 0.0.0.0 --port 8000"]
