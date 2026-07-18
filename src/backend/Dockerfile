FROM python:3.11-slim

WORKDIR /app

# Install requirements from project root
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy alembic config + migrations
COPY alembic.ini ./
COPY migrations/ ./migrations/

# Copy the backend application
COPY src/backend/ ./

EXPOSE 8000

# Run migrations then start the server
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
