import os
import uuid

import pytest

from medterms.db import Database

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

# Set MEDTERMS_TEST_PG=postgresql://user@host/postgres to also run the tests on PostgreSQL.
PG_URL = os.environ.get("MEDTERMS_TEST_PG")


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path):
    if request.param == "sqlite":
        database = Database(f"sqlite:///{tmp_path / 'test.db'}")
        yield database
        database.close()
        return

    if not PG_URL:
        pytest.skip("MEDTERMS_TEST_PG not set")
    import psycopg

    name = f"medterms_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    base, _, _ = PG_URL.rpartition("/")
    database = Database(f"{base}/{name}")
    yield database
    database.close()
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {name}")
