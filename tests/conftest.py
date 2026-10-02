"""Point the app at a dedicated `<db>_test` database for integration runs.

This must run before `app.db.session` is imported (it builds its engine at
import time), so the integration fixtures can truncate tables freely without
ever touching dev data.
"""

import os

if os.getenv("RUN_INTEGRATION"):
    from tests.database import use_database

    use_database("_test")
