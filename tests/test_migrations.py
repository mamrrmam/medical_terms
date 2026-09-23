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
    assert [v for (v,) in db.query("SELECT version FROM schema_version ORDER BY version")] == [1, 2, 3]
    assert db.table_exists("fee_schedule")
    db.init_schema()  # no-op the second time
    assert db.query("SELECT COUNT(*) FROM schema_version")[0][0] == 3


def test_canadian_payers(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'p.db'}")
    db.init_schema()
    provinces = {j for (j,) in db.query("SELECT jurisdiction FROM payer WHERE jurisdiction LIKE 'CA-%'")}
    assert provinces == {"CA-BC", "CA-AB", "CA-SK", "CA-MB", "CA-ON", "CA-QC", "CA-NB", "CA-NS", "CA-PE", "CA-NL", "CA-YT"}
    # every payer's fee and diagnosis vocabularies exist
    assert db.query(
        "SELECT COUNT(*) FROM payer p LEFT JOIN vocabulary v ON v.vocabulary_id = p.fee_vocabulary_id "
        "WHERE v.vocabulary_id IS NULL")[0][0] == 0
