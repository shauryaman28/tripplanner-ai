"""The load test (Phase 30): what drives the app, and what stands in for the world outside it.

    k6/         the script k6 runs — one traveller's visit, as the trip page makes its requests
    standins/   what answers in place of the models and the providers while it runs

Nothing here is part of the app. The image that is deployed (src/backend/Dockerfile) copies `src/`,
the migrations and nothing else, so none of this is in it.
"""
