"""Point the app at a dedicated `<db>_test` database for integration runs.

This must run before `app.db.session` is imported (it builds its engine at
import time), so the integration fixtures can truncate tables freely without
ever touching dev data.
"""

import os

if os.getenv("RUN_INTEGRATION"):
    from sqlalchemy import make_url

    from app.core.config import settings

    _url = make_url(settings.DATABASE_URL)
    if not _url.database.endswith("_test"):
        settings.DATABASE_URL = _url.set(database=f"{_url.database}_test").render_as_string(hide_password=False)
