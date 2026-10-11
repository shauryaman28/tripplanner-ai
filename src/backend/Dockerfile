# The backend as it is deployed (Phase 27): one image, configured by its environment alone.
#
#   docker build -f src/backend/Dockerfile -t tripplanner-backend .        # from the repo root
#
# No .env is copied in (.dockerignore keeps it out): a platform hands the settings over as
# environment variables, and `python -m app.preflight` says at once which are missing.
FROM python:3.11-slim

# APP_ENV: an image is something that is deployed. Development overrides it (docker-compose.yml
# passes .env), and then nothing below is strict.
ENV APP_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src/backend:/app

WORKDIR /app

# What the app needs (requirements.txt), in exactly the versions the test suites ran on
# (constraints.txt). Without the second file this line installs the newest of everything — which is
# how the first image came to run a SQLAlchemy and a sqlmodel no test here had ever seen.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

# Keep the repo layout: `app.*` lives in src/backend, the agent layer in src/ai
# (the frontend is excluded by .dockerignore).
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY src/ ./src/

# Nothing here needs to be root, so nothing is.
RUN useradd --system --create-home --uid 10001 app
USER app

EXPOSE 8000

# Check the settings, bring the database to the newest migration, serve.
#   ${PORT:-8000}   a platform says which port it will send traffic to (Railway: PORT); 8000 otherwise.
#   exec            uvicorn takes this shell's place, so the platform's "stop" (SIGTERM) reaches it —
#                   and the plans in flight are waited for instead of being cut off (app/main.py).
#   --proxy-headers the platform's proxy says which scheme and client the request really had.
#   --timeout-graceful-shutdown   open connections (event streams) get this long to close.
CMD ["sh", "-c", "python -m app.preflight && alembic upgrade head && exec uvicorn app.main:app --app-dir src/backend --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*' --timeout-graceful-shutdown 10"]
