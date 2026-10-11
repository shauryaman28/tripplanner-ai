Every night at 02:00 UTC the tests that need real services are run on `main` — the integration tests, on a real Postgres with pgvector and a real Redis, and the eval suite once there is one — and the result is added here as a comment: what ran, what passed, and which test failed and why.

A ❌ here means `main` is broken in a way the tests on every push do not see. The comment links to the run.

This issue is made and written to by `.github/workflows/nightly.yml`. To stop a daily notification, unsubscribe from it (it will still be written to). Closing it makes the next night open a new one.
