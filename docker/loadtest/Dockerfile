# The image the load test runs (Phase 30): the image that is deployed, and one directory more.
#
#   docker build -f src/backend/Dockerfile -t tripplanner-backend:under-test .
#   docker build -f docker/loadtest/Dockerfile -t tripplanner-loadtest-backend .
#                                                    (python scripts/loadtest.py up does both, from the repo root)
#
# FROM the deployed image, so that what is measured is what is deployed: the same Python, the same
# pinned packages, the same `src/`, the same user. Added: loadtest/standins — what answers in place
# of the models and the providers, which cannot take load (loadtest/standins/__init__.py).
#
# This is the only image the stand-ins are in. railway.json builds src/backend/Dockerfile.
ARG DEPLOYED=tripplanner-backend:under-test
FROM ${DEPLOYED}

COPY loadtest/__init__.py ./loadtest/__init__.py
COPY loadtest/standins/ ./loadtest/standins/

# The deployed image's command, word for word, but for what is served: the app with the stand-ins in
# (loadtest/standins/app.py), which is `app.main:app` once it has put them there. It refuses to
# load against a database or a Redis that is not on this machine.
CMD ["sh", "-c", "python -m app.preflight && alembic upgrade head && exec uvicorn loadtest.standins.app:app --app-dir src/backend --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*' --timeout-graceful-shutdown 10"]
