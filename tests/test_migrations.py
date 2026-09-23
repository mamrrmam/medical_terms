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
    assert [v for (v,) in db.query("SELECT version FROM schema_version ORDER BY version")] == [1, 2, 3, 4]
    assert db.table_exists("fee_schedule")
    db.init_schema()  # no-op the second time
    assert db.query("SELECT COUNT(*) FROM schema_version")[0][0] == 4


def test_canadian_payers(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'p.db'}")
    db.init_schema()
    provinces = {j for (j,) in db.query("SELECT jurisdiction FROM payer WHERE jurisdiction LIKE 'CA-%'")}
    assert provinces == {"CA-BC", "CA-AB", "CA-SK", "CA-MB", "CA-ON", "CA-QC", "CA-NB", "CA-NS", "CA-PE", "CA-NL", "CA-YT"}
    # every payer's fee and diagnosis vocabularies exist
    assert db.query(
        "SELECT COUNT(*) FROM payer p LEFT JOIN vocabulary v ON v.vocabulary_id = p.fee_vocabulary_id "
        "WHERE v.vocabulary_id IS NULL")[0][0] == 0


def test_fee_schedule_rebuild_keeps_rows(tmp_path):
    """Migration 004 rebuilds fee_schedule and unit_value with wider keys; existing rows survive."""
    from importlib import resources
    path = tmp_path / "v3.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY)")
    for version in (1, 2, 3):
        name = next(f.name for f in resources.files("medterms").joinpath("migrations").iterdir()
                    if f.name.startswith(f"00{version}_"))
        old.executescript(resources.files("medterms").joinpath("migrations", name).read_text())
        old.execute("INSERT INTO schema_version VALUES (?)", (version,))
    old.execute("INSERT INTO concept (concept_id, vocabulary_id, code, name, domain) VALUES (1, 'NS_MSI', '03.03', 'Visit', 'procedure')")
    old.execute("INSERT INTO fee_schedule (payer_id, concept_id, modifier, effective_start, units) VALUES ('NS_MSI', 1, 'RO=HDIN', '2025-04-01', 12.5)")
    old.execute("INSERT INTO unit_value (payer_id, effective_start, amount_per_unit) VALUES ('NS_MSI', '2025-04-01', 2.90)")
    old.commit()
    old.close()

    db = Database(f"sqlite:///{path}")
    db.init_schema()
    assert db.query("SELECT modifier, section, units FROM fee_schedule") == [("RO=HDIN", "", 12.5)]
    assert db.query("SELECT unit_name, amount_per_unit FROM unit_value") == [("MSU", 2.9)]
