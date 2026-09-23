import sqlite3

from medterms.db import Database


def test_upgrades_database_created_before_migrations(tmp_path):
    path = tmp_path / "old.db"
    from importlib import resources
    old = sqlite3.connect(path)
    old.executescript(resources.files("medterms").joinpath("migrations/001_core.sql").read_text())
    old.close()

    db = Database(f"sqlite:///{path}")
    db.init_schema()
    assert [v for (v,) in db.query("SELECT version FROM schema_version ORDER BY version")] == [1, 2]
    assert db.table_exists("fee_schedule")
    db.init_schema()  # no-op the second time
    assert db.query("SELECT COUNT(*) FROM schema_version")[0][0] == 2
